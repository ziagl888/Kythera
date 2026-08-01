"""T-2026-KYT-9050-062 — score legs the way the trailing arm would actually earn on them.

What was wrong with the previous measure
----------------------------------------
`tools/epd_short_generation_study.py` scores a leg as
``realised − index move over the whole holding window``. That reads as "how much of
the move did it capture", and in a trending tape it is unfair by construction: a
take-profit leg exits at TP1 while the market keeps running, so it can never capture
the full window. Over 2026-06 → 08 the index fell 50 % and nearly every SHORT leg came
out negative — a result that cannot separate "poor selection" from "TP truncates the
trend". (The LONG side is not affected: longs in a falling market hit SL, not TP.)

What this measures instead
--------------------------
Both sides under the SAME exit rule, which is also the rule the roster cares about:

  leg value      trailing exit (activation, x give-back) applied to the trade's own
                 coin path — i.e. what Bot 40 would have realised mirroring it
  benchmark      the identical trailing exit applied to the INDEX path over the same
                 window — what the arm would have earned shorting "the market"
  residual       leg − benchmark

Because the leg's own TP policy no longer enters either side, legs with different exit
styles become comparable, and the number answers the question actually being asked:
*would putting this leg in the trailing channel have made money relative to trailing
the market itself?*

The index carries a synthetic high/low
--------------------------------------
A trail fires on wicks, so benchmarking a wick-driven leg against a close-only index
would understate the benchmark and silently flatter every leg. The index is therefore
built with a median high-ratio and low-ratio per hour alongside the close, giving it an
OHLC-shaped path that the same `trail_exit` can walk.

Everything reused, nothing reimplemented (Regel 7): dedup + sane-move loading from
`wave_buildup_study.load_trades`, the trail rule from `trailing_slot_budget`
(`prior_peak` / `trail_exit` / `capture_at`), candles via `read_coin_wick`.

Honest limits, stated up front:
  * Inference is clustered on calendar days — these trades overlap heavily and nominal
    n treats one market move as many observations.
  * Shadow legs carry no slippage; their figures are upper bounds.
  * The index is not tradeable. It is a benchmark for *selection*, not an alternative
    the operator could have taken.

Read-only. A gate flip or a roster seat is an operator decision, not an output here.

Usage:
    python tools/short_leg_trail_value.py --direction SHORT --start 2026-06-01
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from collections import defaultdict

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from tools.trailing_slot_budget import capture_at, trail_exit  # noqa: E402
from tools.wave_buildup_study import load_trades  # noqa: E402

FEE_RT = 0.10
X_FRAC = 0.10  # give-back fraction — the operator's live setting
ACTIVATION = 2.0  # the live activation

#: Median hourly high/low/close ratio across the coin universe. Median, so one fresh
#: listing cannot become the market; LAG yields NULL on a symbol's first observation,
#: which is exactly why that observation contributes nothing.
SQL_INDEX_OHLC = """
    WITH r AS (
        SELECT open_time, high, low, close,
               LAG(close) OVER (PARTITION BY symbol ORDER BY open_time) AS prev
        FROM candles
        WHERE tf = %(tf)s AND is_closed AND open_time >= %(start)s
    )
    SELECT open_time,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY high  / prev),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY low   / prev),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY close / prev)
    FROM r
    WHERE prev IS NOT NULL AND prev > 0
    GROUP BY open_time
    ORDER BY open_time
"""


def build_index_ohlc(rows: list[tuple]) -> dict:
    """(ts, h_ratio, l_ratio, c_ratio) rows → arrays of (t, high, low, close) levels.

    The close level compounds the median close-ratio; the high and low of each bar are
    that bar's ratios applied to the PREVIOUS close level, so the synthetic bar keeps
    the real intrabar span rather than inventing one around the new close.
    """
    t, hi, lo, cl = [], [], [], []
    level = 1.0
    for ts, hr, lr, cr in rows:
        if hr is None or lr is None or cr is None:
            continue
        prev = level
        level = prev * float(cr)
        t.append(np.datetime64(ts.replace(tzinfo=None)))
        hi.append(prev * float(hr))
        lo.append(prev * float(lr))
        cl.append(level)
    return {
        "t": np.asarray(t, dtype="datetime64[ns]"),
        "h": np.asarray(hi, dtype=float),
        "l": np.asarray(lo, dtype=float),
        "c": np.asarray(cl, dtype=float),
    }


def benchmark_trail(index: dict, ot, ct, is_long: bool, x: float, activation: float) -> float | None:
    """Trailing outcome of shorting/longing the INDEX over one trade's window.

    Returns the unlevered %; None when the window is not covered. Entry is the index
    close at (or just before) the trade's open — the same convention the mirror uses
    when it enters at market.
    """
    if len(index["t"]) == 0:
        return None
    ot64 = np.datetime64(ot.replace(tzinfo=None) if getattr(ot, "tzinfo", None) else ot)
    ct64 = np.datetime64(ct.replace(tzinfo=None) if getattr(ct, "tzinfo", None) else ct)
    m = (index["t"] >= ot64) & (index["t"] <= ct64)
    if not m.any():
        return None
    hh, ll, cc = index["h"][m], index["l"][m], index["c"][m]
    e = float(cc[0])
    if e <= 0:
        return None
    fav = ((hh - e) / e * 100.0) if is_long else ((e - ll) / e * 100.0)
    adv = ((ll - e) / e * 100.0) if is_long else ((e - hh) / e * 100.0)
    # Fallback when the trail never arms: the window's close-to-close move, signed.
    hold = ((cc[-1] - e) / e * 100.0) if is_long else ((e - cc[-1]) / e * 100.0)
    k = trail_exit(fav, adv, x, activation)
    ru, _rl = capture_at(fav, x, k, 1.0, hold, hold)
    return ru


def tail_profile(values: list[float]) -> dict:
    """Is a leg's mean carried by the body of its trades, or by a handful of them?

    A leg that shorts after pumps lives on rare coins that collapse 30 %, so its MEAN
    can be several points while its MEDIAN sits near zero. That distinction decides
    whether a positive average is something an operator can expect to repeat, or an
    artefact of a few outliers that may not recur — which is why sign and rank can be
    trusted from the mean while the MAGNITUDE cannot.

    ``top5_share`` is the fraction of the total sum contributed by the five best
    trades. Above ~0.5 the leg is a lottery ticket, whatever its average says.
    """
    if not values:
        return {"n": 0, "mean": 0.0, "median": 0.0, "p25": 0.0, "p75": 0.0,
                "win_rate": 0.0, "top5_share": 0.0}
    s = sorted(values)
    total = sum(s)
    top5 = sum(sorted(values, reverse=True)[:5])
    return {
        "n": len(s),
        "mean": statistics.mean(s),
        "median": statistics.median(s),
        "p25": s[len(s) // 4],
        "p75": s[(3 * len(s)) // 4],
        "win_rate": 100.0 * len([v for v in s if v > 0]) / len(s),
        # A negative total would flip the ratio's sign and read as "no concentration";
        # the magnitude of the share is what matters, so compare against |total|.
        "top5_share": (top5 / abs(total)) if total != 0 else 0.0,
    }


def cluster_t(day_values: list[tuple]) -> tuple[int, float, float]:
    """t over per-DAY means. See epd_short_generation_study for the rationale."""
    by_day: dict = defaultdict(list)
    for day, value in day_values:
        by_day[day].append(value)
    means = [statistics.mean(v) for v in by_day.values()]
    if len(means) < 2:
        return len(means), (means[0] if means else 0.0), float("nan")
    m = statistics.mean(means)
    sd = statistics.stdev(means)
    if sd == 0:
        return len(means), m, float("inf")
    return len(means), m, m / (sd / math.sqrt(len(means)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--direction", default="SHORT", choices=("SHORT", "LONG"))
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--x", type=float, default=X_FRAC)
    ap.add_argument("--activation", type=float, default=ACTIVATION)
    ap.add_argument("--min-trades", type=int, default=25, help="legs below this are listed but flagged")
    args = ap.parse_args()
    is_long = args.direction == "LONG"

    from core.database import get_db_connection
    from tools.trailing_slot_budget import simulate

    conn = get_db_connection()
    try:
        print("loading deduped trades …", flush=True)
        trades = [t for t in load_trades(conn, ["ALL"], lev=1.0, start=args.start)
                  if t["dir"] == args.direction]
        print("  %d %s trades since %s" % (len(trades), args.direction, args.start), flush=True)

        print("simulating the trail on each leg …", flush=True)
        no_candle = simulate(conn, trades, lev=1.0, tf=args.tf, x=args.x, activations=[args.activation])
        print("  %d trades without candle coverage (kept their recorded close)" % no_candle, flush=True)

        print("building the index (median OHLC ratios) …", flush=True)
        with conn.cursor() as cur:
            cur.execute(SQL_INDEX_OHLC, {"tf": args.tf, "start": args.start})
            index = build_index_ohlc(cur.fetchall())
        print("  %d bars" % len(index["t"]), flush=True)
    finally:
        conn.close()

    per_leg: dict = defaultdict(list)
    uncovered = 0
    for t in trades:
        leg_val = t["trail"][args.activation][0]
        bench = benchmark_trail(index, t["ot"], t["ct"], is_long, args.x, args.activation)
        if bench is None:
            uncovered += 1
            continue
        per_leg[t["tag"]].append((t["ct"].date(), leg_val - FEE_RT, bench - FEE_RT))

    print()
    print("=" * 92)
    print("LEG vs MARKET, BOTH UNDER THE TRAILING EXIT  (act=%.0f %%, x=%.0f %%, fee %.2f %%)"
          % (args.activation, args.x * 100, FEE_RT))
    print("=" * 92)
    print("  %-14s %7s %6s %11s %11s %11s %9s %s"
          % ("leg", "n", "days", "leg/trade", "market", "RESIDUAL", "t clust", ""))
    rows = []
    for tag, vals in per_leg.items():
        legs = [v for _d, v, _b in vals]
        bench = [b for _d, _v, b in vals]
        res = [v - b for _d, v, b in vals]
        n_days, _m, t_cl = cluster_t([(d, v - b) for d, v, b in vals])
        rows.append((tag, len(vals), n_days, statistics.mean(legs), statistics.mean(bench),
                     statistics.mean(res), t_cl))
    for tag, n, nd, lv, bv, rv, t_cl in sorted(rows, key=lambda r: -r[5]):
        flag = "  (thin)" if n < args.min_trades else ""
        print("  %-14s %7d %6d %11.3f %11.3f %11.3f %9.2f%s" % (tag, n, nd, lv, bv, rv, t_cl, flag))

    print()
    print("=" * 92)
    print("IS THE MEAN CARRIED BY THE BODY OR BY A FEW TRADES?  (residual distribution)")
    print("=" * 92)
    print("  %-14s %7s %9s %9s %9s %9s %8s %11s"
          % ("leg", "n", "mean", "MEDIAN", "p25", "p75", "win%", "top5 share"))
    for tag, n, _nd, _lv, _bv, rv, _t in sorted(rows, key=lambda r: -r[5]):
        if n < args.min_trades:
            continue
        prof = tail_profile([v - b for _d, v, b in per_leg[tag]])
        warn = "  <== carried by outliers" if prof["top5_share"] > 0.5 else ""
        print("  %-14s %7d %9.3f %9.3f %9.3f %9.3f %8.0f %10.2f%s"
              % (tag, prof["n"], prof["mean"], prof["median"], prof["p25"], prof["p75"],
                 prof["win_rate"], prof["top5_share"], warn))
    print()
    print("  A mean far above its median, or a top5 share past ~0.5, means the average")
    print("  is a few collapsed coins rather than something to expect again. Sign and")
    print("  rank still carry; the MAGNITUDE does not.")

    print()
    print("  %d trade(s) dropped: window outside index coverage" % uncovered)
    print("  Both columns are the SAME trailing rule — the leg's own TP policy no longer")
    print("  enters either side, so legs with different exit styles are comparable.")
    print("  t clust = clustered on calendar days. Shadow legs carry no slippage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

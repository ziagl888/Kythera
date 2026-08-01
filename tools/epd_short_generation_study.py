"""T-2026-KYT-9050-062 — which EPD SHORT generation, if any, deserves to post?

Right now no EPD short posts at all. EPD2 SHORT is parked (audit: both directions
net-negative; its artefact `epd2_model_SHORT.pkl` does not exist either, so the leg is
effectively mute). EPD3 SHORT was parked on 2026-07-23 after T-032 measured net
−0.06 % over 3568 trades. EPD1 SHORT reads `live` in `core.shadow_gate` — but only
because no explicit row exists and the module defaults to LIVE, and nothing has
emitted that tag since Bot 10 renamed to `EPD_LEGACY_TAG = "EPD2"`.

Why a plain results comparison would be wrong
---------------------------------------------
EPD1 SHORT ran 2026-02-24 → 07-06 (46 729 trades). EPD3 SHORT started 07-15 (8 434).
**They never ran at the same time.** Whatever separates their numbers could be the
model or could be four months of different tape, and a head-to-head cannot tell those
apart. The first raw look at EPD3 SHORT since its park showed +0.229 %/trade at
t = +2.90 — and the entire gain sits in the week the market dumped (−0.179 before,
+0.469 after). Flipping a live gate on that would be flipping it on a regime wave.

So each trade is scored against what the market handed it over ITS OWN holding
window, using the same equal-weight altcoin index as `tools/trailing_arm_report.py`
(shared functions, not a second implementation — Regel 7). The residual is what is
left after the tape, and that is the only number comparable across two eras.

Two further corrections the naive read misses:

* **Clustered inference.** These trades overlap heavily — the same coins in the same
  hours, driven by one detector. Nominal n treats them as independent and inflates t.
  The verdict therefore uses a t clustered on calendar days (each day contributes one
  observation), which is the conservative reading.
* **Shadow trades carry no slippage.** A shadow leg's fill is assumed perfect. Every
  number here is therefore an upper bound on what the leg would have realised live.

The index is computed in SQL for volume (≈2.2 M candle rows would otherwise cross
into Python), and a parity pin feeds one fixture through both this query's semantics
and `build_index` so the two shapes cannot drift apart.

Read-only. No writes, no live effect. A gate flip and a roster seat are operator
decisions and are NOT part of this tool.

Usage:
    python tools/epd_short_generation_study.py
    python tools/epd_short_generation_study.py --since 2026-07-15 --direction SHORT
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.time import LEGACY_WRITER_TZ  # noqa: E402
from tools.trailing_arm_report import FEE_RT, index_move_pct, market_implied  # noqa: E402

#: `closed_ai_signals` cannot be read raw, and doing so is not a subtle mistake — it
#: is a spectacular one. The table carries a ~357 k-row duplicate backfill blob and
#: synthetic LEGACY migration prices. Reading EPD1 SHORT raw returns 46 729 trades at
#: a MEDIAN of +21.2 % and a March mean of +15.2 % over 42 084 rows: a money printer
#: that never existed. The deduped view of the same leg is 4 650 trades. Three filters,
#: identical to `tools/wave_buildup_study.load_trades`, make it readable:
#:
#:   1. DISTINCT ON (symbol, model, direction, open_time) — collapses the duplicates.
#:   2. status NOT ILIKE '%LEGACY%' — drops synthetic migration prices.
#:   3. |move| <= MAX_ABS_MOVE_PCT — an unlevered move past 100 % is a data bug.
#:
#: Plus the caller's `--since`, whose default skips the February blob outright.
#:
#: TZ: open_time/close_time are naive, stamped in the legacy writer zone. Converting
#: with `AT TIME ZONE <that zone>` — never the session TZ — is the `core/time.py`
#: contract, and it is what keeps the Feb→Aug span DST-correct across the March switch.
MAX_ABS_MOVE_PCT = 100.0

SQL_TRADES = """
    SELECT upper(model) AS tag,
           open_time  AT TIME ZONE %(tz)s AS opened,
           close_time AT TIME ZONE %(tz)s AS closed,
           entry, close_price
    FROM (
        SELECT DISTINCT ON (symbol, model, upper(btrim(direction)), open_time)
               model, direction, entry, close_price, open_time, close_time
        FROM closed_ai_signals
        WHERE upper(model) LIKE %(family)s
          AND upper(btrim(direction)) = %(dir)s
          AND close_price IS NOT NULL AND close_price > 0
          AND entry IS NOT NULL AND entry > 0
          AND close_time IS NOT NULL
          AND (status IS NULL OR status NOT ILIKE '%%LEGACY%%')
          AND open_time >= %(since)s
        ORDER BY symbol, model, upper(btrim(direction)), open_time,
                 close_time ASC, targets_hit DESC, status ASC
    ) d
    ORDER BY close_time
"""

#: Equal-weight altcoin index: the MEDIAN cross-sectional hourly return, compounded
#: by the caller. Median, so one freshly listed coin printing +300 % in its first hour
#: cannot become the market. `LAG` yields NULL on a symbol's first observation, which
#: is exactly why that observation contributes no return.
SQL_INDEX = """
    WITH r AS (
        SELECT open_time, close,
               LAG(close) OVER (PARTITION BY symbol ORDER BY open_time) AS prev
        FROM candles
        WHERE tf = '1h' AND is_closed AND open_time >= %(since)s
    )
    SELECT open_time, percentile_cont(0.5) WITHIN GROUP (ORDER BY close / prev - 1.0)
    FROM r
    WHERE prev IS NOT NULL AND prev > 0
    GROUP BY open_time
    ORDER BY open_time
"""


def compound(hourly: list[tuple]) -> list[tuple]:
    """(ts, median_return) rows → (ts, index_level). Mirror of build_index's tail."""
    level = 1.0
    out = []
    for ts, ret in hourly:
        if ret is None:
            continue
        level *= 1.0 + float(ret)
        out.append((ts, level))
    return out


def cluster_t(day_values: list[tuple]) -> tuple[int, float, float]:
    """t over per-DAY means: ``(n_days, mean_of_daily_means, t)``.

    Trades from one detector overlap massively — same coins, same hours — so treating
    each as an independent draw inflates t by roughly the square root of the cluster
    size. Collapsing to one observation per day is the conservative correction. Each
    day carries equal weight regardless of how many trades it held, which is standard
    for cluster inference and deliberately does NOT reproduce the per-trade mean.

    ``nan`` when fewer than two days are present: a single day cannot support a claim
    about a leg's edge, and returning 0.0 there would read like a measured null.
    """
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


def naive_t(values: list[float]) -> float:
    if len(values) < 2:
        return float("nan")
    sd = statistics.stdev(values)
    if sd == 0:
        return float("inf")
    return statistics.mean(values) / (sd / math.sqrt(len(values)))


def realised_pct(entry: float, close_price: float, is_long: bool) -> float | None:
    """Unlevered move of a finished source trade, signed by direction.

    None when the inputs are unusable or the move exceeds ``MAX_ABS_MOVE_PCT``: an
    unlevered move past 100 % is a data bug, not a trade, and averaging it in is how
    the raw table reports a median of +21 % per short.
    """
    if entry is None or close_price is None:
        return None
    entry, close_price = float(entry), float(close_price)
    if entry <= 0 or close_price <= 0:
        return None
    move = ((close_price - entry) / entry * 100.0) if is_long else ((entry - close_price) / entry * 100.0)
    if abs(move) > MAX_ABS_MOVE_PCT:
        return None
    return move


def overlap(span_a: tuple, span_b: tuple) -> int:
    """Days two generations were both alive. Zero means no head-to-head is possible."""
    lo = max(span_a[0], span_b[0])
    hi = min(span_a[1], span_b[1])
    return max(0, (hi - lo).days)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="2026-02-24", help="earliest close_time to include")
    ap.add_argument("--direction", default="SHORT", choices=("SHORT", "LONG"))
    ap.add_argument("--family", default="EPD%",
                    help="SQL LIKE over upper(model), e.g. 'EPD%%' or 'MIS%%' (default: EPD%%)")
    args = ap.parse_args()
    is_long = args.direction == "LONG"

    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_INDEX, {"since": args.since})
            index = compound(cur.fetchall())
            cur.execute(SQL_TRADES, {"tz": LEGACY_WRITER_TZ, "dir": args.direction,
                                     "since": args.since, "family": args.family})
            trades = cur.fetchall()
    finally:
        conn.close()

    print("index: %d hourly points, %s → %s, total %+.2f %%"
          % (len(index), index[0][0].date(), index[-1][0].date(),
             (index[-1][1] / index[0][1] - 1.0) * 100.0) if index else "index: EMPTY")

    per_gen: dict = defaultdict(list)
    dropped = 0
    for tag, opened, closed, entry, close_price in trades:
        r = realised_pct(entry, close_price, is_long)
        if r is None:
            dropped += 1
            continue
        mv = index_move_pct(index, opened, closed)
        per_gen[tag].append((opened, closed, r, mv))
    if dropped:
        print("dropped %d row(s) with an implausible move (>%.0f %% unlevered)" % (dropped, MAX_ABS_MOVE_PCT))

    spans = {t: (min(x[0] for x in v).date(), max(x[1] for x in v).date()) for t, v in per_gen.items()}

    print()
    print("=" * 80)
    print("DID THEY EVER RUN TOGETHER?  (no overlap ⇒ a head-to-head cannot separate")
    print("model from tape, and only the market-adjusted residual is comparable)")
    print("=" * 80)
    tags = sorted(per_gen)
    for i, a in enumerate(tags):
        for b in tags[i + 1:]:
            d = overlap(spans[a], spans[b])
            print("  %-6s %s  vs  %-6s %s   →  %d overlapping days%s"
                  % (a, spans[a], b, spans[b], d, "   ⚠ NONE" if d == 0 else ""))

    print()
    print("=" * 80)
    print("PER GENERATION  (%s, unlevered %%, fee %.2f%% round-trip)" % (args.direction, FEE_RT))
    print("=" * 80)
    print("  %-6s %7s %7s %9s %9s %10s %9s %9s"
          % ("tag", "n", "days", "net/trade", "market", "RESIDUAL", "t naive", "t clust"))
    for tag in tags:
        rows = per_gen[tag]
        scored = [(o, c, r, mv) for (o, c, r, mv) in rows if mv is not None]
        if not scored:
            print("  %-6s %7d   (no index coverage)" % (tag, len(rows)))
            continue
        res = [r - market_implied(mv, is_long) for (_o, _c, r, mv) in scored]
        by_day = [(c.date(), r - market_implied(mv, is_long)) for (_o, c, r, mv) in scored]
        n_days, clustered_mean, t_cl = cluster_t(by_day)
        print("  %-6s %7d %7d %9.3f %9.3f %10.3f %9.2f %9.2f"
              % (tag, len(scored), n_days,
                 statistics.mean([r for (_o, _c, r, _m) in scored]) - FEE_RT,
                 statistics.mean([market_implied(mv, is_long) for (_o, _c, _r, mv) in scored]),
                 statistics.mean(res), naive_t(res), t_cl))

    print()
    print("  Residual = realised − what the index handed a %s over the same window" % args.direction)
    print("  t clust = clustered on calendar days. Trust it over t naive: these trades")
    print("  overlap heavily, so nominal n treats one market move as many observations.")

    print()
    print("=" * 80)
    print("WEEK BY WEEK  (residual per trade — exposes regime dependence)")
    print("=" * 80)
    weeks: dict = defaultdict(dict)
    for tag in tags:
        for _o, c, r, mv in per_gen[tag]:
            if mv is None:
                continue
            wk = (c.date() - __import__("datetime").timedelta(days=c.weekday())).isoformat()
            weeks[wk].setdefault(tag, []).append(r - market_implied(mv, is_long))
    print("  %-12s %s" % ("week", "  ".join("%-16s" % t for t in tags)))
    for wk in sorted(weeks):
        cells = ""
        for tag in tags:
            v = weeks[wk].get(tag)
            cells += "%-16s  " % (("n=%-5d %+.3f" % (len(v), statistics.mean(v))) if v else "—")
        print("  %-12s %s" % (wk, cells))

    print()
    print("  Shadow legs carry no slippage — every figure above is an upper bound.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

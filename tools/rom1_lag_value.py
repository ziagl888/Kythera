"""T-2026-KYT-9050-065 — what does ROM1's re-forward lag cost the trailing arm?

The question
------------
ROM1 (bot 28) re-forwards a signal roughly three hours after the original leg opened
it, carrying the ORIGINAL ``open_time``. Measured on live rows: age at insert
p10 = 10 798 s, p50 = 10 799 s, p90 = 10 800 s — a hard band at exactly 3 h, i.e. a
rule, not spread. (A normal leg such as AIM2 SHORT sits at a median of 0 s.)

`tools/short_leg_trail_value.py` scored ROM1 SHORT at +0.86 residual, the cleanest
distribution in the field. That number is **not reachable by the mirror**: it was
computed over ``open_time → close_time``, the trade as the ORIGINAL leg lived it. A
mirror entering at market three hours later buys a different move — most of it has
already happened. Recommending ROM1 on that figure would repeat the TSM1 mistake in a
new shape.

So this re-scores ROM1 twice on the same trades:

    original    trail from the source entry at open_time      (what the leg earned)
    lagged      trail from the MARKET price at open_time+3h   (what the mirror gets)

and benchmarks both against the index trail over their own windows. The gap between
them is the price of the lag, and it is the only number relevant to a roster decision.

Why open_time + 3 h rather than the real insert stamp
-----------------------------------------------------
``closed_ai_signals`` carries no ``inserted_at``; only the live ``ai_signals`` does,
and it holds just the currently-open rows. The offset is a constant by measurement
(the band above), so it is applied as one — and ``--lag-hours`` exposes it so the
assumption can be varied rather than believed.

Read-only. No writes, no live effect.

Usage:
    python tools/rom1_lag_value.py --tag ROM1 --direction SHORT --start 2026-06-01
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import timedelta

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from tools.short_leg_trail_value import (  # noqa: E402
    SQL_INDEX_OHLC,
    benchmark_trail,
    build_index_ohlc,
    cluster_t,
)
from tools.trailing_slot_budget import capture_at, trail_exit  # noqa: E402
from tools.wave_buildup_study import load_trades, read_coin_wick  # noqa: E402

DEFAULT_FEE_RT = 0.10  # taker round-trip; --fee overrides (see T-065 fee realism)


def entry_at(series: dict, when) -> float | None:
    """Market price the mirror would pay: the first candle CLOSE at/after ``when``.

    None when the coin has no candle there — the mirror could not have entered either,
    so the trade drops out rather than being scored against an invented price.
    """
    if len(series["t"]) == 0:
        return None
    w = np.datetime64(when.replace(tzinfo=None) if getattr(when, "tzinfo", None) else when)
    m = series["t"] >= w
    if not m.any():
        return None
    return float(series["c"][m][0])


def trail_from(series: dict, entry: float, start, end, is_long: bool, x: float, act: float) -> float | None:
    """Unlevered trail outcome on a coin path between two instants at a given entry."""
    if entry is None or entry <= 0 or len(series["t"]) == 0:
        return None
    s = np.datetime64(start.replace(tzinfo=None) if getattr(start, "tzinfo", None) else start)
    e = np.datetime64(end.replace(tzinfo=None) if getattr(end, "tzinfo", None) else end)
    m = (series["t"] >= s) & (series["t"] <= e)
    if not m.any():
        return None
    hh, ll, cc = series["h"][m], series["l"][m], series["c"][m]
    fav = ((hh - entry) / entry * 100.0) if is_long else ((entry - ll) / entry * 100.0)
    adv = ((ll - entry) / entry * 100.0) if is_long else ((entry - hh) / entry * 100.0)
    hold = ((cc[-1] - entry) / entry * 100.0) if is_long else ((entry - cc[-1]) / entry * 100.0)
    k = trail_exit(fav, adv, x, act)
    ru, _rl = capture_at(fav, x, k, 1.0, hold, hold)
    return ru


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", default="ROM1")
    ap.add_argument("--direction", default="SHORT", choices=("SHORT", "LONG"))
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--x", type=float, default=0.10)
    ap.add_argument("--activation", type=float, default=2.0)
    ap.add_argument("--lag-hours", type=float, default=3.0,
                    help="measured re-forward offset (ROM1: a hard 10800 s band)")
    ap.add_argument("--fee", type=float, default=DEFAULT_FEE_RT,
                    help="round-trip fee in unlevered %% (0.10 = taker/taker worst case)")
    args = ap.parse_args()
    is_long = args.direction == "LONG"
    lag = timedelta(hours=args.lag_hours)

    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        trades = [t for t in load_trades(conn, ["ALL"], lev=1.0, start=args.start)
                  if t["dir"] == args.direction and t["tag"].upper() == args.tag.upper()]
        print("%s %s: %d deduped trades since %s" % (args.tag, args.direction, len(trades), args.start),
              flush=True)
        if not trades:
            return 0

        with conn.cursor() as cur:
            cur.execute(SQL_INDEX_OHLC, {"tf": args.tf, "start": args.start})
            index = build_index_ohlc(cur.fetchall())
        print("index: %d bars" % len(index["t"]), flush=True)

        by_coin: dict[str, list] = defaultdict(list)
        for t in trades:
            by_coin[t["sym"]].append(t)

        rows = []
        no_candle = 0
        for i, (sym, tl) in enumerate(sorted(by_coin.items()), 1):
            lo = min(t["ot"] for t in tl) - timedelta(hours=2)
            hi = max(t["ct"] for t in tl) + timedelta(hours=2)
            cd = read_coin_wick(conn, sym, lo, hi, args.tf)
            if len(cd["t"]) == 0:
                no_candle += len(tl)
                continue
            for t in tl:
                late_start = t["ot"] + lag
                if late_start >= t["ct"]:
                    # The source trade closed before the mirror would even have entered.
                    rows.append((t["ct"].date(), None, None, None, None, True))
                    continue
                orig = trail_from(cd, t["entry"], t["ot"], t["ct"], is_long, args.x, args.activation)
                late_entry = entry_at(cd, late_start)
                late = trail_from(cd, late_entry, late_start, t["ct"], is_long, args.x, args.activation)
                b_orig = benchmark_trail(index, t["ot"], t["ct"], is_long, args.x, args.activation)
                b_late = benchmark_trail(index, late_start, t["ct"], is_long, args.x, args.activation)
                rows.append((t["ct"].date(), orig, late, b_orig, b_late, False))
            if i % 100 == 0:
                print("  [%d/%d]" % (i, len(by_coin)), flush=True)
    finally:
        conn.close()

    expired = len([r for r in rows if r[5]])
    usable = [r for r in rows if not r[5] and None not in (r[1], r[2], r[3], r[4])]
    print()
    print("=" * 78)
    print("THE PRICE OF A %.0f h RE-FORWARD LAG  (%s %s, fee %.2f %%)"
          % (args.lag_hours, args.tag, args.direction, args.fee))
    print("=" * 78)
    print("  trades: %d | scorable: %d | source closed before the mirror could enter: %d"
          " | no candles: %d" % (len(trades), len(usable), expired, no_candle))
    if not usable:
        print("  nothing scorable — the lag exceeds the typical holding time outright.")
        return 0

    def _line(label, leg, bench):
        res = [x - y for x, y in zip(leg, bench)]
        nd, _m, t_cl = cluster_t([(d, r) for (d, *_rest), r in zip(usable, res)])
        print("  %-26s leg=%+8.3f  market=%+8.3f  residual=%+8.3f  t=%+6.2f"
              % (label, statistics.mean(leg) - args.fee, statistics.mean(bench) - args.fee,
                 statistics.mean(res), t_cl))
        return statistics.mean(res)

    r_orig = _line("as the SOURCE leg lived it", [r[1] for r in usable], [r[3] for r in usable])
    r_late = _line("as the MIRROR would get it", [r[2] for r in usable], [r[4] for r in usable])

    print()
    print("  cost of the lag: %+.3f %%-points per trade" % (r_late - r_orig))
    print("  %d of %d source trades (%.0f %%) closed before the mirror could even enter —"
          % (expired, len(rows), 100.0 * expired / max(len(rows), 1)))
    print("  those are pure loss of opportunity and are NOT in the averages above.")
    print()
    print("  Entry is the first candle close at/after open_time + lag (market entry, the")
    print("  operator's 2026-07-27 decision). Shadow rows carry no slippage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""T-2026-KYT-9050-065 — which legs should the trailing arm actually mirror?

One table, three sources that until now had to be read side by side:

  economics    net per trade after REAL fees and funding (tools/leg_net_economics)
  gate         live / shadow / silent / retired (core.shadow_gate)
  roster       whether the trailing arm mirrors it today (core.trailing_roster)

WRONG METRIC FOR A ROSTER DECISION — read this before acting on the DROP column
-------------------------------------------------------------------------------
`leg_net_economics` scores a leg under ITS OWN take-profit/stop exit. The trailing arm
does not take that exit — it takes the SIGNAL and applies its own trail. A leg can
therefore lose money for itself and earn for the arm, and this table cannot see it.

The effect is not marginal. Of the six legs this table first flagged as DROP, the
trail turns FOUR profitable — one of them the best leg in the whole field:

    leg                own exit    under the trail
    MIS2-168h SHORT      -1.490          +9.074
    BR4H LONG            -0.445          +0.512
    BB_4H LONG           -0.392          +0.460
    SRA2 LONG            -0.036          +0.236
    ATS2 LONG            -0.907          -0.342   (still negative, but beats the tape by +1.99)
    BR1H LONG                 —          ~0       (residual -0.009, outlier-carried, n=98)

Acting on the DROP column alone would have removed four legs the arm makes money on.

**Use `tools/short_leg_trail_value.py` for roster decisions.** This table answers a
different question — "does the leg earn for ITSELF" — which bears on whether a leg
should keep POSTING to its own channel, not on whether the arm should mirror it.

Why absolute contribution and not density
-----------------------------------------
The current roster was selected on net-per-SLOT-DAY. That is the right metric when
slots are scarce — and they are not: `SLOT_CAP` (500) has never once fired in the live
log, the book runs around 96. What binds instead is the EXPOSURE CAP on directional
balance, so the useful question is not "who earns most per seat" but "who adds profit
while helping the book balance". This ranks by total contribution and shows the
per-trade margin next to it, so both readings stay visible.

What the verdict column means
-----------------------------
  KEEP        rostered and profitable — no action
  DROP        rostered but loses money over the window
  ADD         live, profitable, NOT rostered — a roster line, no gate change
  PROMOTE?    profitable but SHADOW — needs an operator gate flip AND a roster line;
              flipping it also makes the leg post to its own Cornix channel
  DEAD        rostered but emitted nothing in the window (a stale roster seat)
  skip        neither rostered nor worth adding

Three cautions that belong next to any decision made from this table:

  * These are ABSOLUTE returns over a window in which the index fell ~50 %. Nearly
    every short earns in such a tape. Whether a leg BEAT the tape is a different
    question, answered fee-independently by tools/short_leg_trail_value.py — and the
    two rankings disagree sharply.
  * `/day` divides by the whole window even for a leg that only ran part of it, so a
    leg retired mid-window reads slower than it was.
  * A PROMOTE? is never just a roster line. Un-parking is on the escalation list.

Read-only. Proposes; changes nothing.

Usage:
    python tools/roster_proposal.py --start 2026-06-01
    python tools/roster_proposal.py --start 2026-06-01 --min-trades 50
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.time import LEGACY_WRITER_TZ  # noqa: E402
from tools.leg_net_economics import (  # noqa: E402
    MAKER,
    SQL_FUNDING,
    SQL_TRADES,
    TAKER,
    exit_is_maker,
    funding_pct,
    round_trip_fee,
    signed_move,
)

KEEP, DROP, ADD, PROMOTE, DEAD, SKIP = "KEEP", "DROP", "ADD", "PROMOTE?", "DEAD", "skip"


def verdict(rostered: bool, live: bool, n: int, net: float, min_trades: int) -> str:
    """Rank one leg. Deliberately conservative in both directions.

    A rostered leg with no trades is DEAD rather than KEEP — a seat that produces
    nothing is not "fine", it is a stale entry that hides the roster's real size.
    A thin leg is never promoted on a handful of trades: below ``min_trades`` the
    sign is not yet a measurement.
    """
    if rostered and n == 0:
        return DEAD
    if n < min_trades:
        return KEEP if rostered and net > 0 else SKIP
    if rostered:
        return KEEP if net > 0 else DROP
    if net <= 0:
        return SKIP
    return ADD if live else PROMOTE


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--leverage", type=float, default=20.0)
    ap.add_argument("--min-trades", type=int, default=30)
    args = ap.parse_args()

    from core import shadow_gate
    from core.database import get_db_connection
    from core.trailing_roster import ROSTER, leg_key

    rostered = {(k[0].upper(), k[1]) for k in ROSTER}

    per: dict = defaultdict(lambda: {"g": [], "f": [], "fee": []})
    conn = get_db_connection()
    try:
        for direction in ("LONG", "SHORT"):
            is_long = direction == "LONG"
            with conn.cursor() as cur:
                cur.execute(SQL_TRADES, {"tz": LEGACY_WRITER_TZ, "dir": direction, "since": args.start})
                trades = cur.fetchall()
                if not trades:
                    continue
                syms = tuple({r[1] for r in trades})
                lo = min(r[5] for r in trades)
                hi = max(r[6] for r in trades)
                cur.execute(SQL_FUNDING, {"syms": syms, "lo": lo, "hi": hi})
                fr: dict = defaultdict(lambda: ([], []))
                for sym, t, rate in cur.fetchall():
                    fr[sym][0].append(t)
                    fr[sym][1].append(float(rate))
            for tag, sym, entry, close_price, status, ot, ct in trades:
                mv = signed_move(entry, close_price, is_long)
                if mv is None:
                    continue
                times, rates = fr.get(sym, ([], []))
                key = (leg_key(tag, direction)[0].upper(), direction)
                p = per[key]
                p["g"].append(mv)
                p["f"].append(funding_pct(times, rates, ot, ct, is_long))
                p["fee"].append(round_trip_fee(status))
            span_days = max(1.0, (hi - lo).total_seconds() / 86400.0)
    finally:
        conn.close()

    # Rostered legs that produced nothing must still appear — that is the finding.
    for key in rostered:
        per.setdefault(key, {"g": [], "f": [], "fee": []})

    rows = []
    for (tag, direction), p in per.items():
        n = len(p["g"])
        net = ((sum(p["g"]) + sum(p["f"])) / n - sum(p["fee"]) / n) if n else 0.0
        live = shadow_gate.is_live(tag, direction)
        rows.append({
            "tag": tag, "dir": direction, "n": n, "per_day": n / span_days,
            "net": net, "lev": net * args.leverage, "total": net * args.leverage * n,
            "live": live, "status": "live" if live else shadow_gate.leg_status(tag, direction),
            "rostered": (tag, direction) in rostered,
            "verdict": verdict((tag, direction) in rostered, live, n, net, args.min_trades),
        })

    print("=" * 108)
    print("ROSTER PROPOSAL  (%s → today, maker %.3f %% / taker %.3f %%, %.0fx leverage, min n=%d)"
          % (args.start, MAKER, TAKER, args.leverage, args.min_trades))
    print("=" * 108)
    print("  %-13s %-6s %6s %6s %10s %9s %11s %-9s %-8s %s"
          % ("leg", "dir", "n", "/day", "net/trade", "xlev %", "sum xlev", "gate", "roster", "verdict"))
    order = {ADD: 0, PROMOTE: 1, KEEP: 2, DROP: 3, DEAD: 4, SKIP: 5}
    for r in sorted(rows, key=lambda r: (order[r["verdict"]], -r["total"])):
        if r["verdict"] == SKIP:
            continue
        print("  %-13s %-6s %6d %6.1f %+10.4f %+9.2f %+11.0f %-9s %-8s %s"
              % (r["tag"], r["dir"], r["n"], r["per_day"], r["net"], r["lev"], r["total"],
                 r["status"], "yes" if r["rostered"] else "-", r["verdict"]))

    print()
    for v, label in ((ADD, "ADD — live and profitable, only a roster line"),
                     (PROMOTE, "PROMOTE? — profitable but SHADOW: gate flip + roster line, operator call"),
                     (DROP, "DROP — rostered and losing over this window"),
                     (DEAD, "DEAD — rostered seat that emitted nothing")):
        sel = [r for r in rows if r["verdict"] == v]
        if sel:
            print("  %-70s %d leg(s), Σ %+.0f" % (label, len(sel), sum(r["total"] for r in sel)))
    kept = [r for r in rows if r["verdict"] == KEEP]
    print("  %-70s %d leg(s), Σ %+.0f" % ("KEEP — rostered and profitable", len(kept),
                                          sum(r["total"] for r in kept)))
    print()
    print("  ABSOLUTE returns over a window where the index fell ~50 %% — nearly every short")
    print("  earns in such a tape. Whether a leg BEAT the tape is answered separately and")
    print("  fee-independently by tools/short_leg_trail_value.py; the two rankings disagree.")
    print("  A PROMOTE? also makes the leg post to its own Cornix channel — escalation item.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

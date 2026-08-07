"""T-2026-KYT-9050-060 — why does Bot 40 not open more trades?

The question this answers
------------------------
"Increase the trade count" has an obvious-looking answer and a correct one, and they
are not the same. The obvious one is the freshness window: rejected signals pile up
at 241–256 s against a 240 s limit, so widening it to 300 s would admit ~241 more
candidates a day. That reasoning is sound and its conclusion is still wrong, because
it stops at the FIRST gate instead of asking which gate actually binds.

A candidate has to clear every gate below. Only two of them leave a row in
`trailing_positions`; the rest exist solely as a per-cycle tally in the fleet log,
which is why an audit against the DB alone systematically blames the wrong one:

    stage 1  read_source_signals   roster · shadow_gate leg_status · entry · sl/targets
    stage 2  freshness             age > TRAILING_BOT_MAX_AGE_SEC   → PREEXISTING (DB)
    stage 3  admit()               SYMBOL_HELD · SYMBOL_REENTRY_LOCK ·
                                   SYMBOL_COOLING · EXPOSURE_CAP ·
                                   SLOT_CAP                         → log tally only
    stage 4  mirroring             no market price · mirrorable_at  → log only
    stage 5  fill                  entry never touched              → ENTRY_NOT_FILLED (DB)

**The binding gate is EXPOSURE_CAP, not the window.** `admit()` refuses a direction
once it leads the other by `EXPOSURE_CAP` (default 50) open mirrors. The live book
sits permanently at +42…+52 LONG, i.e. against the ceiling, so LONG candidates are
already being turned away *after* passing the freshness test. Widening the window
while the cap binds does not create trades — it moves rejections from `PREEXISTING`
to `EXPOSURE_CAP` and the count barely moves.

Two consequences that only fall out of reading the gates together:

* **The cap ties LONG capacity to SHORT supply.** With the book at the ceiling,
  book size ≈ 2 × (open SHORT) + cap. Every additional SHORT position unlocks one
  additional LONG slot, so the SHORT side is the real throttle on total volume —
  the opposite of where the freshness measurement points.
* **A never-closing LONG position costs cap headroom, permanently.** The
  time-stop-exempt cohort is LONG-heavy and cannot be trailed out (never armed), so
  it occupies that share of the ceiling for as long as it lives.

`SLOT_CAP` has never once appeared in the log: the Cornix channel is nowhere near
full. Slot scarcity is not the constraint; directional balance is.

Read-only: SELECTs plus a read of the fleet log. No writes, no live effect.

Usage:
    python tools/trailing_intake_audit.py --log logs/watchdog_debug_20260730_072813.log
    python tools/trailing_intake_audit.py --since "2026-07-30 08:00+03" --no-log
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

#: The tally `admit()` emits once per cycle. Rejections repeat every cycle while the
#: source trade stays open, so these counts are a STANDING PRESSURE ("how many
#: candidates are blocked right now"), never a distinct-signal count. Reading them as
#: distinct signals overstates every gate by the number of cycles it persisted.
TALLY_RE = re.compile(
    r"^(?P<day>\d{4}-\d\d-\d\d) [\d:,]+ - TRAILING_BOT - .*nicht aufgenommen \((?P<body>[^)]*)\)"
)

#: Gates that can appear in that tally, in the order admit() tests them.
# In admit()'s own test order, so the report's column order matches the order a
# candidate is actually judged in. SYMBOL_REENTRY_LOCK joined in
# T-2026-KYT-9050-115; a gate that `parse_tally` reads but this tuple omits is
# dropped from the report entirely — silent capping in the one tool that exists to
# answer "which gate is binding".
ADMIT_GATES = ("SYMBOL_HELD", "SYMBOL_REENTRY_LOCK", "SYMBOL_COOLING", "EXPOSURE_CAP", "SLOT_CAP")


def parse_tally(line: str) -> tuple[str, dict[str, int]] | None:
    """``(day, {gate: blocked})`` for one tally line, or None if the line is not one.

    The body is ``"EXPOSURE_CAP 4, SYMBOL_HELD 1"``. Splitting on the LAST space per
    part matters: gate names contain underscores, never spaces, but a future gate
    name with a space would silently parse its own name as a count.
    """
    m = TALLY_RE.match(line)
    if not m:
        return None
    out: dict[str, int] = {}
    for part in m.group("body").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, count = part.rpartition(" ")
        if not name or not count.isdigit():
            continue
        out[name] = int(count)
    return m.group("day"), out


def summarize_tallies(lines) -> dict[str, dict[str, dict]]:
    """Per day, per gate: in how many cycles it fired and how hard."""
    per_day: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    cycles: Counter = Counter()
    for line in lines:
        parsed = parse_tally(line)
        if parsed is None:
            continue
        day, gates = parsed
        cycles[day] += 1
        for gate, n in gates.items():
            per_day[day][gate].append(n)
    out: dict[str, dict[str, dict]] = {}
    for day, gates in per_day.items():
        out[day] = {"_cycles": {"n": cycles[day]}}
        for gate, counts in gates.items():
            out[day][gate] = {
                "cycles": len(counts),
                "mean": statistics.mean(counts),
                "max": max(counts),
            }
    return out


def headroom(open_long: int, open_short: int, cap: int) -> dict:
    """What the exposure cap allows right now.

    The cap is a rule about the DIFFERENCE, so it converts one side's supply into the
    other side's ceiling: with the book at the limit, total capacity is
    ``2 * min(long, short) + cap``. That identity is the whole argument for treating
    the short side as the throttle on total volume.
    """
    imbalance = open_long - open_short
    return {
        "imbalance": imbalance,
        "long_blocked": imbalance >= cap,
        "short_blocked": imbalance <= -cap,
        "long_headroom": max(0, cap - imbalance),
        "short_headroom": max(0, cap + imbalance),
        "total_capacity_at_cap": 2 * min(open_long, open_short) + cap,
    }


#: The age at rejection is only recoverable while the SOURCE trade is still in
#: `ai_signals`. Once the fleet closes it the row moves to `closed_ai_signals` and this
#: join drops it, so the counts are a LOWER BOUND that shrinks as sources close — two
#: runs on the same day returned 767 and later 707 LONG. Harmless for the shape question
#: ("do rejections cluster just past the limit"), misleading for absolute volume.
SQL_PREEXISTING_AGES = """
    SELECT t.direction,
           count(*),
           percentile_cont(0.10) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (t.opened_at - a.open_time))),
           percentile_cont(0.50) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (t.opened_at - a.open_time))),
           percentile_cont(0.90) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (t.opened_at - a.open_time))),
           count(*) FILTER (WHERE EXTRACT(EPOCH FROM (t.opened_at - a.open_time)) <= 300)
    FROM trailing_positions t
    JOIN ai_signals a ON a.id = t.src_signal_id
    WHERE t.close_reason = 'PREEXISTING' AND t.opened_at >= %s
    GROUP BY 1
"""

SQL_IMBALANCE = """
    WITH h AS (SELECT generate_series(%s::timestamptz, NOW(), interval '6 hours') AS t)
    SELECT h.t,
           count(*) FILTER (WHERE p.direction = 'LONG'),
           count(*) FILTER (WHERE p.direction = 'SHORT')
    FROM h
    LEFT JOIN trailing_positions p
      ON p.filled_at IS NOT NULL AND p.opened_at <= h.t
     AND (p.closed_at IS NULL OR p.closed_at > h.t)
    GROUP BY h.t ORDER BY h.t
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="2026-07-30 08:00+03",
                    help="analysis start (default: first full day after the 240 s recalibration)")
    ap.add_argument("--log", default="", help="fleet log carrying the TRAILING_BOT tallies")
    ap.add_argument("--no-log", action="store_true", help="DB gates only")
    args = ap.parse_args()

    from core.database import get_db_connection
    from core.trailing_roster import ROSTER

    cap = int(os.getenv("TRAILING_BOT_EXPOSURE_CAP", "50"))
    window = float(os.getenv("TRAILING_BOT_MAX_AGE_SEC", "240"))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(SQL_PREEXISTING_AGES, (args.since,))
            ages = cur.fetchall()
            cur.execute(SQL_IMBALANCE, (args.since,))
            imbalance = cur.fetchall()
    finally:
        conn.close()

    print("=" * 78)
    print("STAGE 2 — freshness window (%.0f s)   [leaves a DB row: PREEXISTING]" % window)
    print("=" * 78)
    print("  %-6s %7s %8s %8s %8s %14s" % ("dir", "n", "p10 s", "p50 s", "p90 s", "recoverable@300s"))
    for d, n, p10, p50, p90, under300 in ages:
        print("  %-6s %7d %8.0f %8.0f %8.0f %14d" % (d, n, p10, p50, p90, under300))
    print("  A tight band just past the limit is a pipeline artefact, not an age")
    print("  distribution — the cutoff is slicing through one leg family's latency.")

    print()
    print("=" * 78)
    print("STAGE 3 — admit() gates   [log tally only, NO DB row]")
    print("=" * 78)
    if args.no_log or not args.log:
        print("  skipped (--no-log or no --log given)")
    elif not os.path.exists(args.log):
        print("  log not found: %s" % args.log)
    else:
        with open(args.log, encoding="utf-8", errors="replace") as fh:
            summary = summarize_tallies(fh)
        print("  %-12s %8s | %s" % ("day", "cycles", "  ".join("%-22s" % g for g in ADMIT_GATES)))
        for day in sorted(summary):
            row = ""
            for gate in ADMIT_GATES:
                s = summary[day].get(gate)
                row += "%-22s  " % (("%d cyc, Ø%.1f, max %d" % (s["cycles"], s["mean"], s["max"]))
                                    if s else "—")
            print("  %-12s %8d | %s" % (day, summary[day]["_cycles"]["n"], row))
        if not any(g == "SLOT_CAP" for day in summary for g in summary[day]):
            print("  SLOT_CAP never fired — the Cornix channel is not the constraint.")

    print()
    print("=" * 78)
    print("EXPOSURE CAP (±%d) — the directional ceiling" % cap)
    print("=" * 78)
    print("  %-16s %6s %6s %10s %12s %12s" % ("when", "LONG", "SHORT", "imbalance", "LONG head", "SHORT head"))
    for t, lo, sh in imbalance:
        h = headroom(lo, sh, cap)
        flag = "  <== LONG blocked" if h["long_blocked"] else ("  <== SHORT blocked" if h["short_blocked"] else "")
        print("  %-16s %6d %6d %+10d %12d %12d%s"
              % (t.strftime("%m-%d %H:%M"), lo, sh, h["imbalance"],
                 h["long_headroom"], h["short_headroom"], flag))
    if imbalance:
        _, lo, sh = imbalance[-1]
        h = headroom(lo, sh, cap)
        print()
        print("  With the book at the ceiling, total capacity = 2 × min(L,S) + cap = %d."
              % h["total_capacity_at_cap"])
        print("  Every extra SHORT position raises the LONG ceiling by one, so the SHORT")
        print("  side throttles TOTAL volume — regardless of how many LONG candidates queue up.")

    n_long = len([1 for k in ROSTER if k[1] == "SHORT"])
    print()
    print("  roster: %d legs, %d of them SHORT" % (len(ROSTER), n_long))
    return 0


if __name__ == "__main__":
    sys.exit(main())

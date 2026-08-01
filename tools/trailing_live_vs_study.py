"""T-2026-KYT-9050-047 — does Bot 40 turn its book over faster than the study said?

The question and why it is not rhetorical
-----------------------------------------
PR #198 (`tools/trailing_slot_budget.py` → `staging_models/replay/trailing_slot_budget_live.json`)
picked the operating point act = 2 %, x = 10 % from a SIMULATION on 15m candles.
`core/trailing_roster.py` carries what that run expected: Ø 284.6 / p95 498.0
simultaneously open trades, and a median holding time of 4.6 h. If the live arm
turns over faster than that, two things the operator decided on move at once — the
slot draw (the channel is capped at 500) and the fee load (0.10 % taker round-trip
× number of trades, which is what the 49 204 % net headline was charged).

The first suspect is resolution, and it is NOT a bug
---------------------------------------------------
The study evaluates the trail on 15m candle extremes with a **strictly prior** peak
(`trailing_slot_budget.prior_peak` — within one candle the order of high and low is
unknowable, so a peak may only arm a trail that fires on a LATER candle). The bot
polls live prices every 10 s and has no such restriction: a spike and its give-back
inside one 15m bar arms and fires the same bar. The finer grid must therefore fire
at least as often. That much is arithmetic — the point of this tool is to say by
HOW MUCH, on the arm's own trades, instead of asserting it.

So section 6 replays the study's rule — the imported one, not a re-implementation
(Regel 7) — over each live mirror's OWN holding window and cross-tabulates the two
verdicts. The four cells are the whole answer:

  * live TRAIL, 15m would also have trailed → the exits agree, only the timing moves
  * live TRAIL, 15m would NOT have trailed  → **the resolution-only exits**
  * live held, 15m would have trailed       → the 15m wick fires where the 10 s poll
                                              price never did (the effect runs both
                                              ways, which is why it is measured)
  * live held, 15m would not have            → agreement

Three separations the numbers are worthless without
---------------------------------------------------
1. **`posted` is the live/shadow line.** 6 052 rows in `trailing_positions`, 1 140 of
   them posted. The rest is the shadow book plus admission notes (`PREEXISTING`,
   `SHADOW_CARRYOVER`) that never corresponded to a Cornix position. Aggregating over
   the table mixes them (the T-052 lesson, one directory over).
2. **`ENTRY_NOT_FILLED` never occupied a slot and never paid a fee.** It is a posted
   row with no position behind it. Counting it inflates both the slot draw and the
   fee load.
3. **The live leg mix is not the study's.** MIS1-72h LONG is 35 % of the live book;
   EPD1 SHORT, the study's second-largest leg, has not mirrored once. Any headline
   comparison is therefore a mix comparison unless it is matched per leg — so every
   aggregate here carries its mix-matched twin, built from the study's own per-leg
   numbers weighted by the LIVE counts.

And one correction on the study side: the Ø 284.6 includes ROM1 LONG (11) + ROM1
SHORT (22), which `core/trailing_roster.py` has since excluded as a re-forwarder
duplicate. Mean occupancy is a sum of indicators and therefore exactly additive, so
the roster-matched expectation is 284.6 − 33 = 251.6. p95 is not additive and cannot
be corrected the same way; it is reported uncorrected and labelled.

Honest limits
-------------
* The 15m replay starts at the first bar FULLY inside the holding window, so it can
  miss up to 15 min of early tape. That withholds arming opportunities from the study
  side — the resolution effect measured here is a LOWER bound.
* The study timestamps a trailing exit at the trigger bar's `open_time`; this replay
  uses the bar's CLOSE, because that is the first instant the trigger is knowable
  (the tape-causal pin from T-052, where a deadline read off future tape turned
  59k into 7k). The study's own holding times are thus up to one bar optimistic.
* Beyond the live exit the counterfactual is right-censored at `--horizon-h`: in the
  study the trade would also have ended at its source's recorded close, which is
  earlier. "Would have held ≥ X h longer" is a floor, not an estimate.
* Six live days against a 147-day simulation. Tape, not just rule, differs.

Read-only: SELECTs against trailing_positions and candles. No writes, no live effect.

Usage:
    python tools/trailing_live_vs_study.py
    python tools/trailing_live_vs_study.py --since 2026-07-28 --no-replay
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# The live VPS is Windows and a plain `python tools/…` there writes cp1252: the first
# Σ in the report kills the run with UnicodeEncodeError halfway through, after four
# sections have already printed. Report tools are read by operators, not by CI, so
# they get the encoding right themselves instead of relying on PYTHONIOENCODING.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.trailing_roster import (  # noqa: E402
    ACTIVATION_PCT,
    EXCLUDED_AS_DUPLICATE,
    EXPECTED_OCC_MEAN,
    EXPECTED_OCC_P95,
    RETRACE_FRAC,
    SOURCE_REPORT,
    leg_key,
)
from tools.trailing_arm_report import FEE_RT, net_of_fees, realized_mark  # noqa: E402
from tools.trailing_slot_budget import capture_at, occupancy, prior_peak, trail_exit  # noqa: E402

#: Exits that represent a real position outcome. Deliberately the same list as
#: `trailing_arm_report.REAL_EXIT_REASONS` minus nothing — kept local because this
#: tool ALSO needs the complement (the admission rejects) by name.
REAL_EXIT_REASONS = ("TRAIL", "TIME_STOP", "SL_HIT", "SOURCE_CLOSED", "LEG_RETIRED")

#: Posted rows that never became a position: no slot, no fee, no outcome.
NON_POSITION_REASONS = ("ENTRY_NOT_FILLED", "PREEXISTING", "SHADOW_CARRYOVER")

#: Exits the arm decided itself — the ones whose TIMING the study's rule also governs.
ARM_REASONS = ("TRAIL", "TIME_STOP")

BAR = timedelta(minutes=15)
BAR_TF = "15m"

#: Both ROM1 legs sat in the Ø 284.6 and were later excluded from the roster.
ROM1_OCC_MEAN = 33.0


# --------------------------------------------------------------------------- #
# Pure logic — no DB, pinned in backtest/test_trailing_live_vs_study.py        #
# --------------------------------------------------------------------------- #
def slot_interval(row: dict, now: datetime) -> tuple[datetime, datetime] | None:
    """When did this row actually occupy a Cornix slot? None if it never did.

    A slot is occupied from the FILL, not from the mirror row's insert: between the
    two sits an unfilled limit order, which is exactly what `ENTRY_NOT_FILLED` books
    when it never resolves. `filled_at` only exists from T-050 on (the first 124
    posted rows carry NULL), so `opened_at` stands in there — the mirror enters at
    market, so the two are seconds apart and the substitution is conservative in the
    direction of MORE occupancy, never less.
    """
    if not row.get("posted"):
        return None
    if row.get("close_reason") in NON_POSITION_REASONS:
        return None
    start = row.get("filled_at") or row.get("opened_at")
    if start is None:
        return None
    end = row.get("closed_at") or now
    return (start, end) if end > start else (start, start + timedelta(seconds=1))


def hold_hours(row: dict, now: datetime) -> float | None:
    """Holding time in hours over the slot-occupying interval."""
    iv = slot_interval(row, now)
    if iv is None:
        return None
    return (iv[1] - iv[0]).total_seconds() / 3600.0


def hourly_occupancy(intervals: list[tuple[datetime, datetime]]) -> np.ndarray:
    """Simultaneously open positions per hour, on the same construction the study used.

    Reuses `trailing_slot_budget.occupancy` (diff + cumsum) so live and simulated
    occupancy are literally the same statistic. A sub-hour position still occupies its
    hour — otherwise a book of 10-minute trades would report zero draw while holding
    real Cornix seats.
    """
    if not intervals:
        return np.zeros(0, dtype=np.int64)
    t0 = min(a for a, _ in intervals).replace(minute=0, second=0, microsecond=0)
    starts, ends = [], []
    for a, b in intervals:
        # Floor both ends to the hour, exactly as `trailing_slot_budget.slot_index`
        # does — otherwise live and simulated occupancy stop being the same statistic.
        i = max(0, int((a - t0).total_seconds() // 3600))
        j = max(0, int((b - t0).total_seconds() // 3600))
        if j <= i:
            j = i + 1
        starts.append(i)
        ends.append(j)
    # The grid ends with the last occupied hour. Deriving it from the raw max end
    # instead would append a trailing empty hour whenever a position closes on the
    # hour, quietly dragging the mean occupancy down.
    glen = max(ends)
    return occupancy(np.asarray(starts, dtype=np.int64), np.asarray(ends, dtype=np.int64), glen)


def censored_median_bounds(closed: list[float], open_ages: list[float]) -> tuple[float, float] | None:
    """Median holding time of the live book as an interval, honouring the open positions.

    Six live days against a 147-day simulation means the live book is right-censored:
    every position still open would, if it closed today, land in the upper tail. Taking
    the median over CLOSED rows only therefore reports the live arm as faster than it is
    — and "the live arm is faster" is precisely the hypothesis under test, so the naive
    median is biased toward confirming it.

    Both bounds are exact, no survival model needed:
      * lower — every open position closes right now (its current age is its duration);
      * upper — every open position outlives every closed one.
    They coincide whenever the censored rows all sit above the middle order statistic.
    Returns None if more than half the book is censored, where the median is not
    identifiable at all.
    """
    n = len(closed) + len(open_ages)
    if n == 0 or len(open_ages) * 2 >= n:
        return None
    lo = statistics.median(sorted(closed + open_ages))
    # +inf sorts the censored rows past every observed duration — the upper bound.
    hi = statistics.median(sorted(closed) + [float("inf")] * len(open_ages))
    return lo, hi


def weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    """Median of values under weights — the mix-matched aggregate.

    The study reports a per-leg median holding time; combining those into "what the
    study predicts for the live book" means weighting each leg by how often the LIVE
    book actually traded it. A plain mean over legs would let MIS2-168h SHORT (3 live
    mirrors) count as much as MIS1-72h LONG (403).
    """
    pairs = [(v, w) for v, w in pairs if w > 0]
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2.0:
            return v
    return pairs[-1][0]


def turnover_per_slot_day(n_trades: int, slot_days: float) -> float | None:
    """Trades per occupied slot-day — the turnover measure that survives a mix change.

    Neither holding time nor trade count alone answers "is the live arm faster": a
    book with fewer positions produces fewer trades at identical speed. Trades per
    occupied slot-day normalises that out, and it is also the unit the fee load is
    charged in (`FEE_RT` × trades).
    """
    if slot_days <= 0:
        return None
    return n_trades / slot_days


def fee_share_of_gross(gross_pct: float, n_trades: int, fee: float = FEE_RT) -> float | None:
    """What fraction of the gross move the round-trip fee consumes. None if gross ≤ 0.

    Undefined rather than 0 on a losing book: "fees ate 12 % of a negative gross" is
    a sentence with no meaning, and reporting it as a small positive number is how a
    loss-making book reads as cheap.
    """
    if gross_pct <= 0:
        return None
    return fee * n_trades / gross_pct


def replay_study_rule(
    bars: list[tuple[datetime, float, float]],
    entry: float,
    is_long: bool,
    x: float = RETRACE_FRAC,
    activation: float = ACTIVATION_PCT,
) -> tuple[datetime, float] | None:
    """What the STUDY's rule would have done on these bars: (exit instant, mark) or None.

    `bars` are (open_time, high, low) of fully-closed 15m candles in chronological
    order. The decision itself is `trailing_slot_budget.trail_exit` — imported, not
    reproduced — so this cannot drift away from the rule that picked act = 2 %.

    The exit instant is the trigger bar's CLOSE, not its open. A 15m trigger is not
    knowable until the bar completes; timestamping it at the open would credit the
    study rule with an exit up to 15 min before the information existed, which is the
    look-ahead class that turned T-052's be-family from 59k into 7k.
    """
    if len(bars) < 2 or entry <= 0:
        return None
    highs = np.array([b[1] for b in bars], dtype=float)
    lows = np.array([b[2] for b in bars], dtype=float)
    if is_long:
        fav = (highs - entry) / entry * 100.0
        adv = (lows - entry) / entry * 100.0
    else:
        fav = (entry - lows) / entry * 100.0
        adv = (entry - highs) / entry * 100.0
    k = trail_exit(fav, adv, x, activation)
    if k is None:
        return None
    mark, _ = capture_at(fav, x, k, 1.0, 0.0, 0.0)
    return bars[k][0] + BAR, float(mark)


def bars_in_window(
    series: list[tuple[datetime, float, float]], start: datetime, end: datetime
) -> list[tuple[datetime, float, float]]:
    """Bars FULLY contained in [start, end] — flush on both sides.

    The study's own mask selects on `open_time` alone, so its last bar can reach up to
    one interval past the recorded close and arm a trail on tape the trade never saw
    (documented limit, `trailing_slot_budget_live.md`). Here the window closes flush,
    which withholds trigger opportunities from the study side rather than granting it
    extra ones — the conservative direction for a tool whose thesis is that the study
    fires LESS often.
    """
    return [b for b in series if b[0] >= start and b[0] + BAR <= end]


#: A 15m trigger is only knowable at the bar close, so any Δ inside one bar width is
#: the grid, not a decision. Separating it out is the difference between "the study
#: would have held 20 minutes longer" and "the study would have held".
GRID_H = BAR.total_seconds() / 3600.0


def resolution_bucket(delta_h: float | None) -> str:
    """Where the study's 15m exit lands relative to the live one.

    `delta_h = study_exit − live_exit`, or None when the 15m rule never fires inside
    the horizon.

    The first cut of this analysis compared the two over the LIVE holding window only,
    which put 88 % of the arm's exits in a "the study would have held" bucket. That
    number was an artifact of the window edge: the trigger almost always sits in the
    very bar the live exit falls into, and a flush window excludes exactly that bar.
    Measured against the extended horizon the same trades come back at a median of
    +0.33 h — one bar. Hence the explicit `same-bar` bucket: the boundary between
    grid granularity and a genuinely different operating point is the whole finding,
    and burying it in a binary would have inverted the verdict.
    """
    if delta_h is None:
        return "study-never-fires"
    if delta_h <= 0:
        return "study-earlier"
    if delta_h <= GRID_H:
        return "same-bar"
    return "study-later"


# --------------------------------------------------------------------------- #
# SQL                                                                         #
# --------------------------------------------------------------------------- #
#: `posted` carries the live/shadow line; everything else is filtered in Python so the
#: rejected populations can still be COUNTED rather than silently disappearing.
SQL_MIRRORS = """
    SELECT id, symbol, model, direction, entry, sl, peak_pct, posted,
           opened_at, filled_at, closed_at, close_reason, close_mark_pct
    FROM trailing_positions
    WHERE opened_at >= %s
    ORDER BY opened_at
"""

#: One round trip for the whole replay. 500 per-symbol queries would be kinder to
#: memory and much harsher on a box already at ~98 % CPU.
SQL_BARS = """
    SELECT symbol, open_time, high, low
    FROM candles
    WHERE tf = %s AND is_closed AND open_time >= %s AND symbol = ANY(%s)
    ORDER BY symbol, open_time
"""


def load(conn, since: datetime, replay: bool):
    with conn.cursor() as cur:
        cur.execute(SQL_MIRRORS, (since,))
        cols = [d[0] for d in cur.description]
        mirrors = [dict(zip(cols, r)) for r in cur.fetchall()]
        series: dict[str, list[tuple[datetime, float, float]]] = defaultdict(list)
        if replay:
            syms = sorted({m["symbol"] for m in mirrors if m["posted"]})
            if syms:
                cur.execute(SQL_BARS, (BAR_TF, since - BAR, syms))
                for sym, ts, hi, lo in cur.fetchall():
                    if hi is not None and lo is not None:
                        series[sym].append((ts, float(hi), float(lo)))
    return mirrors, series


def load_study() -> dict:
    with open(os.path.join(REPO_ROOT, SOURCE_REPORT), encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# Report                                                                      #
# --------------------------------------------------------------------------- #
def _pct(values: list[float], q: float) -> float:
    return float(np.percentile(values, q)) if values else 0.0


def report(mirrors: list[dict], series: dict, study: dict, now: datetime, horizon_h: float) -> None:
    live = [m for m in mirrors if m["posted"]]
    shadow = [m for m in mirrors if not m["posted"]]
    positions = [m for m in live if m["close_reason"] not in NON_POSITION_REASONS]
    exits = [m for m in positions if m["closed_at"] is not None and m["close_reason"] in REAL_EXIT_REASONS]
    open_now = [m for m in positions if m["closed_at"] is None]

    print("=" * 78)
    print("1  POPULATION — live (posted) strictly separated from shadow")
    print("=" * 78)
    print("  rows=%d | posted(live)=%d | not posted(shadow+notes)=%d" % (len(mirrors), len(live), len(shadow)))
    sh = defaultdict(int)
    for m in shadow:
        sh[m["close_reason"] or "still open"] += 1
    print("    shadow side: " + ", ".join("%s=%d" % kv for kv in sorted(sh.items(), key=lambda k: -k[1])))
    # The observation that opened this task — "~80 trail fires per hour at ~460 open
    # positions" — is measurable, and it is a property of the bootstrap, not of the
    # rule: the first shadow cycle mirrored a book that was ALREADY running, so those
    # mirrors inherited a peak above the activation floor and fired on their first
    # poll. Nothing since then looks like it, and a rate read off that hour cannot be
    # projected onto steady-state operation.
    burst = [m for m in shadow if m["close_reason"] == "TRAIL" and m["closed_at"] is not None]
    if burst:
        span = (max(m["closed_at"] for m in burst) - min(m["closed_at"] for m in burst)).total_seconds() / 3600.0
        print("  shadow bootstrap: %d TRAIL fires inside %.1f h (%.0f/h) on %s — inherited peaks,"
              % (len(burst), span, len(burst) / span if span else 0.0,
                 min(m["closed_at"] for m in burst).astimezone(timezone.utc).date()))
        print("    already above activation when the mirror opened. Not a steady-state rate.")

    nf = len([m for m in live if m["close_reason"] == "ENTRY_NOT_FILLED"])
    print("  live positions=%d (real exits=%d, open now=%d) | never filled, no slot, no fee=%d"
          % (len(positions), len(exits), len(open_now), nf))
    if positions:
        span0 = min(m["opened_at"] for m in positions)
        print("  live window %s .. %s  (%.1f days)"
              % (span0.date(), now.date(), (now - span0).total_seconds() / 86400.0))

    # ---- 2  holding time ---------------------------------------------------- #
    print()
    print("=" * 78)
    print("2  HOLDING TIME — live against the study's per-leg medians")
    print("=" * 78)
    holds = [h for h in (hold_hours(m, now) for m in exits) if h is not None]
    print("  closed live positions: n=%d  median=%.2f h  p25=%.2f  p75=%.2f  p95=%.2f  mean=%.2f"
          % (len(holds), statistics.median(holds), _pct(holds, 25), _pct(holds, 75),
             _pct(holds, 95), statistics.mean(holds)))
    open_ages = [h for h in (hold_hours(m, now) for m in open_now) if h is not None]
    bounds = censored_median_bounds(holds, open_ages)
    if bounds is not None:
        print("  including the %d still-open positions as right-censored (they can only be LONGER):"
              % len(open_ages))
        print("    median lies in [%.2f h, %s]  — the closed-only median above is the optimistic end"
              % (bounds[0], "%.2f h" % bounds[1] if bounds[1] != float("inf") else "∞"))

    legs = study["legs"]

    def _study(m, field):
        k = leg_key(m["model"], m["direction"])
        return legs.get("%s %s" % k, {}).get(field)

    matched = [(_study(m, "trail_h_median"), 1.0) for m in exits]
    matched = [(v, w) for v, w in matched if v is not None]
    wm = weighted_median(matched)
    print("  study, mix-matched to the live leg counts: weighted median=%s h  (headline over legs: %.1f h)"
          % ("%.2f" % wm if wm is not None else "n/a", study["sweep"]["2.0"]["median_hold_h"]))
    print("  %d of %d exits belong to a leg the study scored" % (len(matched), len(exits)))

    print()
    print("  per (day, direction) — day of the EXIT:")
    print("    %-12s %-6s %5s %9s %9s %9s" % ("day", "dir", "n", "median h", "p75 h", "mean h"))
    byday: dict = defaultdict(list)
    for m in exits:
        h = hold_hours(m, now)
        if h is not None:
            byday[(m["closed_at"].astimezone(timezone.utc).date(), m["direction"])].append(h)
    for key in sorted(byday):
        v = byday[key]
        print("    %-12s %-6s %5d %9.2f %9.2f %9.2f"
              % (key[0], key[1], len(v), statistics.median(v), _pct(v, 75), statistics.mean(v)))

    print()
    print("  per leg — live median vs the study's trailing median (and its hold median):")
    print("    %-18s %5s %10s %12s %12s %8s" % ("leg", "n", "live med", "study trail", "study hold", "ratio"))
    byleg: dict = defaultdict(list)
    for m in exits:
        h = hold_hours(m, now)
        if h is not None:
            byleg["%s %s" % leg_key(m["model"], m["direction"])].append(h)
    for k in sorted(byleg, key=lambda k: -len(byleg[k])):
        v = byleg[k]
        s = legs.get(k, {})
        st, sh_ = s.get("trail_h_median"), s.get("hold_h_median")
        ratio = (statistics.median(v) / st) if st else None
        print("    %-18s %5d %10.2f %12s %12s %8s"
              % (k, len(v), statistics.median(v),
                 "%.2f" % st if st is not None else "—",
                 "%.1f" % sh_ if sh_ is not None else "—",
                 "%.2f×" % ratio if ratio else "—"))

    # ---- 3  exit mix -------------------------------------------------------- #
    print()
    print("=" * 78)
    print("3  EXIT MIX — how the arm's own decisions split against following the fleet")
    print("=" * 78)
    print("    %-12s %-6s %5s " % ("day", "dir", "n") + " ".join("%12s" % r for r in REAL_EXIT_REASONS))
    mix: dict = defaultdict(lambda: defaultdict(int))
    for m in exits:
        mix[(m["closed_at"].astimezone(timezone.utc).date(), m["direction"])][m["close_reason"]] += 1
    for key in sorted(mix):
        row = mix[key]
        n = sum(row.values())
        print("    %-12s %-6s %5d " % (key[0], key[1], n)
              + " ".join("%11.0f%%" % (100.0 * row[r] / n) for r in REAL_EXIT_REASONS))
    tot: dict = defaultdict(int)
    for m in exits:
        tot[m["close_reason"]] += 1
    n = sum(tot.values())
    print("    %-12s %-6s %5d " % ("ALL", "", n)
          + " ".join("%11.0f%%" % (100.0 * tot[r] / n) for r in REAL_EXIT_REASONS))
    trails = [m for m in exits if m["close_reason"] == "TRAIL"]
    if trails:
        hours: dict = defaultdict(int)
        for m in trails:
            hours[m["closed_at"].replace(minute=0, second=0, microsecond=0)] += 1
        elapsed = (max(m["closed_at"] for m in trails)
                   - min(m["closed_at"] for m in trails)).total_seconds() / 3600.0
        print()
        print("  trail fire rate in steady state: %d fires over %.0f elapsed hours = %.1f/h,"
              % (len(trails), elapsed, len(trails) / elapsed if elapsed else 0.0))
        print("    busiest single hour %d. The bootstrap rate in section 1 is not this number."
              % max(hours.values()))

    # ---- 4  value against the fee ------------------------------------------- #
    print()
    print("=" * 78)
    print("4  REALISED MARK AGAINST THE %.2f %% ROUND-TRIP FEE" % FEE_RT)
    print("=" * 78)
    marked = [(m, realized_mark(m)) for m in exits]
    unknown = len([1 for _, mk in marked if mk is None])
    marked = [(m, mk) for m, mk in marked if mk is not None]
    print("  %d of %d exits carry a usable mark (SL reconstruction per T-054); %d unknown"
          % (len(marked), len(exits), unknown))
    print("    %-12s %-6s %5s %10s %10s %10s %10s %10s"
          % ("day", "dir", "n", "Σ gross", "fee", "Σ net", "<fee %", "Ø mark"))

    def _line(label, dirn, sub):
        if not sub:
            return
        mk = [x for _, x in sub]
        gross = sum(mk)
        below = 100.0 * len([x for x in mk if x < FEE_RT]) / len(mk)
        print("    %-12s %-6s %5d %10.1f %10.1f %10.1f %9.0f%% %10.2f"
              % (label, dirn, len(mk), gross, FEE_RT * len(mk), net_of_fees(mk), below, statistics.mean(mk)))

    dayd: dict = defaultdict(list)
    for m, mk in marked:
        dayd[(m["closed_at"].astimezone(timezone.utc).date(), m["direction"])].append((m, mk))
    for key in sorted(dayd):
        _line(str(key[0]), key[1], dayd[key])
    for d in ("LONG", "SHORT"):
        _line("ALL", d, [(m, mk) for m, mk in marked if m["direction"] == d])
    _line("ALL", "", marked)
    print()
    for reason in REAL_EXIT_REASONS:
        sub = [(m, mk) for m, mk in marked if m["close_reason"] == reason]
        _line(reason, "", sub)
    gross_all = sum(mk for _, mk in marked)
    share = fee_share_of_gross(gross_all, len(marked))
    print()
    print("  fee load: %d trades × %.2f %% = %.1f %%-points against a gross of %+.1f → %s"
          % (len(marked), FEE_RT, FEE_RT * len(marked), gross_all,
             "fees eat %.0f %% of the gross" % (100.0 * share) if share is not None
             else "gross is not positive, a fee share would be meaningless"))
    study_below = study["sweep"]["2.0"]["below_fee_pct"]
    print("  study at act=2 expected %.0f %% of trades below the fee (that share is over ALL trades,"
          % study_below)
    print("  including the ones the trail never triggered — compare it to the ALL line, not to TRAIL)")

    # ---- 5  occupancy -------------------------------------------------------- #
    print()
    print("=" * 78)
    print("5  SIMULTANEOUS OCCUPANCY — against Ø %.1f / p95 %.1f from the study"
          % (EXPECTED_OCC_MEAN, EXPECTED_OCC_P95))
    print("=" * 78)
    intervals = [iv for iv in (slot_interval(m, now) for m in live) if iv is not None]
    occ = hourly_occupancy(intervals)
    if len(occ) == 0:
        print("  no occupied slots in the window")
        slot_days = 0.0
    else:
        slot_days = float(occ.sum()) / 24.0
        print("  live: Ø %.1f  median %.0f  p95 %.0f  max %d  over %d hours  → %.1f slot-days"
              % (occ.mean(), np.median(occ), np.percentile(occ, 95), occ.max(), len(occ), slot_days))
        excl = ", ".join("%s %s" % k for k in EXCLUDED_AS_DUPLICATE)
        print("  study Ø %.1f includes %s (%.0f seats) — mean occupancy is additive, so the"
              % (EXPECTED_OCC_MEAN, excl, ROM1_OCC_MEAN))
        print("  roster-matched expectation is %.1f. p95 is not additive and stays uncorrected."
              % (EXPECTED_OCC_MEAN - ROM1_OCC_MEAN))
        print("  → live draws %.0f %% of the roster-matched mean (%.0f %% of the raw Ø 284.6)"
              % (100.0 * occ.mean() / (EXPECTED_OCC_MEAN - ROM1_OCC_MEAN),
                 100.0 * occ.mean() / EXPECTED_OCC_MEAN))

    if len(occ) >= 48:
        recent = occ[-48:]
        print("  last 48 h: Ø %.1f  p95 %.0f  max %d — the steady state, past the ramp-up"
              % (recent.mean(), np.percentile(recent, 95), recent.max()))

    # Occupancy = intake × holding time. Holding time came out at or above the study's
    # (section 2), so a draw at half the expectation can only be intake — which is
    # where the live bot has four filters the simulation never had: one mirror per
    # symbol, the 240 s freshness window, the symbol cooldown and the exposure cap.
    print()
    print("  intake — mirrors admitted per day against the trades the study scored per day:")
    acc = set(study["fills_by_act"]["2.0"]["p95"]["accepted"])
    study_days = (datetime.fromisoformat(study["generated_at"]) - datetime.fromisoformat(
        study["start"]).replace(tzinfo=timezone.utc)).total_seconds() / 86400.0
    live_days = (now - min(m["opened_at"] for m in positions)).total_seconds() / 86400.0
    s_rate = sum(legs[k]["n"] for k in acc) / study_days
    l_rate = len(positions) / live_days
    print("    live  %.1f/day (%d positions over %.1f days)" % (l_rate, len(positions), live_days))
    print("    study %.1f/day (%d trades over %.0f days, chosen p95 selection incl. ROM1)"
          % (s_rate, sum(legs[k]["n"] for k in acc), study_days))
    print("    → live admits %.0f %% of the simulated flow. Occupancy is intake × holding time,"
          % (100.0 * l_rate / s_rate))
    print("      and holding time is NOT short (section 2) — so intake, not turnover, is what")
    print("      leaves the channel half empty.")

    print()
    print("  turnover — trades per occupied slot-day (mix-robust; the fee is charged in this unit):")
    live_to = turnover_per_slot_day(len(exits), slot_days)
    s_n = sum(legs[k]["n"] for k in acc)
    s_sd = sum(legs[k]["slot_days_trail"] for k in acc)
    s_to = turnover_per_slot_day(s_n, s_sd)
    print("    live  %s trades/slot-day  (%d exits / %.1f slot-days)"
          % ("%.3f" % live_to if live_to else "n/a", len(exits), slot_days))
    print("    study %s trades/slot-day  (%d trades / %.0f slot-days, chosen p95 selection)"
          % ("%.3f" % s_to if s_to else "n/a", s_n, s_sd))
    mm = [(turnover_per_slot_day(legs[k]["n"], legs[k]["slot_days_trail"]) or 0.0, float(len(byleg.get(k, []))))
          for k in legs if k in byleg]
    mmv = weighted_median(mm)
    print("    study, mix-matched to the live leg counts: %s trades/slot-day (weighted median)"
          % ("%.3f" % mmv if mmv is not None else "n/a"))
    if live_to and s_to:
        print("    → live turnover is %.2f× the study's aggregate%s"
              % (live_to / s_to, ", %.2f× the mix-matched one" % (live_to / mmv) if mmv else ""))
    print("    fee per slot-day: live %.3f %% vs study %.3f %%"
          % (FEE_RT * (live_to or 0.0), FEE_RT * (s_to or 0.0)))

    # ---- 6  resolution counterfactual --------------------------------------- #
    print()
    print("=" * 78)
    print("6  RESOLUTION — the study's 15m rule replayed on the SAME live mirrors")
    print("=" * 78)
    if not series:
        print("  replay skipped (--no-replay or no candle coverage)")
        return
    print("  each live mirror replayed on its OWN 15m tape from the fill to %.0f h past the live" % horizon_h)
    print("  exit; the rule is the imported one (act %.1f %%, x %.0f %%, strictly prior peak)."
          % (ACTIVATION_PCT, RETRACE_FRAC * 100))
    rows, no_bars = [], 0
    for m in exits:
        iv = slot_interval(m, now)
        if iv is None or not m["entry"]:
            continue
        start, live_exit = iv
        end = min(now, live_exit + timedelta(hours=horizon_h))
        win = bars_in_window(series.get(m["symbol"], []), start, end)
        if len(win) < 2:
            no_bars += 1
            continue
        res = replay_study_rule(win, float(m["entry"]), m["direction"] == "LONG")
        delta = (res[0] - live_exit).total_seconds() / 3600.0 if res is not None else None
        rows.append((m, delta, res[1] if res is not None else None))

    print("  %d of %d exits replayed (%d had fewer than two fully-contained 15m bars)"
          % (len(rows), len(exits), no_bars))
    if not rows:
        return

    reading = {
        "study-earlier": "the 15m wick fired first — live was the SLOWER grid",
        "same-bar": "same 15m bar: grid granularity, not a different operating point",
        "study-later": "the study would genuinely have held on",
        "study-never-fires": "no 15m trigger inside the horizon (right-censored)",
    }
    for label, sub in (("the arm's own exits (TRAIL/TIME_STOP)", [r for r in rows if r[0]["close_reason"] in ARM_REASONS]),
                       ("exits the fleet or the SL ended", [r for r in rows if r[0]["close_reason"] not in ARM_REASONS])):
        if not sub:
            continue
        print()
        print("  %s — n=%d:" % (label, len(sub)))
        counts: dict = defaultdict(int)
        for _, d, _mk in sub:
            counts[resolution_bucket(d)] += 1
        for b in ("study-earlier", "same-bar", "study-later", "study-never-fires"):
            if counts[b]:
                print("    %-18s %5d %5.0f%%   %s" % (b, counts[b], 100.0 * counts[b] / len(sub), reading[b]))
        ds = [d for _, d, _mk in sub if d is not None]
        if ds:
            print("    Δ = study exit − live exit:  median %+.2f h  p25 %+.2f  p75 %+.2f  p95 %+.2f"
                  % (statistics.median(ds), _pct(ds, 25), _pct(ds, 75), _pct(ds, 95)))

    # ---- what the difference is worth in slots and in fees ------------------ #
    later = [d for _, d, _mk in rows if d is not None and d > GRID_H]
    extra = sum(later) / 24.0
    censored = len([1 for _, d, _mk in rows if d is None])
    print()
    print("  SLOT COST OF THE FINER GRID — extra time the study's rule would have held:")
    print("    %d exits beyond the grid width, Σ %.1f slot-days = %+.1f %% on top of the live %.1f"
          % (len(later), extra, 100.0 * extra / slot_days if slot_days else 0.0, slot_days))
    print("    (%d more never trigger inside the horizon and are NOT counted — the true study" % censored)
    print("     draw is higher, so this is a floor on the difference, not an estimate)")

    # ---- and in price ------------------------------------------------------- #
    pairs = [(realized_mark(m), mk) for m, d, mk in rows
             if mk is not None and m["close_reason"] == "TRAIL" and d is not None and abs(d) <= GRID_H]
    pairs = [(a, b) for a, b in pairs if a is not None]
    if pairs:
        print()
        print("  where the two exits land within one bar of each other, only execution separates them:")
        print("    n=%d  live Ø %+.2f %%  vs 15m stop-level Ø %+.2f %%  → Δ %+.2f %%-points per trade"
              % (len(pairs), statistics.mean([a for a, _ in pairs]), statistics.mean([b for _, b in pairs]),
                 statistics.mean([a - b for a, b in pairs])))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="2026-07-26", help="mirrors opened on/after this date (Bot 40 go-live)")
    ap.add_argument("--horizon-h", type=float, default=24.0,
                    help="how far past a live exit the 15m counterfactual may look (right-censor bound)")
    ap.add_argument("--no-replay", action="store_true", help="skip section 6 (no candle read)")
    args = ap.parse_args()

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    study = load_study()

    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        mirrors, series = load(conn, since, not args.no_replay)
    finally:
        conn.close()

    report(mirrors, series, study, datetime.now(timezone.utc), args.horizon_h)
    return 0


if __name__ == "__main__":
    sys.exit(main())

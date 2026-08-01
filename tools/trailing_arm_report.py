"""T-2026-KYT-9050-054 — what did the trailing arm (Bot 40) actually do?

Why this tool exists
--------------------
`tools/trailing_book_health.py` (T-052) SIMULATES exit rules on historical fleet
trades. This one reports the LIVE arm: what Bot 40 really mirrored, really exited,
and what it is still holding. The two answer different questions and neither
replaces the other — the study picks a rule, this measures the rule in production.

Three things it refuses to get wrong, each of which cost a full analysis pass:

1. **SL marks are reconstructed, not dropped.** Until the T-053 fix went live
   (2026-07-30 07:50) every `SL_HIT` was booked with `close_mark_pct = NULL`, so a
   naive `SUM(close_mark_pct)` silently omits the WORST exits. Over the live book
   that reads −194 % instead of −581 % gross — a factor 3, and optimistic exactly
   where it hurts. The fill sits on the stop level and the level is in the row, so
   `core.trailing_state.mark_pct(entry, sl, is_long)` recovers it (Regel 7: same
   source as the live path). Rows without `sl` stay unknown and are COUNTED as
   unknown rather than treated as zero.

2. **`closed_ai_signals.id` is its OWN sequence — it is NOT `ai_signals.id`.**
   Joining `trailing_positions.src_signal_id` onto it looks perfectly reasonable
   and is nearly all noise: of 4611 rows so joined, 9 even had a matching symbol.
   It produced "hold would have returned +2 600 837 %" and a completely inverted
   verdict. The source trade is matched on (symbol, model, direction) + time
   instead, and every match is sanity-checked against the entry price.

3. **`closed_ai_signals.open_time` / `close_time` are NAIVE PG-LOCAL (+03).**
   `trailing_positions.opened_at` is `timestamptz`. Normalising the mirror side
   with `AT TIME ZONE 'UTC'` shifts it −3 h and the match window comes back empty
   (2 of 461). Compare against `opened_at::timestamp` — local wall clock on both
   sides. (`candles.open_time` IS tz-aware, so the benchmark path is unaffected.)

The counterfactual is deliberately narrow
-----------------------------------------
Only `TRAIL` and `TIME_STOP` are the arm's OWN decisions. On `SOURCE_CLOSED` and
`SL_HIT` the mirror merely follows the fleet, so arm == hold there BY CONSTRUCTION
and including them would dilute the measured effect toward zero. A mirror also only
has a counterfactual once its SOURCE trade has closed; sources still open are
reported separately and marked to market, because the resolved subset is
self-selected toward fast closes (i.e. stop-outs) and reading it alone flatters the
arm.

Market context is not decoration
--------------------------------
A market-wide dump landed days after go-live (2026-07-26). Read without it, the
book says LONG −648 vs SHORT −7 %-points and invites exactly one conclusion — kill
the long side. That conclusion does not follow from a book whose long legs were
held through a falling tape, so the report carries an equal-weight altcoin index
(median hourly return across the coin universe) and attributes each trade's mark
against the index move over ITS OWN holding window. Attribution assumes beta = 1;
alts typically run hotter than that, so the market's share is a LOWER bound and the
leg-specific residual an upper bound.

Read-only: SELECTs against trailing_positions, ai_signals, closed_ai_signals and
candles. No writes, no live effect.

Usage:
    python tools/trailing_arm_report.py
    python tools/trailing_arm_report.py --since 2026-07-28 --no-live-prices
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.trailing_state import mark_pct  # noqa: E402

#: Taker round-trip in unlevered percent — repo convention (tools/audit/step4_results.py).
FEE_RT = 0.10

#: A matched source trade must agree with the mirror's market entry to within this
#: fraction. The mirror enters at market up to ~240 s after the signal, so a few
#: tenths of a percent of drift is expected; 3 % apart means the lateral join found a
#: DIFFERENT trade of the same leg, and silently comparing against it is how the
#: id-join disaster (trap 2) stayed invisible for a whole pass.
ENTRY_TOL = 0.03

#: Exits the arm decided itself. Everything else is the mirror following the fleet.
ARM_REASONS = ("TRAIL", "TIME_STOP")

#: Exits that represent a real position outcome (as opposed to an admission reject
#: like PREEXISTING / SHADOW_CARRYOVER / ENTRY_NOT_FILLED).
REAL_EXIT_REASONS = ("TRAIL", "TIME_STOP", "SL_HIT", "SOURCE_CLOSED", "LEG_RETIRED")

#: Mirrors opened before this are exempt from the time-stop (operator decision,
#: 2026-07-28). Kept here so the report can show what the exemption is costing.
GRANDFATHER_CUTOFF = datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Pure logic — no DB, pinned in backtest/test_trailing_arm_report.py           #
# --------------------------------------------------------------------------- #
def realized_mark(row: dict) -> float | None:
    """The mark a closed mirror actually realised, or None if genuinely unknown.

    ``close_mark_pct`` when the bot booked one. For an SL hit booked before the
    T-053 fix the column is NULL although the outcome is known exactly: the fill
    sits on the stop level, and the level is in the row. Same assumption as the
    study makes for hard stops — **fill at the stop level, no slippage** — which
    makes the value the optimistic edge, not the expectation: a gap through the
    stop realises worse.

    An SL row without ``sl`` (pre-T-049) stays None. Guessing the level there is
    precisely the error the original NULL-booking was trying to avoid.
    """
    if row.get("close_mark_pct") is not None:
        return float(row["close_mark_pct"])
    if row.get("close_reason") != "SL_HIT":
        return None
    sl, entry = row.get("sl"), row.get("entry")
    if sl is None or entry is None or float(entry) <= 0:
        return None
    return mark_pct(float(entry), float(sl), row["direction"] == "LONG")


def entry_scale_ok(mirror_entry: float, source_entry: float, tol: float = ENTRY_TOL) -> bool:
    """Does a matched source trade plausibly belong to this mirror?

    The guard that would have caught trap 2 on the first pass: a NEIROUSDT mirror
    entered at 5.8e-05 was "matched" to a source trade whose entry was 12.987.
    """
    if mirror_entry is None or source_entry is None:
        return False
    mirror_entry, source_entry = float(mirror_entry), float(source_entry)
    if mirror_entry <= 0 or source_entry <= 0:
        return False
    return abs(source_entry / mirror_entry - 1.0) <= tol


def classify_arm_exit(m: dict) -> tuple[str, float | None]:
    """Does this arm exit have a usable hold counterfactual? ("resolved", hold) or
    ("unresolved-<why>", None).

    Order matters and is the whole point. ``src_still_open`` is checked FIRST: the
    lateral join is keyed on (symbol, model, direction) + time, NOT on the source id,
    so while the real source is still live the join can match an EARLIER trade of the
    same leg that opened inside the window and closed after the mirror did. Booking
    that as "what holding returned" would compare against a different trade entirely
    — the same class of error as the id-join, just quieter.
    """
    if m.get("src_still_open"):
        return "unresolved-source-open", None
    if m.get("src_close_price") is None or m.get("src_entry") is None:
        return "unresolved-no-match", None
    if not entry_scale_ok(m.get("entry"), m.get("src_entry")):
        return "unresolved-entry-mismatch", None
    hold = mark_pct(float(m["entry"]), float(m["src_close_price"]), m["direction"] == "LONG")
    return "resolved", hold


def market_implied(market_move_pct: float, is_long: bool) -> float:
    """The part of a trade's mark the market alone would have delivered (beta = 1).

    A LONG in a tape that fell 5 % carries −5 % of pure market; the same tape hands
    a SHORT +5 %. Subtracting this from the realised mark leaves the leg-specific
    residual.
    """
    return market_move_pct if is_long else -market_move_pct


def net_of_fees(marks: list[float]) -> float:
    """Sum of unlevered marks minus one round-trip fee per trade."""
    return sum(marks) - FEE_RT * len(marks)


def summarize(marks: list[float]) -> dict:
    """Headline stats over a list of unlevered marks."""
    if not marks:
        return {"n": 0, "gross": 0.0, "net": 0.0, "mean": 0.0, "win_rate": 0.0,
                "mean_win": 0.0, "mean_loss": 0.0}
    wins = [m for m in marks if m > 0]
    losses = [m for m in marks if m <= 0]
    return {
        "n": len(marks),
        "gross": sum(marks),
        "net": net_of_fees(marks),
        "mean": statistics.mean(marks),
        "win_rate": 100.0 * len(wins) / len(marks),
        "mean_win": statistics.mean(wins) if wins else 0.0,
        "mean_loss": statistics.mean(losses) if losses else 0.0,
    }


def build_index(rows: list[tuple]) -> list[tuple[datetime, float]]:
    """Equal-weight altcoin index from (open_time, symbol, close) hourly candles.

    Per hour the MEDIAN cross-sectional return, compounded. Median rather than mean
    because a fresh listing printing +300 % in its first hour would otherwise BE the
    market. Symbols entering or leaving mid-window simply contribute to the hours
    they cover, so a symbol's first observation never enters as a return.
    """
    per_symbol: dict[str, list[tuple[datetime, float]]] = {}
    for ts, sym, close in rows:
        if close is None or float(close) <= 0:
            continue
        per_symbol.setdefault(sym, []).append((ts, float(close)))

    returns_by_ts: dict[datetime, list[float]] = {}
    for series in per_symbol.values():
        series.sort(key=lambda x: x[0])
        for (_, prev), (ts, cur) in zip(series, series[1:]):
            if prev > 0:
                returns_by_ts.setdefault(ts, []).append(cur / prev - 1.0)

    level = 1.0
    out: list[tuple[datetime, float]] = []
    for ts in sorted(returns_by_ts):
        level *= 1.0 + statistics.median(returns_by_ts[ts])
        out.append((ts, level))
    return out


def index_move_pct(index: list[tuple[datetime, float]], t0: datetime, t1: datetime) -> float | None:
    """Percent move of the index between two instants, using the last level at or
    before each.

    None when either end falls outside coverage, with ONE bar of tolerance on the
    right. The distinction matters and is easy to get wrong in both directions:

      * A trade that closed mid-bar (the ordinary case — the index is hourly and
        trades are not) is measured to the last completed hour. That sub-bar gap is
        inherent granularity, not missing data, so it must NOT be rejected.
      * A trade that closed well past the newest index point is a different animal:
        clamping it to the last level attributes it against a stale tape, and since
        that can only ever hit the most RECENT trades it biases the freshest numbers
        in one direction. Those are reported as uncovered — the same stance the tool
        takes on an SL row without a stop level: an unknown stays unknown rather than
        being filled in with the nearest plausible value.

    The bar width is inferred from the index spacing rather than hard-coded, so the
    rule survives a caller passing a different timeframe.
    """
    if not index or t0 is None or t1 is None:
        return None
    if t0 < index[0][0]:
        return None
    if len(index) >= 2:
        bar = min(b[0] - a[0] for a, b in zip(index, index[1:]))
        if t1 > index[-1][0] + bar:
            return None
    elif t1 > index[-1][0]:
        return None
    lo = _level_at(index, t0)
    hi = _level_at(index, t1)
    if lo is None or hi is None or lo <= 0:
        return None
    return (hi / lo - 1.0) * 100.0


def _level_at(index: list[tuple[datetime, float]], t: datetime) -> float | None:
    lvl = None
    for ts, level in index:
        if ts <= t:
            lvl = level
        else:
            break
    return lvl


# --------------------------------------------------------------------------- #
# SQL — the two join traps live here, spelled out                             #
# --------------------------------------------------------------------------- #
SQL_MIRRORS = """
    SELECT t.id, t.src_signal_id, t.symbol, t.model, t.direction, t.entry, t.sl,
           t.peak_pct, t.opened_at, t.filled_at, t.closed_at, t.close_reason,
           t.close_mark_pct,
           (a.id IS NOT NULL) AS src_still_open,
           c.entry        AS src_entry,
           c.close_price  AS src_close_price,
           c.close_time   AS src_close_time,
           c.status       AS src_status
    FROM trailing_positions t
    -- The source signal is still live exactly while its ai_signals row exists;
    -- once the fleet closes it the row MOVES to closed_ai_signals and the id is gone.
    LEFT JOIN ai_signals a ON a.id = t.src_signal_id
    -- NOT `ON c.id = t.src_signal_id`: closed_ai_signals.id is its own sequence
    -- (trap 2). Match the trade by identity + time, then verify via entry price.
    -- `t.opened_at::timestamp` and not `AT TIME ZONE 'UTC'`: closed_ai_signals
    -- timestamps are naive PG-LOCAL, so UTC-normalising the mirror side shifts the
    -- window by the local offset and matches nothing (trap 3).
    LEFT JOIN LATERAL (
        SELECT c.*
        FROM closed_ai_signals c
        WHERE c.symbol = t.symbol AND c.model = t.model AND c.direction = t.direction
          AND c.open_time <= t.opened_at::timestamp
          AND c.open_time >  t.opened_at::timestamp - INTERVAL '90 minutes'
          AND c.close_time >= t.opened_at::timestamp
          AND c.close_price IS NOT NULL
        ORDER BY c.open_time DESC
        LIMIT 1
    ) c ON TRUE
    WHERE t.opened_at >= %s
    ORDER BY t.opened_at
"""

SQL_INDEX = """
    SELECT open_time, symbol, close
    FROM candles
    WHERE tf = '1h' AND open_time >= %s AND is_closed
    ORDER BY symbol, open_time
"""


def load(conn, since: datetime):
    with conn.cursor() as cur:
        cur.execute(SQL_MIRRORS, (since,))
        cols = [d[0] for d in cur.description]
        mirrors = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute(SQL_INDEX, (since,))
        index = build_index(cur.fetchall())
    return mirrors, index


# --------------------------------------------------------------------------- #
# Report                                                                      #
# --------------------------------------------------------------------------- #
def _p(label: str, s: dict) -> None:
    print("  %-22s n=%4d  net=%+9.1f  mean=%+6.2f  win=%3.0f%%  Ø win=%+5.2f  Ø loss=%+6.2f"
          % (label, s["n"], s["net"], s["mean"], s["win_rate"], s["mean_win"], s["mean_loss"]))


def report(mirrors: list[dict], index, prices: dict) -> None:
    filled = [m for m in mirrors if m["filled_at"] is not None]
    closed = [m for m in filled if m["closed_at"] is not None and m["close_reason"] in REAL_EXIT_REASONS]
    open_now = [m for m in mirrors if m["closed_at"] is None]

    print("=" * 78)
    print("POPULATION")
    print("=" * 78)
    rejects = len(mirrors) - len(filled) - len([m for m in open_now if m["filled_at"] is None])
    print("  mirror rows=%d  filled=%d  closed real exits=%d  open now=%d  admission rejects=%d"
          % (len(mirrors), len(filled), len(closed), len(open_now), rejects))

    # ---- realised, with SL reconstruction ---------------------------------- #
    recs, unknown, reconstructed = [], 0, 0
    for m in closed:
        mk = realized_mark(m)
        if mk is None:
            unknown += 1
            continue
        if m["close_mark_pct"] is None:
            reconstructed += 1
        recs.append((m, mk))

    print()
    print("=" * 78)
    print("REALISED  (unlevered %%, fee %.2f%% round-trip)" % FEE_RT)
    print("=" * 78)
    naive = [float(m["close_mark_pct"]) for m, _ in recs if m["close_mark_pct"] is not None]
    print("  SL marks reconstructed=%d   still unknown (no sl on the row)=%d" % (reconstructed, unknown))
    print("  naive SUM(close_mark_pct) would read %+.1f gross — the reconstruction moves it to %+.1f"
          % (sum(naive), sum(mk for _, mk in recs)))
    print()
    _p("ALL", summarize([mk for _, mk in recs]))
    print()
    for reason in REAL_EXIT_REASONS:
        sub = [mk for m, mk in recs if m["close_reason"] == reason]
        if sub:
            _p(reason, summarize(sub))
    print()
    for d in ("LONG", "SHORT"):
        _p("dir " + d, summarize([mk for m, mk in recs if m["direction"] == d]))

    print()
    print("  by day (net):")
    days: dict = {}
    for m, mk in recs:
        days.setdefault(m["closed_at"].date(), []).append(mk)
    cum = 0.0
    for day in sorted(days):
        cum += net_of_fees(days[day])
        print("    %s  n=%4d  net=%+8.1f  cum=%+9.1f" % (day, len(days[day]), net_of_fees(days[day]), cum))

    # ---- market attribution ------------------------------------------------ #
    print()
    print("=" * 78)
    print("MARKET CONTEXT  (equal-weight altcoin index, median hourly return)")
    print("=" * 78)
    if not index:
        print("  no 1h candle coverage in the window — attribution skipped")
    else:
        print("  index spans %s .. %s  (%d hourly points), total move %+.2f %%"
              % (index[0][0].date(), index[-1][0].date(), len(index),
                 (index[-1][1] / index[0][1] - 1.0) * 100.0))
        print("  index by day (close-to-close):")
        by_day: dict = {}
        for ts, lvl in index:
            by_day[ts.date()] = lvl
        prev = None
        for day in sorted(by_day):
            if prev is not None:
                print("    %s  %+6.2f %%" % (day, (by_day[day] / prev - 1.0) * 100.0))
            prev = by_day[day]

        print()
        print("  attribution over each trade's OWN holding window (beta=1 →")
        print("  market share is a LOWER bound, leg residual an UPPER bound):")
        print("    %-6s %5s %10s %12s %12s" % ("dir", "n", "realised", "market-impl", "residual"))
        for d in ("LONG", "SHORT"):
            rows = []
            for m, mk in recs:
                mv = index_move_pct(index, m["opened_at"], m["closed_at"])
                if mv is not None and m["direction"] == d:
                    rows.append((mk, market_implied(mv, d == "LONG")))
            if rows:
                print("    %-6s %5d %10.1f %12.1f %12.1f"
                      % (d, len(rows), sum(r[0] for r in rows), sum(r[1] for r in rows),
                         sum(r[0] - r[1] for r in rows)))

    # ---- open book --------------------------------------------------------- #
    print()
    print("=" * 78)
    print("OPEN BOOK")
    print("=" * 78)
    book = []
    for m in open_now:
        p = prices.get(m["symbol"])
        if p is None or not m["entry"]:
            continue
        book.append((m, mark_pct(float(m["entry"]), p, m["direction"] == "LONG")))
    if not book:
        print("  no live prices available (or book empty) — open leg not marked")
    else:
        print("  %d of %d positions priced" % (len(book), len(open_now)))
        for d in ("LONG", "SHORT", None):
            sub = [(m, mk) for m, mk in book if d is None or m["direction"] == d]
            if not sub:
                continue
            ms = [mk for _, mk in sub]
            print("    %-6s n=%3d  Σmark=%+8.1f  mean=%+6.2f  underwater=%3.0f%%  worst=%+6.1f"
                  % (d or "TOTAL", len(sub), sum(ms), statistics.mean(ms),
                     100.0 * len([x for x in ms if x < 0]) / len(sub), min(ms)))

        gf = [(m, mk) for m, mk in book if m["opened_at"] < GRANDFATHER_CUTOFF]
        if gf:
            now = datetime.now(timezone.utc)
            ages = [(now - m["opened_at"]).total_seconds() / 3600.0 for m, _ in gf]
            armed = len([1 for m, _ in gf if (m["peak_pct"] or -999) >= 2.0])
            print("    time-stop exempt (opened < %s): n=%d  Σmark=%+.1f  median age=%.0fh"
                  "  max=%.0fh  armed=%d"
                  % (GRANDFATHER_CUTOFF.date(), len(gf), sum(mk for _, mk in gf),
                     statistics.median(ages), max(ages), armed))
            print("      → none of the unarmed ones can ever be trailed out (peak < activation);")
            print("        each also holds its symbol's only mirror slot.")

    # ---- counterfactual ---------------------------------------------------- #
    print()
    print("=" * 78)
    print("ARM EXITS vs HOLDING TO THE FLEET'S OWN CLOSE")
    print("=" * 78)
    print("  (TRAIL/TIME_STOP only — on SOURCE_CLOSED/SL_HIT arm == hold by construction)")
    arm = [(m, mk) for m, mk in recs if m["close_reason"] in ARM_REASONS]
    resolved, unresolved, mismatched = [], [], 0
    for m, mk in arm:
        state, hold = classify_arm_exit(m)
        if state == "resolved":
            resolved.append((m, mk, hold))
            continue
        if state == "unresolved-entry-mismatch":
            mismatched += 1
        unresolved.append((m, mk))

    print("  arm decisions=%d | resolved=%d | unresolved=%d (source still open=%d, entry mismatch=%d)"
          % (len(arm), len(resolved), len(unresolved),
             len([1 for m, _ in unresolved if m["src_still_open"]]), mismatched))

    def _delta(label, sub):
        if not sub:
            return
        dl = [a - h for _, a, h in sub]
        print("  %-20s n=%4d | arm=%+8.1f | hold=%+8.1f | Δ=%+8.1f | meanΔ=%+.3f | armBetter=%3.0f%%"
              % (label, len(sub), net_of_fees([a for _, a, _ in sub]),
                 net_of_fees([h for _, _, h in sub]), sum(dl), statistics.mean(dl),
                 100.0 * len([x for x in dl if x > 0]) / len(dl)))

    if resolved:
        print()
        _delta("ALL", resolved)
        for reason in ARM_REASONS:
            _delta(reason, [r for r in resolved if r[0]["close_reason"] == reason])
        for d in ("LONG", "SHORT"):
            _delta("dir " + d, [r for r in resolved if r[0]["direction"] == d])
        holds = sorted(h for _, _, h in resolved)
        print("  hold-side distribution: min=%+.2f p25=%+.2f median=%+.2f p75=%+.2f max=%+.2f"
              " — a loss in %.0f%% of them"
              % (holds[0], holds[len(holds) // 4], statistics.median(holds),
                 holds[3 * len(holds) // 4], holds[-1],
                 100.0 * len([h for h in holds if h < 0]) / len(holds)))

    live_pairs = []
    for m, mk in unresolved:
        p = prices.get(m["symbol"])
        if p is not None and m["src_still_open"] and m["entry"]:
            live_pairs.append((m, mk, mark_pct(float(m["entry"]), p, m["direction"] == "LONG")))
    if live_pairs:
        print()
        print("  sources STILL OPEN — hold leg marked to market now (the resolved subset")
        print("  above is biased toward fast closes, i.e. stop-outs, so read both):")
        _delta("still-open (MTM)", live_pairs)

    if resolved or live_pairs:
        total = sum(a - h for _, a, h in resolved) + sum(a - h for _, a, h in live_pairs)
        print()
        print("  arm effect so far, both legs combined: Δ %+.1f %%-points" % total)

    # ---- bottom line ------------------------------------------------------- #
    print()
    print("=" * 78)
    realised_net = net_of_fees([mk for _, mk in recs])
    open_sum = sum(mk for _, mk in book)
    print("TOTAL EQUITY  realised %+.1f  +  open mark %+.1f  =  %+.1f %%-points (unlevered)"
          % (realised_net, open_sum, realised_net + open_sum))
    print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="2026-07-26",
                    help="only mirrors opened on/after this date (default: Bot 40 go-live)")
    ap.add_argument("--no-live-prices", action="store_true",
                    help="skip the Binance batch call; the open book is then not marked")
    args = ap.parse_args()

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        mirrors, index = load(conn, since)
    finally:
        conn.close()

    prices = {}
    if not args.no_live_prices:
        from core.live_price import get_live_prices_batch

        prices = get_live_prices_batch()

    report(mirrors, index, prices)
    return 0


if __name__ == "__main__":
    sys.exit(main())

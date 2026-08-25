"""Shared cold-start catch-up for the trade monitors (T-2026-KYT-9050-152).

Both monitors keep their per-trade `last_checked` watermark in memory, so every
process restart used to fall into the "no watermark -> newest candle only" branch
and the whole downtime gap went unscored. T-2026-KYT-9050-150 fixed that for
`8_ai_trade_monitor.py`; `5_trade_monitor.py` still carried the old behaviour -
the two had already drifted once, which is why this logic lives here now instead
of being copied a second time.

The pieces are deliberately pure (no I/O, no logging, no DB): the caller owns the
state file path, the logger and the poll loop. That keeps them testable without a
database, which is the only way the monitors get tested at all.

What a caller has to wire up, in this order:

    1. On start, read the persisted watermark and call `resolve_catchup()`. It
       returns the replay start or None; None means "score the newest candle
       only", i.e. the pre-T-150 behaviour.
    2. Feed `catchup_floor()` into BOTH the per-coin candle fetch and the
       per-trade candle filter. Wiring only the second one filters a one-candle
       result set and silently does nothing.
    3. After each completed pass, call `should_disarm_catchup()` and only clear
       the catch-up when it says so.
    4. Persist the pass end (throttled) so the next cold start has a watermark.

Deliberately NOT here: how a close is timestamped. `8_ai_trade_monitor` stamps
`closed_ai_signals.close_time` from the triggering candle, while
`5_trade_monitor` leaves `closed_trades_master.posted` at wall clock, because
other scripts compare `posted` against `datetime.now() - timedelta` to decide
whether a trade closed recently - a replayed backlog would write timestamps a
whole downtime in the past and those checks would skip the rows. That asymmetry
is intentional and belongs to the individual monitors, not here.
"""

from __future__ import annotations

import datetime

import pytz

# Beyond this the gap is not replayed: an unbounded catch-up would score days of
# candles in one pass and is a book-repair job, not a monitor job.
MAX_CATCHUP_HOURS = 48.0
# Re-scan a little before the watermark. Re-scoring an already scored candle is a
# no-op (a closed trade is gone from the active table), missing one is not.
CATCHUP_OVERLAP_MIN = 15
# A permanently stale coin must not keep the catch-up armed forever (every pass
# would re-fetch the whole gap). ~10 min at the 10s poll cadence.
CATCHUP_MAX_PASSES = 60
STATE_WRITE_INTERVAL_S = 60.0


def resolve_catchup(wm_raw, now_utc):
    """Decide the cold-start replay start from the persisted watermark.

    Pure: no I/O, no logging. Returns (catchup_from | None, log_level, message).
    A None start means "score the newest candle only" - the pre-T-150 behaviour.
    Every degenerate input (missing, unreadable, in the future, beyond the cap)
    degrades to that rather than replaying something wrong.
    """
    if not wm_raw:
        return None, "info", "cold start: no persisted watermark - scoring the newest candle only."
    try:
        wm = datetime.datetime.fromisoformat(wm_raw)
    except (ValueError, TypeError):
        return None, "warning", f"cold start: unreadable watermark {wm_raw!r} - scoring the newest candle only."
    if wm.tzinfo is None:
        wm = wm.replace(tzinfo=pytz.UTC)
    gap_h = (now_utc - wm).total_seconds() / 3600.0
    if gap_h < 0:
        return None, "warning", f"cold start: watermark {wm_raw} is in the future - ignoring it."
    if gap_h > MAX_CATCHUP_HOURS:
        return (
            None,
            "warning",
            f"cold start: {gap_h:.1f}h gap exceeds the {MAX_CATCHUP_HOURS}h catch-up cap - "
            "scoring the newest candle only. The gap stays unscored; repair the book out of band.",
        )
    start = wm - datetime.timedelta(minutes=CATCHUP_OVERLAP_MIN)
    return (
        start,
        "info",
        f"cold start: catch-up armed - replaying 5m candles from {start.isoformat()} ({gap_h:.2f}h gap).",
    )


def catchup_floor(catchup_from, open_time):
    """Cold-start scan start for one trade - never before its own open_time.

    A trade opened during the downtime must not be scored against candles from
    before it existed. Accepts a naive open_time (both monitors read one straight
    out of the DB) and reads it as UTC.
    """
    if catchup_from is None:
        return None
    ot = open_time
    if ot is not None and ot.tzinfo is None:
        ot = ot.replace(tzinfo=pytz.UTC)
    if ot is not None and ot > catchup_from:
        return ot
    return catchup_from


def should_disarm_catchup(active_ids, scored_ids, passes):
    """Whether the cold-start catch-up may stop. Returns (disarm, unscored).

    Only once EVERY active trade carries a watermark: trades on coins with stale
    5m data are skipped by the stale guard and never get one, so disarming after
    the first pass would drop their gap for good - and if ingestion was down with
    the fleet, that is every coin. Bounded by CATCHUP_MAX_PASSES so a permanently
    stale coin cannot keep the catch-up armed forever; the caller reports the
    unscored count when it gives up.
    """
    unscored = set(active_ids) - set(scored_ids)
    return (not unscored) or passes >= CATCHUP_MAX_PASSES, unscored

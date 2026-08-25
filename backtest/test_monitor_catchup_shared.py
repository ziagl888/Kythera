"""Standalone (DB-free) guards for the shared monitor cold-start catch-up
(`core/monitor_catchup.py`, T-2026-KYT-9050-152).

These pin the three pure helpers both trade monitors depend on. They used to live
in `test_monitor_coldstart_catchup.py` next to bot 8; when `5_trade_monitor.py`
was ported onto the same mechanism (it had drifted a full release behind bot 8 —
that drift is the whole reason the logic moved into core), the helper tests moved
with the code. What stayed behind is what is genuinely bot-specific: how a close
is timestamped, and whether each bot actually wires the helpers up.

Why this matters at all: `last_checked` in both monitors is an in-memory
watermark, so a process restart used to score every open trade against a single
5m candle and drop the whole downtime gap. After the 2026-08-20 watchdog kill
(72h ExecutionTimeLimit) that mis-booked 1315 close_time values and 525
targets_hit in one 40-second sweep.

Run: python backtest/test_monitor_catchup_shared.py
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.monitor_catchup import (  # noqa: E402  (path must be set before the import)
    CATCHUP_MAX_PASSES,
    CATCHUP_OVERLAP_MIN,
    MAX_CATCHUP_HOURS,
    catchup_floor,
    resolve_catchup,
    should_disarm_catchup,
)

NOW = datetime.datetime(2026, 8, 21, 5, 49, 1, tzinfo=pytz.UTC)


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


# ── the cold-start decision ───────────────────────────────────────────────────
def test_no_watermark_falls_back_to_newest_candle_only():
    start, level, msg = resolve_catchup(None, NOW)
    assert start is None, "without a watermark we must not invent a replay window"
    assert level == "info"
    assert "newest candle only" in msg


def test_real_outage_gap_arms_the_replay():
    """The 2026-08-20 case: watchdog died 13:57:43 UTC, monitor back 05:49:01."""
    wm = datetime.datetime(2026, 8, 20, 13, 57, 43, tzinfo=pytz.UTC)
    start, level, msg = resolve_catchup(_iso(wm), NOW)
    assert start is not None, "a 15.9h gap is inside the cap and must be replayed"
    assert level == "info"
    assert "catch-up armed" in msg
    # the overlap must reach back BEFORE the watermark, never after it
    assert start < wm
    assert wm - start == datetime.timedelta(minutes=CATCHUP_OVERLAP_MIN)


def test_gap_beyond_the_cap_is_refused_not_silently_truncated():
    wm = NOW - datetime.timedelta(hours=MAX_CATCHUP_HOURS + 1)
    start, level, msg = resolve_catchup(_iso(wm), NOW)
    assert start is None, "an over-cap gap must not be replayed"
    assert level == "warning", "and it must be loud - the book stays wrong until repaired"
    assert "exceeds" in msg and "repair the book out of band" in msg


def test_gap_just_inside_the_cap_is_still_armed():
    wm = NOW - datetime.timedelta(hours=MAX_CATCHUP_HOURS - 0.1)
    start, _level, _msg = resolve_catchup(_iso(wm), NOW)
    assert start is not None, "the cap must be an upper bound, not an off-by-one refusal"


def test_future_watermark_is_ignored():
    """Clock skew / a restored backup must not make us skip candles."""
    wm = NOW + datetime.timedelta(hours=3)
    start, level, msg = resolve_catchup(_iso(wm), NOW)
    assert start is None
    assert level == "warning"
    assert "future" in msg


def test_unreadable_watermark_degrades_instead_of_crashing():
    for junk in ("not-a-timestamp", "", 12345, "2026-13-45T99:99:99"):
        start, _level, _msg = resolve_catchup(junk, NOW)
        assert start is None, f"{junk!r} must not arm a replay"


def test_naive_watermark_is_read_as_utc():
    """The persisted value is written with isoformat() from a UTC-aware stamp, but
    an older/hand-edited file may be naive - it must not raise or shift by the
    local +03 offset."""
    start, level, _msg = resolve_catchup("2026-08-20T13:57:43", NOW)
    assert start is not None and level == "info"
    expected = datetime.datetime(2026, 8, 20, 13, 57, 43, tzinfo=pytz.UTC) - datetime.timedelta(
        minutes=CATCHUP_OVERLAP_MIN
    )
    assert start == expected


# ── the per-trade floor ───────────────────────────────────────────────────────
def test_floor_is_none_when_catchup_is_disarmed():
    ot = datetime.datetime(2026, 8, 19, 12, 0, tzinfo=pytz.UTC)
    assert catchup_floor(None, ot) is None, "disarmed must keep the old behaviour"


def test_floor_never_reaches_before_the_trade_opened():
    catchup = datetime.datetime(2026, 8, 20, 13, 42, 43, tzinfo=pytz.UTC)
    opened_later = datetime.datetime(2026, 8, 20, 20, 0, tzinfo=pytz.UTC)
    assert catchup_floor(catchup, opened_later) == opened_later, (
        "a trade opened during the downtime must not be scored against candles from before it existed"
    )


def test_floor_uses_the_catchup_start_for_older_trades():
    catchup = datetime.datetime(2026, 8, 20, 13, 42, 43, tzinfo=pytz.UTC)
    opened_earlier = datetime.datetime(2026, 8, 18, 4, 9, 14, tzinfo=pytz.UTC)
    assert catchup_floor(catchup, opened_earlier) == catchup


def test_floor_handles_naive_open_time():
    """Both monitors read a naive open time straight out of the DB - comparing it
    to an aware catch-up start would raise TypeError inside the poll loop."""
    catchup = datetime.datetime(2026, 8, 20, 13, 42, 43, tzinfo=pytz.UTC)
    # naive on purpose - this is the case under test
    naive_later = datetime.datetime(2026, 8, 20, 20, 0)  # noqa: DTZ001
    assert catchup_floor(catchup, naive_later) == naive_later.replace(tzinfo=pytz.UTC)


# ── the disarm gate ───────────────────────────────────────────────────────────
def test_disarm_only_once_every_trade_was_scored():
    disarm, unscored = should_disarm_catchup({1, 2, 3}, {1, 2, 3}, passes=1)
    assert disarm is True and not unscored


def test_stale_coin_trades_keep_the_catchup_armed():
    """Regression: trades on coins skipped by the stale guard never get a
    watermark. Disarming on the first pass drops their gap for good — and if
    ingestion was down with the fleet, that is every coin."""
    disarm, unscored = should_disarm_catchup({1, 2, 3}, {1, 2}, passes=1)
    assert disarm is False, "must stay armed while a trade is still unscored"
    assert unscored == {3}


def test_disarm_is_bounded_so_a_dead_coin_cannot_pin_the_catchup():
    disarm, unscored = should_disarm_catchup({1, 2, 3}, {1, 2}, passes=CATCHUP_MAX_PASSES)
    assert disarm is True, "a permanently stale coin must not keep re-fetching the gap forever"
    assert unscored == {3}, "and the give-up must be reportable, not silent"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

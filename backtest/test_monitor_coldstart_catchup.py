"""Standalone (DB-free) guards for the AI-trade-monitor cold-start catch-up
(T-2026-KYT-9050-150).

Why this exists: `last_checked` in `8_ai_trade_monitor.py` is an in-memory
watermark. Before this fix every process restart fell into the "no watermark ->
newest candle only" branch, so the whole downtime gap was never scored. After the
2026-08-20 watchdog kill (72h ExecutionTimeLimit) the monitor came back at
2026-08-21 05:49 UTC and closed 943 positions inside 40 seconds against a single
5m candle: 1315 trades got a close_time up to 16h wrong and 525 a wrong
targets_hit. Aggregate PnL was unaffected - this is a book-integrity defect that
poisons roster analytics and the realized-PnL report, not a money loss.

Two things are pinned here:

  (a) `_resolve_catchup` - the cold-start decision. A replay is armed only for a
      sane, bounded gap; a missing, unreadable, future or over-cap watermark must
      fall back to the old newest-candle-only behaviour rather than replay
      something wrong. The cap matters: an unbounded catch-up would score days of
      candles in one pass, which is a book-repair job and not a monitor job.

  (b) `_catchup_floor` - a replay must never reach back before a trade's own
      open_time, otherwise candles from before the signal existed would score it.

  (c) close_time comes from the triggering 5m candle, not NOW(). This was the
      direct cause of the 1315 wrong timestamps and is wrong by one poll gap even
      in normal operation.

Safety note for reviewers: replaying a gap in THIS process cannot fire late
orders - `8_ai_trade_monitor.py` contains no Telegram/Cornix emission at all, it
only writes ai_signals/closed_ai_signals. Test (d) pins that property, because the
whole design rests on it.

The bot is called `8_ai_trade_monitor.py` (digit prefix -> not importable); we
load it via importlib. A DB connection is never opened.

Run: python backtest/test_monitor_coldstart_catchup.py
"""

from __future__ import annotations

import ast
import datetime
import importlib.util
import os
import sys
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_PASSWORD", "test-stub")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-stub")

_spec = importlib.util.spec_from_file_location("ai_trade_monitor", str(ROOT / "8_ai_trade_monitor.py"))
assert _spec and _spec.loader
mon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mon)

NOW = datetime.datetime(2026, 8, 21, 5, 49, 1, tzinfo=pytz.UTC)


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


# ── (a) the cold-start decision ───────────────────────────────────────────────
def test_no_watermark_falls_back_to_newest_candle_only():
    start, level, msg = mon._resolve_catchup(None, NOW)
    assert start is None, "without a watermark we must not invent a replay window"
    assert level == "info"
    assert "newest candle only" in msg


def test_real_outage_gap_arms_the_replay():
    """The 2026-08-20 case: watchdog died 13:57:43 UTC, monitor back 05:49:01."""
    wm = datetime.datetime(2026, 8, 20, 13, 57, 43, tzinfo=pytz.UTC)
    start, level, msg = mon._resolve_catchup(_iso(wm), NOW)
    assert start is not None, "a 15.9h gap is inside the cap and must be replayed"
    assert level == "info"
    assert "catch-up armed" in msg
    # the overlap must reach back BEFORE the watermark, never after it
    assert start < wm
    assert wm - start == datetime.timedelta(minutes=mon.CATCHUP_OVERLAP_MIN)


def test_gap_beyond_the_cap_is_refused_not_silently_truncated():
    wm = NOW - datetime.timedelta(hours=mon.MAX_CATCHUP_HOURS + 1)
    start, level, msg = mon._resolve_catchup(_iso(wm), NOW)
    assert start is None, "an over-cap gap must not be replayed"
    assert level == "warning", "and it must be loud - the book stays wrong until repaired"
    assert "exceeds" in msg and "repair the book out of band" in msg


def test_gap_just_inside_the_cap_is_still_armed():
    wm = NOW - datetime.timedelta(hours=mon.MAX_CATCHUP_HOURS - 0.1)
    start, _level, _msg = mon._resolve_catchup(_iso(wm), NOW)
    assert start is not None, "the cap must be an upper bound, not an off-by-one refusal"


def test_future_watermark_is_ignored():
    """Clock skew / a restored backup must not make us skip candles."""
    wm = NOW + datetime.timedelta(hours=3)
    start, level, msg = mon._resolve_catchup(_iso(wm), NOW)
    assert start is None
    assert level == "warning"
    assert "future" in msg


def test_unreadable_watermark_degrades_instead_of_crashing():
    for junk in ("not-a-timestamp", "", 12345, "2026-13-45T99:99:99"):
        start, _level, _msg = mon._resolve_catchup(junk, NOW)
        assert start is None, f"{junk!r} must not arm a replay"


def test_naive_watermark_is_read_as_utc():
    """The persisted value is written with isoformat() from a UTC-aware stamp, but
    an older/hand-edited file may be naive - it must not raise or shift by the
    local +03 offset."""
    wm_naive = "2026-08-20T13:57:43"
    start, level, _msg = mon._resolve_catchup(wm_naive, NOW)
    assert start is not None and level == "info"
    expected = datetime.datetime(2026, 8, 20, 13, 57, 43, tzinfo=pytz.UTC) - datetime.timedelta(
        minutes=mon.CATCHUP_OVERLAP_MIN
    )
    assert start == expected


# ── (b) the per-trade floor ───────────────────────────────────────────────────
def test_floor_is_none_when_catchup_is_disarmed():
    ot = datetime.datetime(2026, 8, 19, 12, 0, tzinfo=pytz.UTC)
    assert mon._catchup_floor(None, ot) is None, "disarmed must keep the old behaviour"


def test_floor_never_reaches_before_the_trade_opened():
    catchup = datetime.datetime(2026, 8, 20, 13, 42, 43, tzinfo=pytz.UTC)
    opened_later = datetime.datetime(2026, 8, 20, 20, 0, tzinfo=pytz.UTC)
    assert mon._catchup_floor(catchup, opened_later) == opened_later, (
        "a trade opened during the downtime must not be scored against candles from before it existed"
    )


def test_floor_uses_the_catchup_start_for_older_trades():
    catchup = datetime.datetime(2026, 8, 20, 13, 42, 43, tzinfo=pytz.UTC)
    opened_earlier = datetime.datetime(2026, 8, 18, 4, 9, 14, tzinfo=pytz.UTC)
    assert mon._catchup_floor(catchup, opened_earlier) == catchup


def test_floor_handles_naive_open_time():
    """ai_signals.open_time is a naive UTC timestamp - comparing it to an aware
    catch-up start would raise TypeError inside the poll loop."""
    catchup = datetime.datetime(2026, 8, 20, 13, 42, 43, tzinfo=pytz.UTC)
    # naive on purpose - this mirrors ai_signals.open_time and is the case under test
    naive_later = datetime.datetime(2026, 8, 20, 20, 0)  # noqa: DTZ001
    got = mon._catchup_floor(catchup, naive_later)
    assert got == naive_later.replace(tzinfo=pytz.UTC)


# ── (c) + (d) source-level guards ─────────────────────────────────────────────
SRC = (ROOT / "8_ai_trade_monitor.py").read_text(encoding="utf-8")


def test_close_time_is_the_candle_not_wall_clock():
    assert "NOW()" not in SRC.split("INSERT INTO closed_ai_signals")[1][:400], (
        "close_time must be stamped from the triggering 5m candle; NOW() books "
        "wall-clock time and was the cause of the 1315 wrong timestamps"
    )
    assert "close_ts = _close_timestamp(c_ot, open_time)" in SRC


# ── close_time provenance + the same-candle clamp ─────────────────────────────
def test_close_timestamp_uses_the_triggering_candle():
    candle = datetime.datetime(2026, 8, 20, 14, 5, tzinfo=pytz.UTC)
    opened = datetime.datetime(2026, 8, 18, 4, 9, 14)  # noqa: DTZ001 - naive like the DB
    assert mon._close_timestamp(candle, opened) == datetime.datetime(2026, 8, 20, 14, 5)  # noqa: DTZ001


def test_close_timestamp_never_precedes_the_trade_open():
    """Regression: a trade that closes inside the candle it was posted in.

    The forming candle's open_time precedes the signal, so an unclamped stamp
    writes close_time < open_time — a negative holding duration in the book.
    """
    candle = datetime.datetime(2026, 8, 20, 14, 0, tzinfo=pytz.UTC)  # bucket start
    opened = datetime.datetime(2026, 8, 20, 14, 3, 27)  # noqa: DTZ001 - signal posted mid-candle
    assert mon._close_timestamp(candle, opened) == opened, "must clamp at open_time, not backdate"


def test_close_timestamp_returns_naive_utc():
    """closed_ai_signals.close_time is a naive UTC column - an aware value would
    be cast through the session TZ and shift by the local +03 offset."""
    candle = datetime.datetime(2026, 8, 20, 14, 5, tzinfo=pytz.timezone("Europe/Bucharest"))
    got = mon._close_timestamp(candle, None)
    assert got.tzinfo is None
    assert got == candle.astimezone(pytz.UTC).replace(tzinfo=None)


# ── disarm gating ─────────────────────────────────────────────────────────────
def test_disarm_only_once_every_trade_was_scored():
    disarm, unscored = mon._should_disarm_catchup({1, 2, 3}, {1, 2, 3}, passes=1)
    assert disarm is True and not unscored


def test_stale_coin_trades_keep_the_catchup_armed():
    """Regression: trades on coins skipped by the stale guard never get a
    watermark. Disarming on the first pass drops their gap for good — and if
    ingestion was down with the fleet, that is every coin."""
    disarm, unscored = mon._should_disarm_catchup({1, 2, 3}, {1, 2}, passes=1)
    assert disarm is False, "must stay armed while a trade is still unscored"
    assert unscored == {3}


def test_disarm_is_bounded_so_a_dead_coin_cannot_pin_the_catchup():
    disarm, unscored = mon._should_disarm_catchup({1, 2, 3}, {1, 2}, passes=mon.CATCHUP_MAX_PASSES)
    assert disarm is True, "a permanently stale coin must not keep re-fetching the gap forever"
    assert unscored == {3}, "and the give-up must be reportable, not silent"


def _code_identifiers(source: str) -> set[str]:
    """Every name the module actually *executes* - imports, calls, attributes.

    Deliberately AST-based, not a text search: the module documents the very
    property under test in a comment, and a substring check would match its own
    prose instead of real emission code.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
    return {n.lower() for n in names}


def test_monitor_still_emits_nothing():
    """The catch-up is only safe because this process does not post. If someone
    adds an emission here, a replayed backlog would fire 1.5-day-old closes into
    Cornix - hard rule 4. Fail loudly instead."""
    identifiers = _code_identifiers(SRC)
    for needle in ("telegram", "send_message", "signal_post", "outbox", "cornix", "bot.send"):
        hits = sorted(n for n in identifiers if needle in n)
        assert not hits, (
            f"{hits} appeared in executed code of the monitor - the cold-start "
            "catch-up must be re-evaluated before this ships (late close orders)"
        )


def test_catchup_floor_is_actually_wired_into_the_poll_loop():
    """Both call sites matter and the helpers alone prove nothing.

    `coin_min_wm` decides how many candles are FETCHED per coin; the per-trade
    branch decides which of them SCORE. Wiring only the per-trade side would
    filter a one-candle result set and silently change nothing - exactly the bug
    this task fixes, but green.
    """
    calls = [
        n
        for n in ast.walk(ast.parse(SRC))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_catchup_floor"
    ]
    assert len(calls) >= 2, f"_catchup_floor is called {len(calls)}x - both call sites are required"


def test_catchup_is_disarmed_once_the_gate_allows_it():
    """The catch-up must end (else every poll re-scans the whole gap) — but only
    through the gate, never unconditionally after one pass."""
    assert "catchup_from = None" in SRC.split("cold-start catch-up done")[1][:200]


def test_disarm_gate_is_actually_wired_into_the_poll_loop():
    """Same reasoning as the _catchup_floor wiring guard: a pure helper nobody
    calls is a green test over a live bug."""
    calls = [
        n
        for n in ast.walk(ast.parse(SRC))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_should_disarm_catchup"
    ]
    assert len(calls) == 1, f"_should_disarm_catchup is called {len(calls)}x - expected exactly the poll loop"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

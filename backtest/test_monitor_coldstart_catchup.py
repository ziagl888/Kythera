"""Standalone (DB-free) guards for what is specific to `8_ai_trade_monitor.py`
(T-2026-KYT-9050-150, split in T-2026-KYT-9050-152).

The three pure catch-up helpers moved to `core/monitor_catchup.py` when
`5_trade_monitor.py` was ported onto the same mechanism; their tests moved with
them to `test_monitor_catchup_shared.py`. What stays here is what only bot 8
does:

  (a) `close_time` comes from the triggering 5m candle, not `NOW()` — clamped at
      the trade's own open_time. `NOW()` booked wall-clock time, which was the
      direct cause of 1315 wrong timestamps after the 2026-08-20 outage and is
      wrong by one poll gap even in normal operation. This is the asymmetry to
      bot 5, which deliberately leaves `closed_trades_master.posted` at wall
      clock because other scripts use it as a freshness signal.

  (b) The helpers are actually WIRED into the poll loop. A pure helper nobody
      calls is a green test over a live bug.

  (c) The monitor still emits nothing. That property is the entire reason a
      backlog replay is safe here: no Telegram/Cornix call means a replayed
      close cannot reach Cornix late and execute at today's price (hard rule 4).

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

SRC = (ROOT / "8_ai_trade_monitor.py").read_text(encoding="utf-8")


# ── (a) close_time provenance + the same-candle clamp ─────────────────────────
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
    opened = datetime.datetime(2026, 8, 20, 14, 3, 27)  # noqa: DTZ001 - posted mid-candle
    assert mon._close_timestamp(candle, opened) == opened, "must clamp at open_time, not backdate"


def test_close_timestamp_returns_naive_utc():
    """closed_ai_signals.close_time is a naive UTC column - an aware value would
    be cast through the session TZ and shift by the local +03 offset."""
    candle = datetime.datetime(2026, 8, 20, 14, 5, tzinfo=pytz.timezone("Europe/Bucharest"))
    got = mon._close_timestamp(candle, None)
    assert got.tzinfo is None
    assert got == candle.astimezone(pytz.UTC).replace(tzinfo=None)


def test_close_time_is_the_candle_not_wall_clock():
    assert "NOW()" not in SRC.split("INSERT INTO closed_ai_signals")[1][:400], (
        "close_time must be stamped from the triggering 5m candle; NOW() books "
        "wall-clock time and was the cause of the 1315 wrong timestamps"
    )
    assert "close_ts = _close_timestamp(c_ot, open_time)" in SRC


# ── (b) the helpers are wired, not just imported ──────────────────────────────
def _calls_to(source: str, name: str) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def test_catchup_floor_is_actually_wired_into_the_poll_loop():
    """Both call sites matter and importing the helper proves nothing.

    `coin_min_wm` decides how many candles are FETCHED per coin; the per-trade
    branch decides which of them SCORE. Wiring only the per-trade side would
    filter a one-candle result set and silently change nothing — exactly the bug
    T-150 fixed, but green.
    """
    calls = _calls_to(SRC, "_catchup_floor")
    assert len(calls) >= 2, f"_catchup_floor is called {len(calls)}x - both call sites are required"


def test_disarm_gate_is_actually_wired_into_the_poll_loop():
    calls = _calls_to(SRC, "_should_disarm_catchup")
    assert len(calls) == 1, f"_should_disarm_catchup is called {len(calls)}x - expected exactly the poll loop"


def test_catchup_is_disarmed_once_the_gate_allows_it():
    """The catch-up must end (else every poll re-scans the whole gap) — but only
    through the gate, never unconditionally after one pass."""
    assert "catchup_from = None" in SRC.split("cold-start catch-up done")[1][:200]


def test_shared_helpers_come_from_core():
    """Guards the T-152 split: if someone re-inlines a local copy here, the two
    monitors can drift apart again — which is what this task was cleaning up."""
    assert "from core.monitor_catchup import" in SRC
    assert "def _resolve_catchup" not in SRC
    assert "def _catchup_floor" not in SRC
    assert "def _should_disarm_catchup" not in SRC


# ── (c) the no-emission invariant the whole replay rests on ───────────────────
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


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

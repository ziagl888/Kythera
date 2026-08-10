# backtest/test_candle_snapshot_service.py
"""
Unit tests for the refresh scheduling of candle_snapshot_service.py
(T-2026-KYT-9050-132).

DB-free: the DB read (``_read_frame``) is the only thing stubbed out; the
staleness arithmetic, the due-list, the backoff and the sweep fan-out are the
real ones.

Run with: pytest backtest/test_candle_snapshot_service.py -v
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import candle_snapshot_service as svc  # noqa: E402
from core import candle_snapshot as cs  # noqa: E402
from core import fleet  # noqa: E402

CANDLE_COLS = ["symbol", "open_time", "open", "high", "low", "close", "volume"]
_ANCHOR = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
#: half an hour into the next period, so `_ANCHOR` is the newest CLOSED candle
_NOW = _ANCHOR + timedelta(hours=1, minutes=30)


def _frame(n: int = 5, *, end: datetime = _ANCHOR) -> pd.DataFrame:
    rows = [
        ("BTCUSDT", end - timedelta(hours=i), 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0 + i)
        for i in range(n - 1, -1, -1)
    ]
    return pd.DataFrame(rows, columns=CANDLE_COLS)


@pytest.fixture(autouse=True)
def clean_backoff():
    svc._BACKOFF_UNTIL.clear()
    yield
    svc._BACKOFF_UNTIL.clear()


# ── Configuration is pinned to what the nine hourly scanners actually read ────


def test_lookbacks_cover_the_hourly_scanners():
    """The defaults are not arbitrary: bot 14 reads limit=1700, bots 13/34 read a
    95-day 1h window (~2280 candles) and bot 12's joined read asks for 500
    indicator rows. Shrinking these below those numbers turns those bots into
    permanent DB-path fallbacks — silently, because falling back is not an
    error."""
    assert svc.CANDLE_LOOKBACK >= 2280
    assert svc.INDICATOR_LOOKBACK >= 500


def test_default_timeframe_is_the_hourly_scan():
    assert svc.TIMEFRAMES == ["1h"]


def test_lookback_is_per_kind():
    assert svc.lookback_for(cs.KIND_CANDLES) == svc.CANDLE_LOOKBACK
    assert svc.lookback_for(cs.KIND_INDICATORS) == svc.INDICATOR_LOOKBACK


def test_the_service_is_in_the_fleet():
    entry = [p for p in fleet.FLEET if p["script"] == "candle_snapshot_service.py"]
    assert len(entry) == 1
    assert entry[0]["group"] == "core"


# ── due_keys: what a poll costs when the store is warm ────────────────────────


def test_everything_is_due_on_a_cold_store():
    due = svc.due_keys(cs.SnapshotStore(), ["BTCUSDT", "ETHUSDT"], ["1h"], now=_NOW, monotonic=100.0)
    assert sorted(due) == sorted(
        [
            ("BTCUSDT", "1h", cs.KIND_CANDLES),
            ("BTCUSDT", "1h", cs.KIND_INDICATORS),
            ("ETHUSDT", "1h", cs.KIND_CANDLES),
            ("ETHUSDT", "1h", cs.KIND_INDICATORS),
        ]
    )


def test_a_fresh_store_costs_no_db_work():
    """The design's cheap-poll claim: once every frame holds the newest closed
    candle, a poll touches nothing."""
    store = cs.SnapshotStore()
    store.put("BTCUSDT", "1h", cs.KIND_CANDLES, _frame())
    store.put("BTCUSDT", "1h", cs.KIND_INDICATORS, _frame())
    assert svc.due_keys(store, ["BTCUSDT"], ["1h"], now=_NOW, monotonic=100.0) == []


def test_a_new_candle_makes_everything_due_again():
    store = cs.SnapshotStore()
    store.put("BTCUSDT", "1h", cs.KIND_CANDLES, _frame())
    store.put("BTCUSDT", "1h", cs.KIND_INDICATORS, _frame())
    later = _NOW + timedelta(hours=1)
    assert len(svc.due_keys(store, ["BTCUSDT"], ["1h"], now=later, monotonic=100.0)) == 2


def test_backoff_suppresses_a_hopeless_retry():
    """A thin or delisted coin never goes fresh; without the backoff it would be
    re-read on every single poll."""
    key = ("BTCUSDT", "1h", cs.KIND_CANDLES)
    svc._BACKOFF_UNTIL[key] = 500.0
    due = svc.due_keys(cs.SnapshotStore(), ["BTCUSDT"], ["1h"], now=_NOW, monotonic=499.0)
    assert key not in due
    due = svc.due_keys(cs.SnapshotStore(), ["BTCUSDT"], ["1h"], now=_NOW, monotonic=501.0)
    assert key in due


# ── _refresh_one: store, verdict, backoff ─────────────────────────────────────


def test_a_fresh_read_lands_in_the_store_and_clears_the_backoff(monkeypatch):
    key = ("BTCUSDT", "1h", cs.KIND_CANDLES)
    svc._BACKOFF_UNTIL[key] = 999.0
    monkeypatch.setattr(svc, "STORE", cs.SnapshotStore())
    monkeypatch.setattr(svc, "_read_frame", lambda conn, s, tf, k: _frame())
    assert svc._refresh_one(object(), key, _NOW, 100.0) is True
    assert key not in svc._BACKOFF_UNTIL
    assert len(svc.STORE.get(*key)) == 5


def test_a_still_stale_read_is_stored_but_backed_off(monkeypatch):
    """Storing it anyway is deliberate: serve_request refuses it on staleness, so
    it cannot leak, and the rows are already there when the candle lands."""
    key = ("BTCUSDT", "1h", cs.KIND_CANDLES)
    monkeypatch.setattr(svc, "STORE", cs.SnapshotStore())
    monkeypatch.setattr(svc, "_read_frame", lambda conn, s, tf, k: _frame(end=_ANCHOR - timedelta(hours=6)))
    assert svc._refresh_one(object(), key, _NOW, 100.0) is False
    assert svc._BACKOFF_UNTIL[key] == 100.0 + svc.STALE_BACKOFF_SEC
    assert svc.STORE.get(*key) is not None


def test_a_failing_read_rolls_back_and_backs_off(monkeypatch):
    """A failed statement aborts the transaction; the shared connection has to be
    cleaned or every later key in the chunk fails too."""

    class _Conn:
        rolled_back = False

        def rollback(self):
            _Conn.rolled_back = True

    def _boom(conn, symbol, tf, kind):
        raise RuntimeError("relation does not exist")

    key = ("NEWCOINUSDT", "1h", cs.KIND_CANDLES)
    monkeypatch.setattr(svc, "STORE", cs.SnapshotStore())
    monkeypatch.setattr(svc, "_read_frame", _boom)
    assert svc._refresh_one(_Conn(), key, _NOW, 100.0) is False
    assert _Conn.rolled_back is True
    assert svc._BACKOFF_UNTIL[key] == 100.0 + svc.STALE_BACKOFF_SEC
    assert svc.STORE.get(*key) is None


# ── sweep: fan-out over the worker chunks ─────────────────────────────────────


def test_sweep_refreshes_every_due_entry_once(monkeypatch):
    seen: list[tuple[str, str, str]] = []

    def _fake_chunk(keys, now, monotonic):
        seen.extend(keys)
        return len(keys)

    monkeypatch.setattr(svc, "_SYMBOLS", ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    monkeypatch.setattr(svc, "STORE", cs.SnapshotStore())
    monkeypatch.setattr(svc, "_refresh_chunk", _fake_chunk)
    result = svc.sweep()
    assert result["refreshed"] == 6
    assert result["failed"] == 0
    assert sorted(seen) == sorted(set(seen))
    assert len(seen) == 6


def test_sweep_on_a_warm_store_does_nothing(monkeypatch):
    # sweep() reads the real clock, so the fixture is anchored on it too.
    warm = _frame(end=_fresh_anchor(datetime.now(timezone.utc)))
    store = cs.SnapshotStore()
    store.put("BTCUSDT", "1h", cs.KIND_CANDLES, warm)
    store.put("BTCUSDT", "1h", cs.KIND_INDICATORS, warm)
    monkeypatch.setattr(svc, "_SYMBOLS", ["BTCUSDT"])
    monkeypatch.setattr(svc, "STORE", store)
    monkeypatch.setattr(svc, "_refresh_chunk", lambda *a: pytest.fail("a warm store must not trigger a DB read"))
    assert svc.sweep() == {"refreshed": 0, "failed": 0, "duration_sec": 0.0}


def _fresh_anchor(now: datetime) -> datetime:
    from core.candles import last_closed_open_time

    return last_closed_open_time("1h", now)


def test_sweep_counts_the_entries_that_stayed_behind(monkeypatch):
    monkeypatch.setattr(svc, "_SYMBOLS", ["BTCUSDT"])
    monkeypatch.setattr(svc, "STORE", cs.SnapshotStore())
    monkeypatch.setattr(svc, "_refresh_chunk", lambda keys, now, monotonic: 0)
    result = svc.sweep()
    assert result["refreshed"] == 2
    assert result["failed"] == 2


# ── Request dispatch ──────────────────────────────────────────────────────────


def test_health_line_reports_the_gate_state(monkeypatch):
    monkeypatch.delenv(cs.GATE_ENV, raising=False)
    assert svc.handle_line('{"cmd": "health"}')["gate_enabled"] is False
    monkeypatch.setenv(cs.GATE_ENV, "1")
    assert svc.handle_line('{"cmd": "health"}')["gate_enabled"] is True


def test_malformed_input_is_refused_not_raised():
    for line in ("", "[]", "{", '{"cmd": 42}'):
        response = svc.handle_line(line)
        assert response.get("ok") is False, line


def test_store_put_stamps_the_fetch_time():
    store = cs.SnapshotStore()
    before = time.time()
    store.put("BTCUSDT", "1h", cs.KIND_CANDLES, _frame())
    assert store.fetched_at("BTCUSDT", "1h", cs.KIND_CANDLES) >= before


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

# backtest/test_liq_events.py
"""Unit tests for core/liq_events.py — the LQE1 forceOrder liquidation
hypertable module (T-2026-KYT-9050-077, follow-up of MPS1/T-073).

DB-free by construction (test_oi_5m.py pattern): a recording fake connection
captures the DDL/DML the module emits; psycopg2 is mocked at the module
boundary when absent. Pinned here:

  * the DDL contract — PRIMARY KEY (ts, symbol, side, price), TIMESTAMPTZ,
    chunk 1 day, compression after 3 days segmentby=symbol, retention 730 days;
  * the insert contract — batched, ON CONFLICT DO NOTHING, commit on success,
    rollback + re-raise on failure (shared-connection InFailedSqlTransaction
    class, ticker_10s/oi_5m lesson);
  * event parsing — forceOrder payload → aware-UTC ts, value_usdt = z·ap
    (EXECUTED notional, not order size), malformed events dropped with an
    error log (never zero-filled, P0.12), non-forceOrder events ignored.

Also covers the collector's pure buffer logic (_Flusher cap/due) — the only
collector logic that is testable without a websocket.

Run with: pytest backtest/test_liq_events.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

try:  # pragma: no cover - build machine has no libpq; VPS/dev with psycopg2 uses the real one
    import psycopg2.extras  # noqa: F401
except ImportError:
    _psycopg2 = mock.MagicMock()
    sys.modules.setdefault("psycopg2", _psycopg2)
    sys.modules.setdefault("psycopg2.extras", _psycopg2.extras)

from core import liq_events

# ── Recording fakes ───────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, fail_on: str | None = None):
        self.executed: list[tuple[str, tuple | None]] = []
        self._fail_on = fail_on

    def execute(self, sql, params=None):
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError(f"boom on {self._fail_on}")
        self.executed.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, fail_on: str | None = None):
        self.cursor_obj = FakeCursor(fail_on)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _valid_event(**overrides) -> dict:
    o = {
        "s": "BTCUSDT",
        "S": "SELL",
        "o": "LIMIT",
        "q": "0.020",
        "p": "9910.00",
        "ap": "9905.50",
        "X": "FILLED",
        "z": "0.014",
        "T": 1768014460893,
    }
    o.update(overrides)
    return {"e": "forceOrder", "E": 1768014460894, "o": o}


# ── DDL contract ──────────────────────────────────────────────────────────────


def test_ensure_schema_ddl_contract():
    conn = FakeConn()
    liq_events.ensure_schema(conn)
    sqls = [s for s, _ in conn.cursor_obj.executed]
    create = next(s for s in sqls if "CREATE TABLE" in s)
    assert "TIMESTAMPTZ" in create
    assert "PRIMARY KEY (ts, symbol, side, price)" in create
    assert any("create_hypertable" in s and "1 day" in s for s in sqls)
    assert any("compress_segmentby = 'symbol'" in s for s in sqls)
    # segmentby + orderby must cover the FULL primary key — older Timescale
    # versions hard-reject the compression config otherwise (alive-but-dead).
    assert any("compress_orderby = 'ts DESC, side, price'" in s for s in sqls)
    assert any("add_compression_policy" in s and "3 days" in s for s in sqls)
    assert any("add_retention_policy" in s and "730 days" in s for s in sqls)
    assert conn.commits == 1


def test_ensure_schema_rolls_back_and_reraises_on_failure():
    conn = FakeConn(fail_on="create_hypertable")
    with pytest.raises(RuntimeError):
        liq_events.ensure_schema(conn)
    assert conn.rollbacks == 1
    assert conn.commits == 0


# ── Event parsing ─────────────────────────────────────────────────────────────


def test_row_from_force_order_parses_valid_event():
    row = liq_events.row_from_force_order(_valid_event())
    ts, symbol, side, price, avg_price, qty, value = row
    assert ts == datetime.fromtimestamp(1768014460.893, tz=timezone.utc)
    assert ts.tzinfo is not None  # aware UTC, never naive
    assert (symbol, side) == ("BTCUSDT", "SELL")
    assert price == pytest.approx(9910.00)
    assert avg_price == pytest.approx(9905.50)
    assert qty == pytest.approx(0.014)  # cumulative FILLED qty (z), not order size (q)
    assert value == pytest.approx(0.014 * 9905.50)


def test_row_from_force_order_ignores_other_event_types():
    assert liq_events.row_from_force_order({"e": "aggTrade", "o": {}}) is None
    assert liq_events.row_from_force_order({}) is None


def test_row_from_force_order_handles_non_dict_payload():
    # The stream is named @arr — a format change to a JSON array (or any
    # non-dict) must be dropped, never escalate to the connection.
    assert liq_events.row_from_force_order([_valid_event()]) is None  # type: ignore[arg-type]
    assert liq_events.row_from_force_order({"e": "forceOrder", "o": ["not", "a", "dict"]}) is None


def test_row_from_force_order_drops_malformed_never_zero_fills():
    missing_field = _valid_event()
    del missing_field["o"]["ap"]
    assert liq_events.row_from_force_order(missing_field) is None
    garbage_number = _valid_event(z="not-a-number")
    assert liq_events.row_from_force_order(garbage_number) is None


# ── Insert contract ───────────────────────────────────────────────────────────


def test_insert_liq_empty_rows_is_noop():
    conn = FakeConn()
    liq_events.insert_liq(conn, [])
    assert conn.commits == 0
    assert conn.cursor_obj.executed == []


def test_insert_liq_commits_with_on_conflict(monkeypatch):
    captured: dict = {}

    def fake_execute_values(cur, sql, rows, page_size):
        captured["sql"] = sql
        captured["rows"] = rows
        captured["page_size"] = page_size

    monkeypatch.setattr(liq_events, "execute_values", fake_execute_values)
    conn = FakeConn()
    rows = [liq_events.row_from_force_order(_valid_event())]
    liq_events.insert_liq(conn, rows)
    assert "ON CONFLICT (ts, symbol, side, price) DO NOTHING" in captured["sql"]
    assert captured["rows"] == rows
    assert conn.commits == 1


def test_insert_liq_rolls_back_and_reraises_on_failure(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(liq_events, "execute_values", boom)
    conn = FakeConn()
    with pytest.raises(RuntimeError):
        liq_events.insert_liq(conn, [("x",)])
    assert conn.rollbacks == 1
    assert conn.commits == 0


# ── Collector buffer logic (pure part of 41_liq_collector.py) ────────────────


def _load_collector():
    """Import the numeric-prefixed collector module (walkforward_sim pattern);
    websockets is mocked out so the import is dependency-free."""
    for name in ("websockets", "websockets.exceptions", "websockets.sync", "websockets.sync.client"):
        sys.modules.setdefault(name, mock.MagicMock())
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "41_liq_collector.py")
    spec = importlib.util.spec_from_file_location("liq_collector_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_flusher_buffer_cap_drops_oldest():
    collector = _load_collector()
    fl = collector._Flusher()
    for i in range(collector.BUFFER_CAP + 7):
        fl.add((i,))
    assert len(fl.rows) == collector.BUFFER_CAP
    assert fl.rows[0] == (7,)  # oldest dropped, newest kept


def test_flusher_due_by_size_and_time():
    collector = _load_collector()
    fl = collector._Flusher()
    assert not fl.due()  # empty buffer is never due
    fl.add((1,))
    assert not fl.due()  # neither size nor time reached
    fl.last_flush -= collector.FLUSH_INTERVAL_S + 1
    assert fl.due()  # time-based
    fl2 = collector._Flusher()
    for i in range(collector.FLUSH_MAX_ROWS):
        fl2.add((i,))
    assert fl2.due()  # size-based


def test_flusher_flush_never_raises_and_keeps_buffer(monkeypatch):
    collector = _load_collector()
    fl = collector._Flusher()
    fl.add((1,))

    def boom():
        raise RuntimeError("pool down")

    monkeypatch.setattr(collector, "db_connection", boom)
    fl.flush()  # must not raise (collector-loop invariant)
    assert fl.rows == [(1,)]  # buffer kept for the next tick
    assert fl.total_inserted == 0

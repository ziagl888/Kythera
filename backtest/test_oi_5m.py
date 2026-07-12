# backtest/test_oi_5m.py
"""
Unit tests for core/oi_5m.py — the K9/OIC open-interest hypertable module
(T-2026-CU-9050-103, spec: docs/MODEL_CANDIDATES_SPEC_2026-07.md §K9).

DB-free by construction: a recording fake connection captures the DDL/DML the
module emits; psycopg2 is mocked at the module boundary when absent (build
machine has no libpq). What is pinned here:

  * the DDL contract from the binding spec — PRIMARY KEY (ts, symbol),
    TIMESTAMPTZ, chunk 1 day, compression after 3 days segmentby=symbol,
    retention 730 days;
  * the insert contract — batched, ON CONFLICT (ts, symbol) DO NOTHING,
    commit on success, rollback + re-raise on failure (the shared-connection
    InFailedSqlTransaction class from the ticker_10s/PR-#9 lesson);
  * payload parsing — Binance epoch-ms → aware-UTC ts, malformed points are
    dropped (never zero-filled, P0.12 discipline).

Run with: pytest backtest/test_oi_5m.py -v
"""

from __future__ import annotations

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

from core import oi_5m


# ── Recording fakes ───────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, fail_on: str | None = None):
        self.executed: list[tuple[str, tuple | None]] = []
        self._fail_on = fail_on

    def execute(self, sql, params=None):
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError(f"boom on {self._fail_on}")
        self.executed.append((sql, params))

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, fail_on: str | None = None):
        self.cur = FakeCursor(fail_on)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _all_sql(conn: FakeConn) -> str:
    return "\n".join(sql for sql, _ in conn.cur.executed)


# ── ensure_schema: the binding DDL contract ───────────────────────────────────


def test_ensure_schema_emits_the_spec_ddl():
    conn = FakeConn()
    oi_5m.ensure_schema(conn)
    sql = _all_sql(conn)
    assert "CREATE TABLE IF NOT EXISTS oi_5m" in sql
    assert "PRIMARY KEY (ts, symbol)" in sql
    assert "TIMESTAMPTZ" in sql
    assert "open_interest" in sql and "oi_value_usdt" in sql
    assert "create_hypertable" in sql and "INTERVAL '1 day'" in sql
    assert "timescaledb.compress_segmentby = 'symbol'" in sql
    assert "add_compression_policy" in sql and "INTERVAL '3 days'" in sql
    assert "add_retention_policy" in sql and "INTERVAL '730 days'" in sql
    assert conn.commits == 1


def test_ensure_schema_rolls_back_and_reraises_on_ddl_failure():
    conn = FakeConn(fail_on="add_retention_policy")
    with pytest.raises(RuntimeError):
        oi_5m.ensure_schema(conn)
    # The shared connection must come back clean — the caller retries next sweep.
    assert conn.rollbacks == 1
    assert conn.commits == 0


# ── rows_from_hist_payload: parsing + TZ contract ─────────────────────────────


def test_rows_from_hist_payload_converts_epoch_ms_to_aware_utc():
    payload = [
        {"symbol": "BTCUSDT", "sumOpenInterest": "81000.5", "sumOpenInterestValue": "9.5e9", "timestamp": 1783600200000}
    ]
    rows = oi_5m.rows_from_hist_payload("BTCUSDT", payload)
    assert len(rows) == 1
    ts, symbol, oi, val = rows[0]
    assert symbol == "BTCUSDT"
    assert oi == pytest.approx(81000.5)
    assert val == pytest.approx(9.5e9)
    assert ts.tzinfo is not None
    assert ts == datetime.fromtimestamp(1783600200, tz=timezone.utc)


def test_rows_from_hist_payload_drops_malformed_points():
    payload = [
        {"sumOpenInterest": "1", "sumOpenInterestValue": "2"},  # timestamp fehlt
        {"sumOpenInterest": "x", "sumOpenInterestValue": "2", "timestamp": 1783600200000},  # kein float
        {"sumOpenInterest": "3", "sumOpenInterestValue": "4", "timestamp": 1783600500000},  # ok
    ]
    rows = oi_5m.rows_from_hist_payload("ETHUSDT", payload)
    assert len(rows) == 1
    assert rows[0][2] == pytest.approx(3.0)


# ── insert_oi: batched write contract ─────────────────────────────────────────


def test_insert_oi_empty_rows_is_a_noop():
    conn = FakeConn()
    oi_5m.insert_oi(conn, [])
    assert conn.commits == 0 and conn.rollbacks == 0
    assert conn.cur.executed == []


def test_insert_oi_batches_with_on_conflict_do_nothing():
    conn = FakeConn()
    calls: list[tuple] = []
    ts = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)
    with mock.patch.object(oi_5m, "execute_values", lambda cur, sql, rows, page_size: calls.append((sql, rows, page_size))):
        oi_5m.insert_oi(conn, [(ts, "BTCUSDT", 1.0, 2.0), (ts, "ETHUSDT", 3.0, 4.0)])
    assert len(calls) == 1
    sql, rows, page_size = calls[0]
    assert "INSERT INTO oi_5m (ts, symbol, open_interest, oi_value_usdt)" in sql
    assert "ON CONFLICT (ts, symbol) DO NOTHING" in sql
    assert len(rows) == 2
    assert conn.commits == 1


def test_insert_oi_rolls_back_and_reraises_on_failure():
    conn = FakeConn()

    def _boom(*a, **k):
        raise RuntimeError("insert failed")

    with mock.patch.object(oi_5m, "execute_values", _boom):
        with pytest.raises(RuntimeError):
            oi_5m.insert_oi(conn, [(datetime(2026, 7, 12, tzinfo=timezone.utc), "BTCUSDT", 1.0, 2.0)])
    assert conn.rollbacks == 1
    assert conn.commits == 0

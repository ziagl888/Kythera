# backtest/test_ingestion_batch_flush.py
"""
T-2026-CU-9050-169: the ingestion flusher was measured at ~3.185 single-row
INSERTs/s plus a SAVEPOINT/RELEASE pair per candle — the dominant DB and
client-CPU cost of the ingestion. The fix routes the whole 3s buffer through
ONE execute_values batch (upsert_candles_many) on the hyper write-primary,
with the previous isolating path (now grouped per (symbol, tf, closed)) kept
as fallback.

These tests are DB-free and pin the load-bearing equivalences:

  1. Batch ≡ Einzel — one upsert_candles_many call issues the SAME SQL
     statement with the SAME row tuples in the SAME order as the concatenation
     of per-(symbol, tf, closed) upsert_candles calls. Since statement + rows
     are identical, the DB end state (including the IS DISTINCT FROM no-op
     guard and the forming→closed flip) is identical by construction.
  2. The bulk API is hyper-only and validates the per-row closed flag with the
     same bool-strictness as the single path.
  3. _flush_to_db choreography: bulk on hyper primary (one statement, one
     commit, connection reused), rollback + grouped fallback on batch error,
     grouped fallback directly on legacy primary, connection reset on total
     failure.

Run: pytest backtest/test_ingestion_batch_flush.py -v
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

from core import candles as c

UTC = datetime.timezone.utc


def _load_ingestion():
    spec = importlib.util.spec_from_file_location(
        "data_ingestion",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "1_data_ingestion.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {"core.database": mock.MagicMock()}):
        spec.loader.exec_module(mod)
    return mod


ing = _load_ingestion()


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql_text, params=None):
        self.conn.executed.append(sql_text)


class FakeConn:
    """Captures the transaction choreography of the flush paths."""

    closed = 0

    def __init__(self):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.close_calls = 0

    def cursor(self, *a, **kw):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.close_calls += 1


def _row(sym, minute, price):
    ot = datetime.datetime(2026, 7, 19, 12, minute, tzinfo=UTC)
    return (sym, ot, price, price + 1, price - 1, price, 42.0)


def _buffer():
    """Insertion-ordered buffer exactly like WS_KLINE_BUFFER builds it."""
    rows = [
        (("AAAUSDT", "5m", 0), (_row("AAAUSDT", 0, 10.0), True)),
        (("AAAUSDT", "5m", 5), (_row("AAAUSDT", 5, 11.0), False)),
        (("BBBUSDT", "1h", 0), (_row("BBBUSDT", 0, 20.0), True)),
        (("AAAUSDT", "1h", 0), (_row("AAAUSDT", 0, 12.0), True)),
    ]
    return {(sym, tf, i): v for (sym, tf, i), v in rows}


# ── 1. Batch ≡ Einzel on the SQL layer ───────────────────────────────────────


def test_bulk_call_equals_concatenated_single_calls(monkeypatch):
    monkeypatch.setenv("KYTHERA_CANDLES_WRITE_PRIMARY", "hyper")
    monkeypatch.delenv("KYTHERA_CANDLES_DUAL_WRITE", raising=False)

    captured = []

    def fake_execute_values(cur, statement, rows, page_size=None):
        captured.append((statement, list(rows)))

    monkeypatch.setattr(c.extras, "execute_values", fake_execute_values)
    conn = FakeConn()

    # Single path: one upsert_candles per (symbol, tf, closed) group, buffer order.
    groups = [
        ("AAAUSDT", "5m", True, [_row("AAAUSDT", 0, 10.0)]),
        ("AAAUSDT", "5m", False, [_row("AAAUSDT", 5, 11.0)]),
        ("BBBUSDT", "1h", True, [_row("BBBUSDT", 0, 20.0)]),
        ("AAAUSDT", "1h", True, [_row("AAAUSDT", 0, 12.0)]),
    ]
    for sym, tf, closed, rows in groups:
        c.upsert_candles(conn, sym, tf, rows, closed=closed)
    single_statements = {stmt for stmt, _ in captured}
    single_rows = [r for _, rows in captured for r in rows]
    captured.clear()

    # Bulk path: the same candles as ONE call.
    bulk = [(sym, tf, r[1], r[2], r[3], r[4], r[5], r[6], closed) for sym, tf, closed, rows in groups for r in rows]
    n = c.upsert_candles_many(conn, bulk)

    assert n == len(bulk) == 4
    assert len(captured) == 1, "bulk path must be exactly one execute_values round-trip"
    bulk_statement, bulk_rows = captured[0]
    # Same statement → same ON CONFLICT target, same IS DISTINCT FROM guard.
    assert single_statements == {bulk_statement} == {c._CANDLES_HYPER_UPSERT}
    # Same rows in the same order → identical DB end state.
    assert bulk_rows == single_rows


def test_bulk_is_hyper_only(monkeypatch):
    monkeypatch.setenv("KYTHERA_CANDLES_WRITE_PRIMARY", "legacy")
    with pytest.raises(c.CandleSourceError):
        c.upsert_candles_many(FakeConn(), [("A", "5m", None, 1, 1, 1, 1, 1.0, True)])


def test_bulk_rejects_non_bool_closed(monkeypatch):
    monkeypatch.setenv("KYTHERA_CANDLES_WRITE_PRIMARY", "hyper")
    with pytest.raises(TypeError):
        c.upsert_candles_many(FakeConn(), [("A", "5m", None, 1, 1, 1, 1, 1.0, 1)])


def test_bulk_empty_rows_is_noop(monkeypatch):
    monkeypatch.setenv("KYTHERA_CANDLES_WRITE_PRIMARY", "hyper")
    called = []
    monkeypatch.setattr(c.extras, "execute_values", lambda *a, **kw: called.append(a))
    assert c.upsert_candles_many(FakeConn(), []) == 0
    assert called == []


def test_write_primary_accessor_matches_env(monkeypatch):
    monkeypatch.setenv("KYTHERA_CANDLES_WRITE_PRIMARY", "hyper")
    assert c.candles_write_primary() == "hyper"
    monkeypatch.delenv("KYTHERA_CANDLES_WRITE_PRIMARY", raising=False)
    assert c.candles_write_primary() == "legacy"


# ── 2. _flush_to_db choreography ─────────────────────────────────────────────


@pytest.fixture
def flush_env(monkeypatch):
    """Fresh fake connection wired into the module-global flush-conn slot."""
    conn = FakeConn()
    monkeypatch.setattr(ing, "_FLUSH_CONN", None)
    monkeypatch.setattr(ing, "get_db_connection", lambda: conn)
    return conn


def test_flush_hyper_is_one_batch_one_commit(monkeypatch, flush_env):
    conn = flush_env
    monkeypatch.setattr(ing, "candles_write_primary", lambda: "hyper")
    bulk_calls = []
    monkeypatch.setattr(ing, "upsert_candles_many", lambda cn, rows, **kw: bulk_calls.append((cn, list(rows))))
    monkeypatch.setattr(
        ing, "upsert_candles", lambda *a, **kw: pytest.fail("single path must not run on a clean hyper batch")
    )

    buf = _buffer()
    ing._flush_to_db(buf)

    assert len(bulk_calls) == 1
    cn, rows = bulk_calls[0]
    assert cn is conn
    # Row shape (symbol, tf, open_time, o, h, l, c, v, closed) in buffer order.
    expected = [(sym, tf, r[1], r[2], r[3], r[4], r[5], r[6], closed) for (sym, tf, _), (r, closed) in buf.items()]
    assert rows == expected
    assert conn.commits == 1 and conn.rollbacks == 0
    assert "SAVEPOINT" not in " ".join(conn.executed)


def test_flush_reuses_the_connection(monkeypatch):
    conns = []

    def make_conn():
        conns.append(FakeConn())
        return conns[-1]

    monkeypatch.setattr(ing, "_FLUSH_CONN", None)
    monkeypatch.setattr(ing, "get_db_connection", make_conn)
    monkeypatch.setattr(ing, "candles_write_primary", lambda: "hyper")
    monkeypatch.setattr(ing, "upsert_candles_many", lambda cn, rows, **kw: None)

    ing._flush_to_db(_buffer())
    ing._flush_to_db(_buffer())

    assert len(conns) == 1, "flush must reuse one persistent connection"
    assert conns[0].close_calls == 0
    assert conns[0].commits == 2


def test_flush_batch_error_falls_back_to_groups(monkeypatch, flush_env):
    conn = flush_env
    monkeypatch.setattr(ing, "candles_write_primary", lambda: "hyper")

    def boom(cn, rows, **kw):
        raise RuntimeError("one poisoned row")

    monkeypatch.setattr(ing, "upsert_candles_many", boom)
    group_calls = []
    monkeypatch.setattr(
        ing, "upsert_candles", lambda cn, sym, tf, rows, *, closed, **kw: group_calls.append((sym, tf, closed, rows))
    )

    buf = _buffer()
    ing._flush_to_db(buf)

    # Batch failed → rollback, then per-(symbol, tf, closed) groups with savepoints.
    assert conn.rollbacks == 1
    assert [(s, t, cl) for s, t, cl, _ in group_calls] == [
        ("AAAUSDT", "5m", True),
        ("AAAUSDT", "5m", False),
        ("BBBUSDT", "1h", True),
        ("AAAUSDT", "1h", True),
    ]
    sp = [x for x in conn.executed if x.startswith("SAVEPOINT")]
    rel = [x for x in conn.executed if x.startswith("RELEASE")]
    assert len(sp) == 4 and len(rel) == 4
    assert conn.commits == 1  # one commit at the end of the fallback


def test_flush_legacy_goes_straight_to_groups(monkeypatch, flush_env):
    conn = flush_env
    monkeypatch.setattr(ing, "candles_write_primary", lambda: "legacy")
    monkeypatch.setattr(
        ing, "upsert_candles_many", lambda *a, **kw: pytest.fail("bulk path must not run on legacy primary")
    )
    group_calls = []
    monkeypatch.setattr(
        ing, "upsert_candles", lambda cn, sym, tf, rows, *, closed, **kw: group_calls.append((sym, tf, closed))
    )

    ing._flush_to_db(_buffer())

    assert len(group_calls) == 4
    assert conn.commits == 1 and conn.rollbacks == 0


def test_fallback_isolates_a_failing_group(monkeypatch, flush_env):
    conn = flush_env
    monkeypatch.setattr(ing, "candles_write_primary", lambda: "legacy")
    ok_groups = []

    def maybe_fail(cn, sym, tf, rows, *, closed, **kw):
        if sym == "BBBUSDT":
            raise RuntimeError("missing table")
        ok_groups.append((sym, tf, closed, len(rows)))

    monkeypatch.setattr(ing, "upsert_candles", maybe_fail)

    ing._flush_to_db(_buffer())

    # The bad group rolled back to its savepoint; every other group survived.
    assert [x for x in conn.executed if x.startswith("ROLLBACK TO SAVEPOINT")] == ["ROLLBACK TO SAVEPOINT sp_2"]
    assert ok_groups == [
        ("AAAUSDT", "5m", True, 1),
        ("AAAUSDT", "5m", False, 1),
        ("AAAUSDT", "1h", True, 1),
    ]
    assert conn.commits == 1


def test_total_failure_resets_the_connection(monkeypatch, flush_env):
    conn = flush_env
    monkeypatch.setattr(ing, "candles_write_primary", lambda: "hyper")
    monkeypatch.setattr(ing, "upsert_candles_many", lambda cn, rows, **kw: None)

    def commit_boom():
        raise RuntimeError("connection died")

    conn.commit = commit_boom

    ing._flush_to_db(_buffer())

    assert ing._FLUSH_CONN is None, "a dead connection must be dropped for the next flush"
    assert conn.close_calls == 1

# backtest/test_market_tracker_conn.py
"""
Unit tests for the market tracker's DB-connection hygiene (P1.43).

The tracker holds pooled connections (pool max 8 per process). Every code path
that acquires one must return it — on success AND on a raising query — or the
process silently starves after a handful of DB hiccups and stops posting.

Run with: pytest backtest/test_market_tracker_conn.py -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.config fails hard on missing secrets; the build machine ships an empty
# .env. Placeholders keep this test standalone — nothing here opens a socket
# (the pool is lazy) or talks to Telegram.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pandas as pd
import pytest


def _load_tracker():
    spec = importlib.util.spec_from_file_location(
        "market_tracker",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "23_market_tracker.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "core.database": mock.MagicMock(),
            "core.market_utils": mock.MagicMock(),
            "core.bot_naming": mock.MagicMock(pretty_name=lambda x: x),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


mt = _load_tracker()


class FakeConn:
    """Mirrors the PooledConnection contract the tracker relies on."""

    def __init__(self) -> None:
        self.closes = 0
        self.rollbacks = 0

    def __enter__(self) -> FakeConn:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self.closes += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def cursor(self, *a, **kw):
        raise AssertionError("unexpected cursor() in this test")


@pytest.fixture
def conn(monkeypatch) -> FakeConn:
    c = FakeConn()
    monkeypatch.setattr(mt, "get_db_connection", lambda: c)
    monkeypatch.setattr(mt, "send_telegram", lambda *a, **kw: None)
    return c


def _run(coro) -> None:
    """Run a job; later stages may raise on synthetic data — irrelevant here."""
    try:
        asyncio.run(coro)
    except Exception:
        pass


# ── The pool slot comes back on the error path ────────────────────────────────


@pytest.mark.parametrize("job", ["job_signal_summary", "job_per_bot_performance"])
def test_connection_returned_when_query_raises(conn, monkeypatch, job):
    monkeypatch.setattr(mt.pd, "read_sql_query", mock.Mock(side_effect=RuntimeError("db down")))
    _run(getattr(mt, job)())
    assert conn.closes == 1, "pooled connection leaked on the query-error path"


@pytest.mark.parametrize("job", ["job_signal_summary", "job_per_bot_performance"])
def test_connection_returned_on_success(conn, monkeypatch, job):
    monkeypatch.setattr(mt.pd, "read_sql_query", mock.Mock(return_value=pd.DataFrame()))
    _run(getattr(mt, job)())
    assert conn.closes == 1


# ── The ai_signals fallback must run against a clean transaction ──────────────


def test_per_bot_rolls_back_before_ai_signals_fallback(conn, monkeypatch):
    calls: list[str] = []
    rollbacks_at_fallback: list[int] = []

    def fake_read_sql(query, _conn, **kwargs):
        calls.append(query)
        if "LEFT JOIN ml_predictions_master" in query:
            raise RuntimeError("join failed")
        if "FROM ai_signals" in query:
            # Record rather than assert: an AssertionError raised here would be
            # swallowed by the job's own except-and-return.
            rollbacks_at_fallback.append(conn.rollbacks)
        return pd.DataFrame()

    monkeypatch.setattr(mt.pd, "read_sql_query", fake_read_sql)
    _run(mt.job_per_bot_performance())

    assert any("FROM ai_signals" in q and "LEFT JOIN" not in q for q in calls), "fallback query never ran"
    # Postgres would raise InFailedSqlTransaction without the rollback.
    assert rollbacks_at_fallback == [1], "fallback query ran inside the aborted transaction"
    assert conn.closes == 1


# ── A failed regime lookup must not poison the shared connection ──────────────


def test_regime_fit_label_rolls_back_on_error(conn):
    conn.cursor = mock.Mock(side_effect=RuntimeError("relation does not exist"))
    assert mt._get_regime_fit_label(conn, "ABR1") == "---"
    assert conn.rollbacks == 1, "aborted transaction left open for the next bot's lookup"


# ── The contract the `with get_db_connection()` sites depend on ───────────────


def test_pooled_connection_exit_returns_to_pool():
    from core.database import PooledConnection

    closed: list[bool] = []

    class Probe(PooledConnection):
        def close(self) -> None:
            closed.append(True)

    with Probe(mock.MagicMock()):
        pass
    assert closed == [True], "PooledConnection.__exit__ no longer returns the connection"

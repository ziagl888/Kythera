# backtest/test_db_pool_options.py
"""
Unit tests for the DB pool's connection guards (P2.47).

A bot blocked forever on a dead DB socket or a runaway query used to stay
"green" for the watchdog. The pool now hands every connection:
  - a server-side statement_timeout (env-overridable, 0 = disabled for long
    trainer/housekeeping queries),
  - the pre-existing lock_timeout,
  - libpq TCP keepalives so a silently-dropped socket fails fast.

These tests pin the connect() parameters without opening a socket (the pool
constructor is mocked).

Run with: pytest backtest/test_db_pool_options.py -v
"""

from __future__ import annotations

import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.config fails hard on missing secrets; the build machine ships an empty
# .env. Placeholders keep this standalone — the pool is lazy, nothing connects.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest

import core.database as db


@pytest.fixture(autouse=True)
def _reset_pool(monkeypatch):
    """Fresh, un-created pool per test; scrub the env knobs we toggle."""
    monkeypatch.setattr(db, "_POOL", None)
    for key in (
        "KYTHERA_DB_STATEMENT_TIMEOUT_MS",
        "KYTHERA_DB_LOCK_TIMEOUT_MS",
        "KYTHERA_DB_KEEPALIVES_IDLE_S",
        "KYTHERA_DB_KEEPALIVES_INTERVAL_S",
        "KYTHERA_DB_KEEPALIVES_COUNT",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


def _create_pool_capture(monkeypatch):
    """Force pool creation with the constructor mocked; return the call kwargs."""
    fake_pool = mock.MagicMock()
    fake_pool.closed = False
    ctor = mock.MagicMock(return_value=fake_pool)
    monkeypatch.setattr(db.pg_pool, "ThreadedConnectionPool", ctor)
    db._get_pool()
    assert ctor.call_count == 1
    return ctor.call_args.kwargs


# ── Defaults ─────────────────────────────────────────────────────────────────


def test_defaults_carry_statement_and_lock_timeout(monkeypatch):
    kwargs = _create_pool_capture(monkeypatch)
    assert "-c lock_timeout=30000" in kwargs["options"]
    assert "-c statement_timeout=300000" in kwargs["options"]


def test_defaults_enable_tcp_keepalives(monkeypatch):
    kwargs = _create_pool_capture(monkeypatch)
    assert kwargs["keepalives"] == 1
    assert kwargs["keepalives_idle"] == 30
    assert kwargs["keepalives_interval"] == 10
    assert kwargs["keepalives_count"] == 3


# ── Per-process env overrides (the trainer/housekeeping escape hatch) ─────────


def test_statement_timeout_override(monkeypatch):
    monkeypatch.setenv("KYTHERA_DB_STATEMENT_TIMEOUT_MS", "120000")
    kwargs = _create_pool_capture(monkeypatch)
    assert "-c statement_timeout=120000" in kwargs["options"]


def test_statement_timeout_zero_disables_the_cap(monkeypatch):
    monkeypatch.setenv("KYTHERA_DB_STATEMENT_TIMEOUT_MS", "0")
    kwargs = _create_pool_capture(monkeypatch)
    assert "statement_timeout" not in kwargs["options"]
    # lock_timeout must survive the disable.
    assert "-c lock_timeout=30000" in kwargs["options"]


def test_keepalive_idle_override(monkeypatch):
    monkeypatch.setenv("KYTHERA_DB_KEEPALIVES_IDLE_S", "5")
    kwargs = _create_pool_capture(monkeypatch)
    assert kwargs["keepalives_idle"] == 5


# ── The helpers read env at call time, not import time ───────────────────────


def test_helpers_reflect_late_env_changes(monkeypatch):
    monkeypatch.setenv("KYTHERA_DB_LOCK_TIMEOUT_MS", "1000")
    assert "-c lock_timeout=1000" in db._connect_options()
    monkeypatch.setenv("KYTHERA_DB_KEEPALIVES_COUNT", "9")
    assert db._keepalive_kwargs()["keepalives_count"] == 9

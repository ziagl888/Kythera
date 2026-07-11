# backtest/test_orchestrator_startup_check.py
"""
Unit tests for the orchestrator startup reconciliation (P2.24).

A regime change that happens while the orchestrator is DOWN is never observed
as an in-memory flip (check_regime_change_and_close only acts on a delta from
the remembered regime, which starts empty). run_startup_reconciliation closes
that gap: at boot it re-judges every OPEN ROM1 trade against the CURRENT
whitelist and seeds the in-memory baseline. These tests pin that behaviour
DB-free.

Run with: pytest backtest/test_orchestrator_startup_check.py -v
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import importlib.util
import unittest.mock as mock
from unittest.mock import MagicMock

# core.config raises at import when its _required() vars are unset; seed dummies
# before the loader execs the module (the build machine ships an empty .env stub).
os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")

from core import config as _kcfg  # noqa: E402 — must follow the env seed above


def _load_orchestrator():
    spec = importlib.util.spec_from_file_location(
        "signal_orchestrator",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "28_signal_orchestrator.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", {
        "core.database": mock.MagicMock(),
        "core.logging_setup": mock.MagicMock(
            setup_logging=lambda x: __import__("logging").getLogger(x)
        ),
        "core.config": mock.MagicMock(
            REGIME_TRADING_CHANNEL_ID=_kcfg.CH_REGIME_TRADING,
            REGIME_STATUS_CHANNEL_ID=_kcfg.CH_MARKET_DATA,
        ),
        "core.market_utils": mock.MagicMock(),
        "core.trade_utils": mock.MagicMock(),
    }):
        spec.loader.exec_module(mod)
    return mod


orch = _load_orchestrator()


def _reset_regime_globals():
    """Fresh boot state: no remembered regime (as at process start)."""
    orch._last_known_regime = None
    orch._last_known_alt_context = None


def _mock_conn(open_trades):
    """Conn whose only fetch is the OPEN-trades scan in the reconciliation.

    fetchone feeds get_current_regime_full (regime, alt); fetchall feeds the
    orchestrator_open_trades SELECT. get_whitelist_decision and the close/trail
    helpers are patched per-test, so no other DB shape is needed.
    """
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    cur.fetchone = MagicMock(return_value=("TREND_UP", "ALT_NEUTRAL"))
    cur.fetchall = MagicMock(return_value=list(open_trades))
    return conn


# ── Seeding the in-memory baseline ────────────────────────────────────────────

def test_startup_seeds_regime_baseline():
    """The boot regime is stored so the first periodic check does not misfire."""
    _reset_regime_globals()
    conn = _mock_conn(open_trades=[])
    with mock.patch.object(orch, "AUTO_CLOSE_ON_REGIME_CHANGE", False):
        asyncio.run(orch.run_startup_reconciliation(conn))
    assert orch._last_known_regime == "TREND_UP"
    assert orch._last_known_alt_context == "ALT_NEUTRAL"


def test_startup_auto_close_off_skips_reconciliation():
    """With auto-close disabled the baseline is still seeded but nothing closes."""
    _reset_regime_globals()
    conn = _mock_conn(open_trades=[(1, "BTCUSDT", "LONG", "MIS1-8h", 50000.0)])
    with mock.patch.object(orch, "AUTO_CLOSE_ON_REGIME_CHANGE", False), \
         mock.patch.object(orch, "get_whitelist_decision") as gwd, \
         mock.patch.object(orch, "send_telegram") as send:
        asyncio.run(orch.run_startup_reconciliation(conn))
    gwd.assert_not_called()
    send.assert_not_called()
    assert orch._last_known_regime == "TREND_UP"


# ── Reconciliation closes the de-whitelisted loser ────────────────────────────

def test_startup_closes_non_whitelisted_trade():
    """An open trade the current regime no longer whitelists gets a Close command."""
    _reset_regime_globals()
    conn = _mock_conn(open_trades=[(7, "BTCUSDT", "LONG", "MIS1-8h", 50000.0)])
    with mock.patch.object(orch, "AUTO_CLOSE_ON_REGIME_CHANGE", True), \
         mock.patch.object(orch, "TRAIL_WINNERS_ON_REGIME_CHANGE", False), \
         mock.patch.object(orch, "get_whitelist_decision", return_value=(False, "wr_below_overall")), \
         mock.patch.object(orch, "send_telegram") as send, \
         mock.patch.object(orch, "mark_orchestrator_trade_closed") as mark, \
         mock.patch.object(orch, "force_close_trades_for_regime_change",
                           return_value={"ai_closed": 1, "classic_closed": 0}) as force:
        asyncio.run(orch.run_startup_reconciliation(conn))

    posted = [c.args[0] for c in send.call_args_list]
    assert "Close BTCUSDT" in posted  # symbol-wide Cornix close command
    mark.assert_called_once()
    # A/B arm tag written atomically with the startup-triggered close.
    assert mark.call_args.kwargs.get("regime_close_action") == "REGIME_CHANGE_CLOSED"
    # DB-side force-close is model='ROM1' only (P1.9) — foreign bots untouched.
    force.assert_called_once_with(conn, "BTCUSDT", "LONG")


def test_startup_keeps_whitelisted_trade():
    """A still-whitelisted trade is left open — no Close, no force-close."""
    _reset_regime_globals()
    conn = _mock_conn(open_trades=[(7, "ETHUSDT", "SHORT", "ATS1", 3000.0)])
    with mock.patch.object(orch, "AUTO_CLOSE_ON_REGIME_CHANGE", True), \
         mock.patch.object(orch, "TRAIL_WINNERS_ON_REGIME_CHANGE", False), \
         mock.patch.object(orch, "get_whitelist_decision", return_value=(True, "wr_above_overall")), \
         mock.patch.object(orch, "send_telegram") as send, \
         mock.patch.object(orch, "mark_orchestrator_trade_closed") as mark, \
         mock.patch.object(orch, "force_close_trades_for_regime_change") as force:
        asyncio.run(orch.run_startup_reconciliation(conn))

    posted = [c.args[0] for c in send.call_args_list]
    assert not any(p.startswith("Close ") for p in posted)
    mark.assert_not_called()
    force.assert_not_called()
    # always_announce=False on startup + nothing actioned → no status post.
    send.assert_not_called()


# ── Quiet on an empty / no-action pass (watchdog restarts happen often) ────────

def test_startup_empty_pass_posts_no_summary():
    """Zero open trades → no status-channel post (startup passes always_announce=False)."""
    _reset_regime_globals()
    conn = _mock_conn(open_trades=[])
    with mock.patch.object(orch, "AUTO_CLOSE_ON_REGIME_CHANGE", True), \
         mock.patch.object(orch, "get_whitelist_decision") as gwd, \
         mock.patch.object(orch, "send_telegram") as send:
        asyncio.run(orch.run_startup_reconciliation(conn))
    gwd.assert_not_called()  # no open trades → never judged
    send.assert_not_called()  # no summary spam on an empty restart


# ── Regression: the regime-change caller still announces even with no action ───

def test_regime_change_helper_always_announces():
    """The shared helper defaults always_announce=True for the regime-change path:
    an observed flip with zero open trades still posts a summary."""
    conn = _mock_conn(open_trades=[])
    with mock.patch.object(orch, "send_telegram") as send:
        orch._close_non_whitelisted_open_trades(
            conn,
            changes=["BTC-Regime TREND_UP → TREND_DOWN"],
            title="🔄 REGIME CHANGE & AUTO-CLOSE",
            count_label="Open trades before change",
            always_announce=True,
        )
    assert send.call_count == 1  # the summary post


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))

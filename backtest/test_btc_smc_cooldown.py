# backtest/test_btc_smc_cooldown.py
"""Standalone (DB-free) tests for the 21_btc_smc cooldown/dedupe (P2.46).

Background: 21_btc_smc_strategy runs once per hour and posts a Cornix signal the
moment an EMA21 + FVG pivot-retest setup fully closes. Without a cooldown, a
lagging gap-filler makes the same setup re-qualify on the next scan, so the
identical signal was posted a second time ~1h later — a duplicate position with
real money via Cornix.

The fix routes every post through the central trade_cooldowns system inside
send_cornix_signal: check the cooldown first, and on a successful post upsert the
cooldown in the SAME transaction as the outbox INSERT (commit=False + one
conn.commit) so the signal and its dedupe marker are persisted atomically.

These tests pin the wiring without a DB by faking the connection and stubbing the
cooldown helpers (core.market_utils is mocked at import):
  1. cooldown active  -> no outbox INSERT, no update_cooldown, returns False
  2. cooldown clear   -> exactly one outbox INSERT + update_cooldown(commit=False)
                         in one commit, keyed by (COOLDOWN_TAG, SYMBOL, direction)
  3. a DB error is swallowed and reported as a non-post (returns False)

Run: py -3.13 backtest/test_btc_smc_cooldown.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

# Pre-seed pandas/numpy before the patch.dict block (the bot imports pandas at
# module top); a patched teardown must not rip numpy out from under it later
# (memory patch-dict-sys-modules-numpy-teardown).
import pandas  # noqa: F401

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def _load_btc_smc():
    """Import 21_btc_smc_strategy.py under a stable alias (digit prefix)."""
    spec = importlib.util.spec_from_file_location(
        "btc_smc_strategy",
        os.path.join(REPO_ROOT, "21_btc_smc_strategy.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "core.config": mock.MagicMock(CH_BTC_SMC=-1),
            "core.database": mock.MagicMock(),
            "core.market_utils": mock.MagicMock(),
            "core.trade_utils": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


smc = _load_btc_smc()


class _FakeCursor:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()
        self.commits = 0

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        self.commits += 1


def _wire(cooldown_active):
    """Point the module at a fresh fake conn + stubbed cooldown helpers."""
    conn = _FakeConn()
    smc.get_db_connection = mock.MagicMock(return_value=conn)
    smc.check_cooldown = mock.MagicMock(return_value=cooldown_active)
    smc.update_cooldown = mock.MagicMock()
    return conn


def test_cooldown_active_suppresses_the_post():
    conn = _wire(cooldown_active=True)
    posted = smc.send_cornix_signal("LONG", 100.0, 99.0, 102.0, 2.0, 25)
    assert posted is False, "an active cooldown must suppress the post"
    assert conn.cur.executed == [], "no outbox INSERT may run while on cooldown"
    smc.update_cooldown.assert_not_called()
    assert conn.commits == 0, "nothing to commit when suppressed"


def test_clear_cooldown_posts_and_stamps_atomically():
    conn = _wire(cooldown_active=False)
    posted = smc.send_cornix_signal("SHORT", 100.0, 101.0, 97.0, 2.5, 25)
    assert posted is True

    # Exactly one outbox INSERT, into telegram_outbox.
    inserts = [e for e in conn.cur.executed if "telegram_outbox" in e[0]]
    assert len(inserts) == 1, f"expected exactly one outbox INSERT, got {conn.cur.executed}"

    # Cooldown upserted in the SAME transaction (commit=False) and keyed correctly.
    smc.update_cooldown.assert_called_once()
    args, kwargs = smc.update_cooldown.call_args
    assert args[0] is conn
    assert args[1] == smc.COOLDOWN_TAG
    assert args[2] == smc.SYMBOL
    assert args[3] == "SHORT"
    assert kwargs.get("commit") is False, "cooldown must join the outbox commit, not commit on its own"

    # One and only one commit closed both writes together.
    assert conn.commits == 1, "signal + cooldown must land in a single commit"


def test_check_precedes_the_insert():
    """check_cooldown is consulted before anything is written."""
    _wire(cooldown_active=False)
    smc.send_cornix_signal("LONG", 100.0, 99.0, 103.0, 2.0, 25)
    smc.check_cooldown.assert_called_once()
    args, _ = smc.check_cooldown.call_args
    assert args[1] == smc.COOLDOWN_TAG and args[2] == smc.SYMBOL and args[3] == "LONG"


def test_db_error_is_a_non_post():
    """An outbox failure is swallowed and reported as not-posted (no crash)."""
    conn = _wire(cooldown_active=False)
    conn.cur.execute = mock.MagicMock(side_effect=RuntimeError("db down"))
    posted = smc.send_cornix_signal("LONG", 100.0, 99.0, 103.0, 2.0, 25)
    assert posted is False


def test_cooldown_tag_fits_varchar10():
    """Guard the T-024 varchar(10) trap right where the tag lives."""
    assert len(smc.COOLDOWN_TAG) <= 10, f"COOLDOWN_TAG '{smc.COOLDOWN_TAG}' exceeds varchar(10)"
    assert smc.COOLDOWN_HOURS >= 1, "cooldown must be >= the 1h candle duration (P1.27)"


if __name__ == "__main__":
    test_cooldown_active_suppresses_the_post()
    test_clear_cooldown_posts_and_stamps_atomically()
    test_check_precedes_the_insert()
    test_db_error_is_a_non_post()
    test_cooldown_tag_fits_varchar10()
    print("OK — 21_btc_smc cooldown/dedupe wiring holds")

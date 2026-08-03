# backtest/test_delisted_cleanup.py
"""Unit tests for the Binance perp shape guard of the delisted cleanup (P2.17).

Previously `6_housekeeping.cleanup_delisted_trades` closed EVERY symbol that
is not in coins.json — including metals (XAUUSD), cross pairs (ETHBTC) and
forex → nightly false closes at PnL 0. The fix restricts the
delisted close to the shape the fleet actually trades
(`<BASE>USDT`), so that only truly delisted USDT perpetuals get closed.

Tests the real predicate `core.coins.looks_like_usdt_perp` as well as the
selection semantics (membership AND shape) applied inline in
cleanup_delisted_trades. DB-free.

Run with: pytest backtest/test_delisted_cleanup.py -v
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.coins import looks_like_usdt_perp


def test_accepts_real_usdt_perp_shapes():
    for sym in ("BTCUSDT", "ETHUSDT", "1000SHIBUSDT", "SOLUSDT", "1000PEPEUSDT"):
        assert looks_like_usdt_perp(sym), sym


def test_rejects_named_false_close_symbols():
    # Exactly the symbols from audit_reports/02_data_pipeline.md:65-66.
    for junk in ("XAUUSD", "ETHBTC", "EURUSD", "XAGUSD"):
        assert not looks_like_usdt_perp(junk), junk


def test_rejects_malformed_symbols():
    for junk in ("", "usdtbtc", "BTC-USDT", "USDT", "btcusdt", "BTC/USDT"):
        assert not looks_like_usdt_perp(junk), junk


def test_delisted_selection_excludes_non_perp_shapes():
    """Mirrors the inline selection: only (not in coins.json) AND perp shape."""
    active_coins = {"BTCUSDT", "ETHUSDT"}
    rows = [
        {"coin": "BTCUSDT"},  # active → no close
        {"coin": "SOLUSDT"},  # delisted USDT perp → close
        {"coin": "XAUUSD"},   # metals junk → do NOT close (P2.17)
        {"coin": "ETHBTC"},   # cross pair → do NOT close (P2.17)
    ]

    delisted = [r["coin"] for r in rows if r["coin"] not in active_coins and looks_like_usdt_perp(r["coin"])]

    assert delisted == ["SOLUSDT"]

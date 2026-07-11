# backtest/test_delisted_cleanup.py
"""Unit tests für die Binance-Perp-Shape-Guard der Delisted-Cleanup (P2.17).

Vorher schloss `6_housekeeping.cleanup_delisted_trades` JEDES Symbol, das
nicht in coins.json steht — inkl. Metals (XAUUSD), Cross-Pairs (ETHBTC) und
Forex → nächtliche Falsch-Closes bei PnL 0. Der Fix beschränkt den
Delisted-Close auf die Shape, die die Flotte tatsächlich handelt
(`<BASE>USDT`), damit nur echt delistete USDT-Perpetuals geschlossen werden.

Getestet wird das reale Prädikat `core.coins.looks_like_usdt_perp` sowie die
Selektions-Semantik (Mitgliedschaft UND Shape), die in cleanup_delisted_trades
inline angewandt wird. DB-frei.

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
    # Genau die Symbole aus audit_reports/02_data_pipeline.md:65-66.
    for junk in ("XAUUSD", "ETHBTC", "EURUSD", "XAGUSD"):
        assert not looks_like_usdt_perp(junk), junk


def test_rejects_malformed_symbols():
    for junk in ("", "usdtbtc", "BTC-USDT", "USDT", "btcusdt", "BTC/USDT"):
        assert not looks_like_usdt_perp(junk), junk


def test_delisted_selection_excludes_non_perp_shapes():
    """Spiegelt die Inline-Selektion: nur (nicht in coins.json) UND perp-shape."""
    active_coins = {"BTCUSDT", "ETHUSDT"}
    rows = [
        {"coin": "BTCUSDT"},  # aktiv → kein Close
        {"coin": "SOLUSDT"},  # delisted USDT-perp → Close
        {"coin": "XAUUSD"},   # Metals-Junk → NICHT closen (P2.17)
        {"coin": "ETHBTC"},   # Cross-Pair → NICHT closen (P2.17)
    ]

    delisted = [r["coin"] for r in rows if r["coin"] not in active_coins and looks_like_usdt_perp(r["coin"])]

    assert delisted == ["SOLUSDT"]

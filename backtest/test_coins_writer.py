# backtest/test_coins_writer.py
"""Unit tests for core.coins — the single coins.json writer (P2.16).

Covers the three properties that close the double-writer bug:
  1. ONE filter definition (quoteAsset=USDT + status=TRADING + PERPETUAL) —
     the ETHU/ETHBTC/quarterly junk symbols fall out.
  2. Atomic write (tmp + os.replace): a failure mid-write leaves the
     existing coins.json untouched and no tmp leftovers behind.
  3. refresh_coins_json only writes when a complete, non-empty list is
     present — a fetch failure AND an empty/missing symbols field refuse the write.

DB-free, network-free (fetch is monkeypatched).

Run with: pytest backtest/test_coins_writer.py -v
"""

from __future__ import annotations

import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core import coins


def _exchange_info() -> dict:
    """Realistic exchangeInfo excerpt including the junk shapes from the
    ETHU incident (2026-07-06)."""
    return {
        "symbols": [
            {"symbol": "BTCUSDT", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "ETHUSDT", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "1000SHIBUSDT", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL"},
            # Junk: non-USDT quote (ETHU class)
            {"symbol": "ETHU", "quoteAsset": "U", "status": "TRADING", "contractType": "PERPETUAL"},
            # Junk: cross pair
            {"symbol": "ETHBTC", "quoteAsset": "BTC", "status": "TRADING", "contractType": "PERPETUAL"},
            # Junk: quarterly future
            {"symbol": "BTCUSDT_260925", "quoteAsset": "USDT", "status": "TRADING", "contractType": "CURRENT_QUARTER"},
            # Junk: not tradeable (SETTLING/BREAK)
            {"symbol": "DEADUSDT", "quoteAsset": "USDT", "status": "SETTLING", "contractType": "PERPETUAL"},
            # Junk: non-ASCII base (meme perp, e.g. Chinese characters) — matches
            # quote/status/contractType, but is not a valid table identifier
            # → must fall out via the looks_like_usdt_perp shape (T-162).
            {"symbol": "龙虾USDT", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL"},
        ]
    }


def test_filter_keeps_only_usdt_perpetuals():
    result = coins.filter_usdt_perpetuals(_exchange_info())
    assert result == ["BTCUSDT", "ETHUSDT", "1000SHIBUSDT"]


def test_filter_excludes_each_junk_shape():
    result = set(coins.filter_usdt_perpetuals(_exchange_info()))
    for junk in ("ETHU", "ETHBTC", "BTCUSDT_260925", "DEADUSDT", "龙虾USDT"):
        assert junk not in result


def test_filter_drops_non_ascii_symbol_base():
    # T-162: a perp with a non-ASCII base must not enter the universe — otherwise
    # core.candles.validate_symbol raises "invalid symbol for table identifier"
    # in every candle-reading bot that loads coins.json directly.
    info = {"symbols": [{"symbol": "龙虾USDT", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL"}]}
    assert coins.filter_usdt_perpetuals(info) == []
    assert coins.looks_like_usdt_perp("龙虾USDT") is False
    assert coins.looks_like_usdt_perp("BTCUSDT") is True


def test_filter_tolerates_missing_keys():
    # A symbol without the expected fields must not crash, only fall out.
    info = {"symbols": [{"symbol": "WEIRD"}, {"quoteAsset": "USDT"}]}
    assert coins.filter_usdt_perpetuals(info) == []


def test_write_is_atomic_and_roundtrips(tmp_path):
    path = str(tmp_path / "coins.json")
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    coins.write_coins_json_atomic(symbols, path)

    with open(path) as f:
        assert json.load(f) == symbols
    # No tmp leftover in the target directory
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".coins.")]
    assert leftovers == []


def test_write_failure_preserves_existing_file(tmp_path):
    path = str(tmp_path / "coins.json")
    coins.write_coins_json_atomic(["BTCUSDT"], path)  # existing, good file

    # set() is not JSON-serialisable → json.dump raises mid tmp-write.
    with pytest.raises(TypeError):
        coins.write_coins_json_atomic({"not", "serializable"}, path)  # type: ignore[arg-type]

    # Existing file untouched, no tmp leftover → os.replace never ran.
    with open(path) as f:
        assert json.load(f) == ["BTCUSDT"]
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".coins.")]
    assert leftovers == []


def test_refresh_fetches_filters_and_writes(tmp_path, monkeypatch):
    path = str(tmp_path / "coins.json")
    monkeypatch.setattr(coins, "fetch_usdt_perpetual_symbols", lambda base_url, timeout=10: ["BTCUSDT", "ETHUSDT"])

    result = coins.refresh_coins_json("https://fapi.binance.com", path)

    assert result == ["BTCUSDT", "ETHUSDT"]
    with open(path) as f:
        assert json.load(f) == ["BTCUSDT", "ETHUSDT"]


def test_refresh_does_not_write_on_fetch_failure(tmp_path, monkeypatch):
    path = str(tmp_path / "coins.json")
    coins.write_coins_json_atomic(["BTCUSDT"], path)  # existing list

    def _boom(base_url, timeout=10):
        raise RuntimeError("network down")

    monkeypatch.setattr(coins, "fetch_usdt_perpetual_symbols", _boom)

    with pytest.raises(RuntimeError):
        coins.refresh_coins_json("https://fapi.binance.com", path)

    # coins.json stays at the old state — no truncation on fetch failure.
    with open(path) as f:
        assert json.load(f) == ["BTCUSDT"]


def test_refresh_refuses_empty_universe(tmp_path, monkeypatch):
    # A 200 response with an empty symbols list returns [] — the guard must
    # refuse the write, otherwise it empties coins.json fleet-wide.
    path = str(tmp_path / "coins.json")
    coins.write_coins_json_atomic(["BTCUSDT"], path)  # existing, good file
    monkeypatch.setattr(coins, "fetch_usdt_perpetual_symbols", lambda base_url, timeout=10: [])

    with pytest.raises(RuntimeError, match="empty universe"):
        coins.refresh_coins_json("https://fapi.binance.com", path)

    # File untouched, no tmp leftover → the write never ran.
    with open(path) as f:
        assert json.load(f) == ["BTCUSDT"]
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".coins.")]
    assert leftovers == []


def test_refresh_refuses_missing_symbols_field(tmp_path, monkeypatch):
    # Missing symbols field: filter_usdt_perpetuals uses .get('symbols', [])
    # → [] → the same guard applies, coins.json stays untouched.
    path = str(tmp_path / "coins.json")
    coins.write_coins_json_atomic(["BTCUSDT"], path)  # existing, good file

    def _fetch_from_bodyless_response(base_url, timeout=10):
        # Real path: exchangeInfo without a symbols key → filter returns [].
        return coins.filter_usdt_perpetuals({})

    monkeypatch.setattr(coins, "fetch_usdt_perpetual_symbols", _fetch_from_bodyless_response)

    with pytest.raises(RuntimeError, match="empty universe"):
        coins.refresh_coins_json("https://fapi.binance.com", path)

    with open(path) as f:
        assert json.load(f) == ["BTCUSDT"]
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".coins.")]
    assert leftovers == []

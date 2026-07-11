# backtest/test_coins_writer.py
"""Unit tests für core.coins — den einzigen coins.json-Writer (P2.16).

Deckt die drei Eigenschaften ab, die den Doppel-Writer-Bug schließen:
  1. EINE Filter-Definition (quoteAsset=USDT + status=TRADING + PERPETUAL) —
     die ETHU/ETHBTC/Quarterly-Junk-Symbole fallen raus.
  2. Atomarer Write (tmp + os.replace): ein Fehler mitten im Schreiben lässt
     die bestehende coins.json unversehrt und hinterlässt keine tmp-Reste.
  3. refresh_coins_json schreibt NUR, wenn eine vollständige Liste vorliegt.

DB-frei, netz-frei (fetch wird gemonkeypatcht).

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
    """Realistischer exchangeInfo-Ausschnitt inkl. der Junk-Shapes vom
    ETHU-Vorfall (2026-07-06)."""
    return {
        "symbols": [
            {"symbol": "BTCUSDT", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "ETHUSDT", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "1000SHIBUSDT", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL"},
            # Junk: non-USDT quote (ETHU-Klasse)
            {"symbol": "ETHU", "quoteAsset": "U", "status": "TRADING", "contractType": "PERPETUAL"},
            # Junk: Cross-Pair
            {"symbol": "ETHBTC", "quoteAsset": "BTC", "status": "TRADING", "contractType": "PERPETUAL"},
            # Junk: Quartals-Future
            {"symbol": "BTCUSDT_260925", "quoteAsset": "USDT", "status": "TRADING", "contractType": "CURRENT_QUARTER"},
            # Junk: nicht handelbar (SETTLING/BREAK)
            {"symbol": "DEADUSDT", "quoteAsset": "USDT", "status": "SETTLING", "contractType": "PERPETUAL"},
        ]
    }


def test_filter_keeps_only_usdt_perpetuals():
    result = coins.filter_usdt_perpetuals(_exchange_info())
    assert result == ["BTCUSDT", "ETHUSDT", "1000SHIBUSDT"]


def test_filter_excludes_each_junk_shape():
    result = set(coins.filter_usdt_perpetuals(_exchange_info()))
    for junk in ("ETHU", "ETHBTC", "BTCUSDT_260925", "DEADUSDT"):
        assert junk not in result


def test_filter_tolerates_missing_keys():
    # Ein Symbol ohne die erwarteten Felder darf nicht crashen, nur rausfallen.
    info = {"symbols": [{"symbol": "WEIRD"}, {"quoteAsset": "USDT"}]}
    assert coins.filter_usdt_perpetuals(info) == []


def test_write_is_atomic_and_roundtrips(tmp_path):
    path = str(tmp_path / "coins.json")
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    coins.write_coins_json_atomic(symbols, path)

    with open(path) as f:
        assert json.load(f) == symbols
    # Kein tmp-Rest im Zielverzeichnis
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".coins.")]
    assert leftovers == []


def test_write_failure_preserves_existing_file(tmp_path):
    path = str(tmp_path / "coins.json")
    coins.write_coins_json_atomic(["BTCUSDT"], path)  # bestehende, gute Datei

    # set() ist nicht JSON-serialisierbar → json.dump wirft mitten im tmp-Write.
    with pytest.raises(TypeError):
        coins.write_coins_json_atomic({"not", "serializable"}, path)  # type: ignore[arg-type]

    # Bestehende Datei unversehrt, kein tmp-Rest → os.replace lief nie.
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
    coins.write_coins_json_atomic(["BTCUSDT"], path)  # bestehende Liste

    def _boom(base_url, timeout=10):
        raise RuntimeError("network down")

    monkeypatch.setattr(coins, "fetch_usdt_perpetual_symbols", _boom)

    with pytest.raises(RuntimeError):
        coins.refresh_coins_json("https://fapi.binance.com", path)

    # coins.json bleibt auf dem alten Stand — kein Truncate bei Fetch-Fehler.
    with open(path) as f:
        assert json.load(f) == ["BTCUSDT"]

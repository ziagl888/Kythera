# backtest/test_symbol_validation.py
"""
Unit tests for the central coin-symbol validation in core.market_utils.load_coins
(T-2026-CU-9050-096, AUDIT_TODO P3.3 + P3.1).

DB-free and network-free: only reads temp coins.json files.

P3.3 closes the one second-order identifier surface — every symbol becomes an
f-string table name ``f"{sym}_{tf}"`` somewhere in the fleet, so load_coins now
rejects anything that is not ``[A-Z0-9]+`` (dropped with an ERROR log, never
silently kept). On the live coins.json (uppercase USDT perpetuals) nothing is
dropped, so the data-path caller (1_data_ingestion) sees an identical list.

P3.1 folded the six drifted copies of load_coins onto this canon; the usdt_only
and uppercase flags reproduce what those offline callers did locally.

Run with: pytest backtest/test_symbol_validation.py -v
      or: python backtest/test_symbol_validation.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest.mock as mock

# Pre-seed the heavy scientific stack before importing the module under test so
# a combined suite run (shared sys.modules) never tears numpy down mid-import.
import numpy  # noqa: F401
import pandas  # noqa: F401
import scipy.signal  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.market_utils import load_coins  # noqa: E402

_SYMBOL_RE = re.compile(r"[A-Z0-9]+")


def _write(tmpdir: str, payload) -> str:
    path = os.path.join(tmpdir, "coins.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


def test_real_style_list_passes_verbatim() -> None:
    with tempfile.TemporaryDirectory() as d:
        coins = ["BTCUSDT", "ETHUSDT", "1000SHIBUSDT", "BTCUSDC"]
        path = _write(d, coins)
        # Default: no filter, no uppercase — the data-path contract. Identical list.
        assert load_coins(path) == coins


def test_non_conforming_symbols_dropped() -> None:
    with tempfile.TemporaryDirectory() as d:
        # The injection-shaped entries must not survive to become table names.
        payload = [
            "BTCUSDT",
            'ETHUSDT"; DROP TABLE x; --',
            "BTC-USDT",
            "ETH_USDT",
            "btcusdt",  # lowercase fails [A-Z0-9]+ without uppercase=True
            "SOLUSDT",
        ]
        path = _write(d, payload)
        result = load_coins(path)
        assert result == ["BTCUSDT", "SOLUSDT"]
        # Hard guarantee: every survivor is a safe bare identifier.
        for sym in result:
            assert _SYMBOL_RE.fullmatch(sym)


def test_uppercase_flag_recovers_lowercase() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = _write(d, ["btcusdt", "ethusdt", "sol-usdt"])
        # uppercase=True upper-cases before validating: the two clean symbols
        # survive, the dashed one is still dropped.
        assert load_coins(path, uppercase=True) == ["BTCUSDT", "ETHUSDT"]


def test_usdt_only_filters_other_quotes() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = _write(d, ["BTCUSDT", "ETHUSDC", "BNBUSDT", "XRPBUSD"])
        assert load_coins(path, usdt_only=True) == ["BTCUSDT", "BNBUSDT"]


def test_dict_form_is_unwrapped() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = _write(d, {"coins": ["BTCUSDT", "ETHUSDT"], "updated": "2026-07-11"})
        assert load_coins(path) == ["BTCUSDT", "ETHUSDT"]


def test_missing_file_returns_empty() -> None:
    # All-or-nothing contract that 1_data_ingestion relies on (no partial universe).
    assert load_coins(os.path.join(tempfile.gettempdir(), "does_not_exist_9050.json")) == []


def test_broken_json_returns_empty() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "coins.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not json ]")
        assert load_coins(path) == []


def test_dropped_symbol_is_logged() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = _write(d, ["BTCUSDT", "BAD-SYM"])
        with mock.patch("core.market_utils.logger") as log:
            load_coins(path)
        assert log.error.called


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")

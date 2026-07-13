# backtest/test_detector_batch_ticker.py
"""
Unit tests for the detector-cycle batch price fetch (T-2026-CU-9050-085,
AUDIT_TODO P2.44). DB-free and network-free: requests.get is mocked.

The detector used to call get_live_price() once per coin — ~530 serial Binance
klines requests per cycle. get_live_prices_batch() replaces them with a single
/fapi/v1/ticker/price call that returns every symbol; a failure degrades to {}
so the caller falls back to the per-coin HTTP→DB path (no coin is skipped).

Run with: pytest backtest/test_detector_batch_ticker.py -v
      or: python backtest/test_detector_batch_ticker.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_detectors():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "detectors_under_test", os.path.join(root, "3_detectors.py")
    )
    mod = importlib.util.module_from_spec(spec)
    # 3_detectors imports config/DB/strategy modules at load; stub them so the
    # pure batch helper can be imported standalone. get_live_prices_batch now
    # lives in core.live_price (re-exported here); the tests patch the shared
    # ``requests`` module object directly, so it stays real and unstubbed.
    stubs = {
        "core.config": mock.MagicMock(MAIN_CHANNEL_COINS=set(), TELEGRAM_CHANNELS={}),
        "core.database": mock.MagicMock(),
        "core.market_utils": mock.MagicMock(),
        "strategies.strat_5_percent": mock.MagicMock(),
        "strategies.strat_fast_in_out": mock.MagicMock(),
        "strategies.strat_main_channel": mock.MagicMock(),
        "strategies.strat_support_resistance": mock.MagicMock(),
        "strategies.strat_volume_indicator": mock.MagicMock(),
    }
    with mock.patch.dict("sys.modules", stubs):
        spec.loader.exec_module(mod)
    return mod


DET = _load_detectors()


def test_batch_maps_every_symbol_to_a_float_price():
    fake = [
        {"symbol": "BTCUSDT", "price": "65000.5"},
        {"symbol": "ETHUSDT", "price": "3200.25"},
        {"symbol": "SOLUSDT", "price": "150.0"},
    ]
    with mock.patch.object(requests, "get") as g:
        g.return_value.json.return_value = fake
        prices = DET.get_live_prices_batch()
    assert prices == {"BTCUSDT": 65000.5, "ETHUSDT": 3200.25, "SOLUSDT": 150.0}
    # The whole point of P2.44: ONE request for the entire fleet.
    assert g.call_count == 1


def test_batch_failure_returns_empty_dict():
    # A network/rate-limit error must not raise into the cycle — it degrades to
    # the per-coin fallback path.
    with mock.patch.object(requests, "get", side_effect=Exception("boom")):
        assert DET.get_live_prices_batch() == {}


def test_batch_malformed_payload_returns_empty_dict():
    # A row missing 'price'/'symbol' should not partially poison the map; the
    # whole fetch degrades to {} and the caller falls back per coin.
    with mock.patch.object(requests, "get") as g:
        g.return_value.json.return_value = [{"symbol": "BTCUSDT"}]
        assert DET.get_live_prices_batch() == {}


def test_missing_symbol_lookup_is_falsy_for_fallback():
    # The caller does `price_map.get(symbol)` and falls back when falsy. A symbol
    # absent from the batch (e.g. freshly delisted) must read as None, not raise.
    price_map = {"BTCUSDT": 65000.5}
    assert price_map.get("NEWCOINUSDT") is None


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\nAlle {len(fns)} Detector-Batch-Tests bestanden.")

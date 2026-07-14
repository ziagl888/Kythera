# backtest/test_ingestion_ws_timeframes.py
"""
C-Gate Phase 2 slice 2c (T-2026-CU-9050-119): 1d/1w come off the WebSocket
(D-2026-CLD-109 #3) but STAY on the REST/catch-up path.

DB-free: loads 1_data_ingestion (leading-digit module → importlib, like
test_gap_continuity) and pins the WS-vs-REST timeframe split + that the WS
stream builders emit no 1d/1w kline streams.

Run with: pytest backtest/test_ingestion_ws_timeframes.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")


def _load_ingestion():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "1_data_ingestion.py")
    spec = importlib.util.spec_from_file_location("kythera_ingestion", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ing = _load_ingestion()


def test_ws_timeframes_drops_1d_1w():
    assert ing.WS_EXCLUDED_TIMEFRAMES == frozenset({"1d", "1w"})
    assert ing.WS_TIMEFRAMES == ["5m", "15m", "30m", "1h", "2h", "4h"]
    assert "1d" not in ing.WS_TIMEFRAMES
    assert "1w" not in ing.WS_TIMEFRAMES


def test_rest_timeframes_keep_1d_1w():
    # The REST/catch-up + resume path still fetches every timeframe.
    assert "1d" in ing.TIMEFRAMES
    assert "1w" in ing.TIMEFRAMES
    # WS is exactly the REST set minus the excluded frames — no accidental drift.
    assert ing.WS_TIMEFRAMES == [tf for tf in ing.TIMEFRAMES if tf not in ing.WS_EXCLUDED_TIMEFRAMES]


def test_ws_stream_builder_emits_no_1d_1w():
    chunks = ing._new_symbol_stream_chunks(["BTCUSDT", "ETHUSDT"])
    streams = [s for chunk in chunks for s in chunk]
    assert streams, "expected some streams"
    assert not any(s.endswith("@kline_1d") or s.endswith("@kline_1w") for s in streams)
    # every remaining WS frame is present for each symbol
    for sym in ("btcusdt", "ethusdt"):
        for tf in ("5m", "15m", "30m", "1h", "2h", "4h"):
            assert f"{sym}@kline_{tf}" in streams


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

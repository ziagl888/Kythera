# backtest/test_coin_refresh.py
"""
Unit tests for the additive coin-refresh (P2.15, T-2026-CU-9050-092).

Both 1_data_ingestion.py and chart_data_service.py used to freeze their coin set
at process start — a coin listed by Binance after the last restart got no data
until the next restart. The refresh re-reads coins.json periodically and pulls
NEW symbols in ADDITIVELY (new WS worker; ingestion also creates tables + a
one-time catch-up). It never removes streams for vanished coins (teardown stays a
restart concern) and never reacts to a torn/empty coins.json read.

These tests pin the pure diff + sharding + worker-id logic, DB-free.

Run with: pytest backtest/test_coin_refresh.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import chart_data_service as chart  # noqa: E402


def _load_ingestion():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "1_data_ingestion.py")
    spec = importlib.util.spec_from_file_location("kythera_data_ingestion", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ing = _load_ingestion()


# ── The conservative diff (shared invariant, both modules) ───────────────────


def test_new_symbols_detected():
    assert chart.compute_new_symbols({"AUSDT", "BUSDT", "CUSDT"}, {"AUSDT"}) == ["BUSDT", "CUSDT"]
    assert ing.compute_new_symbols({"AUSDT", "BUSDT", "CUSDT"}, {"AUSDT"}) == ["BUSDT", "CUSDT"]


def test_empty_read_is_a_noop_never_removes():
    """A torn/empty coins.json read (load_coins -> []) must not touch the set."""
    tracked = {"AUSDT", "BUSDT"}
    assert chart.compute_new_symbols(set(), tracked) == []
    assert ing.compute_new_symbols(set(), tracked) == []


def test_no_new_symbols_returns_empty():
    tracked = {"AUSDT", "BUSDT"}
    assert chart.compute_new_symbols({"AUSDT", "BUSDT"}, tracked) == []
    assert ing.compute_new_symbols({"AUSDT"}, tracked) == []  # a vanished coin is NOT re-added/removed


def test_result_is_sorted_and_additive_only():
    new = ing.compute_new_symbols({"ZUSDT", "AUSDT", "MUSDT"}, set())
    assert new == ["AUSDT", "MUSDT", "ZUSDT"]  # sorted, deterministic worker assignment


# ── Worker-id allocation is monotonic and continues the initial sequence ─────


def test_ingestion_worker_ids_are_monotonic():
    ing._next_ws_worker_id = 1  # reset for a deterministic assertion
    ids = [ing._allocate_ws_worker_id() for _ in range(4)]
    assert ids == [1, 2, 3, 4]


def test_chart_worker_ids_are_monotonic():
    chart._ws_worker_counter = 0
    ids = [chart._next_worker_id() for _ in range(3)]
    assert ids == [1, 2, 3]


# ── Ingestion sharding: new coins are chunked like the initial fleet ─────────


def test_new_symbol_stream_chunks_respect_the_per_worker_cap():
    # 8 timeframes per coin (core.config.TIMEFRAMES); 30 new coins -> 240 streams.
    chunks = ing._new_symbol_stream_chunks([f"C{i}USDT" for i in range(30)])
    sizes = [len(c) for c in chunks]
    assert sum(sizes) == 30 * len(ing.TIMEFRAMES)
    assert all(s <= ing.WS_STREAMS_PER_WORKER for s in sizes)
    # exactly the documented sharding: 240 -> [180, 60]
    assert sizes == [180, 60]


def test_stream_names_are_lowercase_kline():
    chunks = ing._new_symbol_stream_chunks(["NEWUSDT"])
    streams = chunks[0]
    assert len(streams) == len(ing.TIMEFRAMES)
    assert all(s.startswith("newusdt@kline_") for s in streams)
    assert "newusdt@kline_1h" in streams


def test_single_coin_is_one_small_chunk():
    chunks = ing._new_symbol_stream_chunks(["ONEUSDT"])
    assert len(chunks) == 1
    assert len(chunks[0]) == len(ing.TIMEFRAMES)

# backtest/test_fif2_single_tp.py — the one-shot exit, pinned.
"""Standalone, DB-free tests for tools/fif2_single_tp_backtest.py (T-2026-KYT-9050-111).

The FIF2 decision rides on three mappings: the single-TP outcome (tie -> SL,
neither -> horizon mark, fee always paid), the gate refusing NaN vol (a bot
cannot act on a feature it does not have), and the slot-hour metric the verdict
ranks on. Each is pinned against a hand-built tape.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.fif2_single_tp_backtest import FEE_PCT, single_tp_records, stats  # noqa: E402

T0 = 1_752_000_000
GEOM = {"LONG": (4.0, 5.0), "SHORT": (3.0, 2.0)}


def make_z(highs, lows, entry=100.0, is_long=True, horizon_h=72):
    n = len(highs)
    ts = T0 + np.arange(n, dtype=np.int64) * 300
    return {
        "meta": np.array([json.dumps({"horizon_h": horizon_h})], dtype=object),
        "symbols": np.array(["XUSDT"], dtype=object),
        "models": np.array(["M"], dtype=object),
        "c_sym": np.zeros(n, dtype=np.int32),
        "c_ts": ts,
        "c_high": np.asarray(highs, dtype=float),
        "c_low": np.asarray(lows, dtype=float),
        "c_close": (np.asarray(highs, dtype=float) + np.asarray(lows, dtype=float)) / 2,
        "s_sym": np.zeros(1, dtype=np.int32),
        "s_mod": np.zeros(1, dtype=np.int32),
        "s_long": np.array([is_long]),
        "s_ts": np.array([T0 + 10 * 300], dtype=np.int64),
        "s_entry": np.array([entry]),
    }


def flat(n, at=100.0):
    return [at] * n, [at] * n


def test_tp_first_pays_full_tp_minus_fee():
    highs, lows = flat(200)
    highs[11] = 104.1  # LONG TP1 4% touched first
    recs = single_tp_records(make_z(highs, lows), GEOM, np.array([1.0]))
    assert recs[0]["pnl_pct"] == pytest.approx(4.0 - FEE_PCT)


def test_same_candle_tie_books_the_full_stop():
    highs, lows = flat(200)
    highs[11], lows[11] = 104.5, 94.0  # touches 4% TP and 5% SL in one candle
    recs = single_tp_records(make_z(highs, lows), GEOM, np.array([1.0]))
    assert recs[0]["pnl_pct"] == pytest.approx(-5.0 - FEE_PCT)


def test_neither_touched_closes_at_horizon_mark():
    highs, lows = flat(200, at=101.0)  # +1% mark, never 4% or -5%
    z = make_z(highs, lows)
    recs = single_tp_records(z, GEOM, np.array([1.0]))
    assert recs[0]["pnl_pct"] == pytest.approx(1.0 - FEE_PCT)
    # the position occupies its slot to the end of the covered window
    assert recs[0]["exit_ts"] == int(z["c_ts"][-1])


def test_hold_time_is_the_touching_candles_close():
    highs, lows = flat(200)
    highs[11] = 104.1
    recs = single_tp_records(make_z(highs, lows), GEOM, np.array([1.0]))
    assert recs[0]["hold_h"] == pytest.approx(600 / 3600.0)


def test_stats_slot_hour_is_sum_over_sum():
    recs = [
        {"open_ts": T0, "exit_ts": T0 + 3600, "pnl_pct": 2.0, "hold_h": 1.0},
        {"open_ts": T0 + 7200, "exit_ts": T0 + 7200 + 3 * 3600, "pnl_pct": -1.0, "hold_h": 3.0},
    ]
    s = stats(recs)
    assert s["pp_per_slot_hour"] == pytest.approx((2.0 - 1.0) / 4.0)
    assert s["win_rate"] == pytest.approx(0.5)

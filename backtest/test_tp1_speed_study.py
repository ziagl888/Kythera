# backtest/test_tp1_speed_study.py — the label and the look-back, pinned.
"""Standalone, DB-free tests for tools/tp1_speed_study.py (T-2026-KYT-9050-110).

Two things decide whether this study means anything: the label must book a
same-candle TP/SL tie as SL (the conservative convention every replay in this
repo uses), and the features must never read the candle that was still forming
at the signal instant (hard rule 5 — the classic way studies here have gone
wrong). Both are pinned against hand-built tapes, plus the tie-averaged AUC.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.tp1_speed_study import (  # noqa: E402
    auc_fast_vs_rest,
    compute_labels,
    series_features,
)

T0 = 1_752_000_000


def make_z(highs, lows, signal_offset_candles=10, entry=100.0, is_long=True, horizon_h=72):
    """One-symbol export stub. Candle i opens at T0 + i*300."""
    n = len(highs)
    ts = T0 + np.arange(n, dtype=np.int64) * 300
    return {
        "meta": np.array([json.dumps({"horizon_h": horizon_h})], dtype=object),
        "symbols": np.array(["XUSDT"], dtype=object),
        "c_sym": np.zeros(n, dtype=np.int32),
        "c_ts": ts,
        "c_high": np.asarray(highs, dtype=float),
        "c_low": np.asarray(lows, dtype=float),
        "c_close": (np.asarray(highs, dtype=float) + np.asarray(lows, dtype=float)) / 2,
        "s_sym": np.zeros(1, dtype=np.int32),
        "s_long": np.array([is_long]),
        "s_ts": np.array([T0 + signal_offset_candles * 300], dtype=np.int64),
        "s_entry": np.array([entry]),
    }


def flat(n, at=100.0):
    return [at] * n, [at] * n


# ── the label ────────────────────────────────────────────────────────────────


def test_same_candle_tie_books_as_sl_not_fast_tp():
    """LONG 100: candle 11 touches 104 AND 95 — the tie is an SL, never a win."""
    highs, lows = flat(200)
    highs[11], lows[11] = 104.5, 94.0
    lab = compute_labels(make_z(highs, lows))
    assert bool(lab["covered"][0])
    assert not bool(lab["tp_first"][0])


def test_tp_touch_time_is_the_touching_candles_close():
    """TP1 on the first post-signal candle: hit 600 s after the signal (candle
    opens at +300, closes at +600) — squarely inside the 4 h window."""
    highs, lows = flat(200)
    highs[11] = 104.1
    lab = compute_labels(make_z(highs, lows))
    assert bool(lab["tp_first"][0])
    assert lab["hit_s"][0] == pytest.approx(600.0)


def test_slow_tp1_is_tp_first_but_outside_the_4h_window():
    """TP1 only after 4 h: still TP-first, but the fast_4h label must not fire."""
    highs, lows = flat(400)
    hit_idx = 11 + 49  # candle closes (49+1)*300 + 300 = 15300 s > 4 h after signal
    highs[hit_idx] = 104.1
    lab = compute_labels(make_z(highs, lows))
    assert bool(lab["tp_first"][0])
    assert lab["hit_s"][0] > 4 * 3600


def test_forming_candle_never_enters_the_features():
    """A poison value on the candle covering the signal instant must not leak
    into any as-of return — features end at the last CLOSED candle."""
    n = 30
    ts = T0 + np.arange(n, dtype=np.int64) * 300
    close = np.full(n, 100.0)
    close[7] = 80.0  # 1h-lookback anchor
    close[19] = 90.0  # last closed candle before the signal
    close[20] = 1000.0  # forming at signal time — poison
    s_ts = np.array([T0 + 20 * 300 + 150], dtype=np.int64)
    f = series_features(ts, close, s_ts, {"ret_1h": 3600})
    assert f["ret_1h"][0] == pytest.approx((90.0 / 80.0 - 1) * 100.0)


def test_missing_history_is_nan_not_a_number():
    ts = T0 + np.arange(5, dtype=np.int64) * 300
    close = np.full(5, 100.0)
    s_ts = np.array([T0 + 4 * 300], dtype=np.int64)
    f = series_features(ts, close, s_ts, {"ret_24h": 86400})
    assert np.isnan(f["ret_24h"][0])


# ── the metric ───────────────────────────────────────────────────────────────


def test_auc_separates_and_averages_ties():
    perfect, _ = auc_fast_vs_rest(np.array([1.0, 2, 3, 4]), np.array([0, 0, 1, 1]))
    inverted, _ = auc_fast_vs_rest(np.array([1.0, 2, 3, 4]), np.array([1, 1, 0, 0]))
    tied, _ = auc_fast_vs_rest(np.array([1.0, 1, 2, 2]), np.array([0, 1, 0, 1]))
    assert perfect == pytest.approx(1.0)
    assert inverted == pytest.approx(0.0)
    assert tied == pytest.approx(0.5)


def test_auc_drops_nan_rows():
    a, n = auc_fast_vs_rest(np.array([np.nan, 1.0, 2.0]), np.array([1, 0, 1]))
    assert n == 2
    assert a == pytest.approx(1.0)

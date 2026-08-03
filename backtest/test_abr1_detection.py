# backtest/test_abr1_detection.py
"""
Unit tests for break & retest detection in 18_ai_abr1_bot
(find_pivot_levels + find_break_retest_setups, detector rework 2026-07).

Covers exactly the error classes of the old inline logic:
  1. Direction coupling: high-touch from below a broken resistance
     (= failed breakout) must NOT be LONG anymore.
  2. Hold check: close below the level between break and retest invalidates.
  3. First-touch: only the first retest after the break counts.
  4. Confirmed pivots: edge pivots without PIVOT_WINDOW candle confirmation
     no longer exist (repainting, R07-ABR1-b).

Run with: pytest backtest/test_abr1_detection.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# 18_ai_abr1_bot imports pandas_ta at module level (registers df.ta accessor).
# pandas_ta is in requirements.txt and installed on the VPS; on a
# Python 3.14 build machine it is not installable (pulls numba, no wheel for
# 3.14, source build fails). Without this guard the whole file breaks on
# collection instead of reporting a named skip.
pytest.importorskip("pandas_ta", reason="pandas_ta not installed (numba has no cp314 wheel)")


def _import_abr1():
    path = os.path.join(REPO_ROOT, "18_ai_abr1_bot.py")
    spec = importlib.util.spec_from_file_location("abr1_bot_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["abr1_bot_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


abr1 = _import_abr1()

LEVEL = 100.0  # Resistance level for LONG scenarios (band at ±0.5%: 99.5–100.5)


def make_df(rows):
    """rows: list of (open, high, low, close) — volume/open_time synthetic."""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"]).astype(float)
    df["volume"] = 1000.0
    df["open_time"] = pd.date_range("2026-01-01", periods=len(df), freq="h", tz="UTC")
    return df


def build_long_series(hold_candle=None, retest=None):
    """Resistance 100 (pivot t=10), upbreak t=26, hold t=27–36, retest t=37.

    hold_candle: optional {idx: (o,h,l,c)} to replace individual hold candles.
    retest: optional (o,h,l,c) for the retest candle t=37.
    """
    rows = []
    for t in range(10):  # Baseline below the level, slightly rising (no side pivots)
        base = 90.0 + 0.05 * t
        rows.append((base, base + 0.5, base - 2.0, base))
    rows.append((95.0, LEVEL, 93.0, 95.0))  # t=10: pivot high = level
    for t in range(11, 26):  # stay below the level
        base = 94.0 + 0.05 * (t - 11)
        rows.append((base, base + 1.0, base - 1.0, base))
    rows.append((95.0, 102.0, 94.5, 101.5))  # t=26: break (prev close < 100 < close)
    for k in range(10):  # t=27..36: hold above the level, no band touch (lows > 100.5)
        rows.append((101.0 + 0.1 * k, 101.8 + 0.1 * k, 100.8 + 0.1 * k, 101.3 + 0.1 * k))
    rows.append(retest or (101.4, 101.6, 100.2, 101.2))  # t=37: first retest from above

    if hold_candle:
        for idx, candle in hold_candle.items():
            rows[idx] = candle
    return make_df(rows)


def build_short_series():
    """Support 90 (pivot t=10), downbreak t=26, hold t=27–36, retest t=37 from below."""
    rows = []
    for t in range(10):  # Baseline above the level, slightly declining
        base = 97.0 - 0.05 * t
        rows.append((base + 1.0, base + 2.0, base, base + 1.0))
    rows.append((93.0, 94.0, 90.0, 92.5))  # t=10: pivot low = level 90
    for t in range(11, 26):
        base = 91.2 + 0.03 * (t - 11)
        rows.append((92.0, 93.5, base, 92.2))
    rows.append((92.0, 92.5, 88.0, 88.5))  # t=26: break down (prev close > 90 > close)
    for k in range(10):  # t=27..36: hold below the level, highs < 89.55 (no band touch)
        rows.append((88.5, 89.3 - 0.02 * k, 87.8 - 0.02 * k, 88.4 - 0.02 * k))
    rows.append((89.5, 90.2, 88.9, 89.2))  # t=37: retest from below, close < 90
    return make_df(rows)


# ── Pivot confirmation ─────────────────────────────────────────────────────────

def test_confirmed_resistance_pivot_found():
    df = build_long_series()
    levels = abr1.find_pivot_levels(df)
    res = [l for l in levels if l["type"] == "resistance" and l["price"] == LEVEL]
    assert len(res) == 1
    assert res[0]["index"] == 10


def test_unconfirmed_edge_pivot_ignored():
    """Spike in the last PIVOT_WINDOW candles must NOT be a level anymore (repainting)."""
    rows = [(90 + 0.05 * t, 90.5 + 0.05 * t, 88 + 0.05 * t, 90 + 0.05 * t) for t in range(30)]
    rows[27] = (95.0, 100.0, 93.0, 95.0)  # Spike 3 candles before end — unconfirmed
    df = make_df(rows)
    assert abr1.find_pivot_levels(df) == []


# ── Valid setups ────────────────────────────────────────────────────────────

def test_valid_long_break_retest_detected():
    df = build_long_series()
    levels = abr1.find_pivot_levels(df)
    setups = abr1.find_break_retest_setups(df, len(df) - 1, levels)
    assert len(setups) == 1
    s = setups[0]
    assert s["direction"] == "LONG"
    assert s["level_price"] == LEVEL
    assert s["break_idx"] == 26
    f = s["features"]
    assert f["setup_candles_since_break"] == 11.0
    assert f["setup_level_age_candles"] == 27.0
    assert f["setup_break_strength_pct"] == pytest.approx(1.5)
    assert f["setup_dist_close_level_pct"] == pytest.approx(1.2)
    assert f["setup_retest_wick_pct"] == pytest.approx((101.2 - 100.2) / 101.2 * 100)


def test_valid_short_break_retest_detected():
    df = build_short_series()
    levels = abr1.find_pivot_levels(df)
    setups = abr1.find_break_retest_setups(df, len(df) - 1, levels)
    assert len(setups) == 1
    s = setups[0]
    assert s["direction"] == "SHORT"
    assert s["level_price"] == 90.0
    assert s["break_idx"] == 26


# ── Error class 1: Direction coupling ───────────────────────────────────────

def test_failed_breakout_high_touch_is_not_long():
    """Price falls back below the level after the break and rallies from below
    to the band (high-touch). The old OR logic made this a LONG —
    this is the training-LOSS class (failed_breakout) and must be empty."""
    hold = {i: (98.0, 99.0, 97.5, 98.0 + 0.02 * (i - 27)) for i in range(27, 37)}
    hold[27] = (101.0, 101.5, 97.5, 98.0)  # Fall back below the level
    df = build_long_series(hold_candle=hold, retest=(98.5, 100.2, 98.0, 98.5))
    levels = abr1.find_pivot_levels(df)
    assert abr1.find_break_retest_setups(df, len(df) - 1, levels) == []


def test_retest_close_back_below_level_rejected():
    """Low in the band, but close back below the level → no hold, no LONG."""
    df = build_long_series(retest=(101.0, 101.3, 100.2, 99.8))
    levels = abr1.find_pivot_levels(df)
    assert abr1.find_break_retest_setups(df, len(df) - 1, levels) == []


# ── Error class 2: Hold check ────────────────────────────────────────────────

def test_close_below_level_just_before_retest_rejected():
    """The candle just before the retest closes below the level → no
    valid break remaining between level loss and retest → setup invalidated."""
    df = build_long_series(hold_candle={36: (101.0, 101.5, 99.0, 99.7)})
    levels = abr1.find_pivot_levels(df)
    assert abr1.find_break_retest_setups(df, len(df) - 1, levels) == []


def test_dip_and_rebreak_anchors_to_fresh_break():
    """Dip below the level midway through hold + fresh breakout afterward: this is
    a FRESH break (trainer semantics — every cross is a break event).
    The setup must anchor to the re-break at t=33, not the original break at t=26."""
    df = build_long_series(hold_candle={32: (101.0, 101.5, 99.0, 99.7)})
    levels = abr1.find_pivot_levels(df)
    setups = abr1.find_break_retest_setups(df, len(df) - 1, levels)
    assert len(setups) == 1
    assert setups[0]["direction"] == "LONG"
    assert setups[0]["break_idx"] == 33
    assert setups[0]["features"]["setup_candles_since_break"] == 4.0


# ── Error class 3: First-touch ────────────────────────────────────────────────

def test_second_touch_rejected_first_touch_detected():
    """t=32 already touches the band (low 100.3) — the retest at t=37 is
    then the SECOND touch and doesn't count; t=32 itself is the valid one."""
    df = build_long_series(hold_candle={32: (101.5, 101.8, 100.3, 101.5)})
    levels = abr1.find_pivot_levels(df)
    assert abr1.find_break_retest_setups(df, len(df) - 1, levels) == []
    first_touch = abr1.find_break_retest_setups(df, 32, levels)
    assert len(first_touch) == 1
    assert first_touch[0]["direction"] == "LONG"
    assert first_touch[0]["break_idx"] == 26


# Note: A test "break in pivot confirmation window is rejected" is
# geometrically impossible to construct — a candle that closes above the pivot high
# prevents pivot confirmation itself via greater_equal. The
# earliest-break guard in find_break_retest_setups is a redundant
# safety net (trainer semantics), not an independently testable path.

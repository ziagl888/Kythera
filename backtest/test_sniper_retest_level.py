# backtest/test_sniper_retest_level.py
"""Standalone (DB-free) guard for P2.39 on 25_smc_ml_sniper.py.

Break-and-Retest (Breaker Block) used to score `peak_idx[-2]` / `trough_idx[-2]`
blindly. If the live retest belonged to a different swing — the newest pivot or
an older one — the bot checked the wrong level and either missed the setup or
scored a level the price was nowhere near. `find_breaker_setup` now walks the
pivots newest-first and returns the one the price is actually retesting, gated
by breakout freshness (<= MAX_BB_AGE) and a follow-through past the level.

Loader note: 25_smc_ml_sniper.py loads its XGB artifacts at import and calls
exit(1) on failure, so joblib.load is mocked to a valid artifact dict. pandas /
numpy are imported before the sys.modules patch (memory
patch-dict-sys-modules-numpy-teardown).

Run: py -3.13 backtest/test_sniper_retest_level.py   (or: pytest -q)
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import unittest.mock as mock

import numpy as np
import pandas as pd  # noqa: F401  (pre-seed before any sys.modules patch)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_sniper():
    spec = importlib.util.spec_from_file_location(
        "smc_ml_sniper", os.path.join(REPO_ROOT, "25_smc_ml_sniper.py")
    )
    mod = importlib.util.module_from_spec(spec)
    fake_artifact = {
        "model": mock.MagicMock(),
        "features": ["dir_num"],
        "optimal_threshold": None,
        "calibrator_isotonic": None,
        "meta": {},
    }
    fake_joblib = mock.MagicMock()
    fake_joblib.load.return_value = fake_artifact
    with mock.patch.dict(
        "sys.modules",
        {
            "joblib": fake_joblib,
            "core.database": mock.MagicMock(),
            "core.config": mock.MagicMock(CH_SNIPER_BB=-1, CH_SNIPER_TD=-2),
            "core.market_utils": mock.MagicMock(COOLDOWN_MODULE_MAX_LEN=10),
            "core.trade_utils": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


sniper = _load_sniper()
SRC = open(os.path.join(REPO_ROOT, "25_smc_ml_sniper.py"), encoding="utf-8").read()

N_CLOSED = 40  # closed candles 0..39; index 40 would be the forming candle


def _blind_second_last_level(pivot_indices, level_arr, current_price, band=0.005):
    """The pre-P2.39 selection: always the second-to-last pivot, in-band or not."""
    p = int(pivot_indices[-2])
    level = float(level_arr[p])
    if level * (1 - band) <= current_price <= level * (1 + band):
        return p
    return None


def _long_arrays():
    """Old resistance at idx 5 (100), fresh resistance at idx 25 (110); price is
    retesting the FRESH one. Breakout close at idx 30, follow-through at idx 30."""
    highs = np.full(N_CLOSED + 1, 90.0)
    closes = np.full(N_CLOSED + 1, 90.0)
    highs[5] = 100.0
    highs[25] = 110.0
    closes[30] = 110.5          # first close above 110 after the pivot → breakout
    highs[30] = 111.0           # > 110 * 1.003 → follow-through
    return highs, closes


def test_long_picks_the_retested_level_not_the_second_last():
    highs, closes = _long_arrays()
    peak_idx = [5, 25]
    got = sniper.find_breaker_setup(peak_idx, highs, highs, closes, N_CLOSED, 110.0, "LONG")
    assert got == (25, 30), f"expected the fresh pivot (25, 30), got {got}"
    # Divergence canary: the blind [-2] rule would look at level 100 and miss.
    assert _blind_second_last_level(peak_idx, highs, 110.0) is None


def test_short_picks_the_retested_level_not_the_second_last():
    lows = np.full(N_CLOSED + 1, 200.0)
    closes = np.full(N_CLOSED + 1, 200.0)
    lows[5] = 100.0
    lows[25] = 90.0
    closes[30] = 89.5           # first close below 90 → breakdown
    lows[30] = 89.0             # < 90 * 0.997 → follow-through
    trough_idx = [5, 25]
    got = sniper.find_breaker_setup(trough_idx, lows, lows, closes, N_CLOSED, 90.0, "SHORT")
    assert got == (25, 30), f"expected (25, 30), got {got}"
    assert _blind_second_last_level(trough_idx, lows, 90.0) is None


def test_stale_breakout_is_rejected():
    highs = np.full(N_CLOSED + 1, 90.0)
    closes = np.full(N_CLOSED + 1, 90.0)
    highs[5] = 100.0
    closes[6] = 101.0           # breakout right after the pivot → 33 bars old > MAX_BB_AGE
    highs[6] = 101.0
    assert sniper.find_breaker_setup([5], highs, highs, closes, N_CLOSED, 100.0, "LONG") is None


def test_no_follow_through_is_rejected():
    highs = np.full(N_CLOSED + 1, 90.0)
    closes = np.full(N_CLOSED + 1, 90.0)
    highs[25] = 110.0
    closes[30] = 110.5          # breakout close exists and is fresh...
    highs[30] = 110.1           # ...but never runs 0.3% past the level
    assert sniper.find_breaker_setup([25], highs, highs, closes, N_CLOSED, 110.0, "LONG") is None


def test_price_outside_retest_band_is_rejected():
    highs, closes = _long_arrays()
    # price 105 is > 0.5% away from both 100 and 110
    assert sniper.find_breaker_setup([5, 25], highs, highs, closes, N_CLOSED, 105.0, "LONG") is None


def test_forming_candle_excluded_from_breakout_window():
    """n_closed bounds the scan; a breakout that only exists on the forming candle
    (index N_CLOSED) must not be seen."""
    highs = np.full(N_CLOSED + 1, 90.0)
    closes = np.full(N_CLOSED + 1, 90.0)
    highs[25] = 110.0
    closes[N_CLOSED] = 200.0    # forming candle only
    highs[N_CLOSED] = 200.0
    assert sniper.find_breaker_setup([25], highs, highs, closes, N_CLOSED, 110.0, "LONG") is None


# ---------------------------------------------------------------------------
# Source guards
# ---------------------------------------------------------------------------
def _scan_body():
    body = re.search(r"def scan_market\(\):\n(.*?)\ndef ", SRC, re.DOTALL)
    assert body, "scan_market body not found"
    return body.group(1)


def test_bb_section_uses_the_selector_not_blind_indexing():
    body = _scan_body()
    assert "find_breaker_setup(peak_idx" in body, "BB LONG no longer uses find_breaker_setup"
    assert "find_breaker_setup(trough_idx" in body, "BB SHORT no longer uses find_breaker_setup"
    assert "p_res = peak_idx[-2]" not in body, "blind peak_idx[-2] resurfaced in the BB section"
    assert "p_sup = trough_idx[-2]" not in body, "blind trough_idx[-2] resurfaced in the BB section"


def test_bb_features_anchored_on_the_retest_bar():
    """Feature-timing is deliberately the last closed candle (retest bar)."""
    body = _scan_body()
    assert body.count("extract_ml_features(df, len(df) - 2, 'LONG')") >= 1
    assert body.count("extract_ml_features(df, len(df) - 2, 'SHORT')") >= 1


def test_forming_candle_drop_still_present():
    """Guard-of-a-guard: the P1.46 closed-candle slice must survive this edit."""
    body = _scan_body()
    assert re.search(r"c_highs,\s*c_lows\s*=\s*highs\[:-1\],\s*lows\[:-1\]", body), (
        "P1.46 forming-candle drop was lost — pivots would repaint"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("OK — sniper break-and-retest level selection holds")

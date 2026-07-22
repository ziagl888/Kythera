"""DB-free tests for the vol-targeting sizing layer (tools/research/garch/vol_target).

Covers SPEC AK1 (size = target/forecast, clipped, NaN/<=0 -> MIN_SIZE, scalar ==
vectorized) and AK2 (apply_sizing = signal x size, sign never flipped). Pure
numpy/pandas — runs under plain fleet Python, no arch/ccxt.

Runnable standalone (`python backtest/test_garch_vol_target.py`) or via pytest.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "research", "garch")
)

import vol_target as vt  # noqa: E402


# --------------------------------------------------------------- AK1: sizing
def test_size_from_vol_basic_ratio():
    # target 15, forecast 30 -> 0.5x, within caps
    assert vt.size_from_vol(30.0, target_vol_ann=15.0) == 0.5
    # target 15, forecast 15 -> 1.0x
    assert vt.size_from_vol(15.0, target_vol_ann=15.0) == 1.0


def test_size_from_vol_caps():
    # calm (tiny forecast) is capped at MAX_LEVERAGE, not unbounded
    assert vt.size_from_vol(1.0, target_vol_ann=15.0) == vt.MAX_LEVERAGE
    # storm (huge forecast) floored at MIN_SIZE, never zero
    assert vt.size_from_vol(1000.0, target_vol_ann=15.0) == vt.MIN_SIZE


def test_size_from_vol_bad_input_is_min_size():
    for bad in (0.0, -5.0, float("nan"), None):
        assert vt.size_from_vol(bad, target_vol_ann=15.0) == vt.MIN_SIZE


def test_size_series_matches_scalar():
    fc = pd.Series([30.0, 15.0, 1.0, 1000.0, np.nan, 0.0, -3.0])
    vec = vt.size_series(fc, target_vol_ann=15.0)
    scal = [vt.size_from_vol(x, target_vol_ann=15.0) for x in fc]
    assert np.allclose(vec.to_numpy(), scal, equal_nan=False), (vec.tolist(), scal)


def test_size_series_within_caps():
    fc = pd.Series(np.linspace(0.5, 500, 200))
    s = vt.size_series(fc, target_vol_ann=15.0)
    assert s.min() >= vt.MIN_SIZE and s.max() <= vt.MAX_LEVERAGE


# ----------------------------------------------------------- AK2: apply_sizing
def test_apply_sizing_scalar_no_sign_flip():
    assert vt.apply_sizing(1, 0.5) == 0.5
    assert vt.apply_sizing(-1, 0.5) == -0.5  # short stays short
    assert vt.apply_sizing(0, 1.7) == 0  # flat stays flat


def test_apply_sizing_series_elementwise():
    sig = pd.Series([1.0, -1.0, 0.0, 1.0])
    size = pd.Series([0.5, 2.0, 1.0, 0.25])
    out = vt.apply_sizing(sig, size)
    assert out.tolist() == [0.5, -2.0, 0.0, 0.25]
    # a non-zero multiplier never turns a long into a short and vice-versa
    assert (np.sign(out[sig != 0]) == np.sign(sig[sig != 0])).all()


# --------------------------------------------------------------------- runner
def _run() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{'OK' if not failed else 'FAILED'}: {len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())

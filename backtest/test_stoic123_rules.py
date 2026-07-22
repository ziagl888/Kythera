"""DB-free unit tests for the Stoic 1-2-3 Phase-1 primitives (rules.py).

MA / Wilder-ATR correctness + causality, the close-not-wick break test, the base
detector, and the as-of HTF location gate (no future HTF leak). Runnable
standalone or via pytest.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "research", "stoic123")
sys.path.insert(0, _DIR)

from params import StoicParams  # noqa: E402
from rules import (  # noqa: E402
    compute_indicators,
    detect_base,
    htf_location_series,
    meaningful_break,
    moving_average,
    wilder_atr,
)


def _ohlc(closes, wick=0.1):
    closes = np.asarray(closes, float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {
            "date": pd.date_range("2022-01-01", periods=len(closes), freq="h"),
            "open": opens,
            "high": np.maximum(opens, closes) + wick,
            "low": np.minimum(opens, closes) - wick,
            "close": closes,
        }
    )


# -------------------------------------------------------------------- MAs
def test_sma_matches_rolling_mean():
    s = pd.Series(np.arange(1, 21, dtype=float))
    assert np.isclose(moving_average(s, 5, "sma").iloc[-1], np.mean(range(16, 21)))


def test_ema_seeded_and_causal():
    s = pd.Series([10.0, 11, 12, 13, 14])
    ema = moving_average(s, 3, "ema")
    assert ema.iloc[0] == 10.0  # adjust=False seeds on the first value
    # prefix-stable: truncating the tail does not change earlier EMA values
    assert np.allclose(moving_average(s.iloc[:3], 3, "ema").to_numpy(), ema.to_numpy()[:3])


# -------------------------------------------------------------------- ATR
def test_wilder_atr_positive_and_causal():
    df = _ohlc(100 + np.cumsum(np.random.default_rng(0).normal(0, 1, 60)))
    atr = wilder_atr(df, 14)
    tail = atr.dropna()
    assert (tail > 0).all()
    # a future bar must not change an earlier ATR (trailing recursion)
    atr_short = wilder_atr(df.iloc[:40], 14)
    both = atr.iloc[:40].notna() & atr_short.notna()
    assert np.allclose(atr.iloc[:40][both], atr_short[both])


# --------------------------------------------------------- meaningful_break
def test_meaningful_break_uses_close_with_atr_margin():
    # close must clear level by k*atr; exactly at the margin is not enough
    assert meaningful_break(105.0, 100.0, 4.0, 1.0, "long")  # 105 > 104
    assert not meaningful_break(103.9, 100.0, 4.0, 1.0, "long")  # 103.9 < 104
    assert meaningful_break(95.0, 100.0, 4.0, 1.0, "short")  # 95 < 96
    assert not meaningful_break(96.1, 100.0, 4.0, 1.0, "short")


def test_meaningful_break_bad_inputs_false():
    for atr in (0.0, -1.0, float("nan")):
        assert not meaningful_break(200.0, 100.0, atr, 0.5, "long")
    assert not meaningful_break(float("nan"), 100.0, 4.0, 0.5, "long")


# ----------------------------------------------------------------- base
def test_detect_base_tight_window_returns_boundary():
    p = StoicParams(base_window=5, base_max_range_atr=1.5, retest_touch=False)
    window = _ohlc([105.0, 105.2, 104.9, 105.1, 105.0], wick=0.05)
    base = detect_base(window, atr_at_end=1.0, p=p, direction="long", fast_ma_at_end=110.0)
    assert base is not None
    assert np.isclose(base["boundary"], window["high"].max())  # long boundary = window high


def test_detect_base_wide_window_is_not_a_base():
    p = StoicParams(base_window=5, base_max_range_atr=1.5, retest_touch=False)
    window = _ohlc([100.0, 104.0, 101.0, 106.0, 102.0])  # range >> 1.5*atr
    assert detect_base(window, atr_at_end=1.0, p=p, direction="long", fast_ma_at_end=110.0) is None


def test_detect_base_retest_touch_gate():
    p = StoicParams(base_window=5, base_max_range_atr=1.5, retest_touch=True)
    window = _ohlc([105.0, 105.2, 104.9, 105.1, 105.0], wick=0.05)
    # fast MA far below the base low -> no retest touch -> rejected
    assert detect_base(window, 1.0, p, "long", fast_ma_at_end=100.0) is None
    # fast MA above the base low -> retest touched -> accepted
    assert detect_base(window, 1.0, p, "long", fast_ma_at_end=106.0) is not None


# ------------------------------------------------------ HTF location as-of
def test_htf_location_is_as_of_no_future_leak():
    p = StoicParams(htf_ma_period=3, htf_slope_lookback=1, htf_require_price_side=False)
    # LTF hourly bars across 3 days
    df = pd.DataFrame({"date": pd.date_range("2022-01-01", periods=72, freq="h")})
    df["open"] = df["high"] = df["low"] = df["close"] = 100.0
    # HTF: rising for 2 days then a sharp drop on day 4 (in the future for most LTF bars)
    htf = pd.DataFrame(
        {
            "date": pd.date_range("2022-01-01", periods=5, freq="D"),
            "close": [100.0, 101.0, 102.0, 103.0, 50.0],
        }
    )
    loc = htf_location_series(df, htf, p)
    # LTF bars on day 1 / day 2 map (as-of, backward) to the rising HTF bars ->
    # long-ok; the day-4 crash is entirely in the future and never leaks in.
    assert bool(loc.iloc[30]["htf_long_ok"]) is True  # ~day 1, rising HTF
    assert bool(loc.iloc[50]["htf_long_ok"]) is True  # ~day 2, rising HTF
    assert len(loc) == len(df)
    # no LTF bar reaches the day-4 crash, so nothing is ever short-ok here
    assert not loc["htf_short_ok"].any()


# --------------------------------------------------- compute_indicators wiring
def test_compute_indicators_attaches_columns():
    df = _ohlc(100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 60)))
    ind = compute_indicators(df, StoicParams())
    for col in ("ma_fast", "ma_slow", "atr"):
        assert col in ind.columns
    assert np.isfinite(ind["atr"].iloc[-1]) and ind["atr"].iloc[-1] > 0


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

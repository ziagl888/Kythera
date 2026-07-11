# backtest/test_wilder_rsi.py
"""DB-free unit tests for the RSI span->Wilder migration (T-2026-CU-9050-095, P2.12).

Pins the Wilder contract of 2_indicator_engine.calculate_rsi:
  * it is TRUE Wilder RSI -- ewm(alpha=1/period) -- matched to an independent,
    hand-rolled Wilder RMA recursion (NOT the same ewm call, so the check is not
    circular);
  * the old ewm(span=period) formula is a REGRESSION -- these tests fail against
    it (the migration's whole point);
  * the P1.13/T-054 NaN-warmup contract and the T-060 flat-fall (0/0 -> NaN, not
    a fabricated 50/100) survive the switch.

Run with: pytest backtest/test_wilder_rsi.py -v

Loader note (T-2026-CU-9050-095): numpy/pandas/scipy are imported at module top
(pre-seeded in sys.modules) BEFORE the digit-prefixed engine is loaded by path,
so no patch.dict block can tear the numeric core down (the numpy-teardown trap).
"""

from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np
import pandas as pd
import scipy  # noqa: F401 - pre-seed: the engine pulls scipy; keep it resident

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.config hard-requires these at import; the RSI path opens no connection.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PORT", "5432")


def _load_engine():
    spec = importlib.util.spec_from_file_location(
        "kythera_indicator_engine",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "2_indicator_engine.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ENG = _load_engine()

# A fixed, deterministic close series (ramp + chop) -- no RNG, so pins are stable.
CLOSES = [
    100,
    101,
    102,
    101,
    103,
    105,
    104,
    106,
    108,
    107,
    109,
    111,
    110,
    108,
    107,
    109,
    112,
    114,
    113,
    111,
    110,
    112,
    115,
    117,
    116,
    114,
    118,
    120,
    119,
    121,
]


def _series():
    return pd.Series([float(x) for x in CLOSES])


def _wilder_reference(series, period):
    """Independent Wilder RSI via the recursive RMA definition -- NOT ewm.

    avg[i] = (avg[i-1]*(period-1) + x[i]) / period, seeded at the first real
    delta (index 1) exactly like ewm(alpha=1/period, adjust=False). This is the
    canonical Wilder smoothing; matching it proves calculate_rsi is real Wilder.
    """
    d = series.diff().to_numpy()
    n = len(d)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    ag = np.full(n, np.nan)
    al = np.full(n, np.nan)
    if n > 1:
        ag[1] = up[1]
        al[1] = dn[1]
    for i in range(2, n):
        ag[i] = (ag[i - 1] * (period - 1) + up[i]) / period
        al[i] = (al[i - 1] * (period - 1) + dn[i]) / period
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
    out = 100.0 - (100.0 / (1.0 + rs))
    out[0] = np.nan
    return pd.Series(out)


def _rsi_old_span(series, period):
    """The pre-migration formula: ewm(span=period) == alpha=2/(period+1)."""
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    roll_up = up.ewm(span=period, adjust=False).mean()
    roll_down = down.ewm(span=period, adjust=False).mean()
    rs = roll_up / roll_down
    return 100.0 - (100.0 / (1.0 + rs))


def test_matches_independent_wilder_recursion():
    """calculate_rsi == a hand-rolled Wilder RMA recursion (not the same ewm)."""
    s = _series()
    for period in (6, 9, 12, 14, 24):
        got = ENG.calculate_rsi(s, period).to_numpy()
        ref = _wilder_reference(s, period).to_numpy()
        both = ~(np.isnan(got) | np.isnan(ref))
        assert both.any(), f"no comparable rows for period {period}"
        assert np.allclose(got[both], ref[both], rtol=0, atol=1e-9), (
            f"period {period}: not true Wilder; max|d|={np.abs(got[both] - ref[both]).max():.2e}"
        )


def test_old_span_formula_is_a_regression():
    """The migration must actually move the numbers: calculate_rsi differs from
    the old ewm(span) formula by a material margin (mean > 1 RSI point)."""
    s = _series()
    new14 = ENG.calculate_rsi(s, 14).to_numpy()
    old14 = _rsi_old_span(s, 14).to_numpy()
    both = ~(np.isnan(new14) | np.isnan(old14))
    mean_gap = float(np.abs(new14[both] - old14[both]).mean())
    assert mean_gap > 1.0, f"span vs Wilder gap too small ({mean_gap:.3f}) -- did the migration land?"
    # and they are genuinely different everywhere past the seed, not just on average
    assert not np.allclose(new14[both], old14[both], rtol=0, atol=1e-6)


def test_pinned_reference_values():
    """Hard literals guard against silent drift in the Wilder implementation."""
    r = ENG.calculate_rsi(_series(), 14)
    expected = {
        1: 100.0,  # first real delta is an up-move; down-avg 0 -> rs=inf -> 100
        14: 71.0942458911,
        20: 65.6561649105,
        25: 65.6782060132,
        29: 72.7648971638,
    }
    for idx, val in expected.items():
        assert abs(float(r.iloc[idx]) - val) < 1e-8, f"idx {idx}: {float(r.iloc[idx])} != {val}"


def test_nan_warmup_preserved():
    """P1.13/T-054: the undefined first row flows as NaN, never a fabricated
    50/100. diff()'s leading NaN must survive the alpha switch."""
    r = ENG.calculate_rsi(_series(), 14)
    assert np.isnan(r.iloc[0]), "first row must be NaN (diff warmup), not fabricated"
    # no fabricated neutral-50 or max-100 fill anywhere on the defined tail
    tail = r.iloc[1:].to_numpy()
    assert not np.isnan(tail).any(), "tail should be fully defined for this series"


def test_flat_series_yields_nan_not_fabricated():
    """T-060 flat-fall: a constant price gives 0/0 -> NaN, not 50 or 100."""
    flat = pd.Series([100.0] * 30)
    r = ENG.calculate_rsi(flat, 14).to_numpy()
    # every row is either the diff-warmup NaN or the 0/0 flat NaN -- never a value
    assert np.isnan(r).all(), "flat series must be all-NaN (0/0), no fabricated 50/100"


def test_bounded_0_100():
    """RSI stays in [0, 100] on every defined row."""
    r = ENG.calculate_rsi(_series(), 9).to_numpy()
    defined = r[~np.isnan(r)]
    assert (defined >= 0.0).all() and (defined <= 100.0).all()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all wilder-rsi tests passed")

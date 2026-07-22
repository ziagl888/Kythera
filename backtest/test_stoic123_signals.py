"""DB-free tests for the Stoic 1-2-3 signals.csv contract (SPEC AK4).

Verifies the signals.csv format AND that it runs through the GARCH validation
harness (tools/research/garch/compare.py --signals) unchanged. No arch/ccxt: the
harness runs with a deterministic stub fitter. Runnable standalone or via pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile

import numpy as np
import pandas as pd

_STOIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "research", "stoic123")
_GARCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "research", "garch")
sys.path.insert(0, _STOIC)
sys.path.insert(0, _GARCH)

import compare as gcmp  # noqa: E402  (garch harness)
import garch_forecast as gf  # noqa: E402
from signals import signals_dataframe, write_signals_csv  # noqa: E402
from state_machine import generate_signals  # noqa: E402


class StubFitter:
    def __call__(self, window: np.ndarray):
        return gf.GarchParams(mu=0.05, omega=0.20, alpha=0.08, beta=0.90), float(np.var(window)) + 1e-9


def make_trending_ohlcv(n=650, seed=7) -> pd.DataFrame:
    """A trending-with-pullbacks random walk so some 1-2-3 sequences form."""
    rng = np.random.default_rng(seed)
    drift = np.concatenate([np.full(n // 2, 0.0015), np.full(n - n // 2, -0.0015)])
    steps = rng.normal(drift, 0.015)
    close = 100.0 * np.exp(np.cumsum(steps))
    opens = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, 0.004, n)) * close
    hi = np.maximum(opens, close) + wick
    lo = np.minimum(opens, close) - wick
    dates = pd.date_range("2021-01-01", periods=n, freq="h")
    return pd.DataFrame({"date": dates, "open": opens, "high": hi, "low": lo, "close": close})


def make_htf(n=90, slope=1.0) -> pd.DataFrame:
    close = 100.0 + np.cumsum(np.full(n, slope))
    return pd.DataFrame({"date": pd.date_range("2020-12-01", periods=n, freq="D"), "close": close})


# ------------------------------------------------------------- AK4: format
def test_signals_dataframe_format():
    df, htf = make_trending_ohlcv(), make_htf()
    sig = signals_dataframe(df, htf)
    assert list(sig.columns) == ["date", "signal"]
    assert set(np.unique(sig["signal"])).issubset({-1, 0, 1})
    assert len(sig) == len(df)


def test_signals_csv_written_and_loadable_by_harness():
    df, htf = make_trending_ohlcv(), make_htf()
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        write_signals_csv(df, htf, path)
        # the garch harness loader must accept it and align onto the wf dates
        wf_dates = df["date"].iloc[1:].reset_index(drop=True)
        loaded = gcmp.load_signals(path, wf_dates)
    finally:
        os.remove(path)
    assert set(np.unique(loaded.to_numpy())).issubset({-1.0, 0.0, 1.0})
    assert len(loaded) == len(wf_dates)


def test_end_to_end_through_compare_harness():
    """The direct-anschluss: 1-2-3 signals.csv -> compare.run_comparison (fixed
    vs vol-targeted), no arch, produces the fixed + vol-targeted stat blocks."""
    df, htf = make_trending_ohlcv(), make_htf()
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        write_signals_csv(df, htf, path)
        wf_dates = df["date"].iloc[1:].reset_index(drop=True)
        sig = gcmp.load_signals(path, wf_dates)
        stats, curves = gcmp.run_comparison(df, signals=sig, fit_fn=StubFitter())
    finally:
        os.remove(path)
    assert "sharpe" in stats["fixed_size"] and "sharpe" in stats["vol_targeted"]
    assert len(curves) > 0


def test_at_least_one_trade_on_trending_data():
    """The generator is not inert: a trending series yields >=1 non-flat bar."""
    df, htf = make_trending_ohlcv(), make_htf()
    sig = generate_signals(df, htf).to_numpy()
    assert (sig != 0).any(), "no 1-2-3 signal at all on trending data — generator inert"


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

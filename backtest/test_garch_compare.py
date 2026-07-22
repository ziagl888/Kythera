"""DB-free tests for the validation harness (tools/research/garch/compare).

Covers SPEC:
  AK8  — timing discipline: the position decided at t earns return(t+1)
         (next_ret = ret.shift(-1)), never the same bar.
  AK9  — signals.csv loads, forward-fills onto price dates, clips to [-1, 1].
  AK10 — perf_stats + worst_month are correct on a known return series.
  AK11 — verdict_from_stats gates PULLS / MIXED / NO-PULL / NO-DATA correctly.

Deterministic stub fitter, no arch/ccxt. Runnable standalone or via pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile

import numpy as np
import pandas as pd

_GARCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "research", "garch")
sys.path.insert(0, _GARCH_DIR)

import compare as cmp  # noqa: E402
import garch_forecast as gf  # noqa: E402


def make_prices(n: int = 600, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.02, size=n)
    close = 100.0 * np.exp(np.cumsum(steps))
    dates = pd.date_range("2021-01-01", periods=n, freq="D")
    return pd.DataFrame({"date": dates, "close": close})


class StubFitter:
    def __call__(self, window: np.ndarray):
        return gf.GarchParams(mu=0.05, omega=0.20, alpha=0.08, beta=0.90), float(np.var(window)) + 1e-9


# ---------------------------------------------------------------- AK8: timing
def test_position_earns_next_bar_return_not_same_bar():
    prices = make_prices(600)
    wf = gf.walkforward_garch(prices, fit_fn=StubFitter())
    n = len(wf)
    always_long = pd.Series(np.ones(n))

    _, curves = cmp.run_comparison(prices, signals=always_long, fit_fn=StubFitter())

    # recover the per-bar fixed-strategy returns from the equity curve
    fixed = curves["fixed"].to_numpy()
    recovered = np.empty_like(fixed)
    recovered[0] = fixed[0] - 1.0
    recovered[1:] = fixed[1:] / fixed[:-1] - 1.0
    recovered *= 100.0

    next_ret = wf["ret"].shift(-1)
    valid = wf["fcast_vol"].notna() & next_ret.notna()
    expected_next = next_ret[valid].to_numpy()
    same_bar = wf["ret"][valid].to_numpy()

    assert np.allclose(recovered, expected_next, atol=1e-9), "not aligned to ret(t+1)"
    # and it is genuinely the shifted series, not the same-bar return
    assert not np.allclose(recovered, same_bar, atol=1e-6), "looks like same-bar fill (lookahead)"


# --------------------------------------------------------------- AK9: signals
def test_load_signals_ffill_and_clip():
    dates = pd.Series(pd.date_range("2021-01-01", periods=6, freq="D"))
    rows = pd.DataFrame(
        {
            "date": ["2021-01-02", "2021-01-04", "2021-01-05"],
            "signal": [2, -3, 0],  # 2 -> clip 1 ; -3 -> clip -1 ; 0
        }
    )
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        rows.to_csv(path, index=False)
        s = cmp.load_signals(path, dates)
    finally:
        os.remove(path)
    # before the first signal date -> 0; then clipped + forward-filled
    assert list(s) == [0.0, 1.0, 1.0, -1.0, 0.0, 0.0]


def test_signals_csv_roundtrips_through_run_comparison():
    """A date,signal CSV (the T-024 output contract) runs end to end."""
    prices = make_prices(600)
    wf_dates = prices["date"].iloc[1:].reset_index(drop=True)
    sig_rows = pd.DataFrame({"date": wf_dates, "signal": np.tile([1, 0, -1], len(wf_dates))[: len(wf_dates)]})
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        sig_rows.to_csv(path, index=False)
        sig = cmp.load_signals(path, wf_dates)
        stats, _ = cmp.run_comparison(prices, signals=sig, fit_fn=StubFitter())
    finally:
        os.remove(path)
    assert "sharpe" in stats["fixed_size"] and "sharpe" in stats["vol_targeted"]


# --------------------------------------------------------------- AK10: metrics
def test_perf_stats_and_worst_month_known_series():
    # 30 days +1%, then 30 days -2% (in % units, as perf_stats expects)
    ret = pd.Series([1.0] * 30 + [-2.0] * 30)
    dates = pd.Series(pd.date_range("2021-01-01", periods=60, freq="D"))
    stats = cmp.perf_stats(ret, periods_per_year=365)

    expected_final = float(np.prod(1 + ret.to_numpy() / 100.0))
    assert stats["final_equity_x"] == round(expected_final, 2)
    assert stats["max_drawdown_pct"] < 0  # the -2% stretch draws down
    assert stats["ann_vol_pct"] > 0

    wm = cmp.worst_month(ret, dates)
    # the worst calendar month must be negative and no better than the -2%/day leg
    r_idx = pd.Series(ret.values / 100.0, index=pd.to_datetime(dates.values))
    monthly = r_idx.resample("ME").apply(lambda x: (1 + x).prod() - 1)
    assert wm == round(100 * monthly.min(), 1)
    assert wm < 0


def test_perf_stats_empty_series_is_empty():
    assert cmp.perf_stats(pd.Series([], dtype=float), periods_per_year=365) == {}


# --------------------------------------------------------------- AK11: verdict
def _pair(f_sharpe, v_sharpe, f_dd=-20.0, v_dd=-20.0, f_wm=-10.0, v_wm=-10.0):
    return {
        "fixed_size": {"sharpe": f_sharpe, "max_drawdown_pct": f_dd, "worst_month_pct": f_wm},
        "vol_targeted": {"sharpe": v_sharpe, "max_drawdown_pct": v_dd, "worst_month_pct": v_wm},
    }


def test_verdict_pulls():
    per_coin = {f"C{i}": _pair(0.5, 0.9) for i in range(3)}  # +0.4 sharpe, dd flat
    v = cmp.verdict_from_stats(per_coin)
    assert v["verdict"] == "PULLS", v
    assert v["median_sharpe_delta"] == 0.4


def test_verdict_no_pull_when_sharpe_flat_or_worse():
    per_coin = {f"C{i}": _pair(0.8, 0.6) for i in range(3)}  # -0.2 sharpe
    assert cmp.verdict_from_stats(per_coin)["verdict"] == "NO-PULL"


def test_verdict_mixed_when_sharpe_helps_but_drawdown_pays():
    # sharpe +0.4 but max-DD worsens well past the tolerance
    per_coin = {f"C{i}": _pair(0.5, 0.9, f_dd=-15.0, v_dd=-35.0) for i in range(3)}
    assert cmp.verdict_from_stats(per_coin)["verdict"] == "MIXED"


def test_verdict_no_data_on_empty():
    assert cmp.verdict_from_stats({})["verdict"] == "NO-DATA"


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

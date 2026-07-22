"""DB-free tests for the walk-forward GARCH core (tools/research/garch/garch_forecast).

Covers SPEC:
  AK3 — lookahead-free: the forecast at row t uses only returns <= t (proved by
        prefix-stability: forecasts on prices[:m] == the prefix of forecasts on
        the full series) + an exact hand-computed recursion pin.
  AK4 — rolling-window cap: a fit never sees more than max_window returns;
        max_window=None reproduces the expanding window.
  AK5 — GarchSizer (per-coin param cache + scheduled refit) fed the return
        history incrementally reproduces walkforward's sizing bar-for-bar and
        refits only every refit_every bars.
  AK6 — arch/ccxt imports are lazy: no real fit runs here, so neither is loaded.

A deterministic stub fitter replaces arch, so all of this runs under plain fleet
Python. Runnable standalone or via pytest.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_GARCH_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "research", "garch")
sys.path.insert(0, _GARCH_DIR)

import garch_forecast as gf  # noqa: E402
import vol_target as vt  # noqa: E402

MIN_TRAIN = 60
REFIT_EVERY = 10
MAX_WINDOW = 40


# ---------------------------------------------------------------- fixtures
def make_prices(n: int = 200, seed: int = 0) -> pd.DataFrame:
    """Deterministic geometric random walk -> date,close frame."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.02, size=n)
    close = 100.0 * np.exp(np.cumsum(steps))
    dates = pd.date_range("2022-01-01", periods=n, freq="D")
    return pd.DataFrame({"date": dates, "close": close})


class StubFitter:
    """Deterministic stand-in for arch: fixed stationary params, seed = window
    variance. Pure function of the window, so two instances agree. Records each
    fit-window length for the rolling-cap assertions."""

    def __init__(self) -> None:
        self.window_lengths: list[int] = []

    def __call__(self, window: np.ndarray):
        self.window_lengths.append(len(window))
        params = gf.GarchParams(mu=0.05, omega=0.20, alpha=0.08, beta=0.90)
        seed = float(np.var(window)) + 1e-9
        return params, seed


def _wf(prices, **kw):
    kw.setdefault("min_train", MIN_TRAIN)
    kw.setdefault("refit_every", REFIT_EVERY)
    kw.setdefault("periods_per_year", 365)
    return gf.walkforward_garch(prices, **kw)


# ------------------------------------------------------------- AK3: lookahead
def test_forecast_is_prefix_stable():
    """Truncating the future must not change any past forecast -> no lookahead."""
    prices = make_prices(200)
    full = _wf(prices, max_window=MAX_WINDOW, fit_fn=StubFitter())
    for m in (120, 160):
        short = _wf(prices.iloc[:m], max_window=MAX_WINDOW, fit_fn=StubFitter())
        a = short["fcast_vol"].to_numpy()
        b = full["fcast_vol"].to_numpy()[: len(short)]
        both = ~np.isnan(a) & ~np.isnan(b)
        assert both.any()
        assert np.allclose(a[both], b[both]), f"prefix drift at m={m}"


def test_future_price_change_leaves_early_forecasts_untouched():
    prices = make_prices(200)
    base = _wf(prices, max_window=None, fit_fn=StubFitter())["fcast_vol"].to_numpy()
    tampered = prices.copy()
    k = 180
    tampered.loc[k, "close"] *= 1.5  # shock a late bar
    after = _wf(tampered, max_window=None, fit_fn=StubFitter())["fcast_vol"].to_numpy()
    # a change at price index k first shows in ret[k-1]; forecasts up to t=k-2 are safe
    safe = k - 2
    a, b = base[:safe], after[:safe]
    both = ~np.isnan(a) & ~np.isnan(b)
    assert both.any() and np.allclose(a[both], b[both])


def test_walkforward_matches_reference_recursion():
    """Pin the exact conditional-variance recursion + refit-reseed convention."""
    prices = make_prices(90)
    rets = gf._returns_pct(prices)
    n = len(rets)
    p = gf.GarchParams(mu=0.05, omega=0.20, alpha=0.08, beta=0.90)

    def fixed_fit(window):
        return p, 3.14  # constant seed so the reference below is unambiguous

    wf = _wf(prices, min_train=MIN_TRAIN, refit_every=REFIT_EVERY, max_window=None, fit_fn=fixed_fit)
    got = wf["fcast_vol"].to_numpy() ** 2  # back to variance

    exp = np.full(n, np.nan)
    sigma2 = np.nan
    for t in range(MIN_TRAIN, n):
        if (t - MIN_TRAIN) % REFIT_EVERY == 0:
            sigma2 = 3.14
        eps = rets[t] - p.mu
        sigma2 = p.omega + p.alpha * eps**2 + p.beta * sigma2
        exp[t] = sigma2
    both = ~np.isnan(got) & ~np.isnan(exp)
    assert both.any() and np.allclose(got[both], exp[both])


# ------------------------------------------------------- AK4: rolling window
def test_rolling_window_cap_bounds_fit_size():
    prices = make_prices(200)
    stub = StubFitter()
    _wf(prices, max_window=MAX_WINDOW, fit_fn=stub)
    assert stub.window_lengths, "no fits happened"
    assert max(stub.window_lengths) <= MAX_WINDOW
    # once t exceeds the cap, the window is exactly the cap
    assert MAX_WINDOW in stub.window_lengths


def test_expanding_window_when_none():
    prices = make_prices(200)
    stub = StubFitter()
    _wf(prices, max_window=None, fit_fn=stub)
    # expanding: the largest fit window grows past the rolling cap
    assert max(stub.window_lengths) > MAX_WINDOW


# ------------------------------------------------------- AK5: GarchSizer parity
def _expected_refits(n_rets: int) -> int:
    return len(range(MIN_TRAIN, n_rets, REFIT_EVERY))


def test_sizer_reproduces_walkforward_sizing():
    prices = make_prices(200)
    target = 15.0
    wf = _wf(prices, max_window=MAX_WINDOW, fit_fn=StubFitter())
    rets = gf._returns_pct(prices)
    n = len(rets)

    sizer = gf.GarchSizer(
        target_vol_ann=target,
        min_train=MIN_TRAIN,
        refit_every=REFIT_EVERY,
        max_window=MAX_WINDOW,
        periods_per_year=365,
        fit_fn=StubFitter(),
    )
    # feed the append-only history one bar at a time
    for k in range(MIN_TRAIN + 1, n + 1):
        got = sizer.update(rets[:k])
        expected = vt.size_from_vol(float(wf["fcast_vol_ann"].iloc[k - 1]), target)
        assert abs(got - expected) < 1e-9, f"size mismatch at k={k}: {got} vs {expected}"

    assert sizer.fit_count == _expected_refits(n), (sizer.fit_count, _expected_refits(n))


def test_sizer_returns_min_size_before_min_train():
    sizer = gf.GarchSizer(min_train=MIN_TRAIN, fit_fn=StubFitter())
    rets = gf._returns_pct(make_prices(MIN_TRAIN))  # too short
    assert sizer.update(rets) == sizer.min_size
    assert sizer.fit_count == 0


def test_sizer_sparse_updates_match_dense():
    """Skipping intermediate calls (live gap) still lands on the same state,
    because update replays the missing bars once."""
    prices = make_prices(160)
    rets = gf._returns_pct(prices)
    n = len(rets)
    dense = gf.GarchSizer(min_train=MIN_TRAIN, refit_every=REFIT_EVERY, max_window=MAX_WINDOW, fit_fn=StubFitter())
    sparse = gf.GarchSizer(min_train=MIN_TRAIN, refit_every=REFIT_EVERY, max_window=MAX_WINDOW, fit_fn=StubFitter())
    last_dense = None
    for k in range(MIN_TRAIN + 1, n + 1):
        last_dense = dense.update(rets[:k])
    last_sparse = sparse.update(rets[:n])  # one shot
    assert abs(last_dense - last_sparse) < 1e-9
    assert dense.fit_count == sparse.fit_count


# --------------------------------------------------------- AK6: lazy imports
def test_no_arch_ccxt_imported():
    """No real fit / no ccxt fetch ran, so neither heavy dep was imported."""
    assert "arch" not in sys.modules, "arch was imported by the DB-free path"
    assert "ccxt" not in sys.modules, "ccxt was imported by the DB-free path"


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

"""garch_forecast.py — walk-forward GARCH(1,1) volatility forecasting.

What this does:
  Fits a GARCH(1,1) model walk-forward (no lookahead) and produces a
  1-bar-ahead conditional-volatility forecast for every bar in the sample,
  plus a calm/normal/storm regime label.

What this does NOT do:
  Predict direction. GARCH forecasts the MAGNITUDE of moves, not which way
  they go. Sizing only — compose with a direction engine via
  ``vol_target.apply_sizing`` (signal x size_multiplier).

Adapted from milesdeutscher/garchmethod (MIT) — see LICENSE.upstream. Kythera
adaptations vs upstream:
  * ccxt OHLCV input instead of yfinance (see ccxt_data.py); the core still
    eats a simple ``date, close`` DataFrame, so the data source is decoupled.
  * rolling-window cap (``max_window``) instead of an unbounded expanding
    window — bounds CPU/memory across a 538-coin refit loop. ``max_window=None``
    reproduces the upstream expanding window exactly.
  * injectable ``fit_fn`` so the walk-forward bookkeeping is testable under the
    plain fleet Python without ``arch`` installed.
  * ``GarchSizer`` — a stateful per-coin sizer that caches fitted params and
    refits only on schedule (the live 538-coin path), reproducing the
    walk-forward forecast series bar-for-bar.

Invariants:
  * Zero lookahead: the forecast written at row t is computed from returns with
    index <= t only; GARCH params are always estimated on strictly prior
    returns. Downstream (compare.py) still applies size@t to return(t+1).
  * The conditional-variance recursion and its refit-reseed convention are kept
    byte-identical to upstream; only the fit *window* and the fitter are
    swapped. ``GarchSizer`` and ``walkforward_garch`` share that recursion.

Usage (CLI, needs arch + ccxt):
  python garch_forecast.py --coin BTC/USDT
  python garch_forecast.py --csv prices.csv --json
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

TRADING_DAYS_CRYPTO = 365
TRADING_DAYS_EQUITY = 252
MIN_TRAIN = 500  # bars of history before the first forecast
REFIT_EVERY = 21  # re-estimate params every N bars (walk-forward)
REGIME_LOOKBACK = 365  # window for the vol percentile / regime classification
MAX_WINDOW = 1500  # rolling-window cap for the fit (None = expanding, upstream)

HONESTY_NOTE = (
    "GARCH forecasts magnitude (volatility), not direction. It tells you how "
    "violent tomorrow is likely to be — not which way it goes."
)


@dataclass
class GarchParams:
    """The four GARCH(1,1)-with-constant-mean parameters we roll the recursion
    with between refits. Student-t degrees-of-freedom is not needed downstream
    (we forecast variance, not tail quantiles) so it is intentionally dropped."""

    mu: float
    omega: float
    alpha: float
    beta: float


# ---------------------------------------------------------------- GARCH fitter
def _arch_fit(rets_window: np.ndarray) -> tuple[GarchParams, float]:
    """Default fitter: GARCH(1,1), constant mean, Student-t innovations.

    ``arch`` is imported lazily so this module (and every DB-free test that
    injects a stub fitter) imports without ``arch`` installed. Returns the
    fitted params and the conditional variance of the LAST in-sample bar — the
    seed the walk-forward recursion rolls forward from.
    """
    from arch import arch_model  # lazy: keeps the module import arch-free

    am = arch_model(rets_window, vol="GARCH", p=1, q=1, mean="Constant", dist="t")
    res = am.fit(disp="off", show_warning=False)
    p = res.params
    params = GarchParams(
        mu=float(p["mu"]),
        omega=float(p["omega"]),
        alpha=float(p["alpha[1]"]),
        beta=float(p["beta[1]"]),
    )
    last_sigma2 = float(res.conditional_volatility[-1] ** 2)
    return params, last_sigma2


FitFn = Callable[[np.ndarray], "tuple[GarchParams, float]"]


def _returns_pct(prices: pd.DataFrame) -> np.ndarray:
    """Daily % returns, scaled x100 the way ``arch`` expects (numerical range)."""
    px = prices["close"].to_numpy(dtype=float)
    return 100.0 * np.diff(px) / px[:-1]


def _fit_window_bounds(t: int, max_window: int | None) -> int:
    """Start index of the fit window ending (exclusive) at t. ``None`` ->
    expanding window (start 0), matching upstream; else a rolling cap."""
    if max_window is None:
        return 0
    return max(0, t - max_window)


# ------------------------------------------------------------- walk-forward core
def walkforward_garch(
    prices: pd.DataFrame,
    periods_per_year: int = TRADING_DAYS_CRYPTO,
    min_train: int = MIN_TRAIN,
    refit_every: int = REFIT_EVERY,
    max_window: int | None = MAX_WINDOW,
    fit_fn: FitFn | None = None,
) -> pd.DataFrame:
    """Walk-forward GARCH(1,1). For each bar t >= min_train, forecast the vol of
    bar t+1 using ONLY data available at the close of bar t.

    Params are re-estimated every ``refit_every`` bars on the trailing
    ``max_window`` returns (rolling cap; ``None`` = expanding, upstream). Between
    refits the GARCH recursion is rolled forward with the last fitted params —
    still zero lookahead, because params were estimated on strictly prior data.

    Returns a DataFrame aligned to ``prices.iloc[1:]`` with:
      ret            — % return of the bar
      fcast_vol      — 1-bar-ahead conditional vol forecast (per-bar, %)
      fcast_vol_ann  — annualized forecast vol (%)
      vol_pctile     — percentile of today's forecast vs trailing REGIME_LOOKBACK
      regime         — calm / normal / storm
    """
    fit_fn = fit_fn or _arch_fit
    rets = _returns_pct(prices)
    n = len(rets)
    if n < min_train + 10:
        raise ValueError(f"Need at least {min_train + 10} bars of prices; got {n + 1}.")

    fcast_var = np.full(n, np.nan)  # forecast of NEXT bar's variance, made at t
    params: GarchParams | None = None
    sigma2 = np.nan

    for t in range(min_train, n):
        if (t - min_train) % refit_every == 0:
            start = _fit_window_bounds(t, max_window)
            params, sigma2 = fit_fn(rets[start:t])
        assert params is not None  # first iteration always refits
        # roll the recursion one step with today's observed residual: this turns
        # the variance-at-(t-1) seed into the forecast for t+1.
        eps = rets[t] - params.mu
        sigma2 = params.omega + params.alpha * eps**2 + params.beta * sigma2
        fcast_var[t] = sigma2  # made at close of t, for bar t+1

    out = prices.iloc[1:].copy().reset_index(drop=True)
    out["ret"] = rets
    out["fcast_vol"] = np.sqrt(fcast_var)  # per-bar %
    out["fcast_vol_ann"] = out["fcast_vol"] * np.sqrt(periods_per_year)  # annualized %
    out = _add_regime(out)
    return out


def _add_regime(out: pd.DataFrame, lookback: int = REGIME_LOOKBACK) -> pd.DataFrame:
    """Attach ``vol_pctile`` (rank of today's forecast within the trailing
    window) and a calm/normal/storm ``regime`` cut. Percentile uses only the
    trailing window ending today -> no lookahead."""
    pct = (
        out["fcast_vol"]
        .rolling(lookback, min_periods=90)
        .apply(
            lambda w: (w.iloc[:-1] < w.iloc[-1]).mean() * 100 if len(w) > 1 else np.nan,
            raw=False,
        )
    )
    out["vol_pctile"] = pct
    out["regime"] = pd.cut(out["vol_pctile"], bins=[-1, 33, 67, 101], labels=["calm", "normal", "storm"])
    return out


# ---------------------------------------------------- live per-coin sizing path
class GarchSizer:
    """Stateful per-coin vol-targeting sizer for the live 538-coin path.

    Caches the fitted GARCH params per coin and refits only every
    ``refit_every`` bars, rolling the conditional-variance recursion forward
    with observed returns between refits. Feed it the append-only return history
    (``update``) each closed bar; it returns the size multiplier for the NEXT
    bar. Fed bar-by-bar it reproduces ``walkforward_garch``'s forecast series
    exactly (shared recursion), while paying at most one ``arch`` fit per
    ``refit_every`` bars instead of one per tick.

    Invariants:
      * Zero lookahead: params come from returns strictly before the forecast
        bar; ``_sigma2`` always holds the recursion state at ``_processed`` and
        the returned forecast is one step beyond it.
      * ``update`` must be called with a monotonically growing history (live:
        append-only); it replays any skipped bars once so the state stays
        identical to the dense walk-forward.
    """

    def __init__(
        self,
        target_vol_ann: float = 15.0,
        periods_per_year: int = TRADING_DAYS_CRYPTO,
        min_train: int = MIN_TRAIN,
        refit_every: int = REFIT_EVERY,
        max_window: int | None = MAX_WINDOW,
        fit_fn: FitFn | None = None,
        max_leverage: float = 2.0,
        min_size: float = 0.25,
    ) -> None:
        self.target_vol_ann = target_vol_ann
        self.periods_per_year = periods_per_year
        self.min_train = min_train
        self.refit_every = refit_every
        self.max_window = max_window
        self.fit_fn = fit_fn or _arch_fit
        self.max_leverage = max_leverage
        self.min_size = min_size

        self._params: GarchParams | None = None
        self._sigma2 = np.nan  # recursion state: variance at bar _processed
        self._processed = -1  # last return index folded into _sigma2
        self._fit_t = -1  # t of the most recent refit
        self.fit_count = 0  # how many arch fits we have paid for

    def _step(self, rets: np.ndarray, t: int) -> float:
        """Advance the recursion to forecast the variance for bar t+1, exactly
        as ``walkforward_garch`` does at loop index t. Returns that forecast."""
        if self._params is None or (t - self._fit_t) >= self.refit_every:
            start = _fit_window_bounds(t, self.max_window)
            self._params, self._sigma2 = self.fit_fn(rets[start:t])
            self._fit_t = t
            self.fit_count += 1
        p = self._params
        eps = rets[t] - p.mu
        self._sigma2 = p.omega + p.alpha * eps**2 + p.beta * self._sigma2
        self._processed = t
        return self._sigma2

    def update(self, rets: np.ndarray) -> float:
        """Fold the append-only return history up to now and return the size
        multiplier for the next bar. ``rets`` are % returns (x100, like arch),
        i.e. the ``ret`` column of ``walkforward_garch`` up to the last closed
        bar. Returns ``min_size`` until ``min_train`` bars exist."""
        rets = np.asarray(rets, dtype=float)
        n = len(rets)
        if n - 1 < self.min_train:
            return float(self.min_size)
        # replay any bars observed since the last update (usually exactly one)
        start_t = max(self._processed + 1, self.min_train)
        fcast_var = self._sigma2
        for t in range(start_t, n):
            fcast_var = self._step(rets, t)
        fcast_vol_ann = float(np.sqrt(fcast_var) * np.sqrt(self.periods_per_year))
        return self._size(fcast_vol_ann)

    def _size(self, fcast_vol_ann: float) -> float:
        if fcast_vol_ann <= 0 or np.isnan(fcast_vol_ann):
            return float(self.min_size)
        return float(np.clip(self.target_vol_ann / fcast_vol_ann, self.min_size, self.max_leverage))


# ------------------------------------------------------------------------ CLI
def main() -> None:
    from ccxt_data import fetch_ohlcv_df, load_prices_csv
    from vol_target import size_from_vol

    ap = argparse.ArgumentParser(description="Walk-forward GARCH(1,1) vol forecast.")
    ap.add_argument("--csv", help="CSV with date,close columns")
    ap.add_argument("--coin", help="ccxt symbol, e.g. BTC/USDT")
    ap.add_argument("--exchange", default="binanceusdm", help="ccxt exchange id")
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--periods-per-year", type=int, default=TRADING_DAYS_CRYPTO)
    ap.add_argument("--target-vol", type=float, default=15.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-csv", help="write the full walk-forward series to CSV")
    args = ap.parse_args()

    if args.csv:
        prices = load_prices_csv(args.csv)
    elif args.coin:
        prices = fetch_ohlcv_df(args.coin, exchange_id=args.exchange, timeframe=args.timeframe)
    else:
        sys.exit("Provide --csv or --coin")

    res = walkforward_garch(prices, periods_per_year=args.periods_per_year)
    latest = res.dropna(subset=["fcast_vol"]).iloc[-1]
    mult = size_from_vol(float(latest["fcast_vol_ann"]), args.target_vol)

    payload = {
        "asset": args.coin or args.csv,
        "as_of": str(pd.to_datetime(latest["date"]).date()),
        "forecast_vol_daily_pct": round(float(latest["fcast_vol"]), 3),
        "forecast_vol_annualized_pct": round(float(latest["fcast_vol_ann"]), 1),
        "vol_percentile_1y": round(float(latest["vol_pctile"]), 1) if pd.notna(latest["vol_pctile"]) else None,
        "regime": str(latest["regime"]),
        "target_vol_pct": args.target_vol,
        "position_size_multiplier": round(mult, 2),
        "note": HONESTY_NOTE,
    }
    if args.out_csv:
        res.to_csv(args.out_csv, index=False)
        payload["series_csv"] = args.out_csv
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"\n  {payload['asset']} — as of {payload['as_of']}")
        print(
            f"  1-bar vol forecast : {payload['forecast_vol_daily_pct']}% "
            f"({payload['forecast_vol_annualized_pct']}% annualized)"
        )
        print(f"  vol percentile (1y): {payload['vol_percentile_1y']}")
        print(f"  regime             : {payload['regime']}")
        print(f"  size vs {args.target_vol}% target: {payload['position_size_multiplier']}x")
        print(f"\n  ! {HONESTY_NOTE}\n")


if __name__ == "__main__":
    main()

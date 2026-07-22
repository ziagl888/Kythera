"""compare.py — the honest test. Same strategy, two position-sizing rules.

Runs any signal series twice:
  A) FIXED SIZE   — every trade at 1x
  B) VOL-TARGETED — every trade sized by the GARCH forecast (target_vol / forecast_vol)

Same entries, same exits. The ONLY difference is how much. Then it reports both
equity curves and the numbers side by side, per coin and aggregated, and gives a
structured verdict: does vol-targeting actually pull at Kythera?

Adapted from milesdeutscher/garchmethod (MIT) — see LICENSE.upstream. Kythera
adaptations: multi-coin aggregation + ``verdict_from_stats`` gate (T-022), and
``fit_fn`` passthrough so the timing/metric logic is testable without arch.

Timing discipline (zero lookahead), unchanged from upstream and the seam most
backtests quietly cheat:
  signal known at close of t                 -> applied to return of t+1
  vol forecast made at close of t (for t+1)  -> sizes the t+1 position

Invariants:
  * ``next_ret = ret.shift(-1)``: the position sized/decided at t earns the
    return of t+1. No same-bar fill.
  * A coin/signal pair with no valid bars yields empty stats, not a crash; a
    flat (never-trading) signal yields an all-zero return series -> stats with
    a NaN Sharpe (zero variance), which the verdict aggregation drops.

Usage:
  python compare.py --coin BTC/USDT                       # EMA 9/21 demo signal
  python compare.py --coin BTC/USDT --signals mine.csv    # your own signals.csv
  python compare.py --coins BTC/USDT,ETH/USDT,SOL/USDT    # sample + verdict
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
from garch_forecast import HONESTY_NOTE, FitFn, walkforward_garch
from vol_target import apply_sizing, size_series

# Verdict thresholds (documented heuristic gate, not a law). Vol-targeting
# "pulls" if it lifts the median Sharpe by at least SHARPE_MIN_DELTA without
# making the median max-drawdown OR worst-month materially worse than the
# respective tolerance (both are the "risk axis" of AK11).
SHARPE_MIN_DELTA = 0.10
DD_TOLERANCE = -2.0  # pp of max-drawdown we allow the median to worsen
WM_TOLERANCE = -2.0  # pp of worst-month we allow the median to worsen


# ---------------------------------------------------------------- strategies
def ema_crossover_signals(close: pd.Series, fast: int = 9, slow: int = 21) -> pd.Series:
    """EMA fast/slow crossover, long/flat. Signal known at close of bar t.
    Demo strategy only — a stand-in until real Kythera signals are fed in."""
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    return (ema_f > ema_s).astype(float)  # 1 = long, 0 = flat


def load_signals(path: str, dates: pd.Series) -> pd.Series:
    """Load a ``date, signal`` CSV and align it onto the walk-forward dates.
    Forward-fills a held position between signal dates, clips to [-1, 1]. This
    is the plug that consumes a signals.csv produced elsewhere (e.g. T-024)."""
    df = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in df.columns}
    df = df.rename(columns={cols["date"]: "date", cols["signal"]: "signal"})
    df["date"] = pd.to_datetime(df["date"])
    merged = pd.DataFrame({"date": pd.to_datetime(dates.values)}).merge(df[["date", "signal"]], on="date", how="left")
    return merged["signal"].ffill().fillna(0.0).clip(-1, 1)


# ------------------------------------------------------------------- metrics
def perf_stats(daily_ret: pd.Series, periods_per_year: int) -> dict:
    """Sharpe / CAGR / ann-vol / max-drawdown / final equity from a per-bar
    %-return series."""
    r = daily_ret.dropna() / 100.0
    if len(r) == 0:
        return {}
    equity = (1 + r).cumprod()
    yrs = len(r) / periods_per_year
    final = float(equity.iloc[-1])
    # leverage on a < -100% bar can drive equity <= 0; a fractional power of a
    # non-positive base is NaN, so report a wipeout as -100% CAGR explicitly.
    if final <= 0:
        cagr = -1.0
    else:
        cagr = final ** (1 / yrs) - 1 if yrs > 0 else np.nan
    ann_vol = r.std() * np.sqrt(periods_per_year)
    sharpe = (r.mean() * periods_per_year) / ann_vol if ann_vol > 0 else np.nan
    dd = (equity / equity.cummax() - 1).min()
    return {
        "CAGR_pct": round(100 * cagr, 1),
        "ann_vol_pct": round(100 * ann_vol, 1),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(100 * dd, 1),
        "final_equity_x": round(float(equity.iloc[-1]), 2),
    }


def worst_month(daily_ret: pd.Series, dates: pd.Series) -> float:
    """Worst calendar-month compounded return (%)."""
    r = pd.Series(daily_ret.values / 100.0, index=pd.to_datetime(dates.values))
    if len(r.dropna()) == 0:
        return float("nan")
    m = r.resample("ME").apply(lambda x: (1 + x).prod() - 1)
    return round(100 * m.min(), 1)


# ------------------------------------------------------------------ backtest
def run_comparison(
    prices: pd.DataFrame,
    signals: pd.Series | None = None,
    target_vol: float = 15.0,
    periods_per_year: int = 365,
    fit_fn: FitFn | None = None,
):
    """Run one coin fixed vs vol-targeted. Returns (stats dict, curves frame)."""
    wf = walkforward_garch(prices, periods_per_year=periods_per_year, fit_fn=fit_fn)
    close = wf["close"]

    sig = ema_crossover_signals(close) if signals is None else signals.reset_index(drop=True)

    next_ret = wf["ret"].shift(-1)  # return of bar t+1
    mult = size_series(wf["fcast_vol_ann"], target_vol)  # forecast made at t, for t+1

    strat_fixed = apply_sizing(sig, 1.0) * next_ret
    strat_volt = apply_sizing(sig, mult) * next_ret

    valid = wf["fcast_vol"].notna() & next_ret.notna()
    dates = wf.loc[valid, "date"]
    fixed = strat_fixed[valid]
    volt = strat_volt[valid]

    stats = {
        "fixed_size": {**perf_stats(fixed, periods_per_year), "worst_month_pct": worst_month(fixed, dates)},
        "vol_targeted": {**perf_stats(volt, periods_per_year), "worst_month_pct": worst_month(volt, dates)},
    }
    curves = pd.DataFrame(
        {
            "date": dates.values,
            "fixed": (1 + fixed.values / 100).cumprod(),
            "vol_targeted": (1 + volt.values / 100).cumprod(),
            "regime": wf.loc[valid, "regime"].values,
        }
    )
    return stats, curves


def compare_coins(
    prices_by_coin: dict[str, pd.DataFrame],
    signals_by_coin: dict[str, pd.Series] | None = None,
    target_vol: float = 15.0,
    periods_per_year: int = 365,
    fit_fn: FitFn | None = None,
) -> dict:
    """Run the comparison over a coin sample and attach the aggregate verdict."""
    signals_by_coin = signals_by_coin or {}
    per_coin: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    for coin, prices in prices_by_coin.items():
        try:
            stats, _ = run_comparison(
                prices,
                signals=signals_by_coin.get(coin),
                target_vol=target_vol,
                periods_per_year=periods_per_year,
                fit_fn=fit_fn,
            )
        except (ValueError, KeyError) as exc:
            # one thin-history coin (< min_train+10 bars) must not abort the
            # whole sample verdict — record it and carry on.
            skipped[coin] = str(exc)
            continue
        per_coin[coin] = stats
    return {"per_coin": per_coin, "skipped": skipped, "verdict": verdict_from_stats(per_coin)}


# -------------------------------------------------------------------- verdict
def _delta(volt: dict, fixed: dict, key: str) -> float | None:
    a, b = volt.get(key), fixed.get(key)
    # drop the pair if EITHER side is missing or NaN — a single degenerate coin
    # must not poison the plain np.median aggregation downstream.
    for v in (a, b):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
    return a - b


def verdict_from_stats(per_coin: dict[str, dict]) -> dict:
    """Aggregate the fixed-vs-vol-targeted stats into a PULLS / NO-PULL / MIXED
    gate. The reality check T-022 exists for: if vol-targeting does not lift
    Kythera's risk-adjusted return, T-021 gets shelved.

    Rule:
      * PULLS   — median Sharpe delta >= SHARPE_MIN_DELTA AND the risk axis holds
                  (median max-DD and median worst-month each worsen by no more
                  than their tolerance).
      * NO-PULL — median Sharpe delta <= 0.
      * MIXED   — anything between (Sharpe helps but drawdown/worst-month pays).
    """
    sharpe_deltas, dd_deltas, wm_deltas = [], [], []
    for stats in per_coin.values():
        volt, fixed = stats.get("vol_targeted", {}), stats.get("fixed_size", {})
        sd = _delta(volt, fixed, "sharpe")
        if sd is not None:
            sharpe_deltas.append(sd)
        dd = _delta(volt, fixed, "max_drawdown_pct")
        if dd is not None:
            dd_deltas.append(dd)
        wm = _delta(volt, fixed, "worst_month_pct")
        if wm is not None:
            wm_deltas.append(wm)

    if not sharpe_deltas:
        return {"verdict": "NO-DATA", "reason": "no coin produced comparable stats", "n_coins": len(per_coin)}

    med_sharpe = float(np.median(sharpe_deltas))
    med_dd = float(np.median(dd_deltas)) if dd_deltas else float("nan")
    med_wm = float(np.median(wm_deltas)) if wm_deltas else float("nan")
    dd_ok = np.isnan(med_dd) or med_dd >= DD_TOLERANCE
    wm_ok = np.isnan(med_wm) or med_wm >= WM_TOLERANCE

    if med_sharpe >= SHARPE_MIN_DELTA and dd_ok and wm_ok:
        verdict = "PULLS"
    elif med_sharpe <= 0:
        verdict = "NO-PULL"
    else:
        verdict = "MIXED"

    return {
        "verdict": verdict,
        "n_coins": len(per_coin),
        "median_sharpe_delta": round(med_sharpe, 3),
        "median_max_dd_delta_pct": round(med_dd, 2) if not np.isnan(med_dd) else None,
        "median_worst_month_delta_pct": round(med_wm, 2) if not np.isnan(med_wm) else None,
        "thresholds": {
            "sharpe_min_delta": SHARPE_MIN_DELTA,
            "dd_tolerance_pct": DD_TOLERANCE,
            "wm_tolerance_pct": WM_TOLERANCE,
        },
        "reason": (
            f"median Sharpe {'+' if med_sharpe >= 0 else ''}{med_sharpe:.2f} across "
            f"{len(sharpe_deltas)} coins; median max-DD change "
            f"{med_dd:+.1f}pp"
            if not np.isnan(med_dd)
            else f"median Sharpe {med_sharpe:+.2f}"
        ),
    }


def plot_curves(curves: pd.DataFrame, out_path: str, title: str) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 6.5), facecolor="white")
    d = pd.to_datetime(curves["date"])
    ax.plot(d, curves["fixed"], lw=1.6, color="#888888", label="Fixed size (1x every trade)")
    ax.plot(d, curves["vol_targeted"], lw=1.8, color="#0a7d38", label="Vol-targeted (GARCH sized)")
    storm = (curves["regime"] == "storm").to_numpy()
    ax.fill_between(
        d, 0, 1, where=storm, transform=ax.get_xaxis_transform(), color="#d62728", alpha=0.07, label="Storm regime"
    )
    ax.set_yscale("log")
    ax.set_ylabel("Growth of $1 (log scale)")
    ax.set_title(title, fontsize=13, fontweight="bold", loc="left")
    ax.legend(frameon=False, loc="upper left")
    ax.grid(alpha=0.25, lw=0.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    return out_path


# ------------------------------------------------------------------------ CLI
def _print_pair(coin: str, stats: dict) -> None:
    print(f"\n  {coin} — fixed vs vol-targeted")
    print(f"  {'':22}{'FIXED':>10}{'VOL-TARGETED':>15}")
    rows = [
        ("CAGR %", "CAGR_pct"),
        ("Ann vol %", "ann_vol_pct"),
        ("Sharpe", "sharpe"),
        ("Max drawdown %", "max_drawdown_pct"),
        ("Worst month %", "worst_month_pct"),
        ("Final equity (x)", "final_equity_x"),
    ]
    for label, key in rows:
        f = stats["fixed_size"].get(key, "—")
        v = stats["vol_targeted"].get(key, "—")
        print(f"  {label:22}{str(f):>10}{str(v):>15}")


def main() -> None:
    from ccxt_data import fetch_ohlcv_df, load_prices_csv

    ap = argparse.ArgumentParser(description="Fixed vs vol-targeted validation harness.")
    ap.add_argument("--csv", help="single coin: CSV with date,close")
    ap.add_argument("--coin", help="single coin: ccxt symbol e.g. BTC/USDT")
    ap.add_argument("--coins", help="comma list of ccxt symbols for the sample verdict")
    ap.add_argument("--exchange", default="binanceusdm")
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--signals", help="date,signal CSV (single-coin path)")
    ap.add_argument("--target-vol", type=float, default=15.0)
    ap.add_argument("--periods-per-year", type=int, default=365)
    ap.add_argument("--chart", help="write an equity chart PNG (single-coin path)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.coins:
        symbols = [s.strip() for s in args.coins.split(",") if s.strip()]
        prices_by_coin = {s: fetch_ohlcv_df(s, exchange_id=args.exchange, timeframe=args.timeframe) for s in symbols}
        result = compare_coins(prices_by_coin, target_vol=args.target_vol, periods_per_year=args.periods_per_year)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            for coin, stats in result["per_coin"].items():
                _print_pair(coin, stats)
            print(f"\n  VERDICT: {result['verdict']['verdict']} — {result['verdict']['reason']}")
            print(f"  ! {HONESTY_NOTE}\n")
        return

    if args.csv:
        prices = load_prices_csv(args.csv)
        label = args.csv
    elif args.coin:
        prices = fetch_ohlcv_df(args.coin, exchange_id=args.exchange, timeframe=args.timeframe)
        label = args.coin
    else:
        raise SystemExit("Provide --coin, --csv, or --coins")

    sig = None
    strategy_name = "EMA 9/21 crossover (demo)"
    if args.signals:
        wf_dates = prices["date"].iloc[1:].reset_index(drop=True)
        sig = load_signals(args.signals, wf_dates)
        strategy_name = f"custom signals ({args.signals})"

    stats, curves = run_comparison(
        prices, signals=sig, target_vol=args.target_vol, periods_per_year=args.periods_per_year
    )
    chart = plot_curves(curves, args.chart, f"{label}: {strategy_name}") if args.chart else None
    payload = {
        "asset": label,
        "strategy": strategy_name,
        "target_vol_pct": args.target_vol,
        "results": stats,
        "chart": chart,
        "note": HONESTY_NOTE,
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_pair(label, stats)
        if chart:
            print(f"\n  chart: {chart}")
        print(f"  ! {HONESTY_NOTE}\n")


if __name__ == "__main__":
    main()

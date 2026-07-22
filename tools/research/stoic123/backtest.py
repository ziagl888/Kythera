"""backtest.py — Phase-3 multi-timeframe backtest for the Stoic 1-2-3 system.

Pipeline per coin:
  1. fetch LTF (signal) + HTF (location) OHLCV via ccxt,
  2. chronological OOS split (fit params on in-sample, judge on out-of-sample),
  3. parameter-sensitivity sweep on in-sample -> pick the best combo by Sharpe,
  4. evaluate that combo out-of-sample: Sharpe / Max-DD / Winrate / Trade-count /
     Worst-Month,
  5. optional direct-anschluss: signals.csv through the GARCH harness
     (fixed vs vol-targeted),
  6. an explicit Edge / no-Edge verdict.

The metrics are inline (kept independent of the GARCH package); only the optional
``--with-garch`` step imports `tools/research/garch/compare`. ``ccxt`` is imported
lazily — the pure functions here are DB-free and unit-tested.

Timing discipline: a position decided at close of bar t earns the return of bar
t+1 (``next_ret = ret.shift(-1)``) — no same-bar fill, matching the GARCH harness.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from dataclasses import replace

import numpy as np
import pandas as pd
from params import StoicParams
from signals import signals_dataframe
from state_machine import generate_signals

# Verdict thresholds (documented heuristic, not a law).
EDGE_MIN_SHARPE = 0.30
MIN_TRADES = 10  # below this the sample is too thin to judge


# --------------------------------------------------------------- metrics
def bar_returns_pct(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change().fillna(0.0) * 100.0


def strategy_bar_returns(positions: np.ndarray, bar_ret: pd.Series) -> np.ndarray:
    """Position at t earns the return of t+1 (no same-bar fill)."""
    next_ret = bar_ret.to_numpy()[1:]
    pos = np.asarray(positions, float)[:-1]
    return pos * next_ret


def perf_metrics(strat_ret_pct: np.ndarray, dates: pd.Series, periods_per_year: int) -> dict:
    r = pd.Series(strat_ret_pct).dropna() / 100.0
    if len(r) == 0:
        return {
            "sharpe": float("nan"),
            "max_drawdown_pct": float("nan"),
            "cagr_pct": float("nan"),
            "worst_month_pct": float("nan"),
        }
    equity = (1 + r).cumprod()
    ann_vol = r.std() * np.sqrt(periods_per_year)
    sharpe = (r.mean() * periods_per_year) / ann_vol if ann_vol > 0 else float("nan")
    dd = float((equity / equity.cummax() - 1).min())
    yrs = len(r) / periods_per_year
    final = float(equity.iloc[-1])
    cagr = (final ** (1 / yrs) - 1) if (yrs > 0 and final > 0) else (-1.0 if final <= 0 else float("nan"))
    idx = pd.to_datetime(pd.Series(dates).to_numpy()[1 : 1 + len(r)])
    monthly = pd.Series(r.to_numpy(), index=idx).resample("ME").apply(lambda x: (1 + x).prod() - 1)
    return {
        "sharpe": round(float(sharpe), 2),
        "max_drawdown_pct": round(100 * dd, 1),
        "cagr_pct": round(100 * cagr, 1),
        "worst_month_pct": round(100 * float(monthly.min()), 1) if len(monthly) else float("nan"),
    }


def trade_stats(positions: np.ndarray, bar_ret: pd.Series) -> dict:
    """Winrate + trade count from maximal runs of constant non-zero position.
    A trade's return compounds the strategy bar-returns over the run."""
    pos = np.asarray(positions, int)
    next_ret = bar_ret.to_numpy()[1:] / 100.0
    p = pos[:-1]
    trades = []
    i = 0
    n = len(p)
    while i < n:
        if p[i] == 0:
            i += 1
            continue
        j = i
        while j < n and p[j] == p[i]:
            j += 1
        seg = p[i] * next_ret[i:j]
        trades.append(float(np.prod(1 + seg) - 1))
        i = j
    if not trades:
        return {"n_trades": 0, "winrate_pct": float("nan"), "avg_trade_pct": float("nan")}
    wins = sum(1 for t in trades if t > 0)
    return {
        "n_trades": len(trades),
        "winrate_pct": round(100 * wins / len(trades), 1),
        "avg_trade_pct": round(100 * float(np.mean(trades)), 2),
    }


def run_backtest(df: pd.DataFrame, htf: pd.DataFrame, p: StoicParams, periods_per_year: int) -> dict:
    positions = generate_signals(df, htf, p).to_numpy()
    bar_ret = bar_returns_pct(df)
    strat = strategy_bar_returns(positions, bar_ret)
    m = perf_metrics(strat, df["date"], periods_per_year)
    m.update(trade_stats(positions, bar_ret))
    m["exposure_pct"] = round(100 * float(np.mean(positions != 0)), 1)
    return m


# ---------------------------------------------------------- OOS + sweep
def oos_split(df: pd.DataFrame, is_frac: float = 0.6) -> tuple[pd.DataFrame, pd.DataFrame]:
    cut = int(len(df) * is_frac)
    return df.iloc[:cut].reset_index(drop=True), df.iloc[cut:].reset_index(drop=True)


def default_grid() -> list[dict]:
    """Documented sensitivity grid over the parameters the article leaves fuzzy.
    Spans strict (rare, clean) to loose (frequent, noisy) so the sweep exposes
    the rarity/edge trade-off — the strict 1-2-3 fires only a handful of times,
    the looser end trades often. MA type is fixed to EMA to bound the grid."""
    axes = {
        "break_k_atr": [0.1, 0.25, 0.5],
        "base_window": [3, 5],
        "base_max_range_atr": [1.5, 3.0],
        "retest_touch": [True, False],
    }
    keys = list(axes)
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*axes.values())]


def sensitivity_sweep(
    df_is: pd.DataFrame, htf: pd.DataFrame, base: StoicParams, grid: list[dict], ppy: int
) -> list[dict]:
    rows = []
    for override in grid:
        p = replace(base, **override)
        try:
            m = run_backtest(df_is, htf, p, ppy)
        except ValueError:
            continue
        rows.append({"params": override, **m})
    return rows


def pick_best(sweep: list[dict]) -> dict | None:
    """Best in-sample combo by Sharpe among those with enough trades."""
    eligible = [r for r in sweep if r.get("n_trades", 0) >= MIN_TRADES and np.isfinite(r.get("sharpe", np.nan))]
    if not eligible:
        return None
    return max(eligible, key=lambda r: r["sharpe"])


def verdict(oos: dict) -> dict:
    n = oos.get("n_trades", 0)
    sharpe = oos.get("sharpe", float("nan"))
    if n < MIN_TRADES or not np.isfinite(sharpe):
        return {"verdict": "INSUFFICIENT", "reason": f"only {n} OOS trades (< {MIN_TRADES})"}
    if sharpe >= EDGE_MIN_SHARPE and oos.get("avg_trade_pct", 0) > 0:
        return {"verdict": "EDGE", "reason": f"OOS Sharpe {sharpe} >= {EDGE_MIN_SHARPE}, positive expectancy"}
    return {"verdict": "NO-EDGE", "reason": f"OOS Sharpe {sharpe} < {EDGE_MIN_SHARPE} or non-positive expectancy"}


def backtest_coin(df: pd.DataFrame, htf: pd.DataFrame, base: StoicParams, ppy: int, is_frac: float = 0.6) -> dict:
    df_is, df_oos = oos_split(df, is_frac)
    sweep = sensitivity_sweep(df_is, htf, base, default_grid(), ppy)
    best = pick_best(sweep)
    if best is None:
        return {
            "verdict": {"verdict": "INSUFFICIENT", "reason": "no in-sample combo hit MIN_TRADES"},
            "sensitivity": sweep,
            "in_sample_best": None,
            "out_of_sample": None,
            "chosen_params": None,
        }
    chosen = replace(base, **best["params"])
    oos = run_backtest(df_oos, htf, chosen, ppy)
    return {
        "chosen_params": best["params"],
        "in_sample_best": {k: best[k] for k in best if k != "params"},
        "out_of_sample": oos,
        "sensitivity": sweep,
        "verdict": verdict(oos),
    }


# ------------------------------------------------------------- ccxt fetch
def fetch_ohlcv_full(symbol: str, exchange_id: str, timeframe: str, max_bars: int = 4000) -> pd.DataFrame:
    try:
        import ccxt
    except ImportError:  # pragma: no cover
        sys.exit("ccxt not installed. pip install -r tools/research/garch/requirements-garch.txt")
    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    # page FORWARD from ~max_bars ago so we collect a long trailing history
    # (since=None only ever returns the most recent single page).
    since = ex.milliseconds() - max_bars * tf_ms
    page = 1000  # binanceusdm caps klines at 1000 per request
    rows: list[list] = []
    while len(rows) < max_bars:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=page)
        if not batch:
            break
        rows += batch
        nxt = batch[-1][0] + tf_ms
        if nxt <= since:
            break
        since = nxt
        if len(batch) < page:
            break  # short page => reached the head of the series
        time.sleep(ex.rateLimit / 1000.0)
    if not rows:
        sys.exit(f"no OHLCV for {symbol} {timeframe}")
    df = pd.DataFrame(rows[-max_bars:], columns=["ts", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    return (
        df.drop_duplicates("date").sort_values("date").reset_index(drop=True)[["date", "open", "high", "low", "close"]]
    )


# ------------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser(description="Stoic 1-2-3 multi-timeframe backtest.")
    ap.add_argument("--coins", required=True, help="comma list of ccxt symbols")
    ap.add_argument("--exchange", default="binanceusdm")
    ap.add_argument("--ltf", default="4h", help="signal timeframe")
    ap.add_argument("--htf", default="1d", help="location timeframe")
    ap.add_argument("--is-frac", type=float, default=0.6, help="in-sample fraction for the OOS split")
    ap.add_argument("--ppy", type=int, default=None, help="periods/year (default inferred from LTF)")
    ap.add_argument("--with-garch", action="store_true", help="also run signals through the GARCH harness")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ppy = args.ppy or {"1d": 365, "4h": 365 * 6, "1h": 365 * 24}.get(args.ltf, 365)
    base = StoicParams()
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]

    report: dict = {
        "config": {
            "ltf": args.ltf,
            "htf": args.htf,
            "is_frac": args.is_frac,
            "ppy": ppy,
            "min_trades": MIN_TRADES,
            "edge_min_sharpe": EDGE_MIN_SHARPE,
        },
        "coins": {},
        "skipped": {},
    }
    for coin in coins:
        try:
            df = fetch_ohlcv_full(coin, args.exchange, args.ltf)
            htf = fetch_ohlcv_full(coin, args.exchange, args.htf)
            res = backtest_coin(df, htf, base, ppy, args.is_frac)
            if args.with_garch and res["chosen_params"] is not None:
                res["garch_direct_anschluss"] = _garch_compare(df, htf, replace(base, **res["chosen_params"]))
            report["coins"][coin] = res
        except (ValueError, KeyError) as exc:
            report["skipped"][coin] = str(exc)

    verds = [c["verdict"]["verdict"] for c in report["coins"].values()]
    report["aggregate_verdict"] = _aggregate(verds)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)


def _garch_compare(df: pd.DataFrame, htf: pd.DataFrame, p: StoicParams) -> dict:
    import os

    _g = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "garch")
    sys.path.insert(0, _g)
    import compare as gc  # noqa: PLC0415

    sig_df = signals_dataframe(df, htf, p)
    wf_dates = df["date"].iloc[1:].reset_index(drop=True)
    merged = pd.DataFrame({"date": pd.to_datetime(wf_dates)}).merge(sig_df, on="date", how="left")
    sig = merged["signal"].ffill().fillna(0.0).clip(-1, 1)
    stats, _ = gc.run_comparison(df, signals=sig)
    return stats


def _aggregate(verds: list[str]) -> str:
    if not verds:
        return "NO-DATA"
    if all(v == "INSUFFICIENT" for v in verds):
        return "INSUFFICIENT"
    edge = verds.count("EDGE")
    judged = [v for v in verds if v != "INSUFFICIENT"]
    if edge > len(judged) / 2:
        return "EDGE"
    if edge == 0:
        return "NO-EDGE"
    return "MIXED"


def _print_report(report: dict) -> None:
    for coin, res in report["coins"].items():
        v = res["verdict"]
        oos = res.get("out_of_sample") or {}
        print(f"\n  {coin}: {v['verdict']} — {v['reason']}")
        if oos:
            print(
                f"    OOS: Sharpe {oos.get('sharpe')}  MaxDD {oos.get('max_drawdown_pct')}%  "
                f"WR {oos.get('winrate_pct')}%  trades {oos.get('n_trades')}  "
                f"worst-month {oos.get('worst_month_pct')}%"
            )
            print(f"    chosen params: {res['chosen_params']}")
    if report["skipped"]:
        print(f"\n  skipped: {report['skipped']}")
    print(f"\n  AGGREGATE VERDICT: {report['aggregate_verdict']}\n")


if __name__ == "__main__":
    main()

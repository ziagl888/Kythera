"""DB-free tests for the Stoic 1-2-3 Phase-3 backtest logic (backtest.py).

Trade segmentation, next-bar timing, the OOS split, and the Edge/no-Edge verdict
— all pure (no ccxt). The CLI run over real coins is the AK5 integration proof;
these lock the arithmetic. Runnable standalone or via pytest.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "research", "stoic123")
sys.path.insert(0, _DIR)

from params import StoicParams  # noqa: E402

import backtest as bt  # noqa: E402


def _df(closes):
    closes = np.asarray(closes, float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {
            "date": pd.date_range("2022-01-01", periods=len(closes), freq="h"),
            "open": opens,
            "high": np.maximum(opens, closes) + 0.1,
            "low": np.minimum(opens, closes) - 0.1,
            "close": closes,
        }
    )


def make_trending(n=650, seed=7):
    rng = np.random.default_rng(seed)
    drift = np.concatenate([np.full(n // 2, 0.0015), np.full(n - n // 2, -0.0015)])
    close = 100.0 * np.exp(np.cumsum(rng.normal(drift, 0.015)))
    return _df(close)


def make_htf(n=90, slope=1.0):
    return pd.DataFrame(
        {"date": pd.date_range("2020-12-01", periods=n, freq="D"), "close": 100.0 + np.cumsum(np.full(n, slope))}
    )


# ---------------------------------------------------------- timing + trades
def test_strategy_bar_returns_uses_next_bar():
    df = _df([100, 101, 103, 102])  # bar returns ~ +1%, +1.98%, -0.97%
    bar_ret = bt.bar_returns_pct(df)
    positions = np.array([1, 1, 0, 0])  # long for the first two decisions
    strat = bt.strategy_bar_returns(positions, bar_ret)
    # pos[0]*ret[1] and pos[1]*ret[2]; pos[2..] earn nothing
    assert np.isclose(strat[0], bar_ret.to_numpy()[1])
    assert np.isclose(strat[1], bar_ret.to_numpy()[2])
    assert strat[2] == 0.0


def test_trade_stats_counts_runs_and_winrate():
    df = _df([100, 102, 101, 100, 101, 103])
    bar_ret = bt.bar_returns_pct(df)
    # one long run then one short run
    positions = np.array([1, 1, 0, -1, -1, 0])
    ts = bt.trade_stats(positions, bar_ret)
    assert ts["n_trades"] == 2
    assert 0.0 <= ts["winrate_pct"] <= 100.0


def test_trade_stats_no_trades():
    df = _df([100, 101, 102])
    ts = bt.trade_stats(np.array([0, 0, 0]), bt.bar_returns_pct(df))
    assert ts["n_trades"] == 0


# -------------------------------------------------------------- OOS + verdict
def test_oos_split_sizes():
    df = _df(list(range(100)))
    is_df, oos_df = bt.oos_split(df, 0.6)
    assert len(is_df) == 60 and len(oos_df) == 40


def test_verdict_paths():
    assert bt.verdict({"n_trades": 3, "sharpe": 2.0})["verdict"] == "INSUFFICIENT"
    assert bt.verdict({"n_trades": 20, "sharpe": 1.0, "avg_trade_pct": 0.5})["verdict"] == "EDGE"
    assert bt.verdict({"n_trades": 20, "sharpe": 0.1, "avg_trade_pct": 0.5})["verdict"] == "NO-EDGE"
    assert bt.verdict({"n_trades": 20, "sharpe": 1.0, "avg_trade_pct": -0.5})["verdict"] == "NO-EDGE"


def test_perf_metrics_shape_on_known_series():
    strat = np.array([1.0, -2.0, 1.0, 1.0] * 20)
    dates = pd.Series(pd.date_range("2022-01-01", periods=len(strat) + 1, freq="D"))
    m = bt.perf_metrics(strat, dates, periods_per_year=365)
    for k in ("sharpe", "max_drawdown_pct", "cagr_pct", "worst_month_pct"):
        assert k in m


# ------------------------------------------------------- end-to-end (no ccxt)
def test_backtest_coin_runs_and_verdicts():
    df, htf = make_trending(), make_htf()
    res = bt.backtest_coin(df, htf, StoicParams(), ppy=365 * 6, is_frac=0.6)
    assert res["verdict"]["verdict"] in {"EDGE", "NO-EDGE", "INSUFFICIENT"}
    assert "sensitivity" in res and len(res["sensitivity"]) >= 1


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

# GARCH vol-targeting — Kythera research package

Walk-forward GARCH(1,1) volatility forecasting + vol-targeting position sizing,
plus a fixed-vs-vol-targeted validation harness. Ported from
[`milesdeutscher/garchmethod`](https://github.com/milesdeutscher/garchmethod)
(MIT — see `LICENSE.upstream`), audit verdict **ADAPT**
(`docs`/`T-2026-KYT-9050-021`, `-022`).

> **GARCH forecasts *magnitude*, not direction.** It answers *how much*, never
> *which way*. Compose it with a Kythera direction engine as
> `order_size = signal x size_multiplier` (`apply_sizing`). It is a
> **per-position throttle**, not a portfolio risk model (correlation layer =
> `T-2026-KYT-9050-023`, backlog).

## Install (self-contained — not in the fleet lockfile)

```
python -m pip install -r tools/research/garch/requirements-garch.txt
```

`arch` and `ccxt` are imported lazily; the fleet `requirements.txt` stays clean.
The DB-free tests (`backtest/test_garch_*.py`) run under plain fleet Python with
neither installed.

## Library use

```python
from tools.research.garch import (
    walkforward_garch, size_series, apply_sizing, GarchSizer, fetch_ohlcv_df,
)

prices = fetch_ohlcv_df("BTC/USDT")            # date,close DataFrame (ccxt)
wf = walkforward_garch(prices)                 # + fcast_vol_ann, regime, ...
size = size_series(wf["fcast_vol_ann"], target_vol_ann=15.0)   # [0.25, 2.0]
order = apply_sizing(direction_signal, size)   # signal x size_multiplier
```

**Live 538-coin path** — `GarchSizer` caches fitted params per coin and refits
only every `refit_every` bars (fed the append-only return history each closed
bar, it reproduces `walkforward_garch`'s forecast series exactly):

```python
sizer = GarchSizer(target_vol_ann=15.0)        # one instance per coin
mult = sizer.update(rets_pct_history)          # size multiplier for the next bar
```

## CLI

```
python tools/research/garch/garch_forecast.py --coin BTC/USDT
python tools/research/garch/compare.py --coin BTC/USDT --signals mine.csv
python tools/research/garch/compare.py --coins BTC/USDT,ETH/USDT,SOL/USDT   # verdict
```

`compare.py --signals` consumes a `date,signal` CSV (`signal in {-1,0,1}`) — the
plug for a strategy's `signals.csv` (e.g. the Stoic 1-2-3 module,
`T-2026-KYT-9050-024`).

## Kythera adaptations vs upstream

| Upstream | Here | Why |
|---|---|---|
| yfinance | ccxt OHLCV (`ccxt_data.py`) | crypto perps, ~538 coins |
| expanding window | rolling `max_window` cap (default 1500; `None` = upstream) | bound CPU/mem across a 538-coin refit loop |
| flat CLI scripts | flat scripts **+** importable package | fleet imports the sizer |
| hardcoded `arch` fit | injectable `fit_fn` | DB-free tests without `arch` |
| single-run only | `GarchSizer` param cache + scheduled refit | live per-coin path |
| single-coin compare | `compare_coins` + `verdict_from_stats` gate | T-022 reality check |

## Verdict gate (T-022)

`compare.py --coins ...` runs a coin sample fixed vs vol-targeted and prints a
`PULLS / MIXED / NO-PULL` verdict on the median Sharpe delta + max-drawdown
change. **The real Kythera-signal verdict is DB-bound** (hard rule 1) and runs
in a VPS session with actual signals; on the build machine the harness is
validated on ccxt prices + demo/proxy signals.

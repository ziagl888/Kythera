# Kythera

**A multi-bot crypto trading & market-intelligence system for Binance Futures.**

Named after the [Antikythera mechanism](https://en.wikipedia.org/wiki/Antikythera_mechanism) — the ancient device that coupled dozens of interlocking gears to turn observation into prediction. Kythera does the same with markets: a fleet of ~27 specialized bots, each an expert in one thing, coordinated into a single stream of trading signals delivered to Telegram (and executed via [Cornix](https://cornix.io/)).

> ⚠️ **Risk warning.** This software places and manages leveraged trades and emits trading signals. Crypto trading carries substantial risk of loss. Nothing here is financial advice. Run it against paper/testnet first. You are responsible for any capital you put behind it.

---

## How it works

A single supervisor process, `main_watchdog.py`, launches and monitors the whole fleet. The bots don't call each other directly — they communicate through a **shared PostgreSQL database** and a **Telegram outbox** table. This keeps every bot independent and individually restartable.

```
Binance (WS + REST)
      │
      ▼
1  Data Ingestion ──────────►  PostgreSQL  ◄────────── candles, indicators,
      │                          ▲   │                  signals, trades, outbox
      ▼                          │   │
2  Indicator Engine  ───────────►│   │
      │                          │   ▼
3/7/9…29  Strategy & AI bots ───►│  28 Signal Orchestrator
      │  (read indicators,       │        │ (coordinates the fleet, de-dupes,
      │   emit signals)          │        │  routes to ONE Cornix channel)
      ▼                          │        ▼
10/19/20/23/26  Intelligence ───►│   telegram_outbox
      │  (pump/dump, whales,     │        │
      │   funding, regime)       │        ▼
                                 └── 4 Telegram Bot ──► Telegram channels ──► Cornix ──► Binance
                                            ▲
5  Trade Monitor / 8 AI Trade Monitor ──────┘  (SL/TP tracking, trailing, lifecycle)
```

**Only the Signal Orchestrator's trading channel is wired to Cornix** — the individual bot channels are informational, so trades fire exactly once.

## The fleet

Grouped by role. Names match the process names in `main_watchdog.py`.

### Data & infrastructure
| Script | Process | Role |
|---|---|---|
| `main_watchdog.py` | Watchdog | Supervisor — launches, monitors and restarts the fleet |
| `1_data_ingestion.py` | Data Ingestion | Binance WebSocket + REST → candles in PostgreSQL |
| `chart_data_service.py` | Chart Data Service | Live 10s chart-data WebSocket service |
| `2_indicator_engine.py` | Indicator Engine | ~120 technical indicators computed from candles |
| `4_telegram_bot.py` | Telegram Bot | Consumes the outbox → posts to Telegram channels |
| `6_housekeeping.py` | Housekeeping | Nightly cleanup (03:00 UTC), optional leverage refresh |
| `dashboard.py` | Dashboard | Flask web UI + live process monitor |

### Strategy & AI signal bots
| Script | Process | Role |
|---|---|---|
| `3_detectors.py` | Detectors | Classic strategies (Fast-In-Out, S/R, Volume, 5-Percent) |
| `7_pattern_detector.py` | Pattern Detector | Chart-pattern / trendline breakouts |
| `9_ai_sr_bot.py` | AI SR Bot | ML support/resistance |
| `11_ai_mis_bot.py` | AI MIS1 Detector | Multi-horizon (8/24/72/168h) ML ensemble |
| `12_ai_ats_bot.py` | AI ATS1 Detector | ATS1 ML strategy |
| `13_ai_rub_bot.py` | AI RUB1 Detector | Rubberband mean-reversion (ML) |
| `14_ai_atb_bot.py` | AI ATB1 Detector | Break-and-retest / target ML |
| `15_ai_master_bot.py` | AI AIM1 Detector | Meta-ensemble "master" model |
| `16_smc_forex_metals_bot.py` | SMC FOREX Detector | Smart-Money-Concepts on forex & metals |
| `17_mayank_bot.py` | Mayank Bot | "Mayank" discretionary-style strategy |
| `18_ai_abr1_bot.py` | AI ABR1 Detector | ABR1 ML strategy |
| `21_btc_smc_strategy.py` | BTC SMC Bot | Bitcoin Smart-Money-Concepts |
| `24_quasimodo_bot.py` | Quasimodo Bot | Quasimodo (over-and-under) reversal pattern |
| `25_smc_ml_sniper.py` | TD & BB Bot | SMC ML sniper (trend-detection + break-and-retest) |
| `29_ufi1_bot.py` | UFI1 Fib Bot | Rule-based Fibonacci-inversion SHORT (1D, ≥60% swings) |
| `22_ip_pattern_bot.py` | IP Pattern Bot | Inverse-pattern bot — **currently disabled** in the watchdog |

### Market intelligence
| Script | Process | Role |
|---|---|---|
| `10_pump_dump_detector.py` | Pump Dump Detector | Real-time pump/dump detection |
| `19_whale_logger_bot.py` | Whale Logger | Whale-trade monitor (aggTrade WebSocket) |
| `20_funding_logger_bot.py` | Funding Logger | Funding-rate extremes monitor |
| `23_market_tracker.py` | Market Tracker | Market overview + per-bot performance tables |
| `26_regime_detector.py` | Regime Detector | Market-regime classification |
| `27_bot_regime_analyzer.py` | Bot Regime Analyzer | Per-bot performance by regime |
| `28_signal_orchestrator.py` | Signal Orchestrator | Coordinates the fleet, routes to the single Cornix channel |

### Execution & tracking
| Script | Process | Role |
|---|---|---|
| `5_trade_monitor.py` | Trade Monitor | SL/TP tracking, trailing stops, closes |
| `8_ai_trade_monitor.py` | AI Trade Monitor | Lifecycle tracking for AI signals |

`main_telegram_bot.py` is a separate interactive Telegram command bot (queries, manual controls) and is run on its own, not by the watchdog.

## Repository layout

```
core/                 Shared infrastructure (imported by every bot)
  config.py             Central config — all secrets/channels from .env
  database.py           Pooled PostgreSQL access
  charting.py           mplfinance chart rendering
  market_utils.py       Coin lists, Telegram send helpers
  trade_utils.py        Target/SL math
  regime_logic.py       Regime classification helpers
  state_utils.py        Per-bot state persistence
  bot_naming.py         Canonical bot-name normalisation
  logging_setup.py      Shared logging config
1_… 29_*.py           The numbered fleet (see above)
main_watchdog.py      Fleet supervisor (entry point)
main_telegram_bot.py  Interactive command bot
dashboard.py          Flask web UI
*_backtest.py,        Offline backtesting & ML training
*_trainer.py          (qm_*, smc_*, fib_backtest, check_*)
```

## Requirements

- **Python 3.10+**
- **PostgreSQL 14+**
- A **Telegram bot token** and one or more channels
- *(optional)* Binance API keys — only for the housekeeping leverage refresh

Python dependencies are in `requirements.txt` (pandas/numpy, psycopg2, websockets, scikit-learn/xgboost/scipy, mplfinance/matplotlib, python-telegram-bot, flask).

## Setup

```bash
git clone https://github.com/ziagl888/Kythera.git
cd Kythera
python -m venv .venv && . .venv/Scripts/activate    # Windows
#                        source .venv/bin/activate   # Linux/macOS
pip install -r requirements.txt

cp .env.example .env        # then fill in secrets + channel ids
python main_watchdog.py     # starts and supervises the whole fleet
```

See [`SETUP_ANLEITUNG.md`](SETUP_ANLEITUNG.md) for the detailed, step-by-step walkthrough (German).

## Configuration

All secrets and routing live in `.env` (git-ignored) — **never in code**:

- **Secrets:** `TELEGRAM_BOT_TOKEN`, database credentials (`DB_*`), Binance API keys (optional).
- **Channel routing:** every posting target is a `CH_*` variable (e.g. `CH_MARKET_DATA`, `CH_BTC_SMC`), read through `core/config.py`. `0` disables a target. `.env.example` lists them all.

Required variables (`DB_PASSWORD`, `TELEGRAM_BOT_TOKEN`) fail fast at startup if missing. Hardcoded channel ids and tokens are rejected by a `gitleaks` pre-commit hook (see below).

## Development

```bash
pip install pre-commit
pre-commit install          # ruff lint + format, secret/PII scanning on every commit
pre-commit run --all-files  # run against the whole tree
```

- **Linting/formatting:** [ruff](https://docs.astral.sh/ruff/) — config in `pyproject.toml`, pinned in `.pre-commit-config.yaml` to match the dev/CI version. Legacy analysis/backtest scripts are held to a lighter bar via `extend-exclude`.
- **Secret & PII scanning:** [gitleaks](https://github.com/gitleaks/gitleaks) with custom rules for Telegram tokens and private channel ids (`-100…`), see `.gitleaks.toml`.
- **CI:** GitHub Actions (`.github/workflows/syntax-check.yml`) byte-compiles the tree on every push.

Keep secrets in `.env`, keep PRs small and cohesive, and let the hooks run.

## Operations

`main_watchdog.py` owns the process lifecycle: it staggers startup (each bot has a `start_delay` to avoid a thundering-herd of connections), restarts crashed bots, and periodically recycles the heavier engines (`restart_interval`). The Flask dashboard shows which processes are up. Bots that lose their WebSocket reconnect on their own with exponential backoff.

## Backtesting & training

Offline tools live alongside the fleet and read the same database:
`qm_backtest.py` / `qm_ml_trainer.py` (Quasimodo), `smc_pattern_backtester.py` / `smc_ml_trainer.py` (SMC), `fib_backtest.py` (UFI1), plus `check_funding.py` / `check_whales.py` diagnostics.

## License

See [LICENSE](LICENSE).

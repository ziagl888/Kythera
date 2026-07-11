# Kythera

**A multi-bot crypto trading & market-intelligence system for Binance Futures.**

Named after the [Antikythera mechanism](https://en.wikipedia.org/wiki/Antikythera_mechanism) — the ancient device that coupled dozens of interlocking gears to turn observation into prediction. Kythera does the same with markets: a fleet of ~29 specialized bots, each an expert in one thing, coordinated into a single stream of trading signals delivered to Telegram (and executed via [Cornix](https://cornix.io/)).

> ⚠️ **Risk warning.** This software places and manages leveraged trades and emits trading signals. Crypto trading carries substantial risk of loss. Nothing here is financial advice. Run it against paper/testnet first. You are responsible for any capital you put behind it.

---

## How it works

A single supervisor process, `main_watchdog.py`, launches and monitors the whole fleet. The bots don't call each other directly — they communicate through a **shared PostgreSQL database** and a **Telegram outbox** table. This keeps every bot independent and individually restartable.

```
Binance (WS /market + REST)
      │
      ▼
1  Data Ingestion ──────────►  PostgreSQL  ◄────────── candles, indicators,
      │                          ▲   │                  signals, trades, outbox
      ▼                          │   │
2  Indicator Engine  ───────────►│   │
      │                          │   ▼
3/7/9…29  Strategy & AI bots ───►│  28 Signal Orchestrator
      │  (read indicators,       │        │ (regime whitelist, de-dupes,
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

> For the deeper view — the database-as-message-bus design, the full signal lifecycle, the `core/` shared-layer contracts, and the known structural debt — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

> **Binance WebSocket note.** Binance decommissioned the legacy futures WS endpoints (`/stream`, `/ws`) on **2026-04-23**; kline/aggTrade/markPrice streams now live under `wss://fstream.binance.com/market/…`. All WS consumers in this repo use the new URL. Ingestion additionally runs a REST **freshness fallback** that bridges candle gaps whenever the WS goes silent, and caps each connection at 180 streams (HTTP-414 / silent-cap limits).

## The fleet

Grouped by role. Names match the process names in `main_watchdog.py`.

### Data & infrastructure
| Script | Process | Role |
|---|---|---|
| `main_watchdog.py` | Watchdog | Supervisor — launches, monitors, restarts the fleet; runs health checks every 60s |
| `1_data_ingestion.py` | Data Ingestion | Binance WebSocket + REST → candles in PostgreSQL (incl. gap-aware catch-up + freshness fallback) |
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
| `9_ai_sr_bot.py` | AI SR Bot | ML support/resistance (SRA1) |
| `11_ai_mis_bot.py` | AI MIS1 Detector | Multi-horizon (8/24/72/168h) ML ensemble |
| `12_ai_ats_bot.py` | AI ATS1 Detector | ATS1 ML strategy |
| `13_ai_rub_bot.py` | AI RUB1 Detector | Rubberband mean-reversion (ML) |
| `14_ai_atb_bot.py` | AI ATB1 Detector | Break-and-retest / target ML — **parked** (audit) |
| `15_ai_master_bot.py` | AI AIM2 Detector | **AIM2** meta-gate over all source signals (replaced AIM1 on 2026-07-05, see `docs/AIM2_DESIGN.md`) |
| `16_smc_forex_metals_bot.py` | SMC FOREX Detector | Smart-Money-Concepts on forex & metals |
| `17_mayank_bot.py` | Mayank Bot | "Mayank" discretionary-style strategy |
| `18_ai_abr1_bot.py` | AI ABR1 Detector | ABR1 break-and-retest ML |
| `21_btc_smc_strategy.py` | BTC SMC Bot | Bitcoin Smart-Money-Concepts |
| `24_quasimodo_bot.py` | Quasimodo Bot | Quasimodo (over-and-under) reversal pattern |
| `25_smc_ml_sniper.py` | TD & BB Bot | SMC ML sniper (trend-detection + break-and-retest) |
| `29_ufi1_bot.py` | UFI1 Fib Bot | Fibonacci-inversion SHORT (1D) — **parked** (audit) |
| `22_ip_pattern_bot.py` | IP Pattern Bot | Inverse-pattern bot — **disabled** in the watchdog |
| `30_ai_pex1_bot.py` | AI PEX1 Detector | Pump-exhaustion SHORT (Report 15 S6) — posts to `CH_NEW_IDEAS` |
| `31_ai_fmr1_bot.py` | AI FMR1 Detector | Funding-extreme mean-reversion (Report 15 S8) — `CH_NEW_IDEAS` |
| `32_ai_trm1_bot.py` | AI TRM1 Detector | Transition-resolution BTC trades (Report 15 S10) — `CH_NEW_IDEAS` |
| `33_ai_fif1_bot.py` | AI FIF1 Detector | ML filter over the Fast-In-Out signal stream (Report 15 S11) — `CH_NEW_IDEAS` |
| `34_ai_max1_bot.py` | AI MAX1 Detector | High-conviction throttle over the RUB2-SHORT model (1-3 trades/day) — `CH_MAIN`, gated by `MAX1_LIVE_POSTING` (default off) |

### Market intelligence
| Script | Process | Role |
|---|---|---|
| `10_pump_dump_detector.py` | Pump Dump Detector | Real-time pump/dump detection (EPD1) |
| `19_whale_logger_bot.py` | Whale Logger | Whale-trade monitor (aggTrade WebSocket) |
| `20_funding_logger_bot.py` | Funding Logger | Funding-rate extremes monitor |
| `23_market_tracker.py` | Market Tracker | Market overview + per-bot performance tables |
| `26_regime_detector.py` | Regime Detector | Market-regime classification (5 BTC classes × 3 alt contexts) |
| `27_bot_regime_analyzer.py` | Bot Regime Analyzer | Per-bot performance by regime → whitelist |
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
  health_monitor.py     Fleet health checks (data staleness, CPU, outbox) + Telegram alerts
  process_control.py    Park/restart markers (control/parked, control/restart)
  aim2_features.py      AIM2 feature builder — shared by trainer AND serving
  mis_features.py       MIS1 feature builder (leakage-free, shared)
  research_features.py  Feature builders for bots 30-33 (PEX1/FMR1/TRM1/FIF1, shared)
  model_artifacts.py    Uniform artifact loader for the research bots
  max1_gate.py          MAX1 throttle: probability floor + rolling-24h cap (pure, DB-free)
  signal_post.py        Shared outbox + ai_signals posting for the research bots
  trade_utils.py        Smart-target/SL math, leverage caps
  charting.py           mplfinance chart rendering
  market_utils.py       Coin lists, cooldowns, Telegram send helpers
  regime_logic.py       Regime classification helpers
  state_utils.py        Atomic per-bot state persistence
  bot_naming.py         Canonical bot-name normalisation
  logging_setup.py      Shared logging config
strategies/           Classic strategy modules used by 3_detectors.py
1_… 29_*.py           The numbered fleet (see above)
main_watchdog.py      Fleet supervisor (entry point)
main_telegram_bot.py  Interactive command bot
dashboard.py          Flask web UI
control/              Runtime markers: parked/ (keep bot down), restart/ (one-shot recycle)
tools/                Operations & ML pipeline
  walkforward_sim.py    Walk-forward replay simulator (first-touch labels, fees)
  retrain_from_replay.py  Retraining on replay labels (TD/BB/ABR1/MIS1)
  aim2_build_dataset.py / aim2_train.py   AIM2 dataset + training pipeline
  backup_db.ps1         Nightly pg_dump backup (scheduled task, retention 7d + 4w)
  regression_guard/     Golden-file regression fixtures
  audit/                Audit verification scripts (step 2/5)
backtest/             Unit/regression tests + offline backtests
docs/                 Design documents (AIM2, Regime Orchestrator, TimescaleDB migration)
audit_reports/        Full 2026 audit: 20 reports + per-model dossiers
legacy_trainers/      Sanitized provenance copies of all historical ML trainers
AUDIT_TODO.md         The audit's living findings ledger (P0…P3)
```

## Requirements

- **Python 3.10+**
- **PostgreSQL 14+** (production runs 17.6 + TimescaleDB 2.26; hypertable migration is designed in `docs/TIMESCALE_R1_MIGRATION.md` but not yet applied)
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
- **Channel routing:** every posting target is a `CH_*` variable (e.g. `CH_MARKET_DATA`, `CH_MASTER`), read through `core/config.py`. `0` disables a target. `.env.example` lists them all.
- **Health alerts:** `TELEGRAM_ALERT_CHAT_ID` — where `core/health_monitor.py` sends DATA_STALE / CPU / outbox alerts.
- **AIM2 rollout:** `AIM2_LIVE_POSTING` — `0` = shadow-only (default; predictions logged, nothing posted), `1` = post to the master channel.

Required variables (`DB_PASSWORD`, `TELEGRAM_BOT_TOKEN`) fail fast at startup if missing. Hardcoded channel ids and tokens are rejected by a `gitleaks` pre-commit hook (see below).

## Operations

- **Process lifecycle.** The watchdog staggers startup (`start_delay`), restarts crashed bots with exponential backoff, recycles heavy engines periodically (`restart_interval`), and runs `core/health_monitor.py` every 60s (candle staleness → auto-restart ingestion, CPU saturation, outbox failures → Telegram alert).
- **Parking a bot** (keep it down across restarts): create a marker file `control/parked/<script>.py`. Remove it to unpark. Currently parked by audit decision: `14_ai_atb_bot.py`, `29_ufi1_bot.py`.
- **One-shot restart** (e.g. after an `.env` change): `touch control/restart/<script>.py` — consumed by the watchdog on its next cycle.
- **Database backups.** `tools/backup_db.ps1` runs nightly at 03:30 as scheduled task "Kythera DB Backup" (`pg_dump -Fc` → `D:\_BACKUP\db`, retention 7 daily + 4 weekly).

## ML training & backtesting

Since the 2026 audit (see `audit_reports/`), all retraining follows one rule: **labels come from walk-forward replay of the real posted order geometry** (first-touch TP1-vs-SL, fees included) — never from close-based proxy labels. The pipeline:

1. `tools/walkforward_sim.py` — replays detector setups over historical candles, emits labeled trades (JSONL).
2. `tools/retrain_from_replay.py` — retrains TD/BB/ABR1/MIS1 successors (chronological split + purge gap, isotonic calibration, threshold chosen by replay PnL).
3. `tools/aim2_build_dataset.py` + `tools/aim2_train.py` — the same methodology for the AIM2 meta-gate (results: `audit_reports/20_aim2_training_results.md`).

New artifacts are written **only to the staging directory** — deploying one into the repo root is an explicit operator decision.

Older offline tools (`qm_backtest.py`, `smc_pattern_backtester.py`, `fib_backtest.py`, `check_*.py`) still work and read the same database. Regression tests live in `backtest/`; golden-file fixtures in `tools/regression_guard/`.

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

## Documentation

| Document | What it is |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the system fits together: message-bus design, signal lifecycle, `core/` contracts, structural debt |
| [`docs/OPUS-HANDOFF.md`](docs/OPUS-HANDOFF.md) | Working manual for contributors: work cycle, curated traps, quality bar, escalation rules |
| [`AUDIT_TODO.md`](AUDIT_TODO.md) | Living findings ledger (R / P0–P3 / Z) from the 2026 audit |
| [`docs/AIM2_DESIGN.md`](docs/AIM2_DESIGN.md) · [`docs/REGIME_ORCHESTRATOR.md`](docs/REGIME_ORCHESTRATOR.md) · [`docs/TIMESCALE_R1_MIGRATION.md`](docs/TIMESCALE_R1_MIGRATION.md) | Design docs: AIM2 meta-gate, regime orchestration, the (designed, not-yet-applied) TimescaleDB/R1 migration |
| [`docs/MODEL_INTENT.md`](docs/MODEL_INTENT.md) · [`docs/NEW_IDEAS_BOTS.md`](docs/NEW_IDEAS_BOTS.md) | Per-model intent; the 30–33 research bots |
| [`SETUP_ANLEITUNG.md`](SETUP_ANLEITUNG.md) | Detailed step-by-step setup (German) |
| `audit_reports/` | The full 2026 audit: 20 reports + per-model dossiers |

## License

See [LICENSE](LICENSE).

# Kythera

**A multi-bot crypto trading & market-intelligence system.**

Named after the [Antikythera mechanism](https://en.wikipedia.org/wiki/Antikythera_mechanism) — the ancient device that coupled dozens of interlocking gears to turn observation into prediction. Kythera does the same with markets: ~27 specialized bots, each an expert in one thing, coordinated into a single stream of signals.

> ⚠️ **Risk warning.** This software places and manages leveraged trades and emits trading signals. Crypto trading carries substantial risk of loss. Nothing here is financial advice. Run it against paper/testnet first. You are responsible for any capital you put behind it.

---

## What it does

A supervisor process (`main_watchdog.py`) launches and monitors a fleet of independent bots that talk to each other through a PostgreSQL database and push alerts to Telegram:

- **Data & indicators** — `1_data_ingestion` (Binance WebSocket + REST), `2_indicator_engine` (~120 technical indicators), `6_housekeeping`.
- **Strategy bots** — Smart-Money-Concepts, pattern/trendline breakouts, quasimodo, mean-reversion, and a family of ML models (pump/dump, trend-strength, break-and-retest, meta-ensemble).
- **Market intelligence** — pump/dump detection, whale-trade and funding-rate monitors, a market-regime detector and a signal orchestrator that coordinates the fleet.
- **Execution & delivery** — trade monitor (SL/TP tracking), Telegram outbox consumer, an interactive command bot.

Shared infrastructure lives in `core/` (pooled DB access, config, charting, cooldowns, target math).

## Requirements

- Python 3.10+
- PostgreSQL 14+
- A Telegram bot token and one or more channels

## Setup

```bash
git clone https://github.com/ziagl888/Kythera.git
cd Kythera
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -r requirements.txt

cp .env.example .env        # then fill in your secrets + channel ids
python main_watchdog.py     # starts and supervises the fleet
```

See `SETUP_ANLEITUNG.md` for the detailed walkthrough.

## Configuration

All secrets and routing live in `.env` (git-ignored) — never in code:

- **Secrets:** `TELEGRAM_BOT_TOKEN`, database credentials, Binance API keys.
- **Channel routing:** every posting target is a `CH_*` variable (e.g. `CH_MARKET_DATA`, `CH_BTC_SMC`), read through `core/config.py`. `.env.example` lists them all.

Hardcoded channel ids and tokens are rejected by a `gitleaks` pre-commit hook (see below).

## Development

```bash
pip install pre-commit
pre-commit install          # ruff lint + format, secret/PII scanning on every commit
pre-commit run --all-files  # run against the whole tree
```

- **Linting/formatting:** [ruff](https://docs.astral.sh/ruff/) (`pyproject.toml`).
- **Secret & PII scanning:** [gitleaks](https://github.com/gitleaks/gitleaks) with custom rules for Telegram tokens and private channel ids (`.gitleaks.toml`).

Contributions welcome — open an issue or PR. Please keep secrets in `.env`, and let the hooks run.

## License

See [LICENSE](LICENSE).

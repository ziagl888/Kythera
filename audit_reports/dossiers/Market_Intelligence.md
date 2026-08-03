# Dossier: Market Intelligence (whale logger · funding logger · market tracker)

> **Pure data suppliers/reporters, not traders** — they don't trade, but their data (tracker statistics) is a decision basis for humans. **Note (16b): none** (not assessable as a strategy); framing: "the intelligence layer is a display layer."
> **Core verdict:** collecting without a consumer. Whale and funding data — exactly the data classes that could enrich a regime gate — are consumed by **not a single decision logic**. The whale logger has been **dead since 18.04.**, `ticker_10s` is **empty**, only the funding logger runs cleanly. Either feed it in as features (S8/S12) or shut it down — the current state is pure operating overhead.

## 1. Fact sheet

| Component | Job | Data storage | Channel/consumer |
|---|---|---|---|
| `19_whale_logger_bot.py` | Binance futures aggTrade firehose, whale trades + buy/sell pressure ratios (1h/4h/24h) | `whale_data/whale_trades_*.json` (daily files, full rewrite) | Telegram info posts; **no machine consumer** |
| `20_funding_logger_bot.py` | Funding rates for all perps, breadth/extreme alerts, history | `funding_data/funding_history_*.json` (unbroken since February) | Telegram alerts via `telegram_outbox`; **no machine consumer** |
| `23_market_tracker.py` | Hourly/daily reports: per-bot performance, gainers/losers, regime-fit label, signal summary | reads `closed_trades_master`, `closed_ai_signals`, `ai_signals`, `ml_predictions_master`, regime tables | Telegram report channels; per-bot table = human decision basis |
| (side note) | `ticker_10s` table: intended 10s ticker basis for EPD1 | **empty** — EPD1 works purely in-memory (N3, Report 17) | suggests a data basis that doesn't exist |

## 2. Live balance

- **Whale logger: dead since 18.04.2026** — last `whale_trades_*.json` from 18.04.; even before that the files only covered **49 of 529 symbols** (P1.42 ✔✔: 538 aggTrade streams on **one** WS connection, fapi cap ~200/conn → ~340 symbols silently never delivered; reconnect backoff never resets → capped 300s waits).
- **Funding logger: running** — files unbroken since February, timezone handling clean (epoch-based); but the 75% breadth "extreme" threshold fires in the normal state (baseline +0.01% → alert every 15 min possible over days, P2.40); `lastFundingRate` is the predicted, not the settled, rate.
- **Market tracker: running, but fragile** — pool leak on query error (~1 leak/h → after ~8h all tracker jobs dead until restart, P1.43); "opened" counts double-count AI trades and count shadow predictions too (P1.44) → distorts exactly the per-bot statistic by which humans judge bots (shadow flood: EPD1 31k + AIM1 25k unposted rows/7d from Bot 10).
- **`ticker_10s` empty** — would be the training-data source for S6 (pump-exhaustion short).

## 3. Findings

| ID | Component | Severity | One-liner | Status |
|---|---|---|---|---|
| P1.42 | 19 | HIGH | 538 streams on 1 WS conn (cap ~200) → 49/529 symbols; the logger has been writing no files at all since 18.04. | ✔ |
| 09-W2 | 19 | MEDIUM | Reconnect backoff never resets after success → permanently 300s reconnect waits | ~ |
| 09-W3 | 19 | MEDIUM | CPU scans + full-day JSON rewrite block the event loop alongside the firehose → slow-consumer disconnects | ~ |
| P2.40 | 20 | MEDIUM | "Extreme" breadth threshold of 75% fires in the normal state | ~ |
| 09-F2 | 20 | LOW | "1h" breadth silently falls back to the current value (apparent stability); top-5 lists span ALL Binance symbols instead of the tracked set | ~ |
| P1.43 | 23 | HIGH | Pool leak + missing rollback → after ~8h all tracker jobs dead until restart | ~ |
| P1.44 | 23 | HIGH | "Opened" counts: AI trades double-counted + shadow predictions counted in → per-bot statistic (a decision surface!) distorted | ~ |
| 09-T3 | 23 | MEDIUM | Regime-fit label: a single query error poisons the shared connection → whole column "---"; message chunker cannot split an over-length block (silent Telegram reject) | ~ |
| N3 | DB | MEDIUM | `ticker_10s` is empty — populate (S6 training source) or drop | ✔ |
| 16b-Q7 | all | HIGH | No machine consumer: whale/funding feed into no decision logic — dead data collection resp. human info | ✔ |

Status: ✔ = proven live/DB · ~ = code finding (Report 09), not separately quantified live.

## 4. Dependencies & cross-cutting risks

- **The tracker's per-bot table is the human decision surface** — its two upstream polluters (shadow flood from Bot 10, double-counting P1.44) distort portfolio decisions; on top of that, every tracker WR inherits the monitor-label caveat (Report 17: only 63.4% replay agreement) and the P1.9 censoring (regime closes of foreign trades counted as neutral).
- **Silent-failure house style:** blanket `except:pass` → failure mode "report silently doesn't arrive" (matches the P2.47 pattern: a wedged bot stays green).
- **Strategy proposals hang off this layer:** S8 (funding-extreme mean reversion — 4 months of unbroken history sitting unused) and S12 (whale-flow confirmation — only possible after the P1.42 fix).

## 5. Remediation plan

1. **Whale sharding fix (P1.42):** shard streams across 3 WS connections, reset backoff after a successful connect, JSONL append + rolling aggregates instead of full rewrite — then restart the logger. **Or** (16b): shut it down as long as there is no consumer; only worth operating once S12 exists as a consumer.
2. **`ticker_10s` decision (N3):** populate (becomes the training-data source for S6) or drop — the limbo state fakes a data basis that isn't there.
3. **Funding P2.40:** threshold to 95/85 + magnitude requirement (|rate|>0.02%) or transition alerts; after that S8 is the first real consumer of the funding history.
4. **Tracker hardening (P1.43/P1.44):** `try/finally close` + rollback before fallback; `posted=TRUE` filter, opens only from `ai_signals`+`closed_ai_signals`; rollback in the regime-fit path; per-job heartbeat against silent outages.
5. **Fundamental decision (16b):** either feed whale/funding data in as features into regime/gate (S8/S12), **or** shut the loggers down — collecting without a consumer is pure operating overhead.

## 6. Evidence

- `audit_reports/09_intelligence.md` — code findings 19/20/23 + cross-cutting
- `audit_reports/STEP2_DB_VERIFICATION.md` — P1.42 ✔ (49/529, dead since 18.04.), shadow-flood magnitude
- `audit_reports/17_monitor_replay_and_gaps.md` — N3 (`ticker_10s` empty), coverage matrix (whale dead, funding files current)
- `audit_reports/16_strategy_concept_evaluation.md` — cross-cutting finding 7 (display layer), section 7 (intelligence layer)
- `audit_reports/15_strategy_proposals.md` — S8 funding mean reversion, S12 whale confirmation, S6 (ticker_10s as training source)
- `AUDIT_TODO.md` — P1.42–P1.44, P2.40 with annotations

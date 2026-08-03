# 17 — Monitor replay & remaining analysis gaps (Step 7)

**As of:** 2026-07-03 · **Trigger:** the question "have we missed anything?" — answer: yes, empirical validation of the **trade monitors** was still outstanding. That is exactly where the biggest new finding sits.

## 1. Monitor replay: scoring vs. first-touch truth (n=388, last 30 days)

**Method:** 400 random classic trades (status 0/1/2/SL0-SL2, `closed_trades_master` holds entry/TP1/SL per row) replayed against the complete 5m candles between open and close (first-touch logic; TZ ambiguity of the naive timestamps resolved via close-price alignment).

| Metric | Result |
|---|---|
| Agreement monitor ↔ replay (≥TP1 yes/no) | **only 63.4%** |
| Monitor **missed** TP1 (replay: hit) | **17.8%** (69/388) |
| Monitor awarded TP1 even though the SL, per replay, came **first** | **18.8%** (73/388) |
| `close_price` falls within the close-time candle | only 17.8% (median deviation otherwise 1.21%) |
| TP+SL in the same 5m candle (genuine ambiguity) | 0.3% — does NOT explain the discrepancy |
| 5m data gaps (10 sample coins, 25 days) | **0** — the price data is not the cause |
| TZ-shift alignment | exactly 50/50 UTC vs. local → mixed time writers proven again (R3) |

**Per strategy (agree%):** Fast In And Out 73% · Support Resistance 67% · **5 Percent 45% · Volume Indicator 44%** — for the latter two, the scoring is effectively noise.

**Interpretation:** the code findings P2.7 (monitor checks only the most recent 5m candle per cycle → misses hits in between/during downtime) and P1.2 (trailing SL never trails) are hereby **quantified**: both error classes (missed TPs AND falsely awarded TPs) occur at ~18% each. Since both directions are similarly frequent, the net bias on the Report-14 WRs is moderate — but the **per-trade truth is unreliable**, and so is every statistic trained on it (whitelist! analyzer! S11 labels!). Caveat: part of the discrepancy could be TZ-alignment residual error; the magnitude clearly exceeds that though (ambiguity 0.3%, data gap-free).

**Consequence — the monitor rewrite becomes strategically near-P0:**
1. Forward scan with `last_checked_open_time` instead of "most recent candle" (P2.7 fix) — hits get scored from **candles**, not from the current price at cycle time.
2. `close_price` = price of the triggering candle (not the query timestamp).
3. Trailing-SL fix (P1.2) + TZ fix (R3) in the same pass.
4. After that, re-pull the Report-14 figures once (re-scoring the last 30 days via replay is possible).

## 2. Further gaps found in this step

- **N2 — 800 outbox messages silently dropped (P2.11 quantified):** all with `Timed out` after 3 attempts, including **212 in the Fast-In-And-Out trading channel** and 98 Volume Indicator — signals/SL updates lost without any alarm. Fix as in P2.11 (retry without parse_mode, dead letter + operator alert) plus timeout handling as "unknown outcome" (P0.1).
- **N3 — `ticker_10s` is empty.** EPD1 works purely in-memory; the table is dead. Either populate it (would be a training-data source for S6!) or drop it — currently it suggests a data basis that does not exist.
- **N4 — AI trades are NOT retroactively auditable:** `ai_signals` rows are removed on close (only 1,559 open rows remain) → SL/targets of closed AI trades are gone; the monitor replay from section 1 is impossible for the AI fleet. **Fix (small, important):** write `sl`, `targets`, `entry1/2` into `closed_ai_signals` on close — from then on the AI fleet is exactly as auditable as the classic strategies.
- **N5 — 5m retention ~30 days:** replays/fine-tuning on a 5m basis are only possible for the last 30 days. Either leave it as-is deliberately, or set up a compressed archive for select purposes (monitor re-score, S11 labels).
- **N6 (positive) — datagrepper-5m is clean:** 0 gaps across 10 sample coins × 25 days; together with the 1h census (Step 2: 0 gaps/529 coins), ingestion completeness is well documented. The remaining open ingestion points are R1 (forming candle) and P1.11 (the boundary row stays partial until the REST catch-up).

## 3. Coverage matrix — what's checked now, what remains open?

| Component | Code audit | Empirical | Open |
|---|---|---|---|
| 1_data_ingestion (datagrepper) | ✔ (02) | ✔ gaps 1h+5m=0, forming/partial proven | cross-TF consistency (1h vs 5m aggregate), behaviour after restart (does the catch-up fill the 6h gap from 3.7.?) |
| 2_indicator_engine | ✔ (02) | ✔ RSI formula, POC broadcast, ma_200 | parity of further indicators (WMA/KAMA/TSI) vs. pandas_ta — spot check outstanding |
| 5_/8_ trade monitors | ✔ (03) | ✔ **replay: 63% agreement** | AI monitor replay (only possible after the N4 fix) |
| 4_telegram_bot / outbox | ✔ (03) | ✔ dups, 0 retry doubles, 800 timeouts | no Cornix-side reconciliation (external) |
| Orchestrator/regime | ✔ (04) | ✔ Report 16 | counterfactual value of the gate (proposal 16-no.8) |
| ML bots + trainers (_X) | ✔ (05-08, 13) | ✔ calibration, artifact introspection | MIS1 provenance remains lost |
| Market intelligence (10/19/20/23) | ✔ (09) | ✔ whale dead, funding files current, pump_dump_events | funding content validation; ticker_10s decision (N3) |
| Dashboard/watchdog | ✔ (10, 01) | ✔ P2.47 proven live | dashboard port exposure from outside (P0.8) not tested |
| chart_data_service | ✔ (02) | — | empirically unchecked (low priority, no money path) |
| 99_smc_paper_bot (live only) | **✘ never audited** | — | the only fully unchecked module |
| legacy `_X` runtime (1-datagrepper etc.) | — | not running (watchdog fleet does not include them) | archive |
| exchange/Cornix reality | — | — | external sample (50 trades) still recommended |

**Conclusion:** with the monitor replay, the last big internal gap is closed. What genuinely remains open: 99_smc_paper_bot (code never seen), the external Cornix/exchange reconciliation, and the follow-ups N3/N4 + indicator spot checks. The most important new work item from this step is the **monitor rewrite (candle-based first-touch scoring)** — it now comes BEFORE retraining the models, because it supplies their labels.

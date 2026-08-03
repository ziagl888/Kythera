# Dossier: RUB1 (Rubberband Mean Reversion)

> Mean-reversion bot: 4-fold extreme pre-filter + 9-feature ML as a snap-back filter. Grade (Report 16): **D+** (Σ +3,675 net, but tail-/SHORT-driven). Core verdict (Report 13): model **not trustworthy** — a silent MACD-9/21↔12/26 semantic break + a memorized random split; the live profit cannot come from the ML, it comes from the pre-filter + S/R construction.

## 1. Fact Sheet

| | |
|---|---|
| Bot file | `13_ai_rub_bot.py` (hourly; scheduler comment says :12, runs at :10 — P3 drift) |
| Model artifacts | `long_reversion_model.joblib` / `short_reversion_model.joblib` |
| Trainer | `legacy_trainers/BT3-1` (Datagrepper, MACD `ta.macd(fast=9,slow=21)`), `BT3-2-ml_trainer.py` (training), `BT3-3-optimizer.py` (thresholds) — provenance ✔ clarified (Report 13) |
| Training date | not documented in the sources |
| Data source | trainer: BT3-1 export (own MACD calculation 9/21); live: 1h DB indicator columns (`macd_dif_normal_12_26_9`!) + ~95d closes for the regression line |
| Label | **+10% touch/72h with no SL/drawdown path** (X-R1) — when catching a falling knife, the drawdown path is exactly the risk measure |
| Features | 9 |
| Thresholds | 0.75 / 0.85 — via precision maximization on a mini test set (**>5 trades!**, `BT3-3:31`) |
| Signal path | pre-filter (≥8% below/above the 90d regression + RSI<30 + TSI<−15 + Donchian touch) → predict → AI channel via outbox/Cornix; TP/SL via `get_hvn_and_sr_levels`/SR construction; publishes TP1–3, monitor scores up to 10–20 targets (P2.31) |

## 2. Live Track Record (as of 2026-07-03, active era 24.02.–03.07., deduplicated, unleveraged)¹

- **n = 2,496 · WR 57.6% · avg +1.57%/trade · median −0.06 · Σ net +3,675** — the sum comes from tail wins (p95 +33%).
- **Direction split: SHORT 63.9% vs LONG 48.7% WR** — one of the fleet's biggest directional asymmetries; Reports 14/15/16 unanimously recommend: **close the LONG gate**.
- **Monthly trend:** no specific monthly drift reported in the sources.
- **Calibration finding:** RUB1 does not appear with its own row in the Step-2 calibration table; Report 16 rules the ML layer as noise (MACD break, memorized split) — the confidence isn't reliable as a probability anyway (X-R4).

¹ Caveat (Report 17): monitor-generated numbers; monitor scoring only 63.4% replay-consistent (Classic sample), AI trades not auditable retroactively (N4: `ai_signals` deletes SL/targets on close).

## 3. Findings (consolidated)

| ID | Level | Severity | Finding | Status |
|---|---|---|---|---|
| 13-P0 (MACD) | Trainer↔Bot | P0 | **MACD semantic break:** trained on `ta.macd(fast=9,slow=21)` (`BT3-1:85-87`), live the `macd_dif_normal_12_26_9` DB columns are fed in under the same feature name (`13:92-93,150-151`) — invisible to name validation | ✔ confirmed (code) |
| 13-P0 (split) | Trainer | P0 | random split (`BT3-2:34`) over hourly duplicated persistence episodes → test AUC = memorization; live, only the *first* episode hour is traded via the 4h cooldown, training averages over all of them | ✔ confirmed (code) |
| 13-P1 (threshold) | Trainer | P1 | thresholds 0.75/0.85 via precision maximization on a mini test set (>5 trades) — maximum statistic (X-R2) | ✔ confirmed (code) |
| 13-P1 (label) | Trainer | P1 | label with no SL/drawdown path — knife-catch risk unmodeled (X-R1) | ✔ confirmed (code) |
| P1.19 / 06-HIGH | Bot | P1 | prediction on forming-candle indicators: LIMIT 1 = open candle from ~2 min of data; rsi/donchian trigger + all 9 ML features mix the :10 live price with :02 partial indicators; regression includes the current candle (95d vs 2160 candles excluded in training) | ~ open `[DB]` (code double-claimed: AUDIT_TODO + Report 13; not separately measured live) |
| 13-P1 (parity) | Bot/pipeline | P1 | DB indicator parity unverified — Step 2 already proved DB `rsi_14` ≠ Wilder (Δ≈4.8) → **pre-filter gates (rsi<30, tsi<−15) fire live in a different population than in training** | ✔ partially confirmed (RSI); TSI scaling ~ open |
| P2.29 / 06-MEDIUM | Bot (core) | P2 | `get_hvn_and_sr_levels` reads 95d **without ORDER BY** (SL/TP source for SRA1/ATS1/RUB1) → phantom extrema possible as SL/TP prices | ~ open `[DB]` |
| P2.31 / 06-MEDIUM | Monitor | P2 | publishes TP1–3, monitor scores up to 10–20 targets → live stats ≠ Cornix reality | ✔ (Step 2: targets_hit up to 21, RUB1 double digit) |
| 06-MEDIUM (perf) | Bot | P2 | hourly ~95d × 538 coins of closes (~1.2M rows/h) for one linear regression + a per-row `.apply` | ~ open |
| 06-LOW | Bot | P3 | `dist_to_trend_pct` sign flip/blow-up when the trend value is near 0/negative; estimator truthiness (13:52,60); scheduler comment drift | ~ open |

## 4. Dependencies & Cross-Cutting Risks

- **R1 forming candle:** RUB1 explicitly affected (P1.19); the system fix (closed-candle contract) is a prerequisite for any retrain.
- **X-R1** (touch label with no SL), **X-R2** (threshold on a mini test set), **X-R3** (split leakage — per Report 13 "RUB1 is worst"), **X-R4** (uncalibrated confidence), **X-R6** (forming-candle serving) all hit RUB1; plus the family's own feature-semantic break (MACD) as the textbook case for the shared feature builder.
- **P2.12 RSI formula:** DB RSI is ewm(span), not Wilder (Δ avg 4.8) — shifts the pre-filter population (see above).
- **P2.29:** shares the ORDER-BY-less SL/TP source with SRA1/ATS1.
- **Whitelist/orchestrator:** RUB1 is not listed among the frozen raw-name rows catalogued in Step 2; the fleet-wide gate/TRANSITION fallback problem (P0.4/P2.23) applies to the pipeline as a whole.
- **R3 TZ:** mixed naive time columns also affect RUB1 evaluation/cooldowns.
- **Monitor label caveat (Report 17):** WR/PnL are monitor-generated; S11-style label projects and any retrain need the monitor rewrite first.

## 5. Remediation Plan

**(a) Immediately, without a retrain:**
1. **Close the LONG direction gate** (SHORT 63.9% vs LONG 48.7% — Reports 14 D.5, 15 S1, 16 §8.2).
2. Closed-candle fix (P1.19: `open_time < date_trunc('hour', NOW())`, `curr_close` from the same closed candle).
3. `ORDER BY open_time ASC` in `get_hvn_and_sr_levels` (P2.29, one line).
4. Stop communicating confidence as a "%"; set operating points conservatively (Report 13, measure 4).
5. Store exactly the published targets (P2.31); do the regression in SQL/vectorized instead of a 1.2M-row fetch (perf).

**(b) Retrain (Report 13/16: full retrain only, no patch; sequenced after MIS1/AIM1/ABR1, together with ATB1):**
- **Shared feature builder bot↔trainer** (fixes the MACD class structurally) — core requirement from Report 13.
- Label = first-touch simulation of the actual posted geometry **with an SL path** (X-R1/P0.10 simulator, V3 from Report 15).
- Episode dedup + a chronological 3-way split with embargo (X-R3 fix); threshold on validation (instead of the >5-trades test set); isotonic calibration out-of-time; meta.json + startup assertions.
- Clarify indicator parity (Wilder RSI vs ewm, TSI scaling), or re-tune the pre-filter to the DB semantics.
- Prerequisites: R1 fix, monitor rewrite (Report 17), dedup index on `closed_ai_signals` (V2).

**(c) Open questions:** training period/date unknown. TSI scaling parity unchecked. Does the pre-filter alone (without the ML gate) carry the same performance? P1.19 and P2.29 are `[DB]` points without separate live measurement.

## 6. Evidence

- `AUDIT_TODO.md` — P1.19, P2.29, P2.31, P2.12, R1/R3.
- `audit_reports/06_ai_bots_a.md` — bot-engine findings (forming candle, get_hvn without ORDER BY, perf, dist_to_trend, targets divergence).
- `audit_reports/13_x_ml_trainers.md` — RUB1 section (MACD break, memorization split, thresholds, label, RSI parity) + X-R1..R6.
- `audit_reports/14_bot_performance_db.md` — realized numbers (n=2,496, +3,675, p95 +33%, direction split), LONG-gate recommendation.
- `audit_reports/STEP2_DB_VERIFICATION.md` — RSI≠Wilder (Δ4.84), targets_hit proof; no dedicated RUB1 calibration row.
- `audit_reports/16_strategy_concept_evaluation.md` — grade D+, verdict "the win can't come from the ML".
- `audit_reports/15_strategy_proposals.md` — S1 direction gates (RUB1 SHORT-only), V1–V3 prerequisites.
- `audit_reports/17_monitor_replay_and_gaps.md` — 63.4% replay caveat, N4.

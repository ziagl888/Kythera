# Dossier: MIS1 (Pump/Dump Horizon Battery)

> A battery of 8 binary XGBoost classifiers ({8,24,72,168}h × {pump,dump}), hourly per coin. Grade (Report 16): **family C+ — MIS1-72H B− (workhorse, +15,868 net), MIS1-168H C−, MIS1-8H/24H D.** Core verdict (Report 13): technically wired up consistently, statistically not trustworthy; the strong 72H performance arises DESPITE the training methodology, not because of it. The fleet's most urgent retrain candidate.

## 1. Fact Sheet

| | |
|---|---|
| Bot file | `11_ai_mis_bot.py` (hourly scan ~:11 after the :02 indicator run) |
| Model artifacts | `pump_model_{8,24,72,168}h_{pump,dump}_final.pkl` + `threshold_*` (stored atomically with the models) |
| Trainer | `legacy_trainers/X5-analyze_indicators_v8.py` — **found after the fact** (the f-string filename fooled every literal grep; Report 13 addendum). Verification: hyperparameters (1000/4/0.02/spw1.5/gamma2/lambda10) and all 67 features including the accident features reproduced exactly |
| Training date | models + thresholds 26./27.01.2026 |
| Data source | 1h indicator tables from the live DB, 400-day window with today's coins.json (survivorship), training includes forming-candle rows |
| Label | close-to-close return over the horizon: ±5%/8h, ±10%/24h, ±15%/72h, ±25%/168h — **no path/SL, pure future return** (X-R1) |
| Features | 67, identical across all 8 models (introspection; includes ticker-leakage accident features from the `line_cols` loop) |
| Thresholds | per model from `threshold_*` (e.g. 168h_pump 0.2825, only 3 points above the 0.25 shadow floor); fallback 0.60 ≠ init default 0.5; selection via cross-horizon argmax of the raw probabilities |
| Signal path | AI channel via outbox/Cornix; TP/SL from `calculate_smart_targets`; publishes TP1–5, monitor scores up to 21 targets (P2.31) |

## 2. Live Track Record (as of 2026-07-03, active era 24.02.–03.07., deduplicated, unleveraged)¹

| Model | n | WR | avg PnL | Median | Σ net |
|---|---|---|---|---|---|
| MIS1-72H | 11,822 | 63.9% | +1.44% | 0.00 | **+15,868** |
| MIS1-168H | 7,167 | 58.5% | +1.07% | −0.03 | +6,928 |
| MIS1-8H/24H | 1,003 | ~52% | +1.4% | negative | +1,261 |

- **Monthly trend:** 72H positive every month; 168H drifting since May (WR 48/49/35%); 8H/24H purely tail-driven.
- **Direction split:** not reported in the sources.
- **Calibration (Step 2):** 72H **negative** (72% WR @conf<0.4 → 65% @0.5–0.6 — thresholds meaningless, supports P1.17); 168H flat; 8H positive (91% @0.7–0.8, small n) → 8H is one of the four genuinely calibrated candidates for S4 "Calibration-Sized Positions" (Report 15).
- Dead legacy name variants exist in `closed_ai_signals` (`MIS1-72h_dump`, `MSI1-*`), 100% censored — remove during purge.

¹ Caveat (Report 17): all numbers are monitor-generated; monitor scoring only agrees with the first-touch truth 63.4% of the time in the Classic replay, and AI trades are not auditable retroactively (`ai_signals` rows are deleted on close, N4). Per-trade truth unreliable, net bias moderate.

## 3. Findings (consolidated)

| ID | Level | Severity | Finding | Status |
|---|---|---|---|---|
| P1.17 / 06-CRITICAL | Bot | P1 | prediction on the running candle (iloc[-1:], :11 = ~1/6 partial volume) with stale :02 indicators → every prediction structurally skewed | ✔ (Step 3: the forming candle's indicator row really exists; Step 2: negative calibration supports it) |
| P1.18 / 06-HIGH | Bot | P1→P3 | one feature set for all 8 models + `.values` disables name validation → permutation risk | ✘ refuted (Step 3: all 8 pkls have identical 67 `feature_names_in_`, parity test error-free); `.values` fragility remains as P3 |
| 13-P1 (leakage) | Model | P1 | accident features: `pct_distance` over derived columns → values in coin price scale; trees actually split on it (168h_dump 558 splits, 168h_pump top feature 10.4% importance) → **ticker/price-class leakage**, confirmed at the trainer source (`line_cols` loop, line 69) | ✔ confirmed |
| 13-Addendum-P0 | Trainer | P0 | `StratifiedKFold(shuffle=True)` over hourly samples with 8–168h overlapping label windows → twin leakage; reported precision heavily inflated | ✔ confirmed (code) |
| 13-Addendum-P1 | Trainer | P1 | threshold = best precision **maxed over the 5 folds** (maximum statistic), recall floor only 3% | ✔ confirmed (code) |
| 13-Addendum-P1 | Trainer | P1 | the final model is fit on ALL data, the threshold comes from the shuffle folds → the operating point doesn't match the deployed model | ✔ confirmed (code) |
| 13-Addendum-P2 | Trainer | P2 | no calibration (spw=1.5), `fillna(0)` cascade, training includes forming-candle rows, survivorship (today's coins.json over 400 days) | ✔ confirmed (code) |
| P2.32 / 06-MEDIUM | Bot | P2 | `autocommit=True` → outbox/ai_signals/master-log inserts not atomic | ~ open |
| P2.33 / 06-MEDIUM | Bot | P2 | the best-candidate comparison uses raw probabilities from differently calibrated models → a below-threshold candidate can displace an above-threshold signal | ~ open |
| P2.34 / 06-MEDIUM | Bot | P2 | `fillna(0)` doesn't clean `inf` from zero-volume divisions; predict errors swallowed | ~ open |
| P2.31 / 06-MEDIUM | Monitor | P2 | subscribers see TP1–5, monitor scores up to 21 targets → live stats ≠ Cornix reality | ✔ (Step 2: `targets_hit` up to 21) |
| 13-P2 | Model | P2 | 1000 trees with no early stopping, identical hyperparameters for all 8 horizons; dead binary flags (`rsi_14_above_50` importance 0 across all 8) | ✔ confirmed |
| 13-P3 | Model | P3 | 168h_pump threshold 0.2825 only 3 points above the 0.25 shadow floor (shadow band empty); fallback 0.60 ≠ 0.5 | ✔ confirmed |
| 06-LOW | Bot | P3 | `ai_signals` presence check blocks signals AND shadow logging indefinitely (hangs on the monitor delete); dead code `best_prob<0.25` | ~ open |

## 4. Dependencies & Cross-Cutting Risks

- **R1 forming candle (critical):** MIS1 is explicitly named as the most critical R1 consumer; proven live (Step 2: partial candle + an indicator row right on it).
- **X-R1** (label = pure future return with no SL path), **X-R2/X-R3** (fold-max threshold, shuffle twin leakage), **X-R4** (uncalibrated "confidence"), **X-R5** (fillna silent default), **X-R6** (serving on the forming candle) — MIS1 hits all six.
- **P0.4/P2.25 whitelist:** the orchestrator gates the **entire MIS family** via raw-name rows frozen since 19.04. → the regime gate runs on statistics that are 2.5 months stale (✔ Step 2).
- **Monitor label caveat (Report 17):** all WR/PnL, and therefore every future training label, are monitor-generated (63.4% replay consistency); AI history isn't replayable without the N4 fix.
- **R3 TZ:** naive/mixed time columns also affect MIS1 evaluations (session TZ Europe/Bucharest).

## 5. Remediation Plan

**(a) Immediately, without a retrain:**
1. Closed-candle fix in the bot (`iloc[-2:-1]` or `open_time < date_trunc('hour', NOW())`) — P1.17.
2. Stop communicating confidence as a "%"; set operating points conservatively using the Step-2 calibration tables (Report 13, measure 4).
3. Switch candidate ranking to `prob − threshold` (P2.33); `replace([inf,-inf],nan)` before fillna (P2.34); remove autocommit, one commit like ATS/RUB (P2.32).
4. Whitelist fix (P0.4: `pretty_name()` in the orchestrator + staleness gate), so the regime gate works on fresh statistics again for the MIS family.

**(b) Retrain (priority #1 of the entire retrain program, Reports 13/16):**
- Versioned trainer in the repo that **imports the bot's feature builder**; `line_cols` fix (leakage features out).
- Label = first-touch simulation of the geometry actually posted (X-R1 / P0.10 simulator, V3 from Report 15) instead of close-to-close return.
- Only closed candles (R1 fix first!), chronological 3-way split with embargo + episode dedup, threshold on validation, isotonic calibration out-of-time.
- Artifacts + meta.json (features, threshold, training period, git SHA); the bot loads the threshold from meta; startup assertion "no feature is constant".
- **Rather drop MIS1-8H/24H in the retrain** (Report 16: conceptually the thinnest combination); focus on 72H, 168H only with drift monitoring.
- Prerequisites: monitor rewrite (Report 17 — supplies the labels), dedup index on `closed_ai_signals` (V2), N4 fix (write SL/targets along with the close).

**(c) Open questions:** why does 72H work despite the methodology (Report 13 hypothesis: momentum/vol features carry a real signal over long horizons)? 168H drift since May — regime or model aging?

## 6. Evidence

- `AUDIT_TODO.md` — P1.17/P1.18, P2.31–2.34, R1/R3, P0.4.
- `audit_reports/06_ai_bots_a.md` — bot-engine findings (forming candle, feature_names, inf, argmax, autocommit, LOW points).
- `audit_reports/13_x_ml_trainers.md` — MIS1 section (introspection, P1.18 refutation, leakage) + **addendum** (X5 trainer found, label definitions, trainer defects).
- `audit_reports/14_bot_performance_db.md` — realized numbers per horizon, monthly trends, legacy name variants.
- `audit_reports/STEP2_DB_VERIFICATION.md` — calibration measurements, whitelist freeze, targets_hit up to 21, R1 proof.
- `audit_reports/16_strategy_concept_evaluation.md` — grades (72H B−, 168H C−, 8H/24H D), concept verdict, retrain priority #1.
- `audit_reports/15_strategy_proposals.md` — V1–V3 prerequisites, S4 (MIS1-8H as a calibrated sizing candidate).
- `audit_reports/17_monitor_replay_and_gaps.md` — 63.4% replay caveat, N4 (AI trades not auditable).

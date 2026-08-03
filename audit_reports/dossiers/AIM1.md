# Dossier: AIM1 — master/meta model (bot 15)

> Stacking meta-model over all bot signals (market context × swarm × source identity). **Grade F (Report 16).** Core verdict: **actively harmful, reliably inverted** — conf>0.9 → 9.3% WR, Σ −3,399 net; the biggest AI loss-maker. **Pause immediately** (Report 13/14/16 unanimous).

> **CLOSED 2026-07-05:** operator decision — AIM1 is retired (no retrain). Successor **AIM2** per `docs/AIM2_DESIGN.md` (S7 blueprint, batch-E framework); slot 15, channel and posting flow remain, `ai_signals.model='AIM2'` for clean attribution. This dossier is therefore historical.

## 1. Fact sheet

| Field | Content |
|---|---|
| Bot file | `15_ai_master_bot.py` (scans signal candidates from all bots, 5-min window) |
| Artifact | `master_trade_model_xgboost_combined_signals.pkl` (XGBoost; feature list verified via pkl string extraction, offsets 1884001–1884326) |
| Trainer | `legacy_trainers/x10-mlzeitfolge-v2.py` (`master_task.py` is only a loader prototype) — provenance clarified in Report 13 |
| Training date | not documented; the dummy vocabulary proves: **predates the current fleet** (only knows `ai_model_MSI1-*` typos, `conv_bot_{5% Bot, Fast Bot, …}`) |
| Data source | historical signal/indicator DB values from back then; feature join via `dt.round('1h')` — **rounds UP** → join candle close up to ~90 min into the **future** of the signal (live uses floor) |
| Label definition | +10% within 72h **before** −7.5% SL (close-based) → rewards volatility; pkl proof: top gains `atr_21_pct_close` (137) + `atr_14_pct_close` (97) → **the model is a volatility detector** |
| Features | market context (dist/ATR features), signal swarm context, source identity as one-hots (identity block = 14.6% of total gain, `conv_bot_nan` third-most-important feature) — live via `reindex(fill_value=0)` **all identity dummies = 0** |
| Thresholds/operation | posts almost only conf>0.85; `scale_pos_weight=2.105`, test set = early-stopping set, **no calibration**; three inconsistent confidence mappings (v2 / master_task / bot 15) |
| Channel | own AIM1 channel (Step 2: "AIM1 channel is actively harmful") |

## 2. Live results (as of 2026-07-03, active era, deduplicated)

- **n = 3,047 · WR 50.8% · avg −1.02%/trade · median −1.01 · Σ net −3,399 price-%** (Report 14). *Contradiction between both states:* the Step-2 table gives **n=3,125 / WR 50.3%** (before dedup 3,125→3,047 — the only model in the active era with notable duplicates); Feb start at 24% WR.
- **Calibration inverted (Step 2, n=19,561 shadow+posted):** corr(confidence, win) = **−0.304**; bucket 0.8–0.9 → 31.1% WR; bucket **0.9–1.0 → 9.3% WR**. Report 15 (E5, finer buckets, n=19,295): conf 0.9–0.95 → **8.3% WR, −9.53%/trade**; conf **>0.95 flips to 85% WR** (n=267) — the inversion is not monotonic.
- Direction split: not reported. Shadow flood: ~25k unposted `ml_predictions_master` rows/7d (Step 2).[^1]

[^1]: **Monitor caveat (Report 17):** all figures are monitor-generated; the first-touch replay agrees with the monitor only 63.4% of the time (17.8% missed TP1, 18.8% TP1 despite SL-first); AI replay is retroactively impossible (N4: `ai_signals` deletes SL/targets on close). Plus P1.2/P2.7/P2.31/P1.9. The AIM1 inversion is unaffected and robust regardless (sign + code causes documented).

## 3. Findings (consolidated)

Status: ✔ = proven/confirmed (Step 2/3) · ✘ = refuted/excluded · ~ = code finding, open

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| P0.13 | Bot+model | P0 | Source identity one-hots dead for almost all live signals: pkl only knows MSI1 typos/old conv names; live overlap 2/22 (`ATS1`,`EPD1`) resp. **0/5** conv → `reindex` zeroes everything, the meta-model cannot distinguish sources (its core job) → OOD | ✔✔ (Step 2+3) |
| — (Step 2) | Model | P0 | Calibration inverted: corr −0.304, conf>0.9 → 9.3% WR → pause the bot | ✔✔ |
| R13-AIM1-1 | Trainer | P0/P1 | `v2:398`: feature join via `dt.round('1h')` rounds UP → feature candle up to ~90 min into the future; live uses floor → learned directions flip | ✔ (Step 3) |
| R13-AIM1-2 (X-R1) | Trainer | P1 | Volatility label (+10%/72h before −7.5% SL): the most volatile candidates hit the SL first live → **a genuine, honestly learned inversion** | ✔ (Step 3, pkl-proven) |
| R13-AIM1-3 (X-R4) | Trainer | P1 | No calibration, `scale_pos_weight=2.105`, test set = early-stopping set | ✔ (Step 3) |
| P1.21 | Bot | P1 | Indicator features + close from the still-running hour (`open_time <= floor('h')`, features on 2–34 min of data); fix is one character (`<`) | ~ (proven live, R1) |
| R13-AIM1-4 ("F6", self-feedback) | Bot | P2 | History query without a `model_name` filter reads AIM1's **own shadow rows** as input signals → self-feedback loop | ✔ code-documented (Step 3 / Report 07) |
| P2.35 | Bot | P2 | 5-min candidate window without catch-up (comment says 30) + context features count the candidate itself + `conv_signal` dedup key collides across the active/closed tables | ~ |
| R07-AIM1-a | Bot | MEDIUM | Naive detector timestamps interpreted as UTC; naive `join_time` vs. timestamptz | ~ (proven live, R3) |
| R07-AIM1-b | Bot | LOW | Dup gate depends on monitor deletions, no age cap; conn outside try; model is never reloaded | ~ |
| R07-AIM1-c | Bot | P3 | In-code "FIX" comment doubly wrong (reindex can't shift; MIS1 rename can't revive MSI1 features) | ✔ |
| R13-AIM1-5 | Model | — | **Excluded** as causes: label inversion (1=win verified) and wrong predict_proba index (`classes_=[0,1]`, bot takes `[0][1]`) | ✘ |

## 4. Dependencies & cross-cutting risks

- **R1/R3 (proven in Step 2):** P1.21 (forming-hour features) and the TZ mix are live-relevant; AIM1 is among the bots with no forming-candle defense.
- **X-R1…X-R6:** AIM1 violates X-R1 (vol label), X-R2/X-R4 (threshold/no calibration), X-R6 (forming serving); plus, uniquely: dead identity vocabulary + join look-ahead.
- **Self-feedback (F6):** AIM1's shadow output flows back into its own inputs — every vocabulary/behaviour change of other bots AND of AIM1 itself shifts the feature distribution (the most fragile encoding imaginable: one-hots over freely named bot names, Report 16).
- **Whitelist/orchestrator:** Report 16 §7 — part of ROM1's added value (+8pp) is simply negative selection of the worst sources, AIM1 foremost; re-evaluate the gate statistics after the AIM1 pause.
- **Data hygiene:** AIM1 is the only active model with a dedup delta (3,125→3,047, Report 14 A.1) — pull training labels from `closed_ai_signals` only after purge (V2 in Report 15).

## 5. Remediation plan

**(a) Immediate, without retrain: PAUSE.** Stated literally in Report 13 (measure 1: "pause AIM1 — Step-2 proof: inversely predictive"), Report 14 (D.3 "stop/park: AIM1 (inverted + −3.4k net)") and Report 16 (§8 "stop: AIM1 (reliably inverted)"). No bot fix makes the model usable.

**(b) Retrain requirements = new project "AIM2" (Report 15, S7):** current vocabulary from DB DISTINCT (not hardcoded), **floor-1 join identical in trainer and serving**, label = first-touch of the real order geometry (V3 simulator), regime features from `regime_history` (not yet available in 2025 — the most obvious missing predictor), source calibration score as a feature, self-exclusion (no AIM1 input), chronological 3-way split, isotonic calibration, reindex-parity guard. Role: **ranker/sizer** over source signals, not a standalone trader. **Warning (Report 13):** retraining on just the vocabulary is NOT enough — without the label fix (X-R1) and floor join, an overconfident volatility model results again.

**(c) Open questions:** S5 "AIM1 fade" (invert signals 0.85–0.95, on paper ~+9.5%/trade) **only as a shadow experiment** — the inversion is an OOD artifact and conf>0.95 already wins 85%; more realistic use as a veto feature. AIM1-authored share in 5-day windows (feedback magnitude, Report 07 DB question 9) unmeasured; direction split never evaluated; the master gap (non-AIM1 rows without a processed entry) open.

## 6. Evidence

- `AUDIT_TODO.md` → P0.13 (✔✔ incl. pause instruction), P1.21, P2.35
- `audit_reports/07_ai_bots_b.md` → pkl feature extraction, self-feedback, window/dedup/TZ findings, wrong FIX comment
- `audit_reports/13_x_ml_trainers.md` → trainer `x10-mlzeitfolge-v2.py`, round-join look-ahead, vol label, verdict "actively harmful", exclusions, measure "pause"
- `audit_reports/14_bot_performance_db.md` → n=3,047, WR 50.8%, avg −1.02%, Σ −3,399; dedup 3,125→3,047; recommendation stop
- `audit_reports/STEP2_DB_VERIFICATION.md` → calibration inversion (corr −0.304; 0.9–1.0 → 9.3% WR, n=19,561), dummy overlap 2/22 resp. 0/5, WR 50.3% (n=3,125)
- `audit_reports/16_strategy_concept_evaluation.md` → grade F, concept analysis (architecture violates all stacking prerequisites)
- `audit_reports/15_strategy_proposals.md` → E5 figures, S5 AIM1 fade (shadow only), S7 AIM2 blueprint
- `audit_reports/17_monitor_replay_and_gaps.md` → monitor caveat, N4 (AI replay impossible)

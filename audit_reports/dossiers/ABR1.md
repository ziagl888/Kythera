# Dossier: ABR1 — AI Break & Retest (Bot 18)

> ML classification of continuation vs. failed breakout after a level retest. **Note C− (Report 16).** Core verdict: conceptually the second-best ML approach in the fleet and narrowly live-positive (+335, n=110) — but the model is **proven to run on only 7 of 18 features** (P0.12) and is trained without any out-of-sample evaluation; the class-inversion suspicion (P2.38) has been cleared.

## 1. Fact sheet

| Field | Content |
|---|---|
| Bot file | `18_ai_abr1_bot.py` (hourly, code runs at minute 2 — comment claims minute 10) |
| Artifacts | `bt2_model_LONG.json` / `bt2_model_SHORT.json` — XGBoost `multi:softprob`, `num_class=3`, 18 features; **byte-identical (md5)** to the `_X` artifacts from the trainer run on **31.12.2025** — NOT the better `BT2-ML-Final_Saver` run from 30.12. |
| Trainer | `legacy_trainers/BT2-Datagrepper-for-ML.py` (features/labels) + `BT2-ML-Trainer.py` (GridSearch training) + `BT2-Strategybacktester*.py` (threshold "backtests" on training data); provenance proved in Report 13 |
| Training date | 31.12.2025 |
| Data source | 1h DB data, **coin-concatenated and not time-sorted** → TimeSeriesSplit CV cuts coins instead of time |
| Label definition | 3 classes; "success" = **close-only after 12h from `lvl_price`** (no SL path, X-R1); live entry, however, is a retest close → optimistic. `SUCCESS_CLASS_IDX=0` triple-verified (LabelEncoder alphabetical, meta.json, num_class=3) |
| Features | 18 nominal — of which **11 constant at 0** (pandas_ta name mismatch: `KAMA_9` vs `KAMA_9_2_30`, `TSI_12_7` vs `TSI_7_12_7`, `DCL_20` vs `DCL_20_20`, `BBL_20_2` vs `BBL_20_2.0_2.0` → NaN → `fillna(0)`); dead: dist_close_kama9, tsi×4, boll×3, donchian×3 → **7 features in reality** |
| Thresholds | live hardcoded **0.60 (LONG) / 0.80 (SHORT)** — from "backtests" on the training data; trainer optimum 0.77/0.92, Final_Saver meta 0.79/0.86 → **three contradictory values** |
| Positive (Report 07) | the only one of the three bots with correct candle discipline (forming candle excluded), `autocommit=True`, DB-backed 4h cooldown |
| Channel | not documented in the sources |

## 2. Live balance (as of 2026-07-03, active era, deduplicated)

- **n = 110 · WR 63.6% · avg +3.15%/trade · median 0.00 · Σ net +335 price-%** (Report 14: "small; model has only 7 features in reality")
- **Direction split (Step 2): LONG 67.2% / SHORT 59.2% WR** → no class inversion, `SUCCESS_CLASS_IDX=0` consistent (**P2.38 cleared**, matches commit d19a68d)
- Calibration: no robust measurement (Step 2 table "—", n too small); the most honest training metric: **CV-F1(success) = 0.134 ≈ noise** (Final_Saver meta)
- Report 16 assessment: the small gain plausibly comes from the setup + S/R construction, not model skill[^1]

[^1]: **Monitor caveat (Report 17):** all figures are monitor-generated; first-touch replay only 63.4% agreement (17.8% missed TP1, 18.8% TP1 despite SL-first); AI replay is retroactively impossible (N4). Plus P1.2/P2.7/P2.31/P1.9. At n=110 the per-trade uncertainty weighs especially heavily.

## 3. Findings (consolidated)

Status: ✔ = proven/confirmed (Step 2/3) · ✘ = refuted/cleared · ~ = code finding, open

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| P0.12 (=P1.28) | Bot+trainer | P0 | 11/18 features constant at 0 — `expected_pta_cols` never matches the pandas_ta names; split-count proof: exactly these 11 features have 0 splits in both live models; the trainer (`BT2-Datagrepper-for-ML.py:77-92`) had the identical bug → no skew, but half the strategy signal is missing | ✔✔ (Step 3, booster dump) |
| R13-ABR1-1 (X-R2) | Trainer | P0 | Threshold + win rate chosen entirely **in-sample** on the refitted GridSearch model; no hold-out anywhere in the script | ✔ (Step 3) |
| R13-ABR1-2 (X-R3) | Trainer | P1 | TimeSeriesSplit on coin-concatenated, non-time-sorted data → CV cuts coins, not time | ✔ (Step 3) |
| R13-ABR1-3 (X-R1) | Trainer | P1 | Label close-only 12h after the level price vs. live retest-close entry → optimistic; the bot also uses unconfirmed edge pivots that never occurred in training | ✔ (Step 3) |
| R13-ABR1-4 | Trainer | P1 | Threshold chaos: live 0.60/0.80 vs. trainer optimum 0.77/0.92 vs. Final_Saver meta 0.79/0.86 — origin "backtests on training data" | ✔ (Step 3) |
| P2.38 | Model | P2 | SUCCESS_CLASS_IDX/SHORT label semantics suspicion (0↔1 swap, "bot shorts when the model says rising") | ✘ cleared (Step 2: LONG 67.2%/SHORT 59.2%; Step 3: LabelEncoder+meta.json+num_class=3) |
| R07-ABR1-a | Bot | MEDIUM | Signal price stale by up to 3h: entry1/"CMP entry" = close of a retest candle up to 3 candles old | ~ |
| R07-ABR1-b | Bot | MEDIUM | Edge-padded pivot detection (`np.pad 'edge'` + greater_equal) → unconfirmed, repainting levels at the right edge | ~ |
| R13-ABR1-5 | Bot | P2 | `SUCCESS_CLASS_IDX` + thresholds hardcoded instead of loaded from meta.json (no load assert) | ~ |
| R07-ABR1-c | Bot | LOW | Scheduling comment says minute 10, code says minute 2 — collides with the indicator-engine burst | ~ |

## 4. Dependencies & cross-cutting risks

- **R1 (forming candle):** ABR1 is the **only** one of the three AI bots (14/15/18) that defends correctly (`open_time < current_hour_utc`) — R1 risk is closed here, but still relevant for training data drawn from the same tables.
- **R3 (TZ):** no ABR1-specific TZ findings in the sources; the session-TZ issue (Europe/Bucharest) applies system-wide.
- **X-R1/X-R2/X-R3/X-R4/X-R5:** all violated — label without an SL path, in-sample threshold, coin-split leakage, uncalibrated "confidence %", silent default (`fillna(0)` hid the 11-features bug across 3 stages).
- **Silent-feature-death pattern (Report 07):** shared with AIM1/ATB1; a shared startup assertion "no feature constant" catches the class.
- **Artifact governance:** the trainer was not in the repo (now `legacy_trainers/`); the 31.12. run was deployed instead of the better Final_Saver run from 30.12.; class mapping/features belong IN the artifacts (meta.json).
- **Whitelist/orchestrator:** gate statistics are WR-based and monitor-distorted (Report 16 §7 / Report 17) — especially noise-prone at n=110.

## 5. Remediation plan

**(a) Immediate, without retrain:** continued operation is defensible (net positive, no inversion, clean wiring), but: stop communicating confidence as "%" (Report 13, measure 4); startup assertion "no feature constant"; load `SUCCESS_CLASS_IDX`/thresholds from meta.json + load assert; fix entry staleness (last price for entry1, or only signal when the retest is the most recently closed candle); pivot confirmation (`index <= len-PIVOT_WINDOW`).

**(b) Retrain requirements (mandatory for P0.12 — "RETRAIN both models"):** pta prefix-matching fix (template `14:197-211`) in both the bot AND the datagrepper; retrain with all 18 features; chronological 3-way split with embargo (time-sorted, not coin-concatenated); label = first-touch of the real geometry **from the retest close** (V3 simulator, P0.10); threshold on validation; isotonic calibration; meta.json (features, class mapping, threshold, period, hash) in the artifact. Priority in the retrain programme (Report 16 §8): **#4** after MIS1-72H, TD, SRA1; Report 13: "ABR1 (pta fix is a prerequisite)" right after MIS1/AIM1. Prerequisite: R1 fix + dedup purge (V1/V2).

**(c) Open questions:** n=110 too small for robust WR/calibration claims — repeat the outcome-vs-confidence join LONG/SHORT at larger n (was the "decisive test", result so far only cleared at the direction level); why was the worse 31.12. run deployed instead of Final_Saver (30.12.)?; channel undocumented.

## 6. Evidence

- `AUDIT_TODO.md` → P0.12 (✔✔ incl. trainer proof), P1.28 reference, P2.38 (✔✔ cleared)
- `audit_reports/07_ai_bots_b.md` → 11/18 proof (~35k splits traversed), stale entry, edge pivots, scheduling, positives (closed candle, autocommit, cooldown)
- `audit_reports/13_x_ml_trainers.md` → provenance (byte-identical, 31.12.2025, not Final_Saver), in-sample threshold, CV-F1 0.134, coin split, threshold chaos, SUCCESS_CLASS_IDX verification
- `audit_reports/14_bot_performance_db.md` → n=110, WR 63.6%, avg +3.15%, Σ +335 net
- `audit_reports/STEP2_DB_VERIFICATION.md` → P2.38 cleared (LONG 67.2%/SHORT 59.2%)
- `audit_reports/16_strategy_concept_evaluation.md` → Note C−, rescue path (pta fix + retrain with 18 features)
- `audit_reports/17_monitor_replay_and_gaps.md` → monitor caveat (63.4%, N4)

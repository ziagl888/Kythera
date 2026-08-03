# Dossier: ATB1 — Trendline Break/Bounce (Bot 14)

> ML-scored trendline events on 538 coins. **Note D (Report 16).** Core verdict: "The model never saw the event it scores" — the trainer labels a different mathematical object than the bot trades; live net negative (−172). Rebuild from scratch or park.

## 1. Fact sheet

| Field | Content |
|---|---|
| Bot file | `14_ai_atb_bot.py` (hourly scan over 538 coins, minute 3) |
| Artifacts | `long_trend_prediction_model.joblib` / `short_trend_prediction_model.joblib` — XGBClassifier `binary:logistic`, 19 features (pickle-verified: `feature_names` == `features_dict` of the bot) |
| Trainer | `legacy_trainers/BT1-Datagrepper-for-ml.py` (data/labels) + `BT1-ML-Trainer_Optimized.py` (training) + `BT1-Thresholdoptimizing_V2.py` (thresholds). `BT1-ML-Trainer.py` is dead (20 features, would crash) — provenance clarified in Report 13 |
| Training date | not documented (no meta.json in the artifact) |
| Data source | 1h candles/indicators from the live DB; coin universe = today's `coins.json` (survivorship bias, Report 13) |
| Label definition | +10% **touch** within 72h, **without an SL path** — events are crossings of the **90d close regression line**, not the trendlines traded live |
| Live event | pivot-chart trendlines (`find_peaks`, R≥0.2 — extremely lax) with a 4-event state machine (break/bounce up/down); bounce events have **no training counterpart**; break and bounce are indistinguishable for the model (event is not a feature) |
| Thresholds | 0.80 / 0.75 — from `BT1-Thresholdoptimizing_V2.py:48,96-103`, maximised on the (reconstructed) **test set** (maximum-statistic artifact, X-R2) |
| Channel | not documented in the sources |

## 2. Live P&L (as of 2026-07-03, active era 24.02.–03.07., deduplicated)

- **n = 306 · WR 65.7% · avg −0.46%/trade · median 0.00 · Σ net −172 price-%** (Report 14: "negative, consistent with the Report 13 verdict")
- Direction split: not reported for ATB1 (Report 14 lists asymmetries only for EPD1/RUB1/BR1H)
- Calibration: no value in the Step 2 measurement ("—") — no evidence that confidence carries information; Report 16: "The ML gate is de facto a random filter"
- 65.7% "WR" with negative net = textbook example of the cross-cutting finding "win (TP1 touch) ≠ profit"
- Portfolio classification: Report 14 D.3 "Stop/park: … ATB1"; Report 16 §8 "Park"[^1]

[^1]: **Monitor caveat (Report 17):** All live numbers are monitor-generated. The first-touch replay (classic sample, n=388) matches the monitor scoring only **63.4%** of the time (17.8% missed TP1, 18.8% TP1 despite SL-first); for the AI fleet, a retroactive replay is impossible (N4: `ai_signals` deletes SL/targets on close). Plus P1.2 (trailing SL never tightens), P2.7, P2.31, P1.9 — per-trade truth unreliable, net bias moderate.

## 3. Findings (consolidated)

Status: ✔ = proven/confirmed (Step 2/3) · ✘ = refuted · ~ = code finding, open/partially confirmed

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| R13-ATB1-1 (X-R1/P0.10) | Trainer | P0 | Event mismatch: trainer labels crossings of the 90d close regression line, live trades pivot trendlines — the model scores an event population it never saw | ✔ (Step 3) |
| R13-ATB1-2 | Bot+Trainer | P0 | `vol_ratio` skew derived: live ≈1/19 of the training scale (3-min forming candle ÷ rolling-20) — matches the audit observation of ~1/20; no training data for the live value range | ✔ (Step 3) |
| P1.22 | Bot | P1 | ML features on a 3-min-old forming candle (`row=df.iloc[-1]` not sliced, unlike ABR1); RSI/MACD/TSI/BB/DC on partial close | ~ (R1 proven live, bot fix open) |
| P1.23 | Bot | P1 | Aborted transaction poisons the rest of the 538-coin scan (no rollback in the per-coin except, not autocommit) | ~ |
| R13-ATB1-3 (X-R3) | Trainer | P1 | Random `train_test_split` over 72h-**overlapping** windows (`BT1-ML-Trainer_Optimized.py:46`) → twin leakage | ✔ (Step 3) |
| R13-ATB1-4 (X-R2) | Trainer | P1 | Live thresholds 0.80/0.75 maximised on the test set (`BT1-Thresholdoptimizing_V2.py`) | ✔ (Step 3) |
| R13-ATB1-5 (X-R1) | Trainer | P1 | Label +10% touch/72h **without SL** vs. live SL down to −8.8% — confidence estimates a quantity that is never traded | ✔ (Step 3) |
| P2.36 | Bot | P2 | "unknown" state break trigger deliberately reactivated (comment: "BUG FROM YOUR OLD BOT ACTIVE AGAIN") → state loss = mass event flood, stale breaks >0.80 post real signals | ~ |
| P2.37 | Bot | P2 | Main loop only catches KeyboardInterrupt → every scan exception kills the process + leaks the connection; plus a naive `last_alert` TypeError (TZ, R3) | ~ |
| R13-ATB1-6 | Trainer | P2 | `make_scorer(roc_auc_score)` on hard labels (GridSearch optimises the wrong thing); survivorship via today's coins.json; live `fillna(0)` produces values never seen in training (X-R5) | ✔ (Step 3) |
| R07-ATB1-a | Bot | LOW | Live pandas_ta recompute + `fillna(0)` as a documented train/serve drift risk (parity not falsifiable, names match) | ~ |
| R07-ATB1-b | Bot | LOW | "loaded successfully" is logged even when no model file exists → silent info-only degrade | ~ |
| R07-ATB1-c | Bot | LOW | Hourly N+1: 538×95d reads + a 150dpi 22×15in chart per event; CREATE TABLE in the event path | ~ |

## 4. Dependencies & cross-cutting risks

- **R1 (forming candle, proven in Step 2):** ATB1 is one of two of three AI bots (14/15) **without** forming-candle protection — contamination is architectural, backtests on the same tables see final candles → live/backtest divergence is built in.
- **R3 (TZ mix, proven in Step 2):** naive `last_alert` comparison (in the P2.37 area).
- **X-R1…X-R6 (Report 13):** ATB1 violates all six — label≠geometry, test-set threshold, split leakage, uncalibrated "confidence %", silent default (`fillna(0)`), forming-candle serving.
- **Silent-feature-death pattern (Report 07):** shared with ABR1/AIM1; a shared "assert no feature is constant" helper catches all three.
- **Model staleness:** loaded once at start, no hot reload (Report 07, cross-cutting #4); `ml_predictions_master.trade_id` always 0 (dead link); chart lifecycle risk (housekeeping deletes charts >2h, the outbox references them).
- **Whitelist/orchestrator:** gating statistics are based on the misleading WR metric and monitor-skewed outcomes (Report 16 §7, Report 17) — ATB1's evaluations in the gate inherit this too.

## 5. Remediation plan

**(a) Immediately, without retraining:** **Park it** (Report 14 D.3, Report 16 §8 — the model is a random filter with a negative net). If continued operation is forced: stop communicating confidence as a "%" (Report 13, measure 4), `rollback` in the per-coin except (P1.23), "unknown" state to observe-only (P2.36), broad except+backoff in the main loop (P2.37).

**(b) Retrain requirements:** not a fix, but a **rebuild from zero** (Report 16): fix the event definition and label on **live events** (bounce events included, event type as a feature). Prerequisites: R1 fix first, a shared walk-forward first-touch simulator (P0.10/V3) as the label source, a chronological 3-way split with embargo + episode dedup, threshold set on validation, isotonic calibration out-of-time, meta.json (features/threshold/period/hash) in the artifact, startup assertion (Report 13, retraining scaffold). Report 13's ordering recommendation: ATB1 after MIS1/AIM1/ABR1.

**(c) Open questions:** channel + training period undocumented; direction split never evaluated; `trendmeet_rawdata` event bursts around restarts (Report 07, DB question 8) unchecked; the monitor rewrite (Report 17) must come before any new labelling run.

## 6. Evidence

- `AUDIT_TODO.md` → P1.22/P1.23/P2.36/P2.37 (bot findings), R1/R3/R4 context, P0.10 pattern
- `audit_reports/07_ai_bots_b.md` → forming-candle/vol_ratio detail, robustness/LOW findings, pickle verification, cross-cutting notes
- `audit_reports/13_x_ml_trainers.md` → trainer provenance (BT1 chain), verdict "not trustworthy", event mismatch, X-R1..R6
- `audit_reports/14_bot_performance_db.md` → n=306, WR 65.7%, avg −0.46%, Σ −172; recommendation stop/park
- `audit_reports/STEP2_DB_VERIFICATION.md` → R1/R3 proven live; ATB1 without a calibration value
- `audit_reports/16_strategy_concept_evaluation.md` → Note D, concept critique ("random filter", rebuild or park)
- `audit_reports/17_monitor_replay_and_gaps.md` → monitor caveat (63.4% match, N4)

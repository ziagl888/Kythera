# Dossier: TD / BB — SMC-ML-Sniper

> One bot, two ML families: Three-Drive (TD = RSI divergence at three extrema) and Breaker Block (BB = break-and-retest with ML gate, *not* Bollinger). **Note (16): TD B− · BB_4H C− · BB_1H D.** Core verdict: TD is the only well-calibrated ML family in the fleet and a clear keep candidate despite formally worthless training; BB_4H justifies the pipeline repair, BB_1H does not survive fees + noise → park.

## 1. Fact sheet

| | |
|---|---|
| Bot | `25_smc_ml_sniper.py` |
| Models | `td_xgboost_model_1h.pkl` / `td_xgboost_model_4h.pkl` + `bb_xgboost_model_1h.pkl` / `bb_xgboost_model_4h.pkl` |
| Trainer | `smc_ml_trainer.py` — **present in the repo** |
| Signals/TF | TD_1H, TD_4H, BB_1H, BB_4H |
| Channel | own Cornix channel; plain Cornix block + second HTML message with identical block (P3.9 double-parse risk); `send_cornix_signal` never commits (works only via upstream autocommit) |
| Leverage | not quantified in the sources; no R4 finding against 25 |
| Thresholds | hardcoded 0.30 (TD) / 0.40 (BB) for both TFs — ignore the `optimal_threshold` stored in the pkl |

**BR mapping (verified):** the Break-&-Retest tags **BR1H/BR2H/BR4H (+BR1D in the Step 2 figures) do NOT come from Bot 25**, but from the **pattern detector `7_pattern_detector.py`** — break-and-retest *without* an ML gate (tag clarification in Report 16, section 6, "verified in code"). The BR family is therefore covered in the `IP_Pattern.md` dossier. The comparison BB_4H (+ML, +565) vs. the BR family (without ML, −4,106) is, per Report 16, "the best in-vivo argument in the repo that an ML gate over raw break-and-retest signals creates value".

## 2. Live balance (active era 24.02.–03.07., deduplicated, Report 14)

| Tag | n | WR | avg PnL | Median | Σ net |
|---|---|---|---|---|---|
| TD_1H + TD_4H | 2,794 | 57.3% | ~+1.0% | ≈0 | **+2,387** (split per analysis: TD_1H +1,764 / TD_4H +623) |
| BB_4H | 2,162 | 61.2% | +0.36% | −0.05 | **+565** |
| BB_1H | 3,909 | 55.7% | −0.18% | −0.17 | **−1,089** |

- **Calibration (Step 2):** TD_1H (n=2,202, WR 57.2%) **positively calibrated — 78.5% WR @ conf>0.9**, the best-calibrated model in the fleet. **BB: flat** to negative → the BB probabilities are noise (consistent with the breakout-vs-retest feature skew).
- The BB/BR family was strongly negative Mar–Apr, positive from May onward (mini-n, regime gating now filters it out almost entirely) — regime drift visible.
- Caveat (Report 17): monitor-generated, only 63.4% replay agreement (P1.2/P2.7).

## 3. Findings

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| P0.10 | Trainer↔bot | P0 | TD labels on look-ahead entry (pivot close, only known 10 bars later); live fires 11–12 bars after p3 at CMP via `calculate_smart_targets` — labels measure a physically impossible trade | ✔ (code; pattern confirmed Step 3) |
| 11-CRIT | Trainer↔bot | CRITICAL | SL/TP geometry in training (BB fixed 1%/2%, TD 2R from pivot) ≠ live geometry (`calculate_smart_targets`: ATR/S&R/Fib ladder) → `predict_proba` + threshold sweep for outcomes that were never executed | ✔ (code) |
| P1.25 | Bot | HIGH | Trainer entry is hindsight, live geometry entirely different; thresholds hardcoded instead of read from pkl; trainer `p3−p1 ≤ 100` vs. live `MAX_TD_SPAN=50` | ✔ (code) |
| 11-HIGH | Trainer↔bot | HIGH | BB feature skew: features trained on the *breakout* candle (RSI ~65–75), inference on the *retest* candle (RSI ~45–55) → tree splits route retest rows into arbitrary leaves; plus population skew (trainer trades every peak, bot filters) | ✔ (code; flat BB calibration supports it) |
| P1.29 | Trainer | HIGH | Random split on time series + duplicate contamination; threshold chosen on the test set | ✔ (code) |
| P2.39 | Bot | MEDIUM | Breaker Block only checks `peak_idx[-2]` — on a fresh retest the post-breakout high is not yet confirmed as a peak → usually the wrong level; "massive violation" check commented out | ✔ (code) |
| P1.31 | Trainer | HIGH | Silent exception + pool leak → silently truncated coin universe, possible overwrite of the production pkl (`smc_ml_trainer.py:63-90`) | ✘/~ (Step 2: currently not triggered; code bug remains) |
| 11-MED | Trainer | MEDIUM | Unresolved trades labelled as losses (outcome=0 default; the QM trainer does it correctly); retest/entry candle excluded from the outcome scan (fill candle cannot lose); fees declared, never applied (with BB's 1% SL, round-trip fees are 8–15% of an R); `bfill()` leak | ✔ (code) |
| P3.7 | Bot | LOW | Per-coin exceptions logged at DEBUG → invisible | ✔ (code) |
| P3.8 | Bot | LOW | matplotlib without `Agg` → headless crash risk | ✔ (code) |
| P3.9 | Bot | LOW/[DB] | Cornix double-parse risk (plain + HTML in the same channel) | ~ (unverified) |

## 4. Dependencies & cross-cutting risks

- **R1 (forming candle):** 25 treats the last DB row as live — retrain only after the R1 fix.
- **X-R1** ("backtest the detector, trade something else"): TD/BB are the repo-internal prototype of the pattern Step 3 found in 7 of 8 _X families.
- **Monitor caveat (Report 17):** 63.4% scoring agreement → monitor rewrite needed before retraining (it supplies the labels).
- 24+25 together ~2,150 join queries every 3 min (08, cross-cutting); P2.31 (monitor scores up to 21 targets).
- **Report 15:** TD_1H is a candidate for **S4 "calibration-sized positions"** (position size ∝ calibrated probability, TD_1H@>0.9 = 78.5% WR).

## 5. Remediation plan

**Immediate:** park BB_1H (Note D, −1,089). Load thresholds from the pkl (P1.25). Fix the `peak_idx[-2]` level logic (P2.39). Exception logging to WARNING, `Agg` backend, commit in `send_cornix_signal`, check the double-parse issue.

**Retrain (priority #2 in Report 16's retrain programme, after MIS1-72H):** label the TD entry at the `p3+PIVOT_WINDOW` close, use the live SL/TP generator (`calculate_smart_targets`) as label geometry, chronological split (P0.10/P1.25/P1.29); extract BB features at the retest candle + mirror the bot filter in the trainer + fees into labelling (BB skew); fix `try/finally` in the trainer (P1.31). Expectation per Report 16: TD's calibration and selection sharpness plausibly improve further — "no A, because average edge is small and tail-driven".

**Open questions:** live leverage; Cornix double parse [DB]; re-evaluate BB_4H after the skew fix (C− → ?).

## 6. Evidence

- `AUDIT_TODO.md` P0.10, P1.25, P1.29, P1.31, P2.39, P3.7–P3.9
- `audit_reports/08_smc_bots.md` (section 25_smc_ml_sniper.py)
- `audit_reports/11_ml_backtest.md` (TD look-ahead, SL/TP geometry, BB skew, fees)
- `audit_reports/14_bot_performance_db.md` (table B; TD/BB rows)
- `audit_reports/STEP2_DB_VERIFICATION.md` (section D: TD_1H 78.5%@>0.9, BB flat)
- `audit_reports/15_strategy_proposals.md` (E4, S4)
- `audit_reports/16_strategy_concept_evaluation.md` (section 6 incl. BR→Bot 7 tag clarification)
- `audit_reports/17_monitor_replay_and_gaps.md` (monitor caveat)

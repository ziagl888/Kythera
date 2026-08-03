# Dossier: QM (Quasimodo)

> ML-filtered Quasimodo reversals (liquidity sweep + structure break, retest of the sweep zone). **Note (16): QM_1H D+ · QM_4H F.** Core verdict: 67.5% WR and still ≈ 0 net — the exit geometry (TP1 = half the distance, SL beyond the extreme) structurally gives it all back; stop QM_4H, QM_1H only with retraining + exit redesign, otherwise park.

## 1. Fact sheet

| | |
|---|---|
| Bot | `24_quasimodo_bot.py` |
| Models | `qm_xgboost_model_1h.pkl` / `qm_xgboost_model_4h.pkl` (+v2 variants; the v2 artifacts are not separately assessed in the sources) |
| Trainer | `qm_ml_trainer.py` — **present in the repo** (provenance given, unlike MIS1) |
| Signals/TF | QM_1H, QM_4H (signal tags in `closed_ai_signals`) |
| Channel | own Cornix channel; posts a plain Cornix block **and** a second HTML message with an identical block in the same channel (P3.9 double-parse risk) |
| Leverage | not quantified in the sources; no R4 finding against 24 |
| Peculiarity | the bot ignores the `optimal_threshold` stored in the pkl (hardcoded 0.65); live entry = CMP ±1% instead of limit@QML as in the trainer |

## 2. Live balance (active era 24.02.–03.07., deduplicated, Report 14)

| Tag | n | WR | avg PnL | Median | Σ net |
|---|---|---|---|---|---|
| QM_1H | 3,139 | 67.5% | +0.06% | −0.03 | **−139** |
| QM_4H | 556 | 54.9% | −0.40% | −0.29 | **−277** |

- **Calibration (Step 2):** QM_1H **slightly positive** — QM belongs, along with TD_1H, SRA1, MIS1-8H, to the few genuinely calibrated models (E4 in Report 15).
- Textbook example of "win ≠ profit": 67.5% TP1-touch WR with a net-negative sum.
- Caveat (Report 17): all figures are monitor-generated; the monitor agrees only 63.4% with a first-touch replay (P1.2/P2.7) — per-trade truth unreliable.

## 3. Findings

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| P1.24 | Bot | HIGH | Pivot detection on the forming candle without confirmation (`argrelextrema mode='clip'` lets edge pivots through) → repaint + training-serving skew; the trainer gates correctly (`p[0] ≤ curr_idx − PIVOT_WINDOW`) | ✔ (code) |
| P0.10 | Trainer↔bot | P0 | "Backtest the detector, trade something else": the trainer simulates a limit order at the QML, the bot trades CMP ±1% with different geometry → pkl probabilities apply to trades that were never executed | ✔ (pattern confirmed Step 3 in 7 of 8 families) |
| P1.29 | Trainer | HIGH | Random `train_test_split` on time series + overlapping duplicates = contamination; "optimal threshold" chosen on the test set → optimistic operating point in the pkl | ✔ (code) |
| P1.30 | Trainer | HIGH | Fill logic deletes guaranteed losers ("invalidated" instead of stopped-out) + awards same-candle TP wins → labels systematically flattered | ✔ (code) |
| P1.31 | Trainer | HIGH | Silent exception + pool leak → the trainer can silently run on a truncated coin universe and overwrite the production pkl | ✘/~ (Step 2: 0/529 coins without tables — currently not triggered; code bug remains) |
| 11-MED | Bot | MEDIUM | The bot ignores the per-TF `optimal_threshold` from the pkl (fixed 0.65) → still divergent after retrain | ✔ (code) |
| 11-MED | Trainer | MEDIUM | Trend dummy encoding data-dependent (`pd.get_dummies`) vs. the bot hardcoding 3 categories; missing features silently filled with 0, NaN trend → all dummies 0 | ✔ (code) |
| 11-MED | Trainer | MEDIUM | `bfill()` leaks future indicator values into early history; `qm_backtest` (ORDER_EXPIRY 100) vs. trainer (50) simulate different strategies, neither matches the bot | ✔ (code) |
| P3.7 | Bot | LOW | Per-coin exceptions logged at DEBUG → invisible (a systematic error = the bot scans "successfully" and posts nothing) | ✔ (code) |
| P3.8 | Bot | LOW | matplotlib without the `Agg` backend → headless crash risk | ✔ (code) |
| P3.9 | Bot | LOW/[DB] | Cornix double-parse risk: plain block + HTML message with an identical block in the same channel | ~ (unverified) |

## 4. Dependencies & cross-cutting risks

- **R1 (forming candle):** 24 treats the last DB row as finished — the core of P1.24; any retrain before the R1 fix trains on data that doesn't exist live.
- **X-R1** ("label ≠ traded geometry", Report 13/16): identical pattern to the _X trainers, here with the trainer in the repo (P0.10/P1.25 class).
- **Monitor caveat (Report 17):** the whitelist/performance statistics and future labels depend on monitor scoring (63.4% agreement) → monitor rewrite BEFORE retraining.
- P2.31: the monitor scores up to 21 targets, TP1–5 is what gets published → live statistics ≠ Cornix reality.

## 5. Remediation plan

**Immediate (no retrain):** stop QM_4H (Note F, −277). Park or closely monitor QM_1H. Exception logging to WARNING (P3.7), `Agg` backend (P3.8), double-parse check against Cornix (P3.9), load threshold from the pkl.

**Retrain (only after R1 fix + monitor rewrite + V3 simulator):** drop the forming candle + discard pivots with `index > len−1−PIVOT_WINDOW` (P1.24); chronological split with a purge gap, threshold on the validation slice (P1.29); fill-then-stop conservatively, no TP win on the entry candle (P1.30); label = first-touch of the bot's own CMP geometry (P0.10); `try/finally conn.close()` + abort on too few coins (P1.31). **Exit redesign** is a mandatory part — the positive calibration shows the features carry signal, but the TP/SL geometry gives it back.

**Open questions:** role/training status of the v2 pkls; Cornix double parse (P3.9 [DB]); live leverage.

## 6. Evidence

- `AUDIT_TODO.md` P0.10, P1.24, P1.29–P1.31, P3.7–P3.9
- `audit_reports/08_smc_bots.md` (section 24_quasimodo_bot.py)
- `audit_reports/11_ml_backtest.md` (QM fill logic, split, threshold, get_dummies)
- `audit_reports/14_bot_performance_db.md` (table B)
- `audit_reports/STEP2_DB_VERIFICATION.md` (section D, calibration)
- `audit_reports/16_strategy_concept_evaluation.md` (section 6, ranking #13/#23)
- `audit_reports/17_monitor_replay_and_gaps.md` (monitor caveat)

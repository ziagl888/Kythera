# 13 — ML trainer audit (`Documents\_X`) — Step 3

**Status:** 2026-07-03 · **Method:** 6 parallel reviews (trainer code in `_X` ↔ consuming live bot ↔ deployed artifacts), with empirical artifact introspection in the live venv (booster dumps, split counts, feature names, md5). Complements step 1 (`AUDIT_TODO.md`) and step 2 (`STEP2_DB_VERIFICATION.md`).

## Provenance overview (who built what)

| Live artifact | Bot | Trainer in `_X` | Provenance |
|---|---|---|---|
| `bt2_model_LONG/SHORT.json` | 18 ABR1 | `BT2-ML-Trainer.py` (31.12.2025) | ✔ **byte-identical** to the `_X` artifacts (md5); NOT the better `BT2-ML-Final_Saver` run from 30.12. |
| `model_tsi_long_robust.pkl` / `short` | 12 ATS1 | `X8-TSI-ML-V4.py` (long) / `V5.py` (short); data from `X8-TSI-EXPORT-V4/V5.py` | ✔ clarified |
| `long/short_trend_prediction_model.joblib` | 14 ATB1 | `BT1-ML-Trainer_Optimized.py` (+`BT1-Thresholdoptimizing_V2.py`) | ✔ clarified (`BT1-ML-Trainer.py` is dead: 20 features, would crash) |
| `long/short_reversion_model.joblib` | 13 RUB1 | `BT3-2-ml_trainer.py` (+`BT3-3-optimizer.py`) | ✔ clarified |
| `master_trade_model_xgboost_combined_signals.pkl` | 15 AIM1 | `x10-mlzeitfolge-v2.py` (`master_task.py` = only a loader prototype) | ✔ clarified |
| `trade_success_xgb_LONG/SHORT_v2.json` | 9 SRA1 | `X9-SR-ANALYZER-Schritt1.py` (v1) + `core/update_model.py` (conversion) | ✔ **proven: v2 = pure format conversion of v1** (booster comparison: all 100 trees bit-identical, 38 features, LONG+SHORT) |
| `pump_dump_model.pkl` | 10 EPD1 | `zzz.py` (`train_pump_dump_model`, ~L.7054-7242) | ~ trainer exists but is **commented out** (L.7033/7040) — artifact from an unknown run |
| `pump_model_{8,24,72,168}h_{pump,dump}_final.pkl` + `threshold_*` | 11 MIS1 | `X5-analyze_indicators_v8.py` (**found later**, see addendum) | ✔ clarified — the f-string filename fooled every literal search |

## Overall verdicts

| Family | Bot↔artifact wiring | Statistical trustworthiness |
|---|---|---|
| ABR1 | ✔ clean (feature order, class index 0 correct) | ✘ **not trustworthy** — 11/18 features constant 0 (proven), zero out-of-sample evaluation |
| ATS1 | ✔ features identical (29) | ~ usable as a rough ranking, not as a probability; short model unvalidated (`_1h_X` table) |
| ATB1 | ✔ formally (19 features) | ✘ **not trustworthy** — model scores an event population it never saw |
| RUB1 | ✔ formally (9 features) | ✘ **not trustworthy** — MACD semantic break + memorised split |
| AIM1 | ✔ formally, but reindex masks dead vocabulary | ✘✘ **actively harmful** (step 2: inverted; causes now proven in code) |
| SRA1 | ✔ clean (38 features exact) | ~ functional; label semantics unproven, 1h look-ahead in training |
| EPD1 | ✔ feature positions exact | ✘ model is live queried almost only out-of-distribution |
| MIS1 | ✔ technically consistent (67 features ×8 identical — **P1.18 refuted**) | ✘ ticker leakage via accidental features; no provenance |

## Recurring patterns (the actual root causes, analogous to section R of AUDIT_TODO)

- **X-R1 — label ≠ live geometry (7/8 families).** Trainers label touch/close targets without an SL path (ABR1: close after 12h from level price; ATB1/RUB1: +10% touch/72h without SL; ATS1: 2.5/1.5 bracket; AIM1: +10%/−7.5%), but the bot trades `calculate_smart_targets`/SR-SL/DCA entry2. The "confidence" estimates a quantity that's never actually traded. **Fix:** a shared first-touch simulator of the actually posted order geometry as the label source (== P0.10 fix).
- **X-R2 — threshold on used-up data (5/8).** ABR1 fully in-sample (P0), ATB1/RUB1/ATS1 maximised on the test set, AIM1 test set = early-stopping set. All live thresholds are maximum-statistic artefacts. **Fix:** chronological 3-way split + threshold only on validation.
- **X-R3 — split leakage via quasi-duplicates (6/8).** Persisting states/overlapping windows produce twin samples; random/unsorted splits spread them across train+test (RUB1 the worst, ABR1 CV on coin- instead of time-sorted, EPD1 10s tick duplicates). **Fix:** episode dedup + chronological split with embargo.
- **X-R4 — uncalibrated scores as "confidence %" (all).** `scale_pos_weight`/sample weights without recalibration; nowhere isotonic/Platt. **Fix:** calibration on an out-of-time slice, otherwise remove the confidence display.
- **X-R5 — silent-default antipattern.** Missing columns → NaN → `fillna(0)` (ABR1 bug invisible across 3 stages; ATS1 export; MIS1 line_cols). **Fix:** startup assertion "no feature constant" + raise instead of default.
- **X-R6 — serving on forming candle / OOD (step-1 R1 hits all ML bots).**

## Family findings (condensed; severity | file:line)

### ABR1 (BT2) — verdict: not trustworthy, but reproducible
- **P0** `BT2-Datagrepper-for-ML.py:77-92`: identical `expected_pta_cols` bug as the bot → **split-count proof in the live models: exactly the 11 predicted features have 0 splits** (dist_close_kama9, tsi×4, boll×3, donchian×3). The model effectively runs on 7 features.
- **P0** `BT2-ML-Trainer.py:110-162`: threshold+win rate chosen fully **in-sample** on the refitted GridSearch model; no hold-out anywhere in the script. The most honest number in the pipeline: CV-F1(success)=**0.134** (Final_Saver meta) ≈ noise.
- **P1** `BT2-ML-Trainer.py:70,101`: TimeSeriesSplit on **coin-concatenated, not time-sorted** data → CV splits coins, not time.
- **P1** Label: close-only after 12h from `lvl_price` (`:208-213,265-270`), but live entry is a retest close → optimistic. The bot also uses unconfirmed edge pivots (`18:145-149,242`) that never occurred in training.
- **P1** Threshold chaos: live 0.60/0.80 come from "backtests" **on the training data** (`BT2-Strategybacktester*.py:13-22`); trainer optimum 0.77/0.92, Final_Saver meta 0.79/0.86.
- **OK:** `SUCCESS_CLASS_IDX=0` triple-verified (LabelEncoder alphabetical, meta.json, num_class=3). The bot should still load index+threshold from meta.json instead of hardcoding (P2).

### ATS1 (X8-TSI) — verdict: limited; short unvalidated
- **P0** `X8-TSI-EXPORT-V4.py:83,203` vs `12:154-155,199-202`: `obv_val`/`obv_ratio` train/serve skew — training accumulates OBV over ~300 days, live a 500-candle window with normalisation that mathematically changes `obv_ratio` → the high-confidence region is live out-of-distribution. **Explains the measured calibration inversion** (0.6-0.7→71% vs 0.8-0.9→57%).
- **P0** Label geometry 2.5%/1.5%/96h ≠ live (SR targets ≥5%, DCA entry2, SR SL).
- **P1** `EXPORT-V4:272-275`: TP-before-SL on ambiguous candles → optimistic bias exactly in the high-vol samples.
- **P1** `X8-TSI-EXPORT-V5.py:32`: short model trained on **`{coin}_1h_X`** tables (a different source than live!).
- **P1** `X8-TSI-ML-V4.py:59,72`: `scale_pos_weight` without calibration.
- **P2** Threshold PF maximisation on the test set (`ML-V4:91-110`); training data ends 2025-12-15 (6.5 months stale). Positive: chronological split correct, feature list 29/29 identical.

### ATB1 (BT1) — verdict: not trustworthy
- **P0** Event mismatch: the trainer labels crossings of the **90d close regression line** (`BT1-Datagrepper-for-ml.py:204-232`), live trades **pivot chart trend lines** (find_peaks, R≥0.2) with a 4-event state machine including bounce events without a training counterpart (`14:120-145,597-607`). A different mathematical object; `slope_trend` comes from a different regression.
- **P0** `vol_ratio` skew derived: live ≈ 1/19 of the training scale (3-min forming candle ./. rolling-20 including forming) — matches the audit observation ~1/20; the model has no training data for the live value range.
- **P1** `BT1-ML-Trainer_Optimized.py:46`: random `train_test_split` over 72h-overlapping windows; **P1** `BT1-Thresholdoptimizing_V2.py:48,96-103`: live thresholds 0.80/0.75 maximised on the (reconstructed) test set.
- **P1** Label +10% touch/72h **without SL** vs live SL down to −8.8%.
- **P2** `make_scorer(roc_auc_score)` on hard labels (GridSearch optimises the wrong thing); survivorship via today's coins.json; live `fillna(0)` produces values never seen.

### RUB1 (BT3) — verdict: not trustworthy
- **P0** MACD semantic break: training `ta.macd(fast=9,slow=21)` (`BT3-1:85-87`), live feeds `macd_dif_normal_12_26_9` DB columns under the same feature name (`13:92-93,150-151`) — invisible to name validation.
- **P0** `BT3-2:34`: random split over hour-wise duplicated persistence episodes (reversion state persists for many hours) → test AUC = memorisation. Live trades via a 4h cooldown only the *first* episode hour — training averages over all of them.
- **P1** `BT3-3-optimizer.py:31`: thresholds 0.75/0.85 via precision maximisation on a mini test set (>5 trades!).
- **P1** Label without SL/drawdown (knife-catch unmodelled); **P1** live prediction on forming-candle indicators (LIMIT 1) + regression including the current candle (95d vs 2160 candles excl.).
- **P1** DB indicator parity (Wilder RSI? TSI scaling?) unverified — also affects the pre-filter gates (rsi<30, tsi<−15). Note: step 2 already proved that DB `rsi_14` ≠ Wilder (Δ≈4.8) — so the gate fires live in a different population than in training.

### AIM1 (x10-mlzeitfolge-v2) — verdict: actively harmful; causes of the inversion proven in code
- **P0** `v2:170-191` + `15:347`: identity vocabulary dead (MSI1 spelling, `Fast Bot` etc. from then-current DB values; today's names don't exist in the pkl); `reindex(fill_value=0)` discards silently. Identity block = **14.6% of total gain**, `conv_bot_nan` third-most-important feature; the live combination "conv_bot_nan=1 ∧ all ai_model_*=0" never existed in training. The fix comment in `15:121-128` is ineffective.
- **P0/P1** `v2:398`: feature join via `dt.round('1h')` — **rounds UP** → the join candle close (basis of all dist/ATR features) sits up to ~90 min in the signal's future; live uses floor → learned directions flip.
- **P1** Label +10%/72h ahead of a −7.5% SL rewards volatility; pkl proof: top gains `atr_21_pct_close` (137) + `atr_14_pct_close` (97) → **the model is a volatility detector**; live it's exactly the most volatile candidates that hit the real SL first → **genuine inversion** (step-2 measurement: conf>0.9 → 9.3% WR).
- **P1** No calibration, `scale_pos_weight=2.105`, test set = early-stopping set (`v2:544,605-612`).
- **P2** Live self-feedback: the history query `15:176-188` without a `model_name` filter reads AIM1s own shadow rows as signals; **P2** duplicate lock without a time window (`15:363-366`); **P2** three inconsistent confidence mappings (v2/master_task/15).
- **Ruled out:** label inversion (1=win verified) and wrong predict_proba index (classes_=[0,1], the bot takes [0][1]).
- **Important:** retraining on today's vocabulary alone is NOT enough — without the label fix (X-R1) and the floor join, an overconfident volatility model results again.

### SRA1 (X9) — verdict: functional, with an open label question
- **Provenance clarified (proven):** v2.json = format conversion of v1 via `core/update_model.py`; all 100 trees bit-identical. Conversion/training still not versioned (P1) → check in a 3-line script + meta.json.
- **P1** `9:108-114`: conditional ATR features → 35/38 columns → predict throws, batch rollback discards all shadow inserts, the crash repeats every 300s (covers P1.20).
- **P2 (clarify!)** `Schritt1:157`: `success = status in ['SL1','SL2','SL3','4']` — presumably "trailing SL after TPn = win"; if `SL1` means "SL before TP1" in `closed_trades3` semantics, the label is inverted. Verify against DB semantics.
- **P2** 1h look-ahead in the training join (`Schritt1:56-61`, the open_time-keyed candle contains up to +1h of future); **P2** median imputation over the whole dataset vs live raw NaN; **P2** legacy trainer `X9-SR-ANALYZER.py:244-246` random split (mark deprecated).
- Positive: step-1 split chronologically correct; feature parity 38/38 exact (JSON-verified).

### EPD1 (zzz.py) — verdict: model is queried live incorrectly
- **P0** Covariate shift: the trainer samples ONLY `volume_ratio ≥ 5` events (`zzz.py:7103-7104`), live scores **every 10s tick without a gate** (`10:519-565`) → almost all live queries out-of-distribution. Explains (together with B-4/B-6) the flat calibration; the 72.8% WR plausibly comes from the SR-based SL/TP construction, not model skill. **Fix (1 line):** mirror the spike gate before predict.
- **P1** `zzz.py:7033,7040-7041`: the daily training is **commented out, but the log still reports success** — artifact stale/unknown regime.
- **P1** `zzz.py:7178`: random split over 10s quasi-duplicates.
- **P2** Sample weights (pump/dump up to 3.0) without recalibration + `max(prob_pump,prob_dump)` logged as "confidence"; **P2** shadow flood without cooldown (`10:586-593`, ~360 pseudo-replicates/h/symbol) — second cause of corr≈0 in the step-2 measurements; **P2** `float(None)` crash on SQL NULL (`10:537`) kills the whole 10s cycle.
- Positive: feature positions 10/10 exact, class mapping via `classes.index()` correct.

### MIS1 (no trainer) — verdict: technically consistent, statistically not trustworthy, without provenance
- **P1.18 REFUTED:** all 8 models identical 67 `feature_names_in_` (order included); bot selection by name; live parity test on 3 symbols error-free. `classes_=[0,1]`, bot index correct. Thresholds all plausible, different per model, stored atomically with the models (26./27.01.2026).
- **P1** Accidental features: `pct_distance(close, X)` also ran over derived columns (`boll_*_dist_atr`, `ema_200_dist_atr`, `ema_9_cross_above_21`) → values in coin price scale (BTC −3.47e6 vs XRP −167). Trees actually split on this (168h_dump: 558 splits, thresholds up to ±5.9e5; 168h_pump: top feature 10.4% importance) → **ticker/price-class leakage**.
- **P2** 1000 trees without early stopping, identical hyperparameters for all 8 horizons; dead binary flags (`rsi_14_above_50` importance 0 in all 8); forming-candle prediction (`11:196`) actually effective (the running candle's indicator row exists, no NaN masking).
- **P3** 168h_pump threshold 0.2825 only 3 points above the 0.25 shadow floor (shadow band empty); fallback threshold 0.60 ≠ init default 0.5.
- **Sole family with zero reproducibility** → most urgent retraining candidate; minimum requirements below.

## Prioritised measures

**Immediately (possible without a retrain):**
1. Pause AIM1 (step-2 proof: inversely predictive) — until retraining after the X-R1/floor-join fix.
2. EPD1: `vol_ratio ≥ 5` gate before predict (1 line) + shadow cooldown + NULL guard (`10:537`).
3. SRA1: always emit the ATR keys + reindex guard (kills the 300s crash loop).
4. ATS1/ATB1/RUB1/ABR1/MIS1: stop communicating confidence as a "%"; set operating points conservatively from the step-2 calibration tables (e.g. ATS1 at the empirically best 0.6-0.7 bucket).
5. Clarify SRA1 label semantics (`SL1/SL2/SL3/4`) against `closed_trades3` status codes — inversion risk.

**Retraining programme (one shared scaffold instead of 8 ad-hoc trainers):**
- One versioned trainer per family in the repo that **imports the bot's feature builder** (one source instead of copy-paste) — structurally prevents the RUB1-MACD/ABR1-pta/MIS1-line_cols class.
- Label = first-touch simulation of the order geometry actually posted (X-R1, == AUDIT_TODO P0.10).
- Closed candles only (R1 fix first!), join on the last closed candle (floor−1) in trainer AND serving.
- Chronological 3-way split with embargo + episode dedup; threshold on validation; isotonic calibration out-of-time.
- Native artifacts (save_model JSON) + meta.json (feature list, class mapping, threshold, training period, data hash, git SHA); bots load threshold/class index from meta instead of hardcoding.
- Startup assertion in every bot: no feature constant, no non-null columns discarded by reindex.

**Order recommendation:** MIS1 (no provenance + ticker leakage) and AIM1 (actively harmful) first, then ABR1 (pta fix is a prerequisite), then ATB1/RUB1 (event/feature parity), then ATS1 (OBV fix), EPD1 (gate fix may suffice for now), SRA1 last (functionally the healthiest).

---

## Addendum (2026-07-03, later): MIS1 trainer FOUND — `X5-analyze_indicators_v8.py`

Re-scan of `D:\_BACKUP` + the whole user profile on request: the trainer was in `_X` the whole time,
but saves with an f-string (`f"pump_model_{name}_final.pkl"`, L.254-255) — every literal grep came up empty.
**Verification:** hyperparameters exactly identical to the pkl introspection (1000/4/0.02/spw1.5/gamma2/lambda10),
the feature build produces exactly the 67 features **including the F1 accidental features**: the `line_cols`
loop (L.69) runs after `boll_*_dist_atr`/`ema_200_dist_atr`/`ema_9_cross_above_21` are created and produces
their `_dist_pct` versions — the ticker leakage finding is thus confirmed at the source.

**Now-known label definitions:** close-to-close return over the horizon, thresholds ±5%/8h, ±10%/24h,
±15%/72h, ±25%/168h (L.153-161). No path/SL — pure future return (X-R1 applies here too).

**Trainer defects (short audit):**
- **P0:** `StratifiedKFold(shuffle=True)` (L.194) over hourly samples with 8-168h **overlapping**
  label windows → twin leakage; the reported precision values are strongly inflated.
- **P1:** threshold = best precision **maxed across the 5 folds** (L.240-243) → maximum-statistic;
  plus a recall floor of only 3%.
- **P1:** the final model is fitted on ALL data (L.252), but the threshold comes from the
  shuffle folds → the operating point doesn't match the deployed model.
- **P2:** no calibration (scale_pos_weight=1.5), `fillna(0)` cascade, training including forming-candle
  rows, a 400-day window with today's coins.json (survivorship).

**Consequence:** MIS1 is reproducible (retraining basis available — saved in `legacy_trainers/`),
and the verdicts stand: MIS1-72H's strong live performance arises DESPITE this methodology
(plausibly because the momentum/vol features on long horizons carry a real signal), not because of it.
Retraining per the scaffold above (chronological split, path label via first-touch simulator, line_cols fix,
calibration) remains priority #1 of the model remediation.

---

## Addendum 2 (2026-07-04): in-repo models QM/TD/BB verified via artifact introspection — provenance remainder list

The provenance table above only covered the `Documents\_X` families. The **in-repo models** (trainers `qm_ml_trainer.py`/`smc_ml_trainer.py` in the repo itself) were now re-checked via pickle introspection (fleet Python, xgboost 3.1.2):

| Artifact | Finding | Provenance |
|---|---|---|
| `qm_xgboost_model_1h/4h.pkl` | dict {model, features, optimal_threshold}; **20 features exactly = qm_ml_trainer schema** (rsi_14, tsi_25_13_13, macd_*_normal_12_26_9, *_dist_pct, trend dummies, dir_num); stored with xgb 3.1.2 (= installed version, no version skew) | ✔ clarified |
| `td_xgboost_model_1h/4h.pkl` · `bb_xgboost_model_1h/4h.pkl` | identical schema (20 features, smc_ml_trainer order), xgb 3.1.2 | ✔ clarified |
| `qm_xgboost_model_v2.pkl` | **orphan** — no bot loads it (`24:35` only loads `_1h/_4h`); stored threshold 0.1 | clean up/archive |
| `pump_dump_model.pkl` (EPD1) | bare XGBClassifier, 10 unnamed features, no metadata — **provenance closed via the D:\ backups instead (see below)** | ✔ clarified (addendum 3) |

**Threshold drift (new, concrete):** the pkls store `optimal_threshold`, the bots deviate: the QM bot hardcodes **0.65** vs. the stored **0.30** (deliberate per the FIX comment `24:37`, but undocumented against the artifact); Sniper BB hardcodes **0.40** vs. stored **0.35** (`25:42`); TD 0.30 = 0.30 ✔. Recommendation: load thresholds from the pkl and treat deviations as an explicit override with a stated reason.

**Remaining provenance list (complete):**
1. **Systemic metadata gap:** not a single artifact (even the clarified ones) carries meta.json/training date/data window/git hash — provenance everywhere is only *reconstructed*, never *declared*. Fix = X-R5/P3.4: `{model, features, thresholds, xgb_version, trained_at, data_window, trainer_git_hash}` as a mandatory format on the next retrain.
2. **Known remaining points from the main table:** ABR1 deploys the 31.12. run instead of the better `BT2-ML-Final_Saver` run from 30.12. (deliberate?); ATS1 short trained on `_1h_X` tables (data source unvalidated); SRA1 label semantics (`SL1/SL2/SL3/4`) still unproven.

---

## Addendum 3 (2026-07-04): EPD1 provenance CLOSED via D:\ backups

Reconstruction via the backup series `D:\_BACKUP\` (zips 2025-11-07 … 2026-04-01) + `Documents\_X\zzz-sicherung.py`:

1. **Training time proven:** `pump_dump_model.pkl` carries the identical timestamp **2026-01-22 22:22** in **all** backups from 2026-03-06 onward and is **md5-identical** to the deployed repo artifact (`6c09741a…`) — the live model is the 22.01.2026 run, unchanged since.
2. **Trainer lineage proven:** `zzz.py` from 12.12.2025 (backup) contains **no** pump-dump training yet; `Documents\_X\zzz-sicherung.py` from **22.12.2025** contains `train_pump_dump_model()` with **active** calls (L. 4785/4792) and `joblib.dump("pump_dump_model.pkl")` (L. 5218); in `zzz.py` from 26.02.2026 (current state) the calls are commented out. Timeline: feature built ~22.12.2025 → ran until at least 22.01.2026 → deactivated before 26.02.2026.
3. **Functional identity proven:** diff of the training function (zzz-sicherung 5037-5218 vs. zzz.py 7054-7242) = **8 lines, all at the end of the function** (logging/except/finally) — the commented-out code in today's zzz.py IS the code that built the deployed model.
4. **Report-13 core finding confirmed in the run version:** the training sampling gate `if volume_ratio < 5.0: continue` (zzz-sicherung L. 50) and the 10-feature list (starting `volume_ratio, price_change_60s, buy_pressure, volatility, …`, L. 104-116/172) stand exactly like this in the version that ran on 22.01. — the covariate shift (live every 10s tick is scored without a gate) is thereby finally proven, as is the random split (`train_test_split(random_state=42, stratify=y)`, L. 125).

**Consequence:** all 9 model families now have clarified provenance. For EPD1 that concretely means: the model is 5.5 months stale (trained 22.01. on data before that), the daily retraining was deliberately disabled (the log keeps reporting success — P1 remains), and the gate fix (`vol_ratio ≥ 5` before `predict`, report 16 recommendation) now has a proven code basis.

# Agent 7: AI Bots 14/15/18 (ATB1, AIM1 Master, ABR1) — artifact-level verified

## 18_ai_abr1_bot.py (ABR1)
### [HIGH] [ml-correctness] 11 of 18 model features are permanently zero — pandas_ta column names never match (trainer had same bug)
- 18:112-177 (esp 120-133). expected_pta_cols expects KAMA_9/TSI_12_7/BBL_20_2/DCL_20; pandas_ta produces KAMA_9_2_30, TSI_7_12_7, BBL_20_2.0_2.0, DCL_20_20 → all missing → NaN → fillna(0). Dead: dist_close_kama9_pct, tsi(+signal+2 flags), 3× boll dist, 3× donchian dist = 11/18.
- PROVEN: traversed bt2_model_LONG/SHORT.json ~35k splits each — exactly those 11 features have 0 splits. Model trades on only 7 features. Trainer had identical bug → no skew but half the intended strategy signal doesn't exist.
- Fix: prefix-match columns (like 14:197-211), RETRAIN both models, startup assert no feature constant.

### [MEDIUM] [ml-correctness] SUCCESS_CLASS_IDX=0 comment claims verification against a trainer that doesn't exist on machine; 0↔1 swap + SHORT-label semantics unverifiable
- 18:41-54, 367. Verified: multi:softprob num_class=3, 18 features — so commit d19a68d right that OLD comment was wrong. Class ordering (alphabetical: continuation_success=0) plausible, weakly supported (class 2 = majority prior). BT2-ML-Trainer.py not anywhere on C:\_BOT. If SHORT dataset labeled RAW price change → class 0 = "price rose +5%" → bot shorts when model predicts up. Strict SHORT threshold 0.80 consistent with either.
- Fix: persist LabelEncoder.classes_ in model, assert at load. DB-phase: outcome-vs-confidence join LONG/SHORT separately = decisive test.

### [MEDIUM] [bug] Signal price up to 3h stale — entry1/"CMP Entry" is close of a retest candle up to 3 closed candles back (18:303-305,374). Fix: last price for entry1 or only signal when retest is most recent closed candle.
### [MEDIUM] [ml-correctness] Edge-padded pivot detection (np.pad 'edge' + greater_equal) → unconfirmed repainting levels at right edge (18:184-188). Fix: index <= len-PIVOT_WINDOW.
### [LOW] Scheduling comment says minute 10, code runs minute 2 (collides with indicator engine burst) (18:387-388).
- POSITIVE 18: autocommit=True; forming candle correctly excluded (df[open_time < current_hour_utc]); DB-backed 4h cooldown.

## 15_ai_master_bot.py (AIM1 Master)
### [HIGH] [ml-correctness] Source-identity one-hot features DEAD for nearly all live signals — trained dummy names don't match any current bot names
- 15:220-273, 485. Extracted REQUIRED_FEATURES from master pkl: only ai_model_ATS1/EPD1/MSI1-{8h..168h}_{pump,dump} (typo MSI1, lowercase h, _pump/_dump), ai_model_nan, conv_bot_{5% Bot,Fast Bot,SR Bot,Volume Bot,nan}. Live writes MIS1-24H (uppercase, stripped), ATB1, BB_1H, conv names "Fast In And Out"/"5 Percent"/etc. NONE map → reindex → all identity dummies 0. In-code FIX comment wrong twice (reindex can't shift; MIS1 rename can't resurrect MSI1 features).
- PROVEN: pkl string extraction offsets 1884001-1884326. Meta-model can't distinguish sources (its core job) → OOD extrapolation.
- Fix: retrain on current vocabulary; log dropped dummy columns. DB-phase: DISTINCT model_name/strategy = live vocabulary.

### [HIGH] [ml-correctness] Indicator features + close from still-forming hour candle (15:391, 423-431). join_time=floor('h'), open_time<=join_time → forming candle. Features on 2-34 min of data. Fix: open_time < join_time (one char).
### [MEDIUM] [ml-correctness] Context features count the candidate itself; AIM1's own shadow output feeds back into inputs (15:303-316,443-467,597-604). latest_signal_age_hours≈0 always; hist_ai no model!='AIM1' filter → self-feedback loop. Fix: exclude candidate row, filter AIM1.
### [MEDIUM] [robustness] 5-min candidate window (comment says 30) with no catch-up — downtime/slow cycle drops signals forever (15:299,352-372). Fix: widen to 60 min, rely on processed-signals dedup.
### [MEDIUM] [data-integrity] conv_signal dedup key collides across active/closed tables with independent id sequences (15:321-331,364-372,588-595). Fix: distinct signal_types or business key.
### [MEDIUM] [bug] Naive local timestamps from detectors interpreted as UTC; naive join_time vs timestamptz (15:335,376,391; 3_detectors:54,117). Fix: UTC everywhere.
### [LOW] Dup-gate depends on monitor deletions, no age cap; conn outside try; model never reloaded (15:296,500-517,635-639).

## 14_ai_atb_bot.py (ATB1)
### [HIGH] [ml-correctness] ML features on 3-min-old forming candle — vol_ratio ~1/20 of training scale
- 14:228-233, 613-625, 789-794. Scan at minute 3; row=df.iloc[-1] NOT sliced (unlike ABR1). vol_ratio = 3-min volume / 20-full-hour avg ≈0.05 vs ~1.0; RSI/MACD/TSI/BB/DC on partial close. Models (verified pickle: XGBClassifier binary:logistic, feature_names match features_dict) presumably trained on closed candles.
- Fix: features on df_90d.iloc[:-1], keep last_close from forming for break geometry. Retraining check.

### [HIGH] [robustness] Aborted transaction poisons rest of 538-coin scan — per-coin except has no rollback (14:612-614,761-762). Not autocommit (unlike ABR1). One bad coin → coins after it all InFailedSqlTransaction until scan ends. Fix: rollback in except or autocommit=True.
### [MEDIUM] [robustness] Main loop catches only KeyboardInterrupt — any scan exception kills process + leaks conn; state access outside try; naive last_alert TypeError (14:593,605-610,764,789-799). Fix: try/finally close, broad except+backoff, normalize tz.
### [MEDIUM] [bug] "unknown"-state break trigger deliberately re-enabled — state loss ⇒ mass event flood (14:52-56,660-673,76-78). Comment admits "BUG AUS DEINEM ALTEN BOT WIEDER AKTIV". Corrupt/deleted state.json → hundreds of BREAK posts+charts, stale breaks passing 0.80 emit real signals. Fix: treat unknown as observe-only.
### [LOW] Live pandas_ta recompute + fillna(0) as acknowledged train/serve drift risk (14:255-263) — couldn't falsify parity (names match), documented risk.
### [LOW] "loaded successfully" logged even when no model file exists → silent info-only degrade (14:42,103-111).
### [LOW] [performance] Hourly N+1: 538×95d reads + 3 extra full reads + 150dpi 22×15in figure per event; CREATE TABLE in event path (14:274-288,613,683,701).

## Cross-cutting observations
1. Forming-candle contamination architectural; of the three only ABR1 defends. Backtests on these tables see final candles → live/backtest divergence baked in for 14 and 15.
2. Silent feature-death dominant ML failure mode: ABR1 (11/18 dead, proven), AIM1 (identity dummies dead, proven), ATB1 (fillna(0)). Shared "assert no feature constant / warn on filled" helper catches all.
3. Trainer scripts NOT in repo (no BT2/master trainer). "Verified against training code" comments unreproducible. Persist class mappings + feature defs INSIDE artifacts.
4. Model staleness: all load once at startup, no hot-reload. Master model predates most of current fleet (dummy vocabulary proves it).
5. SQL: table names f-stringed from coins.json + (bot 15) coin values from DB. Identifier whitelist regex.
6. Chart lifecycle: housekeeping deletes generated_charts/charts >2h; outbox references them → backlog >2h = dangling image_path.
7. ml_predictions_master.trade_id always 0 across writers — dead column / lost linkage.

## Questions for live-DB phase
1. SHOW timezone + VPS OS tz.
2. DISTINCT model_name / strategy vs master model dummies (expected near-zero overlap).
3. ABR1 outcome calibration: closed_ai_signals win/loss vs confidence, LONG vs SHORT separately (decisive SUCCESS_CLASS_IDX test).
4. Do _1h / _1h_indicators contain current open hour row now?
5. Age distribution of open AIM1 (and per-model) ai_signals rows.
6. Master gap: non-AIM1 rows with no master_ai_processed_signals entry.
7. id-collision active vs closed; ids survive move?
8. trendmeet_rawdata event bursts around ATB1 restarts.
9. AIM1-authored share in 5-day windows (feedback magnitude).
10. outbox image_path >2h + send status.

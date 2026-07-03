# Agent 6: AI Bots 9/11/12/13 (SR, MIS, ATS, RUB)

Verified context: ingestion buffers every WS kline without k['x'] check; indicator engine recomputes at :02/:32 incl. forming candle; 8_ai_trade_monitor deletes ai_signals rows on close.

### [CRITICAL] [ml-correctness] MIS1 predicts on the still-forming candle with stale partial-candle indicators
- 11_ai_mis_bot.py:228, 238-242. df_current = iloc[-1:] = open 1h candle at :11 (~11 min partial volume) joined with :02 indicators. volume_ratio features structurally depressed (~1/6 of full). Training on closed candles → systematic skew on EVERY prediction; tuned thresholds meaningless. Contrast: 12_ai_ats uses -2 correctly.
- Fix: iloc[-2:-1] or WHERE open_time < date_trunc('hour', NOW()).
- Confidence: high | DB-phase: yes (confidence calibration)

### [HIGH] [ml-correctness] MIS1: one model's feature_names_in_ used for all 8 models + .values strips name validation
- 11_ai_mis_bot.py:205-209, 250, 259. Retrained model with different features → silently permuted predictions; per-model except:pass swallows shape errors.
- Fix: per-model DataFrame with own feature_names_in_; log exceptions.

### [HIGH] [ml-correctness] RUB1 predicts on forming-candle indicators (LIMIT 1 = open candle from ~2 min of data)
- 13_ai_rub_bot.py:90-97, 117-131, 150-158. rsi<30 / donchian triggers + all 9 ML features mix :10 live price with :02 partial-candle indicators.
- Fix: WHERE open_time < date_trunc('hour', NOW()); curr_close from same closed candle.

### [HIGH] [robustness] SRA1: conditionally missing ATR features + no per-trade exception isolation → one bad coin aborts whole batch repeatedly
- 9_ai_sr_bot.py:135-143, 268-275, 241-305. ATR NULL → 35 vs 38 columns → predict raises → whole cycle aborts + rollback discards shadow inserts; retried every 5 min for 60 min, starving other trades.
- Fix: always emit ATR features as NaN (XGB handles); per-trade try/except.

### [MEDIUM] [bug] core/trade_utils.get_hvn_and_sr_levels reads 95 days of candles WITHOUT ORDER BY — argrelextrema depends on row order
- core/trade_utils.py:263-276 (used by SRA1/ATS1/RUB1 for SL/TP!). Heap order unspecified, hot-updated tables → out-of-order rows → phantom extrema → wrong SL/TP prices. calculate_smart_targets (55) has ORDER BY.
- Fix: add ORDER BY open_time ASC. One line.
- DB-phase: yes (anomalous SL distances; ctid inspection)

### [MEDIUM] [data-integrity] SRA1 logs posted=True even when cooldown suppressed the actual post
- 9_ai_sr_bot.py:163-164, 278-283. process_ai_trade returns None both ways.
- Fix: return bool. DB-phase: join ml_predictions_master(posted) vs ai_signals → phantom posts.

### [MEDIUM] [ml-correctness] MIS1: fillna(0) doesn't clean inf from zero-volume divisions; predict errors swallowed
- 11_ai_mis_bot.py:131-133, 182, 258-265. volume/shift(1) → inf; sklearn raises (silently skipped forever), xgboost accepts inf (untrained regime).
- Fix: replace([inf,-inf], nan).fillna(0); log swallowed exceptions.

### [MEDIUM] [ml-correctness] MIS1 best-candidate selection compares raw probabilities across differently calibrated models; below-threshold candidate shadows above-threshold one
- 11_ai_mis_bot.py:252-271, 293-311. 8h@0.55 (thr 0.60) beats 168h@0.50 (thr 0.45) → actionable signal discarded.
- Fix: rank by prob - threshold (margin).

### [MEDIUM] [ml-correctness] ATS1: OBV features window-length-dependent despite normalization fix
- 12_ai_ats_bot.py:108-112, 125, 130, 165-166, 213-217. len(rows)>=50 lets 50-499-candle coins through with different accumulation window than training/500-serving.
- Fix: require >=500 for ML path; scale-free OBV features at next retrain.

### [MEDIUM] [data-integrity] MIS1: autocommit=True → outbox/ai_signals/master-log inserts not atomic
- 11_ai_mis_bot.py:190, 384-425. Crash between the 4 INSERTs → signal without monitor record (never tracked, dedupe unarmed) or vice versa.
- Fix: drop autocommit, single commit like ATS/RUB.
- DB-phase: orphaned posted=True without ai_signals.

### [MEDIUM] [data-integrity] Subscribers see TP1-3 (TP1-5 MIS) but monitor scores up to 10-20 targets → live stats diverge from Cornix reality
- 9:191,223; 12:293,338; 13:272,318; 11:347,415. Message truncates targets[:3]/[:5], ai_signals stores full list (up to 20) → monitor hunts TP4-20 for days, books SL-after-TP3.
- Fix: store exactly published targets.
- DB-phase: yes — distorts every per-strategy performance readout.

### [MEDIUM] [performance] RUB1 fetches ~95d × 538 coins closes hourly for a linear regression + per-row .apply timestamp
- 13_ai_rub_bot.py:82-87, 117. ~1.2M rows/hour. Fix: regr_slope/regr_intercept in SQL or vectorize + downsample.

### [MEDIUM] [ml-correctness] SRA1 feeds forming-candle indicator row + entry price up to 60 min stale labeled "CMP Entry"
- 9_ai_sr_bot.py:54-74, 154-188, 244-253. Posted entry from active_trades_master up to 60 min old; SL/TP geometry around stale price.
- Fix: closed-candle lookup; live price for posted entry or drift check.

### [LOW] RUB1: sign flip/blow-up of dist_to_trend_pct when fitted trend value near zero/negative (13:127-131,167-171). Fix: guard trend_val<=0, clip to training range.
### [LOW] MIS1: ai_signals presence check blocks signals AND shadow logging unboundedly; depends on monitor deleting rows (11:277-288). Fix: age cap + move check after shadow logging.
### [LOW] Scheduler comments contradict trigger minutes (ATS says :08 runs :13; RUB says :12 runs :10).
### [LOW] SRA1 shadow-log: comment 0.45 vs code 0.35; minimal insert writes NULL time/direction/entry (9:285-299).
### [LOW] Table names f-string interpolated (all four + core/trade_utils). Central regex gate in load_coins.
### [LOW] Estimator truthiness checks (if not MODEL) instead of is None (12:83,91; 13:52,60).
### [LOW] MIS1 dead code: best_prob<0.25 unreachable; inconsistent threshold defaults 0.5 vs 0.60.

## Cross-cutting observations
1. Forming-candle problem is ARCHITECTURAL: ingestion no k['x'], engine stamps partial rows at :02/:32. Every latest-row consumer inherits skew (MIS critical, RUB/SRA1 material; ATS dodges via -2). System fix: persist closed candles only, or separate live row/table.
2. predict_proba[:,1] convention assumed everywhere; NO trainer code in repo for these models → training-serving parity unverifiable; any retrain can silently change semantics (worst with MIS feature_names misuse).
3. Duplicate/restart protection generally sound (DB-based cooldowns/dedupe). Weak spots: MIS unbounded block, SRA1 posted=True.
4. Minute-equality scheduling: busy/restarting during minute → scan skipped entirely; no catch-up.
5. Heavy bots cluster at :10/:11/:13 after :02 indicator run on shared DB.
6. Scale-dependent features (raw prices) in single global models across 538 coins — modeling smell for retrain.
7. Positive: get_hvn centralization, ensure_min_tp_distance, cooldown-after-send ordering, threshold-drift logging.

## Questions for live-DB phase
1. Calibration per model tag: confidence buckets vs realized outcome.
2. Phantom posts SRA1: posted=True without ai_signals match.
3. MIS dedupe starvation: open-duration distribution; >7d blackout windows.
4. targets_hit distribution — closes beyond TP3/TP5 = divergence population.
5. Horizon shadowing signatures per threshold.
6. RUB extreme-dist losers.
7. New-listing outcomes split by history length (<500 vs >=500).
8. SRA1 entry drift vs market price at insert.
9. Physical row order of un-ORDER-BY'd 95d query on high-churn coins.

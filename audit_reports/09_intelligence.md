# Agent 9: Market Intelligence (10_pump_dump, 19_whale, 20_funding, 23_market_tracker)

Verdict on documented timestamp fix (PUMP_DUMP_TIMESTAMP_FIX_README): real and correct for the price-move ladder, but INCOMPLETE — 3 paths still index-based.

## 10_pump_dump_detector.py
### [HIGH] [bug] Timestamp fix incomplete — Volume-Explosion and ML features still index-based
- 10:522-529, 552-558. volumes_10s[-18:]/prices[-18:] as "3 min", prices[-7:] as p_chg_60s, data[-30] as change_5min. Stale check only guarantees data[-1] <60s old; after restart deque has up-to-4h-old entries below fresh tail → false VOLUME EXPLOSION alerts, skewed ML features.
- Fix: route through _find_bucket_before/_find_bucket_range like the ladder.

### [HIGH] [performance] Unconditional CREATE TABLE IF NOT EXISTS + INSERT into pump_dump_events per symbol per 10s tick
- 10:569-578. ~108 stmt/s permanent; ~4.6M rows/day; before cooldown check; rsi/tsi/macd/ema columns never populated.
- Fix: CREATE once at startup; sample/batch inserts; populate or drop columns.
- DB-phase: row count, housekeeping prune cadence.

### [HIGH] [data-integrity] Shadow-mode inserts into ml_predictions_master have no cooldown/dedup — floods table, poisons tracker stats
- 10:625-635 (consumer 23:402-405). 0.25<=prob<0.60 → row every 10s tick; symbol hovering → up to 8640 rows/day, each counted as "opened signal" by tracker.
- Fix: per-symbol shadow cooldown (15 min) or dedupe; consumers filter posted=TRUE.

### [MEDIUM] [performance] Per-symbol per-tick indicator SELECT before cooldown gate (N+1) — 10:560 vs gate 580. Fix: cooldown check first; cache 5 min.
### [MEDIUM] [robustness] Ladder alerts re-fire every 300s during one sustained move (alert storm) — 10:381-537. 360-bucket tier true for whole hour. Fix: re-alert only if move extends; scale cooldown with tier window.
### [LOW] Round-level cooldown asymmetric: MARKET channel re-sent every 180s (10:217-256). Fix: one cooldown per (level,direction) both channels.
### [LOW] Chart path reused for two outbox rows; if worker deletes after send, 2nd message loses chart (10:233-250). Verify worker semantics.
### [LOW] Cooldown persistence misses PRICE_VOLUME_ALERT_STATE symbols not in PUMP_DUMP_STATE (10:136-147).

## 19_whale_logger_bot.py
### [HIGH] [robustness] 538 aggTrade streams on a single Futures WS connection — fapi caps ~200 streams/conn
- 19:334-336. Beyond-cap streams silently not delivered (or handshake 414) → whale stats miss ~340 symbols, coins.json order decides which survive.
- Fix: shard into 3 connections.
- Confidence: medium — verify: distinct symbols in whale_data files vs coins.json.

### [MEDIUM] [robustness] Reconnect backoff never resets after successful connection
- 19:338-409. Reset at line 409 unreachable after except+continue → after ~7 disconnects over weeks every reconnect waits capped 300s.
- Fix: reset right after connect succeeds (or after N stable seconds).

### [MEDIUM] [performance] CPU-bound 3-day scans + full-day JSON rewrite stall event loop next to firehose WS
- 19:157-227, 282-294. 12+ full scans of WHALE_TRADES (10^5-10^6 dicts); json.dump holds GIL; recv backlog → Binance disconnects slow consumer (feeds backoff finding).
- Fix: rolling aggregates on ingest; JSONL append.

### [LOW] Reconnect gaps silently deflate 1h/4h/24h ratios — annotate "data gap Xm".
### [LOW] {prc:.2f} renders sub-cent coins as 0.00 (19:207,212). Fix: :.6g.
### [LOW] m=True labeled "SHORT" is actually taker sell pressure — rename BUY/SELL pressure.

## 20_funding_logger_bot.py
### [MEDIUM] [bug] "Extreme" breadth threshold 75% positive fires in ordinary conditions
- 20:164-186. Baseline funding +0.01% → majority positive is steady state → alert every 15 min for days.
- Fix: raise to 95/85 only, require magnitude |rate|>0.02%, or alert on transitions.
- DB-phase: outbox frequency of this alert.

### [LOW] "1h" breadth silently falls back to current value (20:248) — fake stability. Fix: N/A like bps diffs.
### [LOW] Top-5 pos/neg lists over ALL Binance symbols, not tracked set (20:215, 255-262). Fix: intersect load_coins.
### [LOW] Index rebuild + full-day rewrite every 5 min; get_historical_rate O(n) despite bisect (20:66-99, 379-387).
- Timezone check: CLEAN (epoch-based, tz-aware). Caveat: lastFundingRate is predicted rate, not settled funding.

## 23_market_tracker.py
### [HIGH] [robustness] Pooled connections leak on query failure → pool exhaustion kills tracker; missing rollback guarantees AI-open fallback also fails
- 23:395-429, 749-831 (esp. 816-826). Read failure → except → return WITHOUT close → slot leaked per hour; fallback reuses conn without rollback (InFailedSqlTransaction → guaranteed raise). After ~8h all tracker jobs dead until restart.
- Fix: try/finally close; rollback before fallback.
- DB-phase: idle-in-transaction conns from tracker.

### [HIGH] [data-integrity] Signal-summary "Opened" counts double-count AI trades and include shadow predictions
- 23:399-425, 514. df_act_ai = ALL ml_predictions_master rows 24h (no posted filter, archive rows) + closed_ai_signals → every closed AI signal counted twice; shadow noise counted as opened (EPD1 "Opened: 360L").
- Fix: posted=TRUE filter; count opens exclusively from ai_signals+closed_ai_signals.

### [MEDIUM] [data-integrity] ai_signals→ml_predictions JOIN fan-out; dedup key lacks symbol; keep='last' attaches wrong created_at
- 23:797-815. Fix: symbol in dedup subset; closest m.time <= creation; better: created_at column on ai_signals.

### [MEDIUM] [performance] job_per_bot_performance loads FULL trade history into pandas hourly + row-wise apply
- 23:755-776. Unbounded growth. Fix: SQL GROUP BY with FILTER; reuse vectorized _compute_outcome_flags.

### [MEDIUM] [performance] All "async" jobs fully synchronous; job_main_reports ~7k sequential queries at XX:00:15; jobs serialize
- 23:64-183, 1486-1538. Fix: consolidated SQL per window, asyncio.to_thread, UNION ALL batches.

### [MEDIUM] [robustness] _get_regime_fit_label: one failed query poisons shared conn for all remaining bots (no rollback)
- 23:646-712, 1259-1303. First error → aborted txn → whole Regime-Fit column "---".
- Fix: rollback in except or autocommit.

### [MEDIUM] [bug] Message chunker cannot split oversized single block; inactive-bot rows merge into one giant block
- 23:1180-1231, 1349-1414. Quiet night + 45 bots → block >4096 → Telegram rejects, silently (outbox insert only).
- Fix: blank separator after every row; hard line-split fallback in _build_chunks.

### [LOW] avg_daily fallback 1 → absurd percentages (23:144-147). Fix: N/A.
### [LOW] TP staffelung assumes status 0..4 but AI targets_hit 0..19; n_sl from status==0 counts non-SL closes (23:765, 1105-1113). Fix: >=4 bucket; SL from close_reason/pnl.
### [LOW] Blanket except:pass makes whole reports silently disappear (23:94,121,224,314,366). Fix: warning + skipped-count.
### [LOW] f-string table names (23:78-84 et al). Central validation.
### [LOW] Scheduler comments disagree with code; WR ignores fees — add "excl. fees" legend.

## Cross-cutting observations
1. Silent exception swallowing house style → failure mode "report quietly doesn't arrive". Per-job last_success heartbeat in DB would fix observability.
2. Transaction-hygiene bug repeats (reuse after failed stmt without rollback) — standardize.
3. JSON persistence (10/19/20): full rewrite every 5 min + full history in RAM → same JSONL+rolling-aggregate refactor fixes all three.
4. Timezone largely clean in code; risk is DB column types (naive vs tz) for pandas comparisons in job_gainers_losers.
5. Per-bot performance table is the decision surface; its two upstream polluters live in file 10 (shadow flood, double-count).

## Questions for live-DB phase
1. Column types open_time/time/posted/close_time — timestamp or timestamptz? (Decides whether job_gainers_losers silently skips every coin.)
2. closed_trades_master.posted is timestamp (used as closed_at), not boolean?
3. Row counts: pump_dump_events, ml_predictions_master by posted/model_name, closed tables totals.
4. ai_signals creation-timestamp column? targets_hit distribution.
5. regime_current/bot_regime_performance deployed? win_rate 0-100 or 0-1?
6. pg_stat_activity idle conns owned by tracker; pg_stat_statements XX:00 burst.
7. Outbox: image_path deletion semantics; MESSAGE_TOO_LONG errors; CH_PUMP_MARKET rows/hour on volatile days.
8. Whale coverage: distinct symbols in whale_data JSON vs coins.json (proves 200-stream cap).
9. FUNDING EXTREME alert frequency per day.

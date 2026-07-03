# Agent 5: Classic Strategies (3_detectors, strategies/*, 7_pattern_detector, 22_ip_pattern_bot)

### [HIGH] [strategy-logic] All 5 classic strategies evaluate the still-forming candle; 1h strategies additionally run mid-candle at :32
- 3:190-192 / strat_5_percent:40 / strat_fast_in_out:58 / strat_volume_indicator:72 / strat_main_channel:18 / strat_support_resistance:14 (root: ingestion no k['x']; engine recomputes ALL TFs at :02 AND :32, stamps 1h 'updated' both times). iloc[0] = candle ~2 min old; at :32 the 1h candle is 32 min into formation. 7_pattern uses len-2 correctly.
- Fix: skip forming row (iloc[1] when iloc[0].open_time == current period open); engine stamp 1h only at :02; or closed-candle path in ingestion.
- DB-phase: MAX(open_time) of _1h_indicators at :35; signal minute distribution :32-:35.

### [HIGH] [strategy-logic] SHORT "headroom" check is a sign-flipped no-op (support*0.95 instead of *1.05)
- strat_fast_in_out:74, strat_5_percent:97. LONG: close < resistance*0.95 (>=5% headroom). SHORT written close > support*0.95 — true essentially always → SHORT has NO headroom guard.
- Fix: close > support*1.05 in both files (materially tightens SHORT — recheck hit rates).
- DB-phase: SHORT vs LONG WR; distance-to-support at SHORT entry.

### [HIGH] [strategy-logic] Empty-zone target interpolation produces LONG TPs BELOW entry (SHORT TPs at -25/-50/-75%)
- strat_main_channel:70-87, 115-132; strat_support_resistance:53-57, 65-69. Targets padded 0.0; interpolation branch never guards t1==0 → t1 = 0.75*entry for LONG. find_support_resistance_zones legitimately empty on low-volume ATH drift.
- Fix: if t1 == 0: return None (or fixed-% fallback like Volume).
- DB-phase: active_trades LONG with target1 < entry; target1 ≈ 0.75*entry fingerprint.

### [HIGH] [robustness] One bad coin inside a strategy call crashes the entire detector process
- 3:186-233, 243-257. Per-coin try covers only indicator read; strategy calls + write_signal_atomic unprotected; main() catches ONLY FileNotFoundError → process dies mid-scan, watchdog-restart loop, back half of alphabet never scanned.
- Fix: per-coin try/except with rollback; broad except with backoff in main().

### [HIGH] [strategy-logic] Volume Indicator has no cooldown — stale spike (up to 5 days old) re-fires every 30 min
- strat_volume_indicator:68-100, 36-65. Only guard is_trade_already_active; TP1 +2.5% can hit within the hour → serial re-entry loop for days from one historical event.
- Fix: check_cooldown/update_cooldown (12-24h) or dedupe on spike candle timestamp.
- DB-phase: Volume Indicator signals per coin per 24h — expect clusters.

### [HIGH] [bug] Pattern detector: break_idx frozen while 168-candle window slides → age-expiry never fires, ACTIVE_PATTERNS grows forever
- 7:336-341, 360-367, 265-271. candles_since_break ≈ 0 forever; max_candles dead code; orphaned entries persisted+reloaded, unbounded growth of active_patterns.json.
- Fix: expire on now - break_time (already saved!) > max_candles*tf; GC old entries on load.

### [MEDIUM] [bug] Volume spike detection: oldest spike wins; spike at index 0 always classified as Sell Spike
- strat_volume_indicator:57-63. First (oldest) spike returned; i==0 falls to else → return -1 regardless of direction → SHORT against accumulation event.
- Fix: iterate reverse; fetch one candle before window / skip i==0.

### [MEDIUM] [strategy-logic] HVN detection: sum-per-exact-float-price vs single-candle 3σ threshold — meaning depends on tick size
- strat_volume_indicator:22-30. Fine-tick (BTC): closes never repeat → gate ≈ never passes; coarse-tick (SHIB): trivially always passes → gate degenerates.
- Fix: bin prices (pd.cut) + percentile of binned distribution.
- DB-phase: per-coin signal counts skew toward coarse-tick coins.

### [MEDIUM] [data-integrity] Pattern detector dedupe sets memory-only → restart re-fires breakout/retest/fakeout alerts + overwrites tracked state
- 7:31-32, 334, 378, 406. ALERTED_PATTERNS/ALERTED_RETESTS never persisted (22_ip got exactly this fix: alerted_qms.json). Trades protected by DB cooldown — alert spam + tracking corruption.
- Fix: persist both sets in same JSON; derive breakout-alerted from ACTIVE_PATTERNS membership.

### [HIGH→before re-enable] [robustness] 22_ip: one bad coin aborts entire scan (try wraps whole coin loop) — 22:193-278. Fix before re-enabling.

### [MEDIUM] [strategy-logic] OBV divergence filter: N-candle sum vs 1-candle 2σ band — statistically near-meaningless
- core/market_utils:179-187 (used by main_channel + support_resistance). For N>=10 passes on noise; for short periods nearly impossible. Gate decorative.
- Fix: scale band by √N or mean-slope comparison.

### [MEDIUM] 5 Percent SHORT uses ema_12 < ema_55 where LONG uses ema_21 > ema_55 (likely typo) — strat_5_percent:86 vs 56.
### [MEDIUM] REQUIRED_COLUMNS guard missing ema_200/wma_21/wma_26 actually used — strat_5_percent:11-14 vs 53-90. Latent silent-never-fires.
### [MEDIUM] [data-integrity] telegram_outbox created by 3_detectors WITHOUT image_path; pattern bots INSERT image_path → bootstrap-order UndefinedColumn, silently swallowed per coin (alert blackout). Also ai_signals/trade_cooldowns have no DDL anywhere. Fix: single schema-bootstrap module.
### [MEDIUM] [performance] 538 serial Binance HTTP calls per detector cycle (×2 at hour boundary), before knowing if needed — 3:199-201. Fix: one batch GET /fapi/v1/ticker/price, or lazy fetch after conditions pass.
### [MEDIUM] [performance] Volume strategy reads 90d × 30m (~4320 rows) per coin per 30-min cycle as FIRST gate (~2.3M rows/cycle) — strat_volume_indicator:14-19,77. Fix: reorder guards; cache HVN per coin.
### [MEDIUM] [data-integrity] Stored SUPPORT/RESISTANCE_PRICE history mixed-vintage (scalar broadcast + partial rewrites) — "previous candle's level" semantics don't hold; first-hit search vs today's level = artifact. Also fallback can set SUPPORT_PRICE above close (2_indicator_engine:442-457,469).
### [MEDIUM] [robustness] Chart directories grow unbounded (7:27-28,139; 22:29-30,159) — verify whether outbox consumer/housekeeping cleans (housekeeping cleans "charts" — check these dirs specifically).
### [LOW] Table names from coins.json interpolated ~40 sites. One load-time regex validation closes all.
### [LOW] Closed candles' final values lost by keyed WS buffer (context; see agent 2).
### [LOW] Dead duplicate signal writers in 3_detectors (53-106) — third copy of message builder. Delete.
### [LOW] open_handler leaks pooled connection on exception (82-88) — use db_connection() helper.
### [LOW] 22_ip alerts on stale QM structures up to 300 candles old, no mitigation check (213-233).

## Cross-cutting observations
1. Forming-candle is THE systemic issue: ingestion → engine (:02 AND :32 recompute all TFs) → detector iloc[0]. 7_pattern correct, 5 classics wrong.
2. Dup protection inconsistent by generation: pattern bots DB-cooldowns (good); fast/5pct GLOBAL win-count cooldown (400/500 wins in 3-4h across ALL coins — near-dead guard, perversely throttles after wins never losses); Volume nothing. No classic strategy has per-coin cooldown.
3. Main Channel and Support Resistance are the same strategy (identical hit/divergence/OBV logic; MC adds ATR-SL) on same data for 38 coins → one event = two near-identical leveraged signals in two Cornix channels (double exposure).
4. active_trades_master uses REAL (float4) for prices.
5. Three copies of signal-message builder; two copies of S/R strategy.

## Questions for live-DB phase
1. Forming-candle proof (MAX open_time at :35; signal minute distribution).
2. Corrupt targets query (LONG target1<=entry OR target1=0; 0.75*entry fingerprint).
3. Volume Indicator re-fire clusters.
4. Cooldown reality: max wins per rolling 3h — has 400/500 ever been reached? Status value domain?
5. SHORT-side quality of fast/5pct (quantifies *0.95 no-op).
6. Live outbox schema (image_path? channel_id=0 rows?); ai_signals/trade_cooldowns exist?
7. support_price run-lengths; rows with support_price > close.
8. Signal lag (active_trades.time vs candle close); posted TZ mix.
9. Duplicate PATTERN BREAKOUT messages around restarts.
10. 5 Percent liveness: signals/week, LONG/SHORT split (26 AND-conditions — fires at all?).

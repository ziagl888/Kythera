# Agent 8: SMC/Pattern Bots (16, 17, 21, 24, 25, 29)

## 16_smc_forex_metals_bot.py
### [CRITICAL] [strategy-logic] FVG mitigation entry is unreachable — dead signal path
- 16:159, 418, 446. Mitigation scan includes the current candle; trigger uses identical predicate on current candle → mutually exclusive. FVG feature silently dead — bot only emits STRUCTURE signals. "FIX" comment extended loop to len(df) and killed the feature.
- Fix: range(fvg['index']+1, len(df)-1). DB-proof: zero SMC_FVG cooldown rows.

### [HIGH] [strategy-logic] Decisions on still-forming candle (repaint) — both data sources
- 16:329-344, 131-134, 105-110. DB source doesn't drop running candle; yfinance FIX deliberately keeps in-progress last row; 2h/4h resample last bucket partial; 1d/1w guaranteed unfinished at scan times. BOS on forming candle; forming 1d/1w keeps condition true for days → 12h cooldown → refire all week.
- Fix: iloc[:-1] for DB; drop partial rows/buckets for yfinance; cooldown >= candle duration.

### [MEDIUM] Weekend/static-data refire for forex: market closed Fri 22:00, bot scans all weekend, same Friday signal re-posts every 12h cooldown lapse with Friday price into closed market (16:483, 348). Fix: skip Sat/Sun + freshness gate.
### [MEDIUM] No SL-side/RR sanity check on BOS: sl=last swing low can be 20-30% away or even above entry; "20x-10x" posted (16:354, 389). Fix: ATR/%-cap + reject sl>=entry. (21 does validate.)
### [LOW] 2h/4h resample in exchange-local TZ before UTC conversion → buckets misaligned vs Binance, DST shift (16:105-110; also 17:61-66).

## 17_mayank_bot.py
### [MEDIUM] Static-data refire after cooldown expiry (weekends) — same as 16 (17:246-253, 259). Fix: freshness gate or trigger-candle-timestamp in cooldown key.
### [MEDIUM] No FVG age limit — months-old gaps generate "retest" signals; oldest-first break (21 caps MAX_FVG_AGE=48) (17:234, 304). Fix: adopt 48-candle window, newest-first.
### [LOW] Three separate pool connections per signal (17:258, 286, 325).

## 21_btc_smc_strategy.py
### [HIGH] [strategy-logic] 100x leverage with 0.4-1.2% SL — SL beyond liquidation
- 21:31-35, 199, 238. At 100x isolated, liquidation ~-0.9% before the 1.2% SL; even 0.4% floor = -40% margin/stop. RR check ignores leverage.
- Fix: lev <= 0.5/sl_pct or DESIRED_LEVERAGE <= 25.

### [MEDIUM] [bug] No cooldown/dedupe — DB write-lag causes duplicate signals for same candle
- 21:121-123, 264. Unconditional iloc[:-1]; if filler late, last closed candle dropped → same trigger candle signals twice 1h apart. Zero cooldown in file.
- Fix: standard check_cooldown/update_cooldown or dedupe on trigger open_time.

## 24_quasimodo_bot.py
### [HIGH] [strategy-logic] Pivots detected on forming candle without confirmation — training-serving skew vs qm_ml_trainer
- 24:110-111 vs trainer:183. argrelextrema mode='clip' lets elements 1-2 bars from end pass as pivots with clipped neighbors; trainer gates p[0] <= curr_idx - PIVOT_WINDOW. Live fires on repaintable p4 — regime model never saw.
- Fix: drop forming candle + discard pivots with index > len-1-PIVOT_WINDOW.

### [MEDIUM] Missing model features silently zero-filled; NaN trend → all dummies 0 (24:212-215, 206-209; same 25:93-97, 320-323). Fix: WARN on absent feature; skip on unknown trend.
### [MEDIUM] Per-symbol exceptions logged at DEBUG — invisible (24:280-281; 25:306-307). 29 does it right.
### [LOW] Live entry/threshold diverge from trainer (limit@QML vs CMP ±1%; hardcoded 0.65 vs pkl optimal_threshold). predict_proba[0][1] itself verified CORRECT.

## 25_smc_ml_sniper.py
### [HIGH] [strategy-logic] Three-Drive: trainer entry is hindsight (pivot close), live enters 12 bars later with different SL/TP → model probabilities not transferable
- 25:203-241, 131 vs trainer:126-149. Threshold 0.30 hardcoded, ignores pkl optimal_threshold; trainer p3-p1<=100 vs live MAX_TD_SPAN=50.
- Fix: retrain with realistic entries (p3+PIVOT_WINDOW close) + live SL/TP generator; load thresholds from pkl.

### [MEDIUM] Breaker Block: checks only peak_idx[-2] — during fresh retest the post-breakout high not yet confirmed as peak → compares wrong level most of the time (missed/wrong fires). Feature timing: trainer at breakout, live at retest. "Massive violation" check commented but not implemented (25:250-264 vs trainer:183-211).
### [LOW] send_cornix_signal never commits — works only via upstream autocommit (25:385-416).

## 29_ufi1_bot.py
### [HIGH] [strategy-logic] "Candle closes below Fib" confirmation can be evaluated on the still-forming daily candle
- 29:177-193, 66-88. j can reach n-1 (today's running 1d row); intraday dip counts as confirmed daily rejection. Backtest used closed candles.
- Fix: j <= n-2 (exclude running candle).

### [HIGH] [strategy-logic] SL ≈ 25-40% above entry with 20x leverage — liquidation long before SL
- 29:194, 244. sl=swing_high*1.03; entry ~0.77*sh → SL distance ~34%; 20x isolated liquidates ~+5%. Backtest "+0.83R" cannot survive 20x.
- Fix: lev from SL distance (~1-2x) or cap UFI1 at <=3x.

### [MEDIUM] Fib level mislabeled: 0.382 in code = 61.8% retracement of the dump (29:109-111, 48). Internally consistent IF backtest used same formula — document convention, confirm vs backtest.
### [MEDIUM] Aged setups refire every 48h; confirmation candle may be ~2 weeks old; stale corridor wide (29:154-215, 44). ai_signals blocks only while open (monitor deletes rows). Fix: confirmation within last 2-3 daily candles, or setup-keyed cooldown.
### [LOW] "Rejection" accepted without level ever touched (close within ±2% suffices) (29:182-190).

## Cross-cutting observations
1. DB candle contract interpreted inconsistently: 21 drops last row; 16/24/25 treat last row as live; 29 mixes. One documented contract + shared fetch_closed_candles() helper eliminates the bug class.
2. scipy argrelextrema mode='clip' edge behavior → premature-pivot leak in every pivot consumer (market_utils.calculate_pivots, 24, 25). QM trainer shows correct pattern.
3. mplfinance RAM leak: FIXED everywhere in scope (finally + plt.close('all')).
4. matplotlib backend: only 16 sets Agg; 17/24/25 import pyplot without → GUI canvases; headless service would crash. One line per bot.
5. Leverage vs SL distance never reconciled anywhere (21: 100x/1.2%; 29: 20x/34%). Shared cap_leverage_to_sl(sl_pct) in trade_utils fixes all bots.
6. Cornix double-parse risk: 24/25/29 insert plain Cornix block AND second HTML message embedding identical block into same channel → if Cornix parses both, double execution. 16 embeds in single message (safe).
7. Pool arithmetic: ~27 × (2-8) vs default max_connections=100.
8. SQL identifier surface theoretical; no validation anywhere.
9. 24+25 together ~2150 join-queries every 3 min; 4h half redundant 98% of time.
10. Logging style diverges: 29 model (ERROR+exc_info+rollback); 24/25 debug (invisible); 16/17 error without traceback.

## Questions for live-DB phase
1. Does {symbol}_{tf} contain the running candle + write latency after close? (Decides 16/21/24/25/29 findings.)
2. trade_cooldowns: ANY module='SMC_FVG' rows? (Dead-code proof.) SMC_BOS at ~12h spacing on 1d/1w?
3. Do METALS tables exist (XAUUSDT etc. not standard Binance perps)? If absent, market silently never signals + error-log every 15 min.
4. ml_predictions_master posted-vs-shadow hit rates per model (QM/TD/BB) — quantifies skew.
5. How fast do monitor/housekeeping delete ai_signals rows? (Real refire window for 24/25/29.)
6. max_connections + current connection count.
7. Outbox history: plain+HTML pair on Cornix channels; double-execution evidence.
8. Weekend timestamps CH_SMC_FOREX/CH_MAYANK; duplicate BTC_SMC ~1h apart.

# Agent 11: ML Trainers + Backtests (qm_ml_trainer, smc_ml_trainer, qm_backtest, fib_backtest, smc_pattern_backtester, check_*, backtest/)

### [CRITICAL] [ml-correctness] Three-Drive (TD) labels built on look-ahead entry the live bot can never take
- smc_ml_trainer.py:128,159 (also smc_pattern_backtester.py:142). entry=closes[p3] where p3 is argrelextrema(order=10) pivot — knowable only 10 bars later. Live bot (25:203/224) fires 11-12 bars after p3 at current market price via calculate_smart_targets. Labels measure a physically impossible trade; on a reversal pattern 11 bars of drift is most of the edge → live WR structurally worse than training metrics.
- Fix: label from pivot-confirmation moment (closes[p3+PIVOT_WINDOW]) with the bot's SL/TP construction.
- DB-phase: yes (posted-vs-blocked realized WR for TD_1H/4H)

### [CRITICAL] [ml-correctness] SMC models trained on SL/TP geometry the live bot never trades
- smc_ml_trainer.py:195-196,226-227 (BB fixed 1%/2%), 129-133,160-164 (TD 2R from pivot) vs 25:131 (calculate_smart_targets: ATR/S&R/fib ladder, dynamic SL). predict_proba is probability for an outcome definition never executed; threshold sweep + "RR 1:2" void. EV of prob>=0.40 trades can be negative even with positive training PnL.
- Fix: train on smart-target geometry, or bot trades the labeled geometry.

### [HIGH] [ml-correctness] BB training/serving feature skew: features from breakout candle, inference from retest candle
- smc_ml_trainer.py:206,237 vs 25:264,291. Breakout RSI ~65-75 vs retest ~45-55 → tree splits route retest rows to arbitrary leaves → probabilities noise. Also population skew (trainer trades every peak; bot has freshness/overshoot filters).
- Fix: extract training features at retest candle j-1; mirror bot filters in trainer.

### [HIGH] [ml-correctness] Random train_test_split on time series + overlapping duplicates = contamination; "optimal threshold" selected ON the test set
- qm_ml_trainer.py:261,290-325; smc_ml_trainer.py:262,276-314. Near-duplicate rows both sides of split; threshold = argmax PnL on same test set → optimistically biased operating point saved into pkl.
- Fix: chronological split with purge gap; threshold on validation slice.

### [HIGH] [backtest-validity] QM fill logic silently deletes guaranteed losers and awards same-candle TP wins
- qm_ml_trainer.py:121-179; qm_backtest.py:167-184. (a) Trigger candle also touching SL → "invalidated" (no trade) though entry filled on the way → real same-candle stop-out removed from PnL + labels. (b) Trade triggered at curr_idx immediately outcome-checked against same candle → TP win awarded though high may precede fill.
- Fix: fill-then-stop conservative; no TP win on entry candle; outcome from curr_idx+1.

### [HIGH] [robustness] Silent exception + pooled-connection leak → trainers silently run on truncated coin universe, can overwrite production pkl
- qm_ml_trainer.py:67-94; smc_ml_trainer.py:63-90; qm_backtest.py:54-67; backtest/smc_btc_backtest.py:73-85. Missing indicator table → except returns empty, conn leaked; after 8 leaks pool exhausted → EVERY remaining coin skipped silently → model trained on 0-8 coins saved over production pkl.
- Fix: try/finally close (or with db_connection()); log skipped symbols; abort if processed count anomalously low.
- DB-phase: count coins lacking tables.

### [HIGH] [backtest-validity] fib_backtest selects entries using future global low of the window — live UFI1 sees different trade population
- fib_backtest.py:252-262,327-336 vs 29:158-169. argmin over full 30-bar window embeds future knowledge; live bot at scan time has low-so-far → fires on retracements backtest never counted, shallower fib anchors. WR 54.2%/+0.83R measured on cleaner population.
- Fix: walk-forward simulation of the scan process (replay bot's own setup function bar-by-bar).

### [HIGH] [backtest-validity] UFI1 live entries can be days-to-weeks stale at CMP — "+278R" claim unsupported for live bot
- 29:177-226,241,363 vs fib_backtest.py:395-467. (1) No recency check on confirmation candle → 3-week-old setup still returns; entry=live_price anywhere in [tp1*1.02, sl) → WR/R arbitrary (entry near SL = tiny risk/huge R; thesis long dead). (2) Backtest exits via 5-target trailing ladder; bot posts single TP1 → +0.83R avg comes mostly from runners bot never captures. (3) Re-fire after 48h cooldown + ai_signals clear → trade-count inflation.
- Fix: require confirmation in last 1-2 closed daily candles; re-derive SL/TP vs actual live entry; align backtest exit model with single-TP1 before quoting numbers.
- DB-phase: realized UFI1 trades vs claim.

### [MEDIUM] [ml-correctness] Unresolved SMC trades labeled as losses (outcome=0 default) — qm trainer correctly excludes (inconsistent). smc_ml_trainer.py:135-142 etc. Fix: None default + drop.
### [MEDIUM] [backtest-validity] SMC retest/entry candle excluded from outcome scan (fill candle can't lose) — smc_ml_trainer.py:198,229; smc_pattern_backtester.py:186,214. Fix: check candle j SL-first.
### [MEDIUM] [backtest-validity] Fees declared but never applied — smc_pattern_backtester.py:20 (dead FEE_RATE), Net_R gross; fib fee-free. BB 1% SL: round-trip 0.08-0.15% = 8-15% of one R → flips marginal thresholds. Fix: fee_r per trade.
### [MEDIUM] [ml-correctness] Thresholds trained per-TF and saved into pkl, but both bots ignore them (24: fixed 0.65; 25: hardcoded {'bb':0.40,'td':0.30} both TFs). After retrain silently divergent. Fix: load from pkl with floor, or delete field.
### [MEDIUM] [data-integrity] bfill() leaks future indicator values into early-history features (trainers only; bots benign) — qm:87, smc:83. Fix: dropna after ffill, no bfill.
### [MEDIUM] [backtest-validity] Survivorship bias: all backtests/trainers run today's coins.json over 1-2y history. Short-heavy fib understates wins; LONG setups overstate. DB-phase: do delisted coins' tables still exist? (cheap fix if yes)
### [MEDIUM] [backtest-validity] qm_backtest (ORDER_EXPIRY 100) vs qm_ml_trainer (50) simulate different strategies; neither matches bot (no resting order, point-in-time SL check, can't see earlier SL-zone sweep, 50%/100% TP ladder vs binary label).
### [MEDIUM] [backtest-validity] No capital/concurrency model: totals sum unlimited parallel correlated positions (qm $5k fixed margin, fib sum of all R × 531 coins, backtest/ 100x). Max-DD only on trade close. Fix: concurrency cap + margin ledger, or report per-trade expectancy.
### [MEDIUM] [code-quality] Coin-level exceptions in both live snipers logged at DEBUG — invisible (24:280-281; 25:306-307). Systematic error (renamed column) → bot scans "successfully" forever, posts nothing. Fix: WARNING + per-scan aggregate + alert threshold.
### [MEDIUM] [ml-correctness] QM trend-dummy encoding data-dependent (pd.get_dummies) vs bot hardcodes 3 categories (qm:244-249 vs 24:206-209; smc trainer does it right). trend_None column risk. Fix: hardcode 3 dummies.
### [LOW] No class-imbalance handling/calibration; "Win Probability X%" is uncalibrated score; pkl without version metadata. Fix: store xgb.__version__, assert on load; calibrate if displayed as probability.
### [LOW] Global warnings.filterwarnings("ignore") in every entry point — hides unpickle version warnings (early signal for skew). Fix: suppress only known-noisy.
### [LOW] Stale comments/dead code: qm_backtest LEVERAGE comment, TIMEFRAMES table; fib docstring says v3; backfill_regime_history imports ITSELF in run_backfill.
### [LOW] check_funding renders timestamps local while writer treats as UTC (check_funding.py:37). Fix: tz=timezone.utc.
### [LOW] backtest/ legacy scripts: in-sample grid optimization, close-of-trigger fills, 100x nominal PnL, bare except. No live consumer. Fix: archive/ + disclaimer, don't quote PnL.

## Cross-cutting observations
1. DOMINANT PATTERN: "backtest the detector, trade something else." All three families: idealized fills (limit at level / pivot close / confirmation close) vs bots posting CMP at scan time with different SL/TP. None of the published WR/R numbers describes the system that trades. Highest-leverage fix: one shared walk-forward simulator consuming the bot's own setup functions.
2. Index-based ≠ time-based: pattern spans in candle counts assume gapless tables → gap audit needed.
3. Intrabar convention conservative (SL-first) everywhere except QM entry-candle win + invalidated-instead-of-stopped — the two that pay.
4. Reproducibility half-done: seeds fixed but window NOW()-2y; no record what a deployed pkl was trained on. JSON sidecar per model (train date, coin/trade count, xgb version, metrics).
5. Silent-failure culture in offline tools opposite of fleet discipline; trainers can silently overwrite production models.

## Questions for live-DB phase
1. Shadow-log validation: live WR by confidence bucket per model_name (QM/BB/TD) — direct measure of skew.
2. Is last row of {symbol}_{tf} the forming candle? (Trainer curr_idx-1 matches bot len-2 only then.)
3. How many coins.json symbols lack tables? (>=8 → silent truncation active.)
4. trend_direction value domain + NULL rate.
5. open_time column type + server TZ (NOW()-2y window shift; regime_history.ts naive-UTC).
6. ai_signals lifecycle: rows deleted on close? (dedupe-forever vs UFI1 stale re-fire.)
7. Candle-gap audit per _1h/_1d table.
8. Delisted coins' tables still exist?

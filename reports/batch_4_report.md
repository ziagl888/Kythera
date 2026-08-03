# Batch 4 Report — Indicator Engine & Strategies

**Target files:** `2_indicator_engine.py`, `strategies/*`

## Completed

### #6 — Trendline division by 0 and NaN robustness (2_indicator_engine.py)
`calculate_trendline_and_channel_robust_optimized()`:
- On a fully constant price series (`y == y[0]`), `stats.linregress` returns NaN. Now: early return with neutral values (slope=0, direction=SIDEWAYS).
- On a successful linregress, additional `np.isfinite()` checks for slope, intercept, r_value, std_dev.
- Direction threshold `0.0001 * y[0]` failed at `y[0] == 0` (theoretically possible through faulty ingestion). Now: `abs(base)` and fallback to `y[-1]` instead of `y[0]`, plus a minimum threshold `1e-8` for the edge case.

### #12 — Volume indicator iloc instead of loc (strategies/strat_volume_indicator.py)
`detect_volume_spike_in_period()`: `df_period.loc[index - 1, 'close']` replaced with positional `iloc` access. Additionally `reset_index(drop=True)` after the SQL read, so the index is guaranteed 0..N-1.

### #45 — indicator_state.json written atomically (2_indicator_engine.py)
Temp file + `fsync` + `os.replace` instead of writing directly to the target file. Prevents half-written JSONs on a concurrent read from the detector process.

## Cleared as false alarms

### #9 — HVN binning
The code uses `bins = int(np.sqrt(len(prices)))`, i.e. dynamically scaled — not hardcoded to 0.5%. The 0.5% figure in the original analysis referred to the duplicate filter between peaks (`abs(p - poc_price) > poc_price * 0.005`), and that one is functionally sound (merging redundant nearby peaks). No change risks HVN stability.

### #26 — fibs['extensions'] unused
`FIB_EXTENSION_*` are written into the results on line 394 and thus into the indicators table in the DB. So they are not "dead" — they just aren't read directly in the Python code.

### #29 — strat_5_percent SL without resistances
On close code inspection: the 5-percent strategy uses an **ATR-based** SL with 3.5×ATR and already has an implemented cap (`live_price * 0.95/1.05` if the ATR SL deviates more than 5% from price). The resistance/support values are only used in the filter conditions, not for the SL calculation. Not a bug.

### #41 — strat_main_channel on smoothed close
The bot uses `close_price_current` = the final 1h close of the just-completed candle. That's already a semantic smoothing (1h bucket aggregation). Additional EMA smoothing would delay channel-break signals — counterproductive for the strategy.

### #44 — BB std=2.0 hardcoded, MACD inconsistencies
BB std=2 is industry standard. The two MACD variants (9/21/9 fast and 12/26/9 normal) are both standard and deliberately kept in parallel — not an inconsistency but a dual variant for different signal types.

### #46 — calc_kama column names as string
The function returns numeric values and is called with integer parameters (`KAMA_{p}` where p is an int). Not a string typo.

### #47 — calc_wma length 200 on 100 candles
`rolling(window=200)` returns NaN with <200 candles, `fillna(0)` produces 0. Not ideal as an MA fallback, but by design — the ML models were trained on exactly this behaviour. Switching to "carry-forward" or similar would require retraining.

### #49 — lookback_candles = 3000
The value of 3000 is used **only on the first run** (initial population of the indicator table). In ongoing operation it's 1000 — small enough.

## Verification
All 6 files parse cleanly.

## Summary Batch 4
Of 10 planned fixes: 3 real bugs fixed, 7 identified as false alarms from the original analysis. The indicator engine and the strategies are overall sounder than my initial analysis suggested — my analysis was too pessimistic in this area.

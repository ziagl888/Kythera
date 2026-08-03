# Batch 2 Report — AI Bot Signal Quality

**Target files:** `10_pump_dump_detector.py`, `11_ai_mis_bot.py`, `12_ai_ats_bot.py`, `13_ai_rub_bot.py`, `14_ai_atb_bot.py`, `18_ai_abr1_bot.py`

## Completed

### #17 — RUB bot cooldown before ML prediction (13_ai_rub_bot.py)
`check_cooldown()` is now called before `predict_proba()`. With 500 coins × several event types, this saves significant CPU when most coins are in cooldown. The shadow log for rejected trades (prob < threshold) still runs after the prediction.

### #20 — ATB NaN/Inf safeguard (14_ai_atb_bot.py)
Additional `replace([inf, -inf], nan).fillna(0)` after feature construction. I did NOT do the actual refactor (indicators from DB instead of pandas_ta) — documented with reasoning: since the ML model was already trained on pandas_ta values, switching to DB values would change the feature semantics and require retraining.

### #24 — RUB get_f made more robust (13_ai_rub_bot.py)
`get_f()` previously only checked for `None`. Now it also checks for NaN/Inf, with a defensive `float()` conversion and fallback. Prevents crashes for fresh coins in the warm-up phase.

### #25 — ABR1 defensive features (18_ai_abr1_bot.py)
`X_event` is cleaned before `predict_proba()` via `replace([inf, -inf], nan).fillna(0)`.

### #27 — MIS1 threshold loading logged (11_ai_mis_bot.py)
When loading the models, it is now explicitly logged which thresholds were taken from the separate pkl files. This means drift between model version and threshold file is immediately noticeable. No code change to the loading logic itself — it was already correct, just made more visible.

### #74 — ABR1 SUCCESS_CLASS_IDX documented (18_ai_abr1_bot.py)
The value `SUCCESS_CLASS_IDX=0` was given a detailed comment: the standard convention would be `1`, but here it's `0` for historical reasons. **Actual correctness must be verified against the training notebook** — if the model was trained on `y=1=success`, this MUST be `1` here. I cannot decide this without access to the training.

### #75 — ABR1 asymmetric thresholds documented (18_ai_abr1_bot.py)
Comment explaining the LONG=0.60/SHORT=0.80 asymmetry (historically more false positives on SHORT setups during bull phases).

### #76 — ABR1 minute filter removed (18_ai_abr1_bot.py)
The filter `retest_candle['open_time'].minute != 0` was ineffective because 1h candles ALWAYS have `minute == 0`. The current (running) candle is already cut off at line 219 via `df = df[df['open_time'] < current_hour_utc]`, so the filter was redundant. Removed + comment for clarification.

## Not a bug / classified as false alarm

### #39 — Pump/dump volume confirmation
After closer review: the price-based alert in block A) of the detector is deliberately a **market notification alert** (e.g. for news events and fast moves), not a trade signal. An additional volume confirmation would reduce sensitivity and change the use case. The ML part in block B) already has volume features integrated into the model.

### #40 — MIS1 only uses 1h
False alarm from my original analysis: MIS1 processes 1h OHLCV data, but tests **all 8 horizon models** (8h/24h/72h/168h × pump/dump) against this data and picks the best one (see `for horizon, cfg in PUMP_MODELS.items()` + `candidates.sort`). That is exactly the designed behaviour.

## Deferred

### #28 — Master bot symbol_cleanup regex
Belongs to `15_ai_master_bot.py` → will be handled in Batch 3.

## Verification
All 6 files parse cleanly.

## Recommendations for later review

- **ABR1 SUCCESS_CLASS_IDX**: Please **verify manually against the training notebook**. If `y=1` denotes winning trades there, this must be changed to `1` here. The current value `0` is only safe if `y=0` was explicitly trained as "success".
- **ATB indicators from DB**: sensible mid-term, but only alongside a retrain of the model. Too risky as a standalone fix.

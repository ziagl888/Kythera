# Batch 5 Report — Market Tracker, Whale & Funding Logger

**Target files:** `19_whale_logger_bot.py`, `20_funding_logger_bot.py`, `23_market_tracker.py`, `core/update_model.py`, `check_funding.py`, `check_whales.py`

## Completed

### #71/#73 — Market Tracker category mapping (23_market_tracker.py)
`get_category()` was inconsistent:
- `TD_*` (Three-Drive from SMC Sniper) was categorized as INDICATOR — belongs to PATTERN
- `BB_*` (Breaker Block) and `QM_*` (Quasimodo) were VOLUME — belong to PATTERN
- `SRA1` was missing entirely from the mapping (belongs to LEVEL)
- `SMC_*` (Forex bot) was not accounted for — now PATTERN

Clean reassignment by signal type: INDICATOR (oscillators/crossover), VOLUME (pure volume), LEVEL (S/R & reversion), PATTERN (SMC/chart patterns/trendline).

### #72 — Market Tracker volume approximation improved (23_market_tracker.py)
`SUM(volume * close)` migrated to `SUM(volume * (open + close) / 2)` (mid-price). Reduces error on candles with a large intra-candle move. The real `quote_volume` from Binance would be better but is not stored in the DB — would be an ingestion schema change.

### #81 — Whale Logger format_usd negative values (19_whale_logger_bot.py)
For `val < 0` (e.g. `-1_500_000`), values fell through all branches and were output as `$-1500000` (raw). Now: sign split off, absolute value formatted, sign prepended → `-$1.5M`.

### #82 — Funding Logger check_top20 None instead of 50.0 (20_funding_logger_bot.py)
On an empty `current_rates_dict` the function returned 50.0 as a "neutral" fallback. That faked sentiment where there was no data. Now: returns `None`. Both call sites (sentiment engine + overview) were migrated to None handling:
- Sentiment engine: skips the alert check
- Overview: shows "N/A" instead of `0.0%`

### #83 — calc_diff_bps None on missing history (20_funding_logger_bot.py)
Previously `return 0.0` on `historical=None` — that was displayed as "+0.0bps" = "stable", even though "no data" was meant. Now: returns `None`. Overview display uses helper `_fmt_bps()` for "N/A" on None.

### #85 — update_model cleanly skips threshold files (core/update_model.py)
Threshold files (`threshold_*.pkl`) contain only a float, no ML model. Previously the call crashed silently inside `except Exception` with `AttributeError: 'float' object has no attribute 'save_model'`. Now:
1. Filename check: if `threshold_*` → explicitly skip
2. Defensive `hasattr(model, "save_model")` check for all other cases (also catches non-threshold files with foreign objects)

## Documented as too minor or not critical

### #80 — Whale Logger shutdown-save race
Under the asyncio single-threaded model and with SIGINT-based shutdown (event loop is stopped beforehand), this is not reproducible in practice. `list(WHALE_TRADES)` is atomic thanks to the GIL. The theoretical race would only be relevant in a real threaded environment, not in asyncio.

### #84 — FUNDING_BY_SYMBOL asyncio race
Same reason: asyncio is single-threaded, dict reassignment is atomic. Between `.get(symbol)` and `timestamps = [r[0] for r in series]` there is no `await`, so no other coroutine can run in between.

## No code fix worthwhile

### #50 — Market Tracker 10.000+ queries
The real fix would be a shared OHLCV table (`ohlcv_30m` with a `symbol` column) instead of separate tables per coin. That's an ingestion schema change (`1_data_ingestion.py`) and would affect all bots — clearly outside the scope of this bug-fix round. Alternative: UNION ALL over 500 sub-queries — that saves no work, only client round-trips. Marked as performance backlog.

## Verification
All 6 files parse cleanly.

## Important for the deploy
- The Funding Logger was extensively refactored (None handling in 4 places). On the first run after deploy, please check that the Telegram outputs are formatted correctly — in particular on the first run when there is no 1h/24h history yet, "N/A" should now be shown instead of "0.0bps" or "50.0%".
- The Market Tracker categorization affects the hourly signal summary — some signals change category. That is intentional and makes the statistics more meaningful.

# Batch 1 Report — Data Ingestion, Monitor & Housekeeping

**Target files:** `1_data_ingestion.py`, `5_trade_monitor.py`, `6_housekeeping.py`, `7_pattern_detector.py`, `8_ai_trade_monitor.py`

## Completed

### #8/#16 — Monitor connection robust (5_trade_monitor.py, 8_ai_trade_monitor.py)
Previously, ONE connection held over the entire bot lifetime. On a DB hiccup, the connection stayed dead and the monitor kept looping with a useless connection. Now:
- Trade monitor: connection is managed in `ensure_conn()`/`reset_conn()` helpers. On exception the dead connection is discarded and rebuilt at the next loop start.
- AI monitor: reconnect block in the `except` handler with fallback (`conn = None` if the reconnect also fails → next loop tries again).

### #14 — DB flusher SAVEPOINT-based (1_data_ingestion.py)
Before: a single faulty row (e.g. missing table for a new coin) rolled back the entire 100-row batch → hundreds of candles lost. Now: every row in its own `SAVEPOINT`, single failures are discarded, all other rows commit cleanly. Logging deduplicated per table (not per row) so the logs don't flood.

### #21 — active_patterns.json atomic write (7_pattern_detector.py)
Previously a direct `open('w')` into the target file. On a concurrent read from another process, a half-written JSON file could be read. Now: write the temp file completely, `fsync`, then `os.replace` for an atomic swap.

### #36 — targets_hit cast defensively to int (8_ai_trade_monitor.py)
Previously the DB value was passed through directly. Depending on schema (TEXT or INTEGER) a string could come back, which aborted `range(new_targets_hit, ...)` with a TypeError. Now: `int(targets_hit)` with fallback 0. Also explicitly `int(...)`-cast at the `INSERT ... VALUES ... targets_hit`.

### #48 — telegram_outbox cleanup (6_housekeeping.py)
New function `cleanup_telegram_outbox(max_age_days=7)` deletes all `sent=TRUE` entries older than 7 days nightly (or, if there is no `created_at` column: all sent entries). Prevents unbounded table growth, which after a few months slowed down the Telegram bot's `SELECT WHERE sent=FALSE`. Call integrated into the nightly 03:00 housekeeping job.

## Already done or not a bug

### #7 — get_live_price uses 5m candle
On reading the code: the monitor deliberately uses the `high`/`low`/`close` of the newest (potentially open) 5m candle to detect wick breaches of SL/TP. That is **intended design**, not a bug. The original critique "open candle instead of ticker" had missed the wick-aware detection intent.

### #23 — pump/dump in-memory cooldown
Noted during review: the cooldown is already persisted in `pump_dump_state.json` (`last_alert_time` per symbol, both for pump/dump and for price-volume alerts). Not a bug, was wrong in my original list.

## Deferred

### #12 — detect_volume_spike_in_period df.loc[index-1]
The function lives in `strategies/strat_volume_indicator.py`, not in the monitor scope. Belongs systematically in Batch 4 (Indicator Engine & Strategies).

## Verification

All 5 files parse cleanly:
- 1_data_ingestion.py ✅
- 5_trade_monitor.py ✅
- 6_housekeeping.py ✅
- 7_pattern_detector.py ✅
- 8_ai_trade_monitor.py ✅

## Recommendations for later review

- The monitor refactoring still uses the outer variable `c` instead of real dependency injection. On a later modernisation I would encapsulate that into a `class Monitor` — but that is P3 and outside the scope.
- Outbox cleanup: if `telegram_outbox` is already very full on the first run, the `DELETE` may run slowly. Possibly batch it with `LIMIT 10000` if the prod DB is large.

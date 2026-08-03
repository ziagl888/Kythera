# Batch 6 Report — Architecture, Charting & Dashboard

**Target files:** `4_telegram_bot.py`, `6_housekeeping.py`, `main_watchdog.py`, `core/trade_utils.py`, `core/state_utils.py` (new), 5 AI bot files for centralisation

## Completed

### #31 — Housekeeping respects outbox references (6_housekeeping.py)
`cleanup_generated_charts` now loads the `DISTINCT image_path` list of all unsent outbox entries before deleting, and then skips files that are still referenced. Prevents charts of still-pending messages from being deleted during a Telegram rate-limit backlog.

### #52 — get_hvn_and_sr_levels centralised (core/trade_utils.py)
The function was duplicated **bit-identically** across 5 bots (9_ai_sr_bot, 10_pump_dump_detector, 12_ai_ats_bot, 13_ai_rub_bot, 14_ai_atb_bot — verified via md5). Now it lives once in `core/trade_utils.py`, all 5 bots import from there. Future changes to HVN/SR logic only need to be made in one place — no more drift risk between bots.

### #68/#87 — Telegram chart deletion under multiple references (4_telegram_bot.py)
New function `try_delete_chart_if_unreferenced(cur, image_path, current_msg_id)` checks via SELECT whether another unsent outbox entry references the same image_path. Only deletes if not. Previously the same chart file could end up twice in the outbox (e.g. with parallel logging from two perspectives) — the first send deleted the file, the second fell back to "text only". Both call sites (`mark_sent` and `mark_failure`) now use the safe helper.

### #70 — Dashboard output to a log file instead of DEVNULL (main_watchdog.py)
Previously `stdout=DEVNULL, stderr=DEVNULL` — when the dashboard crashed, the reason was invisible and the user couldn't debug it. Now `logs/dashboard.log` with append mode and a timestamp header on every restart. `stderr` is redirected to the same stream (STDOUT) so traceback and normal log are together.

### #88 — Central state-persistence helper (core/state_utils.py, new)
New module with `atomic_write_json(filepath, data)` and `atomic_read_json(filepath, default)`:
- Write: temp file + fsync + os.replace (guarantees atomic, crash-safe)
- Read: with default fallback; on a JSON decode error the corrupt file is preserved as `.corrupt` and the default is returned (bot keeps running instead of crashing)
- Automatic creation of the target directory

The bots that already have their own atomic-write patterns (from Batch 1/4) can be consolidated onto this helper later. For this iteration: the new module is in place and available for new integrations — existing bots remain untouched with their tested logic (no refactor risk).

## Documented as false alarms / non-critical

### #43 — SMC Forex hardcoded "20x-10x"
The SMC Forex bot hardcodes `20x-10x` leverage. That's intentional — it uses yfinance tickers (`GC=F`, `JPY=X`, etc.) that aren't in `max_leverage.json`. For TradFi assets, conservative leverage is intended, not a bug.

### #54 — SMC ML sniper Pine Script emulation
Code review found no Pine Script idioms (ta.barssince, ta.valuewhen, etc.) that were problematically emulated. The original point was speculative.

### #57 — Quasimodo unused config
All top-level constants (`MIN_CONFIDENCE`, `ZONE_TOLERANCE`, `PIVOT_WINDOW`, `PRICE_BASED_INDICATORS`, `ABSOLUTE_INDICATORS`) are used in the bot. No dead code.

### #90 — active_trades_master vs ai_signals FK
The two tables are intentionally parallel (conventional trades vs AI trades), no FK relationship intended. They're only merged in the Market Tracker via UNION. No inconsistency.

## Verification
**All 47 Python files in the project parse cleanly** (including the 5 bots after removal of the duplicated get_hvn_and_sr_levels).

## Closing recommendations

- The 7 remaining state files (active_patterns, alerted_qms, trendline_state, pump_dump_state, indicator_state, funding_history, and a few others) could be migrated onto `core.state_utils` in a later refactor. That's low priority though — the atomic write logic is already correctly implemented in the changed bots.
- The new `logs/dashboard.log` should be periodically checked/rotated (currently append-only). If the dashboard runs stably it doesn't need this — but the bot shouldn't let the file grow unbounded. Recommendation: set up a logrotate config.

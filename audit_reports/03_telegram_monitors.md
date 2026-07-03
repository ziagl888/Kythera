# Agent 3: Telegram + Trade Monitors (4_telegram_bot.py, 5_trade_monitor.py, 8_ai_trade_monitor.py, handlers/open_handler.py)

### [SEVERITY: CRITICAL] [CATEGORY: data-integrity] Outbox is at-least-once: duplicate sends to the Cornix channel on commit failure, crash, or send-timeout
- **File:** 4_telegram_bot.py:240-264 (send → mark → commit), 278-324 (error paths)
- **Problem:** Delivery protocol: send_message → mark_sent → commit. Three duplicate vectors: (1) commit fails after successful send → row re-fetched (sent=FALSE) → re-sent every iteration during DB flap; (2) crash/kill between send and commit → re-sent after restart; (3) telegram TimedOut after TG accepted the message is handled as failure → mark_failure → retried up to 3 times → up to 3 duplicates.
- **Failure scenario:** Entry signal to Cornix channel sent; PG restarts before commit → re-send → Cornix opens trade TWICE on Binance (double position/risk).
- **Fix:** 'sending' state committed BEFORE send; on restart, rows stuck in 'sending' go to dead-letter/manual queue for trading channels; treat TimedOut as unknown-outcome, not retryable failure.
- **Confidence:** high | **DB-phase:** yes (look for historical duplicate outbox content)

### [SEVERITY: HIGH] [CATEGORY: bug] No staleness TTL on outbox — hours-old trading signals blasted after downtime
- **File:** 4_telegram_bot.py:183-194
- **Problem:** Fetch takes every sent=FALSE AND failed=FALSE row regardless of age; created_at never used. After hours of downtime all queued signals get posted at long-gone prices.
- **Fix:** per-channel max-age (e.g. 15 min for signal channels), mark older failed=TRUE last_error='expired'. Config-driven.
- **Confidence:** high | **DB-phase:** yes (created_at → sent lag distribution)

### [SEVERITY: HIGH] [CATEGORY: bug] Trailing stop in 5_trade_monitor never trails — update_trade_level passed the OLD SL for levels 2/3
- **File:** 5_trade_monitor.py:246-247
- **Problem:** On TP2/TP3 the code passes trade['sl'] (current DB value) instead of the previous target → SL never advances. 8_ai_trade_monitor.py:204/226 does it correctly (new_sl = targets[new_targets_hit - 2]) → drift between the two monitors.
- **Failure scenario:** LONG hits TP1/TP2/TP3 then retraces fully → recorded close at breakeven instead of TP2-locked profit. All multi-target PnL/win stats systematically wrong.
- **Fix:** for new_level >= 2 pass targets[new_level - 2].
- **Confidence:** high | **DB-phase:** yes (does anything read active_trades_master.sl to post Cornix SL updates? then CRITICAL)

### [SEVERITY: HIGH] [CATEGORY: bug] Per-channel FIFO breaks on transient send failure — dependent messages out of order
- **File:** 4_telegram_bot.py:305-314, 316-324
- **Problem:** On non-RetryAfter errors the failed message is retried in a LATER batch while successors of the same channel send now. Entry signal (fails once) vs SL-update (sends now) → Cornix sees SL-update before its signal.
- **Fix:** after retryable failure, block that channel for the rest of the batch (blocked_channels set).
- **Confidence:** high | **DB-phase:** no

### [SEVERITY: HIGH] [CATEGORY: bug] `:.6f` price formatting corrupts sub-0.001 coins in Cornix signal text
- **File:** handlers/open_handler.py:104-110
- **Problem:** Fixed 6 decimals → for micro-priced coins entries/SL/TPs rounded by double-digit %, adjacent TPs collapse to identical strings → Cornix rejects or executes at wrong levels. Also "CMP Entry" label used even for LIMIT entries.
- **Fix:** significant-digit formatting (or Binance tickSize per symbol), strip trailing zeros; conditional label.
- **Confidence:** high (math) / medium (affected coins) | **DB-phase:** no (check symbol universe for sub-0.001 prices)

### [SEVERITY: HIGH] [CATEGORY: bug] 8_ai_trade_monitor: int > str TypeError if current_target_hit column is TEXT — whole monitor tick dies
- **File:** 8_ai_trade_monitor.py:265
- **Problem:** `new_targets_hit > (targets_hit or 0)` compares int vs raw value; code elsewhere defends TEXT ('0' or 0 → '0') → TypeError → outer except → entire iteration aborts → monitor effectively dead for ALL trades while such a row exists.
- **Fix:** old_targets_hit = int(targets_hit or 0) with try/except; compare against that.
- **Confidence:** high (code) / medium (live column type) | **DB-phase:** yes (\d ai_signals)

### [SEVERITY: MEDIUM] RetryAfter on one channel stalls the ENTIRE outbox for the flood wait
- **File:** 4_telegram_bot.py:266-292 — per-channel block already set, but also global sleep + batch abort. Fix: drop global sleep/abort; per-channel timestamp block suffices.

### [SEVERITY: MEDIUM] No FOR UPDATE SKIP LOCKED / singleton guard — two consumer instances double-send everything
- **File:** 4_telegram_bot.py:183-194. Fix: pg_advisory_lock at startup and/or FOR UPDATE SKIP LOCKED.

### [SEVERITY: MEDIUM] Monitors only inspect newest 5m candle — SL/TP hits during downtime permanently missed
- **File:** 5_trade_monitor.py:152-176; 8_ai_trade_monitor.py:82-110. ORDER BY open_time DESC LIMIT 1. Fix: persist last_checked_open_time, scan forward on catch-up, SL-first per candle.
- **DB-phase:** yes

### [SEVERITY: MEDIUM] Unguarded close race with other writers: double rows in closed tables, lost SL updates
- **File:** 5_trade_monitor.py:31-65, 76-85; 8_ai_trade_monitor.py:245-276. Snapshot read → INSERT closed + DELETE by id without guard; housekeeping DELISTED close races. Fix: DELETE ... RETURNING * first, skip INSERT if 0 rows; optimistic guard on update_trade_level.
- **DB-phase:** yes (duplicate coin+time rows in closed tables)

### [SEVERITY: MEDIUM] `posted` timezone fix likely ineffective: tz-aware UTC into TIMESTAMP WITHOUT TIME ZONE
- **File:** 5_trade_monitor.py:22-25 DDL vs 33-38/59. PG converts to session TimeZone before stripping → local wall time again if session TZ is Europe/Vienna. Fix: TIMESTAMPTZ migration, or pin `-c timezone=UTC` in pool options (core/database.py:42), or insert naive UTC.
- **Confidence:** medium | **DB-phase:** yes (SHOW timezone)

### [SEVERITY: MEDIUM] SHORT trade with sl=0 placeholder instantly "stopped out" at price 0 (+100% PnL recorded)
- **File:** 5_trade_monitor.py:216-225. sl_hit = high >= sl always true for sl=0 SHORT → close at 0.0, fantasy +100%. LONG mirror: SL never hits. Fix: guard sl > 0.
- **DB-phase:** yes (count active trades with sl<=0/NULL)

### [SEVERITY: MEDIUM] Permanently-failing messages dropped after 3 attempts, no alerting — lost Cornix signal is silent
- **File:** 4_telegram_bot.py:22, 124-144, 305-314. Deterministic errors (HTML entities, too long) fail 3× → failed=TRUE, only log trace. Fix: retry once with parse_mode=None; operator alert on final failure for signal channels.

### [SEVERITY: MEDIUM] telegram_outbox grows unbounded; hot query has no supporting index
- **File:** 4_telegram_bot.py:40-71. Fix: partial index ON (id) WHERE sent=FALSE AND failed=FALSE + retention job. **DB-phase:** yes (size, EXPLAIN, purge?)

### [SEVERITY: MEDIUM] N+1 candle queries per 10s tick in both monitors
- One SELECT LIMIT 1 per active coin per tick, serial. Fix: latest_candles upsert table maintained by ingestion → 1 query/tick. **DB-phase:** yes (tick duration)

### [SEVERITY: LOW] 5_trade_monitor advances max one target level per tick (8_ai loops correctly)
- 5_trade_monitor.py:230-249. Fix: loop consecutive targets per candle.

### [SEVERITY: LOW] Trade closed with status "4" when fewer than 4 targets exist
- 5_trade_monitor.py:246-249. Fix: close with str(new_level).

### [SEVERITY: LOW] String-built SQL with table names from DB rows
- 5_trade_monitor.py:156, 8_ai_trade_monitor.py:86, core/trade_utils.py:55,264. Fix: regex-validate coin or psycopg2.sql.Identifier.

### [SEVERITY: LOW] "@None" attribution when poster has no TG username
- handlers/open_handler.py:95-96,111. Fix: (user.username or user.full_name) if user else "Trader".

### [SEVERITY: LOW] Blocking requests.get / file IO inside async handlers
- open_handler.py:20,119; 4_telegram_bot.py:243. Fix: asyncio.to_thread; PermissionError on chart → retry without counting attempt.

### [SEVERITY: LOW] Chart file deleted before sent=TRUE commit — failed commit loses image for retry
- 4_telegram_bot.py:117-121. Fix: delete after successful commit.

### [SEVERITY: LOW] (core) Pool has lock_timeout but no statement_timeout; session timezone unpinned
- core/database.py:42. Fix: `-c lock_timeout=30000 -c statement_timeout=60000 -c timezone=UTC`.

## Cross-cutting observations
1. Outbox at-least-once by construction, nothing downstream duplicate-tolerant; needs explicit stance ('sending' limbo for trading channel).
2. 5_trade_monitor and 8_ai_trade_monitor ~70% same algorithm with divergent semantics → consolidate into core/.
3. Monitors are simulation-side, never emit outbox messages ("STUMM"); if anything posts SL updates to Cornix from active_trades_master.sl, trailing/sl=0 findings become CRITICAL.
4. Error philosophy "log and keep looping" — no dead-letter surfacing, no metrics, no alert path.
5. Positive: per-channel rate limiter design sound; wick-aware SL-first correct; stale-candle guard good; chart refcount fix shows race understood.

## Questions for live-DB phase
1. SHOW timezone; column types closed_trades_master.time/posted, ai_signals.current_target_hit (TEXT vs INT), closed_ai_signals schema.
2. Which processes write active_trades_master.status/sl; does anything post SL updates to Cornix from those columns?
3. telegram_outbox: row count, retention, indexes, max created_at→now for unsent, last_error histogram.
4. Duplicate evidence: identical message text sent twice within minutes (attempts>0 AND sent=TRUE = smoking gun); duplicate coin+entry in closed tables.
5. active trades with sl<=0/NULL; trades open > N days.
6. Symbols with price < 0.001; Cornix tolerance for "CMP Entry" on LIMIT.
7. Monitor tick duration under load; pg_stat_statements on _5m LIMIT-1 queries.

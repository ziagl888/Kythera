# Agent 1: Core Infra + Watchdog + main_telegram_bot

### [HIGH] [bug] core/update_model.py silently overwrites .pkl/.joblib models in place, destroying the original
- core/update_model.py:35. filename.replace(".model","_v2.json") no-op for *_model.pkl names → new_filename==filename → save_model overwrites production pickle with XGB-native format. Prints "Erfolg".
- Fix: splitext-based name + hard-refuse if new==old.

### [HIGH] [security] Telegram permission system fails OPEN on any config-load error
- core/bot_utils.py:12-18 (+ main_telegram_bot.py:48). load_config error → {"*": ["*"]} = everyone-everything. open() without encoding="utf-8" → cp1252 UnicodeDecodeError on emoji → wildcard perms. !open builds real trade signal.
- Fix: fail closed (deny-all) + alert; encoding="utf-8".

### [HIGH] [robustness] Watchdog backoff time.sleep freezes entire monitor loop (up to 15 min/crashing bot/cycle)
- main_watchdog.py:299-303. During sleep: no other restarts, no park/restart consumption, no dashboard checks. After sleep start_process without re-checking is_parked.
- Fix: per-process not_before timestamp; re-check is_parked before every start_process.

### [HIGH] [robustness] No single-instance guard on watchdog → duplicate fleet → duplicate Cornix trades
- taskkill /F doesn't run SIGTERM handler → orphaned children; new watchdog spawns 2nd fleet → every signal twice → Cornix double-executes. No orphan detection at startup.
- Fix: named mutex/pidfile at watchdog AND bot startup; Windows Job Object KILL_ON_JOB_CLOSE.
- DB-phase: duplicate outbox/active_trades rows as evidence.

### [HIGH] [bug] PooledConnection.close() not idempotent — double-close poisons pool or kills another thread's live connection
- core/database.py:83-95. 2nd close: rollback on conn possibly held by other thread; putconn raises PoolError; except then physically closes conn sitting in pool. No health check in getconn. HOTFIX_README documents prior production double-close incident.
- Fix: _returned flag; error path putconn(conn, close=True).

### [HIGH] [robustness] Pool slot leak when rollback() fails on dead connection — pool exhausts permanently
- core/database.py:86-95. Server-dropped conn: closed=False client-side, rollback raises → putconn never called → slot leaked. 8 events → PoolError forever; bot looks healthy, produces nothing. getconn has no liveness check → after DB restart every pooled conn dead on first use.
- Fix: except path putconn(conn, close=True) in own try.
- DB-phase: yes

### [HIGH] [robustness] Fleet connection budget: 27 × maxconn 8 + workers vs PG max_connections
- core/database.py:22-23. Worst case >216 conns vs default 100. minconn=2 → ~60+ idle baseline. Crash-restart bursts (stagger only at boot) → "too many clients" → restart storm.
- Fix: MIN 0-1/MAX 2-3 env-overridable for single-threaded bots, or pgBouncer.
- DB-phase: yes (max_connections, pg_stat_activity by app)

### [MEDIUM] [bug] `with conn:` semantics changed from psycopg2 (commit-on-exit) to rollback-and-release
- core/database.py:74-79. __exit__ → close() → rollback + putconn. Docstring claims backwards compat — false. Any `with get_db_connection() as conn:` without explicit commit silently loses writes. Spot-checked sites commit explicitly; trap armed for future.
- Fix: mirror psycopg2 semantics (commit on success) or warn when rolling back non-idle txn.

### [MEDIUM] [concurrency] Pool returns connections with autocommit=True still set — session-state contamination
- Six bots set conn.autocommit=True (16:307, 18:391, 24:71, 11:190, 10:732, 25:156); putconn/close never reset → later transactional code gets autocommit conn → non-atomic multi-statement writes, no-op rollback.
- Fix: reset autocommit=False in close() before putconn.

### [MEDIUM] [data-integrity] Cooldown write uses DB NOW() while read assumes naive timestamps are UTC — off by server timezone
- core/market_utils.py:104-135. update_cooldown: NOW() (server TZ); check_cooldown: datetime.now(utc) vs naive assumed-UTC. Non-UTC server TZ → cooldowns too long (suppressed signals) or too short (duplicate signals).
- Fix: write client-side aware UTC or NOW() AT TIME ZONE 'UTC'; timestamptz.
- DB-phase: yes (SHOW timezone, \d trade_cooldowns)

### [MEDIUM] [bug] calculate_smart_targets swallows own "Insufficient data" guard and returns fabricated levels
- core/trade_utils.py:59, 236-245. ValueError raised inside same try whose except returns %-template (sl=e1*0.92, tp=e1*1.05). Caller can't distinguish computed vs fabricated. Same fallback on any DB error.
- Fix: return None/raise; % fallback only behind explicit opt-in.

### [MEDIUM] [concurrency] is_trade_already_active is check-then-act across 27 processes — duplicate active trades possible
- core/market_utils.py:83-95. No advisory lock, presumably no unique constraint.
- Fix: partial unique index (coin,direction,strategy) WHERE status='WORKING' + INSERT ON CONFLICT.
- DB-phase: yes (\d active_trades_master, historical dups)

### [MEDIUM] [bug] update_cooldown commits the caller's whole transaction as side effect
- core/market_utils.py:135. conn.commit() on borrowed conn mid-transaction → partial writes persisted; subsequent rollback no-op.
- Fix: remove commit, document caller-commits, or dedicated short-lived conn.

### [MEDIUM] [bug] calculate_obv compares multi-hour OBV change against ±2σ band of 1-hour changes
- core/market_utils.py:179-187. k-hour walk has √k× σ of one step → long windows almost always "diverge" → gate ~coin-flip on window length. strat_main_channel gates LONG on it. Baseline includes signal window.
- Fix: scale band by window length (mean*k ± 2σ√k) or per-hour normalize; exclude window from baseline.
- DB-phase: yes (trigger rate vs window length)

### [MEDIUM] [security] Table names f-string interpolated (market_utils.py:162-164; trade_utils.py:55,263-265). Fix: central regex gate or sql.Identifier.

### [MEDIUM] [robustness] Windows terminate() is hard kill — no graceful shutdown; 5s escalation dead code
- main_watchdog.py:169-181, 306-311. TerminateProcess: no txn finish/state flush. Indicator engine's ProcessPool workers survive parent kill → old workers write concurrently with restarted engine.
- Fix: CREATE_NEW_PROCESS_GROUP + CTRL_BREAK_EVENT first, then kill; or Job Object.

### [MEDIUM] [robustness] Watchdog has no hang detection — wedged bot stays green forever
- main_watchdog.py:294-304 + database.py:42 (no statement_timeout, no keepalives). Half-open TCP → bot blocks in execute() for hours; watchdog/dashboard show running.
- Fix: statement_timeout + keepalives in pool options; heartbeat file/DB row per cycle, watchdog restarts on staleness.
- DB-phase: yes (ancient active/idle-in-transaction sessions)

### [MEDIUM] [robustness] atomic_write_json fails on Windows when concurrent reader holds target open — update silently dropped
- core/state_utils.py:50. os.replace PermissionError; callers ignore False return. Fixed .tmp name → two concurrent writers interleave → corrupted JSON installed.
- Fix: unique tmp name (NamedTemporaryFile), retry replace on PermissionError.

### [LOW] main_telegram_bot username resolution: dead branch + None username → all channel posts authenticate as shared "Channel_Admin" string (main_telegram_bot.py:45). Fix: key channel perms by chat id.
### [LOW] log_command_atomic rewrites entire history file per command; concurrent writers lose entries (bot_utils.py:45-71). Fix: JSONL append.
### [LOW] _ch()/DB_PORT crash on empty-string env vars at import (config.py:29,47-49). Fix: int(os.getenv(name) or "0").
### [LOW] Watchdog log unrotated; dashboard restart leaks file handles; no dashboard-crash backoff (main_watchdog.py:14,93-101,120-125).
### [LOW] atomic_read_json docstring promises rewrite of default state file — code doesn't (state_utils.py:71-73).
### [LOW] get_max_leverage silently defaults 20x for unknown symbols; cache never refreshes; _LEVERAGE_MAP_PATH unused (market_utils.py:44-70). DB-phase: diff max_leverage.json vs coins.json.
### [LOW] warnings.filterwarnings pandas suppression as import side effect of core.database (database.py:14).
### [LOW] Stale restart markers: parked bots fire spurious restart on unpark; markers for removed scripts linger (main_watchdog.py:269-284).
### [LOW] find_support_resistance_zones iterrows over 2160 rows; FVG mitigation O(n²) worst 500k iters (market_utils.py:222; trade_utils.py:123-146).
### [LOW] pretty_name misses mixed-case pump/dump suffixes (bot_naming.py:33). DB-phase: SELECT DISTINCT strategy.
### [LOW] close_pool dead code; emoji logs risk UnicodeEncodeError on cp1252 consoles (database.py:153; logging_setup.py:45).

## Cross-cutting observations
1. Everything is cwd-relative (bot_config.json, coins.json, logs/, Popen paths). Wrong WorkingDirectory → load_coins returns [] silently → bots healthy over zero coins. process_control.py is positive counter-example (__file__ anchor).
2. At-least-once outbox delivery to Cornix + hard TerminateProcess kills → duplicate window hit by design (scheduled recycles, parking), not only accident.
3. Per-symbol tables (~4300) force dynamic SQL everywhere = root cause of f-string pattern.
4. Error philosophy log-and-continue: silent signal degradation invisible to watchdog (crash=detected, degradation=invisible).
5. Parking/restart marker design sound. Harden PooledConnection at one choke point (idempotent close, autocommit reset, slot deregistration) instead of auditing 27 bots forever.

## Questions for live-DB phase
1. SHOW max_connections; pg_stat_activity count by application_name; stale sessions.
2. SHOW timezone; \d trade_cooldowns; stored values vs UTC.
3. \d active_trades_master: unique index on WORKING rows? historical duplicates?
4. telegram_outbox: index on (sent,failed,id)? bloat? duplicate messages in short windows (double-fleet evidence)?
5. DISTINCT strategy names un-normalized (bypass pretty_name)?
6. pg_stat_activity idle-in-transaction hours / long active queries.
7. max_leverage.json coverage vs coins.json (which symbols get silent 20x default).

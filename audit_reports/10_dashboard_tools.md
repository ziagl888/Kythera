# Agent 10: Dashboard + Tools (dashboard.py, core/charting.py, core/process_control.py, tools/*)

### [CRITICAL] [security] Dashboard binds 0.0.0.0 with zero authentication — anyone reaching port 5000 can stop the trading fleet
- dashboard.py:1152 (bind), 290-317 (control endpoints). No auth/token/allowlist on /api/process/<script>/stop|start|restart, /api/system/stop_all|restart_all|start_all, log endpoints. Docstring claims localhost.
- Failure: POST /api/system/stop_all from anywhere → persistent park markers → fleet down, stays down across reboots. Log endpoints leak strategy behavior.
- Fix: bind 127.0.0.1 (+SSH tunnel) or token auth in before_request + firewall. Verify firewall live.
- Confidence: high (code), medium (reachability) | DB-phase: verify port exposure on VPS

### [HIGH] [security] CSRF on all state-changing endpoints — webpage in operator's browser can stop fleet even if firewalled
- dashboard.py:290-317. POST but no CSRF token, no Origin/Host validation → simple-request CSRF + DNS rebinding.
- Fix: before_request check Origin/Referer/Host, or require custom header (forces preflight). 3 lines.

### [HIGH] [bug] Dashboard PROCESSES list drifted from watchdog — 4 live bots invisible; "Stop All" leaves Signal Orchestrator running
- dashboard.py:31-62 vs main_watchdog.py:52-66. Missing: 26_regime_detector, 27_bot_regime_analyzer, 28_signal_orchestrator, 29_ufi1_bot.
- Failure: Stop All → orchestrator/regime bots keep acting on half-dead pipeline; crashes invisible in UI.
- Fix: single shared core/fleet.py consumed by both; short-term add the 4 entries.

### [HIGH] [robustness] Dashboard log streaming holds bot log files open on Windows — breaks RotatingFileHandler rollover
- dashboard.py:244-256 vs core/logging_setup.py:50-54. Windows rename of open file (no FILE_SHARE_DELETE) → doRollover fails on every emit once >10MB while Logs tab open; records dropped. Streamer also keeps stale inode after rotation.
- Fix: poll with re-open + remembered offset (detect truncation by size<offset); re-check path each iteration.
- Confidence: medium-high

### [HIGH] [performance] /api/status does 25 full psutil process sweeps + ~3s of hard sleeps per call, per tab, every 6s
- dashboard.py:104-128,131-159,162-179,285-287. _find_pid_for_script full process_iter per bot; cpu_percent(interval=0.1) blocking per running bot; get_system_stats +0.2s; SSE poller every 5s.
- Fix: one process_iter sweep per refresh → filename→pid map; cpu_percent(interval=None); server-side snapshot cache ~2s.

### [MEDIUM] [robustness] SSE log-stream threads never detect client disconnect while log silent — thread + handle leak
- dashboard.py:249-256. No heartbeat (unlike /api/events). Fix: heartbeat comment every N idle iterations.

### [MEDIUM] [bug] stream_log crashes mid-stream when log file doesn't exist yet
- dashboard.py:246-249. yields Waiting… then open() → FileNotFoundError → stream dies, never recovers. Fix: loop-wait until file exists.

### [MEDIUM] [robustness] "Restart All"/"Start All" bypass boot stagger — 25 bots respawn in one watchdog cycle, hammering DB
- dashboard.py:212-227; main_watchdog.py:279-284. start_delay only applies at boot; restart markers all consumed in one 10s cycle. Dialog claims "(gestaffelt)".
- Fix: watchdog respects stagger in consume-restart/missing-process branches (e.g. max 1-2 starts per cycle).
- DB-phase: yes (connection spikes during restart_all; max_connections vs 29 bots × pool max 8)

### [MEDIUM] [robustness] Watchdog crash-backoff sleeps up to 900s inside monitor loop — park/restart intents frozen fleet-wide
- main_watchdog.py:299-302. time.sleep(delay) blocks whole supervision loop; dashboard promises <=10s.
- Fix: per-bot not_before timestamp instead of sleeping.

### [MEDIUM] [robustness] Regression guard: unarmed state and golden-deletion both pass silently — pre-commit hook currently protects nothing
- tools/regression_guard/guard.py:128-137; fixtures/golden only .gitkeep. NOT ARMED → pass; golden deleted → silent disarm.
- Fix: run extract+refresh promptly (DB phase!); if manifest.json exists but goldens missing → exit 1.
- DB-phase: yes (extract is the live-DB step)

### [MEDIUM] [code-quality] db_schema_analysis.py duplicated; tools/ copy stale and cannot run from its own location
- tools/ copy: sys.path.insert points to tools/ → core import fails always. Root copy canonical (got ruff cleanup).
- Fix: delete tools/db_schema_analysis.py.

### [LOW] [security] SQL f-string table identifiers from symbol names — core/charting.py:138-143; rgcore.py:130-132. Fix: regex validate or sql.Identifier.
### [LOW] [robustness] Park/stop endpoints accept arbitrary script names — dashboard.py:199-209,295-302. Fix: 404 if script not in SCRIPT_MAP.
### [LOW] [bug] /api/logs unvalidated n param — 500 on non-int, near-full dump on negative; readlines whole file. Fix: clamp.
### [LOW] [robustness] Stale restart marker survives parking; consume-then-crash loses restart — main_watchdog.py:269-284. Fix: consume_restart in parked branch.
### [LOW] [robustness] Guard BLAS pin uses setdefault — pre-existing OMP_NUM_THREADS defeats determinism → spurious FAILs. Fix: overwrite unconditionally.
### [LOW] [robustness] Guard: fixture without golden silently unchecked; refresh hint uses POSIX env syntax on Windows. Fix: flag as breach; print both syntaxes.
### [LOW] [bug] describe_project.py: tree -I is GNU syntax → Windows tree.com garbage, no fallback (only FileNotFoundError). Dumps full source recursively incl. no .git skip → info-leak footgun. Requires GUI.
### [LOW] [code-quality] Dashboard misc: except (AttributeError, Exception); import-time thread start; disk_usage("/") on Windows; ambiguous PID matching (any python process with same basename, e.g. second checkout).

## Explicit non-findings (checked, clean)
- charting.py figure leaks: NONE — finally + plt.close('all'), Agg backend, _CHART_LOCK. Temp charts cleaned by housekeeping (2h max age) — residual risk only if housekeeping parked.
- XSS: clean (textContent/JSON.parse).
- Path traversal: clean (SCRIPT_MAP validation, marker filename sanitization).
- Flask vs psycopg2 pool: dashboard imports NO DB code at all — zero queries per refresh.
- db_schema_analysis SQL: static catalog queries, read-only, no injection.
- Guard drift detection when armed: proven by smoke mode (perturbation assert).

## Cross-cutting observations
1. Config duplication is dominant systemic risk: PROCESSES twice (drifted by 4 bots), log-name convention re-derived, db_schema_analysis twice → core/fleet.py single source.
2. Intent-marker process-control design genuinely good; weaknesses at edges.
3. Security posture binary on firewall; 127.0.0.1 bind is one-line neutralizer.
4. Dashboard CPU-heavier than it looks.
5. Regression guard well-engineered but currently no-op — arm before further indicator refactors.

## Questions for live-DB phase
1. Is port 5000 reachable externally? (Test-NetConnection / firewall inbound rules)
2. PG max_connections; pg_stat_activity peak during Restart All.
3. Run guard.py extract + refresh against live DB; which 4 coins/6 TFs have warm tables >=600 bars?
4. db_schema_analysis regex only counts USDT tables — do non-USDT tables exist (forex/metals bot 16)?
5. Any logs/*.log currently >10MB (rotation stuck = confirms Windows rename-block)?
6. Does chart_data_service enforce symbol charset?

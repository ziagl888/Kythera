# Agent 12: Cross-Cutting Sweep (Secrets, SQL, Datetime, Schema-Map, Deps, Duplication, Outbox-Contract)

### [HIGH] [datetime] Cooldown circuit breaker of the classic strategies compares naive local time against the UTC-written posted column
- strategies/strat_fast_in_out.py:42-48, strategies/strat_5_percent.py:25-29. Writers (5_trade_monitor:38 UTC-FIX, 6_housekeeping:164, 28) write UTC; strategies compare datetime.now() local. CET/CEST: 3h window only covers 1-2h → mass win-block fires too late/never.
- Fix: aware UTC in both strat files (like the 5_trade_monitor fix).
- DB phase: SELECT max(posted), now() — offset visible?

### [HIGH] [schema|datetime] trade_cooldowns DDL drift ×4: WITH vs WITHOUT TIME ZONE — bootstrap order determines cooldown semantics
- 11:445-451, 24:425-431, 25:524-530 (WITH TZ) vs 26_regime_detector.py:194-200 (WITHOUT). Writer NOW() (timestamptz), reader interprets naive as UTC → WITHOUT + server TZ Vienna → cooldowns 1-2h longer.
- Fix: canonical DDL (timestamptz) in core; ALTER migration.
- DB-phase: \d trade_cooldowns, SHOW timezone.

### [HIGH] [datetime] active_trades_master.time/posted written naive-local (3_detectors:54,117), but compared against NOW()/aware UTC (9_ai_sr:248, 23:399)
- PG TZ=UTC + VPS=CEST → 60-min window becomes 2h+ → duplicate AI re-evaluation; 24h stats shifted.
- Fix: lift 3_detectors to UTC; timestamptz long-term.

### [MEDIUM] [schema|telegram] telegram_outbox DDL drift: 3_detectors creates the table without image_path; ensure_schema does NOT migrate image_path in afterwards
- 3_detectors.py:103,141 vs 4_telegram_bot.py:51-70 (ALTERs only attempts/failed/last_error/created_at).
- Fresh DB + 3_detectors first → all ~15 chart bots crash with UndefinedColumn.
- Fix: ALTER ADD COLUMN IF NOT EXISTS image_path in ensure_schema; align/remove the narrow CREATEs.

### [MEDIUM] [deps] requirements.txt completely unpinned (all 20 packages)
- pandas_ta fragile with numpy>=2; PTB major breaks; xgboost pkl version-sensitive. 9_ai_sr:158 comment shows: this class of bug has already struck. No lockfile.
- Fix: pip freeze as requirements.lock.txt; at least major pins.

### [MEDIUM] [schema] ai_signals (13 writers) and ml_predictions_master (9 writers) have NO DDL in the repo — schema lives only in the live DB
- No unique backstop detectable; dedup is app-side SELECT-then-INSERT without ON CONFLICT.
- Fix: pg_dump --schema-only as docs/schema.sql; unique index + ON CONFLICT DO NOTHING.
- DB phase: dump constraints, measure duplicate rate.

### [MEDIUM] [datetime|schema] closed_ai_signals.close_time: NOW() (8:247, 6:201) and Python UTC param (28:729) mixed across three writers
- Server TZ ≠ UTC → close_times off by an offset → regime analyzer/tracker durations skewed.
- Fix: uniformly UTC param or NOW() + timestamptz.

### [LOW] [sql] f-string SQL only with internal table names (~15 sites) — no injection path, quoting inconsistent. No %-format/.format()/concat SQL found; all values parametrised. Optional sql.Identifier.

### [LOW] [security] Secrets/git hygiene CLEAN: .env never committed, history clean (12 commits, pickaxe empty), gitleaks with no holes, no hardcoding. Remainder: 27 .pkl models committed (pickle=code exec; trusted source, PR doesn't see binary diffs). Optional SHA256 manifest.

### [LOW] [exceptions] 1 bare except: (backtest/smc_btc_backtest.py:307); ~43 pass/continue swallows, mostly a cleanup pattern. Substantive candidate: core/trade_utils.py:103 HVN calculation swallows silently → signal without an HVN level, with no visibility.

### [LOW] [duplication] db_schema_analysis root vs tools (tools excluded from ruff → keeps drifting). load_coins ×6 with semantic drift (core: raw; chart: dedup; fib: fallback BTC/ETH; qm: USDT filter). fetch_db_data ×6, send_cornix_signal ×3, get_live_price ×3. Positive: get_db_connection/send_telegram/cooldowns centralised.

### [LOW] [logging] Three unrotated sinks: 2_indicator_engine (indicator_calculation.log root), main_watchdog (watchdog.log), dashboard.log Popen pipe. Fix: setup_logging everywhere; truncate in housekeeping.

### [LOW] [schema] ml_predictions_master: 9_ai_sr:297 deviating column list (5 instead of 8 columns — time/direction/entry NULL).

## Table → writer/reader map (dimension 5)
- telegram_outbox: ~19 writers (by design a queue), consumer 4, cleanup 6 (7 days). Contract breach: narrow DDL only in 3.
- ai_signals: 13 writers (7,9,10,11,12,13,14,15,18,24,25,28,29), NO ON CONFLICT, no DDL.
- ml_predictions_master: 9 writers, no DDL, 1 deviating column list.
- active_trades_master: writer only 3_detectors; DELETE by 5,6,28.
- closed_trades_master: 3 writers (5,6,28) — columns identical, posted UTC ok.
- closed_ai_signals: 3 writers (6,8,28) — close_time mixed (finding).
- trade_cooldowns: centralised (market_utils), but DDL drift ×4.
- regime_*: clean, ON CONFLICT ok.
- {sym}_{tf} OHLCV: writer 1 + 6 (gap-fill), ON CONFLICT ok at 1.

## Outbox contract (dimension 9)
- ALL signal bots go through the outbox; direct API only on the consumer side (4, handlers, main_telegram_bot). ✔

## Cross-cutting observations
1. Repo mid-remediation wave; remaining findings mostly "fix on one side of the contract, other side forgotten". strategies/, handlers/, tools/ are EXCLUDED from ruff — that's exactly where the unfixed remainder sits → exclude set = remediation backlog.
2. Schema ownership is the structural core problem: CREATE TABLE scattered across ~10 files with drift; the most important tables have no DDL. A core/schema.py or schema.sql + migration runner would fix three findings structurally.
3. Timezone policy is only a per-file convention. Recommendation: core utc_now() + ruff DTZ rules (flake8-datetimez) in pyproject.
4. CI is minimal (AST parse + import smoke + secret grep); ruff check as a CI job would be free.
5. Secrets hygiene exemplary.

## Questions for live-DB phase
1. SHOW timezone — decides which of the three TZ findings actually bite live, and in which direction.
2. \d trade_cooldowns — which variant won? Offset visible?
3. \d telegram_outbox — image_path present? failed rows + last_error?
4. \d ai_signals/ml_predictions_master — constraints/indexes? Duplicate rate?
5. SELECT max(posted), now() AT TIME ZONE 'UTC' FROM closed_trades_master.
6. Row counts + indexes of the hot tables (outbox sent index; closed_trades posted index for strat COUNT at 538 coins).
7. Orphaned tables from earlier bot generations (does anyone read pump_dump_events?).

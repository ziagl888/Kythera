# Agent 12: Cross-Cutting Sweep (Secrets, SQL, Datetime, Schema-Map, Deps, Duplication, Outbox-Contract)

### [HIGH] [datetime] Cooldown-Circuit-Breaker der klassischen Strategien vergleicht naive Lokalzeit gegen UTC-geschriebene posted-Spalte
- strategies/strat_fast_in_out.py:42-48, strategies/strat_5_percent.py:25-29. Writer (5_trade_monitor:38 UTC-FIX, 6_housekeeping:164, 28) schreiben UTC; Strategien vergleichen datetime.now() lokal. CET/CEST: 3h-Fenster deckt nur 1-2h → Massen-Win-Block feuert zu spät/nie.
- Fix: aware-UTC in beiden strat-Dateien (wie 5_trade_monitor-FIX).
- DB-phase: SELECT max(posted), now() — Offset sichtbar?

### [HIGH] [schema|datetime] trade_cooldowns DDL-Drift ×4: WITH vs WITHOUT TIME ZONE — Bootstrap-Reihenfolge bestimmt Cooldown-Semantik
- 11:445-451, 24:425-431, 25:524-530 (WITH TZ) vs 26_regime_detector.py:194-200 (WITHOUT). Writer NOW() (timestamptz), Reader interpretiert naive als UTC → WITHOUT + Server-TZ Vienna → Cooldowns 1-2h länger.
- Fix: kanonische DDL (timestamptz) in core; ALTER-Migration.
- DB-phase: \d trade_cooldowns, SHOW timezone.

### [HIGH] [datetime] active_trades_master.time/posted naiv-lokal geschrieben (3_detectors:54,117), aber mit NOW()/aware-UTC verglichen (9_ai_sr:248, 23:399)
- PG-TZ=UTC + VPS=CEST → 60-min-Fenster wird 2h+ → doppelte AI-Nachbewertung; 24h-Statistik verschoben.
- Fix: 3_detectors auf UTC heben; langfristig timestamptz.

### [MEDIUM] [schema|telegram] telegram_outbox DDL-Drift: 3_detectors legt Tabelle ohne image_path an; ensure_schema migriert image_path NICHT nach
- 3_detectors.py:103,141 vs 4_telegram_bot.py:51-70 (ALTERs nur attempts/failed/last_error/created_at).
- Frische DB + 3_detectors zuerst → alle ~15 Chart-Bots crashen mit UndefinedColumn.
- Fix: ALTER ADD COLUMN IF NOT EXISTS image_path in ensure_schema; schmale CREATEs angleichen/entfernen.

### [MEDIUM] [deps] requirements.txt vollständig ungepinnt (alle 20 Pakete)
- pandas_ta fragil mit numpy>=2; PTB Major-Brüche; xgboost-pkl versionssensitiv. 9_ai_sr:158-Kommentar zeigt: Klasse hat schon zugeschlagen. Kein Lockfile.
- Fix: pip freeze als requirements.lock.txt; mindestens Major-Pins.

### [MEDIUM] [schema] ai_signals (13 Writer) und ml_predictions_master (9 Writer) haben KEINE DDL im Repo — Schema lebt nur in Live-DB
- Kein Unique-Backstop erkennbar; Dedup app-seitig SELECT-then-INSERT ohne ON CONFLICT.
- Fix: pg_dump --schema-only als docs/schema.sql; Unique-Index + ON CONFLICT DO NOTHING.
- DB-phase: Constraints dumpen, Duplikat-Quote messen.

### [MEDIUM] [datetime|schema] closed_ai_signals.close_time: NOW() (8:247, 6:201) und Python-UTC-Param (28:729) gemischt über drei Writer
- Server-TZ ≠ UTC → close_times um Offset auseinander → Regime-Analyzer/Tracker-Dauern verzerrt.
- Fix: einheitlich UTC-Param oder NOW() + timestamptz.

### [LOW] [sql] f-String-SQL nur mit internen Tabellennamen (~15 Sites) — kein Injection-Pfad, Quoting inkonsistent. Kein %-Format/.format()/Konkat-SQL gefunden; alle Werte parametrisiert. Optional sql.Identifier.

### [LOW] [security] Secrets/Git-Hygiene SAUBER: .env nie committed, Historie clean (12 Commits, Pickaxe leer), gitleaks ohne Löcher, kein Hardcode. Rest: 27 .pkl-Modelle committed (pickle=code exec; trusted source, PR sieht Binär-Diffs nicht). Optional SHA256-Manifest.

### [LOW] [exceptions] 1 bares except: (backtest/smc_btc_backtest.py:307); ~43 pass/continue-Swallows meist Cleanup-Pattern. Inhaltlicher Kandidat: core/trade_utils.py:103 HVN-Berechnung schluckt still → Signal ohne HVN-Level ohne Sichtbarkeit.

### [LOW] [duplication] db_schema_analysis root vs tools (tools von ruff excluded → driftet weiter). load_coins ×6 mit Semantik-Drift (core: roh; chart: dedup; fib: fallback BTC/ETH; qm: USDT-Filter). fetch_db_data ×6, send_cornix_signal ×3, get_live_price ×3. Positiv: get_db_connection/send_telegram/cooldowns zentralisiert.

### [LOW] [logging] Drei unrotierte Senken: 2_indicator_engine (indicator_calculation.log root), main_watchdog (watchdog.log), dashboard.log Popen-Pipe. Fix: setup_logging überall; Truncate im Housekeeping.

### [LOW] [schema] ml_predictions_master: 9_ai_sr:297 abweichende Spaltenliste (5 statt 8 Spalten — time/direction/entry NULL).

## Tabelle → Writer/Reader-Map (Dimension 5)
- telegram_outbox: ~19 Writer (by design Queue), Consumer 4, Cleanup 6 (7 Tage). Kontraktbruch nur schmale DDL in 3.
- ai_signals: 13 Writer (7,9,10,11,12,13,14,15,18,24,25,28,29), KEIN ON CONFLICT, keine DDL.
- ml_predictions_master: 9 Writer, keine DDL, 1 abweichende Spaltenliste.
- active_trades_master: Writer nur 3_detectors; DELETE durch 5,6,28.
- closed_trades_master: 3 Writer (5,6,28) — Spalten identisch, posted UTC ok.
- closed_ai_signals: 3 Writer (6,8,28) — close_time gemischt (Finding).
- trade_cooldowns: zentral (market_utils), aber DDL-Drift ×4.
- regime_*: sauber, ON CONFLICT ok.
- {sym}_{tf} OHLCV: Writer 1 + 6 (Gap-Fill), ON CONFLICT ok bei 1.

## Outbox-Contract (Dimension 9)
- ALLE Signal-Bots gehen über die Outbox; Direkt-API nur Consumer-Seite (4, handlers, main_telegram_bot). ✔

## Cross-cutting observations
1. Repo mitten in Sanierungswelle; verbleibende Findings meist "Fix auf einer Kontraktseite, Gegenseite vergessen". strategies/, handlers/, tools/ sind von ruff EXCLUDED — genau dort sitzt der ungefixte Rest → Exclude-Set = Sanierungs-Backlog.
2. Schema-Ownership strukturelles Kernproblem: CREATE TABLE über ~10 Dateien verstreut mit Drift; wichtigste Tabellen ohne DDL. Ein core/schema.py bzw. schema.sql + Migrations-Runner erledigt drei Findings strukturell.
3. Timezone-Politik nur Konvention pro Datei. Empfehlung: core utc_now() + ruff DTZ-Rules (flake8-datetimez) in pyproject.
4. CI minimal (AST-Parse + Import-Smoke + Secret-Grep); ruff check als CI-Job wäre gratis.
5. Secrets-Hygiene vorbildlich.

## Questions for live-DB phase
1. SHOW timezone — entscheidet welche der drei TZ-Findings live brennen und in welche Richtung.
2. \d trade_cooldowns — welche Variante gewann? Offset sichtbar?
3. \d telegram_outbox — image_path vorhanden? failed-Rows + last_error?
4. \d ai_signals/ml_predictions_master — Constraints/Indexe? Duplikat-Quote?
5. SELECT max(posted), now() AT TIME ZONE 'UTC' FROM closed_trades_master.
6. Row-Counts + Indexe der Hot-Tables (outbox sent-Index; closed_trades posted-Index für strat-COUNT bei 538 Coins).
7. Verwaiste Tabellen früherer Bot-Generationen (pump_dump_events liest je jemand?).

# Batch 1 Report — Data Ingestion, Monitor & Housekeeping

**Target files:** `1_data_ingestion.py`, `5_trade_monitor.py`, `6_housekeeping.py`, `7_pattern_detector.py`, `8_ai_trade_monitor.py`

## Completed

### #8/#16 — Monitor-Connection robust (5_trade_monitor.py, 8_ai_trade_monitor.py)
Previously, EINE Connection über die gesamte Bot-Lifetime gehalten. Bei DB-Hiccup blieb die Connection tot, der Monitor loopte mit nutzloser Connection weiter. Now:
- Trade Monitor: Connection wird in `ensure_conn()`/`reset_conn()`-Helpern verwaltet. Bei Exception wird die tote Connection verworfen und beim nächsten Loop-Start neu aufgebaut.
- AI Monitor: Reconnect-Block im `except`-Handler mit Fallback (`conn = None` wenn Reconnect auch fehlschlägt → nächster Loop versucht erneut).

### #14 — DB-Flusher SAVEPOINT-basiert (1_data_ingestion.py)
Before: Eine einzige fehlerhafte Row (e.g. fehlende Tabelle für neuen Coin) rollte den gesamten 100er-Batch zurück → hunderte Kerzen verloren. Now: Jede Row in eigenem `SAVEPOINT`, Einzelfehler werden verworfen, alle anderen Rows committen sauber. Logging dedupliziert pro Tabelle (nicht pro Row) damit die Logs nicht flooden.

### #21 — active_patterns.json atomares Write (7_pattern_detector.py)
Previously direktes `open('w')` in die Zieldatei. Bei gleichzeitigem Read aus anderem Prozess konnte ein halb-geschriebener JSON-File gelesen werden. Now: Temp-File komplett schreiben, `fsync`, dann `os.replace` für atomaren Swap.

### #36 — targets_hit defensiv zu Int casten (8_ai_trade_monitor.py)
Previously direkte Weitergabe des DB-Werts. Je nach Schema (TEXT oder INTEGER) konnte ein String zurückkommen, der `range(new_targets_hit, ...)` mit TypeError abbrach. Now: `int(targets_hit)` mit Fallback 0. Zusätzlich beim `INSERT ... VALUES ... targets_hit` explizit `int(...)` gecastet.

### #48 — telegram_outbox Cleanup (6_housekeeping.py)
Neue Funktion `cleanup_telegram_outbox(max_age_days=7)` löscht nächtlich alle `sent=TRUE`-Einträge die älter als 7 Tage sind (oder falls keine `created_at`-Spalte: alle gesendeten). Verhindert unbegrenztes Tabellenwachstum, das nach ein paar Monaten das `SELECT WHERE sent=FALSE` des Telegram-Bots ausbremste. Aufruf im nightly 03:00-Routinejob integriert.

## Already done or not a bug

### #7 — get_live_price nutzt 5m-Kerze
On reading the code: Der Monitor nutzt das `high`/`low`/`close` der neuesten (potenziell offenen) 5m-Kerze bewusst, um Wick-Durchschüsse von SL/TP zu erkennen. Das ist **gewolltes Design**, not a bug. Die ursprüngliche Kritik "offene Kerze statt Ticker" hatte die Wick-Aware-Detection-Absicht übersehen.

### #23 — Pump/Dump In-Memory-Cooldown
Noted during review: Der Cooldown wird bereits in `pump_dump_state.json` persistiert (`last_alert_time` pro Symbol, sowohl für Pump/Dump als auch für Price-Volume-Alerts). Kein Bug, war in meiner ursprünglichen Liste falsch.

## Deferred

### #12 — detect_volume_spike_in_period df.loc[index-1]
Die Funktion liegt in `strategies/strat_volume_indicator.py`, nicht im Monitor-Scope. Gehört systematisch in Batch 4 (Indicator Engine & Strategies).

## Verification

All 5 files parse cleanly:
- 1_data_ingestion.py ✅
- 5_trade_monitor.py ✅
- 6_housekeeping.py ✅
- 7_pattern_detector.py ✅
- 8_ai_trade_monitor.py ✅

## Recommendations für späteren Review

- Das Monitor-Refactoring nutzt noch die äußere Variable `c` statt eine echte Dependency-Injection. Bei einer späteren Modernisierung würde ich das in eine `class Monitor` kapseln — aber das ist P3 und außerhalb des Scopes.
- Outbox-Cleanup: Falls die `telegram_outbox` bereits sehr voll ist beim ersten Lauf, kann der `DELETE` langsam laufen. Ggf. mit `LIMIT 10000` batchen, falls die Prod-DB groß ist.

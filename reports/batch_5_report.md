# Batch 5 Report — Market Tracker, Whale & Funding Logger

**Target files:** `19_whale_logger_bot.py`, `20_funding_logger_bot.py`, `23_market_tracker.py`, `core/update_model.py`, `check_funding.py`, `check_whales.py`

## Completed

### #71/#73 — Market Tracker Kategorie-Mapping (23_market_tracker.py)
`get_category()` war inkonsistent:
- `TD_*` (Three-Drive aus SMC Sniper) wurde als INDICATOR kategorisiert — gehört zu PATTERN
- `BB_*` (Breaker Block) und `QM_*` (Quasimodo) waren als VOLUME — gehören zu PATTERN
- `SRA1` fehlte komplett in der Zuordnung (gehört zu LEVEL)
- `SMC_*` (Forex-Bot) war nicht vorgesehen — jetzt PATTERN

Saubere Neuzuordnung nach Signal-Typ: INDICATOR (Oszillatoren/Crossover), VOLUME (reines Volumen), LEVEL (S/R & Reversion), PATTERN (SMC/Chart-Patterns/Trendline).

### #72 — Market Tracker Volume-Näherung improved (23_market_tracker.py)
`SUM(volume * close)` auf `SUM(volume * (open + close) / 2)` (Mid-Price) migrated. Reduziert Fehler bei Kerzen mit großer Intra-Candle-Bewegung. Echter `quote_volume` aus Binance wäre besser, ist aber nicht in der DB gespeichert — wäre ein Ingestion-Schema-Change.

### #81 — Whale Logger format_usd negative Werte (19_whale_logger_bot.py)
Bei `val < 0` (e.g. `-1_500_000`) fielen Werte durch alle Branches und wurden als `$-1500000` (roh) ausgegeben. Now: Sign abtrennen, absoluten Wert formatieren, Sign voranstellen → `-$1.5M`.

### #82 — Funding Logger check_top20 None statt 50.0 (20_funding_logger_bot.py)
Bei leerem `current_rates_dict` lieferte die Funktion 50.0 als "neutral"-Fallback. Das täuschte Sentiment vor wo keine Daten da waren. Now: `None` zurückgeben. Beide Call-Sites (Sentiment-Engine + Overview) wurden auf None-Handling migrated:
- Sentiment-Engine: skippt den Alert-Check
- Overview: zeigt "N/A" statt `0.0%` an

### #83 — calc_diff_bps None bei fehlender Historie (20_funding_logger_bot.py)
Previously `return 0.0` bei `historical=None` — das wurde als "+0.0bps" angezeigt = "stabil", obwohl eigentlich "keine Daten" gemeint war. Now: `None` zurückgeben. Overview-Display nutzt Helper `_fmt_bps()` für "N/A" bei None.

### #85 — update_model Threshold-Files sauber überspringen (core/update_model.py)
Threshold-Files (`threshold_*.pkl`) enthalten nur einen float, kein ML-Modell. Previously crashte der Aufruf silent in `except Exception` mit `AttributeError: 'float' object has no attribute 'save_model'`. Now:
1. Dateiname-Check: wenn `threshold_*` → explizit skippen
2. Defensive `hasattr(model, "save_model")`-Check für alle anderen Fälle (fängt auch nicht-Threshold-Files mit fremden Objekten ab)

## Als zu klein oder unkritisch dokumentiert

### #80 — Whale Logger Shutdown-Save Race
Unter asyncio single-threaded Model und bei SIGINT-basiertem Shutdown (Event-Loop wird vorher gestoppt) ist das in der Praxis nicht reproduzierbar. `list(WHALE_TRADES)` ist wegen GIL atomar. Der theoretische Race wäre nur in echter Thread-Umgebung relevant, nicht in asyncio.

### #84 — FUNDING_BY_SYMBOL asyncio race
Gleicher Grund: asyncio ist single-threaded, Dict-Reassignment ist atomar. Zwischen `.get(symbol)` und `timestamps = [r[0] for r in series]` gibt es kein `await`, also kann keine andere Coroutine dazwischen laufen.

## Kein Code-Fix sinnvoll

### #50 — Market Tracker 10.000+ Queries
Die echte Fix wäre eine gemeinsame OHLCV-Tabelle (`ohlcv_30m` mit `symbol`-Spalte) statt separater Tabellen pro Coin. Das ist ein Ingestion-Schema-Change (`1_data_ingestion.py`) und würde alle Bots betreffen — deutlich außerhalb des Scopes dieser Bug-Fix-Runde. Alternativ: UNION ALL über 500 Sub-Queries — das spart keine Arbeit, nur Client-Roundtrips. Markiere als Performance-Backlog.

## Verification
Alle 6 Dateien parse cleanly.

## Wichtig für den Deploy
- Der Funding-Logger wurde umfassend refactored (None-Handling an 4 Stellen). Beim ersten Run nach Deploy bitte prüfen dass die Telegram-Ausgaben korrekt formatiert sind — insbesondere beim ersten Lauf wenn noch keine 1h/24h-Historie existiert, sollten jetzt "N/A" statt "0.0bps" oder "50.0%" angezeigt werden.
- Die Market-Tracker-Kategorisierung wirkt sich auf die stündliche Signal-Summary aus — einige Signale wechseln die Kategorie. Das ist gewollt und macht die Statistik aussagekräftiger.

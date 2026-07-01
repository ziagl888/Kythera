# Batch 6 Report — Architektur, Charting & Dashboard

**Target files:** `4_telegram_bot.py`, `6_housekeeping.py`, `main_watchdog.py`, `core/trade_utils.py`, `core/state_utils.py` (neu), 5 AI-Bot-Dateien für Zentralisierung

## Completed

### #31 — Housekeeping respektiert Outbox-Referenzen (6_housekeeping.py)
`cleanup_generated_charts` lud vor dem Löschen die `DISTINCT image_path`-Liste aller ungesendeten Outbox-Einträge und überspringt dann Dateien die noch referenziert sind. Verhindert dass bei einem Telegram-Rate-Limit-Backlog die Charts der noch-ausstehenden Nachrichten gelöscht werden.

### #52 — get_hvn_and_sr_levels zentralisiert (core/trade_utils.py)
Die Funktion war **bit-identisch** in 5 Bots dupliziert (9_ai_sr_bot, 10_pump_dump_detector, 12_ai_ats_bot, 13_ai_rub_bot, 14_ai_atb_bot — via md5 verifiziert). Jetzt einmal in `core/trade_utils.py`, alle 5 Bots importieren von dort. Zukünftige Änderungen an HVN/SR-Logik müssen nur noch an einer Stelle gemacht werden, kein Drift-Risiko mehr zwischen Bots.

### #68/#87 — Telegram Chart-Löschung unter Mehrfachreferenzen (4_telegram_bot.py)
Neue Funktion `try_delete_chart_if_unreferenced(cur, image_path, current_msg_id)` prüft via SELECT ob ein anderer ungesendeter Outbox-Eintrag dieselbe image_path referenziert. Nur wenn nicht, wird gelöscht. Previously konnte das gleiche Chart-File doppelt in der Outbox stehen (e.g. bei paralleler Logging aus zwei Perspektiven), der erste Send löschte die Datei, der zweite fiel auf "nur Text" zurück. Beide Call-Sites (`mark_sent` und `mark_failure`) nutzen jetzt den sicheren Helper.

### #70 — Dashboard-Output in Log-Datei statt DEVNULL (main_watchdog.py)
Previously `stdout=DEVNULL, stderr=DEVNULL` — wenn das Dashboard crashte, war der Grund unsichtbar und der User konnte nicht debuggen. Jetzt `logs/dashboard.log` mit Append-Mode und einem Timestamp-Header bei jedem Neustart. `stderr` wird auf den gleichen Stream gelenkt (STDOUT) damit Traceback und normales Log zusammen sind.

### #88 — Zentrale State-Persistence-Helper (core/state_utils.py, neu)
Neues Modul mit `atomic_write_json(filepath, data)` und `atomic_read_json(filepath, default)`:
- Write: Temp-File + fsync + os.replace (garantiert atomar, crash-safe)
- Read: mit Default-Fallback; bei JSON-Decode-Error wird die korrupte Datei als `.corrupt` wegsichert und der Default zurückgegeben (Bot läuft weiter statt zu crashen)
- Automatisches Anlegen des Zielverzeichnisses

Die Bots, die bisher eigene atomare-Write-Patterns haben (aus Batch 1/4), können nachträglich auf diesen Helper konsolidiert werden. Für diese Iteration: neues Modul ist da und steht neuen Integrations zur Verfügung — Bestandsbots bleiben mit ihrer getesteten Logik unberührt (kein Refactor-Risiko).

## Als false alarme / unkritisch dokumentiert

### #43 — SMC Forex hardcoded "20x-10x"
Der SMC-Forex-Bot hardcoded `20x-10x` Leverage. Das ist bewusst — er nutzt yfinance-Tickers (`GC=F`, `JPY=X` etc.), die nicht in `max_leverage.json` stehen. Für TradFi-Assets ist konservativer Hebel gewollt, not a bug.

### #54 — SMC-ML-Sniper Pine-Script-Emulation
Beim Code-Review keine Pine-Script-Idiome (ta.barssince, ta.valuewhen, etc.) found, die problematisch emuliert wären. Der ursprüngliche Punkt war spekulativ.

### #57 — Quasimodo unused config
Alle Top-Level-Konstanten (`MIN_CONFIDENCE`, `ZONE_TOLERANCE`, `PIVOT_WINDOW`, `PRICE_BASED_INDICATORS`, `ABSOLUTE_INDICATORS`) werden im Bot genutzt. Kein Dead-Code.

### #90 — active_trades_master vs ai_signals FK
Die beiden Tabellen sind bewusst parallel (conv-Trades vs AI-Trades), kein FK-Verhältnis gedacht. Zusammengeführt werden sie nur im Market Tracker via UNION. Keine Inkonsistenz.

## Verification
**Alle 47 Python-Dateien im Projekt parse cleanly** (inklusive der 5 Bots nach Entfernung der duplizierten get_hvn_and_sr_levels).

## Abschließende Recommendations

- Die 7 verbleibenden State-Files (active_patterns, alerted_qms, trendline_state, pump_dump_state, indicator_state, funding_history, und ein paar andere) könnten in einem späteren Refactor auf `core.state_utils` migrated werden. Das ist aber niedrige Priorität — die atomare Write-Logik ist in den geänderten Bots bereits korrekt implementiert.
- Das neue `logs/dashboard.log` sollte periodisch geprüft/rotiert werden (aktuell Append-only). Falls das Dashboard stabil läuft, braucht es das nicht — aber der Bot sollte die Datei nicht unbegrenzt wachsen lassen. Empfehlung: logrotate-Config einrichten.

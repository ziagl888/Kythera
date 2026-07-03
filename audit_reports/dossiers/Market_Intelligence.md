# Dossier: Market Intelligence (Whale-Logger · Funding-Logger · Market-Tracker)

> **Reine Datenlieferanten/Reporter, keine Trader** — sie handeln nicht, aber ihre Daten (Tracker-Statistiken) sind Entscheidungsgrundlage für Menschen. **Note (16b): keine** (nicht als Strategie bewertbar); Einordnung: „Der Intelligence-Layer ist ein Anzeige-Layer."
> **Kernverdikt:** Sammeln ohne Konsument. Whale- und Funding-Daten — genau die Datenklassen, die ein Regime-Gate veredeln könnten — werden von **keiner einzigen Entscheidungslogik** konsumiert. Der Whale-Logger ist **seit 18.04. tot**, `ticker_10s` ist **leer**, nur der Funding-Logger läuft sauber. Entweder als Features einspeisen (S8/S12) oder abschalten — der Ist-Zustand ist reiner Betriebsaufwand.

## 1. Steckbrief

| Komponente | Aufgabe | Datenhaltung | Kanal/Konsument |
|---|---|---|---|
| `19_whale_logger_bot.py` | Binance-Futures aggTrade-Firehose, Whale-Trades + Buy/Sell-Pressure-Ratios (1h/4h/24h) | `whale_data/whale_trades_*.json` (Tages-Files, Full-Rewrite) | Telegram-Info-Posts; **kein maschineller Konsument** |
| `20_funding_logger_bot.py` | Funding-Rates aller Perps, Breadth-/Extreme-Alerts, Historie | `funding_data/funding_history_*.json` (seit Februar lückenlos) | Telegram-Alerts via `telegram_outbox`; **kein maschineller Konsument** |
| `23_market_tracker.py` | Stunden-/Tagesreports: Per-Bot-Performance, Gainers/Losers, Regime-Fit-Label, Signal-Summary | liest `closed_trades_master`, `closed_ai_signals`, `ai_signals`, `ml_predictions_master`, Regime-Tabellen | Telegram-Report-Channels; Per-Bot-Tabelle = menschliche Entscheidungsgrundlage |
| (Randnotiz) | `ticker_10s`-Tabelle: gedachte 10s-Ticker-Basis für EPD1 | **leer** — EPD1 arbeitet rein in-memory (N3, Report 17) | suggeriert eine Datenbasis, die nicht existiert |

## 2. Live-Bilanz

- **Whale-Logger: tot seit 18.04.2026** — letztes `whale_trades_*.json` vom 18.04.; davor deckten die Files nur **49 von 529 Symbolen** ab (P1.42 ✔✔: 538 aggTrade-Streams auf **einer** WS-Connection, fapi-Cap ~200/Conn → ~340 Symbole still nie geliefert; Reconnect-Backoff resettet nie → capped 300s-Waits).
- **Funding-Logger: läuft** — Files seit Februar lückenlos, Timezone-Handling sauber (epoch-basiert); aber die 75%-Breadth-„Extreme"-Schwelle feuert im Normalzustand (Baseline +0,01% → Alert alle 15 min über Tage möglich, P2.40); `lastFundingRate` ist die prognostizierte, nicht die settled Rate.
- **Market-Tracker: läuft, aber fragil** — Pool-Leak bei Query-Fehler (~1 Leak/h → nach ~8h alle Tracker-Jobs tot bis Restart, P1.43); „Opened"-Counts doppeln AI-Trades und zählen Shadow-Predictions mit (P1.44) → verzerrt genau die Per-Bot-Statistik, nach der Menschen Bots beurteilen (Shadow-Flut: EPD1 31k + AIM1 25k ungepostete Rows/7d aus Bot 10).
- **`ticker_10s` leer** — wäre die Trainingsdaten-Quelle für S6 (Pump-Exhaustion-Short).

## 3. Befunde

| ID | Komponente | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| P1.42 | 19 | HIGH | 538 Streams auf 1 WS-Conn (Cap ~200) → 49/529 Symbole; Logger schreibt seit 18.04. gar keine Files mehr | ✔ |
| 09-W2 | 19 | MEDIUM | Reconnect-Backoff resettet nach Erfolg nie → dauerhaft 300s-Reconnect-Waits | ~ |
| 09-W3 | 19 | MEDIUM | CPU-Scans + Full-Day-JSON-Rewrite blockieren den Event-Loop neben der Firehose → Slow-Consumer-Disconnects | ~ |
| P2.40 | 20 | MEDIUM | „Extreme"-Breadth-Schwelle 75% feuert im Normalzustand | ~ |
| 09-F2 | 20 | LOW | „1h"-Breadth fällt still auf den aktuellen Wert zurück (Schein-Stabilität); Top-5-Listen über ALLE Binance-Symbole statt Tracked-Set | ~ |
| P1.43 | 23 | HIGH | Pool-Leak + fehlender Rollback → nach ~8h alle Tracker-Jobs tot bis Restart | ~ |
| P1.44 | 23 | HIGH | „Opened"-Counts: AI-Trades doppelt + Shadow-Predictions mitgezählt → Per-Bot-Statistik (Entscheidungsfläche!) verzerrt | ~ |
| 09-T3 | 23 | MEDIUM | Regime-Fit-Label: ein Query-Fehler vergiftet die Shared-Conn → ganze Spalte „---"; Message-Chunker kann Überlängen-Block nicht teilen (stiller Telegram-Reject) | ~ |
| N3 | DB | MEDIUM | `ticker_10s` ist leer — befüllen (S6-Trainingsquelle) oder droppen | ✔ |
| 16b-Q7 | alle | HIGH | Kein maschineller Konsument: Whale/Funding fließen in keine Entscheidungslogik — totes Datensammeln bzw. Human-Info | ✔ |

Status: ✔ = live/DB bewiesen · ~ = Code-Befund (Report 09), live nicht separat quantifiziert.

## 4. Abhängigkeiten & Querschnitts-Risiken

- **Tracker-Per-Bot-Tabelle ist die menschliche Entscheidungsfläche** — ihre zwei Upstream-Verschmutzer (Shadow-Flut aus Bot 10, Doppelzählung P1.44) verzerren Portfolio-Entscheidungen; zusätzlich erbt jede Tracker-WR den Monitor-Label-Vorbehalt (Report 17: nur 63,4% Replay-Übereinstimmung) und die P1.9-Zensur (Regime-Closes fremder Trades als neutral).
- **Silent-Failure-Hausstil:** Blanket-`except:pass` → Failure-Mode „Report kommt still nicht an" (deckt sich mit dem P2.47-Muster: wedged Bot bleibt grün).
- **Strategie-Vorschläge hängen an dieser Schicht:** S8 (Funding-Extreme Mean-Reversion — 4 Monate lückenlose Historie liegen ungenutzt) und S12 (Whale-Flow-Confirmation — erst nach P1.42-Fix möglich).

## 5. Sanierungsplan

1. **Whale-Sharding-Fix (P1.42):** Streams auf 3 WS-Connections sharden, Backoff-Reset nach erfolgreichem Connect, JSONL-Append + rollende Aggregate statt Full-Rewrite — dann Logger-Neustart. **Oder** (16b): abschalten, solange kein Konsument existiert; erst mit S12 als Abnehmer lohnt der Betrieb.
2. **`ticker_10s`-Entscheidung (N3):** befüllen (wird Trainingsdaten-Quelle für S6) oder droppen — der Schwebezustand täuscht eine Datenbasis vor.
3. **Funding P2.40:** Schwelle auf 95/85 + Magnitude-Anforderung (|rate|>0,02%) oder Transitions-Alerts; danach ist S8 der erste echte Konsument der Funding-Historie.
4. **Tracker-Härtung (P1.43/P1.44):** `try/finally close` + Rollback vor Fallback; `posted=TRUE`-Filter, Opens nur aus `ai_signals`+`closed_ai_signals`; Rollback im Regime-Fit-Pfad; Per-Job-Heartbeat gegen stille Ausfälle.
5. **Grundsatzentscheidung (16b):** Whale-/Funding-Daten entweder als Features in Regime/Gate einspeisen (S8/S12) **oder** die Logger abschalten — sammeln ohne Konsument ist reiner Betriebsaufwand.

## 6. Belege

- `audit_reports/09_intelligence.md` — Code-Findings 19/20/23 + Cross-cutting
- `audit_reports/STEP2_DB_VERIFICATION.md` — P1.42 ✔ (49/529, tot seit 18.04.), Shadow-Flut-Größenordnung
- `audit_reports/17_monitor_replay_and_gaps.md` — N3 (`ticker_10s` leer), Abdeckungs-Matrix (Whale tot, Funding-Files aktuell)
- `audit_reports/16_strategy_concept_evaluation.md` — Querschnittsbefund 7 (Anzeige-Layer), Abschnitt 7 (Intelligence-Layer)
- `audit_reports/15_strategy_proposals.md` — S8 Funding-Mean-Reversion, S12 Whale-Confirmation, S6 (ticker_10s als Trainingsquelle)
- `AUDIT_TODO.md` — P1.42–P1.44, P2.40 mit Annotationen

# 17 — Monitor-Replay & verbleibende Analyse-Lücken (Step 7)

**Stand:** 2026-07-03 · **Anlass:** Frage „haben wir noch etwas übersehen?" — Antwort: Ja, die empirische Validierung der **Trade-Monitore** stand noch aus. Genau dort sitzt der größte neue Befund.

## 1. Monitor-Replay: Scoring vs. First-Touch-Wahrheit (n=388, letzte 30 Tage)

**Methode:** 400 zufällige Classic-Trades (Status 0/1/2/SL0-SL2, `closed_trades_master` hält Entry/TP1/SL je Row) gegen die vollständigen 5m-Kerzen zwischen Open und Close nachgespielt (First-Touch-Logik; TZ-Ambiguität der naiven Zeitstempel per Close-Preis-Alignment aufgelöst).

| Messgröße | Ergebnis |
|---|---|
| Übereinstimmung Monitor ↔ Replay (≥TP1 ja/nein) | **nur 63,4%** |
| Monitor **verpasste** TP1 (Replay: getroffen) | **17,8%** (69/388) |
| Monitor vergab TP1, obwohl SL laut Replay **zuerst** kam | **18,8%** (73/388) |
| `close_price` liegt in der Close-Zeitpunkt-Kerze | nur 17,8% (mediane Abweichung sonst 1,21%) |
| TP+SL in derselben 5m-Kerze (echte Ambiguität) | 0,3% — erklärt die Diskrepanz NICHT |
| 5m-Datenlücken (10 Sample-Coins, 25 Tage) | **0** — an den Kursdaten liegt es nicht |
| TZ-Shift-Alignment | exakt 50/50 UTC vs. Lokal → gemischte Zeit-Writer erneut bewiesen (R3) |

**Pro Strategie (agree%):** Fast In And Out 73% · Support Resistance 67% · **5 Percent 45% · Volume Indicator 44%** — bei den letzten beiden ist das Scoring de facto Rauschen.

**Interpretation:** Die Code-Findings P2.7 (Monitor prüft nur die jüngste 5m-Kerze pro Zyklus → verpasst Hits dazwischen/bei Downtime) und P1.2 (Trailing-SL zieht nie nach) sind hiermit **quantifiziert**: Beide Fehlklassen (verpasste TPs UND fälschlich vergebene TPs) treten je ~18% auf. Da beide Richtungen ähnlich häufig sind, ist der Netto-Bias der Report-14-WRs moderat — aber die **per-Trade-Wahrheit ist unzuverlässig**, und damit jede darauf trainierte Statistik (Whitelist! Analyzer! S11-Labels!). Vorbehalt: Ein Teil der Diskrepanz kann TZ-Alignment-Restfehler sein; die Größenordnung übersteigt das aber deutlich (Ambiguität 0,3%, Daten lückenlos).

**Konsequenz — Monitor-Rewrite wird strategisch P0-nah:**
1. Forward-Scan mit `last_checked_open_time` statt „jüngste Kerze" (P2.7-Fix) — Hits werden aus **Kerzen** gescored, nicht aus dem aktuellen Preis beim Zyklus.
2. `close_price` = Preis der auslösenden Kerze (nicht Abfragezeitpunkt).
3. Trailing-SL-Fix (P1.2) + TZ-Fix (R3) im selben Zug.
4. Danach Report-14-Zahlen einmal neu ziehen (Re-Score der letzten 30 Tage per Replay möglich).

## 2. Weitere in diesem Schritt gefundene Lücken

- **N2 — 800 Outbox-Messages still verworfen (P2.11 quantifiziert):** alle mit `Timed out` nach 3 Versuchen, darunter **212 im Fast-In-And-Out-Trading-Channel** und 98 Volume-Indicator — verlorene Signale/SL-Updates ohne jeden Alarm. Fix wie P2.11 (Retry ohne parse_mode, Dead-Letter + Operator-Alert) plus Timeout-Handling als „unknown outcome" (P0.1).
- **N3 — `ticker_10s` ist leer.** EPD1 arbeitet rein in-memory; die Tabelle ist tot. Entweder befüllen (wäre Trainingsdaten-Quelle für S6!) oder droppen — aktuell suggeriert sie eine Datenbasis, die nicht existiert.
- **N4 — AI-Trades sind rückwirkend NICHT auditierbar:** `ai_signals`-Rows werden beim Close entfernt (nur 1.559 offene Rows vorhanden) → SL/Targets geschlossener AI-Trades sind weg; der Monitor-Replay von Abschnitt 1 ist für die AI-Flotte unmöglich. **Fix (klein, wichtig):** beim Close `sl`, `targets`, `entry1/2` in `closed_ai_signals` mitschreiben — ab dann ist die AI-Flotte genauso prüfbar wie die Classic-Strats.
- **N5 — 5m-Retention ~30 Tage:** Replays/Feintrainings auf 5m-Basis sind nur für die letzten 30 Tage möglich. Bewusst so lassen oder für ausgewählte Zwecke (Monitor-Re-Score, S11-Labels) ein komprimiertes Archiv anlegen.
- **N6 (positiv) — Datagrepper-5m ist sauber:** 0 Lücken über 10 Sample-Coins × 25 Tage; zusammen mit dem 1h-Census (Step 2: 0 Lücken/529 Coins) ist die Ingestion-Vollständigkeit gut belegt. Die offenen Ingestion-Punkte bleiben R1 (Forming Candle) und P1.11 (Boundary-Row bleibt Partial bis zum REST-Catch-up).

## 3. Abdeckungs-Matrix — was ist jetzt geprüft, was bleibt offen?

| Komponente | Code-Audit | Empirisch | Offen |
|---|---|---|---|
| 1_data_ingestion (Datagrepper) | ✔ (02) | ✔ Gaps 1h+5m=0, Forming/Partial bewiesen | Cross-TF-Konsistenz (1h vs 5m-Aggregat), Verhalten nach Neustart (füllt Catch-up die 6h-Lücke vom 3.7.?) |
| 2_indicator_engine | ✔ (02) | ✔ RSI-Formel, POC-Broadcast, ma_200 | Parität weiterer Indikatoren (WMA/KAMA/TSI) vs pandas_ta — Spot-Check ausstehend |
| 5_/8_ Trade-Monitore | ✔ (03) | ✔ **Replay: 63% Übereinstimmung** | AI-Monitor-Replay (erst nach N4-Fix möglich) |
| 4_telegram_bot / Outbox | ✔ (03) | ✔ Dups, 0 Retry-Doppel, 800 Timeouts | Kein Cornix-seitiger Abgleich (extern) |
| Orchestrator/Regime | ✔ (04) | ✔ Report 16 | Counterfactual-Wert des Gates (Vorschlag 16-Nr.8) |
| ML-Bots + Trainer (_X) | ✔ (05-08, 13) | ✔ Kalibrierung, Artefakt-Introspektion | MIS1-Provenienz bleibt verloren |
| Market-Intelligence (10/19/20/23) | ✔ (09) | ✔ Whale tot, Funding-Files aktuell, pump_dump_events | Funding-Inhaltsvalidierung; ticker_10s-Entscheidung (N3) |
| Dashboard/Watchdog | ✔ (10, 01) | ✔ P2.47 live belegt | Dashboard-Port-Exponierung von außen (P0.8) nicht getestet |
| chart_data_service | ✔ (02) | — | empirisch ungeprüft (niedrige Prio, kein Geldpfad) |
| 99_smc_paper_bot (nur live) | **✘ nie auditiert** | — | einziges komplett ungeprüftes Modul |
| Legacy `_X`-Runtime (1-datagrepper etc.) | — | läuft nicht (Watchdog-Fleet enthält sie nicht) | archivieren |
| Exchange-/Cornix-Realität | — | — | externe Stichprobe (50 Trades) weiterhin empfohlen |

**Fazit:** Mit dem Monitor-Replay ist die letzte große interne Lücke geschlossen. Wirklich offen bleiben: 99_smc_paper_bot (Code nie gesehen), der externe Cornix/Exchange-Abgleich, und die Nacharbeiten N3/N4 + Indikator-Spot-Checks. Der wichtigste neue Arbeitsauftrag aus diesem Schritt ist der **Monitor-Rewrite (Kerzen-basiertes First-Touch-Scoring)** — er steht jetzt VOR dem Neutraining der Modelle, weil er deren Labels liefert.

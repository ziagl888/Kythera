# Dossier: Main Channel

> Funktionales Duplikat von Support Resistance auf 38-Coin-Whitelist — **Note C−** (Report 16) · Σ **−77** bei nur n=202 (≈ 0). Kernverdikt: „in Support Resistance mergen (ATR-SL als Verbesserung mitnehmen), nicht separat betreiben" — teilt den live bestätigten P0.7-Bug.

## 1. Steckbrief
- **Modul:** `strategies/strat_main_channel.py`, Runner `3_detectors.py`, Monitoring `5_trade_monitor.py`.
- **Signal-Logik:** Logisch **identisch mit Support Resistance** (gleiche Hit-/Divergenz-/OBV-Logik: wiederholter Level-Test + RSI-Divergenz + OBV-Bestätigung), nur auf einer 38-Coin-Whitelist und mit **ATR-SL statt Fix-SL**.
- **Channel:** eigener Cornix-Trading-Channel via `telegram_outbox` — dasselbe Marktereignis erzeugt so zwei nahezu identische Hebel-Signale in zwei Channels (verdeckte Doppel-Exposure statt Diversifikation).
- **Cooldowns:** kein Per-Coin-Cooldown (Classic-Familie generell).

## 2. Live-Bilanz (Report 14, dedupliziert, `closed_trades_master`)
- **n = 202** · WR **67,3%** · ø **−0,28%**/Trade · Median **0,00** · Σ netto **−77** Preis-% — klein, ≈ 0.
- Richtungssplit und Monatstrend bei diesem n nicht separat berichtet.
- **Scoring-Vorbehalt (Report 17):** Für Main Channel wurde im Replay **keine strategie-spezifische agree%-Zahl** ausgewiesen (n zu klein in der 388er-Stichprobe); es gilt der Flottenwert von nur **63,4%** Übereinstimmung Monitor↔Replay (17,8% verpasste TP1, 18,8% TP1 trotz SL-zuerst) — bei n=202 ist die Bilanz doppelt unsicher (kleines n × unzuverlässiges Scoring).

## 3. Befunde
| ID | Schweregrad | Einzeiler | Status |
|---|---|---|---|
| P0.7 | **P0** | Leere-Zone-Interpolation erzeugt LONG-TPs UNTER dem Entry (`strat_main_channel.py:70-87,115-132`, t1==0 ungeguarded → TP1 = 0,75·Entry) | **✔** (Step 2: 5 aktive + 79 geschlossene LONG-Trades mit target1 ≤ entry, MC+SR zusammen) |
| P1.15 | Hoch | Ein schlechter Coin killt den ganzen Detector-Prozess | ~ |
| P2.44 | Mittel | 538 serielle Binance-HTTP-Calls pro Detector-Zyklus | ~ |
| R1/05 | Hoch | Bewertet die noch laufende Kerze; Engine stempelt :02 UND :32 | ✔ (Step 2) |
| 05 | Mittel | OBV-Divergenz-Filter statistisch nahezu bedeutungslos (Gate dekorativ) | ~ |
| 05/16b | Kontext | Duplikat von Support Resistance → Doppel-Exposure in zwei Cornix-Channels | ~ |

## 4. Abhängigkeiten & Querschnitts-Risiken
- **R1 Forming Candle** (Step 2 bewiesen): Level-/Divergenz-Erkennung auf Partial-Kerzen; gespeicherte S/R-Level-Historien zudem mixed-vintage (P1.12 ✔ — Scalar-Broadcast).
- **R3 TZ-Mix:** naive Zeitspalten mit gemischter UTC/Lokal-Semantik (Step 2 bewiesen).
- **Monitor-Bugs P1.2/P2.7:** Trailing-SL zieht nie nach, nur jüngste 5m-Kerze geprüft — bei n=202 kann das Vorzeichen der Bilanz allein durch Scoring-Fehler kippen.
- **Outbox-Verluste (N2):** 800 Messages still verworfen (kein MC-spezifischer Wert berichtet); Whitelist-Raw-Namen-Rows seit 19.04. eingefroren (P0.4/P2.25) → Orchestrator-Gating der Channel-Fallback-Bots auf 2,5 Monate alten Statistiken.

## 5. Sanierungsplan
- **Sofort:** **P0.7-Fix** (`if t1 == 0: return None`) — geld-kritisch, live bestätigt; korrupte aktive Trades bereinigen. P1.15-per-Coin-Isolation.
- **Strukturell:** **Merge in Support Resistance** (Report 16: „mergen statt Doppelbetrieb") — den ATR-SL als Verbesserung in die gemergte Strategie mitnehmen, den separaten Channel/Betrieb einstellen und damit die verdeckte Doppel-Exposure beenden. Weitergehende Sanierung (Closed-Candle, OBV-Ersatz, S1 Direction-Gate) läuft dann über das Support-Resistance-Dossier; ein eigenständiger Weiterbetrieb von Main Channel ist bei n=202 und Σ −77 nicht begründbar.

## 6. Belege
- `AUDIT_TODO.md`: P0.7 (✔ Step 2), P1.15, P2.44, R1, R3
- `audit_reports/STEP2_DB_VERIFICATION.md` §C: P0.7 = 5 aktive + 79 geschlossene
- `audit_reports/05_classic_strats.md`: Empty-Zone-Interpolation, MC≡SR (Cross-cutting #3), OBV-Filter
- `audit_reports/14_bot_performance_db.md` §C: Zahlenzeile Main Channel
- `audit_reports/16_strategy_concept_evaluation.md` §3: Note C−, Merge-Verdikt
- `audit_reports/17_monitor_replay_and_gaps.md` §1: Flotten-agree 63,4%

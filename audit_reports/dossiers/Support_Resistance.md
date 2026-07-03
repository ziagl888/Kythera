# Dossier: Support Resistance

> Level-Retest mit RSI-Divergenz — **Note B−** (Report 16) · **einzige netto-positive Classic-Strategie** (Σ **+596**), SHORT-Seite trägt alles. Kernverdikt: „Beste Rettungskandidatin" — ausbauen, Main Channel hineinmergen, aber P0.7 (LONG-TPs unter Entry) ist live bestätigt und muss sofort gefixt werden.

## 1. Steckbrief
- **Modul:** `strategies/strat_support_resistance.py`, Runner `3_detectors.py`, Monitoring `5_trade_monitor.py`.
- **Signal-Logik:** Wiederholter Test eines S/R-Levels + RSI-Divergenz zwischen erstem und aktuellem Hit + OBV-Bestätigung → Umkehr-Einstieg; Targets aus echten Struktur-Zonen, fixer 2,5%-SL. Einzige Classic-Idee mit echter Selektionslogik — die niedrige Signalfrequenz (1.917 vs. 111k bei FIFO) zeigt, dass die Filter tatsächlich filtern.
- **Channel:** eigener Cornix-Trading-Channel via `telegram_outbox` (Whitelist-Rohname „Support Resistance"). Achtung: logisch identisch mit Main Channel → ein Event erzeugt zwei nahezu identische Hebel-Signale in zwei Channels (verdeckte Doppel-Exposure).
- **Cooldowns:** kein Per-Coin-Cooldown (Classic-Familie generell).

## 2. Live-Bilanz (Report 14, dedupliziert, `closed_trades_master`)
- **n = 1.917** · WR **63,5%** · ø **+0,41%**/Trade · Median **0,00** · Σ netto **+596** Preis-%.
- **Richtungssplit:** SHORT (+0,66% ø) trägt den **gesamten** Gewinn — Direction-Gate erwägen. Monatstrend nicht separat ausgewiesen.
- **Scoring-Vorbehalt (Report 17):** Monitor-Scoring stimmt bei Support Resistance zu **67%** mit dem First-Touch-Replay überein (Flotte 63,4%) — besser als 5 Percent/Volume, aber jede dritte Trade-Klassifikation ist falsch; die +596 sind monitor-generiert und vor dem Monitor-Rewrite nicht belastbar.

## 3. Befunde
| ID | Schweregrad | Einzeiler | Status |
|---|---|---|---|
| P0.7 | **P0** | Leere-Zone-Interpolation erzeugt LONG-TPs UNTER dem Entry (t1==0 ungeguarded → TP1 = 0,75·Entry; SHORT −25/−50/−75%) | **✔** (Step 2: 5 aktive + 79 geschlossene LONG-Trades mit target1 ≤ entry) |
| P1.15 | Hoch | Ein schlechter Coin killt den ganzen Detector-Prozess | ~ |
| P2.44 | Mittel | 538 serielle Binance-HTTP-Calls pro Detector-Zyklus | ~ |
| R1/05 | Hoch | Bewertet die noch laufende Kerze; Engine stempelt :02 UND :32 | ✔ (Step 2) |
| 05 | Mittel | OBV-Divergenz-Filter statistisch nahezu bedeutungslos (N-Kerzen-Summe vs. 1-Kerzen-2σ-Band) — Gate dekorativ | ~ |
| 05 | Mittel | Fixer 2,5%-SL ignoriert Coin-Volatilität | ~ |
| 05 | Kontext | Duplikat-Strategie zu Main Channel → Doppel-Exposure | ~ |

## 4. Abhängigkeiten & Querschnitts-Risiken
- **R1 Forming Candle** (Step 2 bewiesen): Hit-/Divergenz-Erkennung auf Partial-Kerzen; zusätzlich sind gespeicherte SUPPORT/RESISTANCE_PRICE-Historien mixed-vintage (Scalar-Broadcast, P1.12 ✔) — „Level der Vorkerze"-Semantik hält nicht.
- **R3 TZ-Mix:** naive Zeitstempel, gemischte Semantik (Step 2 bewiesen).
- **Monitor-Bugs P1.2/P2.7:** Trailing-SL zieht nie nach, nur jüngste 5m-Kerze geprüft → 67% Replay-Agree; die Bilanz kann nach Re-Score kippen (beide Fehlklassen ~18%, Netto-Bias moderat).
- **Outbox-Verluste (N2):** 800 Messages still verworfen (kein SR-spezifischer Wert berichtet); Whitelist-Rohname „Support Resistance" seit 19.04. eingefroren (P0.4/P2.25).

## 5. Sanierungsplan
- **Sofort:** **P0.7-Fix** (`if t1 == 0: return None` bzw. Fixed-%-Fallback) — geld-kritisch, live bestätigt; die 5 aktiven korrupten Trades bereinigen. P1.15-per-Coin-Isolation.
- **Strukturell (Report 16: „Beste Rettungskandidatin"):** Closed-Candle-Disziplin (R1), **ATR-SL von Main Channel übernehmen** statt fixer 2,5%, OBV-Baustein ersetzen oder streichen (√N-Skalierung), **Main Channel hineinmergen** (Doppel-Exposure beenden), **S1 Direction-Gate** erwägen (SHORT trägt alles — LONG-Seite prüfen/drosseln). Nach Monitor-Rewrite Re-Score, dann als einzige Classic gezielt ausbauen (Report 16 §8: „Support Resistance als einzige ausbauen").

## 6. Belege
- `AUDIT_TODO.md`: P0.7 (✔ Step 2), P1.15, P2.44, R1, R3
- `audit_reports/STEP2_DB_VERIFICATION.md` §C: P0.7 = 5 aktive + 79 geschlossene
- `audit_reports/05_classic_strats.md`: Empty-Zone-Interpolation, OBV-Filter, MC≡SR-Duplikat, Fix-SL
- `audit_reports/14_bot_performance_db.md` §C: Zahlenzeile, SHORT trägt
- `audit_reports/16_strategy_concept_evaluation.md` §3: Note B−, Rettungs-Verdikt
- `audit_reports/15_strategy_proposals.md`: S1 Direction-Gates
- `audit_reports/17_monitor_replay_and_gaps.md` §1: agree 67%

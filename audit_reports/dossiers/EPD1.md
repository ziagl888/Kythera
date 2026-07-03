# Dossier: EPD1 (Echtzeit-Pump/Dump-Detektor)

> 10-Sekunden-Tick-Detektor für Volumen-Ignition in Alt-Perps mit 3-Klassen-XGBoost — **Note C+** (Report 16, Rang 6): bestes Edge-Narrativ und stärkster ø der Flotte (+3,34%/Trade), aber das Modell wird **live fast nur out-of-distribution befragt** (fehlendes vol_ratio-Gate) und der Gewinn ist regimeabhängig (Juli negativ).

## 1. Steckbrief

| Feld | Inhalt |
|---|---|
| Bot | `10_pump_dump_detector.py` |
| Artefakt | `pump_dump_model.pkl` (3-Klassen-Modell; Klassen-Mapping via `classes.index()` korrekt) — **Artefakt aus unbekanntem Lauf/stale** |
| Trainer | `legacy_trainers/zzz.py` → `train_pump_dump_model` (~Z.7054-7242). Status: Trainer existiert, tägliches Training ist aber **auskommentiert** (Z.7033/7040-7041) — **Log meldet trotzdem Erfolg** |
| Label/Training | Trainer sampelt NUR `volume_ratio ≥ 5`-Events (zzz.py:7103-7104); random Split über 10s-Quasi-Duplikate (zzz.py:7178); Sample-Weights (Pump/Dump bis 3.0) ohne Nachkalibrierung |
| Features | 10; Feature-Positionen Bot↔Modell exakt verifiziert (Report 13). Live: Volumen-Anomalie + Mikro-Momentum aus dem 24h-Ticker |
| Thresholds | Shadow-Band 0.25 ≤ prob < 0.60, Post ab 0.60; `max(prob_pump, prob_dump)` wird als „Confidence" geloggt (unkalibriert); 15-min-Cooldown |
| Channels | Pump/Dump-Alert-Channels inkl. MARKET-Channel (Round-Level-Cooldown asymmetrisch, MARKET re-sendet alle 180s); Trades in `ai_signals`/`closed_ai_signals` als Modell `EPD1` |
| Datenbasis | Rein in-memory-Ticker-Puffer — die Tabelle `ticker_10s` ist **leer** (N3, Report 17) |

## 2. Live-Bilanz (aktive Ära 24.02.–03.07., dedupliziert; Report 14/Step 2)¹

- **n = 4.392 · WR 72,8% · ø +3,34%/Trade · Median +3,63% · Σ netto +14.222 Preis-%** — stärkster ø und zweitgrößter Ertragsträger der AI-Flotte.
- **Richtungssplit: SHORT 76,5% vs. LONG 50,2% WR** — die größte Richtungs-Asymmetrie der Flotte; bestätigt das „Pump-Faden"-Muster (Report 14/16).
- **Kalibrierung: flach** (corr ≈ 0, aber hohes Grundniveau; Step 2) — passt zum OOD-Serving: die 72,8% WR stammen plausibel aus der S/R-basierten SL/TP-Konstruktion, nicht aus Modell-Skill.
- **Monatstrend:** fast der gesamte Gewinn aus Mai/Juni (+14,6k, Alt-Pump-Phase), **Juli negativ (−345)** → Regime-Abhängigkeit, Drift-Watch Pflicht.

¹ *Monitor-Vorbehalt (Report 17): Zahlen monitor-generiert (Replay-Übereinstimmung nur 63,4%; P1.2/P2.7/P2.31 — EPD1 hat 215 Rows mit 20 gescorten Targets); AI-Trades rückwirkend nicht replaybar (N4).*

## 3. Befunde (konsolidiert)

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| 13-B/P0 | Pipeline | P0 | **Covariate-Shift:** Trainer sampelt nur `vol_ratio ≥ 5`, Live scored jeden 10s-Tick ohne Gate (`10:519-565`) → fast alle Queries out-of-distribution; erklärt die flache Kalibrierung | ✔ (code-belegt + Step-2-Messung) |
| 13-B/P1a | Trainer | P1 | Tägliches Training auskommentiert, Log meldet trotzdem Erfolg → Artefakt stale/unbekanntes Regime (zzz.py:7033,7040-7041) | ✔ |
| 13-B/P1b | Trainer | P1 | Random Split über 10s-Quasi-Duplikate → Metriken memorisiert (zzz.py:7178; X-R3) | ✔ |
| P1.39 | Bot | P1 | Timestamp-Fix unvollständig: Volume-Explosion + ML-Features noch index-basiert → nach Restart falsche „VOLUME EXPLOSION"-Alerts, schiefe Features (`10:522-529,552-558`) | ✔ (code-belegt) |
| P1.40 | Bot/DB | P1 | Unconditional CREATE+INSERT in `pump_dump_events` pro Symbol pro 10s-Tick → ~108 stmt/s, ~4,6M Rows/Tag; rsi/tsi-Spalten nie befüllt | ~ (Step 2: Tabelle existiert, schmales Schema) |
| P1.41 | Bot/DB | P1 | Shadow-Inserts in `ml_predictions_master` ohne Cooldown → bis 8.640 Rows/Tag/Symbol; Step 2: 31k EPD1-Rows/7d; vergiftet Tracker-Stats und Kalibrierungsmessungen | ✔ (quantifiziert) |
| 13-B/P2 | Bot | P2 | `float(None)`-Crash bei SQL-NULL (`10:537`) killt den ganzen 10s-Zyklus | ✔ (code-belegt) |
| 13-B/P2b | Trainer | P2 | Sample-Weights ohne Nachkalibrierung; `max(prob_pump,prob_dump)` als „Confidence" (X-R4) | ✔ |
| 09-M | Bot | Mittel | Ladder-Alerts refeuern alle 300s während einer anhaltenden Bewegung (Alert-Storm); Round-Level-Cooldown asymmetrisch (MARKET alle 180s) | ~ (offen) |
| N3 | Daten | — | `ticker_10s` leer — EPD1 rein in-memory; suggerierte Trainingsdaten-Basis existiert nicht | ✔ (Report 17) |

Positiv: Feature-Positionen 10/10 exakt, Klassen-Mapping korrekt (Report 13).

## 4. Abhängigkeiten & Querschnitts-Risiken

- **Kernrisiko = fehlendes `vol_ratio ≥ 5`-Gate live → OOD:** Das Modell beantwortet permanent Fragen, für die es keine Trainingsdaten hat (X-R1/X-R3/X-R4 alle betroffen). Der Live-Ertrag hängt damit an S/R-Konstruktion + Marktregime, nicht am Modell.
- **R3 (TZ)** über die naiven Zeitspalten; **R1** trifft EPD1 weniger direkt (Ticker-basiert), aber die DB-Featurepfade erben P1.39.
- **Shadow-Flut (P1.41)** ist zweite Ursache für corr≈0 in den Step-2-Kalibrierungsmessungen und verzerrt via Market-Tracker (P1.44) jede Per-Bot-Statistik.
- Nachfolger-Konzept existiert: **S6 „Pump-Exhaustion-Short"** (Report 15, Tier 2) — Short-only, Gate live gespiegelt, Microstructure-Features, First-Touch-Label; setzt aber die N3-Entscheidung (ticker_10s befüllen) voraus.

## 5. Sanierungsplan

**a) Sofort (ohne Retrain; Report 13 Sofortmaßnahme 2 + Report 16):**
1. **1-Zeilen-Gate-Fix:** `vol_ratio ≥ 5` vor `predict` spiegeln — „billigster Fix der ganzen Flotte", bringt das Modell erstmals in seine Trainingsverteilung.
2. **Shadow-Cooldown** (per Symbol, ~15 min) für `ml_predictions_master` + Consumer filtern `posted=TRUE` (P1.41).
3. **NULL-Guard** bei `10:537` (verhindert Zyklus-Kill).
4. Direction-Gate: **LONG zu** (50,2% WR; Report 14 D.5 / 16 Abschnitt 8).
5. P1.39-Restpfade über `_find_bucket_before/range` routen; `pump_dump_events`-CREATE einmalig + Inserts samplen (P1.40); Juli-Drift beobachten.

**b) Retrain/Umbau:**
- Kurzfristig reicht ggf. der Gate-Fix (Report 13 Reihenfolge: „EPD1 (Gate-Fix reicht ggf. vorerst)"). Danach Retrain nach dem gemeinsamen Gerüst (Feature-Builder des Bots importieren, Episoden-Dedup, zeitlicher Split, First-Touch-Label der Short-Geometrie, Kalibrierung) — oder direkt der Umbau zu **S6 Pump-Exhaustion-Short** als sauberer Nachfolger. Nach Gate-Fix + Retrain „Potenzial Richtung B" (Report 16).

**c) Offene Fragen:**
- Provenienz des deployten `pump_dump_model.pkl` (Trainingslauf/-zeitraum unbekannt, Training seit wann auskommentiert?).
- `ticker_10s`: befüllen (Trainingsdaten-Quelle für S6) oder droppen (N3).
- Hält die SHORT-Asymmetrie außerhalb der Alt-Pump-Phase Mai/Juni? (rollierende Re-Validierung der Gates).

## 6. Belege

- `AUDIT_TODO.md` P1.39–P1.41 · `audit_reports/09_intelligence.md` (Detector-Findings) · `audit_reports/13_x_ml_trainers.md` (EPD1-Abschnitt, X-R1..R6, Sofortmaßnahmen) · `audit_reports/14_bot_performance_db.md` (n=4.392, +14.222, SHORT 76,5% vs LONG 50,2%, Juli −345) · `audit_reports/STEP2_DB_VERIFICATION.md` (Kalibrierung flach, Shadow-Flut 31k/7d) · `audit_reports/16_strategy_concept_evaluation.md` (Note C+, Abschnitt 4) · `audit_reports/15_strategy_proposals.md` (S6) · `audit_reports/17_monitor_replay_and_gaps.md` (N3, N4, Monitor-Vorbehalt).

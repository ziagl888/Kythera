# Dossier: MIS1 (Pump/Dump-Horizont-Batterie)

> Batterie aus 8 binären XGBoost-Klassifikatoren ({8,24,72,168}h × {pump,dump}), stündlich pro Coin. Note (Report 16): **Familie C+ — MIS1-72H B− (Arbeitspferd, +15.868 netto), MIS1-168H C−, MIS1-8H/24H D.** Kernverdikt (Report 13): technisch konsistent verkabelt, statistisch nicht vertrauenswürdig; die starke 72H-Performance entsteht TROTZ der Trainingsmethodik, nicht wegen ihr. Dringendster Retrain-Kandidat der Flotte.

## 1. Steckbrief

| | |
|---|---|
| Bot-Datei | `11_ai_mis_bot.py` (stündlicher Scan ~:11 nach dem :02-Indikator-Lauf) |
| Modell-Artefakte | `pump_model_{8,24,72,168}h_{pump,dump}_final.pkl` + `threshold_*` (atomar mit den Modellen gespeichert) |
| Trainer | `legacy_trainers/X5-analyze_indicators_v8.py` — **nachträglich gefunden** (f-String-Dateiname täuschte alle Literal-Greps; Report 13 Addendum). Verifikation: Hyperparameter (1000/4/0.02/spw1.5/gamma2/lambda10) und alle 67 Features inkl. der Unfall-Features exakt reproduziert |
| Trainingsdatum | Modelle + Thresholds 26./27.01.2026 |
| Datenquelle | 1h-Indikator-Tabellen der Live-DB, 400-Tage-Fenster mit heutiger coins.json (Survivorship), Training inkl. Forming-Candle-Rows |
| Label | Close-to-Close-Return über den Horizont: ±5%/8h, ±10%/24h, ±15%/72h, ±25%/168h — **kein Pfad/SL, reine Zukunftsrendite** (X-R1) |
| Features | 67, identisch über alle 8 Modelle (Introspektion; darunter Ticker-Leakage-Unfall-Features aus der `line_cols`-Schleife) |
| Thresholds | pro Modell aus `threshold_*` (z.B. 168h_pump 0.2825, nur 3 Punkte über dem 0.25-Shadow-Floor); Fallback 0.60 ≠ Init-Default 0.5; Auswahl per Cross-Horizon-Argmax der rohen Probabilities |
| Signalweg | AI-Channel via Outbox/Cornix; TP/SL aus `calculate_smart_targets`; publiziert TP1–5, Monitor scored bis 21 Targets (P2.31) |

## 2. Live-Bilanz (Stand 2026-07-03, aktive Ära 24.02.–03.07., dedupliziert, ungehebelt)¹

| Modell | n | WR | ø PnL | Median | Σ netto |
|---|---|---|---|---|---|
| MIS1-72H | 11.822 | 63,9% | +1,44% | 0,00 | **+15.868** |
| MIS1-168H | 7.167 | 58,5% | +1,07% | −0,03 | +6.928 |
| MIS1-8H/24H | 1.003 | ~52% | +1,4% | negativ | +1.261 |

- **Monatstrend:** 72H in jedem Monat positiv; 168H seit Mai driftend (WR 48/49/35%); 8H/24H rein tail-getrieben.
- **Richtungssplit:** in den Quellen nicht ausgewiesen.
- **Kalibrierung (Step 2):** 72H **negativ** (72% WR @conf<0.4 → 65% @0.5–0.6 — Schwellen bedeutungslos, stützt P1.17); 168H flach; 8H positiv (91% @0.7–0.8, kleine n) → 8H ist einer der vier echt kalibrierten Kandidaten für S4 „Calibration-Sized Positions" (Report 15).
- In `closed_ai_signals` existieren tote Alt-Namensvarianten (`MIS1-72h_dump`, `MSI1-*`), zu 100% zensiert — beim Purge entfernen.

¹ Vorbehalt (Report 17): Alle Zahlen sind monitor-generiert; das Monitor-Scoring stimmt im Classic-Replay nur zu 63,4% mit der First-Touch-Wahrheit überein, und AI-Trades sind rückwirkend nicht auditierbar (`ai_signals`-Rows werden beim Close gelöscht, N4). Per-Trade-Wahrheit unzuverlässig, Netto-Bias moderat.

## 3. Befunde (konsolidiert)

| ID | Ebene | Schwere | Befund | Status |
|---|---|---|---|---|
| P1.17 / 06-CRITICAL | Bot | P1 | Prediction auf der laufenden Kerze (iloc[-1:], :11 = ~1/6-Partialvolumen) mit stale :02-Indikatoren → jede Prediction strukturell verzerrt | ✔ (Step 3: Indikator-Zeile der Forming Candle real vorhanden; Step 2: Negativ-Kalibrierung stützt) |
| P1.18 / 06-HIGH | Bot | P1→P3 | Ein Feature-Set für alle 8 Modelle + `.values` deaktiviert Namensvalidierung → Permutationsrisiko | ✘ widerlegt (Step 3: alle 8 pkls identische 67 `feature_names_in_`, Parity-Test fehlerfrei); `.values`-Fragilität bleibt als P3 |
| 13-P1 (Leakage) | Modell | P1 | Unfall-Features: `pct_distance` über abgeleitete Spalten → Werte in Coin-Preisskala; Bäume splitten real darauf (168h_dump 558 Splits, 168h_pump Top-Feature 10,4% Importance) → **Ticker-/Preisklassen-Leakage**, an der Trainer-Quelle (`line_cols`-Schleife, Z.69) bestätigt | ✔ bestätigt |
| 13-Addendum-P0 | Trainer | P0 | `StratifiedKFold(shuffle=True)` über stündliche Samples mit 8–168h überlappenden Label-Fenstern → Zwillings-Leakage; berichtete Precision stark inflationiert | ✔ bestätigt (Code) |
| 13-Addendum-P1 | Trainer | P1 | Threshold = beste Precision **über die 5 Folds gemaxt** (Maximum-Statistik), Recall-Floor nur 3% | ✔ bestätigt (Code) |
| 13-Addendum-P1 | Trainer | P1 | Final-Modell auf ALLEN Daten gefittet, Threshold stammt aus den Shuffle-Folds → Operating-Point passt nicht zum deployten Modell | ✔ bestätigt (Code) |
| 13-Addendum-P2 | Trainer | P2 | Keine Kalibrierung (spw=1.5), `fillna(0)`-Kaskade, Training inkl. Forming-Candle-Rows, Survivorship (heutige coins.json über 400 Tage) | ✔ bestätigt (Code) |
| P2.32 / 06-MEDIUM | Bot | P2 | `autocommit=True` → Outbox/ai_signals/master-log-Inserts nicht atomar | ~ offen |
| P2.33 / 06-MEDIUM | Bot | P2 | Best-Candidate vergleicht rohe Probabilities verschieden kalibrierter Modelle → unter-Schwelle-Kandidat verdrängt über-Schwelle-Signal | ~ offen |
| P2.34 / 06-MEDIUM | Bot | P2 | `fillna(0)` reinigt kein `inf` aus Zero-Volume-Divisionen; predict-Fehler verschluckt | ~ offen |
| P2.31 / 06-MEDIUM | Monitor | P2 | Subscriber sehen TP1–5, Monitor scored bis 21 Targets → Live-Statistik ≠ Cornix-Realität | ✔ (Step 2: `targets_hit` bis 21) |
| 13-P2 | Modell | P2 | 1000 Bäume ohne Early Stopping, identische Hyperparameter für alle 8 Horizonte; tote Binär-Flags (`rsi_14_above_50` in allen 8 Importance 0) | ✔ bestätigt |
| 13-P3 | Modell | P3 | 168h_pump-Threshold 0.2825 nur 3 Punkte über 0.25-Shadow-Floor (Shadow-Band leer); Fallback 0.60 ≠ 0.5 | ✔ bestätigt |
| 06-LOW | Bot | P3 | `ai_signals`-Presence-Check blockiert Signale UND Shadow-Logging unbegrenzt (hängt am Monitor-Delete); Dead-Code `best_prob<0.25` | ~ offen |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1 Forming Candle (kritisch):** MIS1 ist explizit als kritischster R1-Konsument genannt; live bewiesen (Step 2: Partial-Kerze + Indikatorzeile darauf).
- **X-R1** (Label = reine Zukunftsrendite ohne SL-Pfad), **X-R2/X-R3** (Fold-Max-Threshold, Shuffle-Zwillings-Leakage), **X-R4** (unkalibrierte „Confidence"), **X-R5** (fillna-Silent-Default), **X-R6** (Serving auf Forming Candle) — MIS1 trifft alle sechs.
- **P0.4/P2.25 Whitelist:** Der Orchestrator gated die **gesamte MIS-Familie** über Raw-Namen-Rows, die seit 19.04. eingefroren sind → Regime-Gate auf 2,5 Monate alten Statistiken (✔ Step 2).
- **Monitor-Label-Vorbehalt (Report 17):** Alle WR/PnL und damit auch jedes künftige Trainingslabel sind monitor-generiert (63,4%-Replay-Konsistenz); AI-Historie ohne N4-Fix nicht replayfähig.
- **R3 TZ:** naive/gemischte Zeitspalten betreffen auch MIS1-Auswertungen (Session-TZ Europe/Bucharest).

## 5. Sanierungsplan

**(a) Sofort, ohne Retrain:**
1. Closed-Candle-Fix im Bot (`iloc[-2:-1]` bzw. `open_time < date_trunc('hour', NOW())`) — P1.17.
2. Confidence nicht mehr als „%" kommunizieren; Operating-Points konservativ anhand der Step-2-Kalibrierungstabellen (Report 13, Maßnahme 4).
3. Kandidaten-Ranking auf `prob − threshold` umstellen (P2.33); `replace([inf,-inf],nan)` vor fillna (P2.34); autocommit entfernen, ein Commit wie ATS/RUB (P2.32).
4. Whitelist-Fix (P0.4: `pretty_name()` im Orchestrator + Staleness-Gate), damit das Regime-Gate für die MIS-Familie wieder auf frischen Statistiken arbeitet.

**(b) Retrain (Priorität #1 des gesamten Retrain-Programms, Reports 13/16):**
- Versionierter Trainer im Repo, der den **Feature-Builder des Bots importiert**; `line_cols`-Fix (Leakage-Features raus).
- Label = First-Touch-Simulation der tatsächlich geposteten Order-Geometrie (X-R1 / P0.10-Simulator, V3 aus Report 15) statt Close-to-Close-Return.
- Nur geschlossene Kerzen (R1-Fix zuerst!), zeitlicher 3-Wege-Split mit Embargo + Episoden-Dedup, Threshold auf Validation, Isotonic-Kalibrierung out-of-time.
- Artefakte + meta.json (Features, Threshold, Trainingszeitraum, Git-SHA); Bot lädt Threshold aus meta; Startup-Assertion „kein Feature konstant".
- **MIS1-8H/24H im Retrain eher streichen** (Report 16: konzeptionell dünnste Kombination); Fokus 72H, 168H nur mit Drift-Monitoring.
- Vorbedingungen: Monitor-Rewrite (Report 17 — liefert die Labels), Dedup-Index auf `closed_ai_signals` (V2), N4-Fix (SL/Targets beim Close mitschreiben).

**(c) Offene Fragen:** Warum funktioniert 72H trotz der Methodik (Hypothese Report 13: Momentum-/Vol-Features tragen auf langen Horizonten echtes Signal)? 168H-Drift seit Mai — Regime oder Modellalterung?

## 6. Belege

- `AUDIT_TODO.md` — P1.17/P1.18, P2.31–2.34, R1/R3, P0.4.
- `audit_reports/06_ai_bots_a.md` — Bot-Engine-Findings (Forming Candle, feature_names, inf, Argmax, autocommit, LOW-Punkte).
- `audit_reports/13_x_ml_trainers.md` — MIS1-Sektion (Introspektion, P1.18-Widerlegung, Leakage) + **Addendum** (X5-Trainer gefunden, Label-Definitionen, Trainer-Defekte).
- `audit_reports/14_bot_performance_db.md` — realisierte Zahlen je Horizont, Monatstrends, Alt-Namensvarianten.
- `audit_reports/STEP2_DB_VERIFICATION.md` — Kalibrierungsmessungen, Whitelist-Freeze, targets_hit bis 21, R1-Beweis.
- `audit_reports/16_strategy_concept_evaluation.md` — Noten (72H B−, 168H C−, 8H/24H D), Konzept-Verdikt, Retrain-Priorität #1.
- `audit_reports/15_strategy_proposals.md` — V1–V3-Voraussetzungen, S4 (MIS1-8H als kalibrierter Sizing-Kandidat).
- `audit_reports/17_monitor_replay_and_gaps.md` — 63,4%-Replay-Vorbehalt, N4 (AI-Trades nicht auditierbar).

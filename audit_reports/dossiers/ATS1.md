# Dossier: ATS1 (TSI-Sniper)

> Event-getriebener Richtungsklassifikator: nur bei TSI-Fast-Crossover auf der letzten geschlossenen Kerze wird das XGBoost-Modell befragt. Note (Report 16): **C+** (Σ +1.622 netto). Kernverdikt (Report 13): architektonisch die Blaupause der Familie (Event-Gate, korrekte Kerzen-Disziplin), aber OBV-Train/Serve-Skew invertiert die Confidence; als grobes Ranking nutzbar, nicht als Wahrscheinlichkeit — Short-Modell unvalidiert.

## 1. Steckbrief

| | |
|---|---|
| Bot-Datei | `12_ai_ats_bot.py` (stündlich; Scheduler-Kommentar sagt :08, läuft :13 — P3-Drift) |
| Modell-Artefakte | `model_tsi_long_robust.pkl` / `model_tsi_short_robust.pkl` |
| Trainer | `legacy_trainers/X8-TSI-ML-V4.py` (long) / `X8-TSI-ML-V5.py` (short); Trainingsdaten aus `legacy_trainers/X8-TSI-EXPORT-V4.py` / `X8-TSI-EXPORT-V5.py` (Provenienz ✔ geklärt, Report 13) |
| Trainingsdatum | Trainingsdaten enden **2025-12-15** — zum Audit-Zeitpunkt 6,5 Monate stale |
| Datenquelle | Export aus den 1h-DB-Tabellen; OBV im Training über ~300 Tage kumuliert. **Short-Modell (V5) trainiert auf `{coin}_1h_X`-Tabellen — andere Quelle als live!** |
| Label | 2,5%/1,5%-Bracket, 96h Horizont; TP-vor-SL bei ambiguen Kerzen (optimistischer Bias in High-Vol-Samples) — ≠ Live-Geometrie (SR-Targets ≥5%, DCA-Entry2, SR-SL) |
| Features | 29, Train↔Serve identisch (positiv verifiziert) |
| Thresholds | per Profit-Factor-Maximierung **auf dem Test-Set** gewählt (`ML-V4:91-110`) — Maximum-Statistik-Artefakt (X-R2) |
| Signalweg | TSI-Crossover-Gate → predict → AI-Channel via Outbox/Cornix; TP/SL via `get_hvn_and_sr_levels`/SR-Konstruktion; publiziert TP1–3, Monitor scored bis 10–20 Targets (P2.31). Einziger Bot der Familie mit korrekter Closed-Candle-Disziplin (`-2`) |

## 2. Live-Bilanz (Stand 2026-07-03, aktive Ära 24.02.–03.07., dedupliziert, ungehebelt)¹

- **n = 1.768 · WR 65,8% · ø +1,02%/Trade · Median 0,00 · Σ netto +1.622** — positiv trotz Trainer-Mängeln (Report 14: Urteil „behalten/fokussieren").
- **Richtungssplit:** in den Quellen nicht ausgewiesen.
- **Monatstrend:** keine auffällige Monats-Drift in den Quellen berichtet (anders als MIS1-168H/EPD1).
- **Kalibrierungsbefund (Step 2): leicht negativ — invertiert im oberen Band:** Bucket 0,6–0,7 → **71% WR**, Bucket 0,8–0,9 → **57% WR**. Report 13 erklärt das kausal über den OBV-Skew: die High-Confidence-Region liegt live out-of-distribution.

¹ Vorbehalt (Report 17): monitor-generierte Zahlen; Monitor-Scoring nur 63,4% replay-konsistent (Classic-Stichprobe), AI-Trades rückwirkend nicht auditierbar (N4: `ai_signals` löscht SL/Targets beim Close).

## 3. Befunde (konsolidiert)

| ID | Ebene | Schwere | Befund | Status |
|---|---|---|---|---|
| 13-P0 (OBV-Skew) | Trainer/Modell | P0 | `obv_val`/`obv_ratio` Train/Serve-Skew: Training kumuliert ~300 Tage, live 500-Kerzen-Fenster mit Normalisierung, die `obv_ratio` mathematisch verändert → High-Confidence-Region live OOD | ✔ bestätigt (Code + Step-2-Inversionsmessung) |
| 13-P0 (Label) | Trainer | P0 | Label-Geometrie 2,5%/1,5%/96h ≠ Live-Geometrie (SR-Targets ≥5%, DCA-Entry2, SR-SL) — X-R1 | ✔ bestätigt (Code) |
| 13-P1 | Trainer | P1 | TP-vor-SL bei ambiguen Kerzen (`EXPORT-V4:272-275`) → optimistischer Bias genau in High-Vol-Samples | ✔ bestätigt (Code) |
| 13-P1 | Trainer | P1 | Short-Modell (V5) trainiert auf **`{coin}_1h_X`**-Tabellen — andere Datenquelle als live → Short unvalidiert | ✔ bestätigt (Code) |
| 13-P1 | Trainer | P1 | `scale_pos_weight` ohne Nachkalibrierung (X-R4) | ✔ bestätigt (Code) |
| 13-P2 | Trainer | P2 | Threshold-PF-Maximierung auf dem Test-Set; Daten 6,5 Monate stale. Positiv: zeitlicher Split korrekt, 29/29 Features identisch | ✔ bestätigt (Code) |
| 06-MEDIUM | Bot | P2 | OBV-Features fensterlängen-abhängig trotz Normalisierungs-Fix: `len(rows)>=50` lässt 50–499-Kerzen-Coins mit anderem Akkumulationsfenster durch | ~ offen |
| P2.29 / 06-MEDIUM | Bot (core) | P2 | `get_hvn_and_sr_levels` liest 95d **ohne ORDER BY** (SL/TP-Quelle für SRA1/ATS1/RUB1) → Phantom-Extrema als SL/TP-Preise möglich | ~ offen `[DB]` |
| P2.31 / 06-MEDIUM | Monitor | P2 | Publiziert TP1–3, Monitor scored bis 10–20 Targets → Live-Statistik ≠ Cornix-Realität | ✔ (Step 2: targets_hit bis 21 flottenweit) |
| 06-LOW | Bot | P3 | Scheduler-Kommentar ≠ Trigger-Minute; Estimator-Truthiness (`if not MODEL` statt `is None`, 12:83,91) | ~ offen |
| (Kontrast) P1.17 | Bot | — | Forming-Candle-Prediction: ATS1 macht es mit `-2` **richtig** — explizit als Positiv-Referenz genannt | ✘ nicht betroffen |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1 Forming Candle:** ATS1 weicht dem Serving-Teil aus (Closed-Candle `-2`), sitzt aber auf derselben Datenpipeline; gespeicherte Historie enthält Partial-/Broadcast-Werte (P1.11/P1.12).
- **X-R1** (Bracket-Label ≠ gehandelte Geometrie), **X-R2** (Threshold auf Test-Set), **X-R4** (spw ohne Kalibrierung), **X-R5** (Silent-Defaults im Export) treffen ATS1; **X-R3/X-R6** entschärft (zeitlicher Split korrekt, Closed-Candle-Serving).
- **P2.29:** teilt die ORDER-BY-lose SL/TP-Quelle mit SRA1/RUB1.
- **Whitelist/Orchestrator:** ATS1 ist nicht unter den in Step 2 gelisteten eingefrorenen Raw-Namen-Rows aufgeführt; das generelle Gate-/Fallback-Problem (P0.4/P2.23) betrifft die Signal-Pipeline aber flottenweit.
- **Monitor-Label-Vorbehalt (Report 17):** WR/PnL monitor-generiert; jedes Retrain-Label erbt das bis zum Monitor-Rewrite.

## 5. Sanierungsplan

**(a) Sofort, ohne Retrain:**
1. **Operating-Point auf den empirisch besten 0,6–0,7-Bucket legen** (Report 13 Maßnahme 4, Report 16 §8.3 — „quasi kostenlos").
2. Confidence nicht mehr als „%" kommunizieren (X-R4).
3. `ORDER BY open_time ASC` in `get_hvn_and_sr_levels` (P2.29, eine Zeile).
4. ML-Pfad erst ab ≥500 Kerzen zulassen (06-MEDIUM-Fix).
5. Exakt die publizierten Targets in `ai_signals` speichern (P2.31).

**(b) Retrain (Report 13/16; Reihenfolge: nach MIS1/AIM1/ABR1/ATB1+RUB1):**
- Skalenfreie OBV-Features (behebt den Inversions-Mechanismus an der Wurzel).
- Short-Modell auf dieselbe Datenquelle wie live stellen (weg von `_1h_X`).
- Label = First-Touch-Simulation der echten SR/DCA-Geometrie (X-R1/P0.10-Simulator); TP-vor-SL-Ambiguität konservativ auflösen.
- Gemeinsamer Feature-Builder Bot↔Trainer, zeitlicher 3-Wege-Split, Threshold auf Validation, Isotonic-Kalibrierung out-of-time, meta.json; frische Daten (Stand 2025-12-15 ersetzen).
- Vorbedingungen: R1-Fix, Monitor-Rewrite (Report 17), Dedup-Index (V2).

**(c) Offene Fragen:** Richtungssplit LONG/SHORT nie ausgewiesen — vor einem Direction-Gate messen. Ist der Live-Gewinn Modell-Skill oder Event-Gate+SR-Konstruktion (Report 16: Familienbefund „kein belegbarer ML-Skill")? Short-Modell-Validität erst nach Retrain beurteilbar.

## 6. Belege

- `AUDIT_TODO.md` — P2.29, P2.31, R1/R3-Kontext, P1.17-Kontrast („ATS macht es mit −2 richtig").
- `audit_reports/06_ai_bots_a.md` — Bot-Engine-Findings (OBV-Fensterlänge, get_hvn ohne ORDER BY, Targets-Divergenz, LOW-Punkte).
- `audit_reports/13_x_ml_trainers.md` — ATS1-Sektion (OBV-Skew, Label, V5-Quelle, Threshold, Verdikt „eingeschränkt; Short unvalidiert").
- `audit_reports/14_bot_performance_db.md` — realisierte Zahlen (n=1.768, +1.622 netto), Portfolio-Einstufung „behalten".
- `audit_reports/STEP2_DB_VERIFICATION.md` — Kalibrierungsmessung (leicht negativ, 0,6–0,7→71% vs 0,8–0,9→57%).
- `audit_reports/16_strategy_concept_evaluation.md` — Note C+, „architektonische Blaupause", Sofortmaßnahme Operating-Point.
- `audit_reports/15_strategy_proposals.md` — V1–V3-Voraussetzungen (Simulator als Label-Quelle).
- `audit_reports/17_monitor_replay_and_gaps.md` — 63,4%-Replay-Vorbehalt, N4.

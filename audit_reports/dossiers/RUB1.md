# Dossier: RUB1 (Rubberband Mean Reversion)

> Mean-Reversion-Bot: 4-fach-Extrem-Vorfilter + 9-Feature-ML als Snap-Back-Filter. Note (Report 16): **D+** (Σ +3.675 netto, aber tail-/SHORT-getrieben). Kernverdikt (Report 13): Modell **nicht vertrauenswürdig** — stiller MACD-9/21↔12/26-Semantikbruch + memorisierter Random-Split; der Live-Gewinn kann nicht vom ML stammen, er kommt aus Vorfilter + S/R-Konstruktion.

## 1. Steckbrief

| | |
|---|---|
| Bot-Datei | `13_ai_rub_bot.py` (stündlich; Scheduler-Kommentar sagt :12, läuft :10 — P3-Drift) |
| Modell-Artefakte | `long_reversion_model.joblib` / `short_reversion_model.joblib` |
| Trainer | `legacy_trainers/BT3-1` (Datagrepper, MACD `ta.macd(fast=9,slow=21)`), `BT3-2-ml_trainer.py` (Training), `BT3-3-optimizer.py` (Thresholds) — Provenienz ✔ geklärt (Report 13) |
| Trainingsdatum | in den Quellen nicht dokumentiert |
| Datenquelle | Trainer: BT3-1-Export (eigene MACD-Berechnung 9/21); Live: 1h-DB-Indikator-Spalten (`macd_dif_normal_12_26_9`!) + ~95d Closes für die Regressionsgerade |
| Label | **+10%-Touch/72h ohne SL/Drawdown-Pfad** (X-R1) — beim Messer-Fangen ist genau der Drawdown-Pfad die Risikogröße |
| Features | 9 |
| Thresholds | 0.75 / 0.85 — per Precision-Maximierung auf Mini-Test-Set (**>5 Trades!**, `BT3-3:31`) |
| Signalweg | Vorfilter (≥8% unter/über 90d-Regression + RSI<30 + TSI<−15 + Donchian-Touch) → predict → AI-Channel via Outbox/Cornix; TP/SL via `get_hvn_and_sr_levels`/SR-Konstruktion; publiziert TP1–3, Monitor scored bis 10–20 Targets (P2.31) |

## 2. Live-Bilanz (Stand 2026-07-03, aktive Ära 24.02.–03.07., dedupliziert, ungehebelt)¹

- **n = 2.496 · WR 57,6% · ø +1,57%/Trade · Median −0,06 · Σ netto +3.675** — die Summe stammt aus Tail-Gewinnen (p95 +33%).
- **Richtungssplit: SHORT 63,9% vs LONG 48,7% WR** — eine der großen Richtungs-Asymmetrien der Flotte; Reports 14/15/16 empfehlen einhellig: **LONG-Gate zu**.
- **Monatstrend:** keine spezifische Monats-Drift in den Quellen berichtet.
- **Kalibrierungsbefund:** RUB1 taucht in der Step-2-Kalibrierungstabelle nicht mit eigener Zeile auf; Report 16 verdikt den ML-Layer als Rauschen (MACD-Bruch, memorisierter Split) — die Confidence ist ohnehin nicht als Wahrscheinlichkeit belastbar (X-R4).

¹ Vorbehalt (Report 17): monitor-generierte Zahlen; Monitor-Scoring nur 63,4% replay-konsistent (Classic-Stichprobe), AI-Trades rückwirkend nicht auditierbar (N4: `ai_signals` löscht SL/Targets beim Close).

## 3. Befunde (konsolidiert)

| ID | Ebene | Schwere | Befund | Status |
|---|---|---|---|---|
| 13-P0 (MACD) | Trainer↔Bot | P0 | **MACD-Semantikbruch:** trainiert auf `ta.macd(fast=9,slow=21)` (`BT3-1:85-87`), live werden `macd_dif_normal_12_26_9`-DB-Spalten unter demselben Feature-Namen gefüttert (`13:92-93,150-151`) — für die Namensvalidierung unsichtbar | ✔ bestätigt (Code) |
| 13-P0 (Split) | Trainer | P0 | Random-Split (`BT3-2:34`) über stundenweise duplizierte Persistenz-Episoden → Test-AUC = Memorization; live wird via 4h-Cooldown nur die *erste* Episodenstunde gehandelt, Training mittelt über alle | ✔ bestätigt (Code) |
| 13-P1 (Threshold) | Trainer | P1 | Thresholds 0.75/0.85 per Precision-Maximierung auf Mini-Test-Set (>5 Trades) — Maximum-Statistik (X-R2) | ✔ bestätigt (Code) |
| 13-P1 (Label) | Trainer | P1 | Label ohne SL/Drawdown-Pfad — Knife-Catch-Risiko unmodelliert (X-R1) | ✔ bestätigt (Code) |
| P1.19 / 06-HIGH | Bot | P1 | Prediction auf Forming-Candle-Indikatoren: LIMIT 1 = offene Kerze aus ~2 min Daten; rsi/donchian-Trigger + alle 9 ML-Features mischen :10-Live-Preis mit :02-Partial-Indikatoren; Regression inkl. aktueller Kerze (95d vs 2160 Kerzen exkl. im Training) | ~ offen `[DB]` (Code doppelt belegt: AUDIT_TODO + Report 13; live nicht separat gemessen) |
| 13-P1 (Parität) | Bot/Pipeline | P1 | DB-Indikator-Parität unverifiziert — Step 2 bewies bereits DB-`rsi_14` ≠ Wilder (Δ≈4,8) → **Vorfilter-Gates (rsi<30, tsi<−15) feuern live in einer anderen Population als im Training** | ✔ teilbestätigt (RSI); TSI-Skalierung ~ offen |
| P2.29 / 06-MEDIUM | Bot (core) | P2 | `get_hvn_and_sr_levels` liest 95d **ohne ORDER BY** (SL/TP-Quelle für SRA1/ATS1/RUB1) → Phantom-Extrema als SL/TP-Preise möglich | ~ offen `[DB]` |
| P2.31 / 06-MEDIUM | Monitor | P2 | Publiziert TP1–3, Monitor scored bis 10–20 Targets → Live-Statistik ≠ Cornix-Realität | ✔ (Step 2: targets_hit bis 21, RUB1 zweistellig) |
| 06-MEDIUM (Perf) | Bot | P2 | Stündlich ~95d × 538 Coins Closes (~1,2M Rows/h) für eine lineare Regression + per-Row-`.apply` | ~ offen |
| 06-LOW | Bot | P3 | `dist_to_trend_pct`-Vorzeichenkipp/Blow-up bei Trend-Wert nahe 0/negativ; Estimator-Truthiness (13:52,60); Scheduler-Kommentar-Drift | ~ offen |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1 Forming Candle:** RUB1 explizit betroffen (P1.19); der System-Fix (Closed-Candle-Vertrag) ist Voraussetzung für jedes Retrain.
- **X-R1** (Touch-Label ohne SL), **X-R2** (Threshold auf Mini-Test-Set), **X-R3** (Split-Leakage — laut Report 13 „RUB1 am schlimmsten"), **X-R4** (unkalibrierte Confidence), **X-R6** (Forming-Candle-Serving) treffen RUB1; dazu der familien-eigene Feature-Semantikbruch (MACD) als Paradefall für den gemeinsamen Feature-Builder.
- **P2.12 RSI-Formel:** DB-RSI ist ewm(span), kein Wilder (Δ ø 4,8) — verschiebt die Vorfilter-Population (siehe oben).
- **P2.29:** teilt die ORDER-BY-lose SL/TP-Quelle mit SRA1/ATS1.
- **Whitelist/Orchestrator:** RUB1 ist nicht unter den in Step 2 gelisteten eingefrorenen Raw-Namen-Rows aufgeführt; das flottenweite Gate-/TRANSITION-Fallback-Problem (P0.4/P2.23) gilt für die Pipeline insgesamt.
- **R3 TZ:** gemischte naive Zeitspalten betreffen auch RUB1-Auswertung/Cooldowns.
- **Monitor-Label-Vorbehalt (Report 17):** WR/PnL monitor-generiert; S11-artige Label-Vorhaben und jedes Retrain brauchen erst den Monitor-Rewrite.

## 5. Sanierungsplan

**(a) Sofort, ohne Retrain:**
1. **LONG-Direction-Gate schließen** (SHORT 63,9% vs LONG 48,7% — Reports 14 D.5, 15 S1, 16 §8.2).
2. Closed-Candle-Fix (P1.19: `open_time < date_trunc('hour', NOW())`, `curr_close` aus derselben geschlossenen Kerze).
3. `ORDER BY open_time ASC` in `get_hvn_and_sr_levels` (P2.29, eine Zeile).
4. Confidence nicht mehr als „%" kommunizieren; Operating-Points konservativ setzen (Report 13 Maßnahme 4).
5. Exakt die publizierten Targets speichern (P2.31); Regression in SQL/vektorisiert statt 1,2M-Row-Fetch (Perf).

**(b) Retrain (Report 13/16: nur Komplett-Retrain, kein Patch; Reihenfolge nach MIS1/AIM1/ABR1, zusammen mit ATB1):**
- **Gemeinsamer Feature-Builder Bot↔Trainer** (behebt die MACD-Klasse strukturell) — Kernanforderung aus Report 13.
- Label = First-Touch-Simulation der echten geposteten Geometrie **mit SL-Pfad** (X-R1/P0.10-Simulator, V3 aus Report 15).
- Episoden-Dedup + zeitlicher 3-Wege-Split mit Embargo (X-R3-Fix); Threshold auf Validation (statt >5-Trades-Test-Set); Isotonic-Kalibrierung out-of-time; meta.json + Startup-Assertions.
- Indikator-Parität klären (Wilder-RSI vs ewm, TSI-Skalierung) bzw. Vorfilter auf die DB-Semantik neu tunen.
- Vorbedingungen: R1-Fix, Monitor-Rewrite (Report 17), Dedup-Index auf `closed_ai_signals` (V2).

**(c) Offene Fragen:** Trainingszeitraum/-datum unbekannt. TSI-Skalierungs-Parität ungeprüft. Trägt der Vorfilter allein (ohne ML-Gate) dieselbe Performance? P1.19 und P2.29 sind `[DB]`-Punkte ohne separate Live-Messung.

## 6. Belege

- `AUDIT_TODO.md` — P1.19, P2.29, P2.31, P2.12, R1/R3.
- `audit_reports/06_ai_bots_a.md` — Bot-Engine-Findings (Forming Candle, get_hvn ohne ORDER BY, Perf, dist_to_trend, Targets-Divergenz).
- `audit_reports/13_x_ml_trainers.md` — RUB1-Sektion (MACD-Bruch, Memorization-Split, Thresholds, Label, RSI-Parität) + X-R1..R6.
- `audit_reports/14_bot_performance_db.md` — realisierte Zahlen (n=2.496, +3.675, p95 +33%, Richtungssplit), Empfehlung LONG-Gate.
- `audit_reports/STEP2_DB_VERIFICATION.md` — RSI≠Wilder (Δ4,84), targets_hit-Beweis; keine eigene RUB1-Kalibrierungszeile.
- `audit_reports/16_strategy_concept_evaluation.md` — Note D+, Verdikt „Gewinn kann nicht vom ML stammen".
- `audit_reports/15_strategy_proposals.md` — S1 Direction-Gates (RUB1 nur SHORT), V1–V3-Voraussetzungen.
- `audit_reports/17_monitor_replay_and_gaps.md` — 63,4%-Replay-Vorbehalt, N4.

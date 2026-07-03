# Dossier: ABR1 — AI Break & Retest (Bot 18)

> ML-Klassifikation Continuation vs. Failed-Breakout nach Level-Retest. **Note C− (Report 16).** Kernverdikt: konzeptionell zweitbester ML-Ansatz der Flotte und live knapp positiv (+335, n=110) — aber das Modell fährt **bewiesen nur auf 7 von 18 Features** (P0.12) und ist ohne jede Out-of-Sample-Evaluation trainiert; Klassen-Inversions-Verdacht (P2.38) ist entwarnt.

## 1. Steckbrief

| Feld | Inhalt |
|---|---|
| Bot-Datei | `18_ai_abr1_bot.py` (stündlich, Code läuft Minute 2 — Kommentar behauptet Minute 10) |
| Artefakte | `bt2_model_LONG.json` / `bt2_model_SHORT.json` — XGBoost `multi:softprob`, `num_class=3`, 18 Features; **byte-identisch (md5)** mit den `_X`-Artefakten des Trainer-Laufs vom **31.12.2025** — NICHT der bessere `BT2-ML-Final_Saver`-Lauf vom 30.12. |
| Trainer | `legacy_trainers/BT2-Datagrepper-for-ML.py` (Features/Labels) + `BT2-ML-Trainer.py` (GridSearch-Training) + `BT2-Strategybacktester*.py` (Threshold-„Backtests" auf Trainingsdaten); Provenienz in Report 13 bewiesen |
| Trainingsdatum | 31.12.2025 |
| Datenquelle | 1h-DB-Daten, **coin-konkateniert und nicht zeitsortiert** → TimeSeriesSplit-CV schneidet Coins statt Zeit |
| Label-Definition | 3 Klassen; „success" = **Close-only nach 12h ab `lvl_price`** (kein SL-Pfad, X-R1); Live-Entry ist aber Retest-Close → optimistisch. `SUCCESS_CLASS_IDX=0` dreifach verifiziert (LabelEncoder alphabetisch, meta.json, num_class=3) |
| Features | 18 nominell — davon **11 konstant 0** (pandas_ta-Namens-Mismatch: `KAMA_9` vs `KAMA_9_2_30`, `TSI_12_7` vs `TSI_7_12_7`, `DCL_20` vs `DCL_20_20`, `BBL_20_2` vs `BBL_20_2.0_2.0` → NaN → `fillna(0)`); tot: dist_close_kama9, tsi×4, boll×3, donchian×3 → **real 7 Features** |
| Thresholds | live hardcoded **0.60 (LONG) / 0.80 (SHORT)** — aus „Backtests" auf den Trainingsdaten; Trainer-Optimum 0.77/0.92, Final_Saver-meta 0.79/0.86 → **drei widersprüchliche Stände** |
| Positiv (Report 07) | einziger der drei Bots mit korrekter Kerzen-Disziplin (Forming Candle ausgeschlossen), `autocommit=True`, DB-gestützter 4h-Cooldown |
| Channel | in den Quellen nicht dokumentiert |

## 2. Live-Bilanz (Stand 2026-07-03, aktive Ära, dedupliziert)

- **n = 110 · WR 63,6% · ø +3,15%/Trade · Median 0,00 · Σ netto +335 Preis-%** (Report 14: „klein; Modell real nur 7 Features")
- **Richtungssplit (Step 2): LONG 67,2% / SHORT 59,2% WR** → keine Klassen-Inversion, `SUCCESS_CLASS_IDX=0` konsistent (**P2.38 entwarnt**, deckt sich mit Commit d19a68d)
- Kalibrierung: keine belastbare Messung (Step-2-Tabelle „—", n zu klein); ehrlichste Trainings-Kennzahl: **CV-F1(success) = 0,134 ≈ Rauschen** (Final_Saver-meta)
- Einordnung Report 16: der kleine Gewinn stammt plausibel aus Setup + S/R-Konstruktion, nicht aus Modell-Skill[^1]

[^1]: **Monitor-Vorbehalt (Report 17):** Alle Zahlen monitor-generiert; First-Touch-Replay nur 63,4% Übereinstimmung (17,8% verpasste TP1, 18,8% TP1 trotz SL-zuerst); AI-Replay rückwirkend unmöglich (N4). Dazu P1.2/P2.7/P2.31/P1.9. Bei n=110 wiegt die per-Trade-Unsicherheit besonders schwer.

## 3. Befunde (konsolidiert)

Status: ✔ = bewiesen/bestätigt (Step 2/3) · ✘ = widerlegt/entwarnt · ~ = Code-Befund, offen

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| P0.12 (=P1.28) | Bot+Trainer | P0 | 11/18 Features konstant 0 — `expected_pta_cols` matcht pandas_ta-Namen nie; Split-Count-Beweis: exakt diese 11 Features haben 0 Splits in beiden Live-Modellen; Trainer (`BT2-Datagrepper-for-ML.py:77-92`) hatte identischen Bug → kein Skew, aber halbes Strategie-Signal fehlt | ✔✔ (Step 3, Booster-Dump) |
| R13-ABR1-1 (X-R2) | Trainer | P0 | Threshold + Win-Rate vollständig **in-sample** auf dem refitteten GridSearch-Modell gewählt; kein Hold-Out im ganzen Skript | ✔ (Step 3) |
| P2.38 | Modell | P2 | SUCCESS_CLASS_IDX-/SHORT-Label-Semantik-Verdacht (0↔1-Swap, „Bot shortet, wenn Modell steigend sagt") | ✘ entwarnt (Step 2: LONG 67,2%/SHORT 59,2%; Step 3: LabelEncoder+meta.json+num_class=3) |
| R13-ABR1-2 (X-R3) | Trainer | P1 | TimeSeriesSplit auf coin-konkatenierten, nicht zeitsortierten Daten → CV schneidet Coins, nicht Zeit | ✔ (Step 3) |
| R13-ABR1-3 (X-R1) | Trainer | P1 | Label Close-only nach 12h ab Level-Preis vs. Live-Retest-Close-Entry → optimistisch; Bot nutzt zudem unbestätigte Edge-Pivots, die im Training nie vorkamen | ✔ (Step 3) |
| R13-ABR1-4 | Trainer | P1 | Threshold-Chaos: live 0.60/0.80 vs. Trainer-Optimum 0.77/0.92 vs. Final_Saver-meta 0.79/0.86 — Herkunft „Backtests auf Trainingsdaten" | ✔ (Step 3) |
| R07-ABR1-a | Bot | MEDIUM | Signalpreis bis 3h stale: entry1/„CMP Entry" = Close einer bis zu 3 Kerzen alten Retest-Kerze | ~ |
| R07-ABR1-b | Bot | MEDIUM | Edge-gepaddete Pivot-Erkennung (`np.pad 'edge'` + greater_equal) → unbestätigte, repaintende Levels am rechten Rand | ~ |
| R13-ABR1-5 | Bot | P2 | `SUCCESS_CLASS_IDX` + Thresholds hardcoded statt aus meta.json geladen (Load-Assert fehlt) | ~ |
| R07-ABR1-c | Bot | LOW | Scheduling-Kommentar Minute 10, Code Minute 2 — kollidiert mit Indicator-Engine-Burst | ~ |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1 (Forming Candle):** ABR1 ist der **einzige** der drei AI-Bots (14/15/18), der sich korrekt verteidigt (`open_time < current_hour_utc`) — R1-Risiko hier gebannt, für Trainingsdaten aus denselben Tabellen aber weiter relevant.
- **R3 (TZ):** keine ABR1-spezifischen TZ-Findings in den Quellen; Session-TZ-Problematik (Europe/Bucharest) gilt systemweit.
- **X-R1/X-R2/X-R3/X-R4/X-R5:** alle verletzt — Label ohne SL-Pfad, In-Sample-Threshold, Coin-Split-Leakage, unkalibrierte „Confidence %", Silent-Default (`fillna(0)` versteckte den 11-Features-Bug über 3 Stufen).
- **Silent-Feature-Death-Muster (Report 07):** gemeinsam mit AIM1/ATB1; geteilte Startup-Assertion „kein Feature konstant" fängt die Klasse.
- **Artefakt-Governance:** Trainer lag nicht im Repo (jetzt `legacy_trainers/`); deployt wurde der 31.12.-Lauf statt des besseren Final_Saver-Laufs vom 30.12.; Klassen-Mapping/Features gehören IN die Artefakte (meta.json).
- **Whitelist/Orchestrator:** Gate-Statistiken WR-basiert und monitor-verzerrt (Report 16 §7 / Report 17) — bei n=110 besonders rauschanfällig.

## 5. Sanierungsplan

**(a) Sofort ohne Retrain:** Weiterbetrieb vertretbar (netto positiv, keine Inversion, saubere Verkabelung), aber: Confidence nicht mehr als „%" kommunizieren (Report 13, Maßnahme 4); Startup-Assertion „kein Feature konstant"; `SUCCESS_CLASS_IDX`/Thresholds aus meta.json laden + Load-Assert; Entry-Staleness fixen (letzter Preis für entry1 bzw. nur signalisieren, wenn Retest = jüngste geschlossene Kerze); Pivot-Bestätigung (`index <= len-PIVOT_WINDOW`).

**(b) Retrain-Anforderungen (für P0.12 zwingend — „RETRAIN both models"):** pta-Prefix-Matching-Fix (Vorlage `14:197-211`) in Bot UND Datagrepper; Neutraining mit allen 18 Features; zeitlicher 3-Wege-Split mit Embargo (zeitsortiert, nicht coin-konkateniert); Label = First-Touch der echten Geometrie **ab Retest-Close** (V3-Simulator, P0.10); Threshold auf Validation; Isotonic-Kalibrierung; meta.json (Features, class_mapping, Threshold, Zeitraum, Hash) im Artefakt. Priorität im Retrain-Programm (Report 16 §8): **#4** nach MIS1-72H, TD, SRA1; Report 13: „ABR1 (pta-Fix ist Voraussetzung)" direkt nach MIS1/AIM1. Voraussetzung: R1-Fix + Dedup-Purge (V1/V2).

**(c) Offene Fragen:** n=110 zu klein für belastbare WR-/Kalibrierungsaussagen — Outcome-vs-Confidence-Join LONG/SHORT bei größerem n wiederholen (war der „entscheidende Test", Ergebnis bisher nur Entwarnung auf Richtungsebene); warum wurde der schlechtere 31.12.-Lauf statt Final_Saver (30.12.) deployt?; Channel undokumentiert.

## 6. Belege

- `AUDIT_TODO.md` → P0.12 (✔✔ inkl. Trainer-Beweis), P1.28-Verweis, P2.38 (✔✔ entwarnt)
- `audit_reports/07_ai_bots_b.md` → 11/18-Beweis (~35k Splits traversiert), Stale-Entry, Edge-Pivots, Scheduling, Positiva (Closed-Candle, autocommit, Cooldown)
- `audit_reports/13_x_ml_trainers.md` → Provenienz (byte-identisch, 31.12.2025, nicht Final_Saver), In-Sample-Threshold, CV-F1 0,134, Coin-Split, Threshold-Chaos, SUCCESS_CLASS_IDX-Verifikation
- `audit_reports/14_bot_performance_db.md` → n=110, WR 63,6%, ø +3,15%, Σ +335 netto
- `audit_reports/STEP2_DB_VERIFICATION.md` → P2.38-Entwarnung (LONG 67,2%/SHORT 59,2%)
- `audit_reports/16_strategy_concept_evaluation.md` → Note C−, Rettungspfad (pta-Fix + Retrain mit 18 Features)
- `audit_reports/17_monitor_replay_and_gaps.md` → Monitor-Vorbehalt (63,4%, N4)

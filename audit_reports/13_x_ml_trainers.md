# 13 — ML-Trainer-Audit (`Documents\_X`) — Step 3

**Stand:** 2026-07-03 · **Methode:** 6 parallele Reviews (Trainer-Code in `_X` ↔ konsumierender Live-Bot ↔ deployte Artefakte), mit empirischer Artefakt-Introspektion im Live-venv (Booster-Dumps, Split-Counts, Feature-Namen, md5). Ergänzt Step 1 (`AUDIT_TODO.md`) und Step 2 (`STEP2_DB_VERIFICATION.md`).

## Provenienz-Übersicht (wer baute was)

| Live-Artefakt | Bot | Trainer in `_X` | Provenienz |
|---|---|---|---|
| `bt2_model_LONG/SHORT.json` | 18 ABR1 | `BT2-ML-Trainer.py` (31.12.2025) | ✔ **byte-identisch** zu `_X`-Artefakten (md5); NICHT der bessere `BT2-ML-Final_Saver`-Lauf vom 30.12. |
| `model_tsi_long_robust.pkl` / `short` | 12 ATS1 | `X8-TSI-ML-V4.py` (long) / `V5.py` (short); Daten aus `X8-TSI-EXPORT-V4/V5.py` | ✔ geklärt |
| `long/short_trend_prediction_model.joblib` | 14 ATB1 | `BT1-ML-Trainer_Optimized.py` (+`BT1-Thresholdoptimizing_V2.py`) | ✔ geklärt (`BT1-ML-Trainer.py` ist tot: 20 Features, würde crashen) |
| `long/short_reversion_model.joblib` | 13 RUB1 | `BT3-2-ml_trainer.py` (+`BT3-3-optimizer.py`) | ✔ geklärt |
| `master_trade_model_xgboost_combined_signals.pkl` | 15 AIM1 | `x10-mlzeitfolge-v2.py` (`master_task.py` = nur Loader-Prototyp) | ✔ geklärt |
| `trade_success_xgb_LONG/SHORT_v2.json` | 9 SRA1 | `X9-SR-ANALYZER-Schritt1.py` (v1) + `core/update_model.py` (Konvertierung) | ✔ **bewiesen: v2 = reine Formatkonvertierung von v1** (Booster-Vergleich: alle 100 Bäume bit-identisch, 38 Features, LONG+SHORT) |
| `pump_dump_model.pkl` | 10 EPD1 | `zzz.py` (`train_pump_dump_model`, ~Z.7054-7242) | ~ Trainer existiert, ist aber **auskommentiert** (Z.7033/7040) — Artefakt aus unbekanntem Lauf |
| `pump_model_{8,24,72,168}h_{pump,dump}_final.pkl` + `threshold_*` | 11 MIS1 | `X5-analyze_indicators_v8.py` (**nachträglich gefunden**, s. Addendum) | ✔ geklärt — f-String-Dateiname täuschte alle Literal-Suchen |

## Gesamtverdikte

| Familie | Verkabelung Bot↔Artefakt | Statistische Vertrauenswürdigkeit |
|---|---|---|
| ABR1 | ✔ sauber (Feature-Ordnung, Klassenindex 0 korrekt) | ✘ **nicht vertrauenswürdig** — 11/18 Features konstant 0 (bewiesen), null Out-of-Sample-Evaluation |
| ATS1 | ✔ Features identisch (29) | ~ als grobes Ranking nutzbar, nicht als Wahrscheinlichkeit; Short-Modell unvalidiert (`_1h_X`-Tabelle) |
| ATB1 | ✔ formal (19 Features) | ✘ **nicht vertrauenswürdig** — Modell scored eine Event-Population, die es nie sah |
| RUB1 | ✔ formal (9 Features) | ✘ **nicht vertrauenswürdig** — MACD-Semantikbruch + memorisierter Split |
| AIM1 | ✔ formal, aber reindex maskiert totes Vokabular | ✘✘ **aktiv schädlich** (Step 2: invers; Ursachen jetzt code-belegt) |
| SRA1 | ✔ sauber (38 Features exakt) | ~ funktional; Label-Semantik unbelegt, 1h-Look-ahead im Training |
| EPD1 | ✔ Feature-Positionen exakt | ✘ Modell wird live fast nur out-of-distribution befragt |
| MIS1 | ✔ technisch konsistent (67 Features ×8 identisch — **P1.18 widerlegt**) | ✘ Ticker-Leakage über Unfall-Features; keine Provenienz |

## Wiederkehrende Muster (die eigentlichen Root-Causes, analog Abschnitt R der AUDIT_TODO)

- **X-R1 — Label ≠ Live-Geometrie (7/8 Familien).** Trainer labeln Touch-/Close-Ziele ohne SL-Pfad (ABR1: Close nach 12h ab Level-Preis; ATB1/RUB1: +10%-Touch/72h ohne SL; ATS1: 2.5/1.5-Bracket; AIM1: +10%/−7.5%), der Bot handelt aber `calculate_smart_targets`/SR-SL/DCA-Entry2. Die "Confidence" schätzt eine nie gehandelte Größe. **Fix:** ein gemeinsamer First-Touch-Simulator der geposteten Order-Geometrie als Label-Quelle (== P0.10-Fix).
- **X-R2 — Threshold auf verbrauchten Daten (5/8).** ABR1 komplett in-sample (P0), ATB1/RUB1/ATS1 auf dem Test-Set maximiert, AIM1 Testset=Early-Stopping-Set. Alle Live-Thresholds sind Maximum-Statistik-Artefakte. **Fix:** zeitlicher 3-Wege-Split + Threshold nur auf Validation.
- **X-R3 — Split-Leakage über Quasi-Duplikate (6/8).** Persistierende Zustände/überlappende Fenster erzeugen Zwillings-Samples; Random-/unsortierte Splits verteilen sie auf Train+Test (RUB1 am schlimmsten, ABR1 CV auf coin- statt zeitsortiert, EPD1 10s-Tick-Duplikate). **Fix:** Episoden-Dedup + zeitlicher Split mit Embargo.
- **X-R4 — Unkalibrierte Scores als "Confidence %" (alle).** `scale_pos_weight`/Sample-Weights ohne Nachkalibrierung; nirgends Isotonic/Platt. **Fix:** Kalibrierung auf Out-of-Time-Slice, sonst Confidence-Anzeige entfernen.
- **X-R5 — Silent-Default-Antipattern.** Fehlende Spalten → NaN → `fillna(0)` (ABR1-Bug 3 Stufen unsichtbar; ATS1-Export; MIS1-line_cols). **Fix:** Startup-Assertion "kein Feature konstant" + raise statt Default.
- **X-R6 — Serving auf Forming Candle / OOD (Step-1-R1 trifft alle ML-Bots).**

## Familien-Findings (kondensiert; Schweregrad | Datei:Zeile)

### ABR1 (BT2) — Verdikt: nicht vertrauenswürdig, aber reproduzierbar
- **P0** `BT2-Datagrepper-for-ML.py:77-92`: identischer `expected_pta_cols`-Bug wie Bot → **Split-Count-Beweis in den Live-Modellen: exakt die 11 vorhergesagten Features haben 0 Splits** (dist_close_kama9, tsi×4, boll×3, donchian×3). Modell fährt real auf 7 Features.
- **P0** `BT2-ML-Trainer.py:110-162`: Threshold+Win-Rate vollständig **in-sample** auf dem refitteten GridSearch-Modell gewählt; kein Hold-Out im ganzen Skript. Ehrlichste Zahl der Pipeline: CV-F1(success)=**0.134** (Final_Saver-meta) ≈ Rauschen.
- **P1** `BT2-ML-Trainer.py:70,101`: TimeSeriesSplit auf **coin-konkatenierten, nicht zeitsortierten** Daten → CV schneidet Coins, nicht Zeit.
- **P1** Label: Close-only nach 12h ab `lvl_price` (`:208-213,265-270`), Live-Entry ist aber Retest-Close → optimistisch. Bot nutzt zudem unbestätigte Edge-Pivots (`18:145-149,242`), die im Training nie vorkamen.
- **P1** Threshold-Chaos: Live 0.60/0.80 stammen aus "Backtests" **auf den Trainingsdaten** (`BT2-Strategybacktester*.py:13-22`); Trainer-Optimum 0.77/0.92, Final_Saver-meta 0.79/0.86.
- **OK:** `SUCCESS_CLASS_IDX=0` dreifach verifiziert (LabelEncoder alphabetisch, meta.json, num_class=3). Bot sollte Index+Threshold trotzdem aus meta.json laden statt hardcoden (P2).

### ATS1 (X8-TSI) — Verdikt: eingeschränkt; Short unvalidiert
- **P0** `X8-TSI-EXPORT-V4.py:83,203` vs `12:154-155,199-202`: `obv_val`/`obv_ratio` Train/Serve-Skew — Training kumuliert OBV über ~300 Tage, live 500-Kerzen-Fenster mit Normalisierung, die `obv_ratio` mathematisch verändert → High-Confidence-Region live out-of-distribution. **Erklärt die gemessene Kalibrierungs-Inversion** (0.6-0.7→71% vs 0.8-0.9→57%).
- **P0** Label-Geometrie 2.5%/1.5%/96h ≠ Live (SR-Targets ≥5%, DCA-Entry2, SR-SL).
- **P1** `EXPORT-V4:272-275`: TP-vor-SL bei ambiguen Kerzen → optimistischer Bias genau in High-Vol-Samples.
- **P1** `X8-TSI-EXPORT-V5.py:32`: Short-Modell trainiert auf **`{coin}_1h_X`**-Tabellen (andere Quelle als Live!).
- **P1** `X8-TSI-ML-V4.py:59,72`: `scale_pos_weight` ohne Kalibrierung.
- **P2** Threshold-PF-Maximierung auf dem Test-Set (`ML-V4:91-110`); Trainingsdaten enden 2025-12-15 (6,5 Monate stale). Positiv: zeitlicher Split korrekt, Featureliste 29/29 identisch.

### ATB1 (BT1) — Verdikt: nicht vertrauenswürdig
- **P0** Event-Mismatch: Trainer labelt Kreuzungen der **90d-Close-Regressionsgeraden** (`BT1-Datagrepper-for-ml.py:204-232`), Live handelt **Pivot-Chart-Trendlinien** (find_peaks, R≥0.2) mit 4-Event-State-Machine inkl. Bounce-Events ohne Trainings-Gegenstück (`14:120-145,597-607`). Anderes mathematisches Objekt; `slope_trend` aus anderer Regression.
- **P0** `vol_ratio`-Skew hergeleitet: live ≈ 1/19 der Trainingsskala (3-min-Forming-Candle ./. rolling-20 inkl. Forming) — deckt die Audit-Beobachtung ~1/20; Modell hat für den Live-Wertebereich keine Trainingsdaten.
- **P1** `BT1-ML-Trainer_Optimized.py:46`: random `train_test_split` über 72h-überlappende Fenster; **P1** `BT1-Thresholdoptimizing_V2.py:48,96-103`: Live-Thresholds 0.80/0.75 auf dem (rekonstruierten) Test-Set maximiert.
- **P1** Label +10%-Touch/72h **ohne SL** vs Live-SL bis −8.8%.
- **P2** `make_scorer(roc_auc_score)` auf harten Labels (GridSearch optimiert das Falsche); Survivorship über heutige coins.json; Live-`fillna(0)` erzeugt nie gesehene Werte.

### RUB1 (BT3) — Verdikt: nicht vertrauenswürdig
- **P0** MACD-Semantikbruch: Training `ta.macd(fast=9,slow=21)` (`BT3-1:85-87`), Live füttert `macd_dif_normal_12_26_9`-DB-Spalten unter demselben Feature-Namen (`13:92-93,150-151`) — für die Namensvalidierung unsichtbar.
- **P0** `BT3-2:34`: Random-Split über stundenweise duplizierte Persistenz-Episoden (Reversion-Zustand hält viele Stunden an) → Test-AUC = Memorization. Live handelt via 4h-Cooldown nur die *erste* Episodenstunde — Training mittelt über alle.
- **P1** `BT3-3-optimizer.py:31`: Thresholds 0.75/0.85 per Precision-Maximierung auf Mini-Test-Set (>5 Trades!).
- **P1** Label ohne SL/Drawdown (Knife-Catch unmodelliert); **P1** Live-Prediction auf Forming-Candle-Indikatoren (LIMIT 1) + Regression inkl. aktueller Kerze (95d vs 2160 Kerzen exkl.).
- **P1** DB-Indikator-Parität (Wilder-RSI? TSI-Skalierung?) unverifiziert — betrifft auch die Vorfilter-Gates (rsi<30, tsi<−15). Hinweis: Step 2 hat bereits bewiesen, dass DB-`rsi_14` ≠ Wilder (Δ≈4.8) — das Gate feuert live also in einer anderen Population als im Training.

### AIM1 (x10-mlzeitfolge-v2) — Verdikt: aktiv schädlich; Ursachen der Inversion code-belegt
- **P0** `v2:170-191` + `15:347`: Identity-Vokabular tot (MSI1-Schreibweise, `Fast Bot` etc. aus damaligen DB-Werten; heutige Namen existieren im pkl nicht); `reindex(fill_value=0)` verwirft lautlos. Identity-Block = **14,6% des Gesamt-Gains**, `conv_bot_nan` drittwichtigstes Feature; Live-Kombination "conv_bot_nan=1 ∧ alle ai_model_*=0" existierte im Training nie. Der Fix-Kommentar in `15:121-128` ist wirkungslos.
- **P0/P1** `v2:398`: Feature-Join per `dt.round('1h')` — **rundet AUF** → Join-Kerzen-Close (Basis aller dist-/ATR-Features) liegt bis ~90 min in der Zukunft des Signals; Live nutzt floor → gelernte Richtungen kippen.
- **P1** Label +10%/72h vor −7.5%-SL belohnt Volatilität; pkl-Beweis: Top-Gains `atr_21_pct_close` (137) + `atr_14_pct_close` (97) → **Modell ist ein Volatilitäts-Detektor**; live reißen genau die volatilsten Kandidaten den echten SL zuerst → **echte Inversion** (Step-2-Messung: conf>0.9 → 9,3% WR).
- **P1** Keine Kalibrierung, `scale_pos_weight=2.105`, Testset=Early-Stopping-Set (`v2:544,605-612`).
- **P2** Live-Selbst-Feedback: Hist-Query `15:176-188` ohne `model_name`-Filter liest AIM1s eigene Shadow-Rows als Signale; **P2** Duplikat-Sperre ohne Zeitfenster (`15:363-366`); **P2** drei inkonsistente Confidence-Mappings (v2/master_task/15).
- **Ausgeschlossen:** Label-Inversion (1=Win verifiziert) und falscher predict_proba-Index (classes_=[0,1], Bot nimmt [0][1]).
- **Wichtig:** Neutraining auf aktuellem Vokabular allein reicht NICHT — ohne Label-Fix (X-R1) und floor-Join entsteht wieder ein überkonfidentes Volatilitätsmodell.

### SRA1 (X9) — Verdikt: funktional, mit offener Label-Frage
- **Provenienz geklärt (bewiesen):** v2.json = Formatkonvertierung von v1 via `core/update_model.py`; alle 100 Bäume bit-identisch. Konvertierung/Training trotzdem nicht versioniert (P1) → 3-Zeilen-Skript + meta.json einchecken.
- **P1** `9:108-114`: bedingte ATR-Features → 35/38 Spalten → predict wirft, Batch-Rollback verwirft alle Shadow-Inserts, Crash wiederholt sich alle 300s (deckt P1.20).
- **P2 (klären!)** `Schritt1:157`: `success = status in ['SL1','SL2','SL3','4']` — vermutlich "Trailing-SL nach TPn = Win"; falls `SL1` in `closed_trades3` "SL vor TP1" bedeutet, ist das Label invertiert. Gegen DB-Semantik verifizieren.
- **P2** 1h-Look-ahead im Trainings-Join (`Schritt1:56-61`, open_time-keyed Kerze enthält Zukunft bis +1h); **P2** Median-Imputation über Gesamtdatensatz vs live rohe NaN; **P2** Alt-Trainer `X9-SR-ANALYZER.py:244-246` random Split (deprecated markieren).
- Positiv: Schritt1-Split chronologisch korrekt; Feature-Parität 38/38 exakt (JSON-verifiziert).

### EPD1 (zzz.py) — Verdikt: Modell wird live falsch befragt
- **P0** Covariate-Shift: Trainer sampelt NUR `volume_ratio ≥ 5`-Events (`zzz.py:7103-7104`), Live scored **jeden 10s-Tick ohne Gate** (`10:519-565`) → fast alle Live-Queries out-of-distribution. Erklärt (mit B-4/B-6) die flache Kalibrierung; die 72,8% WR stammen plausibel aus der SR-basierten SL/TP-Konstruktion, nicht aus Modell-Skill. **Fix (1 Zeile):** Spike-Gate vor predict spiegeln.
- **P1** `zzz.py:7033,7040-7041`: tägliches Training **auskommentiert, Log meldet trotzdem Erfolg** — Artefakt stale/unbekanntes Regime.
- **P1** `zzz.py:7178`: random Split über 10s-Quasi-Duplikate.
- **P2** Sample-Weights (Pump/Dump bis 3.0) ohne Nachkalibrierung + `max(prob_pump,prob_dump)` als "Confidence" geloggt; **P2** Shadow-Flut ohne Cooldown (`10:586-593`, ~360 Pseudo-Replikate/h/Symbol) — zweite Ursache für corr≈0 in Step-2-Messungen; **P2** `float(None)`-Crash bei SQL-NULL (`10:537`) killt den ganzen 10s-Zyklus.
- Positiv: Feature-Positionen 10/10 exakt, Klassen-Mapping via `classes.index()` korrekt.

### MIS1 (kein Trainer) — Verdikt: technisch konsistent, statistisch nicht vertrauenswürdig, ohne Provenienz
- **P1.18 WIDERLEGT:** alle 8 Modelle identische 67 `feature_names_in_` (Reihenfolge inkl.); Bot-Selektion per Name; Live-Parity-Test auf 3 Symbolen fehlerfrei. `classes_=[0,1]`, Bot-Index korrekt. Thresholds alle plausibel, pro Modell verschieden, atomar mit Modellen gespeichert (26./27.01.2026).
- **P1** Unfall-Features: `pct_distance(close, X)` lief auch über abgeleitete Spalten (`boll_*_dist_atr`, `ema_200_dist_atr`, `ema_9_cross_above_21`) → Werte in Coin-Preisskala (BTC −3.47e6 vs XRP −167). Bäume splitten real darauf (168h_dump: 558 Splits, Schwellen bis ±5.9e5; 168h_pump: Top-Feature 10,4% Importance) → **Ticker-/Preisklassen-Leakage**.
- **P2** 1000 Bäume ohne Early Stopping, identische Hyperparameter für alle 8 Horizonte; tote Binär-Flags (`rsi_14_above_50` in allen 8 Importance 0); Forming-Candle-Prediction (`11:196`) real wirksam (Indikator-Zeile der laufenden Kerze existiert, keine NaN-Maskierung).
- **P3** 168h_pump-Threshold 0.2825 nur 3 Punkte über dem 0.25-Shadow-Floor (Shadow-Band leer); Fallback-Threshold 0.60 ≠ Init-Default 0.5.
- **Einzige Familie ohne jede Reproduzierbarkeit** → dringendste Neutraining-Kandidatin; Minimalanforderungen siehe unten.

## Priorisierte Maßnahmen

**Sofort (ohne Retrain möglich):**
1. AIM1 pausieren (Step-2-Beweis: invers prädiktiv) — bis Neutraining nach X-R1/floor-Join-Fix.
2. EPD1: `vol_ratio ≥ 5`-Gate vor predict (1 Zeile) + Shadow-Cooldown + NULL-Guard (`10:537`).
3. SRA1: ATR-Keys immer emittieren + reindex-Guard (killt den 300s-Crash-Loop).
4. ATS1/ATB1/RUB1/ABR1/MIS1: Confidence nicht mehr als "%" kommunizieren; Operating-Points konservativ anhand der Step-2-Kalibrierungstabellen setzen (z.B. ATS1 auf den empirisch besten 0.6-0.7-Bucket).
5. SRA1-Label-Semantik (`SL1/SL2/SL3/4`) gegen `closed_trades3`-Statuscodes klären — Inversionsrisiko.

**Neutraining-Programm (ein gemeinsames Gerüst statt 8 Ad-hoc-Trainer):**
- Ein versionierter Trainer je Familie im Repo, der den **Feature-Builder des Bots importiert** (eine Quelle statt Copy-Paste) — verhindert die Klasse RUB1-MACD/ABR1-pta/MIS1-line_cols strukturell.
- Label = First-Touch-Simulation der tatsächlich geposteten Order-Geometrie (X-R1, == AUDIT_TODO P0.10).
- Nur geschlossene Kerzen (R1-Fix zuerst!), Join auf letzte geschlossene Kerze (floor−1) in Trainer UND Serving.
- Zeitlicher 3-Wege-Split mit Embargo + Episoden-Dedup; Threshold auf Validation; Isotonic-Kalibrierung out-of-time.
- Artefakte nativ (save_model JSON) + meta.json (Feature-Liste, class_mapping, Threshold, Trainingszeitraum, Daten-Hash, Git-SHA); Bots laden Threshold/Klassenindex aus meta statt hardcoden.
- Startup-Assertion in jedem Bot: kein Feature konstant, keine per reindex verworfenen Nicht-Null-Spalten.

**Reihenfolge-Empfehlung:** MIS1 (keine Provenienz + Ticker-Leakage) und AIM1 (aktiv schädlich) zuerst, dann ABR1 (pta-Fix ist Voraussetzung), dann ATB1/RUB1 (Event-/Feature-Parität), dann ATS1 (OBV-Fix), EPD1 (Gate-Fix reicht ggf. vorerst), SRA1 zuletzt (funktional am gesündesten).

---

## Addendum (2026-07-03, später): MIS1-Trainer GEFUNDEN — `X5-analyze_indicators_v8.py`

Nachscan von `D:\_BACKUP` + gesamtem User-Profil auf Anfrage: Der Trainer lag die ganze Zeit in `_X`,
speichert aber mit f-String (`f"pump_model_{name}_final.pkl"`, Z.254-255) — alle Literal-Greps liefen ins Leere.
**Verifikation:** Hyperparameter exakt identisch mit der pkl-Introspektion (1000/4/0.02/spw1.5/gamma2/lambda10),
Feature-Bau erzeugt exakt die 67 Features **inklusive der F1-Unfall-Features**: die `line_cols`-Schleife (Z.69)
läuft nach dem Anlegen von `boll_*_dist_atr`/`ema_200_dist_atr`/`ema_9_cross_above_21` und produziert deren
`_dist_pct`-Versionen — der Ticker-Leakage-Befund ist damit an der Quelle bestätigt.

**Jetzt bekannte Label-Definitionen:** Close-to-Close-Return über den Horizont, Schwellen ±5%/8h, ±10%/24h,
±15%/72h, ±25%/168h (Z.153-161). Kein Pfad/SL — reine Zukunftsrendite (X-R1 gilt auch hier).

**Trainer-Defekte (Kurz-Audit):**
- **P0:** `StratifiedKFold(shuffle=True)` (Z.194) über stündliche Samples mit 8-168h **überlappenden**
  Label-Fenstern → Zwillings-Leakage; die berichteten Precision-Werte sind stark inflationiert.
- **P1:** Threshold = beste Precision **über die 5 Folds gemaxt** (Z.240-243) → Maximum-Statistik;
  zusätzlich Recall-Floor nur 3%.
- **P1:** Final-Modell wird auf ALLEN Daten gefittet (Z.252), der Threshold stammt aber aus den
  Shuffle-Folds → Operating-Point passt nicht zum deployten Modell.
- **P2:** keine Kalibrierung (scale_pos_weight=1.5), `fillna(0)`-Kaskade, Training inkl. Forming-Candle-Rows,
  400-Tage-Fenster mit heutiger coins.json (Survivorship).

**Konsequenz:** MIS1 ist reproduzierbar (Retraining-Grundlage vorhanden — in `legacy_trainers/` gesichert),
und die Verdikte bleiben: Die starke Live-Performance von MIS1-72H entsteht TROTZ dieser Methodik
(vermutlich weil die Momentum-/Vol-Features auf langen Horizonten echte Signale tragen), nicht wegen ihr.
Neutraining nach dem Gerüst oben (zeitlicher Split, Pfad-Label via First-Touch-Simulator, line_cols-Fix,
Kalibrierung) bleibt Priorität #1 der Modell-Sanierung.

---

## Addendum 2 (2026-07-04): In-Repo-Modelle QM/TD/BB per Artefakt-Introspektion verifiziert — Provenienz-Restliste

Die Provenienz-Tabelle oben deckte nur die `Documents\_X`-Familien ab. Die **In-Repo-Modelle** (Trainer `qm_ml_trainer.py`/`smc_ml_trainer.py` im Repo selbst) wurden jetzt per Pickle-Introspektion (Fleet-Python, xgboost 3.1.2) nachgeprüft:

| Artefakt | Befund | Provenienz |
|---|---|---|
| `qm_xgboost_model_1h/4h.pkl` | dict {model, features, optimal_threshold}; **20 Features exakt = qm_ml_trainer-Schema** (rsi_14, tsi_25_13_13, macd_*_normal_12_26_9, *_dist_pct, trend-Dummies, dir_num); gespeichert mit xgb 3.1.2 (= installierte Version, kein Versions-Skew) | ✔ geklärt |
| `td_xgboost_model_1h/4h.pkl` · `bb_xgboost_model_1h/4h.pkl` | identisches Schema (20 Features, smc_ml_trainer-Reihenfolge), xgb 3.1.2 | ✔ geklärt |
| `qm_xgboost_model_v2.pkl` | **Orphan** — kein Bot lädt es (`24:35` lädt nur `_1h/_4h`); stored threshold 0.1 | aufräumen/archivieren |
| `pump_dump_model.pkl` (EPD1) | nackter XGBClassifier, 10 Features ohne Namen, keine Metadata — **Provenienz aber via Backups auf D:\ geschlossen (s.u.)** | ✔ geklärt (Addendum 3) |

**Threshold-Drift (neu, konkret):** Die pkls speichern `optimal_threshold`, die Bots weichen ab: QM-Bot hardcodet **0.65** vs. gespeichert **0.30** (laut FIX-Kommentar `24:37` bewusst, aber undokumentiert gegenüber dem Artefakt); Sniper BB hardcodet **0.40** vs. gespeichert **0.35** (`25:42`); TD 0.30 = 0.30 ✔. Empfehlung: Thresholds aus dem pkl laden und Abweichungen als expliziten Override mit Begründung führen.

**Verbleibende Provenienz-Restliste (vollständig):**
1. **Metadata-Lücke systemisch:** Kein einziges Artefakt (auch die geklärten) trägt meta.json/Trainingsdatum/Datenfenster/Git-Hash — Provenienz ist überall nur *rekonstruiert*, nicht *deklariert*. Fix = X-R5/P3.4: `{model, features, thresholds, xgb_version, trained_at, data_window, trainer_git_hash}` als Pflichtformat beim nächsten Retrain.
2. **Bekannte Rest-Punkte aus der Haupttabelle:** ABR1 deployed den 31.12.-Lauf statt des besseren `BT2-ML-Final_Saver`-Laufs vom 30.12. (bewusst?); ATS1-Short auf `_1h_X`-Tabellen trainiert (Datenquelle unvalidiert); SRA1-Label-Semantik (`SL1/SL2/SL3/4`) weiterhin unbelegt.

---

## Addendum 3 (2026-07-04): EPD1-Provenienz via D:\-Backups GESCHLOSSEN

Rekonstruktion über die Backup-Serie `D:\_BACKUP\` (Zips 2025-11-07 … 2026-04-01) + `Documents\_X\zzz-sicherung.py`:

1. **Trainingszeitpunkt bewiesen:** `pump_dump_model.pkl` trägt in **allen** Backups ab 2026-03-06 denselben Zeitstempel **2026-01-22 22:22** und ist **md5-identisch** zum deployten Repo-Artefakt (`6c09741a…`) — das Live-Modell ist der Lauf vom 22.01.2026, seitdem unverändert.
2. **Trainer-Lineage bewiesen:** `zzz.py` vom 12.12.2025 (Backup) enthält **noch kein** Pump-Dump-Training; `Documents\_X\zzz-sicherung.py` vom **22.12.2025** enthält `train_pump_dump_model()` mit **aktiven** Aufrufen (Z. 4785/4792) und `joblib.dump("pump_dump_model.pkl")` (Z. 5218); in `zzz.py` vom 26.02.2026 (heutiger Stand) sind die Aufrufe auskommentiert. Timeline: Feature eingebaut ~22.12.2025 → lief bis mind. 22.01.2026 → vor dem 26.02.2026 deaktiviert.
3. **Funktionsidentität bewiesen:** Diff der Trainingsfunktion (zzz-sicherung 5037-5218 vs. zzz.py 7054-7242) = **8 Zeilen, alle am Funktionsende** (Logging/except/finally) — der auskommentierte Code in der heutigen zzz.py IST der Code, der das deployte Modell gebaut hat.
4. **Report-13-Kernbefund in der gelaufenen Version bestätigt:** Das Trainings-Sampling-Gate `if volume_ratio < 5.0: continue` (zzz-sicherung Z. 50) und die 10-Feature-Liste (beginnend `volume_ratio, price_change_60s, buy_pressure, volatility, …`, Z. 104-116/172) stehen exakt so in der Version, die am 22.01. lief — der Covariate-Shift (live wird jeder 10s-Tick ohne Gate gescored) ist damit endgültig belegt, ebenso der Random-Split (`train_test_split(random_state=42, stratify=y)`, Z. 125).

**Konsequenz:** Alle 9 Modell-Familien haben jetzt geklärte Provenienz. Für EPD1 heißt das konkret: Das Modell ist 5,5 Monate stale (trainiert 22.01. auf Daten davor), das tägliche Retraining wurde bewusst abgeschaltet (Log meldet weiterhin Erfolg — P1 bleibt), und der Gate-Fix (`vol_ratio ≥ 5` vor `predict`, Report 16 Empfehlung) hat nun eine bewiesene Code-Grundlage.

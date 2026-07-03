# Dossier: TD / BB — SMC-ML-Sniper

> Ein Bot, zwei ML-Familien: Three-Drive (TD = RSI-Divergenz an drei Extrema) und Breaker Block (BB = Break-and-Retest mit ML-Gate, *nicht* Bollinger). **Note (16): TD B− · BB_4H C− · BB_1H D.** Kernverdikt: TD ist die einzige gut kalibrierte ML-Familie der Flotte und klarer Behalten-Kandidat trotz formal wertlosem Training; BB_4H rechtfertigt die Pipeline-Reparatur, BB_1H überlebt Fees + Rauschen nicht → parken.

## 1. Steckbrief

| | |
|---|---|
| Bot | `25_smc_ml_sniper.py` |
| Modelle | `td_xgboost_model_1h.pkl` / `td_xgboost_model_4h.pkl` + `bb_xgboost_model_1h.pkl` / `bb_xgboost_model_4h.pkl` |
| Trainer | `smc_ml_trainer.py` — **im Repo vorhanden** |
| Signale/TF | TD_1H, TD_4H, BB_1H, BB_4H |
| Channel | eigener Cornix-Channel; Plain-Cornix-Block + zweite HTML-Message mit identischem Block (P3.9 Doppel-Parse-Risiko); `send_cornix_signal` committet nie (funktioniert nur via Upstream-Autocommit) |
| Leverage | in den Quellen nicht beziffert; kein R4-Befund gegen 25 |
| Schwellen | hardcoded 0.30 (TD) / 0.40 (BB) für beide TFs — ignorieren den im pkl gespeicherten `optimal_threshold` |

**BR-Zuordnung (geprüft):** Die Break-&-Retest-Tags **BR1H/BR2H/BR4H (+BR1D in den Step-2-Zahlen) stammen NICHT aus Bot 25**, sondern aus dem **Pattern-Detector `7_pattern_detector.py`** — Break-and-Retest *ohne* ML-Gate (Tag-Klärung in Report 16, Abschnitt 6, „im Code verifiziert"). Die BR-Familie wird daher im Dossier `IP_Pattern.md` behandelt. Der Vergleich BB_4H (+ML, +565) vs. BR-Familie (ohne ML, −4.106) ist laut Report 16 „das beste In-vivo-Argument im Repo, dass ein ML-Gate über Break-and-Retest-Rohsignalen Wert stiftet".

## 2. Live-Bilanz (aktive Ära 24.02.–03.07., dedupliziert, Report 14)

| Tag | n | WR | ø PnL | Median | Σ netto |
|---|---|---|---|---|---|
| TD_1H + TD_4H | 2.794 | 57,3% | ~+1,0% | ≈0 | **+2.387** (Aufteilung lt. Auswertung: TD_1H +1.764 / TD_4H +623) |
| BB_4H | 2.162 | 61,2% | +0,36% | −0,05 | **+565** |
| BB_1H | 3.909 | 55,7% | −0,18% | −0,17 | **−1.089** |

- **Kalibrierung (Step 2):** TD_1H (n=2.202, WR 57,2%) **positiv kalibriert — 78,5% WR @ conf>0.9**, das am besten kalibrierte Modell der Flotte. **BB: flach** bis negativ → die BB-Wahrscheinlichkeiten sind Rauschen (passt zum Breakout-vs-Retest-Feature-Skew).
- BB-/BR-Familie war Mär–Apr stark negativ, ab Mai positiv (Mini-n, Regime-Gating filtert sie inzwischen fast weg) — Regime-Drift sichtbar.
- Vorbehalt (Report 17): monitor-generiert, nur 63,4% Replay-Übereinstimmung (P1.2/P2.7).

## 3. Befunde

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| P0.10 | Trainer↔Bot | P0 | TD-Labels auf Look-ahead-Entry (Pivot-Close, erst 10 Bars später bekannt); Live feuert 11–12 Bars nach p3 zu CMP via `calculate_smart_targets` — Labels messen einen physisch unmöglichen Trade | ✔ (Code; Muster Step 3 bestätigt) |
| 11-CRIT | Trainer↔Bot | CRITICAL | SL/TP-Geometrie im Training (BB fix 1%/2%, TD 2R vom Pivot) ≠ Live-Geometrie (`calculate_smart_targets`: ATR/S&R/Fib-Ladder) → `predict_proba` + Threshold-Sweep für nie exekutierte Outcomes | ✔ (Code) |
| P1.25 | Bot | HIGH | Trainer-Entry Hindsight, Live-Geometrie völlig anders; Schwellen hardcoded statt aus pkl; Trainer `p3−p1 ≤ 100` vs. Live `MAX_TD_SPAN=50` | ✔ (Code) |
| 11-HIGH | Trainer↔Bot | HIGH | BB-Feature-Skew: Features an der *Breakout*-Kerze (RSI ~65–75) trainiert, Inferenz an der *Retest*-Kerze (RSI ~45–55) → Tree-Splits routen Retest-Rows in beliebige Leaves; dazu Populations-Skew (Trainer handelt jeden Peak, Bot filtert) | ✔ (Code; BB-Kalibrierung flach stützt es) |
| P1.29 | Trainer | HIGH | Random Split auf Zeitreihen + Duplikat-Kontamination; Threshold auf dem Test-Set gewählt | ✔ (Code) |
| P2.39 | Bot | MEDIUM | Breaker Block prüft nur `peak_idx[-2]` — bei frischem Retest ist das Post-Breakout-Hoch noch nicht als Peak bestätigt → meist falsches Level; „Massive violation"-Check auskommentiert | ✔ (Code) |
| P1.31 | Trainer | HIGH | Silent-Exception + Pool-Leak → stilles trunkiertes Coin-Universum, Overwrite des Produktions-pkl möglich (`smc_ml_trainer.py:63-90`) | ✘/~ (Step 2: aktuell nicht getriggert; Code-Bug bleibt) |
| 11-MED | Trainer | MEDIUM | Unresolved Trades als Losses gelabelt (outcome=0-Default; QM-Trainer macht es richtig); Retest-/Entry-Kerze vom Outcome-Scan ausgeschlossen (Fill-Kerze kann nicht verlieren); Fees deklariert, nie angewandt (bei BB-1%-SL sind Round-Trip-Fees 8–15% eines R); `bfill()`-Leak | ✔ (Code) |
| P3.7 | Bot | LOW | Per-Coin-Exceptions auf DEBUG → unsichtbar | ✔ (Code) |
| P3.8 | Bot | LOW | matplotlib ohne `Agg` → headless-Crash-Risiko | ✔ (Code) |
| P3.9 | Bot | LOW/[DB] | Cornix-Doppel-Parse-Risiko (Plain + HTML im selben Channel) | ~ (ungeprüft) |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1 (Forming Candle):** 25 behandelt die letzte DB-Zeile als live — Retrain erst nach R1-Fix.
- **X-R1** („backtest the detector, trade something else"): TD/BB sind der Repo-interne Prototyp des Musters, das Step 3 in 7/8 _X-Familien fand.
- **Monitor-Vorbehalt (Report 17):** 63,4% Scoring-Übereinstimmung → Monitor-Rewrite vor Neutraining (liefert die Labels).
- 24+25 zusammen ~2.150 Join-Queries alle 3 min (08, Cross-Cutting); P2.31 (Monitor scored bis 21 Targets).
- **Report 15:** TD_1H ist Kandidat für **S4 „Calibration-Sized Positions"** (Positionsgröße ∝ kalibrierte Prob, TD_1H@>0.9 = 78,5% WR).

## 5. Sanierungsplan

**Sofort:** BB_1H parken (Note D, −1.089). Schwellen aus pkl laden (P1.25). `peak_idx[-2]`-Level-Logik fixen (P2.39). Exception-Logging auf WARNING, `Agg`-Backend, Commit in `send_cornix_signal`, Doppel-Parse prüfen.

**Retrain (Priorität #2 im Retrain-Programm von Report 16, nach MIS1-72H):** TD-Entry bei `p3+PIVOT_WINDOW`-Close labeln, Live-SL/TP-Generator (`calculate_smart_targets`) als Label-Geometrie, chronologischer Split (P0.10/P1.25/P1.29); BB-Features an der Retest-Kerze extrahieren + Bot-Filter im Trainer spiegeln + Fees ins Labeling (BB-Skew); `try/finally`-Fix im Trainer (P1.31). Erwartung laut Report 16: Kalibrierung und Selektionsschärfe von TD steigen plausibel weiter — „kein A, weil ø-Edge klein und tail-getrieben".

**Offene Fragen:** Live-Leverage; Cornix-Doppel-Parse [DB]; BB_4H nach Skew-Fix neu bewerten (C− → ?).

## 6. Belege

- `AUDIT_TODO.md` P0.10, P1.25, P1.29, P1.31, P2.39, P3.7–P3.9
- `audit_reports/08_smc_bots.md` (Abschnitt 25_smc_ml_sniper.py)
- `audit_reports/11_ml_backtest.md` (TD-Look-ahead, SL/TP-Geometrie, BB-Skew, Fees)
- `audit_reports/14_bot_performance_db.md` (Tabelle B; TD/BB-Zeilen)
- `audit_reports/STEP2_DB_VERIFICATION.md` (Abschnitt D: TD_1H 78,5%@>0.9, BB flach)
- `audit_reports/15_strategy_proposals.md` (E4, S4)
- `audit_reports/16_strategy_concept_evaluation.md` (Abschnitt 6 inkl. Tag-Klärung BR→Bot 7)
- `audit_reports/17_monitor_replay_and_gaps.md` (Monitor-Vorbehalt)

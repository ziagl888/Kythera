# Dossier: QM (Quasimodo)

> ML-gefilterte Quasimodo-Reversals (Liquidity-Sweep + Struktur-Bruch, Retest der Sweep-Zone). **Note (16): QM_1H D+ · QM_4H F.** Kernverdikt: 67,5% WR und trotzdem ≈ 0 netto — die Exit-Geometrie (TP1 = halbe Strecke, SL jenseits des Extrems) gibt strukturell alles zurück; QM_4H stoppen, QM_1H nur mit Neutraining + Exit-Redesign, sonst parken.

## 1. Steckbrief

| | |
|---|---|
| Bot | `24_quasimodo_bot.py` |
| Modelle | `qm_xgboost_model_1h.pkl` / `qm_xgboost_model_4h.pkl` (+v2-Varianten; die v2-Artefakte werden in den Quellen nicht separat bewertet) |
| Trainer | `qm_ml_trainer.py` — **im Repo vorhanden** (Provenienz gegeben, anders als bei MIS1) |
| Signale/TF | QM_1H, QM_4H (Signal-Tags in `closed_ai_signals`) |
| Channel | eigener Cornix-Channel; postet Plain-Cornix-Block **und** zweite HTML-Message mit identischem Block in denselben Channel (P3.9 Doppel-Parse-Risiko) |
| Leverage | in den Quellen nicht beziffert; kein R4-Befund gegen 24 |
| Besonderheit | Bot ignoriert den im pkl gespeicherten `optimal_threshold` (hardcoded 0.65); Entry live = CMP ±1% statt Limit@QML wie im Trainer |

## 2. Live-Bilanz (aktive Ära 24.02.–03.07., dedupliziert, Report 14)

| Tag | n | WR | ø PnL | Median | Σ netto |
|---|---|---|---|---|---|
| QM_1H | 3.139 | 67,5% | +0,06% | −0,03 | **−139** |
| QM_4H | 556 | 54,9% | −0,40% | −0,29 | **−277** |

- **Kalibrierung (Step 2):** QM_1H **leicht positiv** — QM gehört mit TD_1H, SRA1, MIS1-8H zu den wenigen echt kalibrierten Modellen (E4 in Report 15).
- Paradebeispiel „Win ≠ Profit": 67,5% TP1-Touch-WR bei netto negativer Summe.
- Vorbehalt (Report 17): alle Zahlen monitor-generiert; Monitor stimmt nur zu 63,4% mit einem First-Touch-Replay überein (P1.2/P2.7) — per-Trade-Wahrheit unzuverlässig.

## 3. Befunde

| ID | Ebene | Schweregrad | Einzeiler | Status |
|---|---|---|---|---|
| P1.24 | Bot | HIGH | Pivot-Detection auf der Forming Candle ohne Confirmation (`argrelextrema mode='clip'` lässt Kanten-Pivots durch) → Repaint + Training-Serving-Skew; Trainer gated korrekt (`p[0] ≤ curr_idx − PIVOT_WINDOW`) | ✔ (Code) |
| P0.10 | Trainer↔Bot | P0 | „Backtest the detector, trade something else": Trainer simuliert Limit-Order am QML, Bot handelt CMP ±1% mit anderer Geometrie → pkl-Wahrscheinlichkeiten gelten für nie ausgeführte Trades | ✔ (Muster Step 3 in 7/8 Familien bestätigt) |
| P1.29 | Trainer | HIGH | Random `train_test_split` auf Zeitreihen + überlappende Duplikate = Kontamination; „optimal threshold" auf dem Test-Set gewählt → optimistischer Operating Point im pkl | ✔ (Code) |
| P1.30 | Trainer | HIGH | Fill-Logik löscht garantierte Verlierer („invalidated" statt Stop-out) + vergibt Same-Candle-TP-Wins → Labels systematisch geschönt | ✔ (Code) |
| P1.31 | Trainer | HIGH | Silent-Exception + Pool-Leak → Trainer kann still auf trunkiertem Coin-Universum laufen und Produktions-pkl überschreiben | ✘/~ (Step 2: 0/529 Coins ohne Tabellen — aktuell nicht getriggert; Code-Bug bleibt) |
| 11-MED | Bot | MEDIUM | Bot ignoriert per-TF-`optimal_threshold` aus dem pkl (fix 0.65) → nach Retrain still divergent | ✔ (Code) |
| 11-MED | Trainer | MEDIUM | Trend-Dummy-Encoding datenabhängig (`pd.get_dummies`) vs. Bot hardcodet 3 Kategorien; fehlende Features still 0-gefüllt, NaN-Trend → alle Dummies 0 | ✔ (Code) |
| 11-MED | Trainer | MEDIUM | `bfill()` leakt zukünftige Indikatorwerte in frühe Historie; `qm_backtest` (ORDER_EXPIRY 100) vs. Trainer (50) simulieren verschiedene Strategien, keiner matcht den Bot | ✔ (Code) |
| P3.7 | Bot | LOW | Per-Coin-Exceptions auf DEBUG geloggt → unsichtbar (systematischer Fehler = Bot scannt „erfolgreich" und postet nichts) | ✔ (Code) |
| P3.8 | Bot | LOW | matplotlib ohne `Agg`-Backend → headless-Crash-Risiko | ✔ (Code) |
| P3.9 | Bot | LOW/[DB] | Cornix-Doppel-Parse-Risiko: Plain-Block + HTML-Message mit identischem Block im selben Channel | ~ (ungeprüft) |

## 4. Abhängigkeiten & Querschnitts-Risiken

- **R1 (Forming Candle):** 24 behandelt die letzte DB-Zeile als fertig — Kern von P1.24; jeder Retrain vor dem R1-Fix trainiert erneut auf Daten, die es live nicht gibt.
- **X-R1** („Label ≠ gehandelte Geometrie", Report 13/16): identisches Muster wie bei den _X-Trainern, hier mit Trainer im Repo (P0.10/P1.25-Klasse).
- **Monitor-Vorbehalt (Report 17):** Whitelist-/Performance-Statistik und künftige Labels hängen am Monitor-Scoring (63,4% Übereinstimmung) → Monitor-Rewrite VOR Neutraining.
- P2.31: Monitor scored bis 21 Targets, publiziert werden TP1–5 → Live-Statistik ≠ Cornix-Realität.

## 5. Sanierungsplan

**Sofort (kein Retrain):** QM_4H stoppen (Note F, −277). QM_1H parken oder eng beobachten. Exception-Logging auf WARNING (P3.7), `Agg`-Backend (P3.8), Doppel-Parse-Check gegen Cornix (P3.9), Threshold aus pkl laden.

**Retrain (nur nach R1-Fix + Monitor-Rewrite + V3-Simulator):** Forming Candle droppen + Pivots mit `index > len−1−PIVOT_WINDOW` verwerfen (P1.24); chronologischer Split mit Purge-Gap, Schwelle auf Validation-Slice (P1.29); fill-then-stop konservativ, kein TP-Win auf der Entry-Kerze (P1.30); Label = First-Touch der bot-eigenen CMP-Geometrie (P0.10); `try/finally conn.close()` + Abbruch bei zu wenig Coins (P1.31). **Exit-Redesign** ist Pflichtteil — die positive Kalibrierung zeigt, dass die Features Signal tragen, aber die TP/SL-Geometrie es zurückgibt.

**Offene Fragen:** Rolle/Trainingsstand der v2-pkls; Cornix-Doppel-Parse (P3.9 [DB]); Live-Leverage.

## 6. Belege

- `AUDIT_TODO.md` P0.10, P1.24, P1.29–P1.31, P3.7–P3.9
- `audit_reports/08_smc_bots.md` (Abschnitt 24_quasimodo_bot.py)
- `audit_reports/11_ml_backtest.md` (QM-Fill-Logik, Split, Threshold, get_dummies)
- `audit_reports/14_bot_performance_db.md` (Tabelle B)
- `audit_reports/STEP2_DB_VERIFICATION.md` (Abschnitt D, Kalibrierung)
- `audit_reports/16_strategy_concept_evaluation.md` (Abschnitt 6, Ranking #13/#23)
- `audit_reports/17_monitor_replay_and_gaps.md` (Monitor-Vorbehalt)

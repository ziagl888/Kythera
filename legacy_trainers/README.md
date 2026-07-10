# legacy_trainers — eingefrorene ML-Trainer aus `Documents\_X`

**Zweck:** Provenienz-Sicherung. Diese Skripte haben die aktuell live geladenen Modell-Artefakte
erzeugt (Audit Step 3, `audit_reports/13_x_ml_trainers.md`). Sie sind **Referenz, kein gepflegter
Code** — die dort dokumentierten Defekte (Label-Geometrie, Split-Leakage, In-Sample-Thresholds,
Feature-Skews) sind ABSICHTLICH unverändert konserviert. Neutrainings folgen dem Gerüst aus
Report 13/15, nicht diesen Skripten.

> **NICHT LÖSCHEN.** Dass kein Prozess diese Dateien importiert und sie ohne Env-Vars nicht laufen,
> macht sie nicht zu totem Code — es ist ihr Zweck. Sie sind die einzige Reproduktionsgrundlage der
> acht Artefakte in der Tabelle unten. `docs/CANDLE_CALL_SITES.md` hat sie einmal als „toter Code,
> löschbar" geführt; das ist dort seit 2026-07-10 korrigiert (Operator-Entscheid, Frage §5.8).
> Sie werden auch bei der TimescaleDB-Migration **nicht umverdrahtet** — nach Phase C laufen sie
> ohnehin nie wieder, und genau das ist in Ordnung.

**Sanitisierung:** Alle Credentials (DB-Passwort, Telegram-Token, Binance-API-Key/Secret,
Channel-IDs) wurden durch `os.getenv(...)`-Reads bzw. Platzhalter ersetzt. Die Skripte sind
syntaktisch valide, aber ohne gesetzte Env-Vars nicht lauffähig — gewollt.

## Zuordnung Trainer → Live-Artefakt → Bot

| Trainer | erzeugt | Konsument |
|---|---|---|
| `BT1-Datagrepper-for-ml.py` → `BT1-ML-Trainer_Optimized.py` (+`BT1-Thresholdoptimizing_V2.py`) | `long/short_trend_prediction_model.joblib` | 14 ATB1 |
| `BT1-ML-Trainer.py`, `BT1-Thresholdoptimizing.py`, `BT1-Backtest-Trendline.py` | (tote/ältere Generation) | — |
| `BT2-Datagrepper-for-ML.py` → `BT2-ML-Trainer.py` | `bt2_model_LONG/SHORT.json` (byte-identisch verifiziert) | 18 ABR1 |
| `BT2-ML-Final_Saver.py` | `models/long_break_retest_xgb_20251230_*.json` (nie deployt, methodisch besser) | — |
| `BT2-Strategybacktester(_v2).py`, `BT2-Backtest-Breakandretest.py` | In-Sample-"Backtests" (Quelle der 0.60/0.80-Thresholds) | — |
| `BT3-1-datagrepperandbacktest.py` → `BT3-2-ml_trainer.py` (+`BT3-3-optimizer.py`) | `long/short_reversion_model.joblib` | 13 RUB1 |
| `X8-TSI-EXPORT-V4/V5.py` → `X8-TSI-ML-V4/V5.py` | `model_tsi_long/short_robust.pkl` | 12 ATS1 |
| `X8-TSI-ML.py`, `-V3.py` | ältere Generationen | — |
| `X9-SR-ANALYZER-Schritt1.py` | `trade_success_xgb_LONG/SHORT_v1.model` → via `core/update_model.py` als `*_v2.json` (bit-identisch verifiziert) | 9 SRA1 |
| `X9-SR-ANALYZER.py` | Kombi-Modell v1 (deprecated, Random-Split) | — |
| `x10-mlzeitfolge-v2.py` | `master_trade_model_xgboost_combined_signals.pkl` | 15 AIM1 |
| `x10-mlzeitfolge.py`, `master_task.py` | Vorgänger / Loader-Prototyp | — |
| `zzz.py` (v1-Monolith; Trainer: `train_pump_dump_model`, ~Z.7050-7240) | `pump_dump_model.pkl` | 10 EPD1 |
| **`X5-analyze_indicators_v8.py`** (ältere Generationen: `X5-*.py`) | `pump_model_{8,24,72,168}h_{pump,dump}_final.pkl` + `threshold_*_final.pkl` | **11 MIS1** |

**MIS1-Provenienz nachträglich GEFUNDEN** (Nachscan der Backups/Platte auf User-Anfrage):
Der Trainer speichert mit f-String-Dateinamen (`f"pump_model_{name}_final.pkl"`), weshalb alle
Literal-Suchen ihn verfehlten. Verifikation: Hyperparameter (n_estimators=1000, max_depth=4,
lr=0.02, scale_pos_weight=1.5, gamma=2.0, reg_lambda=10) und Feature-Bau (inkl. der pathologischen
`*_dist_atr_dist_pct`-Unfall-Features) decken sich exakt mit der pkl-Introspektion aus Report 13.
Label-Definitionen: Close-to-Close-Return ≥ ±5%/8h, ±10%/24h, ±15%/72h, ±25%/168h.
Trainer-Defekte (Addendum in Report 13): StratifiedKFold **mit shuffle** über massiv überlappende
Horizont-Fenster (Leakage), Threshold = beste Precision über die Folds (Selektions-Bias),
Final-Fit auf ALLEN Daten, keine Kalibrierung.

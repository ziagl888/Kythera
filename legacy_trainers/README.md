# legacy_trainers — frozen ML trainers from `Documents\_X`

**Purpose:** Provenance preservation. These scripts produced the model artefacts currently loaded
live (Audit Step 3, `audit_reports/13_x_ml_trainers.md`). They are **reference, not maintained
code** — the defects documented there (label geometry, split leakage, in-sample thresholds,
feature skews) are DELIBERATELY preserved unchanged. Retrains follow the scaffold from
Report 13/15, not these scripts.

> **DO NOT DELETE.** The fact that no process imports these files and they don't run without env
> vars set doesn't make them dead code — that's their purpose. They are the only reproduction
> basis for the eight artefacts in the table below. `docs/CANDLE_CALL_SITES.md` once listed them
> as "dead code, deletable"; that has been corrected there since 2026-07-10 (operator decision,
> question §5.8). They are also **not rewired** during the TimescaleDB migration — after Phase C
> they never run again anyway, and that's fine.

**Sanitisation:** All credentials (DB password, Telegram token, Binance API key/secret,
channel IDs) were replaced with `os.getenv(...)` reads or placeholders. The scripts are
syntactically valid but not runnable without env vars set — intentional.

## Mapping trainer → live artefact → bot

| Trainer | produces | consumer |
|---|---|---|
| `BT1-Datagrepper-for-ml.py` → `BT1-ML-Trainer_Optimized.py` (+`BT1-Thresholdoptimizing_V2.py`) | `long/short_trend_prediction_model.joblib` | 14 ATB1 |
| `BT1-ML-Trainer.py`, `BT1-Thresholdoptimizing.py`, `BT1-Backtest-Trendline.py` | (dead/older generation) | — |
| `BT2-Datagrepper-for-ML.py` → `BT2-ML-Trainer.py` | `bt2_model_LONG/SHORT.json` (byte-identical verified) | 18 ABR1 |
| `BT2-ML-Final_Saver.py` | `models/long_break_retest_xgb_20251230_*.json` (never deployed, methodologically better) | — |
| `BT2-Strategybacktester(_v2).py`, `BT2-Backtest-Breakandretest.py` | In-sample "backtests" (source of the 0.60/0.80 thresholds) | — |
| `BT3-1-datagrepperandbacktest.py` → `BT3-2-ml_trainer.py` (+`BT3-3-optimizer.py`) | `long/short_reversion_model.joblib` | 13 RUB1 |
| `X8-TSI-EXPORT-V4/V5.py` → `X8-TSI-ML-V4/V5.py` | `model_tsi_long/short_robust.pkl` | 12 ATS1 |
| `X8-TSI-ML.py`, `-V3.py` | older generations | — |
| `X9-SR-ANALYZER-Schritt1.py` | `trade_success_xgb_LONG/SHORT_v1.model` → via `core/update_model.py` as `*_v2.json` (bit-identical verified) | 9 SRA1 |
| `X9-SR-ANALYZER.py` | Combined model v1 (deprecated, random split) | — |
| `x10-mlzeitfolge-v2.py` | `master_trade_model_xgboost_combined_signals.pkl` | 15 AIM1 |
| `x10-mlzeitfolge.py`, `master_task.py` | Predecessor / loader prototype | — |
| `zzz.py` (v1 monolith; trainer: `train_pump_dump_model`, ~line 7050-7240) | `pump_dump_model.pkl` | 10 EPD1 |
| **`X5-analyze_indicators_v8.py`** (older generations: `X5-*.py`) | `pump_model_{8,24,72,168}h_{pump,dump}_final.pkl` + `threshold_*_final.pkl` | **11 MIS1** |

**MIS1 provenance FOUND retroactively** (rescan of backups/disk on user request):
The trainer saves with f-string filenames (`f"pump_model_{name}_final.pkl"`), which is why all
literal searches missed it. Verification: hyperparameters (n_estimators=1000, max_depth=4,
lr=0.02, scale_pos_weight=1.5, gamma=2.0, reg_lambda=10) and feature construction (including the
pathological `*_dist_atr_dist_pct` accident features) match the pkl introspection from Report 13
exactly.
Label definitions: close-to-close return ≥ ±5%/8h, ±10%/24h, ±15%/72h, ±25%/168h.
Trainer defects (addendum in Report 13): StratifiedKFold **with shuffle** across massively
overlapping horizon windows (leakage), threshold = best precision across the folds (selection
bias), final fit on ALL data, no calibration.

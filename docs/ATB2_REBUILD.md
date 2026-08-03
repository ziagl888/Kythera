# ATB2 — Converging-Channel Breakout: Rebuild (T-2026-CU-9050-104)

Rebuild from scratch of the parked trendline bot (Bot 14, ATB1) per
**`docs/MODEL_INTENT.md` §11** (Michi, 2026-07-07). ATB1 is dead: audit note D,
live Σ −172 net at 65.7% "WR", core verdict Report 16 "the model never saw the
event it scored" (the trainer labelled crossings of the 90d close regression line,
live it traded pivot trendlines). Dossier: `audit_reports/dossiers/ATB1.md`.

## 1. What ATB2 does differently (X-R findings addressed)

| Old (ATB1, dead) | New (ATB2) | fixes |
|---|---|---|
| Event = crossing of the 90d close regression line | **Converging channel** (wedge/triangle/pennant) from confirmed swing pivots, closed breakout | X-R1 event mismatch |
| Label = +10% touch/72h **without SL** | First-touch **TP1-before-SL** of the measured-move geometry via `simulate_exit`, fees included | X-R1/X-R5 |
| Random `train_test_split` over 72h overlapping windows | Chronological 3-way split + 3d purge embargo | X-R3 twin leakage |
| Threshold maximised on the test set | Threshold via `pick_threshold_safe` **on validation** (None = not deployable) | X-R2 |
| Uncalibrated "confidence %" | Isotonic calibration out-of-time | Report 16 |
| No meta.json, silent feature death | Artifact meta (`model_id=ATB2`, feature list, threshold) + `assert_features_alive` | X-R6 |

The 5-factor WillyAlgoTrader score (penetration depth/ATR, body ratio,
body commitment, volume spike, RSI momentum) does **not** enter as a
hand-weighted score, but as 5 XGB setup features alongside the channel
geometry — analogous to `18_ai_abr1_bot.GEOMETRY_FEATURES`.

## 2. Code (in this PR, built + tested DB-free)

- **`core/atb2_features.py`** — shared source for bot + simulator + trainer
  (X-R1 rule). Confirmed pivots (no-repaint), channel fit (§11 criteria:
  ≥3 touches per edge, convergence ≥2%, width 0.5…120×ATR,
  volume contraction <85%), closed breakout, `ATB2_FEATURES` contract,
  measured-move targets, `assert_features_alive`. Indicators (ATR/RSI/EMA)
  deterministic from OHLCV — no `pandas_ta` version drift (P0.12).
- **`tools/walkforward_sim.py`** — adapter `run_atb2` (`--strategy atb2`).
  Label geometry = measured move; additionally smart targets of the same
  candle as a comparison (`smart_*` fields, §11 measured move VS. smart
  targets).
- **`tools/retrain_from_replay.py`** — runner `run_atb` (`--strategy atb2`):
  per direction, chronological split + 3d purge, isotonic, threshold on
  validation, artifact + `_meta.json` to `staging_models/` with `model_id=ATB2`.
- **`backtest/test_atb2_features.py`** — 9 DB-free tests (detection both
  directions, no-repaint, feature contract, no-channel-on-trend,
  alive assertion, measured-move geometry, end-to-end adapter).

## 3. VPS run book (phase B — NOT on the build machine)

DB-bound → only in a VPS session, trainer in `Documents\_X`. **Sequential
jobs rule:** strictly queued BEHIND the running T-061 retrain queue
(`_X\t061_full_rerun_runner.ps1`) — exactly one train/sim job at a time.
Live tables read-only, CPU-throttled (the simulator sets BELOW_NORMAL and
checks CPU headroom itself).

```powershell
# 1) Labeling: Walk-Forward-Replay über coins.json (365d), schreibt
#    staging_models/replay/atb2_replay_365d.jsonl
py -3.13 tools\walkforward_sim.py --strategy atb2 --days 365 --resume

# 2) Training: je Richtung, Artefakt + Meta nach staging_models/
py -3.13 tools\retrain_from_replay.py --strategy atb2
#    -> staging_models\atb2_model_LONG.pkl  + _meta.json
#    -> staging_models\atb2_model_SHORT.pkl + _meta.json
#    -> staging_models\retrain_atb2_stats.json
```

Context advantages now (labelling on the current DB): indicator history
P1.13-cleaned (T-061), RSI single-domain Wilder (T-097).

## 4. Deploy verdict (what "deployable" means)

Per direction, deployable only if **`optimal_threshold` ≠ None** (i.e.
`pick_threshold_safe` found a validation probability with avg net PnL > 0
at ≥ min_n trades) **and** the out-of-time test stats (`test_stats`)
confirm the validation verdict (positive Σ net PnL, plausible
calibration). A `threshold=None` is a **valid "do not deploy"** —
like RUB2-LONG/EPD2. Compare measured move vs. smart targets via the
`smart_*` fields in the replay before committing to a geometry.

## 5. Follow-up (gated, C-gate Michi)

Only AFTER a deployable ATB2 verdict:

1. **Bot serving rewire** (`14_ai_atb_bot.py`): replace the old single
   trendline detector (`detect_trend`/`classify_trendline_event`/`get_ml_prediction`)
   with `core.atb2_features.find_channel_breakout`; load the model via
   `core.model_artifacts.load_artifact` (idle mode, `expected_features=ATB2_FEATURES`);
   post the measured-move geometry; **P1.45**: tag from `contract["tag"]`
   (`meta.model_id`) instead of hardcoded `MODEL_ID='ATB1'` (pattern: `18_ai_abr1_bot.py:520`).
   Without an artifact the bot then runs cleanly in idle mode.
   **Parity contract:** the serving path MUST load ≥ `atb2_features.MIN_HISTORY_CANDLES`
   (1500) closed 1h candles per coin before the decision candle, or
   EMA200-dependent features (dist_ema200) will drift against the replay (X-R1).
2. **Unpark** (remove `control/parked/14_ai_atb_bot.py`) — operator decision.

Until then ATB1 stays parked (no live effect, no artifact in the live path).

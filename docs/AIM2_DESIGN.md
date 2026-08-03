# AIM2 — Rebuild of the master meta-model (replaces AIM1)

**As of:** 2026-07-05 · **Decision:** AIM1 is retired (audit: reliably inverted,
grade F, dossier `audit_reports/dossiers/AIM1.md`). AIM2 takes over bot slot 15, channel
(`CH_MASTER`) and posting flow unchanged. Blueprint = Report 15 S7 on the Batch-E scaffolding.

## 1. Why not retrain AIM1

The inversion (conf>0.9 → 9.3% WR) has four proven root causes in the trainer, not the bot:
volatility label (X-R1), `round('1h')` lookahead join, dead identity vocabulary
(overlap 2/22 resp. 0/5), no calibration. Batch E confirmed: retraining on the same
pipeline reproduces the inverted volatility model. → rebuild.

## 2. Role

**Ranker/gate over source signals**, not an independent alpha generator. AIM2 answers, per
source signal, exactly the bot's decision: *"Would an AIM1-style trade (smart-targets
geometry at signal time) have hit TP1 before SL?"* Keep expectations realistic
(Batch-E core thesis: no gate has so far shown robust out-of-time expectancy): benefit =
selection/prioritization, failure = a clean proof that the slot should stay closed for good.

## 3. Training events

| Source | Period | Volume | Sampling |
|---|---|---|---|
| `ml_predictions_master` posted=true, model≠AIM1 | 25.02.–today | ~39.6k | 100% |
| `active/closed_trades_master` (conv) | 25.02.–today | ~206k | FIFO 25%, Volume Indicator 35%, rest 100% (deterministic via md5 hash) |

Timezone contract (Step-2 R3, re-measured here): **all** writers of
`ml_predictions_master`/`*_trades_master` stamp `time` in PG local time
(Europe/Bucharest, incl. DST switch 29.03.) — converted to UTC via `tz_localize`.
`regime_history.ts` is naive UTC. Candles are `timestamptz`.

## 4. Label (X-R1 fix)

Per event: `entry` = close of the last **closed** 1h candle before the event;
geometry = `calculate_smart_targets(df=win1h)` with a window up to this candle (as-of, no
lookahead); replay = `simulate_exit` from `tools/walkforward_sim.py` (wick-aware first touch,
SL-first on ambiguity, fees, monitor trailing) over `targets[:3]`, horizon cap 14 days.
`outcome_tp1` = classification label; `net_pnl_pct` (Cornix ladder approximation) = basis
for the threshold choice. `open_at_end` → excluded from training.

## 5. Features (shared builder `core/aim2_features.py`)

Trainer and bot import the **same** builder (MIS1 pattern from e84bc7d):

- **Market** (row of the last closed 1h candle, floor−1 join): dist-% to ema_9/21/50/200,
  kama_21, wma_21, boll_20 (3), donchian_20 (3), support/resistance/trendline_price;
  rsi_6/14, tsi, macd dif/dea (12/26/9), trendline_slope, r_squared; atr_14/atr_21 as %-close;
  trend_direction one-hots.
- **Regime** (`regime_history` as-of, the predictor missing in 2025): regime + alt_context one-hots,
  confidence/_btc/_alt, btc_return_1h/4h, btc_atr_1h/4h_pct, btcdom_return_24h, staleness (min).
- **Swarm** (5d window per coin, **without AIM1/AIM2 and without the event itself** — F6 fix):
  total/long/short, direction prob, age of the last signal, confluence same-dir 4h,
  distinct sources same-dir 4h.
- **Source:** one-hot from **DB vocabulary at training time** (not hardcoded; the list travels into
  the artifact), source_conf (AI: model confidence; conv: mapping like bot 15), trailing WR 30d from
  `closed_ai_signals` (win := status~TARGET or targets_hit≥1; identical semantics in trainer
  and serving), n-basis, entry_drift_pct (close vs. source entry), direction_num.
- **Deliberately left out:** absolute prices/scales (ticker leakage), AIM1 history, raw volume.

## 6. Training (X-R2/R4 fix)

Chronological 70/15/15 split with a 7-day purge gap (P1.29). XGBoost binary (hist).
Early stopping on val. **Isotonic calibration on val. Threshold choice via replay net PnL
on val** (not a formula, not test). Test stays untouched until the final report.
Report: AUC/Brier, reliability buckets (calibrated vs. replay outcome), gate uplift
(PnL/trade with vs. without gate on test), per-source breakdown. Artifact **only to
`staging_models`** (P1.35): model, features, threshold, calibrator, vocab, meta.

## 7. Serving (bot 15 → AIM2)

- Artifact `master_meta_model_aim2.pkl`; deploy = deliberate copy from staging (operator).
- Feature build exclusively via `core/aim2_features.py`; `reindex` on the artifact feature list
  with a **parity guard** (warning if the non-null share falls below threshold → OOD suspicion = P0.13 watch).
- Swarm/history query excludes `model_name IN ('AIM1','AIM2')` (F6 fix).
- Posting flow, channel, Cornix format unchanged; `ai_signals.model='AIM2'` (clean attribution,
  AIM1 statistics stay closed off). MIN_CONFIDENCE comes from the artifact (val operating point).
- Model reload: 1×/day instead of never (R07-AIM1-b).

## 8. Rollout gates

1. Out-of-time test shows gate uplift > 0 after fees, reliability monotonic → otherwise stop, slot stays closed.
2. **4–8 weeks shadow** (`ml_predictions_master`, posted=false) — shadow-WR-CI vs. break-even.
3. Only then unpark bot 15 with posting. Abort criterion defined up front: shadow-WR-CI below
   break-even → back into the park.

## 9. Artifacts & ownership

New: `core/aim2_features.py`, `tools/aim2_build_dataset.py`, `tools/aim2_train.py`, this plan.
Rework: `15_ai_master_bot.py`. No overlap with the parallel ABR1 rework
(`18_ai_abr1_bot.py`, `tools/walkforward_sim.py`, `tools/retrain_from_replay.py` — import only).

# Report 20 — AIM2 Training: Results & Robustness Check

**Date:** 2026-07-05 · **Context:** Operator decision to retire AIM1 and replace it with AIM2
(docs/AIM2_DESIGN.md, Report 15 S7). Pipeline: `tools/aim2_build_dataset.py` →
`tools/aim2_train.py`, artifact in `staging_models/master_meta_model_aim2.pkl` (+ `_report.json`).

## 1. Dataset

115,018 events (2026-02-25 → 07-05), of which 109,570 labelled (5,448 `open_at_end` excluded).
Sources: 43k posted AI signals + 198k conv (FIFO 25% / Volume 35% deterministically undersampled,
weights in training). Label = first-touch TP1-before-SL of the as-of reconstructed smart-targets
geometry (`simulate_exit`, fees, SL-first conservative, 14d cap). Baseline: **WR 54.1%,
ø replay PnL −0.61%/trade** — the unfiltered signal stream loses after fees (covers Report 14).

TZ re-measurement: ALL writers of `ml_predictions_master`/`*_trades_master` stamp
PG local time (Europe/Bucharest); `regime_history.ts` = naive UTC; candles = timestamptz.
Conversion in the builder; the old AIM1 bot compared local against UTC (≈3h offset, R07-AIM1-a).

## 2. Main run (chrono 70/15/15, 7d purge; test = 01.06.–05.07.)

| Metric | Value |
|---|---|
| AUC val / test | 0.656 / 0.686 |
| Brier test (calibrated) | 0.224 |
| Calibration | **monotonic**: bucket 0.0–0.1 → 7.6% WR … 0.9–1.0 → 89.6% WR (AIM1 inversion eliminated) |
| Operating point (val replay PnL) | thr = 0.61 |
| Gate uplift test | without gate **−0.69%**/trade → with gate **+1.92%**/trade, WR 70.5%, pass rate 34.2% (n=5,628/16,436) |
| Monthly (gated) | Jun +1.80% (n=5,105, WR 68.9%) · Jul +3.48% (n=523, WR 80.7%) |

Top features: ema_200_dist, direction_num, ALT context, support/resistance distance,
entry_drift, regime CHOP, source identity/trailing WR, swarm. **ATR not in the top 25**
— the AIM1 failure mode (volatility detector) is not reproduced.

## 3. Robustness checks (all passed)

1. **Dumb baselines fail out-of-time** — the uplift is NOT concealed source selection:
   source filter (positive train sources) → **−0.94%**/trade; source+direction → **−0.71%**;
   both ≤ "no gate" (−0.69%). Confirms the Batch-E thesis once more that static gates don't
   generalise — AIM2s added value is context-dependent selection WITHIN the sources.
2. **Second OOT fold** (test = 18.04.–01.06.): AUC 0.61, uplift −0.55% → **+0.17%**/trade
   (thr 0.63, pass 20.4%). Thinner, but positive; monthly Apr +0.07 / May +1.54.
   **No test month Apr–Jul negative.** Honest expectation: sign robust, magnitude varies.
3. **Label lookahead probe** (signal-hour candle skipped, 60-symbol sample, 13,888
   shared events): 0.7% label flips, symmetric (53 W→L vs 45 L→W), WR 0.532→0.531.
   The replay convention doesn't distort anything.
4. **Cluster check:** 14,832 of 16,436 test events are distinct (coin, hour, direction)
   decisions — barely any correlation inflation.

## 4. Remaining caveats

- **Fill assumption:** replay fills instantly at entry1 (limit reality can be worse) —
  the same limitation as all Batch-E replays; that's exactly what the shadow phase is for.
- Test window overall Feb–Jul 2026, one market regime cycle; the `open_at_end` exclusion
  disadvantages slow trades at the data edge.
- Conv trailing WR is missing (only AI sources have `closed_ai_signals` history).

## 5. Recommendation (rollout gates from the design doc)

Gate 1 (OOT uplift > 0 after fees) is **passed**. Next step: copy the artifact from staging
into the repo root and unpark bot 15 **in shadow mode** (posts nothing — only writes
`ml_predictions_master` rows with model_name='AIM2'). After 4–8 weeks: shadow WR CI against
break-even → only then `AIM2_LIVE_POSTING=1`. Abort criterion: shadow WR CI below break-even.

# MPS2 — Upper-Band SHORT Backtest, House Geometry (T-2026-KYT-9050-078)

Generated: 2026-08-03T05:01:19.132420+00:00 · status: complete
Universe: 527/527 symbols · trades: 5,652
Window: 2026-06-12 20:40:00+00:00 → 2026-08-03 00:30:00+00:00 (split 2026-07-08 10:35:00+00:00)

## VERDICT: **NO-DEPLOY**

Gate (pre-registered): val AND test avg net PnL > 0 at n ≥ 100 per half.

| half | n | avg net | t | TP1-WR (closed) | avg R | open@cap | skipped (no targets) |
|---|---|---|---|---|---|---|---|
| val | 3385 | 0.1763% | 1.3 | 77.2% | 0.017 | 30 | 0 |
| test | 2267 | -0.0513% | -0.26 | 77.4% | 0.007 | 59 | 0 |

## Wiring

* Events: gate-study semantics (prior-bar upper band, ≥ 0.3 % above prior close, touch by high), no re-entry while the prior geometry exit is open.
* Geometry: `get_hvn_and_sr_levels` (as-of trailing 30d 15m frame) → `hvn_sr_trade_geometry`(SHORT) → `ensure_min_tp_distance`(5%) → `simulate_exit` first-touch ladder, 3 TPs, taker fee 0.05%/side, SL-first on same-bar ambiguity, scan cap 1152 bars (4d).

## Honest limits

* SAME window as the MPS1 gate study → in-sample deployability check, NOT fresh evidence (T-007 lesson). Real out-of-sample = future oi_5m weeks.
* Heatmap leverage mix is an assumption (see engine header); survivorship via coins.json.
* The 5 % min-TP house rule sits far above the measured 4h drift (~0.16 %) — a NO-DEPLOY here falsifies the COMBINATION drift × house geometry, not the drift itself.

# Wave-Exit Phase 1 — High-Fidelity Sim Validation (AIM2)

_generated 2026-07-23 12:58:29.904421+00:00 · read-only · window 2026-07-07 14:20:00 → 2026-07-23 00:00:00_

**Backbone:** complete wick-aware **5m** OHLC candles (`candles`, 12× finer than the 1h live monitor) for touch detection; **10s** ticks (`ticker_10s`) only as an order resolver for SL-vs-TP sequence within a 5m candle. **Geometry:** immutable Cornix text (`telegram_outbox`), original SL/entry2/TP1-3. **Outcome ground truth:** `closed_ai_signals`.

> Why not pure 10s: `ticker_10s` is a ~40s snapshot with gaps (coverage median 0.25) and misses ~81% of SL touch events → a pure tick sim escapes the stops and skews realized ~2.7×. The 5m candle is gap-free and wick-aware.

Closed in the window: 1285 · geometry matched & scored: **673** · unmatched (outbox retention): 604.
Scored-trades span: 2026-07-10 18:48:31.207150 → 2026-07-22 22:15:37.406260 (outbox retention skews the set toward **more recent** trades — keep this in mind when reading the aggregates).

## Validation — `monitor` config (entry1-only, internal targets) vs recorded closed_ai_signals

- targets_hit **exact**: 97.92%  ·  **±1**: 99.26%
- Win/Loss (TP1 touch) **agreement**: 99.26%

> Residual divergence comes from the finer resolution (5m wick + true intra-candle ordering) compared to the 1h monitor — the sim is deliberately *more faithful* here than the recorded-outcome source.

## Realized aggregate per config

| config | n | unlev mean% | unlev sum% | net sum% | leveraged sum% (n) | WR(TP1)% | avg duration med/mean h |
|---|--:|--:|--:|--:|--:|--:|--:|
| monitor | 673 | 0.4111 | 276.64 | 209.44 | 13796.0 (673) | 64.49 | 21.83/31.68 |
| dca10 | 673 | 0.0808 | 54.36 | 4.61 | 5822.5 (673) | 64.49 | 21.83/31.72 |
| cornix3 | 673 | 0.2512 | 169.03 | 119.18 | 8106.4 (673) | 64.64 | 20.67/30.51 |

**Reading aid:** `monitor` = 1:1 reproduction of the bot monitor (validation anchor). `cornix3` = what Cornix actually trades (DCA entry1/entry2, 3 published TPs in thirds) — the headline realized figure and the basis for the Phase-2 overlay.

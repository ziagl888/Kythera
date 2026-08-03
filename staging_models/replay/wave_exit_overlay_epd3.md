# Wave-Exit Phase 1 — high-fidelity sim validation (EPD3)

_generated 2026-07-23 16:47:57.206498+00:00 · read-only · window 2026-07-07 14:20:00 → 2026-07-23 00:00:00_

**Backbone:** full wick-aware **5m** OHLC candles (`candles`, 12× finer than the 1h live monitor) for touch detection; **10s** ticks (`ticker_10s`) only as an order resolver for SL-vs-TP order within a 5m candle. **Geometry:** immutable Cornix text (`telegram_outbox`), original SL/entry2/TP1-3. **Outcome ground truth:** `closed_ai_signals`.

> Why not pure 10s: `ticker_10s` is a ~40s snapshot with gaps (coverage median 0.25) and misses ~81% of SL touch events → a pure tick sim escapes the stops and distorts realized ~2.7×. The 5m candle is gap-free and wick-aware.

Closed in window: 6254 · geometry matched & scored: **604** · unmatched (outbox retention): 5573.
Scored-trades span: 2026-07-15 00:09:45.587461 → 2026-07-22 23:57:47.390157 (outbox retention skews the set toward **younger** trades — keep this in mind when reading the aggregates).

## Validation — `monitor` config (entry1-only, internal targets) vs recorded closed_ai_signals

- targets_hit **exact**: 93.87%  ·  **±1**: 99.5%
- Win/loss (TP1 touch) **agreement**: 98.51%

> The residual divergence comes from the finer resolution (5m wick + real intra-candle ordering) versus the 1h monitor — the sim is deliberately *more faithful* here than the recorded-outcome source.

## Realized aggregate per config

| config | n | unlev mean% | unlev sum% | net sum% | leveraged sum% (n) | WR(TP1)% | Ø duration med/mean h |
|---|--:|--:|--:|--:|--:|--:|--:|
| monitor | 604 | 0.0557 | 33.65 | -26.75 | 5364.6 (604) | 79.47 | 6.29/12.62 |
| dca10 | 604 | 0.0031 | 1.9 | -36.35 | 2610.4 (604) | 79.47 | 6.29/12.62 |
| cornix3 | 604 | 0.092 | 55.58 | 17.33 | 3329.1 (604) | 79.8 | 6.0/11.12 |

**Reading aid:** `monitor` = 1:1 reproduction of the bot monitor (validation anchor). `cornix3` = what Cornix actually trades (DCA entry1/entry2, 3 published TPs in thirds) — the headline realized number and the basis for the phase 2 overlay.


---

## Phase 2 — auto-close overlays (on `cornix3`, real-money DCA/3-TP)

n_arts = 604 (leveraged, scored). Metric = REALIZED (locked-in) — unlev sum% / leveraged sum%; MaxDD = peak-to-trough of the aggregated open-positions wave (leveraged account units). **Baseline = hold-to-TP/SL.**

### KEY FINDING

- **Leveraged realized: no overlay variant beats hold.** Baseline +3329.1% vs (a) 776.4…850.4% / (c) 764.1…896.1% — worse across the ENTIRE sweep, robustly. The leveraged sum is dominated by a few fat-tail wave hits (−100% clamp asymmetry) that every overlay caps.
- **Unlevered realized:** Baseline 55.58% vs (a) -5.76…-1.45% / (c) 18.82…27.25% — overlays not better.
- **Drawdown: (c) is a risk tool.** MaxDD wave 12.2 (hold) → 4.2…4.2 (~3× smaller).
- **Conclusion:** wave intuition captures **no** leveraged edge out-of-sample; (c) converts upside variance into drawdown protection. **NO-EDGE on the headline metric.**

> ⚠ **WR(TP1)% is misleading under overlays** (the rule closes on MTM retrace, not on TP touch → tp1=False even though closed profitably). Realized is the metric, not WR. Overlay (a) triggers at ~95% (peak retrace fires even on small waves — an activation threshold would only trail large waves, but that isn't necessary here: the sign is already clear).

### Overlay (a) — per-trade trailing TP (close at X% retrace from the trade MTM peak)

| X% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD wave | triggered% |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 604 | 55.58 | 3329.1 | 79.8 | 12.2 | 0.0 |
| 10 | 604 | -1.54 | 843.7 | 16.2 | — | 85.6 |
| 15 | 604 | -4.2 | 792.3 | 17.2 | — | 84.8 |
| 20 | 604 | -5.76 | 788.8 | 18.0 | — | 84.3 |
| 25 | 604 | -2.92 | 847.7 | 18.4 | — | 83.4 |
| 30 | 604 | -1.45 | 850.4 | 19.5 | — | 82.3 |
| 40 | 604 | -5.57 | 776.4 | 21.2 | — | 81.3 |

### Overlay (c) — portfolio circuit breaker (close-ALL at Y% retrace of the aggregate wave)

| Y% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD wave | flattened |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 604 | 55.58 | 3329.1 | 79.8 | 12.2 | 0 |
| 10 | 604 | 26.92 | 888.0 | 14.7 | 4.2 | 548 |
| 15 | 604 | 27.25 | 896.1 | 14.7 | 4.2 | 548 |
| 20 | 604 | 24.91 | 866.4 | 14.6 | 4.2 | 548 |
| 25 | 604 | 18.82 | 764.1 | 14.6 | 4.2 | 548 |
| 30 | 604 | 20.18 | 776.0 | 14.7 | 4.2 | 544 |
| 40 | 604 | 22.34 | 816.6 | 15.7 | 4.2 | 541 |

### Long/short separated (unlev sum% / lev sum%)

| Rule | LONG | SHORT |
|---|--:|--:|
| Baseline | — | 55.58/3329.1 |
| (a) X=10% | — | -1.54/843.7 |
| (a) X=15% | — | -4.2/792.3 |
| (a) X=20% | — | -5.76/788.8 |
| (a) X=25% | — | -2.92/847.7 |
| (a) X=30% | — | -1.45/850.4 |
| (a) X=40% | — | -5.57/776.4 |
| (c) Y=10% | — | 26.92/888.0 |
| (c) Y=15% | — | 27.25/896.1 |
| (c) Y=20% | — | 24.91/866.4 |
| (c) Y=25% | — | 18.82/764.1 |
| (c) Y=30% | — | 20.18/776.0 |
| (c) Y=40% | — | 22.34/816.6 |

**Honest limit:** 7d/674 legs, younger window (outbox bias). Wave capture is market timing; what is tested is whether a MECHANICAL rule catches the wave out-of-sample or is only visible in hindsight. What's evaluated are robust **bands + sign** across the sweep, not a single best point. NO-EDGE is a valid result.

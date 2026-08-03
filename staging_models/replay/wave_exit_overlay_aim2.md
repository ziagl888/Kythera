# Wave-Exit Phase 1 — High-Fidelity Sim Validation (AIM2)

_generated 2026-07-23 16:38:14.087937+00:00 · read-only · window 2026-07-07 14:20:00 → 2026-07-23 00:00:00_

**Backbone:** full wick-aware **5m** OHLC candles (`candles`, 12× finer than the 1h live monitor) for touch detection; **10s** ticks (`ticker_10s`) only as an order resolver for SL-vs-TP ordering within a 5m candle. **Geometry:** immutable Cornix text (`telegram_outbox`), original SL/entry2/TP1-3. **Outcome ground truth:** `closed_ai_signals`.

> Why not pure 10s: `ticker_10s` is a ~40s snapshot with gaps (coverage median 0.25) and misses ~81% of SL touch events → a pure tick sim escapes the stops and distorts realized ~2.7×. The 5m candle is gap-free and wick-aware.

Closed in the window: 1299 · geometry matched & scored: **683** · unmatched (outbox retention): 608.
Scored trades span: 2026-07-10 18:48:31.207150 → 2026-07-22 22:15:37.406260 (outbox retention skews the set towards **younger** trades — keep this in mind when reading the aggregates).

## Validation — `monitor` config (entry1-only, internal targets) vs recorded closed_ai_signals

- targets_hit **exact**: 97.95%  ·  **±1**: 99.27%
- Win/loss (TP1 touch) **agreement**: 99.27%

> Residual divergence comes from the finer resolution (5m wick + real intra-candle ordering) versus the 1h monitor — the sim is deliberately *more faithful* here than the recorded-outcome source.

## Realized aggregate per config

| config | n | unlev mean% | unlev sum% | net sum% | leveraged sum% (n) | WR(TP1)% | Ø duration med/mean h |
|---|--:|--:|--:|--:|--:|--:|--:|
| monitor | 683 | 0.4223 | 288.42 | 220.22 | 14078.4 (683) | 64.57 | 22.33/32.18 |
| dca10 | 683 | 0.0816 | 55.73 | 5.33 | 5873.4 (683) | 64.57 | 22.33/32.22 |
| cornix3 | 683 | 0.2533 | 173.01 | 122.51 | 8209.6 (683) | 64.71 | 20.83/31.03 |

**Reading aid:** `monitor` = 1:1 reproduction of the bot monitor (validation anchor). `cornix3` = what Cornix actually trades (DCA entry1/entry2, 3 published TPs in thirds) — the headline realized number and the basis for the phase-2 overlay.


---

## Phase 2 — Auto-close overlays (on `cornix3`, real-money DCA/3-TP)

n_arts = 683 (leveraged, scored). Metric = REALIZED (locked-in) — unlev sum% / leveraged sum%; MaxDD = peak-to-trough of the aggregated open-positions wave (leveraged account units). **Baseline = hold-to-TP/SL.**

### CORE FINDING

- **Leveraged realized: no overlay variant beats hold.** Baseline +8209.6% vs (a) 4565.1…5163.1% / (c) 4164.2…4720.7% — robust across the ENTIRE sweep, worse. The leveraged sum is dominated by a few fat-tail wave hits (−100% clamp asymmetry) that every overlay caps.
- **Unlevered realized:** baseline 173.01% vs (a) 244.45…284.15% / (c) 245.24…275.68% — overlays mostly BETTER (cut underwater tails).
- **Drawdown: (c) is a risk tool.** MaxDD wave 43.2 (hold) → 5.0…6.4 (~9× smaller).
- **Verdict:** wave intuition captures **no** leveraged edge out-of-sample; (c) converts upside variance into drawdown protection. **NO-EDGE on the headline metric.**

> ⚠ **WR(TP1)% is misleading under overlays** (the rule closes on MTM retrace, not on TP touch → tp1=False even though closed profitably). Realized is the metric, not WR. Overlay (a) triggers at ~95% (peak retrace fires even on small waves — an activation threshold would only trail large waves, but isn't needed here: the sign is already clear).

### Overlay (a) — Per-trade trailing TP (close at X% retrace from trade MTM peak)

| X% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD wave | triggered% |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 683 | 173.01 | 8209.6 | 64.7 | 43.2 | 0.0 |
| 10 | 683 | 249.12 | 4632.5 | 7.0 | — | 95.0 |
| 15 | 683 | 249.45 | 4664.7 | 7.9 | — | 94.3 |
| 20 | 683 | 244.45 | 4565.1 | 8.1 | — | 93.6 |
| 25 | 683 | 260.62 | 4855.2 | 8.8 | — | 92.7 |
| 30 | 683 | 284.15 | 5163.1 | 10.0 | — | 91.4 |
| 40 | 683 | 273.83 | 4923.1 | 12.2 | — | 89.9 |

### Overlay (c) — Portfolio circuit breaker (close ALL at Y% retrace of the aggregate wave)

| Y% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD wave | flattened |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 683 | 173.01 | 8209.6 | 64.7 | 43.2 | 0 |
| 10 | 683 | 252.99 | 4542.4 | 6.7 | 5.6 | 667 |
| 15 | 683 | 268.19 | 4620.8 | 7.2 | 5.0 | 667 |
| 20 | 683 | 274.56 | 4720.7 | 7.8 | 5.0 | 665 |
| 25 | 683 | 275.68 | 4675.4 | 8.1 | 6.4 | 665 |
| 30 | 683 | 274.52 | 4660.6 | 8.6 | 6.4 | 663 |
| 40 | 683 | 245.24 | 4164.2 | 10.2 | 6.0 | 660 |

### Long/short separated (unlev sum% / lev sum%)

| Rule | LONG | SHORT |
|---|--:|--:|
| Baseline | 132.99/4325.6 | 40.02/3884.0 |
| (a) X=10% | 89.29/1704.1 | 159.84/2928.5 |
| (a) X=15% | 86.33/1655.9 | 163.12/3008.8 |
| (a) X=20% | 85.39/1609.4 | 159.06/2955.7 |
| (a) X=25% | 94.11/1750.1 | 166.52/3105.1 |
| (a) X=30% | 115.28/1979.4 | 168.87/3183.7 |
| (a) X=40% | 109.79/1882.0 | 164.04/3041.1 |
| (c) Y=10% | 80.33/1471.8 | 172.66/3070.6 |
| (c) Y=15% | 94.96/1598.0 | 173.23/3022.9 |
| (c) Y=20% | 96.24/1606.4 | 178.32/3114.3 |
| (c) Y=25% | 94.57/1503.2 | 181.1/3172.2 |
| (c) Y=30% | 97.94/1545.3 | 176.58/3115.3 |
| (c) Y=40% | 82.43/1310.6 | 162.81/2853.6 |

**Honest limitation:** 7d/674 legs, younger window (outbox bias). Wave capture is market timing; what's being tested is whether a MECHANICAL rule catches the wave out-of-sample or is only visible in hindsight. What's assessed is robust **bands + sign** across the sweep, not a best point. NO-EDGE is a valid result.

# Wave Exit Phase 1 — High-Fidelity Sim Validation (SRA2)

_generated 2026-07-23 16:49:24.694603+00:00 · read-only · window 2026-07-07 14:20:00 → 2026-07-23 00:00:00_

**Backbone:** full wick-aware **5m** OHLC candles (`candles`, 12x finer than the 1h live monitor) for touch detection; **10s** ticks (`ticker_10s`) only as order resolver for SL-vs-TP sequencing within a 5m candle. **Geometry:** immutable Cornix text (`telegram_outbox`), original SL/entry2/TP1-3. **Outcome ground truth:** `closed_ai_signals`.

> Why not pure 10s: `ticker_10s` is a ~40s snapshot with gaps (coverage median 0.25) and misses ~81% of SL touch events → a pure tick sim escapes the stops and distorts realized ~2.7x. The 5m candle is gap-free and wick-aware.

Closed within the window: 552 · geometry matched & scored: **29** · unmatched (outbox retention): 515.
Scored-trades span: 2026-07-16 20:54:18.284426 → 2026-07-22 23:31:39.601055 (outbox retention skews the set toward **younger** trades — keep this in mind when reading the aggregates).

## Validation — `monitor` config (entry1-only, internal targets) vs recorded closed_ai_signals

- targets_hit **exact**: 96.55%  ·  **±1**: 100.0%
- win/loss (TP1 touch) **agreement**: 96.55%

> Residual divergence comes from the finer resolution (5m wick + real intra-candle ordering) versus the 1h monitor — the sim is deliberately *more faithful* here than the recorded-outcome source.

## Realized aggregate per config

| config | n | unlev mean% | unlev sum% | net sum% | leveraged sum% (n) | WR(TP1)% | avg duration med/mean h |
|---|--:|--:|--:|--:|--:|--:|--:|
| monitor | 29 | -0.4006 | -11.62 | -14.52 | 92.0 (29) | 79.31 | 8.67/14.65 |
| dca10 | 29 | -0.4547 | -13.19 | -14.94 | -84.6 (29) | 79.31 | 8.67/14.65 |
| cornix3 | 29 | -0.4547 | -13.19 | -14.94 | -84.6 (29) | 79.31 | 8.67/14.65 |

**Reading aid:** `monitor` = 1:1 reproduction of the bot monitor (validation anchor). `cornix3` = what Cornix actually trades (DCA entry1/entry2, 3 published TPs in thirds) — the headline realized figure and the basis for the phase-2 overlay.


---

## Phase 2 — Auto-close overlays (on `cornix3`, real-money DCA/3-TP)

n_arts = 29 (leveraged, scored). Metric = REALIZED (locked-in) — unlev sum% / leveraged sum%; MaxDD = peak-to-trough of the aggregated open-positions wave (leveraged account units). **Baseline = hold-to-TP/SL.**

### CORE FINDING

> ⚠ **THIN (n=29 < 30): below the significance threshold — illustrative only, no verdict.** With this few legs, a single window determines the sign.

- **Leveraged realized: at least one overlay variant beats hold.** Baseline -84.6% vs (a) 39.3…73.3% / (c) 15.9…51.2%. Since the baseline here is NEGATIVE/weak, any early-exit rule beats an unfavourable hold — that is a window artefact, not a proven timing edge (n small, check baseline sign).
- **Unlevered realized:** baseline -13.19% vs (a) 1.41…3.37% / (c) -0.5…1.46% — overlays mostly BETTER (cut underwater tails).
- **Drawdown: (c) is a risk tool.** MaxDD wave 2.9 (hold) → 1.0…1.0 (~3x smaller).
- **Conclusion:** too thin for a verdict — see THIN note above.

> ⚠ **WR(TP1)% is misleading under overlays** (the rule closes on MTM retrace, not on TP touch → tp1=False even though it closed profitably). Realized is the metric, not WR. Overlay (a) triggers at ~95% (peak retrace also fires on small waves — an activation threshold would only trail large waves, but is not needed here: the sign is already clear).

### Overlay (a) — per-trade trailing TP (close at X% retrace from trade MTM peak)

| X% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD wave | triggered% |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 29 | -13.19 | -84.6 | 79.3 | 2.9 | 0.0 |
| 10 | 29 | 3.21 | 70.7 | 20.7 | — | 86.2 |
| 15 | 29 | 3.16 | 69.5 | 20.7 | — | 86.2 |
| 20 | 29 | 3.37 | 73.3 | 24.1 | — | 82.8 |
| 25 | 29 | 2.97 | 65.2 | 24.1 | — | 82.8 |
| 30 | 29 | 2.85 | 62.8 | 24.1 | — | 82.8 |
| 40 | 29 | 1.41 | 39.3 | 24.1 | — | 82.8 |

### Overlay (c) — portfolio circuit breaker (close-ALL at Y% retrace of the aggregate wave)

| Y% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD wave | flattened |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 29 | -13.19 | -84.6 | 79.3 | 2.9 | 0 |
| 10 | 29 | 1.25 | 47.1 | 20.7 | 1.0 | 25 |
| 15 | 29 | 1.24 | 46.8 | 20.7 | 1.0 | 25 |
| 20 | 29 | 1.46 | 51.2 | 24.1 | 1.0 | 25 |
| 25 | 29 | 1.0 | 42.1 | 24.1 | 1.0 | 25 |
| 30 | 29 | 0.88 | 39.6 | 24.1 | 1.0 | 25 |
| 40 | 29 | -0.5 | 15.9 | 24.1 | 1.0 | 25 |

### Long/short separated (unlev sum% / lev sum%)

| Rule | LONG | SHORT |
|---|--:|--:|
| Baseline | -13.19/-84.6 | — |
| (a) X=10% | 3.21/70.7 | — |
| (a) X=15% | 3.16/69.5 | — |
| (a) X=20% | 3.37/73.3 | — |
| (a) X=25% | 2.97/65.2 | — |
| (a) X=30% | 2.85/62.8 | — |
| (a) X=40% | 1.41/39.3 | — |
| (c) Y=10% | 1.25/47.1 | — |
| (c) Y=15% | 1.24/46.8 | — |
| (c) Y=20% | 1.46/51.2 | — |
| (c) Y=25% | 1.0/42.1 | — |
| (c) Y=30% | 0.88/39.6 | — |
| (c) Y=40% | -0.5/15.9 | — |

**Honest limit:** 7d/674 legs, younger window (outbox bias). Wave capture is market timing; what is tested is whether a MECHANICAL rule catches the wave out-of-sample or is only visible in hindsight. What's assessed are robust **bands + sign** across the sweep, not a best point. NO-EDGE is a valid result.

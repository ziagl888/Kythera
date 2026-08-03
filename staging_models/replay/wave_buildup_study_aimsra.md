# Wave Buildup — Realized-vs-Unrealized (Phase A, T-2026-KYT-9050-041)

_generated 2026-07-25 17:36:59.201921+00:00 · read-only · models AIM+SRA · 20x assumed · window 2026-02-27 02:32:26.790675 → 2026-07-25 20:20:50.181014_

**What this is:** Follow-up to T-035. Reconstructs the aggregated open **unrealized** wave and the realized outcomes of the curated S/R AI bots (AIM, SRA) over the **full history** from the RECORDED trades (`closed_ai_signals`, dedup report-14 survivor key) + candles — **without** the immutable Cornix geometry (which T-035 capped at ~7d outbox retention). Trade-off: no intra-trade DCA/TP laddering modelled (first-order wave). Leverage **flat-assumed at 20x** (not persisted before ~July; the pattern is leverage-agnostic). Realized = recorded entry→close move.

Trades scored: **6950** · without candles: 467.

## C1 — Realized-vs-unrealized asymmetry (the premise)

| Segment | n | mean realized% | mean **peak**% (wick) | **giveback** | WR% | giveback p50/p90/p95 |
|---|--:|--:|--:|--:|--:|--:|
| ALL | 6950 | +38.2 | +184.44 | +146.25 | 39.1 | 118.7/287.8/371.8 |
| AIM | 5566 | +43.35 | +209.02 | +165.67 | 35.9 | 134.9/307.1/405.1 |
| AIM-LONG | 2170 | +34.51 | +207.84 | +173.32 | 33.9 | 122.3/345.8/502.2 |
| AIM-SHORT | 3396 | +49.0 | +209.78 | +160.78 | 37.2 | 140.9/295.5/360.4 |
| SRA | 1384 | +17.46 | +85.59 | +68.13 | 51.9 | 44.0/146.9/191.4 |
| SRA-LONG | 725 | +14.93 | +90.66 | +75.73 | 48.8 | 52.5/150.6/204.7 |
| SRA-SHORT | 659 | +20.24 | +80.02 | +59.78 | 55.2 | 37.1/141.9/176.7 |

**Core observation, evidenced:** of the losing trades, **85.4% were up ≥+10%** at some point, 77.6% ≥+25%, **60.3% ≥+50%**, 36.3% ≥+100%, 16.2% ≥+200%** (leveraged, wick) — and then closed at a loss. Gains evaporate, losses are fully realized. Avg trade: realized +38.2% vs peak +184.44% → **+146.25% giveback**.

Aggregate wave: max Σ open unrealized **+39269** margin units (2026-06-06 07:00:00), up to **507** concurrently open (avg 112.5).

## C2 — Cooldown probe: expectancy N days AFTER a large wave peak

Baseline (all) real_unlev **+0.034%** · 27 peak events (p85).

| Cohort | n | real_unlev% | Δ vs baseline |
|---|--:|--:|--:|
| 1 day(s) after peak | 1467 | -0.306 | -0.340 |
| 2 day(s) after peak | 2644 | +0.064 | +0.030 |
| 3 day(s) after peak | 3115 | +0.083 | +0.049 |
| 5 day(s) after peak | 3756 | +0.188 | +0.154 |
| 7 day(s) after peak | 4211 | +0.226 | +0.191 |

**Verdict C2:** only the first ~24h after a peak are measurably weaker; days 2–7 are NOT worse (if anything better). The "pause 3–5 days after a large wave" idea would not historically have saved any expectancy — it would have given up good days instead. **The lever sits on the close side, not on re-entry timing.**

## CEIL — Capture ceiling & risk-adjusted resolution

HOLD (actual) Σ lev **+265461** · PERFECT-PEAK (hindsight upper bound) Σ lev +1281882 (~4.8x hold, unreachable).

| Trailing X% | Σ lev | mean lev% | vs hold | % of ceiling | **Sharpe lev** | mean unlev% | trig% |
|--:|--:|--:|--:|--:|--:|--:|--:|
| **hold** | +265461 | +38.2 | — | — | **+0.204** | +0.034 | 0 |
| 10% | +313269 | +45.07 | +47808 | 4.7% | **+0.534** | +1.955 | 93.7 |
| 15% | +293636 | +42.25 | +28175 | 2.8% | **+0.526** | +1.814 | 93.7 |
| 20% | +274228 | +39.46 | +8767 | 0.9% | **+0.517** | +1.675 | 93.7 |
| 25% | +255094 | +36.7 | -10367 | -1.0% | **+0.507** | +1.537 | 93.7 |
| 30% | +236228 | +33.99 | -29233 | -2.9% | **+0.495** | +1.401 | 93.7 |
| 40% | +199268 | +28.67 | -66192 | -6.5% | **+0.467** | +1.135 | 93.6 |
| 50% | +166791 | +24.0 | -98670 | -9.7% | **+0.409** | +0.901 | 93.1 |

**The reversal:** on the leveraged **sum**, trailing barely/does not beat hold (the sum is dominated by a few uncapped fat-tail hits, trailing caps those — confirms T-035). **Risk-adjusted it flips:** per-trade **Sharpe lev +0.204 (hold) → +0.534 (trailing 10%)** — trailing roughly halves the spread and lifts the mean. Unlevered: mean +0.034%/trade (hold, ~breakeven) → +1.955%/trade (trailing 10%).

### Compounding equity (fixed stake fraction, chronological by close) — the real account

| Stake/trade | HOLD final | HOLD MaxDD | Trailing 10% final | Trailing 10% MaxDD |
|--:|--:|--:|--:|--:|
| 1% | ×1.01e+11 | **73.8%** | ×2.95e+13 | **11.6%** |
| 2% | ×1.09e+21 | **93.3%** | ×4.79e+26 | **22.0%** |
| 5% | ×1.65e+46 | **99.9%** | ×7.8e+64 | **46.9%** |

**Conclusion Phase A:** (1) The asymmetry is real and large — premise confirmed. (2) The cooldown/re-entry idea is NOT supported by the data. (3) A tight **trailing close (10–15%)** is risk-adjusted and compounding **clearly superior** (higher Sharpe, more compounding, ~6x smaller drawdown) — T-035's "hold wins" was an artefact of the leveraged **sum**. The next step is validation on the T-035 high-fidelity harness (5m wick + 10s resolver, Sharpe/MaxDD instead of sum), including the entry2-as-SL question.

## Honest limits

- First-order wave: recorded entry/close as ground truth, NO intra-trade DCA/TP laddering — the unrealized amplitude is slightly overestimated (peak timing unaffected). The T-035 phase-2 harness is the laddering-faithful reference (but there only ~7d outbox window).
- Leverage flat-assumed at 20x (not persisted before ~July). The pattern (giveback, Sharpe reversal) is leverage-agnostic; the absolute leveraged numbers scale with this assumption + the -100% clamp.
- Trailing sim: 1h wick, peak-before-trigger on the same candle (slightly optimistic at the trigger level). A 5m/10s resolver (T-035) sharpens this; the DIRECTION (Sharpe/MaxDD advantage) is robust.
- Compounding sequential by close → ignores concurrency (up to the concurrently open trades noted above, cross-margin). The absolute multiples (×1e26) are NOT to be taken literally — ratio + MaxDD are the signal.
- Live/shadow gate state is time-variable; no gate filtering here — this covers the AIM/SRA strategies across all generations (AIM1→AIM2, SRA1→SRA2).

# Leg composition for a Cornix-traded channel — candle replay of the whole fleet

**T-2026-KYT-9050-104** · 2026-08-05 · read-only study, no live change ·
tools: `tools/leg_composition_replay.py`, `tools/oi_gate_eval.py` ·
data: 42,277 signals / 3.86M 5m candles, 2026-07-11 → 2026-08-02, 72h horizon

**Origin (Michi, 2026-08-05):** a Cornix backtest of the AIM and Drawdown (Bot 40)
channels over 01.–05.08. at four position sizes produced results spanning +109.6 %
to −50.3 %. The question was whether they can be verified and what follows for the
composition of a traded channel.

## Verdict in one line

The Cornix numbers are internally consistent but not comparable across sizes, the
window was not representative, and **nothing in leg selection, exit rule or SL
geometry survived both regime cohorts except one thing: an OI filter on the short
side.**

---

## 1. The Cornix backtest measures sizing, not strategy

All nine rows reconcile against the stated capital ($1000 / $800 / $10,000). What
does not reconcile is comparing them to each other.

Cornix sizes off the **available** balance. Simulated against the real open/close
intervals of the book, mean position size therefore saturates while concentration
explodes:

| Setting | mean size | largest single position | N_eff (AIM) | top-20 share |
|---|--:|--:|--:|--:|
| 2 USD @ 800 | 0.250 % | 0.25 % | 299 | 6.7 % |
| 1 % @ 1,000 | 0.576 % | 1.0 % | 285 | 10.9 % |
| 5 % @ 10,000 | 1.069 % | 5.0 % | 194 | 22.7 % |
| 10 % @ 10,000 | 1.243 % | 10.0 % | **131** | 29.8 % |

The model predicts the 1 % run at 2.30× the 2-USD run (AIM) and 2.32× (DT);
observed 2.05× and 1.91×. Between 5 % and 10 % mean exposure barely moves (+16 %)
while the observed results differ 7-fold — that gap is allocation timing, not edge.

**Consequence:** only the fixed-amount run allocates uniformly across all signals.
Above ~1 % the percentage setting buys no exposure, only concentration.

## 2. The window was not representative

The trailing book runs **+0.31 pp/trade** over 01.–05.08. and **−0.61 pp/trade**
over 11.07.–02.08. At 2 USD / 20× (5 % notional per trade) the longer window is
−33 % of account. A 4.5-day sample cannot carry a composition decision.

## 3. The regime split is mandatory

`EXPOSURE_CAP` + time-stop went live **2026-07-28 14:00Z**:

| cohort | n | LONG share | pp/trade |
|---|--:|--:|--:|
| before | 637 | **83 %** | **−1.342** |
| after | 1,045 | **51 %** | **+0.191** |

The cap removed the tail: worst day before −549 pp, after −37.7 pp over nine days.
**Pooling the cohorts produced a false verdict once** ("MIS1-72H is the loss
engine"); post-cutoff that leg runs +0.38 pp over 149 trades. Both tools hard-wire
the split, and `backtest/test_leg_composition_replay.py` pins it.

## 4. Direction edge is a property of the geometry, not the market

Same data, same period, two geometries:

| geometry | book of positive-expectancy legs |
|---|---|
| TP 4 % / SL 5 % | **95 % LONG** / 5 % SHORT |
| TP 3 % / SL 2 % | 24 % LONG / **76 % SHORT** |

AIM2-SHORT moves +0.062 → **+0.531**, TD_1H-LONG +1.127 → −0.627. Mechanically
consistent with down-moves being faster and sharper: shorts need a tight target
*and* a tight stop, longs need room. Break-even win rate at TP3/SL2 is 40 %.

The original grid floor of 3.0 was itself a defect — nearly every short leg
optimised on the smallest available SL, i.e. the run was reading its own boundary.
The grid now starts at 1.5.

## 5. SL distance is not a universal lever

On 1,015 mirrors, expectancy is flat from SL 3 % to 8 % (−0.62 … −0.66 pp) and the
exit rule is a wash (trail −0.61 vs hold-with-ladder −0.66). Leg selection spans
2.6 pp. But per §4 the SL *does* matter once split by direction — the flat result
came from a population that was 83 % long.

Note on method: bucketing trades by the SL stored in `closed_ai_signals.status` is
invalid. That value is the **monitor-trailed** stop, so the bucket is conditioned
on the outcome — the "<3 % SL" bucket shows a 94.7 % TP1 rate because those are the
trades that ran far enough for the stop to be pulled up behind them.

## 6. Short legs are regime-unstable

The same legs, two adjacent windows, five-figure samples:

| | signals in positive legs | in negative legs |
|---|--:|--:|
| 11.07.–28.07. | **14,806** | 1,438 |
| 28.07.–02.08. | 309 | **4,220** |

AIM2-SHORT +1.222 → +0.062, BR1Hv2-SHORT +0.451 → −1.159, EPD3-SHORT +0.108 →
−0.374. An expectancy ranking would therefore recommend a different roster every
few weeks. **Direction balance has to be a channel-level constraint
(`EXPOSURE_CAP`), never an emergent property of a ranking.**

## 7. The one regime-stable finding: OI as a short-side filter

As a global ranker OI carries nothing — **T-094 replicates**, no AUC above 0.56:

| feature | LONG post/pre | SHORT post/pre |
|---|--:|--:|
| `oi_chg_4h` | 0.555 / 0.552 | 0.458 / 0.463 |
| `oi_chg_24h` | 0.519 / 0.539 | 0.516 / 0.496 |
| `oi_pct_30d` | 0.510 / 0.539 | 0.518 / 0.504 |

AUC is the wrong statistic here: the effect sits in the tail, not in the ranking.
Bottom quintile of `oi_chg_4h` (open interest down ≥ ~1.2 % over 4h), TP3/SL2:

| cohort | n | win rate | exp pp | other four quintiles |
|---|--:|--:|--:|---|
| pre | 3,158 | **55.0 %** | **+0.739** | +0.21 … +0.33 |
| post | 901 | **51.4 %** | **+0.552** | −0.12 … +0.04 |

Longs show the mirror (top quintile +0.284 / +0.168 vs bottom −0.324 / −0.507):
long with rising OI is new money behind the move, short with falling OI is a
short-covering rally with none.

This is the only result in the study that survives both cohorts, and it
independently reproduces **T-096's DIVERGENCE-SHORT** on a different population —
T-096 generated its own events, this filters the fleet's existing shorts.

Against it: the quintile was chosen after seeing it. What offsets that is the
two-cohort replication plus agreement with T-096's pre-registered hypothesis — more
than a post-hoc pick, less than an out-of-sample test. The gate also discards 80 %
of short volume.

## Data quality (measured, not assumed)

* `oi_5m` is not a 5-minute table. Median cadence **5.0 min until 2026-07-06,
  10.0 min from 2026-07-13 onward** — the collector degraded and stayed degraded
  (T-2026-KYT-9050-097 now has a date). Every lookup is as-of with a 45-min
  staleness cap; 597 signals voided rather than filled.
* 45h OI outage 2026-07-12 → 14 inside the window.
* `liq_events` starts 2026-08-03 — no liquidation feature is testable yet (T-095).
* Fleet signal volume grew 6× over 8 weeks (2,319/week mid-June → 15,769/week end
  of July). The fleet is not stationary; a single chronological split would compare
  two different fleets.

## Retractions from this session

Recorded because each was stated with confidence before being measured properly:

1. **"MIS1-72H is the loss engine, drop it."** Came from pooling the two regime
   cohorts. Post-cutoff it is +0.38 pp over 149 trades.
2. **"AIM2 posts every signal twice, live double-trade."** The second message
   carries a chart image and is inert for Cornix. Real residual: the `else` branch
   without an image, 10 of 299 in the window (29 of 615 since 01.07.) — a ~3–5 %
   defect, not a systematic double post. Worth its own ticket, not a money-path
   emergency.
3. **"There are no profitable shorts; shorts are insurance you pay for."** Based on
   the 5-day post-cutoff cohort. The 17-day pre-cutoff cohort has shorts broadly
   positive on five-figure samples.

## Not concluded here → T-105

Sizing and trade count cannot be answered by per-trade expectancy. The book
reports +0.69 pp/trade for AIM2 where the Cornix run implies +0.054 pp — a factor
of 13 from ladder partials, fills and fees. That gap closes only in a portfolio
simulation with fixed-USD sizing, occupancy/margin, `EXPOSURE_CAP`, leverage, the
TP ladder and fees, reporting equity curve, max drawdown and worst day.

Because the fleet is non-stationary and the OI cadence changed mid-period, that
study must run **walk-forward** (fit on a trailing window, apply frozen to the
next), not on a single split — and it must down-sample the pre-13.07. OI to the
10-minute grid so the feature is measured the same way throughout.

Interaction worth pre-registering: filtering harder lowers peak occupancy, which
*raises* the affordable fixed position size (`amount ≤ utilisation × capital /
peak_occupancy`). Expectancy and sizing multiply; the tables above see only the
first factor.

## Reproduce

```bash
# on the VPS (needs .env) — read-only, BELOW_NORMAL, one sequential read per symbol
python tools/leg_composition_replay.py export --since 2026-07-11 --force-on-busy
python tools/oi_gate_eval.py features --force-on-busy
# anywhere (numpy only, no DB, no credentials)
python tools/leg_composition_replay.py replay --tp 3.0 --sl 2.0
python tools/oi_gate_eval.py gate --tp 3.0 --sl 2.0
```

The `.npz` exports (34 MB + features) are gitignored — regenerate them, or carry
the file to a dev machine and run the replay half there.

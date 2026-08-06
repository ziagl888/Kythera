# Leg composition for a Cornix-traded channel — candle replay of the whole fleet

**T-2026-KYT-9050-104** · 2026-08-05, **corrected 2026-08-06** · read-only study,
no live change · tools: `tools/leg_composition_replay.py`, `tools/oi_gate_eval.py` ·
data: 43,330 signals / 3.96M 5m candles, 2026-07-11 → 2026-08-02, 72h horizon

> **Correction notice (T-2026-KYT-9050-107).** The first version of this study read
> `closed_ai_signals.open_time` as UTC. That column is naive and mixed-domain, so
> ~84 % of signals were stamped 3 h late. **Section 7's headline finding did not
> survive the re-run and is retracted there in full.** Sections 4 and 6 were
> recomputed on the corrected export and their numbers have moved. Sections 1, 2,
> 3, 5 and "Data quality" were never produced by the committed tools at all — see
> the provenance note below.
>
> **Provenance, which the first version did not state.** Only sections 4, 6 and 7
> come from `tools/leg_composition_replay.py` + `tools/oi_gate_eval.py` and the
> committed `reports/*.json`. Sections **1, 2, 3, 5**, "Data quality" and
> "Retractions" come from an ad-hoc session against the live book on 2026-08-05:
> no code in this PR computes them and no committed artifact contains them. They
> are recorded observations, not reproducible results, and must not be quoted as
> if the reports backed them.

**Origin (Michi, 2026-08-05):** a Cornix backtest of the AIM and Drawdown (Bot 40)
channels over 01.–05.08. at four position sizes produced results spanning +109.6 %
to −50.3 %. The question was whether they can be verified and what follows for the
composition of a traded channel.

## Verdict in one line

The Cornix numbers are internally consistent but not comparable across sizes, the
window was not representative, and **after the timestamp correction, nothing in
leg selection, exit rule or SL geometry survives both regime cohorts.** The OI
short-side filter that the first version reported as the one survivor was an
artifact of the defect (§7).

The earlier phrasing — "nothing survived … except one thing" — was also wrong on
its own terms, independently of the timestamp bug: "survives" was never defined,
and at the study's own TP3/SL2 geometry **18 legs are sign-stable positive in both
cohorts** (5 under the n ≥ 40 filter §4 applies), including EPD3-SHORT on
n = 6,864/2,352. The claim should have been the narrower one it actually
supported: no *leg-selection rule* generalised across the cohorts.

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

| geometry | book of positive-expectancy legs | before correction |
|---|---|---|
| TP 4 % / SL 5 % | **95 % LONG** / 5 % SHORT | 95 % / 5 % (unchanged) |
| TP 3 % / SL 2 % | **66 % LONG** / 34 % SHORT | 24 % / **76 %** |

The qualitative claim survives — geometry moves the direction balance of the book,
and it moves it a long way. The **magnitude does not**: at TP3/SL2 the corrected
run is 66/34 LONG-majority, where the defective one read 76 % SHORT. So "tight
geometry makes the book short" is retracted; what remains is "tight geometry makes
the book materially less long" (95 % → 66 %).

Break-even win rate at TP3/SL2 is 41.8 % including the 0.09 pp round-trip fee (the
40 % previously stated is the fee-free figure).

Two caveats on this section, both understated in the first version:

* **Fee asymmetry.** `leg_composition_replay.py` subtracts `FEE_PCT = 0.09` and
  `oi_gate_eval.py` does not, yet both were reported as "pp" side by side. §4/§6
  are net, the old §7 was gross.
* **The grid still reads its own edge.** Raising the floor from 3.0 to 1.5 moved
  the boundary rather than removing it: **64 of 72** ranked cells (n ≥ 40) still
  optimise on a grid edge, mostly at the TP ceiling 8.0 or the new SL floor 1.5.
  Per-leg optimal geometry is therefore *not* identified by this run. Only the
  fixed-geometry comparison in the table above is supported.

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

The same legs, two adjacent windows, five-figure samples. **SHORT legs only, at
TP4/SL5, with no minimum-n filter** — §4 applies n ≥ 40 and this tally does not,
which the first version failed to state:

| | signals in positive legs | in negative legs |
|---|--:|--:|
| 11.07.–28.07. | **14,953** | 1,377 |
| 28.07.–02.08. | 462 | **4,463** |

Per leg, pre → post at TP4/SL5 (n ≥ 40 in both cohorts):

| leg | n pre/post | pre | post |
|---|--:|--:|--:|
| AIM2 SHORT | 1,289 / 251 | +1.285 | **+0.513** |
| MAX1 SHORT | 251 / 80 | +0.722 | −0.505 |
| SRA2 SHORT | 372 / 85 | +0.841 | −0.946 |
| BR1Hv2 SHORT | 1,079 / 253 | +0.403 | −1.155 |
| BR2H SHORT | 651 / 101 | +0.310 | −1.200 |
| ROM1 SHORT | 2,661 / 757 | +0.255 | −0.537 |
| EPD3 SHORT | 6,916 / 2,499 | +0.052 | −0.430 |

**This is the section the correction left standing.** Eight of nine SHORT legs
with usable samples flip negative across the cutoff, and only AIM2 stays positive.
An expectancy ranking would therefore recommend a different roster every few weeks.
**Direction balance has to be a channel-level constraint (`EXPOSURE_CAP`), never an
emergent property of a ranking.**

One caveat the first version did not carry: these `n` are signal counts, not
independent observations. ROM1 is a re-forwarder, so its ~2.7k rows mirror events
already counted under the originating leg. Read the sample sizes as volume, not as
statistical power.

## 7. The OI short-side filter — RETRACTED, it was a timestamp artifact

**This section previously reported the study's one regime-stable finding. It does
not exist.** It was produced by reading `closed_ai_signals.open_time` — a naive,
mixed-domain column — as UTC, which stamped ~84 % of the population 3 h late. The
`oi_chg_4h` window then spanned `[t−1h, t+3h]` instead of `[t−4h, t]`: it
straddled the signal and carried three hours of *post-signal* open interest. An OI
drop measured partly after entry can simply be the position closing. The apparent
edge was that look-ahead.

Re-run 2026-08-06 on the corrected export (T-2026-KYT-9050-107), same geometry
(TP3/SL2), same quintile construction, `oi_chg_4h | SHORT`:

| cohort | quintile | before (3 h late) | after (corrected) |
|---|---|--:|--:|
| pre | q1 (most OI drain) | **+0.739** | **+0.324** |
| pre | q5 (most OI build) | +0.243 | **+0.340** |
| post | q1 | **+0.552** | **−0.065** |
| post | q5 | +0.040 | **+0.230** |
| pre / post | AUC | 0.463 / 0.458 | 0.501 / 0.518 |

Read the two right-hand columns as a whole and the finding inverts. Pre-cutoff,
the bottom quintile no longer beats the top (+0.324 against +0.340). Post-cutoff
the bottom quintile is **negative** and the best bucket is now the *opposite* end.
The AUCs move from 0.458/0.463 — below 0.5, i.e. weakly informative in the
inverted direction — to 0.501/0.518, which is a coin flip.

So there is no bottom-quintile short edge, no mirror in the longs to interpret,
and **nothing in this study reproduces T-096**. The reproduction claim is
withdrawn in full: it was never independent in time either (T-096 ran
2026-06-12 → 08-04 and *contains* this window, 07-11 → 08-02), but that is now
moot, because after correction there is no result to compare.

What survives from this section is one methodological point, and it is the
expensive kind: **a study that reads a naive timestamp column must prove which
domain it is in.** `tools/leg_composition_replay.py` now carries a writer-aware
conversion (ROM1 explicit-UTC vs the 13 `DEFAULT now()` writers on Bucharest, plus
the R3 flip window) and a hard gate that fails the export when the recorded entry
stops falling inside the candle at its claimed instant. That check moved from
32.6 % to 68.2 % on this data; the defective read would not pass it.

**Consequence for ODS1 (bot 42, T-2026-KYT-9050-106):** its second evidence pillar
is gone, not merely weakened. ODS1 now rests on T-096 alone — which is unaffected,
because `tools/oi_event_study.py` generates its events from `oi_5m` directly and
never touches the signal tables.

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

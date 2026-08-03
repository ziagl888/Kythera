# Report 21 — ABR1 LONG: optimisation study (negative result)

**Date:** 2026-07-06 · **Data basis:** replay `detector_fix/abr1_replay_365d.jsonl`
(new detector after rework, 100 coins / 365 d, 27,705 LONG events) + 1h OHLCV read-only from the live DB.
**Occasion:** After the detector rework (CHANGELOG 2026-07-05), only the SHORT binary model was
deployed; LONG stayed on the legacy blocker. Operator question: can LONG be saved —
"break & retest works for longs too, doesn't it?"

**Short answer: no — not in this market year, not with these levers.** All three
optimisation classes (trade management, ML entry selection, regime filter) were fully
simulated on real price data; none turns LONG positive. LONG stays off.

---

## 1. Diagnosis: why LONG loses

| | LONG | SHORT (reference) |
|---|---|---|
| TP1 first-touch WR | 55.5% | 58.0% |
| avg win / avg loss | +3.01% / −5.09% | +4.18% / −5.14% |
| break-even WR (from payoff) | **≈ 62.8%** | ≈ 55.2% |
| avg PnL/trade | **−0.59%** | +0.28% |

The problem is the **payoff asymmetry, not the hit rate**: LONG wins pay ~28%
less than SHORT wins at the same loss size. Even the best months (2025-09: 64% WR,
2026-04: 63%) barely scrape break-even. Monthly WR swings 43–64% → strongly
regime-driven. None of the 23 features separates meaningfully (best quartile −0.16%/trade).

Structural root causes in the code: `calculate_smart_targets` sets the SL ≥ 3×ATR
below entry (avg risk 4.96%) — generic swing geometry instead of setup invalidation at
the level; the ladder management (1/n, trailing only from TP2) gives up 2/3 of the
position at the full SL under `sl_after_tp1` (3,746 trades).

## 2. Levers tested

### 2a. Trade management (exit resimulation, 27,559 trades, baseline replication 99.7%)

| Variant | WR | avg PnL/trade | avg R | Sum |
|---|---|---|---|---|
| V0 original (SL 3×ATR, trailing from TP2) | 55.4% | −0.60% | −0.10 | −16,566% |
| V1 + breakeven SL after TP1 | 55.4% | −0.50% | −0.09 | −13,742% |
| V2 setup SL 1.0% below level | 32.6% | −0.25% | −0.13 | −6,845% |
| V3 setup SL 1.5% below level | 38.2% | −0.30% | −0.12 | −8,201% |
| V4 = V2 + BE after TP1 | 32.6% | **−0.24%** | −0.13 | −6,696% |

The tight setup SL roughly halves the nominal loss, but is **risk-adjusted worse**
(−0.13 R vs. −0.10 R): the 1h wicks tag the tight stop too often. BE-after-TP1 helps
(+0.10 pp), but isn't enough on its own. No month except the fringe month 2026-07 turns stably positive.

### 2b. ML entry selection (XGB, label `net_pnl > 0`, chrono 70/15/15 + 7d purge, 23+3 features)

| | Val (q0.95 slice) | Test (same threshold) |
|---|---|---|
| under V0 management | **+3.25%**/trade | **−2.17%**/trade |
| under V4 management | +0.74%/trade | −1.07%/trade |

Every test slice negative, and **the higher the threshold, the worse** — the model
learns val-regime patterns that invert out-of-sample. Identical signature to the
batch retrain (report 19 / deploy 2026-07-06: test WR 51.8% == base rate, top
bucket inverted). This is not a training bug, it's missing signal in the features.

### 2c. BTC regime filter (EMA200(1d) / 30d momentum, previous-day shift)

| Regime | n | V0 avg | V4 avg |
|---|---|---|---|
| BTC > EMA200 | 6,508 | **−1.08%** | −0.22% |
| BTC < EMA200 | 21,197 | −0.46% | −0.25% |

Even inverted: upward resistance breaks in alts get sold *harder* during a BTC
uptrend regime. Unusable as a gate.

## 3. Assessment & recommendation

The asymmetry is market-logically consistent: upward breaks in alts get faded —
the strategy's edge sits on the SHORT side (failed/overextended moves), which the
deployed SHORT gate confirms (test WR 68% vs. 63.7%, +1.5%/trade).

1. **LONG stays off** (status quo: legacy 3-class model without meta.json acts as a
   de-facto block @ threshold 0.60). No code change needed.
2. **Stop turning further knobs on exit geometry/threshold** — the search space is
   grazed out here; further iterations would be overfitting on the same 365 days.
3. Reactivation only via **new information sources** (order flow/funding/whale data
   from bot 19/20, BTC dominance, level confluence across timeframes) — its own
   research project, not tuning — **or** via a regime shift: re-run the replay
   quarterly; re-evaluate once the unfiltered LONG base rate durably crosses ~63%
   (break-even).
4. V1 (BE-after-TP1) would be worth checking as a *general* management improvement
   for SHORT too (+0.10 pp on LONG with no WR loss) — separate ticket, concerns
   `8_ai_trade_monitor`.

**Artifacts:** diagnosis/resim/model scripts in the session scratchpad; resim raw
data `resim_results.pkl`. Replay + stats: `_X\staging_models\replay\detector_fix\`,
`_X\staging_models\retrain_abr1_stats.json`.

---

## Addendum (2026-07-06 evening): target side also tested — negative

At the operator's request, the last untested structural lever was checked:
**R-based targets** (TP1/2/3 = entry + 1R/2R/3R) instead of level-cluster targets —
the hypothesis that the near cluster targets above entry cause the payoff asymmetry.

| Variant | WR (TP1) | avg PnL/trade | avg R | BE-WR (payoff) | pnl>0 rate |
|---|---|---|---|---|---|
| G0 original | 55.3% | −0.61% | −0.10 | 48.4% | 41.7% |
| G1 smart SL + R targets | 46.7% | −0.84% | −0.14 | 36.2% | 29.2% |
| G2 setup SL + R targets | 47.6% | **−0.21%** | −0.11 | 36.7% | 31.8% |
| G3 = G2 + BE after TP1 | 47.6% | −0.21% | −0.11 | 53.5% | 47.6% |
| G4 = G1 + BE after TP1 | 46.7% | −0.69% | −0.12 | 53.9% | 46.7% |

Central observation: the geometry only shifts HOW it loses — the risk-adjusted
expectancy stays across the ENTIRE geometry space at **−0.10 to −0.14 R**
(fees explain only ~0.05 R of that). Symmetric payoff
(G1: BE-WR 36%) is paid for exactly by the collapsing win rate (29%).
No month except the fringe month 2026-07 stably positive. **This falsifies entry
gate, SL side, target side, management, ML selection, and BTC regime alike — the
LONG side has no edge in this market year, period.**

Next (last) candidate per §3: new information sources. Funding-rate
history is being backfilled (`tools/backfill_funding_rates.py`, table
`funding_rates`) — unlike whale data (WS only live again since 04.07.),
funding is fully available historically.

---

## Addendum 2 (2026-07-06 late evening): feature recheck — mechanics + funding

Operator hypothesis: "we're not looking at the right indicators." 16 **setup
mechanics features** (break volume/body/close position, pullback volume, level
touches, coin trend 7d/30d, distance to 30d high/low, SMA50d/20d, relative
strength vs. BTC, ATR) and 6 **funding features** (latest rate, 24h/72h mean,
7d sum, 90d percentile, trend) were tested.

**Mechanics features:** univariately, positive cells for the first time —
`level_touches` Q4 (+0.10%, 61% WR), `dist_lo_30d` Q1 (+0.49%, 65%: early
reversal breaks near the 30d low), `atr14_rel` Q1 (+0.24%, 64%). Notable:
**break volume — the textbook criterion — is completely flat.** Rule
combinations (thresholds from train only) reach +0.5…+0.76%/trade in train,
but are ALL negative in the test window (May–Jul 26) → regime-dependent, not
deployable.

**Funding:** 75% of all values cling to the Binance default (+1.0 bps). Signal
sits strictly above that (longs paying real premium = willing-to-pay perp
demand behind the break):

| fund_24h rule | n/year (100 coins) | overall | train | test (May–Jul) |
|---|---|---|---|---|
| ≥ +1.0 bps (default) | 6,954 | −0.58% | −0.67% | −1.29% |
| > +1.5 bps | 311 | +1.32% / 74% | +1.66% | −1.02% (n=49) |
| **> +3.0 bps** | 119 | **+1.12% / 74%** | +1.44% | **+0.69% / 88% (n=17)** |

`fund_24h > +3 bps` is the **only rule in the entire study series that survives
the out-of-sample test** — on an honestly thin test base (n=17).

**Operator decision:** live experiment instead of further validation. LONG now
opens only via the funding gate (`FUNDING_GATE_LONG_BPS = 3.0`, mean of the
last 3 settlements via REST, fail-closed, 30-min cache), tag ABR2, funding
value in the info message. Expectation ~1–2 signals/day across 530 coins.
Review after 4–6 weeks or ≥30 tracked trades.

**Mirror test SHORT (33.5k events):** the same funding zone is consistently
toxic for SHORTs — `fund_24h > +1,5 bps` → −1.21%/trade (train −1.13, test
−1.00; > +3 bps: −1.21/−1.11/−1.72). This cross-validates the LONG gate with
independent data. The symmetric side (fund_24h < −3 bps → +0.56%) is real,
but weaker than the existing model gate (+1.5%/trade) — no replacement, no
change. **Implemented as a SHORT funding veto**
(`FUNDING_VETO_SHORT_BPS = 1.5`, fail-open): SHORT needs the model gate AND
fund_24h ≤ +1.5 bps.

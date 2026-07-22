# T-2026-KYT-9050-030 — GARCH Vol-Targeting LIVE Verdict

**Question (the open half of T-022):** does GARCH vol-targeting improve the realized
risk-adjusted returns of Kythera's *edge-positive* bots?

**Verdict: NO-PULL (immaterial MIXED).** On 16,613 real realized trades across the
edge-positive bots, vol-targeting lifts pooled Sharpe by **+0.009** and the median
bot by **+0.013** — an order of magnitude below the +0.10 gate. **Not a single**
edge-positive bot clears the bar. Vol-targeting reliably shrinks volatility (pooled
σ −8%) but trims the mean almost proportionally, so risk-adjusted return is flat.
**Recommendation: do NOT pursue a gated live-wiring follow-up.** The idea is retired
cheaply.

Type: read-only study on the live VPS (SRV02, `cryptodata@localhost`,
`set_session(readonly=True)` + `statement_timeout`, SELECT only). No writes, no
artifact promotion, no gate flips, no live wiring. Driver:
`tools/research/garch/t030_live_verdict.py`. Date: 2026-07-23.

---

## 1. Method

- **Population.** Realized trades from `closed_ai_signals`, **real-geometry exits
  only** — the synthetic `LEGACY … (±2.5%)` rows (351k of 458k) and `DELISTED /
  CLEANUP` / `ENTRY_NOT_FILLED` are excluded; they encode a fixed ±2.5% and are not
  real per-trade PnL. Per-trade realized return `r = sign·(close_price − entry)/entry`
  (gross; fees scale with size so they don't change the fixed-vs-vol *relative*
  comparison).
- **Edge set (confirmed empirically, realized mean > 0, n ≥ 50).** From the realized
  WR/mean-PnL table (real-geometry exits): the T-022 expected set holds — **AIM2,
  EPD1/EPD3, MIS1 family, MIS2 family, RUB2-SHORT, MAX1**. Legs kept are the
  edge-positive ones (e.g. RUB2-**SHORT** +0.60%/trade is kept; RUB2-LONG −1.97% is
  not). The covered book is strongly edge-positive: pooled **1.265%/trade, WR 47.2%**.
- **GARCH forecast.** For each coin, daily closed candles via the shared reader
  (`core.candles.read_candles`, `include_forming=False`) → one walk-forward
  `walkforward_garch` (min_train=500, refit every 21 bars). **As-of entry, zero
  lookahead:** each trade takes the forecast row whose candle date is *strictly
  before the entry day* (its forecast used only candles closing at/before entry-day
  00:00).
- **Sizing.** `size_from_vol(fcast_vol_ann, target_vol)`, cap [0.25, 2.0]. **target_vol
  is calibrated to the sample median forecast vol (99.15% ann)** so the multiplier
  centers on ~1.0 — a genuine *reallocation* test (bigger in calm, smaller in storm,
  same average book size), not a uniform deleverage. Multiplier: median 1.00, mean
  1.016, p10–p90 = 0.70–1.34, only 0.8% at floor / 1.0% at cap.
- **Metrics.** Fixed (1×) vs vol-targeted (mᵢ·rᵢ) on the **same** trade subset.
  Returns are treated **additively** (fixed-fractional / R-multiple book) because
  these are discrete signals overlapping in time across ~593 coins — sequential
  compounding of an overlapping stream is fictional (it produced a 12M× equity /
  −98% DD artifact). Sharpe = per-trade mean/σ (annualization factor cancels in the
  delta). Verdict gate mirrors `compare.py`: PULLS = Sharpe Δ ≥ +0.10 without DD or
  worst-month worsening > 2pp; NO-PULL = Sharpe Δ ≤ 0; MIXED otherwise.
- **Coverage.** 16,613 / 35,945 edge trades (**46.2%**) on the 318 coins with ≥510
  daily bars. Newer coins (< 510 daily bars) can't seed a 500-bar GARCH warmup and
  are excluded — a mild older-coin tilt, disclosed in §4.

## 2. Results

### Pooled (all 16,613 edge trades, one book)

| Metric | Fixed (1×) | Vol-targeted | Δ |
|---|---:|---:|---:|
| Sharpe (per-trade) | 0.1515 | 0.1601 | **+0.009** |
| Mean return / trade | 1.265% | 1.231% | −2.7% |
| Std / trade | 8.35% | 7.69% | **−7.9%** |
| Win rate | 47.2% | 47.2% | 0.0 (invariant) |
| Max drawdown (R-pp) | −2574 | −2823 | −249 (worse) |
| Worst month (R-pp) | −536 | −370 | +167 (better) |

→ **MIXED, but the Sharpe lift is immaterial** (+0.009 vs a +0.10 bar). σ falls but
so does the mean; DD slightly worse, worst-month slightly better. No risk-adjusted
edge.

### Per bot (Sharpe delta is the headline; +0.10 = PULLS bar)

| Bot | n | Sharpe Δ | σ Δ | MaxDD Δ (pp) | Verdict |
|---|---:|---:|---:|---:|---|
| MIS1-8H | 64 | **+0.066** | −4.6 | +49.6 | MIXED |
| RUB2 | 168 | **+0.054** | −0.4 | −3.4 | MIXED |
| MIS1-24H | 75 | +0.021 | −6.2 | +23.2 | MIXED |
| MAX1 | 87 | +0.017 | +0.3 | −1.3 | MIXED |
| MIS1-72H | 6150 | +0.013 | −0.67 | +8.7 | MIXED |
| MIS1-168H | 3862 | +0.004 | −0.51 | −131.9 | MIXED |
| EPD1 | 2320 | +0.001 | −0.44 | −201.0 | MIXED |
| EPD3 | 2602 | −0.002 | −0.32 | +0.6 | NO-PULL |
| AIM2 | 1128 | −0.005 | −0.80 | −14.6 | NO-PULL |

**Median across 9 bots: Sharpe Δ +0.013, MaxDD Δ −1.34pp, worst-month Δ +0.09pp.**
The two "best" bots (MIS1-8H, RUB2) are the smallest samples (n = 64, 168) and *still*
fall well short of +0.10. The large-n bots (EPD1, EPD3, MIS1-72H/168H, AIM2 — 15,000+
of the 16,600 trades) sit at Sharpe Δ ≈ 0 or negative.

### Sensitivity — the naive `target_vol = 15%` trap

With the harness default 15% target (≈ 6.6× below crypto's ~99% median vol), every
multiplier pegs low: mean size 0.15×, book deleverages 6.6×. Result: MaxDD −2574 →
−645pp, worst-month −536 → −134pp "improve" — but **Sharpe Δ = +0.0006** (flat). This
is a pure size cut, not smarter allocation. It's the mirage that would make a naive
read claim "vol-targeting cut drawdown 4×!"; the calibrated (median-target) run above
is the honest test, and it shows no Sharpe benefit.

## 3. Why it doesn't pull

GARCH forecasts **magnitude, not direction** (the harness says so). On signals that
already carry their own edge, scaling size by inverse regime-vol reshuffles notional
without concentrating capital on the *winning* trades — daily regime vol doesn't
predict which of a bot's trades win. So the σ cut is mechanical (down-size high-vol
coins → lower variance) and comes with an almost-equal mean cut → Sharpe ≈ flat.
This is consistent across all 9 bots and both target calibrations, and it matches the
prior combo-study finding (memory `kythera-stoic123-garch-research`): **the edge lives
in the regime/exit infrastructure, not in a vol-sizing overlay.**

## 4. Caveats (honest bounds)

1. **46% coverage, older-coin tilt.** Only coins with ≥510 daily bars qualify. The
   subset is large and holds the core edge (EPD1, MIS1, AIM2, RUB2, MAX1); a +0.009
   pooled lift is very unlikely to become +0.10 on the newer coins.
2. **Daily GARCH vs sub-daily horizons.** EPD/AIM2 exits are intraday; a 4h/1h GARCH
   would align the forecast bar better. But the failure pattern (σ-cut ∝ mean-cut →
   Sharpe flat) is *structural* and uniform across bots and calibrations, so a finer
   bar is very unlikely to flip a 10×-too-small effect. Left as the single residual
   refinement — **not worth live-VPS CPU** given a NO-PULL is the disciplined outcome.
3. **Fixed-notional additive book, gross returns, trades independent.** Standard
   signal-evaluation frame; no concurrent-capital constraint modeled. Fees omitted
   (scale with size → neutral to the relative comparison).

## 5. Recommendation

**Do not wire GARCH vol-targeting into any bot's sizing.** A money-wired live
integration is not justified: no edge-positive bot shows a material risk-adjusted
improvement, and the pooled/median effect is an order of magnitude below the decision
threshold. If risk-of-ruin (not Sharpe) ever becomes the objective, the naive
low-target deleverage is a simpler, transparent lever — but that's a position-sizing
policy choice for Michi, not a GARCH feature. **T-022 is answered: vol-targeting does
not pull at Kythera. Idea retired.** (Correlation layer T-023 remains separate backlog.)

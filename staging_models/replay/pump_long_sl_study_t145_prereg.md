# Pre-Registration: SL grid for the pump-continuation LONG (T-2026-KYT-9050-145)

_registered 2026-08-15 · BEFORE any outcome was computed · tool:
`tools/pump_long_sl_study.py` · events from the T-144 grid mechanics · paths
from `candles` 5m (since 2026-06-13, 659 symbols)_

**Question (Michi, 2026-08-15):** T-144 found the LONG continuation the
stronger side of extreme pumpers (≥75%: +8.3 net @24h, n=55, t=1.6 —
suggestive, not proven). But these coins are violently volatile: "da benötigt
man einen riesen SL um den long zu halten." So: **how deep does a
continuation long typically get pulled under water (MAE), which SL distance
survives that, and does the edge survive the SL?**

## Pre-registered design (frozen before the first run)

**Events** — identical construction to T-144 (`tools/pump_oi_study.py`:
hourly as-of OI grid, implied price, 45-min staleness, universe floor median
OI ≥ $3M, 24h per-symbol cooldown, first-wins dedupe): M0 pump-only events
`dpx_24h ≥ PUMP_PCT`, matrix **PUMP_PCT ∈ {25, 50, 75}%**, side LONG only.

**Execution & paths** — signal at grid hour t; entry = **open of the first 5m
candle strictly after t** (causal); path = 5m candles `(entry, entry+H]`,
horizons **H ∈ {24, 48, 72}h**. Entry and path share one price source
(candles), no implied-price basis mixing. Events without a 5m candle within
15 min after t are voided, not filled.

**SL grid** — **{10, 15, 20, 25, 30, 40}%** below entry, plus the no-SL hold
baseline. SL is touched when a candle `low ≤ entry × (1 − SL)`; fill =
`min(SL price, open of the touching candle)` (gap-through fills at the worse
open, never better than the SL). Exit otherwise at the close of the last
candle ≤ H.

**Costs** — fees 0.10%/RT on every outcome. Additionally a
**funding-adjusted net** column: realized funding settlements
(`funding_rates`, as-of times inside the holding window; a LONG pays positive
rates) summed per event to its actual exit time. Slippage beyond the gap rule
is not modeled (caveat).

**Metrics per (PUMP_PCT, SL, H) cell** — n, SL-hit rate, net mean ± t, median,
WR, worst, funding-adjusted net; per (PUMP_PCT, H): the **MAE distribution**
(deepest low vs entry before H or SL-exit-free path) at quantiles
{25, 50, 75, 90} — the direct answer to "wie riesig muss der SL sein".

## Pre-registered candidate rule

A (PUMP_PCT, SL, H) cell is a **CANDIDATE** only if ALL hold:

1. n ≥ 30 events;
2. net-of-fee mean > 0 with t ≥ 2.0;
3. funding-adjusted net > 0 (the funding bill must not eat the edge);
4. the cell's net is ≥ (no-SL hold baseline net − 0.5pp) on the same
   (PUMP_PCT, H) — an SL that destroys the edge is not "protection";
5. weekly stability ≥ 60% positive weeks (weeks with ≥ 1 event, cell's H).

Anything else: NO EDGE / NOT CONCLUDABLE. Thresholds and the grid are frozen;
no post-hoc SL search beyond the matrix above.

## Known caveats (accepted up front)

- ~9 weeks, one regime — and T-144's LONG lean is in-sample from the same
  window; this study can promote it to CANDIDATE at best, never to deployment
  without a longer re-run (T-096 discipline: ≥90d before any bot exists).
- 5m lows understate intrabar wicks below 5m resolution; SL-hit rates are a
  floor, not a ceiling.
- Liquidation/leverage is NOT modeled — results are unlevered; any live use
  must keep liquidation strictly beyond the chosen SL (cap_leverage_to_sl
  rule, `core/trade_utils.py`).

# SL grid for the pump-continuation LONG — verdict (T-2026-KYT-9050-145)

_generated 2026-08-15 · read-only study · `tools/pump_long_sl_study.py` ·
events: T-144 grid (M0 pump-only, LONG), paths: `candles` 5m, funding:
`funding_rates` realized to each exit · 658 event-rows, zero voids ·
pre-registration: `pump_long_sl_study_t145_prereg.md` (own commit before the
tool)_

**Question (Michi):** the continuation long on extreme pumpers looks like the
edge, but "da benötigt man einen riesen SL um den long zu halten." How deep is
the typical shakeout, which stop survives it, and does the edge survive the
stop?

## Verdict: **CANDIDATE — LONG on ≥75% pumpers, hold 24h, SL 25–30%.**

Exactly two (adjacent) cells pass all five pre-registered rules; every other
cell fails on t ≥ 2.0.

| Cell (≥75% pump, H=24h) | n | net | t | fund-adj | SL-hit | WR | wk+ |
|---|--:|--:|--:|--:|--:|--:|--:|
| **SL 25%** | 55 | **+10.34%** | **2.26** | +10.53% | 31% | 56% | **90%** |
| **SL 30%** | 55 | **+9.92%** | **2.13** | +10.10% | 20% | 56% | **90%** |
| HOLD no SL (reference) | 55 | +7.69% | 1.51 | — | — | 56% | — |
| SL 10% (reference) | 55 | +0.60% | 0.24 | +0.65% | 75% | 25% | 30% |

## The four reads

1. **Michi's volatility objection is quantitatively exactly right.** MAE on
   ≥75%-pumper longs @24h: median **−16.7%**, p25 −28.4%. Half of all
   continuation longs go ≥17% underwater before they work. A "normal" 10%
   stop gets hit 75% of the time and reduces the trade to noise (+0.6 net);
   the stop has to be huge — 25–30% — to survive the shakeout.
2. **The huge stop is still better than no stop.** SL 25% beats the naked
   hold (+10.3 vs +7.7) because it cuts the catastrophes (worst −25 vs −72)
   while the 31% of stopped trades were mostly lost causes anyway. This is
   the first study in the family where an exit rule ADDS to the edge instead
   of taxing it.
3. **The edge is a 24h phenomenon.** At 48h/72h every cell decays (best
   t=0.94); the continuation is the first day after the signal, then the
   post-pump drift-down from T-144/M4 takes over. Enter on signal, out after
   ~24h, no bag-holding.
4. **Funding does not eat it.** Realized funding on these longs was slightly
   NEGATIVE on average (shorts crowd the pumpers), so the funding-adjusted
   net is a touch better (+10.5 vs +10.3) — the one cost line that works for
   this trade instead of against it.

## Position-size implication (not part of the verdict rule)

Unlevered numbers. With a 25% stop, liquidation must sit beyond the stop:
`cap_leverage_to_sl` (safety 0.5) allows at most **2x** on this trade — this
is a low-leverage, wide-stop, one-day trade or it is nothing. At 2x, the
candidate cell is ≈ +21% on margin per event, ~6 events/week fleet-wide.

## Caveats (why CANDIDATE, not deployment)

- **n=55, ~9 weeks, one regime** — and the motivating examples sit inside the
  window. T-096 discipline applies: re-run at ≥90d history before any live
  arm; a shadow leg (monitored trades, no Cornix) is the correct next step.
- The two qualifying cells are adjacent (25/30%) — robustness hint, but the
  grid is coarse; treat 25–30% as a band, not a point estimate.
- 5m lows understate sub-5m wicks: real SL-hit rates are ≥ the reported ones.
- Entry at next-5m-open assumes the signal is acted on within minutes.

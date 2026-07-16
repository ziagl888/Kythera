# K7 · SKW1 — realized-skewness study (T-2026-CU-9050-141)

> **SMOKE — full run pending.** Produced with symbol/week caps to prove the builder +
> study run end to end. The full-universe report is deferred to the queue (Ein-Job-Regel:
> a second heavy study must not run while another is live).

_Generated 2026-07-16T15:55:35.985134+00:00 · read-only · limit_symbols=3 · 
max_weeks=6 · coins_loaded=3 · weeks=6_

Realized SKEWNESS sort (not MAX/lottery — §K7 F6). Market-neutral (coin − BTC), bottom
dollar-volume tercile dropped per week, funding on the short leg, taker fees both legs.
Survivorship-biased (active USDT-perps only).

## SKW1 long/short spread (LONG low-skew decile, SHORT high-positive-skew decile)

- no week reached MIN_COINS_PER_WEEK (smoke caps)

## Decile sorts — mean market-neutral forward return (byproduct incl. RV/kurtosis)

### `mom_skew_7d` (0 weeks ≥ 20 coins)

_no week reached the coin minimum (smoke caps) — deciles empty._

### `mom_skew_24h` (0 weeks ≥ 20 coins)

_no week reached the coin minimum (smoke caps) — deciles empty._

### `mom_rv_7d` (0 weeks ≥ 20 coins)

_no week reached the coin minimum (smoke caps) — deciles empty._

### `mom_kurt_7d` (0 weeks ≥ 20 coins)

_no week reached the coin minimum (smoke caps) — deciles empty._

## Caveats

- **SMOKE run**: caps make the numbers non-decisive; the stop-criterion verdict (§K7) is
  the FULL run's job. The moment feature-block stays a retrain option regardless (§K7).
- **Survivorship**: cross-section over active USDT-perps only; delisted coins missing.
- **Funding**: coins without funding history contribute 0 (documented, not imputed).
- Realized moments from 15m closed bars only (R1); native NaN, never fillna(0) (P1.20).
# Pre-Registration: pump-conditioned OI study (T-2026-KYT-9050-144)

_registered 2026-08-15 · BEFORE any outcome was computed · tool:
`tools/pump_oi_study.py` · data: `oi_5m` 2026-06-12 → run date (~9.2 weeks)_

**Observation (Michi, 2026-08-15):** the manual OI+volatility combination (4h
chart, OI pane, RVI) works "really well" — but only on coins that pumped at
least ~25% within the last 24h. Examples: ACEUSDT, APRUSDT, BRUSDT (all
2026-08-12…15: pump ≥60%, OI spike, then a hard retrace).

**Prior art:** T-096 REFUTED unconditioned spike-fade (mean −2.46 @24h,
median +0.98 — tail-driven), and found DIVERGENCE·SHORT the only survivor.
This study asks a NARROWER question: does conditioning on a large 24h pump
flip the sign — i.e. is the pump filter what separates Michi's working manual
trades from the refuted naive fade?

## Pre-registered design (frozen before first run)

Method identical to T-096 (`tools/oi_event_study.py`): hourly as-of grid,
45-min staleness cap, implied price `oi_value_usdt/open_interest`, universe
floor median OI ≥ $3M, strictly causal features, fees 0.10%/RT, 24h
per-symbol per-mechanic cooldown, first-wins dedupe.

- **Forward horizons:** 4h / 8h / 24h.
- **Pump condition (the new filter):** `px / px_24h_ago − 1 ≥ PUMP_PCT`,
  matrix PUMP_PCT ∈ {25%, 50%}.
- **OI features on the 4h window** (operator ask: "OI auf 4h Zeitframe"),
  evaluated on the hourly grid; the 24h cooldown collapses persistent
  conditions to one event.

Mechanics — all conditioned on the pump filter; each is read BOTH ways
(SHORT = fade the pump, LONG = ride the continuation):

| # | Mechanic | Condition on top of pump |
|---|---|---|
| M0 | PUMP-ONLY (control) | none — separates "big pumps mean-revert anyway" from OI added value |
| M1 | +OI-SPIKE-4h | `doi_4h ≥ +10%` (fresh money still entering) |
| M2 | +OI-ROLLOVER | 24h OI build `peak_24h/oi_24h_ago − 1 ≥ +25%` AND off-peak `oi/peak_24h − 1 ≤ −5%` (money leaving after the build — the APR/BR chart pattern) |
| M3 | +OI-STALL-4h | `doi_4h ≤ 0%` (pump no longer fed by new positions — T-096 divergence, pump-conditioned) |

## Pre-registered verdict rule

A mechanic/side/threshold cell is a **CANDIDATE** only if ALL hold:

1. n ≥ 30 events;
2. net-of-fee mean > 0 on ≥ 2 of 3 horizons with t ≥ 2.0;
3. weekly stability: ≥ 70% of calendar weeks with events are positive (4h horizon);
4. it beats the M0 pump-only control on the same horizon by ≥ +0.10%-points
   net (otherwise the OI leg adds nothing and the finding is "pumps
   mean-revert", not "OI+vol works").

Anything else: NO EDGE / NOT CONCLUDABLE. No per-coin tuning, no post-hoc
threshold search beyond the frozen matrix above.

## Known caveats (accepted up front)

- ~9.2 weeks, one market regime; implied-price returns, no slippage model.
  Same limits as T-096 — a CANDIDATE here seeds a shadow leg, not a live bot.
- The three motivating examples are in-sample; the verdict counts only the
  full-universe event population.

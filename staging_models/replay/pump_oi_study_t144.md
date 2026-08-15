# Pump-conditioned OI study — verdict (T-2026-KYT-9050-144)

_generated 2026-08-15 · read-only study · `tools/pump_oi_study.py` · data: `oi_5m`
2026-06-12 → 2026-08-15 (~9.2 weeks, 6.59M points) · universe: 237 symbols with
median OI ≥ $3M · hourly as-of grid, 24h per-symbol cooldown · fees 0.10%/RT ·
pre-registration: `pump_oi_study_t144_prereg.md` (thresholds + candidate rule
frozen before the first run)_

**Question (Michi, 2026-08-15):** the manual OI+volatility combo works "really
well" — but only on coins that pumped ≥25% in 24h (ACE/APR/BR). Does the pump
filter flip the sign of the T-096-refuted spike-fade? Bonus (BLUAI): after the
collapse, does the coin pump back or keep falling?

## Verdict: **NO EDGE under the pre-registered rule — no cell qualifies.**

Not a single mechanic/side/threshold cell reaches t ≥ 2.0 on ≥ 2 horizons, and
only one cell (M1·SHORT @50%) clears the 70%-weeks bar at all. The candidate
rule (prereg §verdict) fails everywhere. Numbers below are net of fees,
%-points per event.

| Cell (pump ≥25%) | n | net 4h | 8h | 24h | 48h | 72h | wk+ |
|---|--:|--:|--:|--:|--:|--:|--:|
| M0 PUMP-ONLY · SHORT (control) | 465 | +0.03 | +0.11 | −0.61 | −0.29 | −0.23 | 40% |
| M1 +OI-SPIKE · SHORT | 176 | +0.07 | +0.18 | **−4.63** | −1.83 | −1.55 | 50% |
| M2 +OI-ROLLOVER · SHORT | 87 | +0.73 | +0.84 | −3.70 | −1.74 | −4.14 | 60% |
| M3 +OI-STALL · SHORT | 286 | +0.88 | +1.01 | −0.04 | +0.73 | −0.18 | 50% |
| M4 POST-COLLAPSE · SHORT | 78 | −0.29 | +1.33 | +3.16 | +2.26 | +2.84 | 44% |

(@50% pump the same picture, thinner and wilder: M2·SHORT@50% is −15.9 @24h,
t=−1.96, n=34 — the rollover pumps tended to KEEP RUNNING, hard.)

## The five honest reads

1. **The pump filter does NOT flip the spike-fade sign.** Fading fresh OI on a
   pumped coin is the worst cell in the table (M1·SHORT −4.6 @24h) — fresh
   money entering a pump means continuation, exactly as T-096 found without
   the filter. Michi's three chart examples are survivors of a selection the
   full population does not support.
2. **The median-vs-mean gap explains why the manual combo FEELS great.**
   M0·SHORT @24h: median **+5.7%**, WR 63% — but mean −0.5%. Shorting a
   ≥25%-pumper wins roughly two times out of three; the third time the coin
   does an ACE (+100% more) and one uncapped loss eats ten median wins. A
   discretionary trader remembers the 63%; the book pays the tail.
3. **The only SHORT cell with the right shape is M3 (OI-stall) at 4–8h:**
   +0.88/+1.01 net, beats the control by +0.85/+0.90pp — same direction as
   T-096's divergence survivor, but t ≤ 1.3 here: suggestive, not evidence.
   If anything survives a longer history, it is this: short the pump only
   when the 4h OI feed has STOPPED, and be out within hours, not days.
4. **M4 (the BLUAI bonus question): collapsed pumps drift LOWER, they do not
   pump back.** LONG is negative on every horizon ≥ 8h (−1.5 @8h … −3.0
   @72h); SHORT-continuation is positive (+3.2 @24h) but t ≤ 1.0 and only
   44% of weeks positive. Direction: "fällt weiter / bleibt unten" — but as a
   lean, not a tradeable claim.
5. **Tail risk is the design problem, not the entry.** Every SHORT cell has
   WR ≥ 51% and a positive median somewhere — and a mean dragged negative by
   uncapped continuation tails. An entry study cannot fix that; an EXIT
   design (hard SL / time-stop, cf. the bot-40 lesson in
   `trailing_book_health.md`) is where this pattern would have to earn its
   keep. That would be a NEW pre-registered study (SL-grid on these same
   events), not a re-reading of this one.

## Caveats

- ~9.2 weeks, one regime, implied-price marks, no slippage; on ≥25%-pumpers
  the implied mark is noisier than on the T-096 universe.
- n per cell shrinks fast at @50% (34–137) — the wild @50% numbers are
  tail-driven, read them as variance, not signal.
- The three motivating charts (ACE/APR/BR) are in-sample survivors of the
  same week the study ends in.

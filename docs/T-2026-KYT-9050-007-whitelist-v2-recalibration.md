# Recalibrating whitelist_v2: verdict NO-GO (T-2026-KYT-9050-007)

**Brief (Michi, 2026-08-02):** don't flip v2 and don't leave it as is, but
**recalibrate** it — change the Wilson bound and the break-even threshold and
measure again against the realised forwards. The ticket's Stop-B applies: if
v2 shows no measurable added value, v1 stays.

**Result: Stop-B applies. No parametrisation of v2 survives
out-of-sample.** The gate stays on v1. No flip, no restart, no
write query.

Tool: `tools/whitelist_v2_recalibration.py` (read-only). Runs:
`staging_models/replay/whitelist_v2_recalibration_2026-07-11.md` (in-sample) and
`…_oos_pre-2026-07-03.md` (out-of-sample).

---

## 1. The proposed lever is the wrong one

Across 45 configurations (z × k × break-even) on 1,590 cells:

| Lever | Movement of the open rate |
|---|---|
| Break-even 0.1 → −0.1 | +1.7 pp |
| Shrinkage k 25 → 5 | +1.3 pp |
| **z 1.64 → 0.67** | **+10.0 pp** |
| **z 1.64 → 0** | **+29 to +47 pp** |

v2's strictness sits almost entirely in the **z multiplier of the lower
bound**. Break-even and shrinkage move the gate by one to two percentage
points — both are the levers named in the brief, and both are
ineffective.

Second finding from the same table: **even at the most permissive end, v2
opens only ~53% of cells against v1's 94%.** The measured −55% throughput loss
from PR #239 is not a tuning artefact, it's structural.

## 2. In-sample, one region looked good

Window 2026-07-11 → 08-02, 8,367 forwarded events with a ROM1 leg,
v1 reference **avg +0.0329%/trade** (Σ +108.1%).

| Configuration | Pass rate | avg kept | avg blocked | Reading |
|---|---:|---:|---:|---|
| z 1.64 / k 25 / be 0.1 **(today)** | 6.8% | +0.212 | +0.013 | removes winners |
| z 0.67 / k 10 / be 0.1 | 13.9% | **+0.558** | **−0.076** | removes losers |
| z 0.00 / k 25 / be 0.1 | 25.5% | +0.396 | −0.128 | removes losers |

Read as a backtest, this would be a hit: doubled throughput, tripled kept
expectation, and what got removed was negative. Which is exactly why it's
shown here only as an intermediate step.

## 3. Out-of-sample inverts it — completely

Window 2026-04-18 → 07-03 (ends **before** the 30-day fit window of the
cell statistics), 4,356 forwarded events with a ROM1 leg, 99.9% leg coverage,
v1 reference **avg +0.6886%/trade** (Σ +1,899.1%).

| Configuration | Pass rate | avg kept | avg blocked | Σ blocked | Reading |
|---|---:|---:|---:|---:|---|
| z 1.64 / k 25 / be 0.1 (today) | 3.4% | +4.369 | **+0.554** | **+1,475.3** | removes WINNERS |
| z 0.67 / k 10 / be 0.1 | 5.3% | +2.328 | **+0.594** | **+1,547.6** | removes WINNERS |
| z 0.00 / k 25 / be 0.1 | 5.6% | +2.333 | +0.589 | +1,530.5 | removes WINNERS |

**42 of the 45 configurations remove winners.** Across the whole grid, the
mean of the blocked legs sits at **+0.55 to +0.60%/trade** — v2 would have cut
roughly **80% of this window's realised ROM1 profit** in every parametrisation,
and kept 3–6% of the volume in return.

The three exceptions (`z 0 / be −0,1`) keep **95%** of the traffic: a gate
that effectively doesn't gate, with a blocked Σ of −8 to −24% — noise.

**The region found in-sample reverses.** `z 0,67 / be 0,1` goes from
"removes losers" (avg blocked −0.076) to "removes WINNERS" (avg blocked
+0.594). That's not a weaker result, it's the opposite one.

## 4. Why in-sample looked so good

A selection effect, and the strongest conceivable one. The cell statistics come
from the last 30 days; the in-sample scored legs sit **inside exactly that
window**. A cell passes the gate because its most recent trades did well —
and those same trades then get counted as evidence. The further z is opened,
the more such self-confirming cells get added, and the kept expectation
appears to rise.

That's the same class of error PR #239 named on the trigger leg, here just
via the detour of parametrisation. The out-of-sample run is the counter-check,
and it comes out unambiguous.

**New compared to PR #239:** this out-of-sample run wasn't possible there.
The flip evaluation needs `orchestrator_open_trades.wl_reason`, which is only
populated from early July onward; before the fit window there were zero
usable events. This tool decides the cell **anew** from `bot_regime_performance`
and doesn't need `wl_reason`; that makes the 4,359 forwards from April to
early July evaluable. The original report's "zero out-of-sample" gap is
closed — with a negative finding.

## 5. What this result is NOT

Not a backtest. `bot_regime_performance` is a **snapshot** — measured on every
run and stated in the report: **0 cells** with more than one row. The
statistics the gate decided on back then no longer exist. Both runs use
**today's** cell statistics; they differ in whether the scored trades lie
inside the fit window or not. For a leakage test that is exactly the right
split — for a rollout justification it is not enough.

Second limitation: the two windows have very different baseline levels
(avg +0.033 against +0.689%/trade). They come from different market phases;
the absolute amounts are not directly comparable. The **sign** of the blocked
side is.

Third: only the forwarded side is scored. Suppressed signals by construction
have no ROM1 leg — their outcome is unobserved, not zero.

## 6. Recommendation

1. **v1 stays.** The gate is not flipped and not recalibrated. Stop-B is
   satisfied: v2 shows no measurable added value, in any parametrisation.
2. **Don't touch break-even and shrinkage further** — as levers both proved
   ineffective, and lowering the break-even is harmful across both windows.
3. **Historicise `bot_regime_performance`** (one snapshot row per day).
   As long as the cell statistics get overwritten, no gate variant can ever be
   cleanly checked against its own past — the leakage test above is the best
   achievable approximation, not the right test. Logged as a follow-up task.
4. If v2 is pursued later after all: **only via a live shadow A/B**, as T-031
   established for the SOFT-regime gate.

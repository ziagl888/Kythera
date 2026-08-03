# Intake analysis bot 40 — why more trades aren't landing (T-2026-KYT-9050-060)

**Task (Michi, 2026-08-01):** the trade count should go up — but nothing gets
changed before a full analysis is in.

**Answer in one sentence:** the bottleneck is **not** the freshness window, but
the **exposure cap** — and because the cap acts on the *difference* between the
directions, the real lever is the **SHORT side**, not the LONG side where the
conspicuous rejections sit.

Measurement window: 2026-07-30 08:00 (first full day after the 240s
recalibration, restart 07:28) to 2026-08-01. Tool: `tools/trailing_intake_audit.py`
(read-only).

---

## 1. Why a single-gate analysis is wrong here

A candidate must pass **five stages**. Only two leave a row in
`trailing_positions` — the rest exist only as a counter in the fleet log. Anyone
measuring only against the DB therefore systematically sees the wrong gate.

| Stage | Gate | Trace | measured |
|---|---|---|---|
| 1 | roster · `shadow_gate.leg_status` · entry · SL/targets | none | 31 legs, **all live** |
| 2 | freshness window 240 s | **DB** `PREEXISTING` | 707 LONG / 24 SHORT |
| 3 | `SYMBOL_HELD` | log | avg 1.6 → 2.8 candidates/cycle |
| 3 | `SYMBOL_COOLING` | log | negligible (≤ 5 cycles/day) |
| 3 | **`EXPOSURE_CAP`** | log | **avg 3.2 → 6.0 → 6.6, max 28** |
| 3 | `SLOT_CAP` (500) | log | **never triggered** |
| 4 | no market price · `mirrorable_at` | log | ~93 events total |
| 5 | entry never touched | **DB** `ENTRY_NOT_FILLED` | 46 total |

`SLOT_CAP` hasn't fired **a single time** in three days: the Cornix channel is
nowhere near full. Slot scarcity isn't the problem, directional balance is.

---

## 2. The window looks like the bottleneck — but isn't

The rejected signals sit in a **narrow band right behind the boundary**:

| Direction | n | p10 | p50 | p90 | admissible at 300 s |
|---|---:|---:|---:|---:|---:|
| LONG | 707 | 243 s | 249 s | 256 s | **706** |
| SHORT | 24 | 243 s | 255 s | 593 s | 18 |

That's not an age distribution, that's a **wall** — the same pattern as back at
180 s, one step later. The 240 s were calibrated to a measured ~190 s pipeline
latency of the candle-cycle leg family; that latency now sits at ~250 s. The
boundary cuts right through the latency distribution of **one leg family**, and
that one is almost pure LONG (707:24).

**Adverse selection ruled out:** the rejected LONGs deliver, in the source
trade, **avg +2.39%** against **+1.28%** for the admitted ones (n = 378 vs 80,
sd 8.1 → t ≈ 1.3, so no significant difference). The boundary does **not**
select the better signals — widening the window doesn't buy worse merchandise.

**Even so, widening alone buys almost nothing in volume** — see section 3.

---

## 3. The binding bottleneck: the exposure cap

`admit()` rejects a direction as soon as it leads the opposite direction by
`EXPOSURE_CAP` (50) open mirrors. The book has been stuck against this ceiling
since the fix:

| Time | LONG | SHORT | Imbalance | LONG headroom |
|---|---:|---:|---:|---:|
| 07-30 08:00 | 56 | 8 | +48 | 2 |
| 07-31 02:00 | 74 | 32 | +42 | 8 |
| 08-01 02:00 | 78 | 30 | +48 | 2 |
| 08-01 14:00 | 73 | 21 | **+52** | **0 — LONG blocked** |

Across the whole period, LONG headroom sits between **0 and 8**. LONG
candidates are thus already rejected **after** passing the freshness test.
Widening the window further just moves rejections from `PREEXISTING` to
`EXPOSURE_CAP` — the volume barely moves.

### The identity everything hangs on

The cap bounds the **difference**, not the sum. With the ceiling engaged:

```
Gesamtkapazität  =  2 × min(LONG, SHORT)  +  Cap
```

Currently: 2 × 21 + 50 = **92 positions**. **Every additional SHORT position
raises the LONG ceiling by one** — so the SHORT side throttles the **total
volume**, completely independent of how many LONG candidates are queued up.

### The grandfather cohort pays straight into this bottleneck

**28 of the 30 timestop-exempt mirrors are LONG.** They never close (never
sharply, so unreachable for the trail) and thereby permanently occupy **28 of
the 50 units** of LONG headroom — 56%. They also block 28 symbols against
`SYMBOL_HELD`. The 2026-08-01 decision (#T54-3, "they keep riding") is thus
more expensive than known at the time it was made: it costs not just −81% open
book, but **more than half of LONG throughput**.

---

## 4. Levers, ranked by effect

| # | Lever | expected effect | risk | effort |
|---|---|---|---|---|
| **A** | **put TSM1 SHORT into the roster** | +~30 fills/day SHORT → +~22 standing SHORT → capacity **92 → ~150** | low | 1 roster line + restart |
| B | dissolve the grandfather cohort | +28 LONG headroom **immediately**, +28 free symbols | realising Σ −81% | operator decision, no code |
| C | window 240 → 300 s | volume ~0, but **better selection** (see below) | low | 1 default + pins + restart |
| D | raise `EXPOSURE_CAP` | direct more LONG | **high** | operator decision |

**On A:** TSM1 SHORT produces **66 signals/day**, is **live**, has density 525 —
and was discarded in the original selection solely because of the **slot cap**,
which has **never** bound since. That's the cleanest measure on the table: it
attacks exactly the side that throttles total volume.

**On C:** even without a gain in volume, the window isn't worthless. `admit()`
ranks candidates by **leg density**. A 300 s window offers the same LONG budget
roughly **five times as many** candidates to choose from — the occupied slots
then go to denser legs. The gain is quality per slot, not quantity. **That's why
C belongs after A/B**, not before: create headroom first, then fill it better.

**On D — explicitly not recommended:** T-052 measured that the **one-sided LONG
book was the account damage** and the structural constraint beat every regime
model. Raising the cap reopens exactly that door. If at all, only after a
dedicated study, not as a side effect of a throughput measure.

---

## 5. Honest limits of this analysis

- **The `PREEXISTING` figures are a lower bound and shrink over time.** The age
  at rejection can only be computed while the source trade is still in
  `ai_signals`; once the fleet closes it, the row moves to `closed_ai_signals`
  and the join loses it. Two runs on the same day therefore produced 767 and
  later 707 LONG. For the question "does the rejection sit just behind the
  boundary" this is harmless (the distribution stays the same), for "how many
  in absolute terms" it isn't — there the number is conservative.
- **The log gates are pressure, not counts.** Rejections repeat every 10s
  cycle as long as the source trade stays open. "avg 6.6 EXPOSURE_CAP" means
  "at any given moment ~6.6 candidates are stuck at the ceiling", **not**
  "6.6 signals/day lost". A distinct count would only be possible via DEBUG
  logs, which aren't written.
- **The effect estimate for A is an extrapolation**, not a measurement: it
  assumes the same signal→fill conversion (~45%) and the same holding time
  (~0.73 days) for TSM1 as for the existing SHORT legs. A leg with a different
  time profile shifts the result.
- **The holding time is computed from L/λ** (96 open / 131 fills per day), not
  from the mean of closed trades — the latter is survivorship-biased (0.35
  instead of 0.73 days), because the long positions are still open and thus
  missing from it.
- **The quality probe in section 2 measures the source trade**, not the arm
  exit. As a proxy for "is the rejected merchandise worse" it holds; as a
  return forecast it doesn't.
- **Not measured:** whether the 4 legs discarded due to the slot cap (EPD3
  LONG, BR2H LONG, TSM1 SHORT, BB_1H LONG) would be assessed differently under
  today's conditions — the selection calculation is from 2026-07-26 and the
  slot cap no longer binds.

---

## 6. Recommendation

1. **Implement A** (put TSM1 SHORT into the roster) — attacks the actual
   bottleneck, low risk, one roster line. Own task, effective after a fleet
   restart.
2. **Decide B** (Michi): the grandfather cohort costs more than half of LONG
   headroom. The 2026-08-01 decision was made without this number — it isn't
   thereby wrong, but it needs re-evaluating.
3. **C afterwards**, as a quality, not a volume, measure.
4. **D not without its own study.**

Before any of these changes, the rule still stands: **no live intervention from
a dev session** — roster change and window default are PRs, effective only
after a restart by Michi.

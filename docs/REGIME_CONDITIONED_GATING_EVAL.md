# Regime-conditioned gating — evidence & recommendation (T-2026-CU-9050-125, part 3)

**Michi's question:** For ROM (bot 28) and AIM (bot 15), sources/models are
gated across the board. But a bot that is negative over the ENTIRE PERIOD can
run positive in the RIGHT market phase (regime). Are there such sources — and is
a **regime-dependent** gate worth it instead of a blanket off?

**Short answer:** **Yes, the phenomenon is real** — but the tool for it is
already built. The right answer isn't "new gate" and isn't "blanket
off", but **switching on the already-built v2 EB-shrinkage whitelist
(T-2026-CU-9050-069) on fresh data**, plus visibility in the AIM2 report.
**No live change in this PR** — evidence only.

Reproducible (read-only): `tools/regime_conditioned_gating_scan.py [--window 90|30] [--json]`.

---

## 1. Method (read-only, no replays needed)

The hourly analyzer `27_bot_regime_analyzer.py` already materializes everything:

- **`bot_regime_performance`** — `avg_pnl_pct` / `win_rate` / `n_trades` per
  `(bot_name, regime, alt_context, direction, window_days)`. The row
  `(regime='ALL', alt_context='ALL')` is the GLOBAL expectation of a leg.
- **`bot_regime_whitelist`** — per `(bot, regime, alt_context, direction)` the v1
  gate (`whitelisted`) AND the shadow-v2 gate (`whitelisted_v2` / `reason_v2`).
  `reason_v2` carries the **EB-shrinkage lower bound** (`lb`), the point
  estimate (`est`), the source (`src`, cell vs. bot×regime level) and the
  effective n (`neff`).

"Globally negative, but regime-positive" = a leg with `ALL/ALL avg_pnl_pct < 0`
that has `avg_pnl_pct > 0` in a regime cell. "Robust" = the cell survives
the v2 lower bound (`lb > 0`), i.e. the positive average is still distinguishable
from zero after shrinkage + minimum n.

> **Data-currency caveat:** `bot_regime_performance`/`-whitelist` were last
> computed on **2026-07-13 04:06** — shortly BEFORE the ~14h ingestion
> outage of that day, i.e. ~1 day old and with a thinned-out most-recent window.
> Before any flip the tables must be recomputed on fresh data (check bot-27
> uptime — see `kythera-regime-orchestrator`).

---

## 2. Finding A — the point-estimate decoy (why "just turn it on" loses money)

On the raw regime means a lot looks tempting. Example
**ATS1-LONG**: global −0.01%, but in `TRANSITION` **+1.47%/trade (n=258)**.

But split by `alt_context` and shrunk (v2), it flips:

```
ATS1 LONG  TRANSITION/ALT_NEUTRAL   est=+1.45%  lb=-0.263  -> v2_block
ATS1 LONG  TRANSITION/ALT_STRONG    est=+2.41%  lb=-2.462  -> v2_block
ATS1 LONG  TRANSITION/ALT_WEAK      est=+2.24%  lb=-1.491  -> v2_block
```

The point estimate is positive, the **lower bound stays negative** — at this n
and variance the positive average cannot be told apart from noise (fat-tailed
individual winners; several such cells have sub-50% WR at a "positive" mean).
A naive "regime on because the mean is positive" would trade exactly these
noise cells and lose. **v2 correctly shrinks them away.** This validates the
v2 design and is the core of the answer: the mean alone is worthless as a gate
criterion (MODEL_INTENT rule 3).

---

## 3. Finding B — the DEFENSIBLE cells (18)

**18 regime cells sit under a globally negative leg and survive v2**
(`lb > 0`, window 90d). These are the sources where a blanket-off actually
leaves money on the table. Excerpt (full: tool output):

| Leg (global) | Regime / alt | est | **lb** | neff |
|---|---|---|---|---|
| BR1H-LONG (−0.06%) | HIGH_VOLA / ALT_WEAK | +1.79% | **+1.39%** | **1505** |
| BR2H-LONG (−0.23%) | HIGH_VOLA / ALT_WEAK | +1.32% | +0.71% | 681 |
| EPD1-LONG (−0.32%) | TRANSITION / ALT_STRONG | +7.86% | **+4.21%** | 47 |
| RUB2-SHORT (−0.49%) | CHOP / ALT_NEUTRAL | +1.40% | +0.38% | 45 |
| RUB2-SHORT (−0.49%) | HIGH_VOLA / ALT_NEUTRAL | +3.20% | +0.24% | 37 |
| EPD2-SHORT (−0.04%) | CHOP/HIGH_VOLA (6 cells) | +1.9…3.8% | +1.5…2.7% | 25–35 |
| MIS1-8h-LONG (−0.03%) | HIGH_VOLA / ALT_WEAK | +3.19% | +3.03% | 27 |
| QM_1H/QM_4H-SHORT | HIGH_VOLA / CHOP | +0.6…1.0% | +0.2…0.5% | 25–27 |
| ATB1-SHORT (−0.77%) | TRANSITION / ALT_STRONG | +1.16% | +1.16% | 27 |
| SR-LONG (−0.19%) | HIGH_VOLA / ALT_STRONG | +0.11% | +0.11% | 27 |

Notable: `BR1H-LONG / HIGH_VOLA·ALT_WEAK` with **neff=1505** and lb +1.39%
is statistically very solid; `EPD1-LONG / TRANSITION·ALT_STRONG` with lb +4.21%
is economically large. Of all 132 v2-pass cells, 85 are SHORT / 47 LONG — so
there are robust LONG regime cells too (mainly HIGH_VOLA/TRANSITION), despite
the short-leaning overall picture.

---

## 4. Interpretation — the tool already exists

- **ROM (bot 28)** reads `bot_regime_whitelist` per `(bot, regime, alt_context,
  direction)`. The v2 EB-shrinkage gate IS thereby exactly a regime-conditioned
  expectancy gate — it just isn't read LIVE yet (v1 is live, v2 is a shadow
  column). **Switching on the T-069 flip v1→v2 = activating the
  regime-dependent gating.** This analysis is strong additional evidence FOR that.
- **AIM (bot 15)** does NOT use the whitelist — it's a meta-model with ONE
  global threshold; the regime one-hots are features but collapse onto one
  threshold. `master_meta_model_aim2_report.json::per_source_test` is also
  pooled ONLY across all regimes. AIM's fix is therefore **visibility**: a
  `per_source × regime` cross-table in the AIM2 report (`groupby(["source", regime])`
  over the same `te_meta` frame in `tools/aim2_train.py` — the regime
  one-hots are already in `X`). A regime-conditioned threshold would be a
  model change (its own task), not a prerequisite.

---

## 5. Recommendation

1. **No new gate, no blanket off.** A blanket-off leaves the 18
   robust cells on the table; a naive regime-on trades the decoy cells (finding
   A). The disciplined middle path — regime × alt × direction with EB shrinkage —
   **is already built as the v2 whitelist.**
2. **Push the T-2026-CU-9050-069 flip v1→v2**, but **on freshly computed
   tables** (data-currency caveat §1). The eval tooling is built
   (`tools/whitelist_v2_flip_eval.py`, PR #116); this scan supplies the
   substantive justification.
3. **Extend the AIM2 report with a `per_source × regime` cross-table** (cheap,
   `tools/aim2_train.py`) — makes AIM's regime behaviour visible at all in the first place.
4. **Coupling to part 1 (shadow mode):** exactly the shadowed/suppressed
   legs now supply the regime-conditioned trade records with which their
   later unlocking can be justified. Shadow posting and regime-conditioned
   gating are two ends of the same idea.

Residual risks: data-currency staleness (§1); the `alt_context` split is
decisive (the `ALL/ALL` roll-up overstates things — the decoys in finding A);
sub-50%-WR cells are already excluded by the lb condition; the ROM whitelist
covers the bots re-forwarded by ROM, not the entire AIM source universe.

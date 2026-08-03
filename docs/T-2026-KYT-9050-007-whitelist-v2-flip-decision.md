# whitelist_v2 flip — decision basis for Michi (T-2026-KYT-9050-007)

**As of:** 2026-08-01/02 · **Measurement:** `tools/whitelist_v2_realized_eval.py` against the live DB, strictly read-only · **Raw reports:** `staging_models/replay/whitelist_v2_realized_eval_*.md` + `*_summary.json`

**The flip has not been made and is neither recommended nor rejected here** — `28_signal_orchestrator.get_whitelist_decision` still reads `whitelisted` (v1) unchanged. This document delivers the numbers the decision hinges on, and states what the numbers do **not** support.

---

## 0. The short version in five sentences

1. The flip is not a fine-tuning adjustment: it blocks **87.7% of all whitelist cells** and cuts ROM1 throughput from **377 to 168 forwards/day (−55%)**.
2. On the leg the gate uses to decide (trigger bot), v2 looks good — **but that very leg is exactly what v2 was fitted on**; that is not independent evidence.
3. On the leg that carries the money (ROM1), the effect is **≈ null and unstable in sign**: the signals v2 would additionally block realised **+2.0%** as ROM1 over 21 days (1,342 decided trades) and **−61.6%** over the last 7 days.
4. The "v2 additionally opens" side hinges on **3 cells** and effectively **one leg (AIM2-SHORT)** — and is **fundamentally not measurable** in ROM1 money, because these signals were never traded.
5. **Out-of-sample there is not a single reliable data point** (§5) — not few, but none. Whoever wants to base the flip on evidence first needs a measurement that doesn't exist today (§7).

---

## 1. What the flip mechanically changes

`get_whitelist_decision` swaps exactly one column read: `SELECT whitelisted` → `SELECT whitelisted_v2`. All other gate paths (`no_whitelist_entry`, `whitelist_stale:*`, `regime_is_transition:*`, `regime_unstable:*`) are identical — they run via `is_whitelisted_fallback` and don't know the 4D cell.

**Cell matrix, snapshot 2026-08-01 22:08 UTC (1,590 cells, v2 coverage 100%, analyzer fresh, age 0.3 h):**

| | v2 pass | v2 block | Σ |
|---|---:|---:|---:|
| **v1 open** | 94 | **1,395** | 1,489 |
| **v1 block** | **3** | 98 | 101 |

- v1 open: **93.6%** of cells · v2 open: **6.1%**.
- The premise "the whitelist is ~89% default-open" is **confirmed**: 1,410 of 1,590 cells (**88.7%**) carry `insufficient_data`, i.e. v1's default-open crutch (n < 30).
- Of the 1,395 cells v2 additionally blocks, **1,335 are exactly these crutch cells** and **60 are v1 decisions on merit** (`wr_above_overall`/`counter_trend_specialist`).
- The **three** cells v2 would additionally open, namely:

| Bot | Regime | Alt | Dir | v1 reason | v2 reason |
|---|---|---|---|---|---|
| AIM2 | TREND_UP | ALT_NEUTRAL | SHORT | counter_trend_insufficient | `v2_pass:lb=0.912:est=2.515:src=cell:neff=124` |
| QM_4H | HIGH_VOLA | ALT_WEAK | LONG | wr_below_overall | `v2_pass:lb=1.143:est=2.432:src=cell:neff=117` |
| SRA2 | CHOP | ALT_NEUTRAL | SHORT | wr_below_overall | `v2_pass:lb=0.488:est=1.131:src=cell:neff=163` |

---

## 2. How many real signals does this affect? (window A: 2026-07-11 → 2026-08-01)

22,660 recorded gate events, of which **14,234 cell-decided** (the rest runs via fallback paths the flip doesn't touch).

| | Events |
|---|---:|
| v2 would **additionally block** (`v2_would_block`) | **4,848** |
| v2 would **additionally pass** (`v2_would_open`) | **264** |
| both open | 316 |
| both blocked | 8,806 |

- Gate open rate on cell-decided traffic: **36.28% → 4.07%**.
- ROM1 forwards/day incl. the unchanged fallback floor: **377.0 → 168.1 (−55%)**.

**Correcting an obvious misreading.** From "89% of cells are default-open" it does *not* follow that v2 mainly removes the crutch. On **traffic** it's the reverse:

| v1 path of the additionally blocked events | Events | Share |
|---|---:|---:|
| `wr_above_overall` (decision on **merit**) | 3,964 | **81.8%** |
| `insufficient_data` (default-open **crutch**) | 880 | 18.2% |
| `counter_trend_specialist` | 4 | 0.1% |

The crutch cells are numerous but carry little traffic. **The flip predominantly overrides decisions v1 made on a data basis** — it doesn't just clean up empty cells.

---

## 3. What exactly did these signals realise?

Two measures, deliberately kept apart (details: `docs/WHITELIST_V2_REALIZED_EVAL.md`):

- **Trigger leg** — the source bot's own trade, scored by the monitor. Exists on **both** gate sides (a blocked signal still ran in the bot's own channel) → the only symmetric measurement.
- **ROM1 leg** — the trade the orchestrator actually opened. **The real money**, but only on the forwarded side.

Shown is the **clean subset** (`v1_agree`: today's v1 cell still matches the recorded gate decision). Where it doesn't match, the cell has since moved, and the "divergence" compares two v1 states instead of v1 against v2 — those events stand aside, not inside.

### Window A (2026-07-11 → 2026-08-01, 21.9 days · drift 69.9%)

| Class | Leg | Subset | Events | censored | decided | WR % | Σ move % | avg net %/trade |
|---|---|---|---:|---:|---:|---:|---:|---:|
| v2_would_block | Trigger | **v1_agree** | 3,461 | 0 | **3,160** | 66.6 | **−274.9** | **−0.187** |
| v2_would_block | Trigger | v1_drifted | 1,387 | 0 | 1,346 | 59.3 | −1,000.4 | −0.843 |
| v2_would_block | **ROM1** | **v1_agree** | 3,461 | **2,010** | **1,342** | 81.2 | **+2.0** | **−0.099** |
| v2_would_block | ROM1 | v1_drifted | 1,387 | 915 | 469 | 82.5 | +64.0 | +0.037 |
| v2_would_open | Trigger | **v1_agree** | 124 | 0 | **88** | 86.4 | **+130.8** | **+1.386** |
| v2_would_open | Trigger | v1_drifted | 140 | 0 | 137 | 83.9 | +392.4 | +2.764 |
| v2_would_open | ROM1 | — | 264 | — | **0** | — | — | — |

### Window B (2026-07-25 → 2026-08-01, 7 days · drift 85.8%)

| Class | Leg | Subset | Events | censored | decided | WR % | Σ move % | avg net %/trade |
|---|---|---|---:|---:|---:|---:|---:|---:|
| v2_would_block | Trigger | v1_agree | 1,813 | 0 | 1,537 | 67.7 | −158.2 | −0.203 |
| v2_would_block | **ROM1** | v1_agree | 1,813 | 941 | **763** | 82.0 | **−61.6** | **−0.181** |
| v2_would_open | Trigger | v1_agree | 101 | 0 | 67 | 88.1 | +76.7 | +1.044 |

### The decisive observation

**The two measures contradict each other in magnitude, and the ROM1 leg additionally in sign between the windows.**

- Trigger leg: v2 blocks signals whose source bots lost net (−0.187%/trade over 21d, −0.203% over 7d) — consistently negative, but **barely** above the round-trip fee of 0.1%.
- ROM1 leg on the **same** signals: **+2.0% Σ over 21 days** on 1,342 decided trades (= +0.0015%/trade gross) and **−61.6% Σ over 7 days**. That's not a small edge, that's noise around zero.

That is P1.10 in numbers: **the gate decides on the trigger bot's statistics, but ROM1 geometry is what gets traded.** A bot can lose in its own cell and the ROM1 trade derived from it still not lose.

### Breakdown by bot × direction (window A, trigger leg, top by |Σ|)

**v2 would additionally block:**

| Bot | Dir | decided | Σ move % |
|---|---|---:|---:|
| VolIndic | LONG | 658 | −570.0 |
| MIS1-72h | LONG | 193 | −226.3 |
| BR2H | LONG | 179 | −165.2 |
| **EPD3** | **SHORT** | 186 | **+137.8** |
| ATS2 | LONG | 134 | −126.3 |
| RUB2 | LONG | 25 | −106.3 |
| BR4H | LONG | 60 | −92.9 |
| EPD3 | LONG | 413 | −86.6 |
| **MIS1-168h** | **LONG** | 28 | **+63.7** |
| **RUB1** | **SHORT** | 22 | **+53.5** |

So the flip is **not a clean cut**: it removes VolIndic-LONG and MIS1-72h-LONG (good), but at the same time cuts EPD3-SHORT, MIS1-168h-LONG and RUB1-SHORT (bad). The full table across all affected legs is in `staging_models/replay/whitelist_v2_realized_eval_2026-07-11.md`.

**v2 would additionally pass** — the entire class:

| Bot | Dir | decided (v1_agree) | Σ move % |
|---|---|---:|---:|
| AIM2 | SHORT | 164 total / 88 clean | +473.8 total / +130.8 clean |
| SRA2 | SHORT | 61 | +49.3 |

In the 7-day window, AIM2-SHORT shrinks to **7 decided trades**. The "v2 unlocks money" side is a **single-leg bet**, not a portfolio effect.

---

## 4. What the flip buys in censoring

On the ROM1 side, **2,010 of 3,352 legs (60%) are censored** — closed by `AUTO_CLOSE_ON_REGIME_CHANGE` (`CLOSED_REGIME_CHANGE`), i.e. neither win nor loss (T-032 convention). The orchestrator closes its own trades regime-driven so often that **only 40% of forwarded trades reach any assessable outcome at all**. Every statement about ROM1 money rests on that 40%.

Side finding, not part of the flip decision: `orchestrator_open_trades` shows over 60 days **6,500 `CLOSED_REGIME_CHANGE` against 4,421 lifecycle-closed** trades. This matches the Step 6 finding "auto-close cuts 49% while in profit" and is its own lever that has **nothing** to do with v1-vs-v2.

---

## 5. Why there is no out-of-sample evidence (the most important caveat)

**a) The trigger-leg finding is in-sample.** `27_bot_regime_analyzer` builds `bot_regime_performance` from exactly the closed trigger trades of the last `REFERENCE_WINDOW_DAYS = 30` days, and `_v2_whitelist_decision` decides a cell **solely** from their `avg_pnl_pct`/`pnl_stddev`. Windows A and B lie entirely within that. That v2 blocks cells whose trigger trades realised negative there is **largely a restatement of v2's own fitting criterion**, not independent evidence.

**b) The out-of-sample run delivers nothing.** Window C (2026-05-15 → 2026-07-02, ends before the fit window) contains **0 events of class `v2_would_block`** — because `orchestrator_open_trades.wl_reason` is only populated from early July onward (B8); the entire forwarded side of that era carries `NULL` and is therefore unassignable. The only divergent class present there (`v2_would_open`, 190 events, **exclusively EPD1-SHORT**) is **100% drift-contaminated**: EPD1 has been retired since 2026-07-06, and the cells that blocked back then are v1-open today. Its realised result (Σ −349.3%, avg −2.18%/trade) therefore measures **not** v2 against v1.

**c) The historical whitelist stays unreconstructible** (T-031 finding, re-checked today and **confirmed**): `bot_regime_whitelist` is upsert-only without history, `bot_regime_performance` likewise, and bot 28 logs only the **v1** path per signal, never the v2 verdict. The v2 verdict per event therefore has to come from today's snapshot. The measured v1 drift shows what that costs: **69.9%** agreement over 21 days, **85.8%** over 7 days, **77.9%** in the May/June window.

**Concretely follows:** as long as that stays the case, every v2 evaluation is an approximation with a 14–30% classification error, and a retrospective after a flip could **not** cleanly reconstruct what v1 would have done. The flip would not be measurably reversible — only switchable.

---

## 6. Where the prior findings stand

| Prior finding | Result of this measurement |
|---|---|
| "The whitelist is ~89% default-open" (Step 6) | **confirmed** at the cell level (88.7%) — **but misleading as a statement about traffic**: 81.8% of the additionally blocked traffic came via the merit path (§2). |
| "SOFT-gate counterfactual T-031: NO-EDGE for PnL at −87% churn" | **the same shape finding**, a different mechanism: here −55% throughput against a ROM1 effect of +2.0% over 21 days. |
| "The T-069 flip is strong evidence IN FAVOUR" (`docs/REGIME_CONDITIONED_GATING_EVAL.md` §5) | **does not hold up against the realised measurement.** The analysis there argues on regime-conditioned cell statistics — i.e. on the same quantity v2 is built from. Measured against real forwards, no effect remains on the money leg. |

---

## 7. The decision that's Michi's to make

Three options, all backed by the numbers above:

**(A) Flip.** Realistic expected value after this measurement: throughput −55%, ROM1 PnL effect indistinguishable from zero, loss of EPD3-SHORT / MIS1-168h-LONG / RUB1-SHORT as triggers, gain of a single-leg bet on AIM2-SHORT. Whoever wants the flip for **risk reasons** (fewer trades, less exposure, less slot consumption — cf. the slot budget from T-042) has a clean justification for that; **for PnL reasons the numbers do not support it**.

**(B) Don't flip (Stop-B).** The task's intended valid answer. v1 stays, v2 stays shadow. Costs nothing and loses nothing the measurement could prove.

**(C) Make it measurable first, then decide.** The reason (A) and (B) can't be cleanly separated here is **one missing line of logging**. Bot 28 writes the v1 path into `wl_reason` resp. `reason` at gate-decision time, but doesn't also read the v2 column. If `get_whitelist_decision` **read and logged** the v2 verdict of the same cell alongside it, from the next restart onward every evaluation would be exact instead of approximated — 0% drift, no snapshot problem, and a real A/B over time.

> **Not built, deliberately.** This is a change in the orchestrator's money path and needs a fleet restart to take effect — both outside this session's approval scope. The entry point is `28_signal_orchestrator.get_whitelist_decision` (the existing `SELECT whitelisted, reason, computed_at` would need to also pull `whitelisted_v2, reason_v2`, and `log_suppressed`/`insert_orchestrator_trade` would need to store the result in a new, purely additive column). If Michi wants (C), that's its own small task.

**Recommendation, if one is wanted:** (C) before (A). The flip is too large at −55% throughput to base on an in-sample measurement with a 14–30% classification error and without a single out-of-sample point. If (C) is too much effort, (B) is the conservative answer — v2 stays shadow and costs nothing.

---

## 8. Reproduction

```
python tools/whitelist_v2_realized_eval.py --since 2026-07-11T00:00:00                                 # Fenster A
python tools/whitelist_v2_realized_eval.py --since 2026-07-25T00:00:00                                 # Fenster B
python tools/whitelist_v2_realized_eval.py --since 2026-05-15T00:00:00 --until 2026-07-02T00:00:00     # Fenster C
```

All three runs are read-only (`set_session(readonly=True)`, no INSERT/UPDATE/DELETE in the tool), ran under `--force-on-busy` at measured 72.7% / 90.4% / 96.9% system CPU with BELOW_NORMAL priority and under the job lock. The reports are under `staging_models/replay/whitelist_v2_realized_eval_*.md`.

**Two reading notes on the numbers:**
- Σ move % is the **unlevered**, target-staged realised move (T-115 definition) and the coverage-robust metric. The `Σ lev %` column in the raw reports is **clamped at −100% per trade** (`core.realized_pnl`, liquidation floor) and therefore biased upward — don't read it as "money".
- WR is TP1 touch, not profitability; for ladder-TP bots, a trade with 66% WR and a negative move is entirely normal.

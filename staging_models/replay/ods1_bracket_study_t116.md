# ODS1 bracket re-derivation — VERDICT: NO CHANGE WARRANTED

**T-2026-KYT-9050-116**, operator request Michi 2026-08-08, after ODS1 came out net
negative in its first two live days. Tool: `tools/ods1_bracket_study.py`. Raw surface:
`ods1_bracket_study_t116.json` (50 cells). **Every figure below is read out of that
JSON, not transcribed from a console run** — an earlier revision of this document was
hand-transcribed and three of its claims were wrong against the artifact, each in the
direction that flattered this verdict. See "Corrections" at the end.

## Verdict

**Do not change TP1 1.0 % / TP2 2.0 % / SL 2.0 %** — because **nothing in the grid is
demonstrably better out of sample**, not because the current bracket looks good.

```
PAIRED holdout, best (2.0/4.0/4.0) minus live (1.0/2.0/2.0), SAME 279 events:
    delta = +0.089 pp/trade,  t = +0.60      -> not significant
```

## What was measured

The 45 live closed trades cannot answer this question: they were produced *under* the
current bracket and carry no information about what a different one would have done.
So the entry rule was replayed over the OI history and every candidate bracket scored
path-dependently on the same events.

* **Window** 2026-06-20 → 2026-08-08, bounded below by 5m candle coverage.
* **1217 deduped events** from 14 113 simulated 5-minute polls. **919 fit / 279 holdout
  / 19 purged**, split 2026-07-25 with a 24 h purge gap. The purged cohort is the
  receipt that the gap runs: 919 + 279 = 1198 < 1217. (In the first revision the gap
  purged nothing and the sums added up to 1217 exactly — see Corrections.)
* **Replay == serving.** `find_candidates` and `_as_of` are loaded out of
  `42_ai_ods1_bot.py` itself. The rule under test is the rule that runs.
* **Entry** = the close of the 5m bar the event falls in — a uniform ~5-minute posting
  delay, not an instantaneous fill, and never the OI-implied mark that
  T-2026-KYT-9050-115 removed.
* **Exits** path-dependent on 5m high/low, 50/50 across the rungs as Cornix fills them,
  marked out at 24 h (= bot 40's `TIME_STOP_H`).

## The surface — ranks 1-5 and the live cell

| rank | TP1 | TP2 | SL | n fit | Ø fit | t | n hold | Ø hold | **t hold** | TIME % | SL % | amb % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 2.00 | 4.00 | 4.00 | 919 | +0.334 | 3.33 | 279 | +0.104 | **0.56** | 9.7 | 42.6 | 0.6 |
| 2 | 2.00 | 3.00 | 4.00 | 919 | +0.327 | 3.44 | 279 | +0.053 | **0.30** | 5.2 | 37.2 | 0.6 |
| 3 | 1.50 | 4.00 | 4.00 | 919 | +0.283 | 3.05 | 279 | +0.113 | **0.67** | 9.7 | 42.6 | 0.6 |
| 4 | 1.00 | 4.00 | 4.00 | 919 | +0.282 | 3.40 | 279 | +0.069 | **0.44** | 9.7 | 42.6 | 0.8 |
| 5 | 2.00 | 3.00 | 3.00 | 919 | +0.280 | 3.32 | 279 | +0.017 | **0.11** | 3.8 | 44.7 | 0.8 |
| **44** | **1.00** | **2.00** | **2.00** | 919 | **+0.092** | **1.77** | 279 | **+0.015** | **0.16** | 0.4 | 47.7 | **2.9** |

Rank 5 is listed deliberately: it is the one cell in the top 12 that does *not* sit on
the widest stop, i.e. the counterexample to the boundary argument below. An earlier
revision of this table skipped it.

**Fit t of 3.0–3.7 collapse to holdout t of 0.11–0.67, and the highest holdout t
anywhere in the 50 cells is 0.99.** Not one cell is distinguishable from zero out of
sample. That is a grid search finding noise.

## Three things that keep this from being a recommendation

**The winner sits on the grid boundary.** 11 of the top 12 cells have `SL 4.0`, the
widest stop offered; the exception is rank 5 (2.0/3.0/3.0). When the optimum crowds the
edge, the ranking is largely a statement about the grid — the real optimum may lie
outside it, or there may be none.

**A hypothesis was refuted, and it changed the conclusion.** "Wide stop wins" was
expected to mean *"this is not a bracket at all, it is just holding for 24 h"*, which
would have pointed at bot 40's time stop rather than any bracket. The exit mix says no:
only 3–10 % of trades in the wide cells reach the mark-out. It is a bracket question,
and the bracket answer is "no evidence".

**The live bracket is measured more harshly than every cell that beats it.** Across all
50 cells intra-bar ambiguity spans 0.50–5.59 % (median 1.25 %), and three cells are
*more* ambiguous than the live cell's 2.92 % — so it is not the harshest overall. But
among the **43 cells that outrank it on fit**, ambiguity runs 0.50–2.17 %, i.e. every
single one is treated more leniently, and every ambiguous bar is resolved against the
trade. The comparison that produces the ranking is therefore biased against the
incumbent, which makes the absence of a significant alternative more notable, not less.

## What is consistent, and what to do with it

The live bracket ranks 44 of 50 on fit and 42 of 50 on holdout — near the bottom in both
windows, though six and eight cells respectively are worse. That is weak, direction-only
evidence that a 2 % stop is too tight for this rule. It is **not** a mandate for any
particular alternative, because no alternative separates from it out of sample.

**Recommended action: none on the geometry.** Let the T-115 anchor change accumulate
data instead. Until 2026-08-07 the bot posted its bracket around a price that could be
up to 45 minutes stale against a 1.0 % TP1; every live row before that carries the
defect, and the negative live result cannot be attributed to the geometry while that
confound is in the sample. Re-run once there are ~2–3 weeks of post-anchor rows and
compare the live cell against its own history rather than against a grid.

## Unresolved, and deliberately not built on

ODS1's signals in bot 40's trailing arm show +0.303 %/trade unlevered (n=33) against
≈ −0.13 % unlevered in its own channel — but **30 of those 33 closed as `SOURCE_CLOSED`**,
i.e. bot 40 inherited the source's exit rather than deciding one, so the difference
cannot be an exit effect.

An entry-anchor explanation was proposed and **could not be verified**:
`trailing_positions.src_signal_id` cannot be joined to `closed_ai_signals.id` (that
table runs its own sequence — the join produced 255 % "price differences" from
coincidental id collisions), and a join on symbol + direction + time returned n=0,
pointing at the writer-dependent timezone domain of T-2026-KYT-9050-107. The
association stands; the mechanism does not. Do not cite it as an anchor effect.

## Honest limits

* One tape, ~49 days, one rule. The holdout is 279 events — enough to refute a large
  effect, not to establish a small one.
* Intra-bar order is unknowable from OHLC and is resolved pessimistically throughout.
* Fills are assumed at the posted levels; slippage and Cornix's real fill behaviour are
  not modelled, and neither are `MAX_EMITS_PER_CYCLE`, `has_open_ai_signal` or the drift
  guard — so the event set is every instant the RULE fires, a superset of what the bot
  posts. Harmless under a paired comparison, but it is not "what went live".
* **Right-censoring is silent.** Events in the final hours of the window get paths
  shorter than the full horizon and are marked out at the last available close,
  indistinguishable from a genuine 24 h mark-out in the `TIME` count. All such events
  fall in the holdout. Small here (the live cell records 0.4 % TIME exits) and
  undirected, but unmeasured.
* The grid is bounded at SL 4.0 / TP2 4.0 and the winner crowds that bound.

## Corrections to the first revision of this document

Both PR reviews caught the same defects; they are recorded rather than quietly fixed.

1. **The purge gap purged nothing.** `n_fit + n_hold` equalled `n_events` exactly — the
   arithmetic receipt that no event was dropped. `PURGE_H` only shifted the holdout
   start while the gap cohort stayed in fit, so those trades' 24 h paths ran into the
   first holdout events. Direction was conservative (leakage can only flatter the
   challenger, and the challenger still failed), so the verdict never moved — but four
   documents asserted a mechanism that did not run. Now fixed, with `n_purged` per cell
   as a standing receipt and a test on the assignment rather than the constant.
2. **"The worst cell in the fit window"** — false. Rank 44 of 50; six cells are worse.
3. **"Every top cell sat on the widest stop"** — false. 11 of 12; rank 5 is the exception.
4. **"Measured most harshly of all, 2.9 % against 0.5–0.9 % elsewhere"** — false as
   written. True only against the cells that outrank it (0.50–2.17 %); across all 50 the
   range is 0.50–5.59 % and three cells exceed the live cell.
5. **The surface table omitted rank 4** of the previous run — which was precisely the
   cell contradicting the boundary argument made further down. Hand-transcription; the
   table is now generated from the JSON.

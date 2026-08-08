# ODS1 bracket re-derivation — VERDICT: NO CHANGE WARRANTED

**T-2026-KYT-9050-116**, operator request Michi 2026-08-08, after ODS1 came out net
negative in its first two live days. Tool: `tools/ods1_bracket_study.py`. Raw surface:
`ods1_bracket_study_t116.json`.

## Verdict

**Do not change TP1 1.0 % / TP2 2.0 % / SL 2.0 %.** Not because the current bracket
looks good — it is the worst cell in the fit window — but because **nothing in the
grid is demonstrably better out of sample**, and shipping the fit winner would be an
overfit, not a fix.

```
PAIRED holdout, best (2.0/4.0/4.0) minus live (1.0/2.0/2.0), SAME 279 events:
    delta = +0.088 pp/trade,  t = +0.60      -> not significant
```

## What was measured

The 45 live closed trades cannot answer this question: they were produced *under* the
current bracket and carry no information about what a different one would have done.
So the entry rule was replayed over the OI history and every candidate bracket scored
path-dependently on the same events.

* **Window** 2026-06-20 → 2026-08-08 (bounded below by 5m candle coverage; `oi_5m`
  starts 06-12 but there are no 5m candles that far back).
* **1217 deduped events** from 14 113 simulated 5-minute polls — 938 fit / 279 holdout,
  split 2026-07-25 with a 24 h purge gap (a trade opened just before the seam can run
  for the full horizon, so without the gap its outcome leaks across).
* **Replay == serving.** `find_candidates` and `_as_of` are loaded out of
  `42_ai_ods1_bot.py` itself. The rule under test is the rule that runs.
* **Entry = the 5m close at the event instant**, the posting-time proxy — not the
  OI-implied mark, which is the stale anchor T-2026-KYT-9050-115 removed.
* **Exits** are path-dependent on 5m high/low, 50/50 across the two rungs as Cornix
  fills them, marked out at 24 h (= bot 40's `TIME_STOP_H`).

## The surface

| TP1 | TP2 | SL | n fit | Ø fit | t | n hold | Ø hold | **t hold** | TIME % | SL % | amb % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2.00 | 4.00 | 4.00 | 938 | +0.324 | 3.26 | 279 | +0.104 | **0.56** | 9.5 | 41.9 | 0.6 |
| 2.00 | 3.00 | 4.00 | 938 | +0.318 | 3.38 | 279 | +0.052 | **0.30** | 5.1 | 36.6 | 0.6 |
| 1.00 | 4.00 | 4.00 | 938 | +0.277 | 3.38 | 279 | +0.068 | **0.44** | 9.5 | 41.9 | 0.8 |
| 1.50 | 4.00 | 4.00 | 938 | +0.275 | 2.99 | 279 | +0.113 | **0.67** | 9.5 | 41.9 | 0.6 |
| … | | | | | | | | | | | |
| **1.00** | **2.00** | **2.00** | 938 | **+0.093** | **1.80** | 279 | **+0.015** | **0.16** | 0.4 | 47.0 | **2.9** |

**Fit t of 3.0–3.7 collapse to holdout t of 0.11–0.67.** Not one cell — including the
best — is distinguishable from zero out of sample. That is the signature of a grid
search finding noise, not of an edge.

## Three things that keep this from being a recommendation

**The winner sits on the grid boundary.** Every top cell has `SL 4.0`, the widest stop
offered. When the optimum is at the edge, the ranking is a statement about the grid
rather than about the strategy — the real optimum may be outside it, or there may be
none.

**A hypothesis of mine was refuted, and it matters.** Before running this I expected
"wide stop wins" to mean "this is not a bracket at all, it is just holding for 24 h" —
which would have made the answer *use the time stop in bot 40, not a bracket*. The exit
mix says no: only 3–10 % of trades in the wide cells end at the 24 h mark-out; the rest
resolve at a rung or a stop. This genuinely is a bracket question, and the bracket
answer is "no evidence".

**The live bracket is measured most harshly of all.** Its intra-bar ambiguity is 2.9 %
against 0.5–0.9 % everywhere else: its 2 % stop is close enough that a rung and the stop
land in the *same* 5 m bar five times as often, and every such bar is resolved against
the trade. So the shipped bracket's poor showing is, if anything, understated in its
own favour — which makes the absence of a significant alternative more notable, not less.

## What is consistent, and what to do with it

Across both windows the live bracket sits at the bottom. That is weak, direction-only
evidence that a 2 % stop is too tight for this rule — but it is not a mandate for any
particular alternative, because no alternative separates from it out of sample.

**Recommended action: none on the geometry.** Instead, let the T-115 anchor change
accumulate data. Until 2026-08-07 the bot posted its bracket around a price that could
be up to 45 minutes stale on a 1.0 % TP1; every live row before that carries that
defect, and the negative live result cannot be attributed to the geometry while that
confound is in the sample. Re-run this study once there are ~2–3 weeks of post-anchor
rows and compare the live cell against its own history rather than against a grid.

## Unresolved, and deliberately not built on

ODS1's signals in bot 40's trailing arm show +0.303 %/trade unlevered (n=33) against
≈ −0.13 % unlevered in its own channel — but **30 of those 33 closed as `SOURCE_CLOSED`**,
i.e. bot 40 inherited the source's exit rather than deciding one, so the difference
cannot be an exit effect.

An entry-anchor explanation was proposed and **could not be verified**:
`trailing_positions.src_signal_id` cannot be joined to `closed_ai_signals.id` (that
table runs its own sequence — the join produced 255 % "price differences" from
coincidental id collisions), and a join on symbol + direction + time returned n=0,
which points at an unresolved timezone-domain question for this writer (the naive
column's domain splits by WRITER, T-2026-KYT-9050-107). The association stands; the
mechanism does not. Do not cite it as an anchor effect.

## Honest limits

* One tape, ~49 days, one rule. The holdout is 279 events — enough to refute a large
  effect, not enough to establish a small one.
* Intra-bar order is unknowable from OHLC and is resolved pessimistically throughout.
* Fills are assumed at the posted levels; slippage and Cornix's actual fill behaviour
  are not modelled.
* The grid is bounded at SL 4.0 / TP2 4.0 and the optimum sits on that bound.

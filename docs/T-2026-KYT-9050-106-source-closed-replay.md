# Bot 40 `SOURCE_CLOSED`: the replay says do not remove it

**Verdict: NO REWORK.** The observational case for deleting the `SOURCE_CLOSED`
exit does not survive the replay it asked for. The causal effect is
**+0.14 pp/trade, 95% CI [−0.17, +0.45]** — indistinguishable from zero, and
stable across every robustness variant. The observational cut put the same
number at **+1.12 pp/trade**, about 8× larger.

Read-only analysis on SRV02. No writes, no artifacts, no fleet action.

---

## What was claimed

The 2026-08-05 handover carried this as the one evidence-backed part of a bot 40
rework:

> 433 positions since 11.07., average peak only +1.05 %, average exit **−3.69 %**,
> total −1568 pp. `TIME_STOP` serves a near-identical population (average peak
> +0.92 %) and exits at **−1.31 %** — 2.4 pp better. Removing SOURCE_CLOSED flips
> the arm's book from −0.40 to +0.72 pp/trade.

It also carried its own caveat, which is the reason this study exists: *the two
buckets are not randomly assigned, so this needs a replay, not just the
observational cut.*

## Two defects in the observational cut

### 1. It pools across the cutoff the handover itself says never to pool across

`TIME_STOP` cannot fire before `TIME_STOP_SINCE` = **2026-07-28 14:00Z**. Bot 40
went live 2026-07-26. So the first two days of `SOURCE_CLOSED` come from a window
in which the rival policy was *structurally absent*.

| bucket | n | avg exit | avg peak | sl present |
|---|---:|---:|---:|---:|
| `SOURCE_CLOSED` pre-cutoff | 268 | −4.120 | 0.922 | 125/268 |
| `SOURCE_CLOSED` post-cutoff | 174 | −2.746 | 1.288 | 174/174 |
| `TIME_STOP` (post-cutoff only) | 245 | −1.339 | 0.905 | — |

Pooled, `SOURCE_CLOSED` reads −3.58 against `TIME_STOP` −1.34: a 2.2 pp gap.
Split, the honest comparison is −2.75 against −1.34: **1.41 pp**. The pooled
number overstates the gap by roughly 60 %.

This is the same trap the handover documents two sections earlier ("Never pool
the regime cohorts"), around the same 2026-07-28 14:00Z cutoff, applied to its
own open item.

### 2. The populations are not "near-identical" — the bucket is conditioned on the outcome

`SOURCE_CLOSED` fires when the source trade leaves `ai_signals`, i.e. when the AI
monitor closed it on SL/TP/timeout. Membership in the bucket is therefore
conditioned on the *source having already gone wrong*. That is the same defect as
bucketing by `closed_ai_signals.status` — selecting on the outcome.

The mechanism also forces the composition, which the cohort profile confirms
exactly: post-cutoff, **zero** `SOURCE_CLOSED` rows are older than 24 h (mean age
7.0 h), because a disarmed mirror older than 24 h would already have been
time-stopped. So the two buckets cannot serve the same population by
construction — they partition on age and armed-state, not at random.

Most importantly: **removing the exit rule does not remove the loss.** These
positions were already 2.75 % underwater on average at the moment the source
closed. Deleting them from the book, as the observational cut does, assumes the
trades never happened. They happened. The only question the data can answer is
what they would have done *next* — which is a replay.

## The replay

Paired design: every position is its own control. The actual `SOURCE_CLOSED`
exit is compared against the replayed exit of the **same** position under the
alternative policy. Pairing removes the selection confound entirely, because
membership no longer has to be comparable across positions.

* **Cohort:** the 174 post-cutoff rows. The pre-cutoff cohort is excluded on
  purpose — 143 of its 268 rows have `sl IS NULL` (pre-T-049), so stop exits
  cannot be modelled there at all and a replay would let losers run unbounded,
  flattering the hold-longer policy by construction. It also predates the time
  stop, so it answers a question about a policy regime that no longer exists.
* **Alternative policy:** current policy minus the `SOURCE_CLOSED` branch —
  exits are `SL_HIT`, `TRAIL` (act 2 %, x 10 %), or `TIME_STOP` (24 h from
  `opened_at` while disarmed).
* **Prices:** 5m candles, the finest stored. `core.trailing_state.TrailingState`
  is reused rather than reimplemented, so the trail semantics cannot drift from
  the bot's.

### Conservatism, stated

Every modelling choice was set against the hold-longer policy, so the null below
is not an artifact of a friendly assumption:

* Candle high/low captures wicks a 10 s live poll could miss ⇒ stop exits fire at
  least as often in the replay as live.
* Within a candle the **adverse** extreme is evaluated before the favourable one,
  so a stop resolves before a peak set in that same candle can trail out.
* A peak set by candle *N* cannot trigger a retrace exit until candle *N+1*.
* Fill at stop level, no slippage — the same optimistic edge bot 40 already books
  (`sl_exit_mark`); it applies to both arms, so the pairing is unaffected.

### Result

```
--- alternative exit composition (n=174) ---
  TRAIL        n= 74   avg_alt= +1.636   avg_actual= +1.000
  SL_HIT       n= 63   avg_alt= -8.238   avg_actual= -7.885
  TIME_STOP    n= 26   avg_alt= -1.907   avg_actual= -1.716
  STILL_OPEN   n= 11   avg_alt= -0.517   avg_actual= -0.940

--- paired result (pp per position) ---
  actual SOURCE_CLOSED exit :  -2.746
  replayed no-SC exit       :  -2.605
  paired mean difference    :  +0.141   SE 0.160   t = 0.88
  95% CI                    :  [-0.172, +0.454]
  positions improved        :  73/174 (42.0%)
  total pp actual           :  -477.8
  total pp replayed         :  -453.2
```

**36 % of the cohort (63/174) simply walks on to the stop it was already sitting
on**, from −7.885 to −8.238. That is the whole story: at source-close these
trades are already at the stop level, because the source closed for the same
reason the mirror would have.

### Robustness

| variant | n | actual | alt | paired diff | t |
|---|---:|---:|---:|---:|---:|
| A base (adverse-first) | 174 | −2.746 | −2.607 | **+0.139** | 0.87 |
| B uncensored (≥24 h forward data) | 145 | −2.761 | −2.612 | **+0.149** | 0.80 |
| C optimistic (favourable-first) | 174 | −2.746 | −2.687 | **+0.059** | 0.38 |

The 11 `STILL_OPEN` rows are right-censored, not open-ended winners: all have
between 0.5 h and 17.1 h of forward candle data (5m data ends 2026-08-05 16:30Z).
Dropping them (variant B) does not move the estimate. The optimistic ordering
(C) makes the case *weaker*, not stronger.

No variant produces a difference distinguishable from zero — **but that sentence
was too strong as first written, and the correction matters.** Variants A–C vary
intra-candle *ordering* and *censoring*. The one conservatism rule they never
vary is the peak lag (a peak set in candle N cannot trail out until N+1), and
that rule is worth roughly the whole effect: relaxing it to a candle-close
retrace test — arguably *closer* to the bot's 10 s live poll, not further from it
— moves the estimate to **+0.270 (t 1.64)**, and to **+0.315 (t 1.93,
CI [−0.005, +0.634])** without the carried peak. Found by the independent
re-derivation during review, not by this study.

So the honest statement is not "null everywhere". It is: **underpowered over
eight days, direction consistently positive across every variant either side has
run, never reaching significance, and never flipping sign.** The decision (do not
remove `SOURCE_CLOSED` on this evidence) is unchanged, because the observational
`+1.12 pp/trade` claim is still refuted by an order of magnitude. What changes is
the strength of the negative: this refutes a specific overstated claim, it does
not establish that the rule is worth keeping on PnL grounds.

A free robustness check the review could run and this study could not: with ~20 h
more forward candles (3 of the 11 censored rows resolve) the estimate is +0.123
(t 0.77). Right-censoring is not driving the result.

## What this does not say

* **It is not a defence of `SOURCE_CLOSED` on PnL grounds.** The point estimate is
  positive; it is simply too small and too noisy to act on. n=174 over eight days
  is a small sample and a re-run on a longer book could separate it from zero in
  either direction.
* **The slot argument does not apply — and once corrected it points the other
  way.** Both figures in the first version of this section were wrong; both were
  found in review and re-derived here.
  * *Slot cost.* Stated as 100.5 extra slot-days (0.58/position). That charged
    the full 7-day horizon to the 11 right-censored positions, which only had
    0.5–17.1 h of forward data. Charging the data that exists gives **28.0
    slot-days, 0.15/position**. At the arm's realised +0.432 pp/slot-day that is
    **12.1 pp** foregone against a replayed gain of +24.6 pp — so even under a
    binding cap the change would be net **positive (+12.5 pp)**, not negative as
    originally written.
  * *Occupancy.* Stated as "concurrency peaks at 97 of 500". That counted only
    mirrors **opened** after the cutoff, which is not slot occupancy. Counting
    every mirror open during the window: **peak 173 of 500** (288 all-time),
    mean ≈ 82.

  The conclusion is unchanged — 173/500 is still not binding, so freed slots have
  no alternative use today — but the argument no longer supports keeping the
  rule, and it should not be quoted as if it did.
* **It says nothing about the other rework ideas**, which the handover already
  marked as not evidence-backed.

## The design argument, which the PnL argument never addressed

`SOURCE_CLOSED` is not primarily a PnL rule. `40_trailing_close_bot.py:850-854`:

> The mirror must not hold a position the source strategy no longer holds —
> otherwise the A/B arm no longer measures the same trades.

Bot 40 is an A/B arm against the hold arm. Removing `SOURCE_CLOSED` makes the two
arms trade different books, so the comparison the bot exists to produce stops
being a comparison. Given the PnL case is null, there is nothing on the other
side of that trade.

## Reproduction

`replay_source_closed.py` / `replay_variants.py`, read-only, in the session
scratchpad — not committed: they are one-shot analysis against live rows, not
fleet code, and the repo already carries the DB-free regression surface for
bot 40 (`backtest/test_trailing_*.py`). Everything needed to rebuild them is in
this document: cohort predicate, policy, candle source and the four conservatism
rules.

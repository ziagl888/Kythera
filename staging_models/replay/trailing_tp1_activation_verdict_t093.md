# Bot 40: is the trail closing too early, are the SLs too deep — and would TP1 activation help?

**Verdict T-2026-KYT-9050-093** · **as of:** 2026-08-04 · **status:** completed ·
**basis:** live DB read-only + `tools/trailing_book_health.py` (extended in this task) ·
**prior work:** T-052 (`trailing_arm_verdict_t052.md`), T-054 (market attribution),
T-060/T-062 (intake, SHORT legs).

## The operator's two suspicions and the question

> "The trailings close too early and the SLs go very deep. Can you simulate whether the trail
> would work if it started at TP1 instead of at 2 %?"

**Short answer:**

1. **"Closes too early" — confirmed as an observation, refuted as a lever.** 94,7 % of all live
   trailing exits saw a better price within the following 24 h (median 3,48 pp left on the
   table). But every rule that arms later — act=5, act=10 **and** TP1 — earns *less* per bound
   slot over the same period, because the same patience also holds the losers longer.
2. **"The SLs go very deep" — confirmed, and it is inherited geometry, not a bot defect.** The
   mirrored SL sits a median **7,0 %** below entry (p90 14,9 %, max 53,3 %) — at 20× that is
   −140 % / −298 % margin. Bot 40 copies the source signal's absolute SL by design; the depth is
   the legs' S/R geometry, not the trail's doing.
3. **TP1 activation is measurable and it fixes the book, but it costs money.** Under the
   configuration Bot 40 actually runs (time-stop 24 h + exposure cap ±50) it earns **−8,5 % net,
   −35 % per bound slot and +52 % MaxDD** against today's act=2 %, and in exchange the open book
   goes from −1,26 % to **−0,11 %**. Confirmed on a second, five-month window with independently
   sourced (imputed) geometry: **−11 % / −34 % / +48 %**, book −0,98 % vs. −1,65 % (§5). Against
   the operator's 800-USD capital envelope (net per avg slot is the binding metric, T-052
   addendum 4) that is a **bad trade — do not flip.** It becomes a real candidate at the
   2-channel stage, where the binding metric changes.

## 1. What the live arm actually did

Population: mirrors opened from the grandfather cutoff `TIME_STOP_SINCE = 2026-07-28 14:00Z`
(so the deployed rule: trail act 2 % / x 10 % + time-stop 24 h + cap ±50) up to 2026-08-04
06:21Z — 851 closed mirrors, all posted live, plus 110 still open.

| exit reason | n | avg mark | Σ mark | min |
|---|--:|--:|--:|--:|
| `TRAIL` | 494 | **+2,21 %** | +1 093,3 | −0,06 % |
| `TIME_STOP` | 171 | −1,41 % | −240,6 | −12,08 % |
| `SOURCE_CLOSED` | 144 | −2,91 % | −398,9 | −23,77 % |
| `SL_HIT` | 42 | **−7,24 %** | −304,2 | −20,44 % |
| **net (unlevered pp, equal-weighted)** | **851** | | **+149,6** | |

(`PREEXISTING` rows — 2 761 — are locks, not trades, and carry no mark.)

Read: the arm is **net positive** over the week, but 42 SL hits (4,9 % of all exits) eat
**two thirds** of what 494 trailing exits earned. That asymmetry is exactly what the operator is
feeling.

## 2. "The trail closes too early"

**The trail is a 2-%-scalper in practice, not just in principle.** Over the 494 `TRAIL` exits:

| | value |
|---|--:|
| median peak at exit | **2,30 %** |
| avg peak at exit | 2,66 % |
| exits with peak < 3 % | **417 / 494 (84 %)** |
| exits with peak ≥ 5 % | 22 / 494 (4,5 %) |
| avg realised mark | +2,21 % |

**And the market kept going after almost every exit.** For each `TRAIL` close, the best mark
reachable within the following 24 h, from 15m wicks (n = 493 with candle coverage):

| | value |
|---|--:|
| moved further in our favour after the exit | **467 / 493 (94,7 %)** |
| left on the table (best 24h mark − realised) | median **+3,48 pp**, mean +6,72 pp, p90 +18,73 pp |
| ≥ 2 pp left | 68,0 % |
| ≥ 5 pp left | 37,9 % |
| ≥ 10 pp left | 21,9 % |

**Honest limit of this number:** it is a *favourable-excursion* measure — the best point on the
tape, which no rule realises. It is an upper bound on what was given up, not the result of an
alternative rule. What an alternative rule actually realises is section 3, and there the
picture reverses.

## 3. The simulation: activation at TP1 instead of +2 %

### 3.1 The coverage problem (why the window is five weeks, not five months)

`closed_ai_signals.targets` is only populated from ~June 2026 on: **0 %** March–May, 2 % June,
77 % July, 100 % August — **19,4 %** across the whole March window. A TP1 rule run over the
5-month window of T-052 would silently fall back to act = 2 % on four fifths of the population
and report a `trail-a2` clone under a TP1 label. The tool therefore refuses to hide this
(`--tp1-only` restricts the whole sweep to covered trades, so every rule scores the same
trades; the fallback share is printed and lands in the JSON).

**Primary run: 2026-07-01 → 2026-08-04, 9 149 roster/LIVE trades (ROM1 excluded), all with a
real TP1**, 15m candles, 0,10 % fee, strictly-prior peak.

### 3.2 What "TP1" is as an activation

TP1 is not automatically the *higher* bar the question assumes. Measured over the roster legs:

| TP1 distance from entry | share |
|---|--:|
| < 2 % (arms **earlier** than today) | **23,8 %** |
| 2–5 % | 51,1 % |
| ≥ 5 % | 25,1 % |

Median 3,18 %, mean 4,47 % — so on median it sits between act=2 and act=5, but **per trade** it
follows each leg's own geometry (MIS2 shorts: TP1 ≈ 19–21 %, so they would essentially never
arm; ATS2/SRA2/MAX1: TP1 ≈ 1,7–1,9 %, so they arm earlier than today). Both directions were
simulated: bare TP1, and `max(TP1, 2 %)` which keeps today's micro-scalper floor.

**Cross-check that the sim's geometry is the live geometry:** the sim measures TP1 against the
*source* entry, the live bot enters at *market*. On the 108 currently open mirrors both agree —
TP1 distance median 2,92 % from the source entry vs. 2,95 % from the mirror entry, and 24,1 % of
open mirrors sit under 2 % (matching the 23,8 % above). The market-entry offset does **not**
distort the rule.

### 3.3 Result (July–August window, same 9 149 trades under every rule)

| Rule | n | Σ net | /trade | avg slots | p95 | net/slot-day | **equity MaxDD** | **net/avg slot** | avg book mark | underwater |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Hold (fleet exit) | 9 149 | 120 | 0,013 | 477 | 1 012 | 0,01 | 4 065 | 0 | +1,05 % | 52 % |
| **Trail act=2 (today, bare)** | 9 149 | 6 539 | 0,715 | 226 | 530 | 0,84 | 2 485 | 29 | −2,28 % | 80 % |
| Trail act=5 | 9 149 | 5 870 | 0,642 | 362 | 842 | 0,47 | 3 136 | 16 | −1,72 % | 68 % |
| Trail act=10 | 9 149 | 4 550 | 0,497 | 429 | 955 | 0,31 | 3 352 | 11 | −0,94 % | 59 % |
| **Trail at TP1** | 9 149 | **6 630** | 0,725 | 352 | 795 | 0,55 | 3 230 | 19 | **−0,64 %** | 64 % |
| Trail at max(TP1, 2 %) | 9 149 | 6 587 | 0,720 | 356 | 812 | 0,54 | 3 230 | 19 | −0,63 % | 64 % |
| Trail a2 + ts24 | 9 149 | 6 330 | 0,692 | 112 | 290 | 1,65 | 1 199 | 57 | −1,29 % | 70 % |
| Trail TP1 + ts24 | 9 149 | 5 826 | 0,637 | 150 | 364 | 1,13 | 1 499 | 39 | −0,19 % | 56 % |
| **DEPLOYED: a2 + ts24 + cap ±50** | 6 250 | **6 379** | **1,021** | **70** | 151 | **2,65** | **184** | **91** | −1,26 % | 69 % |
| **TP1 + ts24 + cap ±50** | 6 287 | 5 835 | 0,928 | 98 | 210 | 1,73 | 279 | 59 | **−0,11 %** | 55 % |
| max(TP1,2 %) + ts24 + cap ±50 | 6 274 | 5 807 | 0,925 | 100 | 212 | 1,70 | 289 | 58 | −0,09 % | 54 % |

### 3.4 Findings

1. **TP1 is the best way to arm later — but arming later is not the win.** Bare TP1 beats flat
   act=5 (+13 %) and act=10 (+46 %) on net at a *smaller* book, because the per-trade threshold
   spends patience only where the leg's own geometry justifies it. Against act=2 it is a wash on
   net (6 630 vs. 6 539, +1,4 %) — and worse on MaxDD (3 230 vs. 2 485), because the extra
   patience inflates the book from 226 to 352 average slots.
2. **Under the deployed envelope TP1 clearly loses.** The time-stop and the exposure cap are the
   two mechanisms that make the arm capital-efficient, and TP1 fights both: the book grows 70 →
   98 slots, density falls 2,65 → 1,73, **net per bound slot falls 91 → 59 (−35 %)**, MaxDD rises
   184 → 279. With a fixed 800 USD and one channel, net-per-slot is the metric that decides
   position size — this is a 35 % pay cut for the same margin.
3. **What TP1 *does* buy is the book — the single healthiest full-population trail measured.**
   Book mark −0,11 % (vs. −1,26 % deployed, −2,73 % on the 5-month act=2 reference), underwater
   55 % vs. 69 %, and a far more balanced 60/38 L/S instead of 48/22. This is the structural
   sickness T-052 diagnosed and could not cure without paying for it — TP1 cures it more cheaply
   than any rule in the T-052 series, but not for free.
4. **The 2 % floor is irrelevant.** `max(TP1, 2 %)` and bare TP1 land within 0,7 % of each other
   on every metric. The sub-2-% targets are not where the money is.
5. **Hold was near-flat in this window** (net 120 on 9 149 trades, +1,05 % book): July–August was
   a bad tape for the fleet's own exit, and every trail variant beat it several times over. The
   arm's exit rule is **not** what is hurting the account right now.

## 4. The SL question

**The depth is real and it is inherited.** Bot 40 mirrors at market but keeps the source
signal's absolute SL — deliberately (`mirrorable_at`, T-051): the SL is an S/R level and moving
it would disconnect it. Over 943 mirrors since the cutoff:

| SL distance from entry | value |
|---|--:|
| median | **7,03 %** |
| mean | 8,40 % |
| p90 | **14,87 %** |
| max | 53,34 % |

At 20× cross margin that is −141 % / −297 % / −1 067 % of margin. By leg (n ≥ 10, live mirrors):

| leg | n | median SL % | max SL % | SL hits | Σ SL loss (pp) |
|---|--:|--:|--:|--:|--:|
| MIS2-72h SHORT | 37 | 17,44 | 32,5 | 1 | −15,9 |
| MIS1-8h SHORT | 18 | 12,46 | 16,3 | 1 | −12,5 |
| RUB1 SHORT | 27 | 8,22 | 21,6 | 3 | −22,1 |
| **AIM2 SHORT** | **209** | 7,62 | 20,4 | **11** | **−100,9** |
| SRA2 SHORT | 74 | 7,02 | 19,2 | 4 | −27,1 |
| ATS2 LONG | 104 | 7,02 | 13,0 | 1 | −5,9 |
| MIS1-72h LONG | 141 | 5,02 | 15,2 | 7 | −34,4 |

AIM2 SHORT alone accounts for a third of the arm's SL damage — through volume (209 mirrors), not
through unusual geometry.

**Does capping it help?** T-052 addendum 6 measured a −5 % SL cap on the March–July window and
rejected it (−33 % net on the deployed rule, MaxDD *worse*). Re-measured on this window the
picture is different:

| Rule | Σ net | equity MaxDD | net/avg slot | avg book mark |
|---|--:|--:|--:|--:|
| DEPLOYED (a2 + ts24 + cap ±50) | 6 379 | 184 | 91 | −1,26 % |
| DEPLOYED + SL cap −5 % | 5 925 (−7 %) | **126 (−32 %)** | **93** | −0,61 % |

The reason is in the recovery statistics: over March–July the trades that dipped below −5 %
ended at avg −2,74 % on hold (42,1 % better than −5 %), so the cap sold them 2,3 pp too early.
In July–August the same population ended at avg **−4,76 %** (only 35,0 % better than −5 %) — the
cap became nearly free, and it bought a third off the MaxDD at unchanged capital efficiency.

**This is one five-week window and it contradicts the five-month one.** It is not a
recommendation to flip; it is a flag that the addendum-6 verdict is regime-dependent and worth
re-running when the next month of data is in. Any change here is Michi-gated (escalation list:
gate flips / live behaviour).

## 5. Robustness: the full March–August window with imputed TP1

The five-week window is one regime, and the T-052 series disagrees with it in places (there
act=10 beat act=2 on net; in July–August it loses). So the same sweep was run over
2026-03-01 → 2026-08-04 — 47 766 trades — with the missing TP1 replaced by the leg's own median
(`--tp1-impute`: 9 270 real, 38 282 imputed, 214 without leg coverage riding the act=2 fallback).
**A per-leg constant cannot reproduce per-trade geometry**, so this run tests whether the
*ranking* survives a different tape, not whether the rule works.

Two things this imputation is **not** allowed to be read as. It is **forward-looking**: the leg
median is taken over the whole population, so a March trade is filled from geometry that only
existed in July — hindsight in the *parameter*. It is **not** the T-052 addendum-4 defect: the
activation still uses only entry + target, and the trail still fires against a strictly prior
peak, so nothing about an individual trade's future enters its own exit decision. That is
exactly the line between "a robustness check with a known bias" and "a retracted result".

| Rule | n | Σ net | /trade | avg slots | p95 | net/slot-day | equity MaxDD | **net/avg slot** | avg book mark | u.w. |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Hold | 47 766 | 58 072 | 1,216 | 998 | 1 628 | 0,37 | 20 962 | 58 | +2,25 % | 44 % |
| Trail act=2 | 47 766 | 46 521 | 0,974 | 261 | 501 | 1,14 | 5 146 | 178 | −3,08 % | 80 % |
| Trail act=5 | 47 766 | 59 305 | 1,242 | 530 | 904 | 0,71 | 9 998 | 112 | −2,14 % | 67 % |
| Trail act=10 | 47 766 | **71 554** | 1,498 | 760 | 1 335 | 0,60 | 14 726 | 94 | −1,01 % | 55 % |
| Trail at TP1 | 47 766 | 56 323 | 1,179 | 446 | 758 | 0,81 | 7 733 | 126 | −2,01 % | 69 % |
| Trail a2 + ts24 | 47 766 | 35 348 | 0,740 | 130 | 263 | 1,74 | 3 694 | **271** | −1,58 % | 65 % |
| Trail TP1 + ts24 | 47 766 | 32 531 | 0,681 | 177 | 368 | 1,17 | 4 002 | 184 | −0,99 % | 56 % |
| **DEPLOYED: a2 + ts24 + cap ±50** | 26 717 | **26 723** | 1,000 | **68** | 128 | **2,53** | **674** | **396** | −1,65 % | 65 % |
| **TP1 + ts24 + cap ±50** | 25 139 | 23 722 | 0,944 | 90 | 177 | 1,68 | 1 000 | 263 | **−0,98 %** | 54 % |

Monthly equity delta (Mar → Aug):

| Rule | Mar | Apr | May | Jun | Jul | Aug¹ |
|---|--:|--:|--:|--:|--:|--:|
| Hold | +3 449 | +17 539 | +18 995 | +13 470 | +4 588 | +70 |
| Trail act=2 | +9 070 | +10 458 | +7 076 | +8 886 | **+10 367** | +604 |
| Trail at TP1 | **+10 236** | **+13 040** | **+10 547** | **+11 842** | +9 666 | +971 |
| DEPLOYED | +2 477 | +5 291 | +3 481 | +5 574 | +9 314 | +535 |
| TP1 + ts24 + cap ±50 | +1 914 | +4 300 | +3 195 | +4 976 | +8 684 | +646 |

_¹ four days only._

**The ranking holds, and it holds for a reason that is worth stating precisely:**

1. **Uncapped, TP1 beats act=2 on this tape** (+21 % net, and in four of five full months), and it
   does so at a better book (−2,01 % vs. −3,08 %). But it is still **worse per bound slot**
   (126 vs. 178) — the extra net comes from binding 71 % more capital (446 vs. 261 avg slots).
   act=10 makes even more (71 554) and is even worse per slot (94). This is one consistent
   gradient, not three separate findings: **on this population, patience buys gross return with
   capital, roughly linearly, and slightly negative on the exchange rate.**
2. **Inside the deployed envelope the verdict is identical to the five-week run.** Net −11 %
   (23 722 vs. 26 723), net per avg slot **−34 %** (263 vs. 396), MaxDD +48 % (1 000 vs. 674) —
   against −8,5 % / −35 % / +52 % in the July window. Two different tapes, two different TP1
   sources (real vs. imputed), the same answer to within a few percentage points. That is the
   strongest single result in this task.
3. **And the same book gain shows up:** −0,98 % vs. −1,65 %, 54 % vs. 65 % underwater. TP1's
   effect on book health is as robust as its cost.
4. **July–August was simply a bad month for patience** (trail act=2 is the only rule that earns
   *more* in July than TP1 does). That explains the apparent tension with T-052's act=10 result
   and removes it: the five-week window is not an outlier in the metric that decides here.

## 6. Recommendation

1. **Do not switch the activation to TP1.** It costs **34–35 % of net per bound slot** in exactly
   the configuration the bot runs — measured twice, on two tapes, with two TP1 sources, agreeing
   to within a percentage point. The book-health gain (−0,11 % / −0,98 % instead of −1,26 % /
   −1,65 %) is real, and it is the cheapest cure for the T-052 book sickness measured so far —
   but the binding constraint today is capital, not book composition.
2. **The "too early" feeling is correct but not actionable through the activation.** Every later
   activation measured — act=5, act=10, TP1 — earns less per bound slot on both windows. What
   they buy is gross return in exchange for capital, at a slightly unfavourable rate; with 800
   USD on one channel that trade is not available. If the goal is to let winners run, the other
   lever is the give-back `x`, and that was measured and rejected too (T-052 addendum 2:
   x=20/30 % lose on both axes).
3. **Revisit this the moment the capital constraint moves.** At the 2-channel stage
   (from ~2 000 USD equity, T-052 addendum 4) the binding metric shifts from net-per-slot back
   towards absolute net, and TP1 — which delivers +21 % over act=2 uncapped, at a healthier book
   — becomes a serious candidate. Run it under cap 1000 alongside act=5 at that point.
4. **The SL depth deserves a follow-up, not a fix today.** The −5 % cap flipped from clearly bad
   (March–July) to nearly free (July–August). Re-run addendum 6 when September data is in, and
   keep the decision Michi-gated.
5. **Open, cheap, and not part of this task:** AIM2 SHORT carries a third of the SL damage on
   volume alone. Whether its 209 mirrors/week are worth their SL tail is a leg question
   (T-062-style residual measurement), not an exit-rule question.

## Reproduction

The **simulation** figures (§3, §5) come from committed code and are re-runnable:

```
python tools/trailing_book_health.py --start 2026-07-01 --tp1-only   --tag tp1_jul
python tools/trailing_book_health.py --start 2026-03-01 --tp1-impute --tag tp1_imputed
```

The **live** figures (§1, §2, §4) were ad-hoc read-only queries, not a committed tool — unlike
the arm's standing live report (`tools/trailing_arm_report.py`, #T54-1), which answers a
different question (what the arm did) and carries no TP1 or post-exit-excursion view. Recorded
here so the numbers stay checkable rather than merely asserted. All against
`trailing_positions`, population `opened_at >= '2026-07-28 14:00+00'` (the `TIME_STOP_SINCE`
cutoff), `close_reason IS DISTINCT FROM 'PREEXISTING'`:

- **Exit tally** (§1): `GROUP BY close_reason` with `count(*)`, `avg/min/max/sum(close_mark_pct)`.
- **Trail peak distribution** (§2): same table filtered to `close_reason='TRAIL'`, then
  `avg/percentile_cont(0.5|0.9) ON peak_pct` plus `count(*) FILTER (WHERE peak_pct < 3|5)`.
- **Post-exit excursion** (§2): for each `TRAIL` row, 15m wicks from `read_coin_wick(sym,
  closed_at−1h, closed_at+25h)`; best favourable mark in `(closed_at, closed_at+24h]` measured
  against the mirror's own `entry`, minus the realised `close_mark_pct`. 493 of 494 rows had
  candle coverage.
- **SL geometry** (§4): `(entry−sl)/entry*100` for LONG, `(sl−entry)/entry*100` for SHORT, over
  all rows with `sl IS NOT NULL`; per-leg table `GROUP BY model, direction HAVING count(*) >= 10`.
- **Mirror-vs-source TP1** (§3.2): `trailing_positions` joined to `ai_signals` on
  `src_signal_id` (open mirrors only — `ai_signals` keeps `targets` only while the trade is
  open), TP1 distance computed against `trailing_positions.entry` and against `ai_signals.entry1`.

## Honest limitations

- **The primary run is five weeks, one regime** (2026-07-01 → 2026-08-04), because that is where
  real TP1 geometry exists. §5 covers the five-month window, but only with **imputed** TP1 for
  80 % of its trades — a per-leg constant, exact for the fixed-geometry legs (MIS2) and a
  stand-in for the S/R-derived ones. Neither run alone is conclusive; they agree, which is the
  argument.
- The T-052 ranking disagrees with the July window in places (there act=10 beat act=2 on net;
  in July–August it loses). §5 reproduces T-052's direction on the same five months, so the
  difference is the window, not the method.
- Sim entries are the hold-arm entries from `closed_ai_signals`; the live bot enters at market.
  Verified as immaterial for the TP1 rule (§3.2), not verified for absolute figures.
- 15m resolution, trail fill at the stop level, no slippage; time-stop exit at candle close.
  217 of 9 149 trades without candle coverage fall back to their recorded close.
- The sim has no symbol uniqueness and no slot-cap backlog (AK3/AK4) — it measures the rule, not
  the bot's admission layer.
- Artefact provenance: `trailing_book_health_tp1_imputed.json` was produced before the review
  fixes to `tools/trailing_book_health.py` (mutual-exclusion guard, `tp1_fallback` in the meta
  block, corrected fallback wording). The rule results are unaffected — re-running the committed
  code with the same flags reproduces every figure and only adds the `tp1_fallback` key.
- The two cap rows trade slightly different sets (6 250 vs. 6 287 trades — the cap is an
  admission rule, so a different exit rule frees slots at different times). T-052 warns that
  capped rows are not 1:1 comparable on raw net; at a 0,6 % difference in n it is immaterial
  here, and the argument rests on net-per-slot and density regardless.
- `shadow_gate` roster state is today's snapshot applied across the history.
- The live section is a **one-week** live sample (851 closed mirrors) and includes the market
  attribution caveat from T-054: the LONG side of that week is largely tape, not leg quality.
- The post-exit excursion in §2 is a favourable-excursion upper bound, not an achievable
  alternative result.

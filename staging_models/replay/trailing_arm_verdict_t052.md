# Can the trailing arm become profitable? — Verdict T-2026-KYT-9050-052

**As of:** 2026-07-27 · **Status:** completed · **Basis:** live DB read-only +
`tools/trailing_book_health.py` (new, this task; figures: `trailing_book_health.md/.json`) ·
**Prior work:** T-041 (evaporation confirmed), PR #198 (slot budget), Bot 40 live since 2026-07-26 23:34.

## The question

Is there an exit rule that captures the evaporation finding from T-041 without filling the
book with losers — or is there none?

**Short answer: the pure activation-gated trail cannot, structurally — at any `act`. But
there are rules that can; they cost realised return. And the arm has a second problem that no
exit rule solves: it runs additively to the hold arm on the same account.**

## 1. The live finding, verified (2026-07-27 ~19:30)

The finding behind this session is real and has worsened since the 16:00 measurement:

| | open positions | avg mark (unlev) | underwater |
|---|---|---|---|
| **Trailing arm** (Bot 40, filled mirrors) | 128 LONG / **5 SHORT** | −1,84 % | 91/128 LONG |
| **Hold arm** (same legs, `ai_signals`) | 789 LONG / **324 SHORT** | LONG −1,34 % / **SHORT +3,90 %** | 606/789 or 110/324 |

- The hold arm holds 324 shorts at avg **+3,90 %** — they cushion its 789 longs. The trailing
  arm has trailed exactly these shorts **away** (88 × `TRAIL` SHORT @ avg +3,13 % realised);
  its ratio is 26:1 LONG versus 2,4:1 in the hold arm.
- Realised, the arm looks good (`TRAIL` LONG 108 × avg +2,58 %, SHORT 88 × avg +3,13 %);
  `SOURCE_CLOSED` LONG, by contrast, books avg −2,10 % — the open book foots the bill.
- The clean window (positions from cutoff 15:32:16, market-entry regime) currently stands at
  avg −0,23 % (57 LONG) — not because the mechanism is absent there, but because the unmixing
  takes time: winners leave the book at ~+2,2 %, losers accumulate.

**Mechanism (structural, not regime-dependent):** the trail only arms once Peak > act = 2 %
and can therefore, by construction, **only close winners**. A trade that never reaches +2 %
stays open until the fleet SL. The book unmixes itself — in every market phase; a falling
market just makes it visible faster. The sim confirms this over 5 months: the trail's avg book
mark is negative at **every** month-end (−1,3 … −6,0 %), the hold book's is positive at every
month-end (+0,1 … +2,6 %).

Michi's ~10 % account drawdown is thus **consistent with this, but not proven causal** — the
account carries the entire fleet, the channel's share cannot be isolated.

## 2. The methodological hole (and a double count)

`tools/trailing_slot_budget.py` (PR #198) measures **realised sums** per (day, direction) and
**slot occupancy**. A rule that closes winners and holds losers looks good on this metric and
bad on the open book. The 49 204 % expectation wasn't miscalculated — it answered the wrong
question.

In addition, the selection contained a **double count**: ROM1 (Bot 28) is a re-forwarder; its
trades are the same ones the original legs already post (trap 2 of the task brief). ROM1
LONG+SHORT accounted for 10 334 % of the 49 204 % (21 %). Without ROM1, the same trail expects
**39 116 %** (sim below, 44 144 trades; 11 236 ROM1 rows excluded). In the live bot, the
one-position-per-symbol rule (AK3) suppresses most of these duplicates anyway — the posted
expectation was too high for this reason as well.

New tool: `tools/trailing_book_health.py` measures every exit rule on **both** sides: realised
(net, density per slot-day) AND open book (counts per direction, avg mark, underwater share,
**equity-curve MaxDD** = realised sum + open MTM, unlevered percentage points, equal-weighted;
daily time series in the JSON). Population: roster legs without ROM1, LIVE per (day ×
direction), from 2026-03-01, 15m candles, strictly-prior peak, 0,10 % fee. Pins:
`backtest/test_trailing_book_health.py` (9 tests, DB-free).

## 3. Sim result (March–July, 44 144 trades, all regimes)

Full table: `trailing_book_health.md`. The decisive rows:

| Rule | Σ net | net/slot-day | avg slots | **equity MaxDD** | avg book mark | underwater | avg L/S |
|---|--:|--:|--:|--:|--:|--:|--:|
| Hold (fleet exit) | 59 574 | 0,400 | 1001 | 20 076 | +2,34 % | 44 % | 755/246 |
| **Trail act=2 (Bot 40 today)** | 39 116 | 1,028 | 256 | 4 377 | **−2,73 %** | **78 %** | 214/41 |
| Trail act=5 | 51 866 | 0,654 | 532 | 9 786 | −1,96 % | 66 % | 427/105 |
| Trail act=10 | 64 088 | 0,564 | 764 | 14 856 | −0,89 % | 54 % | 605/159 |
| **Trail 2 + time-stop 24 h** | 27 966 | **1,431** | **131** | **3 015** | −1,17 % | 61 % | 107/24 |
| Trail 2 + time-stop 48 h | 32 846 | 1,247 | 177 | 4 316 | −1,58 % | 67 % | 146/31 |
| Trail 2 + hard stop −2 % | 15 809 | 1,328 | 80 | 2 216 | ±0,00 % | 41 % | 65/15 |
| Trail 2 SHORT only | 53 700 | 0,453 | 796 | 20 736 | +0,93 % | 50 % | 755/41 |
| Trail 2 LONG only | 44 989 | 0,656 | 460 | 5 872 | +1,19 % | 56 % | 214/246 |
| **Trail 2, 50 % partial close** | 49 345 | 0,528 | 628 | 12 166 | +1,51 % | 50 % | 485/144 |
| **Trail 2 + exposure cap ±50** | 21 391 (n=45 %) | **1,448** | 99 | **683** | −2,84 % | 76 % | 65/34 |
| Portfolio trail 10 % | 9 268 | 1,308 | 48 | 5 271 | −0,25 % | 33 % | 35/13 |

Monthly robustness (Δ equity per month, from the daily time series):

| Rule | Mar | Apr | May | Jun | Jul |
|---|--:|--:|--:|--:|--:|
| Hold | +3 848 | +17 346 | +18 145 | +13 984 | +6 638 |
| Trail act=2 | +7 802 | +8 039 | +6 318 | +8 198 | +8 827 |
| Trail 2 + time-stop 24 h | +6 946 | +4 246 | +3 584 | +5 329 | +7 729 |
| Trail 2 + exposure cap ±50 | +1 381 | +2 428 | +4 494 | +6 010 | +6 965 |
| Trail 2, 50 % partial close | +5 825 | +12 693 | +12 232 | +11 091 | +7 732 |

## 4. Findings

1. **Realised, the trail is stably profitable** — +6,3k…+8,8k every month, while hold swings
   between +3,8k and +18,1k (bull months carry hold's edge). The arm is thus not
   "unprofitable" — it trades ~34 % of hold's net for 4,6× less equity MaxDD and 2,6× more
   density. The T-041 thesis (capturing evaporation) works when realised.
2. **But no `act` heals the book.** act=2/5/10 → avg book mark −2,73/−1,96/−0,89 %, underwater
   78/66/54 % — the asymmetry dilutes towards hold but never vanishes, and the DD advantage
   evaporates along with it (4,4k → 14,9k). A winners-only exit necessarily creates a
   losers-only book. **The "raise act" line of thinking is thereby refuted.**
3. **The symmetric loser rule exists and works — at a cost.** Time-stop 24 h on never-armed
   trades: book mark −1,17 % instead of −2,73 %, slots 131 instead of 256, best density of any
   full-population rule (1,431), MaxDD 3 015. Price: −11 150 realised (27 966 instead of
   39 116) — the time-stop sells recoveries (T-041: 85 % of losers were in the black at some
   point; exactly this optionality gets liquidated). That's the honest trade-off, not a free
   lunch.
4. **Hard stop −2 % bleeds out** (15 809 net, −60 % vs. trail): the cleanest book (±0,00 %) at
   the highest price. **Portfolio trail churns itself to death** (9 268 net on 44k trades ×
   0,10 % fee; confirms T-035: wave close is no-edge). **SHORT-only trail keeps hold's MaxDD**
   (20 736) — refuted. LONG-only trail is notable (44 989 net, DD 5 872, book +1,19 % thanks to
   held shorts), but its book health comes from the period's directional bias — as a rule it
   would be a bet that shorts keep carrying, not a structural fix.
5. **Exposure cap ±50 is the best risk limiter, but no healer:** density 1,448, MaxDD 683 (6×
   better than the trail), stably positive month over month — but it only trades 45 % of the
   trades and the book stays compositionally sick (−2,84 %). It caps the damage instead of
   fixing it. (Admission simulated in arrival order; the bot would select by density — a
   conservative estimate.)
6. **50 % partial close dominates hold on both axes** (net −17 %, MaxDD −39 %, book stays
   +1,51 %) — as a **fleet-integrated** variant of the T-041 finding (roadmap B), the mildest
   intervention. As an arm rule, though, it inherits hold's slot hunger (628 slots).
7. **No exit rule solves the additive problem:** the arm runs on the same account as the hold
   arm and, by construction, preferentially holds the positions that are currently losing —
   the account then holds these losers **twice**, while winners keep running only in the hold
   arm. Every arm configuration is extra exposure with this same skew; the rules above only
   change its size and duration.

## 5. Recommendation (operator decision Michi — the bot stays untouched until then)

**If the arm should keep running as its own experiment:** trail act=2 **+ time-stop 24 h**
(close never-armed mirrors to market after 24 h; trivial in the bot: `peak_pct` and
`filled_at` already live in `trailing_positions`). This is the only simulated rule that keeps
the trail's purpose AND systematically clears the loser book — best density, smallest slots,
MaxDD −31 %. Optionally add an exposure cap as a hard limit; the combination is **not**
simulated and would need to run through this tool once before activation.

**If the concern is the account:** park the arm and pursue the T-041 finding as **partial
close in the fleet** (50 % at the trail trigger, rest keeps running) instead — dominates hold
on net AND MaxDD without double exposure, but needs changes to the fleet exits (separate task,
Michi-gated).

**Don't:** raise act, hard stop, portfolio trail, SHORT-only trail — all four measured and
rejected (finding 2/4).

## Addendum 2026-07-28 — operator questions: "are we closing too fast?" and "market-regime gate instead of ROM?"

After the live event (book unmixing in the clean window within ~9 h to 95 % underwater; Bot 40
parked on operator order at 05:29, unparked again at 05:43), eight more rules were simulated
(run 3, 44 650 trades, same population/methodology):

| Rule | Σ net | equity MaxDD | avg book mark | avg L/S | deployable (≤500 slots) |
|---|--:|--:|--:|--:|--:|
| Trail a2, x=20 % | 32 151 | 4 887 | −2,71 % | 219/42 | yes |
| Trail a2, x=30 % | 27 405 | 5 627 | −2,62 % | 221/42 | yes |
| Trail a10, x=20 % | 55 856 | 15 337 | −0,83 % | 613/162 | **no** |
| Trail a2 + time-stop 24 h + cap ±50 | 18 686 | **588** | −1,22 % | 47/21 | yes |
| SL ratchet to breakeven from +2 % (be2) | 15 998 | 8 996 | +1,84 % | 336/90 | yes |
| **be2 + time-stop 24 h** | 28 812 | 5 867 | **+2,85 %** | 287/82 | yes |
| Book-feedback gate (−1 %, min 10) | 6 457 | 820 | −3,51 % | 29/10 | yes |
| BTC direction gate (24h return sign) | 19 669 | 2 969 | −3,12 % | 108/26 | yes |

**Question (a) — are we closing too fast?** In the `x` dimension: **no, the opposite.**
x=20/30 % loses on BOTH axes against x=10 % (net drops, MaxDD rises, book unchanged): the
deeper fill (peak × (1−x)) costs more than the extra runway earns. In the `act` dimension: yes
(act=10 → 62,6k net), but not deployable (771 avg slots > cap) and without a loss cap (MaxDD
14,9k). The rule that **never** sells a running winner below its potential is the **SL ratchet
+ time-stop (be2+ts24)**: healthiest book of all 23 rules (+2,85 %, better than hold), 28,8k
net — an armed trade only ever gets protected (breakeven), never capped; the time-stop clears
the never-armed ones.

**Question (b) — only post trades that fit the market regime?** Both prediction-free gates are
**dominated by the exit rules**: the book-feedback gate strangles the arm (6,5k net — it cuts
off supply exactly when the recovery starts), the BTC direction gate sits below the simple
time-stop on every metric. This matches the repo's regime history (ROM whitelist 89 %
default-open, HMM refuted, SOFT no-edge, η²≈0): market-regime **detection** carries no edge
here. What works is the structural limit — the exposure cap needs no prediction because it
caps the one-sidedness itself.

**Question (c, implicit) — what catches a dump?** Only the combination of admission cap and
time hygiene: **time-stop 24 h + cap ±50** has the best dump protection of the entire
measurement series with MaxDD 588 (7× better than today's trail) — over a period that contains
several dumps. The time-stop alone doesn't catch a dump (operator's objection is correct); the
cap alone doesn't heal the book; together they limit both depth AND duration of the damage.

**Recommendation (updated):**
- **The goal is limiting losses** → Bot 40 on **trail a2 + time-stop 24 h + exposure cap ±50**.
- **Maximise upside with a healthy book** → **be2 + ts24** (SL ratchet instead of trail): never
  caps a runner, book stays permanently positive, moderate DD. The be2+ts24+cap combination is
  NOT simulated — a further run before the rebuild if there's interest.
- Market-regime gates (à la ROM or simpler): **don't build** — measured and dominated.

## Addendum 2 (2026-07-28) — priority comparison: "who ends up with the best March–July performance?"

Operator question (priority upside). Runs 4+5: breakeven variants (be+cap, be5) and —
decisive for comparability — all major candidates **under the hard Cornix-500 slot cap**
(`run_total_cap`, arrival order; conservative relative to the bot's density-based selection).
Without the cap, the rule with the biggest book automatically wins: hold draws avg 1008 slots,
be5+ts24 avg 583/p95 1083 — neither deployable as simulated.

**Ranking, deployable (equity at period end, unlevered percentage points, same period, same population):**

| Rank | Rule | equity final | MaxDD | net/avg-slot | avg book mark | monthly Δ (Mar…Jul) |
|--:|---|--:|--:|--:|--:|---|
| 1 | **Trail act=2 (Bot 40 today)** | **38 157** | 4 377 | 147 | **−2,73 %** (78 % u.w.) | +7,8/+8,0/+6,3/+8,2/+8,5 |
| 2 | **be5+ts24 @ 500-cap** | **34 471** | **4 124** | 85 | **+3,36 %** | +5,1/+8,0/+7,8/+7,8/+6,2 |
| 3 | Hold @ 500-cap | 26 752 | 6 933 | 56 | +2,71 % | +0,5/+5,5/+9,9/+6,8/+4,7 |
| 4 | be2+ts24 @ 500-cap | 21 781 | 4 324 | 67 | +2,87 % | +3,1/+6,1/+3,2/+4,9/+4,9 |
| 5 | Trail a2 + ts24 + cap ±50 | 18 756 | **588** | **278** | −1,22 % | +1,4/+2,7/+3,1/+4,6/+7,1 |

Without a capacity limit (theoretical): be5+ts24 59 0k ≈ hold 58,1k — but the cap costs hold
54 % (its fat book sticks to the limit and rejects arrivals), be5+ts24 only 42 %.

**Reading:**
1. **The current trail also has the best absolute end result, deployable** — its problem was
   never the realised economics, but the structurally sick book (−2,73 %, 78 % underwater,
   negative every month) and the resulting account skew (losers held twice). This week's live
   incident IS this metric.
2. **Priority upside → be5+ts24** (breakeven ratchet from +5 % + time-stop 24 h): ~90 % of the
   trail's end result (34,5k vs. 38,2k), yet the lowest MaxDD of the major rules (4 124), **the
   healthiest book of the whole measurement series (+3,36 %)**, no runner ever gets capped,
   the most even monthly progression. The account skew (arm preferentially holds losers)
   disappears.
3. **be2 is the wrong ratchet threshold** (21,8k) — at +2 %, too many runners get pulled back
   to 0. +5 % lets the upmove breathe; the directional caps (±50/100) additionally strangle
   the breakeven approach (9–12k, run 4) — do not combine.
4. **Maximum capital efficiency + dump protection remains ts24+cap50** (278/slot, MaxDD 588) —
   this is the "smaller but extremely robust arm" configuration.

**Recommendation priority upside: rebuild Bot 40 onto be5+ts24** — replace the trail exit
with: (1) peak ≥ +5 % → ratchet SL to entry (only issue a `Close` once the market touches
entry again; alternatively a real SL update, if Cornix supports that on the channel), (2)
close mirrors without a +2-%-peak after 24 h (`TIME_STOP`), (3) slot cap 500 stays (AK4). All
three building blocks already exist in the bot (peak_pct, filled_at, cap layer). Rebuild +
restart are Michi-gated.

## Addendum 3 (2026-07-28) — operator's two-channel idea: 2 × 500 = 1000 slots

Operator proposal: split the trades across two channels. With "new trade → into the emptier
channel," that's exactly a global 1000-cap (the emptier channel has room as long as fewer than
1000 are open overall; symbol uniqueness stays global). Run 6:

| Rule | n | equity final | MaxDD | avg slots / p95 | net/avg-slot | avg book mark | monthly Δ |
|---|--:|--:|--:|--:|--:|--:|---|
| **be5+ts24 @ 1000 (2 ch.)** | 42 456 | **53 028** | 5 676 | 560 / 975 | 95 | +3,27 % | +11,0/+14,7/+11,3/+9,6/+6,8 |
| Hold @ 1000 (2 ch.) | 36 060 | 43 167 | 14 193 | 812 / 1000 | 53 | +2,33 % | +0,1/+11,8/+13,9/+13,1/+5,1 |
| be5+ts24 @ 500 (1 ch.) | 32 372 | 34 469 | 4 124 | 408 / 500 | 85 | +3,36 % | — |
| Trail act=2 (today, 1 ch.) | 44 748 | 38 155 | 4 377 | 260 / 495 | 147 | −2,73 % | — |
| be5+ts24 uncapped (theor.) | 44 748 | 58 994 | 6 989 | 583 / 1083 | 101 | +3,26 % | — |

**With two channels, be5+ts24 captures 90 % of its uncapped potential** (53,0k of 59,0k; 95 %
of trades get a slot) and clearly beats every deployable alternative: +39 % over today's trail
with a healthy book, +23 % over hold@1000 at 40 % of its MaxDD. Hold benefits far less from
the second channel (its book sticks to the limit even at 1000, March is close to zero).

**3-channel follow-up check (run 7):** the scaling curve of be5+ts24 across the slot cap:

| Channels (cap) | equity final | Δ vs. previous | MaxDD | admitted |
|--:|--:|--:|--:|--:|
| 1 (500) | 34 509 | — | 4 124 | 72 % |
| 2 (1000) | 53 068 | **+18 559** | 5 676 | 95 % |
| 3 (1500) | 56 639 | +3 571 | 5 997 | 98 % |
| ∞ (theor.) | 59 034 | +2 395 | 6 989 | 100 % |

The second channel is the big jump, the third only buys +6,7 % — at 50 % more exposure
capacity and a third integration target. Hold@1500 (53 113) stays below be5+ts24@1500 even
with three channels, at 3× its MaxDD (18 890). Capital-neutral would be to increase position
size across two channels instead of adding a third.

**Final recommendation (priority upside): two channels + be5+ts24.** Operationally: second
channel + Cornix hookup + sizing (Michi); Bot 40 multi-channel (least-loaded assignment,
`channel_id` in `trailing_positions`, close routing, AK3 global); capital note: 1000 slots ×
position size = up to double simultaneous exposure — sizing per channel is the lever. Rebuild
+ restart Michi-gated.

## Addendum 4 (2026-07-28) — CORRECTION: look-ahead in the breakeven+time-stop rules

**The be+ts figures in addenda 2 and 3 were inflated by a look-ahead and are retracted.** The
time-stop in `exit_breakeven` checked "did the trade EVER arm?" over its entire lifetime
instead of "was it armed BY THE DEADLINE?" — every late winner (armed only after 24 h) thus
escaped the stop the live bot would have given it at hour 24. Found while translating the rule
into the bot logic, fixed causally (pin
`test_breakeven_timestop_is_causal_for_late_armers`), run 8:

| Rule | addendum 2/3 (look-ahead) | **causal (run 8)** | MaxDD causal |
|---|--:|--:|--:|
| be2+ts24 | 28 812 | **7 430** | 7 708 |
| be5+ts24 | 58 994 | **7 004** | 8 592 |
| be5+ts24@1000 (2 ch.) | 53 068 | **3 843** | 7 266 |
| be5+ts24@1500 (3 ch.) | 56 639 | **5 394** | 7 852 |

The entire edge of the breakeven family WAS the look-ahead: causally, the 24h stop kills late
winners at their (mostly negative) 24h mark, and the early-armed ones get booked out at 0.
**The be family is thereby rejected; the channel-scaling curve from addendum 3 is moot.** NOT
affected (deadline check was always causal there): hold, all trail variants,
trail+ts24/48/72, caps, gates, portfolio.

**Cleaned deployable ranking (1 channel, equity March–July):**

| Rule | equity final | MaxDD | **net/avg-slot** | DD/avg-slot | p95 slots | avg book mark |
|---|--:|--:|--:|--:|--:|--:|
| Trail act=2 (today) | 38 181 | 4 377 | 147 | 16,8 | 495 | −2,73 % |
| Trail 2 + time-stop 48 h | 32 215 | 4 316 | 180 | 24,1* | 359 | −1,58 % |
| Trail 2 + time-stop 24 h | 27 438 | 3 015 | 207 | 22,7* | 267 | −1,17 % |
| Hold @ 500 | 26 798 | 6 933 | 56 | 14,6 | 500 | +2,71 % |
| **Trail 2 + ts24 + cap ±50** | 18 776 | **588** | **278** | **8,7** | 130 | −1,22 % |

_*DD/avg-slot for the ts rules sits above the trail because their small book spreads the same
residual DD over fewer slots — the absolute MaxDD is smaller regardless._

**Capital reading (operator context: 800 USD available, 1 channel):** with fixed capital,
sizing scales with occupancy — position size can be as large as the p95 occupancy × margin
that fits the risk budget. What then counts is **net per avg-slot**, not the absolute sum:
trail 2 + ts24 + cap ±50 earns 1,9× the plain trail per bound capital (278 vs. 147) at a
seventh of the absolute MaxDD (588) — and, at p95 = 130 slots, allows a ~3,8× larger position
within the same margin envelope. Concentration caveat: fewer, larger positions — still
reasonably diversified at 130 concurrent.

**Corrected recommendation:** for the start (800 USD, existing channel): **keep trail act=2 +
time-stop 24 h + exposure cap ±50** in Bot 40. Before the 2-channel expansion (from ~2000 USD
equity), run the then-relevant candidates once under cap 1000 (among others trail act=5, p95
917 — only fits with two channels).

## Addendum 5 (2026-07-28) — operator question: "should coins with ±50 % in 24h even be traded?"

Trigger COTI (+57,9 %/24 h, 4 open fleet trades, 3 of them pump shorts from the MIS family).
Measured: the coin's 24h pre-move at entry (strictly causal, from candles before the trade),
buckets per direction + four gate variants on trail act=2.

| Bucket (24h move) | dir | n | avg/trade hold | avg/trade trail |
|---|---|--:|--:|--:|
| < −50 % | LONG | 50 | −3,53 | +0,38 |
| < −50 % | SHORT | 41 | +11,57 | **+9,11** |
| ±20 % (calm) | LONG | 33 183 | +1,18 | +0,71 |
| ±20 % (calm) | SHORT | 8 854 | +1,96 | +1,42 |
| > +50 % | LONG | 73 | +2,63 | +3,97 |
| > +50 % | SHORT | 297 | +2,97 | **+2,96** |

**Finding: movers are, per trade, the BEST trades of the arm, not the worst.** Every extreme
bucket is positive under the trail and beats the calm middle; the pump shorts (COTI pattern,
MIS edge) deliver +2,96/trade, dump shorts even +9,11. The one historically toxic cell — LONG
into the fresh dump on hold (−3,53) — the trail neutralises by itself (+0,38). Consistently,
ALL four gates lose money with no DD gain: abs>30 % −4 255 net, abs>50 % −1 494, chase>20 %
−2 301, chase>50 % −652 (MaxDD unchanged at ~4,4k in each case).

**Answer to the operator: do NOT filter movers.** The roster legs already select these coins
correctly (MIS exists exactly for this); a percentage gate has been measured and rejected in
every variant. Caveat: the extreme buckets are thin (n=41–73), but the direction is consistent
and the gate aggregates confirm it.

## Addendum 6 (2026-07-29) — operator question: "cap SL at 5 % movement (max −100 % at 20x)?"

Trigger CHRUSDT: legacy mirror at −8 % unlev (−160 % margin), source SL 12,2 % below entry
(S/R level = −243 % margin at 20x). Question: does a −5 % cap massively worsen the outcome
because many trades recover?

**Recovery statistics (45 082 trades):** 18 955 (42 %) dipped below −5 % unlev at some point.
Of those, on hold **29,7 % ended in the black** and **42,1 % ended better than −5 %**; the avg
end result of the dippers is **−2,74 %** — clearly better than the −5,00 the cap realises. So
on average, the cap sells every dipped trade ~2,3 percentage points below its actual outcome.

| Rule | Σ net | equity MaxDD | avg book mark |
|---|--:|--:|--:|
| Trail a2 (reference) | 37 887 | 4 377 | −2,73 % |
| Trail a2 + SL cap −5 % | 21 862 (−42 %) | 3 679 | −0,82 % |
| **DEPLOYED (Trail+ts24+Cap50)** | **18 930** | **588** | −1,22 % |
| DEPLOYED + SL cap −5 % | 12 687 (−33 %) | **653 (worse!)** | −0,56 % |

**Answer: yes — the cap makes things massively worse (−33 % on the deployed rule) and doesn't
even lower the MaxDD** (588 → 653: the −5 realisations pull the equity curve down themselves).
SL-first semantics on same-candle touches (conservative), pins
`test_hardstop_tie_is_sl_first` + `test_deployed_slcap_takes_the_earliest_event`.

**Placing the CHR pain in context:** −176 % margin MTM is a **legacy-position phenomenon** —
in the new regime, the 24h time-stop clears the never-armed ones before they can ride this
deep (deployed MaxDD 588 shows the book risk is already capped). The right lever against
single-position margin bleed is position size (m ≈ equity/400), not a price cap that realises
42 % of all trades too early. **Recommendation: do not introduce an SL cap.**

## Honest limitations

- Sim entries = `closed_ai_signals` entry (hold-arm geometry); the live bot has been entering
  at **market** since T-051. Irrelevant for the rule comparison (all rules measure the same
  trades), not literal for absolute figures.
- 15m resolution, trail fill at the stop level, no slippage; candle mask not flush (#T42-5,
  inherited unchanged). 647 of 44 144 trades without candle coverage (hold fallback).
- Equity MaxDD in unlevered percentage points over an equal-weighted book — scales with book
  size; net/slot-day serves as the secondary metric. No compounding, no leverage.
- The young mirror book (before cutoff 15:32:16) is phantom-/selection-biased and was not used
  for any behaviour analysis; the strategy basis is `closed_ai_signals`.
- The `shadow_gate` roster state is a snapshot of today applied across the entire history.
- Time-stop exit at candle close (market-order assumption), hard-stop fill at the stop level.
- The sim has no symbol uniqueness and no slot-cap backlog (AK3/AK4) — it measures the rule,
  not the bot's admission layer.

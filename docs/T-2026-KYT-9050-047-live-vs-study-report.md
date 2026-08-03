# Bot 40: is live turnover above the simulated turnover? (T-2026-KYT-9050-047)

_Measurement 2026-08-01 · read-only · tool `tools/trailing_live_vs_study.py` ·
Data snapshot `trailing_positions` since bot start 2026-07-26 evening, 5.6 days ·
Comparison baseline `staging_models/replay/trailing_slot_budget_live.json` (PR #198, act 2 %, x 10 %, tf 15m)_

## Verdict

**No.** Live turnover is **not** systematically above the simulated turnover — for holding
duration it is actually below (i.e. held longer), and for turnover per occupied slot-day it is
9–23% above. The slot budget was not too optimistic but **too pessimistic**: in steady state the
channel runs at **avg. 106 simultaneously open positions against an expected
252** — half empty. The fee load per slot-day is 9% above the budget used to compute the
49 204%.

The suspected cause — 15m candles versus 10s live prices — is real and **quantified: it is
worth around 20 minutes per arm exit** (median Δ +0.33h, p95 +0.63h) and at most 4.7% slot-days.
It does not shift the operating point, because there is no meaningfully shifted operating point
for it to explain.

**The trigger for the question was a bootstrap artifact.** The "~80 trail fires per hour at
~460 open positions" are exactly 80 fires in **1.2 hours on 26.07 between 19:00 and 20:00 UTC** —
the first shadow cycle in which the bot mirrored an already-running book all at once. These
mirrors inherited a peak that was already above the activation threshold on the first poll, and
fired immediately. In live operation: **4.0 trail fires per hour**, busiest single hour 21.

## Recommendation to the operator (#T52-3)

**Keep `act = 2 %`. No change to Bot 40 based on this finding.**

The hypothesis that would have motivated a change — live turnover above simulation, i.e. the
slot and fee budget too optimistic — is refuted. The only measured deviation path costs
≤ 5% slot-days.

Important for the direction of any possible change: **lowering `act` would not use the free
capacity, it would enlarge it.** A lower `act` shortens the holding duration (study: act 1 →
median 2.0h, act 0 → 0.4h) and thereby reduces occupancy further. Anyone who wants to fill the
empty half of the channel has to **raise** `act` or allow more legs — both are separate
decisions, and both should wait until the profit picture turns: the live book stands at
**−906 percentage-points net**, and the bottleneck is not capacity but profit (cause: see T-054,
tape, not a leg defect).

## 1 Population — live and shadow strictly separated

`posted` is the live/shadow line. Aggregating over the whole table mixes the shadow book and the
admission markers into the live numbers (the T-052 lesson).

| | Rows |
|---|--:|
| `trailing_positions` total | 6 055 |
| of which `posted` = live | 1 141 |
| of which shadow + markers | 4 914 (`PREEXISTING` 4 372, `SHADOW_CARRYOVER` 459, `TRAIL` 80, `SOURCE_CLOSED` 3) |
| **Live positions** (excl. `ENTRY_NOT_FILLED`) | **1 095** |
| of which closed with a real exit | 999 |
| of which still open | 96 |
| `ENTRY_NOT_FILLED` — no slot, no fee | 46 |

`ENTRY_NOT_FILLED` is a posted row with no position behind it. Counting it inflates both slot
draw and fee load at the same time.

## 2 Holding duration — live versus the study

| Measure | Value |
|---|--:|
| Live, closed positions only (n=999) | Median **6.00 h** · p25 1.87 · p75 19.72 · p95 48.60 |
| Live, with the 96 open positions as right-censored | Median lies in **[6.71 h; 7.40 h]** |
| Study, mix-matched to the live leg counts | **6.59 h** (weighted median) |
| Study, headline across legs | 4.6 h |

The 4.6h from the report is a median **across legs**, not a median across trades — every leg
counts equally there, MIS2-168h SHORT (3 live mirrors) as much as MIS1-72h LONG (370).
Mix-matched to the live book, the same study expects **6.59 h**. Measured live: 6.00h without,
6.71–7.40h with the open positions.

**Two biases both point in the direction of "live is faster," and the measurement still comes
out the other way:**

- **Censoring.** 5.6 live days against 148 simulated days: whatever holds for a long time is
  still open. The median over closed rows only is the optimistic bound — hence the interval.
- **The 24h time stop does not exist in the study.** It caps 50 live positions at exactly
  24.0h that would have kept running in simulation.

### Per leg (live median versus the study's trailing median)

| Leg | n | live | Study (trail) | Study (hold) | Ratio |
|---|--:|--:|--:|--:|--:|
| MIS1-72h LONG | 370 | 8.76 | 6.59 | 40.7 | **1.33×** |
| AIM2 SHORT | 128 | 4.17 | 2.54 | 29.0 | **1.64×** |
| ATS2 LONG | 107 | 16.44 | 13.21 | 22.8 | **1.24×** |
| SRA2 SHORT | 56 | 3.73 | 5.54 | 10.4 | 0.67× |
| AIM2 LONG | 49 | 9.17 | 5.58 | 22.1 | **1.64×** |
| SRA2 LONG | 43 | 10.73 | 4.71 | 8.5 | **2.28×** |
| SKW1 SHORT | 32 | 6.52 | 3.73 | 5.0 | **1.75×** |
| SKW1 LONG | 27 | 5.70 | 5.13 | 13.5 | 1.11× |
| MAX1 SHORT | 26 | 3.11 | 4.25 | 7.0 | 0.73× |
| RUB1 SHORT | 25 | 1.18 | 1.03 | 18.2 | 1.15× |
| XSM1 LONG | 22 | 1.16 | 2.57 | 11.0 | 0.45× |
| MIS2-72h SHORT | 22 | 0.72 | 0.41 | 44.2 | 1.76× |
| MIS1-168h LONG | 20 | 6.31 | 9.90 | 49.7 | 0.64× |
| MIS1-8h SHORT | 15 | 0.46 | 0.48 | 8.2 | 0.97× |
| RUB1 LONG | 12 | 4.94 | 2.56 | 44.5 | 1.93× |

_(Legs with n < 10 are in the tool output; their individual ratios carry no signal.)_

For the five largest legs, which together account for 65% of the live book, the live arm holds
**longer** than the simulation. The full table is in the tool output.

### Per day × direction (day of exit)

| Day | LONG n / median h | SHORT n / median h |
|---|--:|--:|
| 2026-07-26 | 16 / 1.60 | 7 / 0.29 |
| 2026-07-27 | 233 / 4.55 | 84 / 4.05 |
| 2026-07-28 | 155 / 11.41 | 20 / 2.01 |
| 2026-07-29 | 83 / 35.31 | 39 / 3.47 |
| 2026-07-30 | 55 / 5.52 | 53 / 2.03 |
| 2026-07-31 | 91 / 11.54 | 85 / 3.72 |
| 2026-08-01 | 40 / 16.41 | 38 / 3.43 |

26.07 is the ramp-up day (first posted mirror ~20:30 UTC); the short median values there are
starting inventory, not an operating figure. The LONG side consistently holds 3–10× longer than
the SHORT side; that is the signature of the market attribution from T-054 (falling tape → LONG
crosses the activation threshold less often and is therefore trailed less often).

## 3 Exit mix

Shares across all 999 real exits:

| Reason | Share |
|---|--:|
| `TRAIL` (the arm's own decision) | **54%** |
| `SOURCE_CLOSED` (follows the fleet) | 32% |
| `SL_HIT` | 9% |
| `TIME_STOP` | 5% |

The day/direction breakdown is in the tool output. Notable: `TIME_STOP` first appears from 29.07
onward (cutoff date `TRAILING_BOT_TIME_STOP_SINCE` = 28.07 14:00 UTC plus a 24h grace period) and
since 31.07 accounts for 24–30% of LONG and 5–12% of SHORT exits — no longer an edge case.

`TRAIL` at 54% means: a good half of the exits are the arm's own decisions, the rest follow the
fleet or the exchange. Whether the study would have trailed the same trades just as often is a
separate comparison and appears in section 6 — the shares there are computed over a window that
extends beyond the live exit, and are therefore **not** to be held directly against this table.

## 4 Realized mark versus the fee (0.10% taker round-trip)

All 999 exits carry a usable mark (SL reconstruction per T-054 / backfill T-058).

| Cut | n | Σ gross | Fee | Σ net | < fee | Avg. mark |
|---|--:|--:|--:|--:|--:|--:|
| **ALL** | 999 | −806.2 | 99.9 | **−906.1** | 38% | −0.81 |
| LONG | 673 | −898.6 | 67.3 | −965.9 | 46% | −1.34 |
| SHORT | 326 | +92.4 | 32.6 | +59.8 | 21% | +0.28 |
| `TRAIL` | 536 | +1153.0 | 53.6 | **+1099.4** | **0%** | +2.15 |
| `TIME_STOP` | 50 | −79.5 | 5.0 | −84.5 | 80% | −1.59 |
| `SL_HIT` | 90 | −559.3 | 9.0 | −568.3 | 100% | −6.21 |
| `SOURCE_CLOSED` | 323 | −1320.4 | 32.3 | −1352.7 | 77% | −4.09 |

**The fee is not the problem.** 999 trades × 0.10% = 99.9 percentage-points against a gross of
−806.2. A "fee share of gross" is meaningless on a loss-making book and is deliberately withheld
by the tool rather than printed as a small positive number.

The comparison that actually carries weight is **fee per occupied slot-day** (section 5): live
0.141% against 0.129% in the study — **+9%**.

Regarding the study's comparison value of "25% of trades below fee" at act = 2: that share there
is computed over **all** trades, including those where the trail never fired. The live
equivalent is the ALL row (38%), not the `TRAIL` row. That the `TRAIL` row sits at **0%** is a
matter of construction, not a finding: an armed trail closes at the earliest at 0.9 × 2.0% =
1.8%. The 38% against 25% comes entirely from `SOURCE_CLOSED` (77%) and `SL_HIT` (100%) — i.e.
from the tape, not from turnover frequency.

This is also the T-052 pattern in the live book: by construction the trail can only close
winners (`TRAIL` +1099 net), while losers sit until the fleet or the SL ends them (−1353 and
−568).

## 5 Simultaneous occupancy

| Measure | live | study |
|---|--:|--:|
| Avg. occupancy | **126.4** | 284.6 raw / **251.6** roster-matched |
| Median | 107 | — |
| p95 | **221** | 498.0 |
| Maximum | 291 | 2 001 |
| **last 48h (steady state)** | **avg. 105.7 · p95 114 · max 116** | — |

The study's 284.6 includes ROM1 LONG (11) + ROM1 SHORT (22), which `core/trailing_roster.py` now
excludes as a re-forwarder duplicate. Average occupancy is a sum of indicator functions and
therefore **exactly additive**, so the roster-matched expectation is 251.6. p95 is not additive
and remains uncorrected.

**Live draws 50% of the roster-matched expectation, 42% in steady state.** The Cornix cap of 500
was never within reach: the observed maximum is 291, and 116 in the last 48h.

### Where the gap comes from: intake, not turnover

Occupancy = inflow × holding duration. Holding duration is not short (section 2), so what's left
is inflow:

| | Inflow |
|---|--:|
| live | **195.1 positions/day** (1 095 over 5.6 days) |
| study (selected p95 sample) | 365.5 trades/day (53 944 over 148 days) |
| | **live = 53%** |

The live bot has four admission filters that the simulation did not have: at most one mirror per
symbol (unique index on `symbol WHERE closed_at IS NULL`, frequently binding at 33 legs across
~530 coins), the 240s freshness window, the symbol cooldown, and the exposure cap. On top of
that, the ROM1 exclusion and `shadow_gate` legs that are temporarily not LIVE — EPD1 SHORT, the
second-largest leg in the study with 4 650 trades, has **not mirrored a single time**.

### Turnover per occupied slot-day

The mix-robust measure — and the unit in which the fee accrues:

| | Trades/slot-day |
|---|--:|
| live | **1.405** (999 exits / 711.0 slot-days) |
| study, aggregate | 1.291 |
| study, mix-matched to the live leg counts | 1.146 |
| | **live = 1.09× / 1.23×** |

This is the most honest answer to the title question: turnover per slot is **9–23% above** the
simulation. Fee per slot-day correspondingly 0.141% against 0.129%.

## 6 Resolution — the study's 15m rule replayed on the same mirrors

Every live mirror is recalculated on its **own** 15m band, from fill through 24h after the live
exit, using the rule **imported** from `tools/trailing_slot_budget.py` (act 2%, x 10%,
strictly-prior peak — rule 7, no second implementation). 999 of 999 exits replayed.

**The arm's own exits (`TRAIL`/`TIME_STOP`), n = 586:**

| Bucket | n | Share | Reading |
|---|--:|--:|---|
| `study-earlier` | 57 | 10% | the 15m wick fired first — live was the **slower** grid |
| `same-bar` | 100 | 17% | same 15m candle: grid granularity, not a different operating point |
| `study-later` | 395 | 67% | the study would actually have held longer |
| `study-never-fires` | 34 | 6% | no 15m trigger within the horizon (right-censored) |

**Δ = study exit − live exit: median +0.33 h · p25 +0.24 · p75 +0.42 · p95 +0.63.**

That is the entire resolution effect: **around 20 minutes, under 38 minutes in 95% of cases** —
i.e. one to two 15m candles. The reason is mechanical: the candle extremes *are* the extremes,
the 15m grid sees the same peak and the same pullback as the 10s poll and therefore triggers
mostly in the same or the next candle. The only real tightening is the strictly-prior peak, and
that shifts the trigger by at most one candle.

In 10% of cases the 15m grid even fires **earlier**: there, the candle wick catches a pullback
that the 10s poll price never printed.

**Exits not ended by the arm itself (fleet/SL), n = 413:** 70% trigger **no trail at all** in the
15m grid within the window plus 24h — both rules hold, the fleet or the SL ends the position. In
26% the study would have trailed later (median +0.88 h, p75 +6.24, p95 +18.50).

### What the difference costs in slots

504 exits are more than one candle-width apart, Σ **33.1 slot-days = +4.7%** on the
live-measured 711.0. A further 325 never fire within the horizon and are **not** counted — the
figure is therefore a **lower bound** on the difference, not an estimate.

### And in price

Where both rules fall within one candle of each other (n = 116), only execution separates them:
live avg. **+2.08%** against 15m stop-level avg. **+2.07%** → **Δ +0.02 percentage-points per
trade**. So the finer grid does not sell worse.

## Honest limits

- **5.6 live days against 148 simulated.** That is a tape excerpt, not a regime cross-section.
  The holding-duration claim is backed by the censoring interval, the profit claim is not — for
  that, T-054 still applies (market, not a leg defect).
- **The leg mix is not the study's leg mix.** MIS1-72h LONG accounts for 35% of the live book,
  EPD1 SHORT (study: 4 650 trades) zero. Every aggregate comparison here therefore carries its
  mix-matched twin; where only the aggregate is shown, it is labelled as such.
- **The 15m replay starts at the first candle lying fully within the window** and closes flush.
  The study itself selects only on `open_time` and lets its last candle extend up to one interval
  beyond the close (its documented limit). The flush variant withholds trigger opportunities from
  the study side rather than granting it extra ones — the conservative error for a tool whose
  thesis is that the study fires less often.
- **The exit time in the replay is the candle CLOSE**, not the candle open as in the study. A 15m
  trigger cannot be known before candle close; the study is optimistic by up to one candle at
  this point (the same class of look-ahead that turned 59k into 7k in T-052).
- **The counterfactual is right-censored** at `--horizon-h` (default 24h). In the study, the
  close of the source trade would additionally have ended the position, and earlier at that.
  "Would have held ≥ X h longer" is a floor, not an estimate.
- **Beta = 1 is not assumed here** — this report does not perform market attribution; that is in
  `tools/trailing_arm_report.py` (T-054).

## Reproduction

```
python tools/trailing_live_vs_study.py                    # voller Report
python tools/trailing_live_vs_study.py --no-replay        # ohne Abschnitt 6 (kein Kerzen-Read)
python backtest/test_trailing_live_vs_study.py            # Pins, DB-frei
```

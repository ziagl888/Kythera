# Handover 2026-08-05 → next session

Starter prompt plus the state you need to not repeat a day's worth of dead ends.
Three tasks ran: **T-104** (leg composition replay, PR #274), **T-105** (walk-forward
portfolio simulator, PR #275), **T-106** (ODS1 OI-divergence short, branch pushed,
no PR yet).

---

## Starter prompt

> Kythera, SRV02. Continue T-2026-KYT-9050-106 (ODS1 OI-divergence short).
> Branch `feat/t-2026-kyt-9050-106` is pushed, one commit, 13 tests green.
> Read `docs/HANDOVER-2026-08-05-ods1.md` first — it carries the numbers and the
> traps. Work the open items in the order listed there. Nothing goes live: the
> watchdog registration and any fleet restart are operator actions (hard rule 1),
> and PR #274 / #275 still need Michi or the core reviews.

---

## Open items, in order

1. **Watchdog registration + `bot_catalog` entry for ODS1.** The file exists; a
   file is not a process. `start_delay=283` is already noted in the module
   docstring. The bot does not run until the fleet restarts — operator action.
2. ~~**Bot 40 rework.**~~ **DONE — REFUTED, do not build it.** The replay this item
   asked for has run: `docs/T-2026-KYT-9050-106-source-closed-replay.md`,
   `AUDIT_TODO#T106-1`. The observational case was wrong twice over. It pooled
   across `TIME_STOP_SINCE` (2026-07-28 14:00Z), where `TIME_STOP` cannot fire at
   all — the honest gap is 1.41 pp, not 2.4 pp — and the bucket is conditioned on
   the outcome, because `SOURCE_CLOSED` fires *because* the source hit its stop.
   Removing the rule does not remove the loss, it defers it: paired replay over the
   174 post-cutoff rows gives **+0.141 pp/trade, CI [-0.17, +0.45]**, null across
   every variant, against the +1.12 pp/trade the cut claimed. 36 % of the cohort
   walks straight on to the stop it was already sitting on. Nothing in bot 40 was
   changed. The caveat this item carried was the right instinct — it just needed
   to be acted on rather than passed downstream.
3. **PR for T-106**, then the CHANGELOG entry.
4. **Re-derive ODS1's bracket and roster density from its own live rows** once it
   has a book. Both are placeholders and both say so in the code.

## Numbers you will otherwise re-derive

| | |
|---|---|
| Sizing | `capital / SLOT_CAP` = **1.6 USD** at 800. Below it slots run out and capital idles at 62.5 %; above it trades drop for margin. |
| Best portfolio config | tight geometry (TP3/SL2 both directions) + OI short gate, 1.6 USD, 5x: **+20.92 %**, maxDD **-1.11 %**, worst day -5.25, 6105 trades. Without the gate: +13.54 %, -3.11 %, -11.57. |
| OI gate threshold | `oi_chg_4h <= -1.2 %` (bottom quintile). Keeps 34,165 of 55,655 signals. |
| ODS1 throughput estimate | ~11 events/day (580 over 7.6 weeks), ~33 concurrent at a 3-day hold. Nowhere near the EPD3-SHORT incident (~484/day, blew the Cornix 500 cap). |
| OI cadence | median 5.0 min until 06.07., **10.0 min from 13.07.** onward (T-097). Never assume 5m. |
| `liq_events` | starts 03.08. — too young for anything. T-095 from ~24.08. |

## Traps this session actually fell into

* **Never pool the regime cohorts.** The cutoff is **2026-07-28 14:00Z**
  (`EXPOSURE_CAP` + time-stop go-live). Pooled, the book reads -1.342 pp/trade at
  83 % LONG; split, the post-cohort is +0.191 at 51 % LONG. Pooling produced a
  confident, wrong verdict ("MIS1-72H is the loss engine" — post-cutoff it is
  +0.38 pp over 149 trades).
* **A five-day cohort is not a verdict either.** Over-correcting on the
  post-cutoff window produced "there are no profitable shorts"; the 17-day
  pre-cutoff window has shorts broadly positive on five-figure samples.
* **The SL in `closed_ai_signals.status` is the monitor-TRAILED stop**, not the
  published one. Bucketing by it conditions the bucket on the outcome — the
  "<3 % SL" bucket shows a 94.7 % TP1 rate because those are the trades that ran.
* **A grid whose best cell sits on its own boundary has found nothing.** The first
  run floored TP/SL at 3.0 and nearly every short leg "optimised" there. Floor is
  now 1.5.
* **Model the venue's limits or you are not simulating the venue.** The first
  portfolio run held 800 concurrent positions against a production ceiling of 500
  (`core/trailing_roster.SLOT_CAP`). Michi caught it.
* **Per-trade expectancy cannot see a slot cap.** T-104 concluded longs want room
  and shorts want tight; the portfolio run refuted it — tight wins on both sides
  because it closes faster and frees slots, and the slot is the scarce resource,
  not the capital. The `inverted` falsification control is what exposed this.
* **Do not pipe long background runs through `tail`/`grep`** — it buffers and you
  lose all progress visibility. Use `python -u`.
* **Start long runs first, not last.** Two runs were lost to session end.

## Evidence status of ODS1, stated honestly

Two independent lines support the mechanism: T-096 as a generator, T-104 as a
filter on the fleet's existing shorts. But T-096 ran 12.06.–04.08. and T-104
13.06.–05.08. — **near-total overlap**. They agree across populations, not across
time, so the regime coverage T-096's own ≥90d gate existed to provide is still
missing. Going live now is the operator's deliberate substitute (forward data on a
new tape), not a claim that the evidence is complete. Keep that framing in the PR.

## Things that are decided, do not re-open

* Roster density for ODS1 sits at the **bottom** (0.010) on purpose: the column is
  the eviction order, every other value was measured by PR #198, and an unmeasured
  leg must yield its seat first. Michi was told and may still override.
* `CH_NEW_IDEAS` with `NEW_IDEAS_LIVE_POSTING` defaulting to 1 — the cohort channel
  is the test environment (operator decision 2026-07-06). No shadow phase, by
  operator decision 2026-08-05.

## Still unfixed, no task

`15_ai_master_bot.py:614-618` and `10_pump_dump_detector.py:1400`: when the chart
image fails, the info message goes out without one and is then Cornix-parseable —
a second position on the same signal. 10 of 299 in the sampled window, 29 of 615
since 01.07. Small, real, and nobody owns it.

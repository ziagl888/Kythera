# MM Order-Lifecycle Patterns — Concept Spec for a Kythera Market-Making Venue

**Status:** Concept / pre-decision. This is design input for the open **Hyperliquid-venue
decision**, not an approved workstream. Nothing here builds anything yet.
**Provenance:** Patterns distilled from a deep read of `lihanyu81/polymarket_lp_tool`
(Python `passive_liquidity/` + `rust_mm_bot/`), the cleanest MM order-lifecycle
architecture found in the 2026-07-10 repo audit (KB `mcp-41a50fe33552`).
**License discipline:** the source repo carries **no LICENSE** → all-rights-reserved.
Everything below is a **pattern harvest in our own words**. No line of that code is
copied, ported, or vendored; identifiers are named only where needed to describe a
mechanism. If we ever implement, we implement clean-room from this spec.
**Task:** T-2026-CU-9050-056. **Audience:** whoever owns the Hyperliquid go/no-go and
the first MM prototype.

---

## 1. Why this document exists

Kythera today is a **signal/taker system**: bots read indicators, emit signals, and the
orchestrator routes one Cornix-parseable message per trade to Binance
(see [`ARCHITECTURE.md`](ARCHITECTURE.md)). A market maker is a *different animal* — it
rests two-sided liquidity, earns the spread (and, on some venues, rewards/rebates), and
its entire risk surface is **adverse selection**: the fills you get are the ones you least
wanted. None of the existing 29 bots do this.

The repo audit flagged a Hyperliquid MM venue as "machbar, well-trodden" and pointed at
`polymarket_lp_tool` as the reference for *how* the order lifecycle of such a bot is
structured. This spec extracts that structure as a set of **named, transferable patterns**,
maps each from the source's Polymarket-CLOB assumptions onto a **Hyperliquid perp order
book**, and separates what transfers cleanly from what we would have to design ourselves.

It is deliberately opinionated about which patterns are load-bearing, because the point of
the harvest is to shorten the venue decision — not to catalogue everything the source does.

---

## 2. The one architectural bet: reconciliation over state machine

The single most important design choice in the source — and the one to carry forward — is
that it **does not run an order state machine**. There is no persisted table that walks an
order `intended → submitted → live → partially-filled → filled`. Instead the bot
continuously **re-derives "my open orders" from two independent sources and reconciles
them**:

- **REST truth** — each loop pulls the full authoritative open-order set from the venue.
  An order is live *iff* it appears there; it is gone the moment it stops appearing.
- **WS shadow** — a background user feed maintains a per-order record (original size,
  cumulative matched, remaining) keyed by the venue order id.

Neither is trusted alone. They **mutually ratchet upward**: REST periodically raises the
WS record's cumulative-fill toward REST when REST is ahead (repairing WS lag / a missed
event), and at read time WS can only raise the REST estimate, never lower it. Everything is
clamped to the order's original size.

Two properties fall out, and both matter for us:

1. **Restart safety for free.** State is re-read, not persisted. A crashed/restarted bot
   reconstructs the world from REST on the next loop; there is no local order-store to
   corrupt or replay. This is exactly Kythera's own "the DB *is* the truth, bots are
   restart-safe" bet (`ARCHITECTURE.md` §1) applied to the venue instead of PostgreSQL.
2. **Graceful degradation.** If the WS feed dies or lags past a staleness threshold, the
   system silently downgrades to **REST-only** inference rather than acting on stale data.
   A dead feed produces *fewer* actions, never wrong ones.

**Kythera fit:** we already believe in reconciliation-through-shared-truth. The venue's
REST open-order endpoint plays the role our DB plays; the WS user feed is the low-latency
optimisation on top. Adopt the pattern verbatim in *architecture* (clean-room in code).

---

## 3. Order lifecycle & fill detection

### P1 — Reconciliation-driven order state
As §2. Order identity in the source is purely the **venue order id** (defensively resolved
across inconsistent field names). There is no client-order-id correlation layer because the
bot never tracks optimistic submissions — it re-reads truth.

> **HL divergence (improve on the source):** Hyperliquid supports a **client order id
> (`cloid`)** and emits **explicit `userFills` events with fill ids**. We should carry a
> `cloid` on every order (idempotent placement, clean cancel targeting) and dedupe fills on
> **fill id**, not on order-disappearance heuristics. The source's "order vanished → scan
> recent trades to corroborate it was a fill and not a cancel" dance is a *workaround for a
> feed without first-class fill events* — largely unnecessary on Hyperliquid. Keep the
> reconciliation spine; drop the corroboration crutch.

### P2 — Cumulative-watermark fill detection (idempotent, replay-safe)
This is the crown jewel and it transfers to **any** order-book venue. The bot tracks, per
order id, a **watermark = how much filled quantity has already been announced**. Every
candidate fill computes `delta = observed_cumulative − already_announced`; if `delta ≤ ε`
it is dropped. Consequences:

- Out-of-order events, WS replays after reconnect, and the same fill arriving via two feeds
  all **collapse to a single delta** — notification is driven by a *monotonic cumulative*,
  not by per-event deltas.
- Partial fills accumulate naturally as the watermark climbs; "full fill" is
  `remaining == 0` or `cumulative == original`.
- Newly-seen orders are seeded with their *already-existing* cumulative, so pre-existing
  partials are not retroactively announced.

**Kythera fit:** identical in spirit to our idempotent-outbox / dedup discipline. On
Hyperliquid, seed the watermark from `userFills` (bounded backfill on startup) and advance
it on each fill event; keep REST open-orders as the reconciling backstop.

### P3 — Desired-vs-actual per-side quote reconciliation
Placement is not transactional; it is a **per-side diff**. For each side: compute the
desired price; if an existing same-side order sits within a **tick-tolerance band** of it,
keep that order and **cancel every other same-side order as a duplicate**; otherwise cancel
the stale ones and post fresh. A `skip_reason` short-circuits to "cancel this side out."

The keep-within-tolerance band is what prevents **cancel/repost churn** on sub-tick jitter —
you do not burn a cancel+post to move the quote half a tick. Duplicate-cull guarantees
**exactly one live order per side** even after a messy reconnect.

### P4 — Cancel-then-repost is the *replace* primitive (and the gap it opens)
The CLOB has no atomic amend, so "replace" = **cancel-old → post-new**, always cancel-first.
The source hardens the window this opens: a `pending_replace` set dedupes concurrent
replaces; **the cancel is retried and, if it never confirms, the replace is aborted** (never
post the new order while the old one might still be live — that would double exposure); a
version-mismatch rejection arms a short per-instrument cooldown (the book view is stale —
stop fighting it); post-only is threaded so a repost that would cross is rejected.

> **HL divergence (improve on the source):** Hyperliquid has a **native order modify /
> `batchModify`**. Prefer it — it is cheaper, preserves intent atomically, and eliminates
> the cancel-first double-exposure hazard entirely. Re-express the source's guards
> (concurrent-replace dedup, stale-book cooldown, post-only via `Alo` tif) around a modify
> call rather than a cancel→post sequence. Keep cancel→post only as the fallback path.

### P5 — WS client separation into one lock-guarded hub
The source runs **three** WS clients (market / user / state), which collapse into **two
conceptual feeds** — a *private* feed (my orders + my fills) and a *public* feed (book +
trades). Both funnel into **one thread-safe state hub**; WS callbacks do **state mutation
only** and never run trading logic (the main loop owns all decisions):

- **Private/user feed** — *my* orders and *my* trades. Order messages upsert per-order
  records with a **monotone merge** (`original_size`/`cumulative` taken as `max(incoming,
  existing)` so a stale message can't regress fill); trade messages accumulate matched
  amounts and feed a bounded **activity ring buffer** used later by fill-risk.
- **Public/market feed** — book snapshots, best-bid/ask, incremental level deltas,
  last-trade prints, tick-size changes.

Why split: different auth/subscription semantics, different consumers, different staleness
tolerances — and one feed degrading doesn't blind the other. Connection hygiene is worth
copying wholesale: **exponential-backoff reconnect** (≈2s→60s), **application-level
PING/PONG heartbeat**, explicit `connected`/`subscribed`/`last_error` flags, and
**staleness predicates** that gate trust. No sequence-gap detection — the feed is
snapshot-oriented and gaps are absorbed by re-reconciling against REST.

> **HL mapping:** Hyperliquid's WS gives `l2Book` / `trades` / `activeAssetCtx` (public,
> incl. **mark price, oracle price, funding**) and `userFills` / `orderUpdates` /
> `userEvents` (private). Same two-feed split, same one-hub discipline. `activeAssetCtx`
> adds mark/funding, which the source has no analog for and which a perp MM *must* consume
> (see §6, P13 and §8).

---

## 4. Pricing & adjustment

### P6 — Incremental adjuster, never a fresh quoter
Both source implementations take an **already-resting order + a live book snapshot** and
emit exactly one of `keep / cancel / replace(new_price)`. They never invent orders from
nothing. This framing keeps the hot path small and makes every decision explainable as a
delta against the current quote. Adopt it: the MM's inner loop is
`(resting order, book) → decision`, not `(book) → new quotes`.

### P7 — Strict priority cascade
The adjustment decision is a **first-match cascade**, not a blended score. Order of
precedence (highest first), harvested shape:

1. **Inventory hard-stop.** At `±max_position`, the saturated side's resting order is
   **cancelled outright** — no more accumulation in the loaded direction.
2. **Far-out-of-band cull.** A quote the market has run away from (wrong side of mid /
   well past the eligible band) is cancelled as useless-or-dangerous.
3. **Fill-risk widen.** Elevated adverse-selection signal → push the quote *outward* by a
   risk-scaled number of ticks, staying eligible. Fill-risk **never pulls a quote by
   itself** — it skews deeper (see P11).
4. **Cautious recenter (only when calm).** If not widening and the quote has drifted too
   far from mid, nudge it back inward — gated behind fill-risk == LOW, sufficient
   queue-depth behind the touch, a real mid-move since last time, and a patience streak.
   Moves are **tiered by distance ratio** and bounded to small steps. Philosophy: *move
   reluctantly, in small steps, only when clearly worth it.*
5. **Terminal keep.**

The lesson to carry: **priority cascade > weighted score** for a money loop. It is
auditable ("which rule fired and why"), it has no tuning-fragile blend weights, and the
highest-risk rule always wins.

### P8 — Reprice speed-limits (three independent throttles)
Essential, and *more* important on a fast perp than on a slow prediction market:

- **Min-move-to-justify.** If `|new − old| < min_replace_ticks·tick`, downgrade the
  replace to `keep` (kills sub-tick cancel-spam).
- **Per-update slew clamp.** Even a justified replace is capped at
  `max_reprice_ticks_per_update` — a decision to jump 10 ticks becomes a bounded move, so
  one noisy snapshot can't yank the quote across the book.
- **Post-fill hazard cooldown.** After a fill, stamp the instrument "recently filled →
  hazardous" for a window; widen and stop chasing during it.

Motivation is universal: exchange rate-limits, cancel/post cost, adverse selection from
over-eager requoting, and protection against acting on a stale book. On Hyperliquid these
map onto real **rate-limit budgets** and the funding/mark dynamics — treat the throttles as
first-class, tuned from the live tick/lot spec.

### P9 — Tick-regime classification
The pricing engine classifies the market off **tick size** into `coarse` / `fine` /
`unsupported`, with a secondary `band_ticks = floor(band/tick)` axis:

- **Coarse** (band spans few ticks): pricing is **book-driven and discrete** — enumerate
  the actual resting levels inside the eligible band and *pick one by rank* (deliberately
  avoiding both the closest-to-mid level, which is fill-prone, and the extreme edge);
  abandon the position if too few levels exist.
- **Fine** (band spans many ticks): pricing is **ratio-driven and continuous** — target a
  fraction of the band away from mid, with a safe zone where you simply keep.
- **Unsupported**: `keep` and do nothing — never quote into a grid you don't understand.

> **HL mapping:** Hyperliquid tick/lot sizes are per-asset (`szDecimals`, 5-sig-fig price
> rule) and generally **fine**; the "outcome price ≈ 1.0 tick" coarse case is a
> prediction-market artifact and mostly won't apply. We'd run predominantly in the
> **fine/ratio** regime, re-tuning regime thresholds from each asset's spec. Keep the
> `unsupported → keep` fail-safe.

### P10 — Midpoint filter (anti-noise, anti-snipe)
Before mid drives pricing it is **smoothed and validated**: an EMA blended 50/50 with a
rolling median (EMA for responsiveness, median for outlier rejection); a **jump detector**
that, on `|raw − prev| > threshold`, arms a short **pause window** (a dislocation / sniping
spike — hold off); a fallback when the book yields no mid; and structural hygiene
throughout (positive-size levels only, tick-snapped, price validated in range, effective
tick re-derived from the book when the API's reported tick disagrees). This is venue-general
and directly valuable against **perp wick noise**. Adopt it.

---

## 5. Order-lifecycle summary (the machine in one picture)

```
        ┌──────────────── one lock-guarded state hub ────────────────┐
 PUBLIC │  book snapshot ▸ mid filter (EMA+median, jump-pause)        │
 WS     │  best-bid/ask ▸ tick regime (coarse/fine/unsupported)      │
        │  trades ─────────────────────────────┐                     │
        └──────────────────────────────────────┼─────────────────────┘
 PRIVATE   my orders ▸ monotone-merge records   │  activity ring buffer
 WS        my fills  ▸ cumulative watermark ─────┘        │
                                                          ▼
   REST open-orders (authoritative)  ──ratchet──►  reconciled own-order view
                                                          │
                          ┌───────────────────────────────┘
                          ▼   main loop (owns ALL decisions)
   per side:  desired price  ─── priority cascade ───►  keep / cancel / replace
              (P3 diff)          1 inventory stop            │
                                 2 out-of-band cull          ▼
                                 3 fill-risk widen (P11)   speed-limits (P8)
                                 4 cautious recenter        min-move · slew · post-fill
                                 5 keep                       │
                                                              ▼
   structural deleverage (P12) preempts the cascade    modify / cancel→post (P4)
   volatility hard-gate (P13) suppresses quoting        cumulative-watermark fill (P2)
```

---

## 6. Risk

### P11 — Fill risk (per-quote adverse-selection score)
Not a calibrated probability — an explicit heuristic in `[0,1]` answering *"how exposed is
this resting order to being run over right now?"* Two ideas do the work:

- **Directional tape weighting.** A taker SELL hits resting bids, a taker BUY lifts asks.
  So for a resting BUY, the *threatening* flow is SELL. Each recent print is weighted by
  whether it is aligned-against / same-side / misaligned / unknown, across a **short and a
  long lookback window**, blended with a **spike term** so a sudden burst dominates a calm
  average.
- **Book proximity.** A monotone-decreasing function of how many ticks the quote sits
  behind the touch (`1/(1 + ticks_behind/scale)`).

The two are **multiplicative** — you need *both* live adverse flow *and* front-of-book
exposure to score high. The continuous score is bucketed into ordered levels
(LOW/MODERATE/ELEVATED/HIGH) → **widen by N ticks**, monotone in level. Purely
microstructural; transfers to a perp unchanged.

### P12 — Structural deleverage (position-level, preemptive)
Where fill-risk asks "should this quote sit deeper?", structural risk asks "does this
instrument carry enough *dangerous inventory* that we should actively cut exposure?" It is
evaluated **before** the P7 cascade and **preempts** it for the affected order. Shape:

- **Token/instrument trigger gate:** a global enable, a danger-exposure gate (dangerous
  share count **or** notional over threshold), and a per-instrument **cooldown**.
- **Per-order risk test — a four-way AND:** tight queue (near the touch) **and** high book
  proximity **and** elevated short-window activity **and** directional micro-trend pressure
  against the order. Deep/safe orders are excluded.
- **Action:** a **tiered size cut** (bigger at HIGH than ELEVATED…) plus **reprice deeper**
  off the touch, clamped to stay eligible and capped so you **never rejoin the front of the
  queue**, with a min-move guard against no-op churn.

Transfers cleanly; only the "stay in reward band" clamp is venue-specific (§7).

### P13 — Volatility hard-gate
The one clean binary kill exposed by the source: `abs(1-day price change) > threshold →
suppress/harden quoting`. On a perp, generalise this to a **mark/funding-aware gate** —
suppress on fast mark moves and near funding extremes (see §8).

### P14 — Read-only condition monitor with hysteresis
A parallel observability layer, **strictly out of the order path** (never imported by
place/cancel/replace), emitting logs + alerts. Its alert gate is a clean **hysteresis /
debounce**: rising-edge fires immediately; while the condition persists, resend only on
**cooldown elapsed** or **significant worsening** (per-metric delta thresholds); falling
edge resets. This is a generic pattern worth reusing for *any* Kythera alerting, not just MM.

---

## 7. Mapping to Hyperliquid — what changes at the venue boundary

| Pattern | Polymarket-CLOB assumption | Hyperliquid perp translation | Transfer |
|---|---|---|---|
| P1 identity | venue order id only; no cloid | carry **`cloid`**; target cancels/modifies by it | improve |
| P2 fill watermark | inferred from order-disappearance + trade scan | seed & advance from explicit **`userFills`** (fill id) | **clean** (drop the crutch) |
| P3 per-side diff | keep-band + dup cull | same | **clean** |
| P4 replace | cancel→post (no amend) | native **modify / `batchModify`**, cancel→post as fallback | improve |
| P5 WS split | `/ws/user` + market channel, app PING | `userFills`/`orderUpdates` + `l2Book`/`trades`/`activeAssetCtx` | **clean** |
| P6 incremental adjuster | — | — | **clean** |
| P7 priority cascade | inventory hard-stop only | add continuous skew *below* the hard-stop (§8) | clean + extend |
| P8 speed-limits | fee/rate-limit driven | tuned to HL **rate-limit budget** + tick/lot | **clean** |
| P9 tick regime | coarse (tick≈0.01/1.0) common | mostly **fine**; re-tune from `szDecimals` | partial |
| P10 mid filter | (0,1) probability domain | free price; validate vs **mark/oracle** not (0,1) | **clean** |
| P11 fill risk | directional tape × proximity | same | **clean** |
| P12 structural deleverage | clamp into **reward band** | no reward band; clamp to price bands / max-dev-from-mark | clean (drop band) |
| P13 vol gate | abs 1-day change | **mark move + funding** aware | extend |
| P14 monitor | — | — | **clean** |

**The three prediction-market assumptions to strip:** (a) the **(0,1) outcome-price
domain** and its `1−tick` clamps; (b) the **liquidity-reward half-band** that anchors the
*entire* target-price logic in the source — Hyperliquid has no equivalent rewarded band, so
we must supply the anchor ourselves (a self-defined desired-offset band around mark); (c)
the **binary-market condition/token pairing** (Yes/No under one condition id) — a perp is a
single symbol.

---

## 8. What the source does NOT give us (design-it-ourselves gaps)

Faithful flagging — these are absent in the harvested code and are exactly where a perp MM
differs from a prediction-market LP:

1. **Continuous inventory skew.** The source only does a **hard stop** at `±max_position`;
   there is no "lean the quotes by `−k·inventory`" term. A two-sided perp MM wants a graded
   skew between the neutral zone and the hard cap. Design it in; the hard-stop stays as the
   outer guard.
2. **Funding-rate-aware quoting.** A perp pays/receives funding (~hourly on Hyperliquid).
   Carrying inventory across a funding stamp has a real cost/credit the source's domain
   never has. Quoting and the vol-gate (P13) must consume `activeAssetCtx` funding.
3. **Mark vs oracle vs last.** The source's mid comes straight from the book. A perp has
   **mark price** (liquidation reference) and **oracle price** distinct from book mid;
   pricing, risk and inventory valuation must be explicit about which they use.
4. **Explicit resolution/expiry risk.** Notably, even on its *own* prediction venue the
   source has **no near-resolution kill-switch** — a real gap the audit should not inherit.
   A perp has no resolution, but the analogue is **event/liquidation-cascade risk**; design
   an explicit gate rather than assuming the harvest covers it.
5. **Latency budget.** The source is REST-loop-paced (slow prediction market). A perp MM's
   edge is latency-sensitive; the loop cadence, WS-vs-REST trust window, and slew limits
   need a stated latency budget we do not get from the source.
6. **Maker economics.** No LP reward band on Hyperliquid; maker rebates and HLP dynamics are
   a different incentive shape that the whole target-price logic must be re-anchored around.

---

## 9. What Kythera already gives us (reuse, don't rebuild)

- **Reconciliation-through-shared-truth** is Kythera's founding bet — the venue REST
  endpoint slots into the role the DB plays. §2 is not a new idea here, just a new target.
- **Staging discipline** (`staging_models/`, promotion is an operator decision) maps
  directly onto MM parameter/model rollout — an MM config or fill-risk model ships
  shadow/idle behind a default-off gate, exactly like `AIM2_LIVE_POSTING`.
- **Shared feature builders** (`core/*_features.py`, trainer == serving == replay): if
  fill-risk (P11) ever becomes model-driven, it lives in `core/` under the same X-R1 rule.
- **Forming-candle / look-ahead discipline (R1)** is the same instinct as the source's
  "only trust closed/validated state" — the mid filter (P10) and closed-candle rule are
  cousins.
- **UTC policy (R3)**, **caller-commits-the-transaction**, **atomic state writes**, and the
  **idempotent outbox** are all directly reusable plumbing for an MM subsystem.
- **Escalation boundary** (`OPUS-HANDOFF.md` §6): a live MM is a leveraged money path — any
  rollout, gate-flip, or capital allocation is an operator (Michi) decision, never part of a
  prototype run. This spec assumes an MM prototype runs **shadow/paper first**.

---

## 10. Recommendation & open questions (feeding the venue decision)

**Recommendation.** The harvest *supports* the "Hyperliquid MM is feasible and
well-trodden" reading. The order-lifecycle spine (§2–§5) is venue-general, matches
Kythera's existing reconciliation philosophy, and is **cheaper to build on Hyperliquid than
on the source's venue** because HL gives us `cloid`, explicit `userFills`, and native order
modify — the three things the source had to work around. The genuinely new engineering is
**not** the order lifecycle; it is the perp-specific layer §8 (funding, mark/oracle,
continuous skew, latency budget, maker economics). That is where a prototype's risk and
effort actually sit, and it should be scoped before any commitment.

**Do not** treat this as a green light. It is a green light for a **shadow/paper MM
prototype** whose first goal is to validate §8's assumptions on live Hyperliquid data, not
to quote real size.

**Open questions for the decision:**
1. **Capital & mandate** — is MM a new profit line or a hedging/inventory tool for the
   existing taker fleet? Different answers change the inventory-skew and funding stance.
2. **Single-asset or fleet** — one deep asset (BTC/ETH perp) first, or the multi-coin
   breadth Kythera already runs? The source is single-market-at-a-time.
3. **Latency posture** — colo/low-latency vs the existing Windows-VPS loop cadence. This
   gates whether P8's throttles are conservative or binding.
4. **Model vs rules** — start rules-only (P7 cascade + P11 heuristic), add a learned
   fill-risk model later under the shared-feature-builder rule?
5. **Where MM state lives** — its own tables in the existing PostgreSQL bus, or an isolated
   store? Reconciliation (§2) argues for the DB, consistent with the fleet.

---

## 11. Provenance & references

- **Source (patterns only, no code):** `lihanyu81/polymarket_lp_tool` @ `32f7799`
  (`passive_liquidity/{order_manager,fill_detection,fill_risk,structural_risk,
  adjustment_engine,risk_manager,condition_monitoring,polymarket_ws_*,simple_price_policy}.py`,
  `rust_mm_bot/src/{pricing_engine,execution_engine,risk_monitor}.rs`). No LICENSE → harvest
  discipline per §1.
- **Repo audit:** KB `mcp-41a50fe33552` (2026-07-10, 8 Polymarket-bot repos vs Kythera).
- **Kythera context:** [`ARCHITECTURE.md`](ARCHITECTURE.md) (reconciliation bet, staging
  rule, shared feature builders, escalation), [`OPUS-HANDOFF.md`](OPUS-HANDOFF.md) §6
  (money-path escalation).
- **Task:** T-2026-CU-9050-056 (KB project 9050).
```

# T-2026-KYT-9050-141 — Trailing-book exit gate stage 3: tighten the trail on the hazard signal

**Status:** pre-registration. Committed BEFORE any outcome is computed (git history is the
proof of ordering). Results are appended in a later commit and never edit this section.
Operator decision 2026-08-12: stage 3 after T-139 (tape gate NO-EDGE) and T-140 (hazard
model predicts losers, holdout AUC 0.698 — but the full-exit ACTION loses anyway).

## 1. Question

T-140 established that per-position state predicts doomed trades out-of-sample, and that
closing on the prediction still loses because false positives forfeit full +2.17 % trail
wins. Stage 3 keeps the validated signal and changes the ACTION: instead of exiting, the
position's trailing parameters tighten from the signal instant on. The structural fact
the variants must address: **this book's losers never cross the +2 % activation, so their
trail is never armed** — pure retrace tightening cannot touch them.

## 2. Pre-registered design (frozen)

### Signal (frozen — no re-selection)

Exactly the T-140 winner cell: the frozen T-140 model (same features, same FIT-only
training, same standardization), scope LONG-only, θ = 0.5, evaluated at the same instants
(fill + hourly BTC candle closes, cap 72); NaN-vol instants can never fire. The first
firing instant t* triggers the action. Trades where the signal never fires are untouched
(paired delta 0).

### Action variants (live params: activation 2.0 %, retrace 0.10, time-stop 24 h)

From t* on, the trade's `TrailingState` continues with:

- **A1:** retrace 0.10 → 0.05, activation unchanged (only already-armed trades react).
- **A2:** activation 2.0 % → 1.0 %, retrace unchanged.
- **A3:** activation → 0.0 %, retrace unchanged (arms at any positive peak; for
  underwater trades this approximates an immediate market exit, for in-profit trades it
  trails from the current peak).

### Counterfactual mechanics (the bot's own class, real tick paths)

Per affected trade, the 10s `ticker_10s` path from `filled_at` to `closed_at` (per-trade
bounded range queries, chunked). `core.trailing_state.TrailingState` runs from fill with
LIVE parameters; at the first tick at/after t* the parameters switch to the variant; the
overlay exit is the EARLIER of (tightened-trail close at that tick's mark_pct, the actual
booked close at `closed_at`). One close per trade — fees unchanged. Trades without
tick coverage are excluded and counted (no silent caps).

### Calibration gate (T-134 lesson — must pass BEFORE any counterfactual is read)

The baseline trail (live params, replayed from fill on the same tick path) must
reproduce the booked TRAIL exits: for trades with `close_reason = TRAIL`, report the
share whose replayed exit mark is within ±0.25 pp of the booked `close_mark_pct` and the
median absolute difference. If fewer than 80 % agree within ±0.25 pp, STOP — the replay
is a model gap and the counterfactual is not read.

### Split, selection, criteria (identical to T-139/T-140)

FIT = Bot 40 `filled_at` < 2026-08-04 00:00 UTC; HOLDOUT = Bot 40 ≥ cutoff + all Bot 44.
A1–A3 compared on FIT only by net Σdelta; the single winner is evaluated ONCE on the
holdout. PASS iff: (1) holdout paired Δ mean > 0 with t ≥ 2.0; (2) Σdelta > 0 on each
holdout book; (3) the exit-mix shows the negative SL_HIT + SOURCE_CLOSED mass genuinely
reduced. Anything else → NO-EDGE, nothing is wired. Additionally reported (not
verdict-bearing): TRAIL winners killed vs stage 1 (745) and stage 2 (497), and the split
of overlay exits into "tightened trail closed early" vs "unchanged".

## 3. Results (run 2026-08-12)

**Verdict: NOT MEASURABLE offline — the calibration gate stopped the study, exactly as
pre-registered. This is a failed measurement, NOT a negative finding (the
T-070/T-2026-CU lesson: a failed measurement must never be booked as "no edge").**

The frozen signal fired on 1,930 of 5,274 trades; their full 10s tick paths were pulled
(4.9 M ticks; coverage is good — median first-tick 29 s after fill, last-tick 19 s before
close). The baseline trail replay then failed calibration: of 773 booked-TRAIL trades,
only **17.2 %** reproduced within ±0.25 pp (gate requires 80 %). The failure is not noise
in the matches (median |diff| of the finite cases is 0.026 pp — where the replay closes,
it closes almost exactly right): **615 trades never close in the replay at all.**

### Root cause — the bot's price stream is not `ticker_10s`

The never-closing cases cluster at booked exits of +1.8..+2.0 %, i.e. peaks marginally
above the 2 % activation. There the arming decision hinges on ~0.1 pp: e.g. XMRUSDT
booked +1.84 implies a live peak ≥ 2.044 %, while the symbol's entire `ticker_10s` path
peaks at 2.03 %; conversely LISTAUSDT's path peak (2.11 %) arms the replay, yet the path
never prints the ≤1.90 % mark the bot exited on. Bot 40 polls
`core.live_price.get_live_prices_batch` (Binance REST) — a price stream that sees
extremes on BOTH sides that `ticker_10s` does not record, and that is **persisted
nowhere** (only the final `peak_pct` survives, in the position row). TRAIL exits
concentrate exactly at the activation boundary, so the ~0.1 pp source mismatch flips the
arm/close decision for ~80 % of them. No offline tick source can reproduce the live
trail's decisions; any counterfactual computed on `ticker_10s` would carry this bias into
precisely the marginal trades the variants are about.

### What this buys and what follows

- The T-134 lesson is now institutionalized and it worked: the same class of replay gap
  that silently inflated the slot-budget replay was caught HERE by a pre-registered gate
  before any counterfactual number existed. Stage 1/2 are unaffected (their baseline is
  the booked outcome; no trail re-derivation).
- **Forward path A (cheap, enables the offline study):** persist the bot's own poll
  prices (one INSERT per poll batch into a `trailing_poll_prices` table, or log-file
  append) — a small live change, operator-gated; after ~2 weeks of data this study
  reruns with a faithful calibration.
- **Forward path B (direct, live):** measure trail-tightening as a live A/B — the
  mechanics twin (Bot 44) exists exactly for this (T-117); a tightened-parameter variant
  on the signal is a money-path change and thus an operator decision.
- The stage-3 question stays OPEN. Nothing here contradicts the stage-1/2 result that
  full-exit overlays do not pay.

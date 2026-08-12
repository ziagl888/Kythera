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

## 3. Results

*Appended after the run — absent until then by construction.*

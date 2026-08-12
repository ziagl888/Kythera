# T-2026-KYT-9050-139 — Trailing-book runtime exit gate: stage-1 LONG×tape counterfactual

**Status:** pre-registration. This section is committed BEFORE any outcome is computed —
the git history of this file is the proof of ordering (T-120 discipline). Results are
appended in a later commit and never edit this section.

## 1. Question

The trailing books (Bot 40 `trailing_positions`, Bot 44 `trailing_free_positions`) lose
through three exits the trail cannot protect: SOURCE_CLOSED, SL_HIT (Ø −5.13 %/trade on
Bot 40, tail beyond the stop) and TIME_STOP. Bot 40's realized loss is almost entirely
LONG-side (−281 of −299 %); T-054 attributed the LONG loss to the tape. Stage 1 tests the
cheapest runtime gate that could act on this: **exit LONG positions when the market tape
turns down.** Stage 2 (a per-position hazard model) only runs if stage 1 leaves the SL
damage unexplained.

## 2. Pre-registered design (frozen)

### Data

- Closed, filled, booked rows only: `posted AND filled_at IS NOT NULL AND closed_at IS NOT
  NULL AND close_mark_pct IS NOT NULL AND entry > 0` (NaN guard, T-114).
- Baseline outcome per trade: the booked `close_mark_pct` exactly as it stands. Its known
  bias stays: SL_HIT is booked at stop level with no slippage (optimistic for the
  baseline). Counterfactual gate exits are booked at real `ticker_10s` marks via
  `core.trailing_state.mark_pct` — this biases the comparison AGAINST the gate, which is
  the conservative direction.
- Mark path: `ticker_10s` (the same source the live trail uses — T-128 asymmetry kept).
- Tape source: BTCUSDT 1h **closed** candles only (hard rule 5).

### Counterfactual overlay (no re-derivation of trail/SL)

A trade runs exactly as it actually ran until the FIRST evaluation instant at which the
gate fires. If that instant precedes the actual `closed_at`, the trade closes there at the
first `ticker_10s` mark at/after the instant, booked with `mark_pct`; otherwise the trade
is unchanged (paired delta = 0). One close per trade either way — the fee count is
unchanged, so gross deltas are net deltas. Killed future TRAIL winners automatically show
up as negative deltas (missed-recovery pricing is inherent in the pairing).

### Evaluation instants

Position fill time, plus every BTCUSDT 1h candle close inside the position's life. The
tape state can only flip on a candle close, so denser evaluation adds nothing.

### Tape definitions (both computed at instant t from closed candles only)

- **TD1 (momentum):** down iff close(last closed 1h) < close(1h candle 4 hours earlier).
- **TD2 (mean):** down iff close(last closed 1h) < mean(last 24 closed 1h closes).

### Gate variants (LONG positions only; SHORT is never touched in stage 1)

- **G-A:** exit when tape is down.
- **G-B:** exit when tape is down AND the position's current mark_pct < 0 (underwater).
- **G-C:** as G-B, plus time-in-trade ≥ 1 h (grace period).

### Split and selection (frozen before outcomes)

- **FIT:** Bot 40 trades with `filled_at` < 2026-08-04 00:00 UTC.
- **HOLDOUT:** Bot 40 trades with `filled_at` ≥ 2026-08-04 00:00 UTC, plus ALL Bot 44
  trades. The two holdout books are also reported separately.
- The 6 variant cells (TD1/TD2 × G-A/B/C) are compared on FIT only, by net Σdelta. The
  single FIT winner is evaluated ONCE on HOLDOUT. No second look, no threshold nudging.

### Success criteria (frozen)

PASS — and only then does a wiring task go to the operator — iff on HOLDOUT the selected
variant shows ALL of:

1. paired per-trade Δ mean > 0 with t ≥ 2.0 (zeros included, Welch not needed — one
   sample of deltas);
2. Σdelta > 0 on each holdout book (Bot 40 holdout AND Bot 44) individually;
3. the exit-mix table shows the negative SL_HIT + SOURCE_CLOSED mass reduced, not merely
   renamed into an equally large GATE bucket (T-128: always check the mix, not the mean).

Anything else → **NO-EDGE**, documented, gate not wired. Prior is modest: two exit
overlays died before (T-035, T-031).

### Data hygiene (no silent caps)

- Gate exit mark: first tick within 10 min after the instant; else the 1h candle close of
  the evaluation hour; positions with neither are excluded and counted in the report.
- Every hypertable query carries a lower time bound (T-116 lesson).
- The study reads the live PG read-only, in one snapshot pull to local parquet
  (T-120 pattern); replay and stats run offline against the snapshot.

## 3. Results

*Appended after the run — absent until then by construction.*

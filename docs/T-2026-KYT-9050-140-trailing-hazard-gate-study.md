# T-2026-KYT-9050-140 — Trailing-book exit gate stage 2: per-position hazard model

**Status:** pre-registration. Committed BEFORE any outcome is computed (git history is the
proof of ordering). Results are appended in a later commit and never edit this section.
Operator decision 2026-08-12: run stage 2 despite the stage-1 NO-EDGE (T-139).

## 1. Question

T-139 killed the market-level tape gate: FIT t=+5.73 flipped to holdout t=−2.31, and the
gate's stop savings were outweighed by 745 killed TRAIL winners. Stage 2 asks whether
**per-position state** — the thing a market flag cannot see — predicts doomed trades well
enough that an early exit beats the stage-1 null under the identical frozen protocol.

## 2. Pre-registered design (frozen)

### Data, instants, booking

Identical to T-139 (`tools/trailing_exit_gate_study.py` conventions): closed, filled,
booked trades of both books; evaluation instants = fill + every BTCUSDT 1h candle close
inside the trade's life; counterfactual overlay = trade runs exactly as booked until the
gate first fires, exit booked at the instant's real `ticker_10s` mark (candle-close
fallback), at-fill exit = entry exactly (mark_pct 0); one close per trade → fees
unchanged. Additional pull: 5m closes per book symbol (chunked, lower time bounds) for
the vol feature.

### Features at instant t (all information ≤ t; leakage-checked per feature)

| # | feature | source |
|---|---|---|
| F1 | current `mark_pct` (signed by direction) | hourly mark at t (0 at fill) |
| F2 | drawdown from the running peak: `max(mark_pct at instants ≤ t, 0) − mark_pct(t)` | hourly marks; peak at hourly resolution, NOT the bot's 10s `peak_pct` — same resolution in fit and replay, documented approximation |
| F3 | time in trade (hours) | t − filled_at |
| F4 | own 4h vol: `core.vol_features.rolling_std_pct` over 5m closes ≤ t (window 48) | the T-110-validated shared builder — no reimplementation; NaN during warm-up → sample dropped (bot must not act on a feature it does not have) |
| F5 | direction (LONG=1, SHORT=0) | position |
| F6 | BTC tape TD1 flag at t (as a FEATURE, not a rule) | T-139 `tape_down_series` |

### Label and model

- Label per trade (training only): **bad = booked `close_mark_pct` < 0**. Every instant
  sample of a trade carries its trade's terminal label.
- Per-trade sample cap: the first 72 hourly instants (long trades would otherwise
  dominate; no reweighting beyond the cap — documented simplification).
- Model: logistic regression in plain numpy (deterministic full-batch gradient descent,
  L2 λ=1e-3, features standardized on FIT statistics). Trained ONLY on FIT trades.

### Gate and selection (frozen before outcomes)

- Gate: the first evaluation instant with P(bad | state) > θ closes the trade at that
  instant's mark.
- FIT/HOLDOUT split identical to T-139: FIT = Bot 40 `filled_at` < 2026-08-04 00:00 UTC;
  HOLDOUT = Bot 40 ≥ cutoff + ALL Bot 44 (both books also reported separately).
- Selection grid on FIT only: scope {ALL positions, LONG-only} × θ {0.5, 0.6, 0.7, 0.8,
  0.9} = 10 cells, winner by net Σdelta, ties broken toward the higher θ (fewer exits).
  The single winner is evaluated ONCE on HOLDOUT. No second look.

### Success criteria (frozen — identical to T-139)

PASS iff on HOLDOUT: (1) paired Δ mean > 0 with t ≥ 2.0 (zeros included); (2) Σdelta > 0
on each holdout book individually; (3) the exit-mix table shows the negative
SL_HIT + SOURCE_CLOSED mass genuinely reduced, not renamed into an equally large GATE
bucket. Anything else → NO-EDGE, gate not wired. Additionally reported (not
verdict-bearing): holdout AUC of the frozen model on trade-terminal labels, and the
kill count of future TRAIL winners vs stage 1's 745.

### Guardrails

- The model may not see any terminal information at scoring time; labels exist only in
  FIT training. Vol/candle features come from CLOSED candles ≤ t (hard rule 5 lives in
  the shared builder).
- Samples whose F4 is NaN (5m warm-up/gaps) are dropped from training; at scoring time a
  NaN-F4 instant cannot fire the gate (a bot must not act on a missing feature).
- Study only: wiring into bots 40/44 is a separate operator-gated deploy task after a
  PASS.

## 3. Results

*Appended after the run — absent until then by construction.*

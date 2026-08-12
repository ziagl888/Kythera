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

## 3. Results (run 2026-08-12)

**Verdict: NO-EDGE. The gate is not wired — but the negative is sharper than stage 1's.**

Snapshot: 5,274 trades, 48,895 instant rows (0 NaN-vol rows — the 5m pull covered every
instant), FIT training 19,229 rows, bad share 0.545. Frozen model weights (standardized):
`mark_pct −0.680 · vol_4h −0.444 · hours_in_trade −0.360 · drawdown_from_peak +0.110 ·
is_long +0.153 · btc_td1 −0.052`.

### FIT (10 cells, one look)

| cell | Σdelta | t | | cell | Σdelta | t |
|---|---|---|---|---|---|---|
| **LONG-only/θ0.5** (winner) | **+537.7** | +5.57 | | ALL/θ0.5 | +438.6 | +3.80 |
| LONG-only/θ0.6 | +150.3 | +1.86 | | ALL/θ0.6 | +90.7 | +0.93 |
| LONG-only/θ0.7 | +54.8 | +0.84 | | ALL/θ0.7 | +40.8 | +0.52 |
| LONG-only/θ0.9 | +22.8 | +0.98 | | ALL/θ0.8 | +41.0 | +0.72 |
| LONG-only/θ0.8 | +0.4 | +0.01 | | ALL/θ0.9 | +24.3 | +0.83 |

### HOLDOUT (LONG-only/θ0.5, evaluated once)

| book | n | Σdelta | mean | t |
|---|---|---|---|---|
| all | 3,959 | **−269.9** | −0.068 | **−2.96** |
| Bot 40 holdout | 2,571 | −296.6 | −0.115 | −4.10 |
| Bot 44 | 1,388 | +26.8 | +0.019 | +0.48 |

Criterion 1 (mean > 0, t ≥ 2): **failed — significantly negative.**
Criterion 2 (Σ > 0 both books): **failed** (Bot 40 holdout −296.6).
Criterion 3: SL_HIT mass falls 257→214 (−1,054→−834), SOURCE_CLOSED 1,186→973
(−1,805→−1,467), the GATE bucket is cheap (1,247 exits at Ø −0.37) — and the gate still
loses because it kills **497 future TRAIL winners** (1,867→1,370; ~−1,056 of forgone
trail profit).

### Reading — the sharpest finding of the pair

**Prediction works; the action does not.** The frozen model carries real out-of-sample
signal — instant-level holdout AUC **0.698**, driven by per-position state exactly as
T-110 suggested. Yet acting on that signal by exiting early loses money on the same
holdout. The trailing book's structure explains it: losers mostly die LATE (SOURCE_CLOSED
Ø −1.5, TIME_STOP Ø −1.5), so an early exit saves only a fraction of a stop, while every
false positive forfeits a full +2.17 trail win. With bad-share 0.545 and Ø(win) ≈
|Ø(saved loss)| the asymmetry eats the AUC entirely. A hazard gate would need either far
higher precision at low recall (θ0.8-0.9 cells were already ≈ 0 in FIT) or a cheaper
action than a full exit (e.g. tightening the trail instead of closing — untested here).

Both stages now agree on the null: **runtime exit overlays on this book do not pay, even
with a genuinely predictive model.** The standing levers remain admission thinning
(T-134 watchlist — the fee drag: book Ø/trade < 0.10 % fee) and, if anything, an
action-side redesign (trail tightening) as a NEW pre-registered study, not a rerun of
this one.

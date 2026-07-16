# K2 · XSM1/XSR1 — Cross-Section Momentum-Rotation & Alt-Pump-Reversal (T-2026-CU-9050-143)

_Generated 2026-07-16T23:10:30.794186+00:00 · read-only two-stage study · fee/side 0.0005 (round-trip 0.0010) · 4 coins · status partial (sampling cap)_

## Acceptance Criteria (§K2, binary)

_Graded against this run; items marked (full-run) only fully verify without the sampling cap._

- ✅ **F×H grid complete: 5×3** — F∈[7, 14, 28, 56, 84], H∈[7, 14, 28] enumerated in `run_stage1`.
- ✅ **both signal variants** raw + anchored-to-formation-low — `signal_vec(variant=...)` (['raw', 'anchored']).
- ✅ **both reference frames** absolute + market-neutral (coin−BTC) — `FRAMES=['absolute', 'market_neutral']`, BTC signal subtracted.
- ✅ **liquidity filter** bottom volume tercile excluded — median quote-vol over F, `np.quantile(...,1/3)` cut.
- ✅ **stage-1 decile spreads NET of fees (Regel 10) + short-side funding, correct sign** — LONG net=mean(fwd_top)−fee; SHORT net=mean(−fwd+Σfunding)−fee; short receives +Σ funding_rate (pays when funding<0).
- ✅ **F×H heatmap per variant/direction** — see Heatmaps section (all 2·2·2 panels).
- ✅ **stage-2 event-replay gated to val-positive cells only, simulate_exit as-of** — `run_stage2` runs iff val-positive; get_hvn_and_sr_levels(df=as-of 95d 1h)→simulate_exit (ran 60 cell(s)).
- ✅ **chrono val/test, cell selection on val only** — midpoint split; `val_positive_cells` selects on val, test read once.
- ✅ **survivorship documented, fill_method=None** — coins.json active perps; no forward-fill (NaN-propagating returns).
- ✅ **stop-criterion → no-op verdict valid** — `derive_verdict` emits `no-op/structure-does-not-replicate` when no cell passes.
- ✅ **status field** complete/partial — this run: `partial (sampling cap)`.
- ✅ **resume/checkpoint state in OS-temp not repo** — `C:\Users\Michael\AppData\Local\Temp\xs_momentum_study_state.json` (OS temp dir).
- ⚠ **(full-run)** statistical PASS/verdict validity — this is a SAMPLING-CAPPED smoke (limit_symbols=4, max_weeks=6); numbers are not decisive.

## Reuse verdict (Phase 0b)

**Build, not Reuse/Extend.** `tools/tsmom_study.py` is the resume/checkpoint + reporting TEMPLATE (streaming accumulators, OS-temp atomic state, --resume, verdict/status contract) and is mirrored here. But the analysis is genuinely new: tsmom is a per-coin time-series signal, whereas K2 is a CROSS-SECTIONAL decile-spread over a coin×date panel with per-rebalance ranking, market-neutral (coin−BTC) frame, liquidity tercile and short-side funding — none of which exist in the fleet. A new script is the right call.

**VERDICT: no-op/structure-does-not-replicate**

- grid cells: 120 · val-positive: 60 · PASSING (val>0 AND test>0, ≥4 rebal/half): **0**

- best cell selected on VAL: `F56|H28|anchored|absolute|XSR1_SHORT` → val 8.4613% (n=6) · **test None% (n=0, WR=None)**


Stop-criterion (§K2): no F×H cell with a val+test-consistent net spread ⇒ the structure does not replicate on 2024-26 perps — a documented NEGATIVE verdict is SUCCESS (No-op-Done), never forced positive. Cell selection is ONLY on val; test is read once.

## Stage 2 — event-replay (our geometry, val-positive cells only)

| cell | direction | n_events | geo avg net % | geo WR |
|---|---|--:|--:|--:|
| F14|H14|anchored|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F14|H14|anchored|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F14|H14|raw|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F14|H14|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F14|H28|anchored|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F14|H28|anchored|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F14|H28|raw|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F14|H28|raw|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F14|H7|anchored|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F14|H7|anchored|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F14|H7|raw|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F14|H7|raw|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F28|H14|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F28|H14|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F28|H14|raw|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F28|H14|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F28|H28|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F28|H28|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F28|H28|raw|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F28|H28|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F28|H7|anchored|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F28|H7|anchored|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F28|H7|raw|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F28|H7|raw|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F56|H14|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F56|H14|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F56|H14|raw|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F56|H14|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F56|H28|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F56|H28|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F56|H28|raw|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F56|H28|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F56|H7|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F56|H7|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F56|H7|raw|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F56|H7|raw|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F7|H14|anchored|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F7|H14|anchored|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F7|H14|raw|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F7|H14|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F7|H28|anchored|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F7|H28|anchored|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F7|H28|raw|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F7|H28|raw|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F7|H7|anchored|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F7|H7|anchored|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F7|H7|raw|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F7|H7|raw|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F84|H14|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F84|H14|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F84|H14|raw|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F84|H14|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F84|H28|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F84|H28|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F84|H28|raw|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F84|H28|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F84|H7|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F84|H7|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 0 | None | None |
| F84|H7|raw|absolute|XSM1_LONG | XSM1_LONG | 0 | None | None |
| F84|H7|raw|market_neutral|XSM1_LONG | XSM1_LONG | 0 | None | None |

## Heatmaps — F×H net PnL per variant/frame/direction

### XSM1_LONG · raw · absolute (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | – (1.8255) | – (-0.5414) | – (1.2888) |
| F14 | – (1.8255) | – (-0.5414) | – (1.2888) |
| F28 | – (1.5477) | – (-1.5651) | – (-5.3061) |
| F56 | – (1.5477) | – (-1.5651) | – (-5.3061) |
| F84 | – (1.5477) | – (-1.5651) | – (-5.3061) |

### XSM1_LONG · raw · market_neutral (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | – (1.8255) | – (-0.5414) | – (1.2888) |
| F14 | – (1.8255) | – (-0.5414) | – (1.2888) |
| F28 | – (1.5477) | – (-1.5651) | – (-5.3061) |
| F56 | – (1.5477) | – (-1.5651) | – (-5.3061) |
| F84 | – (1.5477) | – (-1.5651) | – (-5.3061) |

### XSM1_LONG · anchored · absolute (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | – (2.275) | – (0.0991) | – (2.5945) |
| F14 | – (2.275) | – (0.0991) | – (2.5945) |
| F28 | – (1.6244) | – (-0.8118) | – (-1.7367) |
| F56 | – (-1.1418) | – (-5.3478) | – (-8.6613) |
| F84 | – (-1.1418) | – (-5.3478) | – (-8.6613) |

### XSM1_LONG · anchored · market_neutral (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | – (2.275) | – (0.0991) | – (2.5945) |
| F14 | – (2.275) | – (0.0991) | – (2.5945) |
| F28 | – (1.6244) | – (-0.8118) | – (-1.7367) |
| F56 | – (-1.1418) | – (-5.3478) | – (-8.6613) |
| F84 | – (-1.1418) | – (-5.3478) | – (-8.6613) |

### XSR1_SHORT · raw · absolute (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | – (-2.0255) | – (0.3414) | – (-1.4888) |
| F14 | – (-2.0255) | – (0.3414) | – (-1.4888) |
| F28 | – (-1.7477) | – (1.3651) | – (5.1061) |
| F56 | – (-1.7477) | – (1.3651) | – (5.1061) |
| F84 | – (-1.7477) | – (1.3651) | – (5.1061) |

### XSR1_SHORT · raw · market_neutral (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | – (-2.0255) | – (0.3414) | – (-1.4888) |
| F14 | – (-2.0255) | – (0.3414) | – (-1.4888) |
| F28 | – (-1.7477) | – (1.3651) | – (5.1061) |
| F56 | – (-1.7477) | – (1.3651) | – (5.1061) |
| F84 | – (-1.7477) | – (1.3651) | – (5.1061) |

### XSR1_SHORT · anchored · absolute (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | – (-2.475) | – (-0.2991) | – (-2.7945) |
| F14 | – (-2.475) | – (-0.2991) | – (-2.7945) |
| F28 | – (-1.8244) | – (0.6118) | – (1.5367) |
| F56 | – (0.9418) | – (5.1478) | – (8.4613) |
| F84 | – (0.9418) | – (5.1478) | – (8.4613) |

### XSR1_SHORT · anchored · market_neutral (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | – (-2.475) | – (-0.2991) | – (-2.7945) |
| F14 | – (-2.475) | – (-0.2991) | – (-2.7945) |
| F28 | – (-1.8244) | – (0.6118) | – (1.5367) |
| F56 | – (0.9418) | – (5.1478) | – (8.4613) |
| F84 | – (0.9418) | – (5.1478) | – (8.4613) |

## Full grid — stage-1 net PnL, chrono val/test split

| cell | all n | all avg% | all WR | val n | val avg% | test n | test avg% | spread(top−bot)% | short fund bps |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| F14|H14|anchored|absolute|XSM1_LONG | 6 | 0.0991 | 0.1667 | 6 | 0.0991 | 0 | None | 6.4784 | None |
| F14|H14|anchored|absolute|XSR1_SHORT | 6 | -0.2991 | 0.8333 | 6 | -0.2991 | 0 | None | 6.4784 | 0.0 |
| F14|H14|anchored|market_neutral|XSM1_LONG | 6 | 0.0991 | 0.1667 | 6 | 0.0991 | 0 | None | 6.4784 | None |
| F14|H14|anchored|market_neutral|XSR1_SHORT | 6 | -0.2991 | 0.8333 | 6 | -0.2991 | 0 | None | 6.4784 | 0.0 |
| F14|H14|raw|absolute|XSM1_LONG | 6 | -0.5414 | 0.1667 | 6 | -0.5414 | 0 | None | 5.1974 | None |
| F14|H14|raw|absolute|XSR1_SHORT | 6 | 0.3414 | 0.8333 | 6 | 0.3414 | 0 | None | 5.1974 | 0.0 |
| F14|H14|raw|market_neutral|XSM1_LONG | 6 | -0.5414 | 0.1667 | 6 | -0.5414 | 0 | None | 5.1974 | None |
| F14|H14|raw|market_neutral|XSR1_SHORT | 6 | 0.3414 | 0.8333 | 6 | 0.3414 | 0 | None | 5.1974 | 0.0 |
| F14|H28|anchored|absolute|XSM1_LONG | 6 | 2.5945 | 0.5 | 6 | 2.5945 | 0 | None | 11.3973 | None |
| F14|H28|anchored|absolute|XSR1_SHORT | 6 | -2.7945 | 0.5 | 6 | -2.7945 | 0 | None | 11.3973 | 0.0 |
| F14|H28|anchored|market_neutral|XSM1_LONG | 6 | 2.5945 | 0.5 | 6 | 2.5945 | 0 | None | 11.3973 | None |
| F14|H28|anchored|market_neutral|XSR1_SHORT | 6 | -2.7945 | 0.5 | 6 | -2.7945 | 0 | None | 11.3973 | 0.0 |
| F14|H28|raw|absolute|XSM1_LONG | 6 | 1.2888 | 0.5 | 6 | 1.2888 | 0 | None | 8.7859 | None |
| F14|H28|raw|absolute|XSR1_SHORT | 6 | -1.4888 | 0.5 | 6 | -1.4888 | 0 | None | 8.7859 | 0.0 |
| F14|H28|raw|market_neutral|XSM1_LONG | 6 | 1.2888 | 0.5 | 6 | 1.2888 | 0 | None | 8.7859 | None |
| F14|H28|raw|market_neutral|XSR1_SHORT | 6 | -1.4888 | 0.5 | 6 | -1.4888 | 0 | None | 8.7859 | 0.0 |
| F14|H7|anchored|absolute|XSM1_LONG | 6 | 2.275 | 0.3333 | 6 | 2.275 | 0 | None | 4.6755 | None |
| F14|H7|anchored|absolute|XSR1_SHORT | 6 | -2.475 | 0.6667 | 6 | -2.475 | 0 | None | 4.6755 | 0.0 |
| F14|H7|anchored|market_neutral|XSM1_LONG | 6 | 2.275 | 0.3333 | 6 | 2.275 | 0 | None | 4.6755 | None |
| F14|H7|anchored|market_neutral|XSR1_SHORT | 6 | -2.475 | 0.6667 | 6 | -2.475 | 0 | None | 4.6755 | 0.0 |
| F14|H7|raw|absolute|XSM1_LONG | 6 | 1.8255 | 0.3333 | 6 | 1.8255 | 0 | None | 3.7767 | None |
| F14|H7|raw|absolute|XSR1_SHORT | 6 | -2.0255 | 0.6667 | 6 | -2.0255 | 0 | None | 3.7767 | 0.0 |
| F14|H7|raw|market_neutral|XSM1_LONG | 6 | 1.8255 | 0.3333 | 6 | 1.8255 | 0 | None | 3.7767 | None |
| F14|H7|raw|market_neutral|XSR1_SHORT | 6 | -2.0255 | 0.6667 | 6 | -2.0255 | 0 | None | 3.7767 | 0.0 |
| F28|H14|anchored|absolute|XSM1_LONG | 6 | -0.8118 | 0.1667 | 6 | -0.8118 | 0 | None | 4.6565 | None |
| F28|H14|anchored|absolute|XSR1_SHORT | 6 | 0.6118 | 0.8333 | 6 | 0.6118 | 0 | None | 4.6565 | 0.0 |
| F28|H14|anchored|market_neutral|XSM1_LONG | 6 | -0.8118 | 0.1667 | 6 | -0.8118 | 0 | None | 4.6565 | None |
| F28|H14|anchored|market_neutral|XSR1_SHORT | 6 | 0.6118 | 0.8333 | 6 | 0.6118 | 0 | None | 4.6565 | 0.0 |
| F28|H14|raw|absolute|XSM1_LONG | 6 | -1.5651 | 0.1667 | 6 | -1.5651 | 0 | None | 3.15 | None |
| F28|H14|raw|absolute|XSR1_SHORT | 6 | 1.3651 | 0.8333 | 6 | 1.3651 | 0 | None | 3.15 | 0.0 |
| F28|H14|raw|market_neutral|XSM1_LONG | 6 | -1.5651 | 0.1667 | 6 | -1.5651 | 0 | None | 3.15 | None |
| F28|H14|raw|market_neutral|XSR1_SHORT | 6 | 1.3651 | 0.8333 | 6 | 1.3651 | 0 | None | 3.15 | 0.0 |
| F28|H28|anchored|absolute|XSM1_LONG | 6 | -1.7367 | 0.3333 | 6 | -1.7367 | 0 | None | 2.7349 | None |
| F28|H28|anchored|absolute|XSR1_SHORT | 6 | 1.5367 | 0.6667 | 6 | 1.5367 | 0 | None | 2.7349 | 0.0 |
| F28|H28|anchored|market_neutral|XSM1_LONG | 6 | -1.7367 | 0.3333 | 6 | -1.7367 | 0 | None | 2.7349 | None |
| F28|H28|anchored|market_neutral|XSR1_SHORT | 6 | 1.5367 | 0.6667 | 6 | 1.5367 | 0 | None | 2.7349 | 0.0 |
| F28|H28|raw|absolute|XSM1_LONG | 6 | -5.3061 | 0.3333 | 6 | -5.3061 | 0 | None | -4.4039 | None |
| F28|H28|raw|absolute|XSR1_SHORT | 6 | 5.1061 | 0.6667 | 6 | 5.1061 | 0 | None | -4.4039 | 0.0 |
| F28|H28|raw|market_neutral|XSM1_LONG | 6 | -5.3061 | 0.3333 | 6 | -5.3061 | 0 | None | -4.4039 | None |
| F28|H28|raw|market_neutral|XSR1_SHORT | 6 | 5.1061 | 0.6667 | 6 | 5.1061 | 0 | None | -4.4039 | 0.0 |
| F28|H7|anchored|absolute|XSM1_LONG | 6 | 1.6244 | 0.3333 | 6 | 1.6244 | 0 | None | 3.3743 | None |
| F28|H7|anchored|absolute|XSR1_SHORT | 6 | -1.8244 | 0.6667 | 6 | -1.8244 | 0 | None | 3.3743 | 0.0 |
| F28|H7|anchored|market_neutral|XSM1_LONG | 6 | 1.6244 | 0.3333 | 6 | 1.6244 | 0 | None | 3.3743 | None |
| F28|H7|anchored|market_neutral|XSR1_SHORT | 6 | -1.8244 | 0.6667 | 6 | -1.8244 | 0 | None | 3.3743 | 0.0 |
| F28|H7|raw|absolute|XSM1_LONG | 6 | 1.5477 | 0.3333 | 6 | 1.5477 | 0 | None | 3.2211 | None |
| F28|H7|raw|absolute|XSR1_SHORT | 6 | -1.7477 | 0.6667 | 6 | -1.7477 | 0 | None | 3.2211 | 0.0 |
| F28|H7|raw|market_neutral|XSM1_LONG | 6 | 1.5477 | 0.3333 | 6 | 1.5477 | 0 | None | 3.2211 | None |
| F28|H7|raw|market_neutral|XSR1_SHORT | 6 | -1.7477 | 0.6667 | 6 | -1.7477 | 0 | None | 3.2211 | 0.0 |
| F56|H14|anchored|absolute|XSM1_LONG | 6 | -5.3478 | 0.0 | 6 | -5.3478 | 0 | None | -4.4154 | None |
| F56|H14|anchored|absolute|XSR1_SHORT | 6 | 5.1478 | 1.0 | 6 | 5.1478 | 0 | None | -4.4154 | 0.0 |
| F56|H14|anchored|market_neutral|XSM1_LONG | 6 | -5.3478 | 0.0 | 6 | -5.3478 | 0 | None | -4.4154 | None |
| F56|H14|anchored|market_neutral|XSR1_SHORT | 6 | 5.1478 | 1.0 | 6 | 5.1478 | 0 | None | -4.4154 | 0.0 |
| F56|H14|raw|absolute|XSM1_LONG | 6 | -1.5651 | 0.1667 | 6 | -1.5651 | 0 | None | 3.15 | None |
| F56|H14|raw|absolute|XSR1_SHORT | 6 | 1.3651 | 0.8333 | 6 | 1.3651 | 0 | None | 3.15 | 0.0 |
| F56|H14|raw|market_neutral|XSM1_LONG | 6 | -1.5651 | 0.1667 | 6 | -1.5651 | 0 | None | 3.15 | None |
| F56|H14|raw|market_neutral|XSR1_SHORT | 6 | 1.3651 | 0.8333 | 6 | 1.3651 | 0 | None | 3.15 | 0.0 |
| F56|H28|anchored|absolute|XSM1_LONG | 6 | -8.6613 | 0.1667 | 6 | -8.6613 | 0 | None | -11.1144 | None |
| F56|H28|anchored|absolute|XSR1_SHORT | 6 | 8.4613 | 0.8333 | 6 | 8.4613 | 0 | None | -11.1144 | 0.0 |
| F56|H28|anchored|market_neutral|XSM1_LONG | 6 | -8.6613 | 0.1667 | 6 | -8.6613 | 0 | None | -11.1144 | None |
| F56|H28|anchored|market_neutral|XSR1_SHORT | 6 | 8.4613 | 0.8333 | 6 | 8.4613 | 0 | None | -11.1144 | 0.0 |
| F56|H28|raw|absolute|XSM1_LONG | 6 | -5.3061 | 0.3333 | 6 | -5.3061 | 0 | None | -4.4039 | None |
| F56|H28|raw|absolute|XSR1_SHORT | 6 | 5.1061 | 0.6667 | 6 | 5.1061 | 0 | None | -4.4039 | 0.0 |
| F56|H28|raw|market_neutral|XSM1_LONG | 6 | -5.3061 | 0.3333 | 6 | -5.3061 | 0 | None | -4.4039 | None |
| F56|H28|raw|market_neutral|XSR1_SHORT | 6 | 5.1061 | 0.6667 | 6 | 5.1061 | 0 | None | -4.4039 | 0.0 |
| F56|H7|anchored|absolute|XSM1_LONG | 6 | -1.1418 | 0.3333 | 6 | -1.1418 | 0 | None | -2.158 | None |
| F56|H7|anchored|absolute|XSR1_SHORT | 6 | 0.9418 | 0.6667 | 6 | 0.9418 | 0 | None | -2.158 | 0.0 |
| F56|H7|anchored|market_neutral|XSM1_LONG | 6 | -1.1418 | 0.3333 | 6 | -1.1418 | 0 | None | -2.158 | None |
| F56|H7|anchored|market_neutral|XSR1_SHORT | 6 | 0.9418 | 0.6667 | 6 | 0.9418 | 0 | None | -2.158 | 0.0 |
| F56|H7|raw|absolute|XSM1_LONG | 6 | 1.5477 | 0.3333 | 6 | 1.5477 | 0 | None | 3.2211 | None |
| F56|H7|raw|absolute|XSR1_SHORT | 6 | -1.7477 | 0.6667 | 6 | -1.7477 | 0 | None | 3.2211 | 0.0 |
| F56|H7|raw|market_neutral|XSM1_LONG | 6 | 1.5477 | 0.3333 | 6 | 1.5477 | 0 | None | 3.2211 | None |
| F56|H7|raw|market_neutral|XSR1_SHORT | 6 | -1.7477 | 0.6667 | 6 | -1.7477 | 0 | None | 3.2211 | 0.0 |
| F7|H14|anchored|absolute|XSM1_LONG | 6 | 0.0991 | 0.1667 | 6 | 0.0991 | 0 | None | 6.4784 | None |
| F7|H14|anchored|absolute|XSR1_SHORT | 6 | -0.2991 | 0.8333 | 6 | -0.2991 | 0 | None | 6.4784 | 0.0 |
| F7|H14|anchored|market_neutral|XSM1_LONG | 6 | 0.0991 | 0.1667 | 6 | 0.0991 | 0 | None | 6.4784 | None |
| F7|H14|anchored|market_neutral|XSR1_SHORT | 6 | -0.2991 | 0.8333 | 6 | -0.2991 | 0 | None | 6.4784 | 0.0 |
| F7|H14|raw|absolute|XSM1_LONG | 6 | -0.5414 | 0.1667 | 6 | -0.5414 | 0 | None | 5.1974 | None |
| F7|H14|raw|absolute|XSR1_SHORT | 6 | 0.3414 | 0.8333 | 6 | 0.3414 | 0 | None | 5.1974 | 0.0 |
| F7|H14|raw|market_neutral|XSM1_LONG | 6 | -0.5414 | 0.1667 | 6 | -0.5414 | 0 | None | 5.1974 | None |
| F7|H14|raw|market_neutral|XSR1_SHORT | 6 | 0.3414 | 0.8333 | 6 | 0.3414 | 0 | None | 5.1974 | 0.0 |
| F7|H28|anchored|absolute|XSM1_LONG | 6 | 2.5945 | 0.5 | 6 | 2.5945 | 0 | None | 11.3973 | None |
| F7|H28|anchored|absolute|XSR1_SHORT | 6 | -2.7945 | 0.5 | 6 | -2.7945 | 0 | None | 11.3973 | 0.0 |
| F7|H28|anchored|market_neutral|XSM1_LONG | 6 | 2.5945 | 0.5 | 6 | 2.5945 | 0 | None | 11.3973 | None |
| F7|H28|anchored|market_neutral|XSR1_SHORT | 6 | -2.7945 | 0.5 | 6 | -2.7945 | 0 | None | 11.3973 | 0.0 |
| F7|H28|raw|absolute|XSM1_LONG | 6 | 1.2888 | 0.5 | 6 | 1.2888 | 0 | None | 8.7859 | None |
| F7|H28|raw|absolute|XSR1_SHORT | 6 | -1.4888 | 0.5 | 6 | -1.4888 | 0 | None | 8.7859 | 0.0 |
| F7|H28|raw|market_neutral|XSM1_LONG | 6 | 1.2888 | 0.5 | 6 | 1.2888 | 0 | None | 8.7859 | None |
| F7|H28|raw|market_neutral|XSR1_SHORT | 6 | -1.4888 | 0.5 | 6 | -1.4888 | 0 | None | 8.7859 | 0.0 |
| F7|H7|anchored|absolute|XSM1_LONG | 6 | 2.275 | 0.3333 | 6 | 2.275 | 0 | None | 4.6755 | None |
| F7|H7|anchored|absolute|XSR1_SHORT | 6 | -2.475 | 0.6667 | 6 | -2.475 | 0 | None | 4.6755 | 0.0 |
| F7|H7|anchored|market_neutral|XSM1_LONG | 6 | 2.275 | 0.3333 | 6 | 2.275 | 0 | None | 4.6755 | None |
| F7|H7|anchored|market_neutral|XSR1_SHORT | 6 | -2.475 | 0.6667 | 6 | -2.475 | 0 | None | 4.6755 | 0.0 |
| F7|H7|raw|absolute|XSM1_LONG | 6 | 1.8255 | 0.3333 | 6 | 1.8255 | 0 | None | 3.7767 | None |
| F7|H7|raw|absolute|XSR1_SHORT | 6 | -2.0255 | 0.6667 | 6 | -2.0255 | 0 | None | 3.7767 | 0.0 |
| F7|H7|raw|market_neutral|XSM1_LONG | 6 | 1.8255 | 0.3333 | 6 | 1.8255 | 0 | None | 3.7767 | None |
| F7|H7|raw|market_neutral|XSR1_SHORT | 6 | -2.0255 | 0.6667 | 6 | -2.0255 | 0 | None | 3.7767 | 0.0 |
| F84|H14|anchored|absolute|XSM1_LONG | 6 | -5.3478 | 0.0 | 6 | -5.3478 | 0 | None | -4.4154 | None |
| F84|H14|anchored|absolute|XSR1_SHORT | 6 | 5.1478 | 1.0 | 6 | 5.1478 | 0 | None | -4.4154 | 0.0 |
| F84|H14|anchored|market_neutral|XSM1_LONG | 6 | -5.3478 | 0.0 | 6 | -5.3478 | 0 | None | -4.4154 | None |
| F84|H14|anchored|market_neutral|XSR1_SHORT | 6 | 5.1478 | 1.0 | 6 | 5.1478 | 0 | None | -4.4154 | 0.0 |
| F84|H14|raw|absolute|XSM1_LONG | 6 | -1.5651 | 0.1667 | 6 | -1.5651 | 0 | None | 3.15 | None |
| F84|H14|raw|absolute|XSR1_SHORT | 6 | 1.3651 | 0.8333 | 6 | 1.3651 | 0 | None | 3.15 | 0.0 |
| F84|H14|raw|market_neutral|XSM1_LONG | 6 | -1.5651 | 0.1667 | 6 | -1.5651 | 0 | None | 3.15 | None |
| F84|H14|raw|market_neutral|XSR1_SHORT | 6 | 1.3651 | 0.8333 | 6 | 1.3651 | 0 | None | 3.15 | 0.0 |
| F84|H28|anchored|absolute|XSM1_LONG | 6 | -8.6613 | 0.1667 | 6 | -8.6613 | 0 | None | -11.1144 | None |
| F84|H28|anchored|absolute|XSR1_SHORT | 6 | 8.4613 | 0.8333 | 6 | 8.4613 | 0 | None | -11.1144 | 0.0 |
| F84|H28|anchored|market_neutral|XSM1_LONG | 6 | -8.6613 | 0.1667 | 6 | -8.6613 | 0 | None | -11.1144 | None |
| F84|H28|anchored|market_neutral|XSR1_SHORT | 6 | 8.4613 | 0.8333 | 6 | 8.4613 | 0 | None | -11.1144 | 0.0 |
| F84|H28|raw|absolute|XSM1_LONG | 6 | -5.3061 | 0.3333 | 6 | -5.3061 | 0 | None | -4.4039 | None |
| F84|H28|raw|absolute|XSR1_SHORT | 6 | 5.1061 | 0.6667 | 6 | 5.1061 | 0 | None | -4.4039 | 0.0 |
| F84|H28|raw|market_neutral|XSM1_LONG | 6 | -5.3061 | 0.3333 | 6 | -5.3061 | 0 | None | -4.4039 | None |
| F84|H28|raw|market_neutral|XSR1_SHORT | 6 | 5.1061 | 0.6667 | 6 | 5.1061 | 0 | None | -4.4039 | 0.0 |
| F84|H7|anchored|absolute|XSM1_LONG | 6 | -1.1418 | 0.3333 | 6 | -1.1418 | 0 | None | -2.158 | None |
| F84|H7|anchored|absolute|XSR1_SHORT | 6 | 0.9418 | 0.6667 | 6 | 0.9418 | 0 | None | -2.158 | 0.0 |
| F84|H7|anchored|market_neutral|XSM1_LONG | 6 | -1.1418 | 0.3333 | 6 | -1.1418 | 0 | None | -2.158 | None |
| F84|H7|anchored|market_neutral|XSR1_SHORT | 6 | 0.9418 | 0.6667 | 6 | 0.9418 | 0 | None | -2.158 | 0.0 |
| F84|H7|raw|absolute|XSM1_LONG | 6 | 1.5477 | 0.3333 | 6 | 1.5477 | 0 | None | 3.2211 | None |
| F84|H7|raw|absolute|XSR1_SHORT | 6 | -1.7477 | 0.6667 | 6 | -1.7477 | 0 | None | 3.2211 | 0.0 |
| F84|H7|raw|market_neutral|XSM1_LONG | 6 | 1.5477 | 0.3333 | 6 | 1.5477 | 0 | None | 3.2211 | None |
| F84|H7|raw|market_neutral|XSR1_SHORT | 6 | -1.7477 | 0.6667 | 6 | -1.7477 | 0 | None | 3.2211 | 0.0 |

## Population & caveats

- run status: partial (sampling cap) · coins loaded: 4 of 527
- rebalance rows (weekly raster): 6
- peak process RSS: 133.5 MB (panel O(coins×days) + streaming cell accumulators)
- chrono val/test split (UTC): 2025-05-05T00:00:00+00:00 — fixed midpoint of the BTCUSDT 1d window; val=earlier, test=later
- signal variants: raw F-day return; anchored = close/min(low over F) − 1 (distance to formation low, F5)
- reference frames: absolute; market-neutral = coin signal − BTCUSDT signal (removes beta)
- liquidity: exclude bottom volume tercile by median quote-vol over F; quote-vol ≈ base volume × close (the candles table has no quote_asset_volume column — documented approximation)
- decile size = max(1, round(n_liquid·0.1)); ranking on the liquid set only, BTCUSDT excluded from the cross-section
- short-side funding: net_short = mean(−fwd + Σ funding_rate[hold]) − fee; a short RECEIVES funding when funding_rate>0 and PAYS when <0 (spec: Shorts zahlen bei negativem Funding); funding summed over [t, t+H)
- fees: round-trip taker 0.0010 = 2·FEE_PER_SIDE (walkforward_sim, Regel 10 — not reinvented)
- funding_features.py note: core/funding_features.py is the 6-feature as-of ROLLING builder (fund_24h/72h/…); here we need the RAW Σ funding_rate over the exact hold window, so we read funding_rates directly
- **Survivorship bias (Rule 9, strongest here)**: coins.json lists ACTIVE USDT-perps; delisted coins are absent → the replayed cross-section skews to survivors. Returns use fill_method=None (no forward-fill across gaps); a coin missing close[t−F], close[t] or close[t+H] simply drops out of that rebalance.
- **Only closed candles (R1)**: read_candles(include_forming=False); 1d klines anchored 00:00 UTC.
- **WR is not decisive (Rule 8)**: the verdict rests on net-PnL expectancy consistent across the chrono halves.
- CPU-check override: --skip-cpu-check=True (VPS is CPU-saturated; the read-only BELOW_NORMAL job bypasses the walkforward_sim guard deliberately).
- ⚠ SAMPLING CAP: --limit-symbols=4 --max-weeks=6 (NOT a full run — numbers are illustrative, verdict not statistically decisive).
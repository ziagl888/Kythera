# K2 · XSM1/XSR1 — Cross-Section Momentum-Rotation & Alt-Pump-Reversal (T-2026-CU-9050-143)

_Generated 2026-07-17T00:57:38.896426+00:00 · read-only two-stage study · fee/side 0.0005 (round-trip 0.0010) · 527 coins · status complete_

## Acceptance Criteria (§K2, binary)

_Graded against this run; items marked (full-run) only fully verify without the sampling cap._

- ✅ **F×H grid complete: 5×3** — F∈[7, 14, 28, 56, 84], H∈[7, 14, 28] enumerated in `run_stage1`.
- ✅ **both signal variants** raw + anchored-to-formation-low — `signal_vec(variant=...)` (['raw', 'anchored']).
- ⚠ **both frames present but market-neutral is a KNOWN-LIMITATION no-op** — `FRAMES=['absolute', 'market_neutral']`; the BTC-signal subtraction is a per-rebalance SCALAR shift (argsort-invariant) and PnL uses absolute coin returns, so every `market_neutral` cell is byte-identical to its `absolute` twin (60/60). Beta-removal is NOT actually tested here (follow-up: beta-adjust the RETURNS/spread). Does not change the negative verdict.
- ✅ **liquidity filter** bottom volume tercile excluded — median quote-vol over F, `np.quantile(...,1/3)` cut.
- ✅ **stage-1 decile spreads NET of fees (Regel 10) + short-side funding, correct sign** — LONG net=mean(fwd_top)−fee; SHORT net=mean(−fwd+Σfunding)−fee; short receives +Σ funding_rate (pays when funding<0).
- ✅ **F×H heatmap per variant/direction** — see Heatmaps section (all 2·2·2 panels).
- ⚠ **stage-2 (confirmatory) gated to val-positive cells; entry ~1 daily-bar EARLY (known limitation)** — `run_stage2` runs iff val-positive; get_hvn_and_sr_levels(df=as-of 95d 1h)→simulate_exit (ran 58 cell(s)). `dates[t]` is the daily OPEN (`floor('D')`) but the selection signal is `close[t]`, so stage-2 enters ~23h before the signal is observable — a look-ahead in the DIAGNOSTIC replay only; the stage-1-driven verdict is unaffected and stage-2 net is negative regardless. Follow-up: enter at `dates[t]+86400` (first 1h at/after the daily close).
- ✅ **chrono val/test, cell selection on val only** — midpoint split; `val_positive_cells` selects on val, test read once.
- ✅ **survivorship documented, fill_method=None** — coins.json active perps; no forward-fill (NaN-propagating returns).
- ✅ **stop-criterion → non-edge verdict valid** — `derive_verdict` requires a val+test-CONSISTENT cell (BOTH halves ≥ MIN_ROBUST_NET_PCT); otherwise `weak/inconsistent-spread` or `no-op/structure-does-not-replicate` (a near-zero val leg with a large test leg is overfitting, not an edge).
- ✅ **status field** complete/partial — this run: `complete`.
- ✅ **resume/checkpoint state in OS-temp not repo** — `C:\Users\Michael\AppData\Local\Temp\xs_momentum_study_state.json` (OS temp dir).

## Reuse verdict (Phase 0b)

**Build, not Reuse/Extend.** `tools/tsmom_study.py` is the resume/checkpoint + reporting TEMPLATE (streaming accumulators, OS-temp atomic state, --resume, verdict/status contract) and is mirrored here. But the analysis is genuinely new: tsmom is a per-coin time-series signal, whereas K2 is a CROSS-SECTIONAL decile-spread over a coin×date panel with per-rebalance ranking, market-neutral (coin−BTC) frame, liquidity tercile and short-side funding — none of which exist in the fleet. A new script is the right call.

**VERDICT: weak/inconsistent-spread (not deployable)**

- grid cells: 120 · val-positive: 58 · PASSING (val>0 AND test>0, ≥4 rebal/half): 8 · **ROBUST (both halves ≥0.3%/rebal = the spec's val+test-consistent): 0**

- ⚠ **The 8 'passing' cells are NOT robust:** their val leg is near-zero (< 0.3%/rebal) while test is large — a val+test INCONSISTENCY that is the overfitting signature, not a tradeable edge. With test WR < 0.5 (tail-driven) and the best-on-val cell flipping negative out-of-sample, the honest read is NO robust cross-section edge; nothing is licensed for deployment (operator decision regardless).

- best cell selected on VAL: `F84|H28|raw|absolute|XSR1_SHORT` → val 4.7424% (n=51) · **test -1.6124% (n=58, WR=0.5172)**


Stop-criterion (§K2): no F×H cell with a val+test-consistent net spread ⇒ the structure does not replicate on 2024-26 perps — a documented NEGATIVE verdict is SUCCESS (No-op-Done), never forced positive. Cell selection is ONLY on val; test is read once.

## Passing cells (val>0 AND test>0)

| cell | val avg% (n) | test avg% (n) | test WR |
|---|--:|--:|--:|
| F56|H14|anchored|absolute|XSM1_LONG | 0.02 (51) | 3.1146 (58) | 0.4828 |
| F56|H14|anchored|market_neutral|XSM1_LONG | 0.02 (51) | 3.1146 (58) | 0.4828 |
| F56|H7|anchored|absolute|XSM1_LONG | 0.0754 (51) | 2.0272 (58) | 0.5 |
| F56|H7|anchored|market_neutral|XSM1_LONG | 0.0754 (51) | 2.0272 (58) | 0.5 |
| F84|H7|anchored|absolute|XSM1_LONG | 0.0195 (51) | 1.889 (58) | 0.5 |
| F84|H7|anchored|market_neutral|XSM1_LONG | 0.0195 (51) | 1.889 (58) | 0.5 |
| F28|H14|raw|absolute|XSM1_LONG | 0.0066 (51) | 0.7516 (58) | 0.4483 |
| F28|H14|raw|market_neutral|XSM1_LONG | 0.0066 (51) | 0.7516 (58) | 0.4483 |

## Stage 2 — event-replay (our geometry, val-positive cells only)

| cell | direction | n_events | geo avg net % | geo WR |
|---|---|--:|--:|--:|
| F14|H14|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.509 | 0.545 |
| F14|H14|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.509 | 0.545 |
| F14|H14|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.9777 | 0.5325 |
| F14|H14|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.9777 | 0.5325 |
| F14|H28|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.509 | 0.545 |
| F14|H28|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.509 | 0.545 |
| F14|H28|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.9777 | 0.5325 |
| F14|H28|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.9777 | 0.5325 |
| F14|H7|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.509 | 0.545 |
| F14|H7|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.509 | 0.545 |
| F14|H7|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.9777 | 0.5325 |
| F14|H7|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.9777 | 0.5325 |
| F28|H14|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.2208 | 0.5725 |
| F28|H14|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.2208 | 0.5725 |
| F28|H14|raw|absolute|XSM1_LONG | XSM1_LONG | 400 | 0.445 | 0.515 |
| F28|H14|raw|market_neutral|XSM1_LONG | XSM1_LONG | 400 | 0.445 | 0.515 |
| F28|H28|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.2208 | 0.5725 |
| F28|H28|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.2208 | 0.5725 |
| F28|H28|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.3443 | 0.5825 |
| F28|H28|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.3443 | 0.5825 |
| F28|H7|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.2208 | 0.5725 |
| F28|H7|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.2208 | 0.5725 |
| F28|H7|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.3443 | 0.5825 |
| F28|H7|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.3443 | 0.5825 |
| F56|H14|anchored|absolute|XSM1_LONG | XSM1_LONG | 400 | 0.8492 | 0.5075 |
| F56|H14|anchored|market_neutral|XSM1_LONG | XSM1_LONG | 400 | 0.8492 | 0.5075 |
| F56|H14|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.1234 | 0.6125 |
| F56|H14|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.1234 | 0.6125 |
| F56|H28|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 400 | 0.0052 | 0.5725 |
| F56|H28|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | 0.0052 | 0.5725 |
| F56|H28|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.1234 | 0.6125 |
| F56|H28|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.1234 | 0.6125 |
| F56|H7|anchored|absolute|XSM1_LONG | XSM1_LONG | 400 | 0.8492 | 0.5075 |
| F56|H7|anchored|market_neutral|XSM1_LONG | XSM1_LONG | 400 | 0.8492 | 0.5075 |
| F56|H7|raw|absolute|XSM1_LONG | XSM1_LONG | 400 | 0.9721 | 0.5675 |
| F56|H7|raw|market_neutral|XSM1_LONG | XSM1_LONG | 400 | 0.9721 | 0.5675 |
| F7|H14|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -1.6246 | 0.5 |
| F7|H14|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -1.6246 | 0.5 |
| F7|H14|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -1.7009 | 0.485 |
| F7|H14|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -1.7009 | 0.485 |
| F7|H28|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -1.6246 | 0.5 |
| F7|H28|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -1.6246 | 0.5 |
| F7|H28|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -1.7009 | 0.485 |
| F7|H28|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -1.7009 | 0.485 |
| F7|H7|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -1.6246 | 0.5 |
| F7|H7|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -1.6246 | 0.5 |
| F7|H7|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -1.7009 | 0.485 |
| F7|H7|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -1.7009 | 0.485 |
| F84|H14|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.1964 | 0.64 |
| F84|H14|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.1964 | 0.64 |
| F84|H28|anchored|absolute|XSR1_SHORT | XSR1_SHORT | 400 | 0.2168 | 0.5925 |
| F84|H28|anchored|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | 0.2168 | 0.5925 |
| F84|H28|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.1964 | 0.64 |
| F84|H28|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.1964 | 0.64 |
| F84|H7|anchored|absolute|XSM1_LONG | XSM1_LONG | 400 | 0.8779 | 0.5125 |
| F84|H7|anchored|market_neutral|XSM1_LONG | XSM1_LONG | 400 | 0.8779 | 0.5125 |
| F84|H7|raw|absolute|XSR1_SHORT | XSR1_SHORT | 400 | -0.1964 | 0.64 |
| F84|H7|raw|market_neutral|XSR1_SHORT | XSR1_SHORT | 400 | -0.1964 | 0.64 |

## Heatmaps — F×H net PnL per variant/frame/direction

### XSM1_LONG · raw · absolute (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | 0.6425 (-0.776) | 1.8744 (-0.8252) | 0.3312 (-2.7817) |
| F14 | 0.678 (-0.8202) | -0.3552 (-1.0169) | -2.4223 (-2.8914) |
| F28 | 0.5163 (-0.3087) | 0.7516 (0.0066) | 1.4495 (-2.8692) |
| F56 | -0.0179 (0.3592) | 0.8003 (-0.544) | 0.7946 (-3.1934) |
| F84 | -0.4866 (-0.2628) | -0.5463 (-1.3383) | -1.3528 (-4.9035) |

### XSM1_LONG · raw · market_neutral (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | 0.6425 (-0.776) | 1.8744 (-0.8252) | 0.3312 (-2.7817) |
| F14 | 0.678 (-0.8202) | -0.3552 (-1.0169) | -2.4223 (-2.8914) |
| F28 | 0.5163 (-0.3087) | 0.7516 (0.0066) | 1.4495 (-2.8692) |
| F56 | -0.0179 (0.3592) | 0.8003 (-0.544) | 0.7946 (-3.1934) |
| F84 | -0.4866 (-0.2628) | -0.5463 (-1.3383) | -1.3528 (-4.9035) |

### XSM1_LONG · anchored · absolute (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | 1.0045 (-1.6173) | 3.4409 (-1.6958) | 2.8724 (-4.3867) |
| F14 | 1.9613 (-1.2136) | 4.4318 (-1.3173) | 3.201 (-4.0605) |
| F28 | 1.9081 (-0.3814) | 3.0089 (-0.5453) | 3.2509 (-3.2334) |
| F56 | 2.0272 (0.0754) | 3.1146 (0.02) | 2.0477 (-3.025) |
| F84 | 1.889 (0.0195) | 3.6915 (-0.1743) | 2.1238 (-2.8721) |

### XSM1_LONG · anchored · market_neutral (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | 1.0045 (-1.6173) | 3.4409 (-1.6958) | 2.8724 (-4.3867) |
| F14 | 1.9613 (-1.2136) | 4.4318 (-1.3173) | 3.201 (-4.0605) |
| F28 | 1.9081 (-0.3814) | 3.0089 (-0.5453) | 3.2509 (-3.2334) |
| F56 | 2.0272 (0.0754) | 3.1146 (0.02) | 2.0477 (-3.025) |
| F84 | 1.889 (0.0195) | 3.6915 (-0.1743) | 2.1238 (-2.8721) |

### XSR1_SHORT · raw · absolute (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | -2.1267 (0.5385) | -4.2158 (0.5856) | -4.1308 (2.5066) |
| F14 | -2.1303 (0.6186) | -1.9623 (0.8183) | -1.3882 (2.6264) |
| F28 | -1.9196 (0.0768) | -3.114 (-0.28) | -5.4643 (2.524) |
| F56 | -1.1807 (-0.5934) | -2.7763 (0.2816) | -4.3016 (2.8812) |
| F84 | -0.5418 (0.0654) | -1.1455 (1.1487) | -1.6124 (4.7424) |

### XSR1_SHORT · raw · market_neutral (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | -2.1267 (0.5385) | -4.2158 (0.5856) | -4.1308 (2.5066) |
| F14 | -2.1303 (0.6186) | -1.9623 (0.8183) | -1.3882 (2.6264) |
| F28 | -1.9196 (0.0768) | -3.114 (-0.28) | -5.4643 (2.524) |
| F56 | -1.1807 (-0.5934) | -2.7763 (0.2816) | -4.3016 (2.8812) |
| F84 | -0.5418 (0.0654) | -1.1455 (1.1487) | -1.6124 (4.7424) |

### XSR1_SHORT · anchored · absolute (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | -2.7557 (1.3756) | -6.194 (1.4519) | -7.4484 (4.1278) |
| F14 | -3.6316 (1.014) | -7.2383 (1.1217) | -7.9289 (3.8534) |
| F28 | -3.5574 (0.1539) | -5.8393 (0.3234) | -8.0589 (2.94) |
| F56 | -3.5957 (-0.3027) | -5.7983 (-0.2855) | -6.5166 (2.6926) |
| F84 | -3.3099 (-0.2467) | -6.0727 (-0.0899) | -6.0346 (2.5428) |

### XSR1_SHORT · anchored · market_neutral (test avg net %, val in parens)

| F \ H | H7 | H14 | H28 |
|---|--:|--:|--:|
| F7 | -2.7557 (1.3756) | -6.194 (1.4519) | -7.4484 (4.1278) |
| F14 | -3.6316 (1.014) | -7.2383 (1.1217) | -7.9289 (3.8534) |
| F28 | -3.5574 (0.1539) | -5.8393 (0.3234) | -8.0589 (2.94) |
| F56 | -3.5957 (-0.3027) | -5.7983 (-0.2855) | -6.5166 (2.6926) |
| F84 | -3.3099 (-0.2467) | -6.0727 (-0.0899) | -6.0346 (2.5428) |

## Full grid — stage-1 net PnL, chrono val/test split

| cell | all n | all avg% | all WR | val n | val avg% | test n | test avg% | spread(top−bot)% | short fund bps |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| F14|H14|anchored|absolute|XSM1_LONG | 109 | 1.7419 | 0.4312 | 51 | -1.3173 | 58 | 4.4318 | 4.3124 | None |
| F14|H14|anchored|absolute|XSR1_SHORT | 109 | -3.3267 | 0.5138 | 51 | 1.1217 | 58 | -7.2383 | 4.3124 | -179.6064 |
| F14|H14|anchored|market_neutral|XSM1_LONG | 109 | 1.7419 | 0.4312 | 51 | -1.3173 | 58 | 4.4318 | 4.3124 | None |
| F14|H14|anchored|market_neutral|XSR1_SHORT | 109 | -3.3267 | 0.5138 | 51 | 1.1217 | 58 | -7.2383 | 4.3124 | -179.6064 |
| F14|H14|raw|absolute|XSM1_LONG | 109 | -0.6648 | 0.4404 | 51 | -1.0169 | 58 | -0.3552 | 1.1176 | None |
| F14|H14|raw|absolute|XSR1_SHORT | 109 | -0.6613 | 0.5321 | 51 | 0.8183 | 58 | -1.9623 | 1.1176 | -146.4738 |
| F14|H14|raw|market_neutral|XSM1_LONG | 109 | -0.6648 | 0.4404 | 51 | -1.0169 | 58 | -0.3552 | 1.1176 | None |
| F14|H14|raw|market_neutral|XSR1_SHORT | 109 | -0.6613 | 0.5321 | 51 | 0.8183 | 58 | -1.9623 | 1.1176 | -146.4738 |
| F14|H28|anchored|absolute|XSM1_LONG | 109 | -0.1966 | 0.4312 | 51 | -4.0605 | 58 | 3.201 | 4.1989 | None |
| F14|H28|anchored|absolute|XSR1_SHORT | 109 | -2.416 | 0.5138 | 51 | 3.8534 | 58 | -7.9289 | 4.1989 | -313.4422 |
| F14|H28|anchored|market_neutral|XSM1_LONG | 109 | -0.1966 | 0.4312 | 51 | -4.0605 | 58 | 3.201 | 4.1989 | None |
| F14|H28|anchored|market_neutral|XSR1_SHORT | 109 | -2.416 | 0.5138 | 51 | 3.8534 | 58 | -7.9289 | 4.1989 | -313.4422 |
| F14|H28|raw|absolute|XSM1_LONG | 109 | -2.6418 | 0.4037 | 51 | -2.8914 | 58 | -2.4223 | 0.5473 | None |
| F14|H28|raw|absolute|XSR1_SHORT | 109 | 0.4902 | 0.5229 | 51 | 2.6264 | 58 | -1.3882 | 0.5473 | -254.4746 |
| F14|H28|raw|market_neutral|XSM1_LONG | 109 | -2.6418 | 0.4037 | 51 | -2.8914 | 58 | -2.4223 | 0.5473 | None |
| F14|H28|raw|market_neutral|XSR1_SHORT | 109 | 0.4902 | 0.5229 | 51 | 2.6264 | 58 | -1.3882 | 0.5473 | -254.4746 |
| F14|H7|anchored|absolute|XSM1_LONG | 109 | 0.4758 | 0.4495 | 51 | -1.2136 | 58 | 1.9613 | 1.818 | None |
| F14|H7|anchored|absolute|XSR1_SHORT | 109 | -1.458 | 0.5046 | 51 | 1.014 | 58 | -3.6316 | 1.818 | -101.4069 |
| F14|H7|anchored|market_neutral|XSM1_LONG | 109 | 0.4758 | 0.4495 | 51 | -1.2136 | 58 | 1.9613 | 1.818 | None |
| F14|H7|anchored|market_neutral|XSR1_SHORT | 109 | -1.458 | 0.5046 | 51 | 1.014 | 58 | -3.6316 | 1.818 | -101.4069 |
| F14|H7|raw|absolute|XSM1_LONG | 109 | -0.023 | 0.4587 | 51 | -0.8202 | 58 | 0.678 | 1.0252 | None |
| F14|H7|raw|absolute|XSR1_SHORT | 109 | -0.8441 | 0.5138 | 51 | 0.6186 | 58 | -2.1303 | 1.0252 | -86.0518 |
| F14|H7|raw|market_neutral|XSM1_LONG | 109 | -0.023 | 0.4587 | 51 | -0.8202 | 58 | 0.678 | 1.0252 | None |
| F14|H7|raw|market_neutral|XSR1_SHORT | 109 | -0.8441 | 0.5138 | 51 | 0.6186 | 58 | -2.1303 | 1.0252 | -86.0518 |
| F28|H14|anchored|absolute|XSM1_LONG | 109 | 1.3459 | 0.4771 | 51 | -0.5453 | 58 | 3.0089 | 4.1365 | None |
| F28|H14|anchored|absolute|XSR1_SHORT | 109 | -2.9558 | 0.4862 | 51 | 0.3234 | 58 | -5.8393 | 4.1365 | -181.9983 |
| F28|H14|anchored|market_neutral|XSM1_LONG | 109 | 1.3459 | 0.4771 | 51 | -0.5453 | 58 | 3.0089 | 4.1365 | None |
| F28|H14|anchored|market_neutral|XSR1_SHORT | 109 | -2.9558 | 0.4862 | 51 | 0.3234 | 58 | -5.8393 | 4.1365 | -181.9983 |
| F28|H14|raw|absolute|XSM1_LONG | 109 | 0.403 | 0.4679 | 51 | 0.0066 | 58 | 0.7516 | 2.4173 | None |
| F28|H14|raw|absolute|XSR1_SHORT | 109 | -1.788 | 0.4954 | 51 | -0.28 | 58 | -3.114 | 2.4173 | -154.426 |
| F28|H14|raw|market_neutral|XSM1_LONG | 109 | 0.403 | 0.4679 | 51 | 0.0066 | 58 | 0.7516 | 2.4173 | None |
| F28|H14|raw|market_neutral|XSR1_SHORT | 109 | -1.788 | 0.4954 | 51 | -0.28 | 58 | -3.114 | 2.4173 | -154.426 |
| F28|H28|anchored|absolute|XSM1_LONG | 109 | 0.217 | 0.4312 | 51 | -3.2334 | 58 | 3.2509 | 4.9302 | None |
| F28|H28|anchored|absolute|XSR1_SHORT | 109 | -2.9126 | 0.5138 | 51 | 2.94 | 58 | -8.0589 | 4.9302 | -322.4677 |
| F28|H28|anchored|market_neutral|XSM1_LONG | 109 | 0.217 | 0.4312 | 51 | -3.2334 | 58 | 3.2509 | 4.9302 | None |
| F28|H28|anchored|market_neutral|XSR1_SHORT | 109 | -2.9126 | 0.5138 | 51 | 2.94 | 58 | -8.0589 | 4.9302 | -322.4677 |
| F28|H28|raw|absolute|XSM1_LONG | 109 | -0.5712 | 0.4404 | 51 | -2.8692 | 58 | 1.4495 | 3.7642 | None |
| F28|H28|raw|absolute|XSR1_SHORT | 109 | -1.7266 | 0.4954 | 51 | 2.524 | 58 | -5.4643 | 3.7642 | -273.8778 |
| F28|H28|raw|market_neutral|XSM1_LONG | 109 | -0.5712 | 0.4404 | 51 | -2.8692 | 58 | 1.4495 | 3.7642 | None |
| F28|H28|raw|market_neutral|XSR1_SHORT | 109 | -1.7266 | 0.4954 | 51 | 2.524 | 58 | -5.4643 | 3.7642 | -273.8778 |
| F28|H7|anchored|absolute|XSM1_LONG | 109 | 0.8369 | 0.4771 | 51 | -0.3814 | 58 | 1.9081 | 2.5369 | None |
| F28|H7|anchored|absolute|XSR1_SHORT | 109 | -1.8209 | 0.4954 | 51 | 0.1539 | 58 | -3.5574 | 2.5369 | -100.8036 |
| F28|H7|anchored|market_neutral|XSM1_LONG | 109 | 0.8369 | 0.4771 | 51 | -0.3814 | 58 | 1.9081 | 2.5369 | None |
| F28|H7|anchored|market_neutral|XSR1_SHORT | 109 | -1.8209 | 0.4954 | 51 | 0.1539 | 58 | -3.5574 | 2.5369 | -100.8036 |
| F28|H7|raw|absolute|XSM1_LONG | 109 | 0.1303 | 0.4954 | 51 | -0.3087 | 58 | 0.5163 | 1.3165 | None |
| F28|H7|raw|absolute|XSR1_SHORT | 109 | -0.9855 | 0.4771 | 51 | 0.0768 | 58 | -1.9196 | 1.3165 | -85.3527 |
| F28|H7|raw|market_neutral|XSM1_LONG | 109 | 0.1303 | 0.4954 | 51 | -0.3087 | 58 | 0.5163 | 1.3165 | None |
| F28|H7|raw|market_neutral|XSR1_SHORT | 109 | -0.9855 | 0.4771 | 51 | 0.0768 | 58 | -1.9196 | 1.3165 | -85.3527 |
| F56|H14|anchored|absolute|XSM1_LONG | 109 | 1.6667 | 0.4862 | 51 | 0.02 | 58 | 3.1146 | 4.2242 | None |
| F56|H14|anchored|absolute|XSR1_SHORT | 109 | -3.2189 | 0.4771 | 51 | -0.2855 | 58 | -5.7983 | 4.2242 | -174.3317 |
| F56|H14|anchored|market_neutral|XSM1_LONG | 109 | 1.6667 | 0.4862 | 51 | 0.02 | 58 | 3.1146 | 4.2242 | None |
| F56|H14|anchored|market_neutral|XSR1_SHORT | 109 | -3.2189 | 0.4771 | 51 | -0.2855 | 58 | -5.7983 | 4.2242 | -174.3317 |
| F56|H14|raw|absolute|XSM1_LONG | 109 | 0.1713 | 0.4587 | 51 | -0.544 | 58 | 0.8003 | 2.4599 | None |
| F56|H14|raw|absolute|XSR1_SHORT | 109 | -1.3455 | 0.5138 | 51 | 0.2816 | 58 | -2.7763 | 2.4599 | -129.7461 |
| F56|H14|raw|market_neutral|XSM1_LONG | 109 | 0.1713 | 0.4587 | 51 | -0.544 | 58 | 0.8003 | 2.4599 | None |
| F56|H14|raw|market_neutral|XSR1_SHORT | 109 | -1.3455 | 0.5138 | 51 | 0.2816 | 58 | -2.7763 | 2.4599 | -129.7461 |
| F56|H28|anchored|absolute|XSM1_LONG | 109 | -0.3258 | 0.4037 | 51 | -3.025 | 58 | 2.0477 | 4.3218 | None |
| F56|H28|anchored|absolute|XSR1_SHORT | 109 | -2.2077 | 0.5321 | 51 | 2.6926 | 58 | -6.5166 | 4.3218 | -300.9306 |
| F56|H28|anchored|market_neutral|XSM1_LONG | 109 | -0.3258 | 0.4037 | 51 | -3.025 | 58 | 2.0477 | 4.3218 | None |
| F56|H28|anchored|market_neutral|XSR1_SHORT | 109 | -2.2077 | 0.5321 | 51 | 2.6926 | 58 | -6.5166 | 4.3218 | -300.9306 |
| F56|H28|raw|absolute|XSM1_LONG | 109 | -1.0714 | 0.4495 | 51 | -3.1934 | 58 | 0.7946 | 3.1015 | None |
| F56|H28|raw|absolute|XSR1_SHORT | 109 | -0.9408 | 0.4954 | 51 | 2.8812 | 58 | -4.3016 | 3.1015 | -241.2806 |
| F56|H28|raw|market_neutral|XSM1_LONG | 109 | -1.0714 | 0.4495 | 51 | -3.1934 | 58 | 0.7946 | 3.1015 | None |
| F56|H28|raw|market_neutral|XSR1_SHORT | 109 | -0.9408 | 0.4954 | 51 | 2.8812 | 58 | -4.3016 | 3.1015 | -241.2806 |
| F56|H7|anchored|absolute|XSM1_LONG | 109 | 1.114 | 0.4862 | 51 | 0.0754 | 58 | 2.0272 | 2.4171 | None |
| F56|H7|anchored|absolute|XSR1_SHORT | 109 | -2.0549 | 0.4771 | 51 | -0.3027 | 58 | -3.5957 | 2.4171 | -95.4942 |
| F56|H7|anchored|market_neutral|XSM1_LONG | 109 | 1.114 | 0.4862 | 51 | 0.0754 | 58 | 2.0272 | 2.4171 | None |
| F56|H7|anchored|market_neutral|XSR1_SHORT | 109 | -2.0549 | 0.4771 | 51 | -0.3027 | 58 | -3.5957 | 2.4171 | -95.4942 |
| F56|H7|raw|absolute|XSM1_LONG | 109 | 0.1586 | 0.5046 | 51 | 0.3592 | 58 | -0.0179 | 1.119 | None |
| F56|H7|raw|absolute|XSR1_SHORT | 109 | -0.9059 | 0.4679 | 51 | -0.5934 | 58 | -1.1807 | 1.119 | -72.8871 |
| F56|H7|raw|market_neutral|XSM1_LONG | 109 | 0.1586 | 0.5046 | 51 | 0.3592 | 58 | -0.0179 | 1.119 | None |
| F56|H7|raw|market_neutral|XSR1_SHORT | 109 | -0.9059 | 0.4679 | 51 | -0.5934 | 58 | -1.1807 | 1.119 | -72.8871 |
| F7|H14|anchored|absolute|XSM1_LONG | 109 | 1.0375 | 0.4587 | 51 | -1.6958 | 58 | 3.4409 | 3.3982 | None |
| F7|H14|anchored|absolute|XSR1_SHORT | 109 | -2.6165 | 0.4954 | 51 | 1.4519 | 58 | -6.194 | 3.3982 | -178.3652 |
| F7|H14|anchored|market_neutral|XSM1_LONG | 109 | 1.0375 | 0.4587 | 51 | -1.6958 | 58 | 3.4409 | 3.3982 | None |
| F7|H14|anchored|market_neutral|XSR1_SHORT | 109 | -2.6165 | 0.4954 | 51 | 1.4519 | 58 | -6.194 | 3.3982 | -178.3652 |
| F7|H14|raw|absolute|XSM1_LONG | 109 | 0.6113 | 0.4495 | 51 | -0.8252 | 58 | 1.8744 | 2.4356 | None |
| F7|H14|raw|absolute|XSR1_SHORT | 109 | -1.9693 | 0.5229 | 51 | 0.5856 | 58 | -4.2158 | 2.4356 | -149.0296 |
| F7|H14|raw|market_neutral|XSM1_LONG | 109 | 0.6113 | 0.4495 | 51 | -0.8252 | 58 | 1.8744 | 2.4356 | None |
| F7|H14|raw|market_neutral|XSR1_SHORT | 109 | -1.9693 | 0.5229 | 51 | 0.5856 | 58 | -4.2158 | 2.4356 | -149.0296 |
| F7|H28|anchored|absolute|XSM1_LONG | 109 | -0.524 | 0.4037 | 51 | -4.3867 | 58 | 2.8724 | 3.1084 | None |
| F7|H28|anchored|absolute|XSR1_SHORT | 109 | -2.032 | 0.5321 | 51 | 4.1278 | 58 | -7.4484 | 3.1084 | -305.4623 |
| F7|H28|anchored|market_neutral|XSM1_LONG | 109 | -0.524 | 0.4037 | 51 | -4.3867 | 58 | 2.8724 | 3.1084 | None |
| F7|H28|anchored|market_neutral|XSR1_SHORT | 109 | -2.032 | 0.5321 | 51 | 4.1278 | 58 | -7.4484 | 3.1084 | -305.4623 |
| F7|H28|raw|absolute|XSM1_LONG | 109 | -1.1253 | 0.3853 | 51 | -2.7817 | 58 | 0.3312 | 2.5741 | None |
| F7|H28|raw|absolute|XSR1_SHORT | 109 | -1.0252 | 0.5229 | 51 | 2.5066 | 58 | -4.1308 | 2.5741 | -251.8718 |
| F7|H28|raw|market_neutral|XSM1_LONG | 109 | -1.1253 | 0.3853 | 51 | -2.7817 | 58 | 0.3312 | 2.5741 | None |
| F7|H28|raw|market_neutral|XSR1_SHORT | 109 | -1.0252 | 0.5229 | 51 | 2.5066 | 58 | -4.1308 | 2.5741 | -251.8718 |
| F7|H7|anchored|absolute|XSM1_LONG | 109 | -0.2222 | 0.4404 | 51 | -1.6173 | 58 | 1.0045 | 1.0306 | None |
| F7|H7|anchored|absolute|XSR1_SHORT | 109 | -0.8227 | 0.5138 | 51 | 1.3756 | 58 | -2.7557 | 1.0306 | -108.7409 |
| F7|H7|anchored|market_neutral|XSM1_LONG | 109 | -0.2222 | 0.4404 | 51 | -1.6173 | 58 | 1.0045 | 1.0306 | None |
| F7|H7|anchored|market_neutral|XSR1_SHORT | 109 | -0.8227 | 0.5138 | 51 | 1.3756 | 58 | -2.7557 | 1.0306 | -108.7409 |
| F7|H7|raw|absolute|XSM1_LONG | 109 | -0.0212 | 0.4679 | 51 | -0.776 | 58 | 0.6425 | 0.9844 | None |
| F7|H7|raw|absolute|XSR1_SHORT | 109 | -0.8797 | 0.5046 | 51 | 0.5385 | 58 | -2.1267 | 0.9844 | -90.4412 |
| F7|H7|raw|market_neutral|XSM1_LONG | 109 | -0.0212 | 0.4679 | 51 | -0.776 | 58 | 0.6425 | 0.9844 | None |
| F7|H7|raw|market_neutral|XSR1_SHORT | 109 | -0.8797 | 0.5046 | 51 | 0.5385 | 58 | -2.1267 | 0.9844 | -90.4412 |
| F84|H14|anchored|absolute|XSM1_LONG | 109 | 1.8827 | 0.5046 | 51 | -0.1743 | 58 | 3.6915 | 4.581 | None |
| F84|H14|anchored|absolute|XSR1_SHORT | 109 | -3.2734 | 0.4679 | 51 | -0.0899 | 58 | -6.0727 | 4.581 | -153.3851 |
| F84|H14|anchored|market_neutral|XSM1_LONG | 109 | 1.8827 | 0.5046 | 51 | -0.1743 | 58 | 3.6915 | 4.581 | None |
| F84|H14|anchored|market_neutral|XSR1_SHORT | 109 | -3.2734 | 0.4679 | 51 | -0.0899 | 58 | -6.0727 | 4.581 | -153.3851 |
| F84|H14|raw|absolute|XSM1_LONG | 109 | -0.9169 | 0.4587 | 51 | -1.3383 | 58 | -0.5463 | 1.0858 | None |
| F84|H14|raw|absolute|XSR1_SHORT | 109 | -0.0721 | 0.5046 | 51 | 1.1487 | 58 | -1.1455 | 1.0858 | -108.1695 |
| F84|H14|raw|market_neutral|XSM1_LONG | 109 | -0.9169 | 0.4587 | 51 | -1.3383 | 58 | -0.5463 | 1.0858 | None |
| F84|H14|raw|market_neutral|XSR1_SHORT | 109 | -0.0721 | 0.5046 | 51 | 1.1487 | 58 | -1.1455 | 1.0858 | -108.1695 |
| F84|H28|anchored|absolute|XSM1_LONG | 109 | -0.2137 | 0.4128 | 51 | -2.8721 | 58 | 2.1238 | 4.5886 | None |
| F84|H28|anchored|absolute|XSR1_SHORT | 109 | -2.0213 | 0.5321 | 51 | 2.5428 | 58 | -6.0346 | 4.5886 | -262.4082 |
| F84|H28|anchored|market_neutral|XSM1_LONG | 109 | -0.2137 | 0.4128 | 51 | -2.8721 | 58 | 2.1238 | 4.5886 | None |
| F84|H28|anchored|market_neutral|XSR1_SHORT | 109 | -2.0213 | 0.5321 | 51 | 2.5428 | 58 | -6.0346 | 4.5886 | -262.4082 |
| F84|H28|raw|absolute|XSM1_LONG | 109 | -3.0142 | 0.3761 | 51 | -4.9035 | 58 | -1.3528 | 0.0975 | None |
| F84|H28|raw|absolute|XSR1_SHORT | 109 | 1.361 | 0.5872 | 51 | 4.7424 | 58 | -1.6124 | 0.0975 | -200.4777 |
| F84|H28|raw|market_neutral|XSM1_LONG | 109 | -3.0142 | 0.3761 | 51 | -4.9035 | 58 | -1.3528 | 0.0975 | None |
| F84|H28|raw|market_neutral|XSR1_SHORT | 109 | 1.361 | 0.5872 | 51 | 4.7424 | 58 | -1.6124 | 0.0975 | -200.4777 |
| F84|H7|anchored|absolute|XSM1_LONG | 109 | 1.0143 | 0.4771 | 51 | 0.0195 | 58 | 1.889 | 2.4019 | None |
| F84|H7|anchored|absolute|XSR1_SHORT | 109 | -1.8767 | 0.4954 | 51 | -0.2467 | 58 | -3.3099 | 2.4019 | -85.5401 |
| F84|H7|anchored|market_neutral|XSM1_LONG | 109 | 1.0143 | 0.4771 | 51 | 0.0195 | 58 | 1.889 | 2.4019 | None |
| F84|H7|anchored|market_neutral|XSR1_SHORT | 109 | -1.8767 | 0.4954 | 51 | -0.2467 | 58 | -3.3099 | 2.4019 | -85.5401 |
| F84|H7|raw|absolute|XSM1_LONG | 109 | -0.3819 | 0.4679 | 51 | -0.2628 | 58 | -0.4866 | 0.6459 | None |
| F84|H7|raw|absolute|XSR1_SHORT | 109 | -0.2577 | 0.5046 | 51 | 0.0654 | 58 | -0.5418 | 0.6459 | -60.2067 |
| F84|H7|raw|market_neutral|XSM1_LONG | 109 | -0.3819 | 0.4679 | 51 | -0.2628 | 58 | -0.4866 | 0.6459 | None |
| F84|H7|raw|market_neutral|XSR1_SHORT | 109 | -0.2577 | 0.5046 | 51 | 0.0654 | 58 | -0.5418 | 0.6459 | -60.2067 |

## Population & caveats

- run status: complete · coins loaded: 525 of 527
- rebalance rows (weekly raster): 109
- peak process RSS: 298.8 MB (panel O(coins×days) + streaming cell accumulators)
- chrono val/test split (UTC): 2025-05-05T12:00:00+00:00 — fixed midpoint of the BTCUSDT 1d window; val=earlier, test=later
- signal variants: raw F-day return; anchored = close/min(low over F) − 1 (distance to formation low, F5)
- reference frames: absolute; market-neutral = coin signal − BTCUSDT signal — ⚠ KNOWN LIMITATION: this scalar shift is argsort-invariant and PnL is absolute, so it removes NO beta (market_neutral ≡ absolute, 60/60 identical); follow-up = beta-adjust the returns/spread. Non-verdict-affecting (result is negative regardless).
- liquidity: exclude bottom volume tercile by median quote-vol over F; quote-vol ≈ base volume × close (the candles table has no quote_asset_volume column — documented approximation)
- decile size = max(1, round(n_liquid·0.1)); ranking on the liquid set only, BTCUSDT excluded from the cross-section
- short-side funding: net_short = mean(−fwd + Σ funding_rate[hold]) − fee; a short RECEIVES funding when funding_rate>0 and PAYS when <0 (spec: Shorts zahlen bei negativem Funding); funding summed over [t, t+H)
- fees: round-trip taker 0.0010 = 2·FEE_PER_SIDE (walkforward_sim, Regel 10 — not reinvented)
- funding_features.py note: core/funding_features.py is the 6-feature as-of ROLLING builder (fund_24h/72h/…); here we need the RAW Σ funding_rate over the exact hold window, so we read funding_rates directly
- **Survivorship bias (Rule 9, strongest here)**: coins.json lists ACTIVE USDT-perps; delisted coins are absent → the replayed cross-section skews to survivors. Returns use fill_method=None (no forward-fill across gaps); a coin missing close[t−F], close[t] or close[t+H] simply drops out of that rebalance.
- **Only closed candles (R1)**: read_candles(include_forming=False); 1d klines anchored 00:00 UTC.
- **WR is not decisive (Rule 8)**: the verdict rests on net-PnL expectancy consistent across the chrono halves.
- CPU-check override: --skip-cpu-check=True (VPS is CPU-saturated; the read-only BELOW_NORMAL job bypasses the walkforward_sim guard deliberately).
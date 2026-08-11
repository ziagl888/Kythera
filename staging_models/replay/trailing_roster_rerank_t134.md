# Trailing roster re-rank — T-2026-KYT-9050-134
Replay `trailing_slot_budget_livewindow_t134.json` (window from 2026-07-26, generated 2026-08-10T22:04:07.285470+00:00) scored against the live book from 2026-07-26. Calibration: same-window (model error isolated from regime).

## Verdict

**No seat changes are recommended on this evidence.**

- The premise the task started from does not survive: every unrostered leg with a positive expected contribution (7 of them) is **fitted, not measured** — none has live trades. The largest is worth +30 %-points. The legs PR #198 rejected on density turn out net-NEGATIVE once corrected, so the roster kept them out for a stated reason that was wrong and an outcome that was right.
- The real finding points the other way: **9 rostered legs lose money on live evidence**, -762 %-points combined.
- But **0 of them clear |t| > 2**. On a two-week book that is a watchlist, not a retirement list. Acting on it now would repeat the error this tool was built to catch.

## Calibration — does the replay predict the live arm?

Fit over 19 legs with >= 30 live trades:

    live = -0.223 + 0.273 * replay      R2 = 0.610, residual sd = 0.325 pp

The replay overstates **16 of 19** calibrated legs; median error -0.756 pp per trade. A slope below 1 means the error grows with the prediction, so the top of the raw ranking is the least trustworthy part of it.

| leg | replay net | live net | error | live n |
|---|---:|---:|---:|---:|
| MIS2-72H SHORT | +6.337 | +1.449 | -4.888 | 92 |
| MIS1-8H SHORT | +2.013 | +1.046 | -0.967 | 72 |
| RUB1 SHORT | +1.970 | +0.335 | -1.635 | 81 |
| AIM2 LONG | +1.490 | +0.209 | -1.281 | 186 |
| AIM2 SHORT | +1.295 | -0.511 | -1.806 | 480 |
| MIS1-168H LONG | +0.901 | -0.140 | -1.041 | 90 |
| SRA2 LONG | +0.853 | +0.214 | -0.639 | 175 |
| SKW1 LONG | +0.792 | -0.231 | -1.023 | 51 |
| SKW1 SHORT | +0.557 | +0.191 | -0.366 | 79 |
| ATS2 LONG | +0.552 | -0.254 | -0.806 | 466 |
| FIF2 LONG | +0.409 | -0.347 | -0.756 | 174 |
| BR4H LONG | +0.351 | +0.139 | -0.212 | 113 |
| MIS1-72H LONG | +0.187 | -0.852 | -1.039 | 672 |
| SRA2 SHORT | +0.097 | +0.058 | -0.039 | 224 |
| XSM1 LONG | +0.030 | -0.308 | -0.338 | 32 |
| FIF2 SHORT | -0.159 | -0.147 | +0.012 | 402 |
| MAX1 SHORT | -0.195 | -0.147 | +0.048 | 94 |
| TD_1H SHORT | -0.218 | -0.236 | -0.018 | 38 |
| ODS1 SHORT | -0.240 | -0.053 | +0.187 | 86 |

## Ranking by expected net contribution

Primary column is absolute contribution (effective per-trade x replay trade count), because the seat cap provably never binds. Density is retained as the PR #198 measure.

`basis` says where the per-trade number comes from: **live** for legs with >= 30 trades in the real book, **fitted** for legs that have none and must be extrapolated through the calibration line. A fitted value never overrides a measured one — the fit regresses toward the mean and would otherwise rehabilitate legs the live book has already convicted.

| leg | roster | basis | n | replay/trade | effective/trade | total | density | occ p95 | live n |
|---|:--:|:--:|---:|---:|---:|---:|---:|---:|---:|
| MIS2-72h SHORT | YES | live | 105 | +6.337 | +1.449 | +152 | 211.585 | 1 | 92 |
| AIM2 LONG | YES | live | 532 | +1.490 | +0.209 | +111 | 2.414 | 53 | 186 |
| SRA2 LONG | YES | live | 509 | +0.853 | +0.214 | +109 | 1.120 | 36 | 175 |
| MIS1-8h SHORT | YES | live | 91 | +2.013 | +1.046 | +95 | 52.889 | 1 | 72 |
| MIS2-24h SHORT | YES | fitted | 38 | +6.223 | +1.476 | +56 | 246.038 | 1 | 24 |
| RUB1 SHORT | YES | live | 128 | +1.970 | +0.335 | +43 | 21.709 | 3 | 81 |
| ABR2 LONG | - | fitted | 135 | +1.643 | +0.226 | +30 | 4.113 | 9 |  |
| BR4H LONG | YES | live | 214 | +0.351 | +0.139 | +30 | 0.407 | 23 | 113 |
| EPD2 SHORT | - | fitted | 38 | +3.050 | +0.610 | +23 | 132.711 | 1 |  |
| MIS2-168h SHORT | YES | fitted | 19 | +5.008 | +1.144 | +22 | 162.657 | 1 | 18 |
| SRA2 SHORT | YES | live | 306 | +0.097 | +0.058 | +18 | 0.180 | 19 | 224 |
| SKW1 SHORT | YES | live | 89 | +0.557 | +0.191 | +17 | 2.432 | 8 | 79 |
| XSR1 SHORT | - | fitted | 95 | +1.449 | +0.173 | +16 | 4.874 | 8 |  |
| EPD2 LONG | - | fitted | 35 | +2.357 | +0.421 | +15 | 208.509 | 1 |  |
| MIS2-168h LONG | - | fitted | 233 | +1.027 | +0.058 | +13 | 5.945 | 7 |  |
| MIS1-72h SHORT | - | fitted | 191 | +0.946 | +0.036 | +7 | 15.693 | 3 |  |
| ATS2 SHORT | thin | fitted | 8 | +2.021 | +0.329 | +3 | 4.324 | 1 |  |
| BB_4H SHORT | - | fitted | 78 | +0.931 | +0.031 | +2 | 1.061 | 8 |  |
| RUB4 LONG | thin | fitted | 6 | +2.053 | +0.338 | +2 | 5.374 | 1 |  |
| BB_4H LONG | YES | fitted | 20 | +0.897 | +0.022 | +0 | 0.497 | 7 | 16 |
| MIS1-24h LONG | YES | fitted | 13 | +0.918 | +0.028 | +0 | 7.355 | 1 | 10 |
| TD_4H LONG | YES | fitted | 23 | +0.848 | +0.009 | +0 | 1.050 | 3 | 9 |
| MAX2 LONG | thin | fitted | 23 | +0.719 | -0.026 | -1 | 0.603 | 4 |  |
| RUB1 LONG | YES | fitted | 29 | +0.730 | -0.023 | -1 | 2.831 | 2 | 24 |
| MIS1-24h SHORT | - | fitted | 83 | +0.763 | -0.014 | -1 | 18.035 | 2 |  |
| TD_1H LONG | YES | fitted | 114 | +0.766 | -0.014 | -2 | 0.689 | 33 | 17 |
| QM_1H LONG | YES | fitted | 6 | -0.190 | -0.275 | -2 | -1.151 | 1 | 2 |
| LIS1 SHORT | thin | fitted | 1 | -11.542 | -3.374 | -3 | -7.045 | 1 |  |
| QM_1H SHORT | thin | fitted | 14 | -0.261 | -0.294 | -4 | -0.218 | 3 |  |
| BR1D LONG | thin | fitted | 11 | -0.587 | -0.383 | -4 | -0.843 | 1 |  |
| ATB2 SHORT | thin | fitted | 19 | -0.267 | -0.296 | -6 | -1.405 | 1 |  |
| MIS2-24h LONG | - | fitted | 112 | +0.625 | -0.052 | -6 | 3.733 | 3 |  |
| BR1D SHORT | thin | fitted | 16 | -0.526 | -0.366 | -6 | -0.406 | 4 |  |
| ODS1 SHORT | YES | live | 114 | -0.240 | -0.053 | -6 | -6.192 | 3 | 86 |
| UFI1 SHORT | YES | fitted | 11 | -1.393 | -0.603 | -7 | -7.556 | 1 | 12 |
| SRA1 SHORT | - | fitted | 35 | -0.029 | -0.231 | -8 | -0.055 | 3 |  |
| TD_1H SHORT | YES | live | 36 | -0.218 | -0.236 | -8 | -0.462 | 4 | 38 |
| MIS2-72h LONG | - | fitted | 283 | +0.651 | -0.045 | -13 | 4.567 | 6 |  |
| TD_4H SHORT | YES | fitted | 12 | -3.115 | -1.073 | -13 | -5.091 | 2 | 10 |
| ATB2 LONG | - | fitted | 56 | -0.065 | -0.240 | -13 | -0.203 | 4 |  |
| FIF1 SHORT | - | fitted | 102 | +0.297 | -0.142 | -14 | 2.968 | 3 |  |
| MIS1-168h SHORT | - | fitted | 52 | -0.374 | -0.325 | -17 | -4.653 | 1 |  |
| SKW1 LONG | YES | live | 82 | +0.792 | -0.231 | -19 | 1.154 | 12 | 51 |
| SRA1 LONG | - | fitted | 86 | -0.011 | -0.226 | -19 | -0.009 | 11 |  |
| RUB3 LONG | - | fitted | 152 | +0.287 | -0.144 | -22 | 0.378 | 20 |  |
| FIF1 LONG | - | fitted | 122 | +0.099 | -0.196 | -24 | 0.503 | 8 |  |
| MIS1-8h LONG | - | fitted | 38 | -1.918 | -0.746 | -28 | -24.744 | 1 |  |
| XSM1 LONG | YES | live | 93 | +0.030 | -0.308 | -29 | 0.111 | 6 | 32 |
| PEX1 SHORT | - | fitted | 137 | -0.151 | -0.264 | -36 | -0.741 | 7 |  |
| MAX1 SHORT | YES | live | 259 | -0.195 | -0.147 | -38 | -0.424 | 19 | 94 |
| BR2H LONG | - | fitted | 476 | +0.469 | -0.095 | -45 | 0.551 | 43 |  |
| MIS2-8h LONG | - | fitted | 254 | -0.070 | -0.242 | -61 | -0.527 | 5 |  |
| BR4H SHORT | - | fitted | 146 | -0.750 | -0.427 | -62 | -0.735 | 20 |  |
| BR2H SHORT | - | fitted | 314 | -0.181 | -0.272 | -85 | -0.235 | 26 |  |
| BR1Hv2 LONG | - | fitted | 866 | +0.407 | -0.112 | -97 | 0.515 | 71 |  |
| FIF2 SHORT | YES | live | 784 | -0.159 | -0.147 | -116 | -9.880 | 13 | 402 |
| MIS1-168h LONG | YES | live | 882 | +0.901 | -0.140 | -123 | 1.229 | 134 | 90 |
| FIF2 LONG | YES | live | 423 | +0.409 | -0.347 | -147 | 8.917 | 9 | 174 |
| BR1Hv2 SHORT | - | fitted | 640 | -0.763 | -0.431 | -276 | -0.907 | 65 |  |
| ATS2 LONG | YES | live | 1085 | +0.552 | -0.254 | -276 | 0.535 | 129 | 466 |
| TSM1 SHORT | - | fitted | 1119 | -0.142 | -0.261 | -293 | -0.175 | 89 |  |
| EPD3 LONG | - | fitted | 3336 | +0.434 | -0.104 | -348 | 0.654 | 202 |  |
| AIM2 SHORT | - | live | 738 | +1.295 | -0.511 | -377 | 3.964 | 24 | 480 |
| ROM1 SHORT | - | fitted | 2879 | +0.101 | -0.195 | -562 | 0.540 | 77 |  |
| ROM1 LONG | - | fitted | 4918 | +0.289 | -0.144 | -707 | 0.969 | 145 |  |
| MIS1-72h LONG | - | live | 1689 | +0.187 | -0.852 | -1439 | 0.212 | 168 | 672 |
| EPD3 SHORT | - | fitted | 6982 | +0.052 | -0.209 | -1456 | 0.132 | 237 |  |

## Candidates — unrostered legs that survive the correction

**There is no separate bot-44 list, and there cannot be one.** Both arms run the same `ROSTER` from `core.trailing_roster` — `TRAILING_BOT_PROFILE` switches the exposure cap and the channels, not the seat register (`40_trailing_close_bot.py`, profile block). A leg is seated in both arms or in neither, so the free arm's spare capacity cannot be filled independently of bot 40. The list below is therefore the candidate list for the roster as a whole; admitting any of these legs would seat them in both arms at once.

- **ABR2 LONG** — +0.226 %/trade (fitted) over 135 replay trades (+30 % total), density 4.113, p95 occupancy 9 seats. NO live coverage — the fit extrapolated beyond its support.

- **EPD2 SHORT** — +0.610 %/trade (fitted) over 38 replay trades (+23 % total), density 132.711, p95 occupancy 1 seats. NO live coverage — the fit extrapolated beyond its support.

- **XSR1 SHORT** — +0.173 %/trade (fitted) over 95 replay trades (+16 % total), density 4.874, p95 occupancy 8 seats. NO live coverage — the fit extrapolated beyond its support.

- **EPD2 LONG** — +0.421 %/trade (fitted) over 35 replay trades (+15 % total), density 208.509, p95 occupancy 1 seats. NO live coverage — the fit extrapolated beyond its support.

- **MIS2-168h LONG** — +0.058 %/trade (fitted) over 233 replay trades (+13 % total), density 5.945, p95 occupancy 7 seats. NO live coverage — the fit extrapolated beyond its support.

- **MIS1-72h SHORT** — +0.036 %/trade (fitted) over 191 replay trades (+7 % total), density 15.693, p95 occupancy 3 seats. NO live coverage — the fit extrapolated beyond its support.

- **BB_4H SHORT** — +0.031 %/trade (fitted) over 78 replay trades (+2 % total), density 1.061, p95 occupancy 8 seats. NO live coverage — the fit extrapolated beyond its support.

## Rostered legs with no trades in the window

**5** seated legs produced no closed trade since 2026-07-26, so they appear in no ranking above. They occupy a roster seat and deliver nothing; whether that is a dead model or merely a quiet fortnight is not decidable from this window.

    ABR1 LONG, ABR1 SHORT, BR1H LONG, EPD1 SHORT, QM_4H LONG

## Rostered legs that lose money on live evidence

These are **measured, not extrapolated**: each has live trades past the floor. This is where the roster is actually wrong, and it is the opposite of the question the task started from.

| leg | n | live/trade | t | total | live n |
|---|---:|---:|---:|---:|---:|
| ATS2 LONG | 1085 | -0.254 | -1.86 | -276 | 466 |
| FIF2 LONG | 423 | -0.347 | -1.35 | -147 | 174 |
| MIS1-168h LONG | 882 | -0.140 | -0.41 | -123 | 90 |
| FIF2 SHORT | 784 | -0.147 | -1.52 | -116 | 402 |
| MAX1 SHORT | 259 | -0.147 | -0.45 | -38 | 94 |
| XSM1 LONG | 93 | -0.308 | -0.40 | -29 | 32 |
| SKW1 LONG | 82 | -0.231 | -0.37 | -19 | 51 |
| TD_1H SHORT | 36 | -0.236 | -0.32 | -8 | 38 |
| ODS1 SHORT | 114 | -0.053 | -0.34 | -6 | 86 |

Combined drag **-762 %-points** over the window. Of these, **0** clear |t| > 2 on their own book: none. The rest are directionally negative but within noise for this window and should be re-checked rather than acted on.

## Limits

- Every candidate is **uncalibrated by construction** — it has no live trades, so its corrected value is the fit extrapolated beyond its support.
- The replay has no stop-loss and no time-stop; live those are ~14 % of exits at ~-2.6 %. It also assumes every mirror fills and ignores the symbol and re-entry locks.
- **`total` is NOT realised PnL.** It is the effective per-trade rate times the REPLAY trade count, so a live-measured leg mixes a measured rate with a simulated volume — ATS2 LONG is -0.240 x 1085 although only 448 live trades exist. The `live n` column is there to make that visible; read `total` as a sizing estimate, never as a book result.
- Seat recommendations are input to an operator decision, never a change made here.

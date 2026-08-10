# Trailing roster re-rank — T-2026-KYT-9050-134
Replay `trailing_slot_budget_livewindow_t134.json` (window from 2026-07-26, generated 2026-08-10T22:04:07.285470+00:00) scored against the live book from 2026-07-26. Calibration: same-window (model error isolated from regime).

## Verdict

**No seat changes are recommended on this evidence.**

- The premise the task started from does not survive: every unrostered leg with a positive expected contribution (8 of them) is **fitted, not measured** — none has live trades. The largest is worth +34 %-points. The legs PR #198 rejected on density turn out net-NEGATIVE once corrected, so the roster kept them out for a stated reason that was wrong and an outcome that was right.
- The real finding points the other way: **9 rostered legs lose money on live evidence**, -661 %-points combined.
- But **0 of them clear |t| > 2**. On a two-week book that is a watchlist, not a retirement list. Acting on it now would repeat the error this tool was built to catch.

## Calibration — does the replay predict the live arm?

Fit over 19 legs with >= 30 live trades:

    live = -0.197 + 0.272 * replay      R2 = 0.614, residual sd = 0.321 pp

The replay overstates **17 of 19** calibrated legs; median error -0.633 pp per trade. A slope below 1 means the error grows with the prediction, so the top of the raw ranking is the least trustworthy part of it.

| leg | replay net | live net | error | live n |
|---|---:|---:|---:|---:|
| MIS2-72H SHORT | +6.337 | +1.402 | -4.935 | 91 |
| MIS1-8H SHORT | +2.013 | +0.995 | -1.018 | 67 |
| RUB1 SHORT | +1.970 | +0.531 | -1.439 | 80 |
| AIM2 LONG | +1.490 | +0.354 | -1.136 | 177 |
| AIM2 SHORT | +1.295 | -0.511 | -1.806 | 480 |
| MIS1-168H LONG | +0.901 | -0.079 | -0.980 | 86 |
| SRA2 LONG | +0.853 | +0.228 | -0.625 | 172 |
| SKW1 LONG | +0.792 | -0.217 | -1.009 | 49 |
| SKW1 SHORT | +0.557 | +0.226 | -0.331 | 74 |
| ATS2 LONG | +0.552 | -0.240 | -0.792 | 448 |
| FIF2 LONG | +0.409 | -0.224 | -0.633 | 154 |
| BR4H LONG | +0.351 | +0.139 | -0.212 | 113 |
| MIS1-72H LONG | +0.187 | -0.852 | -1.039 | 672 |
| SRA2 SHORT | +0.097 | +0.038 | -0.059 | 211 |
| XSM1 LONG | +0.030 | -0.308 | -0.338 | 32 |
| FIF2 SHORT | -0.159 | -0.177 | -0.018 | 363 |
| MAX1 SHORT | -0.195 | -0.147 | +0.048 | 94 |
| TD_1H SHORT | -0.218 | -0.254 | -0.036 | 37 |
| ODS1 SHORT | -0.240 | -0.028 | +0.212 | 83 |

## Ranking by expected net contribution

Primary column is absolute contribution (effective per-trade x replay trade count), because the seat cap provably never binds. Density is retained as the PR #198 measure.

`basis` says where the per-trade number comes from: **live** for legs with >= 30 trades in the real book, **fitted** for legs that have none and must be extrapolated through the calibration line. A fitted value never overrides a measured one — the fit regresses toward the mean and would otherwise rehabilitate legs the live book has already convicted.

| leg | roster | basis | n | replay/trade | effective/trade | total | density | occ p95 | live n |
|---|:--:|:--:|---:|---:|---:|---:|---:|---:|---:|
| AIM2 LONG | YES | live | 532 | +1.490 | +0.354 | +189 | 2.414 | 53 | 177 |
| MIS2-72h SHORT | YES | live | 105 | +6.337 | +1.402 | +147 | 211.585 | 1 | 91 |
| SRA2 LONG | YES | live | 509 | +0.853 | +0.228 | +116 | 1.120 | 36 | 172 |
| MIS1-8h SHORT | YES | live | 91 | +2.013 | +0.995 | +91 | 52.889 | 1 | 67 |
| RUB1 SHORT | YES | live | 128 | +1.970 | +0.531 | +68 | 21.709 | 3 | 80 |
| MIS2-24h SHORT | YES | fitted | 38 | +6.223 | +1.493 | +57 | 246.038 | 1 | 23 |
| ABR2 LONG | - | fitted | 135 | +1.643 | +0.249 | +34 | 4.113 | 9 |  |
| BR4H LONG | YES | live | 214 | +0.351 | +0.139 | +30 | 0.407 | 23 | 113 |
| EPD2 SHORT | - | fitted | 38 | +3.050 | +0.631 | +24 | 132.711 | 1 |  |
| MIS2-168h SHORT | YES | fitted | 19 | +5.008 | +1.163 | +22 | 162.657 | 1 | 18 |
| SKW1 SHORT | YES | live | 89 | +0.557 | +0.226 | +20 | 2.432 | 8 | 74 |
| MIS2-168h LONG | - | fitted | 233 | +1.027 | +0.082 | +19 | 5.945 | 7 |  |
| XSR1 SHORT | - | fitted | 95 | +1.449 | +0.196 | +19 | 4.874 | 8 |  |
| EPD2 LONG | - | fitted | 35 | +2.357 | +0.443 | +16 | 208.509 | 1 |  |
| SRA2 SHORT | YES | live | 306 | +0.097 | +0.038 | +12 | 0.180 | 19 | 211 |
| MIS1-72h SHORT | - | fitted | 191 | +0.946 | +0.060 | +11 | 15.693 | 3 |  |
| BB_4H SHORT | - | fitted | 78 | +0.931 | +0.056 | +4 | 1.061 | 8 |  |
| ATS2 SHORT | thin | fitted | 8 | +2.021 | +0.352 | +3 | 4.324 | 1 |  |
| RUB4 LONG | thin | fitted | 6 | +2.053 | +0.360 | +2 | 5.374 | 1 |  |
| TD_1H LONG | YES | fitted | 114 | +0.766 | +0.011 | +1 | 0.689 | 33 | 17 |
| BB_4H LONG | YES | fitted | 20 | +0.897 | +0.046 | +1 | 0.497 | 7 | 16 |
| MIS1-24h SHORT | - | fitted | 83 | +0.763 | +0.010 | +1 | 18.035 | 2 |  |
| TD_4H LONG | YES | fitted | 23 | +0.848 | +0.033 | +1 | 1.050 | 3 | 9 |
| MIS1-24h LONG | YES | fitted | 13 | +0.918 | +0.052 | +1 | 7.355 | 1 | 10 |
| RUB1 LONG | YES | fitted | 29 | +0.730 | +0.001 | +0 | 2.831 | 2 | 23 |
| MAX2 LONG | thin | fitted | 23 | +0.719 | -0.002 | -0 | 0.603 | 4 |  |
| QM_1H LONG | YES | fitted | 6 | -0.190 | -0.249 | -1 | -1.151 | 1 | 2 |
| MIS2-24h LONG | - | fitted | 112 | +0.625 | -0.027 | -3 | 3.733 | 3 |  |
| ODS1 SHORT | YES | live | 114 | -0.240 | -0.028 | -3 | -6.192 | 3 | 83 |
| LIS1 SHORT | thin | fitted | 1 | -11.542 | -3.332 | -3 | -7.045 | 1 |  |
| QM_1H SHORT | thin | fitted | 14 | -0.261 | -0.268 | -4 | -0.218 | 3 |  |
| BR1D LONG | thin | fitted | 11 | -0.587 | -0.357 | -4 | -0.843 | 1 |  |
| ATB2 SHORT | thin | fitted | 19 | -0.267 | -0.270 | -5 | -1.405 | 1 |  |
| BR1D SHORT | thin | fitted | 16 | -0.526 | -0.340 | -5 | -0.406 | 4 |  |
| MIS2-72h LONG | - | fitted | 283 | +0.651 | -0.020 | -6 | 4.567 | 6 |  |
| UFI1 SHORT | YES | fitted | 11 | -1.393 | -0.576 | -6 | -7.556 | 1 | 12 |
| SRA1 SHORT | - | fitted | 35 | -0.029 | -0.205 | -7 | -0.055 | 3 |  |
| TD_1H SHORT | YES | live | 36 | -0.218 | -0.254 | -9 | -0.462 | 4 | 37 |
| FIF1 SHORT | - | fitted | 102 | +0.297 | -0.117 | -12 | 2.968 | 3 |  |
| ATB2 LONG | - | fitted | 56 | -0.065 | -0.215 | -12 | -0.203 | 4 |  |
| TD_4H SHORT | YES | fitted | 12 | -3.115 | -1.043 | -13 | -5.091 | 2 | 9 |
| MIS1-168h SHORT | - | fitted | 52 | -0.374 | -0.299 | -16 | -4.653 | 1 |  |
| SRA1 LONG | - | fitted | 86 | -0.011 | -0.200 | -17 | -0.009 | 11 |  |
| SKW1 LONG | YES | live | 82 | +0.792 | -0.217 | -18 | 1.154 | 12 | 49 |
| RUB3 LONG | - | fitted | 152 | +0.287 | -0.119 | -18 | 0.378 | 20 |  |
| FIF1 LONG | - | fitted | 122 | +0.099 | -0.170 | -21 | 0.503 | 8 |  |
| MIS1-8h LONG | - | fitted | 38 | -1.918 | -0.718 | -27 | -24.744 | 1 |  |
| XSM1 LONG | YES | live | 93 | +0.030 | -0.308 | -29 | 0.111 | 6 | 32 |
| PEX1 SHORT | - | fitted | 137 | -0.151 | -0.238 | -33 | -0.741 | 7 |  |
| BR2H LONG | - | fitted | 476 | +0.469 | -0.070 | -33 | 0.551 | 43 |  |
| MAX1 SHORT | YES | live | 259 | -0.195 | -0.147 | -38 | -0.424 | 19 | 94 |
| MIS2-8h LONG | - | fitted | 254 | -0.070 | -0.216 | -55 | -0.527 | 5 |  |
| BR4H SHORT | - | fitted | 146 | -0.750 | -0.401 | -59 | -0.735 | 20 |  |
| MIS1-168h LONG | YES | live | 882 | +0.901 | -0.079 | -70 | 1.229 | 134 | 86 |
| BR1Hv2 LONG | - | fitted | 866 | +0.407 | -0.087 | -75 | 0.515 | 71 |  |
| BR2H SHORT | - | fitted | 314 | -0.181 | -0.246 | -77 | -0.235 | 26 |  |
| FIF2 LONG | YES | live | 423 | +0.409 | -0.224 | -95 | 8.917 | 9 | 154 |
| FIF2 SHORT | YES | live | 784 | -0.159 | -0.177 | -139 | -9.880 | 13 | 363 |
| BR1Hv2 SHORT | - | fitted | 640 | -0.763 | -0.404 | -259 | -0.907 | 65 |  |
| ATS2 LONG | YES | live | 1085 | +0.552 | -0.240 | -261 | 0.535 | 129 | 448 |
| TSM1 SHORT | - | fitted | 1119 | -0.142 | -0.236 | -264 | -0.175 | 89 |  |
| EPD3 LONG | - | fitted | 3336 | +0.434 | -0.079 | -265 | 0.654 | 202 |  |
| AIM2 SHORT | - | live | 738 | +1.295 | -0.511 | -377 | 3.964 | 24 | 480 |
| ROM1 SHORT | - | fitted | 2879 | +0.101 | -0.170 | -489 | 0.540 | 77 |  |
| ROM1 LONG | - | fitted | 4918 | +0.289 | -0.119 | -584 | 0.969 | 145 |  |
| EPD3 SHORT | - | fitted | 6982 | +0.052 | -0.183 | -1278 | 0.132 | 237 |  |
| MIS1-72h LONG | - | live | 1689 | +0.187 | -0.852 | -1439 | 0.212 | 168 | 672 |

## Candidates — unrostered legs that survive the correction

- **ABR2 LONG** — +0.249 %/trade (fitted) over 135 replay trades (+34 % total), density 4.113, p95 occupancy 9 seats. NO live coverage — the fit extrapolated beyond its support.

- **EPD2 SHORT** — +0.631 %/trade (fitted) over 38 replay trades (+24 % total), density 132.711, p95 occupancy 1 seats. NO live coverage — the fit extrapolated beyond its support.

- **MIS2-168h LONG** — +0.082 %/trade (fitted) over 233 replay trades (+19 % total), density 5.945, p95 occupancy 7 seats. NO live coverage — the fit extrapolated beyond its support.

- **XSR1 SHORT** — +0.196 %/trade (fitted) over 95 replay trades (+19 % total), density 4.874, p95 occupancy 8 seats. NO live coverage — the fit extrapolated beyond its support.

- **EPD2 LONG** — +0.443 %/trade (fitted) over 35 replay trades (+16 % total), density 208.509, p95 occupancy 1 seats. NO live coverage — the fit extrapolated beyond its support.

- **MIS1-72h SHORT** — +0.060 %/trade (fitted) over 191 replay trades (+11 % total), density 15.693, p95 occupancy 3 seats. NO live coverage — the fit extrapolated beyond its support.

- **BB_4H SHORT** — +0.056 %/trade (fitted) over 78 replay trades (+4 % total), density 1.061, p95 occupancy 8 seats. NO live coverage — the fit extrapolated beyond its support.

- **MIS1-24h SHORT** — +0.010 %/trade (fitted) over 83 replay trades (+1 % total), density 18.035, p95 occupancy 2 seats. NO live coverage — the fit extrapolated beyond its support.

## Rostered legs that lose money on live evidence

These are **measured, not extrapolated**: each has live trades past the floor. This is where the roster is actually wrong, and it is the opposite of the question the task started from.

| leg | n | live/trade | t | total | live n |
|---|---:|---:|---:|---:|---:|
| ATS2 LONG | 1085 | -0.240 | -1.71 | -261 | 448 |
| FIF2 SHORT | 784 | -0.177 | -1.71 | -139 | 363 |
| FIF2 LONG | 423 | -0.224 | -0.82 | -95 | 154 |
| MIS1-168h LONG | 882 | -0.079 | -0.22 | -70 | 86 |
| MAX1 SHORT | 259 | -0.147 | -0.45 | -38 | 94 |
| XSM1 LONG | 93 | -0.308 | -0.40 | -29 | 32 |
| SKW1 LONG | 82 | -0.217 | -0.33 | -18 | 49 |
| TD_1H SHORT | 36 | -0.254 | -0.33 | -9 | 37 |
| ODS1 SHORT | 114 | -0.028 | -0.18 | -3 | 83 |

Combined drag **-661 %-points** over the window. Of these, **0** clear |t| > 2 on their own book: none. The rest are directionally negative but within noise for this window and should be re-checked rather than acted on.

## Limits

- Every candidate is **uncalibrated by construction** — it has no live trades, so its corrected value is the fit extrapolated beyond its support.
- The replay has no stop-loss and no time-stop; live those are ~14 % of exits at ~-2.6 %. It also assumes every mirror fills and ignores the symbol and re-entry locks.
- Seat recommendations are input to an operator decision, never a change made here.

# Trailing arm book health — exit rules measured on the open book (T-2026-KYT-9050-052)

_generated 2026-08-04T07:02:05.936031+00:00 · read-only · roster legs excluding ROM1 · x=10% · tf 15m · since 2026-03-01 · fee 0.10 %/trade · 47766 trades_

_TP1 coverage: 9270 trades with a usable TP1, 38282 imputed from the leg median · uncovered trades fall back to act=2 %, so the `trail-tp1` rows are a BLEND_

**Question:** `tools/trailing_slot_budget.py` measured realised sums and slots. A rule that
closes winners and holds losers looks good there and bad in the open book —
Bot 40 proved that live. Here every rule is measured on BOTH sides: realised
AND composition of the open book (equity = realised sum + open MTM,
equal-weighted, unlevered %-points).

| Rule | n | Σ net | avg/trade | avg slots | p95 | net/slot-day | Equity final | **Equity MaxDD** | net/avg slot | DD/avg slot | avg book mark | book underwater | avg L open | avg S open |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Hold (Fleet-Exit, SL/TP/Timeout) | 47766 | 58072 | 1.216 | 998 | 1628 | 0.372 | 58072 | **20962** | 58 | 21.0 | +2.25 % | 44 % | 751 | 248 |
| Trail act=2 % (Bot 40 today) | 47766 | 46521 | 0.974 | 261 | 501 | 1.139 | 46521 | **5146** | 178 | 19.7 | -3.08 % | 80 % | 218 | 43 |
| Trail act=5 % | 47766 | 59305 | 1.242 | 530 | 904 | 0.715 | 59305 | **9998** | 112 | 18.9 | -2.14 % | 67 % | 425 | 105 |
| Trail act=10 % | 47766 | 71554 | 1.498 | 760 | 1335 | 0.602 | 71554 | **14726** | 94 | 19.4 | -1.01 % | 55 % | 600 | 160 |
| Trail act=2 %, x=20 % (closes slower) | 47766 | 39547 | 0.828 | 262 | 501 | 0.964 | 39547 | **5510** | 151 | 21.0 | -3.02 % | 80 % | 219 | 43 |
| Trail act=2 %, x=30 % | 47766 | 33618 | 0.704 | 266 | 503 | 0.808 | 33618 | **7102** | 126 | 26.7 | -2.81 % | 78 % | 221 | 45 |
| Trail act=10 %, x=20 % | 47766 | 63529 | 1.330 | 765 | 1341 | 0.531 | 63529 | **15424** | 83 | 20.2 | -0.92 % | 55 % | 602 | 162 |
| Trail 2 % + Time-Stop 24 h | 47766 | 35348 | 0.740 | 130 | 263 | 1.736 | 35348 | **3694** | 271 | 28.4 | -1.58 % | 65 % | 106 | 24 |
| Trail 2 % + Time-Stop 48 h | 47766 | 39987 | 0.837 | 178 | 354 | 1.438 | 39987 | **4855** | 225 | 27.3 | -1.98 % | 70 % | 147 | 31 |
| Trail 2 % + Time-Stop 72 h | 47766 | 42397 | 0.888 | 203 | 406 | 1.338 | 42397 | **4991** | 209 | 24.6 | -2.24 % | 73 % | 168 | 34 |
| Trail 2 % + Hard-Stop −2 % | 47766 | 31762 | 0.665 | 70 | 153 | 2.912 | 31762 | **2508** | 455 | 36.0 | +0.05 % | 40 % | 57 | 13 |
| Trail 2 % only SHORT (LONG holds) | 47766 | 55508 | 1.162 | 794 | 1553 | 0.447 | 55508 | **21643** | 70 | 27.3 | +0.79 % | 51 % | 751 | 43 |
| Trail 2 % only LONG (SHORT holds) | 47766 | 49085 | 1.028 | 466 | 890 | 0.674 | 49085 | **5917** | 105 | 12.7 | +1.00 % | 58 % | 218 | 248 |
| Trail 2 %, 50 % partial close | 47766 | 52296 | 1.095 | 630 | 1018 | 0.531 | 52296 | **12660** | 83 | 20.1 | +1.36 % | 51 % | 484 | 145 |
| Trail armed at TP1 (per trade, no floor) | 47766 | 56323 | 1.179 | 446 | 758 | 0.807 | 56323 | **7733** | 126 | 17.3 | -2.01 % | 69 % | 343 | 104 |
| Trail armed at max(TP1, 2 %) | 47766 | 56298 | 1.179 | 447 | 761 | 0.805 | 56298 | **7732** | 126 | 17.3 | -2.01 % | 69 % | 343 | 104 |
| Trail at TP1 + Time-Stop 24 h | 47766 | 32531 | 0.681 | 177 | 368 | 1.174 | 32531 | **4002** | 184 | 22.6 | -0.99 % | 56 % | 137 | 40 |
| Trail at max(TP1, 2 %) + Time-Stop 24 h | 47766 | 32455 | 0.679 | 178 | 370 | 1.168 | 32455 | **4000** | 183 | 22.5 | -0.99 % | 56 % | 138 | 40 |
| Trail at TP1 + Time-Stop 24 h + Cap ±50 | 25139 | 23722 | 0.944 | 90 | 177 | 1.680 | 23722 | **1000** | 263 | 11.1 | -0.98 % | 54 % | 59 | 32 |
| Trail at max(TP1, 2 %) + ts24 + Cap ±50 | 25123 | 23694 | 0.943 | 91 | 178 | 1.672 | 23694 | **1000** | 261 | 11.0 | -0.97 % | 54 % | 59 | 32 |
| Trail 2 % + Exposure-Cap ±50 | 22126 | 29256 | 1.322 | 103 | 201 | 1.812 | 29256 | **813** | 283 | 7.9 | -3.28 % | 78 % | 67 | 37 |
| Trail 2 % + Exposure-Cap ±100 | 28197 | 33576 | 1.191 | 139 | 252 | 1.547 | 33576 | **1394** | 242 | 10.0 | -3.19 % | 79 % | 100 | 39 |
| Trail 2 % + Time-Stop 24 h + Cap ±50 | 26717 | 26723 | 1.000 | 68 | 128 | 2.532 | 26723 | **674** | 396 | 10.0 | -1.65 % | 65 % | 47 | 21 |
| SL ratchet: breakeven from +2 % (no trail) | 47766 | 16843 | 0.353 | 444 | 742 | 0.243 | 16843 | **9891** | 38 | 22.3 | +2.11 % | 46 % | 342 | 102 |
| Breakeven from +2 % + Time-Stop 24 h | 47766 | 8774 | 0.184 | 277 | 474 | 0.203 | 8774 | **8344** | 32 | 30.1 | +4.75 % | 31 % | 203 | 74 |
| Breakeven 2 % + Time-Stop 24 h + Cap ±50 | 23952 | 3200 | 0.134 | 141 | 251 | 0.145 | 3200 | **2027** | 23 | 14.4 | +5.26 % | 30 % | 80 | 61 |
| Breakeven 2 % + Time-Stop 24 h + Cap ±100 | 29516 | 3556 | 0.120 | 176 | 288 | 0.130 | 3556 | **2753** | 20 | 15.7 | +4.98 % | 31 % | 110 | 66 |
| Breakeven from +5 % + Time-Stop 24 h | 47766 | 8455 | 0.177 | 310 | 539 | 0.174 | 8455 | **9116** | 27 | 29.4 | +4.85 % | 33 % | 225 | 85 |
| Hold under a hard 500-slot cap | 23487 | 27787 | 1.183 | 474 | 500 | 0.375 | 27787 | **7258** | 59 | 15.3 | +2.66 % | 43 % | 357 | 117 |
| Breakeven 2 % + Time-Stop 24 h @ 500-Cap | 44309 | 4523 | 0.102 | 260 | 438 | 0.111 | 4523 | **6398** | 17 | 24.6 | +4.74 % | 32 % | 188 | 72 |
| Breakeven 5 % + Time-Stop 24 h @ 500-Cap | 43698 | 4260 | 0.098 | 287 | 468 | 0.095 | 4260 | **6831** | 15 | 23.8 | +4.86 % | 34 % | 206 | 82 |
| Hold @ 1000 (2 Channels, least-loaded) | 38884 | 43123 | 1.109 | 807 | 1000 | 0.342 | 43123 | **14888** | 53 | 18.4 | +2.28 % | 44 % | 606 | 201 |
| Breakeven 5 % + Time-Stop 24 h @ 1000 (2 Channels) | 46444 | 5285 | 0.114 | 305 | 524 | 0.111 | 5285 | **7779** | 17 | 25.5 | +4.84 % | 33 % | 220 | 85 |
| Hold @ 1500 (3 Channels) | 45783 | 53073 | 1.159 | 966 | 1476 | 0.351 | 53073 | **19684** | 55 | 20.4 | +2.24 % | 44 % | 721 | 245 |
| Breakeven 5 % + Time-Stop 24 h @ 1500 (3 Channels) | 47037 | 6780 | 0.144 | 308 | 538 | 0.141 | 6780 | **8377** | 22 | 27.2 | +4.85 % | 33 % | 222 | 85 |
| Book feedback gate (D only if open D book > −1 %) | 8442 | 14272 | 1.690 | 32 | 88 | 2.858 | 14272 | **1305** | 447 | 40.9 | -3.87 % | 80 % | 22 | 10 |
| BTC direction gate (LONG only if 24h ret > 0) | 25238 | 27926 | 1.107 | 133 | 270 | 1.347 | 27926 | **2758** | 211 | 20.8 | -3.40 % | 80 % | 105 | 27 |
| Mover gate: ignore coin |24h| > 30 % (Trail a2) | 46281 | 42505 | 0.918 | 259 | 498 | 1.048 | 42505 | **5208** | 164 | 20.1 | -2.99 % | 80 % | 218 | 42 |
| Mover gate: ignore coin |24h| > 50 % (Trail a2) | 47282 | 45257 | 0.957 | 261 | 500 | 1.110 | 45257 | **5135** | 173 | 19.7 | -3.07 % | 80 % | 218 | 43 |
| Chase gate: ignore only chasing > 20 % | 47065 | 40611 | 0.863 | 261 | 500 | 0.996 | 40611 | **5233** | 156 | 20.1 | -3.09 % | 80 % | 218 | 43 |
| Chase gate: ignore only chasing > 50 % | 47649 | 44679 | 0.938 | 261 | 501 | 1.094 | 44679 | **5151** | 171 | 19.7 | -3.09 % | 80 % | 218 | 43 |
| Trail a2 + SL cap −5 % unlev (−100 % @20x) | 47766 | 31320 | 0.656 | 160 | 346 | 1.248 | 31320 | **4243** | 195 | 26.4 | -0.88 % | 66 % | 136 | 24 |
| DEPLOYED (Trail+ts24+Cap50) + SL cap −5 % | 27256 | 22392 | 0.822 | 57 | 112 | 2.517 | 22392 | **798** | 394 | 14.0 | -0.62 % | 59 % | 41 | 16 |
| DEPLOYED today: Trail+ts24+Cap50 (causal, reference) | 26717 | 26723 | 1.000 | 68 | 128 | 2.532 | 26723 | **674** | 396 | 10.0 | -1.65 % | 65 % | 47 | 21 |
| Portfolio trail 10 % (no per-trade trail) | 47766 | 13736 | 0.288 | 68 | 263 | 1.291 | 13736 | **6602** | 202 | 97.0 | -0.32 % | 38 % | 51 | 17 |
| Portfolio trail 15 % (no per-trade trail) | 47766 | 13783 | 0.288 | 69 | 253 | 1.282 | 13783 | **6602** | 200 | 96.0 | -0.28 % | 38 % | 52 | 17 |

## Reading Guide

- **Equity MaxDD** is the metric the study lacked: max. drawdown of the curve
  (realised + open), in unlevered %-points across the equal-weighted book.
- **avg book mark** = time-averaged mean mark of the open positions. A strongly
  negative value means: the book structurally consists of losers.
- **book underwater** = time-averaged share of open positions in the red.

# Trailing arm book health — exit rules measured on the open book (T-2026-KYT-9050-052)

_generated 2026-08-04T06:43:53.328601+00:00 · read-only · roster legs excluding ROM1 · x=10% · tf 15m · since 2026-07-01 · fee 0.10 %/trade · 9149 trades_

_TP1 coverage: 9149 trades with a usable TP1 · population restricted to covered trades (`--tp1-only`)_

**Question:** `tools/trailing_slot_budget.py` measured realised sums and slots. A rule that
closes winners and holds losers looks good there and bad in the open book —
Bot 40 proved that live. Here every rule is measured on BOTH sides: realised
AND composition of the open book (equity = realised sum + open MTM,
equal-weighted, unlevered %-points).

| Rule | n | Σ net | avg/trade | avg slots | p95 | net/slot-day | Equity final | **Equity MaxDD** | net/avg slot | DD/avg slot | avg book mark | book underwater | avg L open | avg S open |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Hold (Fleet-Exit, SL/TP/Timeout) | 9149 | 120 | 0.013 | 477 | 1012 | 0.007 | 120 | **4065** | 0 | 8.5 | +1.05 % | 52 % | 326 | 150 |
| Trail act=2 % (Bot 40 today) | 9149 | 6539 | 0.715 | 226 | 530 | 0.844 | 6539 | **2485** | 29 | 11.0 | -2.28 % | 80 % | 190 | 36 |
| Trail act=5 % | 9149 | 5870 | 0.642 | 362 | 842 | 0.473 | 5870 | **3136** | 16 | 8.7 | -1.72 % | 68 % | 282 | 80 |
| Trail act=10 % | 9149 | 4550 | 0.497 | 429 | 955 | 0.309 | 4550 | **3352** | 11 | 7.8 | -0.94 % | 59 % | 313 | 117 |
| Trail act=2 %, x=20 % (closes slower) | 9149 | 5327 | 0.582 | 227 | 534 | 0.684 | 5327 | **2527** | 24 | 11.2 | -2.23 % | 80 % | 190 | 36 |
| Trail act=2 %, x=30 % | 9149 | 4195 | 0.459 | 230 | 542 | 0.530 | 4195 | **2634** | 18 | 11.4 | -2.02 % | 78 % | 192 | 38 |
| Trail act=10 %, x=20 % | 9149 | 3534 | 0.386 | 431 | 960 | 0.239 | 3534 | **3473** | 8 | 8.1 | -0.89 % | 59 % | 313 | 118 |
| Trail 2 % + Time-Stop 24 h | 9149 | 6330 | 0.692 | 112 | 290 | 1.646 | 6330 | **1199** | 57 | 10.7 | -1.29 % | 70 % | 90 | 22 |
| Trail 2 % + Time-Stop 48 h | 9149 | 6460 | 0.706 | 157 | 368 | 1.197 | 6460 | **1367** | 41 | 8.7 | -1.64 % | 75 % | 129 | 28 |
| Trail 2 % + Time-Stop 72 h | 9149 | 6838 | 0.747 | 182 | 400 | 1.096 | 6838 | **1533** | 38 | 8.4 | -1.78 % | 76 % | 151 | 31 |
| Trail 2 % + Hard-Stop −2 % | 9149 | 6034 | 0.659 | 70 | 187 | 2.510 | 6034 | **772** | 86 | 11.0 | +0.15 % | 47 % | 55 | 15 |
| Trail 2 % only SHORT (LONG holds) | 9149 | 2526 | 0.276 | 362 | 854 | 0.203 | 2526 | **4186** | 7 | 11.6 | -0.96 % | 64 % | 326 | 36 |
| Trail 2 % only LONG (SHORT holds) | 9149 | 4133 | 0.452 | 340 | 698 | 0.353 | 4133 | **1966** | 12 | 5.8 | +1.01 % | 57 % | 190 | 150 |
| Trail 2 %, 50 % partial close | 9149 | 3330 | 0.364 | 351 | 774 | 0.276 | 3330 | **2861** | 9 | 8.2 | +0.15 % | 60 % | 258 | 93 |
| Trail armed at TP1 (per trade, no floor) | 9149 | 6630 | 0.725 | 352 | 795 | 0.548 | 6630 | **3230** | 19 | 9.2 | -0.64 % | 64 % | 271 | 81 |
| Trail armed at max(TP1, 2 %) | 9149 | 6587 | 0.720 | 356 | 812 | 0.540 | 6587 | **3230** | 19 | 9.1 | -0.63 % | 64 % | 274 | 82 |
| Trail at TP1 + Time-Stop 24 h | 9149 | 5826 | 0.637 | 150 | 364 | 1.130 | 5826 | **1499** | 39 | 10.0 | -0.19 % | 56 % | 112 | 38 |
| Trail at max(TP1, 2 %) + Time-Stop 24 h | 9149 | 5769 | 0.631 | 152 | 370 | 1.104 | 5769 | **1512** | 38 | 9.9 | -0.16 % | 55 % | 114 | 39 |
| Trail at TP1 + Time-Stop 24 h + Cap ±50 | 6287 | 5835 | 0.928 | 98 | 210 | 1.726 | 5835 | **279** | 59 | 2.8 | -0.11 % | 55 % | 60 | 38 |
| Trail at max(TP1, 2 %) + ts24 + Cap ±50 | 6274 | 5807 | 0.925 | 100 | 212 | 1.695 | 5807 | **289** | 58 | 2.9 | -0.09 % | 54 % | 61 | 39 |
| Trail 2 % + Exposure-Cap ±50 | 4970 | 6057 | 1.219 | 104 | 218 | 1.702 | 6057 | **271** | 58 | 2.6 | -2.29 % | 79 % | 68 | 36 |
| Trail 2 % + Exposure-Cap ±100 | 5878 | 6466 | 1.100 | 129 | 267 | 1.459 | 6466 | **462** | 50 | 3.6 | -2.28 % | 79 % | 94 | 36 |
| Trail 2 % + Time-Stop 24 h + Cap ±50 | 6250 | 6379 | 1.021 | 70 | 151 | 2.653 | 6379 | **184** | 91 | 2.6 | -1.26 % | 69 % | 48 | 22 |
| SL ratchet: breakeven from +2 % (no trail) | 9149 | -499 | -0.054 | 326 | 701 | -0.045 | -499 | **3391** | -2 | 10.4 | +0.96 % | 55 % | 234 | 92 |
| Breakeven from +2 % + Time-Stop 24 h | 9149 | 165 | 0.018 | 196 | 436 | 0.025 | 165 | **2388** | 1 | 12.2 | +3.36 % | 36 % | 122 | 73 |
| Breakeven 2 % + Time-Stop 24 h + Cap ±50 | 6767 | 972 | 0.144 | 148 | 318 | 0.192 | 972 | **606** | 7 | 4.1 | +3.58 % | 35 % | 77 | 71 |
| Breakeven 2 % + Time-Stop 24 h + Cap ±100 | 7460 | 791 | 0.106 | 162 | 347 | 0.142 | 791 | **794** | 5 | 4.9 | +3.47 % | 36 % | 89 | 73 |
| Breakeven from +5 % + Time-Stop 24 h | 9149 | 178 | 0.019 | 209 | 463 | 0.025 | 178 | **2726** | 1 | 13.1 | +3.18 % | 39 % | 136 | 73 |
| Hold under a hard 500-slot cap | 5801 | 1418 | 0.244 | 316 | 500 | 0.131 | 1418 | **1566** | 4 | 5.0 | +1.26 % | 50 % | 190 | 125 |
| Breakeven 2 % + Time-Stop 24 h @ 500-Cap | 8555 | 125 | 0.015 | 184 | 426 | 0.020 | 125 | **1537** | 1 | 8.4 | +3.36 % | 36 % | 111 | 73 |
| Breakeven 5 % + Time-Stop 24 h @ 500-Cap | 8514 | 274 | 0.032 | 194 | 456 | 0.041 | 274 | **1674** | 1 | 8.6 | +3.18 % | 39 % | 121 | 73 |
| Hold @ 1000 (2 Channels, least-loaded) | 8587 | 84 | 0.010 | 450 | 862 | 0.005 | 84 | **3200** | 0 | 7.1 | +1.05 % | 52 % | 300 | 150 |
| Breakeven 5 % + Time-Stop 24 h @ 1000 (2 Channels) | 9101 | 196 | 0.021 | 208 | 462 | 0.027 | 196 | **2626** | 1 | 12.6 | +3.18 % | 39 % | 134 | 73 |
| Hold @ 1500 (3 Channels) | 9135 | 132 | 0.014 | 476 | 1007 | 0.008 | 132 | **4050** | 0 | 8.5 | +1.05 % | 52 % | 326 | 150 |
| Breakeven 5 % + Time-Stop 24 h @ 1500 (3 Channels) | 9149 | 178 | 0.019 | 209 | 463 | 0.025 | 178 | **2726** | 1 | 13.1 | +3.18 % | 39 % | 136 | 73 |
| Book feedback gate (D only if open D book > −1 %) | 1245 | 1259 | 1.011 | 35 | 115 | 1.035 | 1259 | **377** | 36 | 10.6 | -2.70 % | 83 % | 26 | 9 |
| BTC direction gate (LONG only if 24h ret > 0) | 4250 | 2951 | 0.694 | 102 | 232 | 0.842 | 2951 | **1636** | 29 | 16.0 | -2.60 % | 82 % | 87 | 15 |
| Mover gate: ignore coin |24h| > 30 % (Trail a2) | 8889 | 5474 | 0.616 | 225 | 529 | 0.708 | 5474 | **2493** | 24 | 11.1 | -2.29 % | 80 % | 190 | 35 |
| Mover gate: ignore coin |24h| > 50 % (Trail a2) | 9064 | 6004 | 0.662 | 226 | 529 | 0.776 | 6004 | **2485** | 27 | 11.0 | -2.28 % | 80 % | 190 | 35 |
| Chase gate: ignore only chasing > 20 % | 8977 | 5006 | 0.558 | 225 | 529 | 0.647 | 5006 | **2498** | 22 | 11.1 | -2.29 % | 80 % | 190 | 36 |
| Chase gate: ignore only chasing > 50 % | 9119 | 6016 | 0.660 | 226 | 530 | 0.777 | 6016 | **2485** | 27 | 11.0 | -2.28 % | 80 % | 190 | 36 |
| Trail a2 + SL cap −5 % unlev (−100 % @20x) | 9149 | 5706 | 0.624 | 167 | 390 | 0.996 | 5706 | **1599** | 34 | 9.6 | -0.87 % | 70 % | 139 | 28 |
| DEPLOYED (Trail+ts24+Cap50) + SL cap −5 % | 6345 | 5925 | 0.934 | 64 | 140 | 2.711 | 5925 | **126** | 93 | 2.0 | -0.61 % | 64 % | 44 | 20 |
| DEPLOYED today: Trail+ts24+Cap50 (causal, reference) | 6250 | 6379 | 1.021 | 70 | 151 | 2.653 | 6379 | **184** | 91 | 2.6 | -1.26 % | 69 % | 48 | 22 |
| Portfolio trail 10 % (no per-trade trail) | 9149 | 2596 | 0.284 | 34 | 172 | 2.213 | 2596 | **686** | 76 | 20.1 | +0.60 % | 46 % | 25 | 9 |
| Portfolio trail 15 % (no per-trade trail) | 9149 | 2700 | 0.295 | 38 | 172 | 2.054 | 2700 | **686** | 71 | 17.9 | +0.75 % | 45 % | 27 | 11 |

## Reading Guide

- **Equity MaxDD** is the metric the study lacked: max. drawdown of the curve
  (realised + open), in unlevered %-points across the equal-weighted book.
- **avg book mark** = time-averaged mean mark of the open positions. A strongly
  negative value means: the book structurally consists of losers.
- **book underwater** = time-averaged share of open positions in the red.

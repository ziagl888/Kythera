# K3 · FRL — Funding-Risk-Layer study (T-2026-CU-9050-134)

_Generated 2026-07-16T06:50:08.487727+00:00 · read-only fleet analysis · fee/side 0.0005 (round-trip 0.0010)_

**VERDICT: edge-found**

- monotone funding→PnL gradient stable in BOTH directions & BOTH halves: **True** (primary)
- hard extreme-zone claim stable in both halves: True (secondary)

## Primary test — monotone funding→PnL gradient (per-trade Spearman)

ABR predicts LONG corr>0 (higher funding → better LONG), SHORT corr<0.

| dir | expected | Spearman all | val | test | sign holds both halves |
|---|---|--:|--:|--:|:--:|
| LONG | positive | 0.0593 | 0.0441 | 0.0174 | True |
| SHORT | negative | -0.0884 | -0.1207 | -0.0182 | True |

## Population

- raw closed_ai_signals rows: 445,675
- deduped (symbol,model,direction,open_time): 88,192
- priced (entry>0 & close_price present): 88,192
- with as-of funding (fund_24h): 82,657
- open_time span (UTC): 2026-02-24 10:43:59.650539+00:00 .. 2026-07-16 06:26:13.088268+00:00
- median split (val|test): 2026-04-07 00:14:22.182900992+00:00
- fund_24h quintile edges (bps): [-0.615, 0.329, 0.5, 0.5]
- winsor bounds for mean net-PnL (1/99 pct, %): [-55.666, 60.0] — WR & median use raw values

## Hypothesis test (chrono val/test must agree)

### SHORT@extreme-positive
- zone n (total): 1855
- zone net-PnL/trade  val: -4.105%  |  test: 2.3036%
- baseline net-PnL/trade  val: 5.8276%  |  test: 0.747%
- worse-than-baseline in BOTH halves: False (sufficient n both halves: True)

### LONG@extreme-negative
- zone n (total): 4036
- zone net-PnL/trade  val: -5.0172%  |  test: -0.9104%
- baseline net-PnL/trade  val: -3.7501%  |  test: 1.3567%
- worse-than-baseline in BOTH halves: True (sufficient n both halves: True)

## Direction × funding zone (fleet-wide, funded trades)

| dir | zone | n | WR | avg net PnL % | avg fund_24h bps | val PnL% (n) | test PnL% (n) |
|---|---|--:|--:|--:|--:|--:|--:|
| LONG | EXTREME_NEG(<-3bps) | 4036 | 0.3067 | -3.5896 | -20.864 | -5.0172 (2633) | -0.9104 (1403) |
| LONG | Q1 | 5499 | 0.3764 | -3.3196 | -1.481 | -5.2685 (3915) | 1.4971 (1584) |
| LONG | Q2 | 9936 | 0.4095 | -1.8408 | -0.038 | -3.9884 (5988) | 1.4164 (3948) |
| LONG | Q3 | 18977 | 0.3837 | -0.8985 | 0.485 | -3.5938 (8923) | 1.4936 (10054) |
| LONG | Q5 | 7386 | 0.3989 | 0.2218 | 1.142 | -1.7118 (3294) | 1.7782 (4092) |
| LONG | EXTREME_POS(>+3bps) | 1469 | 0.3662 | 1.3241 | 6.469 | 2.1027 (444) | 0.9869 (1025) |
| SHORT | EXTREME_NEG(<-3bps) | 3322 | 0.5467 | 5.6631 | -21.031 | 7.1212 (2060) | 3.2829 (1262) |
| SHORT | Q1 | 3675 | 0.4882 | 5.9483 | -1.481 | 9.0601 (2399) | 0.0979 (1276) |
| SHORT | Q2 | 6598 | 0.4657 | 3.5514 | -0.029 | 6.6105 (3287) | 0.5144 (3311) |
| SHORT | Q3 | 14330 | 0.4505 | 2.5669 | 0.486 | 5.7043 (5835) | 0.4119 (8495) |
| SHORT | Q5 | 5574 | 0.4286 | 1.2451 | 1.223 | 2.5321 (1967) | 0.5433 (3607) |
| SHORT | EXTREME_POS(>+3bps) | 1855 | 0.372 | 0.2895 | 6.641 | -4.105 (583) | 2.3036 (1272) |

## ABR2 gate generalization (fleet-wide)

| test | n | WR | avg net PnL % |
|---|--:|--:|--:|
| LONG in-gate (fund_24h>+3bps) | 1469 | 0.3662 | 1.3241 |
| LONG out-gate | 45834 | 0.3841 | -1.4497 |
| SHORT in-veto (fund_24h>+1.5bps) | 3333 | 0.408 | 1.38 |
| SHORT out-veto | 32021 | 0.464 | 3.2406 |

## Per-model extreme-zone effect (funded n>=300)

| model | dir | base n | base PnL% | ext-pos n | ext-pos PnL% | ext-neg n | ext-neg PnL% |
|---|---|--:|--:|--:|--:|--:|--:|
| AIM1 | LONG | 878 | 0.3189 | 109 | 1.0853 | 150 | -0.5972 |
| AIM1 | SHORT | 2181 | -1.367 | 429 | -3.2502 | 323 | 1.5691 |
| AIM2 | LONG | 829 | -0.0661 | 24 | 3.2005 | 47 | -4.9604 |
| AIM2 | SHORT | 490 | 1.29 | 29 | 3.5548 | 50 | 3.8401 |
| ATS1 | LONG | 3120 | -3.7678 | 125 | -1.8008 | 307 | -3.4986 |
| ATS1 | SHORT | 2266 | 5.8633 | 134 | 4.6874 | 269 | 5.5163 |
| ATS1_Robust | LONG | 476 | -33.1343 | 1 | 7.3849 | 58 | -22.7599 |
| ATS1_Robust | SHORT | 473 | 32.7055 | 1 | -11.3504 | 58 | 22.249 |
| BB_1H | LONG | 1700 | 1.3626 | 35 | -0.7779 | 69 | 2.745 |
| BB_1H | SHORT | 2339 | -1.4517 | 42 | -1.3369 | 128 | -0.1351 |
| BB_4H | LONG | 1126 | 1.454 | 11 | -2.0488 | 40 | 3.1575 |
| BB_4H | SHORT | 1343 | -0.8062 | 17 | 0.9369 | 73 | -0.5977 |
| BR1H | LONG | 3519 | 1.3059 | 79 | 1.2162 | 162 | 0.0352 |
| BR1H | SHORT | 3228 | -2.1776 | 67 | -3.2168 | 158 | -1.216 |
| BR1Hv2 | LONG | 333 | -0.5428 | 2 | 5.0827 | 6 | -0.5354 |
| BR1Hv2 | SHORT | 345 | -1.0831 | 4 | 2.6927 | 13 | 0.5485 |
| BR2H | LONG | 1932 | 0.8821 | 39 | 3.1001 | 92 | -0.6982 |
| BR2H | SHORT | 1903 | -1.6529 | 27 | -1.3714 | 83 | -1.5495 |
| BR4H | LONG | 859 | 1.3264 | 26 | 1.3963 | 37 | -2.4914 |
| BR4H | SHORT | 810 | -1.5216 | 10 | -2.6784 | 37 | -2.5696 |
| EPD1 | LONG | 1466 | -5.681 | 142 | 5.3115 | 422 | 0.2084 |
| EPD1 | SHORT | 5449 | 5.2039 | 315 | 3.4893 | 492 | 4.4861 |
| EPD3 | LONG | 630 | 0.7916 | 37 | 0.0677 | 21 | -0.1036 |
| EPD3 | SHORT | 553 | -0.4744 | 28 | -3.0806 | 46 | 0.8031 |
| MIS1-168H | LONG | 7056 | 1.0428 | 162 | 3.4748 | 517 | 0.2265 |
| MIS1-168H | SHORT | 98 | 3.2738 | 36 | 6.5398 | 27 | 2.9177 |
| MIS1-168h_dump | SHORT | 513 | 24.9095 | 1 | 37.9983 | 126 | 22.5867 |
| MIS1-168h_pump | LONG | 530 | -25.6716 | 1 | -20.7742 | 76 | -17.789 |
| MIS1-24H | LONG | 191 | 4.2073 | 21 | 4.5776 | 33 | 0.3811 |
| MIS1-24H | SHORT | 204 | 1.5783 | 43 | -0.4888 | 60 | 5.0265 |
| MIS1-24h_dump | SHORT | 639 | 21.2361 | 1 | 52.6061 | 167 | 16.0746 |
| MIS1-24h_pump | LONG | 488 | -21.6961 | 1 | -51.2384 | 135 | -21.6363 |
| MIS1-72H | LONG | 11561 | 1.3935 | 336 | 1.2222 | 757 | -0.0935 |
| MIS1-72H | SHORT | 298 | 2.4914 | 65 | 1.6502 | 79 | 1.0387 |
| MIS1-72h_dump | SHORT | 608 | 22.5008 | 1 | 52.6061 | 148 | 15.6647 |
| MIS1-72h_pump | LONG | 633 | -27.0296 | 1 | -10.5334 | 135 | -22.9197 |
| MIS1-8H | LONG | 207 | -1.6169 | 29 | 0.1861 | 55 | -1.907 |
| MIS1-8H | SHORT | 379 | 2.4438 | 65 | -0.669 | 85 | 5.2722 |
| MIS1-8h_dump | SHORT | 718 | 20.8264 | 2 | -1.5301 | 171 | 15.4807 |
| MIS1-8h_pump | LONG | 846 | -16.8396 | 2 | 17.682 | 167 | -18.3642 |
| QM_1H | LONG | 1506 | 0.3246 | 28 | 1.2683 | 115 | -0.5557 |
| QM_1H | SHORT | 1467 | -0.4033 | 33 | -1.1613 | 81 | -0.1112 |
| QM_4H | LONG | 145 | 0.7566 | 6 | 8.8389 | 18 | 2.3325 |
| QM_4H | SHORT | 377 | -1.1205 | 7 | -7.6624 | 20 | 0.0144 |
| ROM1 | LONG | 2713 | -0.0215 | 95 | 0.5025 | 181 | -0.1222 |
| ROM1 | SHORT | 3972 | 0.4251 | 156 | 1.6925 | 221 | 1.7431 |
| RUB1 | LONG | 1062 | 3.0311 | 55 | -1.6223 | 124 | -2.489 |
| RUB1 | SHORT | 1473 | 1.007 | 193 | -0.4029 | 173 | 3.6793 |
| SRA1 | LONG | 307 | 0.8119 | 3 | -0.3848 | 15 | -1.1294 |
| SRA1 | SHORT | 348 | -0.0487 | 9 | -0.7976 | 12 | -0.0967 |
| TD_1H | LONG | 1323 | 1.7491 | 19 | 2.1654 | 83 | 1.2879 |
| TD_1H | SHORT | 1062 | 0.3636 | 55 | 1.2132 | 32 | 2.4235 |
| TD_4H | LONG | 390 | 1.5723 | 10 | 1.4694 | 31 | -1.3335 |
| TD_4H | SHORT | 266 | 1.4966 | 8 | -5.0386 | 12 | 0.5307 |

## Caveats

- **Survivorship bias**: funding_rates covers active USDT-perps (530 symbols) vs 716 signal symbols; delisted coins partly missing → funded population skews to survivors.
- PnL is realized close-vs-entry net of round-trip taker fee (0.10%); it is the logged outcome, not a re-simulation. Many legacy rows carry fixed ±2.5% outcomes.
- Funding zoning uses fund_24h (the ABR gate quantity). Extreme zones use the ABR ±3bps cut; quintiles cover the whole fund_24h distribution incl. extremes.
- WR alone is not decisive (Rule 8). Verdict rests on net-PnL stable across the chrono val/test halves with n>=100 in each.
- **Effect is modest and ATTENUATING**: |Spearman| ≈ 0.06–0.12 in the val half but collapses toward zero in the test half (LONG +0.017, SHORT -0.018). The SIGN is consistent (ABR direction), the STRENGTH is weak and weakening recently. This confirms the ABR gate *direction* fleet-wide, but does not license a hard fleet-wide extreme-zone veto — the SHORT extreme-positive bin fails strict both-halves stability (test-half regime compression).
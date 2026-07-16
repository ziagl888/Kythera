# K3 · FRL — Funding-Risk-Layer study (T-2026-CU-9050-134)

_Generated 2026-07-16T07:18:20.286551+00:00 · read-only fleet analysis · fee/side 0.0005 (round-trip 0.0010)_

**VERDICT: direction-confirmed, magnitude-weak**

- fund_24h gradient sign-stable both directions & halves: **True**; magnitude-stable (|ρ|≥0.03 both halves): **False** (primary)
- hard extreme-zone claim stable in both halves: False (secondary)

## Primary test — fund_24h → PnL gradient (per-trade Spearman)

ABR predicts LONG corr>0 (higher funding → better LONG), SHORT corr<0.

| dir | expected | Spearman all | val | test | sign both | |ρ|≥floor both |
|---|---|--:|--:|--:|:--:|:--:|
| LONG | positive | 0.0592 | 0.0441 | 0.0173 | True | False |
| SHORT | negative | -0.0885 | -0.1208 | -0.0182 | True | False |

## Multi-feature gradient — full §K3 set incl. cross-section percentile

Same per-trade Spearman on fund_72h, fund_7d_cum and **cs_pctl** (the genuine ABR2 cross-section percentile: each trade's coin ranked vs ALL coins' as-of fund_24h at entry).

| feature | dir | Spearman all | val | test | sign both | |ρ|≥floor both |
|---|---|--:|--:|--:|:--:|:--:|
| fund_24h | LONG | 0.0592 | 0.0441 | 0.0173 | True | False |
| fund_24h | SHORT | -0.0885 | -0.1208 | -0.0182 | True | False |
| fund_72h | LONG | 0.0587 | 0.0469 | 0.0066 | True | False |
| fund_72h | SHORT | -0.0887 | -0.1267 | -0.0113 | True | False |
| fund_7d_cum | LONG | 0.0438 | 0.0208 | 0.0022 | True | False |
| fund_7d_cum | SHORT | -0.0704 | -0.0942 | -0.0052 | True | False |
| cs_pctl | LONG | 0.0207 | 0.0055 | 0.0397 | True | False |
| cs_pctl | SHORT | -0.0594 | -0.0568 | -0.0585 | True | True |

## Cross-section percentile (cs_pctl) — top vs bottom quintile expectancy

Bottom quintile = coin's funding low vs peers; top quintile = high vs peers. ABR expects LONG better / SHORT worse as cs_pctl rises. Means shown winsorized AND raw.

| dir | bucket | n | WR | avg net PnL % (wins) | avg net PnL % (raw) | median % | val raw% (n) | test raw% (n) |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| LONG | bottom Q1 | 9447 | 0.3518 | -2.021 | 0.4836 | -0.2138 | 0.1472 (5085) | 0.8758 (4362) |
| LONG | top Q5 | 9255 | 0.3741 | -2.1106 | -1.1488 | -0.1459 | -4.4873 (4680) | 2.2665 (4575) |
| SHORT | bottom Q1 | 7089 | 0.5046 | 4.4309 | 1.2038 | 0.3115 | 0.3977 (3434) | 1.9612 (3655) |
| SHORT | top Q5 | 7843 | 0.4557 | 3.7329 | 2.2254 | -0.1 | 3.7739 (3448) | 1.0106 (4395) |

## Population

- raw closed_ai_signals rows: 445,685
- deduped (symbol,model,direction,open_time): 88,202
- priced (entry>0 & close_price present): 88,202
- with as-of funding (fund_24h): 82,667
- with cross-section pctl (cs_pctl): 82,826
- open_time span (UTC): 2026-02-24 10:43:59.650539+00:00 .. 2026-07-16 06:26:13.088268+00:00
- median split (val|test): 2026-04-07 00:19:25.687913984+00:00
- fund_24h quintile edges (bps): [-0.615, 0.329, 0.5, 0.5]
- quintile degeneracy: edges_tie=True, collapsed=['Q4'] — fund_24h ties at the exchange default funding rate: the 60th and 80th pct edges coincide, so the interior quintile between them is empty (collapsed). The gradient/verdict do not depend on quintile bins (they use per-trade Spearman + the ±3bps extreme cuts); the collapsed bin is simply omitted from the zone table, documented here rather than silently dropped.
- winsor bounds for mean net-PnL (1/99 pct, %): [-55.666, 59.993] — WR, median & raw mean use raw values

## Hypothesis test (chrono val/test must agree; RAW means)

### SHORT@extreme-positive
- zone n (total): 1855
- zone RAW net-PnL/trade  val: -16.9842%  |  test: 2.4442%
- baseline RAW net-PnL/trade  val: 3.5656%  |  test: 0.7631%
- worse-than-baseline in BOTH halves: False (sufficient n both halves: True)

### LONG@extreme-negative
- zone n (total): 4036
- zone RAW net-PnL/trade  val: 2.9893%  |  test: 0.0098%
- baseline RAW net-PnL/trade  val: -2.5598%  |  test: 1.5708%
- worse-than-baseline in BOTH halves: False (sufficient n both halves: True)

## Direction × funding zone (fleet-wide, funded trades)

| dir | zone | n | WR | avg net PnL % (wins) | avg net PnL % (raw) | avg fund_24h bps | val raw% (n) | test raw% (n) |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| LONG | EXTREME_NEG(<-3bps) | 4036 | 0.3067 | -3.5899 | 1.9535 | -20.864 | 2.9893 (2633) | 0.0098 (1403) |
| LONG | Q1 | 5500 | 0.3765 | -3.3182 | -2.846 | -1.481 | -4.7028 (3916) | 1.7444 (1584) |
| LONG | Q2 | 9936 | 0.4095 | -1.841 | -1.7637 | -0.038 | -3.9089 (5987) | 1.4885 (3949) |
| LONG | Q3 | 18979 | 0.3836 | -0.8989 | -0.7556 | 0.485 | -3.3572 (8923) | 1.5528 (10056) |
| LONG | Q5 | 7387 | 0.3988 | 0.2208 | 0.4825 | 1.143 | -1.4005 (3294) | 1.9979 (4093) |
| LONG | EXTREME_POS(>+3bps) | 1469 | 0.3662 | 1.3239 | 4.288 | 6.469 | 9.0473 (444) | 2.2265 (1025) |
| SHORT | EXTREME_NEG(<-3bps) | 3323 | 0.5468 | 5.6629 | -1.1765 | -21.032 | -3.9612 (2060) | 3.3655 (1263) |
| SHORT | Q1 | 3675 | 0.4882 | 5.9483 | 5.541 | -1.481 | 8.4348 (2399) | 0.1005 (1276) |
| SHORT | Q2 | 6598 | 0.4657 | 3.5513 | 3.5048 | -0.029 | 6.5059 (3288) | 0.5237 (3310) |
| SHORT | Q3 | 14334 | 0.4505 | 2.5649 | 2.3533 | 0.486 | 5.1783 (5836) | 0.4133 (8498) |
| SHORT | Q5 | 5575 | 0.4287 | 1.2452 | 1.0196 | 1.223 | 1.9028 (1970) | 0.537 (3605) |
| SHORT | EXTREME_POS(>+3bps) | 1855 | 0.372 | 0.2894 | -3.6618 | 6.641 | -16.9842 (583) | 2.4442 (1272) |

## ABR2 gate generalization (fleet-wide, RAW means)

| test | n | WR | avg net PnL % (wins) | avg net PnL % (raw) |
|---|--:|--:|--:|--:|
| LONG in-gate (fund_24h>+3bps) | 1469 | 0.3662 | 1.3239 | 4.288 |
| LONG out-gate | 45838 | 0.3841 | -1.4499 | -0.7869 |
| SHORT in-veto (fund_24h>+1.5bps) | 3333 | 0.408 | 1.3799 | -0.9628 |
| SHORT out-veto | 32027 | 0.464 | 3.2396 | 2.3546 |

## Per-model extreme-zone effect (funded n>=300; RAW means; month-split in JSON)

| model | dir | base n | base raw PnL% | ext-pos n | ext-pos raw% | ext-neg n | ext-neg raw% |
|---|---|--:|--:|--:|--:|--:|--:|
| AIM1 | LONG | 878 | 1.2937 | 109 | 3.0407 | 150 | 2.4074 |
| AIM1 | SHORT | 2181 | -1.3082 | 429 | -3.1737 | 323 | 1.7166 |
| AIM2 | LONG | 831 | -0.0629 | 24 | 3.3751 | 47 | -4.9604 |
| AIM2 | SHORT | 490 | 1.29 | 29 | 3.5548 | 50 | 3.8401 |
| ATS1 | LONG | 3120 | -2.8749 | 125 | -0.5142 | 307 | 0.5646 |
| ATS1 | SHORT | 2266 | 4.7025 | 134 | 4.7707 | 269 | 2.1043 |
| ATS1_Robust | LONG | 476 | -30.9772 | 1 | 7.3849 | 58 | -10.9763 |
| ATS1_Robust | SHORT | 473 | 29.7512 | 1 | -11.3504 | 58 | 10.2578 |
| BB_1H | LONG | 1700 | 1.5357 | 35 | -0.7779 | 69 | 3.3648 |
| BB_1H | SHORT | 2339 | -1.4517 | 42 | -1.3369 | 128 | -0.1351 |
| BB_4H | LONG | 1126 | 1.454 | 11 | -2.0488 | 40 | 3.1575 |
| BB_4H | SHORT | 1343 | -0.8062 | 17 | 0.9369 | 73 | -0.5977 |
| BR1H | LONG | 3519 | 1.3647 | 79 | 2.6399 | 162 | 0.0352 |
| BR1H | SHORT | 3228 | -2.1919 | 67 | -3.2168 | 158 | -1.216 |
| BR1Hv2 | LONG | 333 | -0.5428 | 2 | 5.0827 | 6 | -0.5354 |
| BR1Hv2 | SHORT | 345 | -1.0831 | 4 | 2.6927 | 13 | 0.5485 |
| BR2H | LONG | 1932 | 0.8936 | 39 | 3.4396 | 92 | -0.6982 |
| BR2H | SHORT | 1903 | -1.6506 | 27 | -1.3714 | 83 | -1.5495 |
| BR4H | LONG | 859 | 1.3536 | 26 | 1.3963 | 37 | -2.4914 |
| BR4H | SHORT | 810 | -1.5216 | 10 | -2.6784 | 37 | -2.5696 |
| EPD1 | LONG | 1466 | 6.0156 | 142 | 25.007 | 422 | 31.7189 |
| EPD1 | SHORT | 5449 | 0.617 | 315 | -19.3668 | 492 | -28.9168 |
| EPD3 | LONG | 631 | 0.7817 | 37 | 0.0677 | 21 | -0.1036 |
| EPD3 | SHORT | 558 | -0.4655 | 28 | -3.0806 | 47 | 0.8971 |
| MIS1-168H | LONG | 7056 | 1.1538 | 162 | 3.5915 | 517 | 0.8487 |
| MIS1-168H | SHORT | 98 | 3.4767 | 36 | 6.88 | 27 | 3.2003 |
| MIS1-168h_dump | SHORT | 513 | 22.9737 | 1 | 37.9983 | 126 | 17.7277 |
| MIS1-168h_pump | LONG | 530 | -23.6314 | 1 | -20.7742 | 76 | -2.6934 |
| MIS1-24H | LONG | 191 | 5.5344 | 21 | 8.0741 | 33 | 4.6207 |
| MIS1-24H | SHORT | 204 | 1.5795 | 43 | -0.4888 | 60 | 5.0305 |
| MIS1-24h_dump | SHORT | 639 | 19.762 | 1 | 52.6061 | 167 | 12.7189 |
| MIS1-24h_pump | LONG | 488 | -20.8498 | 1 | -51.2384 | 135 | -17.7537 |
| MIS1-72H | LONG | 11561 | 1.56 | 336 | 2.4694 | 757 | 0.4779 |
| MIS1-72H | SHORT | 298 | 2.5331 | 65 | 1.9031 | 79 | 0.9129 |
| MIS1-72h_dump | SHORT | 608 | 21.0425 | 1 | 52.6061 | 148 | 11.8615 |
| MIS1-72h_pump | LONG | 633 | -25.7526 | 1 | -10.5334 | 135 | -17.9552 |
| MIS1-8H | LONG | 207 | -1.1916 | 29 | 0.1861 | 55 | -0.3775 |
| MIS1-8H | SHORT | 379 | 2.5023 | 65 | -0.669 | 85 | 5.5331 |
| MIS1-8h_dump | SHORT | 718 | 18.8573 | 2 | -115.2468 | 171 | 11.3265 |
| MIS1-8h_pump | LONG | 846 | -14.4783 | 2 | 129.1317 | 167 | -13.5222 |
| QM_1H | LONG | 1506 | 0.3246 | 28 | 1.2683 | 115 | -0.5557 |
| QM_1H | SHORT | 1467 | -0.4033 | 33 | -1.1613 | 81 | -0.1112 |
| QM_4H | LONG | 145 | 0.7566 | 6 | 8.8389 | 18 | 2.3325 |
| QM_4H | SHORT | 377 | -1.1205 | 7 | -7.6624 | 20 | 0.0144 |
| ROM1 | LONG | 2713 | 0.1213 | 95 | 3.0377 | 181 | 0.2499 |
| ROM1 | SHORT | 3972 | 0.4346 | 156 | 1.8432 | 221 | 1.7558 |
| RUB1 | LONG | 1062 | 3.3265 | 55 | -1.2219 | 124 | -2.4725 |
| RUB1 | SHORT | 1473 | 1.045 | 193 | -0.4225 | 173 | 4.112 |
| SRA1 | LONG | 307 | 0.8119 | 3 | -0.3848 | 15 | -1.1294 |
| SRA1 | SHORT | 348 | -0.0487 | 9 | -0.7976 | 12 | -0.0967 |
| TD_1H | LONG | 1323 | 1.8183 | 19 | 5.1244 | 83 | 1.2879 |
| TD_1H | SHORT | 1062 | 0.3636 | 55 | 1.2132 | 32 | 2.4235 |
| TD_4H | LONG | 390 | 1.5914 | 10 | 1.4694 | 31 | -1.3335 |
| TD_4H | SHORT | 266 | 1.5027 | 8 | -5.0386 | 12 | 0.6669 |

## Caveats

- **Survivorship bias**: funding_rates covers active USDT-perps (530 symbols) vs 716 signal symbols; delisted coins partly missing → funded population skews to survivors.
- PnL is realized close-vs-entry net of round-trip taker fee (0.10%); it is the logged outcome, not a re-simulation. Many legacy rows carry fixed ±2.5% outcomes.
- Means are shown BOTH winsorized (global 1/99 pct, tail-safe) AND raw (unclipped). The raw mean and median are the honest read on SHORT-squeeze tail losses — winsorizing attenuates exactly the tail the hypothesis is about.
- cs_pctl is a genuine cross-section rank (coin vs ALL peers' as-of fund_24h at entry), NOT the builder's per-symbol self-history fund_pctl_90d. Entry timestamps are hour-floored for the cross-section panel (funding steps on an ~8h grid → negligible as-of error).
- Autumn DST fall-back rows would map to NaT (ambiguous='NaT') and drop; the study window (Feb–Jul 2026) has no fall-back transition, so zero rows are affected here.
- WR alone is not decisive (Rule 8). Verdict rests on the fund_24h gradient being both sign- AND magnitude-stable (|ρ|≥0.03) across the chrono halves; sign-only yields 'direction-confirmed, magnitude-weak', not 'edge-found'.
- **Effect is modest and ATTENUATING**: |Spearman| ≈ 0.06–0.12 in the val half but collapses toward zero in the test half. The SIGN is consistent (ABR direction) across fund_24h/72h/7d_cum/cs_pctl, the STRENGTH is weak and weakening. This confirms the ABR gate *direction* fleet-wide, but does NOT license a hard fleet-wide extreme-zone veto.
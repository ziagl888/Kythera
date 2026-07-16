# K8 · SET — Settlement-/time-of-day expectancy study (T-2026-CU-9050-135)

_Generated 2026-07-16T07:55:24.983952+00:00 · read-only fleet analysis · fee/side 0.0005 (round-trip 0.0010)_

**VERDICT: timing-edge-found**

- stable prefer/avoid windows (both chrono halves, n≥300, |Δ|≥0.5pp): **34** (fleet-level: 18)
- **magnitude attenuates hard**: median |Δ| val 3.1837pp → test 1.0017pp (val/test ≈ 3.18×). The SIGN is stable across both halves; the STRENGTH is mostly in-sample and decays — low-conviction, not a hard gate.

## Population

- raw closed_ai_signals rows: 445,750
- deduped (symbol,model,direction,open_time): 88,267
- priced (entry>0 & close_price present): 88,267
- with valid UTC entry time (analysed): 88,267
- open_time span (UTC): 2026-02-24 10:43:59.650539+00:00 .. 2026-07-16 07:47:52.399249+00:00
- median split (val|test): 2026-04-06 01:35:40.378823936+00:00
- winsor bounds for mean net-PnL (1/99 pct, %): [-57.977, 59.636] — WR, median & raw mean use raw values

## Recommendation table — bot(model-tag) × window → avoid / prefer

Only windows whose winsorized net-PnL delta vs. the group×direction baseline is sign-stable across BOTH chrono halves with n≥100/half and |Δ|≥0.5pp. TAG:x = model-tag; map to bots via bot_catalog.

| group | dir | dimension | window | action | Δ val pp | Δ test pp | n val | n test |
|---|---|---|---|:--:|--:|--:|--:|--:|
| FLEET | LONG | settlement-offset(min) | [+090,+120) | prefer | 3.3418 | 1.1212 | 502 | 1061 |
| FLEET | LONG | settlement-offset(min) | [+150,+180) | avoid | -17.5968 | -0.6416 | 4689 | 909 |
| FLEET | LONG | settlement-offset(min) | [+210,+240) | prefer | 5.7068 | 0.5164 | 638 | 1369 |
| FLEET | LONG | settlement-offset(min) | [-150,-120) | prefer | 3.6706 | 1.4847 | 802 | 1470 |
| FLEET | LONG | settlement-offset(min) | [-210,-180) | prefer | 5.2383 | 0.7973 | 941 | 1106 |
| FLEET | SHORT | settlement-offset(min) | [+000,+030) | avoid | -5.7595 | -1.0001 | 1615 | 1896 |
| FLEET | SHORT | settlement-offset(min) | [+120,+150) | avoid | -4.0558 | -0.8713 | 1285 | 1421 |
| FLEET | SHORT | settlement-offset(min) | [-120,-090) | avoid | -4.9755 | -0.7269 | 1709 | 1458 |
| TAG:BR1H | SHORT | settlement-offset(min) | [+000,+030) | avoid | -0.5544 | -0.8505 | 230 | 164 |
| TAG:BR1H | SHORT | settlement-offset(min) | [+180,+210) | avoid | -0.6076 | -1.3147 | 297 | 136 |
| TAG:BR1H | SHORT | settlement-offset(min) | [-180,-150) | prefer | 0.6806 | 1.3979 | 265 | 128 |
| TAG:BR2H | LONG | settlement-offset(min) | [-240,-210) | prefer | 0.657 | 0.7501 | 259 | 224 |
| TAG:MIS1-72H | LONG | settlement-offset(min) | [+060,+090) | avoid | -1.1475 | -0.9604 | 286 | 362 |
| TAG:MIS1-72H | LONG | settlement-offset(min) | [+150,+180) | avoid | -0.9052 | -2.5031 | 382 | 222 |
| TAG:MIS1-72H | LONG | settlement-offset(min) | [+210,+240) | prefer | 1.0276 | 0.7968 | 425 | 649 |
| TAG:MIS1-72H | LONG | settlement-offset(min) | [-210,-180) | prefer | 0.8356 | 1.1059 | 470 | 472 |
| TAG:MIS1-72H | LONG | settlement-offset(min) | [-240,-210) | prefer | 1.3101 | 1.0247 | 642 | 138 |
| FLEET | LONG | entry-hour(UTC) | 17 | prefer | 2.2937 | 0.5417 | 759 | 720 |
| FLEET | LONG | entry-hour(UTC) | 19 | prefer | 3.6244 | 0.6797 | 881 | 1298 |
| FLEET | LONG | entry-hour(UTC) | 20 | prefer | 4.8589 | 1.051 | 1283 | 1070 |
| FLEET | LONG | entry-hour(UTC) | 21 | prefer | 2.2956 | 1.865 | 708 | 1301 |
| FLEET | LONG | entry-hour(UTC) | 23 | prefer | 4.0393 | 1.0138 | 771 | 815 |
| FLEET | SHORT | entry-hour(UTC) | 0 | avoid | -5.8122 | -1.0032 | 744 | 1109 |
| FLEET | SHORT | entry-hour(UTC) | 2 | avoid | -5.298 | -0.7382 | 511 | 843 |
| FLEET | SHORT | entry-hour(UTC) | 21 | avoid | -3.7953 | -0.8011 | 463 | 885 |
| FLEET | SHORT | entry-hour(UTC) | 3 | avoid | -6.8044 | -0.8005 | 408 | 717 |
| FLEET | SHORT | entry-hour(UTC) | 4 | avoid | -5.5818 | -1.03 | 631 | 958 |
| TAG:EPD1 | SHORT | entry-hour(UTC) | 10 | prefer | 17.9604 | 0.6248 | 555 | 118 |
| TAG:MIS1-168H | LONG | entry-hour(UTC) | 13 | prefer | 0.9409 | 1.0575 | 110 | 287 |
| TAG:MIS1-72H | LONG | entry-hour(UTC) | 10 | avoid | -3.0256 | -0.8522 | 163 | 174 |
| TAG:MIS1-72H | LONG | entry-hour(UTC) | 19 | prefer | 1.0653 | 2.0117 | 372 | 348 |
| TAG:MIS1-72H | LONG | entry-hour(UTC) | 20 | prefer | 1.4071 | 1.7545 | 549 | 192 |
| TAG:MIS1-72H | LONG | entry-hour(UTC) | 5 | avoid | -0.6428 | -1.526 | 704 | 153 |
| TAG:MIS1-72H | LONG | entry-hour(UTC) | 8 | avoid | -1.9557 | -1.4736 | 151 | 261 |

## Settlement-offset buckets — FLEET (30-min bins, offset to nearest 00/08/16 UTC)

Negative = entry BEFORE the settlement, positive = AFTER. Buckets with n≥300 shown.

| dir | bucket | n | WR | net PnL % (wins) | net PnL % (raw) | median % | boot CI95 raw% | val raw% (n) | test raw% (n) | class |
|---|---|--:|--:|--:|--:|--:|:--:|--:|--:|:--:|
| LONG | [-240,-210) | 4707 | 0.4209 | 0.7292 | 1.3786 | -0.1 | [0.902,1.8992] | 1.0726 (2624) | 1.764 (2083) | neutral |
| LONG | [-210,-180) | 2047 | 0.4372 | 1.7086 | 4.1014 | -0.1 | [2.81,5.4529] | 5.8749 (941) | 2.5925 (1106) | prefer |
| LONG | [-180,-150) | 3697 | 0.4047 | 0.1812 | 0.4942 | -0.1 | [0.0007,0.9596] | -0.6788 (2069) | 1.9849 (1628) | neutral |
| LONG | [-150,-120) | 2272 | 0.4379 | 1.692 | 3.0442 | -0.1 | [2.0907,4.1236] | 2.6856 (802) | 3.2399 (1470) | prefer |
| LONG | [-120,-090) | 3924 | 0.4011 | 0.4096 | 1.2182 | -0.1 | [0.5605,1.9045] | 1.1231 (2128) | 1.3308 (1796) | neutral |
| LONG | [-090,-060) | 2041 | 0.3479 | 0.379 | 1.0299 | -0.1276 | [0.2961,1.8424] | 1.5322 (626) | 0.8077 (1415) | neutral |
| LONG | [-060,-030) | 3448 | 0.4066 | 0.5361 | 0.6297 | -0.1 | [0.2196,1.0661] | 0.3292 (2042) | 1.0661 (1406) | neutral |
| LONG | [-030,+000) | 2061 | 0.3998 | 0.7538 | 1.2211 | -0.1 | [0.6097,1.8908] | 1.2205 (807) | 1.2215 (1254) | neutral |
| LONG | [+000,+030) | 4345 | 0.3807 | 0.3795 | 0.4278 | -0.119 | [0.1433,0.7504] | -0.0475 (2218) | 0.9234 (2127) | neutral |
| LONG | [+030,+060) | 1554 | 0.3526 | -0.1974 | 0.2276 | -0.1482 | [-0.4813,1.0005] | -0.6413 (585) | 0.7522 (969) | neutral |
| LONG | [+060,+090) | 3579 | 0.3677 | -0.1411 | -0.1319 | -0.1 | [-0.505,0.2174] | -1.2282 (1728) | 0.8916 (1851) | neutral |
| LONG | [+090,+120) | 1563 | 0.4056 | 1.4389 | 1.9483 | -0.1 | [1.1434,2.764] | -0.5395 (502) | 3.1254 (1061) | prefer |
| LONG | [+120,+150) | 4165 | 0.3906 | 0.1051 | 0.2644 | -0.1 | [-0.1142,0.6632] | -0.2913 (2507) | 1.1047 (1658) | neutral |
| LONG | [+150,+180) | 5598 | 0.2094 | -17.9783 | -15.4312 | -18.4078 | [-16.6986,-14.1596] | -18.5746 (4689) | 0.7842 (909) | avoid |
| LONG | [+180,+210) | 3722 | 0.3949 | 0.0321 | 0.2556 | -0.1 | [-0.1903,0.7137] | -0.4904 (1968) | 1.0926 (1754) | neutral |
| LONG | [+210,+240) | 2007 | 0.4305 | 1.7883 | 2.3675 | -0.1 | [1.7096,3.1111] | 2.7663 (638) | 2.1816 (1369) | prefer |
| SHORT | [-240,-210) | 3628 | 0.395 | 0.4775 | -0.3174 | -0.1 | [-0.9725,0.3019] | -1.0608 (1655) | 0.3061 (1973) | neutral |
| SHORT | [-210,-180) | 1594 | 0.4787 | 1.0886 | -3.4776 | -0.1 | [-5.7779,-1.3896] | -14.5132 (550) | 2.3361 (1044) | neutral |
| SHORT | [-180,-150) | 2428 | 0.4432 | 1.0165 | -0.7396 | -0.1 | [-1.9036,0.3744] | -2.4955 (1094) | 0.7003 (1334) | neutral |
| SHORT | [-150,-120) | 1330 | 0.4767 | 1.6564 | 1.0209 | -0.1 | [0.031,2.0373] | -0.833 (393) | 1.7984 (937) | neutral |
| SHORT | [-120,-090) | 3167 | 0.413 | 0.3286 | -0.487 | -0.1 | [-1.1749,0.1771] | -0.9331 (1709) | 0.0359 (1458) | avoid |
| SHORT | [-090,-060) | 1436 | 0.4429 | 1.6574 | 1.6099 | -0.1 | [0.8771,2.3197] | 2.1416 (434) | 1.3796 (1002) | neutral |
| SHORT | [-060,-030) | 2411 | 0.4268 | 1.167 | 1.0967 | -0.1 | [0.5429,1.6133] | 1.6454 (1110) | 0.6285 (1301) | neutral |
| SHORT | [-030,+000) | 1374 | 0.4454 | 0.6339 | 0.1637 | -0.1 | [-0.721,1.0271] | -4.0686 (326) | 1.4803 (1048) | neutral |
| SHORT | [+000,+030) | 3511 | 0.364 | -0.2239 | -0.2192 | -0.1194 | [-0.5618,0.1088] | -0.2166 (1615) | -0.2213 (1896) | avoid |
| SHORT | [+030,+060) | 1603 | 0.4504 | 1.021 | 0.6532 | -0.1 | [-0.1205,1.4373] | -1.2576 (457) | 1.4152 (1146) | neutral |
| SHORT | [+060,+090) | 2366 | 0.4121 | 0.8077 | 0.804 | -0.1 | [0.3155,1.2834] | 1.1274 (1040) | 0.5505 (1326) | neutral |
| SHORT | [+090,+120) | 1315 | 0.4418 | 1.0231 | 0.4187 | -0.1 | [-0.6319,1.478] | -2.0548 (385) | 1.4427 (930) | neutral |
| SHORT | [+120,+150) | 2706 | 0.4043 | 0.6534 | 0.609 | -0.1 | [0.1114,1.0852] | 1.3773 (1285) | -0.0858 (1421) | avoid |
| SHORT | [+150,+180) | 4911 | 0.7104 | 17.8071 | 14.7718 | 18.5269 | [13.4232,16.0941] | 18.9739 (3749) | 1.2143 (1162) | neutral |
| SHORT | [+180,+210) | 2414 | 0.4292 | 1.4307 | 0.801 | -0.1 | [0.0438,1.6101] | 0.9324 (1088) | 0.6932 (1326) | neutral |
| SHORT | [+210,+240) | 1343 | 0.4259 | -0.5552 | -1.3195 | -0.1 | [-2.3011,-0.2927] | -6.6643 (369) | 0.7053 (974) | neutral |

## Entry-hour buckets — FLEET (hour of day, UTC)

| dir | hour | n | WR | net PnL % (wins) | net PnL % (raw) | median % | boot CI95 raw% | val raw% (n) | test raw% (n) | class |
|---|---|--:|--:|--:|--:|--:|:--:|--:|--:|:--:|
| LONG | 0 | 2036 | 0.3964 | 0.6835 | 0.8904 | -0.1 | [0.3625,1.4496] | -0.0037 (880) | 1.5709 (1156) | neutral |
| LONG | 1 | 1789 | 0.365 | 0.2959 | 0.5046 | -0.1338 | [-0.072,1.1332] | -0.4181 (794) | 1.2408 (995) | neutral |
| LONG | 2 | 2074 | 0.3997 | 0.1317 | 0.3298 | -0.1 | [-0.1543,0.8803] | 0.1209 (1319) | 0.6948 (755) | neutral |
| LONG | 3 | 1925 | 0.4234 | 0.7923 | 0.9956 | -0.1 | [0.4519,1.5671] | 1.0677 (973) | 0.9218 (952) | neutral |
| LONG | 4 | 2290 | 0.4157 | 0.5224 | 0.5416 | -0.1 | [0.0751,0.9519] | -0.0668 (1261) | 1.2873 (1029) | neutral |
| LONG | 5 | 1970 | 0.4066 | -0.0266 | -0.0198 | -0.1 | [-0.4599,0.3897] | -0.4996 (1385) | 1.1162 (585) | neutral |
| LONG | 6 | 1814 | 0.4388 | 0.8522 | 0.9559 | -0.1 | [0.43,1.5216] | 0.6 (1084) | 1.4843 (730) | neutral |
| LONG | 7 | 2081 | 0.4104 | 0.4021 | 0.6053 | -0.1 | [0.0932,1.1646] | 0.9248 (1039) | 0.2867 (1042) | neutral |
| LONG | 8 | 1969 | 0.3718 | 0.5691 | 0.6006 | -0.1344 | [0.1433,1.0161] | 0.5323 (828) | 0.6502 (1141) | neutral |
| LONG | 9 | 1874 | 0.4093 | 0.6269 | 0.6591 | -0.1 | [0.1954,1.1783] | -1.13 (677) | 1.6709 (1197) | neutral |
| LONG | 10 | 6014 | 0.2195 | -16.8002 | -14.4541 | -14.4171 | [-15.5907,-13.2508] | -17.6662 (4983) | 1.0707 (1031) | neutral |
| LONG | 11 | 1625 | 0.3631 | -0.0427 | 0.1959 | -0.1 | [-0.4721,0.9328] | -0.9776 (752) | 1.2067 (873) | neutral |
| LONG | 12 | 2111 | 0.4173 | 0.9958 | 3.6064 | -0.1 | [2.1106,5.0092] | 5.2255 (1021) | 2.0899 (1090) | neutral |
| LONG | 13 | 1990 | 0.4101 | 0.8253 | 2.5774 | -0.1 | [1.3257,3.8672] | 3.2718 (778) | 2.1316 (1212) | neutral |
| LONG | 14 | 1899 | 0.3412 | -0.0333 | 2.0247 | -0.216 | [0.6855,3.5382] | 4.1782 (871) | 0.2 (1028) | neutral |
| LONG | 15 | 1842 | 0.3979 | 0.3474 | 0.654 | -0.1 | [-0.022,1.3156] | 0.5485 (1039) | 0.7904 (803) | neutral |
| LONG | 16 | 1894 | 0.3501 | -0.6177 | -0.4134 | -0.1994 | [-0.9939,0.2018] | -0.8384 (1095) | 0.1691 (799) | neutral |
| LONG | 17 | 1479 | 0.3584 | 0.0269 | 0.2944 | -0.1376 | [-0.4214,0.9887] | -1.7077 (759) | 2.405 (720) | prefer |
| LONG | 18 | 1675 | 0.3881 | 0.3335 | 0.5733 | -0.1 | [-0.0761,1.2297] | 0.0496 (894) | 1.1727 (781) | neutral |
| LONG | 19 | 2179 | 0.4263 | 1.0338 | 1.5916 | -0.1 | [0.9246,2.316] | 0.5631 (881) | 2.2897 (1298) | prefer |
| LONG | 20 | 2353 | 0.4433 | 1.5433 | 2.5631 | -0.1 | [1.8086,3.3116] | 2.4098 (1283) | 2.7469 (1070) | prefer |
| LONG | 21 | 2009 | 0.435 | 1.4556 | 1.8186 | -0.1 | [1.2183,2.528] | -1.5594 (708) | 3.6568 (1301) | prefer |
| LONG | 22 | 2252 | 0.373 | 0.3988 | 0.5788 | -0.1 | [0.0271,1.1548] | -1.1771 (799) | 1.5443 (1453) | neutral |
| LONG | 23 | 1586 | 0.4029 | 1.2142 | 1.4021 | -0.1 | [0.7466,2.0199] | 0.1639 (771) | 2.5734 (815) | prefer |
| SHORT | 0 | 1853 | 0.3648 | -0.2496 | -0.6064 | -0.1 | [-1.242,-0.0158] | -1.155 (744) | -0.2384 (1109) | avoid |
| SHORT | 1 | 1268 | 0.4322 | 0.8845 | 0.7755 | -0.1 | [0.0128,1.5494] | -0.7422 (473) | 1.6785 (795) | neutral |
| SHORT | 2 | 1354 | 0.3752 | 0.1096 | 0.108 | -0.1 | [-0.4905,0.7367] | 0.2158 (511) | 0.0426 (843) | avoid |
| SHORT | 3 | 1125 | 0.3787 | -0.4801 | -0.5361 | -0.1 | [-1.3295,0.3403] | -1.4667 (408) | -0.0066 (717) | avoid |
| SHORT | 4 | 1589 | 0.3612 | -0.1742 | -0.2102 | -0.1802 | [-0.9164,0.4092] | -0.181 (631) | -0.2294 (958) | avoid |
| SHORT | 5 | 980 | 0.4582 | 1.3089 | 1.2257 | -0.1 | [0.3589,2.2075] | 0.9782 (433) | 1.4215 (547) | neutral |
| SHORT | 6 | 1394 | 0.4362 | 0.6232 | 0.6232 | -0.1 | [0.021,1.2266] | 0.7613 (705) | 0.4819 (689) | neutral |
| SHORT | 7 | 1195 | 0.4234 | 0.3638 | -0.1541 | -0.1 | [-1.1413,0.7905] | -2.9776 (447) | 1.5333 (748) | neutral |
| SHORT | 8 | 1599 | 0.3827 | 0.1012 | 0.1478 | -0.1 | [-0.3541,0.6698] | -1.2872 (593) | 0.9936 (1006) | neutral |
| SHORT | 9 | 1165 | 0.4086 | 0.7276 | 0.7697 | -0.1 | [0.0028,1.5246] | 1.0289 (493) | 0.5795 (672) | neutral |
| SHORT | 10 | 4750 | 0.7105 | 18.6302 | 15.9381 | 20.6742 | [14.5664,17.2106] | 18.973 (3952) | 0.908 (798) | neutral |
| SHORT | 11 | 1206 | 0.432 | 2.1423 | 2.1744 | -0.1 | [1.3053,2.953] | 3.9629 (545) | 0.6997 (661) | neutral |
| SHORT | 12 | 1844 | 0.4507 | 1.5723 | -2.527 | -0.1 | [-4.5525,-0.6204] | -7.8869 (786) | 1.4549 (1058) | neutral |
| SHORT | 13 | 1430 | 0.4755 | 1.8265 | -0.9756 | -0.1 | [-2.8589,0.8421] | -5.5384 (591) | 2.2385 (839) | neutral |
| SHORT | 14 | 1729 | 0.4239 | 0.9342 | -0.1561 | -0.1 | [-1.3147,0.875] | -1.2608 (831) | 0.8662 (898) | neutral |
| SHORT | 15 | 1370 | 0.4803 | 1.497 | 1.4645 | -0.1 | [0.7001,2.1896] | 2.3655 (518) | 0.9167 (852) | neutral |
| SHORT | 16 | 1662 | 0.4284 | 0.6927 | 0.7009 | -0.1 | [0.1919,1.2496] | 0.9497 (735) | 0.5037 (927) | neutral |
| SHORT | 17 | 1248 | 0.4263 | 1.0313 | 0.4591 | -0.1 | [-0.5448,1.3858] | 0.4905 (459) | 0.4408 (789) | neutral |
| SHORT | 18 | 1513 | 0.4627 | 0.3812 | -1.0973 | -0.1 | [-2.4118,0.0067] | -3.8331 (571) | 0.5611 (942) | neutral |
| SHORT | 19 | 1426 | 0.4635 | 0.466 | -1.3027 | -0.1 | [-2.6833,0.0403] | -5.9645 (504) | 1.2456 (922) | neutral |
| SHORT | 20 | 1789 | 0.4421 | 0.4725 | -0.9509 | -0.1 | [-2.0334,-0.0006] | -4.3458 (788) | 1.7216 (1001) | neutral |
| SHORT | 21 | 1348 | 0.431 | 0.5761 | -0.1811 | -0.1 | [-1.2155,0.7695] | -0.4487 (463) | -0.0412 (885) | avoid |
| SHORT | 22 | 1480 | 0.4074 | 0.6328 | 0.1152 | -0.1 | [-0.6394,0.9057] | -0.2541 (607) | 0.372 (873) | neutral |
| SHORT | 23 | 1220 | 0.391 | 0.9828 | 0.858 | -0.1 | [0.1387,1.6233] | 1.286 (471) | 0.5889 (749) | neutral |

## Per model-tag breakdowns (n≥1500)

Tags with their own breakdown: TAG:AIM1, TAG:ATS1, TAG:BB_1H, TAG:BB_4H, TAG:BR1H, TAG:BR2H, TAG:BR4H, TAG:EPD1, TAG:MIS1-168H, TAG:MIS1-72H, TAG:QM_1H, TAG:ROM1, TAG:RUB1, TAG:TD_1H. Full per-bucket detail in the JSON.

- **TAG:AIM1**: 0 stable window(s) (none)
- **TAG:ATS1**: 0 stable window(s) (none)
- **TAG:BB_1H**: 0 stable window(s) (none)
- **TAG:BB_4H**: 0 stable window(s) (none)
- **TAG:BR1H**: 3 stable window(s) → ['[+000,+030)', '[+180,+210)', '[-180,-150)']
- **TAG:BR2H**: 1 stable window(s) → ['[-240,-210)']
- **TAG:BR4H**: 0 stable window(s) (none)
- **TAG:EPD1**: 1 stable window(s) → ['10']
- **TAG:MIS1-168H**: 1 stable window(s) → ['13']
- **TAG:MIS1-72H**: 10 stable window(s) → ['[+060,+090)', '[+150,+180)', '[+210,+240)', '[-210,-180)', '[-240,-210)', '10', '19', '20', '5', '8']
- **TAG:QM_1H**: 0 stable window(s) (none)
- **TAG:ROM1**: 0 stable window(s) (none)
- **TAG:RUB1**: 0 stable window(s) (none)
- **TAG:TD_1H**: 0 stable window(s) (none)

## Caveats

- **Selection / survivorship bias (Rule 9)**: the population is only trades the fleet actually OPENED and CLOSED, conditioned on the existing entry logic — including each bot's SCAN SCHEDULE, which clusters entries at specific minutes/hours. A time-of-day effect can be a scan-schedule confound (a window over/under-represented because a bot only runs then), not a genuine microstructure edge; the study says nothing about untaken windows.
- **TZ**: open_time is naive local Bucharest (P2.1–P2.6). Converted DST-aware to UTC before computing offsets/hours; a constant +3h would smear every offset by an hour on one side of the 2026-03-29 DST jump. Autumn fall-back rows would map to NaT and drop — none in this window.
- Means are shown BOTH winsorized (global 1/99 pct, tail-safe) AND raw (unclipped) with the median; window classification uses the winsorized mean so a single legacy ±tail row cannot flip a bucket. WR alone is not decisive (Rule 8).
- PnL is realized close-vs-entry net of round-trip taker fee (0.10%); it is the logged outcome, not a re-simulation. Many legacy rows carry fixed ±2.5% outcomes.
- Bootstrap CI = 1000-resample percentile CI of the raw-mean net-PnL per bucket (descriptive; no significance test). A window is only recommended on cross-half stability + a magnitude floor, never on a CI/p-value alone.
# K1 · TSM1 — Time-Series-Momentum on 6h aggregates (T-2026-CU-9050-138)

_Generated 2026-07-16T19:37:30.081800+00:00 · read-only replay · fee/side 0.0005 (round-trip 0.0010) · 527 coins · 1,178,990 events_

**VERDICT: no-op/paper-falsified**

- grid cells: 30 · val-positive (geometry-a): 3 · PASSING (val>0 AND test>0 at n_test≥200): **0**

- best cell selected on VAL: `4h|L12|k0.5` → val 0.128% (n=11171) · **test -0.0533% (n=32902, WR=0.6679)**


Stop-criterion (§K1): geometry-(a) is the deployable truth. No cell with BOTH val AND test positive avg net PnL at n_test≥200 ⇒ paper falsified for our stack (a NEGATIVE result is SUCCESS). Threshold picked on VAL, TEST read once. We do NOT chase the paper's monthly re-optimization — that is its overfitting vector.

## Geometry-(a) vs paper-(b) divergence — cost of the Cornix substitution

Across all 1,178,990 events, geometry-(a) avg net -0.1293%. The paper time-exit labels:

| paper H (bars) | paper avg net % | geo − paper (pp) | corr(geo,paper) |
|---|--:|--:|--:|
| 8 | -0.1952 | 0.0659 | 0.4029 |
| 16 | -0.3169 | 0.1875 | 0.3499 |
| 28 | -0.2747 | 0.1453 | 0.2308 |

_Divergence = our geometry (smart-targets + fixed SL, first-touch on 1h) vs the paper's time-exit + 15% catastrophe SL. A large gap or low correlation is the quantified cost of substituting the deployable Cornix geometry for the paper's exit._

## Full grid — geometry-(a) net PnL, chrono val/test split

| cell | all n | all avg% | all WR | val n | val avg% | test n | test avg% | test WR |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| 4h|L12|k0.0 | 71112 | -0.0356 | 0.6524 | 31454 | -0.0034 | 39658 | -0.0611 | 0.6614 |
| 4h|L12|k0.5 | 44073 | -0.0073 | 0.6584 | 11171 | 0.128 | 32902 | -0.0533 | 0.6679 |
| 4h|L12|k1.0 | 28673 | -0.1272 | 0.6456 | 6906 | -0.2333 | 21767 | -0.0936 | 0.6604 |
| 4h|L16|k0.0 | 66148 | -0.1262 | 0.6485 | 29296 | -0.1135 | 36852 | -0.1362 | 0.6588 |
| 4h|L16|k0.5 | 40601 | -0.1954 | 0.6453 | 10287 | -0.1813 | 30314 | -0.2002 | 0.6559 |
| 4h|L16|k1.0 | 26200 | -0.3117 | 0.631 | 6241 | -0.369 | 19959 | -0.2938 | 0.6453 |
| 4h|L24|k0.0 | 57702 | -0.1779 | 0.6495 | 25161 | -0.1743 | 32541 | -0.1807 | 0.6627 |
| 4h|L24|k0.5 | 34446 | -0.1184 | 0.6562 | 8713 | -0.147 | 25733 | -0.1087 | 0.6689 |
| 4h|L24|k1.0 | 22201 | -0.1728 | 0.6422 | 5326 | -0.4803 | 16875 | -0.0758 | 0.6624 |
| 4h|L32|k0.0 | 51926 | -0.1339 | 0.6595 | 22771 | -0.1988 | 29155 | -0.0832 | 0.6711 |
| 4h|L32|k0.5 | 30541 | -0.13 | 0.6584 | 7549 | -0.1376 | 22992 | -0.1275 | 0.6692 |
| 4h|L32|k1.0 | 19536 | -0.1106 | 0.6491 | 4706 | -0.0446 | 14830 | -0.1315 | 0.6615 |
| 4h|L8|k0.0 | 78680 | -0.0919 | 0.6423 | 34045 | -0.0528 | 44635 | -0.1217 | 0.6514 |
| 4h|L8|k0.5 | 50363 | -0.1174 | 0.6519 | 12309 | -0.1618 | 38054 | -0.1031 | 0.6632 |
| 4h|L8|k1.0 | 33158 | -0.1387 | 0.6423 | 7688 | -0.3299 | 25470 | -0.081 | 0.6603 |
| 6h|L12|k0.0 | 58525 | -0.2606 | 0.6441 | 25937 | -0.3227 | 32588 | -0.2111 | 0.6587 |
| 6h|L12|k0.5 | 35466 | -0.1804 | 0.6485 | 9311 | -0.2628 | 26155 | -0.151 | 0.6615 |
| 6h|L12|k1.0 | 22241 | -0.2283 | 0.6398 | 5546 | -0.3576 | 16695 | -0.1854 | 0.6547 |
| 6h|L16|k0.0 | 53521 | -0.2535 | 0.6487 | 23894 | -0.3586 | 29627 | -0.1688 | 0.6672 |
| 6h|L16|k0.5 | 31532 | -0.1643 | 0.657 | 8305 | -0.2547 | 23227 | -0.132 | 0.6705 |
| 6h|L16|k1.0 | 19865 | -0.1658 | 0.6449 | 5005 | -0.3643 | 14860 | -0.0989 | 0.6624 |
| 6h|L24|k0.0 | 45929 | -0.1134 | 0.6643 | 19949 | -0.1306 | 25980 | -0.1002 | 0.6737 |
| 6h|L24|k0.5 | 26713 | -0.1603 | 0.6565 | 6818 | -0.0864 | 19895 | -0.1857 | 0.6666 |
| 6h|L24|k1.0 | 16922 | -0.1309 | 0.6506 | 4238 | -0.1513 | 12684 | -0.1241 | 0.6641 |
| 6h|L32|k0.0 | 40624 | -0.0567 | 0.6723 | 17506 | 0.0098 | 23118 | -0.1072 | 0.6788 |
| 6h|L32|k0.5 | 23784 | -0.0517 | 0.6729 | 6219 | -0.0438 | 17565 | -0.0546 | 0.6855 |
| 6h|L32|k1.0 | 15374 | -0.1427 | 0.6547 | 4002 | -0.1955 | 11372 | -0.1242 | 0.6682 |
| 6h|L8|k0.0 | 66378 | -0.038 | 0.6522 | 29576 | -0.0927 | 36802 | 0.0059 | 0.6684 |
| 6h|L8|k0.5 | 40665 | -0.0271 | 0.6604 | 10466 | 0.0277 | 30199 | -0.046 | 0.672 |
| 6h|L8|k1.0 | 26091 | -0.1038 | 0.6508 | 6220 | -0.0375 | 19871 | -0.1246 | 0.6608 |

## Population & caveats

- run status: complete · coins done: 527
- coins replayed: 527 (of 527 in coins.json)
- total events (all cells, both aggregations): 1,178,990
- peak process RSS: 291.0 MB (streaming accumulators, memory O(cells), not O(events))
- 1h→6h resample anchored 00/06/12/18 UTC (origin=epoch), full 6-hour windows only
- native 4h grid = resample-artifact robustness check
- chrono val/test split epoch (UTC): 2026-01-13T07:30:00+00:00 — a FIXED calendar divider (midpoint of the BTCUSDT 1h window), not a per-cell median; val=earlier half, test=later half
- exact quantiles (median/p5/p95) omitted by design: they need all events (incompatible with the streaming O(cells) memory budget) and are not load-bearing for the §K1 stop-criterion (val+test positive AVG net at n≥200); n, WR and avg net are all EXACT from the accumulators
- geometry exit: first-touch TP-vs-SL on 1h candles, 3 published TPs, scan capped 60d
- paper exit: time-exit after H∈[8, 16, 28] aggregate bars, 15% catastrophe SL
- **Survivorship bias (Rule 9)**: coins.json lists ACTIVE USDT-perps; delisted coins are absent → the replayed population skews to survivors. Documented, not corrected.
- **Only closed candles (R1)**: read_candles(include_forming=False); a 6h window counts only when all 6 one-hour candles are present. σ and ROC are trailing/as-of (no lookahead).
- **WR is not decisive (Rule 8)**: the verdict rests on net-PnL expectancy consistent across the chrono val/test halves, geometry-(a) label only.
- CPU-check override: --skip-cpu-check=True (the VPS is CPU-saturated; the walkforward_sim guard would abort this read-only BELOW_NORMAL job).
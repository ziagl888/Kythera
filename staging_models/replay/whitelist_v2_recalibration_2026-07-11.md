# v2 whitelist: parameter sensitivity against realized ROM1 legs

- Window: `2026-07-11 00:00:00` → `jetzt`  (22.7 days)
- Gate events: 23492 (of which forwarded: 8607)
- ROM1 legs attached: 97.2%
- Generated: 2026-08-02 17:12:37.627172+00:00  |  CPU at start: 100.0

## Why this is NOT a backtest (measured, not asserted)

`bot_regime_performance` carries **0** cells with more than one row → a pure snapshot, not history. `last_computed` ranges from `2026-04-18 18:17:49.890235` to `2026-08-02 17:06:51.539034`.

The cell statistics the gate used to decide at the time of a past event no longer exist. Every re-decision below uses **today's** statistics on **that day's** traffic and thereby mixes two effects: what the parameters do, and how the cells have drifted since then. What remains reliable is the *shape* of the parameter response and the *sign* of the blocked legs. A flip cannot be justified from this — only a live shadow A/B (like T-031) can decide it.

## Reference: what v1 actually let through

- 8367 of 8607 forwarded events with a ROM1 leg  | Σ move 108.1 %  | Ø 0.0329 %/trade

## Parameter grid

| z | k | break-even | kept | pass-through | Ø kept % | Ø blocked % | Σ blocked % | reading |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1.64 **(heute)** | 25.0 | 0.1 | 583/8607 | 6.77 % | 0.2124 | 0.0126 | 37.2 | removes WINNER |
| 1.64 | 25.0 | 0.0 | 793/8607 | 9.21 % | 0.0928 | 0.0251 | 72.8 | removes WINNER |
| 1.64 | 25.0 | -0.1 | 1355/8607 | 15.74 % | -0.3504 | 0.1375 | 354.8 | removes WINNER |
| 1.64 | 10.0 | 0.1 | 573/8607 | 6.66 % | 0.1744 | 0.0167 | 49.2 | removes WINNER |
| 1.64 | 10.0 | 0.0 | 796/8607 | 9.25 % | 0.0967 | 0.0245 | 71.0 | removes WINNER |
| 1.64 | 10.0 | -0.1 | 1309/8607 | 15.21 % | -0.4473 | 0.1553 | 406.5 | removes WINNER |
| 1.64 | 5.0 | 0.1 | 574/8607 | 6.67 % | 0.1784 | 0.0162 | 47.7 | removes WINNER |
| 1.64 | 5.0 | 0.0 | 797/8607 | 9.26 % | 0.1004 | 0.024 | 69.5 | removes WINNER |
| 1.64 | 5.0 | -0.1 | 1311/8607 | 15.23 % | -0.4444 | 0.1548 | 405.0 | removes WINNER |
| 1.28 | 25.0 | 0.1 | 662/8607 | 7.69 % | 0.2931 | -0.0023 | -6.7 | removes loser |
| 1.28 | 25.0 | 0.0 | 882/8607 | 10.25 % | 0.2228 | 0.0028 | 7.9 | removes WINNER |
| 1.28 | 25.0 | -0.1 | 1478/8607 | 17.17 % | -0.2788 | 0.1225 | 312.5 | removes WINNER |
| 1.28 | 10.0 | 0.1 | 663/8607 | 7.7 % | 0.2592 | 0.0022 | 6.3 | removes WINNER |
| 1.28 | 10.0 | 0.0 | 887/8607 | 10.31 % | 0.2059 | 0.0052 | 14.7 | removes WINNER |
| 1.28 | 10.0 | -0.1 | 1479/8607 | 17.18 % | -0.2962 | 0.1276 | 325.5 | removes WINNER |
| 1.28 | 5.0 | 0.1 | 661/8607 | 7.68 % | 0.2841 | -0.001 | -2.9 | removes loser |
| 1.28 | 5.0 | 0.0 | 874/8607 | 10.15 % | 0.1985 | 0.0072 | 20.4 | removes WINNER |
| 1.28 | 5.0 | -0.1 | 1483/8607 | 17.23 % | -0.2925 | 0.127 | 323.7 | removes WINNER |
| 1.04 | 25.0 | 0.1 | 695/8607 | 8.07 % | 0.3588 | -0.0145 | -41.5 | removes loser |
| 1.04 | 25.0 | 0.0 | 1500/8607 | 17.43 % | -0.0642 | 0.0627 | 157.7 | removes WINNER |
| 1.04 | 25.0 | -0.1 | 2141/8607 | 24.88 % | -0.1063 | 0.0903 | 210.1 | removes WINNER |
| 1.04 | 10.0 | 0.1 | 706/8607 | 8.2 % | 0.3634 | -0.0157 | -44.9 | removes loser |
| 1.04 | 10.0 | 0.0 | 1419/8607 | 16.49 % | -0.1488 | 0.0833 | 214.2 | removes WINNER |
| 1.04 | 10.0 | -0.1 | 2114/8607 | 24.56 % | -0.14 | 0.1015 | 238.7 | removes WINNER |
| 1.04 | 5.0 | 0.1 | 711/8607 | 8.26 % | 0.3242 | -0.0101 | -29.0 | removes loser |
| 1.04 | 5.0 | 0.0 | 1422/8607 | 16.52 % | -0.1733 | 0.0904 | 232.2 | removes WINNER |
| 1.04 | 5.0 | -0.1 | 2072/8607 | 24.07 % | -0.1588 | 0.1072 | 253.8 | removes WINNER |
| 0.67 | 25.0 | 0.1 | 1184/8607 | 13.76 % | 0.4492 | -0.0521 | -142.0 | removes loser |
| 0.67 | 25.0 | 0.0 | 2016/8607 | 23.42 % | 0.0627 | 0.0212 | 50.0 | removes WINNER |
| 0.67 | 25.0 | -0.1 | 3488/8607 | 40.53 % | -0.1206 | 0.1674 | 293.2 | removes WINNER |
| 0.67 | 10.0 | 0.1 | 1195/8607 | 13.88 % | 0.5582 | -0.0764 | -207.8 | removes loser |
| 0.67 | 10.0 | 0.0 | 1994/8607 | 23.17 % | 0.1156 | 0.0013 | 3.1 | removes WINNER |
| 0.67 | 10.0 | -0.1 | 3489/8607 | 40.54 % | -0.1222 | 0.1694 | 296.0 | removes WINNER |
| 0.67 | 5.0 | 0.1 | 1202/8607 | 13.97 % | 0.5398 | -0.0737 | -200.1 | removes loser |
| 0.67 | 5.0 | 0.0 | 2002/8607 | 23.26 % | 0.1042 | 0.0053 | 12.6 | removes WINNER |
| 0.67 | 5.0 | -0.1 | 3433/8607 | 39.89 % | -0.1413 | 0.1808 | 321.3 | removes WINNER |
| 0.0 | 25.0 | 0.1 | 2197/8607 | 25.53 % | 0.396 | -0.1276 | -290.6 | removes loser |
| 0.0 | 25.0 | 0.0 | 3570/8607 | 41.48 % | -0.0834 | 0.1358 | 236.7 | removes WINNER |
| 0.0 | 25.0 | -0.1 | 4315/8607 | 50.13 % | -0.1457 | 0.2612 | 376.6 | removes WINNER |
| 0.0 | 10.0 | 0.1 | 2147/8607 | 24.94 % | 0.3555 | -0.106 | -243.5 | removes loser |
| 0.0 | 10.0 | 0.0 | 3525/8607 | 40.96 % | -0.1239 | 0.1693 | 297.4 | removes WINNER |
| 0.0 | 10.0 | -0.1 | 4319/8607 | 50.18 % | -0.1549 | 0.2735 | 393.9 | removes WINNER |
| 0.0 | 5.0 | 0.1 | 2156/8607 | 25.05 % | 0.3526 | -0.1066 | -243.7 | removes loser |
| 0.0 | 5.0 | 0.0 | 3553/8607 | 41.28 % | -0.1247 | 0.1748 | 302.2 | removes WINNER |
| 0.0 | 5.0 | -0.1 | 4282/8607 | 49.75 % | -0.1555 | 0.2734 | 394.6 | removes WINNER |

## Limits

- Snapshot instead of history: today's cell statistics on that day's traffic — parameter effect and cell drift are not separable (measured above).
- Only the forwarded side is scored. Suppressed signals have, by construction, no ROM1 leg — their outcome is not observed, not null.
- Events without an attached leg count as `n_no_leg` and are never counted as 0.
- No result from this run justifies a gate flip. The question only becomes decidable via a live shadow A/B or once bot_regime_performance is historized.

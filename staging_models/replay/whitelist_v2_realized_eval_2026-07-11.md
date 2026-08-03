# whitelist_v2 Flip — realized decision basis (T-2026-KYT-9050-007)

**Window:** 2026-07-11 00:00:00 → 2026-08-01 22:28:24.712790+00:00 (UTC)
**Snapshot:** 1590 cells, v2 coverage 100.0%, age 0.32h
(analyzer alive)

## 1. Cell divergence (today's snapshot)

| Class | Cells | Share |
|---|---:|---:|
| both_open | 94 | 5.9% |
| both_block | 98 | 6.2% |
| v2_would_block | 1395 | 87.7% |
| v2_would_open | 3 | 0.2% |
| v2_missing | 0 | 0.0% |

## 2. Actual gate traffic

- Events total: **22660**, of which cell-decided (flip-relevant): **14234**
- Gate rate open: v1 **36.28%** → v2 **4.07%**
- ROM1 forwards/day: v1 **377.04** → v2 (forecast) **168.08**
- v1 drift of the snapshot approximation: 9951/14234 = **69.91%** agreement

## 3. What the divergent signals REALIZED

### 3a. Trigger leg (source bot's own trade — symmetric, both sides)

| Class | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2_would_block | 4848 | 4533 | 0 | 4506 | 64.4 | -1275.3 | -0.383 | -3340.6 (4363) |
| v2_would_open | 264 | 225 | 0 | 225 | 84.9 | 523.2 | 2.225 | 7827.3 (192) |
| both_open | 316 | 305 | 0 | 304 | 75.3 | 305.9 | 0.906 | 8077.5 (273) |
| both_block | 8806 | 8039 | 1 | 8033 | 67.0 | -562.5 | -0.170 | 11776.9 (7923) |
| unaffected | 8426 | 7749 | 0 | 7737 | 66.9 | -853.9 | -0.210 | 13771.9 (7708) |

**Flip balance on the trigger leg:** v2 removes Σ -1275.3% (4506 decided trades), v2 unblocks Σ 523.2% (225) → **Δ 1798.5%** (unlevered move).

### 3b. ROM1 leg (the real money — exists only on the forwarded side)

| Class | Events | with ROM1 leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2_would_block | 4848 | 4736 | 2925 | 1811 | 81.6 | 66.0 | -0.064 | 17449.2 (1613) |
| both_open | 316 | 313 | 99 | 214 | 83.6 | 33.7 | 0.058 | 1787.0 (177) |
| unaffected | 8426 | 2990 | 1881 | 1109 | 81.0 | 66.6 | -0.040 | 13991.7 (1088) |

> `v2_would_open` structurally has NO ROM1 leg: these signals were never forwarded, hence never traded as ROM1. The additionally unblocked side is fundamentally not measurable in ROM1 money — only in the trigger leg (3a), and that carries a different geometry (P1.10).

## 3c. Clean vs. drift-contaminated (the reliable subset)

The flip class compares the RECORDED v1 decision with TODAY'S v2 cell. Where today's v1 cell no longer matches the recorded decision, the cell has since moved — then the class compares two different cell states, not v1 against v2. Only `v1_agree` is a clean v1-vs-v2 reading.

| Class | Subset | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2_would_block | v1_agree | 3461 | 3180 | 0 | 3160 | 66.6 | -274.9 | -0.187 | 8618.4 (3051) |
| v2_would_block | v1_drifted | 1387 | 1353 | 0 | 1346 | 59.3 | -1000.4 | -0.843 | -11959.0 (1312) |
| v2_would_open | v1_agree | 124 | 88 | 0 | 88 | 86.4 | 130.8 | 1.386 | 2924.2 (88) |
| v2_would_open | v1_drifted | 140 | 137 | 0 | 137 | 83.9 | 392.4 | 2.764 | 4903.1 (104) |

## 4. Which v1 path did the divergent traffic come through?

`insufficient_data` is v1's default-open crutch (n < 30 in the cell), `wr_above_overall` / `counter_trend_specialist` are v1 decisions ON MERIT. The cell matrix and the traffic answer this differently.

### v2_would_block — trigger leg by v1 path

| v1 path | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| wr_above_overall | 3964 | 3704 | 0 | 3704 | 66.1 | -822.3 | -0.322 | -5669.1 (3649) |
| insufficient_data | 880 | 825 | 0 | 798 | 56.5 | -456.8 | -0.672 | 2258.5 (710) |
| counter_trend_specialist | 4 | 4 | 0 | 4 | 75.0 | 3.8 | 0.839 | 70.0 (4) |

### v2_would_open — trigger leg by v1 path

| v1 path | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| wr_below_overall | 229 | 197 | 0 | 197 | 85.3 | 448.9 | 2.179 | 6314.1 (164) |
| counter_trend_insufficient | 35 | 28 | 0 | 28 | 82.1 | 74.3 | 2.554 | 1513.2 (28) |

## 5. Breakdown by bot × direction

### v2_would_block — trigger leg

| Bot | Dir | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| VolIndic | LONG | 673 | 658 | 0 | 658 | 60.2 | -570.0 | -0.966 | -11078.7 (658) |
| MIS1-72h | LONG | 222 | 193 | 0 | 193 | 47.7 | -226.3 | -1.273 | -2031.4 (193) |
| BR2H | LONG | 198 | 179 | 0 | 179 | 47.5 | -165.2 | -1.023 | -1698.7 (156) |
| EPD3 | SHORT | 186 | 186 | 0 | 186 | 84.9 | 137.8 | 0.641 | 3815.4 (186) |
| ATS2 | LONG | 165 | 134 | 0 | 134 | 68.7 | -126.3 | -1.042 | -999.8 (134) |
| RUB2 | LONG | 26 | 25 | 0 | 25 | 24.0 | -106.3 | -4.353 | -695.8 (19) |
| BR4H | LONG | 63 | 60 | 0 | 60 | 40.0 | -92.9 | -1.649 | -970.1 (48) |
| EPD3 | LONG | 497 | 413 | 0 | 413 | 76.3 | -86.6 | -0.310 | 1997.9 (413) |
| MIS1-168h | LONG | 47 | 28 | 0 | 28 | 78.6 | 63.7 | 2.176 | 1169.7 (28) |
| FastInOut | LONG | 359 | 359 | 0 | 359 | 60.2 | -61.9 | -0.272 | -1419.6 (359) |
| RUB1 | SHORT | 23 | 22 | 0 | 22 | 86.4 | 53.5 | 2.334 | 946.3 (22) |
| BB2_4H | LONG | 17 | 17 | 0 | 17 | 29.4 | -50.4 | -3.064 | -306.0 (10) |
| MIS2-72h | SHORT | 28 | 28 | 0 | 17 | 29.4 | -48.8 | -2.968 | 323.8 (17) |
| SRA2 | LONG | 103 | 91 | 0 | 91 | 68.1 | -47.9 | -0.626 | 233.6 (91) |
| RUB1 | LONG | 9 | 7 | 0 | 7 | 14.3 | -43.7 | -6.341 | -536.3 (7) |
| MIS2-8h | LONG | 27 | 27 | 0 | 27 | 48.1 | 42.8 | 1.484 | 1084.1 (23) |
| RUB2 | SHORT | 36 | 35 | 0 | 35 | 91.4 | 39.4 | 1.025 | 912.1 (35) |
| MIS2-24h | LONG | 21 | 21 | 0 | 21 | 57.1 | -38.9 | -1.950 | 175.6 (19) |
| BR2H | SHORT | 144 | 143 | 0 | 143 | 61.5 | 38.8 | 0.172 | 1087.0 (126) |
| BR4H | SHORT | 44 | 42 | 0 | 42 | 52.4 | -35.4 | -0.942 | -675.7 (41) |
| SR | LONG | 272 | 272 | 0 | 272 | 67.3 | -33.2 | -0.222 | -453.0 (272) |
| MIS2-168h | LONG | 22 | 22 | 0 | 22 | 59.1 | 29.2 | 1.227 | 1270.1 (22) |
| AIM2 | LONG | 52 | 45 | 0 | 45 | 71.1 | 27.8 | 0.517 | 751.3 (34) |
| TD_1H | LONG | 33 | 27 | 0 | 27 | 66.7 | 25.9 | 0.860 | 722.7 (26) |
| MIS2-24h | SHORT | 13 | 13 | 0 | 6 | 66.7 | 25.1 | 4.090 | 85.7 (4) |
| ATS1 | SHORT | 33 | 33 | 0 | 33 | 87.9 | 21.9 | 0.565 | -6.3 (11) |
| 5Percent | SHORT | 36 | 35 | 0 | 35 | 71.4 | -20.8 | -0.694 | -378.5 (35) |
| VolIndic | SHORT | 846 | 774 | 0 | 774 | 67.2 | -20.6 | -0.127 | -345.0 (774) |
| MIS1-8h | SHORT | 13 | 13 | 0 | 13 | 53.8 | 19.7 | 1.412 | 1187.3 (13) |
| BR1D | LONG | 5 | 5 | 0 | 5 | 40.0 | -15.7 | -3.237 | -287.7 (5) |
| BR1D | SHORT | 20 | 20 | 0 | 20 | 55.0 | -13.7 | -0.785 | -227.8 (19) |
| TD_4H | SHORT | 9 | 8 | 0 | 8 | 62.5 | -13.5 | -1.789 | -12.8 (8) |
| BB_4H | SHORT | 51 | 51 | 0 | 51 | 70.6 | 13.0 | 0.155 | 315.1 (51) |
| SR | SHORT | 132 | 131 | 0 | 131 | 71.0 | 12.5 | -0.004 | 383.0 (131) |
| 5Percent | LONG | 29 | 29 | 0 | 29 | 75.9 | -11.6 | -0.499 | -196.9 (29) |
| BB_4H | LONG | 68 | 68 | 0 | 68 | 61.8 | 11.0 | 0.062 | 591.1 (68) |
| MIS2-168h | SHORT | 10 | 10 | 0 | 9 | 33.3 | -10.0 | -1.211 | 533.3 (8) |
| SRA1 | SHORT | 4 | 4 | 0 | 4 | 100.0 | 9.9 | 2.365 | 197.2 (4) |
| MIS2-8h | SHORT | 15 | 15 | 0 | 7 | 42.9 | 8.6 | 1.125 | — (0) |
| QM_1H | SHORT | 5 | 5 | 0 | 5 | 100.0 | 8.0 | 1.491 | -2.0 (1) |
| EPD2 | SHORT | 1 | 1 | 0 | 1 | 0.0 | -7.1 | -7.209 | -71.1 (1) |
| ATS2 | SHORT | 1 | 1 | 0 | 1 | 100.0 | 6.9 | 6.798 | 138.0 (1) |
| ABR2 | LONG | 44 | 44 | 0 | 44 | 63.6 | 6.8 | 0.054 | 586.6 (37) |
| FastInOut | SHORT | 134 | 134 | 0 | 134 | 64.9 | -5.9 | -0.144 | -216.1 (134) |
| TD_4H | LONG | 13 | 12 | 0 | 12 | 58.3 | -5.9 | -0.592 | -33.2 (12) |
| BB2_4H | SHORT | 25 | 25 | 0 | 25 | 48.0 | -5.2 | -0.308 | -96.8 (19) |
| Main Channel | SHORT | 7 | 7 | 0 | 7 | 57.1 | -5.0 | -0.814 | -100.0 (7) |
| Main Channel | LONG | 10 | 10 | 0 | 10 | 70.0 | -5.0 | -0.598 | -99.5 (10) |
| TD2_4H | SHORT | 1 | 1 | 0 | 1 | 100.0 | 3.1 | 3.021 | 62.4 (1) |
| TD2_4H | LONG | 1 | 1 | 0 | 1 | 0.0 | -2.9 | -3.014 | — (0) |
| EPD2 | LONG | 3 | 3 | 0 | 3 | 66.7 | -1.9 | -0.750 | 7.3 (1) |
| SRA1 | LONG | 6 | 6 | 0 | 6 | 66.7 | -1.8 | -0.408 | 90.8 (6) |
| MIS2-72h | LONG | 12 | 12 | 0 | 12 | 50.0 | -1.4 | -0.215 | 684.8 (11) |
| SRA2 | SHORT | 2 | 2 | 0 | 2 | 100.0 | 1.3 | 0.533 | 25.3 (2) |
| MIS1-24h | LONG | 2 | 2 | 0 | 2 | 50.0 | -1.1 | -0.631 | 12.1 (2) |
| TD_1H | SHORT | 3 | 1 | 0 | 1 | 100.0 | 0.8 | 0.737 | 8.4 (1) |
| ABR2 | SHORT | 22 | 21 | 0 | 21 | 71.4 | 0.2 | -0.092 | 204.0 (16) |
| QM_1H | LONG | 2 | 2 | 0 | 2 | 50.0 | -0.2 | -0.195 | -3.8 (2) |
| UFI1 | SHORT | 2 | 0 | 0 | 0 | — | — | — | — (0) |
| ATB2 | LONG | 1 | 0 | 0 | 0 | — | — | — | — (0) |

### v2_would_open — trigger leg

| Bot | Dir | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| AIM2 | SHORT | 173 | 164 | 0 | 164 | 84.1 | 473.8 | 2.789 | 6516.3 (131) |
| SRA2 | SHORT | 91 | 61 | 0 | 61 | 86.9 | 49.3 | 0.709 | 1311.0 (61) |

## 6. Measurement limits (measured, not assumed)

- IN-SAMPLE on the trigger leg: `27_bot_regime_analyzer` builds `bot_regime_performance` from exactly these closed trigger trades of the last 30 days (from 2026-07-02 22:28:24.712741), and v2 decides a cell purely from their avg_pnl/stddev. That v2 blocks cells here whose trigger trades realized negatively is therefore, to a large extent, a restatement of v2's fitting criterion — NOT independent evidence. Independent are (a) the ROM1 leg, on which v2 was not fitted, and (b) a run with `--until` before 2026-07-02.
- Snapshot approximation: `bot_regime_whitelist` is UPSERT-only with no history — the per-event v2 verdict comes from today's snapshot (2026-08-01 22:08:40.703564), not from the state at signal time. The v1 drift (69.91% agreement over 14234 events) measures this error on the only axis where both states are known.
- The historical whitelist is therefore still NOT reconstructable (confirms the T-031 finding): neither `bot_regime_whitelist` nor `bot_regime_performance` keep a history, and bot 28 logs only the v1 path per signal, never the v2 verdict.
- `v2_would_open` has no ROM1 leg — these signals were never traded. The unblocked side is only measurable via the source bot's trade, which carries a DIFFERENT geometry than ROM1 (docs/REGIME_ORCHESTRATOR.md, P1.10).
- Trigger-leg coverage < 100%: unmatched events are counted as `no_twin`, not scored as 0. Causes: signal still open, trade never opened, monitor gap.
- WR is TP1 touch, PnL is the target-staggered unlevered move (core.realized_pnl, T-115 definition). `lev` PnL is exact-only — coverage per row readable via `n_with_leg`.

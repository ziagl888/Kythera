# whitelist_v2 Flip — realized decision basis (T-2026-KYT-9050-007)

**Window:** 2026-07-25 00:00:00 → 2026-08-01 22:28:59.292429+00:00 (UTC)
**Snapshot:** 1590 cells, v2 coverage 100.0%, age 0.34h
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

- Events total: **9404**, of which cell-decided (flip-relevant): **5938**
- Gate rate open: v1 **35.97%** → v2 **2.53%**
- ROM1 forwards/day: v1 **479.03** → v2 (forecast) **228.81**
- v1 drift of the snapshot approximation: 5094/5938 = **85.79%** agreement

## 3. What the divergent signals REALIZED

### 3a. Trigger leg (source bot's own trade — symmetric, both sides)

| Class | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2_would_block | 2089 | 1787 | 0 | 1786 | 66.1 | -363.7 | -0.304 | 2442.1 (1786) |
| v2_would_open | 103 | 68 | 0 | 68 | 86.8 | 69.5 | 0.922 | 1697.6 (68) |
| both_open | 47 | 40 | 0 | 39 | 74.4 | 46.7 | 1.098 | 1639.9 (39) |
| both_block | 3699 | 2949 | 0 | 2944 | 65.3 | -446.1 | -0.252 | -2382.2 (2944) |
| unaffected | 3466 | 2817 | 0 | 2816 | 66.9 | -349.4 | -0.224 | 3452.9 (2816) |

**Flip balance on the trigger leg:** v2 removes Σ -363.7% (1786 decided trades), v2 unblocks Σ 69.5% (68) → **Δ 433.2%** (unlevered move).

### 3b. ROM1 leg (the real money — exists only on the forwarded side)

| Class | Events | with ROM1 leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2_would_block | 2089 | 1977 | 1120 | 857 | 81.7 | -78.9 | -0.192 | 7965.8 (857) |
| both_open | 47 | 44 | 26 | 18 | 55.6 | -45.1 | -2.604 | -458.3 (18) |
| unaffected | 3466 | 1549 | 1104 | 445 | 82.7 | -7.4 | -0.117 | 5281.2 (445) |

> `v2_would_open` structurally has NO ROM1 leg: these signals were never forwarded, hence never traded as ROM1. The additionally unblocked side is fundamentally not measurable in ROM1 money — only in the trigger leg (3a), and that carries a different geometry (P1.10).

## 3c. Clean vs. drift-contaminated (the reliable subset)

The flip class compares the RECORDED v1 decision with TODAY'S v2 cell. Where today's v1 cell no longer matches the recorded decision, the cell has since moved — then the class compares two different cell states, not v1 against v2. Only `v1_agree` is a clean v1-vs-v2 reading.

| Class | Subset | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2_would_block | v1_agree | 1813 | 1538 | 0 | 1537 | 67.7 | -158.2 | -0.203 | 4526.3 (1537) |
| v2_would_block | v1_drifted | 276 | 249 | 0 | 249 | 55.8 | -205.6 | -0.926 | -2084.2 (249) |
| v2_would_open | v1_agree | 101 | 67 | 0 | 67 | 88.1 | 76.7 | 1.044 | 1797.6 (67) |
| v2_would_open | v1_drifted | 2 | 1 | 0 | 1 | 0.0 | -7.1 | -7.245 | -100.0 (1) |

## 4. Which v1 path did the divergent traffic come through?

`insufficient_data` is v1's default-open crutch (n < 30 in the cell), `wr_above_overall` / `counter_trend_specialist` are v1 decisions ON MERIT. The cell matrix and the traffic answer this differently.

### v2_would_block — trigger leg by v1 path

| v1 path | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| wr_above_overall | 1864 | 1611 | 0 | 1611 | 67.1 | -291.5 | -0.281 | 1194.1 (1611) |
| insufficient_data | 225 | 176 | 0 | 175 | 56.6 | -72.3 | -0.513 | 1248.1 (175) |

### v2_would_open — trigger leg by v1 path

| v1 path | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| wr_below_overall | 91 | 61 | 0 | 61 | 86.9 | 49.3 | 0.709 | 1311.0 (61) |
| counter_trend_insufficient | 12 | 7 | 0 | 7 | 85.7 | 20.2 | 2.784 | 386.6 (7) |

## 5. Breakdown by bot × direction

### v2_would_block — trigger leg

| Bot | Dir | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| MIS1-72h | LONG | 147 | 122 | 0 | 122 | 44.3 | -184.9 | -1.616 | -1688.8 (122) |
| ATS2 | LONG | 165 | 134 | 0 | 134 | 68.7 | -126.3 | -1.042 | -999.8 (134) |
| EPD3 | LONG | 497 | 413 | 0 | 413 | 76.3 | -86.6 | -0.310 | 1997.9 (413) |
| MIS1-168h | LONG | 47 | 28 | 0 | 28 | 78.6 | 63.7 | 2.176 | 1169.7 (28) |
| BR2H | LONG | 71 | 52 | 0 | 52 | 46.2 | -61.1 | -1.275 | -856.4 (52) |
| RUB1 | SHORT | 23 | 22 | 0 | 22 | 86.4 | 53.5 | 2.334 | 946.3 (22) |
| RUB1 | LONG | 9 | 7 | 0 | 7 | 14.3 | -43.7 | -6.341 | -536.3 (7) |
| AIM2 | LONG | 24 | 17 | 0 | 17 | 76.5 | 38.6 | 2.173 | 690.6 (17) |
| FastInOut | LONG | 172 | 172 | 0 | 172 | 58.7 | -34.7 | -0.302 | -753.4 (172) |
| VolIndic | SHORT | 471 | 399 | 0 | 399 | 65.2 | 32.5 | -0.018 | 843.8 (399) |
| SR | LONG | 46 | 46 | 0 | 46 | 63.0 | -31.2 | -0.779 | -598.8 (46) |
| 5Percent | SHORT | 24 | 23 | 0 | 23 | 69.6 | -18.6 | -0.911 | -319.9 (23) |
| MIS1-8h | SHORT | 12 | 12 | 0 | 12 | 50.0 | 17.7 | 1.374 | 1147.9 (12) |
| VolIndic | LONG | 110 | 97 | 0 | 97 | 63.9 | -17.5 | -0.280 | -406.3 (97) |
| SRA2 | LONG | 67 | 55 | 0 | 55 | 74.5 | 17.2 | 0.213 | 806.7 (55) |
| TD_1H | LONG | 23 | 17 | 0 | 17 | 64.7 | 12.6 | 0.644 | 324.1 (17) |
| MIS1-24h | LONG | 1 | 1 | 0 | 1 | 0.0 | -12.3 | -12.374 | -100.0 (1) |
| ATS2 | SHORT | 1 | 1 | 0 | 1 | 100.0 | 6.9 | 6.798 | 138.0 (1) |
| SR | SHORT | 52 | 51 | 0 | 51 | 68.6 | 5.3 | 0.003 | 176.3 (51) |
| BB_4H | LONG | 1 | 1 | 0 | 1 | 100.0 | 5.3 | 5.176 | 105.5 (1) |
| TD_4H | LONG | 3 | 2 | 0 | 2 | 100.0 | 5.2 | 2.478 | 103.1 (2) |
| MIS2-72h | SHORT | 4 | 4 | 0 | 3 | 33.3 | -5.0 | -1.751 | -9.5 (3) |
| BR4H | LONG | 6 | 3 | 0 | 3 | 66.7 | 4.6 | 1.418 | 56.7 (3) |
| TD_4H | SHORT | 3 | 2 | 0 | 2 | 50.0 | -4.4 | -2.285 | 21.4 (2) |
| 5Percent | LONG | 12 | 12 | 0 | 12 | 83.3 | 3.8 | 0.212 | 90.6 (12) |
| MIS2-168h | SHORT | 3 | 3 | 0 | 3 | 33.3 | -3.3 | -1.211 | 213.3 (3) |
| MIS2-24h | SHORT | 2 | 2 | 0 | 2 | 50.0 | -1.7 | -0.957 | 42.9 (2) |
| SRA2 | SHORT | 2 | 2 | 0 | 2 | 100.0 | 1.3 | 0.533 | 25.3 (2) |
| FastInOut | SHORT | 85 | 85 | 0 | 85 | 65.9 | -0.3 | -0.104 | -185.2 (85) |
| QM_1H | LONG | 2 | 2 | 0 | 2 | 50.0 | -0.2 | -0.195 | -3.8 (2) |
| UFI1 | SHORT | 1 | 0 | 0 | 0 | — | — | — | — (0) |
| ATB2 | LONG | 1 | 0 | 0 | 0 | — | — | — | — (0) |
| TD_1H | SHORT | 2 | 0 | 0 | 0 | — | — | — | — (0) |

### v2_would_open — trigger leg

| Bot | Dir | Events | with leg | censored | decided | WR% | Σ Move% | Ø net% | Σ lev% (n) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| SRA2 | SHORT | 91 | 61 | 0 | 61 | 86.9 | 49.3 | 0.709 | 1311.0 (61) |
| AIM2 | SHORT | 12 | 7 | 0 | 7 | 85.7 | 20.2 | 2.784 | 386.6 (7) |

## 6. Measurement limits (measured, not assumed)

- IN-SAMPLE on the trigger leg: `27_bot_regime_analyzer` builds `bot_regime_performance` from exactly these closed trigger trades of the last 30 days (from 2026-07-02 22:28:59.292389), and v2 decides a cell purely from their avg_pnl/stddev. That v2 blocks cells here whose trigger trades realized negatively is therefore, to a large extent, a restatement of v2's fitting criterion — NOT independent evidence. Independent are (a) the ROM1 leg, on which v2 was not fitted, and (b) a run with `--until` before 2026-07-02.
- Snapshot approximation: `bot_regime_whitelist` is UPSERT-only with no history — the per-event v2 verdict comes from today's snapshot (2026-08-01 22:08:40.703564), not from the state at signal time. The v1 drift (85.79% agreement over 5938 events) measures this error on the only axis where both states are known.
- The historical whitelist is therefore still NOT reconstructable (confirms the T-031 finding): neither `bot_regime_whitelist` nor `bot_regime_performance` keep a history, and bot 28 logs only the v1 path per signal, never the v2 verdict.
- `v2_would_open` has no ROM1 leg — these signals were never traded. The unblocked side is only measurable via the source bot's trade, which carries a DIFFERENT geometry than ROM1 (docs/REGIME_ORCHESTRATOR.md, P1.10).
- Trigger-leg coverage < 100%: unmatched events are counted as `no_twin`, not scored as 0. Causes: signal still open, trade never opened, monitor gap.
- WR is TP1 touch, PnL is the target-staggered unlevered move (core.realized_pnl, T-115 definition). `lev` PnL is exact-only — coverage per row readable via `n_with_leg`.

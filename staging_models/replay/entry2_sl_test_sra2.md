# entry2-as-SL vs DCA — 3-arm test (SRA2) — T-2026-KYT-9050-043

_generated 2026-07-25 19:34:04.086415+00:00 · read-only · window 2026-07-18 00:00:00 → 2026-07-26 00:00:00_

**High-fidelity harness (T-035):** 5m wick candles (touch backbone) + 10s order resolver, geometry from immutable Cornix text (`telegram_outbox`, entry1/entry2/orig-SL/TP1-3), realized via `core.realized_pnl`. Compared on the **entry2-present** trade set (all 3 arms defined). Metric risk-adjusted (T-041): the leveraged **sum** is a fat-tail/−100% clamp artefact → **Sharpe (per-trade lev) + compounding MaxDD (fixed 2%)** are the signal.

Scored (entry2 set): **98**

## The 3 arms

| Arm | Setup | n | unlev sum% | unlev mean% | lev sum% | **Sharpe lev** | **MaxDD (2%)** | WR(TP1)% |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| A | DCA (entry1+entry2, orig SL) | 98 | -41.18 | -0.4202 | -313.8 | **-0.085** | **11.6%** | 75.5 |
| B | Single entry1, orig SL | 98 | -40.16 | -0.4098 | 152.7 | **0.027** | **14.1%** | 75.5 |
| C | Single entry1, SL@entry2 | 98 | -0.16 | -0.0016 | 106.4 | **0.019** | **14.4%** | 74.5 |

### Findings

- **Dropping DCA (A→B Sharpe -0.085→+0.027):** helps.
- **SL@entry2 instead of DCA add (B→C Sharpe +0.027→+0.019):** does not help / neutral.
- **MaxDD (2%):** A 11.6% · B 14.1% · C 14.4% 
- **Best arm (Sharpe): B.** Michi's entry2-as-SL (C) does not robustly beat DCA — see the numbers.

### Long/short separated (Sharpe lev / MaxDD%)

| Arm | LONG | SHORT |
|---|--:|--:|
| A | -0.102/12.2% | 1.302/0.0% |
| B | 0.008/15.2% | 1.302/0.0% |
| C | -0.0/15.5% | 1.302/0.0% |

**Honest limits:** ~7d/outbox window (geometry retention), entry2 set (not all trades publish entry2). SL@entry2 is a **tight** stop (closer than the original SL) → more small stops, fewer deep losers; whether that holds up net depends on entry2-fill→recover vs →SL. Compounding sequential-after-close (ignores concurrency). NO-EDGE is a valid result.

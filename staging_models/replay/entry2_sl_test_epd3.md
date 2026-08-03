# entry2-as-SL vs DCA — 3-arm test (EPD3) — T-2026-KYT-9050-043

_generated 2026-07-25 19:35:48.587424+00:00 · read-only · window 2026-07-18 00:00:00 → 2026-07-26 00:00:00_

**High-Fidelity Harness (T-035):** 5m wick candles (touch backbone) + 10s order resolver, geometry from immutable Cornix text (`telegram_outbox`, entry1/entry2/orig-SL/TP1-3), realized via `core.realized_pnl`. Compared on the **entry2-present** trade set (all 3 arms defined). Metric risk-adjusted (T-041): the leveraged **Sum** is a fat-tail/−100% clamp artefact → **Sharpe (per-trade lev) + compounding MaxDD (fixed 2%)** are the signal.

Scored (entry2 set): **1165**

## The 3 arms

| Arm | Setup | n | unlev sum% | unlev mean% | lev sum% | **Sharpe lev** | **MaxDD (2%)** | WR(TP1)% |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| A | DCA (entry1+entry2, orig SL) | 1165 | 117.27 | 0.1007 | 6836.3 | **0.139** | **10.8%** | 79.7 |
| B | Single entry1, orig SL | 1165 | 246.25 | 0.2114 | 13331.3 | **0.199** | **14.4%** | 79.7 |
| C | Single entry1, SL@entry2 | 1165 | 273.7 | 0.2349 | 5591.3 | **0.081** | **23.7%** | 73.4 |

### Findings

- **Dropping DCA (A→B Sharpe +0.139→+0.199):** helps.
- **SL@entry2 instead of DCA-add (B→C Sharpe +0.199→+0.081):** doesn't help / neutral.
- **MaxDD (2%):** A 10.8% · B 14.4% · C 23.7% 
- **Best arm (Sharpe): B.** Michi's entry2-as-SL (C) doesn't robustly beat DCA — see the numbers.

### Long/short split (Sharpe lev / MaxDD%)

| Arm | LONG | SHORT |
|---|--:|--:|
| A | 0.155/5.2% | 0.138/11.1% |
| B | 0.192/7.4% | 0.199/11.9% |
| C | 0.062/8.5% | 0.083/26.1% |

**Honest limits:** ~7d/outbox window (geometry retention), entry2 set (not all trades publish entry2). SL@entry2 is a **tighter** stop (closer than the original SL) → more small stops, fewer deep losers; whether that nets out positive depends on entry2-fill→recover vs →SL. Compounding sequential-after-close (ignores simultaneity). NO-EDGE is a valid result.

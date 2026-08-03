# entry2-as-SL vs DCA — 3-arm test (AIM2) — T-2026-KYT-9050-043

_generated 2026-07-25 20:01:48.604309+00:00 · read-only · window 2026-07-11 00:00:00 → 2026-07-26 00:00:00_

**High-fidelity harness (T-035):** 5m wick candles (touch backbone) + 10s order resolver, geometry from immutable Cornix text (`telegram_outbox`, entry1/entry2/orig-SL/TP1-3), realized via `core.realized_pnl`. Compared on the **entry2-present** trade set (all 3 arms defined). Metric risk-adjusted (T-041): the leveraged **sum** is a fat-tail/−100% clamp artefact → **Sharpe (per-trade lev) + compounding MaxDD (fixed 2%)** are the signal.

Scored (entry2 set): **600**

## The 3 arms

| Arm | Setup | n | unlev sum% | unlev mean% | lev sum% | **Sharpe lev** | **MaxDD (2%)** | WR(TP1)% |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| A | DCA (entry1+entry2, orig SL) | 600 | 324.16 | 0.5403 | 9387.6 | **0.179** | **16.4%** | 67.0 |
| B | Single entry1, orig SL | 600 | 692.92 | 1.1549 | 18846.9 | **0.267** | **15.8%** | 67.0 |
| C | Single entry1, SL@entry2 | 600 | 742.97 | 1.2383 | 14632.5 | **0.234** | **10.7%** | 57.8 |

### Findings

- **Dropping DCA (A→B Sharpe +0.179→+0.267):** helps.
- **SL@entry2 instead of DCA add (B→C Sharpe +0.267→+0.234):** does not help / neutral.
- **MaxDD (2%):** A 16.4% · B 15.8% · C 10.7% → the tightest stop (C) compresses the drawdown.
- **Best arm (Sharpe): B.** Michi's entry2-as-SL (C) does not robustly beat DCA — see the numbers.

### Long/short separated (Sharpe lev / MaxDD%)

| Arm | LONG | SHORT |
|---|--:|--:|
| A | 0.209/7.8% | 0.153/30.0% |
| B | 0.297/10.4% | 0.24/32.6% |
| C | 0.251/8.5% | 0.218/24.8% |

**Honest limits:** ~7d/outbox window (geometry retention), entry2 set (not all trades publish entry2). SL@entry2 is a **tight** stop (closer than the original SL) → more small stops, fewer deep losers; whether that holds up net depends on entry2-fill→recover vs →SL. Compounding sequential-after-close (ignores concurrency). NO-EDGE is a valid result.

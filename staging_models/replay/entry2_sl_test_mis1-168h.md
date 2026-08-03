# entry2-as-SL vs DCA — 3-arm test (MIS1-168H) — T-2026-KYT-9050-043

_generated 2026-07-25 20:03:21.710771+00:00 · read-only · window 2026-05-14 00:00:00 → 2026-07-26 00:00:00_

**High-fidelity harness (T-035):** 5m wick candles (touch backbone) + 10s order resolver, geometry from immutable Cornix text (`telegram_outbox`, entry1/entry2/orig-SL/TP1-3), realized via `core.realized_pnl`. Compared on the **entry2-present** trade set (all 3 arms defined). Metric risk-adjusted (T-041): the leveraged **sum** is a fat-tail/−100% clamp artefact → **Sharpe (per-trade lev) + compounding MaxDD (fixed 2%)** are the signal.

Scored (entry2 set): **342**

## The 3 arms

| Arm | Setup | n | unlev sum% | unlev mean% | lev sum% | **Sharpe lev** | **MaxDD (2%)** | WR(TP1)% |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| A | DCA (entry1+entry2, orig SL) | 342 | 24.42 | 0.0714 | 1136.5 | **0.059** | **24.4%** | 65.5 |
| B | Single entry1, orig SL | 342 | 193.54 | 0.5659 | 4761.9 | **0.16** | **20.2%** | 65.5 |
| C | Single entry1, SL@entry2 | 342 | 340.82 | 0.9965 | 5887.8 | **0.222** | **12.9%** | 59.4 |

### Findings

- **Dropping DCA (A→B Sharpe +0.059→+0.16):** helps.
- **SL@entry2 instead of DCA add (B→C Sharpe +0.16→+0.222):** helps.
- **MaxDD (2%):** A 24.4% · B 20.2% · C 12.9% → the tightest stop (C) compresses the drawdown.
- **Best arm (Sharpe): C.** Michi's entry2-as-SL (C) holds up.

### Long/short separated (Sharpe lev / MaxDD%)

| Arm | LONG | SHORT |
|---|--:|--:|
| A | 0.059/24.4% | — |
| B | 0.16/20.2% | — |
| C | 0.222/12.9% | — |

**Honest limits:** ~7d/outbox window (geometry retention), entry2 set (not all trades publish entry2). SL@entry2 is a **tight** stop (closer than the original SL) → more small stops, fewer deep losers; whether that holds up net depends on entry2-fill→recover vs →SL. Compounding sequential-after-close (ignores concurrency). NO-EDGE is a valid result.

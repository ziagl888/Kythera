# entry2-als-SL vs DCA — 3-Arm-Test (MIS1-168H) — T-2026-KYT-9050-043

_generated 2026-07-25 20:03:21.710771+00:00 · read-only · window 2026-05-14 00:00:00 → 2026-07-26 00:00:00_

**High-Fidelity-Harness (T-035):** 5m-Wick-Kerzen (Touch-Backbone) + 10s-Order-Resolver, Geometrie aus immutablem Cornix-Text (`telegram_outbox`, entry1/entry2/orig-SL/TP1-3), Realized via `core.realized_pnl`. Verglichen auf dem **entry2-vorhandenen** Trade-Set (alle 3 Arme definiert). Metrik risiko-adjustiert (T-041): der leveraged **Sum** ist ein Fat-Tail/−100%-Clamp-Artefakt → **Sharpe (per-Trade lev) + kompoundierende MaxDD (fixe 2%)** sind das Signal.

Gescort (entry2-Set): **342**

## Die 3 Arme

| Arm | Setup | n | unlev sum% | unlev mean% | lev sum% | **Sharpe lev** | **MaxDD (2%)** | WR(TP1)% |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| A | DCA (entry1+entry2, orig SL) | 342 | 24.42 | 0.0714 | 1136.5 | **0.059** | **24.4%** | 65.5 |
| B | Single entry1, orig SL | 342 | 193.54 | 0.5659 | 4761.9 | **0.16** | **20.2%** | 65.5 |
| C | Single entry1, SL@entry2 | 342 | 340.82 | 0.9965 | 5887.8 | **0.222** | **12.9%** | 59.4 |

### Befund

- **DCA weglassen (A→B Sharpe +0.059→+0.16):** hilft.
- **SL@entry2 statt DCA-Add (B→C Sharpe +0.16→+0.222):** hilft.
- **MaxDD (2%):** A 24.4% · B 20.2% · C 12.9% → engster Stop (C) drückt den Drawdown.
- **Bester Arm (Sharpe): C.** Michis entry2-als-SL (C) trägt.

### Long/Short getrennt (Sharpe lev / MaxDD%)

| Arm | LONG | SHORT |
|---|--:|--:|
| A | 0.059/24.4% | — |
| B | 0.16/20.2% | — |
| C | 0.222/12.9% | — |

**Ehrliche Grenzen:** ~7d/Outbox-Fenster (Geometrie-Retention), entry2-Set (nicht alle Trades publizieren entry2). SL@entry2 ist ein **enger** Stop (näher als der Original-SL) → mehr kleine Stops, weniger tiefe Verlierer; ob das netto trägt, hängt an entry2-fill→recover vs →SL. Compounding sequenziell-nach-close (ignoriert Gleichzeitigkeit). NO-EDGE ist ein valides Ergebnis.

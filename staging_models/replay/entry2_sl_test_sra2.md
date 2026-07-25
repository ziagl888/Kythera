# entry2-als-SL vs DCA — 3-Arm-Test (SRA2) — T-2026-KYT-9050-043

_generated 2026-07-25 19:34:04.086415+00:00 · read-only · window 2026-07-18 00:00:00 → 2026-07-26 00:00:00_

**High-Fidelity-Harness (T-035):** 5m-Wick-Kerzen (Touch-Backbone) + 10s-Order-Resolver, Geometrie aus immutablem Cornix-Text (`telegram_outbox`, entry1/entry2/orig-SL/TP1-3), Realized via `core.realized_pnl`. Verglichen auf dem **entry2-vorhandenen** Trade-Set (alle 3 Arme definiert). Metrik risiko-adjustiert (T-041): der leveraged **Sum** ist ein Fat-Tail/−100%-Clamp-Artefakt → **Sharpe (per-Trade lev) + kompoundierende MaxDD (fixe 2%)** sind das Signal.

Gescort (entry2-Set): **98**

## Die 3 Arme

| Arm | Setup | n | unlev sum% | unlev mean% | lev sum% | **Sharpe lev** | **MaxDD (2%)** | WR(TP1)% |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| A | DCA (entry1+entry2, orig SL) | 98 | -41.18 | -0.4202 | -313.8 | **-0.085** | **11.6%** | 75.5 |
| B | Single entry1, orig SL | 98 | -40.16 | -0.4098 | 152.7 | **0.027** | **14.1%** | 75.5 |
| C | Single entry1, SL@entry2 | 98 | -0.16 | -0.0016 | 106.4 | **0.019** | **14.4%** | 74.5 |

### Befund

- **DCA weglassen (A→B Sharpe -0.085→+0.027):** hilft.
- **SL@entry2 statt DCA-Add (B→C Sharpe +0.027→+0.019):** hilft nicht / neutral.
- **MaxDD (2%):** A 11.6% · B 14.1% · C 14.4% 
- **Bester Arm (Sharpe): B.** Michis entry2-als-SL (C) schlägt DCA nicht robust — siehe Zahlen.

### Long/Short getrennt (Sharpe lev / MaxDD%)

| Arm | LONG | SHORT |
|---|--:|--:|
| A | -0.102/12.2% | 1.302/0.0% |
| B | 0.008/15.2% | 1.302/0.0% |
| C | -0.0/15.5% | 1.302/0.0% |

**Ehrliche Grenzen:** ~7d/Outbox-Fenster (Geometrie-Retention), entry2-Set (nicht alle Trades publizieren entry2). SL@entry2 ist ein **enger** Stop (näher als der Original-SL) → mehr kleine Stops, weniger tiefe Verlierer; ob das netto trägt, hängt an entry2-fill→recover vs →SL. Compounding sequenziell-nach-close (ignoriert Gleichzeitigkeit). NO-EDGE ist ein valides Ergebnis.

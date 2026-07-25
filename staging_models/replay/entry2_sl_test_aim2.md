# entry2-als-SL vs DCA — 3-Arm-Test (AIM2) — T-2026-KYT-9050-043

_generated 2026-07-25 19:32:57.707325+00:00 · read-only · window 2026-07-18 00:00:00 → 2026-07-26 00:00:00_

**High-Fidelity-Harness (T-035):** 5m-Wick-Kerzen (Touch-Backbone) + 10s-Order-Resolver, Geometrie aus immutablem Cornix-Text (`telegram_outbox`, entry1/entry2/orig-SL/TP1-3), Realized via `core.realized_pnl`. Verglichen auf dem **entry2-vorhandenen** Trade-Set (alle 3 Arme definiert). Metrik risiko-adjustiert (T-041): der leveraged **Sum** ist ein Fat-Tail/−100%-Clamp-Artefakt → **Sharpe (per-Trade lev) + kompoundierende MaxDD (fixe 2%)** sind das Signal.

Gescort (entry2-Set): **558**

## Die 3 Arme

| Arm | Setup | n | unlev sum% | unlev mean% | lev sum% | **Sharpe lev** | **MaxDD (2%)** | WR(TP1)% |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| A | DCA (entry1+entry2, orig SL) | 558 | 323.19 | 0.5792 | 9303.8 | **0.186** | **16.4%** | 67.0 |
| B | Single entry1, orig SL | 558 | 670.4 | 1.2014 | 18302.8 | **0.272** | **15.8%** | 67.0 |
| C | Single entry1, SL@entry2 | 558 | 697.85 | 1.2506 | 13783.2 | **0.231** | **11.4%** | 57.2 |

### Befund

- **DCA weglassen (A→B Sharpe +0.186→+0.272):** hilft.
- **SL@entry2 statt DCA-Add (B→C Sharpe +0.272→+0.231):** hilft nicht / neutral.
- **MaxDD (2%):** A 16.4% · B 15.8% · C 11.4% → engster Stop (C) drückt den Drawdown.
- **Bester Arm (Sharpe): B.** Michis entry2-als-SL (C) schlägt DCA nicht robust — siehe Zahlen.

### Long/Short getrennt (Sharpe lev / MaxDD%)

| Arm | LONG | SHORT |
|---|--:|--:|
| A | 0.22/7.8% | 0.153/29.7% |
| B | 0.31/10.4% | 0.236/32.8% |
| C | 0.261/8.5% | 0.202/26.7% |

**Ehrliche Grenzen:** ~7d/Outbox-Fenster (Geometrie-Retention), entry2-Set (nicht alle Trades publizieren entry2). SL@entry2 ist ein **enger** Stop (näher als der Original-SL) → mehr kleine Stops, weniger tiefe Verlierer; ob das netto trägt, hängt an entry2-fill→recover vs →SL. Compounding sequenziell-nach-close (ignoriert Gleichzeitigkeit). NO-EDGE ist ein valides Ergebnis.

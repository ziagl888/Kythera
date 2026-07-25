# entry2-als-SL vs DCA — 3-Arm-Test (EPD3) — T-2026-KYT-9050-043

_generated 2026-07-25 19:35:48.587424+00:00 · read-only · window 2026-07-18 00:00:00 → 2026-07-26 00:00:00_

**High-Fidelity-Harness (T-035):** 5m-Wick-Kerzen (Touch-Backbone) + 10s-Order-Resolver, Geometrie aus immutablem Cornix-Text (`telegram_outbox`, entry1/entry2/orig-SL/TP1-3), Realized via `core.realized_pnl`. Verglichen auf dem **entry2-vorhandenen** Trade-Set (alle 3 Arme definiert). Metrik risiko-adjustiert (T-041): der leveraged **Sum** ist ein Fat-Tail/−100%-Clamp-Artefakt → **Sharpe (per-Trade lev) + kompoundierende MaxDD (fixe 2%)** sind das Signal.

Gescort (entry2-Set): **1165**

## Die 3 Arme

| Arm | Setup | n | unlev sum% | unlev mean% | lev sum% | **Sharpe lev** | **MaxDD (2%)** | WR(TP1)% |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| A | DCA (entry1+entry2, orig SL) | 1165 | 117.27 | 0.1007 | 6836.3 | **0.139** | **10.8%** | 79.7 |
| B | Single entry1, orig SL | 1165 | 246.25 | 0.2114 | 13331.3 | **0.199** | **14.4%** | 79.7 |
| C | Single entry1, SL@entry2 | 1165 | 273.7 | 0.2349 | 5591.3 | **0.081** | **23.7%** | 73.4 |

### Befund

- **DCA weglassen (A→B Sharpe +0.139→+0.199):** hilft.
- **SL@entry2 statt DCA-Add (B→C Sharpe +0.199→+0.081):** hilft nicht / neutral.
- **MaxDD (2%):** A 10.8% · B 14.4% · C 23.7% 
- **Bester Arm (Sharpe): B.** Michis entry2-als-SL (C) schlägt DCA nicht robust — siehe Zahlen.

### Long/Short getrennt (Sharpe lev / MaxDD%)

| Arm | LONG | SHORT |
|---|--:|--:|
| A | 0.155/5.2% | 0.138/11.1% |
| B | 0.192/7.4% | 0.199/11.9% |
| C | 0.062/8.5% | 0.083/26.1% |

**Ehrliche Grenzen:** ~7d/Outbox-Fenster (Geometrie-Retention), entry2-Set (nicht alle Trades publizieren entry2). SL@entry2 ist ein **enger** Stop (näher als der Original-SL) → mehr kleine Stops, weniger tiefe Verlierer; ob das netto trägt, hängt an entry2-fill→recover vs →SL. Compounding sequenziell-nach-close (ignoriert Gleichzeitigkeit). NO-EDGE ist ein valides Ergebnis.

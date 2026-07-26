# Trailing-Close FINALISE (risk-adjusted, High-Fidelity) — EPD3 — T-2026-KYT-9050-046

_generated 2026-07-26 07:37:08.608203+00:00 · read-only · window 2026-07-21 00:00:00 → 2026-07-27 00:00:00_

**High-Fidelity-Harness (T-035):** per-Trade-Trailing-TP (Overlay a) auf dem **DCA-treuen** cornix3-MTM, 5m-Wick + 10s-Resolver. T-035 wertete das nur auf der leveraged **Summe** aus (Fat-Tail/−100%-Clamp-Artefakt → schien NO-EDGE). Hier **risiko-adjustiert** (T-041): per-Trade leveraged **Sharpe** + kompoundierende **MaxDD (fixe 2%)**, hold vs Trailing-X.

Gescort (leveraged Arts): **1157**

| Variante | n | mean lev% | **Sharpe lev** | **MaxDD (2%)** |
|---|--:|--:|--:|--:|
| **hold** | 1157 | 6.11 | **0.145** | **10.6%** |
| Trailing 10% | 1157 | 1.5 | **0.076** | **8.4%** |
| Trailing 15% | 1157 | 1.53 | **0.076** | **8.6%** |
| Trailing 20% | 1157 | 1.53 | **0.076** | **8.9%** |
| Trailing 25% | 1157 | 1.6 | **0.076** | **8.7%** |
| Trailing 30% | 1157 | 1.67 | **0.078** | **8.3%** |
| Trailing 40% | 1157 | 1.57 | **0.072** | **8.1%** |

### Befund

- **Bester Trailing-X = 30%: Sharpe +0.078 vs hold +0.145** → kein Sharpe-Gewinn.
- **MaxDD (2%): hold 10.6% → Trailing 30% 8.3%** (~1.3× kleiner).
- **Fidelity-Vergleich:** T-041 (First-Order 1h) fand dieselbe Richtung (Sharpe rauf, MaxDD runter). Hier abgeschwächt/nicht bestätigt — siehe Zahlen.

**Ehrliche Grenzen:** ~Outbox-Fenster (Geometrie-Retention), leveraged Arts, Compounding sequenziell-nach-close (ignoriert Gleichzeitigkeit) → MaxDD-Ratio ist das Signal, nicht der absolute Multiple. Trailing-Deploy = eigener Operator-Entscheid (Michi).

# Trailing-Close FINALISE (risk-adjusted, High-Fidelity) — AIM2 — T-2026-KYT-9050-046

_generated 2026-07-26 07:33:32.012196+00:00 · read-only · window 2026-07-11 00:00:00 → 2026-07-27 00:00:00_

**High-Fidelity-Harness (T-035):** per-Trade-Trailing-TP (Overlay a) auf dem **DCA-treuen** cornix3-MTM, 5m-Wick + 10s-Resolver. T-035 wertete das nur auf der leveraged **Summe** aus (Fat-Tail/−100%-Clamp-Artefakt → schien NO-EDGE). Hier **risiko-adjustiert** (T-041): per-Trade leveraged **Sharpe** + kompoundierende **MaxDD (fixe 2%)**, hold vs Trailing-X.

Gescort (leveraged Arts): **491**

| Variante | n | mean lev% | **Sharpe lev** | **MaxDD (2%)** |
|---|--:|--:|--:|--:|
| **hold** | 491 | 14.87 | **0.193** | **9.5%** |
| Trailing 10% | 491 | 10.07 | **0.349** | **2.8%** |
| Trailing 15% | 491 | 9.86 | **0.342** | **2.7%** |
| Trailing 20% | 491 | 9.75 | **0.332** | **2.5%** |
| Trailing 25% | 491 | 9.9 | **0.328** | **2.5%** |
| Trailing 30% | 491 | 9.98 | **0.322** | **2.5%** |
| Trailing 40% | 491 | 10.31 | **0.318** | **2.7%** |

### Befund

- **Bester Trailing-X = 10%: Sharpe +0.349 vs hold +0.193** → Trailing hebt den risiko-adjustierten Ertrag.
- **MaxDD (2%): hold 9.5% → Trailing 10% 2.8%** (~3.4× kleiner).
- **Fidelity-Vergleich:** T-041 (First-Order 1h) fand dieselbe Richtung (Sharpe rauf, MaxDD runter). Hier auf 5m+10s+DCA **bestätigt**.

**Ehrliche Grenzen:** ~Outbox-Fenster (Geometrie-Retention), leveraged Arts, Compounding sequenziell-nach-close (ignoriert Gleichzeitigkeit) → MaxDD-Ratio ist das Signal, nicht der absolute Multiple. Trailing-Deploy = eigener Operator-Entscheid (Michi).

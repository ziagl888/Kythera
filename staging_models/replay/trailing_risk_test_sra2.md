# Trailing-Close FINALISE (risk-adjusted, High-Fidelity) — SRA2 — T-2026-KYT-9050-046

_generated 2026-07-26 07:34:22.811345+00:00 · read-only · window 2026-07-21 00:00:00 → 2026-07-27 00:00:00_

**High-Fidelity-Harness (T-035):** per-Trade-Trailing-TP (Overlay a) auf dem **DCA-treuen** cornix3-MTM, 5m-Wick + 10s-Resolver. T-035 wertete das nur auf der leveraged **Summe** aus (Fat-Tail/−100%-Clamp-Artefakt → schien NO-EDGE). Hier **risiko-adjustiert** (T-041): per-Trade leveraged **Sharpe** + kompoundierende **MaxDD (fixe 2%)**, hold vs Trailing-X.

Gescort (leveraged Arts): **116**

| Variante | n | mean lev% | **Sharpe lev** | **MaxDD (2%)** |
|---|--:|--:|--:|--:|
| **hold** | 116 | 0.99 | **0.024** | **11.6%** |
| Trailing 10% | 116 | 3.21 | **0.146** | **2.6%** |
| Trailing 15% | 116 | 3.17 | **0.145** | **2.6%** |
| Trailing 20% | 116 | 3.15 | **0.144** | **2.7%** |
| Trailing 25% | 116 | 3.05 | **0.139** | **2.8%** |
| Trailing 30% | 116 | 2.91 | **0.133** | **2.8%** |
| Trailing 40% | 116 | 2.64 | **0.121** | **2.8%** |

### Befund

- **Bester Trailing-X = 10%: Sharpe +0.146 vs hold +0.024** → Trailing hebt den risiko-adjustierten Ertrag.
- **MaxDD (2%): hold 11.6% → Trailing 10% 2.6%** (~4.5× kleiner).
- **Fidelity-Vergleich:** T-041 (First-Order 1h) fand dieselbe Richtung (Sharpe rauf, MaxDD runter). Hier auf 5m+10s+DCA **bestätigt**.

**Ehrliche Grenzen:** ~Outbox-Fenster (Geometrie-Retention), leveraged Arts, Compounding sequenziell-nach-close (ignoriert Gleichzeitigkeit) → MaxDD-Ratio ist das Signal, nicht der absolute Multiple. Trailing-Deploy = eigener Operator-Entscheid (Michi).

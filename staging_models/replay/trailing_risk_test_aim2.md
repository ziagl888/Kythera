# Trailing-close FINALISE (risk-adjusted, high-fidelity) — AIM2 — T-2026-KYT-9050-046

_generated 2026-07-26 07:33:32.012196+00:00 · read-only · window 2026-07-11 00:00:00 → 2026-07-27 00:00:00_

**High-fidelity harness (T-035):** per-trade trailing TP (overlay a) on the **DCA-faithful** cornix3-MTM, 5m wick + 10s resolver. T-035 evaluated this only on the leveraged **sum** (fat-tail/−100% clamp artefact → looked like NO-EDGE). Here **risk-adjusted** (T-041): per-trade leveraged **Sharpe** + compounding **MaxDD (fixed 2%)**, hold vs trailing-X.

Scored (leveraged arts): **491**

| Variante | n | mean lev% | **Sharpe lev** | **MaxDD (2%)** |
|---|--:|--:|--:|--:|
| **hold** | 491 | 14.87 | **0.193** | **9.5%** |
| Trailing 10% | 491 | 10.07 | **0.349** | **2.8%** |
| Trailing 15% | 491 | 9.86 | **0.342** | **2.7%** |
| Trailing 20% | 491 | 9.75 | **0.332** | **2.5%** |
| Trailing 25% | 491 | 9.9 | **0.328** | **2.5%** |
| Trailing 30% | 491 | 9.98 | **0.322** | **2.5%** |
| Trailing 40% | 491 | 10.31 | **0.318** | **2.7%** |

### Findings

- **Best trailing-X = 10%: Sharpe +0.349 vs hold +0.193** → trailing lifts the risk-adjusted return.
- **MaxDD (2%): hold 9.5% → trailing 10% 2.8%** (~3.4× smaller).
- **Fidelity comparison:** T-041 (first-order 1h) found the same direction (Sharpe up, MaxDD down). Here on 5m+10s+DCA **confirmed**.

**Honest limits:** ~outbox window (geometry retention), leveraged arts, compounding sequential-after-close (ignores concurrency) → the MaxDD ratio is the signal, not the absolute multiple. Trailing deploy = separate operator decision (Michi).

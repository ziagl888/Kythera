# Trailing-close FINALISE (risk-adjusted, high-fidelity) — SRA2 — T-2026-KYT-9050-046

_generated 2026-07-26 07:34:22.811345+00:00 · read-only · window 2026-07-21 00:00:00 → 2026-07-27 00:00:00_

**High-fidelity harness (T-035):** per-trade trailing TP (overlay a) on the **DCA-faithful** cornix3-MTM, 5m wick + 10s resolver. T-035 evaluated this only on the leveraged **sum** (fat-tail/−100% clamp artefact → looked like NO-EDGE). Here **risk-adjusted** (T-041): per-trade leveraged **Sharpe** + compounding **MaxDD (fixed 2%)**, hold vs trailing-X.

Scored (leveraged arts): **116**

| Variante | n | mean lev% | **Sharpe lev** | **MaxDD (2%)** |
|---|--:|--:|--:|--:|
| **hold** | 116 | 0.99 | **0.024** | **11.6%** |
| Trailing 10% | 116 | 3.21 | **0.146** | **2.6%** |
| Trailing 15% | 116 | 3.17 | **0.145** | **2.6%** |
| Trailing 20% | 116 | 3.15 | **0.144** | **2.7%** |
| Trailing 25% | 116 | 3.05 | **0.139** | **2.8%** |
| Trailing 30% | 116 | 2.91 | **0.133** | **2.8%** |
| Trailing 40% | 116 | 2.64 | **0.121** | **2.8%** |

### Findings

- **Best trailing-X = 10%: Sharpe +0.146 vs hold +0.024** → trailing lifts the risk-adjusted return.
- **MaxDD (2%): hold 11.6% → trailing 10% 2.6%** (~4.5× smaller).
- **Fidelity comparison:** T-041 (first-order 1h) found the same direction (Sharpe up, MaxDD down). Here on 5m+10s+DCA **confirmed**.

**Honest limits:** ~outbox window (geometry retention), leveraged arts, compounding sequential-after-close (ignores concurrency) → the MaxDD ratio is the signal, not the absolute multiple. Trailing deploy = separate operator decision (Michi).

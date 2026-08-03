# Trailing-close FINALISE (risk-adjusted, high-fidelity) — EPD3 — T-2026-KYT-9050-046

_generated 2026-07-26 07:37:08.608203+00:00 · read-only · window 2026-07-21 00:00:00 → 2026-07-27 00:00:00_

**High-fidelity harness (T-035):** per-trade trailing TP (overlay a) on the **DCA-faithful** cornix3-MTM, 5m wick + 10s resolver. T-035 evaluated this only on the leveraged **sum** (fat-tail/−100% clamp artefact → looked like NO-EDGE). Here **risk-adjusted** (T-041): per-trade leveraged **Sharpe** + compounding **MaxDD (fixed 2%)**, hold vs trailing-X.

Scored (leveraged arts): **1157**

| Variante | n | mean lev% | **Sharpe lev** | **MaxDD (2%)** |
|---|--:|--:|--:|--:|
| **hold** | 1157 | 6.11 | **0.145** | **10.6%** |
| Trailing 10% | 1157 | 1.5 | **0.076** | **8.4%** |
| Trailing 15% | 1157 | 1.53 | **0.076** | **8.6%** |
| Trailing 20% | 1157 | 1.53 | **0.076** | **8.9%** |
| Trailing 25% | 1157 | 1.6 | **0.076** | **8.7%** |
| Trailing 30% | 1157 | 1.67 | **0.078** | **8.3%** |
| Trailing 40% | 1157 | 1.57 | **0.072** | **8.1%** |

### Findings

- **Best trailing-X = 30%: Sharpe +0.078 vs hold +0.145** → no Sharpe gain.
- **MaxDD (2%): hold 10.6% → trailing 30% 8.3%** (~1.3× smaller).
- **Fidelity comparison:** T-041 (first-order 1h) found the same direction (Sharpe up, MaxDD down). Here weakened/not confirmed — see the numbers.

**Honest limits:** ~outbox window (geometry retention), leveraged arts, compounding sequential-after-close (ignores concurrency) → the MaxDD ratio is the signal, not the absolute multiple. Trailing deploy = separate operator decision (Michi).

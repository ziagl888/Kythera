# SOFT-Regime Gate Counterfactual — T-2026-KYT-9050-031

_generated 2026-07-23 02:51:41.707899+00:00 · 90d window · feature hl=192 checks (16.0h)_

**Verdict: NO-EDGE for a proven PnL uplift (churn-confirmed; live A/B is the only settler)** — SOFT robustly cuts churn 87% and the churn-affected (disagree) forwarded trades DO underperform (6.01pp lower WR, p=0.0013) — a real directional signal. But it is not a demonstrable PnL uplift: (a) the WR gap reaches significance only at heavy smoothing (hl≥16h, gating ~half the flow) and is insignificant at ≤8h; (b) first-touch replay PnL is near-zero and NEGATIVE in both buckets (agree -0.0586%, disagree -0.205%/trade) — consistent with T-029's η²≈0, you are choosing between losers, not toward a winner; (c) 'disagree' ≠ 'SOFT would suppress' — that mapping needs the historical whitelist, which is overwritten each cycle (unreconstructable), and the only available proxy (current snapshot, circular) points the OTHER way. Verdict: the churn win does not convert to a proven PnL gain on the joinable evidence; only a live shadow A/B of a SOFT gate can settle it.

## 1 · Churn (live regime_history reconstruction)

| timeline | switches/30d | mean dwell (h) | % episodes <1h |
|---|--:|--:|--:|
| raw_stored | 2731.37 | 0.26 | 95.8 |
| rule_recon | 170.06 | 4.23 | 43.6 |
| soft_12 | 209.8 | 3.43 | 54.4 |
| soft_48 | 75.05 | 9.57 | 30.3 |
| soft_96 | 37.53 | 19.11 | 15.5 |
| soft_192 | 22.64 | 31.58 | 17.4 |
| soft_288 | 11.08 | 64.04 | 12.7 |

SOFT(hl=192) cuts RULE switches by **87%** (170.06→22.64 /30d).

## 2 · Reconstruction fidelity

RULE_recon vs recorded `regime_at_open`: **91.85%** agreement over 7902 forwarded trades. (Residual = warm-up cold-start + ingestion-outage debounce desync.)

## 3 · PnL signal — forwarded TP/SL win-rate by SOFT-vs-RULE agreement

Ground-truth outcome (`status`), zero replay, zero whitelist proxy. SOFT smooths only the BTC regime; `alt_context` held fixed.

| bucket | decided (TP+SL) | TP | SL | win-rate |
|---|--:|--:|--:|--:|
| SOFT agrees RULE | 1524 | 951 | 573 | 62.4% |
| SOFT disagrees RULE | 1259 | 710 | 549 | 56.39% |

Δ = **6.01pp** (agree − disagree), z=3.216, p=0.0013. 4286 `CLOSED_REGIME_CHANGE` auto-closes excluded (no TP/SL label).

Top disagreement shifts (recorded RULE → SOFT), disagree bucket:

| shift | n | win-rate |
|---|--:|--:|
| TRANSITION->CHOP | 459 | 55.8% |
| HIGH_VOLA->CHOP | 188 | 49.5% |
| TRANSITION->TREND_UP | 113 | 49.6% |
| TRANSITION->TREND_DOWN | 89 | 55.1% |
| TRANSITION->HIGH_VOLA | 75 | 66.7% |
| CHOP->TREND_DOWN | 74 | 54.1% |
| CHOP->TRANSITION | 54 | 63.0% |
| CHOP->TREND_UP | 50 | 54.0% |
| TREND_UP->CHOP | 42 | 69.0% |
| TREND_DOWN->CHOP | 40 | 75.0% |
| CHOP->HIGH_VOLA | 37 | 64.9% |
| HIGH_VOLA->TREND_DOWN | 12 | 58.3% |

## 4 · Robustness across half-lives

| hl (checks) | hl (h) | % fwd disagree | agree WR | disagree WR | Δpp | p |
|--:|--:|--:|--:|--:|--:|--:|
| 12 | 1.0 | 25.9% | 60.27% | 58.12% | 2.14 | 0.30524 |
| 48 | 4.0 | 39.4% | 60.59% | 58.29% | 2.31 | 0.22554 |
| 96 | 8.0 | 45.0% | 60.72% | 58.36% | 2.36 | 0.20869 |
| 192 | 16.0 | 47.7% | 62.4% | 56.39% | 6.01 | 0.0013 |
| 288 | 24.0 | 48.8% | 63.38% | 55.37% | 8.0 | 2e-05 |

## 5 · PnL magnitude — first-touch replay by bucket

ROM1 geometry replay (`rom1_counterfactual`) — WR is not PnL (R:R matters). Absolute level reflects a fixed ROM1 geometry, not per-bot realized PnL; the cross-bucket comparison is the signal.

| bucket | n scored | avg net PnL% | median | sum net PnL% | replay TP1 WR |
|---|--:|--:|--:|--:|--:|
| SOFT agrees RULE | 4100 | -0.0586 | 1.3396 | -240.28 | 77.68% |
| SOFT disagrees RULE | 3747 | -0.205 | 1.3406 | -768.08 | 77.08% |

## 6 · Whitelist reflip (FLAGGED PROXY — not a verdict)

_PROXY — current whitelist snapshot, circular + single-timepoint. NOT a verdict._

| SOFT gate outcome | n | TP | SL | win-rate |
|---|--:|--:|--:|--:|
| would keep (forward) | 6014 | 1456 | 1014 | 58.95% |
| would suppress | 1888 | 205 | 108 | 65.5% |

0 rows without alt_context/soft regime skipped.

## Join limits (honest)

- bot_regime_whitelist is overwritten wholesale each analyzer cycle (PK on the 4-tuple, no history) → the as-of whitelist for a past signal under a DIFFERENT regime is not reconstructable; the §6 reflip uses the current snapshot as a circular proxy only.
- prob↔outcome not reliably joinable in the live DB → outcome proxied by orchestrator trade `status` (CLOSED_TP/SL), not realized PnL.
- orchestrator_suppressed_signals carries no alt_context → whitelist reflip is forwarded-only.
- SOFT smooths the BTC regime axis only; alt_context held at its recorded value.
- CLOSED_REGIME_CHANGE auto-closes (majority of forwarded exits) carry no TP/SL label and are excluded from the win-rate — they are themselves regime-driven, so SOFT would also change their timing (interaction not modelled here).

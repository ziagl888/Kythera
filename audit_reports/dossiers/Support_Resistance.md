# Dossier: Support Resistance

> Level retest with RSI divergence — **grade B−** (Report 16) · **only net-positive classic strategy** (Σ **+596**), the SHORT side carries everything. Core verdict: "best rescue candidate" — expand it, merge Main Channel into it, but P0.7 (LONG TPs below entry) is confirmed live and must be fixed immediately.

## 1. Profile
- **Module:** `strategies/strat_support_resistance.py`, runner `3_detectors.py`, monitoring `5_trade_monitor.py`.
- **Signal logic:** repeated test of an S/R level + RSI divergence between the first and current hit + OBV confirmation → reversal entry; targets from real structure zones, fixed 2.5% SL. The only classic idea with genuine selection logic — the low signal frequency (1,917 vs. 111k for FIFO) shows that the filters actually filter.
- **Channel:** own Cornix trading channel via `telegram_outbox` (whitelist raw name "Support Resistance"). Caution: logically identical to Main Channel → one event produces two nearly identical leveraged signals in two channels (hidden double exposure).
- **Cooldowns:** no per-coin cooldown (classic family in general).

## 2. Live balance (Report 14, deduplicated, `closed_trades_master`)
- **n = 1,917** · WR **63.5%** · ø **+0.41%**/trade · median **0.00** · Σ net **+596** price-%.
- **Direction split:** SHORT (+0.66% ø) carries the **entire** profit — consider a direction gate. Monthly trend not reported separately.
- **Scoring caveat (Report 17):** monitor scoring agrees with the first-touch replay **67%** of the time for Support Resistance (fleet 63.4%) — better than 5 Percent/Volume, but every third trade classification is wrong; the +596 is monitor-generated and not reliable before the monitor rewrite.

## 3. Findings
| ID | Severity | One-liner | Status |
|---|---|---|---|
| P0.7 | **P0** | Empty-zone interpolation produces LONG TPs BELOW the entry (t1==0 unguarded → TP1 = 0.75·entry; SHORT −25/−50/−75%) | **✔** (Step 2: 5 active + 79 closed LONG trades with target1 ≤ entry) |
| P1.15 | High | One bad coin kills the whole detector process | ~ |
| P2.44 | Medium | 538 serial Binance HTTP calls per detector cycle | ~ |
| R1/05 | High | Scores the still-forming candle; the engine stamps at :02 AND :32 | ✔ (Step 2) |
| 05 | Medium | OBV divergence filter is statistically near-meaningless (N-candle sum vs. 1-candle 2σ band) — gate is decorative | ~ |
| 05 | Medium | Fixed 2.5% SL ignores coin volatility | ~ |
| 05 | Context | Duplicate strategy of Main Channel → double exposure | ~ |

## 4. Dependencies & cross-cutting risks
- **R1 forming candle** (Step 2 proven): hit/divergence detection on partial candles; additionally, the stored SUPPORT/RESISTANCE_PRICE histories are mixed-vintage (scalar broadcast, P1.12 ✔) — "previous-candle level" semantics don't hold.
- **R3 TZ mix:** naive timestamps, mixed semantics (Step 2 proven).
- **Monitor bugs P1.2/P2.7:** trailing SL never tightens, only the most recent 5m candle is checked → 67% replay agreement; the balance can flip after re-score (both error classes ~18%, net bias moderate).
- **Outbox losses (N2):** 800 messages silently dropped (no SR-specific figure reported); whitelist raw name "Support Resistance" frozen since 19.04 (P0.4/P2.25).

## 5. Remediation plan
- **Immediately:** **P0.7 fix** (`if t1 == 0: return None` or fixed-% fallback) — money-critical, confirmed live; clean up the 5 active corrupted trades. P1.15 per-coin isolation.
- **Structurally (Report 16: "best rescue candidate"):** closed-candle discipline (R1), **adopt Main Channel's ATR SL** instead of the fixed 2.5%, replace or drop the OBV component (√N scaling), **merge Main Channel in** (end the double exposure), consider **S1 direction gate** (SHORT carries everything — check/throttle the LONG side). After the monitor rewrite, re-score, then expand this one deliberately as the only classic (Report 16 §8: "expand Support Resistance as the only one").

## 6. Evidence
- `AUDIT_TODO.md`: P0.7 (✔ Step 2), P1.15, P2.44, R1, R3
- `audit_reports/STEP2_DB_VERIFICATION.md` §C: P0.7 = 5 active + 79 closed
- `audit_reports/05_classic_strats.md`: empty-zone interpolation, OBV filter, MC≡SR duplicate, fixed SL
- `audit_reports/14_bot_performance_db.md` §C: figure line, SHORT carries it
- `audit_reports/16_strategy_concept_evaluation.md` §3: grade B−, rescue verdict
- `audit_reports/15_strategy_proposals.md`: S1 direction gates
- `audit_reports/17_monitor_replay_and_gaps.md` §1: agreement 67%

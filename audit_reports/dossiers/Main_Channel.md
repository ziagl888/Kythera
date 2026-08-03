# Dossier: Main Channel

> Functional duplicate of Support Resistance on a 38-coin whitelist — **grade C−** (Report 16) · Σ **−77** at only n=202 (≈ 0). Core verdict: "merge into Support Resistance (carry over the ATR-SL as an improvement), don't run separately" — shares the live-confirmed P0.7 bug.

## 1. Profile
- **Module:** `strategies/strat_main_channel.py`, runner `3_detectors.py`, monitoring `5_trade_monitor.py`.
- **Signal logic:** logically **identical to Support Resistance** (same hit/divergence/OBV logic: repeated level test + RSI divergence + OBV confirmation), just on a 38-coin whitelist and with **ATR-SL instead of fixed SL**.
- **Channel:** its own Cornix trading channel via `telegram_outbox` — the same market event thus generates two nearly identical leveraged signals in two channels (hidden double exposure instead of diversification).
- **Cooldowns:** no per-coin cooldown (classic family in general).

## 2. Live balance (Report 14, deduplicated, `closed_trades_master`)
- **n = 202** · WR **67.3%** · avg **−0.28%**/trade · median **0.00** · Σ net **−77** price-% — small, ≈ 0.
- Direction split and monthly trend not reported separately at this n.
- **Scoring caveat (Report 17):** the replay did not report a **strategy-specific agree% figure** for Main Channel (n too small in the 388-sample); the fleet value of only **63.4%** monitor↔replay agreement applies (17.8% missed TP1, 18.8% TP1 despite SL-first) — at n=202 the balance is doubly uncertain (small n × unreliable scoring).

## 3. Findings
| ID | Severity | One-liner | Status |
|---|---|---|---|
| P0.7 | **P0** | Empty-zone interpolation produces LONG TPs BELOW entry (`strat_main_channel.py:70-87,115-132`, t1==0 unguarded → TP1 = 0.75·entry) | **✔** (Step 2: 5 active + 79 closed LONG trades with target1 ≤ entry, MC+SR combined) |
| P1.15 | High | One bad coin kills the entire detector process | ~ |
| P2.44 | Medium | 538 serial Binance HTTP calls per detector cycle | ~ |
| R1/05 | High | Evaluates the still-forming candle; engine stamps :02 AND :32 | ✔ (Step 2) |
| 05 | Medium | OBV divergence filter statistically near-meaningless (gate decorative) | ~ |
| 05/16b | Context | Duplicate of Support Resistance → double exposure in two Cornix channels | ~ |

## 4. Dependencies & cross-cutting risks
- **R1 forming candle** (Step 2 proven): level/divergence detection on partial candles; stored S/R level histories are also mixed-vintage (P1.12 ✔ — scalar broadcast).
- **R3 TZ mix:** naive time columns with mixed UTC/local semantics (Step 2 proven).
- **Monitor bugs P1.2/P2.7:** trailing SL never tightens, only the most recent 5m candle is checked — at n=202, the sign of the balance can flip from scoring errors alone.
- **Outbox losses (N2):** 800 messages silently dropped (no MC-specific figure reported); whitelist raw-name rows frozen since 19.04 (P0.4/P2.25) → orchestrator gating of the channel-fallback bots runs on 2.5-month-old statistics.

## 5. Remediation plan
- **Immediate:** **P0.7 fix** (`if t1 == 0: return None`) — money-critical, live-confirmed; clean up corrupt active trades. P1.15 per-coin isolation.
- **Structural:** **merge into Support Resistance** (Report 16: "merge instead of running both") — carry the ATR-SL over as an improvement into the merged strategy, discontinue the separate channel/operation, and thereby end the hidden double exposure. Further remediation (closed-candle, OBV replacement, S1 direction gate) then runs through the Support Resistance dossier; a standalone continuation of Main Channel is not justifiable at n=202 and Σ −77.

## 6. Evidence
- `AUDIT_TODO.md`: P0.7 (✔ Step 2), P1.15, P2.44, R1, R3
- `audit_reports/STEP2_DB_VERIFICATION.md` §C: P0.7 = 5 active + 79 closed
- `audit_reports/05_classic_strats.md`: empty-zone interpolation, MC≡SR (cross-cutting #3), OBV filter
- `audit_reports/14_bot_performance_db.md` §C: Main Channel figures row
- `audit_reports/16_strategy_concept_evaluation.md` §3: grade C−, merge verdict
- `audit_reports/17_monitor_replay_and_gaps.md` §1: fleet agree 63.4%

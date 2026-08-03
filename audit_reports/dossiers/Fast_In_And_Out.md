# Dossier: Fast In And Out (FIFO)

> Momentum scalper without an edge hypothesis — **grade F** (Report 16) · biggest loss contributor of the entire fleet (Σ **−25,843** price-% net). Core verdict: "not rescuable — there's no selection that bugfixes could uncover. Shut it down" — the only alternative: an S11 filter model in front of it.

## 1. Profile
- **Module:** `strategies/strat_fast_in_out.py`, runner `3_detectors.py`, monitoring `5_trade_monitor.py`.
- **Signal logic:** three conditions on 30m — RSI_9 between 55–75, EMA9>EMA21, 5% "headroom" to resistance — one TP at +1.25%. Effectively the definition of "it's currently going up"; matches hundreds of coins on every upward drift (111,387 trades).
- **Channel:** own Cornix trading channel via `telegram_outbox` (whitelist raw name "Fast In And Out").
- **Cooldowns:** no per-coin cooldown; only a global win-count circuit breaker (400/500 wins in 3–4h across ALL coins — practically a dead guard, perversely throttling after wins, never after losses), also TZ-broken (P2.1: 3h window only covers 1–2h in CEST).

## 2. Live balance (Report 14, deduplicated, `closed_trades_master`)
- **n = 111,387** · WR **60.6%** · ø **−0.13%**/trade · median **+1.25%** · Σ net **−25,843** price-%.
- Pattern: median positive, ø negative → rare but huge loss tails; the classic family's abs>50% outliers concentrate here ("pennies in front of the steamroller"). Direction split not reported separately. Monthly trend not reported separately.
- **Important:** E6 (Report 15) — a loss cap at −3% improves ø by only 0.02pp → the problem is **selection, not outlier tails**.
- **Scoring caveat (Report 17):** monitor scoring agrees with the first-touch replay only **73%** of the time for FIFO (fleet overall 63.4%; 17.8% missed TP1, 18.8% TP1 despite SL-first) — per-trade truth is of limited reliability; additionally **212 dropped outbox messages in the FIFO trading channel** (lost signals/SL updates without an alert).

## 3. Findings
| ID | Severity | One-liner | Status |
|---|---|---|---|
| P1.14 | High | SHORT "headroom" check is a sign-flipped no-op (`close > support*0.95` instead of `*1.05`) → SHORT without a guard | ~ (code, [DB] open) |
| P1.15 | High | One bad coin kills the whole detector process (strategy calls unprotected) | ~ |
| P2.1 | Medium | Cooldown circuit breaker compares naive local time against UTC `posted` → window shrinks | ~ ([DB]) |
| P2.44 | Medium | 538 serial Binance HTTP calls per detector cycle (before every check) | ~ |
| R1/05 | High | Scores the still-forming 30m candle (forming candle); the engine stamps at :02 AND :32 | ✔ (Step 2) |
| 05 | Context | Global win cooldown 400/500 practically dead; no per-coin cooldown | ~ |
| 16b | Concept | No edge hypothesis; payoff structurally negative | ✔ (live figures) |

## 4. Dependencies & cross-cutting risks
- **R1 forming candle** (Step 2 proven): signals on ~2-minute-old partial candles, at :32 on a 1h candle open for 32 minutes.
- **R3 TZ mix** (session TZ Europe/Bucharest, naive columns mixing UTC/local) → P2.1 live-relevant.
- **Monitor bugs P1.2/P2.7:** trailing SL never tightens; only the most recent 5m candle is checked → all FIFO KPIs monitor-distorted (replay agreement only 73%).
- **Outbox losses (N2):** 212 of the 800 silently dropped messages affected the FIFO channel; also md5-identical messages fired 2–3× within 60 min (detector refire, Step 2 P0.1).
- **Stale whitelist (P0.4/P2.25):** orchestrator gates "Fast In And Out" on raw-name statistics frozen since 19.04.

## 5. Remediation plan
- **Immediately:** shut down (portfolio recommendation Report 16, section 8: "Stop: … Fast In And Out"). −25.8k Σ net with no edge hypothesis doesn't justify continued operation.
- **Structurally (if continued operation is desired):** **S11 "FIFO filter model"** (Report 15) — a meta-classifier before posting based on the 111k labelled trades (largest dataset in-house); even +0.3pp ø improvement flips the strategy from −25.8k to positive. Prerequisite: monitor rewrite (Report 17) for clean labels + V1–V3 (R1 fix, dedup, first-touch simulator). Plus exit redesign S13 (make TP/SL geometry fee-positive, otherwise switch off) and P1.14/P2.1 fixes.

## 6. Evidence
- `AUDIT_TODO.md`: P1.14, P1.15, P2.1, P2.44, R1, R3
- `audit_reports/05_classic_strats.md`: forming candle, headroom no-op, cooldown anatomy (cross-cutting #2)
- `audit_reports/14_bot_performance_db.md` §C: FIFO figure line
- `audit_reports/16_strategy_concept_evaluation.md` §3: grade F, verdict
- `audit_reports/15_strategy_proposals.md`: E6, S11, S13
- `audit_reports/17_monitor_replay_and_gaps.md` §1–2: agreement 73%, 212 outbox losses

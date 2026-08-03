# Dossier: 5 Percent

> Fake confluence from ~26 redundant filters — **grade D** (Report 16) · textbook "win ≠ profit": **71.1% WR and still Σ −5,766** net. Core verdict: without a redesign of entry timing and exits, no positive expectancy can be justified; at most the LONG side as an experiment.

## 1. Profile
- **Module:** `strategies/strat_5_percent.py`, runner `3_detectors.py`, monitoring `5_trade_monitor.py`.
- **Signal logic:** ~26 AND conditions (RSI band, TSI, complete EMA/WMA/KAMA alignment, MACD, Donchian/Boll mid). The confluence is fake: almost all conditions are smoothings of the same close price and collapse to "an established, steep trend" → systematically late entry into overextended moves; fixed % targets, no time exit.
- **Channel:** own Cornix trading channel via `telegram_outbox` (whitelist raw name "5 Percent").
- **Cooldowns:** no per-coin cooldown; only a global win-count circuit breaker (500 wins across all coins — practically dead, throttles after wins instead of losses), TZ-broken (P2.1).

## 2. Live balance (Report 14, deduplicated, `closed_trades_master`)
- **n = 19,385** · WR **71.1%** · ø **−0.20%**/trade · median **−0.05%** · Σ net **−5,766** price-%.
- Highest "win rate" of the classic family and clearly negative — TP1 touch counts as a win, then trailing/SL gives it all back, fees eat the rest.
- **Direction split:** LONG side 76% WR at n=1,087 — worth investigating, but n too small for confidence (Report 14 D.5). Monthly trend not reported separately.
- **Scoring caveat (Report 17):** monitor scoring agrees with the first-touch replay only **45%** of the time for 5 Percent — the per-trade scoring is **de facto noise** here; all figures above (including the 71% WR and the LONG split) are correspondingly unreliable until the monitor rewrite + re-score has run.

## 3. Findings
| ID | Severity | One-liner | Status |
|---|---|---|---|
| P1.14 | High | SHORT "headroom" check is a sign-flipped no-op (`close > support*0.95`) → SHORT without a guard | ~ (code, [DB] open) |
| P2.43 | Medium | SHORT uses `ema_12 < ema_55` where LONG uses `ema_21 > ema_55` (likely a typo); `REQUIRED_COLUMNS` doesn't cover `ema_200/wma_21/wma_26` (latent silent-never-fire) | ~ |
| P2.1 | Medium | Cooldown circuit breaker compares naive local time against UTC `posted` | ~ ([DB]) |
| P1.15 | High | One bad coin kills the whole detector process | ~ |
| P2.44 | Medium | 538 serial Binance HTTP calls per detector cycle | ~ |
| R1/05 | High | Scores the still-forming candle; the engine stamps at :02 AND :32 | ✔ (Step 2) |
| 16b | Concept | Fake confluence, late entry, fixed targets, no regime awareness | ✔ (live figures) |

## 4. Dependencies & cross-cutting risks
- **R1 forming candle** (Step 2 proven): 26 conditions are evaluated on partial candles.
- **R3 TZ mix:** session TZ Europe/Bucharest → P2.1 live-relevant (3h window really 1–2h).
- **Monitor bugs P1.2/P2.7:** trailing SL never tightens, only the most recent 5m candle is checked — for 5 Percent, with only 45% replay agreement, this is the most severe consequence: the strategy currently cannot be seriously evaluated.
- **Outbox losses (N2):** 800 messages silently dropped (no 5-Percent-specific figure reported); whitelist raw name "5 Percent" frozen since 19.04 (P0.4/P2.25) → orchestrator gating on statistics 2.5 months stale.

## 5. Remediation plan
- **Immediately:** P1.14 fix (`close > support*1.05`) + P2.43 typo/`REQUIRED_COLUMNS` fix + P2.1 TZ fix; P1.15 per-coin try/except in the detector. Close the SHORT side until re-evaluation or park the strategy.
- **Structurally:** monitor rewrite first (Report 17) + re-score, then re-evaluate — before that, every decision is built on 45%-noise labels. After that: **S1 direction gate** (keep only the LONG side running as an experiment, Report 16 §8: "5 Percent only as an experiment on the LONG side"), exit redesign per S13 (fee-positive TP/SL geometry), otherwise switch off. The S11 filter pattern is in principle transferable, but FIFO and Volume Indicator take priority (more data).

## 6. Evidence
- `AUDIT_TODO.md`: P1.14, P1.15, P2.1, P2.43, P2.44, R1, R3
- `audit_reports/05_classic_strats.md`: headroom no-op, EMA typo, REQUIRED_COLUMNS, cooldown anatomy
- `audit_reports/14_bot_performance_db.md` §C + D.5: figure line, LONG split n=1,087
- `audit_reports/16_strategy_concept_evaluation.md` §3: grade D, verdict
- `audit_reports/15_strategy_proposals.md`: S1 direction gates, S13
- `audit_reports/17_monitor_replay_and_gaps.md` §1: agreement 45%

# Dossier: BTC SMC (100x)

> Rule-based BTC SMC strategy with the best-crafted setup design in the SMC family — and a mathematically broken 100x leverage design. **Grade (16): D (F as-is).** Core verdict: **P0.5** — at 100x isolated, liquidation sits at ~−0.9%, *before* every 0.4–1.2% SL; every stop = −100% margin. As-is, the bot is a liquidation generator; with a leverage fix + an honest walk-forward it's the most worth-checking rule-based SMC bot.

## 1. Profile

| | |
|---|---|
| Bot | `21_btc_smc_strategy.py` — rule-based, no ML, no trainer |
| Market/TF | BTC; 1h signals (double-signal window "1h apart", P2.46) |
| Leverage | **`DESIRED_LEVERAGE = 100x`** (21:31-35) at 0.4–1.2% SL (21:199, 238) — **P0** |
| Setup quality | positively highlighted (Report 16): FVG age caps (MAX_FVG_AGE=48), trend filter, R:R check, SL validation; the only bot in the family that correctly drops the last (forming) candle (`iloc[:-1]`) |
| Parameter origin | in-sample grid search (never validated out-of-sample) |
| Tracking | **none** — writes no `ai_signals`, doesn't appear in any performance statistic |

## 2. Live balance

**None.** Report 16 (ranking #21): untracked — one of the three unmeasured bots (16/17/21), no valid backtest. The only "balance" is arithmetic: at 100x isolated, every position gets liquidated at ~−0.9%, before the SL (0.4–1.2%) triggers; even the 0.4% floor means −40% margin per stop. The bot's R:R check ignores leverage.

## 3. Findings

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| **P0.5** | Bot | **P0** | 100x leverage with 0.4–1.2% SL → liquidation ~−0.9% *before* the SL; every stop = −100% margin; R:R check ignores leverage | ✔ (code, `21:31-35,199,238`) |
| P2.46 | Bot | MEDIUM | No cooldown/dedupe anywhere in the file: unconditional `iloc[:-1]` + DB write lag → the same trigger candle signals twice, 1h apart | ✔ (code, `21:121-123,264`) |
| 16-concept | Concept | MEDIUM | Parameters from in-sample grid search; never an honest walk-forward | ✔ (concept review) |
| 16-meta | Process | MEDIUM | Completely unmeasured (no `ai_signals`, no performance statistic) | ✔ |
| 08-positive | Bot | — | Validates the SL side (unlike 16), correctly drops the last candle, age caps/trend filter/R:R check present — "best setup design in the SMC family" | ✔ |

## 4. Dependencies & cross-cutting risks

- **R4 (leverage vs. SL, core root cause):** 21 is, alongside UFI1 (P0.6), the namesake of the R4 finding "leverage-vs-SL never cross-checked anywhere". Fix class: central `core/trade_utils.py: cap_leverage_to_sl(sl_pct)` (e.g. `lev ≤ 0.5/sl_pct`), used by all signal-emitting bots — closes P0.5, P0.6 and the ROM1 SL distances (P2.27) in one go (Report 16, recommendation 8.4).
- **R1:** not affected — 21 is the reference implementation for correct closed-candle handling in the family.
- No tracking → the monitor caveat (Report 17) is moot; but also no evidence of any edge.

## 5. Remediation plan

**Immediately (P0, above all else):** cap leverage — `lev ≤ 0.5/sl_pct` or `DESIRED_LEVERAGE ≤ 25` (fix from P0.5/R4). Until that's deployed, the bot must not touch capital.

**Rule fixes:** standard `check_cooldown/update_cooldown` or dedupe on trigger `open_time` (P2.46); retrofit instrumentation (`ai_signals`).

**Validation instead of retrain:** honest walk-forward (V3 simulator from Report 15) instead of in-sample grid search — per Report 16, 21 is, *after* the leverage fix, "the most worth-checking rule-based SMC bot".

**Open questions:** historical double signals ~1h apart in the channel (08, DB question 8) never evaluated; real signal frequency unknown.

## 6. Evidence

- `AUDIT_TODO.md` P0.5, P2.46, R4
- `audit_reports/08_smc_bots.md` (section 21_btc_smc_strategy.py + cross-cutting 1/5)
- `audit_reports/16_strategy_concept_evaluation.md` (section 6, ranking #21; cross-cutting findings 5+6; recommendation 8.4/8.5)

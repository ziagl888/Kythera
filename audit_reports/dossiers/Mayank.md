# Dossier: Mayank

> Rule-based FVG bot ("FVG fully closed" as entry). **Note (16): D.** Core verdict: implemented more consistently than Bot 16 (closed-candle discipline), but the entry concept is itself a *devalued* level in SMC doctrine — a knife-catch at the old gap floor; completely unmeasured, harmless as an info channel, unratable as a strategy.

## 1. Profile

| | |
|---|---|
| Bot | `17_mayank_bot.py` — rule-based, no ML, no trainer |
| Signal logic | FVG retest: "FVG fully closed" triggers entry; no age limit for gaps |
| Channel | CH_MAYANK |
| Leverage | not quantified in the sources; no R4 finding against 17 |
| Tracking | **none** — writes no `ai_signals`, doesn't appear in any performance statistics |

## 2. Live track record

**None.** Report 16 (ranking #20): n = untracked, WR = ?, Σ = ? — "unmeasurable". One of the three unmeasured bots (16/17/21): no tracking, no valid backtest. Consequence from Report 16, cross-cutting finding 6 / recommendation 8.5: instrument or shut down.

## 3. Findings

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| P2.45a | Bot | MEDIUM | Static-data refire after cooldown expiry (weekends) — like Bot 16: the same old signal is reposted as soon as the cooldown expires | ✔ (Code) |
| P2.45b | Bot | MEDIUM | No FVG age limit — month-old gaps generate "retest" signals; oldest-first break (Bot 21 caps MAX_FVG_AGE=48) | ✔ (Code) |
| 08-LOW | Bot | LOW | Three separate pool connections per signal | ✔ (Code) |
| 08-LOW | Bot | LOW | 2h/4h resample in exchange local time before UTC conversion (shared with 16) | ✔ (Code) |
| 16-Concept | Concept | — | "FVG fully closed" as entry conceptually shaky: a fully filled gap is considered devalued in SMC doctrine | ✔ (Concept review) |
| P3.8 | Bot | LOW | matplotlib without an `Agg` backend → headless crash risk (17/24/25 affected) | ✔ (Code) |

Positive (08, cross-cutting): 17 is classified as "implemented more consistently (closed candle)" — the R1 repaint finding affecting its neighbours 16/24/25 doesn't apply to it in the same way.

## 4. Dependencies & cross-cutting risks

- **R1:** the DB candle contract is interpreted inconsistently fleet-wide; 17 is one of the cleaner consumers, but would also benefit from a shared `fetch_closed_candles()`.
- **R4:** no specific finding, but the family has no leverage-vs-SL reconciliation anywhere — a central `cap_leverage_to_sl()` should include 17 too.
- No tracking → no monitor distortion (Report 17), but also zero evidence; the whitelist/analyzer don't know the bot.

## 5. Remediation plan

**Immediate:** decide instrument vs. shut down (Report 16: unmeasured strategies have no claim to returns). As a pure info channel without Cornix execution, it would be "harmless" per Report 16.

**Rule fixes (if kept):** adopt Bot 21's MAX_FVG_AGE=48 window, newest-first instead of oldest-first (P2.45b); freshness gate or trigger-candle timestamp in the cooldown key (P2.45a); one pool connection per signal; `Agg` backend (P3.8); resample after UTC.

**Open questions:** weekend timestamps in CH_MAYANK (08, DB question 8) never evaluated; real signal quality unknown for lack of tracking.

## 6. Evidence

- `AUDIT_TODO.md` P2.45, P3.8
- `audit_reports/08_smc_bots.md` (section 17_mayank_bot.py + cross-cutting)
- `audit_reports/16_strategy_concept_evaluation.md` (section 6, ranking #20; cross-cutting finding 6, recommendation 8.5)

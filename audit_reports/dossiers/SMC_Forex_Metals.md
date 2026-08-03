# Dossier: SMC Forex/Metals

> Rule-based SMC bot (structure break + FVG) on forex/metals symbols. **Note (16): D−.** Core verdict: "retail SMC folklore + repaint entries + SL without sanity check" — completely unmeasured (no `ai_signals`, no valid backtest) → shutdown candidate; if kept, instrument first.

## 1. Fact sheet

| | |
|---|---|
| Bot | `16_smc_forex_metals_bot.py` — rule-based, no ML, no trainer |
| Signals/TF | STRUCTURE (BOS) + FVG mitigation; cooldown modules SMC_1H/2H/4H/1D_FVG occupied; 2h/4h via resample, plus 1d/1w |
| Data sources | DB **and** yfinance (both with the forming-candle problem) |
| Channel | CH_SMC_FOREX; the only bot in the family that embeds the Cornix block in **one** message (no P3.9 double-parse risk) |
| Leverage | "20x-10x" is posted without an SL-distance check (SL = last swing low, can be 20–30% away or even beyond entry) |
| Tracking | **none** — writes no `ai_signals`, doesn't appear in any performance statistic |

## 2. Live track record

**None.** Bot 16 is one of the three completely unmeasured bots (16/17/21, report 16 cross-cutting finding 6): n, WR, PnL unknown; no valid backtest exists. Report 16: "What can't be measured has no claim to returns in a bot fleet: instrument it or shut it down." The only live footprint in the sources: 83 `SMC_*_FVG` cooldown rows (step 2) — so the bot does actually fire.

## 3. Findings

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| P1.26 | Bot | — | "FVG entry is unreachable dead code" | **✘ REFUTED** (step 2: 83 SMC_1H/2H/4H/1D_FVG cooldown rows — the FVG path fires; claim wrong or applied to an older code version) |
| P1.27 | Bot | HIGH | Decisions on the forming candle in **both** data sources (DB doesn't drop the running candle; yfinance "fix" deliberately keeps the in-progress row); forming 1d/1w holds the condition for days → 12h cooldown → refires all week | ✔ (code) |
| P2.45a | Bot | MEDIUM | Weekend/stale-data refire: forex closes Fri 22:00, bot scans all weekend, the same Friday signal reposts with the Friday price into the closed market | ✔ (code) |
| P2.45b | Bot | MEDIUM | No SL side/RR sanity check on BOS: SL can be 20–30% away or beyond entry; "20x-10x" is posted anyway (21 validated, 16 not) | ✔ (code) |
| 08-LOW | Bot | LOW | 2h/4h resample in exchange local time before UTC conversion → buckets shifted against Binance, DST shift (also in 17) | ✔ (code) |
| P2.45c | Infra | — | Do the METALS tables even exist? | ✔ cleared (step 2: XAU/XAG/XAUT/PAXG tables exist in full) |
| P3.8 | Bot | — | matplotlib backend | ✔ ok — 16 is the **only** bot in the family that sets `Agg` |

## 4. Dependencies & cross-cutting risks

- **R1 (forming candle):** 16 misinterprets the DB candle contract (treats the last row as live) — part of the bug class a shared `fetch_closed_candles()` would close (08, cross-cutting 1).
- **R4 (leverage vs. SL):** no reconciliation; the posted 10–20x against 20–30% SLs is the same defect class as P0.5/P0.6 — a central `cap_leverage_to_sl()` would cover 16 too.
- No monitor/DB tracking → the monitor caveat (report 17) doesn't even apply here: there simply are **no** numbers at all, neither skewed nor honest.

## 5. Remediation plan

**Immediately:** make a decision — **shut down** (report 16 recommendation: "shutdown candidate") or instrument (`ai_signals` writes like the AI fleet). Until then no capital allocation is justifiable.

**If kept (rule fixes, no retrain needed):** `iloc[:-1]` for DB, drop partial rows/buckets for yfinance, cooldown ≥ candle duration (P1.27); Sat/Sun skip + freshness gate (P2.45a); ATR/% cap + reject `sl ≥ entry` and leverage derived from SL distance (P2.45b/R4); resample after UTC conversion (LOW).

**Open questions:** weekend timestamps in CH_SMC_FOREX (08, DB question 8) never evaluated; real signal frequency/outcome unknown for lack of tracking.

## 6. Evidence

- `AUDIT_TODO.md` P1.26 (✘), P1.27, P2.45, P3.8
- `audit_reports/08_smc_bots.md` (section 16_smc_forex_metals_bot.py + cross-cutting)
- `audit_reports/STEP2_DB_VERIFICATION.md` (P1.26 refuted; XAU tables exist)
- `audit_reports/16_strategy_concept_evaluation.md` (section 6, ranking #22; cross-cutting finding 6)

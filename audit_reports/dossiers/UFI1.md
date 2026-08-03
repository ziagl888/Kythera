# Dossier: UFI1 (Fibonacci dead-cat-bounce short)

> Rule-based daily Fibonacci short on dumped coins (no ML) — **grade F** (Report 16, rank 26/26): worst model in the fleet per trade (25.7% WR, −7.90% avg), backtest claim "+278R" invalidated by look-ahead, 20x leverage at ~34% SL distance mathematically unviable. Core verdict: **conceptually dead — stop.**

## 1. Fact sheet

| Field | Content |
|---|---|
| Bot | `29_ufi1_bot.py` |
| ML model | **none** — pure Fibonacci rule set |
| Rule set | Dump detection → Fib retracement of the dump → "candle closes below Fib level" as daily confirmation → SHORT. Entry = CMP (~0.77·swing_high), `sl = swing_high·1.03` (~34% distance), single TP1. Note: the "0.382" level in the code is actually the **61.8% retracement** of the dump (labelling convention, `29:109-111,48`); "rejection" is accepted without the level ever being touched (±2% is enough); 48h cooldown → refires aged setups, confirmation candle can be ~2 weeks old |
| Leverage | **20x** — at ~34% SL distance, isolated liquidation happens at ~+5%, long before the SL (P0.6, R4) |
| "Backtest" | `fib_backtest.py` — claims 54.2% WR / +278R / +0.83R avg. Invalidated by: (1) entry choice via the **future global window low** (argmin over the full 30-bar window = look-ahead), (2) 5-target trailing ladder in the backtest vs. single TP1 live, (3) live entries stale by up to weeks at CMP |
| Channel | Cornix channel; posts a plain Cornix block AND a second HTML message with an identical block (double-parse risk P3.9); logging style positive (ERROR+exc_info+rollback — held up as a model per Report 08) |

## 2. Live results (active era, deduplicated; Report 14/Step 2)¹

- **n = 35 · WR 25.7% · avg −7.90%/trade · median −3.22% · Σ net −280 price-%** — "catastrophic (confirms P0.11)"; vs. advertised 54.2% WR.
- Direction split: N/A (short-only strategy). Calibration: N/A (no model). Monthly trend: not reported at n=35.
- **Leverage not factored in:** −7.9% avg at 20x would be liquidation (Report 14) — real account losses are a multiple of the price-%.
- Report 16: "The fact that UFI1 imploded exactly as the look-ahead analysis predicted conversely validates the audit methodology."

¹ *Monitor caveat (Report 17): figures are monitor-generated (overall replay agreement only 63.4%); AI trades cannot be replayed retroactively (N4). This changes nothing about the F verdict — both error classes (missed/false TP1) occur at ~18% each, and the gap to 54.2% is orders of magnitude larger.*

## 3. Findings (consolidated)

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| P0.6 | Bot/risk | P0 | 20x leverage with ~34% SL (`29:194,244`) → isolated liquidation ~+5% **before** the SL; the "+0.83R" does not survive 20x | ✔ (mathematically; R4) |
| P0.11 | Backtest | P0 | The "+278R" claim does not transfer to the live bot: look-ahead entry (future window low), trailing ladder vs. single TP1, stale CMP entries | ✔✔ (Step 2: live 25.7% WR, n=35) |
| 11-H1 | Backtest | High | `fib_backtest.py:252-262,327-336`: argmin over the full window picks entries with future knowledge → live bot sees a different trade population, flatter Fib anchors | ✔ (code-documented) |
| 11-H2 | Bot | High | No recency check on the confirmation candle → week-old setups fire at CMP anywhere in [tp1·1.02, sl) → WR/R arbitrary; refire after 48h + ai_signals clear = trade-count inflation (`29:177-226,241,363`) | ✔ (code-documented) |
| 08-H | Bot | High | "Candle closes below Fib" can be evaluated on the **still-forming daily candle** (j reaches n−1) — an intraday dip counts as a confirmed daily rejection (`29:177-193,66-88`) | ✔ (proven live, R1) |
| 08-M1 | Bot | Medium | Fib level mislabelling (0.382 = 61.8% retracement) — internally consistent ONLY if the backtest uses the same formula; document/confirm | ~ (open) |
| 08-M2 | Bot | Medium | Aged setups refire every 48h; stale corridor is wide; ai_signals only blocks while open (monitor deletes rows) | ✔ (code-documented) |
| 08-L | Bot | Low | "Rejection" accepted without the level ever being touched (close within ±2% is enough) | ✔ |
| P3.9 | Telegram | P3 | Plain Cornix block + HTML duplicate into the same channel → double-parse/double-execution risk | ~ (open, [DB]) |

## 4. Dependencies & cross-cutting risks

- **R4 (leverage vs. SL never reconciled) — the killer:** UFI1 is, alongside BTC SMC (100x/1.2% SL), the main case; **liquidation happens long before the SL**, the SL is never actually reached. The central fix `cap_leverage_to_sl(sl_pct)` in `core/trade_utils` closes the whole class (also ROM1 SL distances p90=17.9%).
- **R1 (forming candle):** confirmation on the running daily candle; bot 29 "mixes" the candle contract (Report 08).
- **P0.10 pattern ("backtest the detector, trade something else"):** fib_backtest is one of the three documented cases of the dominant pattern from Report 11 — none of the published WR/R figures describe the system that actually trades.
- Target population = freshly dumped altcoins with 50% squeeze candles — exactly the assets where a 25–40% SL + 20x is maximally toxic (Report 16).

## 5. Remediation plan

**a) Immediate:**
1. **Stop** — unanimous recommendation from Report 14 (D.3), Report 16 (grade F, "stop immediately", section 8.1). Alternatively, if continued operation is forced: **leverage cap** (derive leverage from SL distance, ~1–2x, hard ≤3x; P0.6/R4) — without it, every single trade is a liquidation candidate.
2. If continued regardless: `j ≤ n−2` (confirmation only on closed daily candles), freshness gate (confirmation within the last 1–2 daily candles), setup-keyed cooldown.

**b) Retrain/rebuild:**
- No retrain (no model). A new backtest would only be valid as a **walk-forward with `find_ufi1_setup`** (replay of the bot's own setup function bar-by-bar, exit model = single TP1, fees) — Report 16: "no reason to believe a correct version would be positive"; the validating evidence itself is invalidated. Rebuild effort is therefore not justified.

**c) Open questions:**
- Confirm the Fib convention (0.382↔61.8%) against `fib_backtest.py` — only relevant if an honest re-backtest is ever run.
- P3.9: check Cornix double-parse in the outbox data (also affects bots 24/25).

## 6. Evidence

- `AUDIT_TODO.md` P0.6, P0.11 (+Step-2 annotations), R4, P3.9 · `audit_reports/08_smc_bots.md` (29_ufi1 findings) · `audit_reports/11_ml_backtest.md` (fib_backtest critique, cross-cutting 1) · `audit_reports/14_bot_performance_db.md` (n=35, 25.7% WR, −7.90% avg, −280 net) · `audit_reports/STEP2_DB_VERIFICATION.md` (P0.11 ✔) · `audit_reports/16_strategy_concept_evaluation.md` (grade F, section 6) · `audit_reports/17_monitor_replay_and_gaps.md` (monitor caveat, N4).

# Dossier: ROM1 / Regime Orchestrator (meta level)

> **Regime-driven signal gate + its own trading bot in one:** the detector classifies the BTC regime, the analyzer builds a bot×regime whitelist from it, the orchestrator gates all fleet signals and re-posts the winners as its own ROM1 trades.
> **Grade (Report 16b): C+** — concept B / implementation D+ / live effect positive.
> **Core verdict:** ROM1 delivers measurable added value (+8pp WR over the fleet, +2,184 net, positive median) — but **despite**, not **because of**, its whitelist: the 4D gate is 89% default-open, gates partly on data frozen for 2.5 months, and the added value plausibly stems from cooldown, opposite-block, coarse negative selection and its own S/R trade construction. The specific 4D hypothesis ("bot X works in regime Y") is **untested**.

## 1. Fact sheet

| | |
|---|---|
| **Components** | `26_regime_detector.py` (BTC regime every 5 min, 15m returns/ATR, adaptive P75/P40 thresholds, 2-check debounce) · `27_bot_regime_analyzer.py` (outcome attribution → whitelist) · `28_signal_orchestrator.py` (gate + ROM1 forwarding + regime auto-close) · `core/regime_logic.py` (classification/debounce logic) |
| **"Model"** | No ML artifact, but the **`bot_regime_whitelist` statistic**: 4D matrix bot × regime × alt context × direction × 3 time windows = 4,056 cells; gate rule `wr_bot ≥ wr_overall` (point estimate, no significance test/shrinkage) |
| **Data flow** | `regime_history` (raw) → debounce → `regime_current` · analyzer → `bot_regime_performance` → `bot_regime_whitelist` · orchestrator scans `telegram_outbox` → `identify_bot` → whitelist gate → forward as ROM1 (**own geometry:** CMP entry, S/R SL, up to 20 targets) → `orchestrator_open_trades`; suppressions → `orchestrator_suppressed_signals`; on regime change, auto-close all open trades on coin+direction |
| **Tables** | `regime_history`, `regime_current`, `bot_regime_performance`, `bot_regime_whitelist`, `orchestrator_open_trades`, `orchestrator_suppressed_signals`, `telegram_outbox` |
| **Channel** | regime trading channel `CH_REGIME_TRADING` (−1003963430969) |

## 2. Live results (Step 2 + Report 14/16, deduplicated, net −0.10% fee, unleveraged)

- **ROM1: n=2,677, WR 69.2% vs. 61.1% fleet (+8pp), avg +0.92%/trade, median +1.00% (nearly the only model with a positive median), Σ +2,184 net.**
- **Lifecycle tight:** 0 OPEN trades older than 7 days; 4,339 closes since 18.04.; the gate genuinely engages: **gate rate 44.7% (Apr) → 63.5% (Jun)** — but for the MIS family + channel bots, the June rate is still based on April statistics (P0.4).
- **Whitelist hollow:** **89% default-open** — fresh rows: 747× `insufficient_data`, only 41× `wr_above_overall` + 52× `wr_below_overall` data-based (11% genuine decisions); **median 7 trades/cell**, 68% of cells <30. Historically, still 3,043 `wr_below_overall` suppressions (partly on stale data).
- **Detector:** only **7 TREND episodes in 5.5 months** (5× TREND_DOWN, 2× TREND_UP, all <1h) — distribution **TRANSITION 44.5% / HIGH_VOLA 29.7% / CHOP 25.8%**; **52% of all raw episodes are <1h flaps** (654/1,257), median duration CHOP/TRANSITION 0.9h; confidence median 0.54, p10 0.40; 2.9 raw changes/day, 17.2% of 2h windows with ≥2 regimes.
- **Auto-close cuts blind:** 3,653 REGIME_CHANGE closes, **median PnL 0.00%, 49.3% cut while in profit**; 35% of all ROM1 trades (1,411/4,339) end via regime close instead of TP/SL; AIM1 is cut at an average of **+9.5%**.
- **ROM1 risk:** SL distance median 7.9%, **p90=17.9%, max 65.3%**; 20/133 signals >15% → at 20x, beyond liquidation (P2.27).
- **Interpretation caveat (16b):** The +8pp is optimistically biased (P1.9 censoring, WR metric, foreign outcomes via P1.8) — the true added value is likely positive (net PnL + median support that independently), but smaller than the headline.

## 3. Findings

Status: ✔ = proven live/DB · ~ = code finding, not (fully) quantified live · ✘ = mitigated/not observed live.

| ID | Component | Severity | One-liner | Status |
|---|---|---|---|---|
| P0.3 / B7 | 28 | CRITICAL | Orchestrator consumes its own ROM1 posts (self-echo): **109 rows** from its own channel; only the 4h cooldown (committed only AFTER send) prevents double trades — the crash window remains | ✔ |
| P0.4 / B5 / P2.25 | 28↔27 | CRITICAL | Bot-name mismatch (pretty_name missing in the orchestrator): raw-name rows **frozen since 19.04.** → MIS family + channel bots gate on stats that are 2.5 months stale | ✔ |
| B1 | 27/whitelist | HIGH | Whitelist **89% default-open** (747× `insufficient_data` vs. 93 data-based decisions) | ✔ |
| B2 | 27/whitelist | HIGH | 4D matrix statistically underpopulated: 4,056 cells, **median 7 trades/cell**, 68% <30 | ✔ |
| B3 | 26 | HIGH | Detector effectively knows only 3 regimes — TREND classes dead (7 episodes/5.5 months, all <1h); structural flaw: trend requires *low* vola, mid-vola band always falls into TRANSITION | ✔ |
| B4 | 26 | HIGH | 52% of raw episodes are <1h flaps; confidence p10=0.40 — the detector often guesses | ✔ |
| B6 | 28/auto-close | HIGH | Auto-close cuts blind: median 0.00% PnL, 49.3% cut in profit, 35% of all ROM1 trades end this way → churn, fees, censored statistics | ✔ |
| B8 | 28 | MEDIUM | Forwards logged without `wl_reason` → not measurable which gate path makes money | ✔ |
| B9 | 27↔28 | HIGH | **Circularity:** the analyzer learns from outcomes the orchestrator itself censors (B6/P1.9) → whitelist WRs systematically flattered | ~ |
| B10 | 28/identify_bot | MEDIUM | 841 `bot_unidentified` suppressions (third-largest reason) — patterns don't cover the signal stream | ✔ |
| P1.6 | 28 | HIGH | `sent=FALSE` filter races the dispatcher → signals silently never gated, no log | ~ |
| P1.7 | 28 | HIGH | Forward pipeline not atomic, batch cursor at pass end → fired-but-untracked / batch replay | ~ |
| P1.8 | 28 | HIGH | `sync_closed_trades` matches foreign trades (no model filter, 720h window) → wrong ROM1 outcomes, opposite protection lapses prematurely | ~ |
| P1.9 | 28 | HIGH | Regime close deletes open trades of **all** bots on coin+direction → foreign losses censored as neutral, whitelist WR biased upward | ~ |
| P1.10 | Docs↔28 | HIGH | Spec drift: docs say "pure signal router", code builds its own trades → gating statistics ≠ execution statistics | ✔ |
| P2.21 | 28/market_utils | MEDIUM | TZ mix: 4h cooldown effectively 6h, 60s window becomes 2h (R3 proven live: DB TZ Europe/Bucharest) | ~ |
| P2.22 | 27 | MEDIUM | Training/serving skew: attribution on RAW `regime_history`, gating on debounced `regime_current` + backfill look-ahead | ~ |
| P2.23 | 28 | MEDIUM | "Unreliable" heuristic counts RAW flaps → overall fallback dominates (TRANSITION 44.5%, 256 `regime_is_transition` suppressions) | ✔ |
| P2.24 | 28 | MEDIUM | Regime changes during downtime never caught up (in-memory state) | ~ |
| P2.26 | 28 | MEDIUM | No same-direction open check (stacking possible after cooldown) — currently **no** stacked duplicates observed live | ✘ |
| P2.27 | 28/ROM1 | MEDIUM | ROM1 SL has no distance cap: p90=17.9%, max 65.3%, 20/133 >15% → at 20x, beyond liquidation (R4) | ✔ |
| P2.28 | 28 | MEDIUM | 60s window + start_delay=175 → every restart silently discards ≥3 min of signal flow | ~ |

## 4. Dependencies & cross-cutting risks

- **Circularity (B9):** whitelist statistic ← monitor outcomes ← orchestrator auto-close. Three co-directional upward biases sit on exactly the number the money gate hangs on: open-trade censoring, regime-change closes removed as "neutral" instead of realized, foreign-trade censoring (P1.9).
- **Monitor-label caveat (Report 17):** monitor scoring agrees with the first-touch replay only **63.4%** of the time (~18% each missed and falsely awarded TP1) → the per-trade truth that the analyzer and whitelist compute on is unreliable. The monitor rewrite therefore comes BEFORE any whitelist hardening.
- **Wrong gate metric:** the gate optimizes WR (TP1 touch), which Report 14 proves to be misleading (67% WR can be net negative); avg_pnl/median/sharpe_like are already in the table and are ignored.
- **Geometry break (P1.10):** the whitelist is built on trades with original parameters, ROM1 executes its own geometry — the statistical chain is broken.
- **Expectation after fixes (16b):** the P0.4 fix brings a moderate additional gain (the TRANSITION fallback remains); the P1.9 fix will **lower** the measured WRs — intended, communicate in advance.

## 5. Remediation plan (4 stages from Report 16, condensed)

1. **Stage 1 — repair (days):** P0.4 (`pretty_name()` after `identify_bot()`, purge April rows, `computed_at` staleness gate >48h, default-open alarm) · P0.3 (channel filter in the scan SELECT, ROM1 hard-reject, commit cooldown BEFORE send; P1.7: transaction first, outbox last) · P1.9 (auto-close only `model='ROM1'`; P1.8: sync with model filter ±60s; P1.6: id cursor) · B10 (align identify_bot patterns against the 841) · B8 (`wl_reason` column).
2. **Stage 2 — make the statistics honest (weeks):** replace the 4D matrix with **hierarchical shrinkage** (empirical Bayes, lower Wilson bound > break-even) · censoring correction: regime closes with PnL-at-close entered into the statistics (B9) · **suppressed counterfactual scorer** ✔(tooling T-2026-CU-9050-047: `tools/rom1_counterfactual.py`, scores both sides per gate path via first-touch replay; run requires the VPS) — makes the gate's value measurable on an ongoing basis for the first time (unknown today).
3. **Stage 3 — evolve detector & gating:** TREND features (EMA slope persistence, ADX), adaptive hysteresis against the 52% flaps, `UNKNOWN` instead of guessing · split TRANSITION or a **transition resolution model S10** (TRANSITION trades run a good 63–64% WR — the regime is tradeable, the fallback is too coarse) · **regime direction matrix S2** as a coarse second gate layer (CHOP→SHORT only, longs there −3.69%/trade; HIGH_VOLA→drop LONGs) · differentiate auto-close (trail winners instead of cutting them — 49% cut in profit, AIM1 at +9.5%) · ROM1 geometry: pass through the original or SL cap + `cap_leverage_to_sl` (P2.27/R4).
4. **Stage 4 — operational robustness:** startup reconcile of open trades against the whitelist (P2.24), detection window 5–10 min + `stale_signal` log (P2.28), fallback/default-open rate as a health metric in the status post.

**Priority (Report 16):** stage 1 complete → counterfactual scorer (no. 8) ✔tooling(T-2026-CU-9050-047) → shrinkage + S2 matrix → stage 3 per data availability. The biggest lever is three **concept changes**, not bug fixes: split TRANSITION, gate metric WR→net expectancy, run ROM1 as its own bot with its own evidence layer (16b). Target grade after P0/P1 fixes + concept changes: **B**.

## 6. Evidence

- `audit_reports/16_regime_orchestrator_analysis.md` — main source: B1–B10, 4-stage plan, target picture (Step 6)
- `audit_reports/04_orchestrator_regime.md` — code findings 26/27/28/regime_logic + cross-cutting
- `audit_reports/STEP2_DB_VERIFICATION.md` — P0.3 (109 self-echoes), P0.4 (frozen since 19.04.), P2.23 (44.5% TRANSITION), P2.27 (SL distances), ROM1 69.2% vs. 61.1%
- `audit_reports/14_bot_performance_db.md` — n=2,677, +2,184 net, median +1.00
- `audit_reports/16_strategy_concept_evaluation.md` — grade C+, section 7 (three readings of the +8pp)
- `audit_reports/15_strategy_proposals.md` — E2/E8, S2 regime direction matrix, S10 transition model
- `audit_reports/17_monitor_replay_and_gaps.md` — monitor-label caveat (63.4%)
- `AUDIT_TODO.md` — P0.2–P0.4, P1.6–P1.10, P2.21–P2.28 with Step-2 annotations

# 16 — Regime orchestrator: overall analysis & improvement proposals (Step 6)

**As of:** 2026-07-03 · Consolidates code audit (Report 04), live DB evidence (Step 2), performance (Report 14), hypotheses (Report 15) + a new empirical evaluation of the overall system (`step6_orchestrator.py`).

## 1. Architecture (as-is)

```
26_regime_detector ──5min──▶ regime_history (raw) ──debounce──▶ regime_current
                                                                      │
27_bot_regime_analyzer ──▶ bot_regime_performance ──▶ bot_regime_whitelist
   (Outcome-Attribution)      (Bot×Regime×Alt×Dir           │
                               ×3 Fenster, 4.056 Zellen)    ▼
28_signal_orchestrator: telegram_outbox scannen ▶ identify_bot ▶ Whitelist-Gate
   ▶ Forward als ROM1 (eigene Geometrie!) ▶ orchestrator_open_trades
   ▶ bei Regime-Wechsel: Auto-Close ALLER offenen Trades auf Coin+Richtung
```

## 2. Empirical stocktake — does the core feature actually work?

**What demonstrably works:**
- **ROM1 delivers value:** 69.2% WR vs 61.1% fleet (+8pp), +0.92%/trade, +2,184 net (n=2,677) — despite all the bugs.
- The lifecycle is tight: 0 OPEN trades older than 7 days; the pipeline runs stably (4,339 closes since 18.04.).
- The gate actually bites: gate rate 44.7% (Apr) → 63.5% (Jun); cooldown and opposite-direction guards fire.

**What is demonstrably broken or hollow:**

| # | Finding | Number |
|---|---|---|
| B1 | **The whitelist is 89% default-open.** Fresh rows: 747× `insufficient_data` (=wave through), only 41× `wr_above_overall` + 52× `wr_below_overall` data-based | 11% real decisions |
| B2 | **Data basis too thin for the 4D matrix:** 4,056 cells, median **7 trades/cell**, 68% under 30 | matrix too fine |
| B3 | **Detector knows practically only 3 regimes:** over 5.5 months, 5× TREND_DOWN and 2× TREND_UP episodes (all <1h!); distribution TRANSITION 44.5% / HIGH_VOLA 29.7% / CHOP 25.8% | TREND classes dead |
| B4 | **52% of all raw episodes are <1h flaps** (654/1,257); median duration CHOP/TRANSITION 0.9h; confidence median 0.54, p10 0.40 | detector flaps |
| B5 | **The gate decides on data 2.5 months stale** for the MIS family + channel bots (P0.4 raw names, frozen 19.04.) — June's gate rate of 63.5% is based there on April statistics | proven (Step 2) |
| B6 | **Auto-close cuts blindly:** 3,653 REGIME_CHANGE closes, median PnL **0.00%**, 49.3% cut while in profit; 35% of all ROM1 trades (1,411/4,339) end via regime close instead of TP/SL → churn + fees + censored statistics (P1.9: affects ALL bots, not just ROM1) | AIM1 gets cut at an average of **+9.5%** |
| B7 | **Self-echo exists:** 109 suppressions originate from the regime channel itself (86 of them as `bot_unidentified`) — so far only cooldown prevents worse (P0.3) | proven |
| B8 | **Forwards are not logged with a reason** (no `wl_reason` in `orchestrator_open_trades`) → one cannot measure which gate path actually earns money | measurement gap |
| B9 | **Circularity:** the analyzer learns from outcomes that the orchestrator itself censors (B6/P1.9) — regime-change losses are sorted out as "neutral" → whitelist WRs are systematically flattered | structural |
| B10 | `bot_unidentified` = 841 suppressions total (third-largest reason) — identify_bot patterns do not cover the signal stream | pattern gaps |

**Assessment:** the system makes money **despite**, not **because of**, its whitelist: at 89% default-open + stale raw rows, what mainly works de facto is (a) the 4h cooldown, (b) the opposite-direction guard, and (c) the coarse fallback heuristic. The actual 4D core (bot×regime selection) is statistically understaffed and partly frozen.

## 3. Improvement proposals

### Stage 1 — Repair (known bugs, days; references = AUDIT_TODO)
1. **P0.4 fix:** `pretty_name()` right after `identify_bot()` + purge the April raw rows + a `computed_at` staleness gate (>48h old ⇒ cell counts as `insufficient_data`), alarm on the default-open rate.
2. **P0.3 fix:** `channel_id != REGIME_TRADING_CHANNEL_ID` in the scan SELECT + ROM1 hard-reject; commit cooldown/tracking BEFORE the send (P1.7: transaction first, outbox insert last, cursor per row).
3. **P1.9 fix:** auto-close only `model='ROM1'`; `sync_closed_trades` with a model filter + ±60s match (P1.8); replace the `sent=FALSE` race with an id cursor (P1.6).
4. **B10:** extend the identify_bot patterns against the 841 `bot_unidentified` (candidates are in the suppressed log).
5. **B8:** add a `wl_reason` column to `orchestrator_open_trades` — from then on every forward is justified and evaluable.

### Stage 2 — Make the statistics honest (weeks)
6. **Replace the 4D matrix with hierarchical shrinkage.** A median of 7 trades/cell carries no decision. Proposal: empirical Bayes — the cell WR shrinks toward the parent mean (bot overall → regime×direction → fleet), weight ∝ n. Effect: no more `insufficient_data` binary crutch, every cell delivers a usable, conservative estimate; gate threshold on the shrinkage WR with a confidence interval (e.g. lower Wilson bound > break-even).
7. **Censorship correction (B9):** don't discard regime-closed trades, but include them in the statistics with PnL at close time (median 0% → neutral effect, but no more selection bias).
8. **Suppressed counterfactual scorer (new, high leverage):** a job that, for every suppression (the outbox row is linked!), recomputes the hypothetical outcome via the first-touch simulator and writes it to `orchestrator_suppressed_signals`. This makes the gate value **continuously measurable**: "suppressions saved X% / cost Y%". Today the benefit of the core feature is simply unknown — that ends every discussion with data.

### Stage 3 — Advance the detector & gating logic
9. **Detector revision (B3/B4):** (a) the TREND classes need their own features (EMA slope persistence, ADX, higher-highs count) — currently they never reach the debounce threshold; (b) flap rate 52% ⇒ extend hysteresis or make the confirmation counter adaptive (short in HIGH_VOLA, long in CHOP); (c) calibrate confidence (p10=0.40 means: it's often guessing) and explicitly report `UNKNOWN` below threshold instead of guessing CHOP.
10. **Discover TRANSITION instead of just enduring it:** 44.5% of the time is "transition" — either resolve the detector more finely, or build the **transition-resolution model (S10, Report 15)** that predicts the resolution direction. The Step-5 data shows: TRANSITION trades are good (63-64% WR) — the regime is tradeable, the fallback just handles it too coarsely.
11. **Regime-direction matrix as a second gate layer (S2):** coarse (regime×direction, 6-15 cells with hundreds of trades) instead of/before the fine bot matrix: CHOP→SHORT only (longs −3.69%/trade), HIGH_VOLA→drop LONGs. Robust, immediately data-viable.
12. **Differentiate the auto-close policy (B6):** instead of market-closing all positions on a change: (a) winners: SL to break-even/trail instead of close (49% are currently cut while in profit, AIM1 at +9.5%!); (b) losers: close as before; (c) counterfactual scorer (no. 8) measures whether the policy actually saves money.
13. **ROM1 geometry:** pass through the original signal geometry (or at least an SL-distance cap + `cap_leverage_to_sl`, R4/P2.27) — currently the gating statistics measure different trades than the source bots ever posted (document P1.10 spec drift).

### Stage 4 — Operational robustness
14. Startup reconcile: after downtime, check all OPEN trades against the current whitelist (P2.24); detection window 5-10 min instead of 60s + a `stale_signal` log (P2.28); regime status posts with fallback rate + default-open rate as a health metric (P3.10).

## 4. Target picture (compact)

> A detector with honest uncertainty (UNKNOWN instead of guessing, working TREND classes), a two-stage gate (coarse regime×direction matrix → fine per shrinkage bot score), forwards and suppressions both logged with a reason AND counterfactual outcome, auto-close that trails winners instead of cutting them — and ROM1 that respects the source signals' geometry. Every component measures itself; the whitelist can never silently freeze again, because staleness and the default-open rate raise alarms.

**Priority:** stage 1 complete (bugfixes, ~days) → no. 8 counterfactual scorer (makes everything else measurable) → no. 6+11 (statistics) → stage 3 depending on the data situation.

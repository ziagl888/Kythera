# 15 — Proposal list: new strategies & models (Step 5, concept)

**As of:** 2026-07-03 · **Basis:** Realized results (Report 14), calibration measurements (Step 2), trainer audit (Report 13), and four targeted hypothesis tests against the live DB (confluence, regime conditioning, AIM1 fade, FIFO tail anatomy; script `step5_hypotheses.py`).

**Empirical building blocks the proposals rest on:**

| # | Finding (deduplicated, active era) | Number |
|---|---|---|
| E1 | Directional asymmetries are large and stable | EPD1 SHORT 76.5% vs LONG 50.2% WR; RUB1 SHORT 63.9% vs LONG 48.7%; BR1H LONG 65.5% vs SHORT 49.5% |
| E2 | Regime conditions direction (orchestrator `regime_at_open`, n=719) | CHOP: SHORT 66.4%/+1.98% vs LONG 59.4%/**−3.69%**; HIGH_VOLA: LONG only 48.4%; TRANSITION: both ~63-64% positive |
| E3 | Confluence: exactly 2 independent models on coin+direction within 4h | LONG: 65.7% WR/+1.40% (vs 61.4% solo); **4+ models = contra signal: 51% WR, −1.0%** (crowding/vol event) |
| E4 | Genuinely calibrated models exist | TD_1H: 78.5% WR @conf>0.9; SRA1, MIS1-8H, QM_1H positively calibrated |
| E5 | AIM1 is systematically inverted | conf 0.9–0.95: **8.3% WR, −9.53%/trade (n=19,295)**; but conf>0.95 flips to 85% WR (n=267) |
| E6 | FIFO losses are broad, not tail-heavy | Loss cap −3% improves the average by only 0.02pp → the problem is selection, not outliers |
| E7 | Profits live in the tails | Median PnL ≈ 0 almost everywhere; sums come from p95 (MIS1-72H, RUB1) |
| E8 | 44.5% of the time is TRANSITION regime | Orchestrator runs on the coarse fallback then |

**Caveats:** Everything is monitor-generated (P1.2/P2.7/P1.9), unleveraged, regime data only since ~April, H2/H3 cells partly small n. Every proposal must pass through the first-touch simulator (P0.10 fix) and a shadow phase (`ml_predictions_master`, posted=false — the infrastructure already exists!) before real money is attached.

---

## Preconditions (foundation — without this, every new strategy is built on sand)

- **V1:** R1 fix (closed-candle contract) — otherwise new models inherit the look-ahead.
- **V2:** Dedup index on `closed_ai_signals` + purge (Report 14 A1) — otherwise training labels are contaminated.
- **V3:** Shared **walk-forward first-touch simulator** (= P0.10/X-R1 fix): a library that replays, bar by bar, the actually posted order geometry (entry1/entry2, SL, targets, trailing) for every setup. It is simultaneously the label source for new models AND the backtest engine for new strategies.
- **V4:** TZ fix (R3), so that time-window features are correct.

---

## Tier 1 — Meta-strategies without new ML (days, use existing signals)

### S1 — "Direction-Gated Portfolio": trim existing bots to their profitable side
- **Evidence:** E1. **Concept:** Configurable direction gates in the orchestrator/per bot: EPD1 SHORT only, RUB1 SHORT only, BR1H LONG only, check the 5-percent LONG side (n small). **Expectation:** raises fleet WR by several points, costs nothing except signal volume. **Risk:** the asymmetry could be regime-dependent → re-validate gates monthly against rolling windows. **Implementation:** rule set + config table, 1–2 days.

### S2 — "Regime-Direction Matrix" (orchestrator 2.0)
- **Evidence:** E2. **Concept:** After the P0.4 whitelist fix, a second gate layer: `CHOP → nur SHORT` (longs there −3.7%/trade!), `HIGH_VOLA → keine LONGs (außer explizit whitelisted)`, `TRANSITION → beide Seiten zulassen` (against intuition the best zone, E8 makes it the largest). Matrix over `regime × alt_context × direction`, data-driven from `orchestrator_open_trades` outcomes, with minimum n and confidence interval instead of point estimate. **Expectation:** replaces the broken per-bot whitelist logic with a more robust, coarser gate with more data per cell. **Risk:** n=719 is still thin → 4–6 weeks of shadow parallel run, cells only go live from n≥100.

### S3 — "Confluence-2 Booster + Crowding Abstain"
- **Evidence:** E3. **Concept:** Signal router counts distinct models per (coin, direction, 4h window): at **exactly 2–3** → increase/prioritize position size; at **≥4** → suppress (or log as a contra observation). This is a pure counter in the orchestrator — no model needed. **Expectation:** LONG confluence-2 ran +1.40%/trade at 65.7% WR. **Risk:** window/threshold were found on the same data → validate out-of-time separately on May–Jul (split available).

### S4 — "Calibration-Sized Positions"
- **Evidence:** E4. **Concept:** For the models proven calibrated (TD_1H, SRA1, MIS1-8H, QM_1H), position size ∝ (calibrated prob − break-even prob) (fractional Kelly, hard-capped); uncalibrated models get a uniform size. Calibration via isotonic on a rolling out-of-time window, monthly refresh. **Expectation:** shifts capital to where confidence is real information (TD_1H@>0.9 = 78.5% WR). **Risk:** calibration drifts → auto-degrade to uniform size if the reliability curve breaks.

### S5 — "AIM1 Fade" (SHADOW EXPERIMENT ONLY)
- **Evidence:** E5. **Concept:** Invert AIM1 signals with conf 0.85–0.95 (LONG signal → SHORT candidate). On paper this would have been ~+9.5%/trade before costs over 19k observations — one of the strongest "edges" in the dataset. **BUT:** the inversion is an out-of-distribution artifact (Report 13) — it can vanish or flip with any data drift (conf>0.95 already wins 85%!). **Therefore:** write exclusively as a shadow strategy into `ml_predictions_master`, observe for 8+ weeks, never go live blind. Realistic use: AIM1-high-conf as a **veto feature** for other bots (if AIM1 says >0.85 in one direction, hands off that direction).

---

## Tier 2 — New models on existing data (weeks, need V1–V3)

### S6 — "Pump-Exhaustion-Short" (EPD1 successor, short-only)
- **Evidence:** E1 (EPD1 SHORT 76.5%/+3.3%), Report 13 B-1 (gate bug). **Concept:** Dedicated short model on pump exhaustion: training samples ONLY at `vol_ratio ≥ 5` (gate mirrored live!), microstructure features from `ticker_10s` (buy_pressure drop, volume decay, spike age), label = first touch of the real SR-based short geometry. Drop the long side entirely. **Why promising:** the existing EPD1 earns money despite a broken query; a cleanly gated short-only model on the same data is the most obvious improvement using existing infrastructure (`pump_dump_events`, ticker buffer).

### S7 — "AIM2": build the meta-model right
- **Evidence:** Report 13 (all root causes known), E4. **Concept:** Retrain the master meta-model with: current vocabulary from DB DISTINCT (not hardcoded), floor−1 join, label = first touch of the real geometry, **regime features from `regime_history`** (did not exist in 2025 — today the most obvious missing predictor), source-model calibration score as a feature, chronological 3-way split, isotonic calibration, reindex parity guard. **Role:** not an independent trader, but a **ranker/sizer** over all source signals (replaces the S4 heuristic long term).

### S8 — "Funding-Extreme Mean-Reversion"
- **Evidence:** Funding data has been gap-free since February (`funding_data/funding_history_*.json`, logger running); P2.40 showed that the current 75% threshold fires even in the normal state — i.e. the signal is unused. **Concept:** Cross-sectional: coins in the top funding percentile (≥95th, overheated longs) SHORT, bottom percentile LONG, hold until funding normalization or time stop; optionally only in matching regime (CHOP/TRANSITION). **Why promising:** classic, economically grounded edge (carry + crowding unwind), a completely new signal source orthogonal to the existing fleet. **First:** backtest on the 4 months of funding history via the V3 simulator.

### S9 — "Cross-Sectional Long/Short Basket" (portfolio instead of individual signals)
- **Evidence:** 529 coins × full OHLCV+indicator history in the DB — used cross-sectionally by no existing strategy; E7 (tail profits) argues for portfolio approaches. **Concept:** Daily/4h rebalancing: rank all coins by momentum (e.g. 7d return, ADX) or reversal score (distance to MA, RSI extreme), top decile LONG / bottom decile SHORT, **delta-neutral** → market direction irrelevant, earns on dispersion. **Special:** needs a portfolio executor (n positions simultaneously, rebalancing) instead of the signal-by-signal Cornix flow — a bigger rebuild, but the data situation for it is already perfect.

### S10 — "Transition-Resolution Model"
- **Evidence:** E8 (44.5% TRANSITION), E2 (TRANSITION trades are good!). **Concept:** Small model that runs ONLY in the TRANSITION regime and predicts the resolution direction (→BULL_TREND/BEAR_TREND/CHOP) from `regime_history` raw features (btc_return_1h/4h, atr, btcdom, confidence trajectories); the output gates the direction of all other bots during the transition. **Why promising:** it directly addresses the biggest weakness of the regime system (P2.23: fallback dominates) in the time window that makes up almost half of all time — and the target data (next stable regime) is trivially labelable from `regime_history`.

### S11 — "FIFO Filter Model" (selection instead of new signals)
- **Evidence:** E6 — FIFO has 111k labeled trades, median +1.25%, average −0.13%; the problem is selection, not tails. **Concept:** Meta-classifier that, BEFORE posting a fast-in-and-out signal, estimates the win probability from entry-time features (regime, direction, confluence counter, RSI/ATR/volume context, coin liquidity class); only lets the top-X% through. **Why promising:** the largest labeled dataset in-house, a clearly defined question, and even a +0.3pp average improvement flips the strategy from −25.8k to positive. Same pattern transferable afterward to Volume Indicator (51k trades).

---

## Tier 3 — After infrastructure fixes

- **S12 — Whale-Flow Confirmation:** After the P1.42 fix (sharding, logger dead since 18.4.): whale netflow as a confirmation feature for S6/S11. Data quality first, then model.
- **S13 — Exit Redesign "Tail Harvesting":** E7: for tail carriers (MIS1-72H, RUB1) test runner exits (small TP1 to cover costs, rest with chandelier trail) — in the V3 simulator against the current exits, before any live exit is changed. For classic (FIFO/5-Percent) the reverse: set TP/SL geometry so the average becomes positive after fees, otherwise switch off.

---

## Recommended order

1. **V1–V3** (foundation; the V3 simulator is the key to everything).
2. **S1 + S3** (pure config/router rules, immediately out-of-time validatable) → first low-risk improvement.
3. Start **S2** in shadow operation (simultaneously collects the data that makes the matrix robust).
4. **S11** (FIFO filter) as the first new model — best data/effort ratio.
5. **S6** (Pump-Exhaustion-Short) and **S8** (funding) in parallel as new alpha sources.
6. **S7** (AIM2) once V3 is in place; **S9/S10** afterward; **S5** runs the whole time only as a shadow logger.

Every candidate goes through: simulator backtest (V3, walk-forward, fees) → 4–8 weeks shadow (`ml_predictions_master`) → calibration/reliability check → small live sizing → scaling. Define abort criteria up front (e.g. shadow-WR-CI falls below break-even).

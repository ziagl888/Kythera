# 16 — Strategy & ML Model Concept Evaluation (Step 8, Supplementary Analysis)

**As of:** 2026-07-03 · **Method:** 5 parallel concept reviews (classic strategies, pump/dump ML family, AI bots SRA1/ATB1/AIM1/ABR1, SMC/pattern family + UFI1, meta-level regime/orchestrator/intelligence). Each strategy was assessed on three levels: **concept** (is the edge hypothesis plausible?), **training/implementation validity** (evidence from Reports 01–13), and **live evidence** (realized figures from Report 14, deduplicated, net of −0.10% round-trip fee, without leverage).

**Grading scale:** A = clear, evidenced edge · B = plausible edge with positive live evidence · C = viable concept, evidence missing/thin · D = concept or implementation structurally questionable · F = conceptually dead or reliably harmful.

---

## 1. Overall Ranking (all strategies/models)

| # | Strategy / Model | Source | n (live) | WR | Σ net | Grade | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | **MIS1-72H** | 11 | 11.822 | 63.9% | **+15.868** | **B−** | workhorse; retrain priority #1 (no provenance!) |
| 2 | **Three-Drive TD_1H/4H** | 25 | 2.794 | 57.3% | +2.387 | **B−** | only well-calibrated ML family; keep + retrain cleanly |
| 3 | **SRA1** | 9 | 396 | 69.9% | +134 | **B−** | conceptually cleanest ML setup (meta-labeling); small but healthy |
| 4 | **Support Resistance** | strategies/ | 1.917 | 63.5% | +596 | **B−** | only net-positive classic strategy; SHORT carries everything |
| 5 | **ROM1 / Orchestrator** | 28 | 2.677 | 69.2% | +2.184 | **C+*** | +8pp WR uplift despite degraded gate; architecture carries it |
| 6 | **EPD1** | 10 | 4.392 | 72.8% | +14.222 | **C+** | best edge narrative, but OOD serving + regime-dependent (July negative) |
| 7 | **ATS1** | 12 | 1.768 | 65.8% | +1.622 | **C+** | architectural blueprint (event gate); OBV skew inverts confidence |
| 8 | **ABR1** | 18 | 110 | 63.6% | +335 | **C−** | solid concept, but only 7/18 features actually active; n too small |
| 9 | **MIS1-168H** | 11 | 7.167 | 58.5% | +6.928 | **C−** | drifting since May (WR 48/49/35); keep only with retrain + monitoring |
| 10 | **Breaker Block BB_4H** | 25 | 2.162 | 61.2% | +565 | **C−** | concept best supported (S/R flip), fix feature skew |
| 11 | **Main Channel** | strategies/ | 202 | 67.3% | −77 | **C−** | duplicate of Support Resistance; merge instead of running both |
| 12 | **RUB1** | 13 | 2.496 | 57.6% | +3.675 | **D+** | ML layer is noise (MACD break); profit = pre-filter + SHORT tails |
| 13 | **QM_1H** | 24 | 3.139 | 67.5% | −139 | **D+** | 67% WR and still ≈0 — exit geometry gives it all back |
| 14 | **Volume Indicator** | strategies/ | 51.440 | 64.1% | −705 | **D+** | small genuine core, degenerated HVN mechanics, fee generator |
| 15 | **MIS1-8H/24H** | 11 | 1.003 | ~52% | +1.261 | **D** | horizon/feature mismatch; better to drop in the retrain |
| 16 | **ATB1** | 14 | 306 | 65.7% | −172 | **D** | model never saw the event it scores; rebuild or park |
| 17 | **BR family (BR1H/2H/4H)** | 7 | 11.756 | 58–60% | **−4.106** | **D** | Break & Retest without ML gate; close BR1H SHORT immediately |
| 18 | **BB_1H** | 25 | 3.909 | 55.7% | −1.089 | **D** | 1h edge doesn't survive fees + noise; park until retrain |
| 19 | **5 Percent** | strategies/ | 19.385 | 71.1% | −5.766 | **D** | false confluence (26 redundant filters), late entry |
| 20 | **Mayank** | 17 | untracked | ? | ? | **D** | unmeasurable; "FVG fully closed" as entry is conceptually shaky |
| 21 | **BTC SMC 100x** | 21 | untracked | ? | ? | **D (F as-is)** | best SMC setup design, but the 100x design = liquidation generator |
| 22 | **SMC Forex/Metals** | 16 | untracked | ? | ? | **D−** | unvalidated + repaint + no tracking; shutdown candidate |
| 23 | **QM_4H** | 24 | 556 | 54.9% | −277 | **F** | stop |
| 24 | **Fast In And Out** | strategies/ | 111.387 | 60.6% | **−25.843** | **F** | no edge hypothesis; biggest loss-maker in the fleet |
| 25 | **AIM1** | 15 | 3.047 | 50.8% | **−3.399** | **F** | reliably inverted (conf>0.9 → 9.3% WR); pause immediately |
| 26 | **UFI1** | 29 | 35 | 25.7% | −280 | **F** | backtest claim invalidated by look-ahead; conceptually dead |

\* The ROM1 grade is the overall meta-level: concept B / implementation D+ / live effect positive — details in Section 7.

---

## 2. Cross-Cutting Findings (apply across almost all strategies)

1. **Win rate is worthless as a KPI — and the system optimizes exactly for it.** All classic strategies sit above 60% WR and lose a combined −13.360; 5 Percent has 71% WR at −5.766, QM_1H 67.5% at −139. "Win" = TP1 touch, after which trailing/SL gives it all back and fees eat the rest. The winners' totals come from the tails (p95), not from WR. Consequence: the dashboard's primary metric and the whitelist gate must switch to **net expectancy/median** (see Report 14 D.7 and Section 7).

2. **Not a single ML model currently has demonstrable ML skill.** Every family breaks the contract between training label and traded order geometry at least once (X-R1 from Report 13: "backtest the detector, trade something else"): idealized fills (pivot close, limit at the level, fixed 1%/2% brackets) in training vs. CMP entry + `calculate_smart_targets` geometry live. The positive live totals plausibly come from **rule-based pre-filters + S/R-based TP/SL construction + a favourable market regime** — not from the models. The sole partial exception: TD_1H is empirically well-calibrated (Step 2), i.e. there the feature signal carries value despite the label bias.

3. **S/R-based trade construction is the secret star.** The four strategies with structure-based targets (Support Resistance, SRA1, ROM1, the MIS1 family via `calculate_smart_targets`) are the relative winners; ROM1 is almost the only model with a positive median (+1.00%). This suggests a substantial part of the fleet's alpha sits in the zone/target logic, not in signal selection.

4. **Direction asymmetries are the cheapest untapped lever.** EPD1 SHORT 76.5% vs. LONG 50.2% WR; RUB1 SHORT 63.9% vs. LONG 48.7%; BR1H LONG 65.5% vs. SHORT 49.5%; Support Resistance: SHORT carries the entire profit. Per-model direction gates are immediately actionable and need no retrain.

5. **Leverage vs. SL is reconciled nowhere (R4).** Two bots are mathematically unviable: BTC SMC (100x, liquidation ~−0.9% before every SL) and UFI1 (20x, liquidation ~+5% at ~34% SL distance); ROM1 SLs reach p90=17.9% distance at 20x. A central `cap_leverage_to_sl()` closes this whole class of bug.

6. **Three bots (16, 17, 21) are completely unmeasured** — they write no `ai_signals`, appear in no performance statistics, and have never seen a valid backtest. Whatever is unmeasurable has no claim to profit in a bot fleet: instrument it or shut it down.

7. **The intelligence layer is a display layer.** Whale and funding data — exactly the data classes that could refine a regime gate — are consumed by not a single decision-making component (and the whale logger has been dead since 18.04.). The only machine feedback loop runs through price/ATR and the bot's own trade history.

---

## 3. Classic Strategies (strategies/, runner `3_detectors.py`)

All 5 share systemic defects: evaluating the **still-forming candle** (R1), a practically dead global win cooldown (throttles after wins, never after losses), and the empty-zone target interpolation that can produce LONG TPs below entry (P0.7).

### Support Resistance — **B−** (Σ +596, only net-positive classic strategy)
**Concept:** repeated test of an S/R level + RSI divergence between first and current hit + OBV confirmation → reversal entry, targets from real structure zones. Theoretically plausible and the only classic idea with real selection logic — the low signal frequency (1.917 vs. 111k for FIFO) shows the filters actually filter.
**Weaknesses:** the OBV component is statistically almost inert (decorative); no regime awareness; the fixed 2.5%-SL ignores coin volatility; the SHORT side (+0.66% avg) carries the entire profit.
**Verdict:** the best rescue candidate. Fix the target interpolation, move to closed-candle evaluation, adopt the ATR SL from Main Channel, replace/drop OBV, consider a direction gate.

### Main Channel — **C−** (Σ −77, n=202)
Logically **identical to Support Resistance** (same hit/divergence/OBV logic), just on a 38-coin whitelist and with an ATR instead of a fixed SL. One event produces two nearly identical leveraged signals in two Cornix channels — hidden double exposure instead of diversification. **Verdict:** merge into Support Resistance (carry over the ATR SL as an improvement), don't run it separately.

### Volume Indicator — **D+** (Σ −705 net at gross +4.439 — fees eat it all)
**Concept:** price at a 90d high-volume node + a 3σ volume spike within the last 5 days determines direction. Volume-profile levels are legitimate, but: a spike up to **5 days old** as a direction signal for a 30m entry is hopelessly stale, and without a cooldown a historical event keeps re-firing every 30 min for days (51.440 trades — signal inflation by construction).
**Implementation degenerates:** HVN detection sums volume per exact float close → the gate measures tick size instead of volume structure; the spike logic picks the *oldest* spike, index-0 spike is always "Sell".
**Verdict:** the fact that a gross plus remains anyway points to a genuine small core (volume zones do work). Only rescuable via rebuild: binned HVNs, a freshness requirement, per-coin cooldown, structure-based targets.

### 5 Percent — **D** (Σ −5.766 at 71.1% WR — a textbook example of win ≠ profit)
**Concept:** ~26 AND conditions (RSI band, TSI, full EMA/WMA/KAMA alignment, MACD, Donchian/Boll mid). The confluence is illusory: almost all conditions are smoothings of the same close price and collapse into a single filter for "established, steep trend" → systematically **late** entries into exhausted moves. Fixed % targets, no time exit, no regime awareness. Plus the SHORT headroom no-op (P1.14) and an EMA typo (P2.43).
**Verdict:** with fixes possibly break-even (the LONG side's 76% WR at n=1.087 is worth investigating), but without a redesign of entry timing and exits no positive expectancy can be justified.

### Fast In And Out — **F** (Σ −25.843 — the biggest loss-maker in the entire fleet)
**Concept:** 3 conditions (RSI_9 55–75, EMA9>EMA21, 5% headroom) on 30m, one TP at +1.25%. That's not an edge hypothesis, it's the definition of "it's currently rising" — true for hundreds of coins in any upward drift (111.387 trades). Payoff structurally negative: median +1.25% (scalps "work" mechanically), but the rare losers are huge — the abs>50% outliers of the classic family concentrate here. Textbook "picking up pennies in front of the steamroller".
**Verdict:** not rescuable — there's no selection logic that bugfixes could uncover. Shut it down.

---

## 4. Pump/Dump ML Family (MIS1, ATS1, RUB1, EPD1)

**Family finding:** none of the four models has demonstrable ML skill; the positive totals come from rule gates + S/R TP/SL + market regime. Every family breaks X-R1 (label ≠ traded geometry) and delivers uncalibrated "confidence" values.

### MIS1 (8 models: {8h,24h,72h,168h}×{pump,dump}) — family **C+**, carried by 72H
**Concept:** a battery of binary XGBoost classifiers, hourly per coin: probability of a pump/dump in the respective horizon, from 67 1h indicator features. Best score wins (cross-horizon argmax), TP/SL from `calculate_smart_targets`. The horizon differentiation is the most interesting part — empirically, 1h features hit their sweet spot exactly at 72h.
**Concept flaws:** the label definition is **unknown** (no trainer exists — zero provenance, zero reproducibility, the only family without a trainer on the machine); argmax compares raw probabilities from differently calibrated models (P2.33); a global model across 538 coins with proven **ticker/price-class leakage** (`pct_distance` accident features in coin price scale, for 168h_pump even the top feature at 10.4% importance — the model has partly learned "which coin is this"). Plus P1.17: prediction on the still-forming candle with ~1/6 partial volume.
- **MIS1-72H: B−** — +15.868 net, positive every single month, the fleet's strongest workhorse. Deduction: a non-reproducible black-box artifact; nobody can say why it works, and nobody could rebuild it. **The most urgent retrain candidate** (versioned trainer, first-touch label on the real geometry, leakage features removed).
- **MIS1-168H: C−** — +6.928 cumulative, but WR 48/49/35% since May: the horizon is too long for stationary 1h features, the model drifts with the regime. Keep only with retrain + drift monitoring.
- **MIS1-8H/24H: D** — ~52% WR, negative median, purely tail-driven. Short horizon + slow features = the conceptually thinnest combination. Better to drop it in the retrain program.

### ATS1 (TSI Sniper) — **C+** (Σ +1.622)
**Concept:** an event-driven filter — the direction model is only queried on a TSI fast crossover on the last *closed* candle. Architecturally the cleanest design in the family: the rule gate guarantees that only the trained event population is scored live; the only bot in the family with correct candle discipline.
**Weaknesses:** OBV train/serve skew (training accumulates over ~300 days, live uses a 500-candle window with different normalization) → explains the measured calibration **inversion** (bucket 0.6–0.7 → 71% WR, 0.8–0.9 → 57%); training label (2.5%/1.5%-bracket) ≠ live geometry; training data 6.5 months stale.
**Verdict:** cheaply rescuable: scale-free OBV features + retrain on the real geometry; the immediate fix is essentially free (set the operating point to the empirically best 0.6–0.7 bucket). Conceptually the blueprint for all the other families.

### RUB1 (Rubberband Mean Reversion) — **D+** (Σ +3.675, but tail-/SHORT-driven)
**Concept:** a 4-fold extreme pre-filter (≥8% below/above a 90d regression + RSI extreme + TSI extreme + Donchian touch), then a 9-feature ML model as a snap-back filter. The basic pattern is well-conceived, but: the label ignores the **SL path** — for knife-catching, the drawdown path is the actual risk measure, and by construction the model cannot learn what it's being used for.
**The family's most severe findings:** an MACD semantics break (trained on 9/21, fed live with 12/26 columns under the same name — invisible to validation); random split across persistence episodes → test AUC is memorization; the pre-filter fires live on a different population (DB RSI ≠ Wilder, Δ≈4.8).
**Verdict:** the live profit **cannot come from the ML** (wrong MACD, wrong RSI) — it comes from the pre-filter + S/R construction, median −0.06, p95 +33%, SHORT 63.9% vs. LONG 48.7% WR. Immediate: close the LONG gate. Rescue only via a complete retrain with a shared feature builder.

### EPD1 (Real-Time Pump/Dump Detector) — **C+** (Σ +14.222, best avg in the fleet — but with an asterisk)
**Concept:** a 10s tick detector: volume anomaly + micro-momentum from the 24h ticker, a 3-class model, 15-min cooldown. The edge narrative is the healthiest in the family — volume ignition is one of the few genuine short-term edges in alt perps, and the SHORT asymmetry (76.5% vs. 50.2% WR) confirms the "pump fade" pattern.
**Core problem (P0-class):** the trainer only sampled `volume_ratio ≥ 5` events, the live bot scores **every tick without a gate** → almost all live queries are out-of-distribution, calibration is flat (corr≈0). The 72.8% WR plausibly comes from the S/R construction. Plus: the training code is commented out (stale artifact), the timestamp fix is incomplete, a shadow flood in `ml_predictions_master`.
**Verdict:** the **cheapest fix in the entire fleet** — the `vol_ratio ≥ 5` gate before `predict` is one line and brings the model into its training distribution for the first time. Caution: almost the entire profit comes from May/June (the alt-pump phase), July negative (−345) → regime dependence, drift watch mandatory. After the gate fix + retrain, potential toward a B.

---

## 5. AI Bots SRA1 / ABR1 / ATB1 / AIM1

### SRA1 — **B−** (Σ +134, n=396, the only one of the four with a positive median)
**Concept:** not a signal generator, but an ML quality filter on top of the classic Support Resistance strategy — de facto **meta-labeling per Lopez de Prado**: a well-defined event population, features at event time, label = the real trade outcome of the same strategy. This gives it the structurally smallest train/live gap in the fleet. The trainer situation is the healthiest (chronological split, provenance proven).
**Open points:** the label semantics `SL1/SL2/SL3/4` are unverified (if `SL1` means "SL before TP1", the label would be partly inverted — clarify!); raw price columns as features (a whiff of scale leakage); a crash loop on missing ATR features (35 instead of 38 columns → predict throws, batch rollback).
**Verdict:** keep. Crash fix immediately (possible without a retrain), verify the label, remove the price features + add calibration at retrain time. The best retrain candidate of the four because the foundation is sound.

### ABR1 (Break & Retest) — **C−** (Σ +335, n=110)
**Concept:** classify continuation vs. failed breakout after a level retest — the right ML formulation for a solid trading concept; conceptually the second-best approach after SRA1.
**But:** it's proven that the model actually runs on only **7 of 18 features** (P0.12, a pandas_ta name mismatch → 11 features constant at 0, verified in the booster dump); threshold + win rate fully in-sample; the most honest number, CV-F1(success) = **0.134 ≈ noise**. The small live profit plausibly comes from the setup + S/R construction, not from model skill. On the plus side: clean wiring (class index verified threefold, closed-candle correct).
**Verdict:** well rescuable with a clear path: the pta prefix-match fix (template at `14:197-211`), retrain with all 18 features (time-based split, first-touch label from the retest close, threshold on validation), a startup assertion "no feature constant".

### ATB1 (Trendline Break/Bounce) — **D** (Σ −172, n=306)
**Concept:** trendline events (break/bounce up/down) ML-scored. Trendline trading is legitimate as a discretionary concept but poorly formalized ("the" trendline doesn't exist; R≥0.2 is extremely lax). Four semantically distinct events are scored by two models that **don't know the event as a feature** — break and bounce are indistinguishable to the model. The deliberately reactivated "unknown" state logic (a code comment: „HIER IST DER BUG AUS DEINEM ALTEN BOT WIEDER AKTIV!") makes the event definition entirely arbitrary.
**Killer:** the trainer labels a **different mathematical object** (crossings of the close regression line instead of pivot trendlines; bounces have no training counterpart at all); `vol_ratio` live is ~1/19 of the training scale; label +10%/72h with no SL vs. live SL down to −8.8%.
**Verdict:** the ML gate is effectively a random filter. Park it; a rescue would mean rebuilding from zero (fix the event definition, label on live events), not a fix.

### AIM1 (Meta-Model) — **F** (Σ −3.399, the biggest AI loss-maker; conf>0.9 → 9.3% WR)
**Concept:** the most ambitious approach — stacking over all bot signals: market context × signal-swarm behaviour × source identity → success probability per candidate. Meta-learning over base signals is fundamentally a good idea, but the architecture violates every precondition: source identity as one-hot over freely named, changing bot names (the most fragile encoding conceivable); the classic bots' "confidence" is a hard-coded fantasy mapping (a constant per source); a self-feedback loop (its own shadow outputs count as input); a single label spanning heterogeneous trade geometries.
**Proven (Report 13):** the identity vocabulary is dead (only historical names in the pkl → all identity dummies are 0 live), a join look-ahead (`round` instead of `floor` → the feature candle reaches up to 90 min into the future), a volatility label (+10%/72h ahead of a −7.5%-SL → the top features are ATR → the model is a volatility detector, and the most volatile candidates hit their SL first live) → a **genuine, honestly learned inversion**.
**Verdict:** the model isn't useless, it's **reliably wrong**. Pause immediately. Rescue = a new project (first-touch label, identical floor-join in trainer and serving, versioned vocabulary, self-exclusion, out-of-time calibration) — retraining just the vocabulary would again produce an overconfident volatility model.

---

## 6. SMC/Pattern Family + UFI1

**Tag clarification (verified in code):** `QM_*` = Quasimodo (24) · `TD_*` = **Three-Drive** divergence (25) · `BB_*` = **Breaker Block** = break-and-retest with ML (25, *not* Bollinger) · `BR1H/2H/4H` = break-and-retest **without** ML from the pattern detector (7, not from 25!) · bots 16/17/21 write no `ai_signals` → **completely unmeasured**.

### Three-Drive TD_1H/4H — **B−** (Σ +2.387; per Step 2, TD_1H is the best-calibrated model in the fleet)
**Concept:** three higher highs / lower lows with opposing RSI at the pivots → momentum exhaustion → reversal. Effectively a classic **RSI divergence strategy** in pattern disguise — and thereby the conceptually soundest in the family (divergence at multiple extrema has an empirical track record).
**Despite formally worthless training** (trainer entry = pivot close with 10-candle hindsight, fixed 2R geometry, random split — P0.10/P1.25/P1.29), TD is clearly net positive and calibrated: the features evidently carry real signal that survives the label bias.
**Verdict:** a clear keep candidate. Biggest lever: a correct retrain (entry at `p3+PIVOT_WINDOW`, live geometry, chronological split) — plausibly calibration and selection sharpness would rise further. Not an A because the average edge is small and tail-driven.

### Breaker Block BB_4H — **C−** (+565) / BB_1H — **D** (−1.089)
**Concept:** break-and-retest ("support becomes resistance") — the best-supported idea in the SMC family. The live pattern matches the theory exactly: on 4h the base edge is large enough to survive ML noise and fees, on 1h it isn't.
**Critical skew:** the trainer extracts features at the *breakout* candle (RSI ~65–75), the bot at the *retest* candle (RSI ~45–55) → the probabilities are noise. Plus the `peak_idx[-2]` level bug (P2.39) and fees of 8–15% of an R at a 1%-SL geometry.
**Verdict:** park BB_1H; BB_4H is the reason to fix the pipeline (features at the retest candle, level logic, fees folded into labeling) rather than delete it.

### BR family (Pattern Detector 7) — **D** (Σ −4.106)
The same break-and-retest idea as BB, but **without an ML gate** and with 4x the signal volume. The comparison BB_4H (+ML, +565) vs. the BR family (no ML, −4.106) is the best in-vivo argument in the repo that an ML gate over raw break-and-retest signals creates value. **Immediate:** close the BR1H SHORT side (LONG 65.5% vs. SHORT 49.5% WR).

### Quasimodo QM_1H — **D+** (−139 at 67.5% WR) / QM_4H — **F** (−277)
**Concept:** liquidity sweep + structure break, retest of the sweep zone as a reversal — among the pattern ideas, one of the more plausible ones and more objectively definable than FVGs. The ML filter as "take the best X%" is the right approach.
**But:** live pivots on the forming candle vs. a correctly gated trainer (training/serving skew); the trainer simulates a limit order at the QML, the bot trades CMP; the fill logic deletes guaranteed losers + awards same-candle TP wins (P1.30) → labels systematically flattered; the bot ignores the stored `optimal_threshold`.
**Verdict:** 67% WR at ±0 means: the geometry (TP1 = half the distance, SL beyond the extreme) structurally gives it all back. Stop QM_4H; QM_1H only with a retrain + exit redesign, otherwise park it.

### SMC Forex/Metals (16) — **D−** · Mayank (17) — **D** · BTC SMC 100x (21) — **D (F as-is)**
All three unmeasured (no tracking, no valid backtest). 16: retail SMC folklore + repaint entries + SL without a sanity check — a shutdown candidate. 17: implemented more consistently (closed candle), but "FVG fully closed" as an entry is, even within SMC theory itself, an *invalidated* level — a knife-catch at an old gap floor; harmless as an info channel, unassessable as a strategy. 21: craft-wise the **best** setup design in the SMC family (age caps, trend filter, R:R check) — but the 100x design is mathematically broken (liquidation ~−0.9% before every SL, P0.5) and the parameters come from an in-sample grid search. With a leverage fix + an honest walk-forward, the most worth-investigating rule-based SMC bot.

### UFI1 (29) — **F** (25.7% WR, −7.90% avg/trade — the worst model in the fleet per trade)
**Concept:** short the dead-cat bounce of dumped coins (retracement rejection on daily). The idea isn't absurd, but the risk design fundamentally contradicts it: SL 25–40% above entry on exactly the assets with 50% squeeze candles, and at 20x the strategy is mathematically unviable (liquidation ~+5%, the SL is never reached — P0.6).
**The "+278R" backtest claim falls apart three ways** (P0.11): entry chosen using the future window low (look-ahead), a 5-target trailing ladder in the backtest vs. single-TP1 live, week-old confirmation candles with a CMP entry.
**Verdict:** here it's not just the implementation but the validating evidence itself that's been invalidated by look-ahead — no reason to believe a correct version would be positive. Stop immediately (consistent with Report 14). The fact that UFI1 imploded exactly as the look-ahead analysis predicted conversely validates the audit methodology.

---

## 7. Meta-Level: Regime Detection, Whitelist, Orchestrator/ROM1 — **C+** (concept B / implementation D+ / live effect positive)

### Regime Detection (26 + core/regime_logic.py)
Two-axis (BTC regime from 15m returns/ATR with adaptive P75/P40 percentile thresholds; alt context from BTCDOM), clean 2-check debouncing. A conceptually sound basic idea with a **structural definition flaw**: trend requires *low* vola (ATR<P40) — the mid-vola band (P40–P75, ~35% of the time) has no classification rule and always falls into the residual class TRANSITION. Live evidence (Step 2): TRANSITION 44.5%, HIGH_VOLA 29.7%, CHOP 25.8% — **TREND_UP/DOWN effectively never occur**. Since TRANSITION is simultaneously the "detector unreliable" trigger, 4D gating is deactivated almost half the time. As it stands, the taxonomy is a vola classifier with a trend label.

### Bot Regime Analyzer (27)
The idea (bot × regime × alt-context × direction → whitelist) is state-of-the-practice; the PnL-based outcome classification (delisting/cleanup/outliers as "neutral") and the asymmetrically strict counter-trend rule are well thought out. **But statistically too naive:** 30 cells per bot at a 30-day window → mostly n<30 → default-open; and even at n=30, `wr_bot ≥ wr_overall` is meaningless as a point-estimate comparison (95% CI ±17pp) — no significance test, no shrinkage. The gate flips on noise. More seriously: **it optimizes the wrong metric** — WR (TP1 touch), which Report 14 proves is misleading; avg_pnl/median/sharpe_like are already in the table and get ignored. Plus three co-directional upward biases from censoring (P1.9 foreign close, open-trade censoring, delisting neutralization) — all on exactly the number the money gate hinges on.

### Orchestrator/ROM1 (28)
The docs say "pure signal router", the code is a **26th trading bot** that uses the other 25 as a screening layer: ROM1 discards the original entry/SL/targets and builds its own (CMP entry, S/R SL without a distance cap — p90 17.9% at 20x!, up to 20 targets). That's even conceptually defensible (it normalizes heterogeneous risk profiles), **but it tears the statistical chain apart**: the whitelist is derived from trades with original parameters, while ROM1 executes something else. Plus self-echo (P0.3, 109 cases), a non-atomic pipeline, `sync_closed_trades` writing foreign outcomes onto ROM1.

### The Central Finding — ROM1 +8pp WR despite a degraded gate
ROM1: n=2.677, WR 69.2% (fleet 61.1%), +2.184 net, almost the only model with a **positive median** (+1.00%). Three readings:
1. **The gate was never fully inert, only degraded** (3.043 genuine suppressions, but on stats up to 2.5 months old + an overall fallback in the dominant TRANSITION mode). In a fleet with this quality spread, even **primitive negative selection has value** — filtering out the worst sources (AIM1, UFI1, negative SHORT sides) produces +8pp almost inevitably.
2. **A substantial part of the added value probably doesn't come from the regime concept**, but from side effects: the 4h cooldown (anti-overtrading), the opposite block, and above all its own trade construction (the positive median points strongly to this). The specific 4D hypothesis ("bot X works in regime Y") is **not validated by the +8pp — it remains untested**.
3. **The +8pp is optimistically biased** (foreign outcomes, P1.9 censoring, the WR metric) — the real added value is probably positive (net PnL and median support this independently), but smaller than the headline number.

### Intelligence Layer (19/20/23/7/22)
The whale logger (dead since 18.04., previously 49/529 symbols) and funding logger are **consumed by no decision-making logic** — dead data collection, or human-info at best. The market tracker is sensible reporting with its own bugs. Pattern Detector 7 isn't an intelligence layer but a (net negative) signal layer.

### Expectation After the Fixes
- P0.4 fix (pretty_name + staleness gate): moderate extra gain; no jump, because the TRANSITION fallback stays untouched.
- The P1.9 fix will **lower** the measured WRs — intentional (honesty), communicate it in advance.
- **The biggest lever is three concept changes, not bugfixes:** (a) split the TRANSITION residual class (mid-vola trend as its own regime), (b) switch the gate metric from WR to net expectancy with a confidence interval/shrinkage, (c) document ROM1 as its own bot and feed its own history into the gate as a second evidence layer.

With the P0/P1 fixes plus these three changes, a B grade is realistic; without the TRANSITION and metric correction it remains a good repost filter with a regime label.

---

## 8. Portfolio Recommendations (consolidated)

**Immediate (no retrain needed):**
1. **Stop:** AIM1 (reliably inverted), UFI1, QM_4H, Fast In And Out. **Park:** ATB1, BB_1H; review the BR family.
2. **Direction gates:** close EPD1 LONG, close RUB1 LONG, close BR1H SHORT.
3. **EPD1 gate fix** (`vol_ratio ≥ 5` before predict — one line) and **ATS1 operating point** set to the 0.6–0.7 bucket.
4. **Central leverage cap** (`cap_leverage_to_sl()`): closes P0.5 (BTC SMC 100x), P0.6 (UFI1), and ROM1 SL distances (P2.27).
5. Bots 16/17/21: instrument (`ai_signals`) or shut down — unmeasured strategies have no claim to profit.

**Retrain program (priority by expected value):**
1. **MIS1-72H** — the biggest profit driver with zero provenance; versioned trainer, first-touch label on the real geometry, leakage features removed.
2. **TD** — best calibration + positive profit; correct labeling should further raise selection sharpness.
3. **SRA1** — the healthiest foundation; verify the label semantics, then retrain.
4. **ABR1** — pta fix + retrain with all 18 features.
5. **EPD1 / ATS1 / RUB1** — a shared feature builder bot↔trainer (X-R2), episode dedup, label with the SL path.
Prerequisite for all of them: **fix R1 (forming candle) first** — otherwise you're training again on data that doesn't exist live; and build the shared walk-forward simulator from P0.10 that replays each bot's own setup functions bar by bar.

**Structural:**
- Switch the KPI from WR to net expectancy/median everywhere (dashboard, whitelist gate, reports) — the current WR display rewards exactly the wrong behaviour.
- Classic family: build out Support Resistance as the sole survivor (+ Main Channel merge), rework the exits; continue 5 Percent only as an experiment on the LONG side.
- Repair the regime taxonomy (split TRANSITION) — until then, every claim that "bot X works in regime Y" is untested.
- Either feed whale/funding data into regime/gate as features, or shut the loggers down — the current state (collecting with no consumer, logger dead) is pure operational overhead.

---

### Positioning Within the Overall Audit
This report adds the concept perspective to the bug-centred Reports 01–13. The central insight from both viewpoints agrees: **the system currently earns its money not through ML skill, but through S/R-based trade construction, coarse negative filters, and market regime** — and it loses money through signal inflation (FIFO), inverted models (AIM1), and unvetted leverage geometries (UFI1, BTC SMC). The sequence "R1 → immediate measures (Section 8) → retrain program" maximizes the expected value of the remediation.

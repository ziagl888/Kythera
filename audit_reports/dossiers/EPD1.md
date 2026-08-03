# Dossier: EPD1 (real-time pump/dump detector)

> 10-second-tick detector for volume ignition in alt perps with a 3-class XGBoost — **grade C+** (Report 16, rank 6): the fleet's best edge narrative and strongest avg (+3.34%/trade), but the model is served **almost exclusively out-of-distribution live** (missing vol_ratio gate) and the profit is regime-dependent (July negative).

## 1. Fact sheet

| Field | Content |
|---|---|
| Bot | `10_pump_dump_detector.py` |
| Artifact | `pump_dump_model.pkl` (3-class model; class mapping via `classes.index()` correct) — **artifact from an unknown/stale run** |
| Trainer | `legacy_trainers/zzz.py` → `train_pump_dump_model` (~lines 7054-7242). Status: trainer exists, but daily training is **commented out** (lines 7033/7040-7041) — **the log reports success anyway** |
| Label/training | Trainer samples ONLY `volume_ratio ≥ 5` events (zzz.py:7103-7104); random split over 10s quasi-duplicates (zzz.py:7178); sample weights (pump/dump up to 3.0) without recalibration |
| Features | 10; feature positions bot↔model verified exactly (Report 13). Live: volume anomaly + micro-momentum from the 24h ticker |
| Thresholds | shadow band 0.25 ≤ prob < 0.60, post from 0.60; `max(prob_pump, prob_dump)` is logged as "confidence" (uncalibrated); 15-min cooldown |
| Channels | pump/dump alert channels incl. the MARKET channel (round-level cooldown asymmetric, MARKET re-sends every 180s); trades in `ai_signals`/`closed_ai_signals` as model `EPD1` |
| Data basis | purely an in-memory ticker buffer — the table `ticker_10s` is **empty** (N3, Report 17) |

## 2. Live results (active era 24.02.–03.07., deduplicated; Report 14/Step 2)¹

- **n = 4,392 · WR 72.8% · avg +3.34%/trade · median +3.63% · Σ net +14,222 price-%** — strongest avg and second-largest earner of the AI fleet.
- **Direction split: SHORT 76.5% vs. LONG 50.2% WR** — the fleet's largest direction asymmetry; confirms the "pump-fade" pattern (Report 14/16).
- **Calibration: flat** (corr ≈ 0, but a high baseline level; Step 2) — consistent with OOD serving: the 72.8% WR plausibly stems from the S/R-based SL/TP construction, not from model skill.
- **Monthly trend:** almost all the profit comes from May/June (+14.6k, alt-pump phase), **July negative (−345)** → regime dependence, drift watch mandatory.

¹ *Monitor caveat (Report 17): figures are monitor-generated (replay agreement only 63.4%; P1.2/P2.7/P2.31 — EPD1 has 215 rows with 20 scored targets); AI trades cannot be replayed retroactively (N4).*

## 3. Findings (consolidated)

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| 13-B/P0 | Pipeline | P0 | **Covariate shift:** trainer samples only `vol_ratio ≥ 5`, live scores every 10s tick without a gate (`10:519-565`) → nearly all queries out-of-distribution; explains the flat calibration | ✔ (code-documented + Step-2 measurement) |
| 13-B/P1a | Trainer | P1 | Daily training commented out, log reports success anyway → artifact stale/from an unknown regime (zzz.py:7033,7040-7041) | ✔ |
| 13-B/P1b | Trainer | P1 | Random split over 10s quasi-duplicates → metrics memorized (zzz.py:7178; X-R3) | ✔ |
| P1.39 | Bot | P1 | Timestamp fix incomplete: volume explosion + ML features still index-based → after a restart, false "VOLUME EXPLOSION" alerts, skewed features (`10:522-529,552-558`) | ✔ (code-documented) |
| P1.40 | Bot/DB | P1 | Unconditional CREATE+INSERT into `pump_dump_events` per symbol per 10s tick → ~108 stmt/s, ~4.6M rows/day; rsi/tsi columns never populated | ~ (Step 2: table exists, narrow schema) |
| P1.41 | Bot/DB | P1 | Shadow inserts into `ml_predictions_master` without cooldown → up to 8,640 rows/day/symbol; Step 2: 31k EPD1 rows/7d; poisons tracker stats and calibration measurements | ✔ (quantified) |
| 13-B/P2 | Bot | P2 | `float(None)` crash on SQL NULL (`10:537`) kills the whole 10s cycle | ✔ (code-documented) |
| 13-B/P2b | Trainer | P2 | Sample weights without recalibration; `max(prob_pump,prob_dump)` used as "confidence" (X-R4) | ✔ |
| 09-M | Bot | Medium | Ladder alerts refire every 300s during a sustained move (alert storm); round-level cooldown asymmetric (MARKET every 180s) | ~ (open) |
| N3 | Data | — | `ticker_10s` empty — EPD1 is purely in-memory; the suggested training data basis does not exist | ✔ (Report 17) |

Positive: feature positions 10/10 exact, class mapping correct (Report 13).

## 4. Dependencies & cross-cutting risks

- **Core risk = missing `vol_ratio ≥ 5` gate live → OOD:** the model is permanently asked questions it has no training data for (X-R1/X-R3/X-R4 all affected). Live profit therefore hangs on the S/R construction + market regime, not on the model.
- **R3 (TZ)** via the naive timestamp columns; **R1** affects EPD1 less directly (ticker-based), but the DB feature paths inherit P1.39.
- **Shadow flood (P1.41)** is a second cause of corr≈0 in the Step-2 calibration measurements and skews every per-bot statistic via the market tracker (P1.44).
- A successor concept exists: **S6 "pump-exhaustion short"** (Report 15, tier 2) — short-only, gate mirrored live, microstructure features, first-touch label; but it requires the N3 decision (populate ticker_10s) first.

## 5. Remediation plan

**a) Immediate (without retrain; Report 13 immediate measure 2 + Report 16):**
1. **1-line gate fix:** mirror `vol_ratio ≥ 5` before `predict` — "cheapest fix in the whole fleet", brings the model into its training distribution for the first time.
2. **Shadow cooldown** (per symbol, ~15 min) for `ml_predictions_master` + filter consumers on `posted=TRUE` (P1.41).
3. **NULL guard** at `10:537` (prevents cycle kill).
4. Direction gate: **close LONG** (50.2% WR; Report 14 D.5 / 16 section 8).
5. Route P1.39 remaining paths via `_find_bucket_before/range`; one-time `pump_dump_events` CREATE + sample inserts (P1.40); watch the July drift.

**b) Retrain/rebuild:**
- Short-term, the gate fix may be enough (Report 13 sequence: "EPD1 (gate fix may suffice for now)"). After that, retrain per the shared framework (import the bot's own feature builder, episode dedup, chronological split, first-touch label of the short geometry, calibration) — or go straight to the rebuild as **S6 pump-exhaustion short** as a clean successor. After gate fix + retrain, "potential towards B" (Report 16).

**c) Open questions:**
- Provenance of the deployed `pump_dump_model.pkl` (training run/period unknown, training commented out since when?).
- `ticker_10s`: populate (training data source for S6) or drop (N3).
- Does the SHORT asymmetry hold outside the alt-pump phase May/June? (rolling re-validation of the gates).

## 6. Evidence

- `AUDIT_TODO.md` P1.39–P1.41 · `audit_reports/09_intelligence.md` (detector findings) · `audit_reports/13_x_ml_trainers.md` (EPD1 section, X-R1..R6, immediate measures) · `audit_reports/14_bot_performance_db.md` (n=4,392, +14,222, SHORT 76.5% vs LONG 50.2%, July −345) · `audit_reports/STEP2_DB_VERIFICATION.md` (calibration flat, shadow flood 31k/7d) · `audit_reports/16_strategy_concept_evaluation.md` (grade C+, section 4) · `audit_reports/15_strategy_proposals.md` (S6) · `audit_reports/17_monitor_replay_and_gaps.md` (N3, N4, monitor caveat).

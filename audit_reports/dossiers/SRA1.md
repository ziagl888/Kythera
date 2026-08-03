# Dossier: SRA1 (support/resistance AI bot)

> ML quality filter (meta-labeling) on top of the classic support-resistance strategy — **grade B−** (Report 16, rank 3): conceptually the cleanest ML setup in the fleet, small but healthy and positively calibrated; core verdict (Report 13): "functional, with an open label question".

## 1. Fact sheet

| Field | Content |
|---|---|
| Bot | `9_ai_sr_bot.py` |
| Artifacts | `trade_success_xgb_LONG_v2.json` / `trade_success_xgb_SHORT_v2.json` — **proven: v2 = pure format conversion of v1** via `core/update_model.py` (booster comparison: all 100 trees bit-identical, 38 features, LONG+SHORT; Report 13) |
| Trainer | `legacy_trainers/X9-SR-ANALYZER-Schritt1.py` (v1) + `core/update_model.py` (conversion). Status: provenance **clarified/proven**, but conversion/training not versioned (P1); mark the old trainer `X9-SR-ANALYZER.py` (random split) as deprecated. Caution: `core/update_model.py:35` overwrites `.pkl`/`.joblib` in place (P1.35) |
| Label | `success = status in ['SL1','SL2','SL3','4']` (Schritt1:157) — presumably "trailing SL after TPn = win"; **if `SL1` means "SL before TP1" in `closed_trades3`, the label is partially inverted → needs clarifying!** (Report 13, P2) |
| Features | 38, parity bot↔model exact (JSON-verified). Deficiencies: raw price columns as features (scale-leakage smell), 1h lookahead in the training join (Schritt1:56-61), median imputation over the whole dataset vs. raw NaN live |
| Thresholds | Shadow-log inconsistency: comment 0.45 vs. code 0.35 (`9:285-299`); minimal insert writes NULL time/direction/entry |
| Channel/exits | Publishes TP1–3 (Cornix), monitor scores up to 10–20 targets → live statistics ≠ Cornix reality (P2.31 ✔, targets_hit up to 21) |
| Concept | Not a signal generator but meta-labeling per Lopez de Prado: well-defined event population, features at event time, label = real trade outcome of the same strategy → structurally the smallest train/live gap in the fleet; Schritt1 split is chronologically correct |

## 2. Live balance (active era 24.02.–03.07., deduplicated; Report 14/Step 2)¹

- **n = 396 · WR 69.9% · avg +0.44%/trade · median +1.12% · Σ net +134 price-%** — "healthy, small"; the only one of the four AI bots (SRA1/ABR1/ATB1/AIM1) with a positive median.
- **Calibration: positive ✓** (Step 2, conf→win) — SRA1 belongs, along with TD_1H, MIS1-8H and QM, to the few genuinely calibrated models in the fleet.
- Direction split/monthly trend: not reported separately in the reports (n small).

¹ *Monitor caveat (Report 17): all figures are monitor-generated; monitor scoring agrees with a first-touch replay only 63.4% of the time (17.8% missed TP1, 18.8% TP1 despite SL-first). AI trades are also not retroactively replayable, because `ai_signals` rows are deleted on close (N4).*

## 3. Findings (consolidated)

| ID | Level | Severity | One-liner | Status |
|---|---|---|---|---|
| P1.20 | Bot | P1 | Conditionally missing ATR features → 35 instead of 38 columns → predict throws → whole iteration crashes, rollback discards shadow inserts; crash loop every 300s for 60 min (`9:135-143,268-305`) | ✔ (code-proven, Report 13) |
| P2.30 | Bot/Data | P2 | Logs `posted=True` even when cooldown suppressed the post → phantom posts in the performance evaluation (`9:163-164,278-283`) | ~ (open, [DB]) |
| P2.29 | Core | P2 | `get_hvn_and_sr_levels` reads 95d **without ORDER BY** (used by SRA1 for SL/TP!) → phantom extrema as SL/TP prices; fix = 1 line | ~ (open, [DB]) |
| 06-M | Bot | Medium | Forming-candle indicator row + entry posted as "CMP entry" while up to 60 min stale (`9:54-74,154-188,244-253`) | ✔ (R1 proven live) |
| 13-P2a | Trainer | P2 | Label semantics `SL1/SL2/SL3/4` unverified — inversion risk | ~ (needs clarifying) |
| 13-P2b | Trainer | P2 | 1h lookahead in the training join (open_time-keyed candle contains future up to +1h) | ✔ (code-proven) |
| 13-P1 | Trainer | P1 | Training/conversion not versioned (3-line script + meta.json missing) | ✔ |
| P2.31 | Monitor | P2 | Subscribers see TP1–3, monitor scores up to 10–20 targets | ✔ (Step 2: targets_hit up to 21) |
| 06-L | Bot | Low | Shadow threshold comment 0.45 vs. code 0.35; minimal insert with NULL fields | ✔ |

## 4. Dependencies & cross-cutting risks

- **R1 (forming candle, proven live):** SRA1 is explicitly listed as affected — features/entry on the running candle; every retrain before the R1 fix trains again on data it does not have live.
- **R3 (TZ mix):** session TZ Europe/Bucharest, naive columns mixed UTC/local — affects cooldown/statistics windows.
- **X-R1 (label ≠ live geometry):** weakest at SRA1 (label = real strategy outcome), but the label semantics are open; **X-R4:** confidence communicated uncalibrated (SRA1 at least empirically positively calibrated); **X-R6:** serving on forming candle.
- Depends on the classic strategy "Support Resistance" (B−, +596, SHORT carries everything) as the event source, and on `core/trade_utils` (P2.29) for the SL/TP construction — Report 16: the S/R trade construction is the "secret star" and plausibly the actual source of profit.

## 5. Remediation plan

**a) Immediately (no retrain):**
1. **ATR-emit fix:** always emit ATR features (as NaN — XGB can handle NaN) + reindex guard + per-trade try/except → kills the 300s crash loop (P1.20; Report 13 immediate measure 3).
2. `ORDER BY open_time ASC` in `get_hvn_and_sr_levels` (P2.29, one line).
3. Fix the `posted` return value as a bool (P2.30); store exactly the published targets (P2.31).

**b) Retrain/rebuild (Report 13/16: best retrain candidate of the four, because the foundation is sound — but last in the order, since it is functionally the healthiest):**
- Verify the label first, then retrain following the shared blueprint: versioned trainer + meta.json, first-touch label of the real geometry, closed candles only (R1 fix first), remove raw price features, isotonic calibration, startup assertion "no feature constant".

**c) Open questions:**
- **SL1/SL2/SL3 label semantics!** Verify against `closed_trades3` status codes — if `SL1` means "SL before TP1", the training label is partially inverted (the most important open question for the family).
- Quantify the phantom-post rate (P2.30) and the SL/TP anomalies from P2.29 via DB join.

## 6. Evidence

- `AUDIT_TODO.md` P1.20, P2.29–P2.31 · `audit_reports/06_ai_bots_a.md` (SRA1 bot findings) · `audit_reports/13_x_ml_trainers.md` (SRA1 section: v2=v1 proof, label question, measures) · `audit_reports/14_bot_performance_db.md` (n=396, +134) · `audit_reports/STEP2_DB_VERIFICATION.md` (calibration positive, targets_hit up to 21, R1 proof) · `audit_reports/16_strategy_concept_evaluation.md` (grade B−, section 5) · `audit_reports/17_monitor_replay_and_gaps.md` (monitor caveat, N4).

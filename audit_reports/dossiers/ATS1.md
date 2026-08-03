# Dossier: ATS1 (TSI-Sniper)

> Event-driven direction classifier: the XGBoost model is only queried on a TSI fast crossover on the last closed candle. Note (Report 16): **C+** (Σ +1.622 net). Core verdict (Report 13): architecturally the blueprint of the family (event gate, correct candle discipline), but OBV train/serve skew inverts the confidence; usable as a rough ranking, not as a probability — short model unvalidated.

## 1. Fact sheet

| | |
|---|---|
| Bot file | `12_ai_ats_bot.py` (hourly; scheduler comment says :08, runs :13 — P3 drift) |
| Model artifacts | `model_tsi_long_robust.pkl` / `model_tsi_short_robust.pkl` |
| Trainer | `legacy_trainers/X8-TSI-ML-V4.py` (long) / `X8-TSI-ML-V5.py` (short); training data from `legacy_trainers/X8-TSI-EXPORT-V4.py` / `X8-TSI-EXPORT-V5.py` (provenance ✔ clarified, Report 13) |
| Training date | training data ends **2025-12-15** — 6.5 months stale as of the audit |
| Data source | export from the 1h DB tables; OBV accumulated over ~300 days in training. **Short model (V5) trained on `{coin}_1h_X` tables — a different source than live!** |
| Label | 2.5%/1.5% bracket, 96h horizon; TP-before-SL on ambiguous candles (optimistic bias in high-vol samples) — ≠ live geometry (SR targets ≥5%, DCA entry2, SR SL) |
| Features | 29, train↔serve identical (positively verified) |
| Thresholds | chosen by profit-factor maximisation **on the test set** (`ML-V4:91-110`) — maximum-statistic artifact (X-R2) |
| Signal path | TSI crossover gate → predict → AI channel via outbox/Cornix; TP/SL via `get_hvn_and_sr_levels`/SR construction; publishes TP1–3, monitor scores up to 10–20 targets (P2.31). The only bot in the family with correct closed-candle discipline (`-2`) |

## 2. Live P&L (as of 2026-07-03, active era 24.02.–03.07., deduplicated, unlevered)¹

- **n = 1.768 · WR 65.8% · avg +1.02%/trade · median 0.00 · Σ net +1.622** — positive despite trainer shortcomings (Report 14: verdict "keep/focus").
- **Direction split:** not reported in the sources.
- **Monthly trend:** no notable monthly drift reported in the sources (unlike MIS1-168H/EPD1).
- **Calibration finding (Step 2): slightly negative — inverted in the upper band:** bucket 0.6–0.7 → **71% WR**, bucket 0.8–0.9 → **57% WR**. Report 13 explains this causally via the OBV skew: the high-confidence region is out-of-distribution live.

¹ Caveat (Report 17): monitor-generated numbers; monitor scoring is only 63.4% replay-consistent (classic sample), AI trades cannot be audited retroactively (N4: `ai_signals` deletes SL/targets on close).

## 3. Findings (consolidated)

| ID | Level | Severity | Finding | Status |
|---|---|---|---|---|
| 13-P0 (OBV skew) | Trainer/model | P0 | `obv_val`/`obv_ratio` train/serve skew: training accumulates ~300 days, live uses a 500-candle window with normalisation that mathematically changes `obv_ratio` → high-confidence region is OOD live | ✔ confirmed (code + Step 2 inversion measurement) |
| 13-P0 (label) | Trainer | P0 | Label geometry 2.5%/1.5%/96h ≠ live geometry (SR targets ≥5%, DCA entry2, SR SL) — X-R1 | ✔ confirmed (code) |
| 13-P1 | Trainer | P1 | TP-before-SL on ambiguous candles (`EXPORT-V4:272-275`) → optimistic bias exactly in high-vol samples | ✔ confirmed (code) |
| 13-P1 | Trainer | P1 | Short model (V5) trained on **`{coin}_1h_X`** tables — a different data source than live → short unvalidated | ✔ confirmed (code) |
| 13-P1 | Trainer | P1 | `scale_pos_weight` without recalibration (X-R4) | ✔ confirmed (code) |
| 13-P2 | Trainer | P2 | Threshold PF maximisation on the test set; data 6.5 months stale. Positive: chronological split correct, 29/29 features identical | ✔ confirmed (code) |
| 06-MEDIUM | Bot | P2 | OBV features window-length dependent despite the normalisation fix: `len(rows)>=50` lets 50–499-candle coins through with a different accumulation window | ~ open |
| P2.29 / 06-MEDIUM | Bot (core) | P2 | `get_hvn_and_sr_levels` reads 95d **without ORDER BY** (SL/TP source for SRA1/ATS1/RUB1) → phantom extrema possible as SL/TP prices | ~ open `[DB]` |
| P2.31 / 06-MEDIUM | Monitor | P2 | Publishes TP1–3, monitor scores up to 10–20 targets → live statistics ≠ Cornix reality | ✔ (Step 2: targets_hit up to 21 fleet-wide) |
| 06-LOW | Bot | P3 | Scheduler comment ≠ trigger minute; estimator truthiness (`if not MODEL` instead of `is None`, 12:83,91) | ~ open |
| (contrast) P1.17 | Bot | — | Forming-candle prediction: ATS1 gets it **right** with `-2` — explicitly cited as a positive reference | ✘ not affected |

## 4. Dependencies & cross-cutting risks

- **R1 forming candle:** ATS1 avoids the serving part (closed-candle `-2`), but sits on the same data pipeline; the stored history contains partial/broadcast values (P1.11/P1.12).
- **X-R1** (bracket label ≠ traded geometry), **X-R2** (threshold on the test set), **X-R4** (spw without calibration), **X-R5** (silent defaults in the export) apply to ATS1; **X-R3/X-R6** defused (chronological split correct, closed-candle serving).
- **P2.29:** shares the ORDER-BY-less SL/TP source with SRA1/RUB1.
- **Whitelist/orchestrator:** ATS1 is not among the frozen raw-name rows listed in Step 2; the general gate/fallback problem (P0.4/P2.23) still affects the signal pipeline fleet-wide.
- **Monitor-label caveat (Report 17):** WR/PnL are monitor-generated; every retrain label inherits this until the monitor rewrite.

## 5. Remediation plan

**(a) Immediately, without retraining:**
1. **Set the operating point to the empirically best 0.6–0.7 bucket** (Report 13 measure 4, Report 16 §8.3 — "essentially free").
2. Stop communicating confidence as a "%" (X-R4).
3. `ORDER BY open_time ASC` in `get_hvn_and_sr_levels` (P2.29, one line).
4. Only allow the ML path from ≥500 candles onward (06-MEDIUM fix).
5. Store exactly the published targets in `ai_signals` (P2.31).

**(b) Retrain (Report 13/16; order: after MIS1/AIM1/ABR1/ATB1+RUB1):**
- Scale-free OBV features (fixes the inversion mechanism at the root).
- Put the short model on the same data source as live (away from `_1h_X`).
- Label = first-touch simulation of the real SR/DCA geometry (X-R1/P0.10 simulator); resolve TP-before-SL ambiguity conservatively.
- Shared feature builder bot↔trainer, chronological 3-way split, threshold on validation, isotonic calibration out-of-time, meta.json; fresh data (replace the 2025-12-15 snapshot).
- Prerequisites: R1 fix, monitor rewrite (Report 17), dedup index (V2).

**(c) Open questions:** LONG/SHORT direction split never reported — measure before a direction gate. Is the live profit model skill or event-gate+SR construction (Report 16: family finding "no demonstrable ML skill")? Short-model validity can only be assessed after a retrain.

## 6. Evidence

- `AUDIT_TODO.md` — P2.29, P2.31, R1/R3 context, P1.17 contrast ("ATS gets it right with −2").
- `audit_reports/06_ai_bots_a.md` — bot engine findings (OBV window length, get_hvn without ORDER BY, targets divergence, LOW items).
- `audit_reports/13_x_ml_trainers.md` — ATS1 section (OBV skew, label, V5 source, threshold, verdict "limited; short unvalidated").
- `audit_reports/14_bot_performance_db.md` — realised numbers (n=1.768, +1.622 net), portfolio classification "keep".
- `audit_reports/STEP2_DB_VERIFICATION.md` — calibration measurement (slightly negative, 0.6–0.7→71% vs 0.8–0.9→57%).
- `audit_reports/16_strategy_concept_evaluation.md` — Note C+, "architectural blueprint", immediate measure: operating point.
- `audit_reports/15_strategy_proposals.md` — V1–V3 prerequisites (simulator as the label source).
- `audit_reports/17_monitor_replay_and_gaps.md` — 63.4% replay caveat, N4.

# 19 — Batch E: trainer validity fixes, walk-forward simulator, retrain candidates

**As of:** 2026-07-04 · **Task:** T-2026-CU-9050-016 · **Branch:** `feat/t-2026-cu-9050-016` (nothing deployed, fleet untouched)

Coverage: AUDIT_TODO P0.10, P0.11, P0.12, P0.13 (prep), P1.29, P1.30, P1.31, P1.35.
Artifacts: exclusively `Documents\_X\staging_models\` (replays under `staging_models\replay\`).

---

## E1 — Trainer code fixes (pure correctness, no training)

### P1.35 — `core/update_model.py` (fixed first, prerequisite for everything else)
`replace(".model", "_v2.json")` was a no-op for `*_model.pkl`/`.joblib` → `save_model()` overwrote the **original artifact in place**. Now: `splitext`-based target name + a hard refuse if target == source **or** the target already exists.

### P0.12 — ABR1 pandas_ta columns (bot + trainer)
- `18_ai_abr1_bot.py` and `Documents\_X\BT2-Datagrepper-for-ML.py` (versioned copy: `trainers_x/BT2-Datagrepper-for-ML.py`): **prefix matching** instead of exact names (`KAMA_9*`, `TSI_*`/`TSIs_*`, `BBL_*`, `DCL_*`, …) + a **hard ValueError** on a missing source column instead of silent `fillna(0)`.
- Bot: **startup self-test** — feature pipeline on real data from up to 3 coins; a constant continuous feature → `exit(1)`. Exactly the failure mode that let the model run for months unnoticed on 7/18 features.
- Datagrepper: constancy assertion over the finished training dataset; max. 2 workers with BELOW_NORMAL instead of `cpu_count()`.
- **⚠ Side finding (deploy-critical):** the ruff cleanup `052ba4c` had removed the **function-local `import pandas_ta`** from b6735d9 as "unused" (the import registers the `df.ta` accessor — a classic F401 misfire). Consequence: the repo state of ABR1 would have died on a deploy on **every coin** with an `AttributeError`, swallowed by the per-coin `except` — a silently dead bot. Live (== b6735d9) was not affected. Module-level import restored. **Recommendation:** secure ruff F401 for accessor libraries (pandas_ta) via `# noqa`; check all other bots for the same pattern.

### P1.29 — chronological split + threshold on validation (`qm_ml_trainer.py`, `smc_ml_trainer.py`)
- 70/15/15 along entry time (new: `entry_time` is now captured in the simulation) with a **purge gap** (QM: `ORDER_EXPIRY`=50 bars; SMC: 100 bars — TD patterns span ≤100, BB ≤60+40).
- Threshold scan now only on the **validation** slice; the test set stays untouched and yields the one honest number (output separately and stored in the pkl `meta`).
- Both trainers now save only to `staging_models\` (never again in place over production pkls) and write a `meta` block (split, test stats, xgb version, n per slice).

### P1.30 — QM fill logic (`qm_ml_trainer.py`, `qm_backtest.py`)
- An SL puncture of a pending order is no longer an "invalidation": since the SL sits beyond the entry, the same candle necessarily also touched the entry → conservative **fill-then-stop = immediate loss**. Previously exactly these guaranteed losers were **deleted** from the dataset.
- **No TP win on the entry candle** anymore (intra-candle order is not determinable); TP evaluation starts with the following candle. `qm_backtest.py` now books the fill-then-stop case as a regular loss including fees/drawdown.

### P1.31 — trainer data loader
- `fetch_merged_data` (qm + smc): `try/finally conn.close()` (previously every query error leaked a pool connection), skips logged as WARN.
- Hard `SystemExit` on **<80% coin coverage** in the qm, smc and BT2 trainers — previously the pipeline trained silently on 0–8 coins and saved over the production pkl.

### P0.13 prep — AIM1 vocabulary reconciliation (documentation only, no retrain)
Live source: `ml_predictions_master.model_name` (from which `15_ai_master_bot.py` builds both `ai_model` and `conv_source_bot`).

| | pkl dummies | live DB (distinct, total) | overlap |
|---|---|---|---|
| `ai_model_*` | 11: ATS1, EPD1, **MSI1**-{8,24,72,168}h_{pump,dump} (typo!), nan | 16: EPD1, AIM1, ATS1, RUB1, BB_1H/4H, MIS1-8H/24H/72H/168H, ATB1, QM_1H/4H, TD_1H/4H, SRA1 | **2/16** (ATS1, EPD1) |
| `conv_bot_*` | 5: `5% Bot`, `Fast Bot`, `SR Bot`, `Volume Bot`, nan | 0 conv names in `ml_predictions_master` (classic bots no longer write there) | **0** |

- All 8 MIS dummies carry the historical **MSI1** spelling → live writes `MIS1-72H` etc. → one-hot always 0.
- Booster gains: `conv_bot_nan` is with **48.3** the strongest identity column (the model has learned "no conv bot" as a feature — live that is ALWAYS on); `ai_model_MSI1-72h_pump` 38.8, `ai_model_ATS1` 33.1 — all dead live.
- Additionally in `closed_ai_signals`: the naming landscape changed on 2026-03-02 (MIS1-*_pump/dump and MSI1-* end there; MIS1-xxH begin) — every identity vocabulary without versioning goes stale within weeks.
- **Consequence (consistent with reports 13/16):** a retrain on the vocabulary alone is not enough (volatility label + round join remain) → AIM1 stays an **off/new-project recommendation**, not a batch-E retrain. Detail data: `p013_result.json` (job tmp) resp. the tables above.

---

## E2 — Walk-forward simulator (`tools/walkforward_sim.py`)

One shared simulator instead of 8 ad-hoc backtests (X-R1 fix, == P0.10):

- **Bots' own setup functions**: UFI1 via import of `find_ufi1_setup` (29), ABR1 via import of the feature builder + `find_pivot_levels` (18), TD/BB as a 1:1 rebuild of the detection from `25_smc_ml_sniper.scan_market` (including all FIX gates: MAX_TD_SPAN, MAX_BB_AGE, freshness conditions).
- **Geometry = posted geometry**: `calculate_smart_targets` now has an optional `df` parameter — the same live function runs in the replay on the historical 1000-candle window up to the decision candle (no copy-paste skew, including live fallback behaviour).
- **Closed candles only**; a decision per closed candle; cooldowns/active-trade dedup like the bots.
- **Exits**: wick-aware first-touch forward scan over 1h candles, **SL-first on ambiguity**, trailing like `8_ai_trade_monitor` (from TP2 → SL to `targets[k-2]`), position fractioning over the published TPs (UFI1: 1, ABR1: 3, TD/BB: 5), **fees 0.05%/side** (P3.6).
- Operating safeguards: BELOW_NORMAL priority (wintypes-correct ctypes fallback), CPU check >90% → abort, DB session read-only, output JSONL to `staging_models\replay\`.

**Deliberate approximations (documented):** UFI1 scan per daily close instead of every 4h; TD/BB scan per closed candle instead of every 3 min; ABR1 indicators computed once over the full series instead of per 240h window (== trainer behaviour; recursive indicators converge); funding costs not modelled; DB indicators historically carry R1 residual risk (forming-candle overwrites).

### P0.11 validation: UFI1 "+278R" falls apart

Full-universe replay (648 coins, 365 days, July 2025 – July 2026), exactly the live geometry (CMP entry, single TP1, SL = swing high +3%):

| Metric | Backtest claim (`fib_backtest.py`) | Honest walk-forward |
|---|---|---|
| Trades | 334 | 435 (384 closed, 51 open) |
| WR (TP1 first touch) | 54.2% | 50.8% |
| Ø R | **+0.83R** | **+0.37R** |
| Σ R | **+278R** | **+141R** |

And the +141R fall apart under cohort analysis:

| Cohort | n closed | WR | Σ R |
|---|---|---|---|
| **2025-10 (crash month)** | 216 | **78.2%** | **+184.7R** |
| all other 11 months | 168 | **~14%** | **−44R** |
| of which 2026-06 (live era) | 16 | 37.5% | +1.8R |

1. **The entire return is a one-month artefact** (October 2025 crash: 60% dumps everywhere, shorts riding into the bear market with 4-month holding times). Without this month the strategy is clearly negative.
2. **Simulator reality check passed:** June-2026 cohort 37.5% WR (n=16) vs. live 25.7% (n=35) — consistent; live additionally burdened by forming-daily-candle repaint (R1: the bot reads the running daily candle) and monitor mis-scoring (report 17).
3. **Leverage reality (the actual death blow):** max adverse excursion over the closed trades — **72% of all trades (and 72% of the winners) run ≥+5% into the red** (median MAE 9.6%, p90 41.9%). At the originally posted 20x (liquidation ~+5%) the majority of the replay "winners" would have been **liquidated before the TP**. Even the paper-R value is thus only realisable at all with ≤1-2x leverage (after the P0.6 fix) — and then, ex-October, a loss-making business remains.

**Deploy recommendation UFI1: leave OFF** (confirms report-16 note F). No retrain — there is no selection layer that would heal the structural finding.

---

## E3 — Retrains on replay labels (staging)

Candidate selection per report 16 + E2: **TD_1H/4H** (best calibration, net positive), **BB_4H** (+BB_1H data for review), **ABR1** (for the first time with 18/18 features after P0.12). NOT retrained: **AIM1** (off recommendation, see above), **UFI1** (off recommendation), **QM** (report 16: stop QM_4H, park QM_1H — exit geometry gives everything back, a retrain won't fix that), **MIS1** (retrain priority #1 per report 16, but needs the 67-feature builder + horizon labels — its own task, see "Not done").

Methodology per model (`tools/retrain_from_replay.py`): label = first-touch TP1-before-SL of the **posted** smart-targets geometry (fees incl.); chronological 70/15/15 split with purge gap; threshold via **real replay PnL** on validation; isotonic calibration (as an extra key in the artifact); calibration report old vs. new on identical test events.

### Results (details + calibration tables: `staging_models\REPORT.md` and `retrain_*_stats.json`)

| Model | Replay events | New model (out-of-time test) | Old model on the same events | Deploy recommendation |
|---|---|---|---|---|
| **TD_4H** | 1,245 / 540d | 63.5% WR @0.50 vs. 63.3% base rate; calibration 0.4→0.8 monotone | 0.8+ bucket = base level (62.5%, n=56) | ✔ defensible, small expectation; data thin structurally |
| **TD_1H** | 3,916 / 540d | **anti-calibrated** (0.0–0.3→75%, 0.7–0.8→44.8%); val PnL negative everywhere | just as flat (0.8+: 52.1%, n=169) | ✘ DO NOT deploy; park TD_1H or run without confidence |
| **BB_4H** | 13,334 / 540d | +5pp WR over base rate @0.60, monotone 0.3→0.7 — but test PnL −90% cumulative | probs collapse <0.5, no ranking | (✔) as a filter, return expectation neutral |
| **ABR1 LONG** | 77,398 / 365d (100 coins) | test WR = base rate, 0.8+ anti-calibrated (35.8%) | 99.5% of probs <0.3 → gate blind live | ✘ close LONG gate |
| **ABR1 SHORT** | 91,627 / 365d (100 coins) | 0.5→0.8 monotone (+2–4pp), operating point better 0.60–0.70 instead of val-0.75 | likewise blind | (✔) with bot rework to binary contract |

**Overall verdict:** no retrain delivers robust out-of-time return — WR rankings don't translate reliably into PnL. Confirms report 16: return comes from trade construction + regime, not from ML skill. Next real lever: exit geometry and MIS1 retraining on this scaffolding.

**Operational notes from the runs:** (a) Both long runs died after hours, within 3 minutes, from an externally killed DB connection (P1.33-class observed live) → simulator now has reconnect retry + `--resume`. (b) `coins.json` switched between the runs from 648 to 530 entries — **P2.16 confirmed live** (two writers with different filters). (c) The ABR1 detector without ML gate emits ~1,700 events/coin/year — a full-universe replay would be ~900k events; capped to the 100 most liquid coins (documented bias).

---

## Additional side finding: BB_1H parking only covers the LONG side

`25_smc_ml_sniper.py:254` gates `tf != '1h'` only in the breaker-block **LONG** branch; the SHORT branch (`:283`) has no TF gate → **BB_1H SHORT keeps firing live**, even though report 14/16 lists BB_1H as parked (−1,089 net). Fix is one line — recommendation: bring the SHORT branch in line or document the parking deliberately.

## Not done (and why)

- **MIS1 retrain** (report-16 priority #1): needs the 67-feature builder from `X5-analyze_indicators_v8.py` including removal of the leakage `line_cols`, horizon labels via the first-touch simulator and the R1 closed-candle discipline — its own, larger task; batch E now delivers the simulator + scaffolding for it.
- **AIM1/UFI1 retrain**: deliberately not done — both off recommendations (rationale above resp. report 16).
- **QM retrain**: trainer is now correct (P1.29/P1.30), but report 16 shows that QM's problem is the exit geometry, not the selection; exit redesign first, then retrain.
- **R1 (forming candle) / rest of monitor rewrite**: not part of batch E; the replay labels bypass both problems (own exits from candles, no monitor labels).
- **Funding costs in the simulator**: not modelled (multi-month UFI1 holds would even be mildly favoured in bear phases — shorts mostly receive funding); for TD/BB/ABR1 (hours-to-days holding time) subordinate.

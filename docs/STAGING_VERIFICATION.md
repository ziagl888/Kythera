# Post-Retrain Staging Verification

**Task:** T-2026-CU-9050-120 · **Tool:** `tools/verify_staging_artifacts.py` · **As of:** 2026-07-14

This checklist accompanies the verification of a retrain artifact **before** it is promoted. Promoting an artifact into the repo root (= live) is an
**explicit operator decision by Michi** (hard rule 2, escalation) — the tool only delivers the finding, it promotes nothing.

## Pipeline

```
walkforward_sim.py --strategy X   →  Replay-JSONL (REPLAY_DIR)
retrain_from_replay.py --strategy X →  Artefakt + Meta + retrain_<name>_stats.json   → STAGING_DIR
verify_staging_artifacts.py         →  Befund (mechanisch + C2-Metriken)
(Michi)                             →  Promotion in Repo-Root + Fleet-Restart
```

- **STAGING_DIR:** `C:\Users\Michael\Documents\_X\staging_models` (env `KYTHERA_STAGING_DIR`).
- **Serving env:** fleet Python `Python313_12` (3.13.12 / xgboost 3.1.2). Retrains
  MUST run in exactly this interpreter — otherwise Check 5 kicks in (xgb skew).

## Running it

```bash
python tools/verify_staging_artifacts.py             # alle Familien
python tools/verify_staging_artifacts.py --only td,bb
```

Read-only. No DB access, no live touch. Exit code **1** as soon as a
**mechanical** contract check is FAIL; metric WARNs are advisory (exit 0).

## Mechanical checks (the tool decides)

| # | Check | Rule | FAIL means |
|---|---|---|---|
| 1 | Artifact in STAGING_DIR, not repo root | HR-2 | wrong storage location |
| 2 | loads via `core.model_artifacts` + feature list == trainer/serving reference | HR-7 / P0.12 | feature drift → bot runs idle or on garbage input |
| 3 | `meta.model_id` == expected generation tag (TD2/BB2/ABR2/MIS2/RUB2/EPD2/ATB2) | HR-6 | bot posts under a fallback tag / old tag |
| 4 | `optimal_threshold` ∈ (0,1) | Contract | `None`/`1.0` = no gate (not-deployable side) |
| 5 | `meta.xgboost_version` == serving xgboost | P3.4 | major drift → silent `predict_proba` skew |
| 6 | Format B: `model_type` startswith `binary` + `_calib.pkl` | Contract | loader would read the wrong probability column |
| 7 | Model has `predict_proba` | Contract | no classifier loaded |
| 8 | Promotion slot is not double-booked (`tools/promotion_guard.py`) | HR-4 / HR-6 | a LIVE challenger reads the root slot of a foreign generation → double post |

### Check 8 — Challenger promotion name guard (T-2026-KYT-9050-057)

`shadow_gate.shadow_artifact_path` returns the bare root filename for a **LIVE**
leg. If a challenger tag (RUB3, EPD3, …) in `SHADOW_ARTIFACTS` carries the
filename of the retrain **generation** instead of its own, the promotion
occupies a slot that the **legacy loader** also reads — both tags post the
same model (rule-4 double trade). For **EPD3-SHORT** this was real on
2026-07-21 (`epd2_model_SHORT.pkl` = bot-10 `EPD2_ARTIFACT_PATHS["SHORT"]`)
and was averted by hand with the challenger-distinct name `epd3_model_SHORT.pkl`;
for **EPD3-LONG** (T-037) the same manual fix a second time.

The guard now checks this automatically — registry-based, without touching the filesystem:

| Case | Severity |
|---|---|
| Leg is **LIVE** and the filename belongs to a foreign tag | **FAIL** (exit 1, promotion stop) |
| Leg is SHADOW/SILENT/RETIRED, filename not challenger-distinct | WARN (latent blocker before the next flip) |
| a staging **file** is claimed by >1 tag | WARN per file (the intent isn't in the filename) |

```bash
python tools/promotion_guard.py            # Exit 1 nur bei FAIL
python tools/promotion_guard.py --strict   # WARNs ebenfalls als Fehler
```

It additionally runs as a pre-commit hook (`kythera-promotion-name-guard`)
and thereby blocks the commit that flips a leg to LIVE without renaming it.
**Open (WARN today):** `RUB3-LONG` still points to
`rub2_model_LONG.pkl` — harmless as long as RUB3 is parked (T-037); before
a RUB3 promotion, the artifact must be named `rub3_model_LONG.pkl`.

## C2 metric report (advisory — Michi decides)

From `retrain_<name>_stats.json` per model/direction: **test WR vs base
rate**, **ΣNet PnL**, **n**. A WARN row (below base rate, PnL ≤ 0, n < 30)
does NOT block — it's the input for the promotion decision. For the
full calibration assessment, also look at the
`calibration_new_test` buckets in the stats JSON (does
`tp1_rate`/`avg_net_pnl_pct` rise monotonically with confidence?).

## Operator gates (the tool does NOT decide)

- [ ] **Promotion = a deliberate Michi decision.** Copying STAGING_DIR → repo root is
      the only live touch. Never part of a training/verification run.
- [ ] **No tag reuse (HR-6).** The new tag (e.g. `MIS3`, `TD2_4H`) must not be
      that of a still-active old model — otherwise old/new mix together in the
      trackers. The bots' transitional dedup covers the generation switch.
- [ ] **Calibration beats the status quo.** C2 report + buckets: the new
      model must beat its base rate AND the old generation in the old/new
      comparison (`calibration_old_same_events`) — otherwise no rollout.
- [ ] **Rollout order.** By live relevance, one bot per step; parked
      bots (ATB1/BB_1H) stay parked until explicitly unparked.
- [ ] **After the promotion:** fleet restart, so the bot loads the new artifact
      (the 24h reload would otherwise only kick in with delay); the threshold comes
      from the meta, not from a hardcoded constant.
- [ ] **Follow the sequential-jobs rule** during training (only as many parallel
      sims as the CPU can carry — otherwise thread oversubscription).

## Worked example (staging state 2026-07-14)

The first run over that state's data found exactly the blockers the tool is
meant to surface:

- `td_xgboost_model_4h.pkl` — loads cleanly, but **test WR 59.2% < base 60.7%**
  → metric WARN, no-go until better.
- `bt2_model_{LONG,SHORT}.json` (ABR2 staging) — **`model_id` is missing** (HR-6 FAIL)
  **and** trained on **xgboost 3.2.0** (serving 3.1.2) → not promotable, regenerate
  with the current trainer.
- `rub2_model_LONG.pkl` / `epd2_model_LONG.pkl` — `threshold=None` → not loadable
  (known not-deployable LONG sides, correctly marked as FAIL).
- `bb_1h/4h`, `rub2_SHORT` — mechanically clean, metrics positive.

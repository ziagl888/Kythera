# T-2026-KYT-9050-006 — TD2/BB2/QM2 Retrain: Verdicts and Rollout Recommendation

**As of:** 2026-08-01 · **Session:** VPS (SRV02), live tables exclusively read-only ·
**Predecessor:** T-2026-CU-9050-061 (recompute + retrain rerun), roster validation 2026-07-14,
`_X\staging_models\T061_RETRAIN_REPORT.md` (2026-07-12, pre-Wilder state)

## Mandate and outcome in one paragraph

Under review were the artifacts from the post-Wilder retrain rerun of 2026-07-14 (Phase A
`bb_1h`, Phase B QM2/`td_4h`/`bb_4h`/`td_1h`), plus the QM2 gap in the replay retrain path.
**The TD/BB artifacts from this rerun no longer exist** — they were overwritten the same
day, minutes after being generated, by a legacy trainer run that uses the same file name in
the same staging directory. Only their metrics survive. Those metrics still carry the
verdict, and it is **NO-GO for all four**: TD_1H is anti-calibrated (confirms the Batch-E
prior finding), BB_1H/BB_4H flip sign between validation and test, TD_4H has a selection
value near zero. Reconstruction is not worth it: the only replay-retrained generation that
was ever live (BB2_4H, 06.–13.07.) realized **-1.57% per leg over 99 legs**, while the
legacy artifact that it replaced — and that replaced it again — books **+0.25% per leg over
3.076 legs** across five months. QM2 is **deliberately excluded, with reasons given**.
Nothing promoted, nothing deployed, no gate touched.

---

## 1. The central finding: the rerun artifacts were overwritten

`tools/retrain_from_replay.py:423` and `smc_ml_trainer.py:376` write to **the same path**:

```
retrain_from_replay.py:423   STAGING_DIR/{strategy}_xgboost_model_{tf}.pkl
smc_ml_trainer.py:376        STAGING_DIR/{prefix}_xgboost_model_{tf}.pkl
```

Timeline on 2026-07-14 in `C:\Users\Michael\Documents\_X\staging_models\` (mtimes read off
directly):

| Time | Event |
|---|---|
| 02:47:20 | `retrain_td_4h_stats.json` |
| 04:07:08 | `retrain_bb_4h_stats.json` |
| 04:33:07 | `retrain_td_1h_stats.json` |
| 05:21:24 | `retrain_bb_1h_stats.json` |
| **05:21:40 – 05:23:50** | **all four `{td,bb}_xgboost_model_{1h,4h}.pkl` rewritten** |

`retrain_from_replay` writes the pkl **before** its stats file (`run_td_bb` →
`save_artifact`, then `main` → stats). A pkl whose mtime is **after** its own stats file is
therefore overwritten — by 2.6 hours for `td_4h`, by 16 seconds for `bb_1h`.

The content proves it independently of the timestamp. The four current files carry
`meta.trainer = 'smc_ml_trainer.py'`, `optimal_threshold = 0.3`, **no** `calibrator_isotonic`
and **no** `meta.model_id`. `retrain_from_replay.save_artifact` always writes both keys
(`:376-385`, `model_id` at `:410`). The artifacts therefore do not come from the rerun.

**Consequence:** the rerun ran and was measured, but its product is gone. What sits in
`staging_models/` and the repo root is the legacy generation.

### 1a. The legacy trainer labels against the P0.10 rule

Not just "a different generation", but the generation that was supposed to replace the
replay program: `smc_ml_trainer.py:153/185` labels against a **synthetic 2R bracket**
(`RR_RATIO = 2.0`, `tp = entry ± dist * RR_RATIO`) — not against the posted Smart Targets.
Exactly the idealized fill that P0.10 forbade, and the reason `tools/walkforward_sim.py` was
built in the first place.

### 1b. The overwrite incident silently reverted the model tag

`25_smc_ml_sniper.py:101/117` takes the posting tag from `meta.model_id` and otherwise falls
back to `{STRATEGY}_{TF}`. In `ml_predictions_master` (read-only) the switch is visible:

| Tag | n | first | last |
|---|---|---|---|
| `TD2_4H` | 118 | 2026-07-06 | **2026-07-13** |
| `BB2_4H` | 1338 | 2026-07-06 | **2026-07-13** |
| `TD_4H` | 112 | 2026-06-25 | 2026-08-01 |
| `BB_4H` | 7992 | 2026-06-25 | 2026-08-01 |

The replay-retrained generation was live from 06. to 13.07. and was then replaced by the
legacy artifacts — the tags fell back to the old names. Confirmed by
`_X\live_backup_20260714_194105\`, which contains exactly the *previous* live artifacts
(sizes 802727/739062/671759/586996 = the "before" side of commit `14e1c6f`).

**Rule-6 assessment:** the tag fallback is not a Rule-6 violation — the bot behaves
correctly, the artifact simply carried no `model_id`. The violation sits one level deeper: a
generation handover happened without a ledger trace.

### 1c. Countermeasure in this PR

`core/staging_guard.assert_no_foreign_overwrite` refuses to overwrite an artifact whose
`meta.trainer` differs from that of the currently running trainer — wired into all three
writers (`retrain_from_replay`, `smc_ml_trainer`, `qm_ml_trainer`). Deliberately
**fail-open**: missing or unreadable provenance blocks nothing, only a proven collision.
Deliberate override: `KYTHERA_ALLOW_TRAINER_OVERWRITE=1`. Pinned in
`backtest/test_staging_guard.py` (8 tests, DB-free), including the real 07-14 case in both
directions.

---

## 2. What is live today (verified directly, not from docs)

SHA256 comparison repo root ↔ `_X\staging_models` ↔ git HEAD, plus pkl internals:

| Artifact | Root == Staging | `meta.trainer` | `optimal_threshold` | Live gate | Tag |
|---|---|---|---|---|---|
| `td_xgboost_model_1h.pkl` | identical | `smc_ml_trainer.py` | 0.30 | **0.30** | `TD_1H` |
| `td_xgboost_model_4h.pkl` | identical | `smc_ml_trainer.py` | 0.30 | **0.30** | `TD_4H` |
| `bb_xgboost_model_1h.pkl` | identical | `smc_ml_trainer.py` | 0.30 | **0.50** | `BB_1H` |
| `bb_xgboost_model_4h.pkl` | identical | `smc_ml_trainer.py` | 0.30 | **0.50** | `BB_4H` |
| `qm_xgboost_model_1h.pkl` | **different** | (no meta) | 0.30 | **0.65** | `QM_1H` |
| `qm_xgboost_model_4h.pkl` | **different** | (no meta) | 0.30 | — (parked) | — |

Gate derivation: `25_smc_ml_sniper.py:93-99` takes `max(optimal_threshold, MIN_PROB_FLOORS)`,
BB floor 0.50 (T-171), TD floor 0.0 — the BB floor therefore **raises**
both BB gates above the artifact value. Bot 24 ignores `optimal_threshold` entirely and
gates on the hardcoded `MIN_CONFIDENCE = 0.65` (`24_quasimodo_bot.py:45/321`). QM_4H is
parked in code (`TIMEFRAMES = ['1h']`, `:42`, audit report 14/16) — the empty QM_4H trace
since 03.07. is not a defect.

---

## 3. Verdicts from the rerun metrics

From `retrain_{td_1h,td_4h,bb_1h,bb_4h}_stats.json` (2026-07-14). Threshold selection for
td/bb runs via `pick_threshold` (summed PnL), **not** via `pick_threshold_safe` — the
migration never reached td/bb (`retrain_from_replay.py:401` without `picker=`, versus
`:611/694/768/841/917` for mis1/rub/ats/epd/atb2).

| Model | Thresh | Val Σ PnL | Test taken | Test Σ PnL | Test WR vs. base | Verdict |
|---|---|---|---|---|---|---|
| **TD_1H** | 0.80 | **-78.2** (n=48) | 33/462 (7%) | **-75.2** | 57.6% vs 56.5% | **NO-GO — anti-calibrated** |
| **TD_4H** | 0.50 | +9.7 (n=59) | 76/122 (62%) | +19.4 | 59.2% vs **60.7%** | **NO-GO — selection value ~0** |
| **BB_1H** | 0.40 | +379.6 (n=5588) | 5603/5684 (**99%**) | **-241.2** | 58.3% vs 58.1% | **NO-GO — gate is a no-op** |
| **BB_4H** | 0.50 | +489.2 (n=871) | 1012/1336 (76%) | **-686.0** | 57.6% vs 54.7% | **NO-GO — filter-only, confirmed** |

Three findings carry the verdict:

**(a) TD_1H is anti-calibrated.** Test calibration runs against probability: bucket
0.0–0.3 → **+4.04%** avg. net PnL, bucket 0.8–1.0 (= the live gate) → **-2.28%**, the worst
of all seven buckets. Validation was already negative, and `pick_threshold` still returns a
threshold because it — unlike `pick_threshold_safe` (`:300-302`) — lacks the deployability
abort. Identical diagnosis to Batch E and to the pre-Wilder run of 12.07. **Reproduced three
times independently.**

**(b) Val→Test flips sign in three of four** (BB_1H +380→-241, BB_4H +489→-686, TD_4H only
barely positive). The threshold chosen on validation does not generalize.

**(c) The BB_1H gate does not select.** At threshold 0.40 it takes 98.6% of test events —
exactly the degeneration the code itself describes at `pick_threshold_safe:274-278`
("rewards volume → degenerates into take-almost-all").

Recomputing the buckets against the **actual live gate** (BB floor 0.50 instead of 0.40),
the BB_1H loss largely disappears (Σ ≈ -37 over n = 4.848, ≈ -0.01%/trade) — BB_1H would
therefore be live more flat than harmful. BB_4H stays unchanged at 0.50, at -0.68%/trade,
TD_1H unchanged at 0.80, at -2.28%/trade.

### Comparison with the pre-Wilder run (12.07.)

The Wilder rewrite **worsened** the cohort, not improved it:

| Model | 12.07. (pre-Wilder) | 14.07. (post-Wilder) |
|---|---|---|
| TD_4H | 110/136, WR 66.4% vs 65.4%, **+185.8** → "promote" | 76/122, WR 59.2% vs **60.7%**, +19.4 → NO-GO |
| BB_4H | 733/1289, WR 58.0% vs 54.1%, -604.9 → filter-only | 1012/1336, WR 57.6% vs 54.7%, -686.0 → filter-only |
| TD_1H | 39/505, Val -69.5 → "park" | 33/462, Val -78.2, Test -75.2 → NO-GO |

The only promotion recommendation from the 12.07. report (TD2_4H) no longer holds on the
Wilder distribution.

### Confidence in the replay numbers (T-2026-KYT-9050-008 check)

As instructed, checked whether the artifacts were created **before** or **after** the
T-008 fix: **before** (14.07. vs. fix on 01.08.) — but with no effect on this cohort. The
epoch defect sits in `walkforward_sim.py:889` and hits exclusively `slope_trend` in the RUB
regression; the td/bb replays run via `run_td_bb` (`:475`) and the 20 `SNIPER_FEATURES`,
which contain neither `slope_trend` nor `dist_to_trend`. `epoch_seconds` does not occur in
the td/bb path. In addition, per T-008 the fix is byte-identical to the prior state under
the fleet interpreter. The two look-ahead fixes (`ac49bc3`, `21a97a6`, both 10.07.) precede
the rerun. The replay curve therefore holds here.

---

## 4. Live counter-check: what the models actually booked

`closed_ai_signals`, deduplicated via `tools/fleet_realized_audit.load_ai_rows`
(DISTINCT ON `symbol, model, direction, open_time`), realized **unleveraged**
target-staggered move per leg — the canonical fleet definition (T-115). Deliberately
unleveraged: the `lev` column is only populated from mid-July onward, a leveraged PnL would
have shortened the window to 2.5 weeks.

| Tag | n | WR | Avg. move/leg | Window |
|---|---|---|---|---|
| `TD_4H` | 697 | 38.6% | **+1.080%** | 03-09 .. 08-01 |
| `TD_1H` | 2538 | 39.0% | **+0.906%** | 03-07 .. 08-01 |
| `BB_4H` | 3076 | 41.9% | **+0.249%** | 03-07 .. 08-01 |
| `QM_1H` | 3175 | 36.3% | +0.065% | 03-06 .. 08-01 |
| `BB_1H` | 4093 | 31.9% | **-0.256%** | 03-07 .. 07-27 |
| **`BB2_4H`** (replay retrain, live 06.–13.07.) | 99 | 36.4% | **-1.572%** | 07-10 .. 07-29 |

The low WR with a positive average is expected: staggered TPs book many small partial gains
as a "losing leg" whenever the remainder runs back to the SL.

**This is the hardest number in the report:** the only replay-retrained generation that was
ever in production lost money — consistent with its own negative test slice (Σ -686).
n = 99 over three weeks is small and could be tape; but the direction matches the replay
measurement, so two independent negatives.

### Direction split — and a sign conflict with the study

| Tag | LONG n / avg. | SHORT n / avg. |
|---|---|---|
| `TD_1H` | 1468 / **+1.523%** | 1070 / +0.059% |
| `TD_4H` | 433 / **+1.301%** | 264 / +0.718% |
| `BB_4H` | 1297 / **+1.216%** | 1779 / **-0.456%** |
| `BB_1H` | 1727 / **+1.345%** | 2366 / **-1.425%** |
| `QM_1H` | 1605 / +0.422% | 1570 / **-0.300%** |

The roster validation from 14.07. (`_X\staging_models\significance\{TD,BB}{1h,4h}.json`,
1000 bootstraps over the 540d replays) says the opposite for **every** cell: `*/LONG`
p_value 0.993–1.000 and `sharpe_prob_positive = 0,0`, `*/SHORT` p 0.001–0.002 and
`prob_positive` 0.999–1.0 (exception TD4h-SHORT: p 0.25, not significant — the "TD4h-SHORT
dead" prior finding is confirmed).

**Live, LONG is the side that carries; in the replay, it's SHORT.** The conflict is real and
not explained by the time window alone (5 months live vs. 540 days replay, overlapping). Two
structural candidates, both visible in the code, neither proven here:

1. **Different populations.** The replay scores *all* detector signals; live, only what
   passes the model gate, prob floor, cooldown and orchestrator whitelist survives.
2. **Different economics.** The replay labels first-touch TP1-before-SL and computes
   `net_pnl_pct` on this binary geometry; live, the monitor books staggered partial TPs with
   trailing. A threshold optimized on replay `net_pnl` therefore does **not** optimize the
   quantity that the fleet actually realizes.

As long as this conflict is unresolved, a promotion based on replay PnL is not defensible —
regardless of how the metrics turn out. This is its own investigation scope and **not** a
byproduct of this task.

---

## 5. Rollout recommendation (operator decision: Michi — none of it executed)

| Model | Recommendation | Rationale |
|---|---|---|
| **TD_1H** (replay retrain) | **Do not reconstruct, do not promote** | Anti-calibrated, reproduced three times; Val negative |
| **TD_4H** (replay retrain) | **Do not reconstruct, do not promote** | Selection below base rate; pre-Wilder recommendation no longer holds |
| **BB_1H** (replay retrain) | **Do not reconstruct, do not promote** | Gate takes 99% — not a gate |
| **BB_4H** (replay retrain) | **Do not reconstruct, do not promote** | filter-only confirmed; live -1.57%/leg as BB2_4H |
| **QM2_1H / QM2_4H** | **Do not promote** | see §6 — no replay path, bot ignores the threshold |
| **Live inventory TD/BB/QM** | **Leave unchanged** | Positive over 5 months (except BB_1H); no better candidate exists |

**The TD/BB replay-retrain line is thus closed as NO-GO.** Three runs (06.07., 11./12.07.,
14.07.) produced no deployable candidate; the one that went live lost money. A fourth run on
the same methodology is not a sensible investment while §4 remains open.

### The lever that would actually move the needle instead

Not the model, but the **direction**. Three cells are negative over 5 months and large
sample sizes:

| Candidate | n | Avg. move/leg | Rough annual effect at the same volume |
|---|---|---|---|
| `BB_1H` SHORT | 2366 | -1.425% | -3.371 leg percentage points within the measurement window |
| `BB_4H` SHORT | 1779 | -0.456% | -812 |
| `QM_1H` SHORT | 1570 | -0.300% | -471 |

Parking SHORT on these three legs is a one-line intervention per bot, reversible, and
targets a proven loss source — unlike another retrain. **But:** it directly contradicts the
replay study (§4) and the fleet-wide short-only line from the roster validation.
**Therefore explicitly framed as a proposal for decision, not a recommendation to execute**
— parking/unparking is a C-gate matter in any case (OPUS-HANDOFF §6).

---

## 6. QM2 gap: deliberately excluded, with reasons given

**Decision: no replay-retrain path for QM2. Exclude it, do not build it.** Four pieces of
evidence from the code, not a claim:

1. **The path is missing from both tools.** `tools/walkforward_sim.py:1151`
   `choices=["ufi1","td","bb","abr1","mis1","rub","atb2","ats"]` and
   `tools/retrain_from_replay.py:980` `choices=["td","bb","abr1","mis1","rub","epd","atb2","ats"]`
   — both without `qm`. Building it would mean: a `run_qm` with the Quasimodo detector over
   540 d × 527 coins plus a `qm` branch in the trainer. For scale: the `bb_1h` replay of the
   same cohort is 48 MB of JSONL.
2. **The main product of a replay retrain never reaches the bot.**
   `24_quasimodo_bot.py:45` hardcodes `MIN_CONFIDENCE = 0.65` and gates on it (`:294/321`);
   `optimal_threshold` from the artifact is never read. The threshold calibrated on
   validation PnL — the actual value-add of the path — would be ineffective unless Bot 24 is
   also changed. (Known as part of AUDIT_TODO P3.6: "thresholds in the pkl but bots
   hardcode").
3. **Half the surface is parked anyway.** `TIMEFRAMES = ['1h']` (`:42`) — QM_4H has been
   idle since audit report 14/16. A QM2_4H would have no consumer.
4. **The sibling strategies of the same bot pair say NO-GO.** td and bb share the feature
   set (`SNIPER_FEATURES`), detector family and replay machinery with qm. Three runs there
   produced no deployable candidate (§3/§5). The expectation that qm, as a fourth variant of
   the same pipeline, would break the pattern is not justified.

Economically: QM_1H books live +0.065%/leg over 3.175 legs — practically zero EV, and with
31 posts in five weeks (`ml_predictions_master`) the smallest surface in the cohort. The
expected return of a rebuild is not proportionate to the effort.

**What a later build would need** (in case Michi wants it after all, as a prerequisite
list): (1) `run_qm` in `walkforward_sim`, (2) a `qm` branch in `retrain_from_replay` with
`picker=pick_threshold_safe`, (3) Bot 24 reads `optimal_threshold` instead of
`MIN_CONFIDENCE`, (4) resolve §4 first — otherwise the path optimizes the wrong target
quantity.

The QM2 artifacts from the legacy trainer (`_X\staging_models\qm_xgboost_model_{1h,4h}.pkl`,
14.07., `model_id` QM2_1H/QM2_4H, thresholds 0.55/0.50) survived the overwrite incident —
they remain in staging and stay unpromoted.

---

## 7. bfill: NOT touched — what a later rollout must carry along

As instructed, **no change**. For the record, with corrected line numbers (the `:126` /
`:220` cited in the ticket are stale):

| Location | current line | Context |
|---|---|---|
| `24_quasimodo_bot.py` | **:140-141** | `df.ffill(); df.bfill()` after `read_candles_with_indicators(limit=100)` |
| `25_smc_ml_sniper.py` | **:311-312** | same, `limit=150` |
| Counterpart | `tools/walkforward_sim.py:263-270` | `ffill()` + **`dropna()`** instead of bfill, with rationale since T-045 |

The replay **discards** the warmup head rows, the bots **impute** them from the future. As
long as the bots run on artifacts trained on imputed head rows (= today, the legacy
generation), this is symmetric and must not be removed in isolation. When rolling out a
replay-trained artifact, both `bfill` calls must be dropped **in the same step**, or serving
will see a row class training never saw.

Since §5 recommends "do not promote", **the bfill stays as is** — the coupling point is
hereby only documented. Practical constraint that lowers the urgency: both bots read only
the most recent 100 and 150 closed candles respectively, so the window only contains NaN
head rows if the coin's overall history is short — the population is young listings, not
the existing roster.

---

## 8. Open PR-43 finding for Michi (numbers, no decision)

Train/serve skew window on new listings between deploy and retrain. Current status:

- **The skew still exists**, because both sides are unchanged: the engine has written NaN
  head rows since T-054, and the bots impute them via `bfill` (§7).
- **The population** is coins below the warmup threshold — at `limit=150` (Bot 25) and
  `limit=100` (Bot 24), that means coins with less than ~350 and ~300 candles of history
  respectively (1h ≈ 15 and 12 days, 4h ≈ 58 and 50 days). Exact coin count **not
  measured** — would have needed a count query across all `_indicators` tables, which would
  have made sense outside the read-only scope of this session.
- **Mitigation options (a)/(b)/(c) remain unchanged and valid.** What's new is only that
  option (c) (skip coins below the warmup threshold) is now the only way to change anything
  **without** a retrain rollout — and §5 recommends precisely no rollout. This means (c)
  permanently decouples the window from the retrain program.
- **Order-of-magnitude from a neighbouring case:** T-2026-KYT-9050-008 first measured the
  related mixed-history risk on RUB2-SHORT — ≈ 1 percentage point of probability drift. Not
  proof for the P1.13 case, but the only existing calibration of the order of magnitude.

---

## 9. Deliberately NOT done

No promote, no rollout, no deploy, no restart, no parking/unparking, no gate flip, no write
query against live tables, no replay or training run (so no job lock needed either — no
heavy job ran). The `bfill` in Bot 24/25 is untouched. The overwritten artifacts were
**not** reconstructed (§5 does not recommend it, and a replay run would have been a heavy
job). `staging_models/` in the repo is unchanged.

## 10. Open

- The sign conflict between replay and live on the direction axis (§4) is **not** resolved.
  It invalidates replay PnL as a promotion criterion for this bot family until it is
  clarified which of the two structural causes is responsible. A separate task candidate.
- Whether the SHORT park (§5) is carried out: Michi.
- PR-43 options (a)/(b)/(c): Michi.
- `pick_threshold` for td/bb was never migrated to `pick_threshold_safe`. Since the line is
  closed, this is deliberately **not** retrofitted here — a migration with no consumer would
  be dead code. If the line is resumed, it is the first step.

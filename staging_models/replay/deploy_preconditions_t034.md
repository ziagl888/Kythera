# Deploy preconditions for T-033 — implementation/findings report (T-2026-KYT-9050-034)

_generated 2026-07-23 · INTERACTIVE session (operator Michi live) · code + staging artifacts · NO deploy/restart/env flip · DB strictly read-only (`set_session(readonly=True)`, only SELECTs) · NO artifact root move (hard rule 2) · basis: `staging_models/replay/fleet_reconfig_t033.md` §3/§5_

## 0. Core finding (for Michi)

The three deploy preconditions flagged by T-033 were examined read-only (all DB access `set_session(readonly=True)`, only SELECTs). Result:
- **Package 3 (EPD3 staging): done** (copied to staging, loader verified).
- **Package 2 (SRA2-SHORT): diagnosis corrected — the leg is UNGATED PROFITABLE (+1.06%/trade, 232 trades).** The T-033 "flood hazard" concern confused volume with unprofitability; a threshold is neither needed nor determinable from the data. → deployable, the open question is volume tolerance.
- **Package 1 (MIS1): REVIVED (code done, operator decision Michi).** No retrain — the MIS1 generation is restored EXACTLY: bot 11 reloads the unchanged `pump_model_*_final.pkl` (+ `threshold_*_final.pkl`), fed through the include_legacy superset. The good legs (MIS1-24H/72H/168H LONG + MIS1-8H SHORT) are default LIVE, the weak ones parked SHADOW. Takes effect at Michi's next fleet restart.

## 1. Package 3 — EPD3-SHORT staging ✅ DONE

- **Fix:** `epd3_model_SHORT.pkl` (root) → copied to `staging_models/epd3_model_SHORT.pkl` (staging is allowed, hard rule 2 only gates root).
- **Verified:** `shadow_gate.load_shadow_artifact("EPD3","SHORT")` now loads (dict, 16 features, threshold 0.6737). Before: the EPD3-SHORT park was silently dead because the SHADOW loader read `staging_models/epd3_model_SHORT.pkl` (missing). Now there is real shadow history in `closed_ai_signals`.
- No root move, no restart needed (the bot loads at the next regular restart/reload).

## 2b. Package 1 — MIS1 revive: IMPLEMENTED (exact restoration, no retrain)

**Operator decision Michi (round 2):** restore MIS1 EXACTLY at its win rate. The artifacts were never gone (`pump_model_*_final.pkl` + `threshold_*_final.pkl` in the repo root), and the old bot-11 load path lives in git history (`99e9de3^`). No retrain needed.

**Implemented (bot 11 + shadow_gate):**
- Bot 11 loads the 8 MIS1 models again (`load_mis1_models`), IN PARALLEL with MIS2 under their own tags `MIS1-*`. Feature feed via `add_advanced_features(include_legacy=True)` — the superset (71 columns) covers the 67 MIS1 features exactly (verified 0 missing across all 8) AND the 63 clean MIS2 features (additively neutral, ONE feature build per coin, no duplicate DB read).
- Geometry stays generation-faithful: `_mis_geometry` gives MIS1 `calculate_smart_targets` for BOTH directions (immediate CMP entry) — exactly the path that produced the audited win rate; MIS2-SHORT keeps its DUMP_RULES bracket. MIS2 emission stays byte-neutral (shared `_post_mis_live_leg` helper, MIS2 tests green).
- Lifecycle in the shadow_gate register: MIS1 removed from `_RETIRED_TAGS`; good legs default LIVE (MIS1-24H/72H/168H LONG + MIS1-8H SHORT), weak ones SHADOW (MIS1-8H LONG + MIS1-24H/72H/168H SHORT). Exactly ONE live generation per (horizon, direction) → no Cornix double post; MIS1 revives exactly the MIS2 legs parked by T-033.

**Two mandatory deviations from the old fidelity (hard rules, deliberately NOT reproduced):** (1) the old HTML message embedded the Cornix block = double-post bug (rule 4, fixed 2026-07-06) → fixed HTML; (2) old MIS1 stored full targets instead of `[:5]` (P2.31 monitor phantom-TP bug) → `[:5]`. **Caveat:** `calculate_smart_targets` was rewired to `core.candles` since the MIS1 era (5856bc6) — functionally equivalent, not guaranteed byte-identical; it is the same function the fleet uses today.

**Tests (DB-free):** `backtest/test_mis1_revive.py` (load + threshold + 67-feature coverage + geometry branching), `test_shadow_gate.py::test_mis1_revive_lifecycle`, `test_mis_tag.py` adapted to the shared processor. ruff + mypy clean. Deploy = Michi's fleet restart.

## 2. (Prior finding) Why MIS1 as a pure retrain did NOT work

**Feature compatibility check (DB-free, all 8 artifacts):** The MIS1 `pump_model_*_final.pkl` are bare `XGBClassifier`s with **67 features** and each consume all **8 leakage columns** (`atr_14` raw, `macd_hist` raw, `macd_dif_delta_1`, `macd_hist_delta_1` plus the 4 "accident" features `boll_upper/lower/ema_200_dist_atr_dist_pct`, `ema_9_cross_above_21_dist_pct`) = exactly the price-class leakage from Report 13-P1 (`core.mis_features.LEGACY_ONLY_COLS`).

| Artifact (all 8) | n_features | Type | Missing vs. clean builder | Uses leakage columns |
|---|---|---|---|---|
| pump_model_{8,24,72,168}h_{pump,dump}_final.pkl | 67 | XGBClassifier | 8 | 8 |

The current builder (`core/mis_features.py`, `include_legacy=False`) only delivers the 63 clean features → the P0.12 self-check in bot 11 would **unload** every MIS1 model. Wiring with `include_legacy=True` would mean letting leakage models post live — exactly what the self-check prevents (= "fake", per the task brief).

**Why a clean retrain ≠ MIS1 revive:** The clean MIS pipeline (`tools/mis1_move_labels.py` → `tools/retrain_from_replay.py --strategy mis1 --label-mode move`) is **exactly the pipeline that produced MIS2** — the same ±X% move-label concept (8h±5% / 24h±10% / 72h±15% / 168h±25%). The only difference MIS1→MIS2 was the leakage-feature cleanup. A clean "MIS1" retrain **reproduces MIS2** (already exists, per the audit T-032 performs worse). The "MIS1 better" edge lived in the leakage features → **not reconstructable cleanly.**

**Operator decision:** "run a fresh MIS2 move retrain, start now (BELOW_NORMAL)". **Blocker (confirmed):** no current MIS replay artifact in `staging_models/replay/`; the existing ones (`_X/…/mis1_replay_{400,540}d.jsonl`, `mis1_move_labels.jsonl`) are **from July 5th** → a retrain on those deterministically reproduces the current MIS2 root artifacts (no added value). A genuinely fresh MIS2 needs a **replay regeneration (`walkforward_sim --strategy mis1`)**. The job was started detached/low-prio and **aborted itself**: `ABBRUCH: System-CPU bei 100% (> 90%) — Fleet nicht zusätzlich belasten` (`MAX_CPU_AT_START=90.0`). The VPS is currently fully saturated → the replay is **not runnable right now**; it needs a quiet CPU window (at night / after CPU relief). Follow-up task, see §4.

## 3. Package 2 — SRA2-SHORT: the "flood hazard" diagnosis was wrong — the leg is ungated PROFITABLE

**Why a retrain/threshold is the wrong lever (data situation, read-only DB):**
- Old label source `closed_trades3`: **dead since 2026-02-23** (0 trades in 60d) → `retrain_sra2.py` straight-up reproduces the null-threshold model (val −0.079% is a **Feb-regime proxy**, not reality).
- Retrain on the fresh source `closed_ai_signals` (operator decision "lower the guard"): SRA2-only 232 trades / 8-day window → val too thin; pooled SRA1+SRA2 641 → `pick_threshold_safe`=**None**. **Reason:** the base rate is already **90% WR / +1.06%/trade** — a prob threshold cannot beat that and the 8-day history does not carry a robust split. A threshold here is **neither needed nor determinable.**

**The decisive finding (realized shadow history, `closed_ai_signals`, net = (entry−close)/entry − 0.10% fees, matches the audit "+1.00%×222"):**

| SRA2-SHORT filter | n | WR | avg net/trade | Σ net |
|---|---|---|---|---|
| **NO gate (post every candidate)** | 232 | 90.5% | **+1.057%** | +245% |
| fund_24h ≤ 0 | 44 | 95.5% | +1.423% | +63% |
| fund_24h ≤ +1.5 | 204 | 91.2% | +1.048% | +214% |
| fund_24h ∈ [+1.5,+3) (ABR "veto zone") | 15 | 86.7% | +1.498% | +23% |

The `threshold=null` "flood" realizes **+1.057%/trade** over 232 trades. The T-033 concern "LIVE posts on every candidate → Cornix flood" confused **volume** with **unprofitability** — the "flood" IS the edge. The negative val signal (−0.079%) came solely from the dead Feb label source.

**Funding gate (operator question):** does not save any edge (it's already there), only trims **volume**. `fund_24h≤0` lifts it to +1.42%, but cuts to 44/232. The ABR "SHORT veto" zone (fund>+1.5 bps) is **positive** for SRA2-SHORT (+1.5%) → the ABR veto does NOT apply here. The edge is broadly positive across all funding zones.

**Conclusion/recommendation:** SRA2-SHORT is **deployable** — it needs NO threshold, because the raw signal realizes +1.06%/trade. The only real remaining topic is **volume** (~29 posts/day ungated) for the Cornix channel — an operator tolerance decision, not a code/model defect. Options: (a) promote ungated to root (Michi, hard rule 2) and accept the volume; (b) an optional additive funding/volume gate in the bot-9 SRA2-SHORT emit as a pure volume brake (its own small code task — NOT edge-necessary).

## 4. Open operator decisions (Michi-gated)

1. **MIS1 revive (code done, §2b):** takes effect only after `tools/restart_fleet.ps1` / watchdog restart (Michi). After the restart, check the log for: `✅ 8/8 MIS1-Modelle (Revive) loaded` + `n MIS2 + 8 MIS1 Modelle kompatibel` (self-check). The MIS1 live legs then post to the MIS_CHANNELS.
2. **SRA2-SHORT promotion (deployable!):** no threshold needed (+1.06%/trade ungated). The decision is **volume tolerance** (~29 posts/day): (a) promote `sra2_model_SHORT.json` to root and go live ungated; OR (b) an optional additive funding/volume gate in the bot-9 emit (its own small code task). Root move = Michi (hard rule 2).
3. **MIS2 alongside:** MIS2 keeps running unchanged (the MIS1 live legs occupy exactly the MIS2 legs parked by T-033; MIS2-SHORT 24/72/168 stays live). An optional fresh MIS2 move retrain (replay regen, needs a quiet CPU window — the job aborted itself at CPU 100%) is NO LONGER needed for the MIS1 revive; only if you want to refresh MIS2 separately.

## 5. Safety contract (rule 1/2/4)

- DB strictly read-only (`set_session(readonly=True)`, only SELECTs; DB user `dbfiller`, but session read-only enforced).
- Only file write in the repo: `staging_models/epd3_model_SHORT.pkl` (staging, allowed). No root move, no restart, no env flip.
- Retrain prototypes ran locally (scratchpad), wrote NO staging artifact (result not deployable → nothing to stage).

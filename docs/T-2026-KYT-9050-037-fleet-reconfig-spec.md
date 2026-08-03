# T-2026-KYT-9050-037 — fleet reconfig from bot_results.xlsx (Michi's request)

**Status:** in_progress · **Priority:** high · **Predecessor:** T-033 (fleet reconfig audit T-032), T-034
**Source:** `C:\Users\Michael\Downloads\bot_results.xlsx`, column **"Todo"** = Michi's authoritative wish.
**Live context:** VPS session, live DB read-only diagnosed 2026-07-24. **This is where real money moves.**

---

## 0. Starting point / how the diff was produced

Michi's wish column was compared leg by leg against the **current live register status** (`core.shadow_gate.leg_status(tag, dir)`). **~40 legs already match** (T-033 already implemented a lot, including Michi's overrides where he KEEPs against the recommendation: 5Percent, FastInOut, SR, VolIndic stay live). **8 open deltas** remain (below).

`✅/❌` semantics of the table: ✅ = LIVE (Cornix post), ❌ = not live (SHADOW/parked). One-direction bots stand at the default-`live` register value on the unused direction but never emit it (empirically n=0/60d) — **no** action needed, purely cosmetic.

---

## 1. Deltas (actual → target)

| # | Bot | Actual (register) | Target (todo) | Intervention class | Status |
|---|---|---|---|---|---|
| 1 | **RUB1** | "live" — but actually posts as **RUB2** | KEEP both (live under RUB1) | B code + A gate | **actionable** |
| 2 | **RUB3-SHORT** | live (inert) | SHADOW both | A gate | **actionable** |
| 3 | **ATB2-LONG** | shadow | KEEP long | A gate (+ artifact) | **BLOCKED** |
| 4 | **EPD3-LONG** | shadow | KEEP long | A gate (+ artifact) | **BLOCKED** |
| 5 | **ATS2** | live (config) ✓, artifact not loaded | ACTIVE | D restart | **restart-gated** |
| 6 | **AIM2-TOPN** | live | RETIRE + delete trades | A gate + C DB-DELETE | **actionable + operator** |
| 7 | **ATS1_Robust** | live | RETIRE + delete trades | A gate + C DB-DELETE | **actionable + operator** |
| 8 | **Main channel** | live (default, tag not in register) | SHADOW both | clarification | **needs-clarify** |

Intervention classes: **A** = gate flip in `core/shadow_gate.py` · **B** = code change bot 13 · **C** = live-DB delete (irreversible, hard rule 1) · **D** = fleet restart (deploy).

---

## 2. Delta details + specifications

### #1 — RUB1 live again (LONG + SHORT) under tag RUB1
**Actual:** bot 13 (`13_ai_rub_bot.py`) posts under tag **RUB2**:
- LONG = legacy RUB1 model `long_reversion_model.joblib` (root, md5 `0227bb4a…`, **identical** to the v2 checkout `crypto_trading_bot_v2/`), tagged RUB2 (`RUB_LONG_TAG = "RUB2"`, line 50).
- SHORT = RUB2 retrain `rub2_model_SHORT.pkl` (newly trained: `strategy=rub2`, +6 funding features, xgb 3.1.2, `optimal_threshold=0.7929`, deployable).
- The legacy SHORT loader was removed in **PR #9** ("falsified"). Legacy SHORT `short_reversion_model.joblib` sits in root (md5 `16ca3711…`, **identical** to v2), but is **not loaded**.

**Target (Michi, xlsx):** RUB1 posts both directions **live** with the **original legacy models**, tagged **RUB1** (LONG 2.48% / SHORT 0.78%, historically both positive). RUB2 retrain gets benched (wish RUB2 = SHADOW both, already matches the register).

**Implementation (bot 13):**
1. `RUB_LONG_TAG` back to `"RUB1"` (reverts the T-030 rename).
2. Reactivate the legacy SHORT branch: load `short_reversion_model.joblib`, post under tag **RUB1** (reverts the PR-#9 removal). Preserve **geometry/threshold parity** with the old RUB1 logic — original behaviour, no new threshold invented. (Git history before PR #9 as reference: `git log --oneline -- 13_ai_rub_bot.py`.)
3. Do NOT route the RUB2 retrain path (`rub2_model_SHORT.pkl`) + RUB3/RUB4 shadow live. RUB2 stays in the register `("RUB2","*"): SHADOW`.
4. `shadow_gate`: enter RUB1 LONG+SHORT = LIVE **explicitly** (currently only default-live) — defense in depth.

**Caution (attribution):** live trades will then appear as tag **RUB1** — a deliberate break with the RUB2 history. Check the cooldown dedup transition (`RUB_LEGACY_TAG`) so that no double post occurs across the tag change (has_open guard, rule 4).

**Verification:** `backtest/test_*rub*.py` (if present, otherwise new: load legacy models, tag == RUB1, both directions emit, exactly one Cornix message). md5 assert that the two legacy models stay unchanged.

### #2 — RUB3-SHORT to SHADOW
`core/shadow_gate.py`: RUB3-LONG is already SHADOW (line 127). RUB3-SHORT sits at `live` (default) — wish is **SHADOW both**. RUB3 in reality only emits LONG shadow (SHORT n=0, inert) → the flip is clean, primarily register hygiene. Add entry `("RUB3","SHORT"): SHADOW`.

### #3 — ATB2-LONG promo · **BLOCKED**
Wish: KEEP long. **Not promotable:**
- No `atb2_model_LONG.pkl` in the repo root (only `staging_models/atb2_model_LONG.pkl`).
- Staging meta: `model_id=ATB2`, **`optimal_threshold=None`**, **`deployable=False`**.
→ A gate flip alone posts nothing (the LIVE loader reads root → None → shadow fallback) resp. would fire ungated (thr=None). **Precondition:** a deployable ATB2-LONG artifact with a threshold moved to root (hard rule 2, Michi decision) OR defer the item. **Default: defer**, document as an open follow-up in the PR body.

### #4 — EPD3-LONG promo · **BLOCKED**
Wish: KEEP long. **Collision hazard:**
- `SHADOW_ARTIFACTS["EPD3"]["LONG"] = "epd2_model_LONG.pkl"` (`core/shadow_gate.py:298`) — **the same file name as the legacy EPD2**. EPD3-LONG in reality emits as shadow (n=440/30d) from `staging_models/epd2_model_LONG.pkl`.
- Moving it to root would make the EPD2 live path (bot 10) load the same file → **double-post bug** (exactly the hazard that the `epd3_` rename for SHORT in PR #185 fixed — see MEMORY `kythera-ws2-golive-promotions`).
→ **Precondition:** a challenger-distinct root file name `epd3_model_LONG.pkl` + loader fix (SHADOW_ARTIFACTS map + LIVE path) analogous to EPD3-SHORT, plus a deployable artifact. **Default: defer**, document follow-up.

### #5 — Activate ATS2 (restart only)
Config matches the wish (`ATS2` live/live in the register + root artifacts `ats2_model_*.pkl` present since 2026-07-23 22:02). But the 21:04 restart ran **before** the artifact move → bot 12 didn't load ATS2 (`_emit_ats2`: `art is None` → silent; no "ATS2 shadow models loaded" in the log). **One fleet restart** activates ATS2. Optionally beforehand `tools/verify_staging_artifacts.py` resp. threshold check of the root artifact.

### #6 / #7 — AIM2-TOPN & ATS1_Robust: RETIRE + delete trades
- Register: gate → `RETIRED` (resp. `SILENT`) for both directions (code PR).
- "Delete trades" = live-DB DELETE against `ai_signals` (and possibly `ml_predictions_master` / `closed_ai_signals`) for tag `AIM2-TOPN` resp. `ATS1_Robust`.
  - **Hard rule 1 + irreversible.** Does NOT run in the code PR. Separate step, **individually approved by Michi**: show an exact SELECT preview (count per table) → approval → DELETE in a transaction, DB backup hint (`tools/backup_db.ps1`).
  - Rationale: `AIM2-TOPN` "too thin", `ATS1_Robust` "synthetic only" → deletion, not just retire.

### #8 — Main channel: SHADOW both (clarify)
`MAX2` replaced the classic main-channel bot via **T-020** (SRA2-LONG fork → CH_MAIN). Tag "Main channel"/`MAINCHANNEL` is not held in the register (leg_status = default-live). **Clarify:** does any emitter still post under "Main channel"? If no → item is documentary (already satisfied by the MAX2 replacement). If yes → set a SHADOW entry.

---

## 3. Order & PR cut

1. **PR core (class A+B):** RUB1 revive (#1) + RUB3-SHORT park (#2) + retire register entries AIM2-TOPN/ATS1_Robust (#6/#7, only the `RETIRED` part, **without** DB delete) + main-channel clarification (#8). Branch `feat/t-2026-kyt-9050-037`, core reviews (z-code-reviewer + z-spec-compliance-review), merge-train.
2. **Blocker docs:** ATB2-LONG (#3) + EPD3-LONG (#4) as open follow-ups in the PR body + `AUDIT_TODO.md` (**not** implemented in this PR).
3. **Operator steps AFTER merge (approve individually):**
   - DB deletes AIM2-TOPN + ATS1_Robust (#6/#7) — preview → approval → delete.
   - Fleet restart (#5 ATS2 + activation of gate flips + RUB1) via `tools/restart_fleet.ps1`.

---

## 4. Hard limits (do not exceed)

- **No live intervention/restart/DB write without explicit Michi approval** (CLAUDE.md hard rule 1, escalation §6).
- **Gate flips** (RUB1 live, RUB3 park, retires) are escalation-gated — code in the PR is ok, **effect only kicks in with a restart** (Michi).
- **DB deletes:** never without asking, always preview-first, transaction, backup hint.
- **Model artifacts only into `staging_models/`** — promotion to root (ATB2/EPD3-LONG) is a Michi decision, not part of this task (hard rule 2).
- **Feature builders/legacy models** of RUB1 left unchanged (md5 assert). Rule #7.
- **Exactly one Cornix message per signal** (rule 4) — check the has_open guard + tag transition during the RUB1 rollback.

## 5. Verification (DB-free)

- `python -m pytest backtest/test_*.py` (resp. the affected ones).
- `python tools/regression_guard/guard.py verify` and `smoke`.
- New/extended RUB1 test: tag==RUB1, both directions, legacy models loaded, exactly one Cornix message, threshold parity.
- Register assert: `python -c "from core import shadow_gate as sg; print(sg.leg_status('RUB1','LONG'), sg.leg_status('RUB1','SHORT'), sg.leg_status('RUB2','LONG'), sg.leg_status('RUB3','SHORT'))"` → expected `live live shadow shadow`.

## 6. Definition of done

- [ ] PR core merged (RUB1 revive + RUB3 park + retires register + main-channel clarification), both core reviews PASS.
- [ ] ATB2-LONG + EPD3-LONG documented as blockers (PR body + AUDIT_TODO.md).
- [ ] `CHANGELOG.md` entry (German), `AUDIT_TODO.md` maintained, KB task status.
- [ ] Operator steps prepared + Michi approval obtained: DB deletes (preview), fleet restart.
- [ ] After restart: read-only verification that RUB1 live posts both (outbox/ai_signals), ATS2 loads + posts, RUB3-SHORT/retires stay silent.

---

## Appendix A — live evidence (read-only, 2026-07-24)

- **RUB1 models identical to v2:** `long_reversion_model.joblib` md5 `0227bb4a…` (== v2), `short_reversion_model.joblib` md5 `16ca3711…` (== v2). Both in root, but SHORT isn't loaded.
- **RUB2 retrain meta:** `strategy=rub2`, `optimal_threshold=0.7929`, val WR 80.2% / +141%, test WR 83.3% / +565%, `deployable=True`.
- **ATB2-LONG staging:** `deployable=False`, `optimal_threshold=None`.
- **EPD3-LONG:** loads `staging_models/epd2_model_LONG.pkl` (legacy-EPD2 name → root collision).
- **ATS2 artifacts:** `ats2_model_{LONG,SHORT}.pkl` in root since 2026-07-23 22:02 (~1 h after the 21:04 restart) → not loaded at startup.
- **Register actual (leg_status):** RUB1 live/live (default), RUB2 shadow/shadow (line 239-240), RUB3 shadow/live, ATB2 shadow/shadow, EPD3 shadow/shadow, ATS2 live/live.

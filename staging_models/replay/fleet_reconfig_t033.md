# Fleet reconfig after audit T-032 — implementation report (T-2026-KYT-9050-033)

_generated 2026-07-23 · CODE-only (no deploy, no live-DB write, no artifact-root moves) · basis: `staging_models/replay/fleet_realized_audit.md` (T-032) + operator plan (bot_results.xlsx, follow-up questions A/B/C clarified)_

## 0. Core finding (important for Michi)

The plan was conceived as "just a `leg_status` flip in the shadow_gate register". During the mechanism analysis (step 1) it turned out: **only some legs go through `post_ai_signal_gated` (where a register flip is enough).** The majority of the legs to be parked (BR/BB/QM pattern, SRA1, RUB2, EPD2 legacy, ABR2, MIS2) post **legacy-direct** — these bots didn't consult the gate **at all**. A plain register entry there would have been a silent no-op.

Solution (a clean cut instead of a hack): a central, purely additive router `core.signal_post.route_legacy_leg`, which the legacy bots call at their emission point. Default = LIVE ⇒ every unregistered leg behaves **byte-identically** to before; only a register entry parks a (tag, direction) leg. This makes the shadow_gate register the **single source of truth** for the fleet lifecycle — consistent for both gated and legacy bots.

## 1. Mechanism mapping (step-1 result)

| Bot(s) | Tags | Post path | Mechanism of the change |
|---|---|---|---|
| 9 (SR) | SRA2 | `post_ai_signal_gated` (`_emit_sra2_shadow`) | **register flip** (SRA2-SHORT → LIVE) |
| 10 (EPD) | EPD3 | `post_ai_signal_gated` (`_emit_epd3_shadow`) | **register flip** (EPD3-SHORT → SHADOW) |
| 12 (ATS) | ATS2 | was shadow-hardcoded (`_emit_ats2_shadow`) | **rewire → gated** (`_emit_ats2` via `post_ai_signal_gated`) + register flip → LIVE |
| 33 (FIF) | FIF1 | was LIVE-or-nothing (`post_ai_signal`) | **rewire → gated** (`post_ai_signal_gated`) + register FIF1 SILENT→SHADOW |
| 7 (Pattern) | BR* | legacy-direct (`process_ai_trade`) | **`route_legacy_leg`** + register |
| 24 (QM) | QM_1H | legacy-direct (`send_cornix_signal`) | **`route_legacy_leg`** + register |
| 25 (SMC) | BB*/TD* | legacy-direct (`send_cornix_signal`) | **`route_legacy_leg`** + register (BB only) |
| 13 (RUB) | RUB2 | legacy-direct | **`route_legacy_leg`** + register |
| 9 (SR) | SRA1 | legacy-direct (`process_ai_trade`) | **`route_legacy_leg`** + register |
| 10 (EPD) | EPD2 | legacy-direct (legacy block) | **`route_legacy_leg`** + register |
| 11 (MIS) | MIS2-* | legacy-direct | **`route_legacy_leg`** + register |
| 18 (ABR) | ABR2 | legacy-direct (`send_signal`) | **`route_legacy_leg`** + register |
| 26/27/28, 3, bot_catalog | — | — | **no change needed** (see §4) |

## 2. Implemented lifecycle changes (code)

**Promote SHADOW→LIVE** (register: entry removed ⇒ default LIVE):
- **ATS2** LONG+SHORT — additionally bot 12 `_emit_ats2_shadow`→`_emit_ats2` rewired to `post_ai_signal_gated` (pattern like bot 9/10). ⚠ artifact precondition §3.
- **SRA2-SHORT** — bot 9 was already gated ⇒ pure flip. ⚠ artifact + threshold precondition §3.

**Park LONG stays LIVE / SHORT→SHADOW** (`route_legacy_leg` + register):
- BR2H, BR4H (bot 7); BB_1H, BB_4H (bot 25); QM_1H (bot 24). `BR1H` (historical pre-rename tag) + `QM_4H` (bot only runs 1h) as documentary entries.

**Park SHORT stays LIVE / LONG→SHADOW:**
- MIS2-24H, MIS2-72H, MIS2-168H (bot 11); EPD3-SHORT (bot 10, register flip). `EPD1` (historical, no longer emitted) documentary.

**Fully →SHADOW (both legs):**
- EPD2 (bot 10), MIS2-8H (bot 11), RUB2 (bot 13), SRA1 (bot 9), BB2_4H (bot 25), BR1D + BR1Hv2 (bot 7), ABR2 (bot 18). "Main channel" already retired (T-020, detector dispatch removed) → no entry.

**Revive SILENT→SHADOW:** FIF1 LONG+SHORT — bot 33 rewired from `post_ai_signal` (LIVE-or-nothing) to `post_ai_signal_gated`, so SHADOW now produces monitored trades (LIVE remains additionally behind `NEW_IDEAS_LIVE_POSTING`).

**RETIRE (SILENT):** AIM1, ATB1, ATS1 — **already at target state** (AIM1 RETIRED via `_RETIRED_TAGS`, ATS1/ATB1 SILENT since T-127). No code change needed.

**NO change (stays LIVE):** ABR1, AIM2, RUB1, TD_4H, TD_1H, ROM1, MAX1, UFI1, XSM1, SRA2-LONG, SKW1, TD2_4H, 5Percent, FastInOut, VolIndic, SR, TSM1, XSR1 — not registered ⇒ default LIVE, byte-identical.

## 3. DEPLOY preconditions for Michi (HARD RULE 2 — NOT part of this task)

The promotions are done in code, but **inert** until the respective artifact sits in the repo root (the LIVE loader reads root via `shadow_artifact_path`). Before the restart:

1. **ATS2 (both legs):** promote `staging_models/ats2_model_LONG.pkl` + `ats2_model_SHORT.pkl` → **repo root**. Thresholds are real (LONG 0.7825 / SHORT 0.9084) → prob gate takes effect. The per-scan double post (60s cadence × persistent crossover) is prevented by the `has_open` guard **retrofitted** into `_emit_ats2` (review fix, see §8) — analogous to bot 9/10. If the artifact move is skipped: `_emit_ats2` loads `None` → ATS2 stays silent (promotion has no effect).
2. **SRA2-SHORT:** `staging_models/sra2_model_SHORT.json` (+ `_meta.json`, `_calib.pkl`) → **repo root**. ⚠ **CRITICAL:** `optimal_threshold` is **NULL** (meta: `deployable=false`, val `avg_net_pnl −0.079%`). LIVE would post on **every** S/R-SHORT candidate (Cornix flood, no prob gate). **Set a threshold or retrain SRA2-SHORT before go-live** — otherwise the promotion is a flood risk.
3. **EPD3-SHORT (park):** the live artifact sits as `epd3_model_SHORT.pkl` in the **root**; as SHADOW the loader reads `staging_models/epd3_model_SHORT.pkl` — **that file is missing there**. Without it EPD3-SHORT does not load and effectively goes **silent** (instead of shadow-tracked). For a real shadow history: copy the artifact to `staging_models/`; otherwise the park is simply silence (fine for stopping the bleeding live posts).
4. **Fleet restart** (Michi-gated): all changes take effect only after `tools/restart_fleet.ps1` / watchdog restart.

## 4. Deliberately NO change (although in `touches`)

- **`3_detectors.py`:** the main-channel retire is already done (T-020). The 4 classic KEEP bots (5Percent/FastInOut/VolIndic/SR) stay LIVE (operator: informational/not Cornix-executed). → no-op.
- **`core/bot_catalog.py`:** all tag families are already mapped; the reconfig changes lifecycle, not the tag→script mapping. `test_bot_catalog.py` green. → no-op.

## 5. Open scope flags

- **FLAG-B (MIS1 revive not possible as a plain flip):** the plan "revive MIS1-24h/72h/168h LONG + MIS1-8h SHORT (SILENT→LIVE)" is **not achievable code-only.** Bot 11 loads exclusively `mis2_model_*.pkl` (generation MIS2) and posts under `MIS2-*` — there is **no MIS1 load path anymore** ("no legacy fallback — MIS1 is off", line 45/92). The MIS1 artifacts (`pump_model_*_final.pkl` in root) are not loaded; additionally the P0.12 feature self-check would unload old 67-feature leakage models. **A register entry MIS1→LIVE would be fake (no emitter).** → **not implemented, flagged.** Reviving = bot-11 reattachment of the MIS1 generation + feature compatibility check = **its own task** (operator decision: is this worth it against the parallel MIS2 park?).
- **FLAG-C (cosmetic `ml_predictions_master` edge cases, all LOW, no money-path impact — the audit data source is `ai_signals`/`closed_ai_signals`, not `predictions.posted`):**
  - Bot 9 (SRA1 shadow): `route_legacy_leg` additionally writes an `ml_predictions` row (trade_id=0) next to the caller's row (trade_id=t_id). The monitored `ai_signals` trade stays singular via `has_open`. Documented in the bot-9 comment.
  - Bots 24/25 (QM/BB shadow): the PRE-route "shadow log" row sets `posted=True`, even though the parked leg doesn't send anything to Cornix; `update_cooldown` keeps running for the shadow (only throttles the frequency, harmless). `post_shadow`'s `log_prediction` dedups against the row already written (same model/coin/dir/4h) → **no** duplicate row. Deliberately NOT reworked (a correct `posted` would need a `leg_status` call in the scan loop) — the money-path diff stays focused.
- **FLAG-D (shadow master switch now also gates promoted LIVE legs, LOW/by-convention):** `_emit_ats2` (ATS2) and `_emit_sra2_shadow` (SRA2, both legs) check `shadow_gate.shadow_posting_enabled()` as a guard. `KYTHERA_SHADOW_POSTING=0` (the "turn off all shadow trades" switch) thereby also silences the promoted **live** legs ATS2/SRA2. Fail-safe (suppressed, never over-posts) and **already the established convention** — bot 9 already gates the already-live SRA2-LONG (since T-185) via exactly this guard. Default `1` ⇒ normal operation unaffected. **→ Michi: confirm that the shadow kill switch should co-gate these gated-promoted live legs (convention) — or I decouple them.**

## 6. Verification

- `backtest/test_shadow_gate.py` — **23 passed** (register goldens refreshed to the T-033 state + new router tests; old goldens deliberately flipped, because the fleet definition changes — rule 9, justified).
- `backtest/test_signal_post_gated.py` — passed (SILENT example FIF1→ATS1 followed through).
- `backtest/test_bot_catalog.py`, `test_published_targets.py`, `test_signal_orchestrator.py` — passed (162 total in the suite).
- `ruff check` + `ruff format --check` (0.15.17) — clean on all touched files.
- `mypy` (2.1.0) — `core/shadow_gate.py` + `core/signal_post.py` clean (locally via `--python-version 3.12` against the numpy-2.5 stub abort of the py3.14 VPS; CI mypy validates regularly).
- **Pre-existing red (NOT from this task):** `test_fleet_definition.py::test_watchdog_view_is_unchanged` red — the watchdog golden is missing bots 36–39 (LIS1/SKW1/TSM1/XSM1, from T-149/T-183). I have **not** touched `core/fleet.py`/`main_watchdog.py`/the golden (`git diff --name-only` confirms) → independent golden drift, deliberately not refreshed alongside here (out-of-scope for this piece).

## 7. Safety contract (rule 1/2/4)

- Default LIVE ⇒ additive; no behaviour change except for explicitly registered legs.
- SHADOW legs write **only** `ai_signals` (monitored), **never** `telegram_outbox` (no Cornix, rule 4) — via `post_shadow_ai_signal`, whose has_open guard prevents double trades/orphans.
- No fleet restart, no live-DB write, no artifact root moves (all flagged as deploy preconditions for Michi, §3).

## 8. Core reviews (both run, before task pause)

- **z-spec-compliance-review → ISSUES:** 7 of 8 lifecycle groups + all 4 hard constraints satisfied; the only ✗ is MIS1 revive (verified-impossible, correctly escalated instead of faked — FLAG-B). The three flagged deploy hazards (ATS2/SRA2-SHORT artifact moves + SRA2-SHORT threshold + EPD3-SHORT staging copy) are operator preconditions, not a code defect. Register-goldens refresh confirmed legitimate (rule 9).
- **z-code-reviewer → NEEDS WORK (1 CRITICAL, 3 LOW), CRITICAL fixed:**
  - **[CRITICAL, FIXED]** `_emit_ats2` (bot 12) was missing the `has_open` guard before the LIVE post (`post_ai_signal` doesn't dedup) → per-scan double trade after the ATS2 promotion. **Guard + import retrofitted** (mirror bot 9:199/bot 10:201); `test_shadow_gate`/`test_signal_post_gated` still green. Was latent anyway (artifact still in staging → ATS2 silent), but mandatory before the Michi artifact move.
  - **[LOW, FIXED]** bot 10 EPD2 shadow `n_show=3`→`len(targets)` (audit parity with the historical EPD2-live series, which stored the full target list).
  - **[LOW, documented]** bots 24/25 `posted=True` cosmetics + shadow cooldown (FLAG-C); shadow master switch gates live legs (FLAG-D). Both deliberately only documented (cosmetic / by-convention), no code rework.
  - Positively confirmed: no parked leg can emit to Cornix; tag normalization correct (BR1Hv2→BR1HV2, MIS2-24h→MIS2-24H, `is_retired` does NOT catch MIS2-*); commit modes (autocommit vs. explicit) safe per bot; bot-33 FIF1 rewire without duplicate prediction log.

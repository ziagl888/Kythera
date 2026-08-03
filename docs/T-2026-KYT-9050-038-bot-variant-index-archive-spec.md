# T-2026-KYT-9050-038 — Bot variant index + reproducible model/code archive

**Status:** in_progress · **Priority:** mid · **Related:** T-037 (RUB1 revive = the live-swap pattern in miniature)
**Live context:** VPS. **Real money.** This work is infra/read-only + staging — **no** live intervention.

---

## 0. Goal (Michi)

Index and archive every **bot generation** so that at **any time** it can be
1. **flipped live with the existing infra** (as with the RUB1 revive, T-037), **or**
2. run head-to-head in **simulations** (generation A/B).

In short: turn the currently scattered state into a **searchable index + a reproducible archive**, plus the tooling to *stage* an archived variant or *simulate* two variants.

---

## 1. Problem (current state)

Models **and** code logic are scattered without an index:
- **Repo ROOT:** ~60 artifacts, live + legacy mixed — e.g. `long/short_reversion_model.joblib` (=RUB1), `pump_model_*_final.pkl` (=MIS1), `model_tsi_*_robust.pkl` (=ATS1_Robust), `mis2_model_*` (=MIS2), `ats2_*`, `sra2_*`, `bb_xgboost_*`, `qm_xgboost_*` …
- **`staging_models/`:** shadow/staging generations + studies (`atb2_*`, `epd2_model_LONG`, `rub2_model_LONG`, `fmr2_*`, …).
- **`.claude/worktrees/`:** dozens of copies of the same artifacts.
- **`crypto_trading_bot_v2/`:** the February originals (hash-verified identical to some root legacy models).
- **Code logic only in git history.** T-037 showed the pattern: switching an old variant live = **old artifact** + **code revert to a git SHA** (RUB1-SHORT logic sat at `07c8874^`) + **tag** + **register flip** + **restart**.

There is **no index** and **no archive** — only partial building blocks (below).

---

## 2. Existing partial infra — USE, don't rebuild

| Building block | What it provides |
|---|---|
| `core/bot_catalog.py` | Tag family → fleet script (family-prefix, longest-wins). `families_for_script()` = reverse. |
| `core/shadow_gate.py` | `SHADOW_ARTIFACTS` (tag → filename per direction) · `_LIFECYCLE` (live/shadow/silent) · `_RETIRED_TAGS` · `leg_status()`. |
| Artifact `*_meta.json` / meta in the pkl | `model_id`, `optimal_threshold`, `deployable`, `trainer`, `strategy`, `features`, `trained_at`, val/test stats. |
| `docs/MODEL_INTENT.md`, `docs/MODEL_CANDIDATES_SPEC_2026-07.md` | Intent/provenance per model family. |
| `walkforward_sim` + `tools/retrain_from_replay.py` | DB-free replay/sim infra (labels from `*_replay_*.jsonl`). |
| `staging_models/` | the only allowed storage location for non-live artifacts. |

The index is, at its core, a **join view** over these sources + the filesystem + git.

---

## 3. Deliverables

### D1 — Variant index (single source of truth, auto-generated)
A **read-only discovery tool** `tools/bot_variants/index.py` that produces one row per **bot × generation**:

| Field | Source |
|---|---|
| `family` / `tag` / `generation` | bot_catalog family-prefix + tag (e.g. RUB, RUB1/RUB2/RUB3/RUB4) |
| `script` | `bot_catalog.script_for_tag()` |
| `artifacts[]` + `md5` + `location` | filesystem scan (root / staging / archive) |
| `lifecycle` | `shadow_gate.leg_status(tag, dir)` per direction |
| `threshold` / `deployable` / `trainer` / `trained_at` | artifact meta |
| `code_ref` | git SHA/tag under which the generation's logic lived/lives in the bot script (see D4) |
| `provenance` | MODEL_INTENT/task reference |

**Output:** `docs/bot_variants_index.md` (human-readable, generated) **+** `model_archive/index.json` (machine-readable). Idempotently regeneratable; unknown tags are **counted and made visible** (no silent drop, unlike bot_catalog).

### D2 — Archive layout `model_archive/`
```
model_archive/
  <family>/<generation>/           # z.B. rub/RUB1/, epd/EPD2/, mis/MIS1/
    <artifact>.pkl|.joblib|.json   # eingesammelte Alt-Modelle (aus root/staging/v2)
    manifest.json                  # model_id, threshold, features, trained_at,
                                   # code_ref (git-SHA/Tag), lifecycle-Historie,
                                   # provenance, md5, source_origin
  index.json                       # D1-Aggregat
```
- Collects the scattered old models into one place. **Code is NOT fully copied** — `code_ref` (git SHA/tag) in the manifest is enough (the bot code lives in git; the live swap turns it into a checkout/revert).
- **Large artifacts:** check the `.gitignore` strategy (the repo currently commits models in root; decide deliberately whether archive binaries are committed or referenced via manifest+provenance — default: commit small/canonical legacy artifacts, reference large studies).

### D3 — Tooling
- `tools/bot_variants/index.py` — discovery (D1), read-only.
- **stage/activate helper** — prepares an archived variant for the live swap: copies the artifact to `staging_models/` and **prints the `code_ref` step** (which git revert/checkout is needed) + the register flip. **NEVER automatically to repo root/live** (hard rule 2) and **no** restart — that stays with Michi.
- **compare/sim harness** — pits two generations head-to-head via the **existing** `walkforward`/replay infra (DB-free), comparative metrics (avg/sum PnL, WR, MaxDD, n). Does NOT rebuild the sim infra, just calls it.

### D4 — `code_ref` resolution
For each generation, determine the git point at which its bot logic was implemented (pattern T-037: RUB1-SHORT = `07c8874^`). Heuristic: `git log --follow -S<model_id-oder-Dateiname> -- <script>` + the task/PR reference from the commit. Result goes into the manifest. Where the logic is still active today → `code_ref = HEAD`.

---

## 4. Phases (recommended, one commit each)
1. **Index (D1)** — discovery tool + generated `bot_variants_index.md`/`index.json`. Immediately useful, purely read-only.
2. **Archive (D2 + D4)** — set up layout, collect old models, manifests + code_refs.
3. **Tooling (D3)** — stage helper + compare/sim harness.

Every phase is mergeable on its own. If the session is under time/scope pressure: **deliver phase 1 first**, the rest as follow-up tasks.

---

## 5. Hard limits (do not exceed — CLAUDE.md)
- **Artifacts only to `staging_models/`** (or `model_archive/`). Promotion to the repo root (= live) is Michi's decision, **never** automatic from the tooling (hard rule 2).
- **No live intervention:** no fleet restart, no write queries against the live DB, no model overwrite in root.
- **Feature builders / existing models unchanged** (rule 7) — this task only reads/copies, trains nothing new.
- **Secrets:** never pull `.env`/`.local` into the archive (gitleaks blocks it; `--no-verify` forbidden).
- Discovery is **read-only**; no writing outside `model_archive/`, `tools/bot_variants/`, `docs/`.

## 6. Verification (DB-free)
- `python -m pytest backtest/test_*.py` (+ new tests for the index tool: known tags → expected family/script/lifecycle; unknown tag gets counted).
- `python tools/regression_guard/guard.py verify` and `smoke`.
- Index round-trip: run `index.py` twice → identical output (idempotent, deterministically sorted; **no** `Date.now()`/randomness in the output rows).
- md5 assert: collected archive artifacts are byte-identical to the source.

## 7. Definition of done
- [ ] Phase 1 (index) merged: `tools/bot_variants/index.py` + generated `docs/bot_variants_index.md` + `model_archive/index.json`, deterministic/idempotent, tests green.
- [ ] (Phase 2/3 depending on scope) archive layout + manifests + stage/compare tooling — or documented as follow-up tasks.
- [ ] `CHANGELOG.md` (German), `AUDIT_TODO.md` maintained, KB task status.
- [ ] Core reviews PASS (z-code-reviewer + z-spec-compliance-review), merge-train.
- [ ] No boundary violation: nothing promoted to root, no DB writes, no live effects.

---

## Appendix — current landscape (read-only, 2026-07-24)

**Root legacy examples (generation → file):**
- RUB1 → `long_reversion_model.joblib` + `short_reversion_model.joblib` (now live again via T-037 under tag RUB1)
- RUB2 → `rub2_model_SHORT.pkl` (retrain, benched/SHADOW) · RUB2-LONG → `staging_models/rub2_model_LONG.pkl` (not deployable)
- MIS1 → `pump_model_{8,24,72,168}h_{pump,dump}_final.pkl` · MIS2 → `mis2_model_*`
- ATS1_Robust → `model_tsi_{long,short}_robust.pkl` · ATS2 → `ats2_model_{LONG,SHORT}.pkl`
- SRA2 → `sra2_model_{LONG,SHORT}.json`(+calib/meta) · AIM2 → `master_meta_model_aim2.pkl`

**Staging:** `atb2_*`, `epd2_model_LONG` (=EPD3-LONG source, thr=None/deployable=False), `rub2_model_LONG`, `fmr2_*`, various `*_study.json`.

**Known pitfalls:** `SHADOW_ARTIFACTS` maps EPD3-LONG to the legacy filename `epd2_model_LONG.pkl` (root collision hazard — the index must make such shared filenames visible). Artifact `model_id` rotates with each retrain (bot_catalog therefore uses family-prefix). Many root models have `deployable=False`/`thr=None` (legacy stubs) — this field belongs in the index.

# SPEC — tools/bot_variants (T-2026-KYT-9050-038, Phase 1: Index)

Full spec: `docs/T-2026-KYT-9050-038-bot-variant-index-archive-spec.md`. This
SPEC.md pins the binary-testable acceptance criteria of the **index phase** (D1).

## Intent
A read-only discovery tool that joins the scattered current state (root/staging/
archive artifacts + lifecycle + fleet script + git) per **bot × generation** into
a deterministically regenerable index: `docs/bot_variants_index.md`
(human-readable) + `model_archive/index.json` (machine-readable). Basis for the
live swap (T-037 pattern) and sim A/B of each generation.

## Acceptance criteria (binary testable)
- [ ] AK1: For known tags the resolver returns the expected
      family/script/lifecycle — e.g. `RUB1`→family `RUB`, script
      `13_ai_rub_bot.py`, LONG/SHORT `live`; `ATB2`→`ATB`/`14_ai_atb_bot.py`/
      `shadow`. Test: `backtest/test_bot_variant_index.py`.
- [ ] AK2: An unknown tag / a non-classifiable artifact file is **counted and
      listed** (no silent drop), analogous to `bot_catalog`. Test:
      unclassified-count > 0 and contains the fixture outsider.
- [ ] AK3: **Idempotent/deterministic** — `build_index()` called twice
      returns byte-identical JSON+Markdown; no `now()`/randomness in the
      output lines. Test: two runs compared; `--check` finds no drift
      directly after `--write`.
- [ ] AK4: **Shared filenames** (one artifact file under >1 tag, e.g.
      `rub2_model_LONG.pkl` under RUB2 **and** RUB3; `epd2_model_LONG.pkl` under
      EPD2/EPD3) are surfaced as a collision warning. Test:
      shared-filename report contains the expected entry.
- [ ] AK5: **md5 == source** — the md5 of an artifact file listed in the index
      is the real md5 of the file on disk. Test: against `hashlib.md5` of the file.
- [ ] AK6: Tool is **read-only outside** `docs/` + `model_archive/index.json`;
      loads without a live DB, without network. Test: import + build without DB env.

## Out of scope (phase 1)
- D2 archive layout (`model_archive/<family>/<gen>/` + manifests) → phase 2.
- D3 stage/activate helpers + compare/sim harness → phase 3.
- D4 exact git SHA resolution per generation → phase 2 (phase 1 sets `code_ref`
  conservatively: `HEAD` if the generation is live/active, otherwise `null`).

## Why build (instead of reuse)
No existing tool joins these sources. `bot_catalog`/`shadow_gate` provide
partial views (tag→script, lifecycle), but no generation index over the
filesystem. The index is exactly the missing join layer.

## Scope of consent
**Allowed:** `tools/bot_variants/**`, `backtest/test_bot_variant_index.py`,
`docs/bot_variants_index.md`, `model_archive/index.json`, one additive
public helper function in `core/bot_catalog.py` (`family_for_tag`).
**Forbidden:** repo-root artifact promotion/overwrite (hard rule 2),
DB writes, fleet restart, `.env`/`.local`, edits to `core/shadow_gate.py`
(T-037 works there in parallel — read only).
**Ask first:** committing large archive binary artifacts (phase 2 decision).

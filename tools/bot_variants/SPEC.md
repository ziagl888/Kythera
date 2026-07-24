# SPEC — tools/bot_variants (T-2026-KYT-9050-038, Phase 1: Index)

Vollspec: `docs/T-2026-KYT-9050-038-bot-variant-index-archive-spec.md`. Dieses
SPEC.md pinnt die binär-testbaren Akzeptanzkriterien der **Index-Phase** (D1).

## Intent
Ein read-only Discovery-Tool, das den verstreuten Ist-Zustand (Root-/Staging-/
Archiv-Artefakte + Lifecycle + Fleet-Script + git) je **Bot × Generation** in
einen deterministisch regenerierbaren Index joint: `docs/bot_variants_index.md`
(menschenlesbar) + `model_archive/index.json` (maschinenlesbar). Basis für den
Live-Swap (T-037-Muster) und Sim-A/B jeder Generation.

## Akzeptanzkriterien (binär testbar)
- [ ] AK1: Für bekannte Tags liefert der Resolver die erwartete
      family/script/lifecycle — z.B. `RUB1`→family `RUB`, script
      `13_ai_rub_bot.py`, LONG/SHORT `live`; `ATB2`→`ATB`/`14_ai_atb_bot.py`/
      `shadow`. Test: `backtest/test_bot_variant_index.py`.
- [ ] AK2: Ein unbekannter Tag / eine nicht klassifizierbare Artefakt-Datei wird
      **gezählt und gelistet** (kein Silent-Drop), analog `bot_catalog`. Test:
      unclassified-count > 0 und enthält den Fixture-Fremdling.
- [ ] AK3: **Idempotent/deterministisch** — `build_index()` zweimal aufgerufen
      liefert byte-identisches JSON+Markdown; kein `now()`/Zufall in den
      Ausgabezeilen. Test: zwei Läufe vergleichen; `--check` findet keine Drift
      direkt nach `--write`.
- [ ] AK4: **Geteilte Dateinamen** (ein Artefakt-File unter >1 Tag, z.B.
      `rub2_model_LONG.pkl` unter RUB2 **und** RUB3; `epd2_model_LONG.pkl` unter
      EPD2/EPD3) werden als Kollisions-Warnung sichtbar gemacht. Test:
      shared-filename-Report enthält den erwarteten Eintrag.
- [ ] AK5: **md5 == Quelle** — der im Index gelistete md5 einer Artefakt-Datei
      ist das echte md5 der Datei auf Platte. Test: gegen `hashlib.md5` der Datei.
- [ ] AK6: Tool ist **read-only außerhalb** `docs/` + `model_archive/index.json`;
      lädt ohne Live-DB, ohne Netzwerk. Test: Import + build ohne DB-Env.

## Out of Scope (Phase 1)
- D2 Archiv-Layout (`model_archive/<family>/<gen>/` + Manifeste) → Phase 2.
- D3 stage/activate-Helfer + compare/sim-Harness → Phase 3.
- D4 exakte git-SHA-Auflösung je Generation → Phase 2 (Phase 1 setzt `code_ref`
  konservativ: `HEAD` wenn die Generation live/aktiv ist, sonst `null`).

## Why build (statt reuse)
Kein bestehendes Tool joint diese Quellen. `bot_catalog`/`shadow_gate` liefern
Teil-Sichten (Tag→Script, Lifecycle), aber keinen Generations-Index über das
Dateisystem. Der Index ist genau die fehlende Join-Schicht.

## Scope of consent
**Erlaubt:** `tools/bot_variants/**`, `backtest/test_bot_variant_index.py`,
`docs/bot_variants_index.md`, `model_archive/index.json`, eine additive
Public-Helper-Funktion in `core/bot_catalog.py` (`family_for_tag`).
**Verboten:** Repo-Root-Artefakt-Promotion/Überschreibung (Hard Rule 2),
DB-Writes, Fleet-Restart, `.env`/`.local`, Edits an `core/shadow_gate.py`
(T-037 arbeitet dort parallel — nur lesen).
**Frag zurück:** Committen großer Archiv-Binärartefakte (Phase 2-Entscheid).

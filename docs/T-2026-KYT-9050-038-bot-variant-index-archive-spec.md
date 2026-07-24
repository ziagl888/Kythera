# T-2026-KYT-9050-038 — Bot-Varianten-Index + reproduzierbares Modell-/Code-Archiv

**Status:** in_progress · **Prio:** mid · **Verwandt:** T-037 (RUB1-Revive = das Live-Swap-Muster im Kleinen)
**Live-Kontext:** VPS. **Echtes Geld.** Diese Arbeit ist Infra/Read-only + staging — **kein** Live-Eingriff.

---

## 0. Ziel (Michi)

Jede **Bot-Generation** so indexieren und archivieren, dass man sie **jederzeit**
1. mit der **bestehenden Infra live schalten** (wie beim RUB1-Revive, T-037), **oder**
2. in **Simulationen gegeneinander antreten** lassen kann (Generation-A/B).

Kurz: Aus dem verstreuten Ist-Zustand einen **durchsuchbaren Index + ein reproduzierbares Archiv** machen, plus die Werkzeuge, um eine archivierte Variante zu *stagen* oder zwei Varianten zu *simulieren*.

---

## 1. Problem (Ist-Zustand)

Modelle **und** Code-Logik liegen ohne Index verstreut:
- **Repo-ROOT:** ~60 Artefakte, live + legacy gemischt — z.B. `long/short_reversion_model.joblib` (=RUB1), `pump_model_*_final.pkl` (=MIS1), `model_tsi_*_robust.pkl` (=ATS1_Robust), `mis2_model_*` (=MIS2), `ats2_*`, `sra2_*`, `bb_xgboost_*`, `qm_xgboost_*` …
- **`staging_models/`:** Shadow-/Staging-Generation + Studies (`atb2_*`, `epd2_model_LONG`, `rub2_model_LONG`, `fmr2_*`, …).
- **`.claude/worktrees/`:** dutzende Kopien derselben Artefakte.
- **`crypto_trading_bot_v2/`:** die Februar-Originale (hash-verifiziert identisch zu manchen Root-Legacy-Modellen).
- **Code-Logik nur in git-History.** T-037 zeigte das Muster: eine alte Variante live schalten = **altes Artefakt** + **Code-Revert auf einen git-SHA** (RUB1-SHORT-Logik lag bei `07c8874^`) + **Tag** + **Register-Flip** + **Restart**.

Es gibt **keinen Index** und **kein Archiv** — nur Teil-Bausteine (unten).

---

## 2. Bestehende Teil-Infra — NUTZEN, nicht neu bauen

| Baustein | Was er liefert |
|---|---|
| `core/bot_catalog.py` | Tag-Familie → Fleet-Script (family-prefix, longest-wins). `families_for_script()` = Reverse. |
| `core/shadow_gate.py` | `SHADOW_ARTIFACTS` (Tag → Dateiname je Richtung) · `_LIFECYCLE` (live/shadow/silent) · `_RETIRED_TAGS` · `leg_status()`. |
| Artefakt-`*_meta.json` / meta im pkl | `model_id`, `optimal_threshold`, `deployable`, `trainer`, `strategy`, `features`, `trained_at`, val/test-stats. |
| `docs/MODEL_INTENT.md`, `docs/MODEL_CANDIDATES_SPEC_2026-07.md` | Intent/Provenienz je Modellfamilie. |
| `walkforward_sim` + `tools/retrain_from_replay.py` | DB-freie Replay-/Sim-Infra (Labels aus `*_replay_*.jsonl`). |
| `staging_models/` | der einzige erlaubte Ablageort für nicht-live Artefakte. |

Der Index ist im Kern eine **Join-Sicht** über diese Quellen + das Dateisystem + git.

---

## 3. Deliverables

### D1 — Variant-Index (Single Source of Truth, auto-generiert)
Ein **read-only Discovery-Tool** `tools/bot_variants/index.py`, das je **Bot × Generation** eine Zeile erzeugt:

| Feld | Quelle |
|---|---|
| `family` / `tag` / `generation` | bot_catalog family-prefix + Tag (z.B. RUB, RUB1/RUB2/RUB3/RUB4) |
| `script` | `bot_catalog.script_for_tag()` |
| `artifacts[]` + `md5` + `location` | Dateisystem-Scan (root / staging / archive) |
| `lifecycle` | `shadow_gate.leg_status(tag, dir)` je Richtung |
| `threshold` / `deployable` / `trainer` / `trained_at` | Artefakt-meta |
| `code_ref` | git-SHA/Tag, unter dem die Generations-Logik im Bot-Script lebt(e) (s. D4) |
| `provenance` | MODEL_INTENT/Task-Referenz |

**Output:** `docs/bot_variants_index.md` (menschenlesbar, generiert) **+** `model_archive/index.json` (maschinenlesbar). Idempotent regenerierbar; unbekannte Tags werden **gezählt und sichtbar gemacht** (kein Silent-Drop, wie bot_catalog).

### D2 — Archiv-Layout `model_archive/`
```
model_archive/
  <family>/<generation>/           # z.B. rub/RUB1/, epd/EPD2/, mis/MIS1/
    <artifact>.pkl|.joblib|.json   # eingesammelte Alt-Modelle (aus root/staging/v2)
    manifest.json                  # model_id, threshold, features, trained_at,
                                   # code_ref (git-SHA/Tag), lifecycle-Historie,
                                   # provenance, md5, source_origin
  index.json                       # D1-Aggregat
```
- Sammelt die verstreuten Alt-Modelle an einen Ort. **Code wird NICHT voll kopiert** — `code_ref` (git-SHA/Tag) im Manifest genügt (der Bot-Code lebt in git; der Live-Swap macht daraus einen Checkout/Revert).
- **Groß-Artefakte:** `.gitignore`-Strategie prüfen (das Repo committet Modelle heute im Root; entscheide bewusst, ob Archiv-Binaries committed oder via Manifest+Herkunft referenziert werden — Default: kleine/kanonische Legacy-Artefakte committen, große studies referenzieren).

### D3 — Tooling
- `tools/bot_variants/index.py` — Discovery (D1), read-only.
- **stage/activate-Helfer** — legt eine archivierte Variante zum Live-Swap bereit: kopiert das Artefakt nach `staging_models/` und **druckt den `code_ref`-Schritt** (welcher git-Revert/Checkout nötig ist) + den Register-Flip. **NIE automatisch nach Repo-Root/live** (Hard Rule 2) und **kein** Restart — das bleibt Michi.
- **compare/sim-Harness** — zwei Generationen head-to-head über die **bestehende** `walkforward`/replay-Infra (DB-frei), vergleichende Metriken (avg/sum PnL, WR, MaxDD, n). Baut die Sim-Infra NICHT neu, ruft sie auf.

### D4 — `code_ref`-Auflösung
Für jede Generation den git-Punkt bestimmen, an dem ihre Bot-Logik implementiert war (Muster T-037: RUB1-SHORT = `07c8874^`). Heuristik: `git log --follow -S<model_id-oder-Dateiname> -- <script>` + die Task-/PR-Referenz aus dem Commit. Ergebnis ins Manifest. Wo die Logik heute noch aktiv ist → `code_ref = HEAD`.

---

## 4. Phasen (empfohlen, je eigener Commit)
1. **Index (D1)** — Discovery-Tool + generierter `bot_variants_index.md`/`index.json`. Sofort nützlich, rein read-only.
2. **Archiv (D2 + D4)** — Layout anlegen, Alt-Modelle einsammeln, Manifeste + code_refs.
3. **Tooling (D3)** — stage-Helfer + compare/sim-Harness.

Jede Phase ist eigenständig mergebar. Wenn die Session Zeit-/Scope-Druck hat: **Phase 1 zuerst liefern**, Rest als Follow-up-Tasks.

---

## 5. Harte Grenzen (nicht überschreiten — CLAUDE.md)
- **Artefakte nur nach `staging_models/`** (bzw. `model_archive/`). Promotion in den Repo-Root (= live) ist Michis Entscheid, **nie** vom Tooling automatisch (Hard Rule 2).
- **Kein Live-Eingriff:** kein Fleet-Restart, keine Schreib-Queries gegen die Live-DB, keine Modell-Überschreibung im Root.
- **Feature-Builder / bestehende Modelle unverändert** (Regel 7) — dieser Task liest/kopiert nur, trainiert nichts neu.
- **Secrets:** nie `.env`/`.local` ins Archiv ziehen (gitleaks blockt; `--no-verify` verboten).
- Discovery ist **read-only**; kein Schreiben außerhalb `model_archive/`, `tools/bot_variants/`, `docs/`.

## 6. Verifikation (DB-frei)
- `python -m pytest backtest/test_*.py` (+ neue Tests für das Index-Tool: bekannte Tags → erwartete family/script/lifecycle; unbekannter Tag wird gezählt).
- `python tools/regression_guard/guard.py verify` und `smoke`.
- Index-Roundtrip: `index.py` zweimal laufen lassen → identischer Output (idempotent, deterministisch sortiert; **kein** `Date.now()`/Zufall in den Output-Zeilen).
- md5-Assert: eingesammelte Archiv-Artefakte sind byte-identisch zur Quelle.

## 7. Definition of Done
- [ ] Phase 1 (Index) gemergt: `tools/bot_variants/index.py` + generierter `docs/bot_variants_index.md` + `model_archive/index.json`, deterministisch/idempotent, Tests grün.
- [ ] (Phase 2/3 je nach Scope) Archiv-Layout + Manifeste + stage/compare-Tooling — oder als Follow-up-Tasks dokumentiert.
- [ ] `CHANGELOG.md` (Deutsch), `AUDIT_TODO.md` gepflegt, KB-Task-Status.
- [ ] Kern-Reviews PASS (z-code-reviewer + z-spec-compliance-review), merge-train.
- [ ] Keine Grenzverletzung: nichts nach Root promotet, keine DB-Writes, keine Live-Effekte.

---

## Anhang — Ist-Landschaft (read-only, 2026-07-24)

**Root-Legacy-Beispiele (Generation → Datei):**
- RUB1 → `long_reversion_model.joblib` + `short_reversion_model.joblib` (jetzt via T-037 wieder live unter Tag RUB1)
- RUB2 → `rub2_model_SHORT.pkl` (Retrain, gebencht/SHADOW) · RUB2-LONG → `staging_models/rub2_model_LONG.pkl` (nicht deploybar)
- MIS1 → `pump_model_{8,24,72,168}h_{pump,dump}_final.pkl` · MIS2 → `mis2_model_*`
- ATS1_Robust → `model_tsi_{long,short}_robust.pkl` · ATS2 → `ats2_model_{LONG,SHORT}.pkl`
- SRA2 → `sra2_model_{LONG,SHORT}.json`(+calib/meta) · AIM2 → `master_meta_model_aim2.pkl`

**Staging:** `atb2_*`, `epd2_model_LONG` (=EPD3-LONG-Quelle, thr=None/deployable=False), `rub2_model_LONG`, `fmr2_*`, diverse `*_study.json`.

**Bekannte Fallen:** `SHADOW_ARTIFACTS` mappt EPD3-LONG auf den Legacy-Dateinamen `epd2_model_LONG.pkl` (Root-Kollisions-Hazard — der Index muss solche geteilten Dateinamen sichtbar machen). Artefakt-`model_id` rotiert je Retrain (bot_catalog nutzt deshalb family-prefix). Viele Root-Modelle haben `deployable=False`/`thr=None` (Legacy-Stubs) — das Feld gehört in den Index.

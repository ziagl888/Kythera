# Fleet-Reconfig nach Audit T-032 — Umsetzungsreport (T-2026-KYT-9050-033)

_generated 2026-07-23 · CODE-only (kein Deploy, kein Live-DB-Write, keine Artefakt-Root-Moves) · Basis: `staging_models/replay/fleet_realized_audit.md` (T-032) + Operator-Plan (bot_results.xlsx, Rückfragen A/B/C geklärt)_

## 0. Kernbefund (wichtig für Michi)

Der Plan war als „nur `leg_status`-Flip im shadow_gate-Register" gedacht. Bei der Mechanismus-Analyse (Schritt 1) stellte sich heraus: **nur ein Teil der Beine läuft über `post_ai_signal_gated` (wo ein Register-Flip genügt).** Die Mehrheit der zu parkenden Beine (BR/BB/QM-Pattern, SRA1, RUB2, EPD2-Legacy, ABR2, MIS2) postet **legacy-direkt** — diese Bots konsultierten den Gate **gar nicht**. Ein reiner Register-Eintrag wäre dort ein stiller No-op gewesen.

Lösung (sauberer Schnitt statt Hack): ein zentraler, rein-additiver Router `core.signal_post.route_legacy_leg`, den die Legacy-Bots an ihrer Emissions-Stelle aufrufen. Default = LIVE ⇒ jedes nicht-registrierte Bein verhält sich **byte-identisch** wie zuvor; erst ein Register-Eintrag parkt ein (tag, direction)-Bein. Damit ist das shadow_gate-Register die **einzige Quelle der Wahrheit** für den Fleet-Lifecycle — konsistent für gated- und legacy-Bots.

## 1. Mechanismus-Mapping (Schritt-1-Ergebnis)

| Bot(s) | Tags | Post-Pfad | Mechanismus der Änderung |
|---|---|---|---|
| 9 (SR) | SRA2 | `post_ai_signal_gated` (`_emit_sra2_shadow`) | **Register-Flip** (SRA2-SHORT → LIVE) |
| 10 (EPD) | EPD3 | `post_ai_signal_gated` (`_emit_epd3_shadow`) | **Register-Flip** (EPD3-SHORT → SHADOW) |
| 12 (ATS) | ATS2 | war shadow-hardcoded (`_emit_ats2_shadow`) | **Rewire → gated** (`_emit_ats2` via `post_ai_signal_gated`) + Register-Flip → LIVE |
| 33 (FIF) | FIF1 | war LIVE-oder-nichts (`post_ai_signal`) | **Rewire → gated** (`post_ai_signal_gated`) + Register FIF1 SILENT→SHADOW |
| 7 (Pattern) | BR* | legacy-direkt (`process_ai_trade`) | **`route_legacy_leg`** + Register |
| 24 (QM) | QM_1H | legacy-direkt (`send_cornix_signal`) | **`route_legacy_leg`** + Register |
| 25 (SMC) | BB*/TD* | legacy-direkt (`send_cornix_signal`) | **`route_legacy_leg`** + Register (nur BB) |
| 13 (RUB) | RUB2 | legacy-direkt | **`route_legacy_leg`** + Register |
| 9 (SR) | SRA1 | legacy-direkt (`process_ai_trade`) | **`route_legacy_leg`** + Register |
| 10 (EPD) | EPD2 | legacy-direkt (Legacy-Block) | **`route_legacy_leg`** + Register |
| 11 (MIS) | MIS2-* | legacy-direkt | **`route_legacy_leg`** + Register |
| 18 (ABR) | ABR2 | legacy-direkt (`send_signal`) | **`route_legacy_leg`** + Register |
| 26/27/28, 3, bot_catalog | — | — | **keine Änderung nötig** (s. §4) |

## 2. Umgesetzte Lifecycle-Änderungen (Code)

**Promote SHADOW→LIVE** (Register: Eintrag entfernt ⇒ Default LIVE):
- **ATS2** LONG+SHORT — zusätzlich Bot 12 `_emit_ats2_shadow`→`_emit_ats2` auf `post_ai_signal_gated` umverdrahtet (Muster wie Bot 9/10). ⚠ Artefakt-Vorbedingung §3.
- **SRA2-SHORT** — Bot 9 war bereits gated ⇒ reiner Flip. ⚠ Artefakt + Threshold-Vorbedingung §3.

**Park LONG bleibt LIVE / SHORT→SHADOW** (`route_legacy_leg` + Register):
- BR2H, BR4H (Bot 7); BB_1H, BB_4H (Bot 25); QM_1H (Bot 24). `BR1H` (historischer Pre-Rename-Tag) + `QM_4H` (Bot fährt nur 1h) als dokumentarische Einträge.

**Park SHORT bleibt LIVE / LONG→SHADOW:**
- MIS2-24H, MIS2-72H, MIS2-168H (Bot 11); EPD3-SHORT (Bot 10, Register-Flip). `EPD1` (historisch, nicht mehr emittiert) dokumentarisch.

**Ganz →SHADOW (beide Beine):**
- EPD2 (Bot 10), MIS2-8H (Bot 11), RUB2 (Bot 13), SRA1 (Bot 9), BB2_4H (Bot 25), BR1D + BR1Hv2 (Bot 7), ABR2 (Bot 18). „Main Channel" bereits retired (T-020, Detektor-Dispatch entfernt) → kein Eintrag.

**Revive SILENT→SHADOW:** FIF1 LONG+SHORT — Bot 33 von `post_ai_signal` (LIVE-oder-nichts) auf `post_ai_signal_gated` umverdrahtet, sodass SHADOW jetzt monitored Trades erzeugt (LIVE bleibt zusätzlich hinter `NEW_IDEAS_LIVE_POSTING`).

**RETIRE (SILENT):** AIM1, ATB1, ATS1 — **bereits im Zielzustand** (AIM1 RETIRED via `_RETIRED_TAGS`, ATS1/ATB1 SILENT seit T-127). Keine Code-Änderung nötig.

**KEINE Änderung (bleibt LIVE):** ABR1, AIM2, RUB1, TD_4H, TD_1H, ROM1, MAX1, UFI1, XSM1, SRA2-LONG, SKW1, TD2_4H, 5Percent, FastInOut, VolIndic, SR, TSM1, XSR1 — nicht registriert ⇒ Default LIVE, byte-identisch.

## 3. DEPLOY-Vorbedingungen für Michi (HARD RULE 2 — NICHT Teil dieses Tasks)

Die Promotions sind im Code fertig, aber **inert**, bis das jeweilige Artefakt im Repo-Root liegt (der LIVE-Loader liest Root via `shadow_artifact_path`). Vor dem Restart:

1. **ATS2 (beide Beine):** `staging_models/ats2_model_LONG.pkl` + `ats2_model_SHORT.pkl` → **Repo-Root** promoten. Thresholds sind real (LONG 0.7825 / SHORT 0.9084) → Prob-Gate greift. Der Per-Scan-Doppelpost (60s-Kadenz × persistierendes Crossover) wird durch den in `_emit_ats2` **nachgerüsteten `has_open`-Guard** verhindert (Review-Fix, s. §8) — analog Bot 9/10. Fehlt der Artefakt-Move: `_emit_ats2` lädt `None` → ATS2 schweigt (Promotion greift nicht).
2. **SRA2-SHORT:** `staging_models/sra2_model_SHORT.json` (+ `_meta.json`, `_calib.pkl`) → **Repo-Root**. ⚠ **KRITISCH:** `optimal_threshold` ist **NULL** (Meta: `deployable=false`, val `avg_net_pnl −0.079%`). LIVE würde auf **jedem** S/R-SHORT-Kandidaten posten (Cornix-Flood, kein Prob-Gate). **Vor Go-Live einen Threshold setzen bzw. SRA2-SHORT retrainen** — sonst ist die Promotion ein Flood-Risiko.
3. **EPD3-SHORT (Park):** Das Live-Artefakt liegt als `epd3_model_SHORT.pkl` im **Root**; als SHADOW liest der Loader `staging_models/epd3_model_SHORT.pkl` — **die Datei fehlt dort**. Ohne sie lädt EPD3-SHORT nicht und wird effektiv **still** (statt shadow-getrackt). Für echte Shadow-Historie: Artefakt nach `staging_models/` kopieren; sonst ist der Park schlicht Silence (fürs Stoppen der blutenden Live-Posts ok).
4. **Fleet-Restart** (Michi-gegatet): Alle Änderungen greifen erst nach `tools/restart_fleet.ps1` / Watchdog-Restart.

## 4. Bewusst KEINE Änderung (obwohl in `touches`)

- **`3_detectors.py`:** Main-Channel-Retire ist bereits vollzogen (T-020). Die 4 klassischen KEEP-Bots (5Percent/FastInOut/VolIndic/SR) bleiben LIVE (Operator: informativ/nicht-Cornix-executed). → No-op.
- **`core/bot_catalog.py`:** Alle Tag-Familien sind bereits gemappt; die Reconfig ändert Lifecycle, nicht die Tag→Script-Zuordnung. `test_bot_catalog.py` grün. → No-op.

## 5. Offene Scope-Flags

- **FLAG-B (MIS1-Revive unmöglich als reiner Flip):** Der Plan „Revive MIS1-24h/72h/168h LONG + MIS1-8h SHORT (SILENT→LIVE)" ist **code-only nicht umsetzbar.** Bot 11 lädt ausschließlich `mis2_model_*.pkl` (Generation MIS2) und postet unter `MIS2-*` — es gibt **keinen MIS1-Load-Pfad mehr** („kein Legacy-Fallback — MIS1 ist aus", Zeile 45/92). Die MIS1-Artefakte (`pump_model_*_final.pkl` im Root) werden nicht geladen; zudem würde der P0.12-Feature-Selfcheck alte 67-Feature-Leakage-Modelle entladen. **Ein Register-Eintrag MIS1→LIVE wäre ein Fake (kein Emitter).** → **Nicht umgesetzt, geflaggt.** Reviven = Bot-11-Wiederanbindung der MIS1-Generation + Feature-Kompatibilitätsprüfung = **eigener Task** (Operator-Entscheid: lohnt sich das ggü. dem parallelen MIS2-Park?).
- **FLAG-C (kosmetische `ml_predictions_master`-Randfälle, alle LOW, keine Money-Path-Wirkung — Audit-Datenquelle ist `ai_signals`/`closed_ai_signals`, nicht `predictions.posted`):**
  - Bot 9 (SRA1-Shadow): `route_legacy_leg` schreibt zusätzlich eine `ml_predictions`-Zeile (trade_id=0) neben der Caller-Zeile (trade_id=t_id). Der monitored `ai_signals`-Trade bleibt via `has_open` singulär. Im Bot-9-Kommentar dokumentiert.
  - Bots 24/25 (QM/BB-Shadow): die PRE-Route-„Shadow-Log"-Zeile setzt `posted=True`, obwohl das geparkte Bein nichts an Cornix schickt; `update_cooldown` läuft für den Shadow weiter (drosselt nur die Frequenz, harmlos). `post_shadow`s `log_prediction` dedupt gegen die schon geschriebene Zeile (gleiches model/coin/dir/4h) → **keine** doppelte Zeile. Bewusst NICHT umgebaut (das korrekte `posted` bräuchte einen `leg_status`-Call im Scan-Loop) — der money-path-Diff bleibt fokussiert.
- **FLAG-D (Shadow-Master-Switch gated jetzt auch promotete LIVE-Beine, LOW/by-convention):** `_emit_ats2` (ATS2) und `_emit_sra2_shadow` (SRA2, beide Beine) prüfen `shadow_gate.shadow_posting_enabled()` als Guard. `KYTHERA_SHADOW_POSTING=0` (der „alle Shadow-Trades aus"-Schalter) legt damit auch die promoteten **Live**-Beine ATS2/SRA2 still. Fail-safe (unterdrückt, nie über-postet) und **bereits geltende Konvention** — Bot 9 gated das schon-live SRA2-LONG (seit T-185) über exakt diesen Guard. Default `1` ⇒ Normalbetrieb unberührt. **→ Michi: bestätigen, dass der Shadow-Kill-Switch diese gated-promoteten Live-Beine mit-gaten soll (Konvention) — oder ich entkopple sie.**

## 6. Verifikation

- `backtest/test_shadow_gate.py` — **23 passed** (Register-Goldens auf T-033-Stand refresht + Router-Tests neu; alte Goldens kippten *bewusst*, weil die Fleet-Definition sich ändert — Regel 9, begründet).
- `backtest/test_signal_post_gated.py` — passed (SILENT-Beispiel FIF1→ATS1 nachgezogen).
- `backtest/test_bot_catalog.py`, `test_published_targets.py`, `test_signal_orchestrator.py` — passed (162 gesamt in der Suite).
- `ruff check` + `ruff format --check` (0.15.17) — clean auf allen berührten Dateien.
- `mypy` (2.1.0) — `core/shadow_gate.py` + `core/signal_post.py` clean (lokal via `--python-version 3.12` gegen den numpy-2.5-Stub-Abbruch der py3.14-VPS; CI-mypy validiert regulär).
- **Pre-existing rot (NICHT von diesem Task):** `test_fleet_definition.py::test_watchdog_view_is_unchanged` rot — der Watchdog-Golden vermisst die Bots 36–39 (LIS1/SKW1/TSM1/XSM1, aus T-149/T-183). Ich habe `core/fleet.py`/`main_watchdog.py`/den Golden **nicht** angefasst (`git diff --name-only` bestätigt) → unabhängige Golden-Drift, hier bewusst nicht mit-refresht (Scope-Fremdkörper).

## 7. Sicherheitsvertrag (Regel 1/2/4)

- Default LIVE ⇒ additiv; keine Verhaltensänderung außer für explizit registrierte Beine.
- SHADOW-Beine schreiben **nur** `ai_signals` (monitored), **nie** `telegram_outbox` (kein Cornix, Regel 4) — via `post_shadow_ai_signal`, dessen has_open-Guard Doppel-Trades/Orphans verhindert.
- Kein Fleet-Restart, kein Live-DB-Write, keine Artefakt-Root-Moves (alle als Deploy-Vorbedingung an Michi geflaggt, §3).

## 8. Kern-Reviews (beide gelaufen, vor task-pause)

- **z-spec-compliance-review → ISSUES:** 7 von 8 Lifecycle-Gruppen + alle 4 Hard-Constraints erfüllt; das einzige ✗ ist MIS1-Revive (verifiziert-unmöglich, korrekt eskaliert statt gefaked — FLAG-B). Die drei geflaggten Deploy-Hazards (ATS2/SRA2-SHORT-Artefakt-Moves + SRA2-SHORT-Threshold + EPD3-SHORT-Staging-Copy) sind Operator-Vorbedingungen, kein Code-Defekt. Register-Goldens-Refresh als legitim (Regel 9) bestätigt.
- **z-code-reviewer → NEEDS WORK (1 CRITICAL, 3 LOW), CRITICAL behoben:**
  - **[CRITICAL, FIXED]** `_emit_ats2` (Bot 12) fehlte der `has_open`-Guard vor dem LIVE-Post (`post_ai_signal` dedupt nicht) → Per-Scan-Doppel-Trade nach der ATS2-Promotion. **Guard + Import nachgerüstet** (mirror Bot 9:199/Bot 10:201); `test_shadow_gate`/`test_signal_post_gated` weiter grün. War ohnehin latent (Artefakt noch in staging → ATS2 stumm), aber vor dem Michi-Artefakt-Move zwingend.
  - **[LOW, FIXED]** Bot 10 EPD2-Shadow `n_show=3`→`len(targets)` (Audit-Parität mit der historischen EPD2-Live-Serie, die die volle Target-Liste speicherte).
  - **[LOW, dokumentiert]** Bots 24/25 `posted=True`-Kosmetik + Shadow-Cooldown (FLAG-C); Shadow-Master-Switch gated Live-Beine (FLAG-D). Beide bewusst nur dokumentiert (kosmetisch / by-convention), kein Code-Umbau.
  - Positiv bestätigt: kein geparktes Bein kann Cornix emittieren; Tag-Normalisierung korrekt (BR1Hv2→BR1HV2, MIS2-24h→MIS2-24H, `is_retired` fängt MIS2-* NICHT); Commit-Modi (autocommit vs. explizit) je Bot sicher; Bot-33-FIF1-Rewire ohne Doppel-Prediction-Log.

# T-2026-KYT-9050-037 — Fleet-Reconfig aus bot_results.xlsx (Michi-Wunsch)

**Status:** in_progress · **Prio:** high · **Vorgänger:** T-033 (Fleet-Reconfig Audit T-032), T-034
**Quelle:** `C:\Users\Michael\Downloads\bot_results.xlsx`, Spalte **„Todo"** = Michis maßgeblicher Wunsch.
**Live-Kontext:** VPS-Session, Live-DB read-only diagnostiziert 2026-07-24. **Hier läuft echtes Geld.**

---

## 0. Ausgangslage / wie der Diff entstand

Michis Wunsch-Spalte wurde Bein-für-Bein gegen den **aktuellen Live-Registerstatus** (`core.shadow_gate.leg_status(tag, dir)`) gestellt. **~40 Beine matchen bereits** (T-033 hat viel umgesetzt, inkl. Michis Overrides wo er gegen die Empfehlung KEEPt: 5Percent, FastInOut, SR, VolIndic bleiben live). Es bleiben **8 offene Deltas** (unten).

`✅/❌`-Semantik der Tabelle: ✅ = LIVE (Cornix-Post), ❌ = nicht-live (SHADOW/geparkt). Ein-Richtungs-Bots stehen auf der ungenutzten Richtung im Register auf Default-`live`, emittieren sie aber nie (empirisch n=0/60d) — **kein** Handlungsbedarf, rein kosmetisch.

---

## 1. Deltas (Ist → Soll)

| # | Bot | Ist (Register) | Soll (Todo) | Eingriffsklasse | Status |
|---|---|---|---|---|---|
| 1 | **RUB1** | „live" — postet aber real als **RUB2** | KEEP beide (live unter RUB1) | B Code + A Gate | **actionable** |
| 2 | **RUB3-SHORT** | live (inert) | SHADOW beide | A Gate | **actionable** |
| 3 | **ATB2-LONG** | shadow | KEEP Long | A Gate (+ Artefakt) | **BLOCKIERT** |
| 4 | **EPD3-LONG** | shadow | KEEP Long | A Gate (+ Artefakt) | **BLOCKIERT** |
| 5 | **ATS2** | live (config) ✓, Artefakt nicht geladen | ACTIVE | D Restart | **restart-gated** |
| 6 | **AIM2-TOPN** | live | RETIRE + Trades löschen | A Gate + C DB-DELETE | **actionable + operator** |
| 7 | **ATS1_Robust** | live | RETIRE + Trades löschen | A Gate + C DB-DELETE | **actionable + operator** |
| 8 | **Main Channel** | live (Default, Tag nicht im Register) | SHADOW beide | Klärung | **needs-clarify** |

Eingriffsklassen: **A** = Gate-Flip in `core/shadow_gate.py` · **B** = Code-Change Bot 13 · **C** = Live-DB-Delete (irreversibel, Hard Rule 1) · **D** = Fleet-Restart (Deploy).

---

## 2. Delta-Details + Vorgaben

### #1 — RUB1 wieder live (LONG + SHORT) unter Tag RUB1
**Ist:** Bot 13 (`13_ai_rub_bot.py`) postet unter Tag **RUB2**:
- LONG = Legacy-RUB1-Modell `long_reversion_model.joblib` (Root, md5 `0227bb4a…`, **identisch** zum v2-Checkout `crypto_trading_bot_v2/`), getaggt RUB2 (`RUB_LONG_TAG = "RUB2"`, Zeile 50).
- SHORT = RUB2-Retrain `rub2_model_SHORT.pkl` (neu trainiert: `strategy=rub2`, +6 Funding-Features, xgb 3.1.2, `optimal_threshold=0.7929`, deployable).
- Legacy-SHORT-Loader wurde in **PR #9** entfernt („falsifiziert"). Legacy-SHORT `short_reversion_model.joblib` liegt im Root (md5 `16ca3711…`, **identisch** zu v2), wird aber **nicht geladen**.

**Soll (Michi, xlsx):** RUB1 postet beide Richtungen **live** mit den **originalen Legacy-Modellen**, getaggt **RUB1** (LONG 2.48 % / SHORT 0.78 %, historisch beide positiv). RUB2-Retrain wird gebencht (Wunsch RUB2 = SHADOW beide, matcht Register schon).

**Umsetzung (Bot 13):**
1. `RUB_LONG_TAG` zurück auf `"RUB1"` (revertiert T-030-Rename).
2. Legacy-SHORT-Zweig reaktivieren: `short_reversion_model.joblib` laden, unter Tag **RUB1** posten (revertiert PR-#9-Removal). **Geometrie/Threshold-Parität** zur alten RUB1-Logik wahren — Original-Verhalten, kein neues Threshold erfinden. (Git-Historie vor PR #9 als Referenz: `git log --oneline -- 13_ai_rub_bot.py`.)
3. RUB2-Retrain-Pfad (`rub2_model_SHORT.pkl`) + RUB3/RUB4-Shadow NICHT live routen. RUB2 bleibt im Register `("RUB2","*"): SHADOW`.
4. `shadow_gate`: RUB1 LONG+SHORT = LIVE **explizit** eintragen (aktuell nur Default-live) — Defense-in-Depth.

**Achtung (Attribution):** Live-Trades erscheinen dann als Tag **RUB1** — bewusster Bruch zur RUB2-Historie. Cooldown-Dedup-Transition (`RUB_LEGACY_TAG`) prüfen, dass kein Doppel-Post über den Tag-Wechsel entsteht (has_open-Guard, Regel 4).

**Verifikation:** `backtest/test_*rub*.py` (falls vorhanden, sonst neu: Legacy-Modelle laden, Tag == RUB1, beide Richtungen emittieren, Cornix genau EINE Message). md5-Assert dass die zwei Legacy-Modelle unverändert bleiben.

### #2 — RUB3-SHORT auf SHADOW
`core/shadow_gate.py`: RUB3-LONG ist bereits SHADOW (Zeile 127). RUB3-SHORT steht auf `live` (Default) — Wunsch **SHADOW beide**. RUB3 emittiert real nur LONG-Shadow (SHORT n=0, inert) → Flip ist sauber, primär Register-Hygiene. Eintrag `("RUB3","SHORT"): SHADOW` ergänzen.

### #3 — ATB2-LONG Promo · **BLOCKIERT**
Wunsch: KEEP Long. **Nicht promotebar:**
- Kein `atb2_model_LONG.pkl` im Repo-Root (nur `staging_models/atb2_model_LONG.pkl`).
- Staging-Meta: `model_id=ATB2`, **`optimal_threshold=None`**, **`deployable=False`**.
→ Gate-Flip allein postet nichts (LIVE-Loader liest Root → None → Shadow-Fallback) bzw. würde ungegatet feuern (thr=None). **Vorbedingung:** deploybares ATB2-LONG-Artefakt mit Threshold nach Root (Hard Rule 2, Michi-Entscheid) ODER Item vertagen. **Default: vertagen**, im PR-Body als offener Follow-up dokumentieren.

### #4 — EPD3-LONG Promo · **BLOCKIERT**
Wunsch: KEEP Long. **Kollisions-Hazard:**
- `SHADOW_ARTIFACTS["EPD3"]["LONG"] = "epd2_model_LONG.pkl"` (`core/shadow_gate.py:298`) — **derselbe Dateiname wie das Legacy-EPD2**. EPD3-LONG emittiert real als Shadow (n=440/30d) aus `staging_models/epd2_model_LONG.pkl`.
- Ein Move nach Root würde den EPD2-Live-Pfad (Bot 10) dieselbe Datei laden lassen → **Doppel-Post-Bug** (exakt der Hazard, den der `epd3_`-Rename für SHORT in PR #185 fixte — siehe MEMORY `kythera-ws2-golive-promotions`).
→ **Vorbedingung:** challenger-distinkter Root-Dateiname `epd3_model_LONG.pkl` + Loader-Fix (SHADOW_ARTIFACTS-Map + LIVE-Pfad) analog EPD3-SHORT, plus deploybares Artefakt. **Default: vertagen**, Follow-up dokumentieren.

### #5 — ATS2 aktivieren (nur Restart)
Config matcht Wunsch (`ATS2` live/live im Register + Root-Artefakte `ats2_model_*.pkl` seit 2026-07-23 22:02 vorhanden). Aber der 21:04-Restart lief **vor** dem Artefakt-Move → Bot 12 lud ATS2 nicht (`_emit_ats2`: `art is None` → silent; kein „ATS2 Shadow-Modelle geladen" im Log). **Ein Fleet-Restart** aktiviert ATS2. Optional davor `tools/verify_staging_artifacts.py` bzw. Threshold-Check des Root-Artefakts.

### #6 / #7 — AIM2-TOPN & ATS1_Robust: RETIRE + Trades löschen
- Register: Gate → `RETIRED` (bzw. `SILENT`) für beide Richtungen (Code-PR).
- **„Trades löschen" = Live-DB-DELETE** gegen `ai_signals` (und ggf. `ml_predictions_master` / `closed_ai_signals`) für Tag `AIM2-TOPN` bzw. `ATS1_Robust`.
  - **Hard Rule 1 + irreversibel.** Läuft NICHT im Code-PR. Separater, **einzeln von Michi freigegebener** Schritt: exaktes SELECT-Preview (count je Tabelle) zeigen → Freigabe → DELETE in Transaktion, DB-Backup-Hinweis (`tools/backup_db.ps1`).
  - Begründung: `AIM2-TOPN` „zu dünn", `ATS1_Robust` „nur synthetisch" → Löschung, nicht nur Retire.

### #8 — Main Channel: SHADOW beide (klären)
`MAX2` hat den klassischen Main-Channel-Bot per **T-020** ersetzt (SRA2-LONG-Fork → CH_MAIN). Tag „Main Channel"/`MAINCHANNEL` ist im Register nicht geführt (leg_status = Default-live). **Klären:** postet noch irgendein Emitter unter „Main Channel"? Falls nein → Item ist dokumentarisch (bereits durch MAX2-Ersatz erfüllt). Falls ja → SHADOW-Eintrag setzen.

---

## 3. Reihenfolge & PR-Schnitt

1. **PR-Kern (Klasse A+B):** RUB1-Revive (#1) + RUB3-SHORT-Park (#2) + Retire-Register-Einträge AIM2-TOPN/ATS1_Robust (#6/#7, nur der `RETIRED`-Teil, **ohne** DB-Delete) + Main-Channel-Klärung (#8). Branch `feat/t-2026-kyt-9050-037`, Kern-Reviews (z-code-reviewer + z-spec-compliance-review), merge-train.
2. **Blocker-Doku:** ATB2-LONG (#3) + EPD3-LONG (#4) als offene Follow-ups im PR-Body + `AUDIT_TODO.md` (**nicht** in diesem PR umsetzen).
3. **Operator-Schritte NACH Merge (einzeln freigeben):**
   - DB-Deletes AIM2-TOPN + ATS1_Robust (#6/#7) — Preview → Freigabe → Delete.
   - Fleet-Restart (#5 ATS2 + Aktivierung Gate-Flips + RUB1) via `tools/restart_fleet.ps1`.

---

## 4. Harte Grenzen (nicht überschreiten)

- **Kein Live-Eingriff/Restart/DB-Write ohne explizite Michi-Freigabe** (CLAUDE.md Hard Rule 1, Eskalation §6).
- **Gate-Flips** (RUB1 live, RUB3 park, Retires) sind escalation-gated — Code im PR ok, **Wirksamwerden erst mit Restart** (Michi).
- **DB-Deletes:** nie ungefragt, immer Preview-first, Transaktion, Backup-Hinweis.
- **Modell-Artefakte nur nach `staging_models/`** — Promotion in den Root (ATB2/EPD3-LONG) ist Michi-Entscheid, nicht Teil dieses Tasks (Hard Rule 2).
- **Feature-Builder/Legacy-Modelle** von RUB1 unverändert lassen (md5-Assert). Regel #7.
- **Genau EINE Cornix-Message pro Signal** (Regel 4) — beim RUB1-Rückbau has_open-Guard + Tag-Transition prüfen.

## 5. Verifikation (DB-frei)

- `python -m pytest backtest/test_*.py` (bzw. die betroffenen).
- `python tools/regression_guard/guard.py verify` und `smoke`.
- Neuer/erweiterter RUB1-Test: Tag==RUB1, beide Richtungen, Legacy-Modelle geladen, EINE Cornix-Message, Threshold-Parität.
- Register-Assert: `python -c "from core import shadow_gate as sg; print(sg.leg_status('RUB1','LONG'), sg.leg_status('RUB1','SHORT'), sg.leg_status('RUB2','LONG'), sg.leg_status('RUB3','SHORT'))"` → erwartet `live live shadow shadow`.

## 6. Definition of Done

- [ ] PR-Kern gemergt (RUB1-Revive + RUB3-Park + Retires-Register + Main-Channel-Klärung), beide Kern-Reviews PASS.
- [ ] ATB2-LONG + EPD3-LONG als Blocker dokumentiert (PR-Body + AUDIT_TODO.md).
- [ ] `CHANGELOG.md`-Eintrag (Deutsch), `AUDIT_TODO.md` gepflegt, KB-Task-Status.
- [ ] Operator-Schritte vorbereitet + Michi-Freigabe eingeholt: DB-Deletes (Preview), Fleet-Restart.
- [ ] Nach Restart: read-only-Verifikation dass RUB1 live beide postet (Outbox/ai_signals), ATS2 lädt + postet, RUB3-SHORT/Retires still.

---

## Anhang A — Live-Evidenz (read-only, 2026-07-24)

- **RUB1-Modelle identisch zu v2:** `long_reversion_model.joblib` md5 `0227bb4a…` (== v2), `short_reversion_model.joblib` md5 `16ca3711…` (== v2). Beide im Root, aber SHORT wird nicht geladen.
- **RUB2-Retrain-Meta:** `strategy=rub2`, `optimal_threshold=0.7929`, val WR 80.2 % / +141 %, test WR 83.3 % / +565 %, `deployable=True`.
- **ATB2-LONG staging:** `deployable=False`, `optimal_threshold=None`.
- **EPD3-LONG:** lädt `staging_models/epd2_model_LONG.pkl` (Legacy-EPD2-Name → Root-Kollision).
- **ATS2-Artefakte:** `ats2_model_{LONG,SHORT}.pkl` im Root seit 2026-07-23 22:02 (~1 h nach dem 21:04-Restart) → beim Start nicht geladen.
- **Register-Ist (leg_status):** RUB1 live/live (Default), RUB2 shadow/shadow (Zeile 239-240), RUB3 shadow/live, ATB2 shadow/shadow, EPD3 shadow/shadow, ATS2 live/live.

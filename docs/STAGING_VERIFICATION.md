# Post-Retrain Staging-Verifikation

**Task:** T-2026-CU-9050-120 · **Tool:** `tools/verify_staging_artifacts.py` · **Stand:** 2026-07-14

Diese Checkliste begleitet die Verifikation eines Retrain-Artefakts, **bevor** es
promotet wird. Die Promotion eines Artefakts in den Repo-Root (= live) ist eine
**explizite Operator-Entscheidung von Michi** (harte Regel 2, Eskalation) — das
Tool liefert nur den Befund, es promotet nichts.

## Pipeline

```
walkforward_sim.py --strategy X   →  Replay-JSONL (REPLAY_DIR)
retrain_from_replay.py --strategy X →  Artefakt + Meta + retrain_<name>_stats.json   → STAGING_DIR
verify_staging_artifacts.py         →  Befund (mechanisch + C2-Metriken)
(Michi)                             →  Promotion in Repo-Root + Fleet-Restart
```

- **STAGING_DIR:** `C:\Users\Michael\Documents\_X\staging_models` (env `KYTHERA_STAGING_DIR`).
- **Serving-Env:** Fleet-Python `Python313_12` (3.13.12 / xgboost 3.1.2). Retrains
  MÜSSEN in genau diesem Interpreter laufen — sonst greift Check 5 (xgb-Skew).

## Ausführen

```bash
python tools/verify_staging_artifacts.py             # alle Familien
python tools/verify_staging_artifacts.py --only td,bb
```

Read-only. Kein DB-Zugriff, kein Live-Touch. Exit-Code **1**, sobald ein
**mechanischer** Contract-Check FAIL ist; Metrik-WARNs sind advisory (Exit 0).

## Mechanische Checks (das Tool entscheidet)

| # | Check | Regel | FAIL bedeutet |
|---|---|---|---|
| 1 | Artefakt in STAGING_DIR, nicht Repo-Root | HR-2 | falscher Ablageort |
| 2 | lädt über `core.model_artifacts` + Feature-Liste == Trainer/Serving-Referenz | HR-7 / P0.12 | Feature-Drift → Bot läuft idle oder auf Müll-Input |
| 3 | `meta.model_id` == erwarteter Gen-Tag (TD2/BB2/ABR2/MIS2/RUB2/EPD2/ATB2) | HR-6 | Bot postet unter Fallback-Tag / Alt-Tag |
| 4 | `optimal_threshold` ∈ (0,1) | Contract | `None`/`1.0` = kein Gate (not-deployable-Seite) |
| 5 | `meta.xgboost_version` == Serving-xgboost | P3.4 | Major-Drift → stiller `predict_proba`-Skew |
| 6 | Format B: `model_type` startswith `binary` + `_calib.pkl` | Contract | Loader läse die falsche Wahrscheinlichkeitsspalte |
| 7 | Modell hat `predict_proba` | Contract | kein Klassifikator geladen |
| 8 | Promotions-Slot ist nicht doppelt belegt (`tools/promotion_guard.py`) | HR-4 / HR-6 | ein LIVE-Challenger liest den Root-Slot einer fremden Generation → Doppel-Post |

### Check 8 — Challenger-Promotions-Namensguard (T-2026-KYT-9050-057)

`shadow_gate.shadow_artifact_path` liefert für ein **LIVE**-Bein den nackten
Root-Dateinamen. Trägt ein Challenger-Tag (RUB3, EPD3, …) in `SHADOW_ARTIFACTS`
den Dateinamen der Retrain-**Generation** statt seinen eigenen, belegt die
Promotion einen Slot, den zugleich der **Legacy-Loader** liest — beide Tags
posten dasselbe Modell (Regel-4-Doppel-Trade). Bei **EPD3-SHORT** war das am
2026-07-21 real (`epd2_model_SHORT.pkl` = Bot-10-`EPD2_ARTIFACT_PATHS["SHORT"]`)
und wurde von Hand durch den challenger-distinkten Namen `epd3_model_SHORT.pkl`
abgewendet; bei **EPD3-LONG** (T-037) dieselbe Handarbeit ein zweites Mal.

Der Guard prüft das jetzt automatisch — registry-basiert, ohne Dateisystem:

| Fall | Schwere |
|---|---|
| Bein ist **LIVE** und der Dateiname gehört einem fremden Tag | **FAIL** (Exit 1, Promotions-Stopp) |
| Bein ist SHADOW/SILENT/RETIRED, Dateiname nicht challenger-distinkt | WARN (latenter Blocker vor dem nächsten Flip) |
| eine Staging-**Datei** wird von >1 Tag beansprucht | WARN je Datei (die Absicht steht nicht im Dateinamen) |

```bash
python tools/promotion_guard.py            # Exit 1 nur bei FAIL
python tools/promotion_guard.py --strict   # WARNs ebenfalls als Fehler
```

Er läuft zusätzlich als pre-commit-Hook (`kythera-promotion-name-guard`) und
blockt damit den Commit, der ein Bein auf LIVE flippt, ohne umzubenennen.
**Offen (heute WARN):** `RUB3-LONG` zeigt weiter auf `rub2_model_LONG.pkl` —
harmlos, solange RUB3 geparkt ist (T-037); vor einer RUB3-Promotion muss das
Artefakt `rub3_model_LONG.pkl` heissen.

## C2-Metrik-Report (advisory — Michi entscheidet)

Aus `retrain_<name>_stats.json` je Modell/Richtung: **Test-WR vs Base-Rate**,
**ΣNet-PnL**, **n**. Eine WARN-Zeile (unter Base-Rate, PnL ≤ 0, n < 30) blockt
NICHT — sie ist der Input für den Promotion-Entscheid. Für die volle
Kalibrierungs-Beurteilung zusätzlich die `calibration_new_test`-Buckets im
Stats-JSON ansehen (steigt `tp1_rate`/`avg_net_pnl_pct` monoton mit der
Confidence?).

## Operator-Gates (das Tool entscheidet NICHT)

- [ ] **Promotion = bewusster Michi-Entscheid.** Kopie STAGING_DIR → Repo-Root ist
      der einzige Live-Touch. Nie Teil eines Trainings-/Verifikations-Laufs.
- [ ] **Kein Tag-Reuse (HR-6).** Der neue Tag (z.B. `MIS3`, `TD2_4H`) darf nicht
      der eines noch aktiven Alt-Modells sein — sonst mischen sich Alt/Neu in den
      Trackern. Der transitionale Dedup der Bots deckt den Generationswechsel.
- [ ] **Kalibrierung schlägt den Status quo.** C2-Report + Buckets: das neue
      Modell muss seine Base-Rate schlagen UND die alte Generation im Alt/Neu-
      Vergleich (`calibration_old_same_events`) — sonst kein Rollout.
- [ ] **Rollout-Reihenfolge.** Nach Live-Relevanz, ein Bot pro Schritt; geparkte
      Bots (ATB1/BB_1H) bleiben geparkt bis zum expliziten Entparken.
- [ ] **Nach der Promotion:** Fleet-Restart, damit der Bot das neue Artefakt lädt
      (24h-Reload greift sonst erst verzögert); der Threshold kommt aus der Meta,
      nicht aus einer Hardcode-Konstante.
- [ ] **Sequential-Jobs-Regel** beim Training beachten (nur so viele parallele
      Sims, wie die CPU trägt — sonst Thread-Oversubscription).

## Worked example (Staging-Stand 2026-07-14)

Der erste Lauf über den damaligen Stand fand genau die Blocker, die das Tool
sichtbar machen soll:

- `td_xgboost_model_4h.pkl` — lädt sauber, aber **Test-WR 59,2 % < Base 60,7 %**
  → Metrik-WARN, No-Go bis besser.
- `bt2_model_{LONG,SHORT}.json` (ABR2-Staging) — **`model_id` fehlt** (HR-6-FAIL)
  **und** auf **xgboost 3.2.0** trainiert (Serving 3.1.2) → nicht promotebar, mit
  aktuellem Trainer neu erzeugen.
- `rub2_model_LONG.pkl` / `epd2_model_LONG.pkl` — `threshold=None` → nicht ladbar
  (bekannte not-deployable LONG-Seiten, korrekt als FAIL markiert).
- `bb_1h/4h`, `rub2_SHORT` — mechanisch sauber, Metriken positiv.

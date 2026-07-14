# Shadow-Mode-Posting — fleet-weit (T-2026-CU-9050-125)

**Ziel:** Jedes `(model_tag, direction)`-Bein, das NICHT live postet, soll statt
Stille einen **überwachten Shadow-Trade** erzeugen — ein Trade mit echtem
realisiertem Ergebnis in `closed_ai_signals`, aber **ohne** Post in einen
Live-Kanal (Cornix/Telegram). So bauen unterdrückte Beine und noch-nicht-
promotete Retrains eine Ergebnis-Historie auf, an der man sie später ehrlich
misst — inklusive der **regime-konditionierten** Freischaltung (Whitelist-v2-Flip
T-2026-CU-9050-069): ein LONG-Bein, das global negativ, aber in `TRANSITION`
positiv ist, lässt sich nur mit Shadow-Trades belegen.

Motiviert von Michi (2026-07-14): die Flotte ist nach dem Recompute effektiv
short-only für die Richtungsmodelle; jedes unterdrückte Bein postet heute nichts
und hat damit **keinen Trade-Record**, auf dem eine spätere Entscheidung fußen
könnte.

---

## 1. Warum es sicher ist — "monitored but unposted"

Ein Shadow-Trade ist eine **`ai_signals`-Zeile OHNE `telegram_outbox`-Zeile**.

Verifiziert (T-2026-CU-9050-125):
1. Der AI-Monitor `8_ai_trade_monitor.py` liest `ai_signals` **ungefiltert**
   (kein Kanal-/Post-Gate), verfolgt Entry-Fill/TP/SL gegen den Live-Preis und
   schreibt beim Close eine `closed_ai_signals`-Zeile. Er enthält **keinerlei**
   Posting-Code (kein `send_telegram`, kein `telegram_outbox`-Insert).
2. Ein Kanal-Post passiert **ausschließlich** über eine `telegram_outbox`-Zeile
   (gedrained von `4_telegram_bot.py`).

⇒ `ai_signals` schreiben, `telegram_outbox` weglassen = getrackt-aber-nie-gepostet.
Der Monitor liefert das realisierte Ergebnis; kein Zeichen erreicht je einen Kanal.

**Sicherheitsvertrag (harte Regeln 1/2/4):**
- **DEFAULT = LIVE.** `core/shadow_gate.py` listet nur explizit als SHADOW/RETIRED
  markierte Beine; alles andere ist live. Der Gate darf **nie** einen bestehenden
  Live-Post in einen Shadow-Post verwandeln — die Verdrahtung ist rein **additiv**
  am Nicht-Post-Zweig jedes Bots.
- Der Code landet, ohne dass etwas live geht: Shadow postet nie in einen Kanal,
  Artefakte bleiben in `staging_models/` (harte Regel 2), Promotion + Fleet-
  Restart bleiben Michis Entscheid.
- Master-Kill-Switch `KYTHERA_SHADOW_POSTING=0` schaltet **alle** Shadow-Emission
  ab (Bots fallen auf das heutige prediction-only-Verhalten zurück).

---

## 2. Zwei Shadow-Klassen

| Klasse | Was | Modell-Quelle | Beispiel |
|---|---|---|---|
| **(A) Neue Generation** | Ein Retrain läuft PARALLEL zum weiter-live alten Tag | `staging_models/<tag>_model_<DIR>.pkl` (Contract-Artefakt) | ATS2 neben live ATS1, ATB2 neben live ATB1 |
| **(B) Unterdrücktes Richtungs-Bein** | Ein sonst-live Modell, dessen eine Richtung (noch) nicht live geht | bereits im Bot geladen | z. B. ein hart geparktes Bein |

Beide teilen dasselbe Emit-Primitiv (`post_shadow_ai_signal`) und dieselbe
`(tag, direction)`-Lifecycle-Klassifikation. Der Unterschied ist nur, ob der Bot
zusätzlich ein Staging-Artefakt laden muss (A) oder das Modell schon hat (B).

> **Wichtig zur Ist-Lage (docs/MODEL_INTENT.md):** Die Flotte ist NICHT pauschal
> „nur SHORT live". Viele LONG-Beine sind bewusst live (RUB-LONG auf Legacy @0,75,
> ABR2-LONG über das Funding-Gate, klassische SR/Main/VolIndic/FastInOut-LONG).
> Die echten Shadow-Kandidaten sind primär Klasse (A) — die nicht-promoteten
> Retrains (ATS2, ATB2, EPD2, SRA2, RUB2-LONG-Retrain, TD2/BB2, QM2 …) — plus die
> wenigen hart geparkten Beine. Deshalb ist Default-LIVE + eine **pro Bein
> begründete** Registry Pflicht, kein pauschaler „alle-LONG-in-Shadow"-Schalter.

---

## 3. Mechanik

### `core/shadow_gate.py`
- `leg_status(tag, direction) -> live|shadow|retired` — Default **live**.
  `_LIFECYCLE` listet nur Nicht-live-Beine (mit Begründung je Zeile),
  `_RETIRED_TAGS` die abgelösten Generationen (AIM1, MIS1, …; reine
  Report-Klassifikation, kein Posting-Effekt).
- `SHADOW_ARTIFACTS` — Klasse-(A)-Tags → Artefakt-Dateinamen je Richtung.
  `load_shadow_artifact(tag, dir)` lädt aus `staging_models/` (fail-soft: fehlt
  das Artefakt, läuft der Bot ohne Shadow-Bein weiter — Artefakt-Präsenz ist
  Michis Promotions-Entscheid).
- `score_artifact(artifact, feature_row)` — **rohe** `predict_proba[:,1]` (der
  Isotonic-Kalibrator ist Reporting; `pick_threshold_safe` wählt den Threshold
  auf der rohen Proba — identisch zu Bot 13/25). `artifact_threshold(artifact)`
  liest `optimal_threshold` (None ⇒ kein Operating-Point, s. u.).

### `core/signal_post.py :: post_shadow_ai_signal(...)`
Schreibt **nur** die `ai_signals`-Zeile (kein `telegram_outbox`) plus die
`ml_predictions_master`-Shadow-Zeile (`posted=False`, deduped via `log_prediction`).
Dedupt gegen offene Trades (`has_open_ai_signal`), committet **nicht** (Regel 8:
der Caller schließt die Transaktion). Trackt genau `targets[:n_show]`
(P2.31-Parität — der Monitor scored die veröffentlichten TPs).

### Emit-Regel je Bein
```
prob = score_artifact(shadow_model, features)
thr  = artifact_threshold(shadow_model)
if thr is None:                 # kein Operating-Point (z. B. ATB2, zu dünn)
    emit shadow trade           # Detektor IST das Gate → jedes Setup sammeln
elif prob >= thr:               # getreue Vorschau des Live-Verhaltens
    emit shadow trade
else:                           # unter Threshold: nur Prediction-Log wie heute
    log_prediction(posted=False)
```

---

## 4. Tag- & Lifecycle-Konvention → Report + Tracker

- Shadow-Trades tragen die **`model_id`-Meta** des Artefakts (Regel 6): neue
  Generationen haben ohnehin einen neuen Tag (ATS2 vs. ATS1) → keine Kollision
  mit `has_open_ai_signal` oder in `closed_ai_signals`.
- Die Lifecycle-Klassifikation ist **pro `(tag, direction)`** — so trennt allein
  die Richtung ein live SHORT-Bein von einem geshadowten LONG-Bein desselben
  Modells, ohne Schema-Änderung an Live-Tabellen.
- Der Sentiment-Report (Teil 2, `23_market_tracker.py`) liest `leg_status(...)`
  und gliedert in **aktiv / shadow / retired**. `tools/track_shadow_model.py`
  liest die realisierten Shadow-Rows weiterhin per Tag-Präfix aus
  `closed_ai_signals`.

---

## 5. Referenz-Verdrahtung (in diesem PR)

### Bot 12 — ATS2 (Klasse A)
Der Live-Pfad baut bereits den geteilten `build_ats_features`-Vektor (ATS2-
Parität). ATS2 scored **denselben** `X_live` auf demselben TSI-Crossover-Event →
getreue Vorschau. `_emit_ats2_shadow()` läuft VOR der ATS1-Band-Logik
(unabhängig von der ATS1-Entscheidung), baut bei `prob >= 0,7825` die identische
HVN/S-R-Geometrie und schreibt einen Shadow-Trade unter Tag `ATS2`.

### Bot 14 — ATB2 (Klasse A)
ATB2 hat einen **eigenen** Detektor (`core/atb2_features.py`, bestätigte Pivots +
geschlossener Ausbruch, EINE Quelle mit `walkforward run_atb2`). `_emit_atb2_shadow()`
macht einen R1-cleanen `read_candles(include_forming=False)`-Read (≥1500 Kerzen,
EMA200-SMA-Seed-Parität), `find_channel_breakout()` auf der letzten geschlossenen
Kerze, `measured_move_targets()` und schreibt bei jedem Setup (ATB2
`optimal_threshold` ist **null** — zu dünne Daten, muss erst Shadow sammeln) einen
Shadow-Trade unter Tag `ATB2`. Läuft unabhängig von der ATB1-Trendlinien-Logik.

Beide kapseln jeden Fehler — der Live-Pfad (ATS1/ATB1) darf nie betroffen sein.

---

## 6. Fleet-weiter Rollout — Pro-Bot-Checkliste

Für jedes weitere Bein gilt dasselbe rein-additive Muster:

1. **Ist-Gating verifizieren:** Postet das Bein heute live? (Bot-Code lesen, NICHT
   raten — Default-LIVE schützt nur, solange die Registry stimmt.) Nur wirklich
   nicht-live Beine eintragen.
2. **Registry pflegen:** `_LIFECYCLE[(TAG, DIR)] = SHADOW` mit Begründung; bei
   Klasse (A) zusätzlich `SHADOW_ARTIFACTS[TAG]` + Artefakt nach `staging_models/`.
3. **Emit verdrahten:** am Nicht-Post-Zweig des Bots `post_shadow_ai_signal(...)`
   nach der Emit-Regel (§3), in eigenem try/except gekapselt.
4. **Feature-Parität:** den GETEILTEN Builder des Modells verwenden (Regel 7) —
   Trainer == Serving. Kein neuer Feature-Pfad.
5. **Test:** DB-freier Unit-Test (Muster `backtest/test_shadow_gate.py`): kein
   `telegram_outbox`, `ai_signals` geschrieben, `posted=False`.

**Kandidaten-Roster (Klasse A/B, nicht promotet — Quelle Roster-Validierung
2026-07-14 + MODEL_INTENT + Staging-Inventar) — ALLE in diesem PR verdrahtet:**

| Bot | Shadow-Tag | Artefakt (staging) | Klasse | Kollision? |
|---|---|---|---|---|
| 12 | **ATS2** | `ats2_model_{L,S}.pkl` | A | nein (live = ATS1) |
| 14 | **ATB2** | `atb2_model_{L,S}.pkl` | A | nein (live = ATB1) |
| 9 | **SRA2** | `sra2_model_{L,S}.json` | A | nein (live = SRA1) |
| 13 | **RUB3** | `rub2_model_LONG.pkl` | B | **ja → Challenger-Tag** (live-LONG postet "RUB2") |
| 10 | **EPD3** | `epd2_model_{L,S}.pkl` | B | **ja → Challenger-Tag** (live postet "EPD2") |

**Challenger-Tag-Konvention (RUB3/EPD3):** Wenn der Retrain ein LIVE-Bein
herausfordert, das bereits unter DEMSELBEN Tag postet, bekommt der Shadow einen
eigenen Generations-Tag (Operator-Entscheid Michi, Regel 6). Grund ist nicht nur
die Attribution: der Active-Trade-Check dieser Bots (`model IN (tag, legacy_tag)`)
würde sonst einen Shadow-Trade eine LIVE-Position blockieren lassen — Verletzung
der rein-additiven Invariante. RUB2-SHORT bleibt live "RUB2"; das Live-EPD bleibt
"EPD2". Der Artefakt-Dateiname trägt weiter die Retrain-Generation (`rub2_*`,
`epd2_*`); nur der geschriebene Tag ist der kollisionsfreie Challenger.

**Nicht-Kandidaten:** TD2_4H / BB2_4H — bereits promotet/live (2026-07-14 Deploy).
QM2 — existiert noch nicht (QM-Rework ist ein künftiger Task).

Kein stiller Cap: der Roster deckt jeden nicht-promoteten Retrain mit ladbarem
Staging-Artefakt ab; Ausnahmen sind oben mit Grund benannt.

**Monitor-Last:** Shadow-Trades erhöhen den Arbeitssatz von `8_ai_trade_monitor`
(mehr offene `ai_signals`-Zeilen). Bei breitem Rollout die offene-Zeilen-Zahl
beobachten; die `has_open_ai_signal`-Dedup je `(symbol, dir, tag)` begrenzt die
Vervielfachung pro Bein.

---

## 7. Ops / Promotion

- **Aktivierung** ist an einen Fleet-Restart gebunden (Michi, harte Regel 1) —
  Shadow-Emission startet erst nach Neustart der betroffenen Bots. Bis dahin
  keine Verhaltensänderung.
- **Promotion eines Shadow-Beins → live:** Artefakt aus `staging_models/` in den
  Repo-Root kopieren (harte Regel 2, Michi), Registry-Eintrag entfernen (Bein wird
  wieder Default-LIVE) bzw. den Live-Serving-Pfad des Bots umstellen, Tag ggf.
  auf die neue Generation heben. Ausschließlich Operator-Entscheid.
- **Abschalten:** `KYTHERA_SHADOW_POSTING=0` (fleet-weit) oder Registry-Eintrag
  entfernen (einzelnes Bein zurück auf Default-LIVE-Verhalten des Bots).

# T-2026-KYT-9050-006 — TD2/BB2/QM2-Retrain: Verdikte und Rollout-Empfehlung

**Stand:** 2026-08-01 · **Session:** VPS (SRV02), Live-Tabellen ausschließlich read-only ·
**Vorgänger:** T-2026-CU-9050-061 (Recompute + Retrain-Rerun), Roster-Validation 2026-07-14,
`_X\staging_models\T061_RETRAIN_REPORT.md` (2026-07-12, Pre-Wilder-Stand)

## Auftrag und Ergebnis in einem Absatz

Zu bewerten waren die Artefakte des Post-Wilder-Retrain-Reruns vom 2026-07-14 (Phase A
`bb_1h`, Phase B QM2/`td_4h`/`bb_4h`/`td_1h`), dazu die QM2-Lücke im Replay-Retrain-Pfad.
**Die TD/BB-Artefakte dieses Reruns existieren nicht mehr** — sie wurden am selben Tag,
Minuten nach ihrer Erzeugung, von einem Legacy-Trainer-Lauf überschrieben, der denselben
Dateinamen im selben Staging-Verzeichnis benutzt. Erhalten sind nur ihre Metriken. Diese
Metriken tragen das Verdikt trotzdem, und es ist **NO-GO für alle vier**: TD_1H ist
anti-kalibriert (bestätigt den Batch-E-Vorbefund), BB_1H/BB_4H kippen zwischen Validation
und Test das Vorzeichen, TD_4H hat einen Selektionswert nahe null. Eine Rekonstruktion lohnt
nicht: die einzige replay-retrainierte Generation, die je live war (BB2_4H, 06.–13.07.),
realisierte **−1,57 % je Bein über 99 Beine**, während das Legacy-Artefakt, das sie ersetzte
und das sie wieder ersetzt hat, über fünf Monate **+0,25 % je Bein über 3.076 Beine** bucht.
QM2 wird **begründet ausgeklammert**. Nichts promotet, nichts deployt, kein Gate angefasst.

---

## 1. Der zentrale Befund: die Rerun-Artefakte wurden überschrieben

`tools/retrain_from_replay.py:423` und `smc_ml_trainer.py:376` schreiben **denselben Pfad**:

```
retrain_from_replay.py:423   STAGING_DIR/{strategy}_xgboost_model_{tf}.pkl
smc_ml_trainer.py:376        STAGING_DIR/{prefix}_xgboost_model_{tf}.pkl
```

Zeitachse am 2026-07-14 in `C:\Users\Michael\Documents\_X\staging_models\` (mtimes selbst
abgelesen):

| Zeit | Ereignis |
|---|---|
| 02:47:20 | `retrain_td_4h_stats.json` |
| 04:07:08 | `retrain_bb_4h_stats.json` |
| 04:33:07 | `retrain_td_1h_stats.json` |
| 05:21:24 | `retrain_bb_1h_stats.json` |
| **05:21:40 – 05:23:50** | **alle vier `{td,bb}_xgboost_model_{1h,4h}.pkl` neu geschrieben** |

`retrain_from_replay` schreibt das pkl **vor** seiner Stats-Datei (`run_td_bb` →
`save_artifact`, dann `main` → Stats). Ein pkl, dessen mtime **nach** der eigenen Stats-Datei
liegt, ist also überschrieben worden — für `td_4h` um 2,6 Stunden, für `bb_1h` um 16 Sekunden.

Der Inhalt beweist es unabhängig vom Zeitstempel. Die vier heutigen Dateien tragen
`meta.trainer = 'smc_ml_trainer.py'`, `optimal_threshold = 0.3`, **kein** `calibrator_isotonic`
und **keine** `meta.model_id`. `retrain_from_replay.save_artifact` schreibt beide Keys
immer (`:376-385`, `model_id` in `:410`). Die Artefakte stammen also nicht aus dem Rerun.

**Konsequenz:** Der Rerun ist gelaufen und gemessen, aber sein Produkt ist weg. Was in
`staging_models/` und im Repo-Root liegt, ist die Legacy-Generation.

### 1a. Der Legacy-Trainer labelt gegen die P0.10-Regel

Nicht nur „eine andere Generation", sondern die Generation, die das Replay-Programm ersetzen
sollte: `smc_ml_trainer.py:153/185` labelt gegen ein **synthetisches 2R-Bracket**
(`RR_RATIO = 2.0`, `tp = entry ± dist * RR_RATIO`) — nicht gegen die geposteten
Smart-Targets. Genau der idealisierte Fill, den P0.10 verboten hat und dessentwegen
`tools/walkforward_sim.py` überhaupt gebaut wurde.

### 1b. Der Überschreib-Vorfall hat still den Modell-Tag zurückgedreht

`25_smc_ml_sniper.py:101/117` nimmt das Posting-Tag aus `meta.model_id` und fällt sonst auf
`{STRATEGY}_{TF}` zurück. In `ml_predictions_master` (read-only) ist der Wechsel sichtbar:

| Tag | n | erste | letzte |
|---|---|---|---|
| `TD2_4H` | 118 | 2026-07-06 | **2026-07-13** |
| `BB2_4H` | 1338 | 2026-07-06 | **2026-07-13** |
| `TD_4H` | 112 | 2026-06-25 | 2026-08-01 |
| `BB_4H` | 7992 | 2026-06-25 | 2026-08-01 |

Die replay-retrainierte Generation war vom 06. bis 13.07. live und ist danach durch die
Legacy-Artefakte ersetzt worden — die Tags fielen auf die Alt-Namen zurück. Bestätigt durch
`_X\live_backup_20260714_194105\`, das exakt die *vorherigen* Live-Artefakte enthält
(Größen 802727/739062/671759/586996 = die „vorher"-Seite des Commits `14e1c6f`).

**Regel-6-Bewertung:** Der Tag-Rückfall ist keine Regel-6-Verletzung — der Bot verhält sich
korrekt, das Artefakt trug schlicht keine `model_id`. Die Verletzung liegt eine Ebene tiefer:
eine Generationsablösung ist ohne Ledger-Spur passiert.

### 1c. Gegenmaßnahme in diesem PR

`core/staging_guard.assert_no_foreign_overwrite` weigert sich, ein Artefakt zu überschreiben,
dessen `meta.trainer` von dem des laufenden Trainers abweicht — verdrahtet in allen drei
Schreibern (`retrain_from_replay`, `smc_ml_trainer`, `qm_ml_trainer`). Bewusst **fail-open**:
fehlende oder unlesbare Provenienz blockt nichts, nur die belegte Kreuzung. Bewusster Tausch:
`KYTHERA_ALLOW_TRAINER_OVERWRITE=1`. Gepinnt in `backtest/test_staging_guard.py` (8 Tests,
DB-frei), inklusive des realen 07-14-Falls in beide Richtungen.

---

## 2. Was heute live ist (selbst nachgesehen, nicht aus Doku)

SHA256-Vergleich Repo-Root ↔ `_X\staging_models` ↔ git-HEAD, plus pkl-Innenansicht:

| Artefakt | Root == Staging | `meta.trainer` | `optimal_threshold` | Live-Gate | Tag |
|---|---|---|---|---|---|
| `td_xgboost_model_1h.pkl` | identisch | `smc_ml_trainer.py` | 0,30 | **0,30** | `TD_1H` |
| `td_xgboost_model_4h.pkl` | identisch | `smc_ml_trainer.py` | 0,30 | **0,30** | `TD_4H` |
| `bb_xgboost_model_1h.pkl` | identisch | `smc_ml_trainer.py` | 0,30 | **0,50** | `BB_1H` |
| `bb_xgboost_model_4h.pkl` | identisch | `smc_ml_trainer.py` | 0,30 | **0,50** | `BB_4H` |
| `qm_xgboost_model_1h.pkl` | **verschieden** | (keine meta) | 0,30 | **0,65** | `QM_1H` |
| `qm_xgboost_model_4h.pkl` | **verschieden** | (keine meta) | 0,30 | — (geparkt) | — |

Gate-Herleitung: `25_smc_ml_sniper.py:93-99` nimmt `max(optimal_threshold, MIN_PROB_FLOORS)`,
BB-Floor 0,50 (T-171), TD-Floor 0,0 — der BB-Floor **hebt** also beide BB-Gates über den
Artefakt-Wert. Bot 24 ignoriert `optimal_threshold` vollständig und gatet auf der
hartkodierten `MIN_CONFIDENCE = 0.65` (`24_quasimodo_bot.py:45/321`). QM_4H ist im Code
geparkt (`TIMEFRAMES = ['1h']`, `:42`, Audit-Report 14/16) — die leere QM_4H-Spur seit 03.07.
ist kein Defekt.

---

## 3. Verdikte der Rerun-Metriken

Aus `retrain_{td_1h,td_4h,bb_1h,bb_4h}_stats.json` (2026-07-14). Threshold-Wahl bei td/bb
läuft über `pick_threshold` (Summen-PnL), **nicht** über `pick_threshold_safe` — die
Migration hat td/bb nie erreicht (`retrain_from_replay.py:401` ohne `picker=`, gegen
`:611/694/768/841/917` bei mis1/rub/ats/epd/atb2).

| Modell | Thresh | Val Σ PnL | Test genommen | Test Σ PnL | Test-WR vs. Basis | Verdikt |
|---|---|---|---|---|---|---|
| **TD_1H** | 0,80 | **−78,2** (n=48) | 33/462 (7 %) | **−75,2** | 57,6 % vs 56,5 % | **NO-GO — anti-kalibriert** |
| **TD_4H** | 0,50 | +9,7 (n=59) | 76/122 (62 %) | +19,4 | 59,2 % vs **60,7 %** | **NO-GO — Selektionswert ~0** |
| **BB_1H** | 0,40 | +379,6 (n=5588) | 5603/5684 (**99 %**) | **−241,2** | 58,3 % vs 58,1 % | **NO-GO — Gate ist ein No-op** |
| **BB_4H** | 0,50 | +489,2 (n=871) | 1012/1336 (76 %) | **−686,0** | 57,6 % vs 54,7 % | **NO-GO — filter-only, bestätigt** |

Drei Befunde tragen das Verdikt:

**(a) TD_1H ist anti-kalibriert.** Die Test-Kalibrierung läuft der Wahrscheinlichkeit
entgegen: Bucket 0,0–0,3 → **+4,04 %** Ø-Netto-PnL, Bucket 0,8–1,0 (= das Live-Gate) →
**−2,28 %**, der schlechteste aller sieben Buckets. Die Validation war bereits negativ, und
`pick_threshold` liefert trotzdem einen Threshold, weil ihm — anders als
`pick_threshold_safe` (`:300-302`) — der Deployability-Abbruch fehlt. Identische Diagnose wie
Batch E und wie der Pre-Wilder-Lauf vom 12.07. **Dreimal unabhängig reproduziert.**

**(b) Val→Test kippt bei drei von vier das Vorzeichen** (BB_1H +380→−241, BB_4H +489→−686,
TD_4H nur knapp positiv). Der auf Validation gewählte Threshold generalisiert nicht.

**(c) Das BB_1H-Gate selektiert nicht.** Bei Threshold 0,40 nimmt es 98,6 % der Test-Events —
exakt die Degeneration, die der Code an `pick_threshold_safe:274-278` selbst beschreibt
(„belohnt Volumen → degeneriert zum Take-almost-all").

Rechnet man die Buckets auf das **tatsächliche Live-Gate** um (BB-Floor 0,50 statt 0,40),
verschwindet der BB_1H-Verlust weitgehend (Σ ≈ −37 über n = 4.848, ≈ −0,01 %/Trade) — BB_1H
wäre live also eher flach als schädlich. BB_4H bleibt bei 0,50 unverändert bei −0,68 %/Trade,
TD_1H bei 0,80 unverändert bei −2,28 %/Trade.

### Vergleich mit dem Pre-Wilder-Lauf (12.07.)

Der Wilder-Rewrite hat die Kohorte **verschlechtert**, nicht verbessert:

| Modell | 12.07. (Pre-Wilder) | 14.07. (Post-Wilder) |
|---|---|---|
| TD_4H | 110/136, WR 66,4 % vs 65,4 %, **+185,8** → „Promoten" | 76/122, WR 59,2 % vs **60,7 %**, +19,4 → NO-GO |
| BB_4H | 733/1289, WR 58,0 % vs 54,1 %, −604,9 → filter-only | 1012/1336, WR 57,6 % vs 54,7 %, −686,0 → filter-only |
| TD_1H | 39/505, Val −69,5 → „Parken" | 33/462, Val −78,2, Test −75,2 → NO-GO |

Die einzige Promotions-Empfehlung des 12.07.-Reports (TD2_4H) trägt auf der
Wilder-Verteilung nicht mehr.

### Vertrauen in die Replay-Zahlen (T-2026-KYT-9050-008-Prüfung)

Auftragsgemäß geprüft, ob die Artefakte **vor** oder **nach** dem T-008-Fix entstanden:
**davor** (14.07. vs. Fix 01.08.) — aber ohne Wirkung auf diese Kohorte. Der Epoch-Defekt
sitzt in `walkforward_sim.py:889` und trifft ausschließlich `slope_trend` in der
RUB-Regression; die td/bb-Replays laufen über `run_td_bb` (`:475`) und die 20
`SNIPER_FEATURES`, die weder `slope_trend` noch `dist_to_trend` enthalten. `epoch_seconds`
kommt im td/bb-Pfad nicht vor. Zusätzlich ist der Fix laut T-008 unter dem Fleet-Interpreter
byte-gleich zum Vorzustand. Die beiden Look-ahead-Fixes (`ac49bc3`, `21a97a6`, beide 10.07.)
liegen **vor** dem Rerun. Die Replay-Kurve trägt hier also.

---

## 4. Live-Gegenprobe: was die Modelle real gebucht haben

`closed_ai_signals`, dedupliziert über `tools/fleet_realized_audit.load_ai_rows`
(DISTINCT ON `symbol, model, direction, open_time`), realisierter **unhebelter**
target-gestaffelter Move je Bein — die kanonische Fleet-Definition (T-115). Bewusst
unhebelt: die `lev`-Spalte ist erst ab Mitte Juli befüllt, ein Hebel-PnL hätte das Fenster
auf 2,5 Wochen verkürzt.

| Tag | n | WR | Ø Move/Bein | Fenster |
|---|---|---|---|---|
| `TD_4H` | 697 | 38,6 % | **+1,080 %** | 03-09 .. 08-01 |
| `TD_1H` | 2538 | 39,0 % | **+0,906 %** | 03-07 .. 08-01 |
| `BB_4H` | 3076 | 41,9 % | **+0,249 %** | 03-07 .. 08-01 |
| `QM_1H` | 3175 | 36,3 % | +0,065 % | 03-06 .. 08-01 |
| `BB_1H` | 4093 | 31,9 % | **−0,256 %** | 03-07 .. 07-27 |
| **`BB2_4H`** (Replay-Retrain, live 06.–13.07.) | 99 | 36,4 % | **−1,572 %** | 07-10 .. 07-29 |

Die niedrige WR bei positivem Ø ist erwartbar: gestaffelte TPs buchen viele kleine
Teilgewinne als „Verlust-Bein" mit, wenn der Rest zum SL zurückläuft.

**Das ist die härteste Zahl des Reports:** die einzige replay-retrainierte Generation, die je
in Produktion war, hat verloren — konsistent mit ihrer eigenen negativen Test-Slice
(Σ −686). n = 99 über drei Wochen ist klein und kann Tape sein; die Richtung stimmt aber mit
der Replay-Messung überein, also zwei unabhängige Negative.

### Richtungs-Split — und ein Vorzeichen-Konflikt zur Studie

| Tag | LONG n / Ø | SHORT n / Ø |
|---|---|---|
| `TD_1H` | 1468 / **+1,523 %** | 1070 / +0,059 % |
| `TD_4H` | 433 / **+1,301 %** | 264 / +0,718 % |
| `BB_4H` | 1297 / **+1,216 %** | 1779 / **−0,456 %** |
| `BB_1H` | 1727 / **+1,345 %** | 2366 / **−1,425 %** |
| `QM_1H` | 1605 / +0,422 % | 1570 / **−0,300 %** |

Die Roster-Validation vom 14.07. (`_X\staging_models\significance\{TD,BB}{1h,4h}.json`,
1000 Bootstraps über die 540d-Replays) sagt für **jede** Zelle das Gegenteil: `*/LONG`
p_value 0,993–1,000 und `sharpe_prob_positive = 0,0`, `*/SHORT` p 0,001–0,002 und
`prob_positive` 0,999–1,0 (Ausnahme TD4h-SHORT: p 0,25, nicht signifikant — der
„TD4h-SHORT tot"-Vorbefund bestätigt sich).

**Live ist LONG die tragende Seite, im Replay ist es SHORT.** Der Konflikt ist real und
nicht durch das Zeitfenster allein erklärbar (5 Monate live vs. 540 Tage Replay,
überlappend). Zwei strukturelle Kandidaten, beide am Code sichtbar, keiner hier bewiesen:

1. **Verschiedene Grundgesamtheiten.** Der Replay bewertet *alle* Detektor-Signale; live
   überlebt nur, was Modell-Gate, Prob-Floor, Cooldown und Orchestrator-Whitelist passiert.
2. **Verschiedene Ökonomie.** Der Replay labelt First-Touch TP1-vor-SL und rechnet
   `net_pnl_pct` auf dieser Binär-Geometrie; live bucht der Monitor gestaffelte Teil-TPs mit
   Trailing. Ein auf Replay-`net_pnl` optimierter Threshold optimiert damit **nicht** die
   Größe, die die Fleet realisiert.

Solange dieser Konflikt nicht aufgelöst ist, ist eine Promotion auf Basis von Replay-PnL
nicht vertretbar — unabhängig davon, wie die Metriken ausfallen. Das ist ein eigener
Untersuchungs-Scope und **kein** Nebenprodukt dieses Tasks.

---

## 5. Rollout-Empfehlung (Operator-Entscheid Michi — nichts davon ausgeführt)

| Modell | Empfehlung | Begründung |
|---|---|---|
| **TD_1H** (Replay-Retrain) | **Nicht rekonstruieren, nicht promoten** | Anti-kalibriert, dreimal reproduziert; Val negativ |
| **TD_4H** (Replay-Retrain) | **Nicht rekonstruieren, nicht promoten** | Selektion unter Basisrate; Pre-Wilder-Empfehlung trägt nicht mehr |
| **BB_1H** (Replay-Retrain) | **Nicht rekonstruieren, nicht promoten** | Gate nimmt 99 % — kein Gate |
| **BB_4H** (Replay-Retrain) | **Nicht rekonstruieren, nicht promoten** | filter-only bestätigt; live −1,57 %/Bein als BB2_4H |
| **QM2_1H / QM2_4H** | **Nicht promoten** | s. §6 — kein Replay-Pfad, Bot ignoriert den Threshold |
| **Live-Bestand TD/BB/QM** | **Unverändert lassen** | Über 5 Monate positiv (außer BB_1H); kein besserer Kandidat existiert |

**Die TD/BB-Replay-Retrain-Linie ist damit als NO-GO geschlossen.** Drei Läufe
(06.07., 11./12.07., 14.07.) haben keinen deploybaren Kandidaten erzeugt; der eine, der live
ging, hat verloren. Ein vierter Lauf auf derselben Methodik ist keine sinnvolle Investition,
solange §4 offen ist.

### Der Hebel, der stattdessen etwas bringen würde

Nicht das Modell, sondern die **Richtung**. Drei Zellen sind über 5 Monate und große
Stichproben negativ:

| Kandidat | n | Ø Move/Bein | Grober Jahres-Effekt bei gleichem Volumen |
|---|---|---|---|
| `BB_1H` SHORT | 2366 | −1,425 % | −3.371 Bein-Prozentpunkte im Messfenster |
| `BB_4H` SHORT | 1779 | −0,456 % | −812 |
| `QM_1H` SHORT | 1570 | −0,300 % | −471 |

Ein SHORT-Park dieser drei Beine ist ein Ein-Zeilen-Eingriff pro Bot, reversibel, und trifft
eine belegte Verlustquelle — im Gegensatz zu einem weiteren Retrain. **Aber:** er widerspricht
frontal der Replay-Studie (§4) und der fleet-weiten Short-only-Linie der Roster-Validation.
**Deshalb ausdrücklich als Vorschlag zur Entscheidung, nicht als Empfehlung zum Ausführen** —
Parken/Entparken ist ohnehin C-Gate (OPUS-HANDOFF §6).

---

## 6. QM2-Lücke: begründet ausgeklammert

**Entscheid: kein Replay-Retrain-Pfad für QM2. Ausklammern, nicht bauen.** Vier Belege am
Code, keine Behauptung:

1. **Der Pfad fehlt in beiden Werkzeugen.** `tools/walkforward_sim.py:1151`
   `choices=["ufi1","td","bb","abr1","mis1","rub","atb2","ats"]` und
   `tools/retrain_from_replay.py:980` `choices=["td","bb","abr1","mis1","rub","epd","atb2","ats"]`
   — beide ohne `qm`. Bauen hieße: ein `run_qm` mit Quasimodo-Detektor über 540 d × 527 Coins
   plus ein `qm`-Zweig im Trainer. Zum Größenvergleich: das `bb_1h`-Replay derselben
   Kohorte ist 48 MB JSONL.
2. **Das Hauptprodukt eines Replay-Retrains erreicht den Bot gar nicht.**
   `24_quasimodo_bot.py:45` setzt `MIN_CONFIDENCE = 0.65` hart und gatet damit
   (`:294/321`); `optimal_threshold` aus dem Artefakt wird nie gelesen. Der auf
   Validation-PnL kalibrierte Threshold — der eigentliche Mehrwert des Pfades — wäre wirkungslos,
   solange Bot 24 nicht zusätzlich geändert wird. (Bekannt als Teil von AUDIT_TODO P3.6:
   „Thresholds im pkl aber Bots hardcoden".)
3. **Die halbe Fläche ist ohnehin geparkt.** `TIMEFRAMES = ['1h']` (`:42`) — QM_4H steht seit
   Audit-Report 14/16 still. Ein QM2_4H hätte keinen Konsumenten.
4. **Die Schwester-Strategien desselben Bot-Paars sagen NO-GO.** td und bb teilen Feature-Satz
   (`SNIPER_FEATURES`), Detektor-Familie und Replay-Maschinerie mit qm. Drei Läufe dort haben
   keinen deploybaren Kandidaten geliefert (§3/§5). Die Erwartung, dass qm als vierte
   Variante derselben Pipeline ausschert, ist nicht begründbar.

Ökonomisch: QM_1H bucht live +0,065 %/Bein über 3.175 Beine — praktisch Null-EV, und mit
31 Posts in fünf Wochen (`ml_predictions_master`) die kleinste Fläche der Kohorte. Der
erwartbare Ertrag eines Neubaus steht in keinem Verhältnis zum Aufwand.

**Was ein späterer Bau bräuchte** (falls Michi ihn doch will, als Vorbedingungs-Liste):
(1) `run_qm` in `walkforward_sim`, (2) `qm`-Zweig in `retrain_from_replay` mit
`picker=pick_threshold_safe`, (3) Bot 24 liest `optimal_threshold` statt `MIN_CONFIDENCE`,
(4) vorher §4 auflösen — sonst optimiert der Pfad die falsche Zielgröße.

Die QM2-Artefakte aus dem Legacy-Trainer (`_X\staging_models\qm_xgboost_model_{1h,4h}.pkl`,
14.07., `model_id` QM2_1H/QM2_4H, Thresholds 0,55/0,50) haben den Überschreib-Vorfall
überlebt — sie liegen weiter in Staging und bleiben unpromotet.

---

## 7. bfill: NICHT angefasst — was beim späteren Rollout mitgehen muss

Auftragsgemäß **keine Änderung**. Zur Aktenlage, mit korrigierten Zeilennummern (die im
Ticket genannten `:126` / `:220` sind veraltet):

| Ort | heutige Zeile | Kontext |
|---|---|---|
| `24_quasimodo_bot.py` | **:140-141** | `df.ffill(); df.bfill()` nach `read_candles_with_indicators(limit=100)` |
| `25_smc_ml_sniper.py` | **:311-312** | dito, `limit=150` |
| Gegenstück | `tools/walkforward_sim.py:263-270` | `ffill()` + **`dropna()`** statt bfill, mit Begründung seit T-045 |

Der Replay **verwirft** die Warmup-Kopfzeilen, die Bots **imputieren** sie aus der Zukunft.
Solange die Bots auf Artefakten laufen, die auf imputierten Kopfzeilen trainiert wurden
(= heute, Legacy-Generation), ist das symmetrisch und darf nicht isoliert entfernt werden.
Beim Rollout eines replay-trainierten Artefakts müssen beide `bfill`-Aufrufe **im selben
Schritt** fallen, sonst sieht Serving eine Zeilenklasse, die Training nie gesehen hat.

Da §5 „nicht promoten" empfiehlt, **bleibt das bfill stehen** — der Kopplungspunkt wird
hiermit nur dokumentiert. Praktische Einschränkung, die die Dringlichkeit senkt: beide Bots
lesen nur die neuesten 100 bzw. 150 geschlossenen Kerzen, das Fenster enthält also nur dann
NaN-Kopfzeilen, wenn die Gesamt-Historie des Coins kurz ist — die Population sind junge
Listings, nicht der Bestand.

---

## 8. Offener PR-43-Befund für Michi (Zahlen, kein Entscheid)

Train/Serve-Skew-Fenster auf Neu-Listings zwischen Deploy und Retrain. Aktueller Stand:

- **Der Skew existiert weiter**, weil beide Seiten unverändert sind: Engine schreibt seit
  T-054 NaN-Kopfzeilen, die Bots imputieren sie via `bfill` (§7).
- **Die Population** sind Coins unterhalb der Warmup-Grenze — bei `limit=150` (Bot 25) und
  `limit=100` (Bot 24) also Coins mit weniger als ~350 bzw. ~300 Kerzen Historie
  (1h ≈ 15 bzw. 12 Tage, 4h ≈ 58 bzw. 50 Tage). Exakte Coin-Zahl **nicht vermessen** —
  hätte eine Zählquery über alle `_indicators`-Tabellen gebraucht, die außerhalb des
  read-only-Rahmens dieser Session sinnvoll gewesen wäre.
- **Die Mitigations-Optionen (a)/(b)/(c) bleiben unverändert gültig.** Neu ist nur, dass
  Option (c) (Coins unter der Warmup-Grenze überspringen) inzwischen der einzige Weg ist, der
  **ohne** Retrain-Rollout etwas ändert — und §5 empfiehlt genau keinen Rollout. Damit
  entkoppelt (c) das Fenster dauerhaft vom Retrain-Programm.
- **Größenordnung aus der Nachbarschaft:** T-2026-KYT-9050-008 hat das verwandte
  Mixed-History-Risiko auf RUB2-SHORT erstmals gemessen — ≈ 1 Prozentpunkt
  Wahrscheinlichkeits-Drift. Kein Beweis für den P1.13-Fall, aber die einzige vorhandene
  Kalibrierung der Größenordnung.

---

## 9. Bewusst NICHT gemacht

Kein Promote, kein Rollout, kein Deploy, kein Restart, kein Parken/Entparken, kein Gate-Flip,
keine Schreib-Query gegen Live-Tabellen, kein Replay- oder Trainings-Lauf (damit auch kein
Job-Lock nötig — es lief kein schwerer Job). Das `bfill` in Bot 24/25 ist unangetastet. Die
überschriebenen Artefakte wurden **nicht** rekonstruiert (§5 empfiehlt es nicht, und ein
Replay-Lauf wäre ein schwerer Job gewesen). `staging_models/` im Repo ist unverändert.

## 10. Offen

- Der Vorzeichen-Konflikt Replay ↔ Live auf der Richtungs-Achse (§4) ist **nicht** aufgelöst.
  Er entwertet Replay-PnL als Promotions-Kriterium für diese Bot-Familie, bis geklärt ist,
  welche der beiden strukturellen Ursachen trägt. Eigener Task-Kandidat.
- Ob der SHORT-Park (§5) gefahren wird: Michi.
- PR-43-Optionen (a)/(b)/(c): Michi.
- `pick_threshold` bei td/bb ist nie auf `pick_threshold_safe` migriert worden. Da die Linie
  geschlossen ist, hier bewusst **nicht** nachgezogen — eine Migration ohne Konsumenten wäre
  toter Code. Bei Wiederaufnahme der Linie ist es der erste Schritt.

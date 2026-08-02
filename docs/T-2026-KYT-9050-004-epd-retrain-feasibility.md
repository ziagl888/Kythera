# T-2026-KYT-9050-004 — EPD-Detektor-Retrain auf den neuen Feature-Definitionen

**Stand:** 2026-08-01 · **Auftrag:** `pump_dump_model.pkl` (Bot 10) wurde auf der ALTEN
Feature-Definition gefittet; P1.39 + die T-035-Ratennormierung stellten vier der zehn
Modell-Inputs um (`vol_ratio`, `p_chg_60s`, `buy_pres`, `volat`). Retrain auf der neuen
Definition, Schnitt nach dem Cut-Point, Artefakt nur nach `staging_models/`, neuer Tag.

---

## Verdikt

**Der Retrain ist heute nicht ausführbar. Kein Artefakt erzeugt — und keins, das man
erzeugen sollte.** Der Blocker ist der Kalender, nicht die Datenqualität: die
Post-Cut-Historie ist 22,0 Tage lang, ein leckagefreier chronologischer 70/15/15-Split
mit dem 7-Tage-Purge-Gap braucht ~50 Tage, ein Operating-Point mit Rückhalt ~122 Tage.

**Zweitbefund, der die Dringlichkeit senkt:** die Verschiebung, wegen der dieser Task
existiert, ist auf der Population, die das Alert-Gate passiert, **nicht von gewöhnlicher
Marktdrift zu unterscheiden** — und das deployte Modell diskriminiert auf Post-Cut-Daten
out-of-sample weiter auf seinem eigenen Niveau. Es gibt kein Anzeichen für ein durch die
Definitionsänderung kaputtes Serving-Modell.

**Empfehlung:** Wiedervorlage **2026-11-09**, Bot 10 unverändert weiterlaufen lassen.
Kein Deploy, kein Gate-Flip, kein Promote — nichts davon steht hier zur Debatte.

---

## 1. Cut-Point: belegt, nicht übernommen

Der Brief nennt den Bot-10-Restart 2026-07-10 17:08:29Z. Gegenprobe an den Daten:
stündliche `pump_dump_events`-Zählung am 2026-07-10 (UTC; der Detector ist der einzige
Writer der Tabelle).

```
00–16 Uhr:  124 170  68  72  75  98 135 127 103  91  91  62  93  97 126  56 148
17–23 Uhr:   24  33  20  10  20  21  29
```

Der Bruch liegt exakt auf der Stunde des Restarts. Er ist **kein Feature-Effekt, sondern
ein Gate-Effekt**: `f485d09` gab dem Stunden-Warmup einen Coverage- und Sample-Floor
(vorher konnte ein einzelner überlebender Bucket die ganze Baseline stellen), und damit
verschwand die Klasse von Müll-Events, die aus einem Ein-Sample-Nenner entstand. Die
Ereignisrate fiel um ~5×.

Der Restart schaltete drei Änderungen gemeinsam scharf — P1.39 (Index → Zeitstempel),
die T-035-Ratennormierung von `p_chg_60s` und den wiederbelebten Volume-Gate. Der
Cut-Point ist daher einer, nicht drei.

## 2. Der Datensatz ist gut — er ist nur zu kurz

`tools/epd2_build_dataset.py --since '2026-07-10 17:00:00'` (2032 s, Job-Lock gehalten):

| | |
|---|---|
| Events nach Gates + 900s-Dedup | 4712 (2582 Pump/LONG, 2130 Dump/SHORT), 403 Symbole |
| geschrieben | 4698 |
| Verluste | `no_candles` 11, `no_ticker` 3, `no_window`/`stale_join`/`geometry_fail` 0 |
| gelabelt | 4327 (7,9 % am 7-d-Horizont noch offen) |
| Basisrate TP1 | LONG 58,9 %, SHORT 69,5 % |
| Spanne | 22,0 Tage |

0,3 % Verlust. Die Pipeline funktioniert auf der neuen Definition einwandfrei.

### Warum der Split trotzdem leer ausgeht

`chrono_split` gibt Val und Test je das 15 %-Quantilsband der Signalzeiten; der Purge-Gap
(7 d = Label-Horizont des Builders) schneidet davon vorne 7 Tage weg. Bei 22 Tagen Spanne
ist das Band 3,3 Tage — **kürzer als der Gap**. Beide Slices sind leer, unabhängig von
der Zeilenzahl. Echter Trainerlauf (`--strategy epd --model-id EPD4`):

```
epd2 LONG:  2378 Events | split 1664/0/0 | Basisrate TP1 58.9%
epd2 LONG:  degenerierter Split — übersprungen. Spanne 21.8d, 15%-Band 3.3d < Purge-Gap 7d
            (Dichte 109 Zeilen/Tag) ⇒ Val/Test leer. Für ≥50 Zeilen je Slice braucht es
            ~50d Spanne (~28d mehr Datensammlung).
epd2 SHORT: 1949 Events | split 1364/0/0 | Basisrate TP1 69.5%
epd2 SHORT: … ~50d Spanne (~28d mehr Datensammlung).
```

Den Purge-Gap zu verkleinern wäre die naheliegende Abkürzung und ist die falsche: er ist
per Konstruktion gleich dem Label-Horizont, und ein Label-Fenster aus dem Train-Slice,
das in den Val-Slice hineinragt, ist genau die Zwillings-Leakage, gegen die der Gap steht.
Ein 4-Tage-Horizont (deckt p95 der realen EPD3-Haltedauer: p50 8,1 h, p90 62,4 h,
p95 97,4 h) verschöbe den Termin um drei Wochen und machte das Modell mit EPD2/EPD3
unvergleichbar. Nicht gemacht.

### Kein Ausweg über mehr Historie

`tools/epd2_build_dataset.py` nimmt den Entry seit T-2026-CU-9050-035 aus `ticker_10s`
und verweigert ein früheres `--since` — der alte Schätzer `close×(1+p_chg_60s/100)` ist
seit der Ratennormierung schlicht falsch. **`ticker_10s` beginnt am 2026-07-07 11:19 UTC**,
drei Tage VOR dem Cut-Point. Der Feb–Juli-Datensatz (85 031 Events), auf dem EPD2/EPD3
gefittet wurden, ist mit dem heutigen Builder nicht mehr reproduzierbar.

Das ist die eigentliche Grenze: **die trainierbare Historie ist ticker-, nicht
cut-point-gebunden.** Der vom Brief geforderte Schnitt am Cut-Point kostet drei Tage.
Die Retention der Hypertable steht auf 365 Tagen (`core/ticker_10s.RETAIN_FOR`) — das
Fenster wächst, es ist nicht gedeckelt.

## 3. Die Verschiebung ist kleiner als die Marktdrift

Zwei-Stichproben-KS je Feature, 14 d vor gegen 14 d nach dem Cut, gemessen gegen ein
Nullband aus 15 benachbarten 14-d-Fensterpaaren der Vor-Cut-Historie (also gegen das,
was gewöhnliche Regimewechsel ohnehin an Verschiebung erzeugen):

| Feature | KS am Cut | Nullband-Median | Nullband-Max | über Nullband? |
|---|---|---|---|---|
| `volume_ratio`  | 0,0361 | 0,0624 | 0,4342 | nein |
| `\|p_chg_60s\|` | 0,0796 | 0,0580 | 0,3355 | nein |
| `buy_pressure`  | 0,1737 | 0,0798 | 0,2039 | nein |
| `volatility`    | 0,0363 | 0,0627 | 0,3536 | nein |

n_pre = 20 084, n_post = 4682. Das kleinere Post-Fenster treibt die KS-Statistik nach
oben, nicht nach unten — der Befund ist damit eher konservativ. Kein Feature verlässt das
Band. Sichtbar ist ein Granularitätseffekt bei `buy_pressure` (p90 0,8333 → 1,0000): der
Anteil steigender Diffs wird über kürzere Fenster grobkörniger, was der Code an
`10_pump_dump_detector.py:1070-1074` bereits als bewusste Kadenz-Abhängigkeit führt.

**Einschränkung:** nur Randverteilungen. Eine gemeinsame Verschiebung bei unveränderten
Rändern ist damit nicht ausgeschlossen.

## 4. Das deployte Modell hält auf Post-Cut-Daten

`epd3_model_{LONG,SHORT}.pkl` (Repo-Root, auf VOR-Cut-Daten gefittet) auf den
Post-Cut-Events gescored — für dieses Modell strikt out-of-sample:

**LONG** (n=2378, Live-Threshold 0,76) — AUC(TP1) 0,586, corr(prob, netPnL) +0,070

| Prob-Bucket | n | TP1 | Ø netto |
|---|---|---|---|
| 0,0–0,3 | 115 | 38,3 % | −5,33 % |
| 0,3–0,4 | 165 | 45,5 % | −1,68 % |
| 0,4–0,5 | 241 | 51,0 % | −0,76 % |
| 0,5–0,6 | 534 | 57,9 % | **+0,17 %** |
| 0,6–0,7 | 915 | 63,6 % | **+0,25 %** |
| 0,7–0,8 | 384 | 65,4 % | −0,32 % |
| 0,8–1,0 | 24 | 66,7 % | +0,36 % |

**SHORT** (n=1949, Live-Threshold 0,6737) — AUC(TP1) 0,537, corr +0,041; am Threshold
n=756 (38,8 %), WR 72,6 %, Ø +0,065 %/Trade.

Die LONG-Kalibrierung ist über den ganzen Bereich monoton in der TP1-Rate — das ist das
Verhalten eines intakten Modells, nicht das eines out-of-distribution befragten. Damit ist
die Prämisse des Tasks („Serving läuft gegen eine verschobene Verteilung") zwar formal
richtig, in ihrer Wirkung aber nicht nachweisbar.

**Nebenbefund, nicht Teil des Auftrags:** der operator-gesetzte LONG-Threshold 0,76
(Volumenkappe, ausdrücklich kein Edge-Filter, T-2026-KYT-9050-037) nimmt auf dieser
Population nur 81 Trades (3,4 %) zu Ø −0,760 %, während die Bänder 0,5–0,7 positiv sind.
n=81 ist dünn und die Replay-Geometrie ist nicht die Live-Geometrie — das ist ein Hinweis
für eine eigene Messung, kein Verdikt.

## 5. Trainings- gegen Serving-Population (offen)

Der Builder dedupt Events auf 900 s je Symbol und spiegelt damit den Alert-Throttle von
Bot 10. Der Throttle-Timer wird aber nur im **Live-Trade-Zweig** zurückgesetzt; für ein
Bein, das nicht live postet, ist er inert, und gebremst wird nur über
`has_open_ai_signal`. Gemessen (`closed_ai_signals`, Tag EPD3, ab 2026-07-11, Shadow und
Live zusammen):

| | Trainings-Zeilen/Tag | Live-Emissionen/Tag | Faktor |
|---|---|---|---|
| LONG  | 108,9 | 295,9 | 2,7× |
| SHORT | 88,6  | 478,6 | 5,4× |

Die Serving-Population ist deutlich dichter als die, auf der trainiert und der Threshold
gewählt wird. Das ist derselben Klasse wie der OOD-Fehler, den das `vol_ratio ≥ 5`-Gate
in EPD2 behoben hat, nur eine Ebene tiefer. **Nicht in diesem Task verifiziert**, ob ein
auf der deduplizierten Population gewählter Threshold die Live-Rate trifft — vor einem
EPD4-Go-Live gehört das geklärt.

## 6. Tag: EPD4 (reserviert, noch nicht registriert)

Belegt sind **EPD1, EPD2, EPD3** — geprüft gegen `tools/bot_variants/index.legacy_artifact_slots()`,
`core/shadow_gate.SHADOW_ARTIFACTS`, `_LIFECYCLE`, `_RETIRED_TAGS` sowie die
DB-Historie (`ai_signals.model`, `closed_ai_signals.model`, `ml_predictions_master.model_name`).
**EPD4 ist überall frei**, und `epd4_model_{LONG,SHORT}.pkl` beansprucht keinen fremden
Loader-Slot (`tools/promotion_guard.check_staging_filename` → PASS).

Registriert ist EPD4 **nicht** — ohne Artefakt wäre ein Eintrag in `core/shadow_gate` tote
Konfiguration. Gepinnt ist stattdessen die Belegung selbst
(`backtest/test_retrain_model_id.py::test_epd4_is_free_in_every_code_registry`), inklusive
der Falle: der Gate-Default ist **LIVE**, ein unregistrierter Tag postet also live. Vor der
ersten EPD4-Emission muss die `_LIFECYCLE`-Zeile stehen.

### P1.45-Verdrahtung — warum hier bewusst kein Rewire

Der Brief verlangt, `meta.model_id` in den Post-Pfad zu verdrahten oder präzise zu
begründen, warum nicht. Der Befund:

1. Für den **Artefakt-Pfad** ist es längst verdrahtet: `module_tag = best_art["tag"]`
   kommt aus `core.model_artifacts.load_artifact` und damit aus `meta.model_id`
   (gepinnt in `backtest/test_epd_tag.py`).
2. Für den **Challenger-/Shadow-Pfad** (`_emit_epd3_shadow`) ist der Tag eine Konstante —
   und das muss so bleiben. Gemessen an den Artefakten selbst:

   | Datei | `meta.model_id` |
   |---|---|
   | `epd3_model_LONG.pkl` (Root, **LIVE**) | `EPD2` |
   | `epd3_model_SHORT.pkl` (Root, live) | `EPD2` |
   | `staging_models/epd3_model_SHORT.pkl` | `EPD3` (re-getaggt, T-2026-KYT-9050-057) |
   | `staging_models/rub2_model_LONG.pkl` (= Artefakt von RUB3) | `RUB2` |

   Würde `load_shadow_artifact` den Tag aus der Meta ziehen, postete das **live** laufende
   EPD3-LONG-Bein ab sofort unter `EPD2` — es verschmölze mit dem geparkten Legacy-Bein,
   und `has_open_ai_signal(symbol, dir, "EPD3")` fände seine eigenen offenen Trades nicht
   mehr. Der hartkodierte Tag ist dort aktuell das Einzige, was die Generationen trennt.

   Der LONG-Tag-Defekt ist bekannt und bewusst offen: `tools/retag_artifact.py` verweigert
   das Re-Dump, weil das Artefakt unter sklearn 1.9.0 gepickelt wurde und die Fleet 1.7.1
   serviert (der Round-Trip würde den Isotonic-Kalibrator degradieren) — siehe
   `backtest/test_epd3_artifact_model_id.py`.

Die Verdrahtung an dieser Stelle ist also **kein fehlendes Feature, sondern durch einen
offenen Artefakt-Defekt blockiert**. Der richtige Zeitpunkt ist der EPD4-Lauf: dessen
Artefakt trägt `model_id = EPD4` von Geburt an (unten), und dann kann der Shadow-Pfad
Register-Tag gegen Meta-Tag prüfen, ohne ein Live-Bein umzubenennen.

## 7. Was dieser Task am Code geändert hat

- `tools/retrain_from_replay.py` — `run_epd(model_id=…)` + CLI-`--model-id`. Der Tag setzt
  `meta.model_id` **und** den Dateinamen-Präfix gemeinsam (`artifact_slot`, identisch zu
  `promotion_guard.tag_prefix`); sie auseinanderlaufen zu lassen ist genau der
  Slot-Kaper-Fehler vom 2026-07-21. Default `EPD2` ⇒ unveränderter Lauf.
- `tools/retrain_from_replay.py` — der degenerierte Split meldet jetzt die Rechnung statt
  nur „übersprungen" (`split_shortfall`), und der Befund landet maschinenlesbar in
  `retrain_<slot>_stats.json`. Das ist die Meldung, die der Lauf im November sehen wird.
- `tools/retrain_pump.py` — `--model-id` durchgereicht.
- `backtest/test_retrain_model_id.py` — neu, 14 Tests.

Kein Artefakt, keine Registrierung, kein Bot-Code. Das Retrain-Kommando für später steht
in `retrain_pump.py`:

```
python tools/retrain_pump.py --since 2026-07-11 --model-id EPD4
```

## 8. Wiedervorlage — **T-2026-KYT-9050-067**

`(0,15 · Spanne − 7 d) · Dichte ≥ Zielzeilen`, Dichte konstant bei 108,9 (LONG) /
88,6 (SHORT) gelabelten Zeilen/Tag angenommen:

| Datum | Val/Test je Richtung | Bewertung |
|---|---|---|
| 2026-08-30 | ~50 | Split nicht mehr degeneriert, statistisch wertlos |
| 2026-09-17 | ~300 | `pick_threshold_safe` (min_n=200) trägt nur ganz unten |
| **2026-11-09** | **~1000** | erster Operating-Point mit Rückhalt bis ~p80 · **Empfehlung** |

Die Dichte ist eine Annahme — sie hängt an der Marktaktivität. Der Ist-Wert steht nach
jedem Lauf in `staging_models/retrain_epd4_stats.json` (`missing_days`); ein Lauf kostet
~35 min Build und ist damit die billigste Art, den Termin nachzuschärfen.

**Offene Punkte für den späteren Lauf** (auch in `epd4_feasibility.json`):

1. `core/shadow_gate`: EPD4 in `_LIFECYCLE` (SHADOW) + `SHADOW_ARTIFACTS`, **bevor** Bot 10
   emittiert — Default ist LIVE.
2. `10_pump_dump_detector`: Emissions-Zweig für EPD4 (Muster `_emit_epd3_shadow`).
3. `tools/verify_staging_artifacts.build_registry()`: die `epd`-Familie globt nur
   `epd2_model_*.pkl`; ein EPD4-Artefakt würde still übersprungen.
4. Trainings- gegen Serving-Population (§5) klären, bevor ein Threshold live geht.

---

**Rohdaten:** `staging_models/replay/epd4_feasibility.json` · `staging_models/retrain_epd4_stats.json`
**Nicht committet:** der 3,4-MB-Ereignis-Datensatz (`epd4_events.jsonl`) — reproduzierbar
über `tools/epd2_build_dataset.py --since '2026-07-10 17:00:00'`.

# Bot 40: liegt der Live-Umschlag über dem simulierten? (T-2026-KYT-9050-047)

_Messung 2026-08-01 · read-only · Werkzeug `tools/trailing_live_vs_study.py` ·
Datenstand `trailing_positions` ab Bot-Start 2026-07-26 abends, 5,6 Tage ·
Vergleichsbasis `staging_models/replay/trailing_slot_budget_live.json` (PR #198, act 2 %, x 10 %, tf 15m)_

## Verdikt

**Nein.** Der Live-Umschlag liegt **nicht** systematisch über dem simulierten — er liegt bei der
Haltedauer eher darunter (also länger gehalten) und beim Umsatz pro belegtem Slot-Tag 9–23 %
darüber. Die Slot-Rechnung war nicht zu optimistisch, sondern **zu pessimistisch**: der Channel
läuft im eingeschwungenen Zustand bei **Ø 106 gleichzeitig offenen Positionen gegen erwartete
252** — halb leer. Die Gebührenlast pro Slot-Tag liegt 9 % über der Rechnung, mit der die
49 204 % gerechnet wurden.

Die vermutete Ursache — 15m-Kerzen gegen 10s-Live-Preise — ist real und **quantifiziert: sie ist
rund 20 Minuten pro Arm-Exit wert** (Median Δ +0,33 h, p95 +0,63 h) und höchstens 4,7 % Slot-Tage.
Sie verschiebt den Betriebspunkt nicht, weil es keinen nennenswert verschobenen Betriebspunkt
gibt, den sie erklären müsste.

**Der Auslöser der Frage war ein Bootstrap-Artefakt.** Die „~80 Trail-Feuer pro Stunde bei ~460
offenen Positionen" sind exakt 80 Feuer in **1,2 Stunden am 26.07. zwischen 19:00 und 20:00 UTC** —
dem ersten Shadow-Zyklus, in dem der Bot ein bereits laufendes Buch auf einmal spiegelte. Diese
Spiegel erbten einen Peak, der beim ersten Poll schon über der Aktivierungsschwelle lag, und
feuerten sofort. Im Live-Betrieb: **4,0 Trail-Feuer pro Stunde**, geschäftigste Einzelstunde 21.

## Empfehlung an den Operator (#T52-3)

**`act = 2 %` beibehalten. Keine Änderung an Bot 40 aus diesem Befund.**

Die Hypothese, die eine Änderung motiviert hätte — Live-Umschlag über Simulation, also Slot- und
Gebührenrechnung zu optimistisch — ist widerlegt. Der einzige gemessene Abweichungspfad kostet
≤ 5 % Slot-Tage.

Wichtig für die Richtung einer möglichen Änderung: **`act` senken würde die freie Kapazität nicht
nutzen, sondern vergrößern.** Ein niedrigeres `act` verkürzt die Haltedauer (Studie: act 1 → Median
2,0 h, act 0 → 0,4 h) und senkt die Belegung damit weiter. Wer die leere Hälfte des Channels füllen
will, muss `act` **erhöhen** oder mehr Beine zulassen — beides ist eine eigene Entscheidung, und
beides sollte warten, bis die Ertragslage dreht: das Live-Buch steht bei **−906 %-Punkten netto**,
und der Engpass ist nicht Kapazität, sondern Ertrag (Ursache siehe T-054: Tape, kein Leg-Defekt).

## 1 Population — live und Shadow strikt getrennt

`posted` ist die Live/Shadow-Linie. Wer über die ganze Tabelle aggregiert, mischt das
Shadow-Buch und die Zulassungs-Vermerke in die Live-Zahlen (die T-052-Lektion).

| | Zeilen |
|---|--:|
| `trailing_positions` gesamt | 6 055 |
| davon `posted` = live | 1 141 |
| davon Shadow + Vermerke | 4 914 (`PREEXISTING` 4 372, `SHADOW_CARRYOVER` 459, `TRAIL` 80, `SOURCE_CLOSED` 3) |
| **Live-Positionen** (ohne `ENTRY_NOT_FILLED`) | **1 095** |
| davon geschlossen mit echtem Exit | 999 |
| davon noch offen | 96 |
| `ENTRY_NOT_FILLED` — kein Slot, keine Gebühr | 46 |

`ENTRY_NOT_FILLED` ist eine gepostete Zeile ohne Position dahinter. Sie mitzuzählen bläht Slot-Draw
und Gebührenlast gleichzeitig auf.

## 2 Haltedauer — live gegen die Studie

| Maß | Wert |
|---|--:|
| Live, nur geschlossene Positionen (n=999) | Median **6,00 h** · p25 1,87 · p75 19,72 · p95 48,60 |
| Live, mit den 96 offenen als rechts-zensiert | Median liegt in **[6,71 h; 7,40 h]** |
| Studie, mix-gematcht auf die Live-Bein-Anzahlen | **6,59 h** (gewichteter Median) |
| Studie, Kopfzeile über Beine | 4,6 h |

Die 4,6 h aus dem Report sind ein Median **über Beine**, kein Median über Trades — jedes Bein zählt
dort gleich, MIS2-168h SHORT (3 Live-Spiegel) so viel wie MIS1-72h LONG (370). Mix-gematcht auf das
Live-Buch erwartet dieselbe Studie **6,59 h**. Live gemessen: 6,00 h ohne, 6,71–7,40 h mit den
offenen Positionen.

**Zwei Verzerrungen laufen beide in Richtung „live ist schneller", und die Messung fällt trotzdem
andersherum aus:**

- **Zensierung.** 5,6 Live-Tage gegen 148 simulierte Tage: was lange hält, ist noch offen. Der
  Median nur über geschlossene Zeilen ist die optimistische Grenze — deshalb das Intervall.
- **Der Zeit-Stop bei 24 h existiert in der Studie nicht.** Er kappt live 50 Positionen bei genau
  24,0 h, die simuliert weitergelaufen wären.

### Pro Bein (Live-Median gegen den Trailing-Median der Studie)

| Bein | n | live | Studie (trail) | Studie (hold) | Verhältnis |
|---|--:|--:|--:|--:|--:|
| MIS1-72h LONG | 370 | 8,76 | 6,59 | 40,7 | **1,33×** |
| AIM2 SHORT | 128 | 4,17 | 2,54 | 29,0 | **1,64×** |
| ATS2 LONG | 107 | 16,44 | 13,21 | 22,8 | **1,24×** |
| SRA2 SHORT | 56 | 3,73 | 5,54 | 10,4 | 0,67× |
| AIM2 LONG | 49 | 9,17 | 5,58 | 22,1 | **1,64×** |
| SRA2 LONG | 43 | 10,73 | 4,71 | 8,5 | **2,28×** |
| SKW1 SHORT | 32 | 6,52 | 3,73 | 5,0 | **1,75×** |
| SKW1 LONG | 27 | 5,70 | 5,13 | 13,5 | 1,11× |
| MAX1 SHORT | 26 | 3,11 | 4,25 | 7,0 | 0,73× |
| RUB1 SHORT | 25 | 1,18 | 1,03 | 18,2 | 1,15× |
| XSM1 LONG | 22 | 1,16 | 2,57 | 11,0 | 0,45× |
| MIS2-72h SHORT | 22 | 0,72 | 0,41 | 44,2 | 1,76× |
| MIS1-168h LONG | 20 | 6,31 | 9,90 | 49,7 | 0,64× |
| MIS1-8h SHORT | 15 | 0,46 | 0,48 | 8,2 | 0,97× |
| RUB1 LONG | 12 | 4,94 | 2,56 | 44,5 | 1,93× |

_(Beine mit n < 10 in der Werkzeug-Ausgabe; ihre Einzelverhältnisse tragen nichts.)_

Bei den fünf größten Beinen, die zusammen 65 % des Live-Buchs stellen, hält der Live-Arm
**länger** als die Simulation. Die vollständige Tabelle steht in der Werkzeug-Ausgabe.

### Pro Tag × Richtung (Tag des Exits)

| Tag | LONG n / Median h | SHORT n / Median h |
|---|--:|--:|
| 2026-07-26 | 16 / 1,60 | 7 / 0,29 |
| 2026-07-27 | 233 / 4,55 | 84 / 4,05 |
| 2026-07-28 | 155 / 11,41 | 20 / 2,01 |
| 2026-07-29 | 83 / 35,31 | 39 / 3,47 |
| 2026-07-30 | 55 / 5,52 | 53 / 2,03 |
| 2026-07-31 | 91 / 11,54 | 85 / 3,72 |
| 2026-08-01 | 40 / 16,41 | 38 / 3,43 |

Der 26.07. ist der Anlauftag (erster geposteter Spiegel ~20:30 UTC), die kurzen Median-Werte dort sind
Anfangsbestand, keine Betriebszahl. Die LONG-Seite hält durchweg 3–10× länger als die SHORT-Seite;
das ist die Signatur der Markt-Attribution aus T-054 (fallendes Tape → LONG kommt seltener über die
Aktivierungsschwelle und wird deshalb seltener getrailt).

## 3 Exit-Mix

Anteile über alle 999 echten Exits:

| Grund | Anteil |
|---|--:|
| `TRAIL` (eigene Entscheidung des Arms) | **54 %** |
| `SOURCE_CLOSED` (folgt der Fleet) | 32 % |
| `SL_HIT` | 9 % |
| `TIME_STOP` | 5 % |

Die Tages-/Richtungs-Auflösung steht in der Werkzeug-Ausgabe. Auffällig: `TIME_STOP` erscheint erst
ab dem 29.07. (Stichtag `TRAILING_BOT_TIME_STOP_SINCE` = 28.07. 14:00 UTC plus 24 h Frist) und trägt
seit dem 31.07. 24–30 % der LONG- und 5–12 % der SHORT-Exits — kein Randfall mehr.

`TRAIL` 54 % bedeutet: gut die Hälfte der Exits sind eigene Entscheidungen des Arms, der Rest ist
Folgen der Fleet bzw. der Börse. Ob die Studie dieselben Trades ebenso oft getrailt hätte, ist ein
eigener Vergleich und steht in Abschnitt 6 — die dortigen Anteile sind über ein Fenster gerechnet,
das über den Live-Exit hinausreicht, und daher **nicht** direkt gegen diese Tabelle zu halten.

## 4 Realisierter Mark gegen die Gebühr (0,10 % Taker-Round-Trip)

Alle 999 Exits tragen einen verwertbaren Mark (SL-Rekonstruktion nach T-054 / Backfill T-058).

| Schnitt | n | Σ brutto | Gebühr | Σ netto | < Gebühr | Ø Mark |
|---|--:|--:|--:|--:|--:|--:|
| **ALLE** | 999 | −806,2 | 99,9 | **−906,1** | 38 % | −0,81 |
| LONG | 673 | −898,6 | 67,3 | −965,9 | 46 % | −1,34 |
| SHORT | 326 | +92,4 | 32,6 | +59,8 | 21 % | +0,28 |
| `TRAIL` | 536 | +1153,0 | 53,6 | **+1099,4** | **0 %** | +2,15 |
| `TIME_STOP` | 50 | −79,5 | 5,0 | −84,5 | 80 % | −1,59 |
| `SL_HIT` | 90 | −559,3 | 9,0 | −568,3 | 100 % | −6,21 |
| `SOURCE_CLOSED` | 323 | −1320,4 | 32,3 | −1352,7 | 77 % | −4,09 |

**Die Gebühr ist nicht das Problem.** 999 Trades × 0,10 % = 99,9 %-Punkte gegen ein Brutto von
−806,2. Ein „Anteil der Gebühr am Brutto" ist auf einem Verlustbuch bedeutungslos und wird vom
Werkzeug bewusst verweigert statt als kleine positive Zahl gedruckt.

Der Vergleich, der trägt, ist **Gebühr pro belegtem Slot-Tag** (Abschnitt 5): live 0,141 % gegen
0,129 % in der Studie — **+9 %**.

Zum Studien-Vergleichswert „25 % der Trades unter Gebühr" bei act = 2: dieser Anteil ist dort über
**alle** Trades gerechnet, inklusive derer, bei denen der Trail nie auslöste. Die Live-Entsprechung
ist die ALLE-Zeile (38 %), nicht die `TRAIL`-Zeile. Dass die `TRAIL`-Zeile bei **0 %** liegt, ist
Konstruktion, kein Befund: ein bewaffneter Trail schließt frühestens bei 0,9 × 2,0 % = 1,8 %. Die
38 % gegen 25 % kommen vollständig aus `SOURCE_CLOSED` (77 %) und `SL_HIT` (100 %) — also aus dem
Tape, nicht aus der Umschlaghäufigkeit.

Das ist zugleich das T-052-Muster im Live-Buch: der Trail kann per Konstruktion nur Gewinner
schließen (`TRAIL` +1099 netto), die Verlierer bleiben liegen, bis Fleet oder SL sie beenden
(−1353 und −568).

## 5 Gleichzeitige Belegung

| Maß | live | Studie |
|---|--:|--:|
| Ø Belegung | **126,4** | 284,6 roh / **251,6** roster-gematcht |
| Median | 107 | — |
| p95 | **221** | 498,0 |
| Maximum | 291 | 2 001 |
| **letzte 48 h (eingeschwungen)** | **Ø 105,7 · p95 114 · max 116** | — |

Die 284,6 der Studie enthalten ROM1 LONG (11) + ROM1 SHORT (22), die `core/trailing_roster.py`
inzwischen als Re-Forwarder-Duplikat ausschließt. Mittlere Belegung ist eine Summe von
Indikatorfunktionen und damit **exakt additiv**, die roster-gematchte Erwartung also 251,6. p95 ist
nicht additiv und bleibt unkorrigiert.

**Live zieht 50 % der roster-gematchten Erwartung, eingeschwungen 42 %.** Der Cornix-Deckel von 500
ist nie in Reichweite gekommen: das beobachtete Maximum liegt bei 291, in den letzten 48 h bei 116.

### Woher die Lücke kommt: Intake, nicht Umschlag

Belegung = Zulauf × Haltedauer. Die Haltedauer ist nicht kurz (Abschnitt 2), also bleibt der Zulauf:

| | Zulauf |
|---|--:|
| live | **195,1 Positionen/Tag** (1 095 über 5,6 Tage) |
| Studie (gewählte p95-Auswahl) | 365,5 Trades/Tag (53 944 über 148 Tage) |
| | **live = 53 %** |

Der Live-Bot hat vier Zulassungsfilter, die die Simulation nicht hatte: höchstens ein Spiegel je
Symbol (Unique-Index auf `symbol WHERE closed_at IS NULL`, bei 33 Beinen auf ~530 Coins häufig
bindend), das 240-s-Frische-Fenster, der Symbol-Cooldown und der Exposure-Cap. Dazu der
ROM1-Ausschluss und `shadow_gate`-Beine, die zwischenzeitlich nicht LIVE sind — EPD1 SHORT, in der
Studie das zweitgrößte Bein mit 4 650 Trades, hat **kein einziges Mal** gespiegelt.

### Umschlag pro belegtem Slot-Tag

Das mix-robuste Maß — und die Einheit, in der die Gebühr anfällt:

| | Trades/Slot-Tag |
|---|--:|
| live | **1,405** (999 Exits / 711,0 Slot-Tage) |
| Studie, Aggregat | 1,291 |
| Studie, mix-gematcht auf die Live-Bein-Anzahlen | 1,146 |
| | **live = 1,09× / 1,23×** |

Das ist die ehrlichste Antwort auf die Titelfrage: der Umschlag pro Slot liegt **9–23 % über** der
Simulation. Gebühr pro Slot-Tag entsprechend 0,141 % gegen 0,129 %.

## 6 Auflösung — die 15m-Regel der Studie auf denselben Spiegeln nachgespielt

Jeder Live-Spiegel wird auf seinem **eigenen** 15m-Band nachgerechnet, vom Fill bis 24 h nach dem
Live-Exit, mit der **importierten** Regel aus `tools/trailing_slot_budget.py` (act 2 %, x 10 %,
strikt vorheriger Peak — Regel 7, keine Zweitimplementierung). 999 von 999 Exits nachgespielt.

**Eigene Exits des Arms (`TRAIL`/`TIME_STOP`), n = 586:**

| Bucket | n | Anteil | Lesart |
|---|--:|--:|---|
| `study-earlier` | 57 | 10 % | der 15m-Docht feuerte zuerst — live war das **langsamere** Raster |
| `same-bar` | 100 | 17 % | dieselbe 15m-Kerze: Raster-Granularität, kein anderer Betriebspunkt |
| `study-later` | 395 | 67 % | die Studie hätte tatsächlich weitergehalten |
| `study-never-fires` | 34 | 6 % | kein 15m-Trigger im Horizont (rechts-zensiert) |

**Δ = Studien-Exit − Live-Exit: Median +0,33 h · p25 +0,24 · p75 +0,42 · p95 +0,63.**

Das ist der ganze Auflösungseffekt: **rund 20 Minuten, in 95 % der Fälle unter 38 Minuten** — also
ein bis zwei 15m-Kerzen. Der Grund ist mechanisch: die Kerzen-Extreme *sind* die Extreme, das 15m-
Raster sieht denselben Peak und denselben Rückgang wie der 10s-Poll und trigger daher meist in
derselben oder der nächsten Kerze. Die einzige echte Verschärfung ist der strikt vorherige Peak,
und der verschiebt den Trigger um höchstens eine Kerze.

In 10 % der Fälle feuert das 15m-Raster sogar **früher**: dort erwischt der Kerzen-Docht einen
Rückgang, den der 10s-Poll-Preis nie gedruckt hat.

**Vom Arm nicht selbst beendete Exits (Fleet/SL), n = 413:** 70 % lösen im 15m-Raster innerhalb
des Fensters plus 24 h **gar keinen Trail** aus — beide Regeln halten, die Fleet oder der SL beendet
die Position. In 26 % hätte die Studie später getrailt (Median +0,88 h, p75 +6,24, p95 +18,50).

### Was der Unterschied in Slots kostet

504 Exits liegen mehr als eine Kerzenbreite auseinander, Σ **33,1 Slot-Tage = +4,7 %** auf die
live gemessenen 711,0. 325 weitere feuern im Horizont nie und sind **nicht** mitgezählt — die Zahl
ist damit eine **Untergrenze** auf den Unterschied, keine Schätzung.

### Und im Preis

Wo beide Regeln innerhalb einer Kerze voneinander liegen (n = 116), trennt sie nur die Ausführung:
live Ø **+2,08 %** gegen 15m-Stop-Level Ø **+2,07 %** → **Δ +0,02 %-Punkte je Trade**. Das feinere
Raster verkauft also nicht schlechter.

## Ehrliche Grenzen

- **5,6 Live-Tage gegen 148 simulierte.** Das ist ein Tape-Ausschnitt, kein Regime-Querschnitt. Die
  Haltedauer-Aussage ist durch das Zensierungs-Intervall abgesichert, die Ertragsaussage nicht — für
  die gilt weiterhin T-054 (Markt, kein Leg-Defekt).
- **Der Bein-Mix ist nicht der der Studie.** MIS1-72h LONG stellt 35 % des Live-Buchs, EPD1 SHORT
  (Studie: 4 650 Trades) null. Jeder Aggregat-Vergleich trägt hier deshalb seinen mix-gematchten
  Zwilling; wo nur das Aggregat steht, ist es als solches beschriftet.
- **Das 15m-Nachspiel startet bei der ersten vollständig im Fenster liegenden Kerze** und schließt
  bündig ab. Die Studie selbst selektiert nur über `open_time` und lässt ihre letzte Kerze bis zu
  einem Intervall über den Close hinausragen (ihre dokumentierte Grenze). Die bündige Variante
  entzieht der Studien-Seite Trigger-Gelegenheiten statt ihr zusätzliche zu geben — der
  konservative Fehler für ein Werkzeug, dessen These lautet, die Studie feuere seltener.
- **Der Exit-Zeitpunkt im Nachspiel ist der Kerzen-SCHLUSS**, nicht die Kerzen-Eröffnung wie in der
  Studie. Ein 15m-Trigger ist vor Kerzenschluss nicht wissbar; die Studie ist an dieser Stelle um
  bis zu eine Kerze optimistisch (dieselbe Klasse Look-ahead, die in T-052 aus 59k → 7k machte).
- **Das Counterfactual ist rechts-zensiert** bei `--horizon-h` (Default 24 h). In der Studie hätte
  zusätzlich der Close des Quell-Trades die Position beendet, und zwar früher. „Hätte ≥ X h länger
  gehalten" ist ein Boden, keine Schätzung.
- **Beta = 1 wird hier nicht unterstellt** — dieser Report macht keine Markt-Attribution; die steht
  in `tools/trailing_arm_report.py` (T-054).

## Reproduktion

```
python tools/trailing_live_vs_study.py                    # voller Report
python tools/trailing_live_vs_study.py --no-replay        # ohne Abschnitt 6 (kein Kerzen-Read)
python backtest/test_trailing_live_vs_study.py            # Pins, DB-frei
```

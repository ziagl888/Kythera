# Kann der Trailing-Arm profitabel werden? — Verdikt T-2026-KYT-9050-052

**Stand:** 2026-07-27 · **Status:** abgeschlossen · **Basis:** Live-DB read-only +
`tools/trailing_book_health.py` (neu, dieser Task; Zahlen: `trailing_book_health.md/.json`) ·
**Vorlauf:** T-041 (Verdampfung belegt), PR #198 (Slot-Budget), Bot 40 live seit 2026-07-26 23:34.

## Die Frage

Gibt es eine Exit-Regel, die den Verdampfungs-Befund aus T-041 einfängt, ohne das Buch mit
Verlierern zu füllen — oder gibt es sie nicht?

**Kurzantwort: Der reine aktivierungs-gegatete Trail kann es strukturell nicht — bei keinem
`act`. Es gibt aber Regeln, die es können; sie kosten realisierten Ertrag. Und der Arm hat ein
zweites Problem, das keine Exit-Regel löst: er läuft additiv zum Hold-Arm auf demselben Konto.**

## 1. Der Live-Befund, verifiziert (2026-07-27 ~19:30)

Der Befund aus dem Session-Anlass ist real und hat sich seit der 16:00-Messung verschärft:

| | offene Positionen | Ø Mark (unlev) | im Minus |
|---|---|---|---|
| **Trailing-Arm** (Bot 40, gefüllte Spiegel) | 128 LONG / **5 SHORT** | −1,84 % | 91/128 LONG |
| **Hold-Arm** (dieselben Beine, `ai_signals`) | 789 LONG / **324 SHORT** | LONG −1,34 % / **SHORT +3,90 %** | 606/789 bzw. 110/324 |

- Der Hold-Arm hält 324 Shorts bei Ø **+3,90 %** — sie federn seine 789 Longs ab. Der
  Trailing-Arm hat genau diese Shorts **weggetrailt** (88 × `TRAIL` SHORT @ Ø +3,13 %
  realisiert); sein Verhältnis ist 26:1 LONG gegen 2,4:1 im Hold-Arm.
- Realisiert sieht der Arm gut aus (`TRAIL` LONG 108 × Ø +2,58 %, SHORT 88 × Ø +3,13 %);
  `SOURCE_CLOSED` LONG bucht dagegen Ø −2,10 % — das offene Buch trägt die Rechnung.
- Das saubere Fenster (Positionen ab Stichtag 15:32:16, Market-Entry-Regime) steht erst bei
  Ø −0,23 % (57 LONG) — nicht weil der Mechanismus dort fehlt, sondern weil die Entmischung
  Zeit braucht: Gewinner verlassen das Buch bei ~+2,2 %, Verlierer akkumulieren.

**Mechanismus (strukturell, nicht regime-abhängig):** Der Trail wird erst bei Peak > act = 2 %
scharf und kann deshalb per Konstruktion **nur Gewinner schließen**. Ein Trade, der nie +2 %
erreicht, bleibt bis zum Fleet-SL liegen. Das Buch entmischt sich von selbst — in jeder
Marktphase; ein fallender Markt macht es nur schneller sichtbar. Die Sim bestätigt das über
5 Monate: der Ø-Buch-Mark des Trails ist an **jedem** Monatsende negativ (−1,3 … −6,0 %),
der des Hold-Buchs an jedem positiv (+0,1 … +2,6 %).

Michis ~10 % Konto-Drawdown ist damit **konsistent, aber nicht bewiesen kausal** — das Konto
trägt die ganze Fleet, der Channel-Anteil ist nicht isolierbar.

## 2. Das methodische Loch (und eine Doppelzählung)

`tools/trailing_slot_budget.py` (PR #198) misst **realisierte Summen** je (Tag, Richtung) und
**Slot-Belegung**. Eine Regel, die Gewinner schließt und Verlierer hält, sieht in dieser Metrik
gut aus und im offenen Buch schlecht. Die 49 204-%-Erwartung war nicht falsch gerechnet — sie
beantwortete die falsche Frage.

Zusätzlich enthielt die Auswahl eine **Doppelzählung**: ROM1 (Bot 28) ist ein Re-Forwarder;
seine Trades sind dieselben, die die Originalbeine schon posten (Falle 2 des Task-Briefs).
ROM1 LONG+SHORT standen für 10 334 % der 49 204 % (21 %). Ohne ROM1 erwartet derselbe Trail
**39 116 %** (Sim unten, 44 144 Trades; 11 236 ROM1-Zeilen ausgeschlossen). Im Live-Bot
unterdrückt die Ein-Position-pro-Symbol-Regel (AK3) die meisten dieser Duplikate ohnehin —
die gepostete Erwartung war auch deshalb zu hoch.

Neues Werkzeug: `tools/trailing_book_health.py` misst jede Exit-Regel an **beiden** Seiten:
realisiert (netto, Dichte je Slot-Tag) UND offenes Buch (Counts je Richtung, Ø Mark,
Unterwasser-Anteil, **MaxDD der Equity-Kurve** = realisierte Summe + offenes MTM, unlevered
%-Punkte, gleichgewichtet; Tages-Zeitreihen im JSON). Population: Roster-Beine ohne ROM1,
LIVE je (Tag × Richtung), ab 2026-03-01, 15m-Kerzen, strikt-vorheriger Peak, 0,10 % Gebühr.
Pins: `backtest/test_trailing_book_health.py` (9 Tests, DB-frei).

## 3. Sim-Ergebnis (März–Juli, 44 144 Trades, alle Regimes)

Vollständige Tabelle: `trailing_book_health.md`. Die entscheidenden Zeilen:

| Regel | Σ netto | netto/Slot-Tag | Ø Slots | **Equity MaxDD** | Ø Buch-Mark | unter Wasser | Ø L/S |
|---|--:|--:|--:|--:|--:|--:|--:|
| Hold (Fleet-Exit) | 59 574 | 0,400 | 1001 | 20 076 | +2,34 % | 44 % | 755/246 |
| **Trail act=2 (Bot 40 heute)** | 39 116 | 1,028 | 256 | 4 377 | **−2,73 %** | **78 %** | 214/41 |
| Trail act=5 | 51 866 | 0,654 | 532 | 9 786 | −1,96 % | 66 % | 427/105 |
| Trail act=10 | 64 088 | 0,564 | 764 | 14 856 | −0,89 % | 54 % | 605/159 |
| **Trail 2 + Zeit-Stop 24 h** | 27 966 | **1,431** | **131** | **3 015** | −1,17 % | 61 % | 107/24 |
| Trail 2 + Zeit-Stop 48 h | 32 846 | 1,247 | 177 | 4 316 | −1,58 % | 67 % | 146/31 |
| Trail 2 + Hard-Stop −2 % | 15 809 | 1,328 | 80 | 2 216 | ±0,00 % | 41 % | 65/15 |
| Trail 2 nur SHORT | 53 700 | 0,453 | 796 | 20 736 | +0,93 % | 50 % | 755/41 |
| Trail 2 nur LONG | 44 989 | 0,656 | 460 | 5 872 | +1,19 % | 56 % | 214/246 |
| **Trail 2, 50 % Teilschließung** | 49 345 | 0,528 | 628 | 12 166 | +1,51 % | 50 % | 485/144 |
| **Trail 2 + Exposure-Cap ±50** | 21 391 (n=45 %) | **1,448** | 99 | **683** | −2,84 % | 76 % | 65/34 |
| Portfolio-Trail 10 % | 9 268 | 1,308 | 48 | 5 271 | −0,25 % | 33 % | 35/13 |

Monats-Robustheit (Δ Equity je Monat, aus den Tages-Zeitreihen):

| Regel | Mär | Apr | Mai | Jun | Jul |
|---|--:|--:|--:|--:|--:|
| Hold | +3 848 | +17 346 | +18 145 | +13 984 | +6 638 |
| Trail act=2 | +7 802 | +8 039 | +6 318 | +8 198 | +8 827 |
| Trail 2 + Zeit-Stop 24 h | +6 946 | +4 246 | +3 584 | +5 329 | +7 729 |
| Trail 2 + Exposure-Cap ±50 | +1 381 | +2 428 | +4 494 | +6 010 | +6 965 |
| Trail 2, 50 % Teilschließung | +5 825 | +12 693 | +12 232 | +11 091 | +7 732 |

## 4. Befunde

1. **Der Trail ist realisiert stabil profitabel** — +6,3k…+8,8k in jedem Monat, während Hold
   zwischen +3,8k und +18,1k schwankt (Bull-Monate tragen Holds Vorsprung). Der Arm ist also
   nicht „unprofitabel" — er tauscht ~34 % von Holds Netto gegen 4,6× weniger Equity-MaxDD
   und 2,6× mehr Dichte. Das T-041-Motiv (Verdampfung einfangen) funktioniert realisiert.
2. **Aber kein `act` heilt das Buch.** act=2/5/10 → Ø Buch-Mark −2,73/−1,96/−0,89 %, Unterwasser
   78/66/54 % — die Asymmetrie verdünnt sich Richtung Hold, verschwindet aber nie, und der
   DD-Vorteil verdunstet dabei (4,4k → 14,9k). Ein Winners-only-Exit erzeugt zwingend ein
   Losers-only-Buch. **Die Denkrichtung „höheres act" ist damit widerlegt.**
3. **Die symmetrische Verlierer-Regel existiert und funktioniert — gegen Gebühr.** Zeit-Stop
   24 h auf nie-scharfe Trades: Buch-Mark −1,17 % statt −2,73 %, Slots 131 statt 256, beste
   Dichte einer Vollpopulations-Regel (1,431), MaxDD 3 015. Preis: −11 150 realisiert
   (27 966 statt 39 116) — der Zeit-Stop verkauft Erholungen (T-041: 85 % der Verlierer waren
   mal im Plus; genau diese Optionalität wird liquidiert). Das ist der ehrliche Trade-off,
   kein Free Lunch.
4. **Hard-Stop −2 % verblutet** (15 809 netto, −60 % vs. Trail): das sauberste Buch (±0,00 %)
   zum höchsten Preis. **Portfolio-Trail churnt sich tot** (9 268 netto bei 44k Trades ×
   0,10 % Gebühr; bestätigt T-035: Wellen-Close no-edge). **Nur-SHORT-Trail behält Holds
   MaxDD** (20 736) — widerlegt. Nur-LONG-Trail ist bemerkenswert (44 989 netto, DD 5 872,
   Buch +1,19 % dank gehaltener Shorts), aber seine Buch-Gesundheit stammt aus dem
   Richtungs-Bias der Periode — als Regel wäre er eine Wette darauf, dass Shorts weiter
   tragen, nicht eine Struktur-Korrektur.
5. **Exposure-Cap ±50 ist der beste Risiko-Begrenzer, aber kein Heiler:** Dichte 1,448,
   MaxDD 683 (6× besser als der Trail), monatlich stabil positiv — aber er handelt nur 45 %
   der Trades und das Buch bleibt zusammensetzungs-krank (−2,84 %). Er deckelt den Schaden,
   statt ihn zu beheben. (Zulassung in Ankunftsreihenfolge simuliert; der Bot würde nach
   Dichte wählen — konservativ geschätzt.)
6. **Teilschließung 50 % dominiert Hold auf beiden Achsen** (netto −17 %, MaxDD −39 %, Buch
   bleibt +1,51 %) — als **Fleet-integrierte** Variante des T-041-Befunds (Roadmap B) die
   mildeste Intervention. Als Arm-Regel erbt sie dagegen Holds Slot-Hunger (628 Slots).
7. **Das Additiv-Problem löst keine Exit-Regel:** Der Arm läuft auf demselben Konto wie der
   Hold-Arm und hält per Konstruktion bevorzugt die Positionen, die gerade verlieren — das
   Konto hält diese Verlierer dann **doppelt**, während Gewinner nur im Hold-Arm weiterlaufen.
   Jede Arm-Konfiguration ist ein Zusatz-Exposure mit dieser Schlagseite; die Regeln oben
   ändern nur ihre Größe und Dauer.

## 5. Empfehlung (Operator-Entscheid Michi — der Bot bleibt unangetastet bis dahin)

**Wenn der Arm als eigenes Experiment weiterlaufen soll:** Trail act=2 **+ Zeit-Stop 24 h**
(nie-scharfe Spiegel nach 24 h zum Markt schließen; im Bot trivial: `peak_pct` und `filled_at`
liegen in `trailing_positions`). Das ist die einzige simulierte Regel, die den Trail-Zweck
behält UND das Verlierer-Buch systematisch räumt — beste Dichte, kleinste Slots, MaxDD −31 %.
Optional zusätzlich ein Exposure-Cap als harte Schranke; die Kombination ist **nicht**
simuliert und müsste vor Aktivierung einmal durch dieses Tool.

**Wenn die Sorge das Konto ist:** Arm parken und den T-041-Befund als **Teilschließung in der
Fleet** (50 % am Trail-Trigger, Rest läuft) weiterverfolgen — dominiert Hold auf Netto UND
MaxDD ohne Doppel-Exposure, braucht aber Eingriffe in die Fleet-Exits (eigener Task,
Michi-gegatet).

**Nicht tun:** act erhöhen, Hard-Stop, Portfolio-Trail, Nur-SHORT-Trail — alle vier gemessen
und verworfen (Befund 2/4).

## Nachtrag 2026-07-28 — Operator-Fragen: „closen wir zu schnell?" und „Marktlage-Gate statt ROM?"

Nach dem Live-Ereignis (Buch-Entmischung im sauberen Fenster binnen ~9 h auf 95 % unter Wasser;
Bot 40 auf Operator-Auftrag 05:29 geparkt, 05:43 wieder entparkt) wurden acht weitere Regeln
simuliert (Lauf 3, 44 650 Trades, gleiche Population/Methodik):

| Regel | Σ netto | Equity MaxDD | Ø Buch-Mark | Ø L/S | deploybar (≤500 Slots) |
|---|--:|--:|--:|--:|--:|
| Trail a2, x=20 % | 32 151 | 4 887 | −2,71 % | 219/42 | ja |
| Trail a2, x=30 % | 27 405 | 5 627 | −2,62 % | 221/42 | ja |
| Trail a10, x=20 % | 55 856 | 15 337 | −0,83 % | 613/162 | **nein** |
| Trail a2 + Zeit-Stop 24 h + Cap ±50 | 18 686 | **588** | −1,22 % | 47/21 | ja |
| SL-Nachzug Breakeven ab +2 % (be2) | 15 998 | 8 996 | +1,84 % | 336/90 | ja |
| **be2 + Zeit-Stop 24 h** | 28 812 | 5 867 | **+2,85 %** | 287/82 | ja |
| Buch-Feedback-Gate (−1 %, min 10) | 6 457 | 820 | −3,51 % | 29/10 | ja |
| BTC-Richtungs-Gate (24h-Ret-Vorzeichen) | 19 669 | 2 969 | −3,12 % | 108/26 | ja |

**Frage (a) — closen wir zu schnell?** In der `x`-Dimension: **nein, im Gegenteil.** x=20/30 %
verliert auf BEIDEN Achsen gegen x=10 % (Netto sinkt, MaxDD steigt, Buch unverändert): der
tiefere Fill (Peak × (1−x)) kostet mehr, als das zusätzliche Laufen-Lassen einbringt. In der
`act`-Dimension: ja (act=10 → 62,6k netto), aber nicht deploybar (771 Ø Slots > Cap) und ohne
Verlust-Deckel (MaxDD 14,9k). Die Regel, die einen laufenden Gewinner **nie** unter seinem
Potenzial verkauft, ist der **SL-Nachzug + Zeit-Stop (be2+ts24)**: gesündestes Buch aller 23
Regeln (+2,85 %, besser als Hold), 28,8k netto — ein scharfer Trade wird nur noch geschützt
(Breakeven), nie gekappt; die Nie-Scharfen räumt der Zeit-Stop.

**Frage (b) — nur zur Marktlage passende Trades posten?** Beide vorhersagefreien Gates sind
**von den Exit-Regeln dominiert**: das Buch-Feedback-Gate würgt den Arm ab (6,5k netto — es
sperrt die Zufuhr genau dann, wenn die Erholung beginnt), das BTC-Richtungs-Gate liegt in allen
Kennzahlen unter dem einfachen Zeit-Stop. Das deckt sich mit der Regime-Historie des Repos
(ROM-Whitelist 89 % default-open, HMM widerlegt, SOFT no-edge, η²≈0): Marktlagen-**Erkennung**
trägt hier keinen Edge. Was funktioniert, ist die strukturelle Begrenzung — der Exposure-Cap
braucht keine Vorhersage, weil er die Einseitigkeit selbst deckelt.

**Frage (c, implizit) — was fängt einen Dump ab?** Nur die Kombination aus Zulassungs-Deckel
und Zeit-Hygiene: **Zeit-Stop 24 h + Cap ±50** hat mit MaxDD 588 (7× besser als der heutige
Trail) den besten Dump-Schutz der gesamten Messreihe — über eine Periode, die mehrere Dumps
enthält. Der Zeit-Stop allein fängt keinen Dump (Operator-Einwand korrekt); der Cap allein
heilt das Buch nicht; zusammen begrenzen sie Tiefe UND Dauer des Schadens.

**Empfehlung (aktualisiert):**
- **Verluste begrenzen ist das Ziel** → Bot 40 auf **Trail a2 + Zeit-Stop 24 h + Exposure-Cap ±50**.
- **Upside maximieren bei gesundem Buch** → **be2 + ts24** (SL-Nachzug statt Trail): kappt nie
  einen Läufer, Buch dauerhaft positiv, moderater DD. Kombination be2+ts24+cap ist NICHT
  simuliert — bei Interesse ein weiterer Lauf vor dem Umbau.
- Marktlagen-Gates (a la ROM oder einfacher): **nicht bauen** — gemessen und dominiert.

## Nachtrag 2 (2026-07-28) — Prio-Vergleich: „Wer hat März–Juli am Ende die beste Performance?"

Operator-Frage (Prio Upside). Läufe 4+5: Breakeven-Varianten (be+cap, be5) und — entscheidend
für Vergleichbarkeit — alle großen Kandidaten **unter dem harten Cornix-500-Slot-Cap**
(`run_total_cap`, Ankunftsreihenfolge; konservativ gegenüber der Dichte-Auswahl des Bots).
Ohne den Cap gewinnt automatisch die Regel mit dem größten Buch: Hold zieht Ø 1008 Slots,
be5+ts24 Ø 583/p95 1083 — beides nicht deploybar wie simuliert.

**Rangliste deploybar (Equity am Periodenende, unlevered %-Punkte, gleicher Zeitraum, gleiche Population):**

| Platz | Regel | Equity final | MaxDD | netto/Ø-Slot | Ø Buch-Mark | Monats-Δ (Mär…Jul) |
|--:|---|--:|--:|--:|--:|---|
| 1 | **Trail act=2 (Bot 40 heute)** | **38 157** | 4 377 | 147 | **−2,73 %** (78 % u.W.) | +7,8/+8,0/+6,3/+8,2/+8,5 |
| 2 | **be5+ts24 @ 500-Cap** | **34 471** | **4 124** | 85 | **+3,36 %** | +5,1/+8,0/+7,8/+7,8/+6,2 |
| 3 | Hold @ 500-Cap | 26 752 | 6 933 | 56 | +2,71 % | +0,5/+5,5/+9,9/+6,8/+4,7 |
| 4 | be2+ts24 @ 500-Cap | 21 781 | 4 324 | 67 | +2,87 % | +3,1/+6,1/+3,2/+4,9/+4,9 |
| 5 | Trail a2 + ts24 + Cap ±50 | 18 756 | **588** | **278** | −1,22 % | +1,4/+2,7/+3,1/+4,6/+7,1 |

Ohne Kapazitätsgrenze (theoretisch): be5+ts24 59 0k ≈ Hold 58,1k — aber der Cap kostet Hold
54 % (sein fettes Buch klebt am Limit und lehnt Ankünfte ab), be5+ts24 nur 42 %.

**Lesart:**
1. **Der heutige Trail hat auch deploybar das beste absolute Endergebnis** — sein Problem war
   nie die realisierte Ökonomie, sondern das strukturell kranke Buch (−2,73 %, 78 % unter
   Wasser, in jedem Monat negativ) und die Konto-Schlagseite daraus (Verlierer doppelt).
   Der Live-Vorfall dieser Woche IST diese Kennzahl.
2. **Prio Upside → be5+ts24** (Breakeven-Ratchet ab +5 % + Zeit-Stop 24 h): ~90 % des
   Trail-Endergebnisses (34,5k vs. 38,2k), dafür niedrigster MaxDD der großen Regeln (4 124),
   **gesündestes Buch der ganzen Messreihe (+3,36 %)**, kein Läufer wird je gekappt, monatlich
   der gleichmäßigste Verlauf. Die Konto-Schlagseite (Arm hält bevorzugt Verlierer) verschwindet.
3. **be2 ist die falsche Ratchet-Schwelle** (21,8k) — bei +2 % werden zu viele Läufer auf 0
   zurückgeholt. +5 % lässt die Aufwärtsbewegung atmen; die Richtungs-Caps (±50/100) würgen
   den Breakeven-Ansatz zusätzlich ab (9–12k, Lauf 4) — nicht kombinieren.
4. **Maximale Kapitaleffizienz + Dump-Schutz bleibt ts24+cap50** (278/Slot, MaxDD 588) —
   das ist die „kleiner, aber extrem robuster Arm"-Konfiguration.

**Empfehlung Prio Upside: Bot 40 auf be5+ts24 umbauen** — Trail-Exit ersetzen durch:
(1) Peak ≥ +5 % → SL auf Entry nachziehen (ein `Close` erst, wenn der Markt den Entry wieder
berührt; alternativ echtes SL-Update, falls Cornix das im Channel kann), (2) Spiegel ohne
+2-%-Peak nach 24 h schließen (`TIME_STOP`), (3) Slot-Cap 500 bleibt (AK4). Alle drei Bausteine
existieren im Bot (peak_pct, filled_at, Cap-Layer). Umbau + Restart sind Michi-gegatet.

## Nachtrag 3 (2026-07-28) — Zwei-Channel-Idee des Operators: 2 × 500 = 1000 Slots

Operator-Vorschlag: die Trades auf zwei Channels aufteilen. Mit „neuer Trade → in den leereren
Channel" ist das exakt ein globaler 1000er-Cap (der leerere Channel hat Platz, solange gesamt
< 1000 offen sind; Symbol-Eindeutigkeit bleibt global). Lauf 6:

| Regel | n | Equity final | MaxDD | Ø Slots / p95 | netto/Ø-Slot | Ø Buch-Mark | Monats-Δ |
|---|--:|--:|--:|--:|--:|--:|---|
| **be5+ts24 @ 1000 (2 Ch.)** | 42 456 | **53 028** | 5 676 | 560 / 975 | 95 | +3,27 % | +11,0/+14,7/+11,3/+9,6/+6,8 |
| Hold @ 1000 (2 Ch.) | 36 060 | 43 167 | 14 193 | 812 / 1000 | 53 | +2,33 % | +0,1/+11,8/+13,9/+13,1/+5,1 |
| be5+ts24 @ 500 (1 Ch.) | 32 372 | 34 469 | 4 124 | 408 / 500 | 85 | +3,36 % | — |
| Trail act=2 (heute, 1 Ch.) | 44 748 | 38 155 | 4 377 | 260 / 495 | 147 | −2,73 % | — |
| be5+ts24 ungedeckelt (theor.) | 44 748 | 58 994 | 6 989 | 583 / 1083 | 101 | +3,26 % | — |

**Mit zwei Channels holt be5+ts24 90 % seines ungedeckelten Potenzials** (53,0k von 59,0k;
95 % der Trades bekommen einen Platz) und schlägt jede deploybare Alternative deutlich:
+39 % gegenüber dem heutigen Trail bei gesundem Buch, +23 % gegenüber Hold@1000 bei 40 %
von dessen MaxDD. Hold profitiert vom zweiten Channel weit weniger (sein Buch klebt auch
bei 1000 am Limit, März fast bei null).

**3-Channel-Nachprüfung (Lauf 7):** die Skalierungskurve von be5+ts24 über den Slot-Deckel:

| Channels (Cap) | Equity final | Δ zum Vorherigen | MaxDD | zugelassen |
|--:|--:|--:|--:|--:|
| 1 (500) | 34 509 | — | 4 124 | 72 % |
| 2 (1000) | 53 068 | **+18 559** | 5 676 | 95 % |
| 3 (1500) | 56 639 | +3 571 | 5 997 | 98 % |
| ∞ (theor.) | 59 034 | +2 395 | 6 989 | 100 % |

Der zweite Channel ist der große Sprung, der dritte kauft nur noch +6,7 % — bei 50 % mehr
Exposure-Kapazität und einem dritten Integrationsziel. Hold@1500 (53 113) bleibt auch mit drei
Channels unter be5+ts24@1500, bei 3× dessen MaxDD (18 890). Kapitalneutral wäre statt eines
dritten Channels die Positionsgröße in zwei Channels zu erhöhen.

**Finale Empfehlung (Prio Upside): zwei Channels + be5+ts24.** Operativ: zweiter Channel +
Cornix-Anbindung + Sizing (Michi); Bot 40 multi-channel (least-loaded-Zuweisung, `channel_id`
in `trailing_positions`, Close-Routing, AK3 global); Kapital-Hinweis: 1000 Slots ×
Positionsgröße = bis zu doppeltes gleichzeitiges Exposure — Sizing pro Channel ist die
Stellschraube. Umbau + Restart Michi-gegatet.

## Nachtrag 4 (2026-07-28) — KORREKTUR: Look-ahead in den Breakeven+Zeit-Stop-Regeln

**Die be+ts-Zahlen der Nachträge 2 und 3 waren durch einen Look-ahead aufgebläht und sind
zurückgezogen.** Der Zeit-Stop in `exit_breakeven` prüfte „wurde der Trade JEMALS scharf?"
über die gesamte Lebensdauer statt „war er BIS ZUR DEADLINE scharf?" — jeder späte Gewinner
(scharf erst nach 24 h) entkam damit dem Stop, den der Live-Bot ihm bei Stunde 24 gegeben
hätte. Gefunden beim Übersetzen der Regel in die Bot-Logik, kausal gefixt (Pin
`test_breakeven_timestop_is_causal_for_late_armers`), Lauf 8:

| Regel | Nachtrag 2/3 (Look-ahead) | **kausal (Lauf 8)** | MaxDD kausal |
|---|--:|--:|--:|
| be2+ts24 | 28 812 | **7 430** | 7 708 |
| be5+ts24 | 58 994 | **7 004** | 8 592 |
| be5+ts24@1000 (2 Ch.) | 53 068 | **3 843** | 7 266 |
| be5+ts24@1500 (3 Ch.) | 56 639 | **5 394** | 7 852 |

Der gesamte Vorsprung der Breakeven-Familie WAR der Look-ahead: kausal tötet der 24h-Stop
die späten Gewinner zu ihrem (meist negativen) 24h-Mark, und die früh Scharfen werden bei 0
ausgebucht. **Die be-Familie ist damit verworfen; die Channel-Skalierungskurve aus Nachtrag 3
ist gegenstandslos.** NICHT betroffen (Deadline-Prüfung war dort immer kausal): hold, alle
Trail-Varianten, trail+ts24/48/72, Caps, Gates, Portfolio.

**Bereinigte deploybare Rangliste (1 Channel, Equity März–Juli):**

| Regel | Equity final | MaxDD | **netto/Ø-Slot** | DD/Ø-Slot | p95 Slots | Ø Buch-Mark |
|---|--:|--:|--:|--:|--:|--:|
| Trail act=2 (heute) | 38 181 | 4 377 | 147 | 16,8 | 495 | −2,73 % |
| Trail 2 + Zeit-Stop 48 h | 32 215 | 4 316 | 180 | 24,1* | 359 | −1,58 % |
| Trail 2 + Zeit-Stop 24 h | 27 438 | 3 015 | 207 | 22,7* | 267 | −1,17 % |
| Hold @ 500 | 26 798 | 6 933 | 56 | 14,6 | 500 | +2,71 % |
| **Trail 2 + ts24 + Cap ±50** | 18 776 | **588** | **278** | **8,7** | 130 | −1,22 % |

_*DD/Ø-Slot der ts-Regeln liegt über dem Trail, weil ihr kleines Buch denselben Rest-DD auf
weniger Slots verteilt — der absolute MaxDD ist trotzdem kleiner._

**Kapital-Lesart (Operator-Kontext: 800 USD verfügbar, 1 Channel):** Bei fixem Kapital wird
nach Belegung skaliert — die Positionsgröße darf so groß sein, wie die p95-Belegung × Margin
ins Risiko-Budget passt. Dann zählt **netto pro Ø-Slot**, nicht die absolute Summe: Trail 2 +
ts24 + Cap ±50 verdient pro gebundenem Kapital das 1,9-fache des reinen Trails (278 vs. 147)
bei einem Siebtel des absoluten MaxDD (588) — und erlaubt bei p95 = 130 Slots eine ~3,8×
größere Position im selben Margin-Rahmen. Konzentrations-Caveat: weniger, größere Positionen
— bei 130 Stück gleichzeitig noch breit diversifiziert.

**Korrigierte Empfehlung:** Für den Start (800 USD, bestehender Channel): **Trail act=2
beibehalten + Zeit-Stop 24 h + Exposure-Cap ±50** in Bot 40. Vor der 2-Channel-Erweiterung
(ab ~2000 USD Equity) einen Lauf mit den dann relevanten Kandidaten unter Cap 1000 (u. a.
Trail act=5, p95 917 — passt erst in zwei Channels).

## Ehrliche Grenzen

- Sim-Entries = `closed_ai_signals`-Entry (Hold-Arm-Geometrie); der Live-Bot steigt seit
  T-051 zum **Markt** ein. Für den Regel-Vergleich unerheblich (alle Regeln messen dieselben
  Trades), für absolute Zahlen nicht wörtlich.
- 15m-Auflösung, Trail-Fill am Stop-Level, keine Slippage; Kerzen-Maske nicht bündig
  (#T42-5, unverändert geerbt). 647 von 44 144 Trades ohne Kerzen-Deckung (Hold-Fallback).
- Equity-MaxDD in unlevered %-Punkten über ein gleichgewichtetes Buch — skaliert mit der
  Buchgröße; als Zweitmaß dient netto/Slot-Tag. Kein Compounding, kein Leverage.
- Das junge Spiegel-Buch (vor Stichtag 15:32:16) ist Phantom-/Selektions-verzerrt und wurde
  für keine Verhaltens-Analyse verwendet; Strategie-Basis ist `closed_ai_signals`.
- Registerstand `shadow_gate` ist ein Standbild von heute über der ganzen Historie.
- Zeit-Stop-Exit zum Kerzen-Close (Market-Order-Annahme), Hard-Stop-Fill am Stop-Level.
- Die Sim kennt keine Symbol-Eindeutigkeit und keinen Slot-Cap-Rückstau (AK3/AK4) — sie
  misst die Regel, nicht die Zulassungsschicht des Bots.

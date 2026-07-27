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

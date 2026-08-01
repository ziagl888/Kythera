# Zulauf-Analyse Bot 40 — warum nicht mehr Trades aufgehen (T-2026-KYT-9050-060)

**Auftrag (Michi, 2026-08-01):** die Trade-Zahl soll steigen — aber nichts wird geändert,
bevor eine vollständige Analyse vorliegt.

**Antwort in einem Satz:** Der Engpass ist **nicht** das Aktualitätsfenster, sondern der
**Exposure-Cap** — und weil der Cap auf die *Differenz* der Richtungen wirkt, ist der
eigentliche Hebel die **SHORT-Seite**, nicht die LONG-Seite, an der die auffälligen
Ablehnungen liegen.

Messfenster: 2026-07-30 08:00 (erster voller Tag nach der 240-s-Nachkalibrierung, Restart
07:28) bis 2026-08-01. Werkzeug: `tools/trailing_intake_audit.py` (read-only).

---

## 1. Warum eine Ein-Gate-Analyse hier falsch liegt

Ein Kandidat muss **fünf Stufen** passieren. Nur zwei hinterlassen eine Zeile in
`trailing_positions` — der Rest existiert ausschließlich als Zähler im Fleet-Log. Wer nur
gegen die DB misst, sieht deshalb systematisch das falsche Gate.

| Stufe | Gate | Spur | gemessen |
|---|---|---|---|
| 1 | Roster · `shadow_gate.leg_status` · Entry · SL/Targets | keine | 31 Beine, **alle live** |
| 2 | Aktualitätsfenster 240 s | **DB** `PREEXISTING` | 707 LONG / 24 SHORT |
| 3 | `SYMBOL_HELD` | Log | Ø 1,6 → 2,8 Kandidaten/Zyklus |
| 3 | `SYMBOL_COOLING` | Log | vernachlässigbar (≤ 5 Zyklen/Tag) |
| 3 | **`EXPOSURE_CAP`** | Log | **Ø 3,2 → 6,0 → 6,6, max 28** |
| 3 | `SLOT_CAP` (500) | Log | **nie ausgelöst** |
| 4 | kein Marktpreis · `mirrorable_at` | Log | ~93 Ereignisse gesamt |
| 5 | Entry nie berührt | **DB** `ENTRY_NOT_FILLED` | 46 gesamt |

`SLOT_CAP` ist in drei Tagen **kein einziges Mal** gefallen: der Cornix-Channel ist nicht
annähernd voll. Platzknappheit ist nicht das Problem, Richtungs-Balance ist es.

---

## 2. Das Fenster sieht aus wie der Engpass — ist es aber nicht

Die abgelehnten Signale liegen in einem **schmalen Band direkt hinter der Grenze**:

| Richtung | n | p10 | p50 | p90 | bei 300 s zulassbar |
|---|---:|---:|---:|---:|---:|
| LONG | 707 | 243 s | 249 s | 256 s | **706** |
| SHORT | 24 | 243 s | 255 s | 593 s | 18 |

Das ist keine Altersverteilung, das ist eine **Wand** — dasselbe Muster wie damals bei
180 s, eine Stufe später. Die 240 s wurden auf gemessene ~190 s Pipeline-Latenz der
Kerzen-Zyklus-Beine kalibriert; die liegt inzwischen bei ~250 s. Die Grenze schneidet mitten
durch die Latenzverteilung **einer Bein-Familie**, und die ist fast reines LONG (707:24).

**Adverse Selection ausgeschlossen:** die abgelehnten LONGs liefern im Quell-Trade
**Ø +2,39 %** gegen **+1,28 %** der zugelassenen (n = 378 vs 80, sd 8,1 → t ≈ 1,3, also
kein signifikanter Unterschied). Die Kante selektiert **nicht** die besseren Signale — wer
das Fenster verbreitert, holt sich keine schlechtere Ware ein.

**Trotzdem bringt Verbreitern allein fast nichts an Stückzahl** — siehe Abschnitt 3.

---

## 3. Der bindende Engpass: der Exposure-Cap

`admit()` weist eine Richtung ab, sobald sie der Gegenrichtung um `EXPOSURE_CAP` (50) offene
Spiegel vorausliegt. Das Buch klebt seit dem Fix an dieser Decke:

| Zeitpunkt | LONG | SHORT | Schieflage | LONG-Spielraum |
|---|---:|---:|---:|---:|
| 07-30 08:00 | 56 | 8 | +48 | 2 |
| 07-31 02:00 | 74 | 32 | +42 | 8 |
| 08-01 02:00 | 78 | 30 | +48 | 2 |
| 08-01 14:00 | 73 | 21 | **+52** | **0 — LONG blockiert** |

Über den gesamten Zeitraum liegt der LONG-Spielraum zwischen **0 und 8**. LONG-Kandidaten
werden also bereits abgewiesen, **nachdem** sie den Aktualitätstest bestanden haben. Ein
weiteres Fenster verschiebt Ablehnungen von `PREEXISTING` nach `EXPOSURE_CAP` — die
Stückzahl bewegt sich kaum.

### Die Identität, auf der alles hängt

Der Cap begrenzt die **Differenz**, nicht die Summe. Bei anliegender Decke gilt:

```
Gesamtkapazität  =  2 × min(LONG, SHORT)  +  Cap
```

Aktuell: 2 × 21 + 50 = **92 Positionen**. **Jede zusätzliche SHORT-Position hebt die
LONG-Decke um eins** — die SHORT-Seite drosselt damit das **Gesamtvolumen**, völlig
unabhängig davon, wie viele LONG-Kandidaten Schlange stehen.

### Die Grandfather-Kohorte zahlt direkt auf diesen Engpass ein

**28 der 30 zeitstop-befreiten Spiegel sind LONG.** Sie schließen nie (nie scharf, also für
den Trail unerreichbar) und belegen damit **dauerhaft 28 der 50 Einheiten LONG-Spielraum** —
56 %. Zusätzlich blockieren sie 28 Symbole gegen `SYMBOL_HELD`. Der Entscheid vom 2026-08-01
(#T54-3, „sie reiten weiter") ist damit teurer als bei seiner Fassung bekannt war: er kostet
nicht nur −81 % offenes Buch, sondern **mehr als die Hälfte des LONG-Durchsatzes**.

---

## 4. Hebel, nach Wirkung geordnet

| # | Hebel | erwartete Wirkung | Risiko | Aufwand |
|---|---|---|---|---|
| **A** | **TSM1 SHORT in den Roster** | +~30 Fills/Tag SHORT → +~22 stehende SHORT → Kapazität **92 → ~150** | gering | 1 Zeile Roster + Restart |
| **B** | Grandfather-Kohorte auflösen | +28 LONG-Spielraum **sofort**, +28 freie Symbole | Realisierung von Σ −81 % | Operator-Entscheid, kein Code |
| **C** | Fenster 240 → 300 s | Stückzahl ~0, aber **bessere Auswahl** (s. u.) | gering | 1 Default + Pins + Restart |
| **D** | `EXPOSURE_CAP` anheben | direkt mehr LONG | **hoch** | Operator-Entscheid |

**Zu A:** TSM1 SHORT produziert **66 Signale/Tag**, ist **live**, hat Dichte 525 — und wurde
bei der ursprünglichen Auswahl allein wegen des **Slot-Caps** verworfen, der seither **nie**
gebunden hat. Das ist die sauberste Maßnahme im Feld: sie greift genau an der Seite an, die
das Gesamtvolumen drosselt.

**Zu C:** Auch ohne Stückzahl-Gewinn ist das Fenster nicht wertlos. `admit()` sortiert
Kandidaten nach **Bein-Dichte**. Ein 300-s-Fenster stellt dem gleichen LONG-Budget rund
**fünfmal so viele** Kandidaten zur Auswahl — die belegten Slots gehen dann an dichtere
Beine. Der Gewinn ist Qualität pro Slot, nicht Menge. **Deshalb gehört C nach A/B**, nicht
davor: erst Spielraum schaffen, dann besser füllen.

**Zu D — ausdrücklich nicht empfohlen:** T-052 hat gemessen, dass das **einseitige LONG-Buch
der Konto-Schaden war** und die strukturelle Schranke jedes Marktlagen-Modell schlug. Den Cap
anzuheben öffnet genau diese Tür wieder. Wenn überhaupt, dann nach einer eigenen Studie und
nicht als Nebeneffekt einer Durchsatz-Maßnahme.

---

## 5. Ehrliche Grenzen dieser Analyse

- **Die Log-Gates sind Druck, keine Stückzahlen.** Abweisungen wiederholen sich in jedem
  10-s-Zyklus, solange der Quell-Trade offen ist. „Ø 6,6 EXPOSURE_CAP" heißt „zu jedem
  Zeitpunkt stehen ~6,6 Kandidaten an der Decke an", **nicht** „6,6 Signale/Tag verloren".
  Eine Distinct-Zählung wäre nur über DEBUG-Logs möglich, die nicht geschrieben werden.
- **Die Wirkungsschätzung zu A ist eine Hochrechnung**, keine Messung: sie unterstellt für
  TSM1 dieselbe Signal→Fill-Konversion (~45 %) und dieselbe Haltedauer (~0,73 Tage) wie für
  die bestehenden SHORT-Beine. Ein Bein mit anderem Zeitprofil verschiebt das Ergebnis.
- **Die Haltedauer ist aus L/λ gerechnet** (96 offen / 131 Fills pro Tag), nicht aus dem
  Mittel geschlossener Trades — letzteres ist survivorship-verzerrt (0,35 statt 0,73 Tage),
  weil die langen Positionen noch offen sind und darin fehlen.
- **Die Qualitätsprobe in Abschnitt 2 misst den Quell-Trade**, nicht den Arm-Exit. Als Proxy
  für „ist die abgelehnte Ware schlechter" trägt sie; als Ertragsprognose nicht.
- **Nicht gemessen:** ob die 4 wegen des Slot-Caps verworfenen Beine (EPD3 LONG, BR2H LONG,
  TSM1 SHORT, BB_1H LONG) unter heutigen Bedingungen anders bewertet würden — die
  Auswahlrechnung stammt vom 2026-07-26 und der Slot-Cap bindet nicht mehr.

---

## 6. Empfehlung

1. **A umsetzen** (TSM1 SHORT in den Roster) — greift am tatsächlichen Engpass, geringes
   Risiko, eine Roster-Zeile. Eigener Task, wirksam nach Fleet-Restart.
2. **B entscheiden** (Michi): die Grandfather-Kohorte kostet über die Hälfte des
   LONG-Spielraums. Der Entscheid vom 2026-08-01 fiel ohne diese Zahl — er ist damit nicht
   falsch, aber neu zu bewerten.
3. **C danach**, als Qualitäts- nicht als Mengenmaßnahme.
4. **D nicht ohne eigene Studie.**

Vor jeder dieser Änderungen gilt weiterhin: **kein Live-Eingriff aus einer Dev-Session** —
Roster-Änderung und Fenster-Default sind PRs, wirksam erst nach einem Restart durch Michi.

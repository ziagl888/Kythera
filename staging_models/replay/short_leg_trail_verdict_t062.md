# Welches SHORT-Bein verdient einen Platz im Trailing-Channel? (T-2026-KYT-9050-062)

**Auftrag (Michi, 2026-08-01):** mehr Shorts für Bot 40 — erst EPD prüfen, dann MIS,
dann TSM1; und den Maßstab reparieren, als er sich als unfair herausstellte.

**Antwort in einem Satz:** Die Kante sitzt in den **dünnen** Beinen (MIS2-Familie,
+5,5 bis +8,1 Punkte Residuum), das Volumen in den **schlechten** (TSM1 −0,72 bei
107 Signalen/Tag) — mehr Shorts und bessere Shorts sind hier verschiedene Ziele.

Messfenster 2026-06-01 → 08-01. Werkzeuge: `tools/short_leg_trail_value.py` (der
tragende Maßstab) und `tools/epd_short_generation_study.py` (Vorstufe, s. §2).

---

## 1. Zwei Maßstäbe, und warum der erste unfair war

Der erste Ansatz bewertete ein Bein als **realisiert − volle Index-Bewegung über das
Haltefenster**. Das klingt nach „wie viel der Bewegung wurde eingefangen" und ist in
einem trendenden Markt konstruktionsbedingt ungerecht: ein Take-Profit-Bein steigt bei
TP1 aus, während der Tape weiterläuft. Über einen Zeitraum mit **−50 % Index** fiel
damit fast jedes SHORT-Bein negativ aus — ein Ergebnis, das „schlechte Auswahl" und
„TP kappt den Trend" nicht trennen kann.

Der zweite Ansatz stellt **beide Seiten unter dieselbe Trail-Regel** (act = 2 %,
x = 10 %):

| | |
|---|---|
| Bein | Trail auf dem eigenen Coin-Pfad = was Bot 40 beim Spiegeln verdient hätte |
| Benchmark | derselbe Trail auf dem **Index**-Pfad über dasselbe Fenster |
| Residuum | Bein − Benchmark |

Damit fällt die eigene TP-Politik auf beiden Seiten heraus, und Beine mit
unterschiedlichem Exit-Stil werden vergleichbar. Der Index trägt dafür ein
**synthetisches Hoch/Tief** (Median-Stunden-High- und -Low-Ratio) — ein Trail feuert
auf Dochten, und ein Close-only-Benchmark hätte jedes Bein geschmeichelt.

---

## 2. Ergebnis unter dem fairen Maßstab

| Bein | n | Bein/Trade | Markt | **Residuum** | t_clust | Gate | Sig./Tag |
|---|---:|---:|---:|---:|---:|---|---:|
| MIS2-168h | 47 | 9,074 | 0,994 | **+8,081** | 8,74 | live, Roster | 0,9 |
| MIS2-72h | 132 | 7,968 | 1,033 | **+6,934** | 9,52 | live, Roster | 6,3 |
| MIS2-24h | 117 | 7,569 | 0,766 | **+6,802** | 4,73 | live, Roster | 2,0 |
| MIS2-8h | 122 | 5,836 | 0,302 | **+5,534** | 3,48 | **shadow** | — |
| MIS1-24h | 61 | 5,901 | 0,964 | **+4,937** | 3,81 | **shadow** | 3,4 |
| MIS1-8h | 104 | 3,434 | 0,889 | +2,546 | 3,79 | live, Roster | 5,0 |
| RUB1 | 257 | 3,595 | 1,252 | +2,343 | 3,17 | live, Roster | 8,6 |
| MIS1-72h | 119 | 3,127 | 1,007 | +2,119 | 2,27 | **shadow** | 6,3 |
| AIM1 | 523 | 2,874 | 1,515 | +1,359 | 5,59 | — | — |
| ROM1 | 4 514 | 1,352 | 0,492 | +0,859 | 2,43 | **Duplikat** | 148 |
| AIM2 | 1 643 | 1,842 | 1,156 | +0,685 | 4,95 | live, Roster | 37,7 |
| … | | | | | | | |
| EPD3 | 7 271 | 0,295 | 0,674 | **−0,379** | −5,90 | shadow | 337 |
| BR2H | 907 | 0,608 | 1,227 | −0,619 | −4,03 | shadow (SHORT) | 27 |
| TSM1 | 1 308 | 0,220 | 0,937 | **−0,717** | −4,58 | **live** | 107 |
| BB_4H | 684 | 0,569 | 1,308 | −0,739 | −3,47 | shadow (SHORT) | 10 |
| BR1Hv2 | 1 331 | 0,419 | 1,238 | −0,819 | −5,18 | shadow | 50 |

**Unter beiden Maßstäben negativ** — und deshalb belastbar: **TSM1 SHORT** (−0,72,
t −4,58) und **EPD3 SHORT** (−0,38, t −5,90). Der T-032-Park von EPD3 steht.

---

## 3. Zwei eigene Fehlurteile, die der faire Maßstab korrigiert hat

**(a) „Die Dichte-Rangliste ist ein Artefakt."** In T-060 hatte ich argumentiert, die
MIS2-Beine stünden nur an der Roster-Spitze, weil ihr Slot-Tage-Nenner fast null ist
(0,8–2,4 Slot-Tage über fünf Monate), und seien damit reine Mikro-Scalper. **Falsch.**
Unter der Exit-Regel des Arms verdienen sie ihren Platz: +6,8 bis +8,1 Residuum bei
t = 4,7 bis 9,5. Die Auswahl vom 26.07. hat die richtigen Beine gezogen.

**(b) „TSM1 SHORT ist der saubere Hebel."** Über zwei Runden empfohlen — auf Basis von
Menge (107 Signale/Tag, live, seinerzeit nur wegen des nie bindenden Slot-Caps
draußen) und **ohne die Qualität zu messen**. TSM1 ist unter beiden Maßstäben
signifikant negativ. Genau der Fehler, den die EPD- und MIS-Durchgänge vermieden
hatten.

**(c) Teilkorrektur:** die MIS1-Shadow-Beine hatte ich als „Rauschen mit Vorzeichen"
abgetan (acht Beine getestet, eines über t = 2). Unter dem fairen Maßstab halten sie:
MIS1-24h +4,94 (t 3,81), MIS1-72h +2,12 (t 2,27).

---

## 4. Die Spannung, die das offenlegt

**Kante und Volumen sind auf der SHORT-Seite entkoppelt.** Die MIS2-Familie liefert
0,9 bis 6,3 Signale/Tag bei Residuum +7; TSM1 liefert 107/Tag bei −0,72; EPD3 liefert
337/Tag bei −0,38. Ein hochvolumiges SHORT-Bein mit positiver Kante existiert nicht,
mit zwei Ausnahmen: **AIM2** (37,7/Tag, +0,69, bereits im Roster) und **ROM1**
(148/Tag, +0,86) — letzteres aber ein Re-Forwarder, dessen Spiegelung die
Original-Beine doppelt zählt (T-052, `EXCLUDED_AS_DUPLICATE`).

Das ist die eigentliche Antwort auf „wir brauchen mehr Shorts": mit den vorhandenen
Beinen ist **mehr Volumen nur zulasten der Qualität** zu haben.

---

## 5. Ehrliche Grenzen

- **Die MIS2-Mittelwerte stehen auf n = 47–132 und sind vermutlich fettschwänzig.**
  Ein Bein, das nach Pumps shortet, lebt von wenigen Coins, die 30 % einbrechen.
  Für **Vorzeichen und Rangfolge** trägt die Messung; wer auf die **Größenordnung**
  setzen will, braucht vorher Median und Quantile. Nicht gerechnet.
- **Shadow-Beine kennen keine Slippage.** Alle Zahlen sind Obergrenzen; MIS2-8h,
  MIS1-24h und MIS1-72h stehen im Shadow und sind davon betroffen.
- **Der Index ist nicht handelbar.** Er ist ein Maßstab für Auswahlgüte, keine
  Alternative, die der Operator hätte wählen können.
- **Inferenz auf Tagesebene geclustert.** Die Trades überlappen stark (dieselben
  Coins, dieselben Stunden); nominales n behandelt eine Marktbewegung als viele
  Beobachtungen. Die t-Werte oben sind die konservativen.
- **1 725 Trades fielen aus der Wertung**, weil ihr Fenster außerhalb der
  Index-Abdeckung lag.
- **`closed_ai_signals` ist nur dedupliziert lesbar** (357k-Dup-Blob + synthetische
  LEGACY-Preise). Roh gelesen meldet EPD1 SHORT einen Median von +21,2 %/Trade über
  46 729 Zeilen; dedupliziert sind es 2 793 Trades. Kontrakt aus
  `wave_buildup_study.load_trades`.

---

## 6. Empfehlung

1. **Nichts an TSM1 SHORT.** Der Roster-Platz wäre negativ belegt. Der Punkt aus
   T-060 („TSM1 rein") ist damit erledigt — er beruhte auf Menge ohne Qualität.
2. **EPD-Shorts bleiben geparkt.** Alle drei Generationen ohne Kante; EPD3 unter
   beiden Maßstäben signifikant negativ.
3. **Kandidaten für einen Gate-Flip, falls mehr SHORT-Volumen gewünscht ist:**
   MIS1-24h (+4,94, t 3,81), MIS1-72h (+2,12, t 2,27), MIS2-8h (+5,53, t 3,48) —
   zusammen ~13 Signale/Tag. Kleine Menge, aber gemessene Kante. Vorher §5 Punkt 1
   klären (Verteilung statt Mittelwert).
4. **Der Exposure-Cap bleibt damit gebunden.** Das Volumenproblem aus T-060 ist mit
   den vorhandenen Beinen nicht lösbar, ohne Qualität einzukaufen. Wer den Durchsatz
   wirklich heben will, muss an der Grandfather-Kohorte ansetzen (28 LONG belegen
   56 % des Cap-Spielraums, #T54-3) — nicht an der SHORT-Zufuhr.

Gate-Flip und Roster-Änderung sind Operator-Entscheide und brauchen einen
Fleet-Restart. Nicht Teil dieses Tasks.

# Trailing-Arm Buch-Gesundheit — Exit-Regeln am offenen Buch gemessen (T-2026-KYT-9050-052)

_generated 2026-07-28T12:33:03.488336+00:00 · read-only · Roster-Beine ohne ROM1 · x=10% · tf 15m · ab 2026-03-01 · Gebühr 0.10 %/Trade · 44755 Trades_

**Frage:** `tools/trailing_slot_budget.py` maß realisierte Summen und Slots. Eine Regel, die
Gewinner schließt und Verlierer hält, sieht dort gut aus und im offenen Buch schlecht —
Bot 40 hat das live bewiesen. Hier wird jede Regel an BEIDEN Seiten gemessen: realisiert
UND Zusammensetzung des offenen Buchs (Equity = realisierte Summe + offenes MTM,
gleichgewichtet, unlevered %-Punkte).

| Regel | n | Σ netto | Ø/Trade | Ø Slots | p95 | netto/Slot-Tag | Equity final | **Equity MaxDD** | netto/Ø-Slot | DD/Ø-Slot | Ø Buch-Mark | Buch unter Wasser | Ø L offen | Ø S offen |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Hold (Fleet-Exit, SL/TP/Timeout) | 44755 | 58103 | 1.298 | 1008 | 1635 | 0.385 | 58103 | **20076** | 58 | 19.9 | +2.31 % | 44 % | 760 | 248 |
| Trail act=2 % (Bot 40 heute) | 44755 | 38167 | 0.853 | 260 | 495 | 0.980 | 38167 | **4377** | 147 | 16.8 | -2.73 % | 78 % | 219 | 42 |
| Trail act=5 % | 44755 | 50576 | 1.130 | 539 | 918 | 0.627 | 50576 | **9786** | 94 | 18.2 | -1.97 % | 66 % | 433 | 106 |
| Trail act=10 % | 44755 | 62651 | 1.400 | 770 | 1357 | 0.543 | 62651 | **14856** | 81 | 19.3 | -0.90 % | 55 % | 610 | 160 |
| Trail act=2 %, x=20 % (langsamer closen) | 44755 | 32255 | 0.721 | 261 | 496 | 0.826 | 32255 | **4887** | 124 | 18.7 | -2.71 % | 78 % | 219 | 42 |
| Trail act=2 %, x=30 % | 44755 | 27491 | 0.614 | 263 | 496 | 0.697 | 27491 | **5627** | 104 | 21.4 | -2.62 % | 77 % | 221 | 42 |
| Trail act=10 %, x=20 % | 44755 | 55942 | 1.250 | 775 | 1368 | 0.482 | 55942 | **15337** | 72 | 19.8 | -0.82 % | 54 % | 612 | 163 |
| Trail 2 % + Zeit-Stop 24 h | 44755 | 27445 | 0.613 | 133 | 267 | 1.383 | 27445 | **3015** | 207 | 22.7 | -1.17 % | 62 % | 108 | 25 |
| Trail 2 % + Zeit-Stop 48 h | 44755 | 32207 | 0.720 | 179 | 358 | 1.201 | 32207 | **4316** | 180 | 24.1 | -1.58 % | 68 % | 148 | 31 |
| Trail 2 % + Zeit-Stop 72 h | 44755 | 34499 | 0.771 | 203 | 410 | 1.133 | 34499 | **4237** | 170 | 20.8 | -1.85 % | 70 % | 169 | 34 |
| Trail 2 % + Hard-Stop −2 % | 44755 | 15642 | 0.349 | 81 | 173 | 1.287 | 15642 | **2216** | 193 | 27.3 | +0.00 % | 41 % | 66 | 15 |
| Trail 2 % nur SHORT (LONG hält) | 44755 | 52108 | 1.164 | 801 | 1563 | 0.434 | 52108 | **20736** | 65 | 25.9 | +0.91 % | 50 % | 760 | 42 |
| Trail 2 % nur LONG (SHORT hält) | 44755 | 44161 | 0.987 | 466 | 889 | 0.632 | 44161 | **5872** | 95 | 12.6 | +1.16 % | 56 % | 219 | 248 |
| Trail 2 %, 50 % Teilschließung | 44755 | 48135 | 1.075 | 634 | 1021 | 0.507 | 48135 | **12166** | 76 | 19.2 | +1.49 % | 50 % | 489 | 145 |
| Trail 2 % + Exposure-Cap ±50 | 20064 | 21169 | 1.055 | 100 | 197 | 1.415 | 21169 | **683** | 212 | 6.8 | -2.83 % | 76 % | 65 | 35 |
| Trail 2 % + Exposure-Cap ±100 | 25647 | 25152 | 0.981 | 137 | 250 | 1.229 | 25152 | **1181** | 184 | 8.6 | -2.83 % | 78 % | 99 | 38 |
| Trail 2 % + Zeit-Stop 24 h + Cap ±50 | 24292 | 18769 | 0.773 | 68 | 130 | 1.857 | 18769 | **588** | 278 | 8.7 | -1.22 % | 62 % | 47 | 21 |
| SL-Nachzug: Breakeven ab +2 % (kein Trail) | 44755 | 16073 | 0.359 | 426 | 724 | 0.252 | 16073 | **8996** | 38 | 21.1 | +1.85 % | 46 % | 336 | 90 |
| Breakeven ab +2 % + Zeit-Stop 24 h | 44755 | 28883 | 0.645 | 369 | 661 | 0.523 | 28883 | **5867** | 78 | 15.9 | +2.86 % | 40 % | 286 | 83 |
| Breakeven 2 % + Zeit-Stop 24 h + Cap ±50 | 20033 | 9006 | 0.450 | 152 | 263 | 0.395 | 9006 | **1005** | 59 | 6.6 | +3.44 % | 38 % | 89 | 63 |
| Breakeven 2 % + Zeit-Stop 24 h + Cap ±100 | 24562 | 11740 | 0.478 | 192 | 318 | 0.407 | 11740 | **1266** | 61 | 6.6 | +3.19 % | 40 % | 124 | 68 |
| Breakeven ab +5 % + Zeit-Stop 24 h | 44755 | 59034 | 1.319 | 582 | 1083 | 0.677 | 59034 | **6989** | 101 | 12.0 | +3.27 % | 36 % | 442 | 141 |
| Hold unter hartem 500-Slot-Cap | 21729 | 26778 | 1.232 | 476 | 500 | 0.376 | 26778 | **6933** | 56 | 14.6 | +2.71 % | 42 % | 360 | 116 |
| Breakeven 2 % + Zeit-Stop 24 h @ 500-Cap | 39370 | 21818 | 0.554 | 327 | 500 | 0.446 | 21818 | **4324** | 67 | 13.2 | +2.88 % | 40 % | 249 | 77 |
| Breakeven 5 % + Zeit-Stop 24 h @ 500-Cap | 32379 | 34509 | 1.066 | 407 | 500 | 0.566 | 34509 | **4124** | 85 | 10.1 | +3.36 % | 36 % | 300 | 108 |
| Hold @ 1000 (2 Channels, least-loaded) | 36066 | 43195 | 1.198 | 811 | 1000 | 0.356 | 43195 | **14193** | 53 | 17.5 | +2.34 % | 44 % | 612 | 200 |
| Breakeven 5 % + Zeit-Stop 24 h @ 1000 (2 Channels) | 42463 | 53068 | 1.250 | 560 | 975 | 0.633 | 53068 | **5676** | 95 | 10.1 | +3.27 % | 36 % | 420 | 140 |
| Hold @ 1500 (3 Channels) | 42823 | 53113 | 1.240 | 974 | 1477 | 0.364 | 53113 | **18890** | 55 | 19.4 | +2.30 % | 44 % | 729 | 245 |
| Breakeven 5 % + Zeit-Stop 24 h @ 1500 (3 Channels) | 43844 | 56639 | 1.292 | 576 | 1064 | 0.656 | 56639 | **5997** | 98 | 10.4 | +3.27 % | 36 % | 436 | 141 |
| Buch-Feedback-Gate (D nur wenn offenes D-Buch > −1 %) | 7944 | 6288 | 0.792 | 41 | 117 | 1.029 | 6288 | **820** | 154 | 20.1 | -3.52 % | 78 % | 31 | 10 |
| BTC-Richtungs-Gate (LONG nur bei 24h-Ret > 0) | 23280 | 19702 | 0.846 | 134 | 280 | 0.980 | 19702 | **2969** | 147 | 22.1 | -3.12 % | 80 % | 108 | 26 |
| Portfolio-Trail 10 % (kein Einzel-Trail) | 44755 | 9316 | 0.208 | 48 | 175 | 1.309 | 9316 | **5271** | 196 | 110.8 | -0.25 % | 33 % | 35 | 13 |
| Portfolio-Trail 15 % (kein Einzel-Trail) | 44755 | 8134 | 0.182 | 53 | 207 | 1.027 | 8134 | **4919** | 154 | 92.9 | -0.21 % | 33 % | 38 | 15 |

## Lesehilfe

- **Equity MaxDD** ist die Kennzahl, die der Studie fehlte: max. Rückgang der Kurve
  (realisiert + offen), in unlevered %-Punkten über das gleichgewichtete Buch.
- **Ø Buch-Mark** = zeitgemittelter Durchschnitts-Mark der offenen Positionen. Ein stark
  negativer Wert heißt: das Buch besteht strukturell aus Verlierern.
- **Buch unter Wasser** = zeitgemittelter Anteil offener Positionen im Minus.

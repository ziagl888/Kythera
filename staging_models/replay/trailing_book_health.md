# Trailing-Arm Buch-Gesundheit — Exit-Regeln am offenen Buch gemessen (T-2026-KYT-9050-052)

_generated 2026-07-28T13:10:50.790949+00:00 · read-only · Roster-Beine ohne ROM1 · x=10% · tf 15m · ab 2026-03-01 · Gebühr 0.10 %/Trade · 44763 Trades_

**Frage:** `tools/trailing_slot_budget.py` maß realisierte Summen und Slots. Eine Regel, die
Gewinner schließt und Verlierer hält, sieht dort gut aus und im offenen Buch schlecht —
Bot 40 hat das live bewiesen. Hier wird jede Regel an BEIDEN Seiten gemessen: realisiert
UND Zusammensetzung des offenen Buchs (Equity = realisierte Summe + offenes MTM,
gleichgewichtet, unlevered %-Punkte).

| Regel | n | Σ netto | Ø/Trade | Ø Slots | p95 | netto/Slot-Tag | Equity final | **Equity MaxDD** | netto/Ø-Slot | DD/Ø-Slot | Ø Buch-Mark | Buch unter Wasser | Ø L offen | Ø S offen |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Hold (Fleet-Exit, SL/TP/Timeout) | 44763 | 58124 | 1.298 | 1008 | 1635 | 0.385 | 58124 | **20076** | 58 | 19.9 | +2.31 % | 44 % | 760 | 248 |
| Trail act=2 % (Bot 40 heute) | 44763 | 38181 | 0.853 | 260 | 495 | 0.980 | 38181 | **4377** | 147 | 16.8 | -2.73 % | 78 % | 219 | 42 |
| Trail act=5 % | 44763 | 50587 | 1.130 | 539 | 918 | 0.627 | 50587 | **9786** | 94 | 18.2 | -1.97 % | 66 % | 433 | 106 |
| Trail act=10 % | 44763 | 62679 | 1.400 | 771 | 1358 | 0.543 | 62679 | **14856** | 81 | 19.3 | -0.90 % | 55 % | 610 | 160 |
| Trail act=2 %, x=20 % (langsamer closen) | 44763 | 32268 | 0.721 | 261 | 496 | 0.826 | 32268 | **4887** | 124 | 18.7 | -2.71 % | 78 % | 219 | 42 |
| Trail act=2 %, x=30 % | 44763 | 27502 | 0.614 | 264 | 496 | 0.697 | 27502 | **5627** | 104 | 21.4 | -2.62 % | 77 % | 221 | 42 |
| Trail act=10 %, x=20 % | 44763 | 55966 | 1.250 | 775 | 1368 | 0.482 | 55966 | **15337** | 72 | 19.8 | -0.83 % | 54 % | 612 | 163 |
| Trail 2 % + Zeit-Stop 24 h | 44763 | 27438 | 0.613 | 133 | 267 | 1.382 | 27438 | **3015** | 207 | 22.7 | -1.17 % | 62 % | 108 | 25 |
| Trail 2 % + Zeit-Stop 48 h | 44763 | 32215 | 0.720 | 179 | 359 | 1.201 | 32215 | **4316** | 180 | 24.1 | -1.58 % | 68 % | 148 | 31 |
| Trail 2 % + Zeit-Stop 72 h | 44763 | 34507 | 0.771 | 203 | 410 | 1.133 | 34507 | **4237** | 170 | 20.8 | -1.85 % | 70 % | 169 | 34 |
| Trail 2 % + Hard-Stop −2 % | 44763 | 15633 | 0.349 | 81 | 173 | 1.286 | 15633 | **2216** | 192 | 27.3 | +0.00 % | 41 % | 66 | 15 |
| Trail 2 % nur SHORT (LONG hält) | 44763 | 52125 | 1.165 | 802 | 1563 | 0.434 | 52125 | **20736** | 65 | 25.9 | +0.91 % | 50 % | 760 | 42 |
| Trail 2 % nur LONG (SHORT hält) | 44763 | 44180 | 0.987 | 466 | 889 | 0.633 | 44180 | **5872** | 95 | 12.6 | +1.16 % | 56 % | 219 | 248 |
| Trail 2 %, 50 % Teilschließung | 44763 | 48152 | 1.076 | 634 | 1021 | 0.507 | 48152 | **12166** | 76 | 19.2 | +1.49 % | 50 % | 490 | 145 |
| Trail 2 % + Exposure-Cap ±50 | 20072 | 21215 | 1.057 | 100 | 196 | 1.418 | 21215 | **683** | 212 | 6.8 | -2.83 % | 76 % | 65 | 35 |
| Trail 2 % + Exposure-Cap ±100 | 25644 | 25167 | 0.981 | 137 | 250 | 1.229 | 25167 | **1181** | 184 | 8.6 | -2.83 % | 78 % | 99 | 38 |
| Trail 2 % + Zeit-Stop 24 h + Cap ±50 | 24298 | 18776 | 0.773 | 68 | 130 | 1.857 | 18776 | **588** | 278 | 8.7 | -1.22 % | 62 % | 47 | 21 |
| SL-Nachzug: Breakeven ab +2 % (kein Trail) | 44763 | 16093 | 0.359 | 426 | 724 | 0.252 | 16093 | **8996** | 38 | 21.1 | +1.85 % | 47 % | 336 | 90 |
| Breakeven ab +2 % + Zeit-Stop 24 h | 44763 | 7430 | 0.166 | 261 | 473 | 0.190 | 7430 | **7708** | 28 | 29.5 | +4.32 % | 32 % | 198 | 63 |
| Breakeven 2 % + Zeit-Stop 24 h + Cap ±50 | 21693 | 4106 | 0.189 | 126 | 227 | 0.217 | 4106 | **1562** | 33 | 12.4 | +4.81 % | 30 % | 74 | 52 |
| Breakeven 2 % + Zeit-Stop 24 h + Cap ±100 | 26991 | 3958 | 0.147 | 160 | 262 | 0.166 | 3958 | **2100** | 25 | 13.2 | +4.59 % | 31 % | 104 | 56 |
| Breakeven ab +5 % + Zeit-Stop 24 h | 44763 | 7004 | 0.157 | 299 | 539 | 0.156 | 7004 | **8592** | 23 | 28.7 | +4.49 % | 34 % | 220 | 79 |
| Hold unter hartem 500-Slot-Cap | 21726 | 26798 | 1.234 | 476 | 500 | 0.376 | 26798 | **6933** | 56 | 14.6 | +2.71 % | 42 % | 360 | 116 |
| Breakeven 2 % + Zeit-Stop 24 h @ 500-Cap | 41533 | 3207 | 0.077 | 245 | 427 | 0.087 | 3207 | **5769** | 13 | 23.6 | +4.30 % | 32 % | 183 | 62 |
| Breakeven 5 % + Zeit-Stop 24 h @ 500-Cap | 40862 | 2572 | 0.063 | 276 | 464 | 0.062 | 2572 | **6235** | 9 | 22.6 | +4.49 % | 34 % | 201 | 75 |
| Hold @ 1000 (2 Channels, least-loaded) | 36071 | 43212 | 1.198 | 812 | 1000 | 0.356 | 43212 | **14193** | 53 | 17.5 | +2.34 % | 44 % | 612 | 200 |
| Breakeven 5 % + Zeit-Stop 24 h @ 1000 (2 Channels) | 43496 | 3843 | 0.088 | 294 | 532 | 0.087 | 3843 | **7266** | 13 | 24.7 | +4.48 % | 34 % | 215 | 79 |
| Hold @ 1500 (3 Channels) | 42831 | 53134 | 1.240 | 974 | 1477 | 0.364 | 53134 | **18890** | 55 | 19.4 | +2.30 % | 44 % | 729 | 245 |
| Breakeven 5 % + Zeit-Stop 24 h @ 1500 (3 Channels) | 44036 | 5394 | 0.122 | 296 | 539 | 0.122 | 5394 | **7852** | 18 | 26.5 | +4.49 % | 34 % | 218 | 79 |
| Buch-Feedback-Gate (D nur wenn offenes D-Buch > −1 %) | 7944 | 6288 | 0.792 | 41 | 117 | 1.029 | 6288 | **820** | 154 | 20.1 | -3.52 % | 78 % | 31 | 10 |
| BTC-Richtungs-Gate (LONG nur bei 24h-Ret > 0) | 23285 | 19711 | 0.847 | 134 | 280 | 0.980 | 19711 | **2969** | 147 | 22.1 | -3.12 % | 80 % | 108 | 26 |
| Portfolio-Trail 10 % (kein Einzel-Trail) | 44763 | 9314 | 0.208 | 48 | 175 | 1.308 | 9314 | **5271** | 196 | 110.8 | -0.25 % | 33 % | 35 | 13 |
| Portfolio-Trail 15 % (kein Einzel-Trail) | 44763 | 8133 | 0.182 | 53 | 207 | 1.026 | 8133 | **4919** | 154 | 92.9 | -0.21 % | 33 % | 38 | 15 |

## Lesehilfe

- **Equity MaxDD** ist die Kennzahl, die der Studie fehlte: max. Rückgang der Kurve
  (realisiert + offen), in unlevered %-Punkten über das gleichgewichtete Buch.
- **Ø Buch-Mark** = zeitgemittelter Durchschnitts-Mark der offenen Positionen. Ein stark
  negativer Wert heißt: das Buch besteht strukturell aus Verlierern.
- **Buch unter Wasser** = zeitgemittelter Anteil offener Positionen im Minus.

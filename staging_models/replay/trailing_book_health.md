# Trailing-Arm Buch-Gesundheit — Exit-Regeln am offenen Buch gemessen (T-2026-KYT-9050-052)

_generated 2026-07-28T03:17:37.498812+00:00 · read-only · Roster-Beine ohne ROM1 · x=10% · tf 15m · ab 2026-03-01 · Gebühr 0.10 %/Trade · 44650 Trades_

**Frage:** `tools/trailing_slot_budget.py` maß realisierte Summen und Slots. Eine Regel, die
Gewinner schließt und Verlierer hält, sieht dort gut aus und im offenen Buch schlecht —
Bot 40 hat das live bewiesen. Hier wird jede Regel an BEIDEN Seiten gemessen: realisiert
UND Zusammensetzung des offenen Buchs (Equity = realisierte Summe + offenes MTM,
gleichgewichtet, unlevered %-Punkte).

| Regel | n | Σ netto | Ø/Trade | Ø Slots | p95 | netto/Slot-Tag | Equity final | **Equity MaxDD** | Ø Buch-Mark | Buch unter Wasser | Ø L offen | Ø S offen |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Hold (Fleet-Exit, SL/TP/Timeout) | 44650 | 58049 | 1.300 | 1008 | 1636 | 0.386 | 58049 | **20076** | +2.31 % | 44 % | 761 | 247 |
| Trail act=2 % (Bot 40 heute) | 44650 | 38048 | 0.852 | 260 | 495 | 0.979 | 38048 | **4377** | -2.73 % | 78 % | 219 | 42 |
| Trail act=5 % | 44650 | 50457 | 1.130 | 539 | 918 | 0.627 | 50457 | **9786** | -1.97 % | 66 % | 433 | 106 |
| Trail act=10 % | 44650 | 62564 | 1.401 | 771 | 1356 | 0.544 | 62564 | **14856** | -0.90 % | 55 % | 611 | 160 |
| Trail act=2 %, x=20 % (langsamer closen) | 44650 | 32151 | 0.720 | 261 | 495 | 0.825 | 32151 | **4887** | -2.71 % | 78 % | 219 | 42 |
| Trail act=2 %, x=30 % | 44650 | 27405 | 0.614 | 264 | 496 | 0.697 | 27405 | **5627** | -2.62 % | 77 % | 221 | 42 |
| Trail act=10 %, x=20 % | 44650 | 55856 | 1.251 | 775 | 1368 | 0.483 | 55856 | **15337** | -0.83 % | 54 % | 613 | 162 |
| Trail 2 % + Zeit-Stop 24 h | 44650 | 27363 | 0.613 | 133 | 268 | 1.381 | 27363 | **3015** | -1.17 % | 62 % | 108 | 25 |
| Trail 2 % + Zeit-Stop 48 h | 44650 | 32140 | 0.720 | 179 | 358 | 1.201 | 32140 | **4316** | -1.58 % | 68 % | 148 | 31 |
| Trail 2 % + Zeit-Stop 72 h | 44650 | 34408 | 0.771 | 204 | 410 | 1.132 | 34408 | **4237** | -1.85 % | 70 % | 169 | 34 |
| Trail 2 % + Hard-Stop −2 % | 44650 | 15555 | 0.348 | 81 | 173 | 1.281 | 15555 | **2216** | +0.00 % | 41 % | 67 | 15 |
| Trail 2 % nur SHORT (LONG hält) | 44650 | 52078 | 1.166 | 803 | 1564 | 0.435 | 52078 | **20736** | +0.91 % | 50 % | 761 | 42 |
| Trail 2 % nur LONG (SHORT hält) | 44650 | 44019 | 0.986 | 466 | 889 | 0.633 | 44019 | **5872** | +1.17 % | 56 % | 219 | 247 |
| Trail 2 %, 50 % Teilschließung | 44650 | 48048 | 1.076 | 634 | 1020 | 0.507 | 48048 | **12166** | +1.49 % | 50 % | 490 | 144 |
| Trail 2 % + Exposure-Cap ±50 | 19979 | 21084 | 1.055 | 100 | 196 | 1.413 | 21084 | **683** | -2.84 % | 76 % | 65 | 35 |
| Trail 2 % + Exposure-Cap ±100 | 25558 | 24994 | 0.978 | 137 | 250 | 1.223 | 24994 | **1181** | -2.84 % | 78 % | 99 | 38 |
| Trail 2 % + Zeit-Stop 24 h + Cap ±50 | 24212 | 18686 | 0.772 | 68 | 130 | 1.852 | 18686 | **588** | -1.22 % | 62 % | 47 | 21 |
| SL-Nachzug: Breakeven ab +2 % (kein Trail) | 44650 | 15998 | 0.358 | 426 | 724 | 0.251 | 15998 | **8996** | +1.84 % | 47 % | 336 | 90 |
| Breakeven ab +2 % + Zeit-Stop 24 h | 44650 | 28812 | 0.645 | 369 | 661 | 0.523 | 28812 | **5867** | +2.85 % | 40 % | 287 | 82 |
| Buch-Feedback-Gate (D nur wenn offenes D-Buch > −1 %) | 7732 | 6457 | 0.835 | 39 | 107 | 1.116 | 6457 | **820** | -3.51 % | 77 % | 29 | 10 |
| BTC-Richtungs-Gate (LONG nur bei 24h-Ret > 0) | 23231 | 19669 | 0.847 | 134 | 281 | 0.980 | 19669 | **2969** | -3.12 % | 80 % | 108 | 26 |
| Portfolio-Trail 10 % (kein Einzel-Trail) | 44650 | 9176 | 0.205 | 48 | 175 | 1.290 | 9176 | **5271** | -0.25 % | 33 % | 35 | 13 |
| Portfolio-Trail 15 % (kein Einzel-Trail) | 44650 | 8058 | 0.180 | 53 | 208 | 1.019 | 8058 | **4919** | -0.22 % | 33 % | 38 | 15 |

## Lesehilfe

- **Equity MaxDD** ist die Kennzahl, die der Studie fehlte: max. Rückgang der Kurve
  (realisiert + offen), in unlevered %-Punkten über das gleichgewichtete Buch.
- **Ø Buch-Mark** = zeitgemittelter Durchschnitts-Mark der offenen Positionen. Ein stark
  negativer Wert heißt: das Buch besteht strukturell aus Verlierern.
- **Buch unter Wasser** = zeitgemittelter Anteil offener Positionen im Minus.

# Trailing-Arm Buch-Gesundheit — Exit-Regeln am offenen Buch gemessen (T-2026-KYT-9050-052)

_generated 2026-07-28T17:51:40.413566+00:00 · read-only · Roster-Beine ohne ROM1 · x=10% · tf 15m · ab 2026-03-01 · Gebühr 0.10 %/Trade · 44883 Trades_

**Frage:** `tools/trailing_slot_budget.py` maß realisierte Summen und Slots. Eine Regel, die
Gewinner schließt und Verlierer hält, sieht dort gut aus und im offenen Buch schlecht —
Bot 40 hat das live bewiesen. Hier wird jede Regel an BEIDEN Seiten gemessen: realisiert
UND Zusammensetzung des offenen Buchs (Equity = realisierte Summe + offenes MTM,
gleichgewichtet, unlevered %-Punkte).

| Regel | n | Σ netto | Ø/Trade | Ø Slots | p95 | netto/Slot-Tag | Equity final | **Equity MaxDD** | netto/Ø-Slot | DD/Ø-Slot | Ø Buch-Mark | Buch unter Wasser | Ø L offen | Ø S offen |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Hold (Fleet-Exit, SL/TP/Timeout) | 44883 | 58141 | 1.295 | 1008 | 1635 | 0.385 | 58141 | **20076** | 58 | 19.9 | +2.30 % | 44 % | 760 | 248 |
| Trail act=2 % (Bot 40 heute) | 44883 | 38160 | 0.850 | 261 | 495 | 0.976 | 38160 | **4377** | 146 | 16.8 | -2.73 % | 78 % | 219 | 42 |
| Trail act=5 % | 44883 | 50543 | 1.126 | 539 | 918 | 0.625 | 50543 | **9786** | 94 | 18.1 | -1.97 % | 66 % | 433 | 106 |
| Trail act=10 % | 44883 | 62626 | 1.395 | 771 | 1358 | 0.542 | 62626 | **14856** | 81 | 19.3 | -0.91 % | 55 % | 610 | 161 |
| Trail act=2 %, x=20 % (langsamer closen) | 44883 | 32243 | 0.718 | 262 | 496 | 0.822 | 32243 | **4887** | 123 | 18.7 | -2.71 % | 78 % | 220 | 42 |
| Trail act=2 %, x=30 % | 44883 | 27464 | 0.612 | 264 | 498 | 0.694 | 27464 | **5627** | 104 | 21.3 | -2.62 % | 77 % | 222 | 42 |
| Trail act=10 %, x=20 % | 44883 | 55912 | 1.246 | 776 | 1369 | 0.481 | 55912 | **15337** | 72 | 19.8 | -0.83 % | 54 % | 612 | 163 |
| Trail 2 % + Zeit-Stop 24 h | 44883 | 27448 | 0.612 | 133 | 268 | 1.378 | 27448 | **3015** | 207 | 22.7 | -1.17 % | 62 % | 108 | 25 |
| Trail 2 % + Zeit-Stop 48 h | 44883 | 32208 | 0.718 | 179 | 358 | 1.197 | 32208 | **4316** | 180 | 24.1 | -1.58 % | 68 % | 148 | 31 |
| Trail 2 % + Zeit-Stop 72 h | 44883 | 34500 | 0.769 | 204 | 412 | 1.130 | 34500 | **4237** | 169 | 20.8 | -1.85 % | 70 % | 169 | 34 |
| Trail 2 % + Hard-Stop −2 % | 44883 | 15645 | 0.349 | 81 | 173 | 1.283 | 15645 | **2216** | 192 | 27.2 | +0.00 % | 41 % | 67 | 15 |
| Trail 2 % nur SHORT (LONG hält) | 44883 | 52055 | 1.160 | 802 | 1562 | 0.433 | 52055 | **20736** | 65 | 25.9 | +0.91 % | 50 % | 760 | 42 |
| Trail 2 % nur LONG (SHORT hält) | 44883 | 44245 | 0.986 | 468 | 889 | 0.631 | 44245 | **5872** | 95 | 12.6 | +1.15 % | 56 % | 219 | 248 |
| Trail 2 %, 50 % Teilschließung | 44883 | 48150 | 1.073 | 634 | 1021 | 0.506 | 48150 | **12166** | 76 | 19.2 | +1.48 % | 50 % | 490 | 145 |
| Trail 2 % + Exposure-Cap ±50 | 20144 | 21294 | 1.057 | 100 | 198 | 1.419 | 21294 | **683** | 213 | 6.8 | -2.84 % | 76 % | 65 | 35 |
| Trail 2 % + Exposure-Cap ±100 | 25696 | 25240 | 0.982 | 137 | 251 | 1.230 | 25240 | **1181** | 184 | 8.6 | -2.83 % | 78 % | 99 | 38 |
| Trail 2 % + Zeit-Stop 24 h + Cap ±50 | 24376 | 18854 | 0.773 | 68 | 130 | 1.859 | 18854 | **588** | 279 | 8.7 | -1.22 % | 62 % | 47 | 21 |
| SL-Nachzug: Breakeven ab +2 % (kein Trail) | 44883 | 16154 | 0.360 | 427 | 724 | 0.252 | 16154 | **8996** | 38 | 21.1 | +1.85 % | 47 % | 336 | 90 |
| Breakeven ab +2 % + Zeit-Stop 24 h | 44883 | 7522 | 0.168 | 261 | 472 | 0.192 | 7522 | **7708** | 29 | 29.5 | +4.32 % | 32 % | 198 | 64 |
| Breakeven 2 % + Zeit-Stop 24 h + Cap ±50 | 21801 | 4254 | 0.195 | 127 | 229 | 0.224 | 4254 | **1562** | 34 | 12.3 | +4.80 % | 30 % | 74 | 52 |
| Breakeven 2 % + Zeit-Stop 24 h + Cap ±100 | 27099 | 4066 | 0.150 | 160 | 264 | 0.170 | 4066 | **2100** | 25 | 13.1 | +4.59 % | 31 % | 104 | 56 |
| Breakeven ab +5 % + Zeit-Stop 24 h | 44883 | 7058 | 0.157 | 300 | 539 | 0.157 | 7058 | **8592** | 24 | 28.7 | +4.48 % | 34 % | 221 | 79 |
| Hold unter hartem 500-Slot-Cap | 21738 | 26857 | 1.236 | 476 | 500 | 0.376 | 26857 | **6933** | 56 | 14.6 | +2.70 % | 42 % | 360 | 116 |
| Breakeven 2 % + Zeit-Stop 24 h @ 500-Cap | 41641 | 3284 | 0.079 | 245 | 428 | 0.089 | 3284 | **5769** | 13 | 23.5 | +4.30 % | 32 % | 183 | 62 |
| Breakeven 5 % + Zeit-Stop 24 h @ 500-Cap | 40975 | 2634 | 0.064 | 276 | 464 | 0.064 | 2634 | **6235** | 10 | 22.6 | +4.48 % | 34 % | 201 | 75 |
| Hold @ 1000 (2 Channels, least-loaded) | 36165 | 43242 | 1.196 | 812 | 1000 | 0.355 | 43242 | **14193** | 53 | 17.5 | +2.33 % | 44 % | 612 | 200 |
| Breakeven 5 % + Zeit-Stop 24 h @ 1000 (2 Channels) | 43613 | 3907 | 0.090 | 294 | 532 | 0.089 | 3907 | **7266** | 13 | 24.7 | +4.47 % | 34 % | 215 | 79 |
| Hold @ 1500 (3 Channels) | 42951 | 53151 | 1.238 | 975 | 1477 | 0.364 | 53151 | **18890** | 55 | 19.4 | +2.29 % | 44 % | 729 | 246 |
| Breakeven 5 % + Zeit-Stop 24 h @ 1500 (3 Channels) | 44156 | 5449 | 0.123 | 296 | 539 | 0.123 | 5449 | **7852** | 18 | 26.5 | +4.48 % | 34 % | 218 | 79 |
| Buch-Feedback-Gate (D nur wenn offenes D-Buch > −1 %) | 8001 | 6404 | 0.800 | 41 | 117 | 1.044 | 6404 | **820** | 156 | 20.0 | -3.51 % | 78 % | 31 | 10 |
| BTC-Richtungs-Gate (LONG nur bei 24h-Ret > 0) | 23351 | 19638 | 0.841 | 135 | 281 | 0.973 | 19638 | **2969** | 146 | 22.1 | -3.12 % | 80 % | 109 | 26 |
| Mover-Gate: Coin |24h| > 30 % ignorieren (Trail a2) | 43473 | 33905 | 0.780 | 260 | 495 | 0.870 | 33905 | **4503** | 130 | 17.3 | -2.71 % | 78 % | 219 | 41 |
| Mover-Gate: Coin |24h| > 50 % ignorieren (Trail a2) | 44423 | 36666 | 0.825 | 261 | 495 | 0.939 | 36666 | **4370** | 141 | 16.8 | -2.73 % | 78 % | 219 | 41 |
| Chase-Gate: nur Hinterherlaufen > 20 % ignorieren | 44252 | 35859 | 0.810 | 260 | 495 | 0.919 | 35859 | **4518** | 138 | 17.4 | -2.73 % | 78 % | 219 | 42 |
| Chase-Gate: nur Hinterherlaufen > 50 % ignorieren | 44769 | 37508 | 0.838 | 261 | 495 | 0.959 | 37508 | **4420** | 144 | 16.9 | -2.73 % | 78 % | 219 | 42 |
| Portfolio-Trail 10 % (kein Einzel-Trail) | 44883 | 9361 | 0.209 | 48 | 175 | 1.313 | 9361 | **5271** | 197 | 110.8 | -0.25 % | 33 % | 35 | 13 |
| Portfolio-Trail 15 % (kein Einzel-Trail) | 44883 | 8152 | 0.182 | 53 | 207 | 1.028 | 8152 | **4919** | 154 | 93.0 | -0.21 % | 33 % | 38 | 15 |

## Lesehilfe

- **Equity MaxDD** ist die Kennzahl, die der Studie fehlte: max. Rückgang der Kurve
  (realisiert + offen), in unlevered %-Punkten über das gleichgewichtete Buch.
- **Ø Buch-Mark** = zeitgemittelter Durchschnitts-Mark der offenen Positionen. Ein stark
  negativer Wert heißt: das Buch besteht strukturell aus Verlierern.
- **Buch unter Wasser** = zeitgemittelter Anteil offener Positionen im Minus.

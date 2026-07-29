# Trailing-Arm Buch-Gesundheit — Exit-Regeln am offenen Buch gemessen (T-2026-KYT-9050-052)

_generated 2026-07-29T05:01:22.288255+00:00 · read-only · Roster-Beine ohne ROM1 · x=10% · tf 15m · ab 2026-03-01 · Gebühr 0.10 %/Trade · 45082 Trades_

**Frage:** `tools/trailing_slot_budget.py` maß realisierte Summen und Slots. Eine Regel, die
Gewinner schließt und Verlierer hält, sieht dort gut aus und im offenen Buch schlecht —
Bot 40 hat das live bewiesen. Hier wird jede Regel an BEIDEN Seiten gemessen: realisiert
UND Zusammensetzung des offenen Buchs (Equity = realisierte Summe + offenes MTM,
gleichgewichtet, unlevered %-Punkte).

| Regel | n | Σ netto | Ø/Trade | Ø Slots | p95 | netto/Slot-Tag | Equity final | **Equity MaxDD** | netto/Ø-Slot | DD/Ø-Slot | Ø Buch-Mark | Buch unter Wasser | Ø L offen | Ø S offen |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Hold (Fleet-Exit, SL/TP/Timeout) | 45082 | 57832 | 1.283 | 1008 | 1635 | 0.381 | 57832 | **20076** | 57 | 19.9 | +2.29 % | 44 % | 760 | 249 |
| Trail act=2 % (Bot 40 heute) | 45082 | 37887 | 0.840 | 262 | 497 | 0.963 | 37887 | **4377** | 145 | 16.7 | -2.73 % | 78 % | 220 | 42 |
| Trail act=5 % | 45082 | 50169 | 1.113 | 540 | 918 | 0.618 | 50169 | **9786** | 93 | 18.1 | -1.98 % | 66 % | 434 | 106 |
| Trail act=10 % | 45082 | 62212 | 1.380 | 772 | 1361 | 0.536 | 62212 | **14856** | 81 | 19.2 | -0.91 % | 55 % | 611 | 161 |
| Trail act=2 %, x=20 % (langsamer closen) | 45082 | 31959 | 0.709 | 262 | 498 | 0.810 | 31959 | **4887** | 122 | 18.6 | -2.71 % | 78 % | 220 | 42 |
| Trail act=2 %, x=30 % | 45082 | 27162 | 0.603 | 265 | 500 | 0.683 | 27162 | **5627** | 103 | 21.3 | -2.63 % | 77 % | 222 | 42 |
| Trail act=10 %, x=20 % | 45082 | 55485 | 1.231 | 776 | 1371 | 0.475 | 55485 | **15337** | 71 | 19.8 | -0.84 % | 54 % | 613 | 163 |
| Trail 2 % + Zeit-Stop 24 h | 45082 | 27392 | 0.608 | 133 | 269 | 1.368 | 27392 | **3015** | 206 | 22.6 | -1.17 % | 62 % | 108 | 25 |
| Trail 2 % + Zeit-Stop 48 h | 45082 | 32022 | 0.710 | 180 | 362 | 1.184 | 32022 | **4316** | 178 | 24.0 | -1.59 % | 68 % | 149 | 31 |
| Trail 2 % + Zeit-Stop 72 h | 45082 | 34278 | 0.760 | 204 | 413 | 1.116 | 34278 | **4237** | 168 | 20.8 | -1.86 % | 70 % | 170 | 34 |
| Trail 2 % + Hard-Stop −2 % | 45082 | 14090 | 0.312 | 82 | 174 | 1.150 | 14090 | **2319** | 173 | 28.5 | +0.00 % | 41 % | 67 | 15 |
| Trail 2 % nur SHORT (LONG hält) | 45082 | 51566 | 1.144 | 802 | 1562 | 0.428 | 51566 | **20736** | 64 | 25.9 | +0.90 % | 50 % | 760 | 42 |
| Trail 2 % nur LONG (SHORT hält) | 45082 | 44153 | 0.979 | 468 | 890 | 0.627 | 44153 | **5872** | 94 | 12.5 | +1.15 % | 56 % | 220 | 249 |
| Trail 2 %, 50 % Teilschließung | 45082 | 47860 | 1.062 | 635 | 1021 | 0.501 | 47860 | **12166** | 75 | 19.2 | +1.47 % | 50 % | 490 | 145 |
| Trail 2 % + Exposure-Cap ±50 | 20202 | 21248 | 1.052 | 100 | 198 | 1.410 | 21248 | **683** | 212 | 6.8 | -2.84 % | 76 % | 65 | 35 |
| Trail 2 % + Exposure-Cap ±100 | 25755 | 25176 | 0.978 | 137 | 251 | 1.222 | 25176 | **1181** | 184 | 8.6 | -2.84 % | 78 % | 99 | 38 |
| Trail 2 % + Zeit-Stop 24 h + Cap ±50 | 24470 | 18930 | 0.774 | 68 | 131 | 1.858 | 18930 | **588** | 279 | 8.7 | -1.22 % | 62 % | 47 | 21 |
| SL-Nachzug: Breakeven ab +2 % (kein Trail) | 45082 | 15851 | 0.352 | 428 | 726 | 0.247 | 15851 | **8996** | 37 | 21.0 | +1.83 % | 47 % | 337 | 91 |
| Breakeven ab +2 % + Zeit-Stop 24 h | 45082 | 7437 | 0.165 | 262 | 471 | 0.189 | 7437 | **7708** | 28 | 29.5 | +4.30 % | 32 % | 198 | 64 |
| Breakeven 2 % + Zeit-Stop 24 h + Cap ±50 | 21931 | 4240 | 0.193 | 127 | 231 | 0.222 | 4240 | **1562** | 33 | 12.3 | +4.79 % | 30 % | 75 | 52 |
| Breakeven 2 % + Zeit-Stop 24 h + Cap ±100 | 27237 | 4169 | 0.153 | 160 | 266 | 0.173 | 4169 | **2100** | 26 | 13.1 | +4.58 % | 31 % | 104 | 56 |
| Breakeven ab +5 % + Zeit-Stop 24 h | 45082 | 7004 | 0.155 | 300 | 539 | 0.155 | 7004 | **8592** | 23 | 28.6 | +4.48 % | 34 % | 221 | 79 |
| Hold unter hartem 500-Slot-Cap | 21791 | 26756 | 1.228 | 476 | 500 | 0.374 | 26756 | **6933** | 56 | 14.6 | +2.69 % | 42 % | 360 | 116 |
| Breakeven 2 % + Zeit-Stop 24 h @ 500-Cap | 41826 | 3201 | 0.076 | 245 | 428 | 0.087 | 3201 | **5769** | 13 | 23.5 | +4.28 % | 32 % | 183 | 62 |
| Breakeven 5 % + Zeit-Stop 24 h @ 500-Cap | 41156 | 2554 | 0.062 | 276 | 465 | 0.061 | 2554 | **6235** | 9 | 22.6 | +4.48 % | 34 % | 201 | 76 |
| Hold @ 1000 (2 Channels, least-loaded) | 36331 | 42957 | 1.182 | 812 | 1000 | 0.352 | 42957 | **14193** | 53 | 17.5 | +2.32 % | 44 % | 612 | 200 |
| Breakeven 5 % + Zeit-Stop 24 h @ 1000 (2 Channels) | 43801 | 3844 | 0.088 | 295 | 532 | 0.087 | 3844 | **7266** | 13 | 24.7 | +4.47 % | 34 % | 215 | 79 |
| Hold @ 1500 (3 Channels) | 43150 | 52842 | 1.225 | 975 | 1477 | 0.360 | 52842 | **18890** | 54 | 19.4 | +2.28 % | 44 % | 729 | 246 |
| Breakeven 5 % + Zeit-Stop 24 h @ 1500 (3 Channels) | 44355 | 5394 | 0.122 | 297 | 539 | 0.121 | 5394 | **7852** | 18 | 26.4 | +4.48 % | 34 % | 218 | 79 |
| Buch-Feedback-Gate (D nur wenn offenes D-Buch > −1 %) | 7885 | 6312 | 0.800 | 40 | 111 | 1.051 | 6312 | **820** | 158 | 20.5 | -3.51 % | 78 % | 30 | 10 |
| BTC-Richtungs-Gate (LONG nur bei 24h-Ret > 0) | 23448 | 19379 | 0.827 | 135 | 281 | 0.953 | 19379 | **2969** | 143 | 22.0 | -3.12 % | 80 % | 109 | 26 |
| Mover-Gate: Coin |24h| > 30 % ignorieren (Trail a2) | 43667 | 33631 | 0.770 | 261 | 496 | 0.858 | 33631 | **4503** | 129 | 17.3 | -2.72 % | 78 % | 220 | 41 |
| Mover-Gate: Coin |24h| > 50 % ignorieren (Trail a2) | 44619 | 36398 | 0.816 | 261 | 496 | 0.926 | 36398 | **4370** | 139 | 16.7 | -2.73 % | 78 % | 220 | 41 |
| Chase-Gate: nur Hinterherlaufen > 20 % ignorieren | 44444 | 35579 | 0.800 | 261 | 496 | 0.907 | 35579 | **4518** | 136 | 17.3 | -2.73 % | 78 % | 220 | 41 |
| Chase-Gate: nur Hinterherlaufen > 50 % ignorieren | 44968 | 37236 | 0.828 | 262 | 497 | 0.947 | 37236 | **4420** | 142 | 16.9 | -2.73 % | 78 % | 220 | 42 |
| Trail a2 + SL-Deckel −5 % unlev (−100 % @20x) | 45082 | 21862 | 0.485 | 171 | 355 | 0.850 | 21862 | **3679** | 128 | 21.5 | -0.82 % | 65 % | 145 | 26 |
| DEPLOYED (Trail+ts24+Cap50) + SL-Deckel −5 % | 24637 | 12687 | 0.515 | 59 | 117 | 1.419 | 12687 | **653** | 213 | 11.0 | -0.56 % | 57 % | 43 | 17 |
| DEPLOYED heute: Trail+ts24+Cap50 (kausal, Referenz) | 24470 | 18930 | 0.774 | 68 | 131 | 1.858 | 18930 | **588** | 279 | 8.7 | -1.22 % | 62 % | 47 | 21 |
| Portfolio-Trail 10 % (kein Einzel-Trail) | 45082 | 9315 | 0.207 | 48 | 175 | 1.302 | 9315 | **5271** | 196 | 110.8 | -0.25 % | 33 % | 35 | 13 |
| Portfolio-Trail 15 % (kein Einzel-Trail) | 45082 | 8116 | 0.180 | 53 | 206 | 1.022 | 8116 | **4919** | 154 | 93.2 | -0.21 % | 33 % | 38 | 15 |

## Lesehilfe

- **Equity MaxDD** ist die Kennzahl, die der Studie fehlte: max. Rückgang der Kurve
  (realisiert + offen), in unlevered %-Punkten über das gleichgewichtete Buch.
- **Ø Buch-Mark** = zeitgemittelter Durchschnitts-Mark der offenen Positionen. Ein stark
  negativer Wert heißt: das Buch besteht strukturell aus Verlierern.
- **Buch unter Wasser** = zeitgemittelter Anteil offener Positionen im Minus.

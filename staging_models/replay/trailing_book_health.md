# Trailing-Arm Buch-Gesundheit — Exit-Regeln am offenen Buch gemessen (T-2026-KYT-9050-052)

_generated 2026-07-28T12:02:44.294686+00:00 · read-only · Roster-Beine ohne ROM1 · x=10% · tf 15m · ab 2026-03-01 · Gebühr 0.10 %/Trade · 44748 Trades_

**Frage:** `tools/trailing_slot_budget.py` maß realisierte Summen und Slots. Eine Regel, die
Gewinner schließt und Verlierer hält, sieht dort gut aus und im offenen Buch schlecht —
Bot 40 hat das live bewiesen. Hier wird jede Regel an BEIDEN Seiten gemessen: realisiert
UND Zusammensetzung des offenen Buchs (Equity = realisierte Summe + offenes MTM,
gleichgewichtet, unlevered %-Punkte).

| Regel | n | Σ netto | Ø/Trade | Ø Slots | p95 | netto/Slot-Tag | Equity final | **Equity MaxDD** | netto/Ø-Slot | DD/Ø-Slot | Ø Buch-Mark | Buch unter Wasser | Ø L offen | Ø S offen |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Hold (Fleet-Exit, SL/TP/Timeout) | 44748 | 58072 | 1.298 | 1008 | 1635 | 0.385 | 58072 | **20076** | 58 | 19.9 | +2.30 % | 44 % | 760 | 248 |
| Trail act=2 % (Bot 40 heute) | 44748 | 38155 | 0.853 | 260 | 495 | 0.979 | 38155 | **4377** | 147 | 16.8 | -2.73 % | 78 % | 219 | 42 |
| Trail act=5 % | 44748 | 50572 | 1.130 | 539 | 918 | 0.627 | 50572 | **9786** | 94 | 18.2 | -1.97 % | 66 % | 433 | 106 |
| Trail act=10 % | 44748 | 62637 | 1.400 | 771 | 1357 | 0.543 | 62637 | **14856** | 81 | 19.3 | -0.90 % | 55 % | 610 | 160 |
| Trail act=2 %, x=20 % (langsamer closen) | 44748 | 32246 | 0.721 | 261 | 496 | 0.825 | 32246 | **4887** | 124 | 18.7 | -2.71 % | 78 % | 219 | 42 |
| Trail act=2 %, x=30 % | 44748 | 27483 | 0.614 | 264 | 496 | 0.697 | 27483 | **5627** | 104 | 21.4 | -2.62 % | 77 % | 221 | 42 |
| Trail act=10 %, x=20 % | 44748 | 55924 | 1.250 | 775 | 1368 | 0.482 | 55924 | **15337** | 72 | 19.8 | -0.83 % | 54 % | 612 | 163 |
| Trail 2 % + Zeit-Stop 24 h | 44748 | 27433 | 0.613 | 133 | 267 | 1.382 | 27433 | **3015** | 207 | 22.7 | -1.17 % | 62 % | 108 | 25 |
| Trail 2 % + Zeit-Stop 48 h | 44748 | 32195 | 0.720 | 179 | 358 | 1.201 | 32195 | **4316** | 180 | 24.1 | -1.58 % | 68 % | 148 | 31 |
| Trail 2 % + Zeit-Stop 72 h | 44748 | 34487 | 0.771 | 203 | 410 | 1.133 | 34487 | **4237** | 170 | 20.8 | -1.85 % | 70 % | 169 | 34 |
| Trail 2 % + Hard-Stop −2 % | 44748 | 15625 | 0.349 | 81 | 173 | 1.285 | 15625 | **2216** | 192 | 27.3 | +0.00 % | 41 % | 66 | 15 |
| Trail 2 % nur SHORT (LONG hält) | 44748 | 52094 | 1.164 | 802 | 1563 | 0.434 | 52094 | **20736** | 65 | 25.9 | +0.91 % | 50 % | 760 | 42 |
| Trail 2 % nur LONG (SHORT hält) | 44748 | 44132 | 0.986 | 466 | 889 | 0.632 | 44132 | **5872** | 95 | 12.6 | +1.16 % | 56 % | 219 | 248 |
| Trail 2 %, 50 % Teilschließung | 44748 | 48113 | 1.075 | 634 | 1021 | 0.507 | 48113 | **12166** | 76 | 19.2 | +1.48 % | 50 % | 489 | 145 |
| Trail 2 % + Exposure-Cap ±50 | 20063 | 21160 | 1.055 | 100 | 197 | 1.415 | 21160 | **683** | 212 | 6.8 | -2.83 % | 76 % | 65 | 35 |
| Trail 2 % + Exposure-Cap ±100 | 25644 | 25140 | 0.980 | 137 | 250 | 1.228 | 25140 | **1181** | 184 | 8.6 | -2.83 % | 78 % | 99 | 38 |
| Trail 2 % + Zeit-Stop 24 h + Cap ±50 | 24285 | 18760 | 0.772 | 68 | 130 | 1.856 | 18760 | **588** | 278 | 8.7 | -1.22 % | 62 % | 47 | 21 |
| SL-Nachzug: Breakeven ab +2 % (kein Trail) | 44748 | 16034 | 0.358 | 426 | 724 | 0.251 | 16034 | **8996** | 38 | 21.1 | +1.84 % | 47 % | 336 | 90 |
| Breakeven ab +2 % + Zeit-Stop 24 h | 44748 | 28844 | 0.645 | 369 | 661 | 0.522 | 28844 | **5867** | 78 | 15.9 | +2.85 % | 40 % | 286 | 83 |
| Breakeven 2 % + Zeit-Stop 24 h + Cap ±50 | 20027 | 8956 | 0.447 | 152 | 263 | 0.393 | 8956 | **1005** | 59 | 6.6 | +3.44 % | 38 % | 89 | 63 |
| Breakeven 2 % + Zeit-Stop 24 h + Cap ±100 | 24554 | 11695 | 0.476 | 192 | 318 | 0.406 | 11695 | **1266** | 61 | 6.6 | +3.18 % | 40 % | 124 | 68 |
| Breakeven ab +5 % + Zeit-Stop 24 h | 44748 | 58994 | 1.318 | 583 | 1083 | 0.677 | 58994 | **6989** | 101 | 12.0 | +3.26 % | 36 % | 442 | 141 |
| Hold unter hartem 500-Slot-Cap | 21725 | 26747 | 1.231 | 476 | 500 | 0.376 | 26747 | **6933** | 56 | 14.6 | +2.71 % | 42 % | 360 | 116 |
| Breakeven 2 % + Zeit-Stop 24 h @ 500-Cap | 39363 | 21779 | 0.553 | 327 | 500 | 0.445 | 21779 | **4324** | 67 | 13.2 | +2.87 % | 40 % | 249 | 77 |
| Breakeven 5 % + Zeit-Stop 24 h @ 500-Cap | 32372 | 34469 | 1.065 | 408 | 500 | 0.565 | 34469 | **4124** | 85 | 10.1 | +3.36 % | 36 % | 300 | 108 |
| Hold @ 1000 (2 Channels, least-loaded) | 36060 | 43167 | 1.197 | 812 | 1000 | 0.355 | 43167 | **14193** | 53 | 17.5 | +2.33 % | 44 % | 612 | 200 |
| Breakeven 5 % + Zeit-Stop 24 h @ 1000 (2 Channels) | 42456 | 53028 | 1.249 | 560 | 975 | 0.633 | 53028 | **5676** | 95 | 10.1 | +3.27 % | 36 % | 420 | 140 |
| Buch-Feedback-Gate (D nur wenn offenes D-Buch > −1 %) | 7942 | 6278 | 0.790 | 41 | 117 | 1.027 | 6278 | **820** | 154 | 20.1 | -3.52 % | 78 % | 31 | 10 |
| BTC-Richtungs-Gate (LONG nur bei 24h-Ret > 0) | 23276 | 19697 | 0.846 | 134 | 280 | 0.980 | 19697 | **2969** | 147 | 22.1 | -3.12 % | 80 % | 108 | 26 |
| Portfolio-Trail 10 % (kein Einzel-Trail) | 44748 | 9304 | 0.208 | 48 | 175 | 1.307 | 9304 | **5271** | 196 | 110.8 | -0.25 % | 33 % | 35 | 13 |
| Portfolio-Trail 15 % (kein Einzel-Trail) | 44748 | 8145 | 0.182 | 53 | 207 | 1.028 | 8145 | **4919** | 154 | 93.0 | -0.21 % | 33 % | 38 | 15 |

## Lesehilfe

- **Equity MaxDD** ist die Kennzahl, die der Studie fehlte: max. Rückgang der Kurve
  (realisiert + offen), in unlevered %-Punkten über das gleichgewichtete Buch.
- **Ø Buch-Mark** = zeitgemittelter Durchschnitts-Mark der offenen Positionen. Ein stark
  negativer Wert heißt: das Buch besteht strukturell aus Verlierern.
- **Buch unter Wasser** = zeitgemittelter Anteil offener Positionen im Minus.

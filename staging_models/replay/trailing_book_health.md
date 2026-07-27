# Trailing-Arm Buch-Gesundheit — Exit-Regeln am offenen Buch gemessen (T-2026-KYT-9050-052)

_generated 2026-07-27T17:00:03.585598+00:00 · read-only · Roster-Beine ohne ROM1 · x=10% · tf 15m · ab 2026-03-01 · Gebühr 0.10 %/Trade · 44144 Trades_

**Frage:** `tools/trailing_slot_budget.py` maß realisierte Summen und Slots. Eine Regel, die
Gewinner schließt und Verlierer hält, sieht dort gut aus und im offenen Buch schlecht —
Bot 40 hat das live bewiesen. Hier wird jede Regel an BEIDEN Seiten gemessen: realisiert
UND Zusammensetzung des offenen Buchs (Equity = realisierte Summe + offenes MTM,
gleichgewichtet, unlevered %-Punkte).

| Regel | n | Σ netto | Ø/Trade | Ø Slots | p95 | netto/Slot-Tag | Equity final | **Equity MaxDD** | Ø Buch-Mark | Buch unter Wasser | Ø L offen | Ø S offen |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Hold (Fleet-Exit, SL/TP/Timeout) | 44144 | 59574 | 1.349 | 1001 | 1636 | 0.400 | 59574 | **20076** | +2.34 % | 44 % | 755 | 246 |
| Trail act=2 % (Bot 40 heute) | 44144 | 39116 | 0.886 | 256 | 490 | 1.028 | 39116 | **4377** | -2.73 % | 78 % | 214 | 41 |
| Trail act=5 % | 44144 | 51866 | 1.175 | 532 | 917 | 0.654 | 51866 | **9786** | -1.96 % | 66 % | 427 | 105 |
| Trail act=10 % | 44144 | 64088 | 1.452 | 764 | 1357 | 0.564 | 64088 | **14856** | -0.89 % | 54 % | 605 | 159 |
| Trail 2 % + Zeit-Stop 24 h | 44144 | 27966 | 0.633 | 131 | 267 | 1.431 | 27966 | **3015** | -1.17 % | 61 % | 107 | 24 |
| Trail 2 % + Zeit-Stop 48 h | 44144 | 32846 | 0.744 | 177 | 354 | 1.247 | 32846 | **4316** | -1.58 % | 67 % | 146 | 31 |
| Trail 2 % + Zeit-Stop 72 h | 44144 | 35158 | 0.796 | 200 | 406 | 1.178 | 35158 | **4237** | -1.85 % | 70 % | 166 | 34 |
| Trail 2 % + Hard-Stop −2 % | 44144 | 15809 | 0.358 | 80 | 170 | 1.328 | 15809 | **2216** | +0.00 % | 41 % | 65 | 15 |
| Trail 2 % nur SHORT (LONG hält) | 44144 | 53700 | 1.216 | 796 | 1564 | 0.453 | 53700 | **20736** | +0.93 % | 50 % | 755 | 41 |
| Trail 2 % nur LONG (SHORT hält) | 44144 | 44989 | 1.019 | 460 | 889 | 0.656 | 44989 | **5872** | +1.19 % | 56 % | 214 | 246 |
| Trail 2 %, 50 % Teilschließung | 44144 | 49345 | 1.118 | 628 | 1019 | 0.528 | 49345 | **12166** | +1.51 % | 50 % | 485 | 144 |
| Trail 2 % + Exposure-Cap ±50 | 19890 | 21391 | 1.075 | 99 | 192 | 1.448 | 21391 | **683** | -2.84 % | 76 % | 65 | 34 |
| Trail 2 % + Exposure-Cap ±100 | 25495 | 25409 | 0.997 | 136 | 247 | 1.257 | 25409 | **1181** | -2.83 % | 77 % | 98 | 38 |
| Portfolio-Trail 10 % (kein Einzel-Trail) | 44144 | 9268 | 0.210 | 48 | 174 | 1.308 | 9268 | **5271** | -0.25 % | 33 % | 35 | 13 |
| Portfolio-Trail 15 % (kein Einzel-Trail) | 44144 | 8002 | 0.181 | 53 | 207 | 1.015 | 8002 | **4919** | -0.21 % | 33 % | 38 | 15 |

## Lesehilfe

- **Equity MaxDD** ist die Kennzahl, die der Studie fehlte: max. Rückgang der Kurve
  (realisiert + offen), in unlevered %-Punkten über das gleichgewichtete Buch.
- **Ø Buch-Mark** = zeitgemittelter Durchschnitts-Mark der offenen Positionen. Ein stark
  negativer Wert heißt: das Buch besteht strukturell aus Verlierern.
- **Buch unter Wasser** = zeitgemittelter Anteil offener Positionen im Minus.

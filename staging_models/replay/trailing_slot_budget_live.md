# Trailing-Bot Slot-Budget — welche Beine in den neuen Channel? (T-2026-KYT-9050-042)

_generated 2026-07-26T14:21:06.702998+00:00 · read-only · nur LIVE-Beine · trailing X=10 % · tf 15m · 20x · ab 2026-03-01 · Gebühr 0.10 %/Trade_

**Frage:** Cornix deckelt einen Channel bei **500 gleichzeitig offenen Trades**. Maßstab ist der **Netto-Ertrag pro belegtem Slot-Tag**, nicht der per-Trade-Sharpe. Bewertet pro **(Tag, Richtung)**, weil `shadow_gate` pro Bein schaltet.

## Aktivierungsschwelle — der entscheidende Parameter

Ein skalenfreier Trail (`schließe bei X % Rückgabe vom Peak`) feuert auch auf einem 0,5-%-Peak und macht die Fleet zum Micro-Scalper. `act` = geforderter Peak (unlev %), bevor der Trail scharf wird. `unter Gebühr` = Anteil Trades, deren Trailing-Ertrag die 0,10-%-Gebühr nicht deckt.

| act % | Σ netto (alle Live-Beine) | Ø/Trade netto | unter Gebühr | Median Haltedauer h | Ø Slots (alle) | netto/Slot | **Füllung 500 netto** | Beine |
|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 0 | 36416 | 0.584 | 18 % | 0.4 | 50 | 731 | **36442** | 36 |
| 1 | 46034 | 0.738 | 15 % | 2.0 | 189 | 244 | **46064** | 37 |
| 2 | 51621 | 0.828 | 25 % | 4.6 | 340 | 152 | **51639** | 37 |
| 3 | 55662 | 0.892 | 32 % | 8.1 | 472 | 118 | **55753** | 36 |
| 5 | 63145 | 1.012 | 42 % | 12.3 | 674 | 94 | **54692** | 30 |
| 10 | 73897 | 1.185 | 52 % | 15.8 | 948 | 78 | **50980** | 23 |

_Referenz hold (ohne Trailing), netto: **64478** · Ø Slots 1221 · Median Haltedauer 27.0 h_

## Beine bei act = 2 %

| Bein | n | Ø Slots hold → trail | p95 | Haltedauer h hold → trail | Σ netto hold → trail | **netto/Slot-Tag** | unter Gebühr |
|---|--:|--:|--:|--:|--:|--:|--:|
| MIS2-72h SHORT | 99 | 1 → **0** | 0 | 44 → 0.4 | 159 → 620 | **311.003** | 2 % |
| MIS2-168h SHORT | 45 | 1 → **0** | 0 | 28 → 0.4 | -10 → 253 | **309.752** | 2 % |
| MIS2-24h SHORT | 112 | 1 → **0** | 0 | 27 → 0.4 | 224 → 674 | **275.374** | 5 % |
| ABR1 LONG | 59 | 0 → **0** | 0 | 2 → 0.4 | 307 → 134 | **12.954** | 27 % |
| MIS1-8h SHORT | 369 | 5 → **1** | 2 | 8 → 0.5 | 488 → 694 | **6.062** | 20 % |
| TD2_4H SHORT ⚠dünn | 3 | 0 → **0** | 0 | 51 → 9.9 | 18 → 7 | **5.442** | 0 % |
| XSM1 LONG | 34 | 0 → **0** | 0 | 11 → 2.6 | 49 → 75 | **4.708** | 26 % |
| ATB2 LONG ⚠dünn | 18 | 0 → **0** | 0 | 1 → 1.1 | 16 → 13 | **4.639** | 56 % |
| AIM2 SHORT | 1311 | 18 → **4** | 26 | 29 → 2.5 | 1369 → 2324 | **4.294** | 15 % |
| ABR1 SHORT | 56 | 2 → **0** | 1 | 14 → 0.7 | 66 → 60 | **4.135** | 25 % |
| RUB1 SHORT | 1509 | 25 → **3** | 8 | 18 → 1.0 | 1506 → 1504 | **3.943** | 20 % |
| MIS1-24h LONG | 191 | 2 → **0** | 2 | 19 → 1.4 | 177 → 164 | **2.875** | 22 % |
| ROM1 LONG | 4625 | 25 → **11** | 50 | 7 → 3.7 | 17 → 4544 | **2.791** | 39 % |
| SKW1 SHORT | 31 | 0 → **0** | 0 | 5 → 3.7 | -28 → 16 | **2.380** | 29 % |
| RUB1 LONG | 1058 | 30 → **4** | 11 | 44 → 2.6 | 3169 → 1290 | **2.346** | 19 % |
| EPD1 SHORT | 4650 | 143 → **25** | 117 | 69 → 4.6 | 16239 → 8046 | **2.238** | 12 % |
| SRA2 SHORT | 300 | 2 → **1** | 12 | 10 → 5.5 | 264 → 331 | **1.852** | 19 % |
| ROM1 SHORT | 5918 | 66 → **22** | 72 | 10 → 3.9 | 1672 → 5790 | **1.837** | 31 % |
| UFI1 SHORT | 55 | 4 → **0** | 2 | 164 → 1.2 | 277 → 114 | **1.770** | 11 % |
| SKW1 LONG | 32 | 0 → **0** | 0 | 14 → 5.1 | 68 → 27 | **1.591** | 22 % |
| AIM2 LONG | 1387 | 15 → **7** | 61 | 22 → 5.6 | 82 → 1382 | **1.409** | 26 % |
| MIS1-72h LONG | 12461 | 282 → **74** | 184 | 41 → 6.6 | 15461 → 10427 | **0.959** | 20 % |
| TD_4H LONG | 429 | 12 → **3** | 10 | 52 → 8.2 | 524 → 341 | **0.885** | 20 % |
| TD_4H SHORT | 280 | 10 → **1** | 5 | 63 → 4.4 | 372 → 138 | **0.748** | 20 % |
| TD_1H LONG | 1425 | 39 → **8** | 28 | 57 → 8.7 | 2094 → 913 | **0.745** | 21 % |
| QM_1H LONG | 1602 | 6 → **4** | 22 | 7 → 4.6 | 511 → 441 | **0.686** | 46 % |
| MAX1 SHORT | 188 | 1 → **0** | 4 | 7 → 4.2 | 70 → 50 | **0.656** | 30 % |
| TD_1H SHORT | 1138 | 32 → **5** | 22 | 55 → 5.9 | 402 → 476 | **0.607** | 23 % |
| SRA2 LONG | 452 | 4 → **3** | 29 | 8 → 4.7 | 194 → 228 | **0.575** | 28 % |
| BB_4H LONG | 1343 | 41 → **12** | 34 | 52 → 12.0 | 1533 → 981 | **0.568** | 22 % |
| BR1H LONG | 3530 | 91 → **26** | 96 | 41 → 8.8 | 4536 → 2149 | **0.560** | 22 % |
| BR4H LONG | 981 | 24 → **7** | 23 | 40 → 9.4 | 988 → 580 | **0.549** | 23 % |
| MIS1-168h LONG | 7279 | 192 → **54** | 132 | 50 → 9.9 | 8776 → 4319 | **0.541** | 23 % |
| ATS2 SHORT ⚠dünn | 7 | 0 → **0** | 0 | 64 → 30.7 | 9 → 5 | **0.506** | 14 % |
| BB_1H LONG | 1782 | 51 → **16** | 55 | 50 → 11.2 | 2197 → 1009 | **0.423** | 24 % |
| QM_4H LONG | 155 | 3 → **1** | 6 | 29 → 8.4 | 163 → 58 | **0.352** | 26 % |
| BR2H LONG | 2262 | 51 → **17** | 52 | 35 → 10.7 | 1330 → 797 | **0.309** | 27 % |
| EPD3 LONG | 3589 | 25 → **18** | 221 | 8 → 4.6 | -383 → 602 | **0.229** | 37 % |
| ATS2 LONG | 840 | 10 → **8** | 89 | 23 → 13.2 | -140 → 62 | **0.054** | 31 % |
| TSM1 SHORT | 727 | 5 → **4** | 44 | 15 → 9.4 | -253 → 26 | **0.048** | 32 % |
| TD2_4H LONG ⚠dünn | 2 | 0 → **0** | 0 | 29 → 14.5 | 0 → -1 | **-0.594** | 50 % |
| MAX2 LONG ⚠dünn | 5 | 0 → **0** | 0 | 20 → 17.4 | -4 → -7 | **-1.966** | 40 % |
| XSR1 SHORT | 34 | 0 → **0** | 0 | 9 → 3.9 | -29 → -34 | **-2.966** | 35 % |

## Kanal-Füllung (greedy nach Netto-Dichte; Gleichzeitigkeit exakt gerechnet)

### Budget 500 nach **mean**

- **Aufgenommen (37):** MIS2-72h SHORT, MIS2-168h SHORT, MIS2-24h SHORT, ABR1 LONG, MIS1-8h SHORT, XSM1 LONG, AIM2 SHORT, ABR1 SHORT, RUB1 SHORT, MIS1-24h LONG, ROM1 LONG, SKW1 SHORT, RUB1 LONG, EPD1 SHORT, SRA2 SHORT, ROM1 SHORT, UFI1 SHORT, SKW1 LONG, AIM2 LONG, MIS1-72h LONG, TD_4H LONG, TD_4H SHORT, TD_1H LONG, QM_1H LONG, MAX1 SHORT, TD_1H SHORT, SRA2 LONG, BB_4H LONG, BR1H LONG, BR4H LONG, MIS1-168h LONG, BB_1H LONG, QM_4H LONG, BR2H LONG, EPD3 LONG, ATS2 LONG, TSM1 SHORT
- Belegung: Ø 340.0 · p95 715.0 · max 2018
- Σ netto: **51639 %** (62304 Trades)
- Abgelehnt: —

### Budget 500 nach **p95**

- **Aufgenommen (33):** MIS2-72h SHORT, MIS2-168h SHORT, MIS2-24h SHORT, ABR1 LONG, MIS1-8h SHORT, XSM1 LONG, AIM2 SHORT, ABR1 SHORT, RUB1 SHORT, MIS1-24h LONG, ROM1 LONG, SKW1 SHORT, RUB1 LONG, EPD1 SHORT, SRA2 SHORT, ROM1 SHORT, UFI1 SHORT, SKW1 LONG, AIM2 LONG, MIS1-72h LONG, TD_4H LONG, TD_4H SHORT, TD_1H LONG, QM_1H LONG, MAX1 SHORT, TD_1H SHORT, SRA2 LONG, BB_4H LONG, BR1H LONG, BR4H LONG, MIS1-168h LONG, QM_4H LONG, ATS2 LONG
- Belegung: Ø 284.6 · p95 498.0 · max 2001
- Σ netto: **49204 %** (53944 Trades)
- Abgelehnt: BB_1H LONG (518), BR2H LONG (528), EPD3 LONG (581), TSM1 SHORT (525)

## Ehrliche Grenzen

- **Registerstand ist zeit-variabel** — der heutige `leg_status` liegt auf der ganzen Historie.
- **15m-Auflösung.** Der Trail wird auf Kerzen-Extremen ausgewertet, mit strikt vorherigem Peak (keine Gleich-Kerzen-Trigger). Ein 5m/10s-Resolver (T-035-Harness) ist die nächste Verschärfung; die DCA-treue Bestätigung der Finalisten steht noch aus.
- **Wert = Σ unlevered %-Bewegung minus Gebühr**, Gleichgewichtung, kein Compounding: als Dichte-Maß robust, als absolute Rendite nicht wörtlich.
- **Slippage ist NICHT modelliert.** Bei niedriger Aktivierungsschwelle sind die Exits klein und zahlreich — dort frisst Slippage überproportional. Das spricht zusätzlich gegen act=0.
- Greedy ist nicht beweisbar optimal (Knapsack).

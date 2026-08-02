# v2-Whitelist: Parameter-Sensitivität gegen realisierte ROM1-Beine

- Fenster: `2026-07-11 00:00:00` → `jetzt`  (22.7 Tage)
- Gate-Events: 23492 (davon geforwardet: 8607)
- ROM1-Legs angehängt: 97.2%
- Erzeugt: 2026-08-02 17:12:37.627172+00:00  |  CPU bei Start: 100.0

## Warum das KEIN Backtest ist (gemessen, nicht behauptet)

`bot_regime_performance` trägt **0** Zellen mit mehr als einer Zeile → reiner Snapshot, keine Historie. `last_computed` von `2026-04-18 18:17:49.890235` bis `2026-08-02 17:06:51.539034`.

Die Zellstatistiken, auf denen das Gate zum Zeitpunkt eines vergangenen Events entschieden hat, existieren nicht mehr. Jede Neu-Entscheidung unten benutzt **heutige** Statistiken auf **damaligem** Verkehr und vermischt damit zwei Effekte: was die Parameter tun, und wie die Zellen seither gedriftet sind. Belastbar bleiben die *Form* der Parameter-Antwort und das *Vorzeichen* der geblockten Beine. Ein Flip lässt sich daraus nicht rechtfertigen — das kann nur ein Live-Shadow-A/B (wie T-031).

## Referenz: was v1 tatsächlich durchgelassen hat

- 8367 von 8607 geforwardeten Events mit ROM1-Leg  | Σ move 108.1 %  | Ø 0.0329 %/Trade

## Parameter-Gitter

| z | k | break-even | behalten | Durchlass | Ø behalten % | Ø geblockt % | Σ geblockt % | Lesart |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1.64 **(heute)** | 25.0 | 0.1 | 583/8607 | 6.77 % | 0.2124 | 0.0126 | 37.2 | entfernt GEWINNER |
| 1.64 | 25.0 | 0.0 | 793/8607 | 9.21 % | 0.0928 | 0.0251 | 72.8 | entfernt GEWINNER |
| 1.64 | 25.0 | -0.1 | 1355/8607 | 15.74 % | -0.3504 | 0.1375 | 354.8 | entfernt GEWINNER |
| 1.64 | 10.0 | 0.1 | 573/8607 | 6.66 % | 0.1744 | 0.0167 | 49.2 | entfernt GEWINNER |
| 1.64 | 10.0 | 0.0 | 796/8607 | 9.25 % | 0.0967 | 0.0245 | 71.0 | entfernt GEWINNER |
| 1.64 | 10.0 | -0.1 | 1309/8607 | 15.21 % | -0.4473 | 0.1553 | 406.5 | entfernt GEWINNER |
| 1.64 | 5.0 | 0.1 | 574/8607 | 6.67 % | 0.1784 | 0.0162 | 47.7 | entfernt GEWINNER |
| 1.64 | 5.0 | 0.0 | 797/8607 | 9.26 % | 0.1004 | 0.024 | 69.5 | entfernt GEWINNER |
| 1.64 | 5.0 | -0.1 | 1311/8607 | 15.23 % | -0.4444 | 0.1548 | 405.0 | entfernt GEWINNER |
| 1.28 | 25.0 | 0.1 | 662/8607 | 7.69 % | 0.2931 | -0.0023 | -6.7 | entfernt Verlierer |
| 1.28 | 25.0 | 0.0 | 882/8607 | 10.25 % | 0.2228 | 0.0028 | 7.9 | entfernt GEWINNER |
| 1.28 | 25.0 | -0.1 | 1478/8607 | 17.17 % | -0.2788 | 0.1225 | 312.5 | entfernt GEWINNER |
| 1.28 | 10.0 | 0.1 | 663/8607 | 7.7 % | 0.2592 | 0.0022 | 6.3 | entfernt GEWINNER |
| 1.28 | 10.0 | 0.0 | 887/8607 | 10.31 % | 0.2059 | 0.0052 | 14.7 | entfernt GEWINNER |
| 1.28 | 10.0 | -0.1 | 1479/8607 | 17.18 % | -0.2962 | 0.1276 | 325.5 | entfernt GEWINNER |
| 1.28 | 5.0 | 0.1 | 661/8607 | 7.68 % | 0.2841 | -0.001 | -2.9 | entfernt Verlierer |
| 1.28 | 5.0 | 0.0 | 874/8607 | 10.15 % | 0.1985 | 0.0072 | 20.4 | entfernt GEWINNER |
| 1.28 | 5.0 | -0.1 | 1483/8607 | 17.23 % | -0.2925 | 0.127 | 323.7 | entfernt GEWINNER |
| 1.04 | 25.0 | 0.1 | 695/8607 | 8.07 % | 0.3588 | -0.0145 | -41.5 | entfernt Verlierer |
| 1.04 | 25.0 | 0.0 | 1500/8607 | 17.43 % | -0.0642 | 0.0627 | 157.7 | entfernt GEWINNER |
| 1.04 | 25.0 | -0.1 | 2141/8607 | 24.88 % | -0.1063 | 0.0903 | 210.1 | entfernt GEWINNER |
| 1.04 | 10.0 | 0.1 | 706/8607 | 8.2 % | 0.3634 | -0.0157 | -44.9 | entfernt Verlierer |
| 1.04 | 10.0 | 0.0 | 1419/8607 | 16.49 % | -0.1488 | 0.0833 | 214.2 | entfernt GEWINNER |
| 1.04 | 10.0 | -0.1 | 2114/8607 | 24.56 % | -0.14 | 0.1015 | 238.7 | entfernt GEWINNER |
| 1.04 | 5.0 | 0.1 | 711/8607 | 8.26 % | 0.3242 | -0.0101 | -29.0 | entfernt Verlierer |
| 1.04 | 5.0 | 0.0 | 1422/8607 | 16.52 % | -0.1733 | 0.0904 | 232.2 | entfernt GEWINNER |
| 1.04 | 5.0 | -0.1 | 2072/8607 | 24.07 % | -0.1588 | 0.1072 | 253.8 | entfernt GEWINNER |
| 0.67 | 25.0 | 0.1 | 1184/8607 | 13.76 % | 0.4492 | -0.0521 | -142.0 | entfernt Verlierer |
| 0.67 | 25.0 | 0.0 | 2016/8607 | 23.42 % | 0.0627 | 0.0212 | 50.0 | entfernt GEWINNER |
| 0.67 | 25.0 | -0.1 | 3488/8607 | 40.53 % | -0.1206 | 0.1674 | 293.2 | entfernt GEWINNER |
| 0.67 | 10.0 | 0.1 | 1195/8607 | 13.88 % | 0.5582 | -0.0764 | -207.8 | entfernt Verlierer |
| 0.67 | 10.0 | 0.0 | 1994/8607 | 23.17 % | 0.1156 | 0.0013 | 3.1 | entfernt GEWINNER |
| 0.67 | 10.0 | -0.1 | 3489/8607 | 40.54 % | -0.1222 | 0.1694 | 296.0 | entfernt GEWINNER |
| 0.67 | 5.0 | 0.1 | 1202/8607 | 13.97 % | 0.5398 | -0.0737 | -200.1 | entfernt Verlierer |
| 0.67 | 5.0 | 0.0 | 2002/8607 | 23.26 % | 0.1042 | 0.0053 | 12.6 | entfernt GEWINNER |
| 0.67 | 5.0 | -0.1 | 3433/8607 | 39.89 % | -0.1413 | 0.1808 | 321.3 | entfernt GEWINNER |
| 0.0 | 25.0 | 0.1 | 2197/8607 | 25.53 % | 0.396 | -0.1276 | -290.6 | entfernt Verlierer |
| 0.0 | 25.0 | 0.0 | 3570/8607 | 41.48 % | -0.0834 | 0.1358 | 236.7 | entfernt GEWINNER |
| 0.0 | 25.0 | -0.1 | 4315/8607 | 50.13 % | -0.1457 | 0.2612 | 376.6 | entfernt GEWINNER |
| 0.0 | 10.0 | 0.1 | 2147/8607 | 24.94 % | 0.3555 | -0.106 | -243.5 | entfernt Verlierer |
| 0.0 | 10.0 | 0.0 | 3525/8607 | 40.96 % | -0.1239 | 0.1693 | 297.4 | entfernt GEWINNER |
| 0.0 | 10.0 | -0.1 | 4319/8607 | 50.18 % | -0.1549 | 0.2735 | 393.9 | entfernt GEWINNER |
| 0.0 | 5.0 | 0.1 | 2156/8607 | 25.05 % | 0.3526 | -0.1066 | -243.7 | entfernt Verlierer |
| 0.0 | 5.0 | 0.0 | 3553/8607 | 41.28 % | -0.1247 | 0.1748 | 302.2 | entfernt GEWINNER |
| 0.0 | 5.0 | -0.1 | 4282/8607 | 49.75 % | -0.1555 | 0.2734 | 394.6 | entfernt GEWINNER |

## Grenzen

- Snapshot statt Historie: heutige Zellstatistiken auf damaligem Verkehr — Parametereffekt und Zell-Drift sind nicht trennbar (oben gemessen).
- Nur die geforwardete Seite ist gescort. Unterdrückte Signale haben per Konstruktion kein ROM1-Bein — ihr Ausgang ist nicht beobachtet, nicht null.
- Events ohne angehängtes Bein zählen als `n_no_leg` und werden nie als 0 verrechnet.
- Kein Ergebnis dieses Laufs rechtfertigt einen Gate-Flip. Entscheidbar wird die Frage erst über ein Live-Shadow-A/B oder nachdem bot_regime_performance historisiert ist.

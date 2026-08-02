# v2-Whitelist: Parameter-Sensitivität gegen realisierte ROM1-Beine

- Fenster: `2026-04-18 00:00:00` → `2026-07-03 00:00:00`  (76.0 Tage)
- Gate-Events: 7655 (davon geforwardet: 4359)
- ROM1-Legs angehängt: 99.9%
- Erzeugt: 2026-08-02 17:16:09.837548+00:00  |  CPU bei Start: 100.0

## Warum das KEIN Backtest ist (gemessen, nicht behauptet)

`bot_regime_performance` trägt **0** Zellen mit mehr als einer Zeile → reiner Snapshot, keine Historie. `last_computed` von `2026-04-18 18:17:49.890235` bis `2026-08-02 17:06:51.539034`.

Die Zellstatistiken, auf denen das Gate zum Zeitpunkt eines vergangenen Events entschieden hat, existieren nicht mehr. Jede Neu-Entscheidung unten benutzt **heutige** Statistiken auf **damaligem** Verkehr und vermischt damit zwei Effekte: was die Parameter tun, und wie die Zellen seither gedriftet sind. Belastbar bleiben die *Form* der Parameter-Antwort und das *Vorzeichen* der geblockten Beine. Ein Flip lässt sich daraus nicht rechtfertigen — das kann nur ein Live-Shadow-A/B (wie T-031).

## Referenz: was v1 tatsächlich durchgelassen hat

- 4356 von 4359 geforwardeten Events mit ROM1-Leg  | Σ move 1899.1 %  | Ø 0.6886 %/Trade

## Parameter-Gitter

| z | k | break-even | behalten | Durchlass | Ø behalten % | Ø geblockt % | Σ geblockt % | Lesart |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1.64 **(heute)** | 25.0 | 0.1 | 149/4359 | 3.42 % | 4.3686 | 0.5544 | 1475.3 | entfernt GEWINNER |
| 1.64 | 25.0 | 0.0 | 182/4359 | 4.18 % | 2.9853 | 0.5823 | 1534.9 | entfernt GEWINNER |
| 1.64 | 25.0 | -0.1 | 192/4359 | 4.4 % | 2.4572 | 0.6018 | 1582.1 | entfernt GEWINNER |
| 1.64 | 10.0 | 0.1 | 160/4359 | 3.67 % | 4.1637 | 0.5524 | 1466.1 | entfernt GEWINNER |
| 1.64 | 10.0 | 0.0 | 160/4359 | 3.67 % | 4.1637 | 0.5524 | 1466.1 | entfernt GEWINNER |
| 1.64 | 10.0 | -0.1 | 160/4359 | 3.67 % | 4.1637 | 0.5524 | 1466.1 | entfernt GEWINNER |
| 1.64 | 5.0 | 0.1 | 148/4359 | 3.4 % | 3.7205 | 0.5781 | 1538.2 | entfernt GEWINNER |
| 1.64 | 5.0 | 0.0 | 161/4359 | 3.69 % | 4.2007 | 0.5496 | 1458.0 | entfernt GEWINNER |
| 1.64 | 5.0 | -0.1 | 161/4359 | 3.69 % | 4.2007 | 0.5496 | 1458.0 | entfernt GEWINNER |
| 1.28 | 25.0 | 0.1 | 193/4359 | 4.43 % | 2.3921 | 0.6043 | 1588.1 | entfernt GEWINNER |
| 1.28 | 25.0 | 0.0 | 195/4359 | 4.47 % | 2.6281 | 0.5911 | 1552.2 | entfernt GEWINNER |
| 1.28 | 25.0 | -0.1 | 195/4359 | 4.47 % | 2.6281 | 0.5911 | 1552.2 | entfernt GEWINNER |
| 1.28 | 10.0 | 0.1 | 206/4359 | 4.73 % | 2.4429 | 0.5962 | 1562.0 | entfernt GEWINNER |
| 1.28 | 10.0 | 0.0 | 209/4359 | 4.79 % | 2.5737 | 0.587 | 1536.2 | entfernt GEWINNER |
| 1.28 | 10.0 | -0.1 | 209/4359 | 4.79 % | 2.5737 | 0.587 | 1536.2 | entfernt GEWINNER |
| 1.28 | 5.0 | 0.1 | 167/4359 | 3.83 % | 4.0075 | 0.5533 | 1466.3 | entfernt GEWINNER |
| 1.28 | 5.0 | 0.0 | 206/4359 | 4.73 % | 2.4448 | 0.5961 | 1561.7 | entfernt GEWINNER |
| 1.28 | 5.0 | -0.1 | 208/4359 | 4.77 % | 2.6666 | 0.5828 | 1525.8 | entfernt GEWINNER |
| 1.04 | 25.0 | 0.1 | 195/4359 | 4.47 % | 2.6281 | 0.5911 | 1552.2 | entfernt GEWINNER |
| 1.04 | 25.0 | 0.0 | 200/4359 | 4.59 % | 2.5339 | 0.5943 | 1559.5 | entfernt GEWINNER |
| 1.04 | 25.0 | -0.1 | 209/4359 | 4.79 % | 2.5028 | 0.593 | 1553.7 | entfernt GEWINNER |
| 1.04 | 10.0 | 0.1 | 213/4359 | 4.89 % | 2.5556 | 0.5872 | 1536.2 | entfernt GEWINNER |
| 1.04 | 10.0 | 0.0 | 219/4359 | 5.02 % | 2.6752 | 0.5783 | 1511.2 | entfernt GEWINNER |
| 1.04 | 10.0 | -0.1 | 221/4359 | 5.07 % | 2.5147 | 0.5858 | 1529.4 | entfernt GEWINNER |
| 1.04 | 5.0 | 0.1 | 214/4359 | 4.91 % | 2.5556 | 0.5872 | 1536.2 | entfernt GEWINNER |
| 1.04 | 5.0 | 0.0 | 215/4359 | 4.93 % | 2.5556 | 0.5872 | 1536.2 | entfernt GEWINNER |
| 1.04 | 5.0 | -0.1 | 223/4359 | 5.12 % | 2.5748 | 0.5824 | 1520.6 | entfernt GEWINNER |
| 0.67 | 25.0 | 0.1 | 218/4359 | 5.0 % | 2.4431 | 0.5926 | 1549.7 | entfernt GEWINNER |
| 0.67 | 25.0 | 0.0 | 218/4359 | 5.0 % | 2.4431 | 0.5926 | 1549.7 | entfernt GEWINNER |
| 0.67 | 25.0 | -0.1 | 219/4359 | 5.02 % | 2.4431 | 0.5926 | 1549.7 | entfernt GEWINNER |
| 0.67 | 10.0 | 0.1 | 231/4359 | 5.3 % | 2.3276 | 0.5936 | 1547.6 | entfernt GEWINNER |
| 0.67 | 10.0 | 0.0 | 233/4359 | 5.35 % | 2.3219 | 0.5933 | 1546.2 | entfernt GEWINNER |
| 0.67 | 10.0 | -0.1 | 239/4359 | 5.48 % | 2.3928 | 0.5871 | 1528.2 | entfernt GEWINNER |
| 0.67 | 5.0 | 0.1 | 239/4359 | 5.48 % | 2.3928 | 0.5871 | 1528.2 | entfernt GEWINNER |
| 0.67 | 5.0 | 0.0 | 239/4359 | 5.48 % | 2.3928 | 0.5871 | 1528.2 | entfernt GEWINNER |
| 0.67 | 5.0 | -0.1 | 239/4359 | 5.48 % | 2.3928 | 0.5871 | 1528.2 | entfernt GEWINNER |
| 0.0 | 25.0 | 0.1 | 246/4359 | 5.64 % | 2.333 | 0.5886 | 1530.5 | entfernt GEWINNER |
| 0.0 | 25.0 | 0.0 | 247/4359 | 5.67 % | 2.328 | 0.5883 | 1528.9 | entfernt GEWINNER |
| 0.0 | 25.0 | -0.1 | 4142/4359 | 95.02 % | 0.7268 | -0.2139 | -24.0 | entfernt Verlierer |
| 0.0 | 10.0 | 0.1 | 258/4359 | 5.92 % | 2.2237 | 0.5903 | 1530.0 | entfernt GEWINNER |
| 0.0 | 10.0 | 0.0 | 258/4359 | 5.92 % | 2.2237 | 0.5903 | 1530.0 | entfernt GEWINNER |
| 0.0 | 10.0 | -0.1 | 4149/4359 | 95.18 % | 0.7217 | -0.1474 | -15.5 | entfernt Verlierer |
| 0.0 | 5.0 | 0.1 | 261/4359 | 5.99 % | 2.2063 | 0.5901 | 1528.4 | entfernt GEWINNER |
| 0.0 | 5.0 | 0.0 | 263/4359 | 6.03 % | 2.2339 | 0.5871 | 1519.3 | entfernt GEWINNER |
| 0.0 | 5.0 | -0.1 | 4150/4359 | 95.21 % | 0.7186 | -0.0779 | -8.1 | entfernt Verlierer |

## Grenzen

- Snapshot statt Historie: heutige Zellstatistiken auf damaligem Verkehr — Parametereffekt und Zell-Drift sind nicht trennbar (oben gemessen).
- Nur die geforwardete Seite ist gescort. Unterdrückte Signale haben per Konstruktion kein ROM1-Bein — ihr Ausgang ist nicht beobachtet, nicht null.
- Events ohne angehängtes Bein zählen als `n_no_leg` und werden nie als 0 verrechnet.
- Kein Ergebnis dieses Laufs rechtfertigt einen Gate-Flip. Entscheidbar wird die Frage erst über ein Live-Shadow-A/B oder nachdem bot_regime_performance historisiert ist.

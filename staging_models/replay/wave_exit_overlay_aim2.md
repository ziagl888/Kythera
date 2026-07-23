# Wave-Exit Phase 1 — High-Fidelity-Sim Validierung (AIM2)

_generated 2026-07-23 13:46:29.905720+00:00 · read-only · window 2026-07-07 14:20:00 → 2026-07-23 00:00:00_

**Backbone:** vollständige wick-aware **5m**-OHLC-Kerzen (`candles`, 12× feiner als der 1h-Live-Monitor) für die Touch-Erkennung; **10s**-Ticks (`ticker_10s`) nur als Order-Resolver für SL-vs-TP-Reihenfolge innerhalb einer 5m-Kerze. **Geometrie:** immutable Cornix-Text (`telegram_outbox`), Original-SL/entry2/TP1-3. **Outcome-Ground-Truth:** `closed_ai_signals`.

> Warum nicht rein 10s: `ticker_10s` ist ein ~40s-Snapshot mit Lücken (Coverage-Median 0.25) und verpasst ~81% der SL-Touch-Events → eine reine Tick-Sim entkommt den Stops und verzerrt Realized ~2.7×. Die 5m-Kerze ist gap-frei und wick-aware.

Closed im Fenster: 1288 · Geometrie gematcht & gescored: **676** · ungematcht (Outbox-Retention): 604.
Gescorte-Trades-Span: 2026-07-10 18:48:31.207150 → 2026-07-22 22:15:37.406260 (Outbox-Retention verzerrt das Set zu **jüngeren** Trades — beim Lesen der Aggregate beachten).

## Validierung — `monitor`-Config (entry1-only, interne Targets) vs recorded closed_ai_signals

- targets_hit **exakt**: 97.93%  ·  **±1**: 99.26%
- Win/Loss (TP1-Touch) **Übereinstimmung**: 99.26%

> Restdivergenz kommt aus der feineren Auflösung (5m-Wick + echte Intra-Candle-Ordnung) gegenüber dem 1h-Monitor — die Sim ist hier bewusst *treuer* als die recorded-Outcome-Quelle.

## Realized-Aggregat je Config

| config | n | unlev mean% | unlev sum% | net sum% | leveraged sum% (n) | WR(TP1)% | Ø-Dauer med/mean h |
|---|--:|--:|--:|--:|--:|--:|--:|
| monitor | 676 | 0.4309 | 291.3 | 223.8 | 14089.1 (676) | 64.5 | 21.83/31.76 |
| dca10 | 676 | 0.0902 | 61.0 | 11.05 | 5955.3 (676) | 64.5 | 21.83/31.8 |
| cornix3 | 676 | 0.2611 | 176.5 | 126.45 | 8255.9 (676) | 64.64 | 20.75/30.59 |

**Lesehilfe:** `monitor` = 1:1-Reproduktion des Bot-Monitors (Validierungsanker). `cornix3` = was Cornix real handelt (DCA entry1/entry2, 3 publizierte TPs in Dritteln) — die Headline-Realized-Zahl und die Basis fürs Phase-2-Overlay.


---

## Phase 2 — Auto-Close-Overlays (auf `cornix3`, real-money DCA/3-TP)

n_arts = 676 (leveraged, gescort). Metrik = REALIZED (locked-in) — unlev Summe% / leveraged Summe%; MaxDD = Peak-to-Trough der aggregierten Open-Positions-Welle (leveraged Kontoeinheiten). **Baseline = hold-to-TP/SL.**

### KERNBEFUND

- **Leveraged Realized: keine Overlay-Variante schlägt hold.** Baseline +8255.9% vs (a) 4539.0…5115.1% / (c) 4164.8…4718.0% — robust über den GANZEN Sweep schlechter. Der leveraged-Summe wird von wenigen Fat-Tail-Wellen-Treffern dominiert (−100%-Clamp-Asymmetrie), die jedes Overlay kappt.
- **Unlevered Realized: Overlays sind BESSER.** Baseline +176.5% vs (a) 243.14…281.75% / (c) 245.27…277.79% — die Regeln schneiden die Underwater-Tails, ohne die (unhebelte) Verteilung so stark von den Winnern abzuhängen.
- **Drawdown: (c) ist ein Risk-Tool.** MaxDD-Welle 41.3 (hold) → 5.0…6.4 (~8× kleiner) — gegen ~44% weniger leveraged Upside.
- **L/S:** der leveraged-Verlust sitzt fast ganz im LONG; SHORT-unlev vervielfacht sich (Tabelle unten). Bestätigt T-032/029/031: der Edge ist RICHTUNGS-, nicht Timing-bedingt.
- **Fazit:** Michis Wellen-Intuition fängt out-of-sample **kein** leveraged-Edge (Markt-Timing), aber (c) konvertiert Upside-Varianz in Drawdown-Schutz. Kein Deploy-Signal für Return-Maximierung; als reiner Portfolio-Circuit-Breaker diskutabel. **NO-EDGE auf der Headline-Metrik.**

> ⚠ **WR(TP1)% ist unter Overlays irreführend** (die Regel schließt auf MTM-Retrace, nicht auf TP-Touch → tp1=False obwohl profitabel geschlossen). Realized ist die Metrik, nicht WR. Overlay (a) triggert bei ~95% (Peak-Retrace feuert auch auf kleinen Wellen — eine Aktivierungs-Schwelle würde nur große Wellen trailen, ist hier aber nicht nötig: das Vorzeichen ist schon klar).

### Overlay (a) — Per-Trade-Trailing-TP (close bei X% Retrace vom Trade-MTM-Peak)

| X% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD-Welle | getriggert% |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 676 | 176.5 | 8255.9 | 64.6 | 41.3 | 0.0 |
| 10 | 676 | 247.82 | 4606.5 | 7.1 | — | 95.0 |
| 15 | 676 | 248.14 | 4638.6 | 8.0 | — | 94.2 |
| 20 | 676 | 243.14 | 4539.0 | 8.1 | — | 93.5 |
| 25 | 676 | 259.32 | 4829.1 | 8.9 | — | 92.6 |
| 30 | 676 | 281.75 | 5115.1 | 10.1 | — | 91.3 |
| 40 | 676 | 271.42 | 4874.9 | 12.1 | — | 89.8 |

### Overlay (c) — Portfolio-Circuit-Breaker (close-ALL bei Y% Retrace der Aggregat-Welle)

| Y% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD-Welle | geflattet |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 676 | 176.5 | 8255.9 | 64.6 | 41.3 | 0 |
| 10 | 676 | 253.17 | 4533.9 | 6.8 | 5.5 | 659 |
| 15 | 676 | 267.73 | 4611.7 | 7.2 | 5.0 | 660 |
| 20 | 676 | 273.1 | 4692.6 | 7.8 | 5.0 | 658 |
| 25 | 676 | 277.79 | 4718.0 | 8.1 | 6.4 | 658 |
| 30 | 676 | 272.98 | 4632.8 | 8.7 | 6.4 | 656 |
| 40 | 676 | 245.27 | 4164.8 | 10.4 | 6.0 | 653 |

### Long/Short getrennt (unlev sum% / lev sum%)

| Regel | LONG | SHORT |
|---|--:|--:|
| Baseline | 136.37/4373.3 | 40.14/3882.7 |
| (a) X=10% | 89.15/1701.3 | 158.67/2905.1 |
| (a) X=15% | 86.19/1653.2 | 161.95/2985.4 |
| (a) X=20% | 85.25/1606.6 | 157.89/2932.4 |
| (a) X=25% | 93.97/1747.3 | 165.35/3081.8 |
| (a) X=30% | 115.14/1976.7 | 166.61/3138.4 |
| (a) X=40% | 109.65/1879.2 | 161.77/2995.7 |
| (c) Y=10% | 80.68/1473.1 | 172.5/3060.8 |
| (c) Y=15% | 94.08/1586.8 | 173.65/3024.9 |
| (c) Y=20% | 95.73/1597.7 | 177.37/3094.9 |
| (c) Y=25% | 95.92/1530.6 | 181.87/3187.4 |
| (c) Y=30% | 98.68/1586.0 | 174.3/3046.8 |
| (c) Y=40% | 82.69/1315.9 | 162.57/2848.8 |

**Ehrliche Grenze:** 7d/674-Legs, jüngeres Fenster (Outbox-Bias). Wellen-Capture ist Markt-Timing; getestet wird, ob eine MECHANISCHE Regel die Welle out-of-sample fängt oder nur im Hindsight sichtbar ist. Bewertet werden robuste **Bänder + Vorzeichen** über den Sweep, nicht ein Best-Punkt. NO-EDGE ist ein valides Ergebnis.

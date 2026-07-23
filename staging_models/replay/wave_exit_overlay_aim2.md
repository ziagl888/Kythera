# Wave-Exit Phase 1 — High-Fidelity-Sim Validierung (AIM2)

_generated 2026-07-23 16:38:14.087937+00:00 · read-only · window 2026-07-07 14:20:00 → 2026-07-23 00:00:00_

**Backbone:** vollständige wick-aware **5m**-OHLC-Kerzen (`candles`, 12× feiner als der 1h-Live-Monitor) für die Touch-Erkennung; **10s**-Ticks (`ticker_10s`) nur als Order-Resolver für SL-vs-TP-Reihenfolge innerhalb einer 5m-Kerze. **Geometrie:** immutable Cornix-Text (`telegram_outbox`), Original-SL/entry2/TP1-3. **Outcome-Ground-Truth:** `closed_ai_signals`.

> Warum nicht rein 10s: `ticker_10s` ist ein ~40s-Snapshot mit Lücken (Coverage-Median 0.25) und verpasst ~81% der SL-Touch-Events → eine reine Tick-Sim entkommt den Stops und verzerrt Realized ~2.7×. Die 5m-Kerze ist gap-frei und wick-aware.

Closed im Fenster: 1299 · Geometrie gematcht & gescored: **683** · ungematcht (Outbox-Retention): 608.
Gescorte-Trades-Span: 2026-07-10 18:48:31.207150 → 2026-07-22 22:15:37.406260 (Outbox-Retention verzerrt das Set zu **jüngeren** Trades — beim Lesen der Aggregate beachten).

## Validierung — `monitor`-Config (entry1-only, interne Targets) vs recorded closed_ai_signals

- targets_hit **exakt**: 97.95%  ·  **±1**: 99.27%
- Win/Loss (TP1-Touch) **Übereinstimmung**: 99.27%

> Restdivergenz kommt aus der feineren Auflösung (5m-Wick + echte Intra-Candle-Ordnung) gegenüber dem 1h-Monitor — die Sim ist hier bewusst *treuer* als die recorded-Outcome-Quelle.

## Realized-Aggregat je Config

| config | n | unlev mean% | unlev sum% | net sum% | leveraged sum% (n) | WR(TP1)% | Ø-Dauer med/mean h |
|---|--:|--:|--:|--:|--:|--:|--:|
| monitor | 683 | 0.4223 | 288.42 | 220.22 | 14078.4 (683) | 64.57 | 22.33/32.18 |
| dca10 | 683 | 0.0816 | 55.73 | 5.33 | 5873.4 (683) | 64.57 | 22.33/32.22 |
| cornix3 | 683 | 0.2533 | 173.01 | 122.51 | 8209.6 (683) | 64.71 | 20.83/31.03 |

**Lesehilfe:** `monitor` = 1:1-Reproduktion des Bot-Monitors (Validierungsanker). `cornix3` = was Cornix real handelt (DCA entry1/entry2, 3 publizierte TPs in Dritteln) — die Headline-Realized-Zahl und die Basis fürs Phase-2-Overlay.


---

## Phase 2 — Auto-Close-Overlays (auf `cornix3`, real-money DCA/3-TP)

n_arts = 683 (leveraged, gescort). Metrik = REALIZED (locked-in) — unlev Summe% / leveraged Summe%; MaxDD = Peak-to-Trough der aggregierten Open-Positions-Welle (leveraged Kontoeinheiten). **Baseline = hold-to-TP/SL.**

### KERNBEFUND

- **Leveraged Realized: keine Overlay-Variante schlägt hold.** Baseline +8209.6% vs (a) 4565.1…5163.1% / (c) 4164.2…4720.7% — robust über den GANZEN Sweep schlechter. Der leveraged-Summe wird von wenigen Fat-Tail-Wellen-Treffern dominiert (−100%-Clamp-Asymmetrie), die jedes Overlay kappt.
- **Unlevered Realized: Overlays sind BESSER.** Baseline +173.01% vs (a) 244.45…284.15% / (c) 245.24…275.68% — die Regeln schneiden die Underwater-Tails, ohne die (unhebelte) Verteilung so stark von den Winnern abzuhängen.
- **Drawdown: (c) ist ein Risk-Tool.** MaxDD-Welle 43.2 (hold) → 5.0…6.4 (~9× kleiner) — gegen ~44% weniger leveraged Upside.
- **L/S:** der leveraged-Verlust sitzt fast ganz im LONG; SHORT-unlev vervielfacht sich (Tabelle unten). Bestätigt T-032/029/031: der Edge ist RICHTUNGS-, nicht Timing-bedingt.
- **Fazit:** Michis Wellen-Intuition fängt out-of-sample **kein** leveraged-Edge (Markt-Timing), aber (c) konvertiert Upside-Varianz in Drawdown-Schutz. Kein Deploy-Signal für Return-Maximierung; als reiner Portfolio-Circuit-Breaker diskutabel. **NO-EDGE auf der Headline-Metrik.**

> ⚠ **WR(TP1)% ist unter Overlays irreführend** (die Regel schließt auf MTM-Retrace, nicht auf TP-Touch → tp1=False obwohl profitabel geschlossen). Realized ist die Metrik, nicht WR. Overlay (a) triggert bei ~95% (Peak-Retrace feuert auch auf kleinen Wellen — eine Aktivierungs-Schwelle würde nur große Wellen trailen, ist hier aber nicht nötig: das Vorzeichen ist schon klar).

### Overlay (a) — Per-Trade-Trailing-TP (close bei X% Retrace vom Trade-MTM-Peak)

| X% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD-Welle | getriggert% |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 683 | 173.01 | 8209.6 | 64.7 | 43.2 | 0.0 |
| 10 | 683 | 249.12 | 4632.5 | 7.0 | — | 95.0 |
| 15 | 683 | 249.45 | 4664.7 | 7.9 | — | 94.3 |
| 20 | 683 | 244.45 | 4565.1 | 8.1 | — | 93.6 |
| 25 | 683 | 260.62 | 4855.2 | 8.8 | — | 92.7 |
| 30 | 683 | 284.15 | 5163.1 | 10.0 | — | 91.4 |
| 40 | 683 | 273.83 | 4923.1 | 12.2 | — | 89.9 |

### Overlay (c) — Portfolio-Circuit-Breaker (close-ALL bei Y% Retrace der Aggregat-Welle)

| Y% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD-Welle | geflattet |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 683 | 173.01 | 8209.6 | 64.7 | 43.2 | 0 |
| 10 | 683 | 252.99 | 4542.4 | 6.7 | 5.6 | 667 |
| 15 | 683 | 268.19 | 4620.8 | 7.2 | 5.0 | 667 |
| 20 | 683 | 274.56 | 4720.7 | 7.8 | 5.0 | 665 |
| 25 | 683 | 275.68 | 4675.4 | 8.1 | 6.4 | 665 |
| 30 | 683 | 274.52 | 4660.6 | 8.6 | 6.4 | 663 |
| 40 | 683 | 245.24 | 4164.2 | 10.2 | 6.0 | 660 |

### Long/Short getrennt (unlev sum% / lev sum%)

| Regel | LONG | SHORT |
|---|--:|--:|
| Baseline | 132.99/4325.6 | 40.02/3884.0 |
| (a) X=10% | 89.29/1704.1 | 159.84/2928.5 |
| (a) X=15% | 86.33/1655.9 | 163.12/3008.8 |
| (a) X=20% | 85.39/1609.4 | 159.06/2955.7 |
| (a) X=25% | 94.11/1750.1 | 166.52/3105.1 |
| (a) X=30% | 115.28/1979.4 | 168.87/3183.7 |
| (a) X=40% | 109.79/1882.0 | 164.04/3041.1 |
| (c) Y=10% | 80.33/1471.8 | 172.66/3070.6 |
| (c) Y=15% | 94.96/1598.0 | 173.23/3022.9 |
| (c) Y=20% | 96.24/1606.4 | 178.32/3114.3 |
| (c) Y=25% | 94.57/1503.2 | 181.1/3172.2 |
| (c) Y=30% | 97.94/1545.3 | 176.58/3115.3 |
| (c) Y=40% | 82.43/1310.6 | 162.81/2853.6 |

**Ehrliche Grenze:** 7d/674-Legs, jüngeres Fenster (Outbox-Bias). Wellen-Capture ist Markt-Timing; getestet wird, ob eine MECHANISCHE Regel die Welle out-of-sample fängt oder nur im Hindsight sichtbar ist. Bewertet werden robuste **Bänder + Vorzeichen** über den Sweep, nicht ein Best-Punkt. NO-EDGE ist ein valides Ergebnis.

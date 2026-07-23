# Wave-Exit Phase 1 — High-Fidelity-Sim Validierung (EPD3)

_generated 2026-07-23 16:47:57.206498+00:00 · read-only · window 2026-07-07 14:20:00 → 2026-07-23 00:00:00_

**Backbone:** vollständige wick-aware **5m**-OHLC-Kerzen (`candles`, 12× feiner als der 1h-Live-Monitor) für die Touch-Erkennung; **10s**-Ticks (`ticker_10s`) nur als Order-Resolver für SL-vs-TP-Reihenfolge innerhalb einer 5m-Kerze. **Geometrie:** immutable Cornix-Text (`telegram_outbox`), Original-SL/entry2/TP1-3. **Outcome-Ground-Truth:** `closed_ai_signals`.

> Warum nicht rein 10s: `ticker_10s` ist ein ~40s-Snapshot mit Lücken (Coverage-Median 0.25) und verpasst ~81% der SL-Touch-Events → eine reine Tick-Sim entkommt den Stops und verzerrt Realized ~2.7×. Die 5m-Kerze ist gap-frei und wick-aware.

Closed im Fenster: 6254 · Geometrie gematcht & gescored: **604** · ungematcht (Outbox-Retention): 5573.
Gescorte-Trades-Span: 2026-07-15 00:09:45.587461 → 2026-07-22 23:57:47.390157 (Outbox-Retention verzerrt das Set zu **jüngeren** Trades — beim Lesen der Aggregate beachten).

## Validierung — `monitor`-Config (entry1-only, interne Targets) vs recorded closed_ai_signals

- targets_hit **exakt**: 93.87%  ·  **±1**: 99.5%
- Win/Loss (TP1-Touch) **Übereinstimmung**: 98.51%

> Restdivergenz kommt aus der feineren Auflösung (5m-Wick + echte Intra-Candle-Ordnung) gegenüber dem 1h-Monitor — die Sim ist hier bewusst *treuer* als die recorded-Outcome-Quelle.

## Realized-Aggregat je Config

| config | n | unlev mean% | unlev sum% | net sum% | leveraged sum% (n) | WR(TP1)% | Ø-Dauer med/mean h |
|---|--:|--:|--:|--:|--:|--:|--:|
| monitor | 604 | 0.0557 | 33.65 | -26.75 | 5364.6 (604) | 79.47 | 6.29/12.62 |
| dca10 | 604 | 0.0031 | 1.9 | -36.35 | 2610.4 (604) | 79.47 | 6.29/12.62 |
| cornix3 | 604 | 0.092 | 55.58 | 17.33 | 3329.1 (604) | 79.8 | 6.0/11.12 |

**Lesehilfe:** `monitor` = 1:1-Reproduktion des Bot-Monitors (Validierungsanker). `cornix3` = was Cornix real handelt (DCA entry1/entry2, 3 publizierte TPs in Dritteln) — die Headline-Realized-Zahl und die Basis fürs Phase-2-Overlay.


---

## Phase 2 — Auto-Close-Overlays (auf `cornix3`, real-money DCA/3-TP)

n_arts = 604 (leveraged, gescort). Metrik = REALIZED (locked-in) — unlev Summe% / leveraged Summe%; MaxDD = Peak-to-Trough der aggregierten Open-Positions-Welle (leveraged Kontoeinheiten). **Baseline = hold-to-TP/SL.**

### KERNBEFUND

- **Leveraged Realized: keine Overlay-Variante schlägt hold.** Baseline +3329.1% vs (a) 776.4…850.4% / (c) 764.1…896.1% — robust über den GANZEN Sweep schlechter. Der leveraged-Summe wird von wenigen Fat-Tail-Wellen-Treffern dominiert (−100%-Clamp-Asymmetrie), die jedes Overlay kappt.
- **Unlevered Realized:** Baseline 55.58% vs (a) -5.76…-1.45% / (c) 18.82…27.25% — Overlays nicht besser.
- **Drawdown: (c) ist ein Risk-Tool.** MaxDD-Welle 12.2 (hold) → 4.2…4.2 (~3× kleiner).
- **Fazit:** Wellen-Intuition fängt out-of-sample **kein** leveraged-Edge; (c) konvertiert Upside-Varianz in Drawdown-Schutz. **NO-EDGE auf der Headline-Metrik.**

> ⚠ **WR(TP1)% ist unter Overlays irreführend** (die Regel schließt auf MTM-Retrace, nicht auf TP-Touch → tp1=False obwohl profitabel geschlossen). Realized ist die Metrik, nicht WR. Overlay (a) triggert bei ~95% (Peak-Retrace feuert auch auf kleinen Wellen — eine Aktivierungs-Schwelle würde nur große Wellen trailen, ist hier aber nicht nötig: das Vorzeichen ist schon klar).

### Overlay (a) — Per-Trade-Trailing-TP (close bei X% Retrace vom Trade-MTM-Peak)

| X% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD-Welle | getriggert% |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 604 | 55.58 | 3329.1 | 79.8 | 12.2 | 0.0 |
| 10 | 604 | -1.54 | 843.7 | 16.2 | — | 85.6 |
| 15 | 604 | -4.2 | 792.3 | 17.2 | — | 84.8 |
| 20 | 604 | -5.76 | 788.8 | 18.0 | — | 84.3 |
| 25 | 604 | -2.92 | 847.7 | 18.4 | — | 83.4 |
| 30 | 604 | -1.45 | 850.4 | 19.5 | — | 82.3 |
| 40 | 604 | -5.57 | 776.4 | 21.2 | — | 81.3 |

### Overlay (c) — Portfolio-Circuit-Breaker (close-ALL bei Y% Retrace der Aggregat-Welle)

| Y% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD-Welle | geflattet |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 604 | 55.58 | 3329.1 | 79.8 | 12.2 | 0 |
| 10 | 604 | 26.92 | 888.0 | 14.7 | 4.2 | 548 |
| 15 | 604 | 27.25 | 896.1 | 14.7 | 4.2 | 548 |
| 20 | 604 | 24.91 | 866.4 | 14.6 | 4.2 | 548 |
| 25 | 604 | 18.82 | 764.1 | 14.6 | 4.2 | 548 |
| 30 | 604 | 20.18 | 776.0 | 14.7 | 4.2 | 544 |
| 40 | 604 | 22.34 | 816.6 | 15.7 | 4.2 | 541 |

### Long/Short getrennt (unlev sum% / lev sum%)

| Regel | LONG | SHORT |
|---|--:|--:|
| Baseline | — | 55.58/3329.1 |
| (a) X=10% | — | -1.54/843.7 |
| (a) X=15% | — | -4.2/792.3 |
| (a) X=20% | — | -5.76/788.8 |
| (a) X=25% | — | -2.92/847.7 |
| (a) X=30% | — | -1.45/850.4 |
| (a) X=40% | — | -5.57/776.4 |
| (c) Y=10% | — | 26.92/888.0 |
| (c) Y=15% | — | 27.25/896.1 |
| (c) Y=20% | — | 24.91/866.4 |
| (c) Y=25% | — | 18.82/764.1 |
| (c) Y=30% | — | 20.18/776.0 |
| (c) Y=40% | — | 22.34/816.6 |

**Ehrliche Grenze:** 7d/674-Legs, jüngeres Fenster (Outbox-Bias). Wellen-Capture ist Markt-Timing; getestet wird, ob eine MECHANISCHE Regel die Welle out-of-sample fängt oder nur im Hindsight sichtbar ist. Bewertet werden robuste **Bänder + Vorzeichen** über den Sweep, nicht ein Best-Punkt. NO-EDGE ist ein valides Ergebnis.

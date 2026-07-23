# Wave-Exit Phase 1 — High-Fidelity-Sim Validierung (SRA2)

_generated 2026-07-23 16:49:24.694603+00:00 · read-only · window 2026-07-07 14:20:00 → 2026-07-23 00:00:00_

**Backbone:** vollständige wick-aware **5m**-OHLC-Kerzen (`candles`, 12× feiner als der 1h-Live-Monitor) für die Touch-Erkennung; **10s**-Ticks (`ticker_10s`) nur als Order-Resolver für SL-vs-TP-Reihenfolge innerhalb einer 5m-Kerze. **Geometrie:** immutable Cornix-Text (`telegram_outbox`), Original-SL/entry2/TP1-3. **Outcome-Ground-Truth:** `closed_ai_signals`.

> Warum nicht rein 10s: `ticker_10s` ist ein ~40s-Snapshot mit Lücken (Coverage-Median 0.25) und verpasst ~81% der SL-Touch-Events → eine reine Tick-Sim entkommt den Stops und verzerrt Realized ~2.7×. Die 5m-Kerze ist gap-frei und wick-aware.

Closed im Fenster: 552 · Geometrie gematcht & gescored: **29** · ungematcht (Outbox-Retention): 515.
Gescorte-Trades-Span: 2026-07-16 20:54:18.284426 → 2026-07-22 23:31:39.601055 (Outbox-Retention verzerrt das Set zu **jüngeren** Trades — beim Lesen der Aggregate beachten).

## Validierung — `monitor`-Config (entry1-only, interne Targets) vs recorded closed_ai_signals

- targets_hit **exakt**: 96.55%  ·  **±1**: 100.0%
- Win/Loss (TP1-Touch) **Übereinstimmung**: 96.55%

> Restdivergenz kommt aus der feineren Auflösung (5m-Wick + echte Intra-Candle-Ordnung) gegenüber dem 1h-Monitor — die Sim ist hier bewusst *treuer* als die recorded-Outcome-Quelle.

## Realized-Aggregat je Config

| config | n | unlev mean% | unlev sum% | net sum% | leveraged sum% (n) | WR(TP1)% | Ø-Dauer med/mean h |
|---|--:|--:|--:|--:|--:|--:|--:|
| monitor | 29 | -0.4006 | -11.62 | -14.52 | 92.0 (29) | 79.31 | 8.67/14.65 |
| dca10 | 29 | -0.4547 | -13.19 | -14.94 | -84.6 (29) | 79.31 | 8.67/14.65 |
| cornix3 | 29 | -0.4547 | -13.19 | -14.94 | -84.6 (29) | 79.31 | 8.67/14.65 |

**Lesehilfe:** `monitor` = 1:1-Reproduktion des Bot-Monitors (Validierungsanker). `cornix3` = was Cornix real handelt (DCA entry1/entry2, 3 publizierte TPs in Dritteln) — die Headline-Realized-Zahl und die Basis fürs Phase-2-Overlay.


---

## Phase 2 — Auto-Close-Overlays (auf `cornix3`, real-money DCA/3-TP)

n_arts = 29 (leveraged, gescort). Metrik = REALIZED (locked-in) — unlev Summe% / leveraged Summe%; MaxDD = Peak-to-Trough der aggregierten Open-Positions-Welle (leveraged Kontoeinheiten). **Baseline = hold-to-TP/SL.**

### KERNBEFUND

> ⚠ **THIN (n=29 < 30): unter der Signifikanzschwelle — nur illustrativ, kein Verdikt.** Bei so wenigen Legs bestimmt ein einzelnes Fenster das Vorzeichen.

- **Leveraged Realized: mindestens eine Overlay-Variante schlägt hold.** Baseline -84.6% vs (a) 39.3…73.3% / (c) 15.9…51.2%. Da die Baseline hier NEGATIV/schwach ist, schlägt jede Früh-Exit-Regel einen ungünstigen Halt — das ist ein Fenster-Artefakt, kein bewiesener Timing-Edge (n klein, Baseline-Vorzeichen prüfen).
- **Unlevered Realized:** Baseline -13.19% vs (a) 1.41…3.37% / (c) -0.5…1.46% — Overlays überwiegend BESSER (schneiden Underwater-Tails).
- **Drawdown: (c) ist ein Risk-Tool.** MaxDD-Welle 2.9 (hold) → 1.0…1.0 (~3× kleiner).
- **Fazit:** Zu dünn für ein Verdikt — siehe THIN-Hinweis oben.

> ⚠ **WR(TP1)% ist unter Overlays irreführend** (die Regel schließt auf MTM-Retrace, nicht auf TP-Touch → tp1=False obwohl profitabel geschlossen). Realized ist die Metrik, nicht WR. Overlay (a) triggert bei ~95% (Peak-Retrace feuert auch auf kleinen Wellen — eine Aktivierungs-Schwelle würde nur große Wellen trailen, ist hier aber nicht nötig: das Vorzeichen ist schon klar).

### Overlay (a) — Per-Trade-Trailing-TP (close bei X% Retrace vom Trade-MTM-Peak)

| X% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD-Welle | getriggert% |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 29 | -13.19 | -84.6 | 79.3 | 2.9 | 0.0 |
| 10 | 29 | 3.21 | 70.7 | 20.7 | — | 86.2 |
| 15 | 29 | 3.16 | 69.5 | 20.7 | — | 86.2 |
| 20 | 29 | 3.37 | 73.3 | 24.1 | — | 82.8 |
| 25 | 29 | 2.97 | 65.2 | 24.1 | — | 82.8 |
| 30 | 29 | 2.85 | 62.8 | 24.1 | — | 82.8 |
| 40 | 29 | 1.41 | 39.3 | 24.1 | — | 82.8 |

### Overlay (c) — Portfolio-Circuit-Breaker (close-ALL bei Y% Retrace der Aggregat-Welle)

| Y% | n | unlev sum% | lev sum% | WR(TP1)% | MaxDD-Welle | geflattet |
|--:|--:|--:|--:|--:|--:|--:|
| Baseline | 29 | -13.19 | -84.6 | 79.3 | 2.9 | 0 |
| 10 | 29 | 1.25 | 47.1 | 20.7 | 1.0 | 25 |
| 15 | 29 | 1.24 | 46.8 | 20.7 | 1.0 | 25 |
| 20 | 29 | 1.46 | 51.2 | 24.1 | 1.0 | 25 |
| 25 | 29 | 1.0 | 42.1 | 24.1 | 1.0 | 25 |
| 30 | 29 | 0.88 | 39.6 | 24.1 | 1.0 | 25 |
| 40 | 29 | -0.5 | 15.9 | 24.1 | 1.0 | 25 |

### Long/Short getrennt (unlev sum% / lev sum%)

| Regel | LONG | SHORT |
|---|--:|--:|
| Baseline | -13.19/-84.6 | — |
| (a) X=10% | 3.21/70.7 | — |
| (a) X=15% | 3.16/69.5 | — |
| (a) X=20% | 3.37/73.3 | — |
| (a) X=25% | 2.97/65.2 | — |
| (a) X=30% | 2.85/62.8 | — |
| (a) X=40% | 1.41/39.3 | — |
| (c) Y=10% | 1.25/47.1 | — |
| (c) Y=15% | 1.24/46.8 | — |
| (c) Y=20% | 1.46/51.2 | — |
| (c) Y=25% | 1.0/42.1 | — |
| (c) Y=30% | 0.88/39.6 | — |
| (c) Y=40% | -0.5/15.9 | — |

**Ehrliche Grenze:** 7d/674-Legs, jüngeres Fenster (Outbox-Bias). Wellen-Capture ist Markt-Timing; getestet wird, ob eine MECHANISCHE Regel die Welle out-of-sample fängt oder nur im Hindsight sichtbar ist. Bewertet werden robuste **Bänder + Vorzeichen** über den Sweep, nicht ein Best-Punkt. NO-EDGE ist ein valides Ergebnis.

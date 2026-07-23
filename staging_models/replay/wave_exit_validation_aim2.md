# Wave-Exit Phase 1 — High-Fidelity-Sim Validierung (AIM2)

_generated 2026-07-23 12:58:29.904421+00:00 · read-only · window 2026-07-07 14:20:00 → 2026-07-23 00:00:00_

**Backbone:** vollständige wick-aware **5m**-OHLC-Kerzen (`candles`, 12× feiner als der 1h-Live-Monitor) für die Touch-Erkennung; **10s**-Ticks (`ticker_10s`) nur als Order-Resolver für SL-vs-TP-Reihenfolge innerhalb einer 5m-Kerze. **Geometrie:** immutable Cornix-Text (`telegram_outbox`), Original-SL/entry2/TP1-3. **Outcome-Ground-Truth:** `closed_ai_signals`.

> Warum nicht rein 10s: `ticker_10s` ist ein ~40s-Snapshot mit Lücken (Coverage-Median 0.25) und verpasst ~81% der SL-Touch-Events → eine reine Tick-Sim entkommt den Stops und verzerrt Realized ~2.7×. Die 5m-Kerze ist gap-frei und wick-aware.

Closed im Fenster: 1285 · Geometrie gematcht & gescored: **673** · ungematcht (Outbox-Retention): 604.
Gescorte-Trades-Span: 2026-07-10 18:48:31.207150 → 2026-07-22 22:15:37.406260 (Outbox-Retention verzerrt das Set zu **jüngeren** Trades — beim Lesen der Aggregate beachten).

## Validierung — `monitor`-Config (entry1-only, interne Targets) vs recorded closed_ai_signals

- targets_hit **exakt**: 97.92%  ·  **±1**: 99.26%
- Win/Loss (TP1-Touch) **Übereinstimmung**: 99.26%

> Restdivergenz kommt aus der feineren Auflösung (5m-Wick + echte Intra-Candle-Ordnung) gegenüber dem 1h-Monitor — die Sim ist hier bewusst *treuer* als die recorded-Outcome-Quelle.

## Realized-Aggregat je Config

| config | n | unlev mean% | unlev sum% | net sum% | leveraged sum% (n) | WR(TP1)% | Ø-Dauer med/mean h |
|---|--:|--:|--:|--:|--:|--:|--:|
| monitor | 673 | 0.4111 | 276.64 | 209.44 | 13796.0 (673) | 64.49 | 21.83/31.68 |
| dca10 | 673 | 0.0808 | 54.36 | 4.61 | 5822.5 (673) | 64.49 | 21.83/31.72 |
| cornix3 | 673 | 0.2512 | 169.03 | 119.18 | 8106.4 (673) | 64.64 | 20.67/30.51 |

**Lesehilfe:** `monitor` = 1:1-Reproduktion des Bot-Monitors (Validierungsanker). `cornix3` = was Cornix real handelt (DCA entry1/entry2, 3 publizierte TPs in Dritteln) — die Headline-Realized-Zahl und die Basis fürs Phase-2-Overlay.

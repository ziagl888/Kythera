# whitelist_v2 Flip — realisierte Entscheidungsgrundlage (T-2026-KYT-9050-007)

**Fenster:** 2026-05-15 00:00:00 → 2026-08-01 22:31:57.404259+00:00 (UTC)
**Snapshot:** 1590 Zellen, v2-Coverage 100.0%, Alter 0.38h
(Analyzer lebt)

## 1. Zell-Divergenz (heutiger Snapshot)

| Klasse | Zellen | Anteil |
|---|---:|---:|
| both_open | 94 | 5.9% |
| both_block | 98 | 6.2% |
| v2_would_block | 1395 | 87.7% |
| v2_would_open | 3 | 0.2% |
| v2_missing | 0 | 0.0% |

## 2. Echter Gate-Traffic

- Events gesamt: **4027**, davon zell-entschieden (flip-relevant): **2016**
- Gate-Rate offen: v1 **0.0%** → v2 **9.42%**
- ROM1-Forwards/Tag: v1 **40.52** → v2 (Prognose) **44.48**
- v1-Drift der Snapshot-Näherung: 1570/2016 = **77.88%** Übereinstimmung

## 3. Was die divergenten Signale REALISIERT haben

### 3a. Trigger-Leg (eigener Trade des Quell-Bots — symmetrisch, beide Seiten)

| Klasse | Events | mit Leg | zensiert | decided | WR% | Σ Move% | Ø netto% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2_would_open | 190 | 190 | 22 | 168 | 52.4 | -349.3 | -2.179 | -56.6 (8) |
| both_block | 1826 | 1823 | 169 | 1654 | 62.9 | 1802.0 | 0.991 | 7596.9 (1149) |
| unaffected | 2011 | 2007 | 794 | 1213 | 56.7 | 361.5 | 0.199 | -3378.3 (906) |

**Flip-Bilanz auf dem Trigger-Leg:** v2 nimmt weg Σ 0.0% (0 entschiedene Trades), v2 schaltet frei Σ -349.3% (168) → **Δ -349.3%** (unlevered Move).

### 3b. ROM1-Leg (das echte Geld — existiert nur auf der forwarded-Seite)

| Klasse | Events | mit ROM1-Leg | zensiert | decided | WR% | Σ Move% | Ø netto% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| unaffected | 2011 | 1942 | 849 | 1093 | 65.6 | 1811.1 | 1.560 | 146.4 (1) |

> `v2_would_open` hat strukturell KEIN ROM1-Leg: diese Signale wurden nie geforwardet, also nie als ROM1 gehandelt. Die zusätzlich freigeschaltete Seite ist in ROM1-Geld grundsätzlich nicht messbar — nur im Trigger-Leg (3a), und das trägt eine andere Geometrie (P1.10).

## 3c. Sauber vs. drift-kontaminiert (die belastbare Teilmenge)

Die Flip-Klasse vergleicht die AUFGEZEICHNETE v1-Entscheidung mit der HEUTIGEN v2-Zelle. Wo die heutige v1-Zelle nicht mehr zur aufgezeichneten Entscheidung passt, hat sich die Zelle seither bewegt — dann vergleicht die Klasse zwei verschiedene Zellstände, nicht v1 gegen v2. Nur `v1_agree` ist ein sauberer v1-vs-v2-Lesewert.

| Klasse | Teilmenge | Events | mit Leg | zensiert | decided | WR% | Σ Move% | Ø netto% | Σ lev% (n) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v2_would_open | v1_drifted | 190 | 190 | 22 | 168 | 52.4 | -349.3 | -2.179 | -56.6 (8) |

## 4. Über welchen v1-Pfad kam der divergente Traffic?

`insufficient_data` ist v1s Default-Open-Krücke (n < 30 in der Zelle), `wr_above_overall` / `counter_trend_specialist` sind v1-Entscheidungen AUF MERIT. Die Zell-Matrix und der Traffic beantworten das unterschiedlich.

### v2_would_open — Trigger-Leg nach v1-Pfad

| v1-Pfad | Events | mit Leg | zensiert | decided | WR% | Σ Move% | Ø netto% | Σ lev% (n) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| wr_below_overall | 190 | 190 | 22 | 168 | 52.4 | -349.3 | -2.179 | -56.6 (8) |

## 5. Aufschlüsselung nach Bot × Richtung

### v2_would_block — Trigger-Leg

_keine Events in dieser Klasse._

### v2_would_open — Trigger-Leg

| Bot | Dir | Events | mit Leg | zensiert | decided | WR% | Σ Move% | Ø netto% | Σ lev% (n) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| EPD1 | SHORT | 190 | 190 | 22 | 168 | 52.4 | -349.3 | -2.179 | -56.6 (8) |

## 6. Messgrenzen (gemessen, nicht angenommen)

- Snapshot-Näherung: `bot_regime_whitelist` ist UPSERT-only ohne Historie — der v2-Verdikt pro Event stammt aus dem heutigen Snapshot (2026-08-01 22:08:40.703564), nicht aus dem Stand zur Signal-Zeit. Die v1-Drift (77.88% Übereinstimmung über 2016 Events) misst diesen Fehler an der einzigen Achse, auf der beide Stände bekannt sind.
- Die historische Whitelist ist damit weiterhin NICHT rekonstruierbar (T-031-Befund bestätigt): weder `bot_regime_whitelist` noch `bot_regime_performance` führen eine Historie, und Bot 28 loggt pro Signal nur den v1-Pfad, nie den v2-Verdikt.
- `v2_would_open` hat kein ROM1-Leg — diese Signale wurden nie gehandelt. Die freigeschaltete Seite ist nur über den Trade des Quell-Bots messbar, der eine ANDERE Geometrie trägt als ROM1 (docs/REGIME_ORCHESTRATOR.md, P1.10).
- Trigger-Leg-Coverage < 100%: unmatched Events sind als `no_twin` gezählt, nicht als 0 gewertet. Ursachen: Signal noch offen, Trade nie eröffnet, Monitor-Lücke.
- WR ist TP1-Touch, PnL ist der target-gestaffelte unlevered Move (core.realized_pnl, T-115-Definition). `lev`-PnL ist exact-only — Coverage pro Zeile über `n_with_leg` ablesbar.

# K15 · SRX — Scratch-Reload-Exit (ABR1-Events)

**Task:** T-2026-CU-9050-137 · **Generiert (UTC):** 2026-07-16T08:49:15.305272+00:00
**Quelle:** `C:\Users\Michael\Documents\_X\staging_models\replay\abr1_replay_365d.jsonl` (read-only)

**Events:** 288211 simuliert (von 288281 im File, 288281 verwendbar; stride=1, 526 Coins; 70 ohne 4h-Kerzen übersprungen).
**Fenster:** 14 Tage · **Fees:** 0.1 % Round-Trip je Leg (walkforward_sim.FEE_PER_SIDE).

## VERDICT: `no_op_thesis_falsified`

Kriterium (Spec §K15 / Regel 8): Variante (b) muss (a) im **Ø-Netto-PnL in BEIDEN Chrono-Hälften (Val UND Test)** schlagen.

| Zelle | Δ Val (b–a) | Δ Test (b–a) | schlägt in beiden? |
|---|---|---|---|
| b · N=2 | -0.4046 | -0.2128 | nein |
| b · N=4 | -0.491 | -0.327 | nein |
| b · N=8 | -0.4926 | -0.3366 | nein |

## Kennzahlen je Variante (Netto-PnL in % des Nominals)

| Variante | n | WR % | Ø net | Median | p5 | p95 |
|---|---|---|---|---|---|---|
| (a) Baseline (Record) | 288211 | 43.05 | -0.1007 | -2.1621 | -9.0305 | 10.7387 |
| (aux) TP1-vs-TouchSL | 288211 | 55.84 | -0.1571 | 2.101 | -8.9935 | 7.6518 |
| (b) Scratch·TouchSL·N=2 | 288211 | 46.45 | -0.4094 | -0.6443 | -7.7949 | 6.498 |
| (b) Scratch·TouchSL·N=4 | 288211 | 48.89 | -0.5097 | -0.178 | -9.6039 | 6.6594 |
| (b) Scratch·TouchSL·N=8 | 288211 | 48.98 | -0.5153 | -0.1613 | -9.6765 | 6.6695 |
| (c) Scratch·CloseSL·N=2 | 288211 | 47.25 | -0.435 | -0.4986 | -8.1145 | 6.5868 |
| (c) Scratch·CloseSL·N=4 | 288211 | 49.84 | -0.5457 | -0.0263 | -10.2008 | 6.7757 |
| (c) Scratch·CloseSL·N=8 | 288211 | 49.94 | -0.5506 | -0.009 | -10.3112 | 6.7921 |

### Zyklen / Re-Entry (Scratch-Varianten)

| Zelle | Ø Zyklen | max | % mit Re-Entry |
|---|---|---|---|
| b · N=2 | 0.814 | 2 | 59.03 |
| b · N=4 | 0.938 | 4 | 59.03 |
| b · N=8 | 0.958 | 8 | 59.03 |
| c · N=2 | 0.868 | 2 | 62.46 |
| c · N=4 | 1.009 | 4 | 62.46 |
| c · N=8 | 1.033 | 8 | 62.46 |

## Chrono-Split (Val = frühere Hälfte, Test = spätere)

Val n=144105 (bis 2026-01-31T06:00:00+00:00), Test n=144106.

| Zelle | Ø net Val | Ø net Test |
|---|---|---|
| (a) Baseline | -0.012 | -0.1894 |
| (b) N=2 | -0.4166 | -0.4022 |
| (b) N=4 | -0.503 | -0.5164 |
| (b) N=8 | -0.5046 | -0.526 |
| (c) N=2 | -0.4499 | -0.4201 |
| (c) N=4 | -0.5445 | -0.5469 |
| (c) N=8 | -0.5436 | -0.5577 |

## Monats-Split (Ø net, Repräsentant N=4)

| Monat | n | (a) base | (b) N4 | (c) N4 |
|---|---|---|---|---|
| 2025-07 | 6585 | -0.3846 | -0.302 | -0.3687 |
| 2025-08 | 21616 | -0.9516 | -0.865 | -0.8847 |
| 2025-09 | 22160 | -0.4629 | -0.8721 | -0.915 |
| 2025-10 | 22985 | -0.0178 | -0.6591 | -0.7634 |
| 2025-11 | 21396 | 0.8785 | -0.4324 | -0.4505 |
| 2025-12 | 25309 | -0.2727 | -0.4956 | -0.5544 |
| 2026-01 | 24525 | 0.9902 | 0.331 | 0.3409 |
| 2026-02 | 21023 | -0.613 | -0.4355 | -0.399 |
| 2026-03 | 28060 | -0.4066 | -0.5186 | -0.5446 |
| 2026-04 | 27307 | -0.4124 | -0.9249 | -1.0264 |
| 2026-05 | 28322 | 0.357 | -0.354 | -0.3388 |
| 2026-06 | 26598 | -0.0979 | -0.4813 | -0.5348 |
| 2026-07 | 12325 | -0.2736 | -0.5096 | -0.5742 |

## Caveats

- **close_based_sl:** Variante (c) unterschätzt bei Hebel das Liquidationsrisiko — Liquidation ist Touch-basiert; Cross-Margin mildert, eliminiert das nicht.
- **survivorship:** Event-Population = ABR1-Walkforward über heute in coins.json handelbare Coins; delistete Paare fehlen → Verlust-Tail optimistisch (gilt für alle Varianten gleich, Vergleich intern konsistent).
- **baseline_asymmetry:** (a) ist die Original-Ladder (mehrere Targets); (b)/(c) verwenden TP1-First-Touch. `aux_geom_tp1_touchsl` isoliert den TP1-statt-Ladder-Effekt von der Scratch-Mechanik.
- **intra_candle:** Bei TP+SL in derselben 4h-Kerze gewinnt der SL (pessimistisch, wie walkforward_sim SL-first).
- **offline_only:** Der Trade-Monitor kennt weder Scratch-Exits noch Re-Entries — reine Offline-Studie, nichts geht in einen Bot.

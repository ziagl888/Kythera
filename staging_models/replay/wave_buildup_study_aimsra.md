# Wave-Buildup — Realized-vs-Unrealized (Phase A, T-2026-KYT-9050-041)

_generated 2026-07-25 17:36:59.201921+00:00 · read-only · models AIM+SRA · 20x assumed · window 2026-02-27 02:32:26.790675 → 2026-07-25 20:20:50.181014_

**Was das ist:** Follow-up zu T-035. Rekonstruiert die aggregierte offene **unrealized**-Welle und die realized-Ergebnisse der kuratierten S/R-AI-Bots (AIM, SRA) über die **volle Historie** aus den RECORDED Trades (`closed_ai_signals`, dedup Report-14-Survivor-Key) + Kerzen — **ohne** die immutable Cornix-Geometrie (die T-035 auf ~7d Outbox-Retention deckelte). Preis dafür: kein intra-trade DCA/TP-Laddering modelliert (First-Order-Welle). Hebel **pauschal 20x angenommen** (vor ~Juli nicht persistiert; Muster ist hebel-agnostisch). Realized = recorded entry→close-Move.

Trades gescort: **6950** · ohne Kerzen: 467.

## C1 — Realized-vs-Unrealized-Asymmetrie (die Prämisse)

| Segment | n | mean realized% | mean **peak**% (wick) | **giveback** | WR% | giveback p50/p90/p95 |
|---|--:|--:|--:|--:|--:|--:|
| ALL | 6950 | +38.2 | +184.44 | +146.25 | 39.1 | 118.7/287.8/371.8 |
| AIM | 5566 | +43.35 | +209.02 | +165.67 | 35.9 | 134.9/307.1/405.1 |
| AIM-LONG | 2170 | +34.51 | +207.84 | +173.32 | 33.9 | 122.3/345.8/502.2 |
| AIM-SHORT | 3396 | +49.0 | +209.78 | +160.78 | 37.2 | 140.9/295.5/360.4 |
| SRA | 1384 | +17.46 | +85.59 | +68.13 | 51.9 | 44.0/146.9/191.4 |
| SRA-LONG | 725 | +14.93 | +90.66 | +75.73 | 48.8 | 52.5/150.6/204.7 |
| SRA-SHORT | 659 | +20.24 | +80.02 | +59.78 | 55.2 | 37.1/141.9/176.7 |

**Kern der Beobachtung, belegt:** von den Verlust-Trades standen **85.4% mal ≥+10 %**, 77.6% ≥+25 %, **60.3% ≥+50 %**, 36.3% ≥+100 %, 16.2% ≥+200 %** (leveraged, wick) im Plus — und schlossen dann im Verlust. Gewinne verdampfen, Verluste werden voll realisiert. Ø-Trade: realized +38.2% vs Peak +184.44% → **+146.25% Giveback**.

Aggregat-Welle: max Σ offene unrealized **+39269** Margin-Einheiten (2026-06-06 07:00:00), bis **507** gleichzeitig offen (Ø 112.5).

## C2 — Cooldown-Probe: Expectancy N Tage NACH einem großen Wellen-Peak

Baseline (alle) real_unlev **+0.034%** · 27 Peak-Events (p85).

| Kohorte | n | real_unlev% | Δ vs Baseline |
|---|--:|--:|--:|
| 1 Tag(e) nach Peak | 1467 | -0.306 | -0.340 |
| 2 Tag(e) nach Peak | 2644 | +0.064 | +0.030 |
| 3 Tag(e) nach Peak | 3115 | +0.083 | +0.049 |
| 5 Tag(e) nach Peak | 3756 | +0.188 | +0.154 |
| 7 Tag(e) nach Peak | 4211 | +0.226 | +0.191 |

**Verdikt C2:** nur die ersten ~24h nach einem Peak sind messbar schwächer; Tag 2–7 sind NICHT schlechter (eher besser). Die „nach großer Welle 3–5 Tage aussetzen“-Idee hätte historisch keine Expectancy gerettet, sondern gute Tage verschenkt. **Der Hebel liegt auf der Close-Seite, nicht beim Re-Entry-Timing.**

## CEIL — Capture-Ceiling & risiko-adjustierte Auflösung

HOLD (actual) Σ lev **+265461** · PERFECT-PEAK (Hindsight-Obergrenze) Σ lev +1281882 (~4.8× hold, unerreichbar).

| Trailing X% | Σ lev | mean lev% | vs hold | % der Ceiling | **Sharpe lev** | mean unlev% | trig% |
|--:|--:|--:|--:|--:|--:|--:|--:|
| **hold** | +265461 | +38.2 | — | — | **+0.204** | +0.034 | 0 |
| 10% | +313269 | +45.07 | +47808 | 4.7% | **+0.534** | +1.955 | 93.7 |
| 15% | +293636 | +42.25 | +28175 | 2.8% | **+0.526** | +1.814 | 93.7 |
| 20% | +274228 | +39.46 | +8767 | 0.9% | **+0.517** | +1.675 | 93.7 |
| 25% | +255094 | +36.7 | -10367 | -1.0% | **+0.507** | +1.537 | 93.7 |
| 30% | +236228 | +33.99 | -29233 | -2.9% | **+0.495** | +1.401 | 93.7 |
| 40% | +199268 | +28.67 | -66192 | -6.5% | **+0.467** | +1.135 | 93.6 |
| 50% | +166791 | +24.0 | -98670 | -9.7% | **+0.409** | +0.901 | 93.1 |

**Die Umkehr:** auf der leveraged **Summe** schlägt Trailing hold kaum/nicht (die Summe wird von wenigen uncapped Fat-Tail-Treffern dominiert, Trailing kappt die — bestätigt T-035). **Risiko-adjustiert dreht es:** per-Trade **Sharpe lev +0.204 (hold) → +0.534 (Trailing 10 %)** — Trailing ~halbiert die Streuung und hebt den Mittelwert. Unlevered: mean +0.034%/Trade (hold, ~breakeven) → +1.955%/Trade (Trailing 10 %).

### Kompoundierende Equity (fixe Einsatz-Fraktion, chronologisch nach close) — das reale Konto

| Einsatz/Trade | HOLD final | HOLD MaxDD | Trailing 10% final | Trailing 10% MaxDD |
|--:|--:|--:|--:|--:|
| 1% | ×1.01e+11 | **73.8%** | ×2.95e+13 | **11.6%** |
| 2% | ×1.09e+21 | **93.3%** | ×4.79e+26 | **22.0%** |
| 5% | ×1.65e+46 | **99.9%** | ×7.8e+64 | **46.9%** |

**Fazit Phase A:** (1) Die Asymmetrie ist real und groß — Prämisse bestätigt. (2) Die Cooldown/Re-Entry-Idee wird von den Daten NICHT gestützt. (3) Ein enger **Trailing-Close (10–15 %)** ist risiko-adjustiert und kompoundierend **klar überlegen** (höherer Sharpe, mehr Compounding, ~6× kleinerer Drawdown) — T-035s „hold gewinnt“ war ein Artefakt der Leveraged-**Summe**. Der nächste Schritt ist die Validierung auf dem T-035-High-Fidelity-Harness (5m-Wick + 10s-Resolver, Sharpe/MaxDD statt Summe), inkl. der entry2-als-SL-Frage.

## Ehrliche Grenzen

- First-Order-Welle: recorded entry/close als Ground-Truth, KEIN intra-trade DCA/TP-Laddering — die unrealized-Amplitude ist leicht überschätzt (Peak-Timing unberührt). Die T-035-Phase-2-Harness ist die laddering-treue Referenz (dort aber nur ~7d Outbox-Fenster).
- Hebel pauschal 20x (vor ~Juli nicht persistiert). Das Muster (Giveback, Sharpe-Umkehr) ist hebel-agnostisch; die absoluten leveraged-Zahlen skalieren mit dieser Annahme + dem -100%-Clamp.
- Trailing-Sim: 1h-Wick, Peak-vor-Trigger auf derselben Kerze (leicht optimistisch beim Trigger-Level). Ein 5m/10s-Resolver (T-035) verschärft das; die RICHTUNG (Sharpe/MaxDD-Vorteil) ist robust.
- Compounding sequenziell nach close → ignoriert Gleichzeitigkeit (bis zu den oben genannten gleichzeitig offenen Trades, Cross-Margin). Die absoluten Multiples (×1e26) sind NICHT wörtlich — Ratio + MaxDD sind das Signal.
- Live/Shadow-Gate-Zustand ist zeit-variabel; hier keine Gate-Filterung — es sind die AIM/SRA-Strategien über alle Generationen (AIM1→AIM2, SRA1→SRA2).

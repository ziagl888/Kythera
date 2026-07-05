# Report 20 — AIM2-Training: Ergebnisse & Robustheitsprüfung

**Datum:** 2026-07-05 · **Kontext:** Operator-Entscheidung, AIM1 ad acta zu legen und durch AIM2
zu ersetzen (docs/AIM2_DESIGN.md, Report 15 S7). Pipeline: `tools/aim2_build_dataset.py` →
`tools/aim2_train.py`, Artefakt in `staging_models/master_meta_model_aim2.pkl` (+ `_report.json`).

## 1. Datensatz

115.018 Events (2026-02-25 → 07-05), davon 109.570 gelabelt (5.448 `open_at_end` ausgeschlossen).
Quellen: 43k gepostete AI-Signale + 198k Conv (FIFO 25% / Volume 35% deterministisch untersampelt,
Gewichte im Training). Label = First-Touch TP1-vor-SL der as-of rekonstruierten Smart-Targets-
Geometrie (`simulate_exit`, Fees, SL-first konservativ, 14d-Kappe). Basis: **WR 54,1%,
ø Replay-PnL −0,61%/Trade** — der ungefilterte Signalstrom verliert nach Fees (deckt Report 14).

TZ-Neuvermessung: ALLE Writer von `ml_predictions_master`/`*_trades_master` stempeln
PG-Lokalzeit (Europe/Bucharest); `regime_history.ts` = naive UTC; Kerzen = timestamptz.
Konvertierung im Builder; der alte AIM1-Bot verglich Lokal gegen UTC (≈3h-Versatz, R07-AIM1-a).

## 2. Hauptlauf (chrono 70/15/15, 7d-Purge; Test = 01.06.–05.07.)

| Metrik | Wert |
|---|---|
| AUC val / test | 0,656 / 0,686 |
| Brier test (kalibriert) | 0,224 |
| Kalibrierung | **monoton**: Bucket 0,0–0,1 → 7,6% WR … 0,9–1,0 → 89,6% WR (AIM1-Inversion beseitigt) |
| Operating Point (Val-Replay-PnL) | thr = 0,61 |
| Gate-Uplift test | ohne Gate **−0,69%**/Trade → mit Gate **+1,92%**/Trade, WR 70,5%, Pass-Rate 34,2% (n=5.628/16.436) |
| Monatlich (gated) | Jun +1,80% (n=5.105, WR 68,9%) · Jul +3,48% (n=523, WR 80,7%) |

Top-Features: ema_200_dist, direction_num, ALT-Kontext, Support/Resistance-Distanz,
entry_drift, Regime-CHOP, Quell-Identität/Trailing-WR, Schwarm. **ATR nicht in den Top 25**
— der AIM1-Fehlermodus (Volatilitäts-Detektor) ist nicht reproduziert.

## 3. Robustheitsprüfungen (alle bestanden)

1. **Dumme Baselines versagen out-of-time** — der Uplift ist NICHT verkappte Quellen-Auswahl:
   Quellen-Filter (positive Train-Quellen) → **−0,94%**/Trade; Quelle+Richtung → **−0,71%**;
   beide ≤ „kein Gate" (−0,69%). Bestätigt erneut die Batch-E-These, dass statische Gates nicht
   generalisieren — AIM2s Mehrwert ist kontextabhängige Selektion INNERHALB der Quellen.
2. **Zweiter OOT-Fold** (Test = 18.04.–01.06.): AUC 0,61, Uplift −0,55% → **+0,17%**/Trade
   (thr 0,63, Pass 20,4%). Dünner, aber positiv; monatlich Apr +0,07 / Mai +1,54.
   **Kein Testmonat Apr–Jul negativ.** Ehrliche Erwartung: Vorzeichen robust, Magnitude schwankt.
3. **Label-Lookahead-Probe** (Signalstunden-Kerze übersprungen, 60-Symbole-Sample, 13.888
   gemeinsame Events): 0,7% Label-Flips, symmetrisch (53 W→L vs 45 L→W), WR 0,532→0,531.
   Die Replay-Konvention verzerrt nichts.
4. **Cluster-Check:** 14.832 von 16.436 Test-Events sind distinkte (Coin, Stunde, Richtung)-
   Entscheidungen — kaum Korrelations-Inflation.

## 4. Verbleibende Vorbehalte

- **Fill-Annahme:** Replay füllt instantan zu entry1 (Limit-Realität kann schlechter sein) —
  gleiche Einschränkung wie alle Batch-E-Replays; genau dafür ist die Shadow-Phase da.
- Testfenster insgesamt Feb–Jul 2026, ein Marktregime-Zyklus; `open_at_end`-Ausschluss
  benachteiligt langsame Trades am Datenrand.
- Conv-Trailing-WR fehlt (nur AI-Quellen haben `closed_ai_signals`-Historie).

## 5. Empfehlung (Rollout-Gates aus dem Design-Doc)

Gate 1 (OOT-Uplift > 0 nach Fees) ist **bestanden**. Nächster Schritt: Artefakt aus staging in
den Repo-Root kopieren und Bot 15 **im Shadow-Modus** entparken (postet nichts — schreibt nur
`ml_predictions_master`-Zeilen mit model_name='AIM2'). Nach 4–8 Wochen: Shadow-WR-CI gegen
Break-even → erst dann `AIM2_LIVE_POSTING=1`. Abbruchkriterium: Shadow-WR-CI unter Break-even.

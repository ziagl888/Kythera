# AIM2 — Neubau des Master-Meta-Modells (ersetzt AIM1)

**Stand:** 2026-07-05 · **Beschluss:** AIM1 wird ad acta gelegt (Audit: verlässlich invertiert,
Note F, Dossier `audit_reports/dossiers/AIM1.md`). AIM2 übernimmt Bot-Slot 15, Channel
(`CH_MASTER`) und Posting-Flow unverändert. Bauplan = Report 15 S7 auf dem Batch-E-Gerüst.

## 1. Warum kein Retrain von AIM1

Die Inversion (conf>0,9 → 9,3% WR) hat vier bewiesene Ursachen im Trainer, nicht im Bot:
Volatilitäts-Label (X-R1), `round('1h')`-Lookahead-Join, totes Identity-Vokabular
(Overlap 2/22 bzw. 0/5), keine Kalibrierung. Batch E hat bestätigt: Retrain auf derselben
Pipeline reproduziert das invertierte Volatilitätsmodell. → Neubau.

## 2. Rolle

**Ranker/Gate über Quellsignale**, kein eigenständiger Alpha-Generator. AIM2 beantwortet je
Quellsignal genau die Entscheidung des Bots: *„Hätte ein AIM1-artiger Trade (Smart-Targets-
Geometrie zum Signalzeitpunkt) TP1 vor SL getroffen?"* Erwartung realistisch halten
(Batch-E-Kernthese: kein Gate zeigte bisher robuste Out-of-Time-Expectancy): Nutzen =
Selektion/Priorisierung, Misserfolg = sauberer Beleg, dass der Slot dauerhaft zu bleibt.

## 3. Trainings-Events

| Quelle | Zeitraum | Volumen | Sampling |
|---|---|---|---|
| `ml_predictions_master` posted=true, model≠AIM1 | 25.02.–heute | ~39,6k | 100% |
| `active/closed_trades_master` (conv) | 25.02.–heute | ~206k | FIFO 25%, Volume Indicator 35%, Rest 100% (deterministisch via md5-Hash) |

Zeitzonen-Vertrag (Step-2 R3, hier neu vermessen): **alle** Writer von
`ml_predictions_master`/`*_trades_master` stempeln `time` in PG-Lokalzeit
(Europe/Bucharest, inkl. DST-Wechsel 29.03.) — Konvertierung nach UTC via `tz_localize`.
`regime_history.ts` ist naive UTC. Kerzen sind `timestamptz`.

## 4. Label (X-R1-Fix)

Je Event: `entry` = Close der letzten **geschlossenen** 1h-Kerze vor dem Event;
Geometrie = `calculate_smart_targets(df=win1h)` mit Fenster bis zu dieser Kerze (as-of, kein
Lookahead); Replay = `simulate_exit` aus `tools/walkforward_sim.py` (wick-aware First-Touch,
SL-first bei Ambiguität, Fees, Monitor-Trailing) über `targets[:3]`, Horizont-Kappe 14 Tage.
`outcome_tp1` = Klassifikationslabel; `net_pnl_pct` (Cornix-Ladder-Approximation) = Grundlage
der Threshold-Wahl. `open_at_end` → vom Training ausgeschlossen.

## 5. Features (geteilter Builder `core/aim2_features.py`)

Trainer und Bot importieren **denselben** Builder (MIS1-Muster aus e84bc7d):

- **Markt** (Zeile der letzten geschlossenen 1h-Kerze, floor−1-Join): dist-% zu ema_9/21/50/200,
  kama_21, wma_21, boll_20 (3), donchian_20 (3), support/resistance/trendline_price;
  rsi_6/14, tsi, macd dif/dea (12/26/9), trendline_slope, r_squared; atr_14/atr_21 als %-close;
  trend_direction-One-Hots.
- **Regime** (`regime_history` asof, der 2025 fehlende Prädiktor): regime- + alt_context-One-Hots,
  confidence/_btc/_alt, btc_return_1h/4h, btc_atr_1h/4h_pct, btcdom_return_24h, Staleness (min).
- **Schwarm** (5d-Fenster je Coin, **ohne AIM1/AIM2 und ohne das Event selbst** — F6-Fix):
  total/long/short, Richtungs-Prob, Alter des letzten Signals, Konfluenz same-dir 4h,
  distinct Quellen same-dir 4h.
- **Quelle:** One-Hot aus **DB-Vokabular zur Trainingszeit** (nicht hardcoded; Liste wandert ins
  Artefakt), source_conf (AI: Modell-Confidence; conv: Mapping wie Bot 15), Trailing-WR 30d aus
  `closed_ai_signals` (win := status~TARGET oder targets_hit≥1; identische Semantik in Trainer
  und Serving), n-Basis, entry_drift_pct (Close vs. Quell-Entry), direction_num.
- **Bewusst draußen:** absolute Preise/Skalen (Ticker-Leakage), AIM1-Historie, Rohvolumen.

## 6. Training (X-R2/R4-Fix)

Chronologischer 70/15/15-Split mit 7-Tage-Purge-Gap (P1.29). XGBoost binär (hist).
Early Stopping auf Val. **Isotonic-Kalibrierung auf Val. Threshold-Wahl per Replay-Netto-PnL
auf Val** (nicht Formel, nicht Test). Test bleibt unberührt bis zum Abschlussreport.
Report: AUC/Brier, Reliability-Buckets (kalibriert vs. Replay-Outcome), Gate-Uplift
(PnL/Trade mit vs. ohne Gate auf Test), Per-Quelle-Breakdown. Artefakt **nur nach
`staging_models`** (P1.35): model, features, threshold, calibrator, vocab, meta.

## 7. Serving (Bot 15 → AIM2)

- Artefakt `master_meta_model_aim2.pkl`; Deploy = bewusstes Kopieren aus staging (Operator).
- Feature-Aufbau ausschließlich über `core/aim2_features.py`; `reindex` auf Artefakt-Featureliste
  mit **Parity-Guard** (Warnung, wenn Nicht-Null-Anteil unter Schwelle → OOD-Verdacht = P0.13-Wache).
- Schwarm-/Historien-Query schließt `model_name IN ('AIM1','AIM2')` aus (F6-Fix).
- Posting-Flow, Channel, Cornix-Format unverändert; `ai_signals.model='AIM2'` (saubere Attribution,
  AIM1-Statistik bleibt abgeschlossen). MIN_CONFIDENCE kommt aus dem Artefakt (Val-Operating-Point).
- Modell-Reload: 1×/Tag statt nie (R07-AIM1-b).

## 8. Rollout-Gates

1. Out-of-Time-Test zeigt Gate-Uplift > 0 nach Fees, Reliability monoton → sonst Stopp, Slot bleibt zu.
2. **4–8 Wochen Shadow** (`ml_predictions_master`, posted=false) — Shadow-WR-CI vs. Break-even.
3. Erst danach Entparken von Bot 15 mit Posting. Abbruchkriterium vorab: Shadow-WR-CI unter
   Break-even → zurück in den Park.

## 9. Artefakte & Zuständigkeiten

Neu: `core/aim2_features.py`, `tools/aim2_build_dataset.py`, `tools/aim2_train.py`, dieser Plan.
Umbau: `15_ai_master_bot.py`. Keine Berührung mit dem parallelen ABR1-Rework
(`18_ai_abr1_bot.py`, `tools/walkforward_sim.py`, `tools/retrain_from_replay.py` — nur Import).

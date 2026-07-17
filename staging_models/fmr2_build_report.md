# K4 · FMR2 — Funding-Extreme-MR mit Normalisierungs-Exit (Code-Prep Build Report)

**Task:** T-2026-CU-9050-146 · **status=partial/smoke** · **Datum:** 2026-07-17
**Binding design:** `docs/NEW_IDEAS_BOTS.md` §"FMR2 — eigener Exit-Pfad" +
`docs/MODEL_CANDIDATES_SPEC_2026-07.md` §K4.

**CODE-PREP ONLY:** Es lief KEIN echter Voll-Retrain (Ein-Job-Regel, Operator-gegated)
und es wurde KEIN Bot / kein Live-Pfad angefasst. Der Bot-31-Exit-Loop (Schritt 4 des
Designs) ist bewusst NICHT gebaut.

## Reuse-vs-Extend-vs-Build (Ein-Zeilen-Verdikt)

**EXTEND** durchgehend — die drei bestehenden Research-Pipeline-Dateien wurden additiv
erweitert (Exit-Predikat + Konstanten in `core/research_features.py`, V2-Labeling-Pfad in
`tools/fmr1_build_dataset.py`, FMR2-Strategie-Eintrag in `tools/new_models_train.py`);
nichts neu erfunden, V1/FMR1 bleibt bit-identisch als Default erhalten.

## Acceptance Criteria (§K4 / FMR2 design, binary)

- [x] **Exit-Predikat + Konstanten in `core/research_features.py`** (EINE Quelle für
  Builder UND künftigen Bot). `fmr2_funding_normalized(direction, funding_cs_pctl,
  funding_z_30d)`: SHORT normalisiert sobald `funding_cs_pctl < FMR2_SHORT_EXIT_CS_PCTL
  (0.80)` ODER `funding_z_30d < FMR2_SHORT_EXIT_Z (1.0)`; LONG symmetrisch
  (`> 0.20` / `> −1.0`). — *verifiziert:* `backtest/test_fmr2_exit.py::test_short_exit_predicate`,
  `::test_long_exit_predicate_symmetric` (grün).
- [x] **Time-Stop 9 Settlements / 3 Tage** als benannte Konstante
  `FMR2_TIME_STOP_SETTLEMENTS = 9`. — *verifiziert:*
  `::test_walk_time_stop_at_9_settlements` (settlements == 9, reason `time_stop`).
- [x] **Harter Katastrophen-SL bleibt** — `FMR2_CATASTROPHE_SL_PCT = 15.0` (Konvention
  K1-Grid / P2.27), `fmr2_catastrophe_sl(direction, entry)`; im Walk als First-Touch
  auf den 1h-Kerzen (touch-basiert, Liquidations-realistisch). — *verifiziert:*
  `::test_walk_catastrophe_sl_first_touch`, `::test_catastrophe_sl_prices`.
- [x] **Native-NaN / as-of / R1** — Predikat ist fail-safe (NaN → nicht normalisiert →
  weiter halten); der Settlement-Z-Score wird pro Settlement as-of neu gerechnet (nur
  Sätze bis einschl. Settlement); Walk startet bei `entry_idx+1` (kein Lookahead).
  — *verifiziert:* `::test_predicate_nan_is_fail_safe`.
- [x] **V2-Labeling = Settlement-Exit, NICHT First-Touch-TP/SL** (der FMR1-Bug).
  `tools/fmr1_build_dataset.py --label-version v2` labelt via
  `simulate_normalization_exit`: Label = Vorzeichen des Netto-PnL am Exit-Preis der
  **Settlement-Kerze** (Close), nicht `outcome_tp1` von `simulate_exit`.
  — *verifiziert:* `::test_walk_normalized_exit`,
  `::test_walk_normalized_prices_at_settlement_close` (Exit am Settlement-Close, PnL =
  reine Round-Trip-Fees bei flachem Preis).
- [x] **V1 (FMR1) bleibt intakt** — `--label-version` default `v1`; der alte
  Smart-Target-/`simulate_exit`-Pfad ist unverändert und weiterhin der Default-Output
  (`fmr1_events.jsonl`). V2 → `fmr2_events.jsonl`.
- [x] **Retrain-Scaffold (Chrono-Split, Purge, `pick_threshold`)** — FMR2 in
  `STRATEGIES` von `tools/new_models_train.py`: `kind=binary`, `features=FMR2_FEATURES`
  (== FMR1-Feature-Vertrag), `purge_days=3` (>= 9-Settlement-Horizont). Wiederverwendet
  den bestehenden `train_binary`-Pfad (70/15/15-Chrono-Split mit Purge-Gap,
  Isotonic-Kalibrierung, Threshold-Wahl per Val-Netto-PnL auf ROHEN Probs).
  — *verifiziert:* Smoke-Lauf unten, exit 0.
- [x] **`meta.model_id = FMR2`** — Artefakt trägt `model_id=FMR2` (aus `STRATEGIES`).
  — *verifiziert:* `staging_models/fmr2_model_smoke_report.json` + joblib-Load
  (`model_id= FMR2`, 15 Features, `kind=binary`, `purge=3`).
- [x] **Artefakt NUR nach staging_models/** — kein Repo-Root-Deploy; der Trainer loggt
  ausdrücklich "NUR staging". Smoke-Artefakt: `staging_models/fmr2_model_smoke.pkl`.
- [x] **Operator-Gate-Grenze eingehalten** — kein echter Retrain (nur Smoke auf
  synthetischem Mini-Datensatz), kein Bot-31-Exit-Loop, kein Live-/DB-Schreibpfad,
  keine Promotion.

## Smoke (DB-frei — Build-Maschine hat keine DB-Credentials)

1. `py -3.13 backtest/test_fmr2_exit.py` → **9/9 grün, exit 0** (Predikat + Walk:
   time-stop, normalized, catastrophe-SL, open_at_end, settlement-close-pricing).
2. Synthetischer Mini-Datensatz (600 Events, injiziertes Signal `net ~ 1.5·z`) →
   `py -3.13 tools/new_models_train.py --strategy fmr2 --events <smoke.jsonl>
   --out staging_models/fmr2_model_smoke.pkl --min-val-trades 10` → **exit 0**:
   Split train=408/val=78/test=90, AUC val 0.976 / test 0.843, Val-OP thr=0.46,
   Artefakt + `_report.json` geschrieben. (Zahlen sind SYNTHETISCH — nur Pipeline-Beweis,
   kein Edge-Statement.)

## Write-time Grounding (gegen echte Quelle vor dem Schreiben verifiziert)

- `funding_cs_pctl` = Cross-Section-`rank(pct=True)` je `funding_time` über alle Coins —
  `tools/fmr1_build_dataset.py::build_events` (`pctl = g["funding_rate"].rank(pct=True)`);
  im V2-Walk pro Settlement über eine vorberechnete `fund["cs_pctl"]`-Spalte verfügbar.
- `funding_z_30d` = `(cur_bps − mean(hist90_bps)) / std(hist90_bps)`,
  `hist90 = letzte FMR1_HISTORY_SETTLEMENTS (90)` Sätze — `core/research_features.funding_stats`.
  Der Walk repliziert exakt diese Formel pro Settlement.
- FMR1-Dataset-Schema (`symbol, ts, direction, weight, entry, sl, targets, label,
  net_pnl_pct, exit_reason, risk_pct, features`) — `tools/fmr1_build_dataset.py::main` +
  Konsument `tools/new_models_train.py::load_events`/`train_binary` (liest `label`,
  `net_pnl_pct`, `weight`, `features`). V2 schreibt dasselbe Schema (+ `settlements`).
- `new_models_train`-Split/Threshold-API: `chrono_split(meta, purge_days)` (Quantile
  0.70/0.85, Purge-Gap), `pick_threshold(raw_val, pnl, w, min_trades)` (Grid 0.30–0.80),
  Threshold auf ROHEN Val-Probs (Gate-Konvention) — unverändert wiederverwendet.
- Settlement-Kerzen-Pricing: Exit-Preis = `closes[i]` der 1h-Kerze bei/nach der
  Settlement-Zeit (8h-Raster auf dem 1h-Kerzen-Raster); First-Touch-SL via
  `lows[i]<=sl` (LONG) / `highs[i]>=sl` (SHORT); Fees `2·FEE_PER_SIDE`
  (`tools/walkforward_sim.FEE_PER_SIDE = 0.0005`).

## Nicht implementiert (bewusst, Operator-gegated)

- **Bot-31-Exit-Loop** (Close-Command via `send_telegram` → `telegram_outbox`, eigene
  Rows per `DELETE … RETURNING` → `closed_ai_signals status='CLOSED_FUNDING_NORMALIZED'`,
  eigener `CH_FMR1`-Channel) — Design-Schritt 4, NUR bei Val+Test-positivem Retrain und
  ausschließlich durch Michi.
- **Echter Voll-Retrain** auf dem realen `fmr2_events.jsonl` (Ein-Job-Regel, VPS,
  read-only DB) — Operator-Slot.

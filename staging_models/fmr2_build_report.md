# K4 · FMR2 — Funding-extreme MR with normalization exit (code-prep build report)

**Task:** T-2026-CU-9050-146 · **status=partial/smoke** · **Date:** 2026-07-17
**Binding design:** `docs/NEW_IDEAS_BOTS.md` §"FMR2 — own exit path" +
`docs/MODEL_CANDIDATES_SPEC_2026-07.md` §K4.

**CODE-PREP ONLY:** NO real full retrain ran (one-job rule, operator-gated)
and NO bot / no live path was touched. The bot 31 exit loop (step 4 of
the design) is deliberately NOT built.

## Reuse-vs-extend-vs-build (one-line verdict)

**EXTEND** throughout — the three existing research pipeline files were extended
additively (exit predicate + constants in `core/research_features.py`, V2 labeling
path in `tools/fmr1_build_dataset.py`, FMR2 strategy entry in
`tools/new_models_train.py`); nothing reinvented, V1/FMR1 stays bit-identical as
the default.

## Acceptance Criteria (§K4 / FMR2 design, binary)

- [x] **Exit predicate + constants in `core/research_features.py`** (ONE source for
  builder AND future bot). `fmr2_funding_normalized(direction, funding_cs_pctl,
  funding_z_30d)`: SHORT normalises as soon as `funding_cs_pctl < FMR2_SHORT_EXIT_CS_PCTL
  (0.80)` OR `funding_z_30d < FMR2_SHORT_EXIT_Z (1.0)`; LONG symmetric
  (`> 0.20` / `> −1.0`). — *verified:* `backtest/test_fmr2_exit.py::test_short_exit_predicate`,
  `::test_long_exit_predicate_symmetric` (green).
- [x] **Time stop 9 settlements / 3 days** as a named constant
  `FMR2_TIME_STOP_SETTLEMENTS = 9`. — *verified:*
  `::test_walk_time_stop_at_9_settlements` (settlements == 9, reason `time_stop`).
- [x] **Hard catastrophe SL stays** — `FMR2_CATASTROPHE_SL_PCT = 15.0` (convention
  K1 grid / P2.27), `fmr2_catastrophe_sl(direction, entry)`; in the walk as first
  touch on the 1h candles (touch-based, liquidation-realistic). — *verified:*
  `::test_walk_catastrophe_sl_first_touch`, `::test_catastrophe_sl_prices`.
- [x] **Native NaN / as-of / R1** — predicate is fail-safe (NaN → not normalized →
  keep holding); the settlement Z-score is recomputed per settlement as-of (only
  sets up to and including the settlement); walk starts at `entry_idx+1` (no
  lookahead). — *verified:* `::test_predicate_nan_is_fail_safe`.
- [x] **V2 labeling = settlement exit, NOT first-touch TP/SL** (the FMR1 bug).
  `tools/fmr1_build_dataset.py --label-version v2` labels via
  `simulate_normalization_exit`: label = sign of the net PnL at the exit price of
  the **settlement candle** (close), not `outcome_tp1` from `simulate_exit`.
  — *verified:* `::test_walk_normalized_exit`,
  `::test_walk_normalized_prices_at_settlement_close` (exit at settlement close, PnL =
  pure round-trip fees at a flat price).
- [x] **V1 (FMR1) stays intact** — `--label-version` default `v1`; the old
  smart-target/`simulate_exit` path is unchanged and remains the default output
  (`fmr1_events.jsonl`). V2 → `fmr2_events.jsonl`.
- [x] **Retrain scaffold (chrono split, purge, `pick_threshold`)** — FMR2 in
  `STRATEGIES` of `tools/new_models_train.py`: `kind=binary`, `features=FMR2_FEATURES`
  (== FMR1 feature contract), `purge_days=3` (>= 9-settlement horizon). Reuses
  the existing `train_binary` path (70/15/15 chrono split with purge gap,
  isotonic calibration, threshold choice by val net PnL on RAW probs).
  — *verified:* smoke run below, exit 0.
- [x] **`meta.model_id = FMR2`** — artifact carries `model_id=FMR2` (from `STRATEGIES`).
  — *verified:* `staging_models/fmr2_model_smoke_report.json` + joblib load
  (`model_id= FMR2`, 15 features, `kind=binary`, `purge=3`).
- [x] **Artifact ONLY into staging_models/** — no repo-root deploy; the trainer explicitly
  logs "staging ONLY". Smoke artifact: `staging_models/fmr2_model_smoke.pkl`.
- [x] **Operator gate boundary respected** — no real retrain (only smoke on a
  synthetic mini dataset), no bot 31 exit loop, no live/DB write path,
  no promotion.

## Smoke (DB-free — build machine has no DB credentials)

1. `py -3.13 backtest/test_fmr2_exit.py` → **9/9 green, exit 0** (predicate + walk:
   time stop, normalized, catastrophe SL, open_at_end, settlement-close pricing).
2. Synthetic mini dataset (600 events, injected signal `net ~ 1.5·z`) →
   `py -3.13 tools/new_models_train.py --strategy fmr2 --events <smoke.jsonl>
   --out staging_models/fmr2_model_smoke.pkl --min-val-trades 10` → **exit 0**:
   split train=408/val=78/test=90, AUC val 0.976 / test 0.843, val OP thr=0.46,
   artifact + `_report.json` written. (Numbers are SYNTHETIC — pipeline proof only,
   no edge statement.)

## Write-time grounding (verified against the real source before writing)

- `funding_cs_pctl` = cross-section `rank(pct=True)` per `funding_time` across all coins —
  `tools/fmr1_build_dataset.py::build_events` (`pctl = g["funding_rate"].rank(pct=True)`);
  in the V2 walk available per settlement via a precomputed `fund["cs_pctl"]` column.
- `funding_z_30d` = `(cur_bps − mean(hist90_bps)) / std(hist90_bps)`,
  `hist90 = letzte FMR1_HISTORY_SETTLEMENTS (90)` settlements — `core/research_features.funding_stats`.
  The walk replicates this formula exactly per settlement.
- FMR1 dataset schema (`symbol, ts, direction, weight, entry, sl, targets, label,
  net_pnl_pct, exit_reason, risk_pct, features`) — `tools/fmr1_build_dataset.py::main` +
  consumer `tools/new_models_train.py::load_events`/`train_binary` (reads `label`,
  `net_pnl_pct`, `weight`, `features`). V2 writes the same schema (+ `settlements`).
- `new_models_train` split/threshold API: `chrono_split(meta, purge_days)` (quantiles
  0.70/0.85, purge gap), `pick_threshold(raw_val, pnl, w, min_trades)` (grid 0.30–0.80),
  threshold on RAW val probs (gate convention) — reused unchanged.
- Settlement candle pricing: exit price = `closes[i]` of the 1h candle at/after the
  settlement time (8h grid on the 1h candle grid); first-touch SL via
  `lows[i]<=sl` (LONG) / `highs[i]>=sl` (SHORT); fees `2·FEE_PER_SIDE`
  (`tools/walkforward_sim.FEE_PER_SIDE = 0.0005`).

## Not implemented (deliberately, operator-gated)

- **Bot 31 exit loop** (close command via `send_telegram` → `telegram_outbox`, own
  rows via `DELETE … RETURNING` → `closed_ai_signals status='CLOSED_FUNDING_NORMALIZED'`,
  own `CH_FMR1` channel) — design step 4, ONLY on val+test-positive retrain and
  exclusively by Michi.
- **Real full retrain** on the real `fmr2_events.jsonl` (one-job rule, VPS,
  read-only DB) — operator slot.

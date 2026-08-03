# Research-Bots 30–33 — PEX1 / FMR1 / TRM1 / FIF1

**Status:** 2026-07-06 · **Source:** `audit_reports/15_strategy_proposals.md` (S6, S8, S10, S11)
· **Task:** T-2026-CU-9050-019

Four new ML bots as a cohort in the shared Telegram channel **`CH_NEW_IDEAS`**
(operator decision 2026-07-06: the channel is the test environment, signals go
live directly; `NEW_IDEAS_LIVE_POSTING=0` switches all four to shadow-only).
Attribution per bot runs via the model tag in `ai_signals` /
`ml_predictions_master` — the channel is only the transport path.

## Overview

| Bot | Tag | Idea (Report 15) | Events | Direction | Cadence |
|---|---|---|---|---|---|
| `30_ai_pex1_bot.py` | PEX1 | S6 pump-exhaustion-short | `pump_dump_events` (vol_ratio ≥ 5, +1,5%/60s) | SHORT only | 60s poll |
| `31_ai_fmr1_bot.py` | FMR1 | S8 funding-extreme mean reversion | funding cross-section (≥95th pctl SHORT / ≤5th pctl LONG) | both | hourly (min. 19) |
| `32_ai_trm1_bot.py` | TRM1 | S10 transition resolution | `regime_current` = TRANSITION | BTCUSDT LONG/SHORT | every 5 min (min %5==4) |
| `33_ai_fif1_bot.py` | FIF1 | S11 FIFO filter | new `active_trades_master` rows (Fast In And Out) | same as source | 60s poll |

Shared building blocks (one source for bot, builder and trainer — X-R1 rule):

- `core/research_features.py` — feature contracts + builder (everything scale-free)
- `core/model_artifacts.py` — artifact loader (`<strat>_model.pkl` in the repo root;
  if the artifact is missing, the bot runs in idle mode)
- `core/signal_post.py` — outbox + `ai_signals` + shadow log (atomic, no
  Cornix block in the info message)

**Entry anchor (T-2026-KYT-9050-011, 2026-08-01):** 30/31/32 fetch the price for
`calculate_smart_targets` + `log_prediction` via
`core.live_price.get_live_price(symbol, conn)` — `core.candles` contract 2:
detection on closed candles, price separate. The context frame does NOT
provide it (since block 5 closed-only → stale for up to ~59 min); for 32 it
only remains a data-freshness guard. If `get_live_price` returns None
(Binance + DB fallback dead), the signal is skipped. FIF1 still takes
`sig["entry"]` from the source row.

All four follow the fleet conventions: closed-candle features (R1),
startup feature self-test (P0.12), gate on raw probability (threshold from
the val operating point), calibrated confidence for display only, cooldowns via
`trade_cooldowns`, tracking by `8_ai_trade_monitor`.

## Training results (2026-07-07, all staging — no deploy without operator)

All four datasets built (after DST fix f95f092) and trained
(`tools/new_models_train.py`, chrono split + purge, gate on raw prob):

| Model | Events | AUC val/test | Val OP | Test gate uplift | Verdict |
|---|---|---|---|---|---|
| PEX1 | 28.855 (26.271 labeled) | 0,545 / 0,565 | thr 0,65 degenerate (99 % pass) | −0,560 → −0,555 %/trade (nothing) | ❌ no selection value; best_iteration=2 |
| FMR1 | 11.503 (10.481) | **0,498** / 0,544 | −2,24 %/trade (n=65) | −1,05 → −0,06 %/trade (n=144) | ❌ val = random, OP negative — no foundation |
| TRM1 | 1.594 (classes 0/5/1.589!) | — not trained — | — | — | ⛔ upstream blocked: detector never holds TREND (step-6 finding) → classes don't exist. Revisit after detector rework/TRANSITION split |
| FIF1 | 120.102 (120.072) | 0,533 / 0,561 | **+0,044 %/trade** (thr 0,67, n=541) | **−0,082 → +0,331 %/trade, WR 75,3 %, n=893/18.011 (5 % pass)** | ⚠ only candidate: val AND test positive, but val edge razor-thin |
| (EPD2) | 78.351 | see MODEL_INTENT §7 | safe picker refuses / val-test break | LONG all buckets negative; SHORT test WR == base rate | ❌ both directions |

Assessment: consistent with the Batch-E core thesis — event-ranking gates
almost never deliver robust out-of-time expectancy. FIF1 is the exception with
a thin but consistently-directed signal in val and test (comparable to
MIS1-8h_pump).

**FIF1 DEPLOYED (operator decision 2026-07-07 ~11:49):** `fif1_model.pkl`
(thr 0,67, 21 features) copied into the repo root, bot 33 recycled via restart
marker, artifact load verified. Runs with `NEW_IDEAS_LIVE_POSTING=1`
LIVE (no shadow — operator pattern like AIM2: Cornix tracking of the posted
signals is the validation). Review after 4–6 weeks against `ai_signals`.
PEX1/FMR1/TRM1: no deploy, bots 30–32 remain idle.

## Design notes per bot

### PEX1 — Pump-exhaustion-short (S6)
Consumes the events of `10_pump_dump_detector` (pumps only: `price_change_60s
≥ +1,5`), Gate `volume_ratio ≥ 5` exactly as in training (report 13 EPD1-P0:
otherwise out-of-distribution). Events older than 30 min are discarded
(catch-up after downtime must not post expired exhaustion theses); the
feature candle is chosen relative to the EVENT time (floor-1 as in training).
Geometry: `calculate_smart_targets` SHORT. **Label geometry (review fix
2026-07-06):** the training entry is the spike-price estimate
`close[idx] × (1 + 60s-Move)` — not the pre-pump close (which would have
produced pump-correlated deflated labels); the replay starts conservatively
after the event candle (whose high contains the run-up before the entry).
Cooldown 4h per coin on JEDEM scored event — an exact mirror of the training
dedup.

### FMR1 — Funding-extreme mean reversion (S8)
Cross-section over ALL coins from a `premiumIndex` request; candidates are
the percentile extremes, the model gates on TP1-before-SL. Settlement
history per candidate comes live via REST (`/fapi/v1/fundingRate`) — the bot
therefore does NOT depend on the backfill state of the `funding_rates`
table. **Known residual skew:** live the *running* rate is evaluated, in
training the *settled* one (same source, one settlement offset). Cooldown
24h per coin/direction. **Deliberate deviation from the S8 exit idea:**
report 15 sketches "hold until funding normalization or time stop" — what's
implemented is the fleet-standard geometry (smart-target TP/SL, training
horizon 7 days), because the first-touch simulator and AI trade monitor
label/track exactly this geometry; a funding-normalization exit would need
its own monitor path. V2 candidate, if the shadow numbers support the idea.

### TRM1 — Transition resolution (S10)
Only runs when the DEBOUNCED regime (`regime_current`) is TRANSITION.
3-class model (0=OTHER, 1=TREND_UP, 2=TREND_DOWN — contract in
`core/research_features.py`); gate = max(P(up), P(down)). On gate pass the
bot posts a BTCUSDT trade in the predicted direction (smart targets).
**Known skew:** training events are raw checks from `regime_history`, live
gates on the debounced regime. Cooldown 12h per direction.

### FIF1 — FIFO filter (S11)
Standalone A/B: the live FIFO path (`3_detectors.py`) remains untouched.
FIF1 polls `Fast In And Out` rows from the last 10 minutes across BOTH
master tables (review fixes 2026-07-06: the closed-UNION catches fast
resolvers that the monitor deletes from active within 60s; the time window
prevents a backlog of days-old signals with expired geometry from being
posted after idle/outage phases). Dedup via a content key
(coin/direction/time/entry); required `(strategy, time)` indexes are created
by the bot at startup. Gate-passers post under tag FIF1 with the ORIGINAL
geometry (entry/TP1/SL unchanged), so that selection is the only
difference. JEDER candidate lands in `ml_predictions_master` (posted
true/false) — that's the A/B evaluation basis.

## Step 2 — Training on the VPS

Order per strategy: dataset builder → trainer → check report → deploy.
All builders run at BELOW_NORMAL priority and read-only against the DB.
Artifacts land ONLY in `%KYTHERA_STAGING_DIR%` (P1.35) — deploying into the
repo root is a deliberate operator decision.

```bash
# 0. Voraussetzung nur für FMR1: Funding-Historie backfillen (resumierbar)
python tools/backfill_funding_rates.py

# 1. Datasets bauen (je ~Minuten bis Stunden; --limit-symbols N für Smoke-Tests)
python tools/pex1_build_dataset.py
python tools/fmr1_build_dataset.py
python tools/trm1_build_dataset.py
python tools/fif1_build_dataset.py            # 111k Events; ggf. --sample-pct 50

# 2. Trainieren (Artefakt + _report.json nach staging_models)
python tools/new_models_train.py --strategy pex1
python tools/new_models_train.py --strategy fmr1
python tools/new_models_train.py --strategy trm1 --min-val-trades 20
python tools/new_models_train.py --strategy fif1

# 3. Report prüfen (GATE-UPLIFT test > 0? Reliability plausibel?) und dann
#    bewusst deployen:
copy %KYTHERA_STAGING_DIR%\pex1_model.pkl C:\_BOT\Kythera\pex1_model.pkl
#    … analog fmr1/trm1/fif1. Bots laden neue Artefakte automatisch (täglicher
#    Reload; im Idle-Modus alle 30 min).
```

**Deploy gate (recommendation, analogous to AIM2 rollout gates):** only
deploy if the test report shows `gate_avg_pnl > 0` AND `n_pass` is large
enough for an honest statement (≥ 50 for PEX1/FIF1, ≥ 20 for FMR1/TRM1). A
negative result is a valid outcome (action-bias correction) — the bot then
stays in idle mode and the idea is parked with its finding.

## VPS setup checklist

1. Add to `.env`: `CH_NEW_IDEAS=<Channel-ID>` and `NEW_IDEAS_LIVE_POSTING=1`.
   Optional per bot: `CH_PEX1` / `CH_FMR1` / `CH_TRM1` / `CH_FIF1` override
   the cohort channel individually (unset → fallback `CH_NEW_IDEAS`; operator
   2026-07-07: start together in the test channel, a dedicated channel only
   once a bot proves itself — the move is then just a .env entry + restart).
2. Have Cornix listen to the new channel (if the signals should be executed
   — otherwise it remains an observation channel).
3. Fleet restart or the `touch control/restart/main_watchdog.py` equivalent —
   the four bots are registered in `PROCESSES_TO_RUN` (start_delay 191–215).
4. Without deployed artifacts the bots run in idle mode (log:
   "Artefakt fehlt … Idle-Modus") — that is the expected state until step 2.

## Open points / deliberate simplifications

- PEX1 uses the 4 event measurements + 1h context; microstructure features
  from a 10s ticker (report 15) don't exist as a live table — deliberately
  deferred until a 10s persistence layer exists. **→ done 2026-07-07:**
  hypertable `ticker_10s` (see "V2 paths" below), data accumulates from the
  next detector restart.
- `pump_dump_events.spike_time` carries no TZ guarantee — the bot measures
  the offset against the wall clock (±12h clip); the builder converts a
  2/3h offset DST-aware via Europe/Bucharest (review fix: a constant offset
  over months would have been 1h wrong across the DST boundary).
- TRM1's `minutes_in_transition` is live the debounced episode duration, in
  training the raw episode duration — accepted approximation, noted in the doc.
- TRM1 uses only the current `confidence_btc/alt` values from the
  "confidence trajectories" (S10); windowed trajectories exist for
  returns/ATR/regime fractions. Confidence deltas are a V2 feature candidate.
- FIF1 deliberately omits two feature families named in S11: the
  cross-model confluence counter (E3 — would need the full multi-source
  event stream in the live path; implemented instead are FIFO-internal
  burst counters) and the coin liquidity class (no clean live proxy without
  new data upkeep; `vol_ratio_sma20` covers part of it). Both are V2
  candidates after the first shadow evaluation.
- TRM1 never posts against an open opposite position (no self-hedge on
  BTCUSDT) — if the prediction flips, it's only logged as shadow.
- Operational note: +4 processes ≈ +8 persistent PG connections
  (KYTHERA_DB_POOL_MIN=2 per process) — check against `max_connections` at
  rollout (P1.34).

## V2 paths after the negative initial finding (operator direction 2026-07-07)

Diagnostic consensus: PEX1 fails due to missing information (best_iteration=2
— there's nothing to learn in the 1h features), FMR1 due to the label
geometry (the S8 thesis "hold until funding normalization" was labeled with
first-touch TP/SL). More standard indicators would change nothing for either.

### PEX2 — 10s microstructure persistence (BUILT 2026-07-07)

Hypertable **`ticker_10s`** (TimescaleDB 2.26, `core/ticker_10s.py`):
`(ts TIMESTAMPTZ, symbol, price, vol_10s, vol_valid)` — the writer is
`10_pump_dump_detector` (it already builds the 10s buckets in-memory from
the `/ticker/24hr` poll; new is ONE batched insert per tick across all coins).

- Budget: ~108 coins × 8.640 ticks/day ≈ 0,9M rows/day (~45 MB raw); chunks
  1 day, compression after 3 days (segmentby=symbol), retention 365 days —
  all native Timescale jobs, housekeeping stays out of scope. No
  P1.40 regression: 1 insert/10s instead of 108 individual inserts.
- Kill switch: `KYTHERA_TICKER_10S_PERSIST=0` (default on).
- TZ contract: `ts` is TIMESTAMPTZ (UTC-aware) — a deliberate deviation from
  the naive legacy columns, ruling out the DST error class (f95f092).
- **Activation:** detector/fleet restart required (runs live from the repo).
- **PEX2 feature candidates** (once enough events with 10s context exist,
  realistically after ~2–3 months): volume decay rate after the spike,
  buy-pressure fade (event vs. +1/+3/+5 min), time-to-peak, retrace share
  since the spike high — i.e. deliberately shift scoring to event+X min so
  the decay is observable.
- Backfill option checked and discarded: historical 10s buckets from
  `data.binance.vision` aggTrades would be reconstructable, but the deltas
  from `/ticker/24hr` (rolling window!) are NOT the same measurement —
  training on aggTrades + live on ticker deltas would be exactly the
  bot≠builder skew the X-R1 rule forbids. So: accumulate data honestly.

### FMR2 — own exit path (design, not yet built)

Core idea: testing the S8 thesis cleanly means exit = funding
normalization OR time stop — in the label AND live. The mechanics model is
ROM1 (`28_signal_orchestrator.py`): `Close <SYMBOL>` command via
`send_telegram(...)` → `telegram_outbox` (Cornix closes), own rows via
`DELETE FROM ai_signals … RETURNING` + insert into `closed_ai_signals`
(`status='CLOSED_FUNDING_NORMALIZED'`), filter strictly on its own model
tag. `8_ai_trade_monitor` has NO custom exit hook — the bot must own the
close timing itself (an hourly scan suffices, settlements come every 8h);
the RETURNING guard defuses the race with the monitor (SL/timeout it
continues to track).

**Order (deliberately offline-first, no bot rebuild before proof):**
1. Exit predicate + constants into `core/research_features.py` (one source
   for builder AND bot, X-R1). Proposal: SHORT exit once
   `funding_cs_pctl < 0.80` OR `funding_z_30d < 1.0`; LONG symmetrically
   (`> 0.20` / `> −1.0`); time stop 9 settlements (3 days); hard SL remains.
2. `fmr1_build_dataset` V2: label = PnL at the normalization/timeout exit
   (exit price of the settlement candle), NOT first-touch TP/SL.
3. Retrain (queue slot, one-job rule). Continue only if val+test positive —
   otherwise the S8 thesis is honestly falsified and gets parked.
4. Only then bot exit loop + deploy under the new tag **FMR2**
   (versioning rule: changed generation = new tag).

**Channel collision — decided (operator 2026-07-07):** Cornix' `Close
<SYMBOL>` closes ALL trades for that symbol in the channel. Since `CH_NEW_IDEAS` is a test channel, the collision is accepted for now. As a
precaution every bot now has its own `.env` override
(`CH_PEX1/CH_FMR1/CH_TRM1/CH_FIF1`, fallback `CH_NEW_IDEAS`,
`core/config.py`) — at the latest when FMR2 goes live with the close path,
it gets its own channel via `CH_FMR1`; that's then just a .env entry +
restart, no code deploy.

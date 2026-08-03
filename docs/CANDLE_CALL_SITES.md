# Candle access: API contract, call-site inventory, migration order

**As of:** 2026-07-09 (commit 1b140a5) · **Task:** T-2026-CU-9050-034 (C1 preparation) · **Parent:** T-2026-CU-9050-018, `docs/TIMESCALE_R1_MIGRATION.md`

Working basis for **phase 0/1** of the R1-+-TimescaleDB migration: the new access API `core/candles.py`, the full inventory of the places that today touch per-coin candle or indicator tables, the R1 blast radius, and the rewiring order.

> **None of this has been executed.** This task only lays down the API, the inventory and `tools/candles_parity.py`. No call site rewired, no dual write, no backfill, no cutover, no schema change. The open operator questions (§5) must be answered before the first rewiring commit.

---

## 1. The API (`core/candles.py`)

```python
read_candles(conn, symbol, tf, *, limit, start, end, include_forming=False, columns=CANDLE_COLUMNS)
read_indicators(conn, symbol, tf, *, limit, start, end, include_forming=False, columns=None)
read_candles_with_indicators(conn, symbol, tf, *, limit, start, end, include_forming=False, ...)   # LEFT JOIN
latest_open_time(conn, symbol, tf, *, include_forming=True)
upsert_candles(conn, symbol, tf, rows, *, closed)          # Caller committet
upsert_indicators(conn, df, symbol, tf)                     # Caller committet
table_exists(conn, table) · indicator_column_names(conn, symbol, tf)
period_start(tf, now) · last_closed_open_time(tf, now) · timeframe_delta(tf)
set_symbol_whitelist(...) · load_symbol_whitelist(path='coins.json')
```

Four contracts, all load-bearing:

1. **Reads always deliver ASC** by `open_time`. `iloc[-1]` is the *newest* candle everywhere. Today ASC and DESC frames get mixed (14 call sites read `DESC LIMIT n` and then reverse it themselves) — exactly trap 1 from `docs/OPUS-HANDOFF.md`.
2. **`include_forming=False` is the default.** Price checks (monitors 5/8, `get_live_price` fallbacks, orchestrator last-close, health-monitor canary, live-parity replica) pass `True` explicitly. Analytical readers don't — that's R1.
3. **Writes don't commit.** The caller owns the transaction (hard rule 8, like `core/signal_post.py`). Whoever replaces `insert_fast()` / `write_indicators_to_db_optimized()` must add a `conn.commit()` — both commit themselves today.
4. **Identifier hygiene (P3.3).** `symbol`/`tf` validated (`^[A-Z0-9]{2,24}$`, TF whitelist), quoted via `psycopg2.sql.Identifier`, optional hard `coins.json` whitelist. The validation additionally demanded in P3.3 for `load_coins` has since been done (T-2026-CU-9050-096, 2026-07-11: central `re.fullmatch(r'[A-Z0-9]+')` check in `core.market_utils.load_coins`, all six callers run through it; test `backtest/test_symbol_validation.py`).

### The `is_closed` stand-in in phase A

The target schema carries `is_closed boolean` from the Binance kline flag `k['x']`. The old tables don't have the column. Phase A derives "closed" from the clock:

> A candle is closed ⇔ `open_time < period_start(tf, now())`.

The cutoff is computed **DB-side from `now()`** (one clock — the writer's) and is timezone-independent: pure epoch arithmetic, anchored to Monday for `1w` (epoch 0 is a Thursday, Binance weekly candles open Monday 00:00 UTC). `date_trunc()` would be wrong — it hangs off the session `TimeZone` and would cut differently depending on the bot process (TZ minefield R3).

Weakness against the real flag: a candle whose period has just ended can, for milliseconds, still carry the values of the last pre-close tick. `KYTHERA_CANDLES_CLOSE_GRACE_SEC` shifts the cutoff back. Default 0 → operator question 5.4.

### What the API deliberately does NOT do

- **No DDL.** `CREATE TABLE`/`CREATE INDEX` stay in `1`/`2`/`6`; they go away without replacement in phase C.
- **No `KYTHERA_CANDLES_SOURCE=hyper`.** The env switch exists and raises `CandleSourceError`. The hypertable path is phase 4 and is not pre-built speculatively.
- **No commit, no retry, no pool handling.**

---

## 2. Call-site inventory

**≈108 verified call sites in 50 live files** (`legacy_trainers/`: 23 files with raw table reads, one aggregate row — **not being rewired**, because no process runs them; the scripts are frozen provenance, see §2 side findings. If the per-coin tables go away in phase C, they'll never run again anyway — that's not a reason to delete them).

Legend **forming today**: `open` = the newest row can be the running candle and is used · `dropped` = the file removes it itself · `bounded` = the query is limited to a closed timestamp · `intended` = the forming candle is the point.
**Target**: `F` = `include_forming=False` · `T` = `include_forming=True`.

### Block A — ingestion, engine, housekeeping (DB writer, VPS-only)

| Site | Function | Kind | TF | Ordering | Forming today | Target | Commits today |
|---|---|---|---|---|---|---|---|
| `1_data_ingestion.py:83` | `create_table_if_needed` | DDL | arg | – | – | stays inline | yes |
| `1_data_ingestion.py:99,102` | `get_latest_open_time` | `to_regclass` + `MAX(open_time)` | arg | aggregate | sees forming | `latest_open_time(include_forming=True)` | – |
| `1_data_ingestion.py:177` | `insert_fast` (REST catch-up) | write-candles, `execute_values`, `IS DISTINCT FROM` | arg | – | writes history + possibly the forming end row | `upsert_candles(closed=…)`, **two calls** (history/forming) | **yes** |
| `1_data_ingestion.py:437` | `_flush_to_db` (WS flush) | write-candles, **SAVEPOINT per row** | buffer | – | writes the live forming candle | `upsert_candles(closed=k['x'])` | yes |
| `2_indicator_engine.py:173,180` | `create_indicator_table` | DDL + index | arg | – | – | stays inline | yes |
| `2_indicator_engine.py:553` | `process_coin_task` | `MAX(open_time)` **on the indicator table** | arg | aggregate | – | **API gap** | – |
| `2_indicator_engine.py:574` | `process_coin_task` | read-candles `SELECT *` | arg | ASC, no limit | **open — indicators are computed over the forming candle** (breaks hard rule 5) | **F** | – |
| `2_indicator_engine.py:513` | `write_indicators_to_db_optimized` | write-indicators | arg | – | writes the indicator row of the forming candle | `upsert_indicators()` | **yes** |
| `6_housekeeping.py:61` | Bootstrap | DDL | 8 TF | – | – | stays inline | yes |
| `6_housekeeping.py:259` | `_fetch_last_close_or_entry` | read-candles | 5m | DESC LIMIT 1 | intended | **T** | – |
| `6_housekeeping.py:440` | Delisted scan | `information_schema` | – | – | – | **API gap** | – |
| `6_housekeeping.py:461` | Retention | **DELETE** | 5m–4h | – | – | **API gap** | yes |
| `6_housekeeping.py:647` | Gap scan | read-candles (`open_time`) | var | ASC | open | F | – |
| `6_housekeeping.py:720` | Gap filler | write-candles, `ON CONFLICT DO NOTHING` | var | – | only closed gaps | `upsert_candles(closed=True)` | yes |
| `6_housekeeping.py:747` | Indicator invalidation | **DELETE** | var | – | – | **API gap** | yes |

### Block B — monitors, orchestrator, price fallbacks (`include_forming=True`)

| Site | Function | Kind | TF | Ordering | Forming today | Target |
|---|---|---|---|---|---|---|
| `5_trade_monitor.py:194,199` | SL/TP scoring | read-candles | 5m | DESC LIMIT 1 / ASC window | **intended** (wick scoring, `:264-270`) | **T** |
| `8_ai_trade_monitor.py:123,128` | AI SL/TP scoring | read-candles | 5m | DESC LIMIT 1 / ASC window | **intended** (`:202-210`) | **T** |
| `28_signal_orchestrator.py:352,787` | `_get_latest_price`, `_get_close_price` | read-candles | 5m | DESC LIMIT 1 | intended | **T** |
| `3_detectors.py:45` | `get_live_price` | read-candles | 5m | DESC LIMIT 1 | intended | **T** |
| `29_ufi1_bot.py:96` | `get_live_price` | read-candles | 1h | DESC LIMIT 1 | intended | **T** |
| `core/health_monitor.py:70` | DATA_STALE canary | `EXTRACT(EPOCH FROM NOW()-max(open_time))` on `BTCUSDT_5m` | 5m | aggregate | **intended** | **T** — without forming, false-positive DATA_STALE risks a fleet restart |
| `tools/audit/live_parity.py:81` | live-serving replica of bot 11 | read-joined | 1h | DESC LIMIT 100 → ASC | **intended** (`:105-116`) | **T** — otherwise parity breaks |

### Block C — AI/strategy bots

| Site | Bot | Kind | TF | Ordering | Forming today | Target |
|---|---|---|---|---|---|---|
| `9_ai_sr_bot.py:61` | SR | read-indicators `SELECT *` | 1h | `open_time<=%s` DESC LIMIT 1 | bounded (past trade timestamp) | F (`end=`) |
| `10_pump_dump_detector.py:175` | Pump/dump | read-indicators | 1h | DESC LIMIT 1 | open | F |
| `11_ai_mis_bot.py:178` | MIS | read-joined | 1h | DESC LIMIT 100 → ASC | **intended, split**: features `iloc[-2:-1]`, live price `iloc[-1]` (fix P1.17, `:227-233`) | **T** + index rework |
| `12_ai_ats_bot.py:127` | ATS | read-joined | 1h | DESC LIMIT 500 → ASC | **intended**: `current_idx=-2`, `prev_idx=-3` (`:148-151`) | **T** + index rework |
| `13_ai_rub_bot.py:110,126` | RUB | read-candles + read-indicators | 1h | `< date_trunc('hour',NOW())` | **dropped** (P1.19) | F |
| `14_ai_atb_bot.py:280,285,290` | ATB (parked) | chart reads | 1h | ASC | open (chart) | T (display) |
| `14_ai_atb_bot.py:618` | ATB | read-candles | 1h | ASC + `.tail(4)` | **intended**: `last_close=iloc[-1]` triggers break/bounce | T |
| `15_ai_master_bot.py:224` | Master/AIM | read-joined | 1h | `< floor(ts)` DESC LIMIT 1 | **dropped** (`:218-238`) | F (`end=`) |
| `16_smc_forex_metals_bot.py:66` | SMC Metals | read-candles | var | DESC LIMIT 300 → ASC | **dropped at the caller** (`:334`, P1.27) | F — remove `:334` |
| `18_ai_abr1_bot.py:308,583` | ABR1 | read-candles | 1h | ASC | `:583` **dropped** (`:595`), `:308` (self-test) open | F — `:595` becomes redundant |
| `21_btc_smc_strategy.py:110` | BTC-SMC | read-candles | var | DESC LIMIT 500 → ASC | **dropped** (`:126`) | F — remove `:126` |
| `22_ip_pattern_bot.py:196` | IP pattern | read-candles | var | DESC LIMIT n → ASC | open: `current_price=iloc[-1]` (`:210`) | F |
| `24_quasimodo_bot.py:90` | Quasimodo | read-joined | var | DESC LIMIT 100 → ASC | pivots dropped (`:115`), price `closes[-1]` open | F — remove `:115` |
| `25_smc_ml_sniper.py:208` | Sniper | read-joined | var | DESC LIMIT 150 → ASC | pivots **dropped** (`:239`, T-2026-CU-9050-036), price `closes[-1]` open | F — remove `:239` |
| `29_ufi1_bot.py:72` | UFI1 (parked) | read-candles | 1d | ASC | open | F |
| `7_pattern_detector.py:272` | Pattern | read-candles | 1h–1d | DESC LIMIT 168 → ASC | **dropped**: `iloc[:-4]` (`:282`), `len(df)-2` (`:310`) | F + offset rework |
| `17_mayank_bot.py` | Mayank | **no DB candles** (yfinance) | – | – | – | — |
| `99_smc_paper_bot.py:60` | Paper (not live) | read-candles | var | – | dropped | F |

### Block D — shared helpers and strategies (highest fan-in)

| Site | Function | Kind | TF | Forming today | Target |
|---|---|---|---|---|---|
| `core/trade_utils.py:304` | `calculate_smart_targets` | read-candles, DESC LIMIT 1000 → ASC | 1h | **open** — forming feeds the swing/HVN/FVG level pool | F |
| `core/trade_utils.py:423` | `get_hvn_and_sr_levels` | read-candles, ASC 95d | 1h | **open** — forming feeds S/R + fibs | F |
| `core/market_utils.py:187` | `calculate_obv` | read-candles | 1h | bounded (caller end timestamp) | F |
| `core/charting.py:138` | mini-chart | read-candles | 5m | open (cosmetic) | F |
| `core/regime_logic.py:81,136` | BTC regime, alt context (literals `BTCUSDT_15m`, `BTCDOMUSDT_15m`) | read-candles | 15m | **open** — forming 15m drives the regime classification → orchestrator gating | F, backfill path needs `end=` |
| `core/research_features.py:312` | `fetch_context_frame` | read-joined | 1h | **dropped** (`searchsorted…-1`, `:339`) | F |
| `strategies/strat_main_channel.py:52` | Signal | read-candles, `<=%s` DESC 480 → ASC | 1h | bounded | F (`end=`) |
| `strategies/strat_support_resistance.py:40` | Signal | read-candles, `<=%s` DESC 480 → ASC | 1h | bounded | F (`end=`) |
| `strategies/strat_volume_indicator.py:18,39,45` | Signal | read-candles | 30m | bounded (strict `<` bounds) | F |
| `3_detectors.py:202` | `run_detectors_for_timeframe` | read-indicators `SELECT *`, **DESC LIMIT 480** (DESC frame goes to the strategies!) | 30m/1h | open | F |

`core/aim2_features.py`, `core/mis_features.py`, `core/rub_features.py`, `core/funding_features.py` have **no direct DB access** — they compute on frames handed to them. Their SQL-fragment constants (`MIS_SQL_INDICATOR_SELECT`, `CONTEXT_SQL_SELECT`) run at the callers and are inventoried there.

`strategies/strat_5_percent.py`, `strategies/strat_fast_in_out.py`, `handlers/open_handler.py`, `dashboard.py` don't touch candle tables. **`chart_data_service.py` doesn't either** — it serves the WS ring buffer and drops the forming 1m candle at `:250`. Design doc T-018 §2 lists it as a call site by mistake; **strike it from the migration backlog.**

### Block E — trainers, backtests, dataset builders, audit tools (offline)

| Site | Kind | TF | Forming today | Target |
|---|---|---|---|---|
| `tools/walkforward_sim.py:174,204` | read-candles / read-joined | 1d/1h/4h | **rewired** (T-2026-CU-9050-037): both loaders go through `core.candles` with `include_forming=False` | ✅ F |
| `tools/walkforward_sim.py:635,759` | read-joined (MIS1/RUB) | 1h | dropped (`date_trunc`) | F |
| `tools/aim2_build_dataset.py:275` · `epd2_build_dataset.py:113` · `research_dataset_common.py:74` | read-joined | 1h | event floor `searchsorted-1` | F (small delta) |
| `tools/retrain_sra2.py:172` | read-indicators | 1h | python floor mask | F |
| `tools/mis1_move_labels.py:65` | read-candles | 1h | dropped (`date_trunc`) | F |
| `tools/regime_rules_study.py:63` | read-candles | 15m | open (mild) | F |
| `tools/regression_guard/rgcore.py:130` | read-candles `SELECT *`, DESC LIMIT 600 → ASC | 30m–1w | open (forming can be frozen into the golden) | F |
| `tools/audit/step2_analysis.py:148,158,190` · `step2_part2.py:17,41,73` · `step7_monitor_replay.py:23,92` | aggregates, `information_schema`, `generate_series` gap census | 1h/5m | – | **API gap**, stay raw |
| `tools/audit/step7_monitor_replay.py:32` | read-candles | 5m | historical | F |
| `qm_ml_trainer.py:86` · `smc_ml_trainer.py:87` · `smc_pattern_backtester.py:51` · `qm_backtest.py:57` | read-joined / read-candles | 1h/4h | open | F |
| `fib_backtest.py:87,97` | `pg_tables` case variant + read-candles | 1d | open | F, **gap** (tries `{symbol.lower()}_1d`) |
| `backtest/smc_btc_backtest{,_v2,_v3}.py` · `trainers_x/BT2-Datagrepper-for-ML.py:47` | read-candles | var | open | F |

Delegating builders with no SQL of their own (same profile as `research_dataset_common:74`): `tools/fif1_build_dataset.py:151`, `fmr1_build_dataset.py:151`, `pex1_build_dataset.py:158`, `trm1_build_dataset.py:127`, `mis2_dump_geometry_study.py`.

`guard.py verify|refresh|smoke` runs **DB-free** on `.npz` fixtures; only `extract` touches the DB. The phase-1 gate is thereby fixture-based and runnable on the build machine.

### API gaps (against the **implemented** API, not the sketch)

The sketch from T-018 §2 had five functions; the built API already closes their biggest gaps (JOIN, `start`/`end`, `limit=None`, `columns=None`, `indicator_column_names`, `table_exists`). What remains:

| Gap | Sites | Suggestion |
|---|---|---|
| **Aggregate SQL** (`SUM`/`MAX`/`MIN`/`CASE` + correlated subselect, `count(DISTINCT)`, `generate_series` gap census) | `23_market_tracker:100,309,321,372`; `step2_analysis:148,190`; `step2_part2:17,73`; `step7_monitor_replay:92` | Market tracker is live-hot → add `window_volume()`/`window_range()` **or** rewrite in pandas (30m × 7d ≈ 336 rows/coin). Audit tools stay raw. |
| **Oldest row in the window** (`ORDER BY ASC LIMIT 1`) | `23_market_tracker:132` | The API always delivers the *newest* N → `read_candles(..., first=True)` needed |
| **`DELETE` by age / from `open_time`** | `6_housekeeping:461,747` | `delete_candles_before()` / `delete_indicators_from()` |
| **`MAX(open_time)` on the indicator table** | `2_indicator_engine:553` | `latest_open_time(..., kind='indicators')` |
| **Table enumeration** | `6_housekeeping:440`; 3 audit tools; `fib_backtest:87` | `list_coin_tables(conn, tf=None)`; `fib_backtest` additionally needs case resolution |
| **DDL** | `1:83`, `2:173,180`, `6:61` | Deliberately outside the API. Goes away in phase C |
| **Mixed ingestion batch** | `1_data_ingestion:177` | `closed=` is one bool per call; the REST catch-up mixes closed history with a forming end row → **two** upsert calls. Not a missing feature, a wiring question |

**Two clean-up findings outside the mandate** (not silently omitted): `db_schema_analysis.py` existed twice (repo root + `tools/`); `legacy_trainers/` (23 files) carries its own raw table reads and its own `get_live_price`. Neither is needed for the migration.

> **Correction 2026-07-10 (T-2026-CU-9050-039).** The paragraph above originally read: *"`db_schema_analysis.py` and `tools/db_schema_analysis.py` are **byte-identical duplicates**; `legacy_trainers/` (23 files) is **dead code** […]. **Both are deletable.**"* Neither claim holds up under inspection of the code.
>
> **`db_schema_analysis.py` was not byte-identical.** The root copy was modernized in `052ba4c` (ruff cleanup), the `tools/` copy is unchanged from the initial import; on top of that, its `sys.path.insert(0, dirname(__file__))` pointed at `tools/`, where there is no `core/` — it could never import `core.database`. `audit_reports/10_dashboard_tools.md:47` and `AUDIT_TODO.md` P3.1 had already noted this correctly. The stale `tools/` copy is deleted, the root copy is canonical (the exclude entries in `pyproject.toml` and `.github/workflows/typecheck.yml` point at it anyway).
>
> **`legacy_trainers/` is not "dead code" in the sense of deletable.** No running process imports the scripts, and they are deliberately not runnable (credentials replaced by `os.getenv(...)` placeholders) — but they are the **only reproduction basis for the eight live-loaded model artifacts**. `legacy_trainers/README.md` maps every trainer to its artifact and bot (MIS1→11, ABR1→18, ATS1→12, RUB1→13, SRA1→9, AIM1→15, EPD1→10, ATB1→14); the folder was created for exactly this (`7b5ec89 feat: preserve the _X ML trainers as frozen provenance`). Their documented defects (label geometry, split leakage, in-sample thresholds, feature skews) are deliberately preserved — they explain the live models' behaviour and are the reference against which the retrain program measures its deltas. **Stays. See operator question §5.8.**

---

## 3. R1 blast radius

**Real behaviour change, and this is exactly what the migration is for:**

- ~~**`25_smc_ml_sniper:208`** — no drop, `argrelextrema` pivots *and* `current_price` on the forming candle. **Silent repaint, the single highest risk.**~~ **Pivot side done** (2026-07-10, T-2026-CU-9050-036, P1.46): `argrelextrema` runs on `highs[:-1]/lows[:-1]`, the intra-candle repaint is gone. `current_price = closes[-1]` deliberately stays live (CMP entry + BB level proximity) — the price side only flips with block 4, after operator question 4/6.
- **`2_indicator_engine:574`** — indicators are computed fleet-wide over the forming candle. Breaks hard rule 5 today.
- **`core/trade_utils:304,423`** — highest fan-in: the forming candle feeds the level pool (swing/HVN/FVG/S-R/fib) of *all* bots.
- **`core/regime_logic:81,136`** — the forming 15m candle drives the regime classification and thereby the orchestrator gating.
- ~~**`tools/walkforward_sim:174,204`** — treats forming as closed: **look-ahead in the walk-forward simulator**, i.e. in exactly the tool that generates the retrain program's labels.~~ **Fixed 2026-07-10 (T-2026-CU-9050-037)** as the first step of block 1: both loaders read via `core.candles` (`include_forming=False`), the invariant is mechanically checked in `backtest/test_feature_lookahead.py`. Still open: the question to the operator whether already-rolled-out models were trained on the old labels.
- `22_ip_pattern:196`, `29_ufi1:72`, `14_ai_atb:618`, `23_market_tracker` (%-change, volatility, volume/range aggregates), `core/charting:138` (cosmetic), `regime_rules_study:63` and `step2_part2:25` (mild).

**Index-coupled — flip only together with an offset rework**, otherwise one *closed* candle too many gets dropped: `7_pattern_detector` (`iloc[:-4]`, `len(df)-2`), `11_ai_mis` (`iloc[-2:-1]` / `iloc[-1]`), `12_ai_ats` (`-2`/`-3`), `24_quasimodo` (`[:-1]` + `closes[-1]`), `16_smc_forex_metals` (`:334`), `21_btc_smc` (`:126`), `18_ai_abr1` (`:595`).

For 11 and 12, the forming candle is **part of the contract** (feature row = second-to-last, live price = last). They stay on `include_forming=True` and get the split done cleanly instead of guessing it from negative indices.

**Must stay `include_forming=True`** — here, `False` would cost money or restart the fleet: monitors `5`/`8`, orchestrator `28`, `get_live_price` in `3`/`29`, `6_housekeeping:259`, **`core/health_monitor:70`** (otherwise false-positive `DATA_STALE`), `tools/audit/live_parity:81` (parity to the live-serving semantics).

**Already forming-safe, no delta:** `9_ai_sr`, `10_pump_dump`, `13_ai_rub` (P1.19), `15_ai_master`, all three `strategies/*`, `core/market_utils`, `core/research_features`, `walkforward_sim:635,759`, `mis1_move_labels`, `retrain_sra2`, the `step2` aggregates.

**Regression guard:** `rgcore` freezes `SELECT * … DESC LIMIT 600`. If the goldens were created with a forming candle, the guard will go red on the switchover. **That is a real signal, not a refresh occasion** (hard rule 9).

---

## 4. Migration order

Six blocks, each its own commit, regression guard before and after. Blocks 1–5 are pure code rewiring (read-only, doable from the build machine); block 6 touches the DB and is **VPS-only** (hard rule 1).

| # | Block | Files | Why here | DB write |
|---|---|---|---|---|
| 1 | Offline tooling | Trainers, backtests, `*_build_dataset`, `walkforward_sim`, `retrain_sra2`, `rgcore`, audit replays, `core/charting` | No live signal path, instantly rollbackable. Surfaces the missing API shapes (aggregates, `first=True`) early. `walkforward_sim` first — that's where the look-ahead sits that contaminates the retrain program | no |
| 2 | Strategies + `3_detectors` + shared helpers | `strat_*`, `3_detectors`, `core/trade_utils`, `core/market_utils` | The strategies are already timestamp-bound (small delta); the helpers unblock the AI bots | no |
| 3 | **Monitors + orchestrator explicitly on `True`** | `5`, `8`, `28`, `3.get_live_price`, `29:96`, `6:259`, `core/health_monitor` | **Before** the first `False` in the money path: make the `True` visible and reviewable. A monitor that silently flips to closed candles scores SL/TP up to 5 minutes late | no |
| 4 | AI bots, **one bot per commit** | `9,10,13,14,15,18,22,24,25,29` (F) and `11,12` (T + index rework) | R1 takes effect here. Document signal rates in a 24h comparison. The pivot repaint in `25` was fixed ahead of schedule (T-2026-CU-9050-036); only the price side remains open there | no |
| 5 | Shared feature builders **plus trainer/replay in the same commit** | `core/research_features`, `core/regime_logic` + associated trainers | Hard rule 7: trainer == serving == replay. Switching them separately = silent feature drift in live models | no |
| 6 | `2_indicator_engine` (reads + writes), `1_data_ingestion`, `6_housekeeping` | Engine read `:574`, upserts, gap filler, DELETE/DDL gaps | Highest R1 impact (indicators over the forming candle) and the caller-commit switch. From here on the data model carries the real `is_closed` | **yes — VPS, C-gate** |

Only after that come phases 2–5 from `docs/TIMESCALE_R1_MIGRATION.md` (dual write, backfill, parity observation, read cutover, cleanup).

### Block 1 status — done (T-2026-CU-9050-107, 2026-07-13)

Block 1 (offline tooling) is rewired. 12 read sites now go through `core.candles` with `include_forming=False`; verified read-only against the live VPS DB (ASC frames, forming candle excluded: `newest open_time < period_start`), regression guard `smoke`+`verify` green, ruff/format green on the non-excluded root files.

- **Rewired:** `core/charting.py`, `tools/mis1_move_labels.py` (+ transitively `mis2_dump_geometry_study`), `tools/regime_rules_study.py`, `tools/retrain_sra2.py`, `tools/research_dataset_common.py` (+ transitively fif1/fmr1/pex1/trm1), `tools/aim2_build_dataset.py`, `tools/epd2_build_dataset.py`, `qm_ml_trainer.py`, `smc_ml_trainer.py`, `qm_backtest.py`, `smc_pattern_backtester.py`, `backtest/smc_btc_backtest{,_v2,_v3}.py`, `tools/regression_guard/rgcore.py`. `tools/walkforward_sim.py` was the first step (T-2026-CU-9050-037).
- **New helper `candles_window_start(since, lookback_days)`** in `research_dataset_common` reproduces the earlier `%s::timestamptz - INTERVAL 'N days'` in Python (localize to LOCAL_TZ, then subtract days). One place for the TZ-sensitive window boundary; aim2/epd2 import it.
- **Deliberately NOT rewired (documented, not silently omitted):**
  - `fib_backtest.py` — the `pg_tables` case-variant probe (`{symbol.lower()}_1d`) collides with the API's uppercase validation (`^[A-Z0-9]{2,24}$`). Its own API gap (§2, case resolution), not a pure rewiring → stays raw until the gap is closed.
  - `tools/audit/step7_monitor_replay.py` — a shallow TZ-forensics throwaway script; the `AT TIME ZONE 'UTC' AS ot` read is deliberately TZ-agnostic (±4h window, shift-0/3 detection). Historical window → forming irrelevant, CI-excluded, zero behaviour benefit against real risk to the delicate shift logic.
  - `trainers_x/BT2-Datagrepper-for-ML.py` — frozen provenance (its own hardcoded `DB_CONFIG`, hyphenated non-importable filename, doesn't import `core`), same class as `legacy_trainers` (§2, §5.8).

### Block 2 status — done (T-2026-CU-9050-108, 2026-07-13)

Block 2 (strategies + `3_detectors` + shared helpers) is rewired. Seven read sites in the **live signal path** now read via `core.candles` with `include_forming=False`. Pure read-only code change; no DB schema. **Live behaviour change → not auto-merged, Michi's approval before enqueueing.**

- **Rewired:** `core/trade_utils.calculate_smart_targets:304` (1000h level pool, DESC-then-reverse goes away → API-ASC), `core/trade_utils.get_hvn_and_sr_levels:423` (95d S/R, `start=utc_now()-95d`), `core/market_utils.calculate_obv:231` (`start=`/`end=`, both inclusive, `.set_index('open_time')`), `strategies/strat_main_channel:61` + `strat_support_resistance:50` (`end=open_time_hit`, `sort_values` goes away), `strategies/strat_volume_indicator` (3 reads, 30m), `3_detectors.run_detectors_for_timeframe:167` (indicator frame of the 5 classic strategies).
- **DESC→ASC trap (core review point).** `3_detectors` hands a DESC frame to five consumers who all index `iloc[0]`=newest — audited: `strat_main_channel`, `strat_support_resistance`, `strat_5_percent`, `strat_fast_in_out` (all `data.iloc[0]`), `strat_volume_indicator` (`df_indexed.iloc[0]`); `strat_fast_in_out` even carries the explicit comment "iloc[-1] WAS the OLDEST candle (df is DESC)". Chosen solution: read via the API (ASC + forming-free), then `.iloc[::-1].reset_index(drop=True)` → exactly the previous DESC frame, **zero consumer reindex**. The only behaviour change: `iloc[0]` = newest CLOSED instead of forming candle (= R1).
- **Strict `<` bounds byte-true.** The volume indicator has two strict `open_time < grenze` reads (HVN baseline, spike history). The API's `end` is inclusive → `end = grenze − timeframe_delta("30m")` reproduces `< grenze` exactly (period-aligned open_times: `<= grenze−30m` ⟺ `< grenze`). The third read (`<= open_time_hit`) maps directly onto `end=`.
- **Fan-in.** `calculate_smart_targets`/`get_hvn_and_sr_levels` are the highest-fan-in sites (live callers 7/9/10/11/12/13/14/15/18/25/34 + `open_handler` + research 30–32); they deliver the **geometry** (SL/TP/entry level), not the signal gate — `include_forming=False` shifts the posted level **values**, not the signal **rate**. The rate is changed by the detector read (5 classic strategies). Offline callers (`walkforward_sim`, `*_build_dataset`) pass `df=` → no DB read, untouched. Orchestrator `28:495` passes `df=` → untouched.
- **Verification (VPS, read-only, 150 coins).** Mechanics 149/149: ASC, forming excluded (`newest open_time < period_start`), detector re-flip = DESC with `iloc[0]` newest closed, closed frame byte-equal to the old query. Live signal-rate A/B **not measurable** (fleet ingestion stood for ~2.4h → no forming candle; historical forming snapshots got overwritten at close). Candle-tip sensitivity as a proxy: 5%/fast gates 0/298, S/R hit precondition 25/149 (~17%), level pools 69–83% of coins (avg ~4.6% shift). Guard `smoke`+`verify` green, ruff/format/mypy green on `core/`+`3_detectors.py`.
- **Deliberately NOT in block 2:** `3_detectors:45 get_live_price` (block 3, target `True`); the AI-bot direct readers (block 4); grace-period/MIS-ATS forming (§5.4/5.5) gate block 4/6, not this block.

### Block 3 status — done (T-2026-CU-9050-109, 2026-07-13)

Block 3 (monitors + orchestrator + price fallbacks) is rewired. The seven remaining price/scoring readers in the money path now read via `core.candles` with **explicit `include_forming=True`**. Pure read-only code change; no DB schema. **Behaviour-preserving** (see below) — still, the money path → not auto-merged, Michi's approval before enqueueing.

- **Rewired:** `5_trade_monitor:194,199` + `8_ai_trade_monitor:123,128` (SL/TP scoring, 5m — first run `limit=1`, otherwise `start=Wasserzeichen`), `28_signal_orchestrator._get_latest_price` + `._get_last_close_price`, `3_detectors.get_live_price`'s DB fallback (`:63`), `29_ufi1_bot.get_live_price` (`:96`, 1h, parked), `6_housekeeping._fetch_last_close_or_entry` (`:270`), `core/health_monitor._check_data_staleness` (`:70` → `latest_open_time(include_forming=True)`).
- **Behaviour-preserving, unlike block 2.** `include_forming=True` adds no forming filter → the rows read are byte-equal to the previous `ORDER BY open_time DESC LIMIT 1` / `WHERE open_time >= %s` queries (the API only wraps them in a `SELECT * FROM (… DESC) s ORDER BY open_time ASC`, same row set). No signal-rate change. That's the point of block 3: make the `True` visible and reviewable **before** block 4 brings the first `False` into the money path.
- **Inventory drift corrected.** The inventory (§2 block B) noted the orchestrator sites as `28:352,787`; in reality they sat at `:449` (`_get_latest_price`) and `:1063` (`_get_last_close_price`). `:1063` was **not inventoried at all** — both are now captured and rewired.
- **Monitors 5/8 — structure preserved.** The loops build `coin_candles[coin]` as a list-of-dicts with `float()` casts + tz normalization from raw tuples. The API delivers an ASC DataFrame; via `rows = list(df.itertuples(index=False, name=None))` the rest of the loop logic (`rows[-1][0]`, `float(r[1..3])`, the downstream `>=` watermark filter) stays **unchanged**. The previously outer `with c.cursor() as cur:` goes away (no more direct SQL), the loop body moves in one indentation level. `open_time` is a `pd.Timestamp` afterwards (tz-aware, a subclass of `datetime`) instead of a `datetime` — a drop-in for every comparison/arithmetic/`.tzinfo` check; the values are second-aligned, so no ns-precision difference.
- **SAVEPOINT reads (28:1063, 6:270).** The SAVEPOINT/`ROLLBACK TO SAVEPOINT` frame stays exactly intact; only the inner `SELECT` is replaced by `read_candles`. `read_candles` opens a **second** cursor on the same connection (allowed) and, on a missing table, raises into the same `except` that rolls back the savepoint — semantics identical.
- **health_monitor — age clock source.** The `EXTRACT(EPOCH FROM NOW()-max(open_time))` aggregate read becomes `latest_open_time(…, include_forming=True)` + `age = (datetime.now(utc) - latest).total_seconds()`. The age now comes from the process wall clock instead of DB `NOW()`; both share the same system clock on the VPS (sub-second delta irrelevant against the minute-scale `STALE_LIMIT_S`). Side-effect hardening: `latest_open_time` checks `table_exists` and returns `None` instead of raising if `BTCUSDT_5m` were ever missing — the watchdog no longer crashes there.
- **Verification (build machine, DB-free — fleet Python 3.13.12).** `py_compile` + import smoke of all 7 files; `ruff check` + `ruff format --check` + `mypy` green on `core/` + the touched root bots; regression guard `smoke` (6 fixtures frozen+verified, perturbation caught) + `verify` (24/24 goldens) green. Live A/B is a no-op by construction (byte-equal reads), so it isn't measured separately.
- **Deliberately NOT in block 3:** the AI-bot direct readers with target `False` (block 4), the `11`/`12` index reworks (block 4, §5.5), the engine-read/writer rebuild (block 6/C-gate). Grace-period §5.4 gates block 4/6, not this block.

### Block 4 status — tranche 1 done (T-2026-CU-9050-111, 2026-07-13)

Block 4 (AI-bot direct readers) is implemented following **Michi's guiding principle** (§5): **detection on closed candles (`include_forming=False`)**, **live price only for generation** (via `get_live_price`, after the signal is detected). Because of the money-path risk, cut into **two tranches**. **Tranche 1** covers the bots without offset rework and without a live-CMP rebuild — six direct readers now read via `core.candles` with `include_forming=False`.

- **Rewired (tranche 1):**
  - `13_ai_rub_bot` — both reads (90d trend + indicator candle) via `read_candles`/`read_indicators`; **no-op**, the previous `open_time < date_trunc('hour', NOW())` filter (P1.19) is identical to the central closed cutoff for 1h.
  - `15_ai_master_bot.load_market_row` — `read_candles_with_indicators`, as-of read of the last closed candle before `floor(ts)`; **no-op**, `end = floor_utc − timeframe_delta("1h")` reproduces the strict `< floor_utc` bound byte-exactly (hour-aligned).
  - `9_ai_sr_bot.get_indicators_at_time` — `read_indicators(end=trade_ts, include_forming=False)`; a tightening at the edge: it used to fire a trade in the middle of the running hour, delivering that hour's partial indicators otherwise via `<= ts`.
  - `10_pump_dump_detector.get_indicators_at_time` — `read_indicators(limit=1, include_forming=False)`; **real R1 change**: the previous unbounded `DESC LIMIT 1` read the forming indicator row.
  - `18_ai_abr1_bot` — self-test sample + live read via `read_candles`; for 1h, `include_forming=False` is exactly the previous `open_time < current_hour_utc` cut, `limit=LIVE_DATA_HISTORY_HOURS` replaces the `.tail()` (the +5h overfetch goes away). `retest_idx = len(df)−1` stays the most recent closed candle.
  - `29_ufi1_bot.load_daily_ohlcv` — `read_candles(include_forming=False)`; **real R1 change** (the previous unbounded read pulled in the forming 1d candle too). 29 already fetches the live price separately via `get_live_price` (block 3) — exactly the pattern.
- **Dict-reader pattern:** 9/10/13(ind) used to build `dict(zip(cur.description, row))`; now `df.iloc[-1].to_dict()` (9: incl. `SELECT *` columns as before; 10/13: `open_time` only for ordering, then `.drop("open_time")`).
- **Deliberately in tranche 2 (T-…-follow-up):** the bots with an **offset rework** (`7_pattern_detector` `len−2→len−1`/`:-4→:-3`; `12_ai_ats` `−2/−3 → −1/−2`) and those with a **live-CMP deferral** (`22_ip_pattern`, `24_quasimodo`, `25_smc_ml_sniper`: pivots/structure on closed, `current_price` for entry/targets from `get_live_price` instead of `closes[-1]`) plus `11_ai_mis` (closed features + `get_live_price` entry + alias reproduction `tsi_fast`/`macd_dif`/`macd_dea`). `14_ai_atb` stays excluded (parked → ATB2 track T-106).
- **Verification (build machine, DB-free — fleet Python 3.13.12):** `py_compile` of all 6 files; `ruff check` + `ruff format --check` + `mypy` green; regression guard `smoke` (6 fixtures) + `verify` (24/24) green. The **real R1 changes (10, 29)** deliberately lower the signal rates — the 24h A/B is a post-merge VPS observation; thresholds only get tuned after the retrain (§5, question 6).

### Block 4 status — tranche 2 subset (offset rework 12 + 7) done (T-2026-CU-9050-111, 2026-07-13)

The two **offset-rework bots** without live-CMP deferral are rewired — the remaining four (`22`/`24`/`25`/`11`) follow in a focused step.

- **`12_ai_ats`** — `read_candles_with_indicators(include_forming=False, limit=500)`, the DESC reversal goes away. The TSI crossover detection already ran on `iloc[-2]` (closed) → with the forming candle excluded, the most recent closed one is `iloc[-1]`, so `current_idx −2→−1`, `prev_idx −3→−2` (the **same** detection candle). The entry price stays from the closed candle (operator exception). Transitional: the 500-row OBV baseline start shifts by exactly one candle — negligible until the ATS retrain (§5 q6).
- **`7_pattern_detector`** — `read_candles(include_forming=False, limit=168)`, the DESC reversal goes away. The breakout candle already ran on `len(df)−2` (closed) → now `len(df)−1`. The `iloc[:-4]` pivot-confirm buffer stays unchanged (index `len−4` is NaN-flagged anyway by the `rolling(9,center)`); the edge pivot only loses its previous forming repaint (correct R1 effect).
- **Verification (DB-free, fleet Python 3.13.12):** `py_compile` + `ruff check`/`format` + `mypy` green on both files.
- **Open (tranche 2 remainder, follow-up task):** `22_ip_pattern`/`24_quasimodo`/`25_smc_ml_sniper` (structure/pivots on closed, `current_price` = entry/targets via `get_live_price` **after** the detected signal instead of `closes[-1]`) and `11_ai_mis` (closed features + `get_live_price` entry + alias reproduction `tsi_fast`/`macd_dif`/`macd_dea`). Clarification point for the follow-up task: **which `get_live_price` source** these bots use (`3_detectors.get_live_price` sits in a numerically named, non-importable file — possibly lift the helper into `core/` or attach the existing batch ticker).

### Block 4 status — tranche 2 complete (22/24/25/11 + core/live_price.py) done (T-2026-CU-9050-111, 2026-07-13)

The tranche-2 remainder is rewired — **block 4 is thereby code-side complete** (only `14_ai_atb` stays excluded → ATB2 track T-106).

- **Source decision (Michi, 2026-07-13):** the `get_live_price` helpers from `3_detectors.py` (numerically named, non-importable) are lifted **1:1 into `core/live_price.py`** (`get_live_price` HTTP→DB-5m fallback, `get_live_prices_batch` 1 call/cycle); `3_detectors` re-exports both names (the batch-ticker test moves onto the real `requests` module object). **Important finding:** for `22`/`24`/`25`, `current_price` feeds the **detection gate** (level proximity/retest), not just the entry — the price must therefore be known **during** the scan. Hence a **batch ticker upfront** (`get_live_prices_batch()` once per scan, `price_map.get(sym) or get_live_price(sym, conn)` per coin) instead of `get_live_price` per coin (that would be ~N HTTP calls/cycle). The §5 guiding principle "price only after detection, no scan overhead" therefore only holds partially — one batch call per cycle, no per-coin overhead.
- **`22_ip_pattern`** — `read_candles(include_forming=False, limit=300)`, the DESC reversal goes away, pivots (`argrelextrema`) now run repaint-free on the closed frame (no manual drop needed). Explicit float cast on OHLC (`core.candles` delivers raw NUMERIC/Decimal — otherwise a `Decimal − float` crash in the QML gate). `current_price` = batch ticker.
- **`24_quasimodo`** — `read_candles_with_indicators(include_forming=False, limit=100)`, the `highs[:-1]/lows[:-1]` drop goes away. Offset shift from the missing forming candle: `touched_recently` `k=1..3 → k=0..2`, `feature_idx len−2 → len−1` (same closed candle, trainer geometry preserved). `candle_columns` without `symbol` (float-cast loop). `current_price` (proximity/SL/zone gates + entry) = batch ticker.
- **`25_smc_ml_sniper`** (heaviest offset rework) — `read_candles_with_indicators(include_forming=False, limit=150)`, the `highs[:-1]` drop goes away. All end-relative offsets +1: `last_closed len−2→len−1`, TD freshness gates `len−p3 <= PIVOT_WINDOW+2 → +1`, `n_closed len−1→len` (breakout search + follow-through now cover the last closed candle, the `find_breaker_setup` docstring updated accordingly), BB retest anchor `extract_ml_features(len−2)→len−1`. Chart tuples stay `(len−1, …, current_price)` = rightmost closed bar + live price. TD pivot indices (`p3`) unchanged (they address the full arrays). `current_price` (BB level proximity + `calculate_smart_targets`) = batch ticker.
- **`11_ai_mis`** — `read_candles_with_indicators(include_forming=False, limit=100)` in `_fetch_mis_frame`, the DESC reversal goes away. The API delivers raw indicator names → **`df.rename` reproduces the three `MIS_SQL_INDICATOR_SELECT` aliases** (`tsi_fast`/`macd_dif`/`macd_dea`), the frame stays byte-equal to `tools/walkforward_sim.py`; `indicator_columns` from the shared catalog (`RSI_COLS + RAW_LINE_COLS + 3 Rohnamen + atr_14`), **`MIS_SQL_INDICATOR_SELECT` untouched** (hard rule). Feature row `iloc[-2:-1] → iloc[-1:]` (still a 1-row DataFrame, byte-equal features of the same closed candle). Entry price = batch ticker. Also covers `startup_feature_selfcheck` (shared `_fetch_mis_frame`).
- **Contract 2 updated (`core/candles.py`):** `11_ai_mis`/`12_ai_ats` are **no longer forming readers** — the exception in the contract is removed; the live price comes via `get_live_price` (already listed as a forming reader) or, for `12`, from the last closed candle.
- **Verification (DB-free, fleet Python 3.13.12):** `py_compile` + `ruff check`/`format --check` + `mypy` green on all 5 files; `backtest/test_detector_batch_ticker.py` 4/4; regression guard `verify` 24/24 after every bot. **Live behaviour change (22/24/25 = signal geometry) → Michi's go before enqueue; the 24h A/B is post-merge VPS; thresholds only after the retrain (§5 q6).** The DB-bound `startup_feature_selfcheck` (bot 11) runs at VPS restart.

### Block 5 status — done (T-2026-CU-9050-112, 2026-07-13, PR #102 merged)

The two shared feature builders read via `core.candles` with `include_forming=False`, each with its trainer/replay caller in the same commit (hard rule 7). `core/funding_features.py` does NOT belong to block 5 (reads `funding_rates`, no candle read; `funding_features_asof` already cuts strictly `<`).

- **5a `core/research_features.fetch_context_frame`** — DESC f-string SQL → `read_candles_with_indicators(include_forming=False, candle_columns=(open_time,close,volume), indicator_columns=CONTEXT_IND_COLS)`, `.iloc[::-1]` **removed** (API ASC; the INVERSE of the block-2 trap — if the reversal stayed, the frame would be DESC again and `searchsorted` would be off). `CONTEXT_IND_COLS` as **one source** in `core/research_features` (derived from `CONTEXT_SQL_SELECT`), imported by `tools/research_dataset_common.load_candles_ctx` → live frame == offline/training frame byte-equal. **Feature parity = no-op** (feature index via `searchsorted` over open_time). **But:** bots 30/31/32 take `live_price = df["close"].iloc[-1]` — the entry anchor shifts from forming to the last closed candle (~≤59 min stale); bot `33_ai_fif1` (deployed) is unaffected (`sig["entry"]`). Follow-up **T-2026-CU-9050-113** (→ `get_live_price`, contract 2). **Done 2026-08-01 as T-2026-KYT-9050-011** (number-range migration): 30/31/32 now fetch the anchor via `core.live_price.get_live_price(symbol, conn)`, `None` ⇒ the signal is skipped instead of being posted with `None`; the feature candle is unchanged. For `32_ai_trm1`, `fetch_context_frame` stays as a pure data-freshness guard (BTCUSDT join present + not staler than `CONTEXT_MAX_STALENESS_H`); it no longer delivers a price there. Pinned in `backtest/test_research_bots_live_price.py`.
- **5b `core/regime_logic.compute_features`** — `"BTCUSDT_15m"`/`"BTCDOMUSDT_15m"` (literals) → `read_candles(include_forming=False)`. **Live gating change** (forming→closed 15m → `classify_regime` → `apply_debounce` → `regime_current` → orchestrator whitelist). **Backfill boundary correction:** the `include_forming` cutoff is **DB-`now()`-based** → does NOT drop the candle that's forming at a historical `as_of`; live runs without `end`, backfill with `end=last_closed_open_time("15m", as_of)` (API `end` inclusive → the candle forming at `as_of` falls out, no look-ahead). Explicit float cast on `high/low/close` + BTCDOM `close` (`core.candles` delivers Decimal). `26_regime_detector` (live) + `backtest/backfill_regime_history` (replay) delegate = **one** edit; `tools/regime_rules_study.py` is a block-1 replica (drift to live thereby closed).
- **Verification (DB-free, fleet Python 3.13.12):** `ruff`/`format --check`/`mypy` green on `core/research_features.py` + `core/regime_logic.py`; `backtest/test_feature_lookahead.py` 20/20 (two `fetch_context_frame` tests migrated to a fake reader + new `compute_features` read-contract test: live-without-`end` vs backfill-`end=last_closed_open_time`); `test_regime_detector` + `test_bot_regime_analyzer` 79/79; regression guard `smoke`+`verify` 24/24. **Reviews:** z-code-reviewer 3/3 PASS (N-vote) + z-spec-compliance PASS (7/7). **Post-merge VPS (open):** `backfill_regime_history.py` new → `regime_history` closed-correct → TRM1 retrain (train + serve read the same table, sequential jobs); thresholds only after the retrain (§5 q6).

### Block 6 status — part 1 (DB-writer code rewiring) done (T-2026-CU-9050-114, 2026-07-13, PR #104 merged)

Block 6 splits into **part 1 (code rewiring of the DB writers, reversible)** and **parts 2/3 (retrain rollout + C-gate, each step Michi-gated, NOT started)**. Part 1 rewires the candle/indicator **writers** from §2 "block A" onto `core.candles` and closes the four remaining API gaps. Built on the live VPS; **live write change → not auto-enqueued, Michi's go before the `cu/reviews` stamp** (then merge-train).

- **Four new `core/candles.py` functions (signatures frozen):** `latest_open_time(kind='indicators')` (indicator-table watermark), `delete_candles_before(cutoff, *, kind)` (retention, `<`), `delete_indicators_from(start)` (gap invalidation, `>=`), `list_coin_tables(tf=None, *, kind=None)` (shape-based enumeration via `_parse_coin_table` — only `{SYM}_{tf}[_indicators]` tables match, system tables fall out; replaces the raw `information_schema` scans + the `"trades"/"telegram"` substring blacklist).
- **`1_data_ingestion`:** `get_latest_open_time`→`latest_open_time(include_forming=True)` (resume byte-equal). `insert_fast`→`upsert_candles`, **closed/forming split** on `period_start(tf, now)` (`< cutoff` = closed, rest forming), two calls, one commit. `_flush_to_db`→`upsert_candles(closed=k['x'])` — **the WS buffer now carries the real Binance closed flag** (value = `(row, bool(k['x']))`); this is the first entry of `is_closed` into the data model via the WS path. SAVEPOINT-per-row preserved via a second cursor on the same transaction (block-3 pattern). `create_table_if_needed` DDL stays inline.
- **`2_indicator_engine` (highest R1 impact of the migration):** **core fix** — `process_coin_task` reads via `read_candles(include_forming=False)`, so indicators are now computed only on **closed** candles (previously breaking hard rule 5). Indicator `MAX(open_time)`→`latest_open_time(kind='indicators')`. `write_indicators_to_db_optimized`→`upsert_indicators`, commit moved to the caller (`process_coin_task`) (hard rule 8). DDL stays inline.
- **`6_housekeeping`:** gap scan→`read_candles(include_forming=False)`; gap filler→`upsert_candles(closed=True)` (`DO NOTHING`→`DO UPDATE … IS DISTINCT FROM`); retention→`list_coin_tables` + `delete_candles_before(kind)` (calendar cutoffs stay DB-side `now() - interval`); indicator invalidation→`delete_indicators_from`; delisted/table scan→`list_coin_tables`.
- **Review finding fixed (`4b2ce32`):** the gap filler counted rows **sent** (`upsert_candles` returns `len(rows)`) instead of rows **written**, which defeated the `candles_inserted_for_cointf == 0` guard on unfillable gaps (Binance `endTime` = `gap_end + expected_delta` sends the already-present right-edge candle `times[i]` along — a no-op upsert still counted → `delete_indicators_from` fired every run + the "N candles filled" log was inflated). Fix: exclude the guaranteed-present edge candle via `>=` — the counter now mirrors real fills, the guard is meaningful again.
- **Verification:** DB-free (`py_compile`/`ruff`/`format --check`/`mypy` green; regression guard `smoke` 6 + `verify` 24; `backtest/test_candles.py` 47/47, 16 new). **DB parity on the live VPS** (`cryptodata`): new read-only byte tests (`list_coin_tables` vs `information_schema`, `latest_open_time(kind='indicators')` vs `MAX`) green; the delete byte tests run against session-local `TEMP … ON COMMIT DROP` tables, **gated behind `KYTHERA_CANDLES_WRITE_PARITY`** (default read-only, hard rule 1) — green + no schema leak. Both core reviews **PASS** (z-code-reviewer 3-vote 2 APPROVED/1 NEEDS WORK, all convergent on the one gap-filler finding → fixed; z-spec-compliance 18/18 ACs, no scope creep).
- **Deliberately NOT in part 1 (C-gate/phase 2):** the **1d/1w WS removal** (REST only, saves ~1,300 streams — §5 q3, D-2026-CLD-109) sits in C-gate phase 2, not in the code rewiring. No hypertable DDL, no dual write, no `KYTHERA_CANDLES_SOURCE=hyper`, no retrain — all parts 2/3.
- **Open (Michi-gated):** block 6 **parts 2/3** — park the ML fleet → retrain on R1-clean walk-forward labels (sequential jobs) → version bump (ABR2/EPD2/… new `model_id`) → C-gate phases 0–5 (hypertable DDL, dual write, backfill, ≥5–7 days parity, read cutover, cleanup = drop the ~9.7k old tables after 7 days + pg_dump). C-gate start is gated behind the T-061 rerun queue. From part 3 on, the R1 box closes in `AUDIT_TODO.md`.

### C-gate phase 0 status — empty hypertables created + executed (T-2026-CU-9050-118, 2026-07-13, PR #108 merged)

C-gate phase 0 = create the two **empty** target hypertables; `core.candles` keeps reading LEGACY (`KYTHERA_CANDLES_SOURCE=legacy`), no bot is touched. Executed on the live VPS (DDL step → Michi's approval before stamp + `--execute`). Rollback is trivial (`DROP TABLE` — nothing reads the new tables until the phase-4 cutover).

- **New module `core/candles_schema.py`** — idempotent `ensure_hypertables(conn)` following `core/oi_5m.ensure_schema` (self-committing, rollback-on-failure). Runner `python -m core.candles_schema` (default = DB-free dry-run print; `--execute` = live DDL). The TimescaleDB extension was already installed (2.26.3, via `oi_5m`/`ticker_10s`), so only `CREATE TABLE` + `create_hypertable`, no extension install.
- **`candles`** (9 columns, §1): `symbol, tf, open_time, open, high, low, close, volume, is_closed`, PK `(symbol, tf, open_time)`. `tf` a real column (was implicit in the per-coin name), `is_closed boolean DEFAULT false` = the R1 contract.
- **`indicators`** (113 columns): `symbol, tf, open_time, is_closed, close` + the **108** indicator columns from `2_indicator_engine.get_indicator_definitions()` — derived **at build time** via importlib (module name starts with a digit, pattern from `backtest/test_gap_continuity.py`), so the hypertable never drifts from the engine/writer (report #18).
- **Decisions D-2026-CLD-109:** **REAL→double precision** for all numeric columns (`_pg_type`; verified 0 `float4` in `indicators`, `trend_direction` stays `text`), **retention unlimited** (no policy), **compression deliberately deferred to phase 5** (operator decision 2026-07-13) — phase 0 = tables + hypertable + index. `create_hypertable(...,'open_time',chunk_time_interval=>'7 days')` in the classic form (like `oi_5m`; equivalent to the `by_range()` from §1, chosen for in-repo precedent).
- **Verification:** DB-free tests (`backtest/test_candles_schema.py`, 5× — canonical parity vs engine defs, REAL→double/TEXT mapping, writer-parity lowercasing, fake-connection behaviour test that runs EXACTLY the phase-0 DDL with **no compression/retention** + rollback-on-failure). Guard smoke+verify 24/24, ruff/format/mypy clean. **Live verified:** both hypertables in `timescaledb_information.hypertables` (1 dim `open_time`, 7-day chunks), empty, no compression/retention jobs, column parity against legacy `BTCUSDT_1h_indicators` = exactly `{tf, is_closed}` new, no legacy column lost, composite index `idx_{tbl}_sym_tf_ot`. Both core reviews **PASS** (z-code-reviewer APPROVED 0 CRITICAL/HIGH; z-spec-compliance 9/9 ACs).
- **Phase-0 gate `backtest/test_candles_db_parity.py` = 11/12.** The one failure (`test_include_forming_false_drops_only_forming_rows`) is a **now-anchored freshness assertion** (`start = period_start(tf, now) − 10·Δ`) that fails on the **ingestion outage** (window empty, since the candles end at 07:25) — **not a phase-0 regression** (legacy reads, orthogonal to the empty hypertables).
- **Open (Michi-gated):** C-gate phases 2–5 (dual write incl. 1d/1w WS removal, backfill, ≥5–7 days parity 0-drift, read cutover `KYTHERA_CANDLES_SOURCE=hyper` + restart, cleanup = drop the old tables) + retrain rollout. The R1 box only closes with phase 5.

---

### C-gate phase 2 (build) status — dual write + backfill + 1d/1w WS removal (T-2026-CU-9050-119, 2026-07-13)

Three reversible, dormant code slices, each its own PR + both core reviews PASS. **Activation (flag on + deploy + backfill + parity observation → phase 3) is fully operator-gated;** no merge changes live behaviour. Reads stay legacy until phase 4.

- **2a Dual write (PR #110, merged):** `KYTHERA_CANDLES_DUAL_WRITE` (default OFF) → `upsert_candles`/`upsert_indicators` additionally write the hypertables within the caller transaction. No bot change (`closed`+`tf` already landed in the signatures in part 1 for this). `is_closed` in SET + `IS DISTINCT FROM` (forming→closed flips in place); indicators `is_closed`=true (the engine only computes on closed candles).
- **2b Backfill (PR #111):** `tools/candles_backfill.py` copies the per-coin history into the hypertables once (a complement to the forward-only dual write). Idempotent (`ON CONFLICT DO NOTHING`), resumable (progress file). Per-row `is_closed=(open_time<period_start(tf,now))` instead of the `…,true` sketch (the old tables carry the forming candle). Indicators copy/cast, NO recompute (D-109 #4). Default dry run; `--execute` writes.
- **2c 1d/1w WS removal (PR #112):** `1_data_ingestion` — `WS_TIMEFRAMES = TIMEFRAMES − {1d,1w}` on both `@kline` builders; REST/catch-up unchanged (1d/1w keep arriving via REST). Saves ~1,300 streams (D-109 #3). WS stays 5m–4h.

Verification: DB-free tests + DB-gated byte tests behind `KYTHERA_CANDLES_WRITE_PARITY` (write into real hypertables, `conn.rollback()` = zero persistence, hypertables verified empty); guard 24/24; ruff/format/mypy clean, whole-repo `ruff check .` green. **Open: activation + phases 3–5** (Michi-gated). Note: the forward dual write produces nothing while live ingestion is down (outage ~14h); the historical backfill is independent of that.

### C-gate phase 4 status — hyper-read backend built (dormant) (T-2026-CU-9050-128, 2026-07-16)

The read-cutover code is built, **dormant behind the flag `KYTHERA_CANDLES_SOURCE=hyper`** (default `legacy`). The flip itself (+ fleet restart) stays Michi-gated and is trivially rollbackable (flag back + restart). No bot touched — the core.candles read call sites route automatically (design intent phase C).

- **Hyper path** in `core/candles.py` for `read_candles`, `read_indicators`, `read_candles_with_indicators`, `latest_open_time` + `indicator_column_names`: instead of the per-coin table from `candles`/`indicators` `WHERE symbol=%s AND tf=%s`. `_assert_legacy_backend()` → `_candle_source()` (validates the flag, dispatches reads; **WRITES/DELETES don't branch** — they always write legacy, the hypertables are kept fresh by the separate `KYTHERA_CANDLES_DUAL_WRITE` mirror, which must stay ON through phase 4→5 → a source flip never stops ingestion).
- **Exact legacy semantics** (behaviour-neutral): the forming filter is **clock-based** (`open_time < period_start`), NOT `is_closed` (which can lag the clock at the edge-candle race → a parity break). `tf`/`is_closed` (hypertable-only) excluded from every projection → legacy shape + ordinal ordering (the `indicators` hypertable lists `tf` after `symbol` and `is_closed` before `close`; dropping the two restores `symbol, open_time, close, …`). The join read fences **both** sides in `(SELECT … OFFSET 0)` subqueries — joining two hypertables on the partition column otherwise triggers TimescaleDB's `mergejoin input data is out of order` (merge over ordered-append paths); the fence forces sort/hash.
- **`table_exists`/`list_coin_tables` stay phase-agnostic** (no hyper branch): they probe the per-coin **relations**, which exist until the phase-5 drop. `SELECT DISTINCT symbol, tf` over the 40M-row hypertable measures >20s (chunk partitioning beats even a loose index scan) and would block the 6_housekeeping retention — which in hyper-read mode deletes the legacy tables regardless. Post-phase-5 (tables gone), both return empty/False.
- **Acceptance (live VPS, read-only):** `backtest/test_candles_db_parity.py` proves **hyper == legacy** for BTC/ETH/SOL + smaller coins over 5m/1h/4h/1d, with/without forming, different windows/limits — candles byte-for-byte, indicators at **float4 precision** (legacy REAL vs hyper `double` = the intended P3.12 upgrade, not drift; a float32 cast reproduces the REAL bit-exactly, a real value difference still shows). 28 coin/TF candle reads + 21 with indicators green. DB-free: `test_candles.py` (source resolver, unknown-backend reject, hyper validation before the connection). Regression guard smoke+verify 24/24, ruff/format/mypy green.
- **Deliberately NOT in this task:** the 6 direct-SQL bypass readers (`11_ai_mis`, `14_ai_atb`, `34_ai_max1`, `core/mis_features`, `core/research_features`, `db_schema_analysis`) bypass core.candles and read `_indicators` directly — rewiring is needed for the phase-5 drop, NOT for the cutover (legacy stays fresh via dual write). The flip `SOURCE=hyper` + restart itself (Michi).

### C-gate phases 3–5 status — ACTIVE since 2026-07-16, two readers stuck (T-2026-KYT-9050-002, 2026-08-01)

Measured read-only on the live system; full numbers in `docs/T-2026-KYT-9050-002-c-gate-status.md`.

- **The migration is live, not dormant.** The `.env` carries `KYTHERA_CANDLES_SOURCE=hyper`,
  `KYTHERA_CANDLES_WRITE_PRIMARY=hyper`, `KYTHERA_CANDLES_DUAL_WRITE=1`. The write-primary flip
  took effect at **2026-07-16 16:23 UTC** (watchdog restart
  `watchdog_debug_20260716_192326.log`; every per-coin table ends exactly at
  `open_time = 2026-07-16 16:00 UTC`). The hyper store is complete and current
  (527 symbols, candles **and** indicators gapless across the boundary, `is_closed=false`
  on exactly 527 rows per TF).
- **Phase 3 (parity cron ≥5–7 days) has been skipped** and cannot be made up
  after the fact: `tools/candles_parity.py` compares legacy vs. hyper, and the legacy side
  has been empty for 16 days — the live run reports `rows old=0` for every symbol. The tool
  itself is intact (self-check and dry run green under both 3.14 **and** fleet 3.13).
- **Two readers got stuck at the deferral.** `16_smc_forex_metals_bot.py:87`
  and `21_btc_smc_strategy.py:136` are the **only** raw SELECTs on per-coin tables remaining in
  the live code (the bypass readers 11/14/34 + `core/*_features` named in "stand phase 4" have
  been rewired since `e5bddde`). Both were held back here as
  "index-coupled — flip only together with an offset rework". That deferral
  assumed the legacy tables would stay authoritative — exactly that lapsed on
  07-16. **Both bots have been running and reading 16-day-old candles ever since.**
- **Blast radius verified, harmless so far:** `CH_SMC_METALS` last posted on
  2026-07-16 09:35 UTC (9 posts across the entire outbox window since 04-17),
  `CH_BTC_SMC` has **zero** posts. So no signal has arisen from stale data yet —
  but both bots emit a Cornix-parsable block, so the case would have been
  money-relevant.
- **No fix in this task**: both bots have effectively been shut down for 16 days; hooking
  them up to live data is an **un-parking** (OPUS-HANDOFF §6 → Michi). Alternative: deliberately
  park them via `control/parked/`. Only after that may the rows in §2 block C be closed.
- **Phase 5 open:** compression is **not** active on either hypertable (0 of
  128 chunks each, no policy), the legacy tables (**64 GB**, not the 25 GB
  named in the design doc) still stand. A drop additionally needs removing the
  `CREATE TABLE IF NOT EXISTS` loop in `6_housekeeping.py:67`, otherwise the
  next housekeeping cycle re-creates ~4,200 empty tables.

## 5. Open operator questions (Michi)

These questions block the start of phase 1. None of them has been decided in this task.

> **Update 2026-07-13 (D-2026-CLD-109):** the C-gate questions are decided (Michi) — **1. retention: unlimited** (compression only, no retention policy), **2. REAL → double precision: yes** (all ~120 columns), **3. 1d/1w: REST only, no WS**. Plus: retrain all bots in sequence (sequential jobs). Details in `docs/TIMESCALE_R1_MIGRATION.md` §5.
>
> **Update 2026-07-13 (block 4, T-2026-CU-9050-111):** the **block-4 questions are decided** (Michi) — **4. close-grace period: `0`** (`KYTHERA_CANDLES_CLOSE_GRACE_SEC=0` stays the default; "more honest" — a candle counts as closed in the millisecond its period ends; raisable later via env var with no code change).
> **5. Guiding principle detection vs. generation (supersedes the first §5.5 interim state "True+split"):** **signal detection** (does a signal fire? — pivots, breakout, TD/QM structure, level proximity, ML features) runs **uniformly on closed candles** → `include_forming=False`; the forming candle is **no longer present** in the analysis frame. The **live price** is only needed **at signal generation** (entry1/CMP, `calculate_smart_targets`) and is fetched **separately via `get_live_price`** (Binance ticker, fallback to the newest DB close), only **after** a signal has been detected — so no query overhead per scan. Applies **uniformly to all block-4 bots incl. `11_ai_mis`/`12_ai_ats`**: features from the last closed candle, entry price via `get_live_price`. Exception flag: `12_ai_ats` already takes its entry price from a closed candle today (`iloc[-2]`) — stays unchanged (not flipped live) unless explicitly wanted. This makes the live CMP a pure generation concern; the pattern bots (24/25/7/22) noted in the inventory (§2 block C) as "F, drop removed" are thereby correctly resolvable. **Question 6 (signal rates)** stays an operating rule: R1 deliberately lowers the rates, thresholds get retuned only **after** the retrain.

1. **Retention** (T-018 §5.1): unlimited history (compressed ~4–6 GB) or a window? Design doc's recommendation: unlimited.
2. **`REAL` → `double precision`** for the ~120 indicator columns (P3.12)? Recommendation: yes, as part of the schema rebuild. Consequence here: `tools/candles_parity.py` canonicalizes floats to 12 significant digits, so the type change doesn't flag every row as drift.
3. **1d/1w still over WS** or only REST/catch-up (saves ~1,300 streams)? Recommendation: REST only for 1d/1w.
4. **Close-grace period.** Default `KYTHERA_CANDLES_CLOSE_GRACE_SEC=0`: a candle counts as closed in the millisecond its period ends. Alternative 2–5s against the pre-close tick race. `0` is more honest, `>0` more conservative. **To be decided before the first `include_forming=False` bot.**
5. **`11_ai_mis` / `12_ai_ats`:** both need the forming candle as the live price and the second-to-last as the feature row. Do they stay on `include_forming=True` with an explicit split (my suggestion), or should they make two calls (`read_candles(include_forming=False)` for features + `latest_price()` for the price)? The latter is cleaner but costs a second query per coin and cycle.
6. **Signal rates.** R1 **lowers** them — that's the point. Classic strategies fire less often, MIS/RUB/ATB feature distributions shift. **Retune thresholds only after the retrain** (Report 16), not during the rewiring.
7. **Owner + branch model.** T-018 §4 demands "migration as ONE branch with a clear owner". With parallel sessions on the same repo that's a precondition, not a recommendation.
8. ~~**Clean-up approval** (side findings): delete `tools/db_schema_analysis.py` as a duplicate? Delete `legacy_trainers/`?~~ — **both decided, 2026-07-10 (T-2026-CU-9050-039).** `tools/db_schema_analysis.py` is **deleted** (stale, never runnable; root is canonical). `legacy_trainers/` **stays** — it is frozen provenance for the eight live-loaded artifacts, not dead code. Deleting it would destroy the reproduction basis of MIS1/ABR1/ATS1/RUB1/SRA1/AIM1/EPD1/ATB1 in order to remove files nobody runs and that are already excluded from ruff/mypy (`docs/OPUS-HANDOFF.md` §4.12: excludes are not to be cleaned up as an end in themselves). This question no longer blocks phase 1.

---

## 6. Verification of this package

| Artifact | Verification | Status |
|---|---|---|
| `core/candles.py` | `ruff check` + `ruff format --check` + `mypy` (= CI) | green |
| `core/candles.py` | `backtest/test_candles.py` — 31 DB-free tests: cutoff arithmetic (incl. the Monday anchor and TZ independence), identifier hygiene, TF sync against `core.config`, argument validation, phase-4 seam | green |
| `tools/candles_parity.py` | `python tools/candles_parity.py --self-check` (DB-free); clean exit 2 without credentials | green |
| Regression guard | `python tools/regression_guard/guard.py smoke` | green (untouched) |
| `tools/candles_parity.py` against both tables | DB needed | **open — VPS, from phase 2** (hypertable doesn't exist yet) |
| **Phase-0 gate from T-018: "API reads byte-equal to direct SQL"** | `backtest/test_candles_db_parity.py` (T-2026-CU-9050-018): DB-free canonicalization core (3 tests, runnable everywhere) + 7 DB tests against the OLD per-coin tables — `read_candles`/`read_indicators` byte-equal to direct SQL, `limit` = newest n + ASC, `include_forming=False` drops exactly the forming rows, the join read leaves the candle side unchanged, `latest_open_time` = `MAX(open_time)` | **green — VPS run 2026-07-12** (DB `cryptodata`, BTCUSDT_1h) |

The build machine has no DB credentials; every DB-bound verification belongs in a VPS session (T-2026-CU-9050-011). `test_candles_db_parity.py` cleanly skips the DB tests there (`pytest.skip`) and only runs the canonicalization core — the phase-0 gate run above took place in a dedicated VPS-owner session (T-2026-CU-9050-018, read-only SELECTs, no writes/DDL).
</content>

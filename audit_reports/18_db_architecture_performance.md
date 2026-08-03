# 18 — DB architecture, performance & calculation consistency (Step 9)

**As of:** 2026-07-04 · **Method:** (A) live measurements directly on the VPS DB (PostgreSQL 17.6, `cryptodata`, statistics window since reset 2026-06-21; the fleet was stopped at measurement time), (B) TimescaleDB as-is analysis, (C/D) two parallel code reviews of calculation consistency across all bots (money math + indicator variants). Closes the performance points left open in the question catalogues of reports 02/12.

---

## A. Live DB measurements (facts)

| Metric | Value | Context |
|---|---|---|
| DB size | **25 GB** | of which ~22.4 GB in per-symbol tables (13 GB `_indicators`, 6.8 GB candles 15m–1w, 2.6 GB 5m) |
| Tables total | **9,782** | 5,522 candle + 4,090 indicator tables + ~170 misc |
| Hypertables | **0** | TimescaleDB 2.26.3 installed, **completely unused** (section B) |
| WAL volume | **110 GB in 13 days ≈ 8.5 GB/day** | 4.4× the DB size per month — churn-driven, `wal_compression=off` |
| Dead tuples total | 5.86M (9.2% of live) | autovacuum (4 workers) structurally can't keep up with 9,782 tables |
| Catalog size | **685 MB** (pg_catalog) | consequence of the table sprawl (~100k catalog entries for columns/indexes) |
| Dead tables | **1,010 tables, 1.75 GB** (0 rows, 0 inserts) | old generations (`conv_signals`, `bot_trades4`, `PAXGUSDT_5m_GOLD`, …) |
| Quarterly leftovers | **108 tables** (`BTCUSDT_260925_*` et al.) | consequence of P2.16 (coins.json writer drift incl. quarterlies) |
| max_connections / RAM settings | 200 · shared_buffers 16 GB · effective_cache_size 48 GB · work_mem 64 MB | RAM side solidly sized |

### A1 — The five performance findings (new, with evidence)

- [ ] **D1 (HIGH): `closed_trades_master` + `closed_ai_signals` are read exclusively via full-table scan.** Since 21.06.: `closed_trades_master` 1.80M seq_scans / **215 billion tuples read** / 0 idx_scans; `closed_ai_signals` 1.52M seq_scans / **219 billion tuples** / 0 idx_scans. Every cooldown check of the classic strategies (per coin per cycle!), every analyzer/tracker evaluation reads the entire table. That's ~33 billion tuples/day of pure scan load — the main reason the detector cycles and the hourly analyzer are expensive. **Fix:** indexes `closed_trades_master(strategy, posted)`, `closed_ai_signals(model, close_time)` resp. `(symbol, model, direction, open_time)` — the latter directly as **UNIQUE** (this also fixes the duplicate backstop from report 14 A.1). `active_trades_master` (321k seq_scans) benefits from `(symbol, strategy)`.
- [ ] **D2 (HIGH): `telegram_outbox` is bloated to 304 MB for 17k rows** (≈18 KB/row; 27.7k updates + 25.8k deletes in the window, only a PK index). The dispatcher poll (500ms loop from 4 and 28) runs over a 300 MB heap. **Fix:** `VACUUM FULL telegram_outbox` (with the fleet stopped, seconds) + partial index `ON telegram_outbox (id) WHERE sent=FALSE AND failed=FALSE` + `fillfactor=70`. The table should then stay permanently below 10 MB.
- [x] **✅(2026-07-04: code lever implemented — WHERE IS DISTINCT FROM in both upsert paths of 1_data_ingestion.py; wal_compression=on + R1/TimescaleDB still outstanding) D3 (MEDIUM): write amplification of the 3s upsert loop measured:** every 5m candle gets **overwritten ~15×** (per 5m table 52,945 updates against 3,503 inserts in the window, × 698 symbols ≈ 2.8M updates/day just for 5m). Together with the 12h catch-up (7-day rewrite), that explains the 8.5 GB WAL/day. **Short-term fix:** upsert only on changed close/volume (`WHERE` clause on the UPDATE part), `wal_compression=on` (one line, ~50% WAL savings); **properly solved** by R1 (write only closed candles) resp. section B.
- [ ] **D4 (MEDIUM): `bot_regime_performance` is an update hotspot:** 830k updates + 548 autovacuums on a tiny table (the hourly analyzer rewrites every cell individually). **Fix:** `TRUNCATE`+bulk `INSERT` per run or `ON CONFLICT` batching; `fillfactor=50` for HOT updates.
- [ ] **D5 (LOW): clean up data junk:** drop 1,010 dead tables (1.75 GB) + 108 quarterly tables (list generable via `n_live_tup=0 AND n_tup_ins=0`); cap `pump_dump_events` (829 MB, largest table, rsi/tsi columns never populated — P1.40) with a retention policy; check retention on `master_ai_processed_signals` (138 MB, 920k rows). Together ~2.7 GB immediately plus ongoing growth stopped.

---

## B. TimescaleDB: installed, unused — assessment & migration path

**As-is state:** extension `timescaledb 2.26.3` is loaded (it's in `shared_preload_libraries`), but **0 hypertables** — the entire time-series load runs over 9,612 plain tables. That's the worst of both worlds: you pay the extension overhead and get none of the benefits.

**Why the current architecture (one table per symbol×tf) causes the problems in A:**
1. 9,782 tables → 685 MB catalog, ~100k autovacuum target tables for 4 workers, no global query ("all coins with a gap") without a 698-fold loop — exactly the pattern of the 8,600 serial `to_regclass+MAX` queries in the catch-up (report 02).
2. Schema changes (e.g. the `is_closed` column from R1) have to be rolled out 9,612 times.
3. No compression/retention concept: the 13 GB indicator tables are cold, append-only history — ideal for compression, but sit uncompressed in the heap and WAL.

**Recommendation (target picture):** two hypertables instead of 9,612 tables:
- `candles(symbol text, tf text, open_time timestamptz, o/h/l/c/v, is_closed bool, PRIMARY KEY(symbol, tf, open_time))` — `segmentby=symbol`, `orderby=open_time`, chunk 7d, compression after 14d, retention as needed.
- `indicators(symbol, tf, open_time, …spalten…)` analogously.
- Expected effects: compression on OHLCV/indicator data typically **90%+** (25 GB → realistically 4–6 GB), WAL drops massively (compressed chunks no longer get rewritten), autovacuum load collapses (2 tables instead of 9,612), global queries (gap census, staleness monitoring, cross-coin features) become single-row, `is_closed` (R1) is a single column.

**Effort & risk (honestly):** this is **not a quick fix but a rebuild** — all ~40 f-string table-name accesses (`f'"{sym}_{tf}"'`, report P3.3) have to be converted to `WHERE symbol=%s AND tf=%s` (ingestion, engine, housekeeping, all bots, trainers). Realistically: build a core helper `candles_read(sym, tf, n)` in `core/`, migrate via dual-write (populate the new hypertable in parallel, migrate readers step by step, drop the old tables only after verification). Sensible timing: **together with the R1 fix**, because both touch the same code. Until then, D1–D5 + `wal_compression=on` deliver most of the short-term relief without architectural risk.

---

## C. Consistency matrix "money math" (code-verified)

### C1 — PnL calculation
**Positive:** the core formula (price delta/entry×100, direction-negated, without leverage/fees) is identical in 5, 8, 23, 27, 28; entry2 never flows into measured PnL.

- [ ] **K1 (CRITICAL): orchestrator outcome by random match.** `28_signal_orchestrator.py:883-918`: `sync_closed_trades` classifies ROM1 trades by the PnL of an **arbitrary** coin/direction-matching foreign trade (30-day window, `LIMIT 1` without `ORDER BY`). The meta layer's self-assessment is non-deterministic. (Sharpens P1.8.)
- [ ] **K2 (HIGH): SL trailing diverges between the monitors.** `5_trade_monitor.py:243-247` leaves the SL at entry (breakeven) forever after TP1, `8_ai_trade_monitor.py:203-226` trails to the previous target. Identical market paths → classic ≈0% PnL, AI clearly positive. **All bot comparisons (23, 27, whitelist) are structurally skewed by this** — classic is systematically measured worse than AI. (Refines P1.2: the 5-monitor doesn't trail "wrong", it doesn't trail at all.)
- [ ] **K3 (HIGH): "win" is ambiguous.** Status `n` from `5_trade_monitor.py:222` means "stopped out via SL after TPn" (close=entry → PnL≈0). The same trade is: a **win** in the classic cooldown (`strat_fast_in_out.py:46`, status 1–4), **neutral** in tracker/analyzer (pnl>0.1% rule, `23:941-959`/`27:126-153`), a **loss** in legacy evaluations ('SL1'→NaN). Cooldown control and performance reporting work with contradictory notions of success.
- [ ] **K4 (MEDIUM): the legacy path in 8 measures differently than the modern one** (`8:175-184`: close-based, ±2.5/−5% thresholds; modern: wick-aware at the level) — legacy and modern AI trades aren't comparable.
- [ ] **K5 (MEDIUM): trainer PnL incompatible with the live metric:** `qm_ml_trainer.py:224-238` computes USD with 20x + fees, `smc_ml_trainer.py:294-296` in R-multiples with **defined but unused** fee/leverage constants, live measures unlevered price-% — trainer thresholds and live win rates are not the same quantity.

### C2 — TP/SL construction (family drift)

| Family | TP | SL | Entry2 | Peculiarity |
|---|---|---|---|---|
| smart-targets (7, 11, 15, 18, 25, open_handler) | S/R+Fib+HVN+FVG, ATR spacing | ATR 3×, **hard cap 15%** | ATR 1.5×, cap 10% | reference implementation |
| get_hvn family (9, 10, 12, 13, 28) | S/R zones, up to 20 TPs | next zone, **no max cap!** | fixed ±5% | ROM1 SL occupied up to 65% (P2.27) |
| **14 ATB** | like get_hvn | fallback **±5%** instead of ±2.5% | **±4%** instead of ±5% | undocumented family break (`14:525-529`) |
| Classic (5 strats) | fixed % or zones | ATR caps 2.5–5% resp. fixed | no | — |
| 16/17/21 SMC | pivot level | opposite pivot, no sanity cap | 16: "entry 2" **only in the Telegram text** | 21 the only 100x bot, all three without tracking |

- [ ] **K6 (MEDIUM):** two SL philosophies for the same `ai_signals` table: smart-targets caps at 15%, the get_hvn family has no cap (evidence for R4/P2.27). ATB additionally deviates from its own family for no documented reason.

### C3 — Cooldowns
Three incompatible worlds: core `check_cooldown` (True=**blocks**, DB, TZ-safe) · 99_paper (True=**allows**, RAM, side effect on check) · classic strats (**global win counter** instead of time cooldown, TZ-naive → window shifted by server offset, and "win" includes breakeven stopouts per K3). Cooldown durations of the same signal family: 15min (EPD1) to 48h (UFI1), undocumented; the orchestrator adds its own 4h on top.

### C4 — Entry price sources (staleness classes)

| Freshness | Bots |
|---|---|
| Seconds (REST/ticker) | 3_detectors (classic), 10 EPD1 |
| ≤5 min (5m close) | 28 ROM1 |
| ≤1 h (1h close) | 11, 12, 13, 14, 18, 29 |
| **≤4 h** (scan-tf close) | 24 QM, 25 TD/BB |
| ≤60 min (foreign entry) | 9 SRA1 (takes over the classic entry) |

- [ ] **K7 (HIGH): entry staleness up to 4h with an immediate 5m check.** The monitor (`8:79-110`) checks all trades immediately against the current 5m candle — an up-to-4h-old "CMP entry" may already have run through the SL by the time it's posted → phantom SL hits, PnL against prices that were never tradeable. The win rates of the families are not comparable purely because of the freshness classes.
- [ ] **K8 (LOW):** the R:R gate computes with `avg(entry1,entry2)` (`11:334-336`, `25:351-353`), but the measurement is against entry1 — the gate evaluates a different trade than the one measured.

---

## D. Consistency matrix indicator calculations (code-verified)

### D core finding: same indicator name, several mathematics

| Indicator | Variants in the repo | Most consequential deviation |
|---|---|---|
| **RSI** | Engine `ewm(span)` (DB, all readers) · pandas_ta **Wilder** (ATB 14:177, ABR1 18:115, RUB1 training) · true Wilder (backtest v3) | DB `rsi_14` behaves like Wilder-RSI(7–8), avg Δ 4.84 points (Step 2). All DB RSI thresholds (RUB1 gate <30/>70, 5-percent band 55–75, MIS/QM/SMC features) fire on a different population than any chart-/pandas_ta-calibrated threshold. |
| **ATR** | Engine **Wilder** `ewm(alpha=1/p)` (2:420) · **SMA-ATR** in `core/trade_utils.py:38-45` (→ `calculate_smart_targets` → SL/entry2 for 7/11/15/18/25/open_handler) and `21:42-55` · **ewm(span)** in `core/regime_logic.py:105-109` (regime P75/P40 gates) | **Three ATR definitions**: "3×ATR SL" means something different per subsystem; calibrations (multipliers, percentiles) don't transfer between engine features, SL sizing, and regime classification. |
| **MACD** | Engine 9/21/9 ("fast") + 12/26/9 ("normal") | **RUB1 semantic break** (P0-class, report 13): trained on ta.macd(9/21/9), fed live with `macd_dif_normal_12_26_9` under the same feature name (13:153-154). |
| **OBV** | no engine column; ATS1 rebased locally over 500 candles (12:165-166) vs. training raw ~300d cumulative | explains the ATS1 calibration inversion (bucket 0.6–0.7 → 71% WR, 0.8–0.9 → 57%). |
| **Bollinger** | Engine ddof=1 vs. pandas_ta ddof=0 (14/18) | bands ~2.6% difference — small, but distorts cross-path comparisons. |
| **HVN** | **four independent definitions**: engine histogram (bins=√n, top 4) · trade_utils 60-bin top 6 · strat_volume "exact float close" (degenerate, P2.42) · `get_hvn_and_sr_levels` computes **no HVN at all despite the name** (trade_utils.py:248-283) | "HVN" in the Telegram text means something different depending on the bot; an ATS1 trade gets a different level pool than an ABR1 trade on the same chart (95d window without HVN vs. 1000h with HVN/FVG). |

### D2 — Pivot detection: five parameter worlds
argrelextrema order=**20** (engine, trade_utils) · order=**5** (market_utils→16/17/21, QM 24 + trainer, 22) · order=**10** (25 + smc trainer, ABR1 with **edge padding and >=**) · `find_peaks distance=8` without window dominance (ATB 14) · rolling window 9 with >= (pattern detector 7). Plus **train/live window breaks**: QM live LIMIT 100 vs. trainer 2 years; TD/BB live LIMIT 150 vs. 2 years; **TD pattern span live ≤50 candles vs. training ≤100** (25:199 vs. smc_ml_trainer.py:123) — the model was trained on a broader pattern population than is filtered live.

### D3 — Normalisation chaos (cross-model comparisons forbidden)
`atr_pct`: RUB1 0.01=1% vs. ATS/QM/SMC 1.0=1% (factor 100). Distance sign: `ema_200_dist` positive="above EMA" for MIS/ATS, **negative**="above EMA" for QM/SMC/master. Slope: four incompatible scales (engine raw, ATS ×1000/close, ATB %/day, RUB per-timestamp regression). Consistent within each model — but any dashboard/orchestrator evaluation that compares such features across models is factor-distorted.

- [ ] **K9 (MEDIUM): pandas_ta version fragility as a ticking clock.** ABR1 (18:116-136) and ATB1 (14:188-193) depend on exact pandas_ta column names; for ABR1, mismatches silently become 0 (P0.12), for ATB1 a KeyError pushes every prediction to 0.0 → **bot goes silent without an alarm** (14:266-268). `requirements.txt` is unpinned (P3.4) — a `pip install -U pandas_ta` silently changes behaviour. **Fix:** prefix matching everywhere + pin version + startup assertion.
- [ ] **K10 (LOW): engine fillna policy inconsistent** (RSI→50, EMA/MA/BOLL→0, KAMA→NaN) — the 0-fills are the root of P1.13; KAMA shows the correct practice.

---

## E. Prioritised recommendations

**Immediate (hours, no risk):**
1. Create the indexes from D1 (incl. UNIQUE backstop on `closed_ai_signals`) — the biggest single lever against the scan load.
2. `VACUUM FULL telegram_outbox` + partial index (D2); `wal_compression=on` (D3).
3. Drop the junk: 1,010 dead + 108 quarterly tables, `pump_dump_events` retention (D5).

**Short-term (days):**
4. Fix K1 (`model='ROM1'` filter + ORDER BY + ±60s window — aligns with P1.8) and decide K2: **one** trailing semantics for both monitors (otherwise every bot comparison statistic stays skewed).
5. Unify the "win" concept (K3): one central `classify_outcome()` in core, used by 23/27/cooldowns.
6. Staleness gate (K7): discard/reprice the signal if the entry candle is older than X minutes (defined per family).
7. Pin pandas_ta + prefix matching (K9).

**Medium-term (plan together with R1):**
8. TimescaleDB migration to 2 hypertables (section B) — structurally solves table sprawl, WAL, autovacuum, schema rollout, and global querying; dual-write migration via a `core/` helper.
9. One reference ATR/RSI implementation in `core/indicators.py`, migrate all local calculations onto it (D core finding); for retrains (report 16, section 8) consistently use the shared feature builder (X-R2).

**Context:** the measurements confirm the code suspicions from report 02 quantitatively (table sprawl real: 9,782; WAL pressure real: 8.5 GB/day; upsert amplification real: 15×) and add two new classes: the index gap on the evaluation tables (D1 — pure scan load of ~33 billion tuples/day) and the calculation inconsistencies (C/D), which explain **why bot comparison statistics are currently only limitedly reliable**: different trailing semantics, three win definitions, four entry freshness classes, and three ATR mathematics flow unfiltered into the same whitelist/ranking decisions.

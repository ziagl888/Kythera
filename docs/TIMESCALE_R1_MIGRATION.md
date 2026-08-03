# Design: R1 + TimescaleDB migration (candles/indicators → 2 hypertables)

**Status:** Draft (2026-07-04) · **Author:** Audit session · **Prerequisite:** Fleet running stably at the state after batch 4 + WS fixes.

> **⚠ This document is the DRAFT from 2026-07-04. Phases 3–5 have been
> live since 2026-07-16 — the phase table in §3 describes a future that is long since past.**
> Measured actual state, the quantitative picture and the still-open operator decisions:
> **`docs/T-2026-KYT-9050-002-c-gate-status.md`** (2026-08-01). In particular, superseded
> here: the 25 GB starting size (real 64 GB), "C: has ~160 GB free" (real 78 GB) and
> the promise "rollback is trivial in every phase" (no longer holds since the write-primary flip).

**Goal:** One project, two root causes:
1. **R1 (Audit #1):** Nail down the forming-candle contract — end look-ahead/repaint in ~all strategies and ML bots. Prerequisite for the entire retrain programme (report 16, section 8).
2. **Table sprawl (report 18):** 9,297 per-symbol tables → 2 hypertables. Expected effects: storage 25 GB → ~4–6 GB (compression), WAL collapse, autovacuum relief, global queries, schema changes in one column instead of 9,297 rollouts.

---

## 1. Target schema

```sql
CREATE TABLE candles (
    symbol     text        NOT NULL,
    tf         text        NOT NULL,          -- '5m','15m','30m','1h','2h','4h','1d','1w'
    open_time  timestamptz NOT NULL,
    open       double precision,
    high       double precision,
    low        double precision,
    close      double precision,
    volume     double precision,
    is_closed  boolean     NOT NULL DEFAULT false,   -- R1: der Vertrag
    PRIMARY KEY (symbol, tf, open_time)
);
SELECT create_hypertable('candles', by_range('open_time', INTERVAL '7 days'));
CREATE INDEX ON candles (symbol, tf, open_time DESC);

ALTER TABLE candles SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, tf',
    timescaledb.compress_orderby   = 'open_time DESC'
);
SELECT add_compression_policy('candles', INTERVAL '14 days');
```

`indicators` analogous: same keys + `is_closed` + the ~120 indicator columns. **Decision pending here (P3.12):** raise columns from `REAL` (float4) to `double precision` — sub-cent coins currently lose precision; compression makes the size difference nearly irrelevant.

**The R1 contract, concretely:**
- Ingestion writes every WS kline with `is_closed = k['x']` (Binance delivers the closed flag — today it is simply ignored).
- REST catch-up/gap-filler write `is_closed = true` (historical candles are closed by definition), except for the most recent period.
- **All indicator/strategy/ML readers consume exclusively `is_closed = true`.** Only price checks (monitors 5/8, get_live_price fallback) may explicitly see the forming candle.

## 2. One API instead of 40 f-string sites: `core/candles.py`

The migration is made invisible to the bots by routing ALL access through a central API beforehand:

```python
read_candles(conn, symbol, tf, limit, include_forming=False)   # → DataFrame, ASC
read_indicators(conn, symbol, tf, limit, include_forming=False)
latest_open_time(conn, symbol, tf)                              # Catch-up-Resume
upsert_candles(conn, rows, closed: bool)                        # Ingestion/Filler
upsert_indicators(conn, df, symbol, tf)                         # Engine
```

- Phase A: The API reads/writes the **old** tables (pure rewiring, no behaviour change — except for the deliberate `include_forming` default of `False`, which switches on R1 bot by bot).
- Phase C: The API is switched internally to the hypertable — **without touching a single bot.**

**Known call sites (rewiring backlog, ~40):** `1_data_ingestion` (flush, catch-up, snapshot) · `2_indicator_engine` (read candles/write indicators) · `6_housekeeping` (gap filler, delisted scan) · `3_detectors` + `strategies/*` (480×indicators, get_live_price fallback) · monitors `5`/`8` (5m polls — *include_forming=True*) · `chart_data_service` · AI bots `9,10,11,12,13,14,15,16,18,21,24,25,29` · `28` (ROM1 5m close) · `core/trade_utils` (2×), `core/market_utils` · trainers `qm/smc_ml_trainer`, `fib_backtest` · `tools/regression_guard`.

## 3. Migration phases (each with a gate)

| Phase | Content | Gate (must be green, otherwise stop) |
|---|---|---|
| **0. Prep** (~0.5 d) | Create hypertables (empty); `core/candles.py` against OLD tables; symbol whitelist validation in `load_coins` (P3.3 — prevents a new junk-table class in the hypertable) | Unit smoke: API reads byte-identical to direct SQL |
| **1. Reader rewiring** (~2–3 d) | Switch call sites to the API, order by risk: chart_data_service → strategies/3 → AI bots → monitors (with `include_forming=True`!) → engine reads. R1 takes effect here per bot (`include_forming=False`) | Regression guard after every block; signal rates in a 24h comparison (R1 WILL lower rates — document, don't panic) |
| **2. Dual write** (~1 d) | Ingestion + engine + gap filler write additionally into the hypertables (forward-only from activation); one-off backfill copy of history via `INSERT INTO candles SELECT ..., true FROM "{SYM}_{tf}"` (batch script, at night) | Parity query: row counts + max(open_time) + sample checksums old vs. new, per TF |
| **3. Observe parity** (≥5–7 days) | Fleet keeps reading OLD, writes twice. Daily automatic parity report (cron) | 0 drift findings on 3 consecutive days |
| **4. Read cutover** (~0.5 d) | Switch `core/candles.py` internally to the hypertable (feature flag `KYTHERA_CANDLES_SOURCE=hyper`), fleet restart | 24h operation: health monitor green, signal rates ±expected, query times via pg_stat_statements ≤ old |
| **5. Cleanup** | Dual write off; drop old tables only **after 7 more days** (pg_dump backup beforehand); compression/retention policies active; `open_time` single indexes disappear with the tables | Restore test of the dump; DB size & WAL rate documented (expectation: −70–80%) |

**Rollback is trivial in every phase:** Up to phase 4 the fleet reads the old tables; the cutover itself is an env flag + restart back.

> **⚠ This promise no longer holds since 2026-07-16.** With `KYTHERA_CANDLES_WRITE_PRIMARY=hyper`
> the per-coin tables are no longer written; they end on 2026-07-16 16:00 UTC.
> Flipping `KYTHERA_CANDLES_SOURCE` back to `legacy` silently sets the fleet to
> 16-day-old candles. The asymmetry is described in `core/candles.py` (§ Phase-5-Write-Primary)
> and measured in `docs/T-2026-KYT-9050-002-c-gate-status.md` §6.

**Actual state of the phases (measured 2026-08-01, details in the status doc):**

| Phase | Design state | Reality |
|---|---|---|
| 0 Prep | Gate | ✅ 2026-07-13, hypertables created |
| 1 Reader rewiring | Gate | ✅ except for **2 deliberately deferred** readers (`16_smc_forex_metals_bot:87`, `21_btc_smc_strategy:136`) — since the write-primary flip these read frozen tables, status doc §5 |
| 2 Dual write + backfill | Gate | ✅ backfill 2026-07-14 (9,669 tables) |
| 3 Parity ≥5–7 days | "0 drift on 3 days" | ❌ **skipped** — read cutover and write primary happened together; the gate can no longer be satisfied retroactively (status doc §3) |
| 4 Read cutover | Gate | ✅ live (`KYTHERA_CANDLES_SOURCE=hyper`) |
| 5 Cleanup | Gate | ⏸ **open** — compression not active (0 of 128 chunks per hypertable), legacy tables (64 GB) not dropped |

## 4. Risks & countermeasures

| Risk | Assessment | Countermeasure |
|---|---|---|
| **R1 lowers signal rates** (bots no longer see the forming candle — intended!) | Certain to occur; classic strats fire less often, MIS/RUB/ATB distributions shift | Communicate beforehand; shadow comparison for 1 week; retune thresholds only after retrain (report 16) |
| Disk during dual write (~+22 GB uncompressed) | C: has ~160 GB free | Compression policy from day 1 on chunks >14 d; backfill at night in batches |
| Upserts into compressed chunks (new coin loads 730 d of history) | rare, slower but supported | Backfill path decompresses selectively or writes before the policy kicks in |
| psycopg2 `execute_values` with new conflict target | small | encapsulated in `core/candles.py` + unit test |
| Trainer/backtests read old tables hard-coded | medium | rewire trainer reads in phase 1 too (batch E has just touched the loaders — coordinate!) |
| Monitors need the forming candle (price checks) | design pitfall #1 | explicit `include_forming=True` ONLY there; code review checklist |
| Two sessions working on the repo in parallel | real (has happened multiple times today) | migration as ONE branch with a clear owner; phase 1 in small commits per bot block |

## 5. Operator decisions — DECIDED (Michi, 2026-07-13)

Durable record: **D-2026-CLD-109** (KB). These four gate C-gate phases 2–5.

1. **Retention:** **UNLIMITED.** No `add_retention_policy` — only the compression policy. Once compressed, the full history is unproblematic (~4–6 GB).
2. **REAL → double precision** (P3.12): **YES**, for ALL ~120 indicator columns as part of the schema rebuild. Sub-cent coins lose precision under `REAL`; compression makes the size difference irrelevant.
3. **1d/1w:** **REST/catch-up only, no more WS.** Saves ~1,300 streams (IP-throttle risk). WS stays for 5m–4h. The rework sits in `1_data_ingestion` (block 6 / phase 2).
4. **Retrain:** **Rerun all possible bots one after another** (sequential-jobs rule, one job at a time). R1 (`include_forming=False`) shifts feature distributions fleet-wide → every ML model needs a retrain on R1-clean walk-forward labels; artifacts to `staging_models/`, rollout per bot is an operator decision. Prerequisite for indicator-dependent retrains: the historical indicator recompute (T-061/P1.13) — the backfill is a plain copy/cast, not a recompute, i.e. old indicators carry the forming-contamination value.

> **C-gate start time:** at the earliest after reader rewiring is finished (blocks 3–6) and after the T-061 rerun queue; every irreversible step (hypertable DDL, backfill, read cutover, table drop) stays escalation-gated (Michi).

## 6. Verification tools

- **Parity script** `tools/candles_parity.py`: compares row count, max(open_time), checksum over OHLCV of the last N days old vs. new per (symbol, tf); exit ≠ 0 on drift → as a phase-3 cron.
- **Regression guard** (tools/regression_guard): existing goldens run unchanged against the API — phase-1 gate.
- **pg_stat_statements** (active since today): query times before/after as a hard cutover metric.
- **Health monitor:** the DATA_STALE check remains the live canary; also temporarily extend to 2 symbols during cutover.

# C-Gate: current state, quantity framework, and open operator decisions

**Status:** 2026-08-01 (measurement 19:54–20:13 UTC on SRV02, live DB `cryptodata`) · **Task:** T-2026-KYT-9050-002 · **Design:** `docs/TIMESCALE_R1_MIGRATION.md` · **Inventory:** `docs/CANDLE_CALL_SITES.md` · **Decision record:** D-2026-CLD-109

All numbers below were measured in this session itself (read-only, no write query).
Anything not verified itself is marked **[not verified]**.
Times in UTC; the DB session responds in `Europe/Bucharest` (+03), conversion has been applied.

---

## 1. The core finding: the C-Gate is no longer dormant, it is live

The design doc and the KB task brief describe phases 3–5 as open and the
phase-2/4 slices as "built, but dormant". **That is outdated.** Measured:

| Flag (fleet's live `.env`) | Value |
|---|---|
| `KYTHERA_CANDLES_DUAL_WRITE` | `1` |
| `KYTHERA_CANDLES_SOURCE` | `hyper` |
| `KYTHERA_CANDLES_WRITE_PRIMARY` | `hyper` |

The fleet has been running fresh since **2026-08-01 16:30–16:34 UTC** (`logs/fleet_restart_20260801_193042.log`,
HEAD `e3181d5`) and is thus reading the hypertables.

**When the switchover actually happened — evidenced, not taken from the ticket:**
The per-coin tables all end exactly at `open_time = 2026-07-16 16:00 UTC` (1h) resp.
`16:20 UTC` (5m). The watchdog restart before that is `logs/watchdog_debug_20260716_192326.log`
(2026-07-16 16:23 UTC). `core/candles.py` skips the per-coin write entirely when
`write_primary == "hyper"`. Chain closed: **`WRITE_PRIMARY=hyper` has been in effect since
2026-07-16 16:23 UTC — for 16 days.**

What was changed in the `.env` today at 10:13 UTC cannot be reconstructed from the file mtime
alone — **[not verified]** which of the three flags that was.

### The UTC flip was NOT activated alongside it

The coupling feared in the brief did not occur:
`git merge-base --is-ancestor 3ba3bbd e3181d5` → **no**. The R3 UTC flip from
T-2026-KYT-9050-005 sits on `main`, but is **not** in the running fleet state. The two
changes are already decoupled; the next restart activates the UTC flip alone.

Structurally, the candle path was never exposed to this anyway: `open_time` is
`timestamp with time zone` on **9,804 of 9,806** per-coin tables. The only two naive columns are in
`ai_signals` and `closed_ai_signals` — neither part of this migration. A
session-TZ-dependent cast in the backfill (`INSERT … SELECT open_time`) was therefore never
possible.

---

## 2. Quantity framework (measured)

PostgreSQL 17.6 · TimescaleDB 2.26.3 · Data directory `C:/PGDATA`

### Storage

| Object | Size |
|---|---|
| Legacy per-coin total (9,683 tables, incl. indexes) | **64 GB** |
| of which candle tables (5,522) | 9,969 MB |
| of which indicator tables (4,161) | **54 GB** |
| Hypertable `candles` (45.0 million rows) | 9,954 MB (heap 4,559 MB / index 5,394 MB) |
| Hypertable `indicators` (18.6 million rows) | 20 GB (heap 18 GB / index 1,889 MB) |
| **Database total** | **98 GB** |
| Drive C: | 263 GB total, **78 GB free** |

**The design doc's 25 GB assumption is too low by a factor of ~2.5** — the legacy footprint is
64 GB, and the lion's share is the indicator tables (54 GB), not the candles. The
design doc's line "25 GB → 4–6 GB" describes a starting size that never existed.

Equally outdated: the risk line "C: has ~160 GB free". In reality it is **78 GB**. The
double footprint (64 GB legacy + 30 GB hyper) is being carried simultaneously today.

### Compression — the real remaining lever

| | |
|---|---|
| Chunks `candles` / `indicators` | 128 / 128 |
| of which compressed | **0 / 0** |
| `compression_enabled` | **false** on both |
| Compression/retention policies | **none** (`timescaledb_information.jobs` empty for both) |

This is consistent with D-2026-CLD-109 (compression deliberately deferred to phase 5) — but it
means the entire expected storage gain remains **unrealised** to date.

**A measured anchor from the same database** instead of an estimate from the design doc:
`oi_5m` is compressed and delivers **652 MB → 78 MB = factor 8.35**. `ticker_10s` is
likewise compressed.

Caution when transferring this: `oi_5m` is a narrow, highly repetitive table.
`indicators` has **108 `double precision` columns** with largely non-repetitive floats
and will compress noticeably worse. A reasonable range, **flagged as an estimate**:
30 GB → **4–10 GB**. Whoever wants the real number should measure it on exactly one chunk (see §4-A).

### Data quality and the R1 contract (measured)

- **527 symbols** were written to in the last 90 minutes.
- Continuity across the 07-16 boundary: `BTCUSDT_1h` in candles **and** indicators
  continuous at 24 rows/day from 07-12 to 07-21 — **no gap** from the switchover.
- `is_closed = false` holds for exactly **527 rows per timeframe** = one forming candle per
  symbol. The R1 storage contract holds.
- Indicator freshness is **exactly closed-only-correct** per timeframe (measurement 19:54 UTC):
  1h → 18:00, 2h → 16:00, 4h → 12:00, 1d → 07-31, 1w → 07-20. No look-ahead, no lag.
- Backfill coverage: 9,669 of 9,683 (symbol, tf, kind) are in the progress file. The 14 missing
  ones are **exclusively `GRVTUSDT`** (new listing after the 07-14 backfill) — its
  legacy tables are **empty** (0 rows), so no history was lost.

---

## 3. Readiness of the two dormant tools (run in-session)

Tested under both interpreters: session Python 3.14.6 / pandas 3.0.3 **and**
fleet Python 3.13.12 / pandas 2.3.2.

| Run | Result |
|---|---|
| `candles_parity.py --self-check` (3.14 and 3.13) | **OK**, exit 0 |
| `candles_parity.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --tf 1h --days 3` against the live DB | **runs**, exit 1, 9 drift findings |
| `candles_backfill.py` dry run (3.14 and 3.13) | **OK**, exit 0, plan spanning 9,683 tables |

No code breakage. `core.time.epoch_seconds` touches neither of the two paths
(`candles_backfill` only imports `utc_now`).

### But: the parity tool can no longer serve its intended purpose

The live run reports `rows old=0` for **every** symbol — the legacy side is empty in every
current window because it has not been written to since 07-16. The 9 drift rows
are artefacts of the phase, not a data problem.

**Consequence: the phase-3 gate ("0 drift over 3 consecutive days") cannot be satisfied
retroactively.** The tool's precondition (fleet reads legacy, writes both) has been moot for 16
days. Run as a nightly cron, it would return exit 1 every night.
No cron entry or parity log could be found — **[not verified]** whether one was ever
set up.

### Operational pitfall in the backfill

`PROGRESS_FILE` is resolved relative to the **checkout**
(`<repo>/control/candles_backfill_progress.json`). Run from this worktree, the backfill reports
"0 already done" and would touch all 9,683 tables again. Because of
`ON CONFLICT DO NOTHING` this is idempotent and safe, but it re-reads the entire
legacy footprint pointlessly. **The backfill must be run exclusively from
`C:\Users\Michael\Documents\Kythera`**, where the 9,669 entries live.

---

## 4. The open operator decisions — with numbers

Two of the three points named in the task brief are **no longer open questions**:

- **Retention:** decided (D-2026-CLD-109 #1, unlimited, no policy). The current state
  confirms it: no retention policy on either hypertable. **No action needed.**
- **Start time:** moot — started on 2026-07-16 16:23 UTC.

Open — and backed by numbers — are these:

### A. Enable compression

- **Deployment:** `ALTER TABLE … SET (timescaledb.compress, segmentby='symbol, tf',
  orderby='open_time DESC')` + `add_compression_policy(…, INTERVAL '14 days')`, as
  envisaged in design doc §1.
- **Expected gain:** 30 GB → 4–10 GB (**estimate**; measured anchor `oi_5m` = 8.35×,
  but `indicators` with 108 float columns is markedly less favourable).
- **Cheapest falsification before the policy:** compress exactly **one** chunk
  (`compress_chunk`) and compare `chunks_detailed_size` before/after. This is
  reversible (`decompress_chunk`) and yields the real number instead of my range.
  The largest `candles` chunk is `_hyper_38_643_chunk` (492 MB, 07-23 to 07-30).
  **This is a write operation → operator decision, not run in this session.**
- **Side effect:** upserts into compressed chunks are slower. With a 14-day grace
  period this only affects backfill-like late writes, not the live stream.

### B. Drop legacy tables (phase 5)

- **Deployment:** 64 GB, that is ~65% of the 98 GB database. The largest single lever,
  bigger than compression.
- **Maturity:** the design doc requires "only 7 days after the cutover". It has been **16 days**.
- **Precondition 1 — pg_dump + restore test.** Design doc gate, non-negotiable.
- **Precondition 2 — otherwise they grow back.** `6_housekeeping.py:67` fires a
  `CREATE TABLE IF NOT EXISTS "{symbol}_{tf}"` across all symbols × timeframes on every
  `coins.json` update. That is exactly where the empty `GRVTUSDT` tables come from. **A drop
  without removing this loop will restore ~4,200 empty tables within one housekeeping cycle.**
- **Precondition 3 — the two readers from §5 must be decided first.**

### C. What happens to the two remaining legacy readers (§5)

### D. Order of dual-write vs. UTC flip

Answered and moot: the candle migration is fully active, the UTC flip
is not (§1). Grounded in the code, there was also never a coupling — `open_time`
is `timestamptz` practically everywhere, the backfill cast was never session-TZ-dependent.

---

## 5. Two bots have been reading frozen tables for 16 days

This is the only real defect found.

Exactly **two** live fleet files still read raw from per-coin tables, bypassing
`core.candles`:

| File | Line | Reads | Channel |
|---|---|---|---|
| `16_smc_forex_metals_bot.py` | 87 | `FROM "{symbol}_{tf}"` (METALS group, `source="database"`) | `CH_SMC_METALS` |
| `21_btc_smc_strategy.py` | 136 | `FROM "{SYMBOL}_{TIMEFRAME}"` (BTCUSDT 1h) | `CH_BTC_SMC` |

Both are in the watchdog roster of the current fleet run and were started on 2026-08-01.
Both are thereby reading candles that end at **2026-07-16 16:00 UTC** — 16 days old.

**This was not an oversight but a deliberate deferral.** `docs/CANDLE_CALL_SITES.md`
lists both in block C with the goal "F — remove caller drop" and lists them in §3 under
"index-coupled — flip only together with offset rework". The deferral, however, silently
assumed that the legacy tables remain authoritative. With the
`WRITE_PRIMARY=hyper` flip on 07-16, that premise no longer holds.

### Blast radius: real, but so far without consequence — verified

- `CH_SMC_METALS`: **9 posts across the entire retained outbox window** (since 2026-04-17),
  the last one on **2026-07-16 09:35 UTC** — i.e. still before the freeze. Nothing since.
- `CH_BTC_SMC`: **zero posts** across the entire window.
- `CH_SMC_FOREX` (same file, but `source="yfinance"`, unaffected by the freeze)
  keeps posting normally: 340 posts, most recently 2026-08-01 00:50 UTC.

**So no signal has arisen from stale data.** That is luck, not design:
both bots emit a **Cornix-parsable block** (`16_…:344-352`), meaning a signal
from prices 16 days old would have been a genuine money incident.

### Why no fix was committed here

The obvious repair — rewiring `fetch_db_data` to `core.candles.read_candles` —
is technically small and would be byte-parity-capable (`include_forming=False` **plus**
removing the caller drop `df.iloc[:-1]` on line 392, otherwise one additional closed candle
would be lost; that is exactly why the offset rework is in the inventory).

It is, however, **not a pure code correction**: both bots have effectively been shut down for
16 days. Reconnecting them to live data means **unparking** a Cornix-posting bot —
and per `docs/OPUS-HANDOFF.md` §6 that is explicitly an
operator decision, not a session decision. The alternative is equally legitimate: deliberately
park both via `control/parked/` and close the lines in the inventory.

**→ Decision for Michi:** repair (signals return on the next restart) **or**
deliberately park. Until then, neither line may be considered "done" and the
legacy tables must not be dropped.

---

## 6. Rollback is no longer trivial — the design promise is broken

`docs/TIMESCALE_R1_MIGRATION.md` §3 says: "Rollback is trivial at every phase: up to phase 4
the fleet reads the old tables; the cutover itself is an env flag + restart back."

That no longer holds, and `core/candles.py:352-355` describes exactly why
(rollback asymmetry of the write primary). The case described there has occurred:

> **`KYTHERA_CANDLES_SOURCE=legacy` is a loaded weapon today.** Flipping back
> "as a rollback" puts the entire fleet on candles that end on 2026-07-16 — silently,
> without error, with full money impact. A rollback of the read side needs a
> backfill of the 16-day gap into the per-coin tables **first**.

The reverse direction is uncritical: the hyper store is written to continuously and never
needs a catch-up.

**Recommendation:** do not leave this state open. Either fill the gap backwards
(makes rollback cheap again, but costs space again on the 64 GB one actually wants to
get rid of) — **or** decide forward, drop the legacy tables (§4-B) and
remove the `legacy` branch from `core/candles.py` along with the flag, so no one can flip
back *anymore*. The second path is the more honest one: it makes visible that there is no way
back, instead of a rollback path that exists only on paper.

---

## 7. Parity plan: what is measured, what is not, and what constitutes a stop

Defined up front, so the drop decision later hinges on numbers, not interpretation.

### What `tools/candles_parity.py` compares today

Per (symbol, tf) over a window of N days, **only closed candles**
(cutoff `core.candles.period_start`, the same clock as the reader API):

1. Row count within the window
2. `max(open_time)`
3. SHA-256 checksum over the OHLCV tuples, ASC, floats canonicalised to
   12 significant digits (so `REAL`-vs-`double` noise does not count as drift)

### What it explicitly does **not** compare

- **Indicators** — not at all. That is 54 GB legacy resp. 20 GB hyper and 108 columns,
  i.e. the larger and more sensitive part of the footprint. It is unchecked.
- The `is_closed` column itself (deliberately: the cutoff is clock-based, not flag-based).
- Anything outside the window — in particular the **historical archive** that the backfill copied.
- Column types, rows beyond OHLCV, symbol/tf assignment.

### Why the original plan no longer applies

Legacy-vs-hyper has been structurally meaningless since the write-primary flip (§3). For the
still-open decision — the **drop** — a different question matters anyway: not "do
both sides agree today", but **"is everything the legacy tables hold contained in the
hyper store, before they disappear"**.

### Proposed replacement gate before the drop

Against the **holdings**, not against the live stream. On a sample of ≥50 symbols
across all timeframes, plus fully for the 8 most liquid ones:

| # | Check | Stop criterion |
|---|---|---|
| G1 | For every (symbol, tf): `count(*)` and `min/max(open_time)` legacy vs. hyper across the **entire** legacy history | **any** row missing from the hyper store that legacy has → stop |
| G2 | OHLCV checksum (existing `checksum_rows` logic) over the full legacy span, not just N days | a checksum mismatch → stop |
| G3 | The same for **indicators** — not covered today, must be added | any deviation → stop |
| G4 | `pg_dump` of the legacy tables + **restore test** into a throwaway DB | restore fails or row counts diverge → stop |
| G5 | §5 decided (bots 16/21 fixed **or** parked) | open → stop |
| G6 | `6_housekeeping.py:67` CREATE loop removed | open → stop (tables would otherwise grow back) |

Tolerance deliberately set at **zero**: the legacy side is frozen and immutable, so there is
no race condition that would justify a tolerance. Every deviation is a
genuine finding. This distinguishes this gate from the original live parity cron, where a
tolerance would have been necessary.

G1–G3 need an extension of `candles_parity.py` (full-span instead of window,
indicator mode). That is the next sensible build work on this task — it only makes
sense after Michi's decision on §4-B/§5 and was therefore not pre-empted here.

---

## 8. What this session deliberately did NOT do

- No flag set, no restart, no cutover, no DDL, no write query.
- No compression enabled and **also no probe chunk compressed** — that would have been a
  write; the number in §4-A therefore honestly remains an estimate.
- Bots 16/21 not fixed (§5, rationale there).
- `tools/candles_parity.py` not extended to the new gate (§7, awaiting decision).

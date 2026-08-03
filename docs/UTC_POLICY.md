# UTC policy (R3)

**As of:** 2026-08-01 · **Tasks:** T-2026-CU-9050-032 (policy), T-2026-KYT-9050-005 (flip) · **Root cause:** R3 (audit) · **Cluster:** AUDIT_TODO P2.1–P2.6, P2.21

Kythera is meant to have exactly one time domain: **UTC**. This file says what of that already applies, what doesn't yet apply, and in what order the rest has to come.

---

## 1. What applies now

| Level | Mechanism | File | Status |
|---|---|---|---|
| Python | `utc_now()` / `utc_now_naive()` / `to_utc()` / `as_naive_utc()` / `from_unix_ts()` | `core/time.py` | **active** |
| Lint | `ruff` rule group `DTZ` forbids naive `datetime.now()` / `utcnow()` / `fromtimestamp(ts)` | `pyproject.toml` | **active** |
| Postgres | every pool session with `-c timezone=UTC` | `core/database.py` | **in the repo (T-2026-KYT-9050-005)**, live from the next fleet restart |
| History | exactly one constant decides how old rows are read: `core.time.R3_CUTOVER_UTC` | `core/time.py` | **decision open, §6** |

New code can no longer introduce naive local time without CI turning red. Existing deliberate exceptions carry a `# noqa: DTZ…` with a rationale — that is the visible residual debt, not a free pass:

- `3_detectors.py` — **done with the flip** (P2.3): writes `utc_now_naive()` into `active_trades_master.time/posted`, the `noqa` is gone.
- `30_ai_pex1_bot.py` — watermark sentinel against `pump_dump_events.spike_time`. Careful, the rationale was wrong: the column is live `timestamp WITH time zone` (measured read-only 2026-08-01), not naive. See §3 and T-2026-KYT-9050-061.

The ruff excludes (`backtest/`, `tools/`, `strategies/`, `handlers/`, `trainers_x/`, `legacy_trainers/`) are not covered by DTZ.

## 2. Why the session TZ is the core of the problem

Part of the live tables are `TIMESTAMP WITHOUT TIME ZONE`. Postgres casts between `timestamptz` and these naive columns using the **session TZ**. That means what `NOW()` writes into a naive column, and how a naive column is compared against `NOW()`, both hang off the VPS's OS TZ.

**The offset is +2/+3h.** The VPS TZ is `Europe/Bucharest` (EET/EEST), measured on 2026-07-05 (`tools/research_dataset_common.py:34`). The AUDIT_TODO entries P2.1–P2.6 talk about "CEST" and "1–2h" — that's the order of magnitude, not the exact figure.

**Not every naive column carries local time.** The domain difference hangs on the writer, not the column type:

- A **naive** Python parameter passes through uncast — `26_regime_detector.py:216` writes `datetime.now(timezone.utc).replace(tzinfo=None)`, i.e. naive-**UTC**. The entire `regime_*` cluster is already correct today and needs **no** compensation. The flip does not touch it.
- An **aware** parameter or `NOW()` gets cast with the session TZ when written into a naive column and thereby lands as **local time** (`5_trade_monitor.posted`, `ml_predictions_master.time`, `pump_dump_events.spike_time`).
- `3_detectors.py` writes naive **local time** directly (P2.3).

Exactly the second and third group are already compensated for explicitly by the existing code (§5). An isolated fix would make these compensations wrong. That is what the audit's individual fixes failed on.

## 3. Column inventory

The target state is `timestamptz` everywhere. **The as-is column was measured read-only against the live DB on 2026-08-01** (`information_schema.columns`) — the domain of the naive columns was additionally falsified empirically: Europe/Bucharest springs forward at 03:00 on 2026-03-29, the local wall clock 03:00–03:59 **does not exist on that day**. A naive column with rows in that window cannot carry local time.

| Table | Columns | As-is (live 2026-08-01) | Rows in the non-existent local hour | Bootstrap DDL |
|---|---|---|---|---|
| `active_trades_master` | `time`, `posted` | naive | 0 (reference hour also 0 — the table is a rolling window) | `3_detectors.py` |
| `closed_trades_master` | `time`, `posted` | naive | **0** vs. 97/39 in the reference hour → local time confirmed | `5_trade_monitor.py` |
| `closed_trades3` (SRA2 retrain source) | `time`, `posted` | naive, dead since 2026-02-23 | — (data ends before the DST change) | legacy |
| `trade_cooldowns` | `last_posted_at` | **live `timestamptz`**, repo DDLs inconsistent (P2.2) | — | `26`, `11`, `24`, `25` |
| `regime_history` | `ts` | naive | **12** vs. 12 in the reference hour → **naive-UTC confirmed, no compensation** | `26_regime_detector.py` |
| `regime_current` | `since`, `alt_context_since`, `last_raw_ts` | naive (UTC writer) | — | `26_regime_detector.py` |
| `bot_regime_performance` | `last_computed` | naive (UTC writer) | — | `26_regime_detector.py` |
| `bot_regime_whitelist` | `computed_at` | naive (UTC writer) | — | `26_regime_detector.py` |
| `orchestrator_open_trades` | `opened_at`, `closed_at` | naive (UTC writer) | 0 (table starts 2026-04-18) | `26_regime_detector.py` |
| `orchestrator_suppressed_signals` | `ts` | naive (UTC writer) | 0 (ditto) | `26_regime_detector.py` |
| `pump_dump_events` | `spike_time` | **live `timestamptz`** — the repo DDL (`10_pump_dump_detector.py:1409`) says `TIMESTAMP`, the live table was altered at some point. That's exactly what kills bot 30 (T-2026-KYT-9050-061) | — | `10_pump_dump_detector.py` |
| `ml_predictions_master` | `time`, `created_at` | naive, **no repo DDL** | 0 vs. 170 in the reference hour → local time confirmed | — (gap, R2/B3) |
| `master_ai_processed_signals` | `processed_at` | **live `timestamptz`** | — | `15_ai_master_bot.py` |
| `ai_signals` | `open_time` | **mixed domain** — verified live 2026-07-10 (T-044): the column is `timestamp without time zone DEFAULT now()`, i.e. every writer that leaves `open_time` to the default stamps session-local (Bucharest). Exception since T-052: ROM1 rows (`28_signal_orchestrator.insert_rom1_signal`) explicitly write naive-UTC, so the lifecycle sync can match against the naive-UTC `opened_at`. Unification = the R3 flip (§4). After the flip, the `DEFAULT now()` cast stamps UTC — the column becomes single-domain without any writer needing to be touched. 2026-08-01: 3.196 rows, of which 247 ROM1 | 0 (ROM1 only starts 2026-05-27, the DST test doesn't apply here) | `28` (UTC), all other AI bots (default = local) |
| `closed_ai_signals` | `open_time`, `close_time` | **both naive** (measured live 2026-08-01) — the earlier line "`close_time` already `timestamptz`" was **wrong**; `8_ai_trade_monitor.py:27` is a `CREATE TABLE IF NOT EXISTS` DDL and never widened the existing column (the same trap as P2.2). Mixed writers = P2.4 | | `8_ai_trade_monitor.py:27` |
| `{sym}_{tf}`, `ticker_10s` | `open_time`, `ts` | already `timestamptz` | | — |

## 4. The flip — what it touches (T-2026-KYT-9050-005, in the repo)

`-c timezone=UTC` in the pool is **not a one-liner**. It shifts the domain of every naive column that accepts an aware-UTC value or `NOW()` in one stroke, and therefore had to land together with every dependent spot in ONE changeset. Components:

1. ✅ `core/database.py` — `_connect_options()` carries `-c timezone=UTC` (`_DEFAULT_SESSION_TZ`).
2. ✅ `3_detectors.py` — `write_signal_atomic` stamps `utc_now_naive()` into **both** columns (`time` and `posted`; it was always one `datetime.now()` call for both values, P2.3). **Mandatory**: without this fix the flip tips `33_ai_fif1_bot.fifo_burst_counts` from correct into drift, while it repairs `5_trade_monitor` (P2.6) and `core/market_utils.update_cooldown` (P2.5).
3. ✅ Removed the compensations from §5 — replaced by one constant (§6).
4. ✅ Docstrings pulled along: the module docstring and `to_utc_naive()` in `15_ai_master_bot.py`, `fetch_recent_signals()` and `fifo_burst_counts()` in `33_ai_fif1_bot.py`, the headers of the four dataset builders and `tools/retrain_sra2.py`.
5. ⏳ What happens to the **history** — an operator decision, §6.

**The flip only takes effect on a fleet restart**, process by process: a bot picks up the new pool option on start. Until then the fleet keeps running unchanged.

Restart effect: rows from before the restart carry local time and are read as UTC from then on — they appear +2/+3h in the future. The affected windows are the short ones (60-min trade monitor, 1h/24h FIF1 burst density, 5-day AIM2 signal stream); the effect runs out with the longest window. FIF1 posts nothing from this: the startup marking in `33_ai_fif1_bot.main()` checks off everything that falls in the window on the first poll — and the apparently-future rows are all inside the window on the first poll (the window has no upper bound).

**Refuted (2026-08-01):** the earlier sentence "`30_ai_pex1_bot.detect_spike_time_offset_h` self-heals after the flip, no intervention needed" was wrong. The function subtracts a naive `now` from `MAX(spike_time)`, and the column is live `timestamptz` — it has thrown `can't subtract offset-naive and offset-aware datetimes` on **every** scan since at least 2026-07-19. Bot 30 has 8.166 failures and not a single successful scan across the four most recent `logs/watchdog_debug_*`. Fixed in T-2026-KYT-9050-061 (a separate commit in the same PR).

## 5. The compensations — the actual reason for the scope

Six spots explicitly computed the drift back out. They were **correct** and would have become **wrong** through the change.

To be precise: the pool option **alone** doesn't touch them — they compare naive parameters against naive columns, and that's session-independent. They become wrong the moment the **writers** write UTC (P2.3 and the aware cast under a UTC session). Since the flip and the writer fix necessarily land together (§4.2), that's the same change.

| Spot | What it did | Now |
|---|---|---|
| `15_ai_master_bot.to_utc_naive()` + `load_signal_stream.since_local` | AIM2 signal stream: `ml_predictions_master.time` and `*_trades_master.time` from Bucharest to UTC | delegates to `core.time.legacy_naive_to_utc`; the SQL bound to `utc_to_legacy_naive` (`since_bound`) |
| `tools/research_dataset_common.py` — `LOCAL_TZ` + `to_utc_naive()` | the shared basis of all research datasets | delegates; `LOCAL_TZ` is now only a re-export of `core.time.LEGACY_WRITER_TZ` |
| `tools/aim2_build_dataset.to_utc_naive()` | AIM2 training dataset | delegates to the shared helper |
| `tools/fif1_build_dataset.py` (imports `to_utc_naive`) | FIF1 training dataset | inherits the delegation |
| `tools/pex1_build_dataset.py` (imports `LOCAL_TZ`) | PEX1 training dataset | **deliberately stays localizing** — see below |
| `tools/retrain_sra2.py` (localizes `closed_trades3` times) | SRA2 retrain | delegates; `closed_trades3` is pure pre-flip history |

**The exception, and why it isn't one.** `pex1_build_dataset.spike_time_to_utc` still localizes — but only if `detect_offset_h` has **measured the offset from the data** (2/3h). That's not an assumption the flip breaks; it's a measurement that comes out at 0 after the flip and therefore no longer even enters that branch; for the live table it's dead anyway, because `spike_time` is `timestamptz` and the aware branch catches it first. Deleting it would only have broken reading old dumps. The DST recipe still sits centrally though: the call is `legacy_naive_to_utc(s, assume_legacy=True)` — the only sanctioned `assume_legacy` call in the repo.

The trainers are the hard part: they read **history**. After the flip, every naive column contains both domains — local time before the restart, UTC after. Neither "always compensate" nor "never compensate" is then correct. A trainer that ignores this produces train/serve skew — exactly the failure mode AIM2 was built against (P0.13). That's why the reading now hangs on **one** constant instead of six copies of the same assumption.

## 6. History: backfill or cutover — open operator decision

The code is built so that **both paths stay open** and neither of them needs any further code change. There is exactly one switch:

```python
core.time.R3_CUTOVER_UTC   # None (Repo-Default) | Instant des Restarts
KYTHERA_R3_CUTOVER_UTC     # gleiche Semantik, pro Prozess, ISO-8601 UTC
```

- `None` ⇒ **uniform-utc**: every naive column carries UTC across its whole history. That's the world after a backfill.
- set ⇒ **cutover**: rows whose stored wall clock lies before the instant are read as `Europe/Bucharest`, the rest as UTC.

Every reader in the fleet goes through `legacy_naive_to_utc` / `utc_to_legacy_naive`; the reading is logged on start (`R3-Zeitdomäne: …`), so a wrong assumption doesn't stay silent.

### What a backfill would have to touch (measured read-only, 2026-08-01)

| Table | Columns | Rows | Size | Span |
|---|---|---|---|---|
| `ml_predictions_master` | `time`, `created_at` | 1.131.684 | 167,3 MiB | 2026-02-24 → now |
| `closed_ai_signals` | `open_time`, `close_time` | 476.535 | 84,5 MiB | 2026-02-24 → now |
| `closed_trades_master` | `time`, `posted` | 382.918 | 96,2 MiB | 2025-08-23 → now |
| `closed_trades3` (SRA2 retrain) | `time`, `posted` | 8.245 | 1,2 MiB | 2025-09-06 → 2026-02-23 |
| `ai_signals` | `open_time` | 3.196 | 70,2 MiB | 2026-02-25 → now |
| `active_trades_master` | `time`, `posted` | 539 | 1,2 MiB | 2026-02-24 → now |
| **Total** | | **≈ 2,00 Mio rows** | **≈ 420 MiB** | |

**Don't touch** (empirically confirmed, §3): the entire `regime_*`/`orchestrator_*` cluster already carries naive-UTC — `regime_history` has 12 rows in the locally non-existent hour. Also out: everything that's already `timestamptz` (`pump_dump_events`, `trade_cooldowns`, `master_ai_processed_signals`, candles, `ticker_10s`).

### Costs and risks, side by side

| | **Backfill** | **Cutover constant** |
|---|---|---|
| Live write on money tables | yes, ~2,00 Mio rows | no |
| Effort | one maintenance window, fleet stopped, backup mandatory | set one line |
| Runtime | **Estimate, not measured**: the four big tables read warm in ~4s total seq-scan; a full UPDATE writes new tuples + WAL + index entries into the 17 B-trees of these tables (8 of them sit on exactly these time columns, so HOT update doesn't apply). Order of magnitude **minutes** (roughly 5–20), then autovacuum. A real write benchmark wasn't permitted from this session (hard rule 1). | 0 |
| Space required | ~+420 MiB bloat until vacuum | 0 |
| Permanent complexity | **zero** — the cutover stays `None`, `LEGACY_WRITER_TZ` becomes dead code | one constant + one branch in `core.time`. The earlier objection "every trainer permanently carries a branch" **no longer applies**: the branch sits centrally once, the trainers never see it. |
| Residual fuzziness | the ambiguous autumn hour: **113 values** (54 `closed_trades_master.time`, 59 `.posted`, 1 `closed_trades3.time`) can't be unambiguously converted back — ±1h | the same 113 values (Series → NaT, i.e. the trainer discards them) **plus** a ≤3h band around the cutover: rows written locally in the last 2–3h before the restart carry a wall clock beyond the cutover and are read as UTC |
| Statistics across the boundary | no boundary, no discontinuity | every reader that does **not** go through `core.time` (ad-hoc SQL, dashboards, the studies from §8) sees a 2–3h jump on the restart day; daily aggregates for exactly that day are shifted accordingly |

### What else to know

**The order determines the effort of the backfill.** If it runs in the same window as the restart and before the new fleet starts, it is unconditional (`UPDATE … SET c = c AT TIME ZONE 'Europe/Bucharest' AT TIME ZONE 'UTC'`, every row is legacy). If it runs later, it needs a mandatory lower bound `WHERE c < '<Restart-Instant>'` — otherwise it converts the new UTC rows a second time. **Recommendation independent of the decision: log the restart instant.** It's the prerequisite for both a later backfill and the value of the cutover constant; without it, only the expensive option remains (guessing the domain per row).

**Until the decision is made, `uniform-utc` applies.** For the running fleet that's correct within hours to days (the windows are 1h to 5d). For a **retrain on pre-flip history** it is wrong — without a cutover constant, the whole history there is shifted by 2–3h. Consequence: **no retrain on legacy columns until §6 is decided**; the builders log their reading into the first line of their output, so a run under the wrong assumption is visible in the log.

`ALTER TABLE`/DDL and the backfill itself are **not** part of this changeset (release scope T-2026-KYT-9050-005: explicitly excluded).

## 7. DDL change to `timestamptz`

Reference DDL: [`migrations/2026-07-r3-timestamptz.sql`](migrations/2026-07-r3-timestamptz.sql). **Not executed**, no runner.

Three conditions before execution:

1. **Operator sign-off (C-gate).** `ALTER TABLE` on live tables is an escalation.
2. **The flip from §4 must be in place first.** Otherwise you'd alter local time into wrong UTC.
3. **Pull the bootstrap DDLs along in the same PR.** `CREATE TABLE IF NOT EXISTS` never widens an existing column — anyone who only alters the live table produces exactly the repo-vs-live drift that got us P2.2 (five days of silent signals).

## 8. Remaining backlog

- **P2.1** (`strategies/strat_fast_in_out.py`, `strat_5_percent.py`): a Python-side comparison of naive local time against UTC `posted`. **Not** healed by the session TZ; `strategies/` is ruff-excluded, DTZ doesn't apply there.
- **P2.3** (`3_detectors.py`), **P2.5** (`core/market_utils.update_cooldown`), **P2.6** (`5_trade_monitor.posted`): handled by the flip from §4 — effective from the restart.
- **P2.4** (`closed_ai_signals.open_time`/`close_time`, three writers), **P2.21** (cooldown/outbox window in `28_signal_orchestrator.py`): a mechanical follow-up onto `core/time.py`. The flip makes `NOW()` UTC in these paths, but the column's mixed **history** remains (§6).
- **Readers outside the fleet, deliberately not pulled along** (analysis tools, neither a live nor a training path; they localize legacy columns themselves and thereby read rows off by 2–3h after the restart). Follow-up = the same one-line delegation to `core.time`:
  - `tools/funding_risk_study.py:130` `to_utc_aware()` (`closed_ai_signals.open_time`)
  - `tools/breadth_study.py:428` — **a standalone bug, independent of the flip**: localizes `regime_history.ts` as Bucharest, even though the column is naive-**UTC** (writer `26_regime_detector.py:216`; empirically §3: 12 rows in the locally non-existent hour). The BTC-regime-features as-of join is therefore already off by 2–3h **today**. Not fixed here — a correction changes the study result and needs a re-run, i.e. its own task.
  - `tools/settlement_timing_study.py` (`closed_ai_signals.open_time`)
  - `tools/analytics_export.py` + `tools/dashboard/app.py` — carry naive values through **verbatim** and never differentiate across the domain boundary; their header note "wall clock Europe/Bucharest" only applies to the legacy rows from the restart on.
- **The aware bypass.** `DTZ` only flags *naive* calls. `datetime.now(timezone.utc)` remains allowed, and the existing code has ~79 call sites of it across 34 files (e.g. `26_regime_detector.py:216`, `core/signal_post.py`, `5_trade_monitor.py`). These are all **correct** — just not routed through `core/time.py`. `utc_now()` is thereby the *sanctioned*, not the *only actual*, time source. The follow-up is grunt work with no behaviour change and belongs in the same follow-on task as the flip; there's no lint gate for it (ruff has no rule "use my helper").

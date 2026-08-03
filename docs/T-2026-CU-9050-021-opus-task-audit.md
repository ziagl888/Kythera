# Opus task audit Kythera — ranked backlog with reasoning

> **⚠ ID-range migration (2026-07-21):** The tasks ranked in this document still carry the old `T-2026-CU-9050-NNN` IDs (a dated snapshot). The ID range has since moved to `T-2026-KYT-9050-NNN` (slug `kythera`) — see CLAUDE.md §Workflow. The 15 most recently open tasks were migrated (018→002, 020→003, 035→004, 041→005, 061→006, 069→007, 071→008, 097→009, 106→010, 113→011, 117→012, 147→013, 178→014, 184→015, 185→016; old docs tombstoned + pointer added), the rest are done/wontfix. When prioritizing, take the **KB as the single source of truth** (`list_kb_docs customer=kythera`), not the IDs here — this ranking remains as a historical reasoning reference.

**As of:** 2026-07-11 (orchestration wave T-2026-CU-9050-075 caught up — T-2026-CU-9050-094; before that ledger verification T-028 from 2026-07-09, based on the Fable-5 extraction from 2026-07-07, T-021). Sources: `AUDIT_TODO.md` (freshly maintained, single source of truth), KB tasks project 9050, `audit_reports/`, CHANGELOG.
**Working rule:** in ranking order; read the KB task doc first for each task (the KB can have moved on further than this document); cycle per `docs/OPUS-HANDOFF.md` §2. Watch the environment column — **BUILD** = the build machine is enough, **VPS** = needs the live DB/VPS session. **Run `git fetch` before prioritizing** — several sessions work on the same repo (see trap 15 in the handoff).

Overarching ordering logic (distilled from the audit): **root causes before point fixes** (R1–R4 generate ~60% of the findings) · **monitor correctness before model retraining** · **measure Z0 before perf fixes** · **Z2 (tunnel) before Z1 (dashboard rewrite)**.

---

## Delivered since the extraction (2026-07-08/09)

This work is merged and was not yet visible in the 07-07 version of this document:

| Task | Date | Result | PR |
|---|---|---|---|
| T-2026-CU-9050-022 | 07-08 | Docs polish, new `docs/ARCHITECTURE.md` + README linking | #11 |
| — | 07-08 | `tools/track_shadow_model.py` — read-only shadow performance tracker for model tags | #12 |
| T-2026-CU-9050-023 | 07-09 | Market tracker per-bot WR correctness: dedup on `closed_ai_signals`, `close_price>0` guard, direction-case normalization, compact A–Z model post | #13 |
| T-2026-CU-9050-024 | 07-09 | **Volume Indicator had been signal-dead since 04.07** — `module_tag 'Volume Indicator'` blows `trade_cooldowns.module varchar(10)`, the error was logged and swallowed. Atomic cooldown writes + length guard | #14 |
| T-2026-CU-9050-025 | 07-09 | Market tracker dedup key v2 (follow-up to 023): `closed_ai_signals` 439.325 raw → natural key collapses only to 360.682 → Report-14 key `(symbol, model, direction, open_time)` shows 81.842 real trades. `DISTINCT ON` on the R14 key, survivor = earliest `close_time` | #15 |
| T-2026-CU-9050-026 | 07-09 | SMC Sniper `send_cornix_signal` ignored the artifact `model_id` — BB2/TD2 trades posted under `BB_4H`/`TD_4H`. Violation of hard rule 6. Generation-aware orchestrator patterns | #16 |
| T-2026-CU-9050-028 | 07-09 | This document + `AUDIT_TODO.md` + `docs/OPUS-HANDOFF.md` verified against the code; T-016 corrected to `done`; A2 remainder evidenced instead of estimated; three new findings (P1.45, P2.51, P3.13) | — |

**Two new error classes from 024/026 — both swept fleet-wide (T-028):**
- *Silent signal death via column overflow:* no second active instance. All 18 `trade_cooldowns.module` writers resolved, longest tag 9 characters, no truncation collision. Residual risk noted as **P3.13**.
- *Post path ignores the artifact `model_id`:* no second **actively mis-firing** instance, but **three latent** ones — `11_ai_mis`, `13_ai_rub`, `24_quasimodo` discard an available `model_id` and post under a source-code constant. Noted as **P1.45**, **blocks MIS3/RUB3/QM2** (directly affects B7 and C2).

## Delivered since the extraction (2026-07-10 — interim batch)

Between the 07-09 version and the orchestration wave, a larger 07-10 batch ran (task details in the KB tasks + `AUDIT_TODO.md` annotations). Kept short, because AUDIT_TODO carries the detailed evidence:

- **A2 fully closed** (P1.37 watchdog backoff, P1.39 pump/dump timestamp, P1.41 shadow cooldown, P1.43 tracker pool leak, P1.44 opened double-counting) — T-2026-CU-9050-029, PRs #18/#23. P1.11 was already fixed anyway.
- **P1.26** SMC-16 FVG dead code (one-liner + guard test) — T-033.
- **P1.45 side finding EPD2/SRA2** wired up (the live bots `10_pump_dump`/`9_ai_sr` now read tag+threshold from the meta) — T-042 (PR #39) + the remaining feature-parity work.
- **New error class "AI bot without an active-trade check" closed fleet-wide** (P1.47 ATB1 `posted` flag T-062, P1.48 RUB T-043, P1.49 EPD+SRA + funding cache T-055) — every posting AI bot now has the position guard.
- **P1.46** Sniper forming pivots (T-036) + **P1.13** warm-up `fillna`→NaN (T-045/054) and the recompute tooling step (T-061) — the live recompute + TD2/BB2/QM2 retrain remains operator-gated.

## Orchestration wave 2026-07-11 (T-2026-CU-9050-075 dispatch, PRs #66–#80)

The orchestrator T-075 dispatched day-waves 1–6 as file-disjoint BUILD tasks T-076..T-093. Cross-checked against `AUDIT_TODO.md` — **the checkbox state is authoritative, not the dispatch list**:

| Task | Result | Findings |
|---|---|---|
| T-076 | Regression guard: manifest-present-but-goldens-missing → exit 1; cooldown-tag test extended around the MIS horizons | P2.51, P3.13 |
| T-077 | DB pool `statement_timeout`(300s) + TCP keepalives + watchdog log heartbeat (`check_heartbeat`, auto-restart default OFF) | P2.47 |
| T-079 | `core/coins.py` — one atomic `coins.json` writer + Binance USDT perp shape guard on delisted cleanup + empty-universe guard | P2.16, P2.17 |
| T-080 | Cornix double-parse check 24/25/29 — **verified no-op** (`parse_cornix_signal` → `None` per bot; 06.07 fix present+correct) | P3.9 |
| T-081 | Market tracker `_hard_split_block` chunker (≤4096); full-history load + synchronous "async" jobs documented as a known risk | P2.41 (PR #69) |
| T-082 | Orchestrator `run_startup_reconciliation` + write-side `cleanup_stale_whitelist_rows` (raw name + 14d) | P2.24, P2.25 |
| T-083 | Monitors/`post_ai_signal` store exactly the published targets (3/5), Cornix block byte-identical | P2.31 |
| T-084 | Window-global indicators (27 columns) only on the newest closed (GESCHLOSSENE) candle (variant B), forming+older NULL; 4 S/R readers switched to `first_valid_index` | P1.12 |
| T-085 | Detector cycle: one batch `ticker/price` call instead of ~530 serial + volume-spike/HVN reclassification | P2.44, P2.42 |
| T-086 | ATB1 unknown state = observe-only + main-loop hardening (takes effect on unparking) | P2.36, P2.37 |
| T-087 | Watchdog `CTRL_BREAK_EVENT` (graceful) + `atomic_write_json` Windows fix (unique tmp/retry) | P2.48, P2.49 |
| T-088 | `21_btc_smc` cooldown/dedupe (`BTCSMC_1H`, 12h) + funding "extreme" threshold 75→95/85 | P2.46, P2.40 |
| T-089 | SMC/Mayank/Sniper: weekend/stale-candle gate, FVG age limit (50 bars), SL/RR guard, break-and-retest picks a real level | P2.45, P2.39 |
| T-090 | AIM2 candidate window 30→60min + table-agnostic `conv_signal` identity dedup (shadow-only) | P2.35 |
| T-091 | `core/fleet.py` = one process definition; watchdog + dashboard import it (bots 26–34 now visible) | R2(a), P1.38-tlw. |
| T-092 | Data-pipeline robustness: `find_contaminating_gap` continuity check, periodic coins.json refresh without a restart, `_consume_with_watchdog` (`chart_data_service`) | P2.13, P2.15, P2.20 |
| T-093 | Sniper edge pivots — `argrelextrema` residual repaint at the right edge (option B, ≥5 confirming closed bars) | P1.46-Rest |

**Still open / special cases:**
- **T-078** (P1.12 first attempt) → **wontfix**, superseded by T-084 (variant B).
- **Only partial:** **R2** (R2(a) process list done, R2(b) `schema.sql`/DDL needs VPS/DB) and **P1.38** (process-list drift removed via `core/fleet.py`; the CSRF origin check, log-streaming handle and the `/api/status` psutil sweeps remain open).
- **New bots/prep (not checkbox-closing):** MAX1 (`34_ai_max1_bot.py`, RUB2-SHORT clone, shadow-only, `MAX1_LIVE_POSTING`=OFF, T-067/070) and the QM2 retrain preparation (`qm_ml_trainer.py` now writes `model_id`, T-061).

---

## Tier A — directly actionable (no Michi decision needed)

### ~~A1 · Ledger and KB hygiene~~ — **DONE 2026-07-09 (T-2026-CU-9050-028)**
Five contradictory checkboxes verified against the code instead of flipped. Result: P1.5/P1.11/P1.18/P2.50 flipped; **P1.26 stayed open as of 07-09 — the ✘ annotation was itself wrong**, the FVG entry is real dead code (the mitigation scan and the trigger use the same predicate on the same candle); the 83 "proving" cooldown rows come from an older, TF-prefixing code version whose key the current code never writes (fixed since, 2026-07-10 T-033). P2.2 remains open (the TZ dimension solved, the column width not — a live `ALTER` is an operator decision). T-2026-CU-9050-016 corrected to `done` with a scoped remainder. New: P1.45, P2.51, P3.13.

### ~~A2 · P1 correctness batch monitors/tracker~~ — **DONE 2026-07-10 (T-2026-CU-9050-029, PRs #18/#23)**
All five open items closed: P1.43 (tracker pool leak `try/finally` + `rollback`), P1.44 (opened double-counting: opens only from `ai_signals`∪`closed_ai_signals`, `posted=TRUE` JOIN), P1.41 (shadow cooldown via `log_prediction`), P1.39 (pump/dump timestamp on the bucket helper + `_find_bucket_nearest` follow-up T-035), P1.37 (watchdog `not_before` instead of `time.sleep`). P1.11 was already fixed anyway. Each item has a DB-free `backtest/` guard. That makes the gating data basis (per-bot statistics) correct.

### ✅ A2b · Wire the artifact `model_id` into the post paths for P1.45 (BUILD, ~2-3h) — **done 2026-07-09, T-2026-CU-9050-030 / PR #24**
**Was:** `11_ai_mis`, `13_ai_rub` and `24_quasimodo` discarded the available `model_id` and posted under a source-code constant (details in the ledger, P1.45) — the same class that PR #16 fixed in the Sniper.

**Done:** MIS pulls the generation per horizon from `meta.model_id`, RUB direction-dependent (SHORT from the meta, LONG keeps the named constant `RUB_LONG_TAG` for its legacy model), QM preemptively including `module_tag` as a mandatory keyword in `send_cornix_signal`. Plus the transitional dedup, because the tag is also the dedupe key (MIS/QM: `model IN (neu, legacy)`; RUB has no active-trade check → cooldown against both tags). Three mutation-tested guard tests. No live-semantics change — the tags today remain MIS2-\*, RUB2, QM_1H. **B7 and C2 are thereby unblocked.** The EPD2/SRA2 side finding has since also been done (2026-07-10, T-042/PR #39 wires tag+threshold from the meta, T-055 adds the active-trade check + funding cache) — the EPD2/SRA2 rollout remains an operator decision (C-gate).

### ~~A3 · P2 robustness cluster ingestion/housekeeping~~ — **DONE (07-09 to 07-11)**
P2.14/P2.18 (retry bound + 429/418 backoff, `core/http_retry.py`, T-2026-CU-9050 07-09 batch), P2.16/P2.17 (`core/coins.py` one atomic writer + perp shape guard, T-079), P2.36/P2.37 (ATB1 unknown state observe-only + main-loop hardening, T-086), P2.49 (`atomic_write_json` Windows fix, T-087). All fixes take effect on the next regular restart, no live deploy.

### A4 · T-2026-CU-9050-020 HMM regime study (VPS for replay data, ~1 day, no live intervention)
**Why:** all ABR1-LONG failures had the same failure mode (regime non-stationarity). The study is cheap to falsify, Batch-E discipline: walk-forward decides.
**Steps fixed in the KB task** (3–4-state Gaussian HMM on BTC-4h; A/B against the 26_regime_detector heuristic AND ROM1 gating; replay data `_X/staging_models/replay/`). **Pre-decision:** classic Markov chains on price states deliberately NOT used (fees eat micro-edges). Result is a report + recommendation, no live code.

### A5 · P3 hygiene batch (BUILD, low, gap filler) — **mostly open**
Already closed: **P3.13** (cooldown-tag length net extended around the MIS horizons, T-076) and the `db_schema_analysis.py` part of **P3.1** (deleted, T-039). Still open: the rest of P3.1 (dead code/`load_coins` dup), P3.2 (log rotation), P3.3-P3.6, P3.7 (coin-level exceptions to ERROR+exc_info like bot 29), P3.8 (matplotlib `Agg` in 17/24/25), P3.10 (spec-drift docs), P3.11, P3.12 (`REAL`→`double`, DB). Only with touch context or as an explicit batch — don't mix in as a drive-by in money-path PRs.

## Tier B — fixed Fable pre-decision (execute; if reality deviates → escalate instead of improvising)

### B1 · R3 central UTC policy (BUILD, ~1 day, before the P2 TZ cluster)
**Pre-decision:** `core/time.py` with `utc_now()`; `-c timezone=UTC` in the pool (`core/database.py`); money time columns → `timestamptz` (prepare the DDL change repo-side only, a live `ALTER TABLE` is an escalation → C-gate); enable ruff `DTZ` rules. Only after that mechanically follow up with P2.1/P2.3–P2.6/P2.21.
**Why this order:** individual TZ fixes without a central policy create new drift — that is the documented audit lesson. Step-2 finding: the timestamptz variant won live (P2.2).

### ~~B2 · Roll out R4 `cap_leverage_to_sl()` to the remaining signal bots~~ — **STRUCK (operator decision Michi 2026-07-14)**
The entire fleet trades cross-margin (every signal poster writes `🚨 Margin: Cross`, emitter sweep 2026-07-14). That means the isolated `1/lev` liquidation assumption behind `cap_leverage_to_sl` does not apply fleet-wide — the same reasoning as the ROM1/MIS2 exclusion (T-101). No rollout; R4 closed in the ledger. The existing caps in 21/29 stay untouched, the 15% SL-distance cap (P2.27) remains the only relevant leverage protection. Details: `AUDIT_TODO.md` R4.

### B3 · R2 fleet/schema single source (BUILD) — **R2(a) done, R2(b) open**
**R2(a) done 2026-07-11 (T-2026-CU-9050-091):** `core/fleet.py` is the single process definition (name/script/group/delays); `main_watchdog.py` + `dashboard.py` import it — drift closed, the dashboard now automatically shows the full fleet incl. bots 26–34, pinned down by `backtest/test_fleet_definition.py`, no behaviour change on the watchdog. Also done: the process-list part of **P1.38** (CSRF/log-streaming/`/api/status` perf remain open).
**R2(b) open (VPS/DB):** `docs/schema.sql` as a DDL reference including the tables that so far have no DDL (`ai_signals`, `ml_predictions_master`); `trade_cooldowns` width drift (`varchar(10)` live, see P2.2). Migration runner design-side only — R2(b) does not touch the live schema.

### B4 · P0.8/Z2 dashboard hardening: Cloudflare tunnel before the dashboard rewrite (VPS, ~0.5-1 day)
**Pre-decision:** the order Z2 before Z1 is decided. Step 1 is immediately doable: bind `dashboard.py` to `127.0.0.1` + `cloudflared` as a Windows service (outbound-only) + Cloudflare Access. That defuses P0.8 (unauthenticated `stop_all` on 0.0.0.0:5000) without waiting for the rewrite. Schedule the deployment moment (dashboard restart) with Michi — the bind change itself is the only live touch.

### ~~B5 · T-2026-CU-9050-010 arm the regression guard~~ — **ALREADY ARMED** (correction 2026-07-09, T-028)
The guard **is armed**: 24 goldens + 24 fixtures + `manifest.json` have been git-tracked since commit `4765e25`, `verify` runs as a pre-commit hook on every commit (`.pre-commit-config.yaml:43`) and reports "OK - 24 fixture(s) match the golden snapshot". This document and `OPUS-HANDOFF.md` §3 claimed the opposite until today — anyone prioritizing off that would have repeated work that was already done. **P2.51 done 2026-07-11 (T-2026-CU-9050-076):** `mode_verify` now checks `os.path.exists(MANIFEST_PATH)` in the empty-goldens branch → manifest-present-but-goldens-missing gives **exit 1** instead of silently passing; manifest-absent remains the legitimate pre-live-DB-freeze pass (`backtest/test_regression_guard_disarm.py`). That makes B5 fully closed. The task's warnings about golden decay and tolerances still apply to future refreshes.

### B6 · T-2026-CU-9050-011 VPS port + Claude Code on the VPS (VPS/ops, ~2.5h, **blocker for A4 replay, B7**)
**Steps are in the KB task.** Hard constraints: don't destabilize the live bot env (no pyarrow — the guard deliberately uses np.savez); never commit `.env` with real creds; the watchdog remains the sole process owner. Open detail question in the task (a dedicated checkout/worktree isolation on the live host) → clarify with Michi = the only C-portion.

### B7 · P0.10 remainder: replay adapters + retrains for QM/ATS1/ATB1/SRA1 (VPS, several sessions)
**Pre-decision:** follow exactly the pattern of the adapters already delivered: lift detection logic into shared `core/*` builders, `tools/walkforward_sim.py --strategy <s>`, retrain via `retrain_from_replay.py`, artifact to `staging_models/` with a new model tag. **Rollout of each candidate = C-gate (Michi).**
**Correction 2026-07-09 (T-028): MIS1 no longer belongs on this list.** The adapter and retrain code are built — `walkforward_sim.py` supports `ufi1, td, bb, abr1, mis1, rub` (`:906`), `retrain_from_replay.py` additionally `epd` (`:771`), shared builders `core/{mis,rub,funding}_features.py` exist. For MIS1 only the **execution** is still outstanding (400d replay on the VPS → train the MIS2 family → calibration report), not the code.
**Without any adapter (grep: 0 hits in `walkforward_sim.py`):** QM, ATS1, ATB1, SRA1. Order by live relevance: MIS1 execution + QM first, ATB1 last (parked).
**Precondition: A2b (P1.45) — ✅ fully satisfied.** MIS/QM (T-030) as well as EPD2/SRA2 (T-042/T-055) now read their artifact `model_id`; a MIS3/QM2/EPD3/SRA2 rollout posts under the new tag. The `model_id` wiring is thereby closed fleet-wide — the rollout block now exists only as a C-gate (Michi), no longer code-side. QM2 retrain preparation is underway (`qm_ml_trainer.py` writes `model_id`, T-061); the P1.13 recompute before it remains operator-gated.

## Tier C — Michi-gated (Opus prepares briefing/numbers, does not replace the verdict)

### C1 · T-2026-CU-9050-018 TimescaleDB R1 migration (VPS, ~14h, the big structural project)
Design done (`docs/TIMESCALE_R1_MIGRATION.md`). **Open operator decisions are in the task** (retention, REAL→double, 1d/1w via REST, start time) + gate "3-5 days stable fleet". Opus may prepare: the `core/candles.py` API + call-site inventory (~40 spots) + `tools/candles_parity.py` as code in the worktree. Dual-write/backfill/cutover only after go-ahead. **Repeat the warning from the task to Michi:** R1 LOWERS signal rates (intended) — only re-tune thresholds after the retrain.
### C2 · Retrain rollouts / artifact promotions (P0.12 ABR2 candidate, future B7 candidates)
Opus delivers: calibration report, replay PnL comparison old/new, recommendation. Michi decides the promotion. **Fixed negative decision, do not reopen:** AIM1/P0.13 stays OFF, no vocabulary retrain (rationale: OPUS-HANDOFF §8).
### C3 · Z0 CPU baseline-load program (VPS; "WICHTIGSTER PUNKT" [most important point] in the ledger)
Pre-decision: **measure first, then fix** — 10-min per-process sampling against the known candidates (full-table scans D1, WAL/table sprawl, P2.19 WMA/KAMA, P2.44 538 HTTP calls, P1.40, P1.38). Opus may build/run the measurement (read-only); any measure from it that touches the live DB/fleet (D1 indexes, D2 VACUUM FULL, D4, D5 drops) goes to Michi individually. Goal: baseline load <50%.
### C4 · Z1 dashboard rewrite
Only after Z2 (B4). The tech decision (Flask vs FastAPI+HTMX/React, SSE/WS, mobile) is a council/Michi decision — Opus prepares the options matrix, folding in P0.8/R2/P1.38/CSRF.

---

## Recommended order (updated 2026-07-11 after the T-075 wave)

**Almost the entire BUILD backlog has been worked through.** Done: ~~A1~~, ~~A2~~, ~~A2b~~ (incl. EPD2/SRA2), ~~A3~~, ~~B5~~, almost the entire P2 robustness cluster (P2.13–P2.51 apart from the remainder named below) and ~~R2(a)~~. Still open in the P2 area are only P2.12, the TZ block P2.1–P2.6/P2.21 (behind R3) and P2.22/P2.23. What's still outstanding overall falls into three groups:

**1. BUILD, executable without Michi (remainder):**
- **P2.12** (Wilder-RSI migration) — a deliberate migration coupled to a retrain; now free, since T-092 cleared the data-pipeline surface (P2.13/15/20). Next indicator-engine item.
- **B1 · R3 central UTC policy** (`core/time.py` is in place, T-032) — the pool flip to `timezone=UTC` + the **TZ cluster P2.1–P2.6/P2.21** hang off it; a dedicated task with a fleet-restart window (details `docs/UTC_POLICY.md`).
- ~~**B2 · R4 remainder**~~ **STRUCK 2026-07-14** (fleet fleet-wide cross-margin, `cap_leverage_to_sl` not applicable — see B2 above), **R1** (forming-candle contract repo-wide — partly via the C1 call-site inventory), **P2.22/P2.23** (regime attribution/"unreliable" heuristic), **P2.38** (ABR1 `SUCCESS_CLASS_IDX`), **P3.12** (`REAL`→`double`, DB-adjacent).
- **A5 · P3 batch** (rest of P3.1, P3.2/P3.3/P3.5/P3.7/P3.8/P3.10/P3.11) — gap filler, don't mix into money-path PRs.

**2. VPS/ops — needs the live host, Michi releases the session:**
- **B6** VPS port + Claude Code on the VPS (blocker for A4 replay and B7).
- **B4/Z2** dashboard on `127.0.0.1` + Cloudflare tunnel (defuses P0.8), **P0.7 remainder** (clean up 5 active corrupt trades), **P2.2 ALTER** (`module` column width live), **R2(b)** `schema.sql`.
- **B7** replay adapters + retrains QM/ATS1/ATB1/SRA1; MIS1 execution.

**3. Michi-gated (C-gate) — Opus only prepares briefing/numbers:**
- **C1** TimescaleDB R1 migration, **C2** retrain rollouts/artifact promotions (P0.12 ABR2, future B7 candidates; AIM1/P0.13 stays OFF), **C3/Z0** CPU baseline-load program (measure first), **C4/Z1** dashboard rewrite (after Z2).

C1/C3 only as preparation, never unilaterally. The `model_id` rollout block (MIS3/RUB3/QM2/EPD3/SRA2) has been lifted code-side — only the respective C-gate remains open.

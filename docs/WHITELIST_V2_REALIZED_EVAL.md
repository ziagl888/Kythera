# Whitelist-v2 flip — realised evaluation (T-2026-KYT-9050-007)

**Tool:** `tools/whitelist_v2_realized_eval.py` · **Runs only on the VPS** (needs the live DB, strictly read-only) · **Purpose:** the numbers Michi's v1→v2 flip decision for the whitelist gate hinges on — measured against **realised** trades, not a replay of the rule.

The verdict itself is in `docs/T-2026-KYT-9050-007-whitelist-v2-flip-decision.md`. The flip is and remains Michi's decision (OPUS-HANDOFF §6, gate flip).

## Distinction from `tools/whitelist_v2_flip_eval.py` (T-2026-CU-9050-069)

The 069 tool answers the same flip question via a **counterfactual replay** of the ROM1 geometry (T-047 mechanics: `compute_rom1_trade_params` + `simulate_exit` on 1h candles). That's the right yardstick when no real outcome exists.

This tool swaps out **only the scoring layer**: the gate semantics (which paths the flip even touches), the divergence classes and the snapshot join are **imported** from `whitelist_v2_flip_eval`, not rebuilt. There is exactly one truth about what the flip changes. The only thing new: instead of a simulation, the **actually closed trade, scored by the monitor,** is read.

## Two realised yardsticks — deliberately kept separate

| Leg | Source | Exists for | Geometry |
|---|---|---|---|
| **Trigger leg** | `closed_ai_signals` (model = tag) resp. `closed_trades_master` (strategy = tag) | **both** gate sides | that of the source bot |
| **ROM1 leg** | `closed_ai_signals` (model = `ROM1`) | **only** the forwarded side | ROM1s own (P1.10) |

The trigger leg is the only **symmetric** measurement: a signal blocked by the gate was still posted to the bot's own channel and scored by the monitors — so it has a real outcome, even without a ROM1 trade.

The ROM1 leg is the **real money**, but it structurally exists only on the forwarded side. `v2_would_open` — the signals v2 would additionally let through — **fundamentally has no ROM1 leg** and cannot get one. That's not a data gap that could be closed; it's the limit of the question.

> **Trap (measured, not inherited):** `closed_trades_master.strategy` is the canonical realised source for the **classic detectors** (`Fast In And Out`, `Volume Indicator`, `Support Resistance`, `5 Percent`, `Main Channel`) — there and only there. **ROM1 is not in `closed_trades_master`** (0 rows, measured 2026-08-01); ROM1 and all AI bots live in `closed_ai_signals`. The tool therefore reads both tables and deduplicates `closed_ai_signals` via the Report 14 survivor key against the 357k-duplicate trap.

## Acceptance criteria (binary testable)

- [x] **AK1 Classification inherited:** the flip classes (`both_open`, `both_block`, `v2_would_block`, `v2_would_open`, `v2_missing`, `cell_missing`, `unaffected`) come via import from `tools/whitelist_v2_flip_eval.py`; this tool defines no gate semantics of its own. — Test: import assertion + `test_flip_delta_*`
- [x] **AK2 Time domain measured:** for each day, the naive time column is matched against the gate events under BOTH readings (UTC / `LEGACY_WRITER_TZ`); the reading with more matches wins, both match counts are in the report. — Test: `test_twin_index_detects_legacy_domain`, `test_twin_index_detects_utc_domain`, `test_pick_domain_*`
- [x] **AK3 1:1 assignment:** a closed trade is assigned to at most ONE gate event (greedy, smallest |Δt| first); collisions are counted and reported, never double-booked. — Test: `test_claim_nearest_never_reuses_a_trade`
- [x] **AK4 One realised definition:** PnL/WR/outcome come from `core.realized_pnl` + `tools/fleet_realized_audit` (T-115/T-032 definition: target-staggered unlevered move, WR = TP1 touch, LEGACY/censored excluded). No math of its own. — Test: `test_realized_from_ai_*`, `test_realized_from_classic_*`
- [x] **AK5 Nothing is silently discarded:** events without a twin (`no_twin`), without a ROM1 leg (`no_rom1_leg`, `not_forwarded`) and without a cell (`cell_missing`) are counted classes; `n_with_leg`, `censored_n` and `lev_n` are in every row. — Test: `test_summarize_legs_counts_missing_legs_separately`, `test_attach_trigger_legs_marks_unmatched`
- [x] **AK6 Asymmetry explicit:** the suppressed side never gets a ROM1 leg, and the report says why. — Test: `test_suppressed_side_never_gets_a_rom1_leg`
- [x] **AK7 Read-only:** `conn.set_session(readonly=True)`, no INSERT/UPDATE/DELETE in the tool. — Review + grep
- [x] **AK8 Breakdown by bot × direction** for both divergent classes, sorted by |Σ move%|. — Test: `test_by_bot_direction_splits_and_sorts`
- [x] **AK9 Clean vs. drift-contaminated:** every divergent class is split into `v1_agree` (today's v1 cell matches the recorded decision — clean v1-vs-v2 comparison) and `v1_drifted` (the cell has moved since — the "difference" compares two v1 states) and reported separately. — Test: `test_agreement_split_separates_drifted_events`
- [x] **AK10 Divergence by v1 path:** the divergent traffic is broken down by the recorded v1 path (`insufficient_data` = default-open crutch vs. `wr_above_overall` = decision on merit). — Test: `test_path_breakdown_splits_crutch_from_merit`

## Out of Scope

- The flip itself (`SELECT whitelisted` → `whitelisted_v2` in `28_signal_orchestrator.get_whitelist_decision`) and the orchestrator restart. Gate flip = Michi (hard rule, OPUS-HANDOFF §6).
- Retuning the `V2_*` constants in `27_bot_regime_analyzer.py`.
- Any as-of reconstruction of historical whitelist states (see caveat 1) and any DB write operation.

## Methodology & caveats (the report repeats them)

1. **Snapshot approximation — and its measured quality.** `bot_regime_whitelist` is UPSERT-only without history, `bot_regime_performance` likewise, and bot 28 logs only the v1 path per signal (`wl_reason` / `reason`), never the v2 verdict. The v2 verdict per event therefore comes from **today's** snapshot. The T-031 finding "the historical whitelist cannot be reconstructed" still holds unchanged — the tool doesn't work around it, it **quantifies** it: the v1 drift (recorded gate path vs. today's v1 cell) measures the error on the only axis where both states are known. The drift grows with the window length; **always read a window together with its drift**, never without.
2. **Drift contaminates the class, not just the accuracy (AK9).** A flip class compares the *recorded* v1 decision with *today's* v2 cell. If today's v1 cell no longer matches the recorded decision, the class compares two different cell states — it then measures cell movement, not v1-vs-v2. Measured in the May/June window: **every single** `v2_would_open` event was drift-contaminated. That's why `v1_agree` is the robust subset; `v1_drifted` stands next to it, not inside it.
3. **v2 is fitted IN-SAMPLE on the trigger leg.** `27_bot_regime_analyzer` builds `bot_regime_performance` from the closed trigger trades of the last `REFERENCE_WINDOW_DAYS = 30` days, and `_v2_whitelist_decision` decides a cell purely from their `avg_pnl_pct`/`pnl_stddev`. A run within this window therefore measures v2 against the data v2 was fitted on — that v2 blocks cells with negatively realised trigger trades there is largely a restatement of the fitting criterion, **not independent evidence**. Independent are (a) the ROM1 leg and (b) a run with `--until` before the window start. The report sets the caveat automatically when the windows overlap (`in_sample_overlap`).
4. **Two time domains in the same column.** Measured on 2026-08-01: `orchestrator_open_trades.opened_at`, `orchestrator_suppressed_signals.ts` and the **ROM1** rows in `closed_ai_signals` carry UTC; the bot rows in `closed_ai_signals`/`closed_trades_master` carry `Europe/Bucharest` wall-clock time (+3h in summer). `KYTHERA_R3_CUTOVER_UTC` is **not set** on the VPS (uniform-utc mode) — so the domain depends on the writer, not on a date. A join that ignores this matches 0.0% of the events (measured). The tool decides the reading per day from the data and reports both match counts.
5. **Censorship by the orchestrator itself.** ROM1 trades closed via `AUTO_CLOSE_ON_REGIME_CHANGE` carry `CLOSED_REGIME_CHANGE` and fall under T-032's `_CENSOR_FRAGMENTS` rule (neither win nor loss). On the ROM1 side, a large share of the legs is therefore **censored** — the `zensiert` column is in every table, and the ROM1 numbers must not be read as a full census.
6. **Trigger leg ≠ ROM1 leg (P1.10).** The gate decides on the trigger bot's statistics, but ROM1 geometry is what's traded. The two yardsticks can contradict each other in **sign**; in the evaluation, they actually do. Reading only one means reading the wrong question.
7. **Fallback traffic is flip-neutral.** `no_whitelist_entry`, `whitelist_stale:*`, `regime_is_transition:*`, `regime_unstable:*` and a NULL `wl_reason` behave identically under v2 (the flip only swaps the 4D cell lookup). They are counted (`unaffected`), but not included in the rate comparison — only in the trades/day forecast, as a constant baseline.
8. **`lev` PnL is exact-only AND clamped.** Missing/unparsable leverage leads to the row being excluded, not to a default (`core.realized_pnl.parse_leverage`); coverage is shown as `(n)` behind every Σ-lev number. In addition, `realized_pnl_pct` clamps every loss at **−100%** (the liquidation floor) — gains are unbounded, losses are not. A Σ-lev sum is therefore **biased upward** and must not be read as "money". The unlevered, target-staggered move is the coverage-robust, clamp-free metric; every statement in the decision document rests on it.

## Execution (VPS session)

```
python tools/whitelist_v2_realized_eval.py --since 2026-07-11T00:00:00                          # volles Shadow-Fenster
python tools/whitelist_v2_realized_eval.py --since 2026-07-25T00:00:00                          # kurz, weniger Drift
python tools/whitelist_v2_realized_eval.py --since 2026-05-15T00:00:00 --until 2026-07-02T00:00:00   # out-of-sample
```

The CPU guard from `walkforward_sim` aborts above >90% system load. The VPS sits permanently at 100% (measured); for this read-only run there is therefore `--cpu-wait-min N` (wait) and `--force-on-busy` (run anyway, at BELOW_NORMAL priority). The measured load at start is recorded as `cpu_at_start_pct` in the summary — a run never claims headroom it didn't have.

Output goes to `KYTHERA_REPLAY_DIR`: `whitelist_v2_realized_eval_<since-datum>.jsonl` (all events incl. skip reasons), `..._summary.json` (all aggregates) and `....md` (report with the bot × direction breakdown).

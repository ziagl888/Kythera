# Whitelist-v2 flip evaluation (T-2026-CU-9050-069)

**Tool:** `tools/whitelist_v2_flip_eval.py` · **Runs only on the VPS** (needs the live DB, strictly read-only) · **Purpose:** data basis for Michi's flip decision v1→v2 of the whitelist gate (T-2026-CU-9050-048, MODEL_INTENT §23).

> **Sister tool (T-2026-KYT-9050-007):** `tools/whitelist_v2_realized_eval.py` answers the same flip question against **realized** trades instead of the counterfactual replay. It imports the gate semantics and the divergence classes from this module (one source of truth) and only swaps out the scoring layer. Docs: `docs/WHITELIST_V2_REALIZED_EVAL.md`, verdict: `docs/T-2026-KYT-9050-007-whitelist-v2-flip-decision.md`.

## Intent

Since the T-068 deploy (2026-07-11), `27_bot_regime_analyzer` writes the shadow columns `whitelisted_v2`/`reason_v2` (net-expectancy lower bound with EB shrinkage) in parallel with the live-read v1 gate (`wr_bot >= wr_overall`). This tool answers the four questions from T-069:

1. **Divergence matrix** — on which cells do v1 and v2 decide differently, and in which direction?
2. **Counterfactual PnL** — what would the divergence cases have earned/cost in the first-touch replay (047 scorer mechanics)?
3. **Volume effect** — gate rate v2 vs. v1 on real signal traffic, ROM1 trades/day forecast.
4. **Decision basis** — figures for flip yes/no/parameter re-tuning. The recommendation itself is written by the VPS session; the flip is Michi's decision (stop-B applies: no added value → v1 stays).

## Acceptance criteria (binary testable)

- [ ] **AC1 divergence matrix:** every cell of the whitelist snapshot is sorted into exactly one class — `both_open`, `both_block`, `v2_would_block` (v1 open / v2 block), `v2_would_open` (v1 block / v2 open), `v2_missing` (column NULL). Sum of classes = cell count. — Test: `test_divergence_matrix_*`
- [ ] **AC2 traffic classification:** every gate event (forwarded via `wl_reason`, suppressed via the `reason` suffix after `bot_not_whitelisted:`) is deterministically classified as flip-affected (cell-decided: `wr_above_overall`, `counter_trend_specialist`, `insufficient_data`, `wr_below_overall`, `counter_trend_insufficient`) or flip-unaffected (`no_whitelist_entry`, `whitelist_stale:*`, `*fallback*`, NULL) — fallback paths do not change through the flip. — Test: `test_classify_*`
- [ ] **AC3 v2 join:** flip-affected events are joined against the snapshot via `(pretty_name(bot), regime, alt_context, direction)`; a missing cell (`cell_missing`) and NULL v2 (`v2_missing`) are counted, never silently dropped. — Test: `test_classify_missing_*`
- [ ] **AC4 one geometry source:** counterfactual scoring runs exclusively via the T-047 mechanics (`tools.rom1_counterfactual.score_row`/`load_1h` → `compute_rom1_trade_params` + `simulate_exit`), no rebuilt geometry (X-R1). — Test: import assertion `test_reuses_047_scorer`
- [ ] **AC5 drift metric:** for cell-decided events, the agreement between "recorded v1 decision (event) vs. v1 in today's snapshot" is computed and reported — it quantifies the error of the snapshot approximation (see caveats). — Test: `test_drift_*`
- [ ] **AC6 volume calculation:** gate rates v1/v2 and the trades/day forecast are pure functions of the classification counters. — Test: `test_volume_*`
- [ ] **AC7 read-only:** `conn.set_session(readonly=True)`; the tool contains no INSERT/UPDATE/DELETE. — Review + grep
- [ ] **AC8 artifacts + visibility:** JSONL (all events incl. skips) + summary JSON to `KYTHERA_REPLAY_DIR`; the console report contains prerequisite checks (bot-27 freshness via `MAX(computed_at)`, v2 column coverage) and per-day event counters (makes the 2026-07-13 outage gap visible). — Test: `test_daily_counts` + run observation

## Out of scope

- The flip itself (gate switch in bot 28 + restart) — its own small VPS intervention after Michi's go-ahead.
- Re-tuning of the V2_* constants — a result of the evaluation, not of this tool.
- Any DB write operation, any as-of reconstruction of historical whitelist states (see caveat 1).

## Why build (phase 0b)

`tools/rom1_counterfactual.py` (047) buckets by v1 gate paths and does not know v2; the divergence axis v1×v2 and the snapshot join exist nowhere. This tool only builds that axis new and delegates geometry+replay entirely to 047/walkforward (extend, no rebuild).

## Methodology & caveats (repeated in the report)

1. **Snapshot approximation:** the v2 verdict per event comes from the *current* whitelist snapshot, not the state at signal time (bot 28 does not log v2 per signal; `bot_regime_whitelist` is UPSERT-only without history). At ≤7 days of distance and 30d statistics windows this drifts slowly; the **AC5 drift metric measures the approximation** against v1 (where both states are known). High v1 drift (>15%) ⇒ read the snapshot numbers only as a trend, extend the evaluation with an as-of reconstruction if needed.
2. **Regime + alt-context on the suppressed side** come from the combined `regime_at_signal` string (`"REGIME/ALT"`, written from `regime_current` — i.e. exactly the debounced state the gate read at decision time; no P2.22 skew). Only legacy rows without a `/` fall back to the `regime_history` lookup at signal time (RAW, P2.22 skew documented there). The forwarded side has `alt_context_at_open` natively.
3. **Counterfactual instead of realized PnL on BOTH sides** (including for trades actually forwarded): same yardstick, no monitor-label dependency (Report-17 caveat: monitor scoring is only 63.4% replay-consistent).
4. **Short window:** signals from the last few days have not yet reached the horizon — `open_at_horizon` trades count mark-to-market into the PnL sum, not into the WR (047 semantics). Default horizon here is 72h (not 168h), matching the short shadow window.
5. **Outage 2026-07-13** (~14h ingestion dead): per-day counters show the gap; the bot-27 freshness check shows whether the analyzer ran through. With a thin window: postpone the evaluation instead of over-interpreting.

## Execution (VPS session, ~17./18.07.)

```
# Schnell (nur Matrix + Volumen, kein Replay):
python tools/whitelist_v2_flip_eval.py --skip-replay

# Voll (mit Counterfactual-Replay der Gate-Events seit Deploy):
python tools/whitelist_v2_flip_eval.py --since 2026-07-11T00:00:00 --horizon-hours 72
```

Output: `KYTHERA_REPLAY_DIR/whitelist_v2_flip_eval_<since-datum>_<horizont>h.jsonl` + `..._summary.json` (parameterized names — comparison runs don't overwrite each other) + console report. Interpretation: `v2_would_block` with a positive counterfactual sum = v2 would take money away; `v2_would_open` with a positive sum = v2 would unlock money; a portfolio comparison v1 selection vs. v2 selection on identical traffic is at the end of the report.

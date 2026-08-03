# SPEC — Z1 Dashboard Shell (Task 0, Foundation)

Task: T-2026-CU-9050-151 · Decision gate: D-2026-CLD-111 (z-council)
Stack (binding): Flask + HTMX + interval polling. No FastAPI, no SPA, no
Node build on-box. Charting via vendored JS (TradingView Lightweight Charts +
Apache ECharts) as static assets.

## Intent
Build the load-bearing shell of the Z1 dashboard: a Flask app factory that
mounts the existing `analytics_api` blueprint (T-131 DuckDB substrate), a
responsive HTMX base layout, a shared chart-lifecycle JS helper (the
core deliverable — prevents canvas/WebGL/listener leaks across the later 9
panels), a polling pattern + ONE demo panel (success-rate endpoint) as an
end-to-end proof, a data-freshness badge foundation and a waitress entrypoint
on 127.0.0.1. Everything DB-free testable. The old `dashboard.py` remains
untouched.

## Acceptance criteria (binary testable)
- [x] AK1: `tools/dashboard/app.create_app(duckdb_path)` returns a Flask app
  that mounts the `analytics_api` blueprint — `GET /api/analytics/success-rate`
  answers 200 against a synthetic DuckDB. — Test: `test_json_api_mounted` ✅.
- [x] AK2: `GET /` returns 200, the responsive base layout (viewport meta),
  wires in HTMX + `chart_lifecycle.js` and contains the demo panel container
  with `hx-get="/panels/success-rate"` and `hx-trigger` polling (`every … s`). —
  Test: `test_index_renders_shell` ✅.
- [x] AK3: `GET /panels/success-rate` returns 200 and renders the
  success-rate fields (bot tag + win rate) from `success_rate_timeseries`
  against the synthetic DuckDB as an HTMX partial. — Test:
  `test_demo_panel_renders_winrate` ✅.
- [x] AK4: `GET /static/js/chart_lifecycle.js` returns 200 and the helper
  registers chart instances + calls `dispose`/`remove` on `htmx:beforeSwap`
  and re-inits on `htmx:afterSwap`. — Test: `test_chart_lifecycle_js_served` ✅.
- [x] AK5: The data-freshness badge renders "Stand HH:MM, Sync vor N min" from
  the T-131 freshness rows; the pure `freshness_summary` function computes the
  age STRICTLY from `synced_at` (UTC) — never by mixing with the naive-local
  `last_row_ts`. — Test: `test_freshness_summary_*` +
  `test_index_shows_badge` ✅.
- [x] AK6: The serving entrypoint binds to 127.0.0.1 (never 0.0.0.0) and runs
  waitress in the prod path (P0.8 lesson). — Test:
  `test_serve_defaults_to_localhost` +
  `test_serve_delegates_to_waitress_path` ✅ (plus a real waitress smoke test).
- [x] AK7: No import and no panel/API route triggers a Postgres connect — the
  entire read path runs only against DuckDB. — Test:
  `test_routes_never_touch_postgres` + `test_import_is_db_free`
  (subprocess) ✅.

## Out of Scope
- Removing/migrating the old `dashboard.py`.
- Auth / mutation endpoints / Cloudflare Access wiring.
- The 9 feature panels themselves (only ONE demo panel as shell proof).
- SSE (interval polling is the default per D-2026-CLD-111).
- Full data-freshness-badge rollout per panel (base version only).

## Why Build (instead of Reuse)
The shell is project-specific wiring (analytics_api blueprint + HTMX +
vendored charts + VPS serving contract). No OSS library delivers exactly this
composition. The substrate (analytics_export/analytics_api from T-131) is
REUSED, not rebuilt.

## Scope of consent
**Allowed:** `tools/dashboard/**` new, `backtest/test_dashboard_shell.py` new,
additive blueprint extraction in `tools/analytics_api.py` (behaviour-preserving,
covered by existing tests), `CHANGELOG.md` entry, on branch
`feat/t-2026-cu-9050-150`.
**Forbidden:** `dashboard.py` (old dashboard), `.env*`/secrets, live DB,
fleet restart, model artifacts, binding to 0.0.0.0, `--no-verify`, main/prod
directly.
**Ask first:** new runtime dependencies (other than flask/htmx/duckdb/waitress
which already exist), real vendor JS acquisition requiring network access.

---

## Feature 3 — Success-rate time-comparison panel (T-2026-CU-9050-155)

Task: T-2026-CU-9050-155 · builds on T-131 (`success_rate_timeseries`) and
T-151 (shell/chart lifecycle).

### Intent
Full time-comparison version of the T-151 demo panel: an ECharts line time
series of the ROLLING 7/30/90d win rate per selected bot over time (not just
a current bar), with bot multiselect and window switcher. New route
`/panels/success-rate-timeseries` — does NOT collide with the existing
`/panels/success-rate` demo (which stays untouched for T-151's own tests).

### Acceptance criteria (binary testable)
- [x] AK1: `analytics_api.rolling_success_rate_series()` returns, per bot, a
  time series of the rolling `window`-day win rate, additive to
  `success_rate_timeseries` (unchanged), the same DECISIVE trade
  definition via `bot_trade_rows`. — Test:
  `test_rolling_success_rate_series_multi_bot_diverges_per_window` ✅.
- [x] AK2: Rolling 7/30/90d windows deliver GENUINELY different values on the
  same day (no coincidentally identical windows) — Test:
  `test_rolling_series_for_bot_windows_diverge_at_last_day` ✅.
- [x] AK3: Bot multiselect filters the time series correctly (multiple bots ->
  multiple series, one bot -> one series). — Test:
  `test_panel_multiselect_two_bots_renders_two_series` +
  `test_panel_single_bot_selection_renders_one_series` ✅.
- [x] AK4: Explicit empty selection (all checkboxes deselected) shows "Keine
  Bots ausgewaehlt" instead of silently falling back to "all bots" —
  Test: `test_selected_bots_respects_explicit_empty_selection` +
  `test_panel_explicit_empty_selection_shows_message` ✅.
- [x] AK5: `GET /panels/success-rate-timeseries` renders an ECharts line
  time series (`data-chart="winrate-timeseries"`), mounted via
  `chart_lifecycle.js` (dispose/re-init on HTMX swap), window switcher
  (7/30/90d) as a form. — Test:
  `test_panel_default_load_selects_all_bots_and_default_window` +
  `test_winrate_timeseries_factory_registered_in_panels_js` ✅.
- [x] AK6: Window switching changes the rendered values end-to-end (not
  only at the function level). — Test:
  `test_panel_window_switch_changes_rendered_values` ✅.
- [x] AK7: No Postgres access, DB-free testable, no breaking the existing
  `/panels/success-rate` demo. — Test:
  `test_panel_never_touches_postgres` +
  `test_existing_success_rate_demo_route_untouched` ✅.

### Out of Scope
- Live control (Feature 4).
- The other panels (fleet registry, leaderboard).
- Changing/rebuilding `success_rate_timeseries` itself (only additive
  extension `rolling_success_rate_series`).
- A new `/api/analytics/success-rate-timeseries` JSON endpoint (the panel
  route calls the analytics function directly, as the other panel routes
  do — no extra JSON API endpoint required).

### Why Build (instead of Reuse)
Rolling time-series windows + bot multiselect + HTMX self-update widget is
project-specific wiring on the existing T-131 substrate; no library delivers
that. `success_rate_timeseries`/`bot_trade_rows` are reused, not rebuilt.

### Scope of consent
**Allowed:** `tools/dashboard/**` additive, `tools/analytics_api.py` additive
(new functions, existing ones unchanged),
`backtest/test_dashboard_success_rate_panel.py` new, `CHANGELOG.md` entry, on
branch `worktree-feat+t-2026-cu-9050-155`.
**Forbidden:** `dashboard.py` (old dashboard), `.env*`/secrets, live DB,
fleet restart, model artifacts, rewriting `success_rate_timeseries` in
substance, `--no-verify`, main/prod directly, push/PR (orchestrator step).
**Ask first:** new runtime dependencies, changes to the existing
`/panels/success-rate` demo route/tests.

---

## Feature 4 — Data-freshness indicator per panel (T-2026-CU-9050-156)

Task: T-2026-CU-9050-156 · builds additively on `freshness_summary()` (T-151)
and `analytics_export.data_freshness()` (T-131).

### Intent
Today ONE shell-global badge (`_freshness_badge.html`, base layout) shows the
data freshness of the MOST RECENT sync across ALL sources. This feature makes
the freshness PANEL-SPECIFIC: each of the four panels (`success-rate`,
`success-rate-timeseries`, `leaderboard`, `fleet-registry`) shows "Stand
HH:MM, Sync vor N min" ONLY for the source(s) that panel actually reads —
and with multiple sources the OLDEST (worst-case), never a fabricated
mixture. The global badge remains unchanged (additive refinement, not a
replacement).

### Acceptance criteria (binary testable)
- [x] AK1: `freshness_summary()` gets two additive optional parameters:
  `sources: Sequence[str] | None` (filters the rows BEFORE aggregation to
  the named sources) and `worst_case: bool = False` (when `True` aggregates
  the OLDEST instead of the (previous default) FRESHEST source —
  the shell-global badge asks "is the pipeline alive at all", a panel badge
  must instead show worst-case). Both defaults reproduce exactly the
  previous behaviour (all existing tests stay green, no signature breaking
  change). — Test:
  `test_freshness_summary_sources_filter_narrows_rows`,
  `test_freshness_summary_worst_case_picks_oldest` +
  all existing `test_freshness_summary_*` unchanged and green.
- [x] AK2: New pure function `panel_freshness(rows, panel, *, now_utc=None)`
  resolves the panel's sources via `PANEL_SOURCES[panel]` and delegates to
  `freshness_summary(rows, sources=..., now_utc=..., worst_case=True)`.
  Panels with `PANEL_SOURCES[panel] == ()` (currently only
  `fleet-registry`, file-based — no DuckDB sync) return
  `FILE_BASED_FRESHNESS` instead of a fabricated time. An unknown panel
  name raises `ValueError` (no silent fallback masking a wrong mapping). —
  Test:
  `test_panel_freshness_leaderboard_and_success_rate_share_sources`,
  `test_panel_freshness_fleet_registry_is_file_based`,
  `test_panel_freshness_unknown_panel_raises`.
- [x] AK3: Two sources with DIFFERENT `synced_at` for the same panel yield
  the OLDER (smaller) freshness — never the average, never the newer,
  regardless of WHICH of the two is the stale one. —
  Test: `test_panel_freshness_oldest_source_wins_regardless_of_which_is_stale`.
- [x] AK4: If any freshness row is missing for the panel's source(s) (empty
  result after the source filter), the panel badge partial renders `—`
  instead of a fabricated timestamp. — Test:
  `test_panel_freshness_badge_partial_missing_shows_dash`.
- [x] AK5: The panel templates `success_rate.html`,
  `success_rate_timeseries.html`, `leaderboard.html`, `fleet_registry.html`
  embed the new parametrised badge partial
  `_panel_freshness_badge.html` (takes the panel-local `freshness`
  variable), END-TO-END over the real routes `GET /panels/{success-rate,
  success-rate-timeseries, leaderboard, fleet-registry}` against a real
  `AnalyticsExporter`/DuckDB fixture. — Test:
  `test_leaderboard_panel_route_renders_own_freshness`
  (integration test, real exporter→DuckDB→route→HTML chain).
- [x] AK6: Age stays STRICTLY computed from `synced_at` (UTC), never from
  `last_row_ts` (naive-local) — inherited from `freshness_summary`, backed
  again by a mutation check (a swap to `last_row_ts` turns the test red). —
  Test: `test_panel_freshness_age_from_synced_at_not_last_row_ts`.

### Out of Scope
- Live control (no auto-refresh button, no manual re-sync trigger).
- Functional rebuild of the four panels themselves (only additive badge
  embedding).
- Removing the shell-global badge (`_freshness_badge.html`/`base.html`
  stay untouched).
- A new `/panels/freshness/<panel>` JSON endpoint — the badge is rendered
  server-side as part of the respective panel fragment and updates with its
  existing poll interval (no extra HTMX round trip).

### Why Build (instead of Reuse)
Panel→source mapping + oldest-wins aggregation is project-specific wiring on
the existing T-131/T-151 substrate; no library delivers that.
`freshness_summary()` is extended additively (new optional parameter,
default path unchanged), not rewritten.

### Scope of consent
**Allowed:** `tools/dashboard/app.py` additive (new parameter on
`freshness_summary`, new functions/constants), `tools/dashboard/templates/**`
additive (new partial + embedding in the four panel templates),
`backtest/test_dashboard_freshness.py` new, `CHANGELOG.md` entry, on branch
`worktree-feat+t-2026-cu-9050-156`.
**Forbidden:** `dashboard.py` (old dashboard), `.env*`/secrets, live DB,
fleet restart, model artifacts, `core/**`, removing/rewriting the existing
global badge or `freshness_summary`'s previous return value at
`sources=None`, `--no-verify`, main/prod directly, push/PR (orchestrator
step).
**Ask first:** new runtime dependencies, changes to existing panel route
signatures/tests from Features 1-3.

---

## Feature 5 — Global success-metric toggle (T-2026-CU-9050-157)

Task: T-2026-CU-9050-157 · builds additively on T-154 (`bot_leaderboard`/
`_LEADERBOARD_SORT_KEYS`) and T-151 (shell).

### Intent
A shell-global success-metric toggle (win rate / expectancy / net PnL) in
the base layout determines which figure the panels highlight. Cross-cutting
via the `?metric=` query param, which the leaderboard panel reads: the
chosen metric is shown as a highlighted column AND used as the default sort
(`metric`→`sort_by`: winrate→winrate, expectancy→expectancy_pct,
netto-pnl→pnl_sum_pct). Sensible default netto-pnl (= the existing
`DEFAULT_LEADERBOARD_SORT`). An unknown `metric` value silently falls back
to the default (no 500). Panels that don't know the metric ignore the
toggle harmlessly.

### Acceptance criteria (binary testable)
- [x] AK1: Pure mapping logic `resolve_metric(raw)` (unknown/None →
  `DEFAULT_METRIC`) and `metric_sort_by(metric)` (→ a key from
  `analytics_api._LEADERBOARD_SORT_KEYS`), Flask/DuckDB-free testable. —
  Test: `test_resolve_metric_*`, `test_metric_sort_by_maps_onto_leaderboard_sort_keys`,
  `test_metric_sort_by_unresolved_value_falls_back_to_default_sort_by`.
- [x] AK2: All three metrics + default: `GET /panels/leaderboard?metric=…`
  sorts by the mapped metric. Fixture ranks the same three bots in
  THREE different orders → a wrong/ignored mapping renders one of the
  OTHER orders (mutation check). — Test:
  `test_leaderboard_panel_metric_{winrate,expectancy,netto_pnl}_*`.
- [x] AK3: Unknown `metric` value → default (no 500), route 200. — Test:
  `test_leaderboard_panel_unknown_metric_falls_back_to_default_no_500`,
  `test_index_unknown_metric_query_param_falls_back_no_500`.
- [x] AK4: The shell toggle (`base.html`) renders the three options, marks
  the active one, and the resolved value is baked into the leaderboard
  panel's own hx-get URL, so load + poll keep the same metric. —
  Test: `test_index_renders_metric_toggle_with_default_active`,
  `test_index_metric_query_param_selects_active_toggle_option`.
- [x] AK5: The chosen metric column is highlighted in the leaderboard
  (`metric-highlight`), consistent with the sort. — Test:
  `test_leaderboard_panel_metric_winrate_reorders_and_highlights`.
- [x] AK6: No Postgres access, DB-free testable. — Test:
  `test_toggle_never_touches_postgres`.

### Out of Scope
- Live control (Feature 4).
- Rebuilding the other panels — they inherit the toggle only harmlessly
  (fleet registry/success-rate/time-comparison ignore `metric`).
- A new JSON API endpoint for the toggle (`/api/analytics/leaderboard`
  already accepts `sort_by` directly).

### Folded-in review nit cleanups (this task touches app.py/CSS/leaderboard test anyway)
- CSS token hygiene: own `--loss` token for `.pnl-negative` (instead of the
  `--stale` freshness token); `--live` (byte-identical to `--accent`)
  removed, `var(--accent)` used directly. Purely cosmetic, no visual
  break.
- Name collision: module function `panel_freshness()` → `panel_freshness_summary()`
  (collided with the nested route handler `def panel_freshness()` in
  `create_app()`); all four panel-context callers + freshness tests adjusted,
  behaviour-preserving.
- Test gap (T-154-MEDIUM): `sort_by="winrate"` and `sort_by="n"` with a
  divergent fixture (order ≠ pnl default) — an ignored `sort_by` now
  turns red.

### Why Build (instead of Reuse)
Shell-global metric toggle + panel highlight/sort coupling is
project-specific wiring on the existing T-131/T-154 substrate; no library
delivers that. `bot_leaderboard` is reused (via its already existing
`sort_by` parameter), not rebuilt.

### Scope of consent
**Allowed:** `tools/dashboard/app.py` additive (new constants/functions +
`metric` param on `_leaderboard_context`/the routes), `tools/dashboard/templates/**`
additive (toggle in `base.html`, `metric` in `index.html`+`leaderboard.html`),
`tools/dashboard/static/css/app.css` (toggle/highlight styles +
`--loss`/`--live` cleanup), `backtest/test_dashboard_metric_toggle.py` new,
additions in `backtest/test_dashboard_leaderboard.py`/`test_dashboard_freshness.py`
(rename), `CHANGELOG.md` entry, on branch `worktree-feat+t-2026-cu-9050-157`.
**Forbidden:** `dashboard.py` (old dashboard), `.env*`/secrets, live DB,
fleet restart, model artifacts, `core/**`, SPEC.md in the repo root,
`--no-verify`, main/prod directly, push/PR (orchestrator step).
**Ask first:** new runtime dependencies, changes to existing
panel route signatures from Features 1-4 beyond the additive `metric`
param.

---

## Feature 6 — Bot x regime performance heatmap (T-2026-CU-9050-158)

Task: T-2026-CU-9050-158 · builds additively on T-131 (`regime_history`
export, `_outcomes_cte`/`_bot_filter`) and T-151 (shell/chart lifecycle).

### Intent
An ECharts heatmap: rows = bots, columns = regime states
(`regime_history.regime`), cell value = the bot's performance IN this
regime (win rate or avg PnL/trade, switchable, clearly labelled). For each
(bot, regime) cell, count the bot's DECISIVE trades whose `closed_at` falls
into the time window in which this regime state was active — an ASOF
join against the `regime_history` log (append-only, a regime applies from
its `ts` until the next log entry). Cells with no trades stay empty ("—"),
never fabricated. Trades whose `closed_at` is BEFORE the first ever
classified regime cannot be assigned to any window and are excluded from
the matrix (not booked into an "UNKNOWN" column).

### Acceptance criteria (binary testable)
- [x] AK1: `analytics_api.bot_regime_matrix()` returns additively
  `{bots, regimes, cells: {bot: {regime: {n, wins, winrate, pnl_sum_pct,
  expectancy_pct}}}}` — wiederverwendet `_outcomes_cte`/`_bot_filter` (dieselbe
  DECISIVE-Trade-Definition wie `bot_trade_rows`/`success_rate_timeseries`,
  unveraendert). — Test: `test_bot_regime_matrix_assigns_trades_to_active_regime_window`.
- [x] AK2: The bot-regime assignment is an ASOF join (`closed_at >= ts`, the
  last `regime_history` entry BEFORE/AT the trade time) — a trade right on
  the regime boundary falls into the NEW window, not the old one; a
  wrongly-directed join (mutation check) makes the cell values demonstrably
  wrong. —
  Test: `test_bot_regime_matrix_boundary_trade_joins_new_regime_window`
  (mutation check).
- [x] AK3: Cells with no trades don't appear in `cells` (no fabricated
  zero value); a bot with trades in only ONE of several regimes has only
  that one entry. — Test: `test_bot_regime_matrix_missing_cell_absent_not_fabricated`.
- [x] AK4: Trades before the first `regime_history` entry are excluded from
  the matrix (no "UNKNOWN" bucket). — Test:
  `test_bot_regime_matrix_trade_before_first_regime_row_excluded`.
- [x] AK5: `GET /panels/regime-heatmap` renders 200, an ECharts heatmap
  (`data-chart="bot-regime-heatmap"`, mounted via `chart_lifecycle.js`) +
  a table fallback view, with a metric switcher (win rate/avg PnL) and
  data-freshness badge (sources `regime_history` + `closed_ai_signals`),
  END-TO-END against a real `AnalyticsExporter`/DuckDB fixture with
  multiple bots x multiple regimes. — Test:
  `test_panel_regime_heatmap_renders_correct_cell_values` (integration test).
- [x] AK6: No Postgres access, DB-free testable, empty regime_history/empty
  outcome tables degrade cleanly (empty matrix, no 500). — Test:
  `test_panel_regime_heatmap_never_touches_postgres`,
  `test_bot_regime_matrix_empty_substrate_degrades_gracefully`.

### Out of Scope
- Live control (Feature 4 family).
- Rebuilding the other panels.
- Writing `regime_history` (read path only).
- markArea regime-band overlays on OTHER panels (only the heatmap itself).
- A new `/api/analytics/*` JSON endpoint (the panel route calls
  `bot_regime_matrix()` directly, as the other additive panel routes have
  done since Feature 3).

### Why Build (instead of Reuse)
Bot x regime ASOF join + heatmap wiring on the existing T-131/T-151
substrate is project-specific; no library delivers that. `_outcomes_cte`/
`_bot_filter`/`_existing_outcome_tables` are reused, not rebuilt; DuckDB
delivers `ASOF JOIN` natively (>= 1.5, verified here at 1.5.4).

### Scope of consent
**Allowed:** `tools/analytics_api.py` additive (new function(s), existing
ones unchanged), `tools/dashboard/app.py` additive (new
constants/functions + route + `PANEL_SOURCES` entry),
`tools/dashboard/templates/**` additive (new partial
`panels/regime_heatmap.html` + embedding in `index.html`),
`tools/dashboard/static/js/panels.js` additive (new ECharts factory),
`tools/dashboard/static/css/app.css` additive (heatmap styles),
`backtest/test_dashboard_regime_heatmap.py` new, `CHANGELOG.md` entry, on
branch `worktree-feat+t-2026-cu-9050-158`.
**Forbidden:** `dashboard.py` (old dashboard), `.env*`/secrets, live DB,
fleet restart, model artifacts, `core/**`, SPEC.md in the repo root, rewriting
existing `analytics_api` aggregate functions in substance, `--no-verify`,
main/prod directly, push/PR (orchestrator step).
**Ask first:** new runtime dependencies, changes to existing panel route
signatures from Features 1-5.

---

## Feature 7 — Coin drilldown with level chain (T-2026-CU-9050-159, Q11)

Task: T-2026-CU-9050-159 · builds additively on T-131 (`_outcomes_cte`/
`_bot_filter`/`_existing_outcome_tables`) and T-151 (shell/chart lifecycle,
vendored Lightweight Charts 4.2.3).

### Intent
A level chain: coin selector (lists only coins with at least one DECISIVE
trade) -> the panel shows for the chosen coin (1) a Lightweight Charts price
line (entry->exit points per trade, connected in close-time order) with
win/loss-coloured trade markers and (2) a compact trade table (close time,
bot/model, direction, entry, exit, PnL, target hit).

**SCOPING (binding):** full OHLCV candles are NOT part of this feature —
the 25GB candle export was deferred in T-131 and is not in the DuckDB
substrate. The panel instead renders the price PATH line through the
entry/exit points of the DECISIVE trades themselves (from
`closed_ai_signals`/`closed_trades`) — no real market candles. Documented
as a follow-up (see "Out of Scope" below + CHANGELOG.md).

### Acceptance criteria (binary testable)
- [x] AK1: `analytics_api.coins_with_trades()` returns the sorted list of
  coins/symbols with at least one DECISIVE trade (trades without PnL /
  housekeeping status don't count) — additive coin-aware CTE
  (`_outcomes_cte_with_coin`), the same `MICRO_PNL_PCT`/`MAX_ABS_PNL_PCT`
  thresholds as `_outcomes_cte`. — Test: `test_coins_with_trades_lists_only_decisive_coins`.
- [x] AK2: `analytics_api.coin_trade_series(con, symbol)` returns the
  DECISIVE trades of ONE coin sorted ascending by `closed_at`
  (`{bot, direction, closed_at, entry, close_price, targets_hit, pnl_pct,
  is_win}`); `targets_hit` is `None` for a `closed_trades`-Zeile (die
  Tabelle hat keine solche Spalte) statt einer fabrizierten 0. — Test:
  `test_coin_trade_series_returns_ordered_decisive_trades_for_one_coin`.
- [x] AK3: A wrong coin filter (mutation check: query on a DIFFERENT
  coin than the chosen one) returns a DIFFERENT set of trades — proving
  the filter is actually wired up. — Test:
  `test_coin_trade_series_wrong_coin_filter_yields_different_trades` (mutation check).
- [x] AK4: An unknown or empty coin (not in `coins_with_trades()`)
  returns `{"coin": symbol, "trades": []}` instead of an error or all
  trades. — Test: `test_coin_trade_series_unknown_coin_returns_empty`.
- [x] AK5: `GET /panels/coin-drilldown` renders 200, the coin selector (only
  coins with trades), a Lightweight Charts price line
  (`data-chart="coin-price-line"`) with win/loss markers and the trade
  table, END-TO-END against a real `AnalyticsExporter`/DuckDB fixture with
  multiple coins x multiple trades. — Test: `test_panel_coin_drilldown_renders_correct_series_and_table`
  (integration test).
- [x] AK6: No coin selected/unknown coin degrades cleanly (no 500,
  hint text instead of chart/table); an empty substrate (no trades at
  all) likewise. — Test: `test_panel_coin_drilldown_unknown_coin_shows_clean_message`,
  `test_panel_coin_drilldown_empty_substrate`.
- [x] AK7: The Lightweight Charts factory `coin-price-line` disposes via
  `chart.remove()` (NOT ECharts `.dispose()`), registered via
  `chart_lifecycle.js`. — Test: `test_coin_price_line_factory_registered_in_panels_js`.
- [x] AK8: No Postgres access, DB-free testable. — Test:
  `test_panel_coin_drilldown_never_touches_postgres`.

### Out of Scope
- Full OHLCV candles (candlesticks) — FOLLOW-UP, gated on the candle export
  from T-131 (25GB, deferred). Once the export exists, the price line can
  be replaced by a real Lightweight Charts candlestick series.
- Rebuilding the other panels.
- A new `/api/analytics/*` JSON endpoint (the panel route calls
  `coin_trade_series()`/`coins_with_trades()` directly, as the other
  additive panel routes have done since Feature 3).
- Multiple coins at once in the chart (only ONE coin per panel state, as
  required by the Q11 curation text).

### Why Build (instead of Reuse)
Coin-level drilldown on the existing T-131 substrate + a Lightweight Charts
price line with trade markers is project-specific wiring; no library
delivers that. `_outcomes_cte`/`_bot_filter`/
`_existing_outcome_tables` remain unchanged (Feature 2/3/6 depend on
them) — the coin variant is its own additive CTE with the same
decisive definition (identical threshold constants).

### Scope of consent
**Allowed:** `tools/analytics_api.py` additive (new function(s)
`coins_with_trades`/`coin_trade_series`/`_outcomes_cte_with_coin`, existing
functions unchanged), `tools/dashboard/app.py` additive (new route
`/panels/coin-drilldown`, new context function(s), `PANEL_SOURCES` entry),
`tools/dashboard/templates/panels/coin_drilldown.html` (new) +
`index.html` embedding, `tools/dashboard/static/js/panels.js` additive (new
Lightweight Charts factory `coin-price-line`), `backtest/test_dashboard_coin_drilldown.py`
new, `CHANGELOG.md` entry, on branch `worktree-feat+t-2026-cu-9050-159`.
**Forbidden:** `dashboard.py` (old dashboard), `.env*`/secrets, live DB,
fleet restart, model artifacts, `core/**`, SPEC.md in the repo root, existing
`analytics_api` aggregate functions (`_outcomes_cte`/`bot_trade_rows`/
`bot_leaderboard`/`success_rate_timeseries`/`bot_regime_matrix`) rewritten
in substance, building full OHLCV candles, `--no-verify`, main/prod directly,
push/PR (orchestrator step).
**Ask first:** new runtime dependencies, changes to existing panel route
signatures from Features 1-6.

---

## Feature 8 — Overnight digest home page (T-2026-CU-9050-160, F1)

Task: T-2026-CU-9050-160 · builds additively on T-131 (`_outcomes_cte_with_coin`/
`_bot_filter`/`_existing_outcome_tables_with_coin`, Feature 7) and
`_regime_history_present` (Feature 6).

### Intent
A digest/summary section RIGHT AT THE TOP of the home page: for a
configurable window (default "overnight" = 8h, switchable 8h/24h/7 days
via `?window=`) at a glance — aggregated net PnL (Σ%), trade count,
overall win rate, top/flop bot (by PnL sum), largest win/loss
(coin+bot+PnL) and (if the substrate carries `regime_history`) the number
of real regime CHANGES in the window (not mere log rows). The window is,
like `success_rate_timeseries`/`rolling_success_rate_series`, NEVER
anchored to a UTC "now" wall clock, but to `max(closed_at)` in the
substrate itself (`as_of`) — this keeps the window computation strictly in
the same naive-local time system as the `closed_at` columns themselves (TZ
contract: no mixing with a real UTC clock, see the analytics_export
TIMEZONE note). An empty window (no trades) shows "Keine Trades im
Fenster", never a 500 or fabricated zero values.

### Acceptance criteria (binary testable)
- [x] AK1: `analytics_api.overnight_digest(con, window_hours, *, as_of=None,
  bots=None)` liefert additiv `{as_of, window_hours, n, wins, pnl_sum_pct,
  winrate, top_bot, flop_bot, best_trade, worst_trade, regime_changes}` —
  reuses the coin-aware CTE from Feature 7
  (`_outcomes_cte_with_coin`/`_existing_outcome_tables_with_coin`), the same
  DECISIVE trade definition as everywhere else. `as_of=None` resolves to
  `max(closed_at)` in the substrate (data-anchored, never wall-clock-anchored). —
  Test: `test_overnight_digest_basic_aggregates`.
- [x] AK2: The window boundary is `closed_at > as_of - INTERVAL window_hours HOUR
  AND closed_at <= as_of` (half-open, the identical pattern as
  `success_rate_timeseries`) — a trade EXACTLY on the lower boundary is
  excluded, a trade just inside is included. A trade outside the window
  (older) must not affect PnL sum/count nor top-/flop-bot — a mutation
  check (window filter removed/inverted) makes `pnl_sum_pct`/`n`
  demonstrably wrong. — Test:
  `test_overnight_digest_window_boundary_excludes_outside_trade` (mutation check).
- [x] AK3: Top bot (highest summed PnL in the window) and flop bot (lowest)
  are correctly determined by sorting — fixture with 3 bots in an
  unambiguous order, a wrong/swapped sort turns the test red
  (mutation check). — Test: `test_overnight_digest_top_and_flop_bot_correct`
  (mutation check).
- [x] AK4: `best_trade`/`worst_trade` (largest win/loss) carry `{bot, coin,
  pnl_pct, closed_at}` of the actual extreme value in the window. — Test:
  `test_overnight_digest_notable_trades_correct`.
- [x] AK5: An empty window (no trade in the `window_hours` span, but the
  substrate has data outside it) returns `n=0`, `pnl_sum_pct=None`,
  `winrate=None`, `top_bot=None`, `flop_bot=None`, `best_trade=None`,
  `worst_trade=None` — never an error, never a fabricated 0. A
  completely empty substrate (no outcome table) degrades identically. —
  Test: `test_overnight_digest_empty_window_degrades_cleanly`,
  `test_overnight_digest_empty_substrate_degrades_cleanly`.
- [x] AK6: `regime_changes` counts REAL regime TRANSITIONS (value !=
  predecessor value in `regime_history`, via a `LAG` window) whose `ts`
  falls in the window — not mere log rows (an append without a value
  change doesn't count). If `regime_history` is missing from the
  substrate, `regime_changes=None` (never fabricated). —
  Test: `test_overnight_digest_regime_changes_counts_real_transitions_only`,
  `test_overnight_digest_regime_changes_none_without_regime_history`.
- [x] AK7: `GET /panels/overnight-digest` (and `?window=8h|24h|168h`) renders
  200: metric tiles (PnL/count/win rate), top/flop bot, notable trades and
  a window switcher, END-TO-END against a real
  `AnalyticsExporter`/DuckDB fixture, mounted right at the TOP of
  `index.html` (before fleet registry). Data-freshness badge
  (`closed_ai_signals`/`closed_trades`/`regime_history`). — Test:
  `test_panel_overnight_digest_renders_correct_values` (integration test),
  `test_index_includes_digest_panel_above_fleet_registry`.
- [x] AK8: No Postgres access, DB-free testable; unknown/missing
  `?window=` value silently falls back to the default (8h) (no 500). —
  Test: `test_panel_overnight_digest_never_touches_postgres`,
  `test_resolve_digest_window_unknown_value_falls_back_to_default`.

### Out of Scope
- Live control (Feature 4 family/F4).
- Decision-ready notifications (M5 = phase 2).
- Rebuilding the other panels.
- A sparkline chart (deliberately left out — tiles/lists suffice for the
  digest; no new ECharts factory entry needed).
- A new `/api/analytics/*` JSON endpoint (the panel route calls
  `overnight_digest()` directly, as the other additive panel routes have
  done since Feature 3).

### Why Build (instead of Reuse)
Window digest aggregation (top/flop bot, notable trades, regime
transitions) on the existing T-131 substrate is project-specific wiring; no
library delivers that. `_outcomes_cte_with_coin`/`_bot_filter`/
`_existing_outcome_tables_with_coin`/`_regime_history_present` are reused,
not rebuilt.

### Scope of consent
**Allowed:** `tools/analytics_api.py` additive (new function(s)
`overnight_digest`/`_regime_changes_in_window`, existing functions
unchanged), `tools/dashboard/app.py` additive (new constants/functions +
route `/panels/overnight-digest`, new `PANEL_SOURCES` entry),
`tools/dashboard/templates/panels/overnight_digest.html` (new) +
`index.html` embedding RIGHT AT THE TOP, `tools/dashboard/static/css/app.css`
additive (tile/column styles), `backtest/test_dashboard_digest.py` new,
`CHANGELOG.md` entry, on branch `worktree-feat+t-2026-cu-9050-160`.
**Forbidden:** `dashboard.py` (old dashboard), `.env*`/secrets, live DB,
fleet restart, model artifacts, `core/**`, SPEC.md in the repo root, existing
`analytics_api` aggregate functions (`_outcomes_cte`, `_outcomes_cte_with_coin`,
`bot_trade_rows`, `bot_leaderboard`, `success_rate_timeseries`,
`bot_regime_matrix`, `coins_with_trades`, `coin_trade_series`) rewritten in
substance, building live control/notifications, `--no-verify`, main/prod
directly, push/PR (orchestrator step).
**Ask first:** new runtime dependencies, changes to existing panel route
signatures from Features 1-7.

---

## Feature 9 — Event annotations as a READ-ONLY event feed (T-2026-CU-9050-161, S10)

Task: T-2026-CU-9050-161 · builds additively on `_regime_changes_in_window`
(Feature 8, lag logic) and `_outcomes_cte_with_coin`/
`_existing_outcome_tables_with_coin`/`_bot_filter` (Feature 7/8). Last
panel of the Z1 dashboard rewrite.

### Intent
S10 is a "simple intervention log", not an annotation EDITOR: a
chronological (newest first) event feed that consolidates + typifies
notable events from the AVAILABLE DuckDB sources — regime transitions from
`regime_history` (time + from->to, via the same lag logic as
`_regime_changes_in_window`) and notable trades from
`closed_ai_signals`/`closed_trades` (largest wins/losses of the window —
coin, bot, PnL, close time). Configurable window (`?window=`, default
24h, alternative 168h/7 days). A WRITING annotations feature would be a
mutation endpoint = F4-/Z2-gated (CLAUDE.md hard rule: no
mutations/live levers in the web UI before Cloudflare Access) —
therefore deliberately READ-ONLY, no POST/write endpoint built.

### Acceptance criteria (binary testable)
- [x] AK1: `analytics_api.event_feed(con, window_hours, *, as_of=None,
  bots=None)` liefert additiv `{as_of, window_hours, events}` mit
  `events: [{type, ts, title, detail}, ...]`, sorted chronologically
  DESCENDING (newest first). `as_of=None` resolves data-anchored
  (`max(closed_at)` across the outcome tables, else `max(ts)` from
  `regime_history`, else `None`) — never a wall-clock "now" clock. — Test:
  `test_event_feed_basic_shape_and_sort_order` (mutation check: sorted
  ascending instead of descending turns the test red).
- [x] AK2: Regime transitions (`type="regime_change"`) are REAL changes
  (value != predecessor value in `regime_history`, via a `LAG` window —
  identical logic to `_regime_changes_in_window`, Feature 8) within the
  window, with a from->to detail. A plain repeat of the same regime
  doesn't count, the very first `regime_history` row (no predecessor) is
  an initialisation, not a transition. — Test:
  `test_event_feed_regime_transitions_correct_and_repeats_excluded`
  (mutation check).
- [x] AK3: Notable trades (`type="notable_trade"`) are the largest wins/
  losses (separated per side via `is_win`, not via sorted `pnl_pct`
  with overlap risk at few trades) in the window, with coin+bot+PnL in
  the detail field. — Test: `test_event_feed_notable_trades_winners_and_losers`.
- [x] AK4: Window logic is half-open (`> as_of - INTERVAL window_hours HOUR
  AND <= as_of`), identisch zu `overnight_digest`/`success_rate_timeseries`
  — an event outside the window must not appear (mutation check:
  window boundary inverted/removed turns the test red). — Test:
  `test_event_feed_window_boundary_excludes_outside_events` (mutation check).
- [x] AK5: An empty feed (no event in the window, but the substrate has
  data outside it) returns `events: []`, never an error, never a
  fabricated event. A completely empty substrate degrades identically
  (`as_of: None, events: []`). — Test:
  `test_event_feed_empty_window_degrades_cleanly`,
  `test_event_feed_empty_substrate_degrades_cleanly`.
- [x] AK6: `GET /panels/event-feed` (and `?window=24h|168h`) renders 200: the
  typed, time-descending sorted event list (icon/label per type +
  timestamp + description), END-TO-END against a real
  `AnalyticsExporter`/DuckDB fixture, mounted as the last panel in
  `index.html` (after coin drilldown). Data-freshness badge
  (`closed_ai_signals`/`closed_trades`/`regime_history`). NO
  POST/write endpoint exists for this panel. — Test:
  `test_panel_event_feed_renders_events_in_descending_order`
  (integration test), `test_index_includes_event_feed_panel_last`.
- [x] AK7: No Postgres access, DB-free testable; unknown/missing
  `?window=` value silently falls back to the default (24h) (no 500). —
  Test: `test_panel_event_feed_never_touches_postgres`,
  `test_resolve_event_feed_window_unknown_value_falls_back_to_default`.

### Out of Scope
- **Writing operator annotations** (freely typed notes/tags by
  Michi) — that would be a mutation endpoint (POST/PUT + CSRF +
  persistence store for the annotation itself) and is explicitly Z2-gated
  (Cloudflare Access + auth must exist first, F4 family). Follow-up task,
  not part of this panel.
- Live control (F4 family).
- A hash journal / audit-trail signing (R9 — struck, see MEMORY).
- Rebuilding the other panels.
- Further event types beyond regime transitions/notable trades (e.g.
  fleet restarts, model promotions) — only built if trivially derivable
  from the existing substrate, deliberately not added here (no
  additional substrate exists that could deliver them DB-free).
- A new `/api/analytics/*` JSON endpoint (the panel route calls
  `event_feed()` directly, as the other additive panel routes have done
  since Feature 3).

### Why Build (instead of Reuse)
Consolidating typed events from two existing T-131 aggregate building
blocks (regime lag logic, coin-aware decisive-trade CTE) is
project-specific wiring; no library delivers that. The lag logic itself
(`_regime_changes_in_window`) and the coin-aware CTE
(`_outcomes_cte_with_coin`/`_existing_outcome_tables_with_coin`/
`_bot_filter`) are reused, not rebuilt.

### Scope of consent
**Allowed:** `tools/analytics_api.py` additive (new functions `event_feed`,
`_regime_transition_events`, `_notable_trade_events`, `_latest_event_anchor`,
existing functions unchanged), `tools/dashboard/app.py` additive (new
constants/functions + route `/panels/event-feed`, new
`PANEL_SOURCES` entry), `tools/dashboard/templates/panels/event_feed.html`
(new) + `index.html` embedding as the last panel,
`tools/dashboard/static/css/app.css` additive (event-feed list styles),
`backtest/test_dashboard_event_feed.py` new, `CHANGELOG.md` entry, on
branch `worktree-feat+t-2026-cu-9050-161`.
**Forbidden:** any POST/PUT/mutation endpoint for annotations,
`dashboard.py` (old dashboard), `.env*`/secrets, live DB, fleet restart,
model artifacts, `core/**`, SPEC.md in the repo root, existing
`analytics_api` aggregate functions (`_outcomes_cte`, `_outcomes_cte_with_coin`,
`_regime_changes_in_window`, `bot_trade_rows`, `bot_leaderboard`,
`success_rate_timeseries`, `bot_regime_matrix`, `coins_with_trades`,
`coin_trade_series`, `overnight_digest`) rewritten in substance,
building live control, `--no-verify`, main/prod directly, push/PR
(orchestrator step).
**Ask first:** new runtime dependencies, changes to existing panel route
signatures from Features 1-8.

---

## Operations — Atomic export publish + scheduled tasks (T-2026-CU-9050-163)

The analytics export (`tools/analytics_export.py`) NEVER writes directly into
the served DuckDB (`staging_models/analytics/analytics.duckdb`), which the
dashboard opens read-only per request. Instead it runs RW on a persistent
**build DB** (`analytics.duckdb.build`, carries the watermark → keeps
incrementality exactly from the first run/seed) and **publishes atomically**:
`shutil.copy2(build, <served>.tmp)` →
`os.replace(<served>.tmp, served)` (atomic on the same volume). This means
the served path is never exclusively locked by the export → dashboard reads
no longer error out during a run. A Windows sharing violation on replace →
retries with a **~30s total budget** (`DEFAULT_PUBLISH_RETRIES=120` ×
`retry_delay_s=0.25`, CLI `--publish-retries`/`--publish-retry-delay`,
T-2026-CU-9050-167). The budget MUST be generous: the dashboard HTMX-polls
several panels and opens the served DB read-only per request, the old 1s
budget (T-163) never found a gap under live polling and the publish
consistently failed. Since every request closes its handle, the served
file is free >90% of the time → a wide window reliably hits a gap. If ALL
attempts fail, the build DB + `.tmp` stay intact, served untouched, exit
code ≠ 0 (no corruption risk) — **self-healing:** the next run republishes
the same fresh data from the build DB, a missed publish is never data loss,
only delayed. Pure publish logic: `publish_duckdb()` (DB-free testable,
`backtest/test_analytics_export_publish.py`).

Rollout seed: the switch to the build DB is the first split from the old
single-file layout. On the FIRST run under the new code, `main()` seeds
`analytics.duckdb.build` once from the existing served DB (`seed_build_db`),
if the build DB is missing but the served one exists → the
`_export_watermark` is preserved, no hours-long full re-export from the
live Postgres. The summary print runs AFTER the publish, so a publish
failure never looks like success (clear `publish PENDING` marking).

The two scheduled tasks (dashboard autostart @127.0.0.1:8098, export every
30 min) are registered reproducibly via
`tools/ops/register_kythera_dashboard_tasks.ps1`
(elevated, S4U, `IgnoreNew` = no overlapping export). The script is
REGISTRATION-ONLY — it stops no process and starts no task (no live
cutover from a committed artifact, CLAUDE.md hard rule 1); cutover +
registration are separate, deliberate operator steps, not part of a
dev session.

## Performance (T-2026-CU-9050-175)

The panel contexts cache their DuckDB-derived data (query payload +
`data_freshness` rows) behind the file-freshness token (`analytics_api._PollCache`,
the same pattern as the `/api/analytics/*` blueprint cache): with an
unchanged export file, every poll is served from memory. The "Sync vor N
min" age is still computed per request; the fleet registry (file-based)
stays uncached. Query-side: rolling series via SQL daily aggregation,
leaderboard via a streamed-column path (optional numpy fast path with a
pure-Python fallback), success-rate window in a single scan, regime
matrix as an ASOF inner join. Result parity is a HARD requirement — net:
`backtest/test_analytics_query_parity.py`. The three pure count/sum
aggregates (rolling / success-rate / regime matrix) are bit-identical
old-vs-new; for the leaderboard, SINCE T-2026-CU-9050-177 ALL fields are
deterministic (see below).

## Deterministic leaderboard risk metrics (T-2026-CU-9050-177)

The non-determinism class documented as open under T-175 is fixed: the
leaderboard query orders by `ORDER BY bot, closed_at, src, id` — `id` is
the monotonically increasing serial Postgres PK of each outcome table
(insertion order, the same column the export keyset cursor uses as a
uniqueness tiebreaker; the best DETERMINISTIC order the exported schema
gives), `src` the union-branch rank (needed because the id spaces of both
tables overlap — 371k collisions in the live export). The order is thus
TOTAL: `max_drawdown_pp`/`max_loss_streak` are reproducible run to run,
even under DuckDB parallelism (`connect_ro` PRAGMA threads=2; proof:
10/10 identical runs on the real DB, before that 23 of 68 bots
diverged), and numpy fast path ≡ pure fallback holds unconditionally
(identical deterministic row stream), no longer only on tie-free
fixtures.

**Limit (T-177 review):** `id` order is NO guarantee of true close
chronology where an upstream writer batch-stamps `closed_at`. A known
~340k-row legacy reclassify block in `closed_ai_signals` shares ONE exact
timestamp — there the `id` order is essentially arbitrary insertion order,
so the resulting risk metrics are deterministic order artefacts (now
stable + reproducible), not chronologically reliable (affects
ATS1/EPD1/MIS1-pump family, ~85-93% of their history). `open_time` as a
tiebreaker for the legacy status branch is a possible follow-up.
DELIBERATE BEHAVIOUR CHANGE: the previously randomness-afflicted display
values are now stable (pinned to the id-order sequence). Net:
`test_leaderboard_risk_metrics_deterministic_across_runs_with_ties` (red
before the fix: value-different duplicate `closed_at` rows, physically
stored outside the id order) + reference-pipeline parity on the same
fixture.

# SPEC — Z1 Dashboard Shell (Task 0, Fundament)

Task: T-2026-CU-9050-151 · Decision gate: D-2026-CLD-111 (z-council)
Stack (bindend): Flask + HTMX + Interval-Polling. Kein FastAPI, kein SPA, kein
Node-Build on-box. Charting via vendored JS (TradingView Lightweight Charts +
Apache ECharts) als statische Assets.

## Intent
Baue die tragende Shell des Z1-Dashboards: eine Flask-App-Factory die den
bestehenden `analytics_api`-Blueprint (T-131 DuckDB-Substrat) mountet, ein
responsives HTMX-Base-Layout, einen geteilten Chart-Lifecycle-JS-Helper (der
Kern-Deliverable — verhindert Canvas/WebGL/Listener-Leaks ueber die spaeteren 9
Panels), ein Polling-Pattern + EIN Demo-Panel (Erfolgsraten-Endpoint) als
End-to-End-Beweis, eine Datenstand-Badge-Basis und einen waitress-Entrypoint an
127.0.0.1. Alles DB-frei testbar. Das alte `dashboard.py` bleibt unangetastet.

## Akzeptanzkriterien (binaer testbar)
- [x] AK1: `tools/dashboard/app.create_app(duckdb_path)` liefert eine Flask-App,
  die den `analytics_api`-Blueprint mountet — `GET /api/analytics/success-rate`
  antwortet 200 gegen eine synthetische DuckDB. — Test: `test_json_api_mounted` ✅.
- [x] AK2: `GET /` liefert 200, das responsive Base-Layout (viewport-Meta),
  bindet HTMX + `chart_lifecycle.js` ein und enthaelt den Demo-Panel-Container
  mit `hx-get="/panels/success-rate"` und `hx-trigger` Polling (`every … s`). —
  Test: `test_index_renders_shell` ✅.
- [x] AK3: `GET /panels/success-rate` liefert 200 und rendert die
  Erfolgsraten-Felder (Bot-Tag + Winrate) aus `success_rate_timeseries` gegen die
  synthetische DuckDB als HTMX-Partial. — Test: `test_demo_panel_renders_winrate` ✅.
- [x] AK4: `GET /static/js/chart_lifecycle.js` liefert 200 und der Helper
  registriert Chart-Instanzen + ruft `dispose`/`remove` bei `htmx:beforeSwap`
  und Re-Init bei `htmx:afterSwap`. — Test: `test_chart_lifecycle_js_served` ✅.
- [x] AK5: Die Datenstand-Badge rendert "Stand HH:MM, Sync vor N min" aus den
  T-131-Freshness-Zeilen; die reine `freshness_summary`-Funktion berechnet das
  Alter STRIKT aus `synced_at` (UTC) — nie durch Mischung mit dem naive-local
  `last_row_ts`. — Test: `test_freshness_summary_*` + `test_index_shows_badge` ✅.
- [x] AK6: Der Serving-Entrypoint bindet an 127.0.0.1 (nie 0.0.0.0) und faehrt
  waitress im Prod-Pfad (P0.8-Lektion). — Test: `test_serve_defaults_to_localhost`
  + `test_serve_delegates_to_waitress_path` ✅ (zusaetzlich realer waitress-Smoke).
- [x] AK7: Kein Import und keine Panel-/API-Route triggert einen Postgres-
  Connect — der gesamte Lesepfad laeuft nur gegen DuckDB. — Test:
  `test_routes_never_touch_postgres` + `test_import_is_db_free` (Subprozess) ✅.

## Out of Scope
- Entfernen/Migrieren des alten `dashboard.py`.
- Auth / Mutations-Endpoints / Cloudflare-Access-Verdrahtung.
- Die 9 Feature-Panels selbst (nur EIN Demo-Panel als Shell-Beweis).
- SSE (Interval-Polling ist Default per D-2026-CLD-111).
- Voller Datenstand-Badge-Ausbau pro Panel (nur Basis-Version).

## Why Build (statt Reuse)
Die Shell ist projekt-spezifische Verdrahtung (analytics_api-Blueprint + HTMX +
vendored Charts + VPS-Serving-Contract). Keine OSS-Library liefert genau diese
Komposition. Substrat (analytics_export/analytics_api aus T-131) wird
WIEDERVERWENDET, nicht neu gebaut.

## Scope of consent
**Erlaubt:** `tools/dashboard/**` neu, `backtest/test_dashboard_shell.py` neu,
additive Blueprint-Extraktion in `tools/analytics_api.py` (verhaltenserhaltend,
durch bestehende Tests abgesichert), `CHANGELOG.md`-Eintrag, auf branch
`feat/t-2026-cu-9050-150`.
**Verboten:** `dashboard.py` (altes Dashboard), `.env*`/secrets, Live-DB,
Fleet-Restart, Modell-Artefakte, Bind an 0.0.0.0, `--no-verify`, main/prod
direkt.
**Frag zurueck:** neue Runtime-Dependencies (ausser flask/htmx/duckdb/waitress
die schon da sind), echte Vendor-JS-Beschaffung mit Netzwerkzugriff.

---

## Feature 3 — Erfolgsraten-Zeitvergleich-Panel (T-2026-CU-9050-155)

Task: T-2026-CU-9050-155 · baut auf T-131 (`success_rate_timeseries`) und
T-151 (Shell/Chart-Lifecycle) auf.

### Intent
Volle Zeitvergleich-Version des T-151-Demo-Panels: eine ECharts-Linien-
Zeitreihe der ROLLIERENDEN 7/30/90d-Winrate pro ausgewaehltem Bot ueber die
Zeit (nicht nur ein aktueller Balken), mit Bot-Multiselect und
Fenster-Umschalter. Neue Route `/panels/success-rate-timeseries` — kollidiert
NICHT mit der bestehenden `/panels/success-rate`-Demo (die bleibt unangetastet
fuer T-151s eigene Tests).

### Akzeptanzkriterien (binaer testbar)
- [x] AK1: `analytics_api.rolling_success_rate_series()` liefert pro Bot eine
  Zeitreihe der rollierenden `window`-Tage-Winrate, additiv zu
  `success_rate_timeseries` (nicht veraendert), gleiche DECISIVE-Trade-
  Definition via `bot_trade_rows`. — Test:
  `test_rolling_success_rate_series_multi_bot_diverges_per_window` ✅.
- [x] AK2: Rollierende 7/30/90d-Fenster liefern am selben Tag GENUINE
  unterschiedliche Werte (keine zufaellig identischen Fenster) — Test:
  `test_rolling_series_for_bot_windows_diverge_at_last_day` ✅.
- [x] AK3: Bot-Multiselect filtert die Zeitreihe korrekt (mehrere Bots ->
  mehrere Serien, ein Bot -> eine Serie). — Test:
  `test_panel_multiselect_two_bots_renders_two_series` +
  `test_panel_single_bot_selection_renders_one_series` ✅.
- [x] AK4: Explizite Leerauswahl (alle Checkboxen abgewaehlt) zeigt "Keine
  Bots ausgewaehlt" statt stillschweigend auf "alle Bots" zurueckzufallen —
  Test: `test_selected_bots_respects_explicit_empty_selection` +
  `test_panel_explicit_empty_selection_shows_message` ✅.
- [x] AK5: `GET /panels/success-rate-timeseries` rendert eine ECharts-
  Linien-Zeitreihe (`data-chart="winrate-timeseries"`), gemountet via
  `chart_lifecycle.js` (dispose/re-init bei htmx-Swap), Fenster-Umschalter
  (7/30/90d) als Formular. — Test:
  `test_panel_default_load_selects_all_bots_and_default_window` +
  `test_winrate_timeseries_factory_registered_in_panels_js` ✅.
- [x] AK6: Fenster-Umschaltung aendert die gerenderten Werte end-to-end (nicht
  nur auf Funktionsebene). — Test:
  `test_panel_window_switch_changes_rendered_values` ✅.
- [x] AK7: Kein Postgres-Zugriff, DB-frei testbar, kein Bruch der
  bestehenden `/panels/success-rate`-Demo. — Test:
  `test_panel_never_touches_postgres` +
  `test_existing_success_rate_demo_route_untouched` ✅.

### Out of Scope
- Live-Steuerung (Feature 4).
- Die anderen Panels (Fleet-Registry, Leaderboard).
- Aenderung/Umbau von `success_rate_timeseries` selbst (nur additive
  Erweiterung `rolling_success_rate_series`).
- Ein neuer `/api/analytics/success-rate-timeseries` JSON-Endpoint (die
  Panel-Route ruft die Analytics-Funktion direkt auf, wie die anderen
  Panel-Routen es tun — kein zusaetzlicher JSON-API-Endpunkt gefordert).

### Why Build (statt Reuse)
Rollierende Fenster-Zeitreihe + Bot-Multiselect + HTMX-Self-Update-Widget ist
projektspezifische Verdrahtung auf dem bestehenden T-131-Substrat; keine
Library liefert das. `success_rate_timeseries`/`bot_trade_rows` werden
wiederverwendet, nicht neu gebaut.

### Scope of consent
**Erlaubt:** `tools/dashboard/**` additiv, `tools/analytics_api.py` additiv
(neue Funktionen, bestehende unveraendert), `backtest/test_dashboard_success_rate_panel.py`
neu, `CHANGELOG.md`-Eintrag, auf branch `worktree-feat+t-2026-cu-9050-155`.
**Verboten:** `dashboard.py` (altes Dashboard), `.env*`/secrets, Live-DB,
Fleet-Restart, Modell-Artefakte, `success_rate_timeseries` inhaltlich
umschreiben, `--no-verify`, main/prod direkt, Push/PR (Orchestrator-Schritt).
**Frag zurueck:** neue Runtime-Dependencies, Aenderung der bestehenden
`/panels/success-rate`-Demo-Route/-Tests.

---

## Feature 4 — Datenstand-Indikator pro Panel (T-2026-CU-9050-156)

Task: T-2026-CU-9050-156 · baut additiv auf `freshness_summary()` (T-151) und
`analytics_export.data_freshness()` (T-131) auf.

### Intent
Heute zeigt EIN shell-globaler Badge (`_freshness_badge.html`, Base-Layout) den
Datenstand des JUENGSTEN Sync ueber ALLE Quellen. Dieses Feature macht den
Datenstand PANEL-SPEZIFISCH: jedes der vier Panels (`success-rate`,
`success-rate-timeseries`, `leaderboard`, `fleet-registry`) zeigt "Stand HH:MM,
Sync vor N min" NUR fuer die Quelle(n), die dieses Panel tatsaechlich liest —
und bei mehreren Quellen die AELTESTE (worst-case), nie eine fabrizierte
Mischung. Der globale Badge bleibt unveraendert bestehen (additive
Verfeinerung, kein Ersatz).

### Akzeptanzkriterien (binaer testbar)
- [x] AK1: `freshness_summary()` bekommt zwei additive optionale Parameter:
  `sources: Sequence[str] | None` (filtert die Zeilen VOR der Aggregation auf
  die genannten Quellennamen) und `worst_case: bool = False` (aggregiert bei
  `True` die AELTESTE statt der (bisherigen Default-)FRISCHESTEN Quelle —
  der shell-globale Badge fragt "lebt die Pipeline ueberhaupt", ein
  Panel-Badge muss dagegen worst-case zeigen). Beide Defaults reproduzieren
  exakt das bisherige Verhalten (alle bestehenden Tests bleiben gruen, keine
  Signatur-Bruchstelle). — Test:
  `test_freshness_summary_sources_filter_narrows_rows`,
  `test_freshness_summary_worst_case_picks_oldest` +
  alle bestehenden `test_freshness_summary_*` unveraendert gruen.
- [x] AK2: Neue reine Funktion `panel_freshness(rows, panel, *, now_utc=None)`
  loest ueber `PANEL_SOURCES[panel]` die Quellen des Panels auf und delegiert
  an `freshness_summary(rows, sources=..., now_utc=..., worst_case=True)`.
  Panels mit `PANEL_SOURCES[panel] == ()` (aktuell nur `fleet-registry`,
  dateibasiert — kein DuckDB-Sync) liefern `FILE_BASED_FRESHNESS` statt einer
  fabrizierten Zeit. Ein unbekannter Panel-Name wirft `ValueError` (keine
  stille Fallback-Vertuschung einer falschen Zuordnung). — Test:
  `test_panel_freshness_leaderboard_and_success_rate_share_sources`,
  `test_panel_freshness_fleet_registry_is_file_based`,
  `test_panel_freshness_unknown_panel_raises`.
- [x] AK3: Zwei Quellen mit UNTERSCHIEDLICHEM `synced_at` fuer dasselbe Panel
  ergeben die AELTERE (kleinere) Freshness — nie der Durchschnitt, nie die
  juengere, unabhaengig davon WELCHE der beiden Quellen die staler ist. —
  Test: `test_panel_freshness_oldest_source_wins_regardless_of_which_is_stale`.
- [x] AK4: Fehlt fuer die Panel-Quelle(n) jede Freshness-Zeile (leeres
  Ergebnis nach dem Quellenfilter), rendert das Panel-Badge-Partial `—`
  statt eines fabrizierten Zeitstempels. — Test:
  `test_panel_freshness_badge_partial_missing_shows_dash`.
- [x] AK5: Die Panel-Templates `success_rate.html`,
  `success_rate_timeseries.html`, `leaderboard.html`, `fleet_registry.html`
  binden das neue parametrisierte Badge-Partial
  `_panel_freshness_badge.html` (nimmt die panel-lokale `freshness`-Variable)
  ein, END-TO-END ueber die realen Routen `GET /panels/{success-rate,
  success-rate-timeseries, leaderboard, fleet-registry}` gegen eine echte
  `AnalyticsExporter`/DuckDB-Fixture. — Test:
  `test_leaderboard_panel_route_renders_own_freshness`
  (Integrationstest, echte Exporter→DuckDB→Route→HTML-Kette).
- [x] AK6: Age bleibt STRIKT aus `synced_at` (UTC) berechnet, nie aus
  `last_row_ts` (naive-local) — geerbt von `freshness_summary`, per
  Mutation-Check erneut belegt (ein Swap auf `last_row_ts` macht den Test
  rot). — Test: `test_panel_freshness_age_from_synced_at_not_last_row_ts`.

### Out of Scope
- Live-Steuerung (kein Auto-Refresh-Button, kein manueller Re-Sync-Trigger).
- Funktionaler Neubau der vier Panels selbst (nur additive Badge-Einbettung).
- Entfernen des shell-globalen Badges (`_freshness_badge.html`/`base.html`
  bleiben unangetastet).
- Ein neuer `/panels/freshness/<panel>`-JSON-Endpoint — der Badge wird
  serverseitig als Teil des jeweiligen Panel-Fragments mitgerendert und
  aktualisiert sich mit dessen bestehendem Poll-Intervall (kein zusaetzlicher
  HTMX-Round-Trip).

### Why Build (statt Reuse)
Panel→Quelle-Zuordnung + Oldest-wins-Aggregation ist projektspezifische
Verdrahtung auf dem bestehenden T-131/T-151-Substrat; keine Library liefert
das. `freshness_summary()` wird additiv erweitert (neuer optionaler Parameter,
Default-Pfad unveraendert), nicht umgeschrieben.

### Scope of consent
**Erlaubt:** `tools/dashboard/app.py` additiv (neuer Parameter an
`freshness_summary`, neue Funktionen/Konstanten), `tools/dashboard/templates/**`
additiv (neues Partial + Einbettung in die vier Panel-Templates),
`backtest/test_dashboard_freshness.py` neu, `CHANGELOG.md`-Eintrag, auf branch
`worktree-feat+t-2026-cu-9050-156`.
**Verboten:** `dashboard.py` (altes Dashboard), `.env*`/secrets, Live-DB,
Fleet-Restart, Modell-Artefakte, `core/**`, Entfernen/Umschreiben des
bestehenden globalen Badges oder von `freshness_summary`s bisherigem
Rueckgabewert bei `sources=None`, `--no-verify`, main/prod direkt, Push/PR
(Orchestrator-Schritt).
**Frag zurueck:** neue Runtime-Dependencies, Aenderung der bestehenden
Panel-Routen-Signaturen/-Tests aus Feature 1-3.

---

## Feature 5 — Globaler Erfolgs-Metrik-Toggle (T-2026-CU-9050-157)

Task: T-2026-CU-9050-157 · baut additiv auf T-154 (`bot_leaderboard`/
`_LEADERBOARD_SORT_KEYS`) und T-151 (Shell) auf.

### Intent
Ein shell-globaler Erfolgs-Metrik-Toggle (Winrate / Expectancy / Netto-PnL)
im Base-Layout bestimmt, welche Kennzahl die Panels hervorheben. Cross-cutting
via `?metric=`-Query-Param, den das Leaderboard-Panel liest: die gewaehlte
Metrik wird als hervorgehobene Spalte gezeigt UND als Default-Sort verwendet
(`metric`→`sort_by`: winrate→winrate, expectancy→expectancy_pct,
netto-pnl→pnl_sum_pct). Sinnvoller Default netto-pnl (= die bestehende
`DEFAULT_LEADERBOARD_SORT`). Unbekannter `metric`-Wert faellt still auf den
Default zurueck (kein 500). Panels, die die Metrik nicht kennen, ignorieren
den Toggle unschaedlich.

### Akzeptanzkriterien (binaer testbar)
- [x] AK1: Reine Mapping-Logik `resolve_metric(raw)` (unbekannt/None →
  `DEFAULT_METRIC`) und `metric_sort_by(metric)` (→ ein Key aus
  `analytics_api._LEADERBOARD_SORT_KEYS`), Flask-/DuckDB-frei testbar. —
  Test: `test_resolve_metric_*`, `test_metric_sort_by_maps_onto_leaderboard_sort_keys`,
  `test_metric_sort_by_unresolved_value_falls_back_to_default_sort_by`.
- [x] AK2: Alle drei Metriken + Default: `GET /panels/leaderboard?metric=…`
  sortiert nach der gemappten Metrik. Fixture rankt dieselben drei Bots in
  DREI verschiedenen Reihenfolgen → ein falsches/ignoriertes Mapping rendert
  eine der ANDEREN Reihenfolgen (Mutation-Check). — Test:
  `test_leaderboard_panel_metric_{winrate,expectancy,netto_pnl}_*`.
- [x] AK3: Unbekannter `metric`-Wert → Default (kein 500), Route 200. — Test:
  `test_leaderboard_panel_unknown_metric_falls_back_to_default_no_500`,
  `test_index_unknown_metric_query_param_falls_back_no_500`.
- [x] AK4: Der Shell-Toggle (`base.html`) rendert die drei Optionen, markiert
  die aktive, und der resolvte Wert wird in die eigene hx-get-URL des
  Leaderboard-Panels gebacken, sodass Load + Poll dieselbe Metrik behalten. —
  Test: `test_index_renders_metric_toggle_with_default_active`,
  `test_index_metric_query_param_selects_active_toggle_option`.
- [x] AK5: Die gewaehlte Metrik-Spalte wird im Leaderboard hervorgehoben
  (`metric-highlight`), konsistent mit dem Sort. — Test:
  `test_leaderboard_panel_metric_winrate_reorders_and_highlights`.
- [x] AK6: Kein Postgres-Zugriff, DB-frei testbar. — Test:
  `test_toggle_never_touches_postgres`.

### Out of Scope
- Live-Steuerung (Feature 4).
- Die anderen Panels neu bauen — sie erben den Toggle nur unschaedlich
  (Fleet-Registry/Erfolgsrate/Zeitvergleich ignorieren `metric`).
- Ein neuer JSON-API-Endpoint fuer den Toggle (der `/api/analytics/leaderboard`
  akzeptiert `sort_by` bereits direkt).

### Eingefaltete Review-Nit-Cleanups (dieser Task fasst app.py/CSS/Leaderboard-Test ohnehin an)
- CSS-Token-Hygiene: eigenes `--loss`-Token fuer `.pnl-negative` (statt des
  `--stale`-Freshness-Tokens); `--live` (byte-identisch zu `--accent`)
  entfernt, `var(--accent)` direkt genutzt. Rein kosmetisch, kein visueller
  Bruch.
- Namens-Kollision: Modul-Funktion `panel_freshness()` → `panel_freshness_summary()`
  (kollidierte mit dem nested Route-Handler `def panel_freshness()` in
  `create_app()`); alle vier Panel-Context-Caller + Freshness-Tests angepasst,
  verhaltenserhaltend.
- Test-Luecke (T-154-MEDIUM): `sort_by="winrate"` und `sort_by="n"` mit
  divergenter Fixture (Reihenfolge ≠ pnl-Default) — ein ignorierter `sort_by`
  wird jetzt rot.

### Why Build (statt Reuse)
Shell-globaler Metrik-Toggle + Panel-Highlight/Sort-Kopplung ist
projektspezifische Verdrahtung auf dem bestehenden T-131/T-154-Substrat;
keine Library liefert das. `bot_leaderboard` wird wiederverwendet (via seinem
bereits vorhandenen `sort_by`-Parameter), nicht neu gebaut.

### Scope of consent
**Erlaubt:** `tools/dashboard/app.py` additiv (neue Konstanten/Funktionen +
`metric`-Param an `_leaderboard_context`/den Routen), `tools/dashboard/templates/**`
additiv (Toggle in `base.html`, `metric` in `index.html`+`leaderboard.html`),
`tools/dashboard/static/css/app.css` (Toggle-/Highlight-Styles + `--loss`/`--live`-
Cleanup), `backtest/test_dashboard_metric_toggle.py` neu, Ergaenzungen in
`backtest/test_dashboard_leaderboard.py`/`test_dashboard_freshness.py` (Rename),
`CHANGELOG.md`-Eintrag, auf branch `worktree-feat+t-2026-cu-9050-157`.
**Verboten:** `dashboard.py` (altes Dashboard), `.env*`/secrets, Live-DB,
Fleet-Restart, Modell-Artefakte, `core/**`, SPEC.md im Repo-Root, `--no-verify`,
main/prod direkt, Push/PR (Orchestrator-Schritt).
**Frag zurueck:** neue Runtime-Dependencies, Aenderung bestehender
Panel-Routen-Signaturen aus Feature 1-4 ueber den additiven `metric`-Param
hinaus.

---

## Feature 6 — Bot x Regime Performance-Heatmap (T-2026-CU-9050-158)

Task: T-2026-CU-9050-158 · baut additiv auf T-131 (`regime_history`-Export,
`_outcomes_cte`/`_bot_filter`) und T-151 (Shell/Chart-Lifecycle) auf.

### Intent
Eine ECharts-Heatmap: Zeilen = Bots, Spalten = Regime-Zustaende
(`regime_history.regime`), Zell-Wert = Performance des Bots IN diesem Regime
(Winrate oder Ø-PnL/Trade, umschaltbar, klar gelabelt). Fuer jede
(Bot, Regime)-Zelle zaehlen die DECISIVEN Trades des Bots, deren `closed_at`
in das Zeitfenster faellt, in dem dieser Regime-Zustand aktiv war — ein ASOF-
Join gegen den `regime_history`-Log (append-only, ein Regime gilt ab seinem
`ts` bis zum naechsten Log-Eintrag). Zellen ohne Trades bleiben leer ("—"),
nie fabriziert. Trades, deren `closed_at` VOR dem ersten je klassifizierten
Regime liegt, koennen keinem Fenster zugeordnet werden und werden aus der
Matrix ausgeschlossen (nicht in eine "UNKNOWN"-Spalte gebucht).

### Akzeptanzkriterien (binaer testbar)
- [x] AK1: `analytics_api.bot_regime_matrix()` liefert additiv
  `{bots, regimes, cells: {bot: {regime: {n, wins, winrate, pnl_sum_pct,
  expectancy_pct}}}}` — wiederverwendet `_outcomes_cte`/`_bot_filter` (dieselbe
  DECISIVE-Trade-Definition wie `bot_trade_rows`/`success_rate_timeseries`,
  unveraendert). — Test: `test_bot_regime_matrix_assigns_trades_to_active_regime_window`.
- [x] AK2: Die Bot-Regime-Zuordnung ist ein ASOF-Join (`closed_at >= ts`, letzter
  `regime_history`-Eintrag VOR/AN dem Trade-Zeitpunkt) — ein Trade auf der
  Regime-Grenze faellt in das NEUE Fenster, nicht ins alte; ein falsch
  gerichteter Join (Mutation-Check) macht die Zell-Werte nachweisbar falsch. —
  Test: `test_bot_regime_matrix_boundary_trade_joins_new_regime_window`
  (Mutation-Check).
- [x] AK3: Zellen ohne Trades erscheinen nicht in `cells` (kein fabrizierter
  Nullwert); ein Bot mit Trades in nur EINEM von mehreren Regimes hat nur
  diesen einen Eintrag. — Test: `test_bot_regime_matrix_missing_cell_absent_not_fabricated`.
- [x] AK4: Trades vor dem ersten `regime_history`-Eintrag werden aus der Matrix
  ausgeschlossen (kein "UNKNOWN"-Bucket). — Test:
  `test_bot_regime_matrix_trade_before_first_regime_row_excluded`.
- [x] AK5: `GET /panels/regime-heatmap` rendert 200, eine ECharts-Heatmap
  (`data-chart="bot-regime-heatmap"`, gemountet via `chart_lifecycle.js`) +
  eine Tabellen-Fallback-Ansicht, mit Metrik-Umschalter (Winrate/Ø-PnL) und
  Datenstand-Badge (Quellen `regime_history` + `closed_ai_signals`), END-TO-END
  gegen eine echte `AnalyticsExporter`/DuckDB-Fixture mit mehreren Bots x
  mehreren Regimes. — Test:
  `test_panel_regime_heatmap_renders_correct_cell_values` (Integrationstest).
- [x] AK6: Kein Postgres-Zugriff, DB-frei testbar, leere Regime_history/leere
  Outcome-Tabellen degradieren sauber (leere Matrix, kein 500). — Test:
  `test_panel_regime_heatmap_never_touches_postgres`,
  `test_bot_regime_matrix_empty_substrate_degrades_gracefully`.

### Out of Scope
- Live-Steuerung (Feature 4-Familie).
- Die anderen Panels neu bauen.
- Schreiben von `regime_history` (nur Lesepfad).
- markArea-Regime-Baender-Overlays auf ANDEREN Panels (nur die Heatmap selbst).
- Ein neuer `/api/analytics/*`-JSON-Endpoint (die Panel-Route ruft
  `bot_regime_matrix()` direkt auf, wie die anderen additiven Panel-Routen
  seit Feature 3 es tun).

### Why Build (statt Reuse)
Bot x Regime-ASOF-Join + Heatmap-Verdrahtung auf dem bestehenden T-131/T-151-
Substrat ist projektspezifisch; keine Library liefert das. `_outcomes_cte`/
`_bot_filter`/`_existing_outcome_tables` werden wiederverwendet, nicht neu
gebaut; DuckDB liefert `ASOF JOIN` nativ (>= 1.5, hier verifiziert 1.5.4).

### Scope of consent
**Erlaubt:** `tools/analytics_api.py` additiv (neue Funktion(en), bestehende
unveraendert), `tools/dashboard/app.py` additiv (neue Konstanten/Funktionen +
Route + `PANEL_SOURCES`-Eintrag), `tools/dashboard/templates/**` additiv (neues
Partial `panels/regime_heatmap.html` + Einbindung in `index.html`),
`tools/dashboard/static/js/panels.js` additiv (neue ECharts-Factory),
`tools/dashboard/static/css/app.css` additiv (Heatmap-Styles),
`backtest/test_dashboard_regime_heatmap.py` neu, `CHANGELOG.md`-Eintrag, auf
branch `worktree-feat+t-2026-cu-9050-158`.
**Verboten:** `dashboard.py` (altes Dashboard), `.env*`/secrets, Live-DB,
Fleet-Restart, Modell-Artefakte, `core/**`, SPEC.md im Repo-Root, bestehende
`analytics_api`-Aggregatfunktionen inhaltlich umschreiben, `--no-verify`,
main/prod direkt, Push/PR (Orchestrator-Schritt).
**Frag zurueck:** neue Runtime-Dependencies, Aenderung bestehender
Panel-Routen-Signaturen aus Feature 1-5.

---

## Feature 7 — Coin-Drilldown mit Ebenen-Kette (T-2026-CU-9050-159, Q11)

Task: T-2026-CU-9050-159 · baut additiv auf T-131 (`_outcomes_cte`/`_bot_filter`/
`_existing_outcome_tables`) und T-151 (Shell/Chart-Lifecycle, vendored
Lightweight Charts 4.2.3) auf.

### Intent
Eine Ebenen-Kette: Coin-Selektor (listet nur Coins mit mindestens einem
DECISIVEN Trade) -> das Panel zeigt fuer den gewaehlten Coin (1) eine
Lightweight-Charts Preislinie (Entry->Exit-Punkte je Trade, verbunden in
Close-Zeit-Reihenfolge) mit Win/Loss-farbigen Trade-Markern und (2) eine
kompakte Trade-Tabelle (Close-Zeit, Bot/Modell, Richtung, Entry, Exit, PnL,
Target-Hit).

**SCOPING (bindend):** Volle OHLCV-Kerzen sind NICHT Teil dieses Features —
der 25GB-Kerzen-Export wurde in T-131 vertagt und liegt nicht im
DuckDB-Substrat. Das Panel rendert stattdessen die Preis-PFAD-Linie durch die
Entry/Exit-Punkte der DECISIVEN Trades selbst (aus `closed_ai_signals`/
`closed_trades`) — keine echten Marktkerzen. Dokumentiert als Follow-up (siehe
"Out of Scope" unten + CHANGELOG.md).

### Akzeptanzkriterien (binaer testbar)
- [x] AK1: `analytics_api.coins_with_trades()` liefert die sortierte Liste der
  Coins/Symbole mit mindestens einem DECISIVEN Trade (Trades ohne PnL /
  Housekeeping-Status zaehlen nicht) — additive Coin-aware CTE
  (`_outcomes_cte_with_coin`), dieselben `MICRO_PNL_PCT`/`MAX_ABS_PNL_PCT`-
  Schwellen wie `_outcomes_cte`. — Test: `test_coins_with_trades_lists_only_decisive_coins`.
- [x] AK2: `analytics_api.coin_trade_series(con, symbol)` liefert die nach
  `closed_at` aufsteigend sortierten DECISIVEN Trades EINES Coins
  (`{bot, direction, closed_at, entry, close_price, targets_hit, pnl_pct,
  is_win}`); `targets_hit` ist `None` fuer eine `closed_trades`-Zeile (die
  Tabelle hat keine solche Spalte) statt einer fabrizierten 0. — Test:
  `test_coin_trade_series_returns_ordered_decisive_trades_for_one_coin`.
- [x] AK3: Ein falscher Coin-Filter (Mutation-Check: Query auf einen ANDEREN
  Coin als den gewaehlten) liefert eine ANDERE Trade-Menge — belegt, dass der
  Filter tatsaechlich verdrahtet ist. — Test:
  `test_coin_trade_series_wrong_coin_filter_yields_different_trades` (Mutation-Check).
- [x] AK4: Unbekannter oder leerer Coin (nicht in `coins_with_trades()`)
  liefert `{"coin": symbol, "trades": []}` statt eines Fehlers oder aller
  Trades. — Test: `test_coin_trade_series_unknown_coin_returns_empty`.
- [x] AK5: `GET /panels/coin-drilldown` rendert 200, den Coin-Selektor (nur
  Coins mit Trades), eine Lightweight-Charts Preislinie
  (`data-chart="coin-price-line"`) mit Win/Loss-Markern und die Trade-Tabelle,
  END-TO-END gegen eine echte `AnalyticsExporter`/DuckDB-Fixture mit mehreren
  Coins x mehreren Trades. — Test: `test_panel_coin_drilldown_renders_correct_series_and_table`
  (Integrationstest).
- [x] AK6: Kein Coin ausgewaehlt/unbekannter Coin degradiert sauber (kein 500,
  Hinweistext statt Chart/Tabelle); leeres Substrat (keine Trades ueberhaupt)
  ebenso. — Test: `test_panel_coin_drilldown_unknown_coin_shows_clean_message`,
  `test_panel_coin_drilldown_empty_substrate`.
- [x] AK7: Lightweight-Charts-Factory `coin-price-line` disposed via
  `chart.remove()` (NICHT ECharts `.dispose()`), via `chart_lifecycle.js`
  registriert. — Test: `test_coin_price_line_factory_registered_in_panels_js`.
- [x] AK8: Kein Postgres-Zugriff, DB-frei testbar. — Test:
  `test_panel_coin_drilldown_never_touches_postgres`.

### Out of Scope
- Volle OHLCV-Kerzen (Candlesticks) — FOLLOW-UP, gated auf den Kerzen-Export
  aus T-131 (25GB, vertagt). Sobald der Export existiert, kann die
  Preislinie durch eine echte Lightweight-Charts Candlestick-Series ersetzt
  werden.
- Die anderen Panels neu bauen.
- Ein neuer `/api/analytics/*`-JSON-Endpoint (die Panel-Route ruft
  `coin_trade_series()`/`coins_with_trades()` direkt auf, wie die anderen
  additiven Panel-Routen seit Feature 3 es tun).
- Mehrere Coins gleichzeitig im Chart (nur EIN Coin pro Panel-Zustand, wie vom
  Q11-Kuratierungstext gefordert).

### Why Build (statt Reuse)
Coin-Level-Drilldown auf dem bestehenden T-131-Substrat + eine
Lightweight-Charts-Preislinie mit Trade-Markern ist projektspezifische
Verdrahtung; keine Library liefert das. `_outcomes_cte`/`_bot_filter`/
`_existing_outcome_tables` bleiben unveraendert (Feature 2/3/6 haengen davon
ab) — die Coin-Variante ist eine eigene, additive CTE mit derselben
Decisive-Definition (identische Schwellen-Konstanten).

### Scope of consent
**Erlaubt:** `tools/analytics_api.py` additiv (neue Funktion(en)
`coins_with_trades`/`coin_trade_series`/`_outcomes_cte_with_coin`, bestehende
Funktionen unveraendert), `tools/dashboard/app.py` additiv (neue Route
`/panels/coin-drilldown`, neue Kontext-Funktion(en), `PANEL_SOURCES`-Eintrag),
`tools/dashboard/templates/panels/coin_drilldown.html` (neu) +
`index.html`-Einbindung, `tools/dashboard/static/js/panels.js` additiv (neue
Lightweight-Charts-Factory `coin-price-line`), `backtest/test_dashboard_coin_drilldown.py`
neu, `CHANGELOG.md`-Eintrag, auf branch `worktree-feat+t-2026-cu-9050-159`.
**Verboten:** `dashboard.py` (altes Dashboard), `.env*`/secrets, Live-DB,
Fleet-Restart, Modell-Artefakte, `core/**`, SPEC.md im Repo-Root, bestehende
`analytics_api`-Aggregatfunktionen (`_outcomes_cte`/`bot_trade_rows`/
`bot_leaderboard`/`success_rate_timeseries`/`bot_regime_matrix`) inhaltlich
umschreiben, volle OHLCV-Kerzen bauen, `--no-verify`, main/prod direkt,
Push/PR (Orchestrator-Schritt).
**Frag zurueck:** neue Runtime-Dependencies, Aenderung bestehender
Panel-Routen-Signaturen aus Feature 1-6.

---

## Feature 8 — Overnight-Digest-Startseite (T-2026-CU-9050-160, F1)

Task: T-2026-CU-9050-160 · baut additiv auf T-131 (`_outcomes_cte_with_coin`/
`_bot_filter`/`_existing_outcome_tables_with_coin`, Feature 7) und
`_regime_history_present` (Feature 6) auf.

### Intent
Eine Digest-/Zusammenfassungs-Sektion GANZ OBEN auf der Startseite: fuer ein
konfigurierbares Fenster (Default "Overnight" = 8h, umschaltbar 8h/24h/7 Tage
via `?window=`) auf einen Blick — aggregierte Netto-PnL (Σ%), Trade-Count,
Gesamt-Win-Rate, Top-/Flop-Bot (nach PnL-Summe), groesster Win/Loss
(Coin+Bot+PnL) und (falls das Substrat `regime_history` traegt) die Anzahl
echter Regime-WECHSEL im Fenster (nicht blosse Log-Zeilen). Das Fenster
verankert sich wie `success_rate_timeseries`/`rolling_success_rate_series` NIE
an einer UTC-"jetzt"-Wanduhr, sondern an `max(closed_at)` im Substrat selbst
(`as_of`) — das haelt die Fensterberechnung strikt in derselben naive-local
Zeitrechnung wie die `closed_at`-Spalten selbst (TZ-Kontrakt: keine
Vermischung mit einer echten UTC-Uhr, siehe analytics_export TIMEZONE-Note).
Ein leeres Fenster (keine Trades) zeigt "Keine Trades im Fenster", nie einen
500er oder fabrizierte Nullwerte.

### Akzeptanzkriterien (binaer testbar)
- [x] AK1: `analytics_api.overnight_digest(con, window_hours, *, as_of=None,
  bots=None)` liefert additiv `{as_of, window_hours, n, wins, pnl_sum_pct,
  winrate, top_bot, flop_bot, best_trade, worst_trade, regime_changes}` —
  wiederverwendet die coin-aware CTE aus Feature 7
  (`_outcomes_cte_with_coin`/`_existing_outcome_tables_with_coin`), dieselbe
  DECISIVE-Trade-Definition wie ueberall sonst. `as_of=None` loest sich auf
  `max(closed_at)` im Substrat auf (data-anchored, nie wall-clock-anchored). —
  Test: `test_overnight_digest_basic_aggregates`.
- [x] AK2: Fenstergrenze ist `closed_at > as_of - INTERVAL window_hours HOUR
  AND closed_at <= as_of` (halboffen, identisches Muster wie
  `success_rate_timeseries`) — ein Trade GENAU auf der unteren Grenze ist
  ausgeschlossen, ein Trade knapp innerhalb ist eingeschlossen. Ein Trade
  ausserhalb des Fensters (aelter) darf weder PnL-Summe/Count noch Top-/
  Flop-Bot beeinflussen — ein Mutation-Check (Fenster-Filter entfernt/verkehrt)
  macht `pnl_sum_pct`/`n` nachweisbar falsch. — Test:
  `test_overnight_digest_window_boundary_excludes_outside_trade` (Mutation-Check).
- [x] AK3: Top-Bot (hoechste Summen-PnL im Fenster) und Flop-Bot (niedrigste)
  werden korrekt sortiert ermittelt — Fixture mit 3 Bots in eindeutiger
  Reihenfolge, eine falsche/vertauschte Sortierung macht den Test rot
  (Mutation-Check). — Test: `test_overnight_digest_top_and_flop_bot_correct`
  (Mutation-Check).
- [x] AK4: `best_trade`/`worst_trade` (groesster Win/Loss) tragen `{bot, coin,
  pnl_pct, closed_at}` des tatsaechlichen Extremwerts im Fenster. — Test:
  `test_overnight_digest_notable_trades_correct`.
- [x] AK5: Leeres Fenster (kein Trade in der `window_hours`-Spanne, aber
  Substrat hat Daten ausserhalb) liefert `n=0`, `pnl_sum_pct=None`,
  `winrate=None`, `top_bot=None`, `flop_bot=None`, `best_trade=None`,
  `worst_trade=None` — nie ein Fehler, nie eine fabrizierte 0. Komplett leeres
  Substrat (keine Outcome-Tabelle) degradiert identisch. — Test:
  `test_overnight_digest_empty_window_degrades_cleanly`,
  `test_overnight_digest_empty_substrate_degrades_cleanly`.
- [x] AK6: `regime_changes` zaehlt ECHTE Regime-UEBERGAENGE (Wert != Vorgaenger-
  Wert in `regime_history`, per `LAG`-Fenster) deren `ts` im Fenster liegt —
  nicht blosse Log-Zeilen (ein Append ohne Wertaenderung zaehlt nicht). Fehlt
  `regime_history` im Substrat, ist `regime_changes=None` (nie fabriziert). —
  Test: `test_overnight_digest_regime_changes_counts_real_transitions_only`,
  `test_overnight_digest_regime_changes_none_without_regime_history`.
- [x] AK7: `GET /panels/overnight-digest` (und `?window=8h|24h|168h`) rendert
  200: Kennzahl-Kacheln (PnL/Count/Win-Rate), Top-/Flop-Bot, Notable-Trades und
  einen Fenster-Umschalter, END-TO-END gegen eine echte
  `AnalyticsExporter`/DuckDB-Fixture, ganz OBEN in `index.html` eingehaengt
  (vor Fleet-Registry). Datenstand-Badge (`closed_ai_signals`/`closed_trades`/
  `regime_history`). — Test:
  `test_panel_overnight_digest_renders_correct_values` (Integrationstest),
  `test_index_includes_digest_panel_above_fleet_registry`.
- [x] AK8: Kein Postgres-Zugriff, DB-frei testbar; unbekannter/fehlender
  `?window=`-Wert faellt still auf den Default (8h) zurueck (kein 500). —
  Test: `test_panel_overnight_digest_never_touches_postgres`,
  `test_resolve_digest_window_unknown_value_falls_back_to_default`.

### Out of Scope
- Live-Steuerung (Feature 4-Familie/F4).
- Entscheidungsfertige Notifications (M5 = Phase 2).
- Die anderen Panels neu bauen.
- Ein Sparkline-Chart (bewusst weggelassen — Kacheln/Listen reichen fuer den
  Digest; kein neuer ECharts-Factory-Eintrag noetig).
- Ein neuer `/api/analytics/*`-JSON-Endpoint (die Panel-Route ruft
  `overnight_digest()` direkt auf, wie die anderen additiven Panel-Routen seit
  Feature 3 es tun).

### Why Build (statt Reuse)
Fenster-Digest-Aggregation (Top/Flop-Bot, Notable Trades, Regime-Transitions)
auf dem bestehenden T-131-Substrat ist projektspezifische Verdrahtung; keine
Library liefert das. `_outcomes_cte_with_coin`/`_bot_filter`/
`_existing_outcome_tables_with_coin`/`_regime_history_present` werden
wiederverwendet, nicht neu gebaut.

### Scope of consent
**Erlaubt:** `tools/analytics_api.py` additiv (neue Funktion(en)
`overnight_digest`/`_regime_changes_in_window`, bestehende Funktionen
unveraendert), `tools/dashboard/app.py` additiv (neue Konstanten/Funktionen +
Route `/panels/overnight-digest`, neuer `PANEL_SOURCES`-Eintrag),
`tools/dashboard/templates/panels/overnight_digest.html` (neu) +
`index.html`-Einbindung GANZ OBEN, `tools/dashboard/static/css/app.css`
additiv (Kachel-/Spalten-Styles), `backtest/test_dashboard_digest.py` neu,
`CHANGELOG.md`-Eintrag, auf branch `worktree-feat+t-2026-cu-9050-160`.
**Verboten:** `dashboard.py` (altes Dashboard), `.env*`/secrets, Live-DB,
Fleet-Restart, Modell-Artefakte, `core/**`, SPEC.md im Repo-Root, bestehende
`analytics_api`-Aggregatfunktionen (`_outcomes_cte`, `_outcomes_cte_with_coin`,
`bot_trade_rows`, `bot_leaderboard`, `success_rate_timeseries`,
`bot_regime_matrix`, `coins_with_trades`, `coin_trade_series`) inhaltlich
umschreiben, Live-Steuerung/Notifications bauen, `--no-verify`, main/prod
direkt, Push/PR (Orchestrator-Schritt).
**Frag zurueck:** neue Runtime-Dependencies, Aenderung bestehender
Panel-Routen-Signaturen aus Feature 1-7.

---

## Feature 9 — Event-Annotations als READ-ONLY Event-Feed (T-2026-CU-9050-161, S10)

Task: T-2026-CU-9050-161 · baut additiv auf `_regime_changes_in_window`
(Feature 8, lag-Logik) und `_outcomes_cte_with_coin`/
`_existing_outcome_tables_with_coin`/`_bot_filter` (Feature 7/8) auf. Letztes
Panel des Z1-Dashboard-Rewrites.

### Intent
S10 ist ein "einfaches Eingriffs-Log", kein Annotations-EDITOR: ein
chronologischer (neueste zuerst) Event-Feed, der notable Events aus den
VERFUEGBAREN DuckDB-Quellen konsolidiert + typisiert anzeigt — Regime-
Uebergaenge aus `regime_history` (Zeitpunkt + von->nach, via dieselbe
lag-Logik wie `_regime_changes_in_window`) und Notable Trades aus
`closed_ai_signals`/`closed_trades` (groesste Wins/Losses des Fensters —
Coin, Bot, PnL, Close-Zeit). Konfigurierbares Fenster (`?window=`, Default
24h, Alternative 168h/7 Tage). Ein SCHREIBENDES Annotations-Feature waere ein
Mutations-Endpoint = F4-/Z2-gegated (CLAUDE.md harte Regel: keine Mutationen/
Live-Hebel in der Web-UI vor Cloudflare Access) — deshalb bewusst READ-ONLY,
kein POST/Write-Endpoint gebaut.

### Akzeptanzkriterien (binaer testbar)
- [x] AK1: `analytics_api.event_feed(con, window_hours, *, as_of=None,
  bots=None)` liefert additiv `{as_of, window_hours, events}` mit
  `events: [{type, ts, title, detail}, ...]`, chronologisch ABSTEIGEND
  sortiert (neueste zuerst). `as_of=None` loest sich data-anchored auf
  (`max(closed_at)` ueber die Outcome-Tabellen, sonst `max(ts)` aus
  `regime_history`, sonst `None`) — nie eine wall-clock "jetzt"-Uhr. — Test:
  `test_event_feed_basic_shape_and_sort_order` (Mutation-Check: asc statt
  desc sortiert macht den Test rot).
- [x] AK2: Regime-Uebergaenge (`type="regime_change"`) sind ECHTE Wechsel
  (Wert != Vorgaenger-Wert in `regime_history`, per `LAG`-Fenster — identische
  Logik wie `_regime_changes_in_window`, Feature 8) im Fenster, mit
  von->nach-Detail. Eine reine Wiederholung desselben Regimes zaehlt nicht,
  die allererste `regime_history`-Zeile (kein Vorgaenger) ist eine
  Initialisierung, keine Transition. — Test:
  `test_event_feed_regime_transitions_correct_and_repeats_excluded`
  (Mutation-Check).
- [x] AK3: Notable Trades (`type="notable_trade"`) sind die groessten Wins/
  Losses (je Seite getrennt ueber `is_win`, nicht ueber sortierte `pnl_pct`
  mit Ueberlappungsrisiko bei wenigen Trades) im Fenster, mit Coin+Bot+PnL im
  Detail-Feld. — Test: `test_event_feed_notable_trades_winners_and_losers`.
- [x] AK4: Fensterlogik ist halboffen (`> as_of - INTERVAL window_hours HOUR
  AND <= as_of`), identisch zu `overnight_digest`/`success_rate_timeseries`
  — ein Event ausserhalb des Fensters darf nicht erscheinen (Mutation-Check:
  Fenstergrenze verkehrt/entfernt macht den Test rot). — Test:
  `test_event_feed_window_boundary_excludes_outside_events` (Mutation-Check).
- [x] AK5: Leerer Feed (kein Event im Fenster, aber Substrat hat Daten
  ausserhalb) liefert `events: []`, nie einen Fehler, nie ein fabriziertes
  Event. Komplett leeres Substrat degradiert identisch
  (`as_of: None, events: []`). — Test:
  `test_event_feed_empty_window_degrades_cleanly`,
  `test_event_feed_empty_substrate_degrades_cleanly`.
- [x] AK6: `GET /panels/event-feed` (und `?window=24h|168h`) rendert 200: die
  typisierte, zeit-absteigend sortierte Event-Liste (Icon/Label je Typ +
  Zeitstempel + Beschreibung), END-TO-END gegen eine echte
  `AnalyticsExporter`/DuckDB-Fixture, als letztes Panel in `index.html`
  eingehaengt (nach Coin-Drilldown). Datenstand-Badge
  (`closed_ai_signals`/`closed_trades`/`regime_history`). KEIN
  POST/Write-Endpoint existiert fuer dieses Panel. — Test:
  `test_panel_event_feed_renders_events_in_descending_order`
  (Integrationstest), `test_index_includes_event_feed_panel_last`.
- [x] AK7: Kein Postgres-Zugriff, DB-frei testbar; unbekannter/fehlender
  `?window=`-Wert faellt still auf den Default (24h) zurueck (kein 500). —
  Test: `test_panel_event_feed_never_touches_postgres`,
  `test_resolve_event_feed_window_unknown_value_falls_back_to_default`.

### Out of Scope
- **Schreibende Operator-Annotationen** (frei getippte Notizen/Tags durch
  Michi) — das waere ein Mutations-Endpoint (POST/PUT + CSRF + Persistenz-
  Store fuer die Annotation selbst) und ist explizit Z2-gegated (Cloudflare
  Access + Auth muss zuerst stehen, F4-Familie). Follow-up-Task, nicht Teil
  dieses Panels.
- Live-Steuerung (F4-Familie).
- Ein Hash-Journal / Audit-Trail-Signierung (R9 — gestrichen, siehe MEMORY).
- Die anderen Panels neu bauen.
- Weitere Event-Typen ueber Regime-Uebergaenge/Notable-Trades hinaus (z. B.
  Fleet-Restarts, Modell-Promotions) — nur gebaut wenn trivial aus dem
  bestehenden Substrat ableitbar, hier bewusst nicht ergaenzt (kein
  zusaetzliches Substrat vorhanden, das sie DB-frei liefern koennte).
- Ein neuer `/api/analytics/*`-JSON-Endpoint (die Panel-Route ruft
  `event_feed()` direkt auf, wie die anderen additiven Panel-Routen seit
  Feature 3 es tun).

### Why Build (statt Reuse)
Konsolidierung typisierter Events aus zwei bestehenden T-131-Aggregat-
Bausteinen (Regime-lag-Logik, coin-aware decisive-Trade-CTE) ist
projektspezifische Verdrahtung; keine Library liefert das. Die lag-Logik
selbst (`_regime_changes_in_window`) und die coin-aware CTE
(`_outcomes_cte_with_coin`/`_existing_outcome_tables_with_coin`/
`_bot_filter`) werden wiederverwendet, nicht neu gebaut.

### Scope of consent
**Erlaubt:** `tools/analytics_api.py` additiv (neue Funktionen `event_feed`,
`_regime_transition_events`, `_notable_trade_events`, `_latest_event_anchor`,
bestehende Funktionen unveraendert), `tools/dashboard/app.py` additiv (neue
Konstanten/Funktionen + Route `/panels/event-feed`, neuer
`PANEL_SOURCES`-Eintrag), `tools/dashboard/templates/panels/event_feed.html`
(neu) + `index.html`-Einbindung als letztes Panel,
`tools/dashboard/static/css/app.css` additiv (Event-Feed-Listen-Styles),
`backtest/test_dashboard_event_feed.py` neu, `CHANGELOG.md`-Eintrag, auf
branch `worktree-feat+t-2026-cu-9050-161`.
**Verboten:** jeder POST/PUT/Mutations-Endpoint fuer Annotationen,
`dashboard.py` (altes Dashboard), `.env*`/secrets, Live-DB, Fleet-Restart,
Modell-Artefakte, `core/**`, SPEC.md im Repo-Root, bestehende
`analytics_api`-Aggregatfunktionen (`_outcomes_cte`, `_outcomes_cte_with_coin`,
`_regime_changes_in_window`, `bot_trade_rows`, `bot_leaderboard`,
`success_rate_timeseries`, `bot_regime_matrix`, `coins_with_trades`,
`coin_trade_series`, `overnight_digest`) inhaltlich umschreiben,
Live-Steuerung bauen, `--no-verify`, main/prod direkt, Push/PR
(Orchestrator-Schritt).
**Frag zurueck:** neue Runtime-Dependencies, Aenderung bestehender
Panel-Routen-Signaturen aus Feature 1-8.

---

## Betrieb — Atomarer Export-Publish + Scheduled Tasks (T-2026-CU-9050-163)

Der Analytics-Export (`tools/analytics_export.py`) schreibt NIE direkt in die
served DuckDB (`staging_models/analytics/analytics.duckdb`), die das Dashboard
per Request read-only oeffnet. Stattdessen laeuft er RW auf einer persistenten
**Build-DB** (`analytics.duckdb.build`, traegt das Watermark → Inkrementalitaet
ab dem ersten Lauf/Seed exakt erhalten) und **publisht atomar**: `shutil.copy2(build, <served>.tmp)` →
`os.replace(<served>.tmp, served)` (atomar auf demselben Volume). Damit wird der
served-Pfad vom Export nie exklusiv gelockt → Dashboard-Reads erroren waehrend
eines Laufs nicht mehr. Windows-Sharing-Violation beim Replace → Retries mit
einem **~30 s Gesamt-Budget** (`DEFAULT_PUBLISH_RETRIES=120` × `retry_delay_s=0.25`,
CLI `--publish-retries`/`--publish-retry-delay`, T-2026-CU-9050-167). Das Budget
MUSS grosszuegig sein: das Dashboard HTMX-pollt mehrere Panels und oeffnet die
served-DB per Request read-only, das alte 1 s-Budget (T-163) fand unter Live-
Polling nie eine Luecke und der Publish schlug dauerhaft fehl. Da jeder Request
sein Handle schliesst, ist die served-Datei >90 % der Zeit frei → ein weites
Fenster trifft zuverlaessig eine Luecke. Scheitern ALLE Versuche, bleiben
Build-DB + `.tmp` intakt, served unangetastet, Exit-Code ≠ 0 (kein Korruptions-
Risiko) — **Selbstheilung:** der naechste Lauf republisht dieselben frischen
Daten aus der Build-DB, ein verpasster Publish ist nie Datenverlust, nur
verzoegert. Reine Publish-Logik: `publish_duckdb()` (DB-frei testbar,
`backtest/test_analytics_export_publish.py`).

Rollout-Seed: Der Wechsel auf die Build-DB ist der erste Split vom alten
Single-File-Layout. Beim ERSTEN Lauf unter dem neuen Code seedet `main()`
einmalig `analytics.duckdb.build` aus der bestehenden served-DB
(`seed_build_db`), falls die Build-DB fehlt aber die served existiert → das
`_export_watermark` bleibt erhalten, kein mehrstuendiger Voll-Re-Export aus dem
Live-Postgres. Der Summary-Print laeuft NACH dem Publish, damit ein
Publish-Fehler nie wie Erfolg aussieht (klare `publish PENDING`-Kennzeichnung).

Die zwei Scheduled Tasks (Dashboard-Autostart @127.0.0.1:8098, Export alle
30 min) werden reproduzierbar via `tools/ops/register_kythera_dashboard_tasks.ps1`
registriert (elevated, S4U, `IgnoreNew` = kein ueberlappender Export). Das Skript
ist REGISTRIERUNGS-ONLY — es stoppt keinen Prozess und startet keine Task (kein
Live-Cutover aus einem committeten Artefakt, CLAUDE.md Harte Regel 1); Cutover +
Registrierung sind separate, bewusste Operator-Schritte, kein Teil einer
Dev-Session.

## Performance (T-2026-CU-9050-175)

Die Panel-Kontexte cachen ihre DuckDB-derivierten Daten (Query-Payload +
`data_freshness`-Rows) hinter dem File-Freshness-Token (`analytics_api._PollCache`,
dasselbe Muster wie der `/api/analytics/*`-Blueprint-Cache): bei unveraenderter
Export-Datei wird jeder Poll aus Memory bedient. Das "Sync vor N min"-Alter wird
weiterhin pro Request gerechnet; Fleet-Registry (dateibasiert) bleibt uncached.
Query-seitig: Rolling-Serie via SQL-Daily-Aggregation, Leaderboard via
Streamed-Column-Pfad (optionaler numpy-Fast-Path mit Pure-Python-Fallback),
success-rate-Fenster in einem Scan, Regime-Matrix als ASOF-Inner-Join.
Ergebnis-Paritaet ist HARTE Anforderung — Netz:
`backtest/test_analytics_query_parity.py`. Umfang ehrlich: die drei reinen
count/sum-Aggregate (rolling / success-rate / regime-matrix) sind bit-
identisch old-vs-new. Beim Leaderboard sind die order-invarianten Felder
(n, wins, winrate, pnl_sum_pct, expectancy_pct) bit-identisch; die zwei
order-abhaengigen Risk-Metriken (max_drawdown_pp, max_loss_streak) erben die
VORBESTEHENDE run-to-run-Nichtdeterminismus-Klasse (Duplikat-`closed_at` +
kein deterministischer Tiebreaker unter DuckDB-Parallelitaet) — der alte Code
hatte sie identisch, es kommt keine neue hinzu. Ein deterministischer
Tiebreaker (= Verhaltensaenderung an Geld-Werten) ist ein separater Follow-up.

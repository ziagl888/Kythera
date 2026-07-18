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

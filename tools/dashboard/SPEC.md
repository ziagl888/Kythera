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

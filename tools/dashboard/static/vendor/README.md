# Vendored front-end libraries (Z1 dashboard)

These are checked-in, self-hosted copies of third-party JS. The dashboard makes
**no CDN requests** — the VPS serves everything locally (offline-safe, no
external dependency at request time, and no on-box npm/Node toolchain per the
framework gate D-2026-CLD-111).

| File | Library | Version | Source (verbatim download URL) | License |
|------|---------|---------|--------------------------------|---------|
| `htmx.min.js` | htmx | 2.0.4 | `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js` | BSD-2-Clause / 0BSD |
| `lightweight-charts.standalone.production.js` | TradingView Lightweight Charts | 4.2.3 | `https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js` | Apache-2.0 |
| `echarts.min.js` | Apache ECharts | 5.6.0 | `https://unpkg.com/echarts@5.6.0/dist/echarts.min.js` | Apache-2.0 |

## Updating a library
1. Download the exact pinned version from the URL above (bump the version in the
   URL and in this table together).
2. Replace the file in place — keep the filename stable; `templates/base.html`
   references these names via `url_for('static', …)`.
3. Re-run `pytest backtest/test_dashboard_shell.py` (asserts the assets are
   served) and smoke-test the chart mount/dispose in a browser.

Do not minify/transform further and do not fetch at build time — the whole point
is a static, reviewable, offline artifact.

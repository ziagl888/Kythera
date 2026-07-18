# backtest/test_dashboard_shell.py
"""DB-free tests for the Z1 dashboard shell (Task 0, T-2026-CU-9050-151).

Mirrors backtest/test_analytics_export.py: the Postgres boundary is replaced by
a synthetic ``ListFetcher`` that feeds the real ``AnalyticsExporter`` into a
temporary DuckDB file. Every dashboard layer — the Flask app factory, the
mounted analytics blueprint, the HTMX shell + demo panel, the freshness badge,
the chart-lifecycle asset, and the serving entrypoint — is then exercised
against that file with Flask's test client. No database, no network.

Covers SPEC AK1–AK7. Run with: pytest backtest/test_dashboard_shell.py -v
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
from typing import Any, Callable

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import analytics_export  # noqa: E402
from tools.analytics_api import WAITRESS_THREADS  # noqa: E402
from tools.analytics_export import (  # noqa: E402
    SOURCES_BY_NAME,
    AnalyticsExporter,
    Cursor,
    SourceSpec,
)
from tools.dashboard import app as dashboard_app  # noqa: E402

UTC = datetime.timezone.utc
_BASE = datetime.datetime(2026, 7, 15, 12, 0, 0)  # noqa: DTZ001  (naive-local, per exporter contract)


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic fetcher — mirrors PostgresFetcher's SELECT contract, no DB
# ─────────────────────────────────────────────────────────────────────────────


def _closed_predicate(spec: SourceSpec, row: dict[str, Any]) -> bool:
    if spec.name == "closed_ai_signals":
        return row.get("close_time") is not None and row.get("status") != "ENTRY_NOT_FILLED"
    return True


class ListFetcher:
    def __init__(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self.data = data

    def fetch(self, spec: SourceSpec, cursor: Cursor | None, limit: int) -> list[dict[str, Any]]:
        rows = [r for r in self.data.get(spec.name, []) if _closed_predicate(spec, r)]
        rows.sort(key=lambda r: (r[spec.ts_col], r[spec.pk_col]))
        if cursor is not None:
            rows = [r for r in rows if (r[spec.ts_col], r[spec.pk_col]) > cursor]
        return rows[:limit]


def _ai_row(i: int, *, model: str, direction: str, entry: float, close: float,
            close_time: datetime.datetime, status: str = "TP1") -> dict[str, Any]:
    return {
        "id": i, "symbol": f"C{i}USDT", "model": model, "direction": direction,
        "entry": entry, "close_price": close, "targets_hit": 1,
        "open_time": close_time - datetime.timedelta(hours=1),
        "close_time": close_time, "status": status, "lev": "20x",
    }


def _fixed_clock(dt: datetime.datetime) -> Callable[[], datetime.datetime]:
    return lambda: dt


def _build_duckdb(tmp_path, *, synced_at: datetime.datetime | None = None) -> str:
    """Materialise a small closed_ai_signals fixture into a temp DuckDB file.

    ABR2 → 2 wins + 1 loss + 2 neutrals (excluded); MIS2 → 1 win. All within the
    7-day window of the latest close_time, so the demo panel counts them.
    """
    ai = [
        _ai_row(1, model="ABR2", direction="LONG", entry=100, close=110,
                close_time=_BASE - datetime.timedelta(days=1)),          # win
        _ai_row(2, model="ABR2", direction="LONG", entry=100, close=105, close_time=_BASE),   # win
        _ai_row(3, model="ABR2", direction="SHORT", entry=100, close=110, close_time=_BASE),  # loss
        _ai_row(4, model="ABR2", direction="LONG", entry=100, close=100.05, close_time=_BASE),  # micro → neutral
        _ai_row(5, model="ABR2", direction="LONG", entry=100, close=50, close_time=_BASE,
                status="DELISTED"),                                       # housekeeping → neutral
        _ai_row(6, model="MIS2", direction="LONG", entry=100, close=120, close_time=_BASE),   # win
    ]
    duckdb_path = str(tmp_path / "analytics.duckdb")
    parquet_root = str(tmp_path / "parquet")
    clock = _fixed_clock(synced_at) if synced_at is not None else None
    kwargs: dict[str, Any] = {"sources": [SOURCES_BY_NAME["closed_ai_signals"]]}
    if clock is not None:
        kwargs["clock"] = clock
    AnalyticsExporter(duckdb_path, parquet_root, ListFetcher({"closed_ai_signals": ai}), **kwargs).run()
    return duckdb_path


@pytest.fixture()
def client(tmp_path):
    duckdb_path = _build_duckdb(tmp_path, synced_at=datetime.datetime(2026, 7, 15, 12, 30, tzinfo=UTC))
    app = dashboard_app.create_app(duckdb_path)
    app.config.update(TESTING=True)
    return app.test_client()


# ─────────────────────────────────────────────────────────────────────────────
# AK1 — the analytics JSON blueprint is mounted on the dashboard app
# ─────────────────────────────────────────────────────────────────────────────


def test_json_api_mounted(client):
    resp = client.get("/api/analytics/success-rate?windows=7&bots=ABR2")
    assert resp.status_code == 200
    body = resp.get_json()
    w7 = {e["bot"]: e for e in body["windows"]["7"]}
    assert w7["ABR2"]["wins"] == 2 and w7["ABR2"]["n"] == 3

    resp_bots = client.get("/api/analytics/bots")
    assert resp_bots.status_code == 200
    assert set(resp_bots.get_json()["bots"]) == {"ABR2", "MIS2"}


# ─────────────────────────────────────────────────────────────────────────────
# AK2 — index renders the responsive HTMX shell
# ─────────────────────────────────────────────────────────────────────────────


def test_index_renders_shell(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Responsive base layout.
    assert 'name="viewport"' in html
    # Vendored assets referenced (no CDN).
    assert "vendor/htmx.min.js" in html
    assert "js/chart_lifecycle.js" in html
    # Demo panel container polls the fragment on an interval.
    assert 'hx-get="/panels/success-rate"' in html
    assert "every 30s" in html


# ─────────────────────────────────────────────────────────────────────────────
# AK3 — the demo panel renders success-rate fields from the DuckDB substrate
# ─────────────────────────────────────────────────────────────────────────────


def test_demo_panel_renders_winrate(client):
    resp = client.get("/panels/success-rate")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "ABR2" in html and "MIS2" in html
    # ABR2 winrate 2/3 → 66.7% rendered as a percentage in the table.
    assert "66.7%" in html
    # The chart mount point + embedded series prove the chart-lifecycle wiring.
    assert 'data-chart="winrate-bars"' in html
    assert 'id="winrate-series"' in html


def test_demo_panel_empty_duckdb(tmp_path):
    # Export nothing → panel degrades gracefully, still 200.
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(duckdb_path, str(tmp_path / "pq"), ListFetcher({}),
                      sources=[SOURCES_BY_NAME["closed_ai_signals"]]).run()
    c = dashboard_app.create_app(duckdb_path).test_client()
    resp = c.get("/panels/success-rate")
    assert resp.status_code == 200
    assert "keine entschiedenen Trades" in resp.get_data(as_text=True)


# ─────────────────────────────────────────────────────────────────────────────
# AK4 — the shared chart-lifecycle asset is served with the required wiring
# ─────────────────────────────────────────────────────────────────────────────


def test_chart_lifecycle_js_served(client):
    resp = client.get("/static/js/chart_lifecycle.js")
    assert resp.status_code == 200
    js = resp.get_data(as_text=True)
    # Disposes on beforeSwap, re-inits on afterSwap — the leak-prevention core.
    assert "htmx:beforeSwap" in js
    assert "htmx:afterSwap" in js
    assert "registerFactory" in js
    # Both charting libraries' teardown methods are handled.
    assert ".dispose" in js and ".remove" in js


# ─────────────────────────────────────────────────────────────────────────────
# AK5 — freshness badge: age from synced_at (UTC) ONLY, never last_row_ts
# ─────────────────────────────────────────────────────────────────────────────


def test_freshness_summary_computes_age_from_synced_at():
    rows = [
        {"source": "closed_ai_signals",
         "last_row_ts": "2026-07-15T12:00:00",       # naive-local wall clock
         "synced_at": "2026-07-15T12:30:00"},        # UTC wall clock
    ]
    now = datetime.datetime(2026, 7, 15, 13, 0, 0, tzinfo=UTC)  # 30 min after synced_at
    summary = dashboard_app.freshness_summary(rows, now_utc=now)
    assert summary["sync_age_min"] == 30
    assert summary["stand"] == "12:00"                # from last_row_ts wall clock
    assert summary["label"] == "Stand 12:00, Sync vor 30 min"
    assert summary["sources"] == 1


def test_freshness_summary_age_ignores_naive_local_last_row_ts():
    """The naive-local last_row_ts must never enter the age computation — only
    synced_at (UTC) does. A wildly different last_row_ts must not move the age."""
    now = datetime.datetime(2026, 7, 15, 13, 0, 0, tzinfo=UTC)
    base = {"source": "s", "synced_at": "2026-07-15T12:30:00"}
    near = dashboard_app.freshness_summary([{**base, "last_row_ts": "2026-07-15T12:29:00"}], now_utc=now)
    far = dashboard_app.freshness_summary([{**base, "last_row_ts": "1999-01-01T00:00:00"}], now_utc=now)
    assert near["sync_age_min"] == far["sync_age_min"] == 30  # age unchanged by last_row_ts


def test_freshness_summary_picks_latest_across_sources():
    rows = [
        {"source": "a", "last_row_ts": "2026-07-15T09:00:00", "synced_at": "2026-07-15T12:00:00"},
        {"source": "b", "last_row_ts": "2026-07-15T11:45:00", "synced_at": "2026-07-15T12:30:00"},
    ]
    now = datetime.datetime(2026, 7, 15, 12, 40, 0, tzinfo=UTC)
    summary = dashboard_app.freshness_summary(rows, now_utc=now)
    assert summary["stand"] == "11:45"        # latest last_row_ts
    assert summary["sync_age_min"] == 10       # newest synced_at (12:30) vs 12:40
    assert summary["sources"] == 2


def test_freshness_summary_empty_and_clock_skew():
    assert dashboard_app.freshness_summary([])["label"] == "Kein Datenstand"
    # Clock skew: synced_at slightly in the future → clamped to 0, never negative.
    now = datetime.datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
    skew = dashboard_app.freshness_summary(
        [{"source": "s", "last_row_ts": "2026-07-15T12:00:00", "synced_at": "2026-07-15T12:05:00"}],
        now_utc=now,
    )
    assert skew["sync_age_min"] == 0


def test_index_shows_badge(client):
    html = client.get("/").get_data(as_text=True)
    assert "Sync vor" in html
    # The badge polls its own fragment too.
    assert 'hx-get="/panels/freshness"' in html
    frag = client.get("/panels/freshness")
    assert frag.status_code == 200
    assert "Sync vor" in frag.get_data(as_text=True)


# ─────────────────────────────────────────────────────────────────────────────
# AK6 — serving entrypoint binds 127.0.0.1 and uses the shared waitress path
# ─────────────────────────────────────────────────────────────────────────────


def test_serve_defaults_to_localhost():
    args = dashboard_app._build_parser().parse_args([])
    assert args.host == "127.0.0.1"  # never 0.0.0.0 (P0.8)


def test_serve_delegates_to_waitress_path():
    calls = []

    def fake_serve(app, **kwargs):
        calls.append((app, kwargs))

    sentinel = object()
    dashboard_app.serve(sentinel, host="127.0.0.1", port=8098, serve_fn=fake_serve)
    assert len(calls) == 1
    app_arg, kwargs = calls[0]
    assert app_arg is sentinel
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8098
    # Reuses analytics_api's bounded waitress thread pool.
    assert kwargs["threads"] == WAITRESS_THREADS


# ─────────────────────────────────────────────────────────────────────────────
# AK7 — no import and no route touches Postgres (DuckDB-only read path)
# ─────────────────────────────────────────────────────────────────────────────


def test_routes_never_touch_postgres(client, monkeypatch):
    """Every serving route must resolve purely against DuckDB. If any handler
    reached for Postgres, this raising stub would turn the response into a 500."""
    def _boom(*_a, **_k):
        raise AssertionError("a route touched Postgres")

    monkeypatch.setattr(analytics_export.PostgresFetcher, "_connection", _boom)
    monkeypatch.setattr(analytics_export.PostgresFetcher, "fetch", _boom)

    for path in ("/", "/panels/success-rate", "/panels/freshness",
                 "/api/analytics/success-rate", "/api/analytics/freshness"):
        assert client.get(path).status_code == 200, path


def test_import_is_db_free():
    """Importing the dashboard app must not pull psycopg2 into the interpreter
    (the DB driver is lazy, deep inside PostgresFetcher). Checked in a clean
    subprocess so the result is independent of what other tests imported."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys; import tools.dashboard.app as m; "
        "assert 'psycopg2' not in sys.modules, 'psycopg2 imported at module load'; "
        "assert hasattr(m, 'create_app'); print('ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout

# backtest/test_dashboard_regime_heatmap.py
"""DB-free tests for the Z1 dashboard Bot x Regime performance heatmap
(Feature 6, T-2026-CU-9050-158).

Mirrors backtest/test_dashboard_leaderboard.py's fixture style: the Postgres
boundary is replaced by a synthetic ``ListFetcher`` feeding the real
``AnalyticsExporter`` into a temporary DuckDB file, using the ACTUAL
``closed_ai_signals`` and ``regime_history`` column names from
``tools/analytics_export.py`` (closed_ai_signals: id, symbol, model,
direction, entry, close_price, targets_hit, open_time, close_time, status,
lev; regime_history: id, ts, regime, alt_context, btc_price, confidence,
confidence_btc, confidence_alt) — the T-152 review lesson: unrealistic
fixture keys can mask a real bug.

Covers SPEC.md AK1-AK6. Run with:
    pytest backtest/test_dashboard_regime_heatmap.py -v
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import analytics_api  # noqa: E402
from tools import analytics_export  # noqa: E402
from tools.analytics_api import bot_regime_matrix  # noqa: E402
from tools.analytics_export import SOURCES_BY_NAME, AnalyticsExporter  # noqa: E402
from tools.dashboard import app as dashboard_app  # noqa: E402

_BASE = datetime.datetime(2026, 7, 1, 0, 0, 0)  # noqa: DTZ001  (naive-local, per exporter contract)


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic fetcher — real closed_ai_signals + regime_history column shapes
# ─────────────────────────────────────────────────────────────────────────────


class ListFetcher:
    def __init__(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self.data = data

    def fetch(self, spec, cursor, limit):  # noqa: ANN001
        rows = list(self.data.get(spec.name, []))
        if spec.name == "closed_ai_signals":
            rows = [r for r in rows if r.get("close_time") is not None and r.get("status") != "ENTRY_NOT_FILLED"]
        rows.sort(key=lambda r: (r[spec.ts_col], r[spec.pk_col]))
        if cursor is not None:
            rows = [r for r in rows if (r[spec.ts_col], r[spec.pk_col]) > cursor]
        return rows[:limit]


def _ai_row(
    i: int,
    *,
    model: str,
    entry: float,
    close: float,
    close_time: datetime.datetime,
    direction: str = "LONG",
    status: str = "TP1",
) -> dict[str, Any]:
    """One realistic closed_ai_signals row — real column names from
    analytics_export.SOURCES_BY_NAME['closed_ai_signals']."""
    return {
        "id": i,
        "symbol": f"C{i}USDT",
        "model": model,
        "direction": direction,
        "entry": entry,
        "close_price": close,
        "targets_hit": 1 if close != entry else 0,
        "open_time": close_time - datetime.timedelta(hours=1),
        "close_time": close_time,
        "status": status,
        "lev": "20x",
    }


def _regime_row(i: int, *, ts: datetime.datetime, regime: str) -> dict[str, Any]:
    """One realistic regime_history row — real column names from
    analytics_export.SOURCES_BY_NAME['regime_history']."""
    return {
        "id": i,
        "ts": ts,
        "regime": regime,
        "alt_context": None,
        "btc_price": 60000.0,
        "confidence": 0.8,
        "confidence_btc": 0.8,
        "confidence_alt": 0.7,
    }


def _day(n: float) -> datetime.datetime:
    return _BASE + datetime.timedelta(days=n)


# Fixture: two regime windows collapse into two distinct labels (TREND appears
# TWICE — day 0 and day 4 — and must merge into ONE "TREND" column, not two).
#   TREND  @ day 0  -> active [day0, day2)
#   RANGE  @ day 2  -> active [day2, day4)
#   TREND  @ day 4  -> active [day4, +inf)
def _regime_fixture_rows() -> list[dict[str, Any]]:
    return [
        _regime_row(1, ts=_day(0), regime="TREND"),
        _regime_row(2, ts=_day(2), regime="RANGE"),
        _regime_row(3, ts=_day(4), regime="TREND"),
    ]


# RUB2: trade in TREND window#1 (+10, day 1), trade in RANGE window (-5, day 3),
#       trade in TREND window#2 (+20, day 5)
#       -> TREND: n=2, wins=2, pnl_sum=30, expectancy=15, winrate=1.0
#       -> RANGE: n=1, wins=0, pnl_sum=-5, winrate=0.0
# MIS2: trade BEFORE the first regime row (day -1, +50 pnl) -> no ASOF match,
#       must be EXCLUDED from the matrix entirely (AK4).
#       trade in RANGE window (-30, day 3.5) -> only cell MIS2 has (AK3: no
#       TREND entry for MIS2 at all — the "missing cell" case).
def _fixture_rows() -> list[dict[str, Any]]:
    return [
        _ai_row(1, model="RUB2", entry=100, close=110, close_time=_day(1)),  # TREND#1, +10
        _ai_row(2, model="RUB2", entry=100, close=95, close_time=_day(3)),  # RANGE, -5
        _ai_row(3, model="RUB2", entry=100, close=120, close_time=_day(5)),  # TREND#2, +20
        _ai_row(4, model="MIS2", entry=100, close=150, close_time=_day(-1)),  # before any regime -> excluded
        _ai_row(5, model="MIS2", entry=100, close=70, close_time=_day(3.5)),  # RANGE, -30
    ]


def _build_duckdb(tmp_path, *, ai_rows=None, regime_rows=None) -> str:
    duckdb_path = str(tmp_path / "analytics.duckdb")
    parquet_root = str(tmp_path / "parquet")
    AnalyticsExporter(
        duckdb_path,
        parquet_root,
        ListFetcher(
            {
                "closed_ai_signals": _fixture_rows() if ai_rows is None else ai_rows,
                "regime_history": _regime_fixture_rows() if regime_rows is None else regime_rows,
            }
        ),
        sources=[SOURCES_BY_NAME["closed_ai_signals"], SOURCES_BY_NAME["regime_history"]],
    ).run()
    return duckdb_path


# ─────────────────────────────────────────────────────────────────────────────
# AK1 — bot_regime_matrix() correctly assigns decisive trades to their active
# regime window, aggregating repeated regime labels into one column
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_regime_matrix_assigns_trades_to_active_regime_window(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = bot_regime_matrix(con)
    finally:
        con.close()

    assert payload["bots"] == ["MIS2", "RUB2"]
    # TREND appeared twice in regime_history (day 0 and day 4) but merges into
    # ONE column, not two — a per-window (rather than per-label) grouping bug
    # would show 3 regime columns instead of 2.
    assert payload["regimes"] == ["RANGE", "TREND"]

    rub2 = payload["cells"]["RUB2"]
    assert rub2["TREND"] == {
        "n": 2,
        "wins": 2,
        "winrate": pytest.approx(1.0),
        "pnl_sum_pct": pytest.approx(30.0),
        "expectancy_pct": pytest.approx(15.0),
    }
    assert rub2["RANGE"]["n"] == 1
    assert rub2["RANGE"]["wins"] == 0
    assert rub2["RANGE"]["pnl_sum_pct"] == pytest.approx(-5.0)


# ─────────────────────────────────────────────────────────────────────────────
# AK2 — ASOF join direction (mutation-check): a trade EXACTLY on the regime
# boundary joins the NEW window, not the old one
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_regime_matrix_boundary_trade_joins_new_regime_window(tmp_path):
    """A trade whose closed_at is EXACTLY equal to a regime_history row's ts
    must be assigned to THAT (new) regime, not the previous one — the ASOF
    join is `closed_at >= ts`, not `closed_at > ts`. A mutation that flips the
    inequality (or reverses the join direction to "closest regime AFTER the
    trade" instead of "closest regime AT/BEFORE the trade") would assign this
    trade to TREND instead of RANGE, making this assertion fail."""
    boundary_ts = _day(2)  # exactly the RANGE row's ts
    duckdb_path = _build_duckdb(
        tmp_path,
        ai_rows=[_ai_row(1, model="RUB2", entry=100, close=110, close_time=boundary_ts)],
        regime_rows=[
            _regime_row(1, ts=_day(0), regime="TREND"),
            _regime_row(2, ts=boundary_ts, regime="RANGE"),
        ],
    )
    con = analytics_api.connect_ro(duckdb_path)
    try:
        payload = bot_regime_matrix(con)
    finally:
        con.close()

    assert payload["regimes"] == ["RANGE"]
    assert "TREND" not in payload["cells"].get("RUB2", {})
    assert payload["cells"]["RUB2"]["RANGE"]["n"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# AK3 — a (bot, regime) pair with zero decisive trades is ABSENT, not a
# fabricated zero-filled cell
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_regime_matrix_missing_cell_absent_not_fabricated(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = bot_regime_matrix(con)
    finally:
        con.close()

    # MIS2 has exactly one decisive, regime-assigned trade (RANGE) — its
    # TREND cell must be absent entirely, not {"n": 0, ...}.
    mis2 = payload["cells"]["MIS2"]
    assert set(mis2.keys()) == {"RANGE"}
    assert "TREND" not in mis2
    assert mis2["RANGE"]["n"] == 1
    assert mis2["RANGE"]["pnl_sum_pct"] == pytest.approx(-30.0)


# ─────────────────────────────────────────────────────────────────────────────
# AK4 — a trade before the very first regime_history row is excluded, not
# bucketed into an "UNKNOWN" column
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_regime_matrix_trade_before_first_regime_row_excluded(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = bot_regime_matrix(con)
    finally:
        con.close()

    # MIS2's day(-1) trade (+50 pnl) predates the first regime_history row
    # (day 0) -> must not inflate ANY cell and must not create an "UNKNOWN"
    # column. Only 2 regime columns total, and MIS2's RANGE cell reflects only
    # the day(3.5) trade (-30), not a phantom (+50-30)/2 blend.
    assert "UNKNOWN" not in payload["regimes"]
    assert payload["regimes"] == ["RANGE", "TREND"]
    assert payload["cells"]["MIS2"]["RANGE"]["n"] == 1
    assert payload["cells"]["MIS2"]["RANGE"]["pnl_sum_pct"] == pytest.approx(-30.0)


# ─────────────────────────────────────────────────────────────────────────────
# AK6 — empty-substrate degrade (no regime_history table, no outcome table)
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_regime_matrix_empty_substrate_degrades_gracefully(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path, str(tmp_path / "pq"), ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    con = analytics_api.connect_ro(duckdb_path)
    try:
        payload = bot_regime_matrix(con)
    finally:
        con.close()
    # regime_history table was never created (not in `sources`) -> the
    # function must degrade cleanly, not raise a CatalogException.
    assert payload == {"bots": [], "regimes": [], "cells": {}}


def test_bot_regime_matrix_no_outcome_tables_degrades_gracefully(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path, str(tmp_path / "pq"), ListFetcher({}),
        sources=[SOURCES_BY_NAME["regime_history"]],
    ).run()
    con = analytics_api.connect_ro(duckdb_path)
    try:
        payload = bot_regime_matrix(con)
    finally:
        con.close()
    assert payload == {"bots": [], "regimes": [], "cells": {}}


def test_bot_regime_matrix_bots_filter(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = bot_regime_matrix(con, bots=["RUB2"])
    finally:
        con.close()
    assert payload["bots"] == ["RUB2"]
    assert "MIS2" not in payload["cells"]


# ─────────────────────────────────────────────────────────────────────────────
# AK5/AK6 — Flask route: end-to-end integration test against real DuckDB
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path):
    app = dashboard_app.create_app(_build_duckdb(tmp_path))
    app.config.update(TESTING=True)
    return app.test_client()


def test_panel_regime_heatmap_renders_correct_cell_values(client):
    """The mandatory integration test: real AnalyticsExporter -> real DuckDB
    file (closed_ai_signals + regime_history) -> real bot_regime_matrix() ASOF
    join -> real Flask route -> rendered HTML, with realistic fixture data
    (not hand-built dicts)."""
    resp = client.get("/panels/regime-heatmap")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "RUB2" in html
    assert "MIS2" in html
    assert "TREND" in html
    assert "RANGE" in html
    # Default metric is winrate: RUB2xTREND = 100.0%, RUB2xRANGE = 0.0%.
    assert "100.0%" in html
    assert "0.0%" in html
    # MIS2's missing TREND cell renders as a dash, never a fabricated value.
    assert "—" in html


def test_panel_regime_heatmap_expectancy_metric_renders_pnl_values(client):
    resp = client.get("/panels/regime-heatmap?metric=expectancy")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # RUB2xTREND expectancy = 15.0 (avg of +10, +20); RUB2xRANGE = -5.0.
    assert "15.000%" in html
    assert "-5.000%" in html


def test_panel_regime_heatmap_unknown_metric_falls_back_no_500(client):
    resp = client.get("/panels/regime-heatmap?metric=not_a_real_metric")
    assert resp.status_code == 200
    assert 'checked' in resp.get_data(as_text=True)  # winrate radio still renders as checked


def test_index_includes_regime_heatmap_panel(client):
    html = client.get("/").get_data(as_text=True)
    assert 'hx-get="/panels/regime-heatmap"' in html
    assert 'id="regime-heatmap-body"' in html


def test_panel_regime_heatmap_never_touches_postgres(client, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("regime-heatmap route touched Postgres")

    monkeypatch.setattr(analytics_export.PostgresFetcher, "_connection", _boom)
    monkeypatch.setattr(analytics_export.PostgresFetcher, "fetch", _boom)
    assert client.get("/panels/regime-heatmap").status_code == 200


def test_panel_regime_heatmap_empty_duckdb(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path, str(tmp_path / "pq"), ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"], SOURCES_BY_NAME["regime_history"]],
    ).run()
    c = dashboard_app.create_app(duckdb_path).test_client()
    resp = c.get("/panels/regime-heatmap")
    assert resp.status_code == 200
    assert "Noch keine Regime-zugeordneten" in resp.get_data(as_text=True)


def test_panel_regime_heatmap_freshness_uses_regime_and_ai_signal_sources():
    # Data-only assertion on the panel->source mapping — no Flask/DuckDB
    # needed. If a future edit widened this to closed_trades (like the
    # leaderboard) or dropped regime_history, this would catch it.
    assert dashboard_app.PANEL_SOURCES["regime-heatmap"] == ("closed_ai_signals", "regime_history")


def test_resolve_regime_heatmap_metric_unknown_falls_back_to_default():
    assert dashboard_app.resolve_regime_heatmap_metric("bogus") == dashboard_app.DEFAULT_REGIME_HEATMAP_METRIC
    assert dashboard_app.resolve_regime_heatmap_metric(None) == dashboard_app.DEFAULT_REGIME_HEATMAP_METRIC
    assert dashboard_app.resolve_regime_heatmap_metric("expectancy") == "expectancy"


def test_regime_heatmap_factory_registered_in_panels_js():
    js_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tools", "dashboard", "static", "js", "panels.js",
    )
    with open(js_path, encoding="utf-8") as f:
        content = f.read()
    assert 'registerFactory("bot-regime-heatmap"' in content

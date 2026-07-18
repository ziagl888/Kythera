# backtest/test_dashboard_coin_drilldown.py
"""DB-free tests for the Z1 dashboard Coin-Drilldown panel (Feature 7,
T-2026-CU-9050-159, Q11 Ebenen-Kette).

Mirrors backtest/test_dashboard_regime_heatmap.py / test_dashboard_freshness.py's
fixture style: the Postgres boundary is replaced by a synthetic ``ListFetcher``
feeding the real ``AnalyticsExporter`` into a temporary DuckDB file, using the
ACTUAL ``closed_ai_signals``/``closed_trades`` column names from
``tools/analytics_export.py`` (closed_ai_signals: id, symbol, model, direction,
entry, close_price, targets_hit, open_time, close_time, status, lev;
closed_trades: id, strategy, coin, direction, lev, entry, close_price, time,
posted, status) — the T-152 review lesson: unrealistic fixture keys can mask a
real bug.

SCOPING: full OHLCV candles are explicitly out of scope for this feature (the
25GB candle export was deferred in T-131) — these tests cover only the
decisive-trade price-path + marker + table rendering, never candlesticks.

Covers SPEC.md Feature 7 AK1-AK8. Run with:
    pytest backtest/test_dashboard_coin_drilldown.py -v
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
from tools.analytics_api import coin_trade_series, coins_with_trades  # noqa: E402
from tools.analytics_export import SOURCES_BY_NAME, AnalyticsExporter  # noqa: E402
from tools.dashboard import app as dashboard_app  # noqa: E402

_BASE = datetime.datetime(2026, 7, 1, 0, 0, 0)  # noqa: DTZ001  (naive-local, per exporter contract)


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic fetcher — real closed_ai_signals + closed_trades column shapes
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
    symbol: str,
    model: str,
    entry: float,
    close: float,
    close_time: datetime.datetime,
    direction: str = "LONG",
    targets_hit: int = 1,
    status: str = "TP1",
) -> dict[str, Any]:
    """One realistic closed_ai_signals row — real column names from
    analytics_export.SOURCES_BY_NAME['closed_ai_signals']."""
    return {
        "id": i,
        "symbol": symbol,
        "model": model,
        "direction": direction,
        "entry": entry,
        "close_price": close,
        "targets_hit": targets_hit,
        "open_time": close_time - datetime.timedelta(hours=1),
        "close_time": close_time,
        "status": status,
        "lev": "20x",
    }


def _ct_row(
    i: int,
    *,
    strategy: str,
    coin: str,
    entry: float,
    close: float,
    posted: datetime.datetime,
    direction: str = "LONG",
    status: str = "TP1",
) -> dict[str, Any]:
    """One realistic closed_trades row — real column names from
    analytics_export.SOURCES_BY_NAME['closed_trades'] (no targets_hit column —
    that table simply lacks one)."""
    return {
        "id": i,
        "strategy": strategy,
        "coin": coin,
        "direction": direction,
        "lev": "10x",
        "entry": entry,
        "close_price": close,
        "time": posted - datetime.timedelta(hours=1),
        "posted": posted,
        "status": status,
    }


def _day(n: float) -> datetime.datetime:
    return _BASE + datetime.timedelta(days=n)


# Fixture: three coins.
#   C1USDT (closed_ai_signals, RUB2): two decisive trades, +10 then -5.
#   C2USDT (closed_trades, MAX1): one decisive trade, +20 (targets_hit must be
#     None — closed_trades has no such column).
#   C3USDT: ONLY a micro-PnL row (below MICRO_PNL_PCT) -> zero decisive trades
#     -> must NOT appear in coins_with_trades() at all (AK1).
def _fixture_ai_rows() -> list[dict[str, Any]]:
    return [
        _ai_row(1, symbol="C1USDT", model="RUB2", entry=100, close=110, close_time=_day(1)),
        _ai_row(2, symbol="C1USDT", model="RUB2", entry=100, close=95, close_time=_day(2)),
        _ai_row(3, symbol="C3USDT", model="MIS2", entry=100, close=100.05, close_time=_day(1)),  # micro-pnl
    ]


def _fixture_ct_rows() -> list[dict[str, Any]]:
    return [
        _ct_row(1, strategy="MAX1", coin="C2USDT", entry=200, close=240, posted=_day(1.5)),
    ]


def _build_duckdb(tmp_path, *, ai_rows=None, ct_rows=None) -> str:
    duckdb_path = str(tmp_path / "analytics.duckdb")
    parquet_root = str(tmp_path / "parquet")
    AnalyticsExporter(
        duckdb_path,
        parquet_root,
        ListFetcher(
            {
                "closed_ai_signals": _fixture_ai_rows() if ai_rows is None else ai_rows,
                "closed_trades": _fixture_ct_rows() if ct_rows is None else ct_rows,
            }
        ),
        sources=[SOURCES_BY_NAME["closed_ai_signals"], SOURCES_BY_NAME["closed_trades"]],
    ).run()
    return duckdb_path


# ─────────────────────────────────────────────────────────────────────────────
# AK1 — coins_with_trades() lists only coins with a decisive trade
# ─────────────────────────────────────────────────────────────────────────────


def test_coins_with_trades_lists_only_decisive_coins(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        coins = coins_with_trades(con)
    finally:
        con.close()

    assert coins == ["C1USDT", "C2USDT"]  # sorted; C3USDT (micro-pnl only) excluded


def test_coins_with_trades_empty_substrate_degrades_gracefully(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path,
        str(tmp_path / "pq"),
        ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    con = analytics_api.connect_ro(duckdb_path)
    try:
        coins = coins_with_trades(con)
    finally:
        con.close()
    assert coins == []


# ─────────────────────────────────────────────────────────────────────────────
# AK2 — coin_trade_series() returns ordered decisive trades for one coin
# ─────────────────────────────────────────────────────────────────────────────


def test_coin_trade_series_returns_ordered_decisive_trades_for_one_coin(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = coin_trade_series(con, "C1USDT")
    finally:
        con.close()

    assert payload["coin"] == "C1USDT"
    trades = payload["trades"]
    assert len(trades) == 2
    # Ascending close-time order.
    assert trades[0]["pnl_pct"] == pytest.approx(10.0)
    assert trades[0]["is_win"] is True
    assert trades[0]["targets_hit"] == 1
    assert trades[1]["pnl_pct"] == pytest.approx(-5.0)
    assert trades[1]["is_win"] is False
    assert trades[0]["bot"] == "RUB2"
    assert trades[0]["direction"] == "LONG"
    assert trades[0]["entry"] == pytest.approx(100.0)
    assert trades[0]["close_price"] == pytest.approx(110.0)


def test_coin_trade_series_closed_trades_row_has_no_target_hit(tmp_path):
    """closed_trades has no targets_hit column — the coin-aware CTE must
    project a real NULL there, never a fabricated 0 (mirrors
    bot_regime_matrix's "never fabricate" convention for missing data)."""
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = coin_trade_series(con, "C2USDT")
    finally:
        con.close()

    assert len(payload["trades"]) == 1
    trade = payload["trades"][0]
    assert trade["bot"] == "MAX1"
    assert trade["pnl_pct"] == pytest.approx(20.0)
    assert trade["targets_hit"] is None


# ─────────────────────────────────────────────────────────────────────────────
# AK3 — mutation check: the coin filter is genuinely wired through
# ─────────────────────────────────────────────────────────────────────────────


def test_coin_trade_series_wrong_coin_filter_yields_different_trades(tmp_path):
    """A query for C1USDT and a query for C2USDT must return DIFFERENT trade
    sets — this is the mutation-check the SPEC calls for: a coin filter that
    is accepted-but-ignored (e.g. always returning every coin's trades, or
    filtering on the wrong column) would make this assertion fail because
    both queries would return the SAME (full) trade set."""
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        c1 = coin_trade_series(con, "C1USDT")
        c2 = coin_trade_series(con, "C2USDT")
    finally:
        con.close()

    c1_bots = {t["bot"] for t in c1["trades"]}
    c2_bots = {t["bot"] for t in c2["trades"]}
    assert c1_bots == {"RUB2"}
    assert c2_bots == {"MAX1"}
    assert c1_bots != c2_bots
    assert len(c1["trades"]) == 2
    assert len(c2["trades"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# AK4 — unknown/empty coin degrades cleanly
# ─────────────────────────────────────────────────────────────────────────────


def test_coin_trade_series_unknown_coin_returns_empty(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = coin_trade_series(con, "NOPE999")
    finally:
        con.close()
    assert payload == {"coin": "NOPE999", "trades": []}


def test_coin_trade_series_none_and_empty_symbol_return_empty(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        assert coin_trade_series(con, None) == {"coin": None, "trades": []}
        assert coin_trade_series(con, "") == {"coin": "", "trades": []}
    finally:
        con.close()


def test_coin_trade_series_no_outcome_tables_degrades_gracefully(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path,
        str(tmp_path / "pq"),
        ListFetcher({}),
        sources=[SOURCES_BY_NAME["regime_history"]],
    ).run()
    con = analytics_api.connect_ro(duckdb_path)
    try:
        payload = coin_trade_series(con, "C1USDT")
    finally:
        con.close()
    assert payload == {"coin": "C1USDT", "trades": []}


# ─────────────────────────────────────────────────────────────────────────────
# AK5/AK6/AK7/AK8 — Flask route: end-to-end integration test against real DuckDB
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path):
    app = dashboard_app.create_app(_build_duckdb(tmp_path))
    app.config.update(TESTING=True)
    return app.test_client()


def test_panel_coin_drilldown_renders_correct_series_and_table(client):
    """The mandatory integration test: real AnalyticsExporter -> real DuckDB
    file (closed_ai_signals + closed_trades) -> real coin_trade_series() ->
    real Flask route -> rendered HTML, with realistic fixture data (not
    hand-built dicts)."""
    resp = client.get("/panels/coin-drilldown?coin=C1USDT")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "C1USDT" in html
    assert "RUB2" in html
    # Selector lists only coins with decisive trades (C3USDT excluded).
    assert "C2USDT" in html  # present in the <option> list
    assert "C3USDT" not in html
    # Trade table values.
    assert "10.0000%" in html
    assert "-5.0000%" in html
    # Chart mount point + JSON series/meta, Lightweight-Charts factory name.
    assert 'data-chart="coin-price-line"' in html
    assert "coin-drilldown-points" in html
    assert "coin-drilldown-meta" in html


def test_panel_coin_drilldown_marker_colors_reflect_win_loss(client):
    resp = client.get("/panels/coin-drilldown?coin=C1USDT")
    html = resp.get_data(as_text=True)
    # Win marker green (#3fb950), loss marker the loss token (#d29922) —
    # asserted via the embedded JSON meta blob.
    assert "#3fb950" in html
    assert "#d29922" in html
    assert "arrowUp" in html
    assert "arrowDown" in html


def test_panel_coin_drilldown_default_load_selects_first_coin(client):
    """No ?coin= at all on first load -> defaults to the first coin
    alphabetically (C1USDT), not an empty "please choose" state."""
    resp = client.get("/panels/coin-drilldown")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "C1USDT" in html
    assert "RUB2" in html


def test_panel_coin_drilldown_unknown_coin_shows_clean_message(client):
    resp = client.get("/panels/coin-drilldown?coin=NOPE999")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Unbekannter Coin" in html
    assert "NOPE999" in html


def test_panel_coin_drilldown_empty_coin_query_param_shows_clean_message(client):
    resp = client.get("/panels/coin-drilldown?coin=")
    assert resp.status_code == 200
    assert "Unbekannter Coin" in resp.get_data(as_text=True)


def test_panel_coin_drilldown_empty_substrate(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path,
        str(tmp_path / "pq"),
        ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"], SOURCES_BY_NAME["closed_trades"]],
    ).run()
    c = dashboard_app.create_app(duckdb_path).test_client()
    resp = c.get("/panels/coin-drilldown")
    assert resp.status_code == 200
    assert "Noch keine entschiedenen Trades" in resp.get_data(as_text=True)


def test_index_includes_coin_drilldown_panel(client):
    html = client.get("/").get_data(as_text=True)
    assert 'hx-get="/panels/coin-drilldown"' in html
    assert 'id="coin-drilldown-body"' in html


def test_panel_coin_drilldown_never_touches_postgres(client, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("coin-drilldown route touched Postgres")

    monkeypatch.setattr(analytics_export.PostgresFetcher, "_connection", _boom)
    monkeypatch.setattr(analytics_export.PostgresFetcher, "fetch", _boom)
    assert client.get("/panels/coin-drilldown").status_code == 200


def test_panel_coin_drilldown_freshness_uses_ai_and_trades_sources():
    # Data-only assertion on the panel->source mapping — no Flask/DuckDB
    # needed. Mirrors test_panel_regime_heatmap_freshness_uses_regime_and_ai_signal_sources.
    assert dashboard_app.PANEL_SOURCES["coin-drilldown"] == ("closed_ai_signals", "closed_trades")


def test_coin_price_line_factory_registered_in_panels_js():
    js_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tools",
        "dashboard",
        "static",
        "js",
        "panels.js",
    )
    with open(js_path, encoding="utf-8") as f:
        content = f.read()
    assert 'registerFactory("coin-price-line"' in content
    # AK7: disposes via chart.remove() (Lightweight Charts' own API), not
    # ECharts' .dispose() — the literal call must be present in the teardown.
    assert "chart.remove()" in content


# ─────────────────────────────────────────────────────────────────────────────
# Pure-function unit tests (no DB/Flask) — _resolve_coin / _coin_chart_series
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_coin_none_defaults_to_first_available():
    assert dashboard_app._resolve_coin(None, ["A", "B"]) == "A"


def test_resolve_coin_none_with_no_coins_returns_none():
    assert dashboard_app._resolve_coin(None, []) is None


def test_resolve_coin_known_value_passes_through():
    assert dashboard_app._resolve_coin("B", ["A", "B"]) == "B"


def test_resolve_coin_unknown_or_empty_value_returns_none():
    assert dashboard_app._resolve_coin("NOPE", ["A", "B"]) is None
    assert dashboard_app._resolve_coin("", ["A", "B"]) is None


def test_coin_chart_series_two_trades_monotonic_time_and_markers():
    trades = [
        {
            "bot": "RUB2",
            "closed_at": "2026-07-01T00:00:00",
            "entry": 100.0,
            "close_price": 110.0,
            "pnl_pct": 10.0,
            "is_win": True,
        },
        {
            "bot": "RUB2",
            "closed_at": "2026-07-02T00:00:00",
            "entry": 100.0,
            "close_price": 95.0,
            "pnl_pct": -5.0,
            "is_win": False,
        },
    ]
    points, markers = dashboard_app._coin_chart_series(trades)
    assert len(points) == 4
    times = [p["time"] for p in points]
    assert times == sorted(times)
    assert len(set(times)) == 4  # strictly increasing, no collisions
    assert len(markers) == 2
    assert markers[0]["color"] == "#3fb950"  # win
    assert markers[0]["shape"] == "arrowUp"
    assert markers[1]["color"] == "#d29922"  # loss
    assert markers[1]["shape"] == "arrowDown"


def test_coin_chart_series_empty_trades_yields_empty_series():
    points, markers = dashboard_app._coin_chart_series([])
    assert points == []
    assert markers == []

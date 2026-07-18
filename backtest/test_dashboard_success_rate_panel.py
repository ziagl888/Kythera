# backtest/test_dashboard_success_rate_panel.py
"""DB-free tests for the Z1 dashboard success-rate TIME-COMPARISON panel
(Feature 3, T-2026-CU-9050-155).

Mirrors backtest/test_dashboard_leaderboard.py's fixture style: the Postgres
boundary is replaced by a synthetic ``ListFetcher`` feeding the real
``AnalyticsExporter`` into a temporary DuckDB file, using the ACTUAL
``closed_ai_signals`` column names from ``tools/analytics_export.py`` and
realistic, versioned model tags (RUB2/ABR2).

Fixture design (the T-152/T-154 lesson: divergent windows, not fixtures where
7d/30d/90d accidentally look identical): RUB2 has three early WINS followed by
four recent LOSSES, so its rolling win-rate at the last data day genuinely
DIFFERS across the 7/30/90d windows (0%, 20%, ~42.9%). ABR2 has its own,
independently-diverging pattern (66.7%, 75%, 80%). Any swapped bot filter or
wrong rolling-window boundary changes at least one of these six numbers, so a
regression is caught by an exact-value assertion, not a shape assertion.

Covers SPEC.md AK1-AK7 (tools/dashboard/SPEC.md, Feature 3 section). Run with:
    pytest backtest/test_dashboard_success_rate_panel.py -v
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
from tools.analytics_api import (  # noqa: E402
    DEFAULT_TIMESERIES_WINDOW,
    TIMESERIES_WINDOWS,
    _daily_buckets_by_bot,
    _rolling_series_for_bot,
    rolling_success_rate_series,
)
from tools.analytics_export import SOURCES_BY_NAME, AnalyticsExporter  # noqa: E402
from tools.dashboard import app as dashboard_app  # noqa: E402

_BASE = datetime.datetime(2026, 7, 1, 12, 0, 0)  # noqa: DTZ001  (naive-local, per exporter contract)


def _day(n: int) -> datetime.datetime:
    return _BASE + datetime.timedelta(days=n)


# ─────────────────────────────────────────────────────────────────────────────
# Pure-function tests — _rolling_series_for_bot / _daily_buckets_by_bot
# (no DuckDB, no Flask; hand-built dicts, exact hand-computed expectations)
# ─────────────────────────────────────────────────────────────────────────────


def test_daily_buckets_by_bot_groups_by_date_and_bot():
    trades = [
        {"bot": "RUB2", "closed_at": _day(5), "pnl_pct": 10.0, "is_win": True},
        {"bot": "RUB2", "closed_at": _day(5), "pnl_pct": -3.0, "is_win": False},  # same day, 2nd trade
        {"bot": "RUB2", "closed_at": _day(6), "pnl_pct": 5.0, "is_win": True},
        {"bot": "ABR2", "closed_at": _day(5), "pnl_pct": 2.0, "is_win": True},
    ]
    by_bot = _daily_buckets_by_bot(trades)
    assert set(by_bot) == {"RUB2", "ABR2"}
    # RUB2 day5: 2 trades (1 win, 1 loss) bucketed together, not split.
    assert by_bot["RUB2"][_day(5).date()] == (2, 1)
    assert by_bot["RUB2"][_day(6).date()] == (1, 1)
    assert by_bot["ABR2"][_day(5).date()] == (1, 1)


def _rub2_daily() -> dict[datetime.date, tuple[int, int]]:
    """3 early wins (days 5/6/10), 4 recent losses (days 35-38) — the fixture
    that makes 7d/30d/90d rolling win-rate at day 38 genuinely diverge."""
    return {
        _day(5).date(): (1, 1),
        _day(6).date(): (1, 1),
        _day(10).date(): (1, 1),
        _day(35).date(): (1, 0),
        _day(36).date(): (1, 0),
        _day(37).date(): (1, 0),
        _day(38).date(): (1, 0),
    }


def test_rolling_series_for_bot_trailing_window_boundary():
    """At day 10, a 7d window must NOT yet include day 5/6 (cutoff = 10-7=3,
    so day5/day6 ARE included since 5>3 and 6>3) — verify the half-open
    boundary precisely: day 10 with window=4 excludes day 5/6 (cutoff=6,
    5<=6 and 6<=6 are both excluded) but includes day 10 itself."""
    daily = _rub2_daily()
    series = _rolling_series_for_bot(daily, window=4)
    by_date = {p["date"]: p for p in series}
    day10 = by_date[_day(10).date().isoformat()]
    # cutoff = 10 - 4 = 6; only dates > 6 count -> day10 itself only (day5,day6 excluded)
    assert day10["n"] == 1
    assert day10["wins"] == 1
    assert day10["winrate"] == pytest.approx(1.0)


def test_rolling_series_for_bot_windows_diverge_at_last_day():
    """The mutation check: 7d, 30d, 90d rolling win-rate at the last data day
    (day 38) must be three DIFFERENT numbers for this fixture. A wrong window
    boundary (off-by-one, wrong comparison operator) or a window mixup would
    collapse two of these to the same (wrong) value."""
    daily = _rub2_daily()
    last_date = _day(38).date().isoformat()

    s7 = {p["date"]: p for p in _rolling_series_for_bot(daily, window=7)}[last_date]
    s30 = {p["date"]: p for p in _rolling_series_for_bot(daily, window=30)}[last_date]
    s90 = {p["date"]: p for p in _rolling_series_for_bot(daily, window=90)}[last_date]

    # window=7: cutoff=31 -> days 35,36,37,38 (4 losses) -> 0/4
    assert s7 == {"date": last_date, "n": 4, "wins": 0, "winrate": 0.0}
    # window=30: cutoff=8 -> days 10,35,36,37,38 (1 win / 5) -> 0.2
    assert s30["n"] == 5 and s30["wins"] == 1
    assert s30["winrate"] == pytest.approx(0.2)
    # window=90: cutoff=-52 -> all 7 days (3 wins / 7)
    assert s90["n"] == 7 and s90["wins"] == 3
    assert s90["winrate"] == pytest.approx(3 / 7)

    # All three genuinely differ — no accidental window collapse.
    assert len({s7["winrate"], s30["winrate"], s90["winrate"]}) == 3


def test_rolling_series_for_bot_empty_daily_returns_empty_list():
    assert _rolling_series_for_bot({}, window=30) == []


# ─────────────────────────────────────────────────────────────────────────────
# DB-backed fixture — real AnalyticsExporter -> real DuckDB, ListFetcher
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
    i: int, *, model: str, direction: str, entry: float, close: float,
    close_time: datetime.datetime, status: str = "TP1",
) -> dict[str, Any]:
    return {
        "id": i, "symbol": f"C{i}USDT", "model": model, "direction": direction,
        "entry": entry, "close_price": close, "targets_hit": 1 if close != entry else 0,
        "open_time": close_time - datetime.timedelta(hours=1),
        "close_time": close_time, "status": status, "lev": "20x",
    }


# RUB2: wins day5/6/10, losses day35/36/37/38 -> diverges 0% / 20% / ~42.9%
#       across 7d/30d/90d (see pure-function test above for the arithmetic).
# ABR2: independent pattern -- wins day1/9/36/37, loss day38 -> diverges
#       66.7% / 75% / 80% across 7d/30d/90d. Distinct from RUB2's numbers at
#       every window, so a swapped bot filter is caught by an exact mismatch.
def _fixture_rows() -> list[dict[str, Any]]:
    return [
        _ai_row(1, model="RUB2", direction="LONG", entry=100, close=110, close_time=_day(5)),
        _ai_row(2, model="RUB2", direction="LONG", entry=100, close=105, close_time=_day(6)),
        _ai_row(3, model="RUB2", direction="LONG", entry=100, close=120, close_time=_day(10)),
        _ai_row(4, model="RUB2", direction="SHORT", entry=100, close=110, close_time=_day(35)),  # loss
        _ai_row(5, model="RUB2", direction="SHORT", entry=100, close=115, close_time=_day(36)),  # loss
        _ai_row(6, model="RUB2", direction="SHORT", entry=100, close=105, close_time=_day(37)),  # loss
        _ai_row(7, model="RUB2", direction="SHORT", entry=100, close=120, close_time=_day(38)),  # loss
        _ai_row(8, model="ABR2", direction="LONG", entry=100, close=130, close_time=_day(1)),
        _ai_row(9, model="ABR2", direction="LONG", entry=100, close=125, close_time=_day(9)),
        _ai_row(10, model="ABR2", direction="LONG", entry=100, close=140, close_time=_day(36)),
        _ai_row(11, model="ABR2", direction="LONG", entry=100, close=150, close_time=_day(37)),
        _ai_row(12, model="ABR2", direction="SHORT", entry=100, close=110, close_time=_day(38)),  # loss
    ]


def _build_duckdb(tmp_path) -> str:
    duckdb_path = str(tmp_path / "analytics.duckdb")
    AnalyticsExporter(
        duckdb_path, str(tmp_path / "parquet"), ListFetcher({"closed_ai_signals": _fixture_rows()}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    return duckdb_path


# ─────────────────────────────────────────────────────────────────────────────
# rolling_success_rate_series() — the analytics-layer function
# ─────────────────────────────────────────────────────────────────────────────


def test_rolling_success_rate_series_multi_bot_diverges_per_window(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        r7 = rolling_success_rate_series(con, window=7)
        r30 = rolling_success_rate_series(con, window=30)
        r90 = rolling_success_rate_series(con, window=90)
    finally:
        con.close()

    assert set(r30["bots"]) == {"RUB2", "ABR2"}
    last_day38 = _day(38).date().isoformat()

    rub2_7 = {p["date"]: p for p in r7["series"]["RUB2"]}[last_day38]
    rub2_30 = {p["date"]: p for p in r30["series"]["RUB2"]}[last_day38]
    rub2_90 = {p["date"]: p for p in r90["series"]["RUB2"]}[last_day38]
    assert rub2_7["winrate"] == pytest.approx(0.0)
    assert rub2_30["winrate"] == pytest.approx(0.2)
    assert rub2_90["winrate"] == pytest.approx(3 / 7)

    abr2_7 = {p["date"]: p for p in r7["series"]["ABR2"]}[last_day38]
    abr2_30 = {p["date"]: p for p in r30["series"]["ABR2"]}[last_day38]
    abr2_90 = {p["date"]: p for p in r90["series"]["ABR2"]}[last_day38]
    assert abr2_7["winrate"] == pytest.approx(2 / 3)
    assert abr2_30["winrate"] == pytest.approx(0.75)
    assert abr2_90["winrate"] == pytest.approx(0.8)


def test_rolling_success_rate_series_bot_multiselect_filters(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        only_rub2 = rolling_success_rate_series(con, bots=["RUB2"], window=DEFAULT_TIMESERIES_WINDOW)
    finally:
        con.close()
    assert only_rub2["bots"] == ["RUB2"]
    assert "ABR2" not in only_rub2["series"]


def test_rolling_success_rate_series_empty_substrate_degrades_gracefully(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path, str(tmp_path / "pq"), ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    con = analytics_api.connect_ro(duckdb_path)
    try:
        payload = rolling_success_rate_series(con)
    finally:
        con.close()
    assert payload == {"window": DEFAULT_TIMESERIES_WINDOW, "bots": [], "series": {}}


# ─────────────────────────────────────────────────────────────────────────────
# app.py dashboard-layer helper — _selected_bots explicit-vs-default semantics
# ─────────────────────────────────────────────────────────────────────────────


def test_selected_bots_defaults_to_all_when_not_filtered():
    result = dashboard_app._selected_bots([], filtered=False, all_bots=["ABR2", "RUB2"])
    assert result == ["ABR2", "RUB2"]


def test_selected_bots_respects_explicit_empty_selection():
    """A real form submission with every checkbox unchecked must NOT silently
    fall back to 'all bots' -- filtered=True + an empty list means the user
    deliberately deselected everything."""
    result = dashboard_app._selected_bots([], filtered=True, all_bots=["ABR2", "RUB2"])
    assert result == []


def test_selected_bots_respects_explicit_partial_selection():
    result = dashboard_app._selected_bots(["RUB2"], filtered=True, all_bots=["ABR2", "RUB2"])
    assert result == ["RUB2"]


# ─────────────────────────────────────────────────────────────────────────────
# Flask route — integration test: real exporter -> DuckDB -> route -> HTML
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path):
    app = dashboard_app.create_app(_build_duckdb(tmp_path))
    app.config.update(TESTING=True)
    return app.test_client()


def test_panel_default_load_selects_all_bots_and_default_window(client):
    """The mandatory integration test: real AnalyticsExporter -> real DuckDB
    file -> real rolling_success_rate_series() -> real Flask route -> rendered
    HTML, with realistic closed_ai_signals fixture data (not hand-built dicts).
    No query params (the plain 'load' trigger) -> all bots, default window."""
    resp = client.get("/panels/success-rate-timeseries")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "RUB2" in html and "ABR2" in html
    assert f'value="{DEFAULT_TIMESERIES_WINDOW}" checked' in html
    assert 'data-chart="winrate-timeseries"' in html
    assert 'id="winrate-timeseries-series"' in html
    # Default-window (30d) exact win-rate values embedded in the JSON series.
    assert "20.0" in html   # RUB2 30d winrate at day 38 (0.2 -> 20.0%)
    assert "75.0" in html   # ABR2 30d winrate at day 38 (0.75 -> 75.0%)


def test_panel_multiselect_two_bots_renders_two_series(client):
    resp = client.get("/panels/success-rate-timeseries?filtered=1&window=90&bots=RUB2&bots=ABR2")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Both bots present as separate chart-series entries (one line each).
    assert '"bot": "RUB2"' in html
    assert '"bot": "ABR2"' in html
    # Both checkboxes checked, plus the 90d window radio.
    assert html.count("checked") == 3


def test_panel_single_bot_selection_renders_one_series(client):
    """The bot-multiselect FORM always lists every available bot as a
    checkbox label (so the user can re-select it later) -- the assertion
    below therefore targets the embedded chart-series JSON specifically
    (``"bot": "X"``), not a bare substring match against the whole page,
    which would also match the unchecked ABR2 label."""
    resp = client.get("/panels/success-rate-timeseries?filtered=1&window=90&bots=RUB2")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '"bot": "RUB2"' in html
    assert '"bot": "ABR2"' not in html
    # The form still offers ABR2 as a (currently unchecked) option.
    assert 'value="ABR2"' in html and "checked" not in html.split('value="ABR2"')[1][:40]


def test_panel_window_switch_changes_rendered_values(client):
    """Explicit divergence check at the route layer, not just the pure
    function: switching window=7 -> window=90 for the same bot selection must
    change the rendered win-rate numbers (RUB2: 0% at 7d vs ~42.9% at 90d)."""
    resp7 = client.get("/panels/success-rate-timeseries?filtered=1&window=7&bots=RUB2")
    resp90 = client.get("/panels/success-rate-timeseries?filtered=1&window=90&bots=RUB2")
    html7 = resp7.get_data(as_text=True)
    html90 = resp90.get_data(as_text=True)
    assert "0.0" in html7    # RUB2 7d winrate at day38 = 0%
    assert "42.9" in html90  # RUB2 90d winrate at day38 = 3/7 = 42.857.. -> 42.9%
    assert html7 != html90


def test_panel_explicit_empty_selection_shows_message(client):
    resp = client.get("/panels/success-rate-timeseries?filtered=1&window=30")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Keine Bots ausgewählt" in html
    assert 'data-chart="winrate-timeseries"' not in html


def test_panel_bad_window_falls_back_to_default(client):
    resp = client.get("/panels/success-rate-timeseries?window=notanint")
    assert resp.status_code == 200
    assert f'value="{DEFAULT_TIMESERIES_WINDOW}" checked' in resp.get_data(as_text=True)

    resp2 = client.get("/panels/success-rate-timeseries?window=999")
    assert resp2.status_code == 200
    assert f'value="{DEFAULT_TIMESERIES_WINDOW}" checked' in resp2.get_data(as_text=True)


def test_panel_empty_duckdb_degrades_gracefully(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path, str(tmp_path / "pq"), ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    c = dashboard_app.create_app(duckdb_path).test_client()
    resp = c.get("/panels/success-rate-timeseries")
    assert resp.status_code == 200
    assert "keine entschiedenen Trades" in resp.get_data(as_text=True)


def test_panel_all_windows_offered_in_switcher(client):
    html = client.get("/panels/success-rate-timeseries").get_data(as_text=True)
    for w in TIMESERIES_WINDOWS:
        assert f'value="{w}"' in html


def test_index_includes_timeseries_panel(client):
    html = client.get("/").get_data(as_text=True)
    assert 'hx-get="/panels/success-rate-timeseries"' in html
    assert 'id="success-rate-timeseries-body"' in html
    assert "every 30s" in html


def test_panel_never_touches_postgres(client, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("success-rate-timeseries route touched Postgres")

    monkeypatch.setattr(analytics_export.PostgresFetcher, "_connection", _boom)
    monkeypatch.setattr(analytics_export.PostgresFetcher, "fetch", _boom)
    assert client.get("/panels/success-rate-timeseries").status_code == 200


def test_existing_success_rate_demo_route_untouched(client):
    """Feature 3 adds a NEW route rather than repurposing the T-151 demo panel
    -- the old /panels/success-rate route must keep working unmodified."""
    resp = client.get("/panels/success-rate")
    assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Chart-lifecycle wiring — the new factory is registered via the shared helper
# ─────────────────────────────────────────────────────────────────────────────


def test_winrate_timeseries_factory_registered_in_panels_js():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js_path = os.path.join(repo_root, "tools", "dashboard", "static", "js", "panels.js")
    with open(js_path, encoding="utf-8") as f:
        js = f.read()
    assert 'registerFactory("winrate-timeseries"' in js
    # Registered through the shared lifecycle helper, not a bespoke mount path.
    assert "ChartLifecycle.registerFactory" in js

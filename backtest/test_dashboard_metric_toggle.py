# backtest/test_dashboard_metric_toggle.py
"""DB-free tests for the Z1 dashboard global success-metric toggle
(Feature 5, T-2026-CU-9050-157): Winrate / Expectancy / Netto-PnL.

Mirrors backtest/test_dashboard_leaderboard.py's fixture style: the Postgres
boundary is replaced by a synthetic ``ListFetcher`` feeding the real
``AnalyticsExporter`` into a temporary DuckDB file, using the ACTUAL
``closed_ai_signals`` column names from ``tools/analytics_export.py``.

Covers:
  - the pure metric -> sort_by mapping (resolve_metric / metric_sort_by),
    testable without Flask/DuckDB at all;
  - the mandatory integration test: a real DuckDB with a fixture whose three
    metrics rank the same three bots in three DIFFERENT orders, so a wrong or
    ignored metric -> sort_by mapping is caught by a changed rendered order,
    not just a changed number (the mutation check the SPEC calls for);
  - the shell (``GET /``) toggle: default, all three explicit values, and an
    unknown value never 500ing.

Run with:
    pytest backtest/test_dashboard_metric_toggle.py -v
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import analytics_api  # noqa: E402
from tools.analytics_export import SOURCES_BY_NAME, AnalyticsExporter  # noqa: E402
from tools.dashboard import app as dashboard_app  # noqa: E402

_BASE = datetime.datetime(2026, 7, 18, 12, 0, 0)  # noqa: DTZ001  (naive-local, per exporter contract)


# ─────────────────────────────────────────────────────────────────────────────
# AK — pure mapping: resolve_metric() / metric_sort_by() (no Flask, no DuckDB)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("winrate", "winrate"),
        ("expectancy", "expectancy"),
        ("netto-pnl", "netto-pnl"),
    ],
)
def test_resolve_metric_passes_through_known_values(raw, expected):
    assert dashboard_app.resolve_metric(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "bogus", "PNL", "win_rate", "netto_pnl"])
def test_resolve_metric_falls_back_to_default_for_unknown_or_missing(raw):
    assert dashboard_app.resolve_metric(raw) == dashboard_app.DEFAULT_METRIC


def test_default_metric_is_netto_pnl_matching_leaderboard_default():
    """Sensible default (SPEC): netto-pnl / pnl_sum_pct — the same metric
    analytics_api.DEFAULT_LEADERBOARD_SORT already defaults to, so an
    unrendered toggle reproduces pre-Feature-5 leaderboard behaviour exactly."""
    assert dashboard_app.DEFAULT_METRIC == "netto-pnl"
    assert dashboard_app.metric_sort_by(dashboard_app.DEFAULT_METRIC) == analytics_api.DEFAULT_LEADERBOARD_SORT


@pytest.mark.parametrize(
    "metric,expected_sort_by",
    [
        ("winrate", "winrate"),
        ("expectancy", "expectancy_pct"),
        ("netto-pnl", "pnl_sum_pct"),
    ],
)
def test_metric_sort_by_maps_onto_leaderboard_sort_keys(metric, expected_sort_by):
    """Every mapped sort_by must be one of bot_leaderboard's accepted keys —
    a typo here would silently fall back inside bot_leaderboard() itself,
    which is exactly the failure mode this parametrized check guards."""
    sort_by = dashboard_app.metric_sort_by(metric)
    assert sort_by == expected_sort_by
    assert sort_by in analytics_api._LEADERBOARD_SORT_KEYS


def test_metric_sort_by_unresolved_value_falls_back_to_default_sort_by():
    assert dashboard_app.metric_sort_by("not-a-real-metric") == analytics_api.DEFAULT_LEADERBOARD_SORT


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic fetcher + a fixture whose three metrics rank 3 bots 3 DIFFERENT
# ways — the mutation check: a wrong/ignored metric->sort_by mapping produces
# a rendered order that matches one of the OTHER two metrics instead.
# ─────────────────────────────────────────────────────────────────────────────


class ListFetcher:
    def __init__(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self.data = data

    def fetch(self, spec, cursor, limit):  # noqa: ANN001
        rows = [r for r in self.data.get(spec.name, [])]
        if spec.name == "closed_ai_signals":
            rows = [r for r in rows if r.get("close_time") is not None and r.get("status") != "ENTRY_NOT_FILLED"]
        rows.sort(key=lambda r: (r[spec.ts_col], r[spec.pk_col]))
        if cursor is not None:
            rows = [r for r in rows if (r[spec.ts_col], r[spec.pk_col]) > cursor]
        return rows[:limit]


def _ai_row(i: int, *, model: str, entry: float, close: float, close_time: datetime.datetime) -> dict[str, Any]:
    return {
        "id": i,
        "symbol": f"C{i}USDT",
        "model": model,
        "direction": "LONG",
        "entry": entry,
        "close_price": close,
        "targets_hit": 1 if close != entry else 0,
        "open_time": close_time - datetime.timedelta(hours=1),
        "close_time": close_time,
        "status": "TP1",
        "lev": "20x",
    }


def _day(n: int) -> datetime.datetime:
    return _BASE + datetime.timedelta(days=n)


# Three bots, each metric ranks them in a DIFFERENT order:
#   MET1 (few, large trades):  n=2,  wins=1,  winrate=0.500, pnl_sum=+10.00, expectancy=+5.0000
#   MET2 (many, small wins):   n=21, wins=20, winrate=0.952, pnl_sum=+9.00,  expectancy=+0.4286
#   MET3 (moderate):           n=4,  wins=3,  winrate=0.750, pnl_sum=+13.00, expectancy=+3.2500
#
#   netto-pnl  desc: MET3(13.00) > MET1(10.00) > MET2(9.00)
#   winrate    desc: MET2(0.952) > MET3(0.750) > MET1(0.500)
#   expectancy desc: MET1(5.0000) > MET3(3.2500) > MET2(0.4286)
# All three orderings are distinct permutations of {MET1, MET2, MET3} — a
# metric wired to the wrong sort_by (or ignored entirely, defaulting to
# netto-pnl) renders one of the OTHER two orders instead, which the
# assertions below catch.
def _fixture_rows() -> list[dict[str, Any]]:
    rows = [
        _ai_row(1, model="MET1", entry=100, close=140, close_time=_day(1)),   # +40%, win
        _ai_row(2, model="MET1", entry=100, close=70, close_time=_day(2)),    # -30%, loss
        _ai_row(100, model="MET3", entry=100, close=105, close_time=_day(1)),  # +5%, win
        _ai_row(101, model="MET3", entry=100, close=105, close_time=_day(2)),  # +5%, win
        _ai_row(102, model="MET3", entry=100, close=105, close_time=_day(3)),  # +5%, win
        _ai_row(103, model="MET3", entry=100, close=98, close_time=_day(4)),   # -2%, loss
    ]
    # MET2: 20 small wins (+0.5%) + 1 small loss (-1%) -> pnl_sum=9.0, winrate 20/21.
    for i in range(20):
        rows.append(_ai_row(200 + i, model="MET2", entry=100, close=100.5, close_time=_day(1) + datetime.timedelta(hours=i)))
    rows.append(_ai_row(300, model="MET2", entry=100, close=99, close_time=_day(1) + datetime.timedelta(hours=20)))
    return rows


def _build_duckdb(tmp_path) -> str:
    duckdb_path = str(tmp_path / "analytics.duckdb")
    parquet_root = str(tmp_path / "parquet")
    AnalyticsExporter(
        duckdb_path,
        parquet_root,
        ListFetcher({"closed_ai_signals": _fixture_rows()}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    return duckdb_path


@pytest.fixture()
def client(tmp_path):
    app = dashboard_app.create_app(_build_duckdb(tmp_path))
    app.config.update(TESTING=True)
    return app.test_client()


# ─────────────────────────────────────────────────────────────────────────────
# AK — integration: GET /panels/leaderboard?metric=... sorts + highlights
# ─────────────────────────────────────────────────────────────────────────────


def test_leaderboard_panel_default_sorts_by_netto_pnl(client):
    html = client.get("/panels/leaderboard").get_data(as_text=True)
    positions = {b: html.index(b) for b in ("MET1", "MET2", "MET3")}
    assert positions["MET3"] < positions["MET1"] < positions["MET2"]
    assert 'class="metric-highlight"' in html  # PnL column is highlighted by default


def test_leaderboard_panel_metric_netto_pnl_explicit(client):
    html = client.get("/panels/leaderboard?metric=netto-pnl").get_data(as_text=True)
    positions = {b: html.index(b) for b in ("MET1", "MET2", "MET3")}
    assert positions["MET3"] < positions["MET1"] < positions["MET2"]


def test_leaderboard_panel_metric_winrate_reorders_and_highlights(client):
    html = client.get("/panels/leaderboard?metric=winrate").get_data(as_text=True)
    positions = {b: html.index(b) for b in ("MET1", "MET2", "MET3")}
    # winrate desc: MET2 (0.952) > MET3 (0.750) > MET1 (0.500) — a DIFFERENT
    # order than the netto-pnl default (MET3 > MET1 > MET2) above.
    assert positions["MET2"] < positions["MET3"] < positions["MET1"]
    assert "Win-Rate" in html
    # The Win-Rate <th> carries the highlight class for this metric (its
    # opening tag precedes the "Win-Rate" text node).
    win_rate_th = html[html.index("<th", html.index("Bot")) : html.index("Win-Rate")]
    assert 'class="metric-highlight"' in win_rate_th


def test_leaderboard_panel_metric_expectancy_reorders(client):
    html = client.get("/panels/leaderboard?metric=expectancy").get_data(as_text=True)
    positions = {b: html.index(b) for b in ("MET1", "MET2", "MET3")}
    # expectancy desc: MET1 (5.0) > MET3 (3.25) > MET2 (0.4286) — the THIRD
    # distinct ordering, different from both netto-pnl and winrate above.
    assert positions["MET1"] < positions["MET3"] < positions["MET2"]


def test_leaderboard_panel_unknown_metric_falls_back_to_default_no_500(client):
    resp = client.get("/panels/leaderboard?metric=not-a-real-metric")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    positions = {b: html.index(b) for b in ("MET1", "MET2", "MET3")}
    # Falls back to netto-pnl (the default) — same order as the explicit
    # netto-pnl case, NOT a 500 and NOT some other silently-broken ordering.
    assert positions["MET3"] < positions["MET1"] < positions["MET2"]


# ─────────────────────────────────────────────────────────────────────────────
# AK — shell toggle: GET / renders the control, reflects the active metric,
# bakes the resolved metric into the leaderboard panel's own poll URL
# ─────────────────────────────────────────────────────────────────────────────


def test_index_renders_metric_toggle_with_default_active(client):
    html = client.get("/").get_data(as_text=True)
    assert 'class="metric-toggle"' in html
    for label in ("Winrate", "Expectancy", "Netto-PnL"):
        assert label in html
    # Default (netto-pnl) is the active option.
    active_start = html.index("metric-toggle__option--active")
    assert "Netto-PnL" in html[active_start : active_start + 80]
    # The resolved metric is baked into the leaderboard panel's own hx-get URL
    # so its "every Ns" poll keeps using it without a further round-trip.
    assert 'hx-get="/panels/leaderboard?metric=netto-pnl"' in html


def test_index_metric_query_param_selects_active_toggle_option(client):
    html = client.get("/?metric=winrate").get_data(as_text=True)
    active_start = html.index("metric-toggle__option--active")
    assert "Winrate" in html[active_start : active_start + 80]
    assert 'hx-get="/panels/leaderboard?metric=winrate"' in html


def test_index_unknown_metric_query_param_falls_back_no_500(client):
    resp = client.get("/?metric=not-a-real-metric")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    active_start = html.index("metric-toggle__option--active")
    assert "Netto-PnL" in html[active_start : active_start + 80]
    assert 'hx-get="/panels/leaderboard?metric=netto-pnl"' in html


def test_toggle_never_touches_postgres(client, monkeypatch):
    from tools import analytics_export

    def _boom(*_a, **_k):
        raise AssertionError("metric toggle touched Postgres")

    monkeypatch.setattr(analytics_export.PostgresFetcher, "_connection", _boom)
    monkeypatch.setattr(analytics_export.PostgresFetcher, "fetch", _boom)
    assert client.get("/?metric=winrate").status_code == 200
    assert client.get("/panels/leaderboard?metric=expectancy").status_code == 200

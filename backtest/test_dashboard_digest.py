# backtest/test_dashboard_digest.py
"""DB-free tests for the Z1 dashboard Overnight-Digest landing summary
(Feature 8, T-2026-CU-9050-160, F1).

Mirrors backtest/test_dashboard_leaderboard.py / test_dashboard_regime_heatmap.py's
fixture style: the Postgres boundary is replaced by a synthetic ``ListFetcher``
feeding the real ``AnalyticsExporter`` into a temporary DuckDB file, using the
ACTUAL ``closed_ai_signals`` and ``regime_history`` column names from
``tools/analytics_export.py`` (closed_ai_signals: id, symbol, model, direction,
entry, close_price, targets_hit, open_time, close_time, status, lev;
regime_history: id, ts, regime, alt_context, btc_price, confidence,
confidence_btc, confidence_alt) — the T-152 review lesson: unrealistic fixture
keys can mask a real bug.

Covers SPEC.md Feature 8 AK1-AK8. Run with:
    pytest backtest/test_dashboard_digest.py -v
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import (
    analytics_api,  # noqa: E402
    analytics_export,  # noqa: E402
)
from tools.analytics_api import overnight_digest  # noqa: E402
from tools.analytics_export import SOURCES_BY_NAME, AnalyticsExporter  # noqa: E402
from tools.dashboard import app as dashboard_app  # noqa: E402
from tools.dashboard.app import resolve_digest_window  # noqa: E402

# The fixture's most recent closed_ai_signals row sits exactly AT this
# timestamp, so overnight_digest()'s default as_of=None (max(closed_at) in the
# substrate) resolves to precisely this value — every window-hours offset
# below is relative to it.
AS_OF = datetime.datetime(2026, 7, 17, 12, 0, 0)  # noqa: DTZ001  (naive-local, per exporter contract)


def _h(offset: float) -> datetime.datetime:
    return AS_OF + datetime.timedelta(hours=offset)


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


# Fixture: 6 decisive trades inside the default 8h window (hand-computable),
# plus two decoys placed to catch a broken window filter:
#   - BND1 sits EXACTLY on the lower boundary (as_of - 8h) — must be EXCLUDED
#     (half-open window: closed_at > as_of - 8h, not >=).
#   - OLD1 sits well outside the window (as_of - 20h) — must be EXCLUDED too.
# Both carry large, distinctive PnL values (+77%, +33%) so an accidentally
# widened window inflates n/pnl_sum_pct/top_bot in an assertion-visible way.
#
#   RUB2: +10 @ h=0, +5 @ h=-3   -> pnl_sum=+15, n=2, wins=2
#   ABR2: -20 @ h=-2, +2 @ h=-6  -> pnl_sum=-18, n=2, wins=1  (FLOP bot)
#   MIS2: +50 @ h=-4, -1 @ h=-7  -> pnl_sum=+49, n=2, wins=1  (TOP bot, best trade)
#   in-window totals: n=6, wins=4, pnl_sum=46, winrate=4/6=0.6667
#   best_trade = MIS2 +50% (coin C5USDT), worst_trade = ABR2 -20% (coin C3USDT)
def _fixture_rows() -> list[dict[str, Any]]:
    return [
        _ai_row(1, model="RUB2", entry=100, close=110, close_time=_h(0)),  # +10%
        _ai_row(2, model="RUB2", entry=100, close=105, close_time=_h(-3)),  # +5%
        _ai_row(3, model="ABR2", entry=100, close=80, close_time=_h(-2)),  # -20%
        _ai_row(4, model="ABR2", entry=100, close=102, close_time=_h(-6)),  # +2%
        _ai_row(5, model="MIS2", entry=100, close=150, close_time=_h(-4)),  # +50%
        _ai_row(6, model="MIS2", entry=100, close=99, close_time=_h(-7)),  # -1%
        _ai_row(7, model="BND1", entry=100, close=177, close_time=_h(-8)),  # +77%, boundary — EXCLUDED
        _ai_row(8, model="OLD1", entry=100, close=133, close_time=_h(-20)),  # +33%, outside — EXCLUDED
    ]


# regime_history: CHOP (h=-30, first ever — no predecessor, not a transition),
# CHOP (h=-9, repeat, also outside the 8h window), TREND (h=-5, REAL
# transition, in-window), TREND (h=-2, repeat, in-window — not counted),
# CHOP (h=-1, REAL transition, in-window) -> 2 real transitions in the
# default 8h window.
def _regime_fixture_rows() -> list[dict[str, Any]]:
    return [
        _regime_row(1, ts=_h(-30), regime="CHOP"),
        _regime_row(2, ts=_h(-9), regime="CHOP"),
        _regime_row(3, ts=_h(-5), regime="TREND"),
        _regime_row(4, ts=_h(-2), regime="TREND"),
        _regime_row(5, ts=_h(-1), regime="CHOP"),
    ]


def _build_duckdb(tmp_path, *, with_regime: bool = True) -> str:
    duckdb_path = str(tmp_path / "analytics.duckdb")
    parquet_root = str(tmp_path / "parquet")
    sources = [SOURCES_BY_NAME["closed_ai_signals"]]
    data = {"closed_ai_signals": _fixture_rows()}
    if with_regime:
        sources.append(SOURCES_BY_NAME["regime_history"])
        data["regime_history"] = _regime_fixture_rows()
    AnalyticsExporter(duckdb_path, parquet_root, ListFetcher(data), sources=sources).run()
    return duckdb_path


# ─────────────────────────────────────────────────────────────────────────────
# AK1 — overnight_digest() basic aggregates
# ─────────────────────────────────────────────────────────────────────────────


def test_overnight_digest_basic_aggregates(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = overnight_digest(con, 8)
    finally:
        con.close()

    assert payload["as_of"] == AS_OF.isoformat()
    assert payload["window_hours"] == 8
    assert payload["n"] == 6
    assert payload["wins"] == 4
    assert payload["pnl_sum_pct"] == pytest.approx(46.0)
    assert payload["winrate"] == pytest.approx(4 / 6)


# ─────────────────────────────────────────────────────────────────────────────
# AK2 — window boundary (half-open, MUTATION-CHECK)
# ─────────────────────────────────────────────────────────────────────────────


def test_overnight_digest_window_boundary_excludes_outside_trade(tmp_path):
    """BND1 (+77%, exactly at as_of - 8h) and OLD1 (+33%, well outside) must
    NOT contribute. A widened window (e.g. >= instead of >, or a dropped
    filter) would inflate n/pnl_sum_pct/top_bot in an assertion-visible way."""
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = overnight_digest(con, 8)
    finally:
        con.close()

    assert payload["n"] == 6  # not 7 (BND1) or 8 (BND1+OLD1)
    assert payload["pnl_sum_pct"] == pytest.approx(46.0)  # not 123.0 or 156.0
    bots_seen = {payload["top_bot"]["bot"], payload["flop_bot"]["bot"]}
    assert "BND1" not in bots_seen
    assert "OLD1" not in bots_seen


# ─────────────────────────────────────────────────────────────────────────────
# AK3 — top/flop bot sort (MUTATION-CHECK)
# ─────────────────────────────────────────────────────────────────────────────


def test_overnight_digest_top_and_flop_bot_correct(tmp_path):
    """MIS2 (+49) is highest, ABR2 (-18) is lowest, RUB2 (+15) is in between —
    a wrong/reversed sort would surface RUB2 or ABR2 as top, or MIS2/RUB2 as
    flop."""
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = overnight_digest(con, 8)
    finally:
        con.close()

    assert payload["top_bot"]["bot"] == "MIS2"
    assert payload["top_bot"]["pnl_sum_pct"] == pytest.approx(49.0)
    assert payload["flop_bot"]["bot"] == "ABR2"
    assert payload["flop_bot"]["pnl_sum_pct"] == pytest.approx(-18.0)


# ─────────────────────────────────────────────────────────────────────────────
# AK4 — notable trades (biggest win/loss)
# ─────────────────────────────────────────────────────────────────────────────


def test_overnight_digest_notable_trades_correct(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = overnight_digest(con, 8)
    finally:
        con.close()

    assert payload["best_trade"]["bot"] == "MIS2"
    assert payload["best_trade"]["coin"] == "C5USDT"
    assert payload["best_trade"]["pnl_pct"] == pytest.approx(50.0)

    assert payload["worst_trade"]["bot"] == "ABR2"
    assert payload["worst_trade"]["coin"] == "C3USDT"
    assert payload["worst_trade"]["pnl_pct"] == pytest.approx(-20.0)


# ─────────────────────────────────────────────────────────────────────────────
# AK5 — empty window / empty substrate degrade cleanly
# ─────────────────────────────────────────────────────────────────────────────


def test_overnight_digest_empty_window_degrades_cleanly(tmp_path):
    """Substrate HAS decisive trades, but none fall in the window when
    anchored far past every one of them."""
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        far_future = AS_OF + datetime.timedelta(hours=100)
        payload = overnight_digest(con, 8, as_of=far_future)
    finally:
        con.close()

    assert payload["n"] == 0
    assert payload["wins"] == 0
    assert payload["pnl_sum_pct"] is None
    assert payload["winrate"] is None
    assert payload["top_bot"] is None
    assert payload["flop_bot"] is None
    assert payload["best_trade"] is None
    assert payload["worst_trade"] is None
    assert payload["as_of"] == far_future.isoformat()


def test_overnight_digest_empty_substrate_degrades_cleanly(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path,
        str(tmp_path / "pq"),
        ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    con = analytics_api.connect_ro(duckdb_path)
    try:
        payload = overnight_digest(con, 8)
    finally:
        con.close()

    assert payload == {
        "as_of": None,
        "window_hours": 8,
        "n": 0,
        "wins": 0,
        "pnl_sum_pct": None,
        "winrate": None,
        "top_bot": None,
        "flop_bot": None,
        "best_trade": None,
        "worst_trade": None,
        "regime_changes": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AK6 — regime transitions (real changes only, not log-entry count)
# ─────────────────────────────────────────────────────────────────────────────


def test_overnight_digest_regime_changes_counts_real_transitions_only(tmp_path):
    """CHOP->TREND (h=-5) and TREND->CHOP (h=-1) are the only two REAL
    transitions inside the 8h window; the repeat CHOP row (h=-9, also outside
    the window) and the repeat TREND row (h=-2) must not be counted."""
    con = analytics_api.connect_ro(_build_duckdb(tmp_path, with_regime=True))
    try:
        payload = overnight_digest(con, 8)
    finally:
        con.close()
    assert payload["regime_changes"] == 2


def test_overnight_digest_regime_changes_none_without_regime_history(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path, with_regime=False))
    try:
        payload = overnight_digest(con, 8)
    finally:
        con.close()
    assert payload["regime_changes"] is None
    assert payload["n"] == 6  # outcome aggregation itself is unaffected


# ─────────────────────────────────────────────────────────────────────────────
# resolve_digest_window() — pure query-string resolver (Flask-free)
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_digest_window_accepts_known_values():
    assert resolve_digest_window("8h") == 8
    assert resolve_digest_window("24h") == 24
    assert resolve_digest_window("168h") == 168
    assert resolve_digest_window("24") == 24  # bare integer, no "h" suffix
    assert resolve_digest_window("  24H  ") == 24  # whitespace + case-insensitive


def test_resolve_digest_window_unknown_value_falls_back_to_default():
    assert resolve_digest_window(None) == 8
    assert resolve_digest_window("") == 8
    assert resolve_digest_window("not-a-number") == 8
    assert resolve_digest_window("12h") == 8  # a real int, but not an offered option


# ─────────────────────────────────────────────────────────────────────────────
# AK7/AK8 — Flask route: end-to-end integration test against real DuckDB
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path):
    app = dashboard_app.create_app(_build_duckdb(tmp_path))
    app.config.update(TESTING=True)
    return app.test_client()


def test_panel_overnight_digest_renders_correct_values(client):
    """The mandatory integration test: real AnalyticsExporter -> real DuckDB
    file -> real overnight_digest() query -> real Flask route -> rendered
    HTML, with realistic closed_ai_signals/regime_history fixture data."""
    resp = client.get("/panels/overnight-digest")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "46.00%" in html  # net PnL sum
    assert "66.7%" in html  # win-rate 4/6
    assert "MIS2" in html  # top bot
    assert "ABR2" in html  # flop bot
    assert "49.00%" in html  # top_bot pnl_sum_pct
    assert "-18.00%" in html  # flop_bot pnl_sum_pct
    assert "C5USDT" in html  # best-trade coin
    assert "C3USDT" in html  # worst-trade coin
    assert "50.00%" in html  # best trade pnl
    assert "-20.00%" in html  # worst trade pnl
    assert ">2<" in html  # regime_changes tile value

    # Boundary/decoy trades must never leak into the rendered fragment either.
    assert "BND1" not in html
    assert "OLD1" not in html


def test_panel_overnight_digest_window_toggle_changes_values(client):
    """?window=24h widens the window enough to pull in BND1 (+77%, now the
    highest single trade AND highest-summed bot with only 1 trade) and OLD1
    (+33%, contributes to the totals but is neither a top/flop bot nor a
    notable trade at 24h) — the rendered totals AND top-bot must change,
    proving the toggle is really wired through (not accepted-and-ignored)."""
    resp = client.get("/panels/overnight-digest?window=24h")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # n=8, pnl_sum = 46 + 77 (BND1) + 33 (OLD1) = 156
    assert "156.00%" in html
    assert "BND1" in html  # now the top bot (single +77% trade beats MIS2's +49 sum)
    assert "77.00%" in html  # BND1's pnl_sum_pct == its one trade's pnl_pct
    # OLD1 legitimately never renders here: it is neither top/flop bot nor the
    # best/worst trade at this window — only its CONTRIBUTION to n/pnl_sum is
    # observable, which the two assertions above already pin down.


def test_panel_overnight_digest_unknown_window_falls_back_no_500(client):
    resp = client.get("/panels/overnight-digest?window=not-a-window")
    assert resp.status_code == 200
    assert "46.00%" in resp.get_data(as_text=True)  # default 8h behaviour


def test_panel_overnight_digest_empty_substrate_shows_clean_message(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path,
        str(tmp_path / "pq"),
        ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    c = dashboard_app.create_app(duckdb_path).test_client()
    resp = c.get("/panels/overnight-digest")
    assert resp.status_code == 200
    assert "Keine Trades im Fenster" in resp.get_data(as_text=True)


def test_panel_overnight_digest_never_touches_postgres(client, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("overnight-digest route touched Postgres")

    monkeypatch.setattr(analytics_export.PostgresFetcher, "_connection", _boom)
    monkeypatch.setattr(analytics_export.PostgresFetcher, "fetch", _boom)
    assert client.get("/panels/overnight-digest").status_code == 200


def test_index_includes_digest_panel_above_fleet_registry(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="overnight-digest-body"' in html
    assert 'hx-get="/panels/overnight-digest"' in html
    assert "every 30s" in html
    # SPEC.md: the digest sits GANZ OBEN — above the fleet-registry panel.
    assert html.index("overnight-digest-body") < html.index("fleet-registry-body")


def test_panel_sources_registers_overnight_digest_with_all_three_sources():
    assert dashboard_app.PANEL_SOURCES["overnight-digest"] == (
        "closed_ai_signals",
        "closed_trades",
        "regime_history",
    )

# backtest/test_dashboard_event_feed.py
"""DB-free tests for the Z1 dashboard read-only Event-Feed panel
(Feature 9, T-2026-CU-9050-161, S10 — the last panel of the Z1 rewrite).

Mirrors backtest/test_dashboard_digest.py's fixture style: the Postgres
boundary is replaced by a synthetic ``ListFetcher`` feeding the real
``AnalyticsExporter`` into a temporary DuckDB file, using the ACTUAL
``closed_ai_signals`` and ``regime_history`` column names from
``tools/analytics_export.py`` (closed_ai_signals: id, symbol, model,
direction, entry, close_price, targets_hit, open_time, close_time, status,
lev; regime_history: id, ts, regime, alt_context, btc_price, confidence,
confidence_btc, confidence_alt) — the T-152 review lesson: unrealistic
fixture keys can mask a real bug.

Covers SPEC.md Feature 9 AK1-AK7. Run with:
    pytest backtest/test_dashboard_event_feed.py -v
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
from tools.analytics_api import event_feed  # noqa: E402
from tools.analytics_export import SOURCES_BY_NAME, AnalyticsExporter  # noqa: E402
from tools.dashboard import app as dashboard_app  # noqa: E402
from tools.dashboard.app import resolve_event_feed_window  # noqa: E402

# The fixture's most recent closed_ai_signals row sits exactly AT this
# timestamp, so event_feed()'s default as_of=None (max(closed_at) in the
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


# Fixture: within the default 24h window —
#   RUB2 wins: +10 @ h=0, +8 @ h=-2, +6 @ h=-5   -> top-3 winners: +10, +8, +6
#   ABR2 losses: -20 @ h=-3, -15 @ h=-6, -1 @ h=-10 -> top-3 losers: -20, -15, -1
#   MIS2 win: +50 @ h=-4 (biggest single win overall)
#   MIS2 loss: -30 @ h=-7 (biggest single loss overall)
# plus two decoys placed to catch a broken window filter:
#   - BND1 sits EXACTLY on the lower boundary (as_of - 24h) — must be EXCLUDED
#     (half-open window: closed_at > as_of - 24h, not >=).
#   - OLD1 sits well outside the window (as_of - 40h) — must be EXCLUDED too.
# Both carry large, distinctive PnL values (+99%, +88%) so an accidentally
# widened window would surface them among the notable-trade events.
def _fixture_rows() -> list[dict[str, Any]]:
    return [
        _ai_row(1, model="RUB2", entry=100, close=110, close_time=_h(0)),  # +10%
        _ai_row(2, model="RUB2", entry=100, close=108, close_time=_h(-2)),  # +8%
        _ai_row(3, model="RUB2", entry=100, close=106, close_time=_h(-5)),  # +6%
        _ai_row(4, model="ABR2", entry=100, close=80, close_time=_h(-3)),  # -20%
        _ai_row(5, model="ABR2", entry=100, close=85, close_time=_h(-6)),  # -15%
        _ai_row(6, model="ABR2", entry=100, close=99, close_time=_h(-10)),  # -1%
        _ai_row(7, model="MIS2", entry=100, close=150, close_time=_h(-4)),  # +50%, biggest win
        _ai_row(8, model="MIS2", entry=100, close=70, close_time=_h(-7)),  # -30%, biggest loss
        _ai_row(9, model="BND1", entry=100, close=199, close_time=_h(-24)),  # +99%, boundary — EXCLUDED
        _ai_row(10, model="OLD1", entry=100, close=188, close_time=_h(-40)),  # +88%, outside — EXCLUDED
    ]


# regime_history: CHOP (h=-30, first ever — no predecessor, not a transition,
# also outside the window), CHOP (h=-20, repeat, in-window but not a
# transition), TREND (h=-15, REAL transition, in-window), TREND (h=-8,
# repeat, in-window — not counted), CHOP (h=-1, REAL transition, in-window)
# -> 2 real transitions in the default 24h window.
def _regime_fixture_rows() -> list[dict[str, Any]]:
    return [
        _regime_row(1, ts=_h(-30), regime="CHOP"),
        _regime_row(2, ts=_h(-20), regime="CHOP"),
        _regime_row(3, ts=_h(-15), regime="TREND"),
        _regime_row(4, ts=_h(-8), regime="TREND"),
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
# AK1 — basic shape + DESCENDING sort order (MUTATION-CHECK)
# ─────────────────────────────────────────────────────────────────────────────


def test_event_feed_basic_shape_and_sort_order(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = event_feed(con, 24)
    finally:
        con.close()

    assert payload["as_of"] == AS_OF.isoformat()
    assert payload["window_hours"] == 24
    events = payload["events"]
    assert len(events) > 0
    # Every event has the four typed fields the panel renders.
    for e in events:
        assert set(e.keys()) == {"type", "ts", "title", "detail"}
        assert e["type"] in ("regime_change", "notable_trade")

    # MUTATION-CHECK: newest-first — a broken sort (ascending, or no sort at
    # all) would fail this monotonic-non-increasing check on real fixture
    # data spanning multiple distinct timestamps.
    timestamps = [e["ts"] for e in events]
    assert timestamps == sorted(timestamps, reverse=True)
    # The single most-recent event in the whole fixture is RUB2's h=0 win.
    assert events[0]["ts"] == _h(0).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# AK2 — regime transitions (real changes only, MUTATION-CHECK)
# ─────────────────────────────────────────────────────────────────────────────


def test_event_feed_regime_transitions_correct_and_repeats_excluded(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path, with_regime=True))
    try:
        payload = event_feed(con, 24)
    finally:
        con.close()

    regime_events = [e for e in payload["events"] if e["type"] == "regime_change"]
    # Only the 2 REAL transitions (CHOP->TREND at h=-15, TREND->CHOP at h=-1)
    # — the repeat CHOP (h=-20, also the substrate's first-ever row) and the
    # repeat TREND (h=-8) must NOT produce an event. A broken LAG predicate
    # (e.g. missing "regime != prev_regime") would inflate this to 4.
    assert len(regime_events) == 2
    assert regime_events[0]["ts"] == _h(-1).isoformat()  # newest first
    assert regime_events[0]["detail"] == "TREND → CHOP"
    assert regime_events[1]["ts"] == _h(-15).isoformat()
    assert regime_events[1]["detail"] == "CHOP → TREND"


def test_event_feed_no_regime_events_without_regime_history(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path, with_regime=False))
    try:
        payload = event_feed(con, 24)
    finally:
        con.close()
    assert all(e["type"] != "regime_change" for e in payload["events"])
    # Notable-trade aggregation itself is unaffected by the absent table.
    assert any(e["type"] == "notable_trade" for e in payload["events"])


# ─────────────────────────────────────────────────────────────────────────────
# AK3 — notable trades: winners and losers split correctly
# ─────────────────────────────────────────────────────────────────────────────


def test_event_feed_notable_trades_winners_and_losers(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = event_feed(con, 24)
    finally:
        con.close()

    trade_events = [e for e in payload["events"] if e["type"] == "notable_trade"]
    titles = [e["title"] for e in trade_events]
    details = [e["detail"] for e in trade_events]

    # Biggest single win (MIS2 +50%, coin C7USDT) and biggest single loss
    # (MIS2 -30%, coin C8USDT) must both appear.
    assert any("C7USDT" in t and "MIS2" in t for t in titles)
    assert "+50.00%" in details
    assert any("C8USDT" in t and "MIS2" in t for t in titles)
    assert "-30.00%" in details

    # Winners are labelled "Gewinn", losers "Verlust" — never the wrong sign.
    for e in trade_events:
        if e["detail"].startswith("+"):
            assert e["title"].startswith("Gewinn")
        else:
            assert e["title"].startswith("Verlust")

    # Boundary/decoy trades never leak into the notable-trade events either.
    assert not any("C9USDT" in t for t in titles)  # BND1, boundary
    assert not any("C10USDT" in t for t in titles)  # OLD1, outside window


# ─────────────────────────────────────────────────────────────────────────────
# AK4 — window boundary (half-open, MUTATION-CHECK)
# ─────────────────────────────────────────────────────────────────────────────


def test_event_feed_window_boundary_excludes_outside_events(tmp_path):
    """BND1 (+99%, exactly at as_of - 24h) and OLD1 (+88%, well outside) must
    not produce notable-trade events; the h=-30 regime row (also outside the
    24h window, and additionally the substrate's first-ever row) must not
    produce a transition event either. A widened window (>= instead of >, or
    a dropped filter) would surface any of these."""
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = event_feed(con, 24)
    finally:
        con.close()

    all_details = " ".join(e["detail"] for e in payload["events"])
    all_titles = " ".join(e["title"] for e in payload["events"])
    assert "99.00%" not in all_details
    assert "88.00%" not in all_details
    assert "C9USDT" not in all_titles
    assert "C10USDT" not in all_titles
    # Exactly the 2 real regime transitions + up to 6 notable-trade events
    # (3 winners + 3 losers) — never more.
    assert len(payload["events"]) <= 8


# ─────────────────────────────────────────────────────────────────────────────
# AK5 — empty window / empty substrate degrade cleanly
# ─────────────────────────────────────────────────────────────────────────────


def test_event_feed_empty_window_degrades_cleanly(tmp_path):
    """Substrate HAS events, but none fall in the window when anchored far
    past every one of them."""
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        far_future = AS_OF + datetime.timedelta(hours=200)
        payload = event_feed(con, 24, as_of=far_future)
    finally:
        con.close()

    assert payload["events"] == []
    assert payload["as_of"] == far_future.isoformat()
    assert payload["window_hours"] == 24


def test_event_feed_empty_substrate_degrades_cleanly(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path,
        str(tmp_path / "pq"),
        ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    con = analytics_api.connect_ro(duckdb_path)
    try:
        payload = event_feed(con, 24)
    finally:
        con.close()

    assert payload == {"as_of": None, "window_hours": 24, "events": []}


# ─────────────────────────────────────────────────────────────────────────────
# resolve_event_feed_window() — pure query-string resolver (Flask-free)
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_event_feed_window_accepts_known_values():
    assert resolve_event_feed_window("24h") == 24
    assert resolve_event_feed_window("168h") == 168
    assert resolve_event_feed_window("168") == 168  # bare integer, no "h" suffix
    assert resolve_event_feed_window("  24H  ") == 24  # whitespace + case-insensitive


def test_resolve_event_feed_window_unknown_value_falls_back_to_default():
    assert resolve_event_feed_window(None) == 24
    assert resolve_event_feed_window("") == 24
    assert resolve_event_feed_window("not-a-number") == 24
    assert resolve_event_feed_window("8h") == 24  # a real int, but not an offered option


# ─────────────────────────────────────────────────────────────────────────────
# AK6/AK7 — Flask route: end-to-end integration test against real DuckDB
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path):
    app = dashboard_app.create_app(_build_duckdb(tmp_path))
    app.config.update(TESTING=True)
    return app.test_client()


def test_panel_event_feed_renders_events_in_descending_order(client):
    """The mandatory integration test: real AnalyticsExporter -> real DuckDB
    file -> real event_feed() query -> real Flask route -> rendered HTML,
    with realistic closed_ai_signals/regime_history fixture data, correctly
    time-descending sorted."""
    resp = client.get("/panels/event-feed")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "MIS2" in html  # biggest win/loss bot
    assert "+50.00%" in html
    assert "-30.00%" in html
    assert "TREND" in html  # regime transition detail
    assert "CHOP" in html

    # Boundary/decoy trades must never leak into the rendered fragment.
    assert "C9USDT" not in html
    assert "C10USDT" not in html

    # DESCENDING order: the newest event (RUB2's h=0 win, +10%) must render
    # before the oldest in-window event (MIS2's -30% loss, h=-7).
    assert html.index("+10.00%") < html.index("-30.00%")


def test_panel_event_feed_window_toggle_changes_values(client):
    """?window=168h widens the window enough to pull in BND1 (+99%) and OLD1
    (+88%) — proving the toggle is really wired through (not
    accepted-and-ignored)."""
    resp = client.get("/panels/event-feed?window=168h")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "+99.00%" in html  # BND1, now inside the widened window
    assert "C9USDT" in html


def test_panel_event_feed_unknown_window_falls_back_no_500(client):
    resp = client.get("/panels/event-feed?window=not-a-window")
    assert resp.status_code == 200
    assert "+50.00%" in resp.get_data(as_text=True)  # default 24h behaviour


def test_panel_event_feed_empty_substrate_shows_clean_message(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path,
        str(tmp_path / "pq"),
        ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    c = dashboard_app.create_app(duckdb_path).test_client()
    resp = c.get("/panels/event-feed")
    assert resp.status_code == 200
    assert "Keine Events im Fenster" in resp.get_data(as_text=True)


def test_panel_event_feed_never_touches_postgres(client, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("event-feed route touched Postgres")

    monkeypatch.setattr(analytics_export.PostgresFetcher, "_connection", _boom)
    monkeypatch.setattr(analytics_export.PostgresFetcher, "fetch", _boom)
    assert client.get("/panels/event-feed").status_code == 200


def test_index_includes_event_feed_panel_last(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="event-feed-body"' in html
    assert 'hx-get="/panels/event-feed"' in html
    assert "every 30s" in html
    # SPEC.md: the event feed is the LAST panel of the Z1 rewrite.
    assert html.index("event-feed-body") > html.index("coin-drilldown-body")


def test_panel_sources_registers_event_feed_with_all_three_sources():
    assert dashboard_app.PANEL_SOURCES["event-feed"] == (
        "closed_ai_signals",
        "closed_trades",
        "regime_history",
    )


def test_event_feed_route_has_no_write_verb(client):
    """CLAUDE.md hard rule: no mutation endpoint in the web UI ahead of
    Cloudflare Access — GET is the only allowed method on this route."""
    resp = client.post("/panels/event-feed")
    assert resp.status_code == 405

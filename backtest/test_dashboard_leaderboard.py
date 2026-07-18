# backtest/test_dashboard_leaderboard.py
"""DB-free tests for the Z1 dashboard Leaderboard + Risk-Metrics panel
(Feature 2, T-2026-CU-9050-154).

Mirrors backtest/test_dashboard_shell.py's fixture style: the Postgres
boundary is replaced by a synthetic ``ListFetcher`` feeding the real
``AnalyticsExporter`` into a temporary DuckDB file, using the ACTUAL
``closed_ai_signals`` column names from ``tools/analytics_export.py``
(id, symbol, model, direction, entry, close_price, targets_hit, open_time,
close_time, status, lev) and realistic model tags (RUB2/MIS2/ABR2) — the
T-152 review lesson: unrealistic fixture keys can mask a real bug.

Covers SPEC.md AK1-AK6. Run with:
    pytest backtest/test_dashboard_leaderboard.py -v
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
    _leaderboard_row,
    _max_consecutive_losses,
    bot_leaderboard,
    bot_trade_rows,
)
from tools.analytics_export import SOURCES_BY_NAME, AnalyticsExporter  # noqa: E402
from tools.dashboard import app as dashboard_app  # noqa: E402

_BASE = datetime.datetime(2026, 7, 10, 12, 0, 0)  # noqa: DTZ001  (naive-local, per exporter contract)


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic fetcher — real closed_ai_signals column shapes, no DB
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


def _ai_row(
    i: int,
    *,
    model: str,
    direction: str,
    entry: float,
    close: float,
    close_time: datetime.datetime,
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


def _day(n: int) -> datetime.datetime:
    return _BASE + datetime.timedelta(days=n)


# Fixture: three bots with distinct, hand-computable PnL/risk profiles, plus a
# fourth bot ("NEU1") whose only rows are neutral/housekeeping (must vanish
# from the leaderboard entirely — AK5).
#
#   RUB2: +10, -5, -10, +30 (close order)  -> pnl_sum=+25, wins=2/4=0.5,
#         equity 10,5,-5,25; peak 10,10,10,25; dd 0,-5,-15,0 -> max_dd=-15
#         loss streak: -5,-10 consecutive -> 2
#   ABR2: +5                                -> pnl_sum=+5, wins=1/1=1.0,
#         equity 5; peak 5; dd 0 -> max_dd=0; loss streak=0
#   MIS2: -10, -20, -30                     -> pnl_sum=-60, wins=0/3=0.0,
#         equity -10,-30,-60; peak -10,-10,-10; dd 0,-20,-50 -> max_dd=-50
#         loss streak=3
#   NEU1: one micro-PnL row (0.05%, below MICRO_PNL_PCT) + one DELISTED row
#         -> zero decisive trades -> must not appear at all.
def _fixture_rows() -> list[dict[str, Any]]:
    return [
        _ai_row(1, model="RUB2", direction="LONG", entry=100, close=110, close_time=_day(1)),
        _ai_row(2, model="RUB2", direction="LONG", entry=100, close=95, close_time=_day(2)),
        _ai_row(3, model="RUB2", direction="LONG", entry=100, close=90, close_time=_day(3)),
        _ai_row(4, model="RUB2", direction="LONG", entry=100, close=130, close_time=_day(4)),
        _ai_row(5, model="ABR2", direction="LONG", entry=100, close=105, close_time=_day(1)),
        _ai_row(6, model="MIS2", direction="LONG", entry=100, close=90, close_time=_day(1)),
        _ai_row(7, model="MIS2", direction="LONG", entry=100, close=80, close_time=_day(2)),
        _ai_row(8, model="MIS2", direction="LONG", entry=100, close=70, close_time=_day(3)),
        _ai_row(9, model="NEU1", direction="LONG", entry=100, close=100.05, close_time=_day(1)),
        _ai_row(10, model="NEU1", direction="LONG", entry=100, close=50, close_time=_day(2), status="DELISTED"),
    ]


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


# ─────────────────────────────────────────────────────────────────────────────
# AK1 — bot_trade_rows() excludes neutral/housekeeping, orders (bot, closed_at)
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_trade_rows_excludes_neutral_and_housekeeping(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        rows = bot_trade_rows(con)
    finally:
        con.close()

    bots_present = {r["bot"] for r in rows}
    assert "NEU1" not in bots_present  # micro-PnL + DELISTED -> zero decisive trades
    assert bots_present == {"RUB2", "ABR2", "MIS2"}

    rub2 = [r for r in rows if r["bot"] == "RUB2"]
    assert len(rub2) == 4
    assert [r["closed_at"] for r in rub2] == sorted(r["closed_at"] for r in rub2)  # ascending order
    assert rub2[0]["pnl_pct"] == pytest.approx(10.0)
    assert rub2[0]["is_win"] is True
    assert rub2[1]["pnl_pct"] == pytest.approx(-5.0)
    assert rub2[1]["is_win"] is False


# ─────────────────────────────────────────────────────────────────────────────
# AK2 — _leaderboard_row() pure aggregation (no I/O)
# ─────────────────────────────────────────────────────────────────────────────


def test_leaderboard_row_computes_pnl_winrate_expectancy_and_risk_metrics():
    trades = [
        {"pnl_pct": 10.0, "is_win": True},
        {"pnl_pct": -5.0, "is_win": False},
        {"pnl_pct": -10.0, "is_win": False},
        {"pnl_pct": 30.0, "is_win": True},
    ]
    row = _leaderboard_row("RUB2", trades)
    assert row["bot"] == "RUB2"
    assert row["n"] == 4
    assert row["wins"] == 2
    assert row["winrate"] == pytest.approx(0.5)
    assert row["pnl_sum_pct"] == pytest.approx(25.0)
    assert row["expectancy_pct"] == pytest.approx(6.25)
    # equity 10,5,-5,25; peak 10,10,10,25; dd 0,-5,-15,0 -> worst -15
    assert row["max_drawdown_pp"] == pytest.approx(-15.0)
    assert row["max_loss_streak"] == 2


def test_leaderboard_row_all_losses_drawdown_and_streak_cover_full_run():
    trades = [
        {"pnl_pct": -10.0, "is_win": False},
        {"pnl_pct": -20.0, "is_win": False},
        {"pnl_pct": -30.0, "is_win": False},
    ]
    row = _leaderboard_row("MIS2", trades)
    assert row["pnl_sum_pct"] == pytest.approx(-60.0)
    assert row["winrate"] == pytest.approx(0.0)
    assert row["expectancy_pct"] == pytest.approx(-20.0)
    # equity -10,-30,-60; peak -10,-10,-10; dd 0,-20,-50 -> worst -50
    assert row["max_drawdown_pp"] == pytest.approx(-50.0)
    assert row["max_loss_streak"] == 3


def test_leaderboard_row_single_win_zero_drawdown_zero_streak():
    row = _leaderboard_row("ABR2", [{"pnl_pct": 5.0, "is_win": True}])
    assert row["max_drawdown_pp"] == pytest.approx(0.0)
    assert row["max_loss_streak"] == 0


def test_max_consecutive_losses_resets_on_win():
    trades = [
        {"is_win": False}, {"is_win": False}, {"is_win": True},
        {"is_win": False}, {"is_win": False}, {"is_win": False}, {"is_win": True},
    ]
    assert _max_consecutive_losses(trades) == 3  # the longer run, not the first


# ─────────────────────────────────────────────────────────────────────────────
# AK3 — bot_leaderboard() sorts descending by pnl_sum_pct by default
# ─────────────────────────────────────────────────────────────────────────────


def test_bot_leaderboard_sorts_by_pnl_descending(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = bot_leaderboard(con)
    finally:
        con.close()

    # RUB2 (+25) > ABR2 (+5) > MIS2 (-60). Any wrong sort direction/key
    # (ascending, or sorting by n/winrate instead) breaks this exact order —
    # the mutation check the SPEC calls for.
    assert [r["bot"] for r in payload["bots"]] == ["RUB2", "ABR2", "MIS2"]
    assert payload["sort_by"] == "pnl_sum_pct"


def test_bot_leaderboard_sort_by_expectancy(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = bot_leaderboard(con, sort_by="expectancy_pct")
    finally:
        con.close()
    # expectancy: RUB2=6.25, ABR2=5.0, MIS2=-20.0 -> same order here, but this
    # exercises the sort_by parameter explicitly (distinct from the default).
    assert [r["bot"] for r in payload["bots"]] == ["RUB2", "ABR2", "MIS2"]
    assert payload["sort_by"] == "expectancy_pct"


def test_bot_leaderboard_unknown_sort_key_falls_back_to_default(tmp_path):
    con = analytics_api.connect_ro(_build_duckdb(tmp_path))
    try:
        payload = bot_leaderboard(con, sort_by="not_a_real_key")
    finally:
        con.close()
    assert payload["sort_by"] == "pnl_sum_pct"


def test_bot_leaderboard_empty_substrate_degrades_gracefully(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path, str(tmp_path / "pq"), ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    con = analytics_api.connect_ro(duckdb_path)
    try:
        payload = bot_leaderboard(con)
    finally:
        con.close()
    assert payload == {"bots": [], "sort_by": "pnl_sum_pct"}


# ─────────────────────────────────────────────────────────────────────────────
# AK4/AK5/AK6 — Flask route: end-to-end integration test against real DuckDB
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path):
    app = dashboard_app.create_app(_build_duckdb(tmp_path))
    app.config.update(TESTING=True)
    return app.test_client()


def test_panel_leaderboard_renders_sorted_rows_with_all_metrics(client):
    """The mandatory integration test: real AnalyticsExporter -> real DuckDB
    file -> real bot_leaderboard() query -> real Flask route -> rendered HTML,
    with realistic closed_ai_signals fixture data (not hand-built dicts)."""
    resp = client.get("/panels/leaderboard")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # All three decisive bots present, in PnL-descending order.
    rub2_pos = html.index("RUB2")
    abr2_pos = html.index("ABR2")
    mis2_pos = html.index("MIS2")
    assert rub2_pos < abr2_pos < mis2_pos

    # Neutral-only bot excluded end-to-end (AK5) — not just at the query layer.
    assert "NEU1" not in html

    # Headline metrics rendered (PnL sum, win-rate, expectancy, trade count,
    # drawdown, loss streak).
    assert "25.00%" in html   # RUB2 pnl_sum_pct
    assert "50.0%" in html    # RUB2 winrate 2/4
    assert "-15.00" in html   # RUB2 max_drawdown_pp
    assert "-60.00%" in html  # MIS2 pnl_sum_pct (all losses)


def test_index_includes_leaderboard_panel(client):
    html = client.get("/").get_data(as_text=True)
    assert 'hx-get="/panels/leaderboard"' in html
    assert 'id="leaderboard-body"' in html
    assert "every 30s" in html


def test_panel_leaderboard_never_touches_postgres(client, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("leaderboard route touched Postgres")

    monkeypatch.setattr(analytics_export.PostgresFetcher, "_connection", _boom)
    monkeypatch.setattr(analytics_export.PostgresFetcher, "fetch", _boom)
    assert client.get("/panels/leaderboard").status_code == 200


def test_leaderboard_json_endpoint_mounted(client):
    resp = client.get("/api/analytics/leaderboard")
    assert resp.status_code == 200
    body = resp.get_json()
    assert [r["bot"] for r in body["bots"]] == ["RUB2", "ABR2", "MIS2"]


def test_leaderboard_json_endpoint_rejects_bad_sort_key(client):
    resp = client.get("/api/analytics/leaderboard?sort_by=not_a_real_key")
    assert resp.status_code == 400


def test_panel_leaderboard_empty_duckdb(tmp_path):
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path, str(tmp_path / "pq"), ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
    ).run()
    c = dashboard_app.create_app(duckdb_path).test_client()
    resp = c.get("/panels/leaderboard")
    assert resp.status_code == 200
    assert "keine entschiedenen Trades" in resp.get_data(as_text=True)

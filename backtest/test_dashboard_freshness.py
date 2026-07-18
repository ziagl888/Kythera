# backtest/test_dashboard_freshness.py
"""DB-free tests for the Z1 dashboard per-panel data-freshness indicator
(Feature 4, T-2026-CU-9050-156).

Mirrors backtest/test_dashboard_shell.py's fixture style: the Postgres
boundary is replaced by a synthetic ``ListFetcher`` feeding the real
``AnalyticsExporter`` into a temporary DuckDB file, using the ACTUAL
``closed_ai_signals``/``closed_trades`` column names from
``tools/analytics_export.py`` (T-152 review lesson: unrealistic fixture keys
can mask a real bug).

Covers SPEC.md Feature 4 AK1-AK6. Run with:
    pytest backtest/test_dashboard_freshness.py -v
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Any, Callable

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.analytics_export import SOURCES_BY_NAME, AnalyticsExporter  # noqa: E402
from tools.dashboard import app as dashboard_app  # noqa: E402

UTC = datetime.timezone.utc
_BASE = datetime.datetime(2026, 7, 15, 9, 0, 0)  # noqa: DTZ001  (naive-local, per exporter contract)


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic fetcher — real closed_ai_signals/closed_trades column shapes
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
    direction: str,
    entry: float,
    close: float,
    close_time: datetime.datetime,
    status: str = "TP1",
) -> dict[str, Any]:
    """One realistic closed_ai_signals row — real column names."""
    return {
        "id": i,
        "symbol": f"C{i}USDT",
        "model": model,
        "direction": direction,
        "entry": entry,
        "close_price": close,
        "targets_hit": 1,
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
    direction: str,
    entry: float,
    close: float,
    posted: datetime.datetime,
    status: str = "TP1",
) -> dict[str, Any]:
    """One realistic closed_trades row — real column names."""
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


def _fixed_clock(dt: datetime.datetime) -> Callable[[], datetime.datetime]:
    return lambda: dt


def _build_two_source_duckdb(tmp_path, *, ai_synced_at: datetime.datetime, trades_synced_at: datetime.datetime) -> str:
    """Materialise closed_ai_signals + closed_trades into one DuckDB file with
    DELIBERATELY DIFFERENT ``synced_at`` per source, so a panel that reads
    BOTH must show the worst-case (older/staler) of the two — the assertion a
    wrong panel->source mapping (dropping one source) would falsify."""
    duckdb_path = str(tmp_path / "analytics.duckdb")
    parquet_root = str(tmp_path / "parquet")
    fetcher = ListFetcher(
        {
            "closed_ai_signals": [
                _ai_row(
                    1,
                    model="RUB2",
                    direction="LONG",
                    entry=100,
                    close=110,
                    close_time=_BASE + datetime.timedelta(minutes=15),
                ),  # last_row_ts 09:15
            ],
            "closed_trades": [
                _ct_row(
                    1, strategy="RUB2", coin="ETHUSDT", direction="LONG", entry=100, close=105, posted=_BASE
                ),  # last_row_ts 09:00 (older)
            ],
        }
    )
    AnalyticsExporter(
        duckdb_path,
        parquet_root,
        fetcher,
        sources=[SOURCES_BY_NAME["closed_ai_signals"]],
        clock=_fixed_clock(ai_synced_at),
    ).run()
    AnalyticsExporter(
        duckdb_path,
        parquet_root,
        fetcher,
        sources=[SOURCES_BY_NAME["closed_trades"]],
        clock=_fixed_clock(trades_synced_at),
    ).run()
    return duckdb_path


# ─────────────────────────────────────────────────────────────────────────────
# AK1 — freshness_summary(): additive `sources` filter + `worst_case` toggle
# ─────────────────────────────────────────────────────────────────────────────


def test_freshness_summary_sources_filter_narrows_rows():
    rows = [
        {"source": "closed_ai_signals", "last_row_ts": "2026-07-15T09:15:00", "synced_at": "2026-07-15T09:20:00"},
        {"source": "regime_history", "last_row_ts": "2026-07-15T05:00:00", "synced_at": "2026-07-15T05:05:00"},
    ]
    now = datetime.datetime(2026, 7, 15, 9, 30, 0, tzinfo=UTC)
    summary = dashboard_app.freshness_summary(rows, now_utc=now, sources=["closed_ai_signals"])
    assert summary["sources"] == 1
    assert summary["stand"] == "09:15"
    assert summary["sync_age_min"] == 10  # only the filtered-in row counts


def test_freshness_summary_sources_filter_none_is_unfiltered_default():
    """Default (sources=None) must reproduce the exact pre-Feature-4 shape —
    the shell-global badge's existing behaviour is unaffected."""
    rows = [
        {"source": "a", "last_row_ts": "2026-07-15T09:00:00", "synced_at": "2026-07-15T12:00:00"},
        {"source": "b", "last_row_ts": "2026-07-15T11:45:00", "synced_at": "2026-07-15T12:30:00"},
    ]
    now = datetime.datetime(2026, 7, 15, 12, 40, 0, tzinfo=UTC)
    summary = dashboard_app.freshness_summary(rows, now_utc=now)
    assert summary["stand"] == "11:45"
    assert summary["sync_age_min"] == 10
    assert summary["sources"] == 2


def test_freshness_summary_worst_case_picks_oldest():
    """worst_case=True must pick the OLDER/staler source, the opposite of the
    default freshest-wins aggregation used by the shell-global badge."""
    rows = [
        {"source": "closed_ai_signals", "last_row_ts": "2026-07-15T09:15:00", "synced_at": "2026-07-15T09:20:00"},
        {"source": "closed_trades", "last_row_ts": "2026-07-15T09:00:00", "synced_at": "2026-07-15T08:00:00"},
    ]
    now = datetime.datetime(2026, 7, 15, 9, 30, 0, tzinfo=UTC)
    freshest = dashboard_app.freshness_summary(rows, now_utc=now, worst_case=False)
    stalest = dashboard_app.freshness_summary(rows, now_utc=now, worst_case=True)
    assert freshest["stand"] == "09:15" and freshest["sync_age_min"] == 10
    assert stalest["stand"] == "09:00" and stalest["sync_age_min"] == 90


# ─────────────────────────────────────────────────────────────────────────────
# AK2 — panel_freshness(): panel -> source resolution
# ─────────────────────────────────────────────────────────────────────────────


def _rows_ai_and_trades(*, ai_synced: str, trades_synced: str) -> list[dict[str, Any]]:
    return [
        {"source": "closed_ai_signals", "last_row_ts": "2026-07-15T09:15:00", "synced_at": ai_synced},
        {"source": "closed_trades", "last_row_ts": "2026-07-15T09:00:00", "synced_at": trades_synced},
    ]


def test_panel_freshness_leaderboard_and_success_rate_share_sources():
    rows = _rows_ai_and_trades(ai_synced="2026-07-15T09:20:00", trades_synced="2026-07-15T08:00:00")
    now = datetime.datetime(2026, 7, 15, 9, 30, 0, tzinfo=UTC)
    for panel in ("leaderboard", "success-rate", "success-rate-timeseries"):
        summary = dashboard_app.panel_freshness(rows, panel, now_utc=now)
        assert summary["sources"] == 2
        assert summary["stand"] == "09:00"  # worst-case: closed_trades' older last_row_ts
        assert summary["sync_age_min"] == 90  # worst-case: closed_trades' older synced_at


def test_panel_freshness_fleet_registry_is_file_based():
    rows = _rows_ai_and_trades(ai_synced="2026-07-15T09:20:00", trades_synced="2026-07-15T08:00:00")
    summary = dashboard_app.panel_freshness(rows, "fleet-registry")
    assert summary["file_based"] is True
    assert summary["stand"] is None
    assert summary["sync_age_min"] is None
    assert summary["label"] == "Live (dateibasiert)"
    # Ignores rows entirely — an empty list must give the identical result.
    assert dashboard_app.panel_freshness([], "fleet-registry") == summary


def test_panel_freshness_unknown_panel_raises():
    with pytest.raises(ValueError, match="unknown panel"):
        dashboard_app.panel_freshness([], "not-a-real-panel")


# ─────────────────────────────────────────────────────────────────────────────
# AK3 — multiple sources with different synced_at -> oldest (worst-case) wins
# ─────────────────────────────────────────────────────────────────────────────


def test_panel_freshness_oldest_source_wins_regardless_of_which_is_stale():
    """Swap which of the two sources is the stale one — the panel must always
    reflect whichever is OLDER, never a fixed source or the freshest."""
    now = datetime.datetime(2026, 7, 15, 9, 30, 0, tzinfo=UTC)

    ai_stale = dashboard_app.panel_freshness(
        _rows_ai_and_trades(ai_synced="2026-07-15T08:00:00", trades_synced="2026-07-15T09:20:00"),
        "leaderboard",
        now_utc=now,
    )
    trades_stale = dashboard_app.panel_freshness(
        _rows_ai_and_trades(ai_synced="2026-07-15T09:20:00", trades_synced="2026-07-15T08:00:00"),
        "leaderboard",
        now_utc=now,
    )
    assert ai_stale["sync_age_min"] == trades_stale["sync_age_min"] == 90


# ─────────────────────────────────────────────────────────────────────────────
# AK4 — missing freshness for a panel's source(s) -> never fabricated
# ─────────────────────────────────────────────────────────────────────────────


def test_panel_freshness_missing_source_gives_no_stand():
    """The panel's sources have no matching row at all (e.g. export never ran
    for them yet) -> stand/sync_age_min stay None, label is the non-fabricated
    placeholder — never invented from an unrelated source's freshness."""
    rows = [{"source": "regime_history", "last_row_ts": "2026-07-15T05:00:00", "synced_at": "2026-07-15T05:05:00"}]
    summary = dashboard_app.panel_freshness(rows, "leaderboard")
    assert summary["stand"] is None
    assert summary["sync_age_min"] is None
    assert summary["label"] == "Kein Datenstand"


def test_panel_freshness_badge_partial_missing_shows_dash(tmp_path):
    """Integration-flavoured: a real (empty) DuckDB -> real Flask route ->
    rendered HTML shows '—' for a panel whose sources never synced, not a
    fabricated Stand/Sync line."""
    duckdb_path = str(tmp_path / "empty.duckdb")
    AnalyticsExporter(
        duckdb_path,
        str(tmp_path / "pq"),
        ListFetcher({}),
        sources=[SOURCES_BY_NAME["closed_ai_signals"], SOURCES_BY_NAME["closed_trades"]],
    ).run()
    client = dashboard_app.create_app(duckdb_path).test_client()
    resp = client.get("/panels/leaderboard")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'class="panel__freshness"' in html
    assert "—" in html
    assert "Sync vor" not in html


# ─────────────────────────────────────────────────────────────────────────────
# AK6 — age from synced_at (UTC) ONLY, never last_row_ts (mutation-check)
# ─────────────────────────────────────────────────────────────────────────────


def test_panel_freshness_age_from_synced_at_not_last_row_ts():
    now = datetime.datetime(2026, 7, 15, 9, 30, 0, tzinfo=UTC)
    base = {"source": "closed_ai_signals", "synced_at": "2026-07-15T09:20:00"}
    near = dashboard_app.panel_freshness(
        [{**base, "last_row_ts": "2026-07-15T09:19:00"}],
        "leaderboard",
        now_utc=now,
    )
    far = dashboard_app.panel_freshness(
        [{**base, "last_row_ts": "1999-01-01T00:00:00"}],
        "leaderboard",
        now_utc=now,
    )
    # A wildly different last_row_ts must not move the age — only synced_at may.
    assert near["sync_age_min"] == far["sync_age_min"] == 10


# ─────────────────────────────────────────────────────────────────────────────
# AK5 — real AnalyticsExporter -> real DuckDB -> real Flask panel route ->
# rendered HTML shows the correct panel-specific data-freshness
# ─────────────────────────────────────────────────────────────────────────────


def test_leaderboard_panel_route_renders_own_freshness(tmp_path):
    duckdb_path = _build_two_source_duckdb(
        tmp_path,
        ai_synced_at=datetime.datetime(2026, 7, 15, 9, 20, tzinfo=UTC),
        trades_synced_at=datetime.datetime(2026, 7, 15, 8, 0, tzinfo=UTC),  # older -> worst-case
    )
    client = dashboard_app.create_app(duckdb_path).test_client()

    # The leaderboard reads BOTH outcome tables (analytics_api._OUTCOME_TABLES)
    # -> its badge must reflect the STALER (closed_trades) last_row_ts, 09:00,
    # not the fresher closed_ai_signals one (09:15). Deterministic regardless
    # of wall-clock time (last_row_ts is fixture data, not "now"-relative).
    html = client.get("/panels/leaderboard").get_data(as_text=True)
    assert 'class="panel__freshness"' in html
    assert "Stand 09:00" in html
    assert "Sync vor" in html

    # success-rate / success-rate-timeseries share the same two sources ->
    # identical panel-specific freshness.
    for path in ("/panels/success-rate", "/panels/success-rate-timeseries"):
        frag = client.get(path).get_data(as_text=True)
        assert "Stand 09:00" in frag

    # fleet-registry is file-based -> genuinely DIFFERENT badge content (no
    # DuckDB timestamp at all), proving the per-panel mapping is real, not a
    # single shared value copy-pasted across templates.
    fleet_html = client.get("/panels/fleet-registry").get_data(as_text=True)
    assert "Live" in fleet_html
    assert "Stand 09:00" not in fleet_html
    assert "Sync vor" not in fleet_html


def test_index_and_existing_global_badge_untouched(tmp_path):
    """The shell-global badge (base.html/_freshness_badge.html) must keep
    reporting the FRESHEST source fleet-wide — Feature 4 is additive, not a
    replacement."""
    duckdb_path = _build_two_source_duckdb(
        tmp_path,
        ai_synced_at=datetime.datetime(2026, 7, 15, 9, 20, tzinfo=UTC),
        trades_synced_at=datetime.datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
    )
    client = dashboard_app.create_app(duckdb_path).test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Global badge still exists and still uses freshest-wins: closed_ai_signals
    # (last_row_ts 09:15) is fresher than closed_trades (09:00).
    assert 'hx-get="/panels/freshness"' in html
    assert "Stand 09:15" in html

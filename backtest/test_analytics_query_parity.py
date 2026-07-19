# backtest/test_analytics_query_parity.py
"""Parity tests for the T-2026-CU-9050-175 DuckDB query optimisations.

HARD RULE of that task: every optimised query must return BIT-IDENTICAL
results — same rows, same values, same ordering. Each test therefore runs the
OLD query shape (either the retained pure-Python reference pipeline —
``bot_trade_rows`` / ``_daily_buckets_by_bot`` / ``_leaderboard_row`` — or a
verbatim copy of the pre-T-175 SQL) against the optimised production function
on the same temporary DuckDB and asserts full-payload equality via
``json.dumps`` (which is ordering-sensitive for both lists and dict insertion
order).

Fixture style mirrors backtest/test_dashboard_leaderboard.py: the Postgres
boundary is replaced by a synthetic ``ListFetcher`` feeding the real
``AnalyticsExporter`` into a temporary DuckDB file, with the ACTUAL live
column names — closed_ai_signals AND closed_trades (to exercise the UNION
path) AND regime_history (to exercise the ASOF join).

Run with:
    pytest backtest/test_analytics_query_parity.py -v
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Any

import duckdb
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import analytics_api  # noqa: E402
from tools.analytics_api import (  # noqa: E402
    _bot_filter,
    _daily_buckets_by_bot,
    _existing_outcome_tables,
    _leaderboard_row,
    _outcomes_cte,
    _rolling_series_for_bot,
    _winrate,
    bot_leaderboard,
    bot_regime_matrix,
    bot_trade_rows,
    connect_ro,
    rolling_success_rate_series,
    success_rate_timeseries,
)
from tools.analytics_export import SOURCES_BY_NAME, AnalyticsExporter  # noqa: E402

_BASE = datetime.datetime(2026, 7, 1, 12, 0, 0)  # noqa: DTZ001  (naive-local, per exporter contract)


def _j(payload: Any) -> str:
    return json.dumps(payload, default=str)


class ListFetcher:
    def __init__(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self.data = data

    def fetch(self, spec, cursor, limit):  # noqa: ANN001
        rows = [r for r in self.data.get(spec.name, [])]
        if spec.name == "closed_ai_signals":
            rows = [r for r in rows if r.get("close_time") is not None and r.get("status") != "ENTRY_NOT_FILLED"]
        if spec.name == "closed_trades":
            rows = [r for r in rows if r.get("posted") is not None]
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
    symbol: str | None = None,
) -> dict[str, Any]:
    return {
        "id": i,
        "symbol": symbol or f"C{i}USDT",
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


def _trade_row(
    i: int,
    *,
    strategy: str,
    direction: str,
    entry: float,
    close: float,
    posted: datetime.datetime,
    status: str = "CLOSED",
) -> dict[str, Any]:
    return {
        "id": i,
        "strategy": strategy,
        "coin": f"T{i}USDT",
        "direction": direction,
        "lev": "10x",
        "entry": entry,
        "close_price": close,
        "time": posted - datetime.timedelta(hours=2),
        "posted": posted,
        "status": status,
    }


def _regime_row(i: int, *, ts: datetime.datetime, regime: str | None) -> dict[str, Any]:
    return {
        "id": i,
        "ts": ts,
        "regime": regime,
        "alt_context": "ALT",
        "btc_price": 60000.0 + i,
        "confidence": 0.8,
        "confidence_btc": 0.7,
        "confidence_alt": 0.6,
    }


def _ts(days: int, hours: int = 0) -> datetime.datetime:
    return _BASE + datetime.timedelta(days=days, hours=hours)


def _fixture_data() -> dict[str, list[dict[str, Any]]]:
    """Two outcome tables + regime history, covering: multi-bot, LONG+SHORT,
    micro/housekeeping exclusions, closed_at TIES (ids 5/6: value-identical
    duplicate rows at the same timestamp — aggregates must be order-invariant
    for them), a trade BEFORE the first regime row (must drop from the
    matrix), and a bot living only in closed_trades (UNION coverage)."""
    ai = [
        # RUB2: mixed wins/losses incl. a SHORT win
        _ai_row(1, model="RUB2", direction="LONG", entry=100, close=110, close_time=_ts(1)),
        _ai_row(2, model="RUB2", direction="SHORT", entry=100, close=90, close_time=_ts(2)),
        _ai_row(3, model="RUB2", direction="LONG", entry=100, close=95, close_time=_ts(3)),
        _ai_row(4, model="RUB2", direction="LONG", entry=100, close=70, close_time=_ts(40)),
        # ties: two value-identical MIS2 rows at the same close_time
        _ai_row(5, model="MIS2", direction="LONG", entry=100, close=92, close_time=_ts(2, 6), symbol="TIEUSDT"),
        _ai_row(6, model="MIS2", direction="LONG", entry=100, close=92, close_time=_ts(2, 6), symbol="TIEUSDT"),
        _ai_row(7, model="MIS2", direction="LONG", entry=100, close=112, close_time=_ts(5)),
        # pre-regime trade (before first regime_history ts) — drops from matrix
        _ai_row(8, model="ABR2", direction="LONG", entry=100, close=104, close_time=_ts(0, -30)),
        # neutral (micro) + housekeeping rows — never decisive
        _ai_row(9, model="NEU1", direction="LONG", entry=100, close=100.05, close_time=_ts(1)),
        _ai_row(10, model="NEU1", direction="LONG", entry=100, close=50, close_time=_ts(2), status="DELISTED"),
        # out-of-band pnl (>MAX_ABS_PNL_PCT) — never decisive
        _ai_row(11, model="RUB2", direction="LONG", entry=100, close=350, close_time=_ts(3, 1)),
    ]
    trades = [
        # a bot that exists ONLY in closed_trades (UNION path)
        _trade_row(100, strategy="Main Channel", direction="LONG", entry=50, close=55, posted=_ts(2, 3)),
        _trade_row(101, strategy="Main Channel", direction="SHORT", entry=50, close=54, posted=_ts(4)),
        _trade_row(102, strategy="Main Channel", direction="LONG", entry=50, close=48, posted=_ts(39)),
    ]
    regimes = [
        _regime_row(1, ts=_ts(0), regime="BULL"),
        _regime_row(2, ts=_ts(2), regime="BEAR"),
        _regime_row(3, ts=_ts(3), regime=None),  # NULL regime — filtered upstream
        _regime_row(4, ts=_ts(4, 12), regime="BULL"),
    ]
    return {"closed_ai_signals": ai, "closed_trades": trades, "regime_history": regimes}


@pytest.fixture(scope="module")
def duckdb_path(tmp_path_factory) -> str:
    tmp = tmp_path_factory.mktemp("parity")
    path = str(tmp / "analytics.duckdb")
    AnalyticsExporter(
        path,
        str(tmp / "parquet"),
        ListFetcher(_fixture_data()),
        sources=[
            SOURCES_BY_NAME["closed_ai_signals"],
            SOURCES_BY_NAME["closed_trades"],
            SOURCES_BY_NAME["regime_history"],
        ],
    ).run()
    return path


@pytest.fixture()
def con(duckdb_path):
    c = connect_ro(duckdb_path)
    yield c
    c.close()


# ─────────────────────────────────────────────────────────────────────────────
# Reference (pre-T-175) implementations
# ─────────────────────────────────────────────────────────────────────────────


def _ref_rolling(con, *, bots=None, window=30):  # noqa: ANN001
    """Old rolling_success_rate_series: python bucketing over bot_trade_rows."""
    trades = bot_trade_rows(con, bots=bots)
    by_bot = _daily_buckets_by_bot(trades)
    series = {bot: _rolling_series_for_bot(daily, window) for bot, daily in by_bot.items()}
    return {"window": window, "bots": sorted(series), "series": series}


def _ref_leaderboard(con, *, bots=None, sort_by="pnl_sum_pct"):  # noqa: ANN001
    """Old bot_leaderboard: per-trade dicts grouped, then _leaderboard_row."""
    by_bot: dict[str, list[dict[str, Any]]] = {}
    for trade in bot_trade_rows(con, bots=bots):
        by_bot.setdefault(trade["bot"], []).append(trade)
    rows = [_leaderboard_row(bot, trades) for bot, trades in by_bot.items()]
    rows.sort(key=lambda r: r[sort_by] if r[sort_by] is not None else float("-inf"), reverse=True)
    return {"bots": rows, "sort_by": sort_by}


def _ref_success_rate(con, *, bots=None, windows=(7, 30, 90), as_of=None):  # noqa: ANN001
    """Old success_rate_timeseries: one full CTE scan PER window."""
    tables = _existing_outcome_tables(con)
    if not tables:
        return {"as_of": None, "bots": [], "windows": {}, "daily": []}
    cte = _outcomes_cte(tables)
    if as_of is None:
        row = con.execute(f"{cte} SELECT max(closed_at) FROM flagged").fetchone()
        as_of = row[0] if row and row[0] is not None else None
    if as_of is None:
        return {"as_of": None, "bots": [], "windows": {}, "daily": []}
    windows_out: dict[int, list[dict[str, Any]]] = {}
    result_bots: set[str] = set()
    for w in windows:
        params: list[Any] = [as_of, as_of]
        bot_sql = _bot_filter(bots, params)
        rows = con.execute(
            f"{cte} SELECT bot, count(*) FILTER (WHERE is_decisive) AS n, "
            "count(*) FILTER (WHERE is_win) AS wins FROM flagged "
            f"WHERE closed_at > (CAST(? AS TIMESTAMP) - INTERVAL {int(w)} DAY) "
            "AND closed_at <= CAST(? AS TIMESTAMP)"
            f"{bot_sql} GROUP BY bot ORDER BY bot",
            params,
        ).fetchall()
        entries = []
        for bot, n, wins in rows:
            n, wins = int(n), int(wins)
            result_bots.add(bot)
            entries.append({"bot": bot, "n": n, "wins": wins, "winrate": _winrate(wins, n)})
        windows_out[w] = entries
    params = []
    bot_sql = _bot_filter(bots, params)
    daily_rows = con.execute(
        f"{cte} SELECT d, bot, count(*) FILTER (WHERE is_decisive) AS n, "
        "count(*) FILTER (WHERE is_win) AS wins FROM flagged "
        f"WHERE bot IS NOT NULL{bot_sql} "
        "GROUP BY d, bot HAVING count(*) FILTER (WHERE is_decisive) > 0 ORDER BY d, bot",
        params,
    ).fetchall()
    daily = []
    for d, bot, n, wins in daily_rows:
        n, wins = int(n), int(wins)
        result_bots.add(bot)
        daily.append(
            {
                "date": d.isoformat() if hasattr(d, "isoformat") else str(d),
                "bot": bot,
                "n": n,
                "wins": wins,
                "winrate": _winrate(wins, n),
            }
        )
    return {
        "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
        "bots": sorted(result_bots),
        "windows": windows_out,
        "daily": daily,
    }


def _ref_regime_matrix(con, *, bots=None):  # noqa: ANN001
    """Old bot_regime_matrix: ASOF LEFT JOIN + WHERE + inner ORDER BY ts."""
    tables = _existing_outcome_tables(con)
    cte = _outcomes_cte(tables)
    params: list[Any] = []
    bot_sql = _bot_filter(bots, params)
    rows = con.execute(
        f"{cte}, "
        "regime_sorted AS (SELECT ts, regime FROM regime_history WHERE regime IS NOT NULL ORDER BY ts), "
        "decisive AS (SELECT bot, closed_at, pnl_pct, is_win FROM flagged "
        f"WHERE is_decisive AND bot IS NOT NULL{bot_sql}) "
        "SELECT d.bot, r.regime, count(*) AS n, count(*) FILTER (WHERE d.is_win) AS wins, "
        "sum(d.pnl_pct) AS pnl_sum "
        "FROM decisive d ASOF LEFT JOIN regime_sorted r ON d.closed_at >= r.ts "
        "WHERE r.regime IS NOT NULL GROUP BY d.bot, r.regime ORDER BY d.bot, r.regime",
        params,
    ).fetchall()
    bots_seen: set[str] = set()
    regimes_seen: set[str] = set()
    cells: dict[str, dict[str, dict[str, Any]]] = {}
    for bot, regime, n, wins, pnl_sum in rows:
        n, wins = int(n), int(wins)
        pnl_sum = float(pnl_sum)
        bots_seen.add(bot)
        regimes_seen.add(regime)
        cells.setdefault(bot, {})[regime] = {
            "n": n,
            "wins": wins,
            "winrate": _winrate(wins, n),
            "pnl_sum_pct": round(pnl_sum, 4),
            "expectancy_pct": round(pnl_sum / n, 4) if n else None,
        }
    return {"bots": sorted(bots_seen), "regimes": sorted(regimes_seen), "cells": cells}


# ─────────────────────────────────────────────────────────────────────────────
# rolling_success_rate_series — SQL daily aggregation vs python bucketing
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("window", [7, 30, 90])
def test_rolling_parity_all_windows(con, window):
    assert _j(rolling_success_rate_series(con, window=window)) == _j(_ref_rolling(con, window=window))


def test_rolling_parity_bot_filter(con):
    new = rolling_success_rate_series(con, bots=["RUB2", "Main Channel"], window=30)
    ref = _ref_rolling(con, bots=["RUB2", "Main Channel"], window=30)
    assert _j(new) == _j(ref)
    assert new["bots"] == ["Main Channel", "RUB2"]


def test_rolling_parity_single_bot(con):
    assert _j(rolling_success_rate_series(con, bots=["MIS2"], window=7)) == _j(_ref_rolling(con, bots=["MIS2"], window=7))


# ─────────────────────────────────────────────────────────────────────────────
# bot_leaderboard — streamed column aggregation vs per-trade dict pipeline
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sort_by", ["pnl_sum_pct", "expectancy_pct", "winrate", "n"])
def test_leaderboard_parity_all_sort_keys(con, sort_by):
    assert _j(bot_leaderboard(con, sort_by=sort_by)) == _j(_ref_leaderboard(con, sort_by=sort_by))


def test_leaderboard_parity_bot_filter(con):
    new = bot_leaderboard(con, bots=["MIS2"])
    assert _j(new) == _j(_ref_leaderboard(con, bots=["MIS2"]))
    assert [r["bot"] for r in new["bots"]] == ["MIS2"]
    # the tied duplicate rows (ids 5/6) are decisive losses: n counts both
    assert new["bots"][0]["n"] == 3


def test_leaderboard_numpy_and_fallback_agree_on_tie_free_fixture(con, monkeypatch):
    """The lazy numpy fast path and the pure-Python fallback agree bit-for-bit
    ON THIS FIXTURE — an honest, scoped claim (T-2026-CU-9050-175 fix round).

    SCOPE / why this is NOT a production-determinism proof: each
    ``bot_leaderboard`` call re-executes the ``ORDER BY bot, closed_at`` query,
    which has no deterministic tie-breaker; at production scale two calls can
    return duplicate-``closed_at`` rows in a different relative order and so
    diverge on the two path-dependent risk metrics (``max_drawdown_pp`` /
    ``max_loss_streak``) — the SAME pre-existing nondeterminism class the old
    pipeline had. This fixture avoids that: its only tied rows (ids 5/6) are
    VALUE-identical (same pnl, same is_win), so the per-bot aggregation is
    order-invariant here regardless of which tie order each execution picks.
    What this test therefore proves is that the two aggregation
    IMPLEMENTATIONS compute the same result from an equivalent row stream — not
    that two separate leaderboard calls yield byte-identical risk metrics on
    tied production data (they need not; see the module/`_leaderboard_rows_streamed`
    docstrings). The order-invariant fields (n/wins/winrate/pnl_sum_pct/
    expectancy_pct) are stable in either case."""
    with_numpy = bot_leaderboard(con)
    monkeypatch.setattr(analytics_api, "_numpy", lambda: None)
    without_numpy = bot_leaderboard(con)
    assert _j(with_numpy) == _j(without_numpy)


def test_leaderboard_numpy_and_fallback_identical_on_one_shared_row_stream():
    """Stronger, tie-robust proof: run BOTH aggregation implementations over
    ONE already-materialised row stream (with VALUE-DIFFERENT tied rows) and
    assert equality — this isolates "the two implementations agree" from "the
    query returns a stable order", so it holds even where a live re-execution
    would reorder ties.

    Builds the numpy fast path (run-boundary slicing + ``.tolist()``) and the
    pure dict-grouping fallback by hand from the SAME ``rows`` list, mirroring
    :func:`analytics_api._leaderboard_rows_streamed`'s two branches, and feeds
    both into the shared :func:`analytics_api._leaderboard_row_from_columns`."""
    np = analytics_api._numpy()
    if np is None:  # pragma: no cover - numpy is a repo dependency
        pytest.skip("numpy unavailable")
    # Two bots; bot B has two rows that TIE (would-be same closed_at) but carry
    # DIFFERENT pnl — the exact case a live re-scan could reorder. Here the
    # order is fixed once, so both implementations must match on it.
    rows = [
        ("A", 10.0, True),
        ("A", -4.0, False),
        ("B", -7.0, False),
        ("B", 3.0, True),
        ("B", -2.0, False),
    ]

    # pure fallback branch
    by_bot: dict[str, tuple[list[float], list[bool]]] = {}
    for bot, pnl, win in rows:
        acc = by_bot.setdefault(bot, ([], []))
        acc[0].append(float(pnl))
        acc[1].append(bool(win))
    pure = [analytics_api._leaderboard_row_from_columns(bot, pnl, win) for bot, (pnl, win) in by_bot.items()]

    # numpy fast-path branch over the identical stream
    bot_col = np.array([r[0] for r in rows], dtype=object)
    pnl_col = np.array([r[1] for r in rows], dtype=np.float64)
    win_col = np.array([r[2] for r in rows], dtype=bool)
    change = np.empty(len(bot_col), dtype=bool)
    change[0] = True
    change[1:] = bot_col[1:] != bot_col[:-1]
    starts = np.flatnonzero(change)
    ends = np.append(starts[1:], len(bot_col))
    fast = [
        analytics_api._leaderboard_row_from_columns(str(bot_col[s]), pnl_col[s:e].tolist(), win_col[s:e].tolist())
        for s, e in zip(starts, ends, strict=True)
    ]
    assert _j(fast) == _j(pure)
    # sanity: the path-dependent metrics are actually exercised. Bot B pnl
    # stream [-7, 3, -2]: equity 0→-7→-4→-6, peak -7→-4→-4, so worst dd = -2.0;
    # loss runs are 1,0,1 → max streak 1.
    b = next(r for r in pure if r["bot"] == "B")
    assert b["max_loss_streak"] == 1
    assert b["max_drawdown_pp"] == -2.0


def test_leaderboard_includes_union_only_bot(con):
    bots = [r["bot"] for r in bot_leaderboard(con)["bots"]]
    assert "Main Channel" in bots  # closed_trades-only bot survives the UNION
    assert "NEU1" not in bots  # zero decisive trades → absent, not n=0


# ─────────────────────────────────────────────────────────────────────────────
# success_rate_timeseries — merged single-scan windows vs per-window scans
# ─────────────────────────────────────────────────────────────────────────────


def test_success_rate_parity_default_windows(con):
    assert _j(success_rate_timeseries(con)) == _j(_ref_success_rate(con))


def test_success_rate_parity_bot_filter_and_subset_window(con):
    new = success_rate_timeseries(con, bots=["RUB2"], windows=[7])
    assert _j(new) == _j(_ref_success_rate(con, bots=["RUB2"], windows=[7]))


def test_success_rate_parity_duplicate_and_unsorted_windows(con):
    new = success_rate_timeseries(con, windows=[30, 7, 30])
    ref = _ref_success_rate(con, windows=[30, 7, 30])
    assert _j(new) == _j(ref)
    assert list(new["windows"].keys()) == [30, 7]  # dict keyed by w, first-set order


def test_success_rate_parity_explicit_as_of(con):
    as_of = _ts(3)  # anchor mid-history: id-4/-102 late trades fall outside
    assert _j(success_rate_timeseries(con, as_of=as_of)) == _j(_ref_success_rate(con, as_of=as_of))


def test_success_rate_windowless_bot_absent_from_short_window(con):
    """A bot whose only rows are outside a window must be ABSENT from that
    window's entry list (not present with n=0) — the inclusion rule the
    merged query reconstructs via its any-rows filter count."""
    as_of = _ts(40)  # ABR2's only trade is at day -1.25 → outside 7d, inside 90d
    payload = success_rate_timeseries(con, as_of=as_of, windows=[7, 90])
    assert "ABR2" not in [e["bot"] for e in payload["windows"][7]]
    assert "ABR2" in [e["bot"] for e in payload["windows"][90]]
    assert _j(payload) == _j(_ref_success_rate(con, as_of=as_of, windows=[7, 90]))


# ─────────────────────────────────────────────────────────────────────────────
# bot_regime_matrix — ASOF inner join vs ASOF LEFT JOIN + WHERE
# ─────────────────────────────────────────────────────────────────────────────


def test_regime_matrix_parity(con):
    new = bot_regime_matrix(con)
    assert _j(new) == _j(_ref_regime_matrix(con))
    # the pre-regime ABR2 trade has no ASOF match → dropped, never "UNKNOWN"
    assert "ABR2" not in new["bots"]
    assert set(new["regimes"]) == {"BULL", "BEAR"}


def test_regime_matrix_parity_bot_filter(con):
    assert _j(bot_regime_matrix(con, bots=["RUB2"])) == _j(_ref_regime_matrix(con, bots=["RUB2"]))


# ─────────────────────────────────────────────────────────────────────────────
# Empty-substrate degrade (no outcome tables at all)
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_substrate_degrades(tmp_path):
    path = str(tmp_path / "empty.duckdb")
    duckdb.connect(path).close()  # file exists, zero tables
    c = connect_ro(path)
    try:
        assert bot_leaderboard(c) == {"bots": [], "sort_by": "pnl_sum_pct"}
        assert rolling_success_rate_series(c) == {"window": 30, "bots": [], "series": {}}
        assert success_rate_timeseries(c) == {"as_of": None, "bots": [], "windows": {}, "daily": []}
        assert bot_regime_matrix(c) == {"bots": [], "regimes": [], "cells": {}}
    finally:
        c.close()


# ─────────────────────────────────────────────────────────────────────────────
# Panel-data cache (tools/dashboard/app.py _cached) — hit path + parity
# ─────────────────────────────────────────────────────────────────────────────


def test_panel_cache_serves_identical_context_without_reconnecting(duckdb_path, monkeypatch):
    from tools.dashboard import app as dashboard_app

    calls = {"n": 0}
    real_connect = dashboard_app.connect_ro

    def counting_connect(p):  # noqa: ANN001
        calls["n"] += 1
        return real_connect(p)

    monkeypatch.setattr(dashboard_app, "connect_ro", counting_connect)

    uncached = dashboard_app._leaderboard_context(duckdb_path)
    cache = analytics_api._PollCache(duckdb_path)
    first = dashboard_app._leaderboard_context(duckdb_path, cache=cache)
    second = dashboard_app._leaderboard_context(duckdb_path, cache=cache)

    # identical rendered data (freshness age is wall-clock-derived but the
    # fixture file's synced_at is fixed, so the whole context must match)
    assert _j(first) == _j(uncached)
    assert _j(second) == _j(first)
    # one connection for the uncached call + one for the cache MISS; the HIT
    # must not open another
    assert calls["n"] == 2


def test_panel_cache_invalidates_on_new_export(tmp_path):
    """Cache BUST path (T-2026-CU-9050-175 fix round): after the export file's
    freshness token advances (a fresh export appends rows → new mtime/size),
    the SAME cached panel context must serve the NEW data, not the stale cached
    payload — driven through the real ``app.py`` panel path + a real re-export,
    not a mocked token.

    Complements ``test_panel_cache_serves_identical_context_without_reconnecting``
    (which only proves a repeat HIT on an unchanged file): here the file
    genuinely changes between two context calls that share one ``_PollCache``.
    """
    from tools.dashboard import app as dashboard_app

    path = str(tmp_path / "analytics.duckdb")
    parquet_root = str(tmp_path / "parquet")

    def _export(ai_rows: list[dict[str, Any]]) -> None:
        AnalyticsExporter(
            path,
            parquet_root,
            ListFetcher({"closed_ai_signals": ai_rows}),
            sources=[SOURCES_BY_NAME["closed_ai_signals"]],
        ).run()

    # Initial export: RUB2 has two decisive wins.
    initial = [
        _ai_row(1, model="RUB2", direction="LONG", entry=100, close=110, close_time=_ts(1)),
        _ai_row(2, model="RUB2", direction="LONG", entry=100, close=120, close_time=_ts(2)),
    ]
    _export(initial)

    cache = analytics_api._PollCache(path)
    ctx1 = dashboard_app._leaderboard_context(path, cache=cache)
    rub2_before = next(r for r in ctx1["bots"] if r["bot"] == "RUB2")
    assert rub2_before["n"] == 2

    # Fresh export APPENDS two more decisive RUB2 trades (higher ids/timestamps
    # so the incremental watermark picks them up) → file grows → token advances.
    appended = initial + [
        _ai_row(3, model="RUB2", direction="LONG", entry=100, close=90, close_time=_ts(3)),
        _ai_row(4, model="RUB2", direction="LONG", entry=100, close=130, close_time=_ts(4)),
    ]
    _export(appended)

    # Same cache object, but the token must have advanced → cache busts → the
    # context reflects all four trades, not the cached n=2.
    ctx2 = dashboard_app._leaderboard_context(path, cache=cache)
    rub2_after = next(r for r in ctx2["bots"] if r["bot"] == "RUB2")
    assert rub2_after["n"] == 4
    # And it matches a fresh uncached read of the new file exactly.
    assert _j(ctx2) == _j(dashboard_app._leaderboard_context(path))

# backtest/test_analytics_export.py
"""DB-free tests for the Z1 analytics export + success-rate endpoint
(T-2026-CU-9050-131).

The build machine has no DB credentials, so the Postgres boundary is replaced
by a synthetic ``ListFetcher`` that mirrors the SELECT contract of
``PostgresFetcher`` (keyset ordering, LIMIT paging, closed-row filter). Every
other layer — watermark advance, DuckDB materialisation, Parquet partitioning,
freshness, and the success-rate query — runs for real against a temporary
DuckDB file.

Covers SPEC AK1–AK7. Run with: pytest backtest/test_analytics_export.py -v
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Any, Callable, Sequence

import duckdb
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import analytics_api  # noqa: E402
from tools.analytics_export import (  # noqa: E402
    SOURCES_BY_NAME,
    AnalyticsExporter,
    Cursor,
    SourceSpec,
    connect,
    data_freshness,
)

UTC = datetime.timezone.utc


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic fetcher — mirrors PostgresFetcher's SELECT contract, no DB
# ─────────────────────────────────────────────────────────────────────────────


def _closed_predicate(spec: SourceSpec, row: dict[str, Any]) -> bool:
    """Python mirror of each source's SQL ``closed_filter`` — so open/unfilled
    fixture rows never reach the exporter, exactly as the DB query would exclude
    them."""
    if spec.name == "closed_trades":
        return row.get("posted") is not None
    if spec.name == "closed_ai_signals":
        return row.get("close_time") is not None and row.get("status") != "ENTRY_NOT_FILLED"
    return True


class ListFetcher:
    """In-memory RowFetcher over fixture rows keyed by logical source name."""

    def __init__(
        self,
        data: dict[str, list[dict[str, Any]]],
        closed_predicate: Callable[[SourceSpec, dict[str, Any]], bool] | None = None,
    ) -> None:
        self.data = data
        self.closed_predicate = closed_predicate
        self.calls: list[tuple[str, Cursor | None, int]] = []

    def fetch(self, spec: SourceSpec, cursor: Cursor | None, limit: int) -> list[dict[str, Any]]:
        self.calls.append((spec.name, cursor, limit))
        rows = list(self.data.get(spec.name, []))
        if self.closed_predicate is not None:
            rows = [r for r in rows if self.closed_predicate(spec, r)]
        rows.sort(key=lambda r: (r[spec.ts_col], r[spec.pk_col]))
        if cursor is not None:
            rows = [r for r in rows if (r[spec.ts_col], r[spec.pk_col]) > cursor]
        return rows[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# Fixture builders
# ─────────────────────────────────────────────────────────────────────────────

# Naive on purpose: the source columns are TIMESTAMP WITHOUT TIME ZONE and the
# exporter stores/compares them naive (see analytics_export TIMEZONE note).
_BASE = datetime.datetime(2026, 7, 10, 12, 0, 0)  # noqa: DTZ001


def _ai_row(i: int, *, model: str, direction: str, entry: float, close: float,
            close_time: datetime.datetime | None, status: str = "TP1") -> dict[str, Any]:
    return {
        "id": i, "symbol": f"C{i}USDT", "model": model, "direction": direction,
        "entry": entry, "close_price": close, "targets_hit": 1,
        "open_time": close_time - datetime.timedelta(hours=1) if close_time else _BASE,
        "close_time": close_time, "status": status, "lev": "20x",
    }


def _trade_row(i: int, *, strategy: str, direction: str, entry: float, close: float,
               posted: datetime.datetime | None, status: str = "TP") -> dict[str, Any]:
    return {
        "id": i, "strategy": strategy, "coin": f"C{i}USDT", "direction": direction,
        "lev": "20x", "entry": entry, "close_price": close,
        "time": posted - datetime.timedelta(hours=2) if posted else _BASE,
        "posted": posted, "status": status,
    }


def _regime_row(i: int, ts: datetime.datetime, regime: str = "TREND_UP") -> dict[str, Any]:
    return {
        "id": i, "ts": ts, "regime": regime, "alt_context": "ALT_STRONG",
        "btc_price": 60000.0 + i, "confidence": 0.8, "confidence_btc": 0.8, "confidence_alt": 0.7,
    }


def _fixed_clock(dt: datetime.datetime) -> Callable[[], datetime.datetime]:
    return lambda: dt


def _paths(tmp_path) -> tuple[str, str]:
    return str(tmp_path / "analytics.duckdb"), str(tmp_path / "parquet")


def _make_exporter(tmp_path, fetcher, *, sources: Sequence[SourceSpec] | None = None,
                   batch_size: int = 5000, clock=None) -> AnalyticsExporter:
    duckdb_path, parquet_root = _paths(tmp_path)
    kwargs: dict[str, Any] = {"batch_size": batch_size}
    if sources is not None:
        kwargs["sources"] = sources
    if clock is not None:
        kwargs["clock"] = clock
    return AnalyticsExporter(duckdb_path, parquet_root, fetcher, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# AK3 — four sources land in DuckDB tables
# ─────────────────────────────────────────────────────────────────────────────


def test_all_sources_materialise_to_duckdb(tmp_path):
    data = {
        "closed_ai_signals": [_ai_row(1, model="ABR2", direction="LONG", entry=100, close=110,
                                      close_time=_BASE)],
        "closed_trades": [_trade_row(1, strategy="SMC", direction="SHORT", entry=200, close=190,
                                     posted=_BASE)],
        "ml_predictions": [{"id": 1, "model_name": "MIS2", "coin": "BTCUSDT", "direction": "LONG",
                            "entry": 60000.0, "confidence": 0.7, "posted": True, "time": _BASE}],
        "regime_history": [_regime_row(1, _BASE)],
    }
    exporter = _make_exporter(tmp_path, ListFetcher(data))
    results = exporter.run()

    assert {r.name for r in results} == {"closed_ai_signals", "closed_trades",
                                         "ml_predictions", "regime_history"}
    assert all(r.rows_exported == 1 for r in results)

    con = connect(_paths(tmp_path)[0])
    try:
        for name in data:
            assert con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] == 1
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# AK3 — Parquet partitions written, one dir per date
# ─────────────────────────────────────────────────────────────────────────────


def test_parquet_partitions_by_date(tmp_path):
    d1 = datetime.datetime(2026, 7, 10, 9, 0)  # noqa: DTZ001
    d2 = datetime.datetime(2026, 7, 11, 9, 0)  # noqa: DTZ001
    data = {"regime_history": [_regime_row(1, d1), _regime_row(2, d2), _regime_row(3, d2)]}
    exporter = _make_exporter(tmp_path, ListFetcher(data),
                              sources=[SOURCES_BY_NAME["regime_history"]])
    exporter.run()

    _, parquet_root = _paths(tmp_path)
    p1 = os.path.join(parquet_root, "regime_history", "dt=2026-07-10", "data.parquet")
    p2 = os.path.join(parquet_root, "regime_history", "dt=2026-07-11", "data.parquet")
    assert os.path.exists(p1) and os.path.exists(p2)

    # Parquet content agrees with DuckDB for the 2-row partition.
    con = duckdb.connect()
    try:
        n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{p2.replace(os.sep, '/')}')").fetchone()[0]
    finally:
        con.close()
    assert n == 2


# ─────────────────────────────────────────────────────────────────────────────
# AK1 — incremental watermark: no-new-rows run exports nothing; new rows only
# ─────────────────────────────────────────────────────────────────────────────


def test_incremental_second_run_is_noop_without_new_rows(tmp_path):
    data = {"regime_history": [_regime_row(i, _BASE + datetime.timedelta(minutes=5 * i))
                               for i in range(1, 6)]}
    fetcher = ListFetcher(data)
    src = [SOURCES_BY_NAME["regime_history"]]

    first = _make_exporter(tmp_path, fetcher, sources=src).run()
    assert first[0].rows_exported == 5

    second = _make_exporter(tmp_path, fetcher, sources=src).run()
    assert second[0].rows_exported == 0
    assert second[0].rows_total == 5  # nothing double-counted


def test_incremental_second_run_picks_up_only_new_rows(tmp_path):
    rows = [_regime_row(i, _BASE + datetime.timedelta(minutes=5 * i)) for i in range(1, 4)]
    data = {"regime_history": rows}
    fetcher = ListFetcher(data)
    src = [SOURCES_BY_NAME["regime_history"]]

    _make_exporter(tmp_path, fetcher, sources=src).run()
    # Two new rows appear after the last watermark.
    rows.append(_regime_row(4, _BASE + datetime.timedelta(minutes=25)))
    rows.append(_regime_row(5, _BASE + datetime.timedelta(minutes=30)))

    second = _make_exporter(tmp_path, fetcher, sources=src).run()
    assert second[0].rows_exported == 2
    assert second[0].rows_total == 5


def test_same_timestamp_tie_not_skipped_across_runs(tmp_path):
    """Keyset (ts, id) must not skip a row sharing the boundary timestamp."""
    ts = _BASE
    rows = [_regime_row(1, ts), _regime_row(2, ts)]  # same ts, different id
    data = {"regime_history": rows}
    fetcher = ListFetcher(data)
    src = [SOURCES_BY_NAME["regime_history"]]

    first = _make_exporter(tmp_path, fetcher, sources=src).run()
    assert first[0].rows_exported == 2
    # A third row with the SAME timestamp but a higher id arrives later.
    rows.append(_regime_row(3, ts))
    second = _make_exporter(tmp_path, fetcher, sources=src).run()
    assert second[0].rows_exported == 1
    assert second[0].rows_total == 3


# ─────────────────────────────────────────────────────────────────────────────
# AK2 — only closed / filled rows exported
# ─────────────────────────────────────────────────────────────────────────────


def test_open_and_unfilled_rows_excluded(tmp_path):
    data = {
        "closed_ai_signals": [
            _ai_row(1, model="ABR2", direction="LONG", entry=100, close=110, close_time=_BASE),
            _ai_row(2, model="ABR2", direction="LONG", entry=100, close=0,
                    close_time=None),  # still open
            _ai_row(3, model="ABR2", direction="SHORT", entry=100, close=100,
                    close_time=_BASE, status="ENTRY_NOT_FILLED"),  # phantom entry
        ],
        "closed_trades": [
            _trade_row(1, strategy="SMC", direction="LONG", entry=100, close=105, posted=_BASE),
            _trade_row(2, strategy="SMC", direction="LONG", entry=100, close=0, posted=None),  # open
        ],
    }
    fetcher = ListFetcher(data, closed_predicate=_closed_predicate)
    exporter = _make_exporter(
        tmp_path, fetcher,
        sources=[SOURCES_BY_NAME["closed_ai_signals"], SOURCES_BY_NAME["closed_trades"]],
    )
    results = {r.name: r for r in exporter.run()}
    assert results["closed_ai_signals"].rows_exported == 1
    assert results["closed_trades"].rows_exported == 1


# ─────────────────────────────────────────────────────────────────────────────
# AK5 — LIMIT batching equals single-batch result
# ─────────────────────────────────────────────────────────────────────────────


def test_batching_matches_single_batch(tmp_path):
    rows = [_regime_row(i, _BASE + datetime.timedelta(minutes=i)) for i in range(1, 26)]
    src = [SOURCES_BY_NAME["regime_history"]]

    big = _make_exporter(tmp_path / "a", ListFetcher({"regime_history": rows}),
                         sources=src, batch_size=1000).run()
    small_fetcher = ListFetcher({"regime_history": rows})
    small = _make_exporter(tmp_path / "b", small_fetcher, sources=src, batch_size=4).run()

    assert big[0].rows_total == small[0].rows_total == 25
    assert big[0].last_pk == small[0].last_pk == 25
    # 25 rows / batch 4 → pages of 4..4,4,4,4,4,1 → last page short-circuits the loop.
    assert len([c for c in small_fetcher.calls if c[0] == "regime_history"]) == 7


# ─────────────────────────────────────────────────────────────────────────────
# AK4 — freshness (Datenstand) is a first-class output
# ─────────────────────────────────────────────────────────────────────────────


def test_freshness_written_and_readable(tmp_path):
    synced = datetime.datetime(2026, 7, 12, 8, 30, tzinfo=UTC)
    last_ts = _BASE + datetime.timedelta(minutes=10)
    data = {"regime_history": [_regime_row(1, _BASE), _regime_row(2, last_ts)]}
    exporter = _make_exporter(tmp_path, ListFetcher(data),
                              sources=[SOURCES_BY_NAME["regime_history"]],
                              clock=_fixed_clock(synced))
    exporter.run()

    con = connect(_paths(tmp_path)[0])
    try:
        fresh = {f["source"]: f for f in data_freshness(con)}
    finally:
        con.close()
    assert "regime_history" in fresh
    row = fresh["regime_history"]
    assert row["rows_total"] == 2
    assert row["last_run_rows"] == 2
    assert row["last_row_ts"] == last_ts.isoformat()
    assert row["synced_at"] == synced.replace(tzinfo=None).isoformat()
    assert row["synced_at_tz"] == "UTC"
    assert row["ts_is_naive_local"] is True


def test_freshness_refreshes_synced_at_on_empty_run(tmp_path):
    data = {"regime_history": [_regime_row(1, _BASE)]}
    fetcher = ListFetcher(data)
    src = [SOURCES_BY_NAME["regime_history"]]
    _make_exporter(tmp_path, fetcher, sources=src,
                   clock=_fixed_clock(datetime.datetime(2026, 7, 12, 8, 0, tzinfo=UTC))).run()

    later = datetime.datetime(2026, 7, 12, 9, 0, tzinfo=UTC)
    _make_exporter(tmp_path, fetcher, sources=src, clock=_fixed_clock(later)).run()

    con = connect(_paths(tmp_path)[0])
    try:
        fresh = {f["source"]: f for f in data_freshness(con)}
    finally:
        con.close()
    # No new rows, but synced_at advanced — the panel's "Sync vor X min" stays honest.
    assert fresh["regime_history"]["last_run_rows"] == 0
    assert fresh["regime_history"]["synced_at"] == later.replace(tzinfo=None).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# AK6 — success-rate endpoint (DuckDB-only)
# ─────────────────────────────────────────────────────────────────────────────


def _build_success_fixture(tmp_path):
    """One bot with a known win/loss/neutral mix across two days."""
    as_of = datetime.datetime(2026, 7, 15, 12, 0)  # noqa: DTZ001
    day0 = as_of - datetime.timedelta(days=1)
    ai = [
        # ABR2: 2 wins, 1 loss → winrate 2/3 on the decisive set
        _ai_row(1, model="ABR2", direction="LONG", entry=100, close=110, close_time=day0),   # win
        _ai_row(2, model="ABR2", direction="LONG", entry=100, close=105, close_time=as_of),   # win
        _ai_row(3, model="ABR2", direction="SHORT", entry=100, close=110, close_time=as_of),   # loss
        # neutral: micro move (|pnl| <= 0.1%) → excluded from denominator
        _ai_row(4, model="ABR2", direction="LONG", entry=100, close=100.05, close_time=as_of),
        # neutral: housekeeping status → excluded
        _ai_row(5, model="ABR2", direction="LONG", entry=100, close=50, close_time=as_of,
                status="DELISTED"),
        # MIS2: 1 win
        _ai_row(6, model="MIS2", direction="LONG", entry=100, close=120, close_time=as_of),
    ]
    data = {"closed_ai_signals": ai}
    exporter = _make_exporter(tmp_path, ListFetcher(data),
                              sources=[SOURCES_BY_NAME["closed_ai_signals"]])
    exporter.run()
    return _paths(tmp_path)[0], as_of


def test_success_rate_winrate_and_neutral_exclusion(tmp_path):
    duckdb_path, as_of = _build_success_fixture(tmp_path)
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        payload = analytics_api.success_rate_timeseries(con, windows=[7, 30], as_of=as_of)
    finally:
        con.close()

    w7 = {e["bot"]: e for e in payload["windows"][7]}
    # ABR2: 3 decisive (2 win, 1 loss), 2 neutrals excluded → winrate 2/3.
    assert w7["ABR2"]["n"] == 3
    assert w7["ABR2"]["wins"] == 2
    assert w7["ABR2"]["winrate"] == pytest.approx(2 / 3, abs=1e-6)
    assert w7["MIS2"]["n"] == 1 and w7["MIS2"]["winrate"] == 1.0
    assert set(payload["bots"]) == {"ABR2", "MIS2"}


def test_success_rate_bot_multiselect(tmp_path):
    duckdb_path, as_of = _build_success_fixture(tmp_path)
    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        payload = analytics_api.success_rate_timeseries(con, bots=["MIS2"], windows=[30], as_of=as_of)
    finally:
        con.close()
    bots_in_window = {e["bot"] for e in payload["windows"][30]}
    assert bots_in_window == {"MIS2"}
    assert all(d["bot"] == "MIS2" for d in payload["daily"])


def test_success_rate_rolling_window_excludes_old_trades(tmp_path):
    as_of = datetime.datetime(2026, 7, 15, 12, 0)  # noqa: DTZ001
    ai = [
        _ai_row(1, model="ABR2", direction="LONG", entry=100, close=110,
                close_time=as_of - datetime.timedelta(days=40)),  # outside 7/30, inside 90
        _ai_row(2, model="ABR2", direction="LONG", entry=100, close=110,
                close_time=as_of - datetime.timedelta(days=2)),   # inside all
    ]
    exporter = _make_exporter(tmp_path, ListFetcher({"closed_ai_signals": ai}),
                              sources=[SOURCES_BY_NAME["closed_ai_signals"]])
    exporter.run()
    con = duckdb.connect(_paths(tmp_path)[0], read_only=True)
    try:
        payload = analytics_api.success_rate_timeseries(con, windows=[7, 30, 90], as_of=as_of)
    finally:
        con.close()
    n_by_window = {w: (e[0]["n"] if e else 0) for w, e in payload["windows"].items()}
    assert n_by_window[7] == 1
    assert n_by_window[30] == 1
    assert n_by_window[90] == 2


def test_success_rate_empty_duckdb(tmp_path):
    # Export nothing → tables exist but empty → graceful empty payload.
    exporter = _make_exporter(tmp_path, ListFetcher({}),
                              sources=[SOURCES_BY_NAME["closed_ai_signals"]])
    exporter.run()
    con = duckdb.connect(_paths(tmp_path)[0], read_only=True)
    try:
        payload = analytics_api.success_rate_timeseries(con)
    finally:
        con.close()
    assert payload["as_of"] is None
    assert payload["bots"] == []


# ─────────────────────────────────────────────────────────────────────────────
# AK6 — Flask blueprint wiring (still DuckDB-only)
# ─────────────────────────────────────────────────────────────────────────────


def test_flask_endpoint_returns_success_rate(tmp_path):
    duckdb_path, _ = _build_success_fixture(tmp_path)
    app = analytics_api.create_app(duckdb_path)
    client = app.test_client()

    resp = client.get("/api/analytics/success-rate?windows=7&bots=ABR2")
    assert resp.status_code == 200
    body = resp.get_json()
    w7 = {e["bot"]: e for e in body["windows"]["7"]}
    assert w7["ABR2"]["wins"] == 2

    resp_bots = client.get("/api/analytics/bots")
    assert resp_bots.status_code == 200
    assert set(resp_bots.get_json()["bots"]) == {"ABR2", "MIS2"}

    resp_bad = client.get("/api/analytics/success-rate?windows=abc")
    assert resp_bad.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# AK7 — importing the exporter must NOT connect to Postgres
# ─────────────────────────────────────────────────────────────────────────────


def test_import_is_db_free(monkeypatch):
    # If importing/using the module triggered a psycopg2 import at module scope,
    # the build machine (no psycopg2 guarantee, no DB) would fail. The lazy
    # import lives inside PostgresFetcher._connection, never at import time.
    import importlib

    import tools.analytics_export as mod

    importlib.reload(mod)
    # Constructing the fetcher must not connect either.
    fetcher = mod.PostgresFetcher(dsn="postg://unused")
    assert fetcher._conn is None


# ─────────────────────────────────────────────────────────────────────────────
# D-2026-CLD-110 stack auflagen (T-2026-CU-9050-136)
#   1. Amplituden-Budget: read connections throttled (PRAGMA threads + memory)
#   2. Waitress serving (one kill-fest process), app.run() only for --dev smoke
#   3. Server-cache with mtime/synced_at sequence check for the 30-60 s polling
# ─────────────────────────────────────────────────────────────────────────────


def _mem_echo_to_bytes(value: Any) -> float:
    """Parse a DuckDB ``current_setting('memory_limit')`` echo to bytes.

    Tolerant of the format differences across DuckDB versions ("512.0 MiB",
    "512MB", a raw byte count) so the throttle assertion does not hinge on the
    exact human string."""
    import re

    s = str(value).strip()
    if s.isdigit():
        return float(s)
    m = re.match(r"([\d.]+)\s*([KMGT]i?B|bytes|B)$", s)
    assert m, f"unparseable memory_limit echo: {s!r}"
    factor = {
        "B": 1, "bytes": 1,
        "KB": 1000, "KiB": 1024,
        "MB": 1000 ** 2, "MiB": 1024 ** 2,
        "GB": 1000 ** 3, "GiB": 1024 ** 3,
        "TB": 1000 ** 4, "TiB": 1024 ** 4,
    }[m.group(2)]
    return float(m.group(1)) * factor


def test_ro_connection_is_throttled(tmp_path):
    """Auflage 2: connect_ro caps threads and memory_limit, stays read-only."""
    duckdb_path, _ = _build_success_fixture(tmp_path)
    con = analytics_api.connect_ro(duckdb_path)
    try:
        threads = con.execute("SELECT current_setting('threads')").fetchone()[0]
        mem = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
        # The throttled connection is still usable for the read path.
        n = con.execute('SELECT COUNT(*) FROM "closed_ai_signals"').fetchone()[0]
    finally:
        con.close()
    # threads is the definitive proof the PRAGMAs were applied.
    assert int(threads) == analytics_api.DUCKDB_THREADS == 2
    # memory_limit is constrained to our cap, well under any machine default.
    assert _mem_echo_to_bytes(mem) <= 1024 ** 3
    assert n >= 1


def test_file_token_tracks_mtime_and_size(tmp_path):
    """The freshness token is None pre-file and advances on any rewrite."""
    p = tmp_path / "analytics.duckdb"
    assert analytics_api._file_token(str(p)) is None
    p.write_bytes(b"abc")
    t1 = analytics_api._file_token(str(p))
    assert t1 is not None
    p.write_bytes(b"abcd")  # size change alone advances the token
    assert analytics_api._file_token(str(p)) != t1


def test_poll_cache_hits_until_token_advances():
    """A cached key is served without rebuilding until the token advances; a
    token bump drops the whole cache (D-110 server-cache + Auflage 1)."""
    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return {"v": calls["n"]}

    token = {"t": 1}
    cache = analytics_api._PollCache("ignored", token=lambda _p: token["t"])

    first = cache.get(("k",), build)
    second = cache.get(("k",), build)
    assert first == second == {"v": 1}
    assert calls["n"] == 1  # second poll served from memory, no rebuild

    # A different key builds separately under the same token.
    assert cache.get(("other",), build) == {"v": 2}
    assert calls["n"] == 2

    # Token advances (new export) → whole cache invalidated → next get rebuilds.
    token["t"] = 2
    assert cache.get(("k",), build) == {"v": 3}
    assert calls["n"] == 3


def test_poll_cache_disabled_builds_every_call():
    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return calls["n"]

    cache = analytics_api._PollCache("ignored", token=lambda _p: 1, enabled=False)
    assert cache.get(("k",), build) == 1
    assert cache.get(("k",), build) == 2
    assert calls["n"] == 2


def test_file_token_reflects_wal_sidecar(tmp_path):
    """A committed-but-uncheckpointed write lands in <file>.wal; the token must
    advance on it even though the main file is untouched (read-only readers
    replay the WAL, so the cache must not serve stale rows)."""
    p = tmp_path / "a.duckdb"
    p.write_bytes(b"main")
    base = analytics_api._file_token(str(p))
    (tmp_path / "a.duckdb.wal").write_bytes(b"waldata")
    assert analytics_api._file_token(str(p)) != base


def test_poll_cache_evicts_when_over_max_entries():
    """Between exports (stable token) the cache is FIFO-bounded, so a run of
    distinct keys cannot grow it without limit."""
    cache = analytics_api._PollCache("ignored", token=lambda _p: 1, max_entries=2)
    cache.get(("a",), lambda: "A")
    cache.get(("b",), lambda: "B")
    cache.get(("c",), lambda: "C")  # len hits 2 → evicts the oldest ("a")

    rebuilt = {"n": 0}

    def build_a():
        rebuilt["n"] += 1
        return "A2"

    assert cache.get(("a",), build_a) == "A2"
    assert rebuilt["n"] == 1  # ("a") was evicted, so it had to rebuild
    # ("c") stayed cached across the eviction.
    assert cache.get(("c",), lambda: pytest.fail("c should be cached")) == "C"


def test_poll_cache_thread_safe_under_concurrent_get():
    """The lock-guarded check-then-act must hold under Waitress's real thread
    pool: many threads on one key get a consistent payload and, once cached,
    further gets never rebuild."""
    import threading

    build_calls = {"n": 0}
    count_lock = threading.Lock()

    def build():
        with count_lock:
            build_calls["n"] += 1
        return {"v": "same"}

    cache = analytics_api._PollCache("ignored", token=lambda _p: 1)
    results: list[Any] = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()  # maximise overlap on the get()
        results.append(cache.get(("k",), build))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(results) == 16
    assert all(r == {"v": "same"} for r in results)
    # Once the entry is cached, further gets are served from memory (no rebuild).
    n_after = build_calls["n"]
    assert cache.get(("k",), build) == {"v": "same"}
    assert build_calls["n"] == n_after


def test_success_rate_cache_invalidates_on_new_export(tmp_path):
    """End-to-end: a poll is served from cache while the file is unchanged, and
    rebuilds once a new export advances the file token."""
    as_of = datetime.datetime(2026, 7, 15, 12, 0)  # noqa: DTZ001
    recent = as_of - datetime.timedelta(days=1)
    data = {
        "closed_ai_signals": [
            _ai_row(1, model="ABR2", direction="LONG", entry=100, close=110, close_time=recent),
        ]
    }
    fetcher = ListFetcher(data)
    src = [SOURCES_BY_NAME["closed_ai_signals"]]
    _make_exporter(tmp_path, fetcher, sources=src).run()

    duckdb_path = _paths(tmp_path)[0]
    client = analytics_api.create_app(duckdb_path).test_client()
    q = f"/api/analytics/success-rate?windows=7&bots=ABR2&as_of={as_of.isoformat()}"

    body1 = client.get(q).get_json()
    assert {e["bot"]: e for e in body1["windows"]["7"]}["ABR2"]["n"] == 1
    # File unchanged → identical cached response.
    assert client.get(q).get_json() == body1

    # A second export appends another decisive ABR2 win (keyset cursor pulls only
    # the new row) → file token advances → cache rebuilds with the new count.
    data["closed_ai_signals"].append(
        _ai_row(2, model="ABR2", direction="LONG", entry=100, close=120, close_time=as_of)
    )
    _make_exporter(tmp_path, fetcher, sources=src).run()

    body2 = client.get(q).get_json()
    assert {e["bot"]: e for e in body2["windows"]["7"]}["ABR2"]["n"] == 2


def test_serve_uses_waitress_with_thread_cap():
    """Prod serving path hands the app to Waitress with the bounded thread pool
    (D-110). serve_fn is injected so no Waitress import is needed here."""
    calls = []

    def fake_serve(app, **kwargs):
        calls.append((app, kwargs))

    sentinel_app = object()
    analytics_api._serve(sentinel_app, host="127.0.0.1", port=8099, serve_fn=fake_serve)

    assert len(calls) == 1
    app_arg, kwargs = calls[0]
    assert app_arg is sentinel_app
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 8099,
        "threads": analytics_api.WAITRESS_THREADS,
    }


def test_serve_dev_uses_flask_run_not_waitress():
    """--dev falls back to the Flask dev server and never touches Waitress."""
    ran = []

    class FakeApp:
        def run(self, **kwargs):
            ran.append(kwargs)

    def fake_serve(*_a, **_k):
        raise AssertionError("dev mode must not invoke Waitress")

    analytics_api._serve(FakeApp(), host="0.0.0.0", port=1234, dev=True, serve_fn=fake_serve)
    assert ran == [{"host": "0.0.0.0", "port": 1234}]

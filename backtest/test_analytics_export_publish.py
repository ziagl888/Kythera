# backtest/test_analytics_export_publish.py
"""DB-free tests for the atomic build-DB → served publish of the Z1 analytics
export (T-2026-CU-9050-163, Teil 5).

The export writes into a persistent BUILD DB (``<served>.build``) and holds the
exclusive DuckDB write lock there; the served file the dashboard reads is only
ever produced by an atomic ``os.replace``. These tests exercise the publish
seam in isolation — real temporary DuckDB files, no Postgres — including the
Windows sharing-violation retry loop (``os.replace`` monkeypatched to raise
``PermissionError`` for the first N attempts).

Run with: py -3.13 -m pytest backtest/test_analytics_export_publish.py -q
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import Any, Callable

import duckdb
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import analytics_export  # noqa: E402
from tools.analytics_export import (  # noqa: E402
    SOURCES_BY_NAME,
    AnalyticsExporter,
    build_db_path,
    connect,
    publish_duckdb,
    read_cursor,
    seed_build_db,
)

_REAL_REPLACE = os.replace  # captured before any monkeypatch


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_duckdb(path: str, *, n_rows: int) -> None:
    """Materialise a standalone DuckDB file with a single table ``t`` of
    ``n_rows`` integer rows — a compact, queryable stand-in for a built DB."""
    con = duckdb.connect(path)
    try:
        con.execute("CREATE TABLE t (i INTEGER)")
        con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n_rows)])
    finally:
        con.close()


def _row_count(path: str) -> int:
    con = duckdb.connect(path, read_only=True)
    try:
        return int(con.execute("SELECT COUNT(*) FROM t").fetchone()[0])
    finally:
        con.close()


class _FlakyReplace:
    """os.replace stand-in that raises PermissionError the first ``fail_times``
    calls, then delegates to the real replace. Records the total call count."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, src, dst):  # noqa: ANN001
        self.calls += 1
        if self.calls <= self.fail_times:
            raise PermissionError(32, "The process cannot access the file")
        return _REAL_REPLACE(src, dst)


# ─────────────────────────────────────────────────────────────────────────────
# Basic publish — build → served
# ─────────────────────────────────────────────────────────────────────────────


def test_publish_copies_build_to_served(tmp_path):
    served = str(tmp_path / "analytics.duckdb")
    build = str(tmp_path / "analytics.duckdb.build")
    _make_duckdb(build, n_rows=7)

    publish_duckdb(build, served)

    # Served now carries the build's data, under the unchanged served name.
    assert os.path.exists(served)
    assert _row_count(served) == 7
    # Build DB is preserved (incrementality resumes from it next run).
    assert os.path.exists(build)
    assert _row_count(build) == 7
    # The .tmp scratch file is consumed by the successful replace.
    assert not os.path.exists(served + ".tmp")


def test_publish_bootstrap_creates_served_when_absent(tmp_path):
    """First run: no served DB exists yet → publish creates it from the build."""
    served = str(tmp_path / "analytics.duckdb")
    build = str(tmp_path / "analytics.duckdb.build")
    _make_duckdb(build, n_rows=3)
    assert not os.path.exists(served)

    publish_duckdb(build, served)

    assert os.path.exists(served)
    assert _row_count(served) == 3


def test_publish_overwrites_stale_served(tmp_path):
    served = str(tmp_path / "analytics.duckdb")
    build = str(tmp_path / "analytics.duckdb.build")
    _make_duckdb(served, n_rows=1)  # old published copy
    _make_duckdb(build, n_rows=9)   # freshly built

    publish_duckdb(build, served)

    assert _row_count(served) == 9  # replaced, not appended


def test_build_db_path_naming():
    assert build_db_path("staging_models/analytics/analytics.duckdb") == analytics_export.Path(
        "staging_models/analytics/analytics.duckdb.build"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Retry-on-lock — os.replace raises PermissionError, then succeeds
# ─────────────────────────────────────────────────────────────────────────────


def test_publish_retries_then_succeeds(tmp_path, monkeypatch):
    served = str(tmp_path / "analytics.duckdb")
    build = str(tmp_path / "analytics.duckdb.build")
    _make_duckdb(build, n_rows=5)

    flaky = _FlakyReplace(fail_times=3)
    monkeypatch.setattr(analytics_export.os, "replace", flaky)
    sleeps: list[float] = []
    monkeypatch.setattr(analytics_export.time, "sleep", lambda s: sleeps.append(s))

    publish_duckdb(build, served, retries=5, retry_delay_s=0.2)

    # 3 failures + 1 success = 4 calls; 3 back-offs slept.
    assert flaky.calls == 4
    assert sleeps == [0.2, 0.2, 0.2]
    assert _row_count(served) == 5
    assert not os.path.exists(served + ".tmp")


def test_publish_succeeds_on_last_allowed_attempt(tmp_path, monkeypatch):
    """Exactly retries-1 failures then success on the final attempt."""
    served = str(tmp_path / "analytics.duckdb")
    build = str(tmp_path / "analytics.duckdb.build")
    _make_duckdb(build, n_rows=2)

    flaky = _FlakyReplace(fail_times=4)  # succeed on attempt 5 (== retries)
    monkeypatch.setattr(analytics_export.os, "replace", flaky)
    monkeypatch.setattr(analytics_export.time, "sleep", lambda _s: None)

    publish_duckdb(build, served, retries=5, retry_delay_s=0.01)

    assert flaky.calls == 5
    assert _row_count(served) == 2


# ─────────────────────────────────────────────────────────────────────────────
# All retries fail — served untouched, build intact, error signalled
# ─────────────────────────────────────────────────────────────────────────────


def test_publish_all_retries_fail_leaves_served_untouched(tmp_path, monkeypatch):
    served = str(tmp_path / "analytics.duckdb")
    build = str(tmp_path / "analytics.duckdb.build")
    _make_duckdb(served, n_rows=1)   # existing published copy (old data)
    _make_duckdb(build, n_rows=8)    # new build that will FAIL to publish

    always_fail = _FlakyReplace(fail_times=999)
    monkeypatch.setattr(analytics_export.os, "replace", always_fail)
    monkeypatch.setattr(analytics_export.time, "sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        publish_duckdb(build, served, retries=5, retry_delay_s=0.01)

    assert always_fail.calls == 5  # exactly `retries` attempts, no more
    # Served keeps its OLD data — never corrupted by a partial publish.
    assert _row_count(served) == 1
    # Build DB stays intact so the next run can republish.
    assert _row_count(build) == 8
    # The .tmp copy is deliberately NOT deleted on failure (data-safe residue).
    assert os.path.exists(served + ".tmp")


def test_publish_all_retries_fail_bootstrap_leaves_no_served(tmp_path, monkeypatch):
    """Failure before the very first publish must not leave a phantom served
    file — only the build DB + the .tmp copy remain."""
    served = str(tmp_path / "analytics.duckdb")
    build = str(tmp_path / "analytics.duckdb.build")
    _make_duckdb(build, n_rows=4)

    always_fail = _FlakyReplace(fail_times=999)
    monkeypatch.setattr(analytics_export.os, "replace", always_fail)
    monkeypatch.setattr(analytics_export.time, "sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        publish_duckdb(build, served, retries=2, retry_delay_s=0.01)

    assert always_fail.calls == 2
    assert not os.path.exists(served)
    assert _row_count(build) == 4
    assert os.path.exists(served + ".tmp")


# ─────────────────────────────────────────────────────────────────────────────
# Defensive: build == served must never self-replace (data-loss guard)
# ─────────────────────────────────────────────────────────────────────────────


def test_publish_build_equals_served_is_noop(tmp_path, monkeypatch):
    path = str(tmp_path / "analytics.duckdb")
    _make_duckdb(path, n_rows=6)

    def _boom(_src, _dst):
        raise AssertionError("os.replace must not run when build == served")

    monkeypatch.setattr(analytics_export.os, "replace", _boom)

    publish_duckdb(path, path)  # must be a silent no-op, no replace, no tmp

    assert _row_count(path) == 6
    assert not os.path.exists(path + ".tmp")


# ─────────────────────────────────────────────────────────────────────────────
# Integration flavour — real exporter builds, then publish serves a queryable DB
# ─────────────────────────────────────────────────────────────────────────────


class _ListFetcher:
    def __init__(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self.data = data

    def fetch(self, spec, cursor, limit):  # noqa: ANN001
        rows = list(self.data.get(spec.name, []))
        rows.sort(key=lambda r: (r[spec.ts_col], r[spec.pk_col]))
        if cursor is not None:
            rows = [r for r in rows if (r[spec.ts_col], r[spec.pk_col]) > cursor]
        return rows[:limit]


def _regime_row(i: int, ts: datetime.datetime) -> dict[str, Any]:
    return {
        "id": i, "ts": ts, "regime": "TREND_UP", "alt_context": "ALT_STRONG",
        "btc_price": 60000.0 + i, "confidence": 0.8, "confidence_btc": 0.8, "confidence_alt": 0.7,
    }


def _fixed_clock(dt: datetime.datetime) -> Callable[[], datetime.datetime]:
    return lambda: dt


def test_exporter_build_then_publish_serves_queryable_duckdb(tmp_path):
    """End-to-end: AnalyticsExporter writes the BUILD DB, publish serves it,
    and the served file is a standalone queryable DuckDB with the exported rows.
    Proves the export never has to open the served path RW."""
    base = datetime.datetime(2026, 7, 15, 9, 0, 0)  # noqa: DTZ001
    served = str(tmp_path / "analytics.duckdb")
    build = build_db_path(served)
    parquet_root = str(tmp_path / "parquet")

    fetcher = _ListFetcher(
        {"regime_history": [_regime_row(1, base), _regime_row(2, base + datetime.timedelta(minutes=5))]}
    )
    AnalyticsExporter(
        build,
        parquet_root,
        fetcher,
        sources=[SOURCES_BY_NAME["regime_history"]],
        clock=_fixed_clock(datetime.datetime(2026, 7, 15, 9, 30, tzinfo=datetime.timezone.utc)),
    ).run()

    # Only the build DB exists so far; the served path is untouched until publish.
    assert os.path.exists(build)
    assert not os.path.exists(served)

    publish_duckdb(build, served)

    con = duckdb.connect(served, read_only=True)
    try:
        n = con.execute('SELECT COUNT(*) FROM "regime_history"').fetchone()[0]
    finally:
        con.close()
    assert n == 2


# ─────────────────────────────────────────────────────────────────────────────
# Rollout seed — migration from the old single-file layout preserves watermark
# (FIX 1 guard: without the seed the first run under the build-DB layout would
#  find no watermark and re-pull the ENTIRE history from live Postgres)
# ─────────────────────────────────────────────────────────────────────────────


def _served_with_watermark(tmp_path, *, rows: list[dict[str, Any]]) -> str:
    """Simulate the OLD single-file layout: a served DuckDB that already carries
    a full ``_export_watermark``, produced by an export straight onto the served
    path (exactly what exists in production before this change ships)."""
    served = str(tmp_path / "analytics.duckdb")
    AnalyticsExporter(
        served,
        str(tmp_path / "parquet"),
        _ListFetcher({"regime_history": rows}),
        sources=[SOURCES_BY_NAME["regime_history"]],
    ).run()
    return served


def test_seed_build_db_migrates_watermark_from_served(tmp_path):
    base = datetime.datetime(2026, 7, 15, 9, 0, 0)  # noqa: DTZ001
    rows = [_regime_row(i, base + datetime.timedelta(minutes=5 * i)) for i in range(1, 4)]
    served = _served_with_watermark(tmp_path, rows=rows)
    build = str(build_db_path(served))
    assert not os.path.exists(build)

    assert seed_build_db(build, served) is True
    assert os.path.exists(build)

    # The seeded build DB carries the served DB's cursor — NOT reset to None.
    spec = SOURCES_BY_NAME["regime_history"]
    con_served = connect(served)
    con_build = connect(build)
    try:
        served_cursor = read_cursor(con_served, spec)
        build_cursor = read_cursor(con_build, spec)
    finally:
        con_served.close()
        con_build.close()
    assert build_cursor is not None
    assert build_cursor == served_cursor


def test_seed_then_export_does_not_re_pull_history(tmp_path):
    """The migration guard's payoff: after seeding, a run against the SAME source
    data pulls ZERO rows (incremental resume) instead of re-exporting the whole
    history as it would from an empty build DB."""
    base = datetime.datetime(2026, 7, 15, 9, 0, 0)  # noqa: DTZ001
    rows = [_regime_row(i, base + datetime.timedelta(minutes=5 * i)) for i in range(1, 6)]
    served = _served_with_watermark(tmp_path, rows=rows)
    build = str(build_db_path(served))

    assert seed_build_db(build, served) is True
    results = AnalyticsExporter(
        build,
        str(tmp_path / "parquet"),
        _ListFetcher({"regime_history": rows}),
        sources=[SOURCES_BY_NAME["regime_history"]],
    ).run()
    assert results[0].rows_exported == 0   # nothing re-pulled from Postgres
    assert results[0].rows_total == 5      # full history present via the seed


def test_seed_noop_when_build_exists(tmp_path):
    """Steady state: an existing build DB is never overwritten by the seed."""
    served = str(tmp_path / "analytics.duckdb")
    build = str(build_db_path(served))
    _make_duckdb(served, n_rows=1)
    _make_duckdb(build, n_rows=99)
    assert seed_build_db(build, served) is False
    assert _row_count(build) == 99


def test_seed_noop_when_both_missing(tmp_path):
    """Genuine first-ever run: no served DB either → no seed → the export then
    does a full export into a fresh empty build DB (intended)."""
    served = str(tmp_path / "analytics.duckdb")
    build = str(build_db_path(served))
    assert seed_build_db(build, served) is False
    assert not os.path.exists(build)


def test_seed_noop_when_build_equals_served(tmp_path):
    """Defensive: build resolving to served must never self-copy."""
    path = str(tmp_path / "analytics.duckdb")
    _make_duckdb(path, n_rows=2)
    assert seed_build_db(path, path) is False
    assert _row_count(path) == 2

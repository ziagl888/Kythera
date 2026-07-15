# tools/analytics_export.py — incremental DuckDB/Parquet analytics export (Z1, T-2026-CU-9050-131)
"""Incremental, watermark-driven export of the closed trading history into a
columnar analytics substrate (Parquet + one DuckDB file).

WHY THIS EXISTS
    The Z1 dashboard (Ideation-Council T-2026-CU-9050-129, curation
    mcp-a868a761829e) must NEVER run analytics queries against the live
    Postgres — they compete with ingestion/WAL on an already-loaded VPS
    (Gutachten-Option A). This job is the sole analytics data path: a Task
    Scheduler job (NOT a bot process; the watchdog stays the process owner)
    pulls only *closed* rows that appeared since the last run and materialises
    them into DuckDB tables + date-partitioned Parquet. The dashboard reads
    ONLY the DuckDB file.

DATA SOURCES (four, each with its own watermark)
    closed_trades_master   strategy trades      watermark = posted     (close ts)
    closed_ai_signals      AI/ML + ROM1 signals watermark = close_time  (close ts)
    ml_predictions_master  shadow predictions   watermark = time        (append log)
    regime_history         ROM1 regime classif. watermark = ts          (append log)

    Candles are deliberately OUT of scope for this task (they are the 25 GB
    elephant; only the 5m base TF is planned, and later).

INCREMENTAL CURSOR
    Each source is paged by a keyset cursor ``(ts_col, id)`` with a STRICT
    ``>`` comparison, persisted in the DuckDB ``_export_watermark`` table. A
    strict keyset on a unique ``id`` tiebreaker neither skips same-timestamp
    ties nor re-exports rows, so no import-time dedup is needed. Natural-key
    duplicates that some tables carry (closed_ai_signals dedupes on
    ``symbol,model,direction,open_time`` in report 14) are exported faithfully
    and deduped at QUERY time by the reader, not here — this stays a truthful
    mirror.

TIMEZONE (R3 minefield — see core/time.py, docs/UTC_POLICY.md)
    The legacy source columns are ``TIMESTAMP WITHOUT TIME ZONE`` and read back
    naive (wall clock ``Europe/Bucharest`` for the older writers). This export
    carries every row timestamp through VERBATIM — it never reinterprets a
    naive value as UTC. The watermark comparison stays entirely within that
    same naive space, so incrementality is correct regardless of the pending
    UTC flip. The only UTC value produced here is ``synced_at`` (the run's
    wall clock), which the freshness record exposes separately and labels as
    UTC. Do not compute a data-age by mixing ``synced_at`` with a naive
    ``last_row_ts`` without accounting for ``ts_is_naive_local``.

DB-FREE BY DESIGN
    The only Postgres boundary is the ``RowFetcher`` seam. The build machine
    has no DB credentials, so all tests inject a synthetic fetcher (see
    backtest/test_analytics_export.py); the real run uses ``PostgresFetcher``
    in a VPS session. Everything else — batching, watermark advance, Parquet
    partitioning, freshness — is pure DuckDB and fully testable offline.

Invariants:
    * A row is exported at most once: the persisted cursor is strictly
      monotonic in ``(ts_col, id)`` and only ever advances after the batch
      that produced it has durably committed.
    * DuckDB table content and Parquet partitions agree after every run for
      the date ranges touched (partitions are re-materialised from the table).
    * ``synced_at`` is UTC wall clock; every source timestamp is stored
      verbatim (naive-local for the legacy tables) and never rebased.

PERFORMANCE RISK (documented, not yet optimised)
    The keyset order ``(ts_col, id)`` wants a composite index on each source
    table; several of these tables lack usable indexes today
    (audit_reports/18). The first full export therefore does full scans and is
    a one-time, off-peak, ``statement_timeout``-capped cost (Gutachten). Steady
    state pulls only the tail and is cheap. Add ``(<ts_col>, id)`` indexes on
    the VPS before enabling a tight (<= 60 s) statement timeout.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import logging
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

import duckdb

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Source specifications
# ─────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Column:
    """One exported column: the Postgres name and its DuckDB storage type."""

    name: str
    duck_type: str


@dataclasses.dataclass(frozen=True)
class SourceSpec:
    """Declarative description of one export source.

    ``name`` is the logical identifier used for the DuckDB table and the
    Parquet subdirectory. ``ts_col`` is the watermark timestamp column and
    ``pk_col`` the unique tiebreaker for the keyset cursor — BOTH must appear
    in ``columns``. ``closed_filter`` is an optional extra SQL predicate that
    restricts the pull to genuinely closed / valid rows.
    """

    name: str
    pg_table: str
    ts_col: str
    pk_col: str
    columns: tuple[Column, ...]
    closed_filter: str | None = None
    # True for the legacy ``TIMESTAMP WITHOUT TIME ZONE`` columns whose naive
    # wall clock is Europe/Bucharest, not UTC (carried through verbatim).
    ts_is_naive_local: bool = True

    def col_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def duck_schema(self) -> str:
        return ", ".join(f'"{c.name}" {c.duck_type}' for c in self.columns)


# The four Z1 Stufe-1 sources. Column lists are the best-known live schema
# (5_trade_monitor.create_closed_trades_table, 8_ai_trade_monitor inserts,
# core/signal_post.log_prediction, 26_regime_detector.regime_history) — VERIFY
# on the VPS before the first real run; a missing/renamed column surfaces as a
# clear SELECT error and is a one-line spec fix.
SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        name="closed_trades",
        pg_table="closed_trades_master",
        ts_col="posted",
        pk_col="id",
        columns=(
            Column("id", "BIGINT"),
            Column("strategy", "VARCHAR"),
            Column("coin", "VARCHAR"),
            Column("direction", "VARCHAR"),
            Column("lev", "VARCHAR"),
            Column("entry", "DOUBLE"),
            Column("close_price", "DOUBLE"),
            Column("time", "TIMESTAMP"),
            Column("posted", "TIMESTAMP"),
            Column("status", "VARCHAR"),
        ),
        # closed_trades_master rows are inserted only on close, atomically with
        # the active-row DELETE (5_trade_monitor.close_trade); posted IS NOT
        # NULL is both the "closed" guard and the watermark's not-null contract.
        closed_filter="posted IS NOT NULL",
    ),
    SourceSpec(
        name="closed_ai_signals",
        pg_table="closed_ai_signals",
        ts_col="close_time",
        # pk_col='id' assumed a serial PK, matching closed_trades_master /
        # regime_history / ml_predictions_master. If closed_ai_signals turns
        # out to lack an id column on the VPS, add one (or switch pk_col to a
        # unique surrogate) — the natural report-14 key
        # (symbol,model,direction,open_time) is NOT unique here.
        pk_col="id",
        columns=(
            Column("id", "BIGINT"),
            Column("symbol", "VARCHAR"),
            Column("model", "VARCHAR"),
            Column("direction", "VARCHAR"),
            Column("entry", "DOUBLE"),
            Column("close_price", "DOUBLE"),
            Column("targets_hit", "INTEGER"),
            Column("open_time", "TIMESTAMP"),
            Column("close_time", "TIMESTAMP"),
            Column("status", "VARCHAR"),
            Column("lev", "VARCHAR"),
        ),
        # Only filled, closed signals. ENTRY_NOT_FILLED are phantom entries the
        # report also excludes (23_market_tracker query_cls_ai).
        closed_filter="close_time IS NOT NULL AND status IS DISTINCT FROM 'ENTRY_NOT_FILLED'",
    ),
    SourceSpec(
        name="ml_predictions",
        pg_table="ml_predictions_master",
        ts_col="time",
        pk_col="id",
        columns=(
            Column("id", "BIGINT"),
            Column("model_name", "VARCHAR"),
            Column("coin", "VARCHAR"),
            Column("direction", "VARCHAR"),
            Column("entry", "DOUBLE"),
            Column("confidence", "DOUBLE"),
            Column("posted", "BOOLEAN"),
            Column("time", "TIMESTAMP"),
        ),
        # Shadow prediction log: append-only, id stable (never back-filled,
        # 15_ai_master_bot). No "closed" state — every row is final on insert.
        closed_filter=None,
    ),
    SourceSpec(
        name="regime_history",
        pg_table="regime_history",
        ts_col="ts",
        pk_col="id",
        columns=(
            Column("id", "BIGINT"),
            Column("ts", "TIMESTAMP"),
            Column("regime", "VARCHAR"),
            Column("alt_context", "VARCHAR"),
            Column("btc_price", "DOUBLE"),
            Column("confidence", "DOUBLE"),
            Column("confidence_btc", "DOUBLE"),
            Column("confidence_alt", "DOUBLE"),
        ),
        # Written only from closed candles (26_regime_detector), ts UNIQUE,
        # append-only.
        closed_filter=None,
    ),
)

SOURCES_BY_NAME = {s.name: s for s in SOURCES}

# DuckDB meta tables.
META_WATERMARK = "_export_watermark"
META_FRESHNESS = "_data_freshness"

# Row cursor: (ts value, pk value). None = start from the beginning of history.
Cursor = tuple[Any, Any]


# ─────────────────────────────────────────────────────────────────────────────
# Fetcher seam — the ONLY Postgres boundary
# ─────────────────────────────────────────────────────────────────────────────


class RowFetcher(Protocol):
    """Pulls the next page of a source strictly after ``cursor``.

    Implementations MUST return at most ``limit`` rows, each a dict keyed by
    ``spec.col_names()``, ordered ascending by ``(spec.ts_col, spec.pk_col)``
    and strictly greater than ``cursor`` (``cursor is None`` = from the start).
    Returning fewer than ``limit`` rows signals the source is exhausted.
    """

    def fetch(self, spec: SourceSpec, cursor: Cursor | None, limit: int) -> list[dict[str, Any]]:
        ...


class PostgresFetcher:
    """Live Postgres fetcher (VPS only). ``psycopg2`` is imported lazily so
    this module stays importable — and testable — on the DB-free build machine.

    A per-session ``statement_timeout`` caps every batch query server-side so a
    heavy first export cannot stall ingestion indefinitely (CPU-blip guard).
    """

    def __init__(self, dsn: str | None = None, statement_timeout_ms: int = 60_000) -> None:
        self._dsn = dsn
        self._statement_timeout_ms = statement_timeout_ms
        self._conn: Any = None

    def _connection(self) -> Any:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        import psycopg2  # lazy: absent from the export's import-time contract

        if self._dsn:
            self._conn = psycopg2.connect(self._dsn)
        else:
            from core.config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER

            self._conn = psycopg2.connect(
                dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD, host=DB_HOST, port=DB_PORT
            )
        self._conn.set_session(readonly=True, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute("SET statement_timeout = %s", (self._statement_timeout_ms,))
        return self._conn

    def fetch(self, spec: SourceSpec, cursor: Cursor | None, limit: int) -> list[dict[str, Any]]:
        cols = ", ".join(f'"{c}"' for c in spec.col_names())
        where: list[str] = []
        params: list[Any] = []
        if spec.closed_filter:
            where.append(f"({spec.closed_filter})")
        if cursor is not None:
            # Row-value keyset: strictly after (ts, pk). NULLs already excluded
            # for the closed sources; the append-only sources never null ts.
            where.append(f'("{spec.ts_col}", "{spec.pk_col}") > (%s, %s)')
            params.extend([cursor[0], cursor[1]])
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (
            f'SELECT {cols} FROM "{spec.pg_table}" {where_sql} '
            f'ORDER BY "{spec.ts_col}" ASC, "{spec.pk_col}" ASC LIMIT %s'
        )
        params.append(limit)
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# DuckDB substrate
# ─────────────────────────────────────────────────────────────────────────────


def connect(duckdb_path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the analytics DuckDB file."""
    path = Path(duckdb_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def ensure_schema(con: duckdb.DuckDBPyConnection, sources: Sequence[SourceSpec]) -> None:
    """Idempotently create the per-source tables and the two meta tables."""
    for spec in sources:
        con.execute(f'CREATE TABLE IF NOT EXISTS "{spec.name}" ({spec.duck_schema()})')
    con.execute(
        f'CREATE TABLE IF NOT EXISTS "{META_WATERMARK}" ('
        "source VARCHAR PRIMARY KEY, last_ts TIMESTAMP, last_pk BIGINT, "
        "rows_total BIGINT, updated_at TIMESTAMP)"
    )
    con.execute(
        f'CREATE TABLE IF NOT EXISTS "{META_FRESHNESS}" ('
        "source VARCHAR PRIMARY KEY, last_row_ts TIMESTAMP, last_pk BIGINT, "
        "ts_is_naive_local BOOLEAN, rows_total BIGINT, last_run_rows BIGINT, "
        "synced_at TIMESTAMP)"
    )


def read_cursor(con: duckdb.DuckDBPyConnection, spec: SourceSpec) -> Cursor | None:
    """Return the persisted keyset cursor for ``spec``, or None if never run."""
    row = con.execute(
        f'SELECT last_ts, last_pk FROM "{META_WATERMARK}" WHERE source = ?', [spec.name]
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return (row[0], row[1])


def _rows_total(con: duckdb.DuckDBPyConnection, spec: SourceSpec) -> int:
    row = con.execute(f'SELECT COUNT(*) FROM "{spec.name}"').fetchone()
    return int(row[0]) if row is not None else 0


def _insert_batch(
    con: duckdb.DuckDBPyConnection, spec: SourceSpec, rows: list[dict[str, Any]]
) -> set[datetime.date]:
    """Append one batch to the DuckDB table. Returns the set of ts dates touched
    (for the follow-up Parquet partition rewrite)."""
    names = spec.col_names()
    placeholders = ", ".join("?" for _ in names)
    col_sql = ", ".join(f'"{n}"' for n in names)
    payload = [tuple(r.get(n) for n in names) for r in rows]
    con.executemany(
        f'INSERT INTO "{spec.name}" ({col_sql}) VALUES ({placeholders})', payload
    )
    touched: set[datetime.date] = set()
    for r in rows:
        ts = r.get(spec.ts_col)
        if isinstance(ts, datetime.datetime):
            touched.add(ts.date())
        elif isinstance(ts, datetime.date):
            touched.add(ts)
    return touched


def _write_cursor(
    con: duckdb.DuckDBPyConnection,
    spec: SourceSpec,
    cursor: Cursor,
    rows_total: int,
    synced_at: datetime.datetime,
) -> None:
    con.execute(
        f'INSERT OR REPLACE INTO "{META_WATERMARK}" '
        "(source, last_ts, last_pk, rows_total, updated_at) VALUES (?, ?, ?, ?, ?)",
        [spec.name, cursor[0], cursor[1], rows_total, synced_at],
    )


def _rewrite_parquet(
    con: duckdb.DuckDBPyConnection,
    spec: SourceSpec,
    parquet_root: Path,
    dates: set[datetime.date],
) -> None:
    """Rewrite exactly the date partitions that received rows this run.

    Layout: ``<root>/<source>/dt=YYYY-MM-DD/data.parquet``. Each partition is
    fully re-materialised from the DuckDB table slice, so Parquet always mirrors
    DuckDB and the rewrite is idempotent. Incremental watermarking means only
    the recent tail dates are ever touched.

    Consistency note: this rewrite runs AFTER the per-batch commits, so a crash
    between a commit and this call can leave a Parquet partition one run stale
    while DuckDB is current. That is tolerable because the dashboard queries the
    DuckDB file, not Parquet; Parquet is the durable/portable copy. A full
    ``COPY``-from-DuckDB rebuild recovers it (watermark stays put on rerun).
    """
    for d in sorted(dates):
        part_dir = parquet_root / spec.name / f"dt={d.isoformat()}"
        part_dir.mkdir(parents=True, exist_ok=True)
        out = (part_dir / "data.parquet").as_posix()
        con.execute(
            f'COPY (SELECT * FROM "{spec.name}" '
            f"WHERE CAST(\"{spec.ts_col}\" AS DATE) = DATE '{d.isoformat()}') "
            f"TO '{out}' (FORMAT PARQUET)"
        )


def _write_freshness(
    con: duckdb.DuckDBPyConnection,
    spec: SourceSpec,
    cursor: Cursor | None,
    rows_total: int,
    last_run_rows: int,
    synced_at: datetime.datetime,
) -> None:
    last_ts = cursor[0] if cursor is not None else None
    last_pk = cursor[1] if cursor is not None else None
    con.execute(
        f'INSERT OR REPLACE INTO "{META_FRESHNESS}" '
        "(source, last_row_ts, last_pk, ts_is_naive_local, rows_total, "
        "last_run_rows, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [spec.name, last_ts, last_pk, spec.ts_is_naive_local, rows_total, last_run_rows, synced_at],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Exporter
# ─────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class SourceResult:
    """Per-source outcome of one export run — the machine-readable summary."""

    name: str
    rows_exported: int
    last_row_ts: datetime.datetime | None
    last_pk: Any
    rows_total: int


class AnalyticsExporter:
    """Runs the incremental export of every configured source into DuckDB +
    Parquet. Construct with an injected :class:`RowFetcher`; call :meth:`run`.
    """

    def __init__(
        self,
        duckdb_path: str | Path,
        parquet_root: str | Path,
        fetcher: RowFetcher,
        *,
        sources: Sequence[SourceSpec] = SOURCES,
        batch_size: int = 5_000,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.duckdb_path = Path(duckdb_path)
        self.parquet_root = Path(parquet_root)
        self.fetcher = fetcher
        self.sources = tuple(sources)
        self.batch_size = batch_size
        self._clock = clock or (lambda: datetime.datetime.now(datetime.timezone.utc))

    def run(self) -> list[SourceResult]:
        con = connect(self.duckdb_path)
        try:
            ensure_schema(con, self.sources)
            results = [self._export_source(con, spec) for spec in self.sources]
        finally:
            con.close()
        return results

    def _export_source(self, con: duckdb.DuckDBPyConnection, spec: SourceSpec) -> SourceResult:
        cursor = read_cursor(con, spec)
        exported = 0
        touched: set[datetime.date] = set()
        while True:
            rows = self.fetcher.fetch(spec, cursor, self.batch_size)
            if not rows:
                break
            synced_at = self._clock().replace(tzinfo=None)
            # One DuckDB transaction per batch: the rows and the advanced
            # watermark commit together, so a crash mid-source resumes cleanly
            # from the last durable cursor (no gaps, no double-count).
            con.execute("BEGIN TRANSACTION")
            try:
                touched |= _insert_batch(con, spec, rows)
                last = rows[-1]
                cursor = (last[spec.ts_col], last[spec.pk_col])
                _write_cursor(con, spec, cursor, _rows_total(con, spec), synced_at)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            exported += len(rows)
            if len(rows) < self.batch_size:
                break

        rows_total = _rows_total(con, spec)
        if touched:
            _rewrite_parquet(con, spec, self.parquet_root, touched)
        # Freshness is written every run — even a no-new-rows run refreshes
        # synced_at so the panel's "Sync vor X min" stays truthful.
        _write_freshness(con, spec, cursor, rows_total, exported, self._clock().replace(tzinfo=None))
        last_ts = cursor[0] if cursor is not None else None
        last_pk = cursor[1] if cursor is not None else None
        logger.info(
            "export %-18s +%d rows (total %d), watermark=%s/%s",
            spec.name, exported, rows_total, last_ts, last_pk,
        )
        return SourceResult(spec.name, exported, last_ts, last_pk, rows_total)


def data_freshness(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Read the Datenstand table — the first-class freshness output the panel
    indicator consumes ("Stand HH:MM, Sync vor X min"). One row per source."""
    try:
        rows = con.execute(
            f'SELECT source, last_row_ts, last_pk, ts_is_naive_local, rows_total, '
            f'last_run_rows, synced_at FROM "{META_FRESHNESS}" ORDER BY source'
        ).fetchall()
    except duckdb.CatalogException:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "source": r[0],
                "last_row_ts": r[1].isoformat() if r[1] is not None else None,
                "last_pk": r[2],
                "ts_is_naive_local": bool(r[3]),
                "rows_total": r[4],
                "last_run_rows": r[5],
                "synced_at": r[6].isoformat() if r[6] is not None else None,
                "synced_at_tz": "UTC",
            }
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Incremental DuckDB/Parquet analytics export (Z1). Run as a "
        "Task Scheduler job on the VPS — needs DB credentials."
    )
    parser.add_argument("--duckdb", default="staging_models/analytics/analytics.duckdb",
                        help="DuckDB output file")
    parser.add_argument("--parquet-dir", default="staging_models/analytics/parquet",
                        help="Root of the date-partitioned Parquet tree")
    parser.add_argument("--batch-size", type=int, default=5_000, help="Rows per LIMIT batch")
    parser.add_argument("--statement-timeout-ms", type=int, default=60_000,
                        help="Per-session Postgres statement_timeout (CPU-blip guard)")
    parser.add_argument("--dsn", default=None, help="Optional psycopg2 DSN (else core.config env)")
    parser.add_argument("--sources", default=None,
                        help="Comma-separated subset of source names (default: all)")
    parser.add_argument("--json", action="store_true", help="Print the run summary as JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.sources:
        wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in SOURCES_BY_NAME]
        if unknown:
            parser.error(f"unknown source(s): {', '.join(unknown)}")
        sources: Sequence[SourceSpec] = [SOURCES_BY_NAME[s] for s in wanted]
    else:
        sources = SOURCES

    fetcher = PostgresFetcher(dsn=args.dsn, statement_timeout_ms=args.statement_timeout_ms)
    try:
        exporter = AnalyticsExporter(
            args.duckdb, args.parquet_dir, fetcher, sources=sources, batch_size=args.batch_size
        )
        results = exporter.run()
    finally:
        fetcher.close()

    summary = [dataclasses.asdict(r) for r in results]
    for r in summary:
        if isinstance(r.get("last_row_ts"), datetime.datetime):
            r["last_row_ts"] = r["last_row_ts"].isoformat()
    if args.json:
        print(json.dumps(summary, default=str, indent=2))
    else:
        total = sum(r["rows_exported"] for r in summary)
        print(f"Exported {total} new rows across {len(summary)} source(s):")
        for r in summary:
            print(f"  {r['name']:<18} +{r['rows_exported']:<6} (total {r['rows_total']}, "
                  f"watermark {r['last_row_ts']})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

# tools/gate_snapshot.py — DuckDB snapshot container for the funding×liq gate pilot
"""Table specs and DuckDB write/read helpers for the T-2026-KYT-9050-120 pilot.

WHY: the study must not run per-trade queries against live Postgres (Michi,
2026-08-09). One read-only export (tools/gate_snapshot_export.py, VPS session)
materialises everything into a single DuckDB file; the study
(tools/funding_liq_gate_study.py) then runs DB-free on that file.

Deliberately NOT the Z1 AnalyticsExporter (tools/analytics_export.py): its
keyset cursor requires a unique integer ``id`` tiebreaker, which neither
``funding_rates`` (PK symbol,funding_time) nor ``liq_events`` (PK
ts,symbol,side,price) has. A study snapshot re-exports rarely and the tables
are small (funding ≈ 0.7M rows, liq ≤ a few 100k), so a one-shot full pull is
the simpler, correct tool — no watermark state to corrupt.

Timezone contract on read-back: tz-aware columns (funding_time, liq ts) come
back as ``datetime64[ns, UTC]``; naive legacy columns (closed_ai_signals
open/close_time = wall-clock Europe/Bucharest) stay NAIVE ``datetime64[ns]``
and are never reinterpreted here — the study localizes them DST-aware itself.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

#: Logical table name → read-only SELECT run by the exporter (VPS session).
#: closed_ai_signals is deduped at export time on the report-14 natural key
#: (symbol, model, direction, open_time), keeping the lowest id (first-written
#: copy) — the raw table carries ~357k duplicate rows.
TABLES: dict[str, str] = {
    "trades": (
        "SELECT DISTINCT ON (symbol, model, direction, open_time) "
        "id, symbol, model, direction, entry, close_price, open_time, close_time, status "
        "FROM closed_ai_signals "
        "ORDER BY symbol, model, direction, open_time, id"
    ),
    "trailing": (
        "SELECT id, symbol, model, direction, entry, sl, opened_at, closed_at, "
        "close_reason, close_mark_pct "
        "FROM trailing_positions "
        "WHERE posted AND filled_at IS NOT NULL AND closed_at IS NOT NULL "
        "AND close_reason IN ('TRAIL','SL_HIT','TIME_STOP','SOURCE_CLOSED') "
        "AND close_mark_pct IS NOT NULL"
    ),
    "funding": ("SELECT symbol, funding_time, funding_rate FROM funding_rates ORDER BY symbol, funding_time"),
    "liq": ("SELECT ts, symbol, side, price, avg_price, qty, value_usdt FROM liq_events ORDER BY ts"),
}

META_TABLE = "_meta"


def write_snapshot(dfs: dict[str, pd.DataFrame], path: str | Path, created_at_utc: str) -> None:
    """Writes all frames plus a row-count meta table into one DuckDB file.

    ``created_at_utc`` is an ISO string stamped by the caller (scripts stamp
    wall clock; tests stamp a constant).
    """
    import duckdb

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        for name, df in dfs.items():
            con.register("_src", df)
            con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM _src')
            con.unregister("_src")
        meta = pd.DataFrame(
            [{"table_name": name, "rows": len(df), "created_at_utc": created_at_utc} for name, df in dfs.items()]
        )
        con.register("_src", meta)
        con.execute(f'CREATE OR REPLACE TABLE "{META_TABLE}" AS SELECT * FROM _src')
        con.unregister("_src")
    finally:
        con.close()


def read_snapshot(path: str | Path, tables: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Reads tables back, normalizing timestamps per the module contract."""
    import duckdb

    con = duckdb.connect(str(path), read_only=True)
    try:
        present = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
        wanted = tables if tables is not None else sorted(present - {META_TABLE})
        out: dict[str, pd.DataFrame] = {}
        for name in wanted:
            if name not in present:
                raise KeyError(f"snapshot {path} has no table {name!r} (present: {sorted(present)})")
            out[name] = _normalize_times(con.execute(f'SELECT * FROM "{name}"').df())
        return out
    finally:
        con.close()


def _normalize_times(df: pd.DataFrame) -> pd.DataFrame:
    """tz-aware → datetime64[ns, UTC]; naive datetimes → datetime64[ns].

    DuckDB may hand back microsecond precision and/or a non-UTC session zone;
    downstream searchsorted/merge_asof need ns (T-073 epoch trap) and one
    canonical zone.
    """
    for col in df.columns:
        dtype = df[col].dtype
        if isinstance(dtype, pd.DatetimeTZDtype):
            df[col] = df[col].dt.tz_convert("UTC").astype("datetime64[ns, UTC]")
        elif pd.api.types.is_datetime64_any_dtype(dtype):
            df[col] = df[col].astype("datetime64[ns]")
    return df

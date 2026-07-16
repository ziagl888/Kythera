# tools/analytics_api.py — first Z1 analytics endpoint: bot success-rate time series (T-2026-CU-9050-131)
"""Read-only success-rate analytics over the DuckDB substrate built by
``tools/analytics_export.py``.

SCOPE
    The first Z1 dashboard endpoint (curation mcp-a868a761829e): a rolling
    (7/30/90d) win-rate time series per bot, with bot-multiselect. It reads
    ONLY the DuckDB file — NEVER live Postgres. That is the whole point of the
    two-layer data path (Gutachten-Option A): analytics never competes with
    ingestion on the VPS.

    Delivered as a thin Flask blueprint because the backend-framework decision
    (T-2026-CU-9050-130) is still open; ``success_rate_timeseries`` is a pure
    DuckDB query function with no Flask dependency, so it survives a later move
    to FastAPI unchanged.

OUTCOME SEMANTICS (mirrors the realized-PnL report, 23_market_tracker)
    A trade's outcome is PnL-based, not status-based, to bypass the known
    writer bugs (LEGACY TARGET HIT writing targets_hit=0, etc.):
      pnl_pct = direction-adjusted (close-entry)/entry*100, only for entry>0
                and close_price>0.
    A trade is DECISIVE (counts toward the win rate denominator) when its pnl
    is present, not housekeeping (DELISTED/CLEANUP/ORPHAN), and
    ``MICRO_PNL_PCT < |pnl| <= MAX_ABS_PNL_PCT``. A WIN is a decisive trade
    with pnl>0. Win rate = wins / decisive. Neutral trades are excluded from
    both numerator and denominator — winrate over decisive trades only.

Invariants:
    * Reads never touch Postgres — the only data source is the DuckDB file.
    * Win rate is computed over DECISIVE trades only; neutral/ambiguous rows
      never inflate or deflate it.
    * ``as_of`` and all row timestamps live in the same (naive-local) space
      the exporter stored them in — no cross-space comparison.
"""

from __future__ import annotations

import datetime
import os
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

import duckdb

# Outcome thresholds — identical to 23_market_tracker (OUTCOME_MIN/MAX_PNL_PCT).
MICRO_PNL_PCT = 0.1
MAX_ABS_PNL_PCT = 100.0

DEFAULT_WINDOWS = (7, 30, 90)

# Which exported tables contribute outcomes, and how their columns map onto the
# unified (bot, direction, entry, close_price, status, closed_at) shape.
_OUTCOME_TABLES = (
    ("closed_ai_signals", "model", "close_time"),
    ("closed_trades", "strategy", "posted"),
)


def _existing_outcome_tables(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str]]:
    present = {
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    return [t for t in _OUTCOME_TABLES if t[0] in present]


def _outcomes_cte(tables: Sequence[tuple[str, str, str]]) -> str:
    """Build the ``scored`` CTE: unified per-trade outcome flags across tables."""
    unions = " UNION ALL ".join(
        f'SELECT "{bot_col}" AS bot, direction, entry, close_price, status, '
        f'"{ts_col}" AS closed_at FROM "{table}"'
        for table, bot_col, ts_col in tables
    )
    return f"""
WITH outcomes AS (
    {unions}
),
scored AS (
    SELECT
        bot,
        CAST(closed_at AS DATE) AS d,
        closed_at,
        CASE
            WHEN entry > 0 AND close_price > 0 AND upper(direction) IN ('LONG', 'SHORT')
            THEN (CASE WHEN upper(direction) = 'SHORT' THEN -1.0 ELSE 1.0 END)
                 * (close_price - entry) / entry * 100.0
            ELSE NULL
        END AS pnl_pct,
        (upper(coalesce(status, '')) LIKE '%DELISTED%'
         OR upper(coalesce(status, '')) LIKE '%CLEANUP%'
         OR upper(coalesce(status, '')) LIKE '%ORPHAN%') AS is_housekeeping
    FROM outcomes
),
flagged AS (
    SELECT
        bot, d, closed_at, pnl_pct,
        (pnl_pct IS NOT NULL AND NOT is_housekeeping
         AND abs(pnl_pct) > {MICRO_PNL_PCT} AND abs(pnl_pct) <= {MAX_ABS_PNL_PCT}) AS is_decisive,
        (pnl_pct IS NOT NULL AND NOT is_housekeeping
         AND abs(pnl_pct) > {MICRO_PNL_PCT} AND abs(pnl_pct) <= {MAX_ABS_PNL_PCT}
         AND pnl_pct > 0) AS is_win
    FROM scored
)"""


def _winrate(wins: int, n: int) -> float | None:
    return round(wins / n, 6) if n else None


def _bot_filter(bots: Sequence[str] | None, params: list[Any]) -> str:
    if not bots:
        return ""
    params.extend(bots)
    placeholders = ", ".join("?" for _ in bots)
    return f" AND bot IN ({placeholders})"


def available_bots(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Distinct bot tags present in the outcome tables."""
    tables = _existing_outcome_tables(con)
    if not tables:
        return []
    cte = _outcomes_cte(tables)
    rows = con.execute(f"{cte} SELECT DISTINCT bot FROM flagged WHERE bot IS NOT NULL ORDER BY bot").fetchall()
    return [r[0] for r in rows]


def success_rate_timeseries(
    con: duckdb.DuckDBPyConnection,
    *,
    bots: Sequence[str] | None = None,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    as_of: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Rolling success-rate time series per bot, read-only from DuckDB.

    Args:
        con: an open DuckDB connection (typically ``read_only=True``).
        bots: optional bot-multiselect filter; None = all bots.
        windows: trailing-day windows for the rolling win rate (default 7/30/90).
        as_of: anchor for the rolling windows; defaults to the latest closed_at
            in the data (so the windows follow the data, not the wall clock).

    Returns a JSON-serialisable dict:
        {
          "as_of": ISO|null,
          "bots": [...],                       # bots actually present in the result
          "windows": {7: [{bot,n,wins,winrate}], 30: [...], 90: [...]},
          "daily": [{date, bot, n, wins, winrate}],   # decisive-trade daily series
        }
    """
    tables = _existing_outcome_tables(con)
    if not tables:
        return {"as_of": None, "bots": [], "windows": {}, "daily": []}
    cte = _outcomes_cte(tables)

    if as_of is None:
        row = con.execute(f"{cte} SELECT max(closed_at) FROM flagged").fetchone()
        as_of = row[0] if row and row[0] is not None else None
    if as_of is None:
        return {"as_of": None, "bots": [], "windows": {}, "daily": []}

    # Rolling windows.
    windows_out: dict[int, list[dict[str, Any]]] = {}
    result_bots: set[str] = set()
    for w in windows:
        params: list[Any] = [as_of, as_of]
        bot_sql = _bot_filter(bots, params)
        rows = con.execute(
            f"{cte} "
            "SELECT bot, "
            "count(*) FILTER (WHERE is_decisive) AS n, "
            "count(*) FILTER (WHERE is_win) AS wins "
            "FROM flagged "
            f"WHERE closed_at > (CAST(? AS TIMESTAMP) - INTERVAL {int(w)} DAY) "
            "AND closed_at <= CAST(? AS TIMESTAMP)"
            f"{bot_sql} "
            "GROUP BY bot ORDER BY bot",
            params,
        ).fetchall()
        entries = []
        for bot, n, wins in rows:
            n, wins = int(n), int(wins)
            result_bots.add(bot)
            entries.append({"bot": bot, "n": n, "wins": wins, "winrate": _winrate(wins, n)})
        windows_out[w] = entries

    # Daily decisive-trade series (for charting the time comparison).
    params = []
    bot_sql = _bot_filter(bots, params)
    daily_rows = con.execute(
        f"{cte} "
        "SELECT d, bot, "
        "count(*) FILTER (WHERE is_decisive) AS n, "
        "count(*) FILTER (WHERE is_win) AS wins "
        "FROM flagged "
        f"WHERE bot IS NOT NULL{bot_sql} "
        "GROUP BY d, bot HAVING count(*) FILTER (WHERE is_decisive) > 0 "
        "ORDER BY d, bot",
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


# ─────────────────────────────────────────────────────────────────────────────
# Thin Flask blueprint (framework decision T-2026-CU-9050-130 still open)
# ─────────────────────────────────────────────────────────────────────────────


def _parse_windows(raw: str | None) -> Sequence[int]:
    if not raw:
        return DEFAULT_WINDOWS
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(int(part))  # ValueError → 400 via the route handler
    return out or DEFAULT_WINDOWS


def _parse_bots(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    bots = [b.strip() for b in raw.split(",") if b.strip()]
    return bots or None


# ─────────────────────────────────────────────────────────────────────────────
# Serving infrastructure — D-2026-CLD-110 stack auflagen (T-2026-CU-9050-136)
# ─────────────────────────────────────────────────────────────────────────────

# Auflage 2 (Amplituden-Budget): the dashboard shares the VPS with ~25 bots, so
# its DuckDB reads must not grab the whole box. Every read connection is capped
# to a small thread pool and a hard memory ceiling. (The companion
# BELOW_NORMAL_PRIORITY_CLASS spawn flag belongs in the watchdog spawn — the
# Z2/deploy moment — not in this code.)
DUCKDB_THREADS = 2
DUCKDB_MEMORY_LIMIT = "512MB"

# Waitress thread pool for the prod serving path (one kill-fest process under
# the watchdog, D-110). Small on purpose — these are light JSON reads behind a
# 30-60 s poll from 1-2 tabs.
WAITRESS_THREADS = 4


def connect_ro(
    path: str | Path,
    *,
    threads: int = DUCKDB_THREADS,
    memory_limit: str = DUCKDB_MEMORY_LIMIT,
) -> duckdb.DuckDBPyConnection:
    """Open a throttled read-only DuckDB connection (D-110 Auflage 2).

    ``read_only=True`` keeps readers off the writer's toes (single-writer export
    job / many-reader dashboard); the two PRAGMAs bound CPU and RAM so a heavy
    query can never starve the bots sharing the VPS.
    """
    con = duckdb.connect(str(path), read_only=True)
    con.execute(f"PRAGMA threads={int(threads)}")
    con.execute(f"PRAGMA memory_limit='{memory_limit}'")
    return con


def _file_token(path: str) -> tuple[int, int] | None:
    """Cheap, connection-free freshness token for the single-writer DuckDB file.

    The exporter refreshes its ``synced_at`` sequence on every run — including a
    no-new-rows run (see analytics_export ``_write_freshness``) — and that write
    rewrites the file, so the file's ``(mtime_ns, size)`` is the observable
    proxy for the synced_at sequence key: unchanged ⇒ the data cannot have
    changed. Reading it costs one ``stat`` and never opens a connection. Returns
    None if the file is not there yet (nothing to cache against).
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


_MISS = object()


class _PollCache:
    """In-memory, rebuildable response cache for the Stufe-1 polling endpoints.

    Keyed by the request's parameters, invalidated wholesale whenever the DuckDB
    file's freshness token advances (see :func:`_file_token`). On an unchanged
    file a poll is served straight from memory — no DuckDB connection, no
    re-scan of the trade history — which is the whole point of the 30-60 s
    poll + server-cache update channel (D-110). The state is a plain dict, fully
    rebuildable from the file, so the process stays TerminateProcess-safe
    (D-110 Auflage 1): a hard kill loses nothing.
    """

    def __init__(
        self,
        path: str,
        *,
        token: Callable[[str], Any] = _file_token,
        enabled: bool = True,
    ) -> None:
        self._path = path
        self._token_fn = token
        self._enabled = enabled
        self._lock = threading.Lock()
        self._current: Any = _MISS
        self._entries: dict[Any, Any] = {}

    def get(self, key: Any, build: Callable[[], Any]) -> Any:
        """Return the cached payload for ``key``, or ``build()`` it and cache it.

        The whole cache is dropped the moment the file token advances, so a hit
        is always exact for the current data snapshot.
        """
        if not self._enabled:
            return build()
        token = self._token_fn(self._path)
        with self._lock:
            if token != self._current:
                self._entries.clear()
                self._current = token
            hit = self._entries.get(key, _MISS)
        if hit is not _MISS:
            return hit
        # Build outside the lock — it opens its own DuckDB connection and may be
        # slow; two concurrent misses for the same key just build twice
        # (idempotent, read-only) instead of serialising every poll behind one
        # query.
        payload = build()
        with self._lock:
            if token == self._current:  # still the freshness we built against
                self._entries[key] = payload
        return payload


def create_app(duckdb_path: str | Path, *, cache_enabled: bool = True):
    """Flask app exposing the read-only analytics endpoints over ``duckdb_path``.

    Each request opens its own read-only DuckDB connection — the file is a
    single-writer (the export job) / many-reader artifact, so read_only readers
    never block the writer and see a consistent snapshot.
    """
    from flask import Flask, jsonify, request

    app = Flask(__name__)
    path = str(duckdb_path)
    cache = _PollCache(path, enabled=cache_enabled)

    def _ro_con() -> duckdb.DuckDBPyConnection:
        return connect_ro(path)

    @app.get("/api/analytics/success-rate")
    def success_rate():
        try:
            windows = _parse_windows(request.args.get("windows"))
        except ValueError:
            return jsonify({"error": "windows must be comma-separated integers"}), 400
        bots = _parse_bots(request.args.get("bots"))
        as_of_raw = request.args.get("as_of")
        as_of = None
        if as_of_raw:
            try:
                as_of = datetime.datetime.fromisoformat(as_of_raw)
            except ValueError:
                return jsonify({"error": "as_of must be ISO-8601"}), 400
            # Row timestamps are stored naive-local (see analytics_export
            # TIMEZONE note); drop any tz so the window comparison stays within
            # that one space instead of silently mixing naive and aware.
            if as_of.tzinfo is not None:
                as_of = as_of.replace(tzinfo=None)

        key = (
            "success-rate",
            tuple(windows),
            tuple(bots) if bots else None,
            as_of.isoformat() if as_of is not None else None,
        )

        def _build() -> dict[str, Any]:
            con = _ro_con()
            try:
                return success_rate_timeseries(con, bots=bots, windows=windows, as_of=as_of)
            finally:
                con.close()

        return jsonify(cache.get(key, _build))

    @app.get("/api/analytics/bots")
    def bots():
        def _build() -> dict[str, Any]:
            con = _ro_con()
            try:
                return {"bots": available_bots(con)}
            finally:
                con.close()

        return jsonify(cache.get(("bots",), _build))

    @app.get("/api/analytics/freshness")
    def freshness():
        from tools.analytics_export import data_freshness

        def _build() -> dict[str, Any]:
            con = _ro_con()
            try:
                return {"freshness": data_freshness(con)}
            finally:
                con.close()

        return jsonify(cache.get(("freshness",), _build))

    return app


def _serve(
    app: Any,
    *,
    host: str,
    port: int,
    dev: bool = False,
    serve_fn: Callable[..., None] | None = None,
) -> None:
    """Serve ``app``. Prod runs one kill-fest Waitress process under the watchdog
    (D-110); ``--dev`` falls back to the Flask dev server for local smoke only.

    ``serve_fn`` is injectable so the wiring is verifiable without importing
    Waitress on the DB-free build machine.
    """
    if dev:
        app.run(host=host, port=port)
        return
    resolved = serve_fn
    if resolved is None:
        from waitress import serve as waitress_serve

        resolved = waitress_serve
    resolved(app, host=host, port=port, threads=WAITRESS_THREADS)


def main(argv: Sequence[str] | None = None) -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="Serve the Z1 analytics endpoints (read-only DuckDB)")
    parser.add_argument("--duckdb", default="staging_models/analytics/analytics.duckdb")
    parser.add_argument("--host", default="127.0.0.1")  # never bind public — Z2/B4 gates that
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--dev", action="store_true",
                        help="Flask dev server instead of Waitress (local smoke only)")
    args = parser.parse_args(argv)
    _serve(create_app(args.duckdb), host=args.host, port=args.port, dev=args.dev)


if __name__ == "__main__":  # pragma: no cover
    main()

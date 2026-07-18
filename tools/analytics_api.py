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
from collections import deque
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
# Leaderboard + risk metrics (Feature 2, T-2026-CU-9050-154)
# ─────────────────────────────────────────────────────────────────────────────

# Sort keys the leaderboard accepts; anything else falls back to the default.
_LEADERBOARD_SORT_KEYS = ("pnl_sum_pct", "expectancy_pct", "winrate", "n")
DEFAULT_LEADERBOARD_SORT = "pnl_sum_pct"


def bot_trade_rows(
    con: duckdb.DuckDBPyConnection, *, bots: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """Ordered, per-bot DECISIVE trade rows for the leaderboard aggregation.

    Reuses the exact same ``flagged``/``is_decisive`` CTE as
    ``success_rate_timeseries`` — a trade counts here iff it would count there
    (pnl present, not housekeeping DELISTED/CLEANUP/ORPHAN, and
    ``MICRO_PNL_PCT < |pnl| <= MAX_ABS_PNL_PCT``). Neutral/open trades never
    reach this function, so no downstream aggregation step needs to re-filter
    them.

    Rows are ordered ``(bot, closed_at)`` ascending — the order the
    drawdown/loss-streak statistics in :func:`_leaderboard_row` depend on
    (both are path-dependent, not just a groupby aggregate).

    Returns one dict per trade: ``{bot, closed_at, pnl_pct, is_win}``. Empty
    list when no outcome table exists yet (mirrors
    ``success_rate_timeseries``'s empty-substrate degrade).
    """
    tables = _existing_outcome_tables(con)
    if not tables:
        return []
    cte = _outcomes_cte(tables)
    params: list[Any] = []
    bot_sql = _bot_filter(bots, params)
    rows = con.execute(
        f"{cte} SELECT bot, closed_at, pnl_pct, is_win FROM flagged "
        f"WHERE is_decisive AND bot IS NOT NULL{bot_sql} ORDER BY bot, closed_at",
        params,
    ).fetchall()
    return [
        {"bot": bot, "closed_at": closed_at, "pnl_pct": float(pnl_pct), "is_win": bool(is_win)}
        for bot, closed_at, pnl_pct, is_win in rows
    ]


def _max_drawdown_pp(pnl_values: Sequence[float]) -> float:
    """Max-drawdown in absolute %-POINTS under the running peak of the
    additive (Σ %-PnL) equity curve, in the given (close-time-ascending) order.

    Pure stdlib — deliberately NOT a numpy import into this otherwise
    dependency-light module (this file previously depended only on
    ``duckdb``; ``tools/analytics_export.py``'s ``PostgresFetcher`` boundary
    keeps psycopg2 lazy for the same reason). The formula mirrors
    ``tools.wf_significance.max_drawdown_pct`` exactly: ``dd = equity - peak``
    (never normalised to peak height — T-2026-CU-9050-053: on fleet-wide
    multi-coin replays the peak height is itself an artefact of trade
    ordering, so a peak-relative ratio would measure that artefact instead of
    the actual drawdown). Kept as a separate, tiny pure-Python implementation
    here rather than importing that module, to avoid pulling numpy into the
    Z1 dashboard's read path.
    """
    if not pnl_values:
        return 0.0
    equity = 0.0
    peak = float("-inf")
    worst = 0.0
    for p in pnl_values:
        equity += p
        if equity > peak:
            peak = equity
        dd = equity - peak
        if dd < worst:
            worst = dd
    return worst


def _max_consecutive_losses(trades: Sequence[dict[str, Any]]) -> int:
    """Longest run of consecutive non-win decisive trades, in the given order.

    Pure/no I/O — a loss streak is a run-length statistic over ``is_win``,
    identical in spirit to :func:`tools.wf_significance.max_drawdown_pct`'s
    order-dependence but simple enough not to warrant its own reused module.
    """
    best = streak = 0
    for t in trades:
        if t["is_win"]:
            streak = 0
        else:
            streak += 1
            best = max(best, streak)
    return best


def _leaderboard_row(bot: str, trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Pure per-bot leaderboard row from an ordered (closed_at ascending) list
    of decisive trades (``{pnl_pct, is_win}``). No DuckDB, no Flask — testable
    with hand-built dicts.

    ``pnl_sum_pct`` is the realized-PnL headline (additive, matching the
    fleet's existing sum-of-%-PnL convention, e.g. 23_market_tracker /
    wf_significance's ``summarize()``). ``expectancy_pct`` is the average
    PnL per trade. ``max_drawdown_pp`` is the absolute (non-normalised)
    max-drawdown of the cumulative-PnL curve in trade-close order (see
    :func:`_max_drawdown_pp`). ``max_loss_streak`` is the longest
    consecutive-loss run — the second risk metric.
    """
    n = len(trades)
    wins = sum(1 for t in trades if t["is_win"])
    pnl_values = [t["pnl_pct"] for t in trades]
    pnl_sum = sum(pnl_values)
    return {
        "bot": bot,
        "n": n,
        "wins": wins,
        "winrate": _winrate(wins, n),
        "pnl_sum_pct": round(pnl_sum, 4),
        "expectancy_pct": round(pnl_sum / n, 4) if n else None,
        "max_drawdown_pp": round(_max_drawdown_pp(pnl_values), 4),
        "max_loss_streak": _max_consecutive_losses(trades),
    }


def bot_leaderboard(
    con: duckdb.DuckDBPyConnection,
    *,
    bots: Sequence[str] | None = None,
    sort_by: str = DEFAULT_LEADERBOARD_SORT,
) -> dict[str, Any]:
    """Per-bot performance leaderboard with risk metrics, read-only from DuckDB.

    Only bots with at least one DECISIVE trade appear — a bot with only
    neutral/housekeeping rows never produces a phantom ``n=0`` entry, because
    it never reaches the groupby (AK5: excluded upstream in
    :func:`bot_trade_rows`, not filtered back out here).

    Args:
        con: an open DuckDB connection (typically ``read_only=True``).
        bots: optional bot-multiselect filter; None = all bots.
        sort_by: one of ``pnl_sum_pct`` (default), ``expectancy_pct``,
            ``winrate``, ``n``. An unrecognised value silently falls back to
            the default rather than raising — the route layer validates and
            400s on a bad value; this pure function stays permissive so it is
            trivially callable from tests.

    Returns a JSON-serialisable dict:
        {"bots": [{bot, n, wins, winrate, pnl_sum_pct, expectancy_pct,
                    max_drawdown_pp, max_loss_streak}, ...],
         "sort_by": <the key actually used>}
    Rows are sorted by ``sort_by`` descending (best first) — the highest PnL/
    expectancy/winrate/count leads the table.
    """
    key = sort_by if sort_by in _LEADERBOARD_SORT_KEYS else DEFAULT_LEADERBOARD_SORT
    by_bot: dict[str, list[dict[str, Any]]] = {}
    for trade in bot_trade_rows(con, bots=bots):
        by_bot.setdefault(trade["bot"], []).append(trade)
    rows = [_leaderboard_row(bot, trades) for bot, trades in by_bot.items()]
    rows.sort(key=lambda r: (r[key] if r[key] is not None else float("-inf")), reverse=True)
    return {"bots": rows, "sort_by": key}


# ─────────────────────────────────────────────────────────────────────────────
# Rolling success-rate time-comparison series (Feature 3, T-2026-CU-9050-155)
# ─────────────────────────────────────────────────────────────────────────────

# Windows the time-comparison panel switches between. Distinct from
# DEFAULT_WINDOWS (the anchored-snapshot windows success_rate_timeseries
# reports) — this constant governs the ROLLING per-day series instead.
TIMESERIES_WINDOWS = (7, 30, 90)
DEFAULT_TIMESERIES_WINDOW = 30


def _daily_buckets_by_bot(
    trades: Sequence[dict[str, Any]],
) -> dict[str, dict[datetime.date, tuple[int, int]]]:
    """Group decisive trades into per-bot, per-calendar-day (n, wins) buckets.

    Pure aggregation over :func:`bot_trade_rows`'s output — no I/O, no DuckDB.
    ``closed_at`` is bucketed by its DATE component only, matching the
    ``CAST(closed_at AS DATE)`` grouping ``success_rate_timeseries`` already
    uses for its own daily series (same day boundary, no new convention).
    """
    by_bot: dict[str, dict[datetime.date, tuple[int, int]]] = {}
    for t in trades:
        closed_at = t["closed_at"]
        d = closed_at.date() if hasattr(closed_at, "date") else closed_at
        bucket = by_bot.setdefault(t["bot"], {})
        n, wins = bucket.get(d, (0, 0))
        bucket[d] = (n + 1, wins + (1 if t["is_win"] else 0))
    return by_bot


def _rolling_series_for_bot(
    daily: dict[datetime.date, tuple[int, int]], window: int
) -> list[dict[str, Any]]:
    """Trailing ``window``-day rolling (n, wins, winrate) for every day this bot
    has at least one decisive trade, in ascending date order.

    A day ``d``'s rolling value sums every bucket with ``date > d - window`` and
    ``date <= d`` — the same half-open trailing-window convention
    ``success_rate_timeseries`` uses for its anchored snapshot (``closed_at >
    as_of - INTERVAL w DAY AND closed_at <= as_of``), just re-anchored at every
    day instead of once at ``as_of``. Implemented as a sliding window over the
    bot's own sorted day list using a ``deque`` (O(days) total — O(1) eviction
    per stale day) since the days are sparse (only days with a decisive trade
    appear as keys) rather than a dense calendar a fixed-size ring buffer
    could index into directly.
    """
    dates = sorted(daily)
    out: list[dict[str, Any]] = []
    included: deque[tuple[datetime.date, int, int]] = deque()
    running_n = 0
    running_wins = 0
    for d in dates:
        n, wins = daily[d]
        included.append((d, n, wins))
        running_n += n
        running_wins += wins
        cutoff = d - datetime.timedelta(days=window)
        while included and included[0][0] <= cutoff:
            _, old_n, old_wins = included.popleft()
            running_n -= old_n
            running_wins -= old_wins
        out.append(
            {
                "date": d.isoformat(),
                "n": running_n,
                "wins": running_wins,
                "winrate": _winrate(running_wins, running_n),
            }
        )
    return out


def rolling_success_rate_series(
    con: duckdb.DuckDBPyConnection,
    *,
    bots: Sequence[str] | None = None,
    window: int = DEFAULT_TIMESERIES_WINDOW,
) -> dict[str, Any]:
    """Rolling ``window``-day win-rate TIME SERIES per bot — the time-comparison
    chart's data source (Feature 3, T-2026-CU-9050-155).

    Additive to :func:`success_rate_timeseries`, which this function does NOT
    modify or duplicate: it reuses :func:`bot_trade_rows` for the identical
    DECISIVE-trade definition (pnl present, not housekeeping, ``MICRO_PNL_PCT <
    |pnl| <= MAX_ABS_PNL_PCT``) and the identical bot-multiselect filter, so a
    trade counts here iff it counts in the anchored snapshot. What differs is
    the SHAPE: instead of one value anchored at ``as_of``,
    :func:`_rolling_series_for_bot` re-anchors the trailing ``window``-day sum
    at every day that bot has data — this is what makes 7d vs. 30d vs. 90d
    visibly DIVERGE on a line chart (7d is noisy/jumps with each new trade,
    90d is smooth), rather than success_rate_timeseries's single-point
    snapshot which cannot show that divergence over time.

    Args:
        con: an open DuckDB connection (typically ``read_only=True``).
        bots: optional bot-multiselect filter; None = all bots.
        window: trailing-day window for the rolling calculation (7/30/90).

    Returns a JSON-serialisable dict:
        {"window": window, "bots": [...], "series": {bot: [{date,n,wins,winrate}, ...]}}
    Empty when the substrate has no outcome tables yet (mirrors
    ``success_rate_timeseries``'s and ``bot_leaderboard``'s empty-substrate
    degrade — never raises).
    """
    trades = bot_trade_rows(con, bots=bots)
    by_bot = _daily_buckets_by_bot(trades)
    series = {bot: _rolling_series_for_bot(daily, window) for bot, daily in by_bot.items()}
    return {"window": window, "bots": sorted(series), "series": series}


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
    try:
        con.execute(f"PRAGMA threads={int(threads)}")
        con.execute(f"PRAGMA memory_limit='{memory_limit}'")
    except Exception:
        con.close()  # never leak a half-configured connection on the poll path
        raise
    return con


def _stat_token(path: str) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of ``path``, or None if it is absent."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _file_token(path: str) -> tuple[Any, Any] | None:
    """Cheap, connection-free freshness token for the single-writer DuckDB file.

    The exporter refreshes its ``synced_at`` sequence on every run — including a
    no-new-rows run (see analytics_export ``_write_freshness``) — and that write
    rewrites the file, so the file's ``(mtime_ns, size)`` is the observable
    proxy for the synced_at sequence key: unchanged ⇒ the data cannot have
    changed. Reading it costs one ``stat`` and never opens a connection.

    We also fold in the DuckDB WAL sidecar (``<file>.wal``): a committed write
    can land in the WAL and not touch the main file's ``(mtime, size)`` until
    the writer's connection closes and checkpoints. Read-only readers still
    replay that WAL data, so the token must advance on it too — otherwise the
    cache could serve stale rows while a fresh read already reflects the new
    ones. In the steady state (a clean, checkpointed run removes the WAL) the
    sidecar is absent and contributes a constant; the coupling to the exporter's
    checkpoint-on-close is thus defended here rather than assumed. Returns None
    if the main file is not there yet (nothing to cache against).
    """
    main = _stat_token(path)
    if main is None:
        return None
    return (main, _stat_token(path + ".wal"))


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

    # Between two export runs the token is stable, so the cache is not cleared;
    # this bounds how many distinct (windows, bots, as_of) combinations can
    # accumulate in that window. Far above the handful a 1-2-tab dashboard
    # produces, but a hard ceiling against pathological/varied querying.
    MAX_ENTRIES = 256

    def __init__(
        self,
        path: str,
        *,
        token: Callable[[str], Any] = _file_token,
        enabled: bool = True,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self._path = path
        self._token_fn = token
        self._enabled = enabled
        self._max_entries = max_entries
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
                if key not in self._entries and len(self._entries) >= self._max_entries:
                    # FIFO-evict the oldest entry (dicts preserve insertion order).
                    self._entries.pop(next(iter(self._entries)))
                self._entries[key] = payload
        return payload


def build_analytics_blueprint(duckdb_path: str | Path, *, cache_enabled: bool = True):
    """Read-only analytics JSON endpoints as a mountable Flask blueprint.

    Extracted from :func:`create_app` (behaviour-preserving — same URLs, same
    cache) so the Z1 dashboard shell (``tools/dashboard/app.py``,
    T-2026-CU-9050-151) can mount the same endpoints on its own Flask app rather
    than run a second server. The blueprint closes over its own throttled
    connection factory and poll cache, so any app that mounts it stays
    independent.

    Each request opens its own read-only DuckDB connection — the file is a
    single-writer (the export job) / many-reader artifact, so read_only readers
    never block the writer and see a consistent snapshot.
    """
    from flask import Blueprint, jsonify, request

    bp = Blueprint("analytics", __name__)
    path = str(duckdb_path)
    cache = _PollCache(path, enabled=cache_enabled)

    def _ro_con() -> duckdb.DuckDBPyConnection:
        return connect_ro(path)

    @bp.get("/api/analytics/success-rate")
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

        # Sort the multi-value params so logically identical requests
        # (bots=A,B vs B,A; windows in any order) share one cache entry — the
        # payload is order-independent (windows keyed by value, bots is a set
        # filter), so this only removes avoidable misses.
        key = (
            "success-rate",
            tuple(sorted(windows)),
            tuple(sorted(bots)) if bots else None,
            as_of.isoformat() if as_of is not None else None,
        )

        def _build() -> dict[str, Any]:
            con = _ro_con()
            try:
                return success_rate_timeseries(con, bots=bots, windows=windows, as_of=as_of)
            finally:
                con.close()

        return jsonify(cache.get(key, _build))

    @bp.get("/api/analytics/leaderboard")
    def leaderboard():
        bots = _parse_bots(request.args.get("bots"))
        sort_by = request.args.get("sort_by") or DEFAULT_LEADERBOARD_SORT
        if sort_by not in _LEADERBOARD_SORT_KEYS:
            return jsonify({"error": f"sort_by must be one of {', '.join(_LEADERBOARD_SORT_KEYS)}"}), 400

        key = ("leaderboard", tuple(sorted(bots)) if bots else None, sort_by)

        def _build() -> dict[str, Any]:
            con = _ro_con()
            try:
                return bot_leaderboard(con, bots=bots, sort_by=sort_by)
            finally:
                con.close()

        return jsonify(cache.get(key, _build))

    @bp.get("/api/analytics/bots")
    def bots():
        def _build() -> dict[str, Any]:
            con = _ro_con()
            try:
                return {"bots": available_bots(con)}
            finally:
                con.close()

        return jsonify(cache.get(("bots",), _build))

    @bp.get("/api/analytics/freshness")
    def freshness():
        from tools.analytics_export import data_freshness

        def _build() -> dict[str, Any]:
            con = _ro_con()
            try:
                return {"freshness": data_freshness(con)}
            finally:
                con.close()

        return jsonify(cache.get(("freshness",), _build))

    return bp


def create_app(duckdb_path: str | Path, *, cache_enabled: bool = True):
    """Standalone Flask app exposing only the read-only analytics endpoints.

    Thin wrapper over :func:`build_analytics_blueprint` — kept so the analytics
    API can still be served on its own (and so the T-131 tests that hit
    ``/api/analytics/*`` on this app stay valid). The Z1 dashboard shell mounts
    the blueprint on its own app instead of calling this.
    """
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(build_analytics_blueprint(duckdb_path, cache_enabled=cache_enabled))
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

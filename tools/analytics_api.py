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
        for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
    }
    return [t for t in _OUTCOME_TABLES if t[0] in present]


def _outcomes_cte(tables: Sequence[tuple[str, str, str]]) -> str:
    """Build the ``scored`` CTE: unified per-trade outcome flags across tables.

    Besides the outcome columns, every row carries a deterministic tie-breaker
    pair (T-2026-CU-9050-177): ``src`` (the row's UNION-branch rank — needed
    because the two source tables' serial ``id`` ranges overlap, 371k
    collisions on the live export) and ``id`` (each table's monotonically
    increasing Postgres serial PK, i.e. insertion order — the same column the
    exporter's keyset cursor already uses as its uniqueness tie-breaker).
    ``ORDER BY bot, closed_at, src, id`` is therefore a TOTAL order: for
    same-instant closes it replays the order the close-processor wrote the
    rows -- the truest DETERMINISTIC ordering derivable from the exported
    schema. Caveat (T-177 review): this is NOT a guarantee of real
    close-chronology where an upstream writer batch-stamps ``closed_at``. A
    known ~340k-row legacy-reclassified ``closed_ai_signals`` block shares one
    exact timestamp; within such a block ``id`` order is essentially arbitrary
    insertion order, so the resulting ``max_drawdown_pp``/``max_loss_streak``
    are deterministic order-artifacts (now stable + reproducible) rather than
    chronologically-meaningful values (affects ATS1/EPD1/MIS1-pump-family,
    ~85-93% of their trade history). Evaluating ``open_time`` as the tie-break
    for the legacy-status branch is a possible follow-up.
    """
    unions = " UNION ALL ".join(
        f'SELECT "{bot_col}" AS bot, direction, entry, close_price, status, "{ts_col}" AS closed_at, '
        f'{src} AS src, id FROM "{table}"'
        for src, (table, bot_col, ts_col) in enumerate(tables)
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
        src,
        id,
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
        bot, d, closed_at, src, id, pnl_pct,
        (pnl_pct IS NOT NULL AND NOT is_housekeeping
         AND abs(pnl_pct) > {MICRO_PNL_PCT} AND abs(pnl_pct) <= {MAX_ABS_PNL_PCT}) AS is_decisive,
        (pnl_pct IS NOT NULL AND NOT is_housekeeping
         AND abs(pnl_pct) > {MICRO_PNL_PCT} AND abs(pnl_pct) <= {MAX_ABS_PNL_PCT}
         AND pnl_pct > 0) AS is_win
    FROM scored
)"""


def _winrate(wins: int, n: int) -> float | None:
    return round(wins / n, 6) if n else None


def _numpy() -> Any:
    """Lazily-imported numpy, or None when unavailable.

    numpy is already a repo dependency (xgboost/wf_significance), but this
    module deliberately imports it lazily and optionally: the module import
    stays numpy-free (DB-free build machines without numpy keep working), and
    every numpy fast-path below has a pure-Python fallback that produces
    bit-identical results (T-2026-CU-9050-175 parity contract).
    """
    try:
        import numpy
    except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
        return None
    return numpy


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

    # Rolling windows — ONE scan for all of them (T-2026-CU-9050-175). The old
    # shape ran one full CTE scan per window; the merged query computes every
    # window's aggregates as FILTER'd counts over the widest window's rows.
    # Result parity is exact: all aggregates are integer counts, the per-window
    # bot list is reconstructed from ``any_rows`` (a bot appears for window w
    # iff it has >= 1 row — decisive or not — inside w, exactly the row set the
    # old per-window ``WHERE closed_at > as_of - w`` GROUP BY produced), and
    # ``ORDER BY bot`` preserves the old per-window entry order.
    windows_out: dict[int, list[dict[str, Any]]] = {}
    result_bots: set[str] = set()
    uniq_windows = sorted({int(w) for w in windows})
    if uniq_windows:
        max_w = uniq_windows[-1]
        params: list[Any] = []
        window_cols = []
        for w in uniq_windows:
            lower = f"closed_at > (CAST(? AS TIMESTAMP) - INTERVAL {w} DAY)"
            window_cols.append(
                f"count(*) FILTER (WHERE {lower}), "
                f"count(*) FILTER (WHERE is_decisive AND {lower}), "
                f"count(*) FILTER (WHERE is_win AND {lower})"
            )
            params.extend([as_of, as_of, as_of])
        params.extend([as_of, as_of])
        bot_sql = _bot_filter(bots, params)
        rows = con.execute(
            f"{cte} "
            f"SELECT bot, {', '.join(window_cols)} "
            "FROM flagged "
            f"WHERE closed_at > (CAST(? AS TIMESTAMP) - INTERVAL {max_w} DAY) "
            "AND closed_at <= CAST(? AS TIMESTAMP)"
            f"{bot_sql} "
            "GROUP BY bot ORDER BY bot",
            params,
        ).fetchall()
        per_window: dict[int, list[dict[str, Any]]] = {w: [] for w in uniq_windows}
        for row in rows:
            bot = row[0]
            for i, w in enumerate(uniq_windows):
                if int(row[1 + 3 * i]) == 0:
                    continue  # no row at all inside w — old per-window query had no group for this bot
                n, wins = int(row[2 + 3 * i]), int(row[3 + 3 * i])
                result_bots.add(bot)
                per_window[w].append({"bot": bot, "n": n, "wins": wins, "winrate": _winrate(wins, n)})
        for w in windows:
            windows_out[w] = per_window[int(w)]

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


def bot_trade_rows(con: duckdb.DuckDBPyConnection, *, bots: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Ordered, per-bot DECISIVE trade rows for the leaderboard aggregation.

    Reuses the exact same ``flagged``/``is_decisive`` CTE as
    ``success_rate_timeseries`` — a trade counts here iff it would count there
    (pnl present, not housekeeping DELISTED/CLEANUP/ORPHAN, and
    ``MICRO_PNL_PCT < |pnl| <= MAX_ABS_PNL_PCT``). Neutral/open trades never
    reach this function, so no downstream aggregation step needs to re-filter
    them.

    Rows are ordered ``(bot, closed_at, src, id)`` ascending — a TOTAL,
    deterministic order (T-2026-CU-9050-177; see :func:`_outcomes_cte` for the
    tie-breaker rationale). The drawdown/loss-streak statistics in
    :func:`_leaderboard_row` depend on this order (both are path-dependent,
    not just a groupby aggregate), so the tie-breaker is what makes them
    reproducible run-to-run even under DuckDB's parallel scan (threads=2).

    Returns one dict per trade: ``{bot, closed_at, pnl_pct, is_win}``. Empty
    list when no outcome table exists yet (mirrors
    ``success_rate_timeseries``'s empty-substrate degrade).

    Since T-2026-CU-9050-175 the leaderboard/rolling-series hot paths no
    longer flow through this function (they aggregate column-wise /
    DB-side instead of materialising ~580k per-trade dicts); it stays as the
    canonical, readable definition of the decisive-trade row stream and as
    the reference pipeline the parity tests compare those fast paths against.
    """
    tables = _existing_outcome_tables(con)
    if not tables:
        return []
    cte = _outcomes_cte(tables)
    params: list[Any] = []
    bot_sql = _bot_filter(bots, params)
    rows = con.execute(
        f"{cte} SELECT bot, closed_at, pnl_pct, is_win FROM flagged "
        f"WHERE is_decisive AND bot IS NOT NULL{bot_sql} ORDER BY bot, closed_at, src, id",
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
    return _max_loss_streak_from_flags(t["is_win"] for t in trades)


def _max_loss_streak_from_flags(win_flags: Any) -> int:
    """Longest run of consecutive falsy flags — the column-shaped core of
    :func:`_max_consecutive_losses` (which delegates here), shared with the
    streamed leaderboard path so both compute the identical statistic."""
    best = streak = 0
    for win in win_flags:
        if win:
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
    return _leaderboard_row_from_columns(
        bot, [t["pnl_pct"] for t in trades], [t["is_win"] for t in trades]
    )


def _leaderboard_row_from_columns(
    bot: str, pnl_values: Sequence[float], win_flags: Sequence[bool]
) -> dict[str, Any]:
    """Column-shaped core of :func:`_leaderboard_row` (which delegates here).

    Same math on the same values in the same order — ``sum()`` (CPython's
    builtin, Neumaier-compensated since 3.12) for ``pnl_sum_pct``, the naive
    sequential loop of :func:`_max_drawdown_pp` for the drawdown — so the
    dict-shaped and column-shaped callers produce bit-identical rows. Exists
    so the streamed leaderboard path (:func:`_leaderboard_rows_streamed`) can
    aggregate without materialising one dict per trade.
    """
    n = len(pnl_values)
    wins = sum(1 for w in win_flags if w)
    pnl_sum = sum(pnl_values)
    return {
        "bot": bot,
        "n": n,
        "wins": wins,
        "winrate": _winrate(wins, n),
        "pnl_sum_pct": round(pnl_sum, 4),
        "expectancy_pct": round(pnl_sum / n, 4) if n else None,
        "max_drawdown_pp": round(_max_drawdown_pp(pnl_values), 4),
        "max_loss_streak": _max_loss_streak_from_flags(win_flags),
    }


def _leaderboard_rows_streamed(
    con: duckdb.DuckDBPyConnection, *, bots: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """Per-bot leaderboard rows without a per-trade dict (T-2026-CU-9050-175).

    Runs the SAME decisive-row query :func:`bot_trade_rows` runs (identical
    CTE, identical ``WHERE is_decisive AND bot IS NOT NULL``, identical
    ``ORDER BY bot, closed_at, src, id``) but projects only the three columns
    the aggregation consumes — the sort keys stay sort keys without ever being
    materialised as 580k Python datetimes. Aggregation happens in one pass via
    :func:`_leaderboard_row_from_columns`, i.e. literally
    :func:`_leaderboard_row`'s own math, so **for a given row stream** a row
    here is bit-identical to a row the old bot_trade_rows→dict→_leaderboard_row
    pipeline produced (~11.5s → ~4.5s on the live substrate).

    DETERMINISM (T-2026-CU-9050-177, superseding the T-175 caveat): the
    ``ORDER BY bot, closed_at, src, id`` is a TOTAL order (see
    :func:`_outcomes_cte` for the tie-breaker rationale), so the row stream —
    and with it the two ORDER-DEPENDENT risk metrics (``max_drawdown_pp``,
    ``max_loss_streak``) — is reproducible run-to-run, including under
    DuckDB's parallel scan (``connect_ro``'s threads=2). Before the
    tie-breaker, duplicate/same-instant ``closed_at`` rows (which really exist
    in ``closed_ai_signals``) could come back in a different relative order on
    different executions and shift those two path-dependent metrics; the pure
    count/sum fields (n, wins, winrate, pnl_sum_pct, expectancy_pct) were and
    remain order-invariant.

    When numpy is importable (lazy, optional — see :func:`_numpy`) the column
    transfer uses ``fetchnumpy`` and per-bot slicing over run boundaries;
    ``.tolist()`` hands the identical Python floats/bools to the identical
    aggregation functions. Both branches re-execute the query independently,
    but since the query's order is now total, they consume the identical row
    stream — the fast path and the pure fallback therefore produce identical
    rows unconditionally, not just on tie-free data. Masked arrays (NULLs —
    impossible for decisive rows, defensively handled anyway) fall back to the
    pure path rather than risking filled sentinel values.
    """
    tables = _existing_outcome_tables(con)
    if not tables:
        return []
    cte = _outcomes_cte(tables)
    params: list[Any] = []
    bot_sql = _bot_filter(bots, params)
    sql = (
        f"{cte} SELECT bot, pnl_pct, is_win FROM flagged "
        f"WHERE is_decisive AND bot IS NOT NULL{bot_sql} ORDER BY bot, closed_at, src, id"
    )
    np = _numpy()
    if np is not None:
        cols = con.execute(sql, params).fetchnumpy()
        bot_col, pnl_col, win_col = cols["bot"], cols["pnl_pct"], cols["is_win"]
        masked = any(isinstance(a, np.ma.MaskedArray) for a in (bot_col, pnl_col, win_col))
        if not masked:
            n_rows = len(bot_col)
            if n_rows == 0:
                return []
            change = np.empty(n_rows, dtype=bool)
            change[0] = True
            change[1:] = bot_col[1:] != bot_col[:-1]
            starts = np.flatnonzero(change)
            ends = np.append(starts[1:], n_rows)
            return [
                _leaderboard_row_from_columns(
                    str(bot_col[s]), pnl_col[s:e].tolist(), win_col[s:e].tolist()
                )
                for s, e in zip(starts, ends, strict=True)
            ]
    rows = con.execute(sql, params).fetchall()
    by_bot: dict[str, tuple[list[float], list[bool]]] = {}
    for bot, pnl, win in rows:
        acc = by_bot.get(bot)
        if acc is None:
            acc = by_bot[bot] = ([], [])
        acc[0].append(float(pnl))
        acc[1].append(bool(win))
    return [_leaderboard_row_from_columns(bot, pnl, win) for bot, (pnl, win) in by_bot.items()]


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
    rows = _leaderboard_rows_streamed(con, bots=bots)
    rows.sort(key=lambda r: r[key] if r[key] is not None else float("-inf"), reverse=True)
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


def _rolling_series_for_bot(daily: dict[datetime.date, tuple[int, int]], window: int) -> list[dict[str, Any]]:
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

    PERFORMANCE (T-2026-CU-9050-175): the per-day (n, wins) buckets are
    aggregated in DuckDB (``GROUP BY bot, d``) instead of transferring every
    decisive trade row into Python and bucketing there
    (:func:`bot_trade_rows` + :func:`_daily_buckets_by_bot`, the previous
    shape — ~580k rows / ~12s on the live substrate vs ~1.2s now). Result
    parity is exact by construction: the SQL groups by the same ``d =
    CAST(closed_at AS DATE)`` day boundary over the same DECISIVE row set,
    both counts are integers, and ``ORDER BY bot, d`` reproduces the old
    first-occurrence bot insertion order. :func:`_daily_buckets_by_bot`
    remains as the pure-Python reference implementation the parity test
    (backtest/test_analytics_query_parity.py) checks this SQL against.
    """
    tables = _existing_outcome_tables(con)
    if not tables:
        return {"window": window, "bots": [], "series": {}}
    cte = _outcomes_cte(tables)
    params: list[Any] = []
    bot_sql = _bot_filter(bots, params)
    rows = con.execute(
        f"{cte} SELECT bot, d, count(*) AS n, count(*) FILTER (WHERE is_win) AS wins "
        f"FROM flagged WHERE is_decisive AND bot IS NOT NULL{bot_sql} "
        "GROUP BY bot, d ORDER BY bot, d",
        params,
    ).fetchall()
    by_bot: dict[str, dict[datetime.date, tuple[int, int]]] = {}
    for bot, d, n, wins in rows:
        if isinstance(d, datetime.datetime):  # DuckDB returns DATE; guard a datetime anyway
            d = d.date()
        by_bot.setdefault(bot, {})[d] = (int(n), int(wins))
    series = {bot: _rolling_series_for_bot(daily, window) for bot, daily in by_bot.items()}
    return {"window": window, "bots": sorted(series), "series": series}


# ─────────────────────────────────────────────────────────────────────────────
# Bot x Regime performance heatmap (Feature 6, T-2026-CU-9050-158)
# ─────────────────────────────────────────────────────────────────────────────

# Cell metrics the heatmap can render — the SAME two headline metrics
# _leaderboard_row already computes (winrate, expectancy_pct), just per
# (bot, regime) instead of per bot overall.
REGIME_MATRIX_METRICS = ("winrate", "expectancy_pct")


def _regime_history_present(con: duckdb.DuckDBPyConnection) -> bool:
    row = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'main' AND table_name = 'regime_history'"
    ).fetchone()
    return row is not None


def bot_regime_matrix(con: duckdb.DuckDBPyConnection, *, bots: Sequence[str] | None = None) -> dict[str, Any]:
    """Per-(bot, regime) performance matrix — the Bot x Regime heatmap's data
    source (Feature 6, T-2026-CU-9050-158).

    Assigns each DECISIVE trade (the identical definition ``bot_trade_rows``/
    ``success_rate_timeseries`` use, via the shared ``_outcomes_cte``/
    ``_bot_filter`` helpers — pnl present, not housekeeping, ``MICRO_PNL_PCT <
    |pnl| <= MAX_ABS_PNL_PCT``) to the regime state that was ACTIVE at its
    ``closed_at``, via a DuckDB ``ASOF JOIN`` against ``regime_history``
    (``ON closed_at >= ts``): for each trade this picks the LATEST
    ``regime_history`` row whose ``ts`` does not exceed the trade's
    ``closed_at`` — i.e. the regime classification in force at that instant.
    ``regime_history`` is an append-only classification log (ROM1,
    ``tools/analytics_export.py`` SOURCES), so a row's regime is valid from its
    own ``ts`` up to (not including) the next row's ``ts`` — the ASOF join
    encodes exactly that half-open window without materialising it.

    A trade whose ``closed_at`` precedes every recorded ``regime_history`` row
    (no regime had been classified yet at that point) has no ASOF match and is
    DROPPED from the matrix — never fabricated into an "UNKNOWN" bucket
    (CLAUDE.md rule: no synthesised data).

    Args:
        con: an open DuckDB connection (typically ``read_only=True``).
        bots: optional bot-multiselect filter; None = all bots.

    Returns a JSON-serialisable dict:
        {"bots": [...], "regimes": [...],
         "cells": {bot: {regime: {n, wins, winrate, pnl_sum_pct, expectancy_pct}}}}
    A (bot, regime) pair with zero decisive trades in that window is simply
    ABSENT from ``cells[bot]`` — never a zero-filled placeholder. Empty
    (``{"bots": [], "regimes": [], "cells": {}}``) when either no outcome
    table or no ``regime_history`` table exists yet in the substrate (mirrors
    the empty-substrate degrade of ``success_rate_timeseries``/
    ``bot_leaderboard``).
    """
    tables = _existing_outcome_tables(con)
    if not tables or not _regime_history_present(con):
        return {"bots": [], "regimes": [], "cells": {}}
    cte = _outcomes_cte(tables)
    params: list[Any] = []
    bot_sql = _bot_filter(bots, params)
    # ASOF **inner** join (T-2026-CU-9050-175): the old shape was ``ASOF LEFT
    # JOIN … WHERE r.regime IS NOT NULL``. ``regime_sorted`` filters NULL
    # regimes out up front, so the only NULL ``r.regime`` a LEFT join can emit
    # is the null-extension of a non-matching trade — exactly the rows the
    # WHERE clause then discarded. The inner join drops them without the
    # null-extend + refilter detour (~2.5s → ~2.0s on the live substrate),
    # provably row-identical. The inner ``ORDER BY ts`` is gone too — the ASOF
    # operator orders its build side itself; the clause only added a sort.
    rows = con.execute(
        f"{cte}, "
        "regime_sorted AS ("
        "    SELECT ts, regime FROM regime_history WHERE regime IS NOT NULL"
        "), "
        "decisive AS ("
        "    SELECT bot, closed_at, pnl_pct, is_win FROM flagged "
        f"    WHERE is_decisive AND bot IS NOT NULL{bot_sql}"
        ") "
        "SELECT d.bot, r.regime, "
        "count(*) AS n, "
        "count(*) FILTER (WHERE d.is_win) AS wins, "
        "sum(d.pnl_pct) AS pnl_sum "
        "FROM decisive d "
        "ASOF JOIN regime_sorted r ON d.closed_at >= r.ts "
        "GROUP BY d.bot, r.regime "
        "ORDER BY d.bot, r.regime",
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
# Coin drill-down (Feature 7, T-2026-CU-9050-159)
# ─────────────────────────────────────────────────────────────────────────────

# Same source tables as _OUTCOME_TABLES, additionally carrying the per-row
# coin/symbol column name (closed_ai_signals: symbol, closed_trades: coin) and
# the target-hit column where the table has one (closed_trades does not —
# None means "project a NULL", never a fabricated 0). Kept as its own
# tuple/CTE builder rather than widening _OUTCOME_TABLES/_outcomes_cte in
# place: those two feed Feature 2/3/6's bot-level aggregates and stay
# byte-for-byte unchanged (CLAUDE.md: additive only, no core aggregate
# rewritten to add a column three other features don't need).
_OUTCOME_TABLES_WITH_COIN = (
    ("closed_ai_signals", "model", "close_time", "symbol", "targets_hit"),
    ("closed_trades", "strategy", "posted", "coin", None),
)


def _existing_outcome_tables_with_coin(
    con: duckdb.DuckDBPyConnection,
) -> list[tuple[str, str, str, str, str | None]]:
    present = {
        r[0]
        for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
    }
    return [t for t in _OUTCOME_TABLES_WITH_COIN if t[0] in present]


def _outcomes_cte_with_coin(tables: Sequence[tuple[str, str, str, str, str | None]]) -> str:
    """Build the coin-aware ``flagged`` CTE for the drill-down panel.

    Uses the IDENTICAL pnl/is_decisive/is_win predicate ``_outcomes_cte``
    builds (same ``MICRO_PNL_PCT``/``MAX_ABS_PNL_PCT`` thresholds, same
    housekeeping-status exclusion via DELISTED/CLEANUP/ORPHAN) — a trade is
    decisive here IFF it would be decisive in ``_outcomes_cte``. The
    projection is wider: per-row coin, entry, close_price and targets_hit
    columns the drill-down table/chart need, which ``_outcomes_cte`` does not
    carry (Feature 2/3/6 never needed them).
    """
    parts = []
    for table, bot_col, ts_col, coin_col, hit_col in tables:
        hit_expr = f'"{hit_col}"' if hit_col else "CAST(NULL AS INTEGER)"
        parts.append(
            f'SELECT "{bot_col}" AS bot, "{coin_col}" AS coin, direction, entry, close_price, '
            f'status, "{ts_col}" AS closed_at, {hit_expr} AS targets_hit FROM "{table}"'
        )
    unions = " UNION ALL ".join(parts)
    return f"""
WITH outcomes AS (
    {unions}
),
scored AS (
    SELECT
        bot, coin, direction, entry, close_price, closed_at, targets_hit,
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
        bot, coin, direction, entry, close_price, closed_at, targets_hit, pnl_pct,
        (pnl_pct IS NOT NULL AND NOT is_housekeeping
         AND abs(pnl_pct) > {MICRO_PNL_PCT} AND abs(pnl_pct) <= {MAX_ABS_PNL_PCT}) AS is_decisive,
        (pnl_pct IS NOT NULL AND NOT is_housekeeping
         AND abs(pnl_pct) > {MICRO_PNL_PCT} AND abs(pnl_pct) <= {MAX_ABS_PNL_PCT}
         AND pnl_pct > 0) AS is_win
    FROM scored
)"""


def coins_with_trades(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Distinct coin/symbol values with at least one DECISIVE trade, sorted.

    Feeds the coin-selector list for the drill-down panel (Feature 7,
    T-2026-CU-9050-159) — a coin with only neutral/housekeeping rows never
    appears, mirroring ``bot_leaderboard``'s "only bots with a decisive trade"
    convention. Empty list when no outcome table exists yet (mirrors every
    other analytics_api aggregate's empty-substrate degrade).
    """
    tables = _existing_outcome_tables_with_coin(con)
    if not tables:
        return []
    cte = _outcomes_cte_with_coin(tables)
    rows = con.execute(
        f"{cte} SELECT DISTINCT coin FROM flagged WHERE is_decisive AND coin IS NOT NULL ORDER BY coin"
    ).fetchall()
    return [r[0] for r in rows]


def coin_trade_series(con: duckdb.DuckDBPyConnection, symbol: str | None) -> dict[str, Any]:
    """Ordered DECISIVE-trade rows for ONE coin — the drill-down panel's data
    source (Feature 7, T-2026-CU-9050-159).

    Args:
        con: an open DuckDB connection (typically ``read_only=True``).
        symbol: the coin/symbol to filter to. ``None``, empty, or a value not
            present in :func:`coins_with_trades` all degrade to an empty trade
            list — never a 500, and never every coin's trades leaking through
            on a falsy value. Unlike ``_bot_filter``'s "empty == unfiltered"
            multiselect convention, a single-coin drill-down must show only
            its OWN coin's trades or nothing at all.

    Returns a JSON-serialisable dict:
        {"coin": symbol, "trades": [{bot, direction, closed_at, entry,
            close_price, targets_hit, pnl_pct, is_win}, ...]}
    ``trades`` is ordered by ``closed_at`` ascending — the order the
    price-line chart needs to draw entry->exit points left to right.
    ``targets_hit`` is ``None`` for a ``closed_trades`` row (that table has no
    such column) — never a fabricated 0. ``entry``/``close_price`` are always
    present for a decisive row (the definition itself requires ``entry > 0
    AND close_price > 0``).
    """
    if not symbol:
        return {"coin": symbol, "trades": []}
    tables = _existing_outcome_tables_with_coin(con)
    if not tables:
        return {"coin": symbol, "trades": []}
    cte = _outcomes_cte_with_coin(tables)
    rows = con.execute(
        f"{cte} SELECT bot, direction, closed_at, entry, close_price, targets_hit, pnl_pct, is_win "
        "FROM flagged WHERE is_decisive AND coin = ? ORDER BY closed_at",
        [symbol],
    ).fetchall()
    trades = [
        {
            "bot": bot,
            "direction": direction,
            "closed_at": closed_at.isoformat() if hasattr(closed_at, "isoformat") else str(closed_at),
            "entry": float(entry),
            "close_price": float(close_price),
            "targets_hit": int(targets_hit) if targets_hit is not None else None,
            "pnl_pct": round(float(pnl_pct), 4),
            "is_win": bool(is_win),
        }
        for bot, direction, closed_at, entry, close_price, targets_hit, pnl_pct, is_win in rows
    ]
    return {"coin": symbol, "trades": trades}


# ─────────────────────────────────────────────────────────────────────────────
# Overnight digest (Feature 8, T-2026-CU-9050-160)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_DIGEST_WINDOW_HOURS = 8


def _regime_changes_in_window(con: duckdb.DuckDBPyConnection, as_of: Any, window_hours: int) -> int:
    """Count REAL regime transitions (a row whose ``regime`` differs from the
    immediately preceding row's, via a ``LAG`` window function) whose ``ts``
    falls in the trailing ``window_hours`` window ending at ``as_of``.

    A transition is counted only when a PRECEDING row exists AND the value
    actually changed — the very first ever ``regime_history`` row (no
    predecessor) is an initialisation, not a transition, and an append that
    repeats the same regime is not a "change" either. Assumes the caller has
    already verified ``regime_history`` exists (mirrors ``bot_regime_matrix``'s
    :func:`_regime_history_present` gate — this helper is not itself
    substrate-existence-safe).
    """
    row = con.execute(
        "WITH ordered AS ("
        "    SELECT ts, regime, lag(regime) OVER (ORDER BY ts) AS prev_regime "
        "    FROM regime_history WHERE regime IS NOT NULL"
        ") "
        "SELECT count(*) FROM ordered "
        "WHERE prev_regime IS NOT NULL AND regime != prev_regime "
        f"AND ts > (CAST(? AS TIMESTAMP) - INTERVAL {int(window_hours)} HOUR) "
        "AND ts <= CAST(? AS TIMESTAMP)",
        [as_of, as_of],
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def overnight_digest(
    con: duckdb.DuckDBPyConnection,
    window_hours: int = DEFAULT_DIGEST_WINDOW_HOURS,
    *,
    as_of: Any = None,
    bots: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Overnight-digest summary over a trailing ``window_hours`` window — the
    landing-page digest section's data source (Feature 8, T-2026-CU-9050-160).

    Reuses the coin-aware CTE built for the coin drill-down
    (:func:`_outcomes_cte_with_coin` / :func:`_existing_outcome_tables_with_coin`,
    Feature 7) — the IDENTICAL decisive-trade definition every other aggregate
    in this module shares, extended with the per-row ``coin`` the notable-trade
    fields need. Neither that CTE nor any other existing aggregate function is
    modified here.

    WINDOW / TIMEZONE: like :func:`success_rate_timeseries`,
    ``as_of`` defaults to ``max(closed_at)`` actually present in the substrate
    rather than a wall-clock "now" — this keeps the trailing-window comparison
    strictly within the SAME (naive-local) timestamp space the ``closed_at``
    columns are stored in (analytics_export TIMEZONE contract), never mixing
    it with a real UTC clock. The window itself is the same half-open
    trailing convention as every rolling window in this module: ``closed_at >
    as_of - INTERVAL window_hours HOUR AND closed_at <= as_of``.

    Args:
        con: an open DuckDB connection (typically ``read_only=True``).
        window_hours: trailing-hour window width (default 8 = "Overnight").
        as_of: anchor for the window; defaults to the latest ``closed_at`` in
            the data.
        bots: optional bot-multiselect filter; None = all bots.

    Returns a JSON-serialisable dict:
        {
          "as_of": ISO|None, "window_hours": int,
          "n": int, "wins": int,
          "pnl_sum_pct": float|None, "winrate": float|None,
          "top_bot": {bot,n,wins,winrate,pnl_sum_pct,expectancy_pct}|None,
          "flop_bot": {...}|None,
          "best_trade": {bot,coin,pnl_pct,closed_at}|None,
          "worst_trade": {...}|None,
          "regime_changes": int|None,   # None only when regime_history absent
        }
    An empty window (no decisive trade in ``window_hours``, whether because
    the substrate has none at all or because every trade in it falls outside
    the window) degrades to ``n=0`` with every derived field ``None`` — never
    a 500, never a fabricated zero standing in for "no data".
    """
    empty: dict[str, Any] = {
        "as_of": None,
        "window_hours": window_hours,
        "n": 0,
        "wins": 0,
        "pnl_sum_pct": None,
        "winrate": None,
        "top_bot": None,
        "flop_bot": None,
        "best_trade": None,
        "worst_trade": None,
        "regime_changes": None,
    }
    tables = _existing_outcome_tables_with_coin(con)
    if not tables:
        return empty
    cte = _outcomes_cte_with_coin(tables)

    if as_of is None:
        row = con.execute(f"{cte} SELECT max(closed_at) FROM flagged").fetchone()
        as_of = row[0] if row and row[0] is not None else None
    if as_of is None:
        return empty

    result = dict(empty)
    result["as_of"] = as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of)
    result["regime_changes"] = (
        _regime_changes_in_window(con, as_of, window_hours) if _regime_history_present(con) else None
    )

    params: list[Any] = [as_of, as_of]
    bot_sql = _bot_filter(bots, params)
    rows = con.execute(
        f"{cte} SELECT bot, coin, closed_at, pnl_pct, is_win FROM flagged "
        "WHERE is_decisive AND bot IS NOT NULL "
        f"AND closed_at > (CAST(? AS TIMESTAMP) - INTERVAL {int(window_hours)} HOUR) "
        "AND closed_at <= CAST(? AS TIMESTAMP)"
        f"{bot_sql} ORDER BY closed_at",
        params,
    ).fetchall()
    if not rows:
        return result

    trades = [
        {
            "bot": bot,
            "coin": coin,
            "closed_at": closed_at.isoformat() if hasattr(closed_at, "isoformat") else str(closed_at),
            "pnl_pct": float(pnl_pct),
            "is_win": bool(is_win),
        }
        for bot, coin, closed_at, pnl_pct, is_win in rows
    ]

    n = len(trades)
    wins = sum(1 for t in trades if t["is_win"])
    pnl_sum = sum(t["pnl_pct"] for t in trades)

    by_bot: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        by_bot.setdefault(t["bot"], []).append(t)
    bot_rows = []
    for bot, bot_trades in by_bot.items():
        bn = len(bot_trades)
        bwins = sum(1 for t in bot_trades if t["is_win"])
        bpnl = sum(t["pnl_pct"] for t in bot_trades)
        bot_rows.append(
            {
                "bot": bot,
                "n": bn,
                "wins": bwins,
                "winrate": _winrate(bwins, bn),
                "pnl_sum_pct": round(bpnl, 4),
                "expectancy_pct": round(bpnl / bn, 4) if bn else None,
            }
        )
    # Highest/lowest summed PnL in the window — same headline convention as
    # DEFAULT_LEADERBOARD_SORT ("pnl_sum_pct"). With a single bot present,
    # top_bot and flop_bot are the same row (there is nothing to contrast).
    top_bot = max(bot_rows, key=lambda r: r["pnl_sum_pct"])
    flop_bot = min(bot_rows, key=lambda r: r["pnl_sum_pct"])

    best = max(trades, key=lambda t: t["pnl_pct"])
    worst = min(trades, key=lambda t: t["pnl_pct"])

    result.update(
        {
            "n": n,
            "wins": wins,
            "pnl_sum_pct": round(pnl_sum, 4),
            "winrate": _winrate(wins, n),
            "top_bot": top_bot,
            "flop_bot": flop_bot,
            "best_trade": {
                "bot": best["bot"],
                "coin": best["coin"],
                "pnl_pct": round(best["pnl_pct"], 4),
                "closed_at": best["closed_at"],
            },
            "worst_trade": {
                "bot": worst["bot"],
                "coin": worst["coin"],
                "pnl_pct": round(worst["pnl_pct"], 4),
                "closed_at": worst["closed_at"],
            },
        }
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Read-only event feed (Feature 9, T-2026-CU-9050-161)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_EVENT_FEED_WINDOW_HOURS = 24

# How many biggest wins / biggest losses (each side counted separately, via
# is_win — never just the N most extreme pnl_pct values, so a window with
# very few decisive trades can never let one side crowd out the other) become
# "notable trade" events per event_feed() call.
NOTABLE_TRADES_PER_SIDE = 3


def _regime_transition_events(con: duckdb.DuckDBPyConnection, as_of: Any, window_hours: int) -> list[dict[str, Any]]:
    """Real regime TRANSITIONS (a row whose ``regime`` differs from the
    immediately preceding row's, via the identical ``LAG``-window logic
    :func:`_regime_changes_in_window` uses to COUNT them) inside the trailing
    ``window_hours`` window ending at ``as_of`` — as typed event dicts
    carrying the full from->to detail that function's plain count discards.

    Deliberately kept as its own sibling query rather than widening
    ``_regime_changes_in_window`` in place (that function stays byte-for-byte
    unchanged, same "additive sibling, not a rewritten core aggregate"
    convention ``_outcomes_cte_with_coin`` already set next to
    ``_outcomes_cte``). Assumes the caller has already verified
    ``regime_history`` exists (mirrors ``_regime_changes_in_window``'s own
    precondition) — not itself substrate-existence-safe.
    """
    rows = con.execute(
        "WITH ordered AS ("
        "    SELECT ts, regime, lag(regime) OVER (ORDER BY ts) AS prev_regime "
        "    FROM regime_history WHERE regime IS NOT NULL"
        ") "
        "SELECT ts, prev_regime, regime FROM ordered "
        "WHERE prev_regime IS NOT NULL AND regime != prev_regime "
        f"AND ts > (CAST(? AS TIMESTAMP) - INTERVAL {int(window_hours)} HOUR) "
        "AND ts <= CAST(? AS TIMESTAMP) "
        "ORDER BY ts",
        [as_of, as_of],
    ).fetchall()
    return [
        {
            "type": "regime_change",
            "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "title": f"Regime-Wechsel: {prev_regime} → {regime}",
            "detail": f"{prev_regime} → {regime}",
        }
        for ts, prev_regime, regime in rows
    ]


def _notable_trade_events(
    con: duckdb.DuckDBPyConnection,
    tables: Sequence[tuple[str, str, str, str, str | None]],
    as_of: Any,
    window_hours: int,
    *,
    bots: Sequence[str] | None = None,
    per_side: int = NOTABLE_TRADES_PER_SIDE,
) -> list[dict[str, Any]]:
    """Biggest win(s)/loss(es) inside the trailing ``window_hours`` window
    ending at ``as_of`` — as typed event dicts.

    Reuses the coin-aware CTE (:func:`_outcomes_cte_with_coin`, built from the
    caller-supplied ``tables`` from :func:`_existing_outcome_tables_with_coin`)
    and the identical half-open window convention :func:`overnight_digest`
    uses — a trade counts here iff it would count there. Splits the decisive
    trades on ``is_win`` (not on sorting ``pnl_pct`` and slicing both ends) so
    the winner/loser sides never overlap even when the window holds fewer
    than ``2 * per_side`` decisive trades.
    """
    cte = _outcomes_cte_with_coin(tables)
    params: list[Any] = [as_of, as_of]
    bot_sql = _bot_filter(bots, params)
    rows = con.execute(
        f"{cte} SELECT bot, coin, closed_at, pnl_pct, is_win FROM flagged "
        "WHERE is_decisive AND bot IS NOT NULL "
        f"AND closed_at > (CAST(? AS TIMESTAMP) - INTERVAL {int(window_hours)} HOUR) "
        "AND closed_at <= CAST(? AS TIMESTAMP)"
        f"{bot_sql}",
        params,
    ).fetchall()
    if not rows:
        return []

    trades = [
        {"bot": bot, "coin": coin, "closed_at": closed_at, "pnl_pct": float(pnl_pct), "is_win": bool(is_win)}
        for bot, coin, closed_at, pnl_pct, is_win in rows
    ]
    winners = sorted((t for t in trades if t["is_win"]), key=lambda t: t["pnl_pct"], reverse=True)[:per_side]
    losers = sorted((t for t in trades if not t["is_win"]), key=lambda t: t["pnl_pct"])[:per_side]

    def _event(t: dict[str, Any]) -> dict[str, Any]:
        ts = t["closed_at"]
        return {
            "type": "notable_trade",
            "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "title": f"{'Gewinn' if t['is_win'] else 'Verlust'}: {t['coin']} ({t['bot']})",
            "detail": f"{t['pnl_pct']:+.2f}%",
        }

    return [_event(t) for t in (*winners, *losers)]


def _latest_event_anchor(
    con: duckdb.DuckDBPyConnection,
    tables: Sequence[tuple[str, str, str, str, str | None]],
    has_regime: bool,
) -> Any:
    """Data-anchored ``as_of`` for :func:`event_feed`: ``max(closed_at)``
    across the outcome tables when any exist (the same data-anchored, never
    wall-clock, convention :func:`overnight_digest` uses), falling back to
    ``max(ts)`` from ``regime_history`` so the feed still anchors to
    something when the substrate carries regime classifications but no trade
    outcomes yet. ``None`` only when the substrate is fully empty of both.
    """
    if tables:
        cte = _outcomes_cte_with_coin(tables)
        row = con.execute(f"{cte} SELECT max(closed_at) FROM flagged").fetchone()
        candidate = row[0] if row and row[0] is not None else None
        if candidate is not None:
            return candidate
    if has_regime:
        row = con.execute("SELECT max(ts) FROM regime_history").fetchone()
        return row[0] if row and row[0] is not None else None
    return None


def event_feed(
    con: duckdb.DuckDBPyConnection,
    window_hours: int = DEFAULT_EVENT_FEED_WINDOW_HOURS,
    *,
    as_of: Any = None,
    bots: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Read-only, chronologically-DESCENDING event feed over a trailing
    ``window_hours`` window — the event-log panel's data source (Feature 9,
    T-2026-CU-9050-161).

    Consolidates two typed event kinds from the DuckDB substrate, each built
    from a query already proven for a sibling feature (nothing here rewrites
    an existing aggregate):
      * ``"regime_change"`` — real regime TRANSITIONS from ``regime_history``
        (:func:`_regime_transition_events`, the same ``LAG``-window logic
        :func:`_regime_changes_in_window` (Feature 8) uses to count them).
      * ``"notable_trade"`` — the biggest win(s)/loss(es) in the window
        (:func:`_notable_trade_events`, the coin-aware decisive-trade CTE
        Feature 7/8 already built).

    WINDOW / TIMEZONE: identical convention to :func:`overnight_digest` —
    ``as_of`` defaults to a data-anchored value (never a wall-clock "now",
    see :func:`_latest_event_anchor`) and the window itself is the same
    half-open trailing convention (``ts/closed_at > as_of - INTERVAL
    window_hours HOUR AND <= as_of``) every rolling window in this module
    already uses — never mixing the naive-local timestamp space with a real
    UTC clock (TIMEZONE contract, see analytics_export module docstring).

    Args:
        con: an open DuckDB connection (typically ``read_only=True``).
        window_hours: trailing-hour window width (default 24h).
        as_of: anchor for the window; defaults to the latest data point in
            the substrate (outcome tables, else ``regime_history``).
        bots: optional bot-multiselect filter — narrows ``notable_trade``
            events only; regime transitions carry no bot dimension.

    Returns a JSON-serialisable dict:
        {"as_of": ISO|None, "window_hours": int,
         "events": [{"type", "ts", "title", "detail"}, ...]}
    ``events`` is sorted by ``ts`` DESCENDING (newest first — an event log,
    not a chart series) with ``type`` as a deterministic tie-breaker for
    same-instant events. An empty window (substrate has data, but nothing
    falls inside ``window_hours``) or a fully empty substrate both degrade to
    ``events: []`` — never a 500, never a fabricated event.
    """
    empty: dict[str, Any] = {"as_of": None, "window_hours": window_hours, "events": []}

    tables = _existing_outcome_tables_with_coin(con)
    has_regime = _regime_history_present(con)

    if as_of is None:
        as_of = _latest_event_anchor(con, tables, has_regime)
    if as_of is None:
        return empty

    events: list[dict[str, Any]] = []
    if tables:
        events.extend(_notable_trade_events(con, tables, as_of, window_hours, bots=bots))
    if has_regime:
        events.extend(_regime_transition_events(con, as_of, window_hours))
    events.sort(key=lambda e: (e["ts"], e["type"]), reverse=True)

    return {
        "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
        "window_hours": window_hours,
        "events": events,
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
    parser.add_argument("--dev", action="store_true", help="Flask dev server instead of Waitress (local smoke only)")
    args = parser.parse_args(argv)
    _serve(create_app(args.duckdb), host=args.host, port=args.port, dev=args.dev)


if __name__ == "__main__":  # pragma: no cover
    main()

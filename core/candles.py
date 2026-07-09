# core/candles.py — single access point for candle and indicator data.
#
# WHY THIS MODULE EXISTS
# ----------------------
# Today ~108 live call sites build table names as f-strings ("{SYMBOL}_{tf}",
# "{SYMBOL}_{tf}_indicators") and each one decides for itself whether it sees
# the still-forming candle. That produces the two root causes of
# docs/TIMESCALE_R1_MIGRATION.md:
#
#   R1  — look-ahead/repaint, because "drop the newest row" is re-implemented
#         (or forgotten) per bot, sometimes on DESC-sorted frames where the
#         newest row is index 0 and `iloc[-1]` is the OLDEST one.
#   #18 — 9.297 per-coin tables instead of two hypertables.
#
# The migration only stays invisible to the fleet if every access goes through
# this module first. Phase A (this file): read/write the OLD per-coin tables,
# behaviour-equal except for the deliberate `include_forming=False` default.
# Phase C: swap the internals to the `candles`/`indicators` hypertables without
# touching a single bot.
#
# CONTRACTS (all of them load-bearing — see docs/CANDLE_CALL_SITES.md)
# -------------------------------------------------------------------
# 1. Reads return ASCENDING by open_time. Always. `iloc[-1]` is the newest row,
#    `iloc[0]` the oldest. No caller has to know how the SQL was ordered.
# 2. `include_forming=False` is the default. Only price-shaped readers may pass
#    True: monitors 5/8, get_live_price fallbacks, the orchestrator's last-close
#    probe, the health-monitor staleness canary, and the two bots whose contract
#    is "features from the second-to-last row, live price from the last"
#    (11_ai_mis, 12_ai_ats). Analytical readers must not.
# 3. Writes DO NOT COMMIT. The caller owns the transaction (same contract as
#    core/signal_post.py). Callers migrating away from insert_fast() /
#    write_indicators_to_db_optimized() must add their own conn.commit().
# 4. Identifier hygiene (P3.3): symbol and timeframe are validated and quoted
#    via psycopg2.sql.Identifier. An optional whitelist (coins.json) can be
#    installed with set_symbol_whitelist(); it is NOT loosened here.
#
# THE `is_closed` GAP (Phase A vs. Phase C)
# ----------------------------------------
# The target schema carries `is_closed boolean` written by ingestion from the
# Binance kline flag `k['x']`. The old per-coin tables have no such column, so
# in Phase A "closed" is DERIVED from the clock: a candle is closed once its
# period has elapsed, i.e. `open_time < period_start(tf, now())`. The cutoff is
# computed DB-side from `now()` — one clock, the writer's — and is timezone
# independent (pure epoch arithmetic, Monday-anchored for '1w').
#
# This derivation is strictly weaker than the real flag in one respect: a row
# whose period just elapsed may still carry the values of the last pre-close
# WebSocket tick for a few hundred milliseconds. KYTHERA_CANDLES_CLOSE_GRACE_SEC
# shifts the cutoff back by N seconds if that race ever shows up in practice.
# Default 0 — no grace, no silent data hiding. See the operator questions in
# docs/CANDLE_CALL_SITES.md §5.
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from psycopg2 import extras, sql

# Mirrors core.config.TIMEFRAMES, but duplicated on purpose: importing
# core.config requires DB_PASSWORD and would make this module unimportable on a
# machine without credentials (build machine, unit tests). backtest/test_candles.py
# asserts that both lists stay in sync.
TF_SECONDS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}

# Epoch 0 is a Thursday; Binance weekly klines open on Monday 00:00 UTC.
# 345600 = 1970-01-05T00:00:00Z, the first Monday.
_TF_EPOCH_OFFSET: dict[str, int] = {"1w": 345600}

CANDLE_COLUMNS: tuple[str, ...] = ("symbol", "open_time", "open", "high", "low", "close", "volume")
INDICATOR_SUFFIX = "_indicators"

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,24}$")
_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_WHITELIST: frozenset[str] | None = None


class CandleSourceError(RuntimeError):
    """Raised when KYTHERA_CANDLES_SOURCE names a backend that is not built yet."""


# ── Identifier hygiene (P3.3) ─────────────────────────────────────────────────


def set_symbol_whitelist(symbols: Iterable[str] | None) -> None:
    """Install (or clear, with None) the set of symbols this process may query.

    Without a whitelist only the regex applies. With one, an unknown symbol is a
    hard ValueError — that is the guard that keeps junk pairs (the "ETHU"
    incident) from creating a new class of tables/rows.
    """
    global _WHITELIST
    _WHITELIST = None if symbols is None else frozenset(symbols)


def load_symbol_whitelist(path: str = "coins.json") -> frozenset[str]:
    """Read coins.json and install it as the whitelist. Returns the set."""
    with open(path, encoding="utf-8") as fh:
        coins = json.load(fh)
    if not isinstance(coins, list) or not coins:
        raise ValueError(f"{path} does not contain a non-empty list of symbols")
    set_symbol_whitelist(coins)
    assert _WHITELIST is not None
    return _WHITELIST


def validate_symbol(symbol: str) -> str:
    """Validate a symbol for use as a table-name component. Returns it unchanged."""
    if not isinstance(symbol, str) or not _SYMBOL_RE.match(symbol):
        raise ValueError(f"invalid symbol for table identifier: {symbol!r}")
    if _WHITELIST is not None and symbol not in _WHITELIST:
        raise ValueError(f"symbol {symbol!r} is not in the configured whitelist")
    return symbol


def validate_timeframe(tf: str) -> str:
    """Validate a timeframe against the supported set. Returns it unchanged."""
    if tf not in TF_SECONDS:
        raise ValueError(f"unsupported timeframe: {tf!r} (known: {sorted(TF_SECONDS)})")
    return tf


def candles_table(symbol: str, tf: str) -> str:
    """Legacy per-coin candle table name, unquoted (e.g. 'BTCUSDT_1h')."""
    return f"{validate_symbol(symbol)}_{validate_timeframe(tf)}"


def indicators_table(symbol: str, tf: str) -> str:
    """Legacy per-coin indicator table name, unquoted (e.g. 'BTCUSDT_1h_indicators')."""
    return f"{candles_table(symbol, tf)}{INDICATOR_SUFFIX}"


def _ident(table: str) -> sql.Identifier:
    return sql.Identifier(table)


def _require_open_time(columns: Sequence[str] | None) -> None:
    """Every projection has to carry open_time — the outer ORDER BY sorts on it."""
    if columns is not None and "open_time" not in columns:
        raise ValueError("column projection must contain 'open_time' (reads are ordered by it)")


def _columns_sql(columns: Sequence[str] | None, prefix: str | None = None) -> sql.Composable:
    if columns is None:
        return sql.SQL("{}.*").format(sql.Identifier(prefix)) if prefix else sql.SQL("*")
    parts: list[sql.Composable] = []
    for col in columns:
        if not _COLUMN_RE.match(col):
            raise ValueError(f"invalid column identifier: {col!r}")
        parts.append(sql.Identifier(prefix, col) if prefix else sql.Identifier(col))
    return sql.SQL(", ").join(parts)


# ── The closed-candle cutoff ──────────────────────────────────────────────────


def _grace_seconds() -> float:
    return float(os.getenv("KYTHERA_CANDLES_CLOSE_GRACE_SEC", "0"))


def period_start(tf: str, now: datetime) -> datetime:
    """Open time of the period that is CURRENTLY forming at `now` (UTC).

    Every candle with `open_time < period_start(tf, now)` is closed. Pure Python
    mirror of `_period_start_sql` — used by tools/candles_parity.py and by the
    unit tests, so the SQL and the Python answer can be compared directly.
    """
    validate_timeframe(tf)
    if now.tzinfo is None:
        raise ValueError("period_start() requires a timezone-aware datetime")
    step = TF_SECONDS[tf]
    off = _TF_EPOCH_OFFSET.get(tf, 0)
    epoch = now.astimezone(timezone.utc).timestamp()
    floored = ((epoch - off) // step) * step + off
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _period_start_sql(tf: str) -> sql.Composable:
    """SQL expression for period_start(tf, db-now() - grace).

    Deliberately epoch arithmetic rather than date_trunc(): date_trunc depends
    on the session TimeZone, and this cutoff must not move when a bot process
    connects with a different timezone than the ingestion process.
    """
    step = TF_SECONDS[tf]
    off = _TF_EPOCH_OFFSET.get(tf, 0)
    return sql.SQL(
        "to_timestamp(floor((extract(epoch from now()) - {grace} - {off}) / {step}) * {step} + {off})"
    ).format(
        grace=sql.Literal(_grace_seconds()),
        off=sql.Literal(off),
        step=sql.Literal(step),
    )


def _forming_filter(tf: str, include_forming: bool, alias: str | None = None) -> sql.Composable:
    if include_forming:
        return sql.SQL("")
    col = sql.Identifier(alias, "open_time") if alias else sql.Identifier("open_time")
    return sql.SQL(" AND {col} < {cutoff}").format(col=col, cutoff=_period_start_sql(tf))


# ── Backend switch (Phase 4 seam, not yet built) ──────────────────────────────


def _assert_legacy_backend() -> None:
    source = os.getenv("KYTHERA_CANDLES_SOURCE", "legacy")
    if source != "legacy":
        raise CandleSourceError(
            f"KYTHERA_CANDLES_SOURCE={source!r}: only 'legacy' (per-coin tables) is implemented. "
            "The hypertable backend lands with migration phase 4 (docs/TIMESCALE_R1_MIGRATION.md)."
        )


# ── Reads ─────────────────────────────────────────────────────────────────────


def _fetch_df(conn: Any, query: sql.Composable, params: Sequence[Any]) -> pd.DataFrame:
    with conn.cursor() as cur:
        # `params or None`: psycopg2 runs %-interpolation whenever vars is not
        # None, so an empty list would still scan the statement for placeholders.
        cur.execute(query, params or None)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    return pd.DataFrame(rows, columns=cols)


def _windowed_select(
    table: str,
    columns: sql.Composable,
    tf: str,
    include_forming: bool,
    start: datetime | None,
    end: datetime | None,
    limit: int | None,
) -> tuple[sql.Composable, list[Any]]:
    """SELECT the newest `limit` rows of the window, returned ASC.

    The LIMIT has to bite on a DESC ordering (newest N candles), the result has
    to arrive ASC (contract 1) — hence the wrapping subselect.
    """
    params: list[Any] = []
    where = sql.SQL("WHERE true")
    if start is not None:
        where += sql.SQL(" AND open_time >= %s")
        params.append(start)
    if end is not None:
        where += sql.SQL(" AND open_time <= %s")
        params.append(end)
    where += _forming_filter(tf, include_forming)

    inner = sql.SQL("SELECT {cols} FROM {tbl} {where} ORDER BY open_time DESC").format(
        cols=columns, tbl=_ident(table), where=where
    )
    if limit is None:
        return (
            sql.SQL("SELECT * FROM ({inner}) s ORDER BY open_time ASC").format(inner=inner),
            params,
        )
    inner += sql.SQL(" LIMIT %s")
    params.append(int(limit))
    return sql.SQL("SELECT * FROM ({inner}) s ORDER BY open_time ASC").format(inner=inner), params


def read_candles(
    conn: Any,
    symbol: str,
    tf: str,
    *,
    limit: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    include_forming: bool = False,
    columns: Sequence[str] | None = CANDLE_COLUMNS,
) -> pd.DataFrame:
    """OHLCV candles for (symbol, tf), ascending by open_time.

    `limit` selects the NEWEST n candles of the window (not the oldest).
    `include_forming=True` is reserved for pure price checks — see contract 2.
    """
    _assert_legacy_backend()
    table = candles_table(symbol, tf)
    _require_open_time(columns)
    query, params = _windowed_select(table, _columns_sql(columns), tf, include_forming, start, end, limit)
    return _fetch_df(conn, query, params)


def read_indicators(
    conn: Any,
    symbol: str,
    tf: str,
    *,
    limit: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    include_forming: bool = False,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Indicator rows for (symbol, tf), ascending by open_time.

    `columns=None` means SELECT * — the ~120 indicator columns are schema-driven
    (2_indicator_engine.get_indicator_definitions), not enumerable here.
    """
    _assert_legacy_backend()
    table = indicators_table(symbol, tf)
    _require_open_time(columns)
    query, params = _windowed_select(table, _columns_sql(columns), tf, include_forming, start, end, limit)
    return _fetch_df(conn, query, params)


def read_candles_with_indicators(
    conn: Any,
    symbol: str,
    tf: str,
    *,
    limit: int | None = None,
    include_forming: bool = False,
    candle_columns: Sequence[str] | None = CANDLE_COLUMNS,
    indicator_columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Candles LEFT JOIN indicators on open_time, ascending.

    This is the dominant AI-bot read pattern (bots 11, 12, 15, 24, 25 and
    core/research_features). It stays one SQL statement so the migration does
    not turn one query into two round-trips per coin.

    Duplicate column names (`symbol`, `close`, `open_time` exist on both sides)
    are resolved in favour of the candle table; the indicator side is projected
    without them. `indicator_columns=None` therefore costs one catalog lookup —
    an unqualified `i.*` would hand the caller a DataFrame with three duplicate
    column labels, which pandas resolves by position, silently.
    """
    _assert_legacy_backend()
    _require_open_time(candle_columns)
    ctab, itab = candles_table(symbol, tf), indicators_table(symbol, tf)
    if indicator_columns is None:
        indicator_columns = indicator_column_names(conn, symbol, tf)
    indicator_columns = [c for c in indicator_columns if c not in ("symbol", "open_time", "close")]
    if not indicator_columns:
        raise ValueError(f"no indicator columns to join for {itab}")

    params: list[Any] = []
    inner = sql.SQL("SELECT {ccols}, {icols} FROM {ctab} h LEFT JOIN {itab} i ON h.open_time = i.open_time").format(
        ccols=_columns_sql(candle_columns, prefix="h"),
        icols=_columns_sql(indicator_columns, prefix="i"),
        ctab=_ident(ctab),
        itab=_ident(itab),
    )
    inner += sql.SQL(" WHERE true") + _forming_filter(tf, include_forming, alias="h")
    inner += sql.SQL(" ORDER BY h.open_time DESC")
    if limit is not None:
        inner += sql.SQL(" LIMIT %s")
        params.append(int(limit))
    query = sql.SQL("SELECT * FROM ({inner}) s ORDER BY open_time ASC").format(inner=inner)
    return _fetch_df(conn, query, params)


def latest_open_time(conn: Any, symbol: str, tf: str, *, include_forming: bool = True) -> datetime | None:
    """MAX(open_time) of the candle table, or None if the table does not exist.

    Default `include_forming=True` keeps the ingestion catch-up/resume semantics
    of 1_data_ingestion.get_latest_open_time() byte-equal: it resumes from the
    newest row it wrote, forming or not.
    """
    _assert_legacy_backend()
    table = candles_table(symbol, tf)
    if not table_exists(conn, table):
        return None
    query = sql.SQL("SELECT MAX(open_time) FROM {tbl} WHERE true").format(tbl=_ident(table))
    query += _forming_filter(tf, include_forming)
    with conn.cursor() as cur:
        cur.execute(query)
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    ts: datetime = row[0]
    return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def table_exists(conn: Any, table: str) -> bool:
    """to_regclass probe. `table` is the unquoted name (candles_table()/indicators_table()).

    to_regclass takes the identifier as TEXT and applies the normal casefolding
    rules, so the name has to arrive quoted — 'BTCUSDT_1h' would be folded to
    lowercase and never match.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (sql.Identifier(table).as_string(cur),))
        row = cur.fetchone()
    return bool(row and row[0] is not None)


def indicator_column_names(conn: Any, symbol: str, tf: str) -> list[str]:
    """Column names of the indicator table, in ordinal order.

    The ~120 indicator columns are generated at runtime
    (2_indicator_engine.get_indicator_definitions), so any code that wants an
    explicit projection instead of `SELECT *` has to ask the catalog.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position",
            (indicators_table(symbol, tf),),
        )
        return [r[0] for r in cur.fetchall()]


# ── Writes (caller commits — contract 3) ──────────────────────────────────────


def upsert_candles(
    conn: Any,
    symbol: str,
    tf: str,
    rows: Sequence[Sequence[Any]],
    *,
    closed: bool,
    page_size: int = 500,
) -> int:
    """Upsert OHLCV rows for one (symbol, tf). Returns the number of rows sent.

    `rows`: (symbol, open_time, open, high, low, close, volume).

    Deviates from the sketch in docs/TIMESCALE_R1_MIGRATION.md §2, which had
    `upsert_candles(conn, rows, closed)`: the legacy backend needs (symbol, tf)
    to pick the table, and the hypertable backend needs them to reject rows that
    disagree with their own `symbol` column. Passing them explicitly keeps both
    honest.

    `closed` is the R1 flag the target schema will store. The legacy per-coin
    tables have no `is_closed` column, so in Phase A it is accepted, validated
    for type, and NOT persisted — the value is reconstructed from open_time on
    read. It is part of the signature from day one so that phase 2 (dual-write)
    does not have to touch the ingestion call sites again.

    The `IS DISTINCT FROM` guard is carried over from insert_fast() (audit D3):
    an unchanged re-upsert must not write a new row version into the WAL.

    Does NOT commit (contract 3).
    """
    _assert_legacy_backend()
    # `closed` arrives from the Binance kline flag k['x'] and must not be a
    # truthy int — the hypertable column is boolean and would silently coerce.
    if closed is not True and closed is not False:
        raise TypeError("closed must be a bool")
    if not rows:
        return 0
    table = candles_table(symbol, tf)
    query = sql.SQL(
        "INSERT INTO {tbl} AS t (symbol, open_time, open, high, low, close, volume) VALUES %s "
        "ON CONFLICT (symbol, open_time) DO UPDATE SET "
        "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume "
        "WHERE (t.open, t.high, t.low, t.close, t.volume) "
        "IS DISTINCT FROM (EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume)"
    ).format(tbl=_ident(table))
    with conn.cursor() as cur:
        extras.execute_values(cur, query.as_string(cur), rows, page_size=page_size)
    return len(rows)


def upsert_indicators(conn: Any, df: pd.DataFrame, symbol: str, tf: str, *, page_size: int = 500) -> int:
    """Upsert an indicator DataFrame for one (symbol, tf). Returns rows sent.

    Every DataFrame column is written; `symbol` and `open_time` form the
    conflict target and are never part of the UPDATE set. Column names are
    validated as identifiers before they reach the SQL string.

    Does NOT commit (contract 3).
    """
    _assert_legacy_backend()
    if df.empty:
        return 0
    if "symbol" not in df.columns or "open_time" not in df.columns:
        raise ValueError("indicator frame needs 'symbol' and 'open_time' columns")
    table = indicators_table(symbol, tf)
    cols = list(df.columns)
    update_cols = [c for c in cols if c not in ("symbol", "open_time")]
    if not update_cols:
        raise ValueError("indicator frame carries no payload columns")

    query = sql.SQL("INSERT INTO {tbl} ({cols}) VALUES %s ON CONFLICT (symbol, open_time) DO UPDATE SET {sets}").format(
        tbl=_ident(table),
        cols=_columns_sql(cols),
        sets=sql.SQL(", ").join(
            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(_valid_col(c))) for c in update_cols
        ),
    )
    values = [tuple(x) for x in df[cols].to_numpy()]
    with conn.cursor() as cur:
        extras.execute_values(cur, query.as_string(cur), values, page_size=page_size)
    return len(values)


def _valid_col(col: str) -> str:
    if not _COLUMN_RE.match(col):
        raise ValueError(f"invalid column identifier: {col!r}")
    return col


# ── Helpers used by the migration tooling ─────────────────────────────────────


def timeframe_delta(tf: str) -> timedelta:
    """Duration of one candle of `tf`."""
    return timedelta(seconds=TF_SECONDS[validate_timeframe(tf)])


def last_closed_open_time(tf: str, now: datetime) -> datetime:
    """open_time of the newest candle that is closed at `now`."""
    return period_start(tf, now) - timeframe_delta(tf)

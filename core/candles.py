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
#    probe (28), the 5m/1h last-price reads (6_housekeeping, 29_ufi1), and the
#    health-monitor staleness canary. Analytical readers must not — the AI bots
#    detect on closed candles and take the live entry price via get_live_price
#    (or, for 12_ai_ats, the last closed close). 11_ai_mis and 12_ai_ats no
#    longer read the forming candle themselves (R1 Block 4, T-2026-CU-9050-111).
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
    """Raised when KYTHERA_CANDLES_SOURCE names a backend that does not exist."""


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


def _table_for_kind(symbol: str, tf: str, kind: str) -> str:
    """Candle or indicator table name for `kind` in {'candles', 'indicators'}."""
    if kind == "candles":
        return candles_table(symbol, tf)
    if kind == "indicators":
        return indicators_table(symbol, tf)
    raise ValueError(f"kind must be 'candles' or 'indicators', got {kind!r}")


def _parse_coin_table(name: str) -> tuple[str, str, str] | None:
    """Parse a raw table name into (symbol, tf, kind), or None if it is not a
    per-coin candle/indicator table.

    'BTCUSDT_1h'            → ('BTCUSDT', '1h', 'candles')
    'BTCUSDT_1h_indicators' → ('BTCUSDT', '1h', 'indicators')
    'active_trades_master'  → None (tf part is not a known timeframe)
    'oi_5m'                 → None (symbol part fails the uppercase regex)
    """
    kind = "candles"
    stem = name
    if stem.endswith(INDICATOR_SUFFIX):
        kind = "indicators"
        stem = stem[: -len(INDICATOR_SUFFIX)]
    idx = stem.rfind("_")
    if idx <= 0:
        return None
    sym, tf = stem[:idx], stem[idx + 1 :]
    if tf not in TF_SECONDS or not _SYMBOL_RE.match(sym):
        return None
    return (sym, tf, kind)


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


def period_start(tf: str, now: datetime, grace_seconds: float | None = None) -> datetime:
    """Open time of the period that is CURRENTLY forming at `now` (UTC).

    Every candle with `open_time < period_start(tf, now)` is closed. Pure Python
    mirror of `_period_start_sql` — used by tools/candles_parity.py and by the
    unit tests, so the SQL and the Python answer can be compared directly.

    `grace_seconds=None` reads KYTHERA_CANDLES_CLOSE_GRACE_SEC, exactly as the
    SQL does. Pass 0.0 explicitly for the un-graced boundary; the two answers
    differ once the operator sets a grace, and a "mirror" that quietly ignored
    it would be a mirror of the wrong statement.
    """
    validate_timeframe(tf)
    if now.tzinfo is None:
        raise ValueError("period_start() requires a timezone-aware datetime")
    grace = _grace_seconds() if grace_seconds is None else grace_seconds
    step = TF_SECONDS[tf]
    off = _TF_EPOCH_OFFSET.get(tf, 0)
    epoch = now.astimezone(timezone.utc).timestamp() - grace
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


# ── Backend switch (Phase 4 read-cutover) ─────────────────────────────────────
#
# KYTHERA_CANDLES_SOURCE selects the READ backend and nothing else:
#   'legacy' (default) — the ~9.3k per-coin `{SYM}_{tf}[_indicators]` tables.
#   'hyper'            — the two `candles` / `indicators` hypertables, filtered by
#                        (symbol, tf). Dormant until the operator flips the flag
#                        and restarts the fleet (docs/TIMESCALE_R1_MIGRATION.md
#                        Phase 4); rollback is the flag back to 'legacy' + restart.
#
# WRITES DO NOT BRANCH ON IT. upsert_candles/upsert_indicators and the two DELETE
# helpers always target the legacy per-coin tables; the hypertables are kept fresh
# by the separate KYTHERA_CANDLES_DUAL_WRITE mirror (which must stay ON across the
# Phase 4→5 window). So a source flip switches what the fleet READS without ever
# stopping ingestion — the writers only validate the flag (to reject a typo'd
# backend) and otherwise ignore it.
#
# The hyper read path preserves EXACT legacy semantics so the cutover is
# behaviour-neutral (byte-parity gate: backtest/test_candles_db_parity.py):
#   * the forming filter stays CLOCK-based (open_time < period_start(tf, now())),
#     NOT the is_closed column. The clock is the one both sides share; the
#     is_closed flag can lag the clock by the WS close-tick race at the boundary
#     candle, so filtering on it would drop a row the legacy read keeps — a parity
#     break. is_closed is the R1 storage contract, not a read filter in Phase 4.
#   * `tf` and `is_closed` are real hypertable columns that do NOT exist on the
#     per-coin tables, so they are EXCLUDED from every projection — a hyper read
#     returns the legacy column shape, in legacy ordinal order.

_KNOWN_CANDLE_SOURCES = ("legacy", "hyper")
_CANDLES_HYPER_TABLE = "candles"
_INDICATORS_HYPER_TABLE = "indicators"
# Present on the hypertables, absent on the legacy per-coin tables → never
# projected, so a hyper read keeps the legacy shape.
_HYPER_ONLY_COLUMNS = ("tf", "is_closed")


def _candle_source() -> str:
    """Resolve KYTHERA_CANDLES_SOURCE to a known read backend, or raise on a typo.

    Governs reads only; the write/delete helpers call this purely to reject a
    misconfigured flag, then always operate on the legacy per-coin tables.
    """
    source = os.getenv("KYTHERA_CANDLES_SOURCE", "legacy")
    if source not in _KNOWN_CANDLE_SOURCES:
        raise CandleSourceError(
            f"KYTHERA_CANDLES_SOURCE={source!r}: known backends are {_KNOWN_CANDLE_SOURCES} "
            "(docs/TIMESCALE_R1_MIGRATION.md)."
        )
    return source


def _hyper_scope(symbol: str, tf: str, alias: str | None = None) -> tuple[sql.Composable, list[Any]]:
    """`symbol = %s AND tf = %s` — the predicate that scopes a hypertable read to
    one (symbol, tf), the hyper equivalent of picking a per-coin table by name.

    Validates symbol/tf (so a hyper read rejects a bad identifier before the
    connection is touched, exactly like candles_table() does for legacy) and
    returns the composed predicate plus its params.
    """
    sym_col = sql.Identifier(alias, "symbol") if alias else sql.Identifier("symbol")
    tf_col = sql.Identifier(alias, "tf") if alias else sql.Identifier("tf")
    pred = sql.SQL("{sym} = %s AND {tf} = %s").format(sym=sym_col, tf=tf_col)
    return pred, [validate_symbol(symbol), validate_timeframe(tf)]


# ── Phase-2 dual-write (forward-only, off by default) ─────────────────────────
#
# When KYTHERA_CANDLES_DUAL_WRITE is truthy, upsert_candles/upsert_indicators
# write the `candles`/`indicators` hypertables IN ADDITION to the legacy per-coin
# tables — a second INSERT in the CALLER's transaction, committed together (so a
# crash between the two never leaves the two stores disagreeing). READS stay
# legacy until the Phase-4 cutover (KYTHERA_CANDLES_SOURCE), so this is invisible
# to every reader; the hypertables just start accumulating the forward stream.
# Read at call time (like KYTHERA_CANDLES_SOURCE) so a per-process flip needs no
# reimport. Backfilling the pre-flag history is a separate one-shot (tools/).


def _dual_write_enabled() -> bool:
    return os.getenv("KYTHERA_CANDLES_DUAL_WRITE", "").strip().lower() in ("1", "true", "yes", "on")


# ── Phase-5 write-primary cutover (reversible, off by default) ────────────────
#
# KYTHERA_CANDLES_WRITE_PRIMARY selects which store the WRITE helpers treat as
# primary:
#   'legacy' (default) — write the ~9.3k per-coin tables; mirror the hypertables
#                        when KYTHERA_CANDLES_DUAL_WRITE is on. Today's behaviour,
#                        byte-for-byte.
#   'hyper'            — write the `candles`/`indicators` hypertables as the PRIMARY
#                        store and SKIP the per-coin write entirely (DUAL_WRITE
#                        becomes moot). The C-Gate Phase-5 perf-trial mode: reads are
#                        already on the hypertables (KYTHERA_CANDLES_SOURCE=hyper), so
#                        this stops maintaining the per-coin sprawl and lets the
#                        write/WAL/storage gain be measured before the tables drop.
# Read at call time (like the read/dual-write flags) so a per-process flip needs no
# reimport; the flip itself is an operator decision (.env + fleet restart).
# ROLLBACK ASYMMETRY: flipping back to 'legacy' resumes per-coin writes, but the
# per-coin tables went UNWRITTEN during the hyper window → they carry a gap. A
# read-cutover back to legacy therefore needs a backfill of that gap first; the
# hyper store, being continuously written, never needs one.
_KNOWN_WRITE_PRIMARIES = ("legacy", "hyper")


def _write_primary() -> str:
    """Resolve KYTHERA_CANDLES_WRITE_PRIMARY to the primary write backend, or raise
    on a typo (same fail-fast contract as _candle_source)."""
    primary = os.getenv("KYTHERA_CANDLES_WRITE_PRIMARY", "legacy")
    if primary not in _KNOWN_WRITE_PRIMARIES:
        raise CandleSourceError(
            f"KYTHERA_CANDLES_WRITE_PRIMARY={primary!r}: known write backends are {_KNOWN_WRITE_PRIMARIES} "
            "(docs/TIMESCALE_R1_MIGRATION.md)."
        )
    return primary


# Fixed table name → plain SQL string is injection-safe; the row values go through
# execute_values. is_closed is part of both the SET and the IS DISTINCT FROM guard
# so a forming→closed re-upsert (same OHLCV, flag flips true) still writes, while a
# genuinely unchanged re-upsert stays a no-op (no WAL churn — audit D3).
_CANDLES_HYPER_UPSERT = (
    "INSERT INTO candles AS t "
    "(symbol, tf, open_time, open, high, low, close, volume, is_closed) VALUES %s "
    "ON CONFLICT (symbol, tf, open_time) DO UPDATE SET "
    "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
    "close=EXCLUDED.close, volume=EXCLUDED.volume, is_closed=EXCLUDED.is_closed "
    "WHERE (t.open, t.high, t.low, t.close, t.volume, t.is_closed) "
    "IS DISTINCT FROM "
    "(EXCLUDED.open, EXCLUDED.high, EXCLUDED.low, EXCLUDED.close, EXCLUDED.volume, EXCLUDED.is_closed)"
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
    *,
    scope: tuple[sql.Composable, Sequence[Any]] | None = None,
) -> tuple[sql.Composable, list[Any]]:
    """SELECT the newest `limit` rows of the window, returned ASC.

    The LIMIT has to bite on a DESC ordering (newest N candles), the result has
    to arrive ASC (contract 1) — hence the wrapping subselect.

    `scope` is the hypertable `symbol = %s AND tf = %s` predicate (with its
    params); None → the legacy per-coin table, whose name already encodes both.
    Either way the open_time window, forming filter and DESC→ASC wrapping are
    byte-identical.
    """
    params: list[Any] = []
    if scope is not None:
        pred, scope_params = scope
        where = sql.SQL("WHERE ") + pred
        params.extend(scope_params)
    else:
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


# Default headroom multiplier and floor for history_start (T-2026-CU-9050-180).
# The read helpers return the newest `limit` closed candles via ORDER BY DESC
# LIMIT, so any lower `start` bound that leaves >= limit closed candles in
# [start, anchor] yields byte-identical rows. safety/min_days pick a window that
# does so with generous margin against gaps (weekends, thin listings, delistings)
# while still letting TimescaleDB exclude the bulk of the candles/indicators
# chunks (measured: an unbounded read scans all 126 chunks; a 30-day bound ~5).
_HISTORY_SAFETY = 3
_HISTORY_MIN_DAYS = 60


def history_start(
    tf: str,
    n_candles: int,
    *,
    anchor: datetime | None = None,
    safety: int = _HISTORY_SAFETY,
    min_days: int = _HISTORY_MIN_DAYS,
) -> datetime:
    """Lower `start` bound covering the newest `n_candles` closed candles of `tf`.

    Returns ``anchor - max(n_candles * TF_SECONDS[tf] * safety, min_days)`` as a
    timezone-aware UTC datetime. Purpose is purely a TimescaleDB chunk-exclusion
    hint for the hot ``read_candles_with_indicators`` call sites: passing this as
    ``start=`` prunes chunks without changing the returned rows, PROVIDED the
    window still holds at least ``n_candles`` closed candles — which the safety
    multiplier and the ``min_days`` floor guarantee for any coin trading at more
    than ``1/safety`` of the wall-clock cadence over ``min_days``.

    Parity caveat (why the window is sized this generously): a coin with MORE
    than ``n_candles`` of total history but so many gaps that its newest
    ``n_candles`` span more than the window would return fewer rows here than an
    unbounded read, and a feature computed from the frame's first row (e.g. the
    ATS OBV baseline) would then shift. The per-site minimum-row guards sit below
    ``n_candles`` (they accept genuinely short-history/new coins on purpose), so
    such a coin is NOT skipped — it is scored on a shorter frame. Reaching this
    requires >``(safety-1)/safety`` of the candles missing across ``min_days``
    (>41 days of holes in 500 hourly candles at safety=3), which a listed
    Binance-futures pair — emitting contiguous klines every interval while
    listed — cannot produce; the largest observed ingestion outage (~14h) is
    three orders of magnitude too small. The margin, not a runtime guard, is the
    protection. `anchor` defaults to now (UTC); an as-of read passes its `end`.
    """
    validate_timeframe(tf)
    base = anchor if anchor is not None else datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    span_seconds = max(n_candles * TF_SECONDS[tf] * safety, min_days * 86400)
    return base - timedelta(seconds=span_seconds)


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
    source = _candle_source()
    _require_open_time(columns)
    if source == "hyper":
        # `SELECT *` on the hypertable would leak the tf/is_closed columns the
        # per-coin tables never had; None → the explicit legacy candle shape
        # (CANDLE_COLUMNS == the per-coin table's `SELECT *` ordinal order).
        proj = _columns_sql(columns if columns is not None else CANDLE_COLUMNS)
        query, params = _windowed_select(
            _CANDLES_HYPER_TABLE, proj, tf, include_forming, start, end, limit, scope=_hyper_scope(symbol, tf)
        )
    else:
        query, params = _windowed_select(
            candles_table(symbol, tf), _columns_sql(columns), tf, include_forming, start, end, limit
        )
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
    source = _candle_source()
    _require_open_time(columns)
    if source == "hyper":
        scope = _hyper_scope(symbol, tf)  # validates symbol/tf before touching conn
        # `SELECT *` on the hypertable would leak the tf/is_closed columns and thus
        # break shape parity; expand `columns=None` to the explicit legacy list.
        proj = list(columns) if columns is not None else indicator_column_names(conn, symbol, tf)
        query, params = _windowed_select(
            _INDICATORS_HYPER_TABLE, _columns_sql(proj), tf, include_forming, start, end, limit, scope=scope
        )
    else:
        query, params = _windowed_select(
            indicators_table(symbol, tf), _columns_sql(columns), tf, include_forming, start, end, limit
        )
    return _fetch_df(conn, query, params)


def _hyper_side_subquery(
    table: str,
    tf: str,
    symbol: str,
    include_forming: bool,
    start: datetime | None,
    end: datetime | None,
) -> tuple[sql.Composable, list[Any]]:
    """`(SELECT * FROM <hypertable> WHERE symbol=%s AND tf=%s [window] [forming] OFFSET 0)`.

    The trailing `OFFSET 0` is an optimization fence: it stops the subquery from
    being pulled up, so its ordered-append hypertable path is not visible to the
    join above it. Both join sides get one, which is what keeps the two-hypertable
    join off TimescaleDB's buggy merge-over-ordered-append path (see
    _read_joined_hyper). Validates symbol/tf and returns the subquery plus params.
    """
    scope_pred, params = _hyper_scope(symbol, tf)  # one source of the (symbol, tf) predicate + validation
    where = sql.SQL("WHERE ") + scope_pred
    if start is not None:
        where += sql.SQL(" AND open_time >= %s")
        params.append(start)
    if end is not None:
        where += sql.SQL(" AND open_time <= %s")
        params.append(end)
    where += _forming_filter(tf, include_forming)
    return sql.SQL("(SELECT * FROM {t} {w} OFFSET 0)").format(t=_ident(table), w=where), params


def _read_joined_hyper(
    conn: Any,
    symbol: str,
    tf: str,
    ccols: sql.Composable,
    icols: sql.Composable,
    limit: int | None,
    start: datetime | None,
    end: datetime | None,
    include_forming: bool,
) -> pd.DataFrame:
    """read_candles_with_indicators on the hypertables.

    Unlike the per-coin join — whose two tables already encode (symbol, tf) — both
    sides here are the SAME two hypertables, so each is fenced in its own
    (symbol, tf)- and window-scoped `(SELECT … OFFSET 0)` subquery. The fence is
    load-bearing: joining two hypertables on the partitioning column lets
    TimescaleDB choose a merge join over its ordered-append paths, which raises the
    server-side error `mergejoin input data is out of order`. Fencing both sides
    removes those ordered paths, so any merge the planner still picks sits on a
    genuine Sort. The join is on open_time alone (both sides already scoped to one
    symbol/tf), reproducing the legacy per-coin join byte-for-byte.
    """
    h_sub, hp = _hyper_side_subquery(_CANDLES_HYPER_TABLE, tf, symbol, include_forming, start, end)
    i_sub, ip = _hyper_side_subquery(_INDICATORS_HYPER_TABLE, tf, symbol, include_forming, start, end)
    inner = sql.SQL(
        "SELECT {ccols}, {icols} FROM {h} h LEFT JOIN {i} i ON h.open_time = i.open_time ORDER BY h.open_time DESC"
    ).format(ccols=ccols, icols=icols, h=h_sub, i=i_sub)
    params = hp + ip
    if limit is not None:
        inner += sql.SQL(" LIMIT %s")
        params.append(int(limit))
    query = sql.SQL("SELECT * FROM ({inner}) s ORDER BY open_time ASC").format(inner=inner)
    return _fetch_df(conn, query, params)


def read_candles_with_indicators(
    conn: Any,
    symbol: str,
    tf: str,
    *,
    limit: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    include_forming: bool = False,
    candle_columns: Sequence[str] | None = CANDLE_COLUMNS,
    indicator_columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Candles LEFT JOIN indicators on open_time, ascending.

    This is the dominant AI-bot read pattern (bots 11, 12, 15, 24, 25 and
    core/research_features). It stays one SQL statement so the migration does
    not turn one query into two round-trips per coin.

    `end` is what an as-of read needs: 15_ai_master_bot reads the newest joined
    row strictly before a floored timestamp, and every replay/backfill path
    reads a historical window. Without it those call sites could not migrate.

    Duplicate column names (`symbol`, `close`, `open_time` exist on both sides)
    are resolved in favour of the candle table; the indicator side is projected
    without them. `indicator_columns=None` therefore costs one catalog lookup —
    an unqualified `i.*` would hand the caller a DataFrame with three duplicate
    column labels, which pandas resolves by position, silently.
    """
    source = _candle_source()
    _require_open_time(candle_columns)
    # Validate up front (both backends) so a bad symbol/tf raises before the
    # indicator_column_names() catalog probe ever touches the connection.
    validate_symbol(symbol)
    validate_timeframe(tf)
    if indicator_columns is None:
        indicator_columns = indicator_column_names(conn, symbol, tf)
    indicator_columns = [c for c in indicator_columns if c not in ("symbol", "open_time", "close")]
    if not indicator_columns:
        raise ValueError(f"no indicator columns to join for {symbol}_{tf}")

    icols = _columns_sql(indicator_columns, prefix="i")
    if source == "hyper":
        # None → explicit legacy candle shape, else `h.*` would leak tf/is_closed
        # from the hypertable (mirrors read_candles / read_indicators).
        hyper_candle_cols = candle_columns if candle_columns is not None else CANDLE_COLUMNS
        ccols = _columns_sql(hyper_candle_cols, prefix="h")
        return _read_joined_hyper(conn, symbol, tf, ccols, icols, limit, start, end, include_forming)

    ccols = _columns_sql(candle_columns, prefix="h")
    params: list[Any] = []
    inner = sql.SQL(
        "SELECT {ccols}, {icols} FROM {ctab} h LEFT JOIN {itab} i ON h.open_time = i.open_time WHERE true"
    ).format(
        ccols=ccols,
        icols=icols,
        ctab=_ident(candles_table(symbol, tf)),
        itab=_ident(indicators_table(symbol, tf)),
    )
    if start is not None:
        inner += sql.SQL(" AND h.open_time >= %s")
        params.append(start)
    if end is not None:
        inner += sql.SQL(" AND h.open_time <= %s")
        params.append(end)
    inner += _forming_filter(tf, include_forming, alias="h")
    inner += sql.SQL(" ORDER BY h.open_time DESC")
    if limit is not None:
        inner += sql.SQL(" LIMIT %s")
        params.append(int(limit))
    query = sql.SQL("SELECT * FROM ({inner}) s ORDER BY open_time ASC").format(inner=inner)
    return _fetch_df(conn, query, params)


def latest_open_time(
    conn: Any, symbol: str, tf: str, *, include_forming: bool = True, kind: str = "candles"
) -> datetime | None:
    """MAX(open_time) of the candle table, or None if the table does not exist.

    Default `include_forming=True` keeps the ingestion catch-up/resume semantics
    of 1_data_ingestion.get_latest_open_time() byte-equal: it resumes from the
    newest row it wrote, forming or not.

    `kind='indicators'` reads the indicator table instead — the resume watermark
    2_indicator_engine.process_coin_task needs (its old `SELECT MAX(open_time)
    FROM {sym}_{tf}_indicators`). Same forming semantics; the indicator table
    only ever holds rows the engine wrote, so include_forming=True is a plain MAX.
    """
    source = _candle_source()
    if source == "hyper":
        if kind not in ("candles", "indicators"):
            raise ValueError(f"kind must be 'candles' or 'indicators', got {kind!r}")
        pred, params = _hyper_scope(symbol, tf)  # validates symbol/tf
        table = _INDICATORS_HYPER_TABLE if kind == "indicators" else _CANDLES_HYPER_TABLE
        # No table_exists probe: the hypertable always exists, and MAX over an
        # absent (symbol, tf) returns NULL → None, the same "no data" answer the
        # legacy missing-table check gives.
        query = sql.SQL("SELECT MAX(open_time) FROM {tbl} WHERE ").format(tbl=_ident(table)) + pred
        query += _forming_filter(tf, include_forming)
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
    else:
        table = _table_for_kind(symbol, tf, kind)
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

    Phase-agnostic on purpose: it probes the per-coin RELATION, which still exists
    under either KYTHERA_CANDLES_SOURCE until the Phase-5 table drop, so no hyper
    branch is needed (and reconstructing it from the 40M-row hypertable would be a
    needless full scan). Once Phase 5 drops the per-coin tables it returns False —
    which is exactly "no data". No hyper reader depends on it: latest_open_time's
    hyper path skips the probe entirely (MAX over an absent coin is already NULL).
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

    Hyper: the one shared `indicators` hypertable carries two extra columns the
    per-coin tables never had (`tf`, `is_closed`); they are dropped so the returned
    list is byte-equal to the legacy per-coin catalog — same names, same ordinal
    order (hyper prepends `tf` after `symbol` and inserts `is_closed` before
    `close`, so removing exactly those two restores symbol, open_time, close, …).
    """
    if _candle_source() == "hyper":
        validate_symbol(symbol)
        validate_timeframe(tf)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s "
                "ORDER BY ordinal_position",
                (_INDICATORS_HYPER_TABLE,),
            )
            cols = [r[0] for r in cur.fetchall()]
        return [col for col in cols if col not in _HYPER_ONLY_COLUMNS]
    with conn.cursor() as cur:
        cur.execute(
            # Schema-qualified: an identically named table in another schema on
            # the search_path would contribute phantom columns to the JOIN.
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "ORDER BY ordinal_position",
            (indicators_table(symbol, tf),),
        )
        return [r[0] for r in cur.fetchall()]


def list_coin_tables(conn: Any, tf: str | None = None, *, kind: str | None = None) -> list[tuple[str, str, str]]:
    """Enumerate the per-coin candle/indicator tables in the current schema.

    Returns (symbol, tf, kind) tuples — kind in {'candles', 'indicators'} — for
    every base table whose name parses as '{SYMBOL}_{tf}' or
    '{SYMBOL}_{tf}_indicators' with a regex-valid symbol and a known timeframe.
    Non-candle tables (active_trades_master, telegram_outbox, funding_rates,
    oi_5m, regime_history, …) never match the pattern, so no substring blacklist
    is needed — the shape is the filter.

    Read-only. Replaces the raw `information_schema.tables` scans (6_housekeeping
    retention/delisted-scan; the audit tools stay raw for now). `tf` restricts to
    one timeframe, `kind` to 'candles' or 'indicators'.

    Phase-agnostic on purpose (like table_exists): it enumerates the per-coin
    RELATIONS, which exist under either KYTHERA_CANDLES_SOURCE until the Phase-5
    drop — so no hyper branch. Reconstructing this from the hypertables would mean
    a `SELECT DISTINCT symbol, tf` over ~40M rows (measured >20 s, the chunk
    partitioning defeats even a loose index scan), which would stall the caller
    (6_housekeeping retention). The retention caller also DELETES from the per-coin
    tables in hyper-read mode (writes stay legacy), so the relation list is exactly
    what it needs. Once Phase 5 drops the per-coin tables this returns an empty list.
    """
    _candle_source()  # reject a typo'd backend; the per-coin relations back this in every phase
    if tf is not None:
        validate_timeframe(tf)
    if kind is not None and kind not in ("candles", "indicators"):
        raise ValueError(f"kind must be 'candles', 'indicators' or None, got {kind!r}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        )
        names = [r[0] for r in cur.fetchall()]
    out: list[tuple[str, str, str]] = []
    for name in names:
        parsed = _parse_coin_table(name)
        if parsed is None:
            continue
        if tf is not None and parsed[1] != tf:
            continue
        if kind is not None and parsed[2] != kind:
            continue
        out.append(parsed)
    return out


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
    _candle_source()  # validate the read-backend flag; the write backend follows _write_primary() below
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
    primary = _write_primary()
    if primary == "legacy":
        with conn.cursor() as cur:
            extras.execute_values(cur, query.as_string(cur), rows, page_size=page_size)
    # Hyper write: the PRIMARY store when write-primary=hyper (legacy skipped), else
    # the dual-write mirror. rows: (symbol, open_time, open, high, low, close, volume)
    # → the hypertable adds tf (position 1) and the R1 is_closed flag (last). Same
    # transaction as any legacy write (a crash never splits the two stores).
    if primary == "hyper" or _dual_write_enabled():
        hyper_rows = [(r[0], tf, r[1], r[2], r[3], r[4], r[5], r[6], closed) for r in rows]
        with conn.cursor() as cur:
            extras.execute_values(cur, _CANDLES_HYPER_UPSERT, hyper_rows, page_size=page_size)
    return len(rows)


def candles_write_primary() -> str:
    """Resolved write-primary backend ('legacy' | 'hyper'), fail-fast on typos.

    Public accessor for callers that pick a write path per batch (the ingestion
    flusher routes hyper primaries onto :func:`upsert_candles_many`). Read at
    call time like every other KYTHERA_CANDLES_* flag — no reimport needed.
    """
    return _write_primary()


def upsert_candles_many(
    conn: Any,
    rows: Sequence[Sequence[Any]],
    *,
    page_size: int = 500,
) -> int:
    """Bulk upsert of mixed-(symbol, tf) candle rows into the `candles` hypertable.

    ``rows``: (symbol, tf, open_time, open, high, low, close, volume, closed) —
    the :func:`upsert_candles` row shape with symbol/tf/closed carried PER ROW
    instead of per call, exactly the tuple order of ``_CANDLES_HYPER_UPSERT``.
    One ``execute_values`` round-trip replaces one statement per candle
    (T-2026-CU-9050-169: the ingestion flusher was measured at ~3.185 single
    INSERTs/s plus per-row SAVEPOINT/RELEASE traffic).

    Hyper-only by design: the legacy backend shards rows over ~9.3k per-coin
    tables, so a cross-symbol batch has no single-statement equivalent there —
    raises :class:`CandleSourceError` when the write primary is 'legacy'
    (callers keep the per-(symbol, tf) path in that case).

    Same statement and therefore the same `IS DISTINCT FROM` no-op guard and
    forming→closed flip semantics as the single-call path — the DB end state of
    one bulk call is identical to the concatenation of per-(symbol, tf, closed)
    :func:`upsert_candles` calls over the same rows.

    Caller contract: (symbol, tf, open_time) must be unique within ``rows`` —
    ON CONFLICT cannot update the same row twice inside one statement (the
    ingestion buffer guarantees this via its dict key). Does NOT commit
    (contract 3).
    """
    _candle_source()  # validate the read-backend flag; the write backend follows _write_primary() below
    if _write_primary() != "hyper":
        raise CandleSourceError(
            "upsert_candles_many is hyper-only (write-primary='legacy' has no cross-symbol "
            "batch equivalent) — use per-(symbol, tf) upsert_candles instead."
        )
    if not rows:
        return 0
    for r in rows:
        # Same guard as upsert_candles: the boolean column must not coerce a
        # truthy int, and a wrong flag would silently flip is_closed semantics.
        if r[8] is not True and r[8] is not False:
            raise TypeError("closed (row position 8) must be a bool")
    with conn.cursor() as cur:
        extras.execute_values(cur, _CANDLES_HYPER_UPSERT, rows, page_size=page_size)
    return len(rows)


def upsert_indicators(conn: Any, df: pd.DataFrame, symbol: str, tf: str, *, page_size: int = 500) -> int:
    """Upsert an indicator DataFrame for one (symbol, tf). Returns rows sent.

    Every DataFrame column is written; `symbol` and `open_time` form the
    conflict target and are never part of the UPDATE set. Column names are
    validated as identifiers before they reach the SQL string.

    Does NOT commit (contract 3).
    """
    _candle_source()  # validate the read-backend flag; the write backend follows _write_primary() below
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
    primary = _write_primary()
    if primary == "legacy":
        with conn.cursor() as cur:
            extras.execute_values(cur, query.as_string(cur), values, page_size=page_size)
    # Hyper write: PRIMARY when write-primary=hyper (legacy skipped), else the mirror.
    if primary == "hyper" or _dual_write_enabled():
        _upsert_indicators_hyper(conn, df, tf, page_size=page_size)
    return len(values)


def _upsert_indicators_hyper(conn: Any, df: pd.DataFrame, tf: str, *, page_size: int) -> None:
    """Dual-write the indicator frame into the `indicators` hypertable.

    The frame carries `symbol`, `open_time`, `close` and the indicator columns but
    no `tf`/`is_closed`; both are added here. **is_closed is always True**: post-R1
    the engine computes indicators only on CLOSED candles
    (`read_candles(include_forming=False)`, Block 6 Part 1), so every persisted
    indicator row belongs to a closed candle. Columns are re-selected by name so
    the value order is independent of the frame's incoming column order. Runs in
    the caller's transaction; does not commit.
    """
    payload_cols = [c for c in df.columns if c not in ("symbol", "open_time")]
    insert_cols = ["symbol", "tf", "open_time", "is_closed", *payload_cols]
    update_cols = ["is_closed", *payload_cols]  # symbol/tf/open_time are the conflict key
    query = sql.SQL(
        "INSERT INTO indicators ({cols}) VALUES %s ON CONFLICT (symbol, tf, open_time) DO UPDATE SET {sets}"
    ).format(
        cols=_columns_sql(insert_cols),
        sets=sql.SQL(", ").join(
            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(_valid_col(c))) for c in update_cols
        ),
    )
    values = [(r[0], tf, r[1], True, *r[2:]) for r in df[["symbol", "open_time", *payload_cols]].to_numpy()]
    with conn.cursor() as cur:
        extras.execute_values(cur, query.as_string(cur), values, page_size=page_size)


def _valid_col(col: str) -> str:
    if not _COLUMN_RE.match(col):
        raise ValueError(f"invalid column identifier: {col!r}")
    return col


def delete_candles_before(conn: Any, symbol: str, tf: str, cutoff: datetime, *, kind: str = "candles") -> int:
    """DELETE rows older than `cutoff` (open_time < cutoff), returning the count.

    Serves 6_housekeeping.clean_old_database_entries (retention): it prunes the
    per-coin candle table — or, with kind='indicators', the indicator table — of
    everything older than the timeframe's retention window. The boundary is
    exclusive (`<`), matching the previous `WHERE open_time < NOW() - INTERVAL`.
    The caller computes `cutoff` (so the calendar-interval arithmetic stays in
    the DB) and passes a timezone-aware datetime.

    Does NOT commit (contract 3).
    """
    _candle_source()  # validate the read-backend flag; delete is NOT part of the write-primary cutover — always the legacy per-coin table
    if cutoff.tzinfo is None:
        raise ValueError("delete_candles_before() requires a timezone-aware cutoff")
    table = _table_for_kind(symbol, tf, kind)
    query = sql.SQL("DELETE FROM {tbl} WHERE open_time < %s").format(tbl=_ident(table))
    with conn.cursor() as cur:
        cur.execute(query, (cutoff,))
        return cur.rowcount


def delete_indicators_from(conn: Any, symbol: str, tf: str, start: datetime) -> int:
    """DELETE indicator rows at or after `start` (open_time >= start), returning the count.

    Serves 6_housekeeping.fill_ohlcv_gaps_and_invalidate_indicators: once a gap
    is back-filled, every indicator row from the first gap onward is invalidated
    so the engine recomputes it with a clean warmup. The boundary is inclusive
    (`>=`), matching the previous `DELETE ... WHERE open_time >= %s`. This is the
    opposite direction to delete_candles_before — deliberately a separate name.

    Does NOT commit (contract 3).
    """
    _candle_source()  # validate the read-backend flag; delete is NOT part of the write-primary cutover — always the legacy per-coin table
    if start.tzinfo is None:
        raise ValueError("delete_indicators_from() requires a timezone-aware start")
    table = indicators_table(symbol, tf)
    query = sql.SQL("DELETE FROM {tbl} WHERE open_time >= %s").format(tbl=_ident(table))
    with conn.cursor() as cur:
        cur.execute(query, (start,))
        return cur.rowcount


# ── Helpers used by the migration tooling ─────────────────────────────────────


def timeframe_delta(tf: str) -> timedelta:
    """Duration of one candle of `tf`."""
    return timedelta(seconds=TF_SECONDS[validate_timeframe(tf)])


def last_closed_open_time(tf: str, now: datetime) -> datetime:
    """open_time of the newest candle that is closed at `now`."""
    return period_start(tf, now) - timeframe_delta(tf)

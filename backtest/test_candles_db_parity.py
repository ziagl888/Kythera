# backtest/test_candles_db_parity.py
"""
Phase-0 gate of docs/TIMESCALE_R1_MIGRATION.md §3, made executable:

    "Unit-Smoke: API-Reads byte-gleich zu Direkt-SQL"

The point of core/candles.py is that ~108 call sites can move off hand-rolled
f-string table queries onto one API *without changing what the database
returns*. This module proves that equivalence against the OLD per-coin tables:
for a real (symbol, tf), the rows core.candles returns are byte-equal — same
rows, same order, same values — to the direct SQL a bot author would have
written by hand.

TWO LAYERS, by design (same split as tools/candles_parity.py)
------------------------------------------------------------
* The canonicalisation core (`canonical_rows`, `frame_to_rows`) is pure and
  DB-free. Its own tests run on the build machine and in CI — they guard the
  comparison harness itself, so a green DB run on the VPS cannot be a false
  pass caused by a broken comparator.
* The parity tests need a live database and the populated per-coin tables, so
  they only exist on the VPS. Without DB credentials (the build machine, harte
  Regel 1) the `conn` fixture calls `pytest.skip` — it never fabricates a pass.

Run on the VPS (this closes the Phase-0 gate, previously "offen — VPS" in
docs/CANDLE_CALL_SITES.md §6):

    py -3.13 -m pytest backtest/test_candles_db_parity.py -v

Build machine (only the DB-free comparator tests run, the rest skip):

    py -3.13 -m pytest backtest/test_candles_db_parity.py -v
"""

from __future__ import annotations

import contextlib
import math
import os
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest
from psycopg2 import sql

from core import candles as c

# ── DB-free canonicalisation core (self-tested below) ─────────────────────────
#
# The API returns a pandas DataFrame; direct SQL returns raw psycopg2 tuples.
# Both carry the SAME database values, but through different type paths: pandas
# turns datetimes into Timestamps and represents SQL NULL as NaN rather than
# None, and float4/float8 can print with differing trailing digits. A raw `==`
# would flag those representation differences as drift. Canonicalisation
# collapses each value to a driver-independent string so the comparison sees the
# value, not the container it arrived in. It deliberately keeps int and float
# distinct (see the self-test) — it reconciles float-representation noise, not a
# genuine int-vs-float type difference.

_MISSING = "∅"  # ∅ — a single token for both None and NaN
_FLOAT_FMT = "{:.12g}"  # matches tools/candles_parity.canonical_row (P3.12 noise floor)


def canonical_cell(value: Any) -> str:
    """One value → a stable string, regardless of the driver that produced it."""
    if value is None:
        return _MISSING
    if isinstance(value, datetime):
        # psycopg2 hands back tz-aware datetimes; normalise to a UTC epoch so a
        # session-TimeZone difference between two connections cannot show up.
        return "t" + str(int(round(value.astimezone(timezone.utc).timestamp() * 1_000_000)))
    if isinstance(value, bool):
        return "b1" if value else "b0"
    if isinstance(value, Decimal):
        value = float(value)
    # numpy scalars (np.float64/np.int64) satisfy the real/integer ABCs below
    # via their Python duck-types once coerced through float()/int().
    if isinstance(value, float):
        if math.isnan(value):
            return _MISSING
        return _FLOAT_FMT.format(value)
    if isinstance(value, int):
        return "i" + str(value)
    if hasattr(value, "item"):  # numpy scalar → its Python scalar, then retry
        return canonical_cell(value.item())
    return "s" + str(value)


def canonical_rows(rows: Sequence[Sequence[Any]]) -> list[tuple[str, ...]]:
    """A row sequence → canonical, order-preserving tuples."""
    return [tuple(canonical_cell(v) for v in row) for row in rows]


def canonical_cell_f4(value: Any) -> str:
    """Like canonical_cell, but reals are compared at float4 (REAL) precision.

    The legacy per-coin indicator columns are REAL (float4, ~7 significant
    digits); the hyper `indicators` columns are `double precision` (P3.12 /
    D-2026-CLD-109 #2), so a forward dual-written row carries the engine's fuller
    float64 in the hypertable and the float4-rounded value in legacy — a real,
    intended precision UPGRADE, not drift. Casting both sides to float32
    reproduces the legacy REAL bit-for-bit (round-to-nearest-even, same on the
    PG float8→float4 and numpy float64→float32 paths), so this compares the
    precision the two backends genuinely share. It is NOT a repr fudge: a value
    that actually differs at float4 resolution still differs here.
    """
    import numpy as np

    if value is None:
        return _MISSING
    if isinstance(value, (datetime, bool)):
        return canonical_cell(value)
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        if math.isnan(value):
            return _MISSING
        return "f4:" + repr(float(np.float32(value)))
    if isinstance(value, int):
        return "i" + str(value)
    if hasattr(value, "item"):
        return canonical_cell_f4(value.item())
    return "s" + str(value)


def canonical_rows_f4(rows: Sequence[Sequence[Any]]) -> list[tuple[str, ...]]:
    """canonical_rows at float4 precision — for legacy-REAL vs hyper-double columns."""
    return [tuple(canonical_cell_f4(v) for v in row) for row in rows]


def frame_to_rows(df: Any, columns: Sequence[str]) -> list[tuple[Any, ...]]:
    """DataFrame → list of row tuples in `columns` order (nothing else touched)."""
    return [tuple(row) for row in df[list(columns)].itertuples(index=False, name=None)]


# ── DB-free tests of the comparator itself (run everywhere) ────────────────────


def test_canonical_cell_is_representation_independent():
    ts = datetime(2026, 7, 9, 14, tzinfo=timezone.utc)
    tokyo = ts.astimezone(timezone(__import__("datetime").timedelta(hours=9)))
    assert canonical_cell(ts) == canonical_cell(tokyo)  # same instant, different tz
    assert canonical_cell(None) == canonical_cell(float("nan"))  # NULL both ways
    assert canonical_cell(5) == canonical_cell(5)
    assert canonical_cell(Decimal("1.5")) == canonical_cell(1.5)  # numeric vs float col
    assert canonical_cell(5) != canonical_cell(5.0)  # int and float stay distinguishable
    # Below the 12-significant-digit floor is not a difference (REAL vs double, P3.12);
    # above it is.
    assert canonical_cell(1.5) == canonical_cell(1.5 + 1e-13)
    assert canonical_cell(1.5) != canonical_cell(1.51)


def test_canonical_cell_f4_reconciles_real_vs_double_but_not_real_drift():
    import numpy as np

    # a value the engine computed as float64; legacy stored only its float4 rounding
    v = 44.037921905517578
    legacy_real = float(np.float32(v))  # what psycopg2 hands back from a REAL column
    assert canonical_cell_f4(v) == canonical_cell_f4(legacy_real)  # double vs REAL of same value
    # a genuine difference at float4 resolution is still caught (not masked)
    assert canonical_cell_f4(44.04) != canonical_cell_f4(44.05)
    assert canonical_cell_f4(None) == canonical_cell_f4(float("nan"))
    assert canonical_cell_f4(Decimal("1.5")) == canonical_cell_f4(1.5)


def test_canonical_rows_is_order_sensitive():
    a = [(1, 2.0), (3, 4.0)]
    assert canonical_rows(a) == canonical_rows(a)
    assert canonical_rows(a) != canonical_rows(list(reversed(a)))


def test_frame_to_rows_selects_requested_columns_in_order():
    import pandas as pd  # a hard prerequisite anyway — core.candles imports it at module load

    df = pd.DataFrame({"open_time": [1, 2], "close": [10.0, 11.0], "junk": [0, 0]})
    assert frame_to_rows(df, ["open_time", "close"]) == [(1, 10.0), (2, 11.0)]


# ── DB fixtures (VPS only; skip cleanly without credentials) ───────────────────

_DIRECT_CANDLE_COLS = ("symbol", "open_time", "open", "high", "low", "close", "volume")
_TF_CANDIDATES = ("1h", "4h", "1d")
_SYMBOL_CANDIDATES = ("BTCUSDT", "ETHUSDT")
_MIN_ROWS = 60  # enough closed candles that limit/window tests are meaningful


@pytest.fixture(scope="module")
def conn():
    """A live pooled connection, or skip. Never fabricates a database."""
    try:
        from core.database import db_connection
    except Exception as exc:  # missing DB_PASSWORD / import-time config on build machine
        pytest.skip(f"no database configuration: {exc}")
    try:
        with db_connection() as connection:
            yield connection
    except Exception as exc:  # pool/connect failure → this is not a VPS session
        pytest.skip(f"database not reachable: {exc}")


def _db_now(conn: Any) -> datetime:
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        now = cur.fetchone()[0]
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def probe(conn):
    """Pick a real (symbol, tf) whose candle table has enough closed rows."""
    now = _db_now(conn)
    for symbol in _SYMBOL_CANDIDATES:
        for tf in _TF_CANDIDATES:
            table = c.candles_table(symbol, tf)
            if not c.table_exists(conn, table):
                continue
            cutoff = c.period_start(tf, now)
            query = sql.SQL("SELECT count(*) FROM {tbl} WHERE open_time < %s").format(tbl=sql.Identifier(table))
            with conn.cursor() as cur:
                cur.execute(query, (cutoff,))
                if cur.fetchone()[0] >= _MIN_ROWS:
                    return symbol, tf
    pytest.skip(f"no candidate table with ≥{_MIN_ROWS} closed rows among {_SYMBOL_CANDIDATES}×{_TF_CANDIDATES}")


def _closed_window(conn: Any, tf: str, periods: int) -> tuple[datetime, datetime]:
    """[start, end] over fully-closed candles only (end = newest closed open_time)."""
    now = _db_now(conn)
    end = c.last_closed_open_time(tf, now)
    start = end - periods * c.timeframe_delta(tf)
    return start, end


def _direct_candles(conn: Any, symbol: str, tf: str, start: datetime, end: datetime) -> list[tuple]:
    table = c.candles_table(symbol, tf)
    query = sql.SQL(
        "SELECT symbol, open_time, open, high, low, close, volume FROM {tbl} "
        "WHERE open_time >= %s AND open_time <= %s ORDER BY open_time ASC"
    ).format(tbl=sql.Identifier(table))
    with conn.cursor() as cur:
        cur.execute(query, (start, end))
        return cur.fetchall()


# ── The Phase-0 gate: API reads == direct SQL ─────────────────────────────────


def test_read_candles_is_byte_equal_to_direct_sql(conn, probe):
    """The core gate: the wrapper returns exactly what the hand-written SELECT does."""
    symbol, tf = probe
    start, end = _closed_window(conn, tf, 200)
    api = c.read_candles(conn, symbol, tf, start=start, end=end, include_forming=True, columns=_DIRECT_CANDLE_COLS)
    direct = _direct_candles(conn, symbol, tf, start, end)
    assert len(api) == len(direct) > 0
    assert canonical_rows(frame_to_rows(api, _DIRECT_CANDLE_COLS)) == canonical_rows(direct)


def test_read_candles_is_ascending(conn, probe):
    symbol, tf = probe
    start, end = _closed_window(conn, tf, 200)
    api = c.read_candles(conn, symbol, tf, start=start, end=end, include_forming=True)
    times = list(api["open_time"])
    assert times == sorted(times)  # contract 1: always ASC, iloc[-1] is newest


def test_read_candles_limit_selects_the_newest_n(conn, probe):
    """`limit=n` must return the NEWEST n rows of the window, then ASC — not the oldest."""
    symbol, tf = probe
    start, end = _closed_window(conn, tf, 200)
    table = c.candles_table(symbol, tf)
    inner = sql.SQL(
        "SELECT symbol, open_time, open, high, low, close, volume FROM {tbl} "
        "WHERE open_time >= %s AND open_time <= %s ORDER BY open_time DESC LIMIT 50"
    ).format(tbl=sql.Identifier(table))
    query = sql.SQL("SELECT * FROM ({inner}) s ORDER BY open_time ASC").format(inner=inner)
    with conn.cursor() as cur:
        cur.execute(query, (start, end))
        direct_newest = cur.fetchall()

    api = c.read_candles(
        conn, symbol, tf, start=start, end=end, limit=50, include_forming=True, columns=_DIRECT_CANDLE_COLS
    )
    assert len(api) == len(direct_newest) == 50
    assert canonical_rows(frame_to_rows(api, _DIRECT_CANDLE_COLS)) == canonical_rows(direct_newest)


def test_include_forming_false_drops_only_forming_rows(conn, probe):
    """R1 contract: include_forming=False removes exactly the forming rows.

    Clock-safe by construction. Both reads cover the SAME window (same `start`,
    no limit) so the only difference between them is the forming filter — no
    newest-N artefact. `cutoff_before` is sampled BEFORE the reads and
    `cutoff_after` AFTER; the API evaluates its own now() in between, so its
    cutoff lies in [cutoff_before, cutoff_after]. Every assertion is phrased
    against whichever bound makes it hold at any instant of that interval, so the
    test never false-fails on a candle closing mid-run.
    """
    symbol, tf = probe
    cutoff_before = c.period_start(tf, _db_now(conn))
    start = cutoff_before - 10 * c.timeframe_delta(tf)
    api_all = c.read_candles(conn, symbol, tf, start=start, include_forming=True)
    api_closed = c.read_candles(conn, symbol, tf, start=start, include_forming=False)
    cutoff_after = c.period_start(tf, _db_now(conn))

    closed_times = set(api_closed["open_time"])
    all_times = set(api_all["open_time"])
    assert closed_times, "window held no closed candles — widen `start`"
    assert closed_times <= all_times  # the filter never invents rows

    # 1. Nothing the closed read returned is forming: it is < the API's cutoff,
    #    which is at most cutoff_after.
    assert all(ot < cutoff_after for ot in closed_times)
    # 2. Nothing already closed before we started is dropped: any such row is
    #    < cutoff_before ≤ the API's cutoff, so the filter must have kept it.
    assert {ot for ot in all_times if ot < cutoff_before} <= closed_times
    # 3. Every dropped row is a forming row (≥ cutoff_before).
    for ot in all_times - closed_times:
        assert ot >= cutoff_before
    # 4. If a forming row physically exists right now, the filter actually drops
    #    it (the test isn't vacuous when there is something to drop).
    newest = c.latest_open_time(conn, symbol, tf, include_forming=True)
    if newest is not None and newest >= cutoff_after:
        assert newest not in closed_times


def test_read_indicators_is_byte_equal_to_direct_sql(conn, probe):
    symbol, tf = probe
    table = c.indicators_table(symbol, tf)
    if not c.table_exists(conn, table):
        pytest.skip(f"{table} does not exist")
    start, end = _closed_window(conn, tf, 200)
    cols = c.indicator_column_names(conn, symbol, tf)
    assert "open_time" in cols

    api = c.read_indicators(conn, symbol, tf, start=start, end=end, include_forming=True, columns=cols)
    proj = sql.SQL(", ").join(sql.Identifier(col) for col in cols)
    query = sql.SQL("SELECT {proj} FROM {tbl} WHERE open_time >= %s AND open_time <= %s ORDER BY open_time ASC").format(
        proj=proj, tbl=sql.Identifier(table)
    )
    with conn.cursor() as cur:
        cur.execute(query, (start, end))
        direct = cur.fetchall()

    # Non-empty, else the byte-equality below is a vacuous 0 == 0 pass. The candle
    # probe guarantees ≥60 closed candles in this window; indicators track them.
    assert len(api) == len(direct) > 0
    assert canonical_rows(frame_to_rows(api, cols)) == canonical_rows(direct)


def test_joined_read_preserves_the_candle_side(conn, probe):
    """The LEFT JOIN read must not add, drop or reorder candle rows vs read_candles.

    The exact indicator projection is core.candles' own business (and re-deriving
    it here would test the test); what the gate cares about is that composing the
    join leaves the candle side byte-equal and ASC.
    """
    symbol, tf = probe
    if not c.table_exists(conn, c.indicators_table(symbol, tf)):
        pytest.skip("no indicator table for the probe symbol")
    start, end = _closed_window(conn, tf, 120)
    plain = c.read_candles(conn, symbol, tf, start=start, end=end, include_forming=True, columns=_DIRECT_CANDLE_COLS)
    joined = c.read_candles_with_indicators(
        conn, symbol, tf, start=start, end=end, include_forming=True, candle_columns=_DIRECT_CANDLE_COLS
    )
    times = list(joined["open_time"])
    assert times == sorted(times)
    assert list(joined["open_time"]) == list(plain["open_time"])
    for col in _DIRECT_CANDLE_COLS:
        assert canonical_rows([[v] for v in joined[col]]) == canonical_rows([[v] for v in plain[col]])


def test_latest_open_time_matches_max_open_time(conn, probe):
    symbol, tf = probe
    table = c.candles_table(symbol, tf)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT MAX(open_time) FROM {tbl}").format(tbl=sql.Identifier(table)))
        direct_max = cur.fetchone()[0]
    direct_max = direct_max.astimezone(timezone.utc) if direct_max.tzinfo else direct_max.replace(tzinfo=timezone.utc)

    api_max = c.latest_open_time(conn, symbol, tf, include_forming=True)
    assert canonical_cell(api_max) == canonical_cell(direct_max)
    # include_forming=False must never return a forming timestamp.
    closed_max = c.latest_open_time(conn, symbol, tf, include_forming=False)
    if closed_max is not None:
        assert closed_max < c.period_start(tf, _db_now(conn))


# ── Block-6 API-gap functions (T-2026-CU-9050-114) ────────────────────────────
#
# Read-shaped gaps (list_coin_tables, latest_open_time kind='indicators') are
# verified read-only, exactly like the Phase-0 gate above. The two mutating gaps
# (delete_candles_before, delete_indicators_from) are byte-tested against a
# SESSION-LOCAL temp table so no live per-coin table is ever locked or written;
# they are additionally gated behind KYTHERA_CANDLES_WRITE_PARITY so the default
# VPS run stays strictly read-only (harte Regel 1). The operator opts in for the
# write-path byte-test in a dedicated owner session.


def test_list_coin_tables_matches_information_schema(conn, probe):
    """Enumeration parity: the API returns exactly the per-coin tables the raw
    information_schema scan would, parsed into (symbol, tf, kind)."""
    symbol, tf = probe
    api = set(c.list_coin_tables(conn))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        )
        raw = [r[0] for r in cur.fetchall()]
    expected = {p for p in (c._parse_coin_table(n) for n in raw) if p is not None}
    assert api == expected
    assert (symbol, tf, "candles") in api  # the probe's own candle table is in there
    # filters are honoured
    assert all(t == tf for _, t, _ in c.list_coin_tables(conn, tf))
    assert all(k == "candles" for _, _, k in c.list_coin_tables(conn, kind="candles"))


def test_latest_open_time_indicators_matches_max(conn, probe):
    """kind='indicators' reads the indicator table's MAX(open_time) byte-equal."""
    symbol, tf = probe
    itable = c.indicators_table(symbol, tf)
    if not c.table_exists(conn, itable):
        pytest.skip(f"{itable} does not exist")
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT MAX(open_time) FROM {t}").format(t=sql.Identifier(itable)))
        direct = cur.fetchone()[0]
    if direct is not None:
        direct = direct.astimezone(timezone.utc) if direct.tzinfo else direct.replace(tzinfo=timezone.utc)
    api = c.latest_open_time(conn, symbol, tf, include_forming=True, kind="indicators")
    assert canonical_cell(api) == canonical_cell(direct)


def _require_write_parity() -> None:
    if not os.getenv("KYTHERA_CANDLES_WRITE_PARITY"):
        pytest.skip("write-path byte-test: set KYTHERA_CANDLES_WRITE_PARITY=1 in an owner session")


def _seed_temp_candles(conn: Any, table: str, opens: Sequence[datetime]) -> None:
    """Create a session-local temp candle table and seed it. ON COMMIT DROP +
    the test's rollback make it impossible to leak into the live schema."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE TEMP TABLE {t} (symbol text, open_time timestamptz, open double precision, "
                "high double precision, low double precision, close double precision, "
                "volume double precision, PRIMARY KEY (symbol, open_time)) ON COMMIT DROP"
            ).format(t=sql.Identifier(table))
        )
        for ot in opens:
            cur.execute(
                sql.SQL("INSERT INTO {t} VALUES (%s, %s, 1, 1, 1, 1, 1)").format(t=sql.Identifier(table)),
                ("ZZTESTXX", ot),
            )


def test_delete_candles_before_deletes_exactly_the_old_rows(conn):
    _require_write_parity()
    sym, tf = "ZZTESTXX", "1h"
    table = c.candles_table(sym, tf)  # "ZZTESTXX_1h" — a temp table shadows any real one
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    opens = [base + i * c.timeframe_delta(tf) for i in range(5)]  # 5 hourly candles
    try:
        _seed_temp_candles(conn, table, opens)
        cutoff = opens[3]  # rows 0,1,2 are older; 3,4 are not
        deleted = c.delete_candles_before(conn, sym, tf, cutoff)
        assert deleted == 3
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT open_time FROM {t} ORDER BY open_time").format(t=sql.Identifier(table)))
            remaining = [r[0] for r in cur.fetchall()]
        assert all(ot >= cutoff for ot in remaining)
        assert len(remaining) == 2
    finally:
        conn.rollback()  # drops the temp table, undoes every write


def test_delete_indicators_from_deletes_the_tail(conn):
    _require_write_parity()
    sym, tf = "ZZTESTXX", "1h"
    table = c.indicators_table(sym, tf)  # "ZZTESTXX_1h_indicators"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    opens = [base + i * c.timeframe_delta(tf) for i in range(5)]
    try:
        _seed_temp_candles(conn, table, opens)  # same shape suffices for the open_time filter
        start = opens[2]  # rows 2,3,4 are >= start
        deleted = c.delete_indicators_from(conn, sym, tf, start)
        assert deleted == 3
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT open_time FROM {t} ORDER BY open_time").format(t=sql.Identifier(table)))
            remaining = [r[0] for r in cur.fetchall()]
        assert all(ot < start for ot in remaining)
        assert len(remaining) == 2
    finally:
        conn.rollback()


def _create_temp_legacy_tables(conn: Any, sym: str, tf: str, ind_payload: Sequence[str]) -> None:
    """Session-local temp per-coin candle + indicator tables (ON COMMIT DROP) so the
    legacy write half of upsert_* lands somewhere harmless; the DUAL write half goes
    to the real `candles`/`indicators` hypertables, which the test reads back and the
    finally-rollback then undoes. No live per-coin table is ever touched."""
    ctable, itable = c.candles_table(sym, tf), c.indicators_table(sym, tf)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE TEMP TABLE {t} (symbol text, open_time timestamptz, open double precision, "
                "high double precision, low double precision, close double precision, "
                "volume double precision, PRIMARY KEY (symbol, open_time)) ON COMMIT DROP"
            ).format(t=sql.Identifier(ctable))
        )
        col_defs = sql.SQL(", ").join(
            sql.SQL("{c} {typ}").format(
                c=sql.Identifier(col), typ=sql.SQL("text" if col == "trend_direction" else "double precision")
            )
            for col in ind_payload
        )
        cur.execute(
            sql.SQL(
                "CREATE TEMP TABLE {t} (symbol text, open_time timestamptz, {cols}, "
                "PRIMARY KEY (symbol, open_time)) ON COMMIT DROP"
            ).format(t=sql.Identifier(itable), cols=col_defs)
        )


def test_dual_write_mirrors_into_hypertables(conn, monkeypatch):
    """With KYTHERA_CANDLES_DUAL_WRITE on, upsert_* mirror into candles/indicators
    with tf + is_closed added; the forming→closed flag transition updates in place;
    with the flag off nothing reaches the hypertables. All writes rolled back."""
    _require_write_parity()
    monkeypatch.setenv("KYTHERA_CANDLES_DUAL_WRITE", "1")
    sym, tf = "ZZTESTXX", "1h"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    d = c.timeframe_delta(tf)
    try:
        _create_temp_legacy_tables(conn, sym, tf, ["close", "rsi_14", "trend_direction"])
        closed_rows = [(sym, base + i * d, 1.0, 2.0, 0.5, 1.5, 10.0) for i in range(2)]
        forming_rows = [(sym, base + 2 * d, 3.0, 4.0, 2.0, 3.5, 20.0)]
        c.upsert_candles(conn, sym, tf, closed_rows, closed=True)
        c.upsert_candles(conn, sym, tf, forming_rows, closed=False)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_closed, count(*) FROM candles WHERE symbol=%s AND tf=%s GROUP BY is_closed", (sym, tf)
            )
            assert dict(cur.fetchall()) == {True: 2, False: 1}
            # value + tf parity on one closed row
            cur.execute(
                "SELECT open, high, low, close, volume FROM candles WHERE symbol=%s AND tf=%s AND open_time=%s",
                (sym, tf, base),
            )
            assert cur.fetchone() == (1.0, 2.0, 0.5, 1.5, 10.0)
        # forming → closed: the flag flips in place, no duplicate row
        c.upsert_candles(conn, sym, tf, forming_rows, closed=True)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM candles WHERE symbol=%s AND tf=%s", (sym, tf))
            assert cur.fetchone()[0] == 3
            cur.execute(
                "SELECT is_closed FROM candles WHERE symbol=%s AND tf=%s AND open_time=%s", (sym, tf, base + 2 * d)
            )
            assert cur.fetchone()[0] is True
        # indicators: tf added, is_closed=True (engine writes closed-only), text col preserved
        df = pd.DataFrame(
            {"symbol": [sym], "open_time": [base], "close": [1.5], "rsi_14": [55.0], "trend_direction": ["UP"]}
        )
        c.upsert_indicators(conn, df, sym, tf)
        with conn.cursor() as cur:
            cur.execute("SELECT tf, is_closed, close, rsi_14, trend_direction FROM indicators WHERE symbol=%s", (sym,))
            assert cur.fetchone() == (tf, True, 1.5, 55.0, "UP")
        # flag OFF → no hypertable write
        monkeypatch.setenv("KYTHERA_CANDLES_DUAL_WRITE", "0")
        _create_temp_legacy_tables(conn, "ZZTESTYY", tf, ["close"])
        c.upsert_candles(conn, "ZZTESTYY", tf, [("ZZTESTYY", base, 1.0, 1.0, 1.0, 1.0, 1.0)], closed=True)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM candles WHERE symbol=%s", ("ZZTESTYY",))
            assert cur.fetchone()[0] == 0
    finally:
        conn.rollback()  # undoes every hypertable write + drops the temp tables


def test_write_primary_hyper_writes_hyper_and_skips_legacy(conn, monkeypatch):
    """AK7 (T-2026-CU-9050-139): with KYTHERA_CANDLES_WRITE_PRIMARY=hyper the write
    helpers write the candles/indicators HYPERTABLES as the primary store and SKIP
    the legacy per-coin write entirely (DUAL_WRITE is moot). The default 'legacy'
    still writes the per-coin table. All writes rolled back; the per-coin side lands
    in ON COMMIT DROP temp tables, so no live table is touched."""
    _require_write_parity()
    sym, tf = "ZZTESTHP", "1h"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ctable, itable = c.candles_table(sym, tf), c.indicators_table(sym, tf)
    rows = [(sym, base, 1.0, 2.0, 0.5, 1.5, 10.0)]
    df = pd.DataFrame({"symbol": [sym], "open_time": [base], "close": [1.5], "rsi_14": [55.0]})
    try:
        _create_temp_legacy_tables(conn, sym, tf, ["close", "rsi_14"])
        # hyper-primary + DUAL_WRITE explicitly OFF → proves hyper is the PRIMARY, not
        # the mirror: the row must still reach the hypertable and NOT the per-coin table.
        monkeypatch.setenv("KYTHERA_CANDLES_WRITE_PRIMARY", "hyper")
        monkeypatch.setenv("KYTHERA_CANDLES_DUAL_WRITE", "0")
        c.upsert_candles(conn, sym, tf, rows, closed=True)
        c.upsert_indicators(conn, df, sym, tf)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM candles WHERE symbol=%s AND tf=%s", (sym, tf))
            assert cur.fetchone()[0] == 1  # hypertable = primary
            cur.execute("SELECT count(*) FROM indicators WHERE symbol=%s AND tf=%s", (sym, tf))
            assert cur.fetchone()[0] == 1
            cur.execute(sql.SQL("SELECT count(*) FROM {t}").format(t=sql.Identifier(ctable)))
            assert cur.fetchone()[0] == 0  # legacy per-coin skipped
            cur.execute(sql.SQL("SELECT count(*) FROM {t}").format(t=sql.Identifier(itable)))
            assert cur.fetchone()[0] == 0
        # default 'legacy' still writes the per-coin table (behaviour-preserving)
        monkeypatch.setenv("KYTHERA_CANDLES_WRITE_PRIMARY", "legacy")
        c.upsert_candles(conn, sym, tf, rows, closed=True)
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT count(*) FROM {t}").format(t=sql.Identifier(ctable)))
            assert cur.fetchone()[0] == 1
    finally:
        conn.rollback()  # undoes every hypertable write + drops the temp tables


# ── Phase-4 read-cutover: hyper backend == legacy backend (T-2026-CU-9050-128) ─
#
# The read-cutover flips KYTHERA_CANDLES_SOURCE from 'legacy' to 'hyper'. It must
# be behaviour-neutral: for a real (symbol, tf) the hyper backend has to return
# rows byte-equal — same rows, order, values, column shape — to the legacy
# backend. These VPS-only tests prove that directly, and are the acceptance gate
# for the task.
#
# Determinism: both reads in a test run inside the SAME uncommitted transaction on
# the pooled `conn`, so now() (transaction_timestamp) — and thus the clock-based
# forming cutoff both backends share — is identical between them. That makes even
# include_forming=False parity race-free: no candle-boundary flake. (The DB-free
# comparator tests above guard the harness, so a green run here cannot be a false
# pass from a broken comparator; without hyper data the tests skip, never fake.)


@contextlib.contextmanager
def _use_source(name: str) -> Iterator[None]:
    """Force KYTHERA_CANDLES_SOURCE for the duration of a read, then restore it —
    core.candles reads the flag at call time, so this switches the backend."""
    prev = os.environ.get("KYTHERA_CANDLES_SOURCE")
    os.environ["KYTHERA_CANDLES_SOURCE"] = name
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("KYTHERA_CANDLES_SOURCE", None)
        else:
            os.environ["KYTHERA_CANDLES_SOURCE"] = prev


def _read_via(source: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    with _use_source(source):
        return fn(*args, **kwargs)


@pytest.fixture(scope="module")
def hyper_probe(conn, probe):
    """The probe (symbol, tf), but only once the hypertables exist AND hold it. A
    box where C-Gate Phase 0/backfill has not run must skip, never fabricate a pass."""
    symbol, tf = probe
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('candles'), to_regclass('indicators')")
        ctbl, itbl = cur.fetchone()
    if ctbl is None or itbl is None:
        pytest.skip("candles/indicators hypertables absent (C-Gate Phase 0 not run on this box)")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM candles WHERE symbol = %s AND tf = %s", (symbol, tf))
        if cur.fetchone()[0] < _MIN_ROWS:
            pytest.skip(f"candles hypertable holds <{_MIN_ROWS} rows for {symbol} {tf} (not backfilled here)")
    return symbol, tf


def test_hyper_read_candles_is_byte_equal_to_legacy(conn, hyper_probe):
    """read_candles: hyper == legacy over a closed window, every limit shape."""
    symbol, tf = hyper_probe
    start, end = _closed_window(conn, tf, 200)
    for limit in (None, 50, 1):
        legacy = _read_via(
            "legacy",
            c.read_candles,
            conn,
            symbol,
            tf,
            start=start,
            end=end,
            limit=limit,
            include_forming=True,
            columns=_DIRECT_CANDLE_COLS,
        )
        hyper = _read_via(
            "hyper",
            c.read_candles,
            conn,
            symbol,
            tf,
            start=start,
            end=end,
            limit=limit,
            include_forming=True,
            columns=_DIRECT_CANDLE_COLS,
        )
        assert len(hyper) == len(legacy) > 0, limit
        assert canonical_rows(frame_to_rows(hyper, _DIRECT_CANDLE_COLS)) == canonical_rows(
            frame_to_rows(legacy, _DIRECT_CANDLE_COLS)
        ), limit


def test_hyper_read_candles_forming_filter_matches_legacy(conn, hyper_probe):
    """include_forming=False: both backends apply the same clock cutoff (shared
    now() in one transaction), so the closed set is identical — and a subset of the
    forming-inclusive read, which proves the filter is actually doing something."""
    symbol, tf = hyper_probe
    start = c.period_start(tf, _db_now(conn)) - 50 * c.timeframe_delta(tf)
    legacy = _read_via(
        "legacy", c.read_candles, conn, symbol, tf, start=start, include_forming=False, columns=_DIRECT_CANDLE_COLS
    )
    hyper = _read_via(
        "hyper", c.read_candles, conn, symbol, tf, start=start, include_forming=False, columns=_DIRECT_CANDLE_COLS
    )
    assert len(hyper) == len(legacy) > 0
    assert canonical_rows(frame_to_rows(hyper, _DIRECT_CANDLE_COLS)) == canonical_rows(
        frame_to_rows(legacy, _DIRECT_CANDLE_COLS)
    )
    hyper_all = _read_via(
        "hyper", c.read_candles, conn, symbol, tf, start=start, include_forming=True, columns=_DIRECT_CANDLE_COLS
    )
    assert len(hyper) <= len(hyper_all)


def test_hyper_indicator_column_names_matches_legacy(conn, hyper_probe):
    """The hyper catalog drops tf/is_closed → byte-equal to the legacy per-coin
    catalog: same names, same ordinal order."""
    symbol, tf = hyper_probe
    if not c.table_exists(conn, c.indicators_table(symbol, tf)):
        pytest.skip("no legacy indicator table for the probe symbol")
    legacy_cols = _read_via("legacy", c.indicator_column_names, conn, symbol, tf)
    hyper_cols = _read_via("hyper", c.indicator_column_names, conn, symbol, tf)
    assert hyper_cols == legacy_cols
    assert "tf" not in hyper_cols and "is_closed" not in hyper_cols
    assert hyper_cols[:3] == ["symbol", "open_time", "close"]


def test_hyper_read_indicators_is_byte_equal_to_legacy(conn, hyper_probe):
    """read_indicators over an explicit projection, and via columns=None (SELECT-*
    shape): the hyper read must not leak tf/is_closed and must match legacy values."""
    symbol, tf = hyper_probe
    if not c.table_exists(conn, c.indicators_table(symbol, tf)):
        pytest.skip("no legacy indicator table for the probe symbol")
    start, end = _closed_window(conn, tf, 200)
    cols = _read_via("legacy", c.indicator_column_names, conn, symbol, tf)

    legacy = _read_via(
        "legacy", c.read_indicators, conn, symbol, tf, start=start, end=end, include_forming=True, columns=cols
    )
    hyper = _read_via(
        "hyper", c.read_indicators, conn, symbol, tf, start=start, end=end, include_forming=True, columns=cols
    )
    assert len(hyper) == len(legacy) > 0
    # float4 precision: legacy indicators are REAL, hyper is double (P3.12).
    assert canonical_rows_f4(frame_to_rows(hyper, cols)) == canonical_rows_f4(frame_to_rows(legacy, cols))

    # columns=None: each backend expands to its own catalog; shape + values must match
    legacy_star = _read_via("legacy", c.read_indicators, conn, symbol, tf, start=start, end=end, include_forming=True)
    hyper_star = _read_via("hyper", c.read_indicators, conn, symbol, tf, start=start, end=end, include_forming=True)
    assert list(hyper_star.columns) == list(legacy_star.columns)  # no tf/is_closed leak, legacy order
    shared = list(legacy_star.columns)
    assert canonical_rows_f4(frame_to_rows(hyper_star, shared)) == canonical_rows_f4(frame_to_rows(legacy_star, shared))


def test_hyper_joined_read_is_byte_equal_to_legacy(conn, hyper_probe):
    """read_candles_with_indicators: the (symbol,tf,open_time) join reproduces the
    legacy per-coin open_time join byte-for-byte, same columns and order."""
    symbol, tf = hyper_probe
    if not c.table_exists(conn, c.indicators_table(symbol, tf)):
        pytest.skip("no legacy indicator table for the probe symbol")
    start, end = _closed_window(conn, tf, 120)
    legacy = _read_via(
        "legacy",
        c.read_candles_with_indicators,
        conn,
        symbol,
        tf,
        start=start,
        end=end,
        include_forming=True,
        candle_columns=_DIRECT_CANDLE_COLS,
    )
    hyper = _read_via(
        "hyper",
        c.read_candles_with_indicators,
        conn,
        symbol,
        tf,
        start=start,
        end=end,
        include_forming=True,
        candle_columns=_DIRECT_CANDLE_COLS,
    )
    assert list(hyper.columns) == list(legacy.columns)
    assert len(hyper) == len(legacy) > 0
    shared = list(legacy.columns)
    # float4: the candle side is double==double (already proven byte-equal at 12g
    # in test_hyper_read_candles), the indicator side is REAL vs double (P3.12).
    assert canonical_rows_f4(frame_to_rows(hyper, shared)) == canonical_rows_f4(frame_to_rows(legacy, shared))


def test_hyper_latest_open_time_matches_legacy(conn, hyper_probe):
    """latest_open_time: hyper MAX(open_time) == legacy, both kinds, both forming
    settings (the resume watermark must not shift at the cutover)."""
    symbol, tf = hyper_probe
    for kind in ("candles", "indicators"):
        if kind == "indicators" and not c.table_exists(conn, c.indicators_table(symbol, tf)):
            continue
        for forming in (True, False):
            legacy = _read_via("legacy", c.latest_open_time, conn, symbol, tf, include_forming=forming, kind=kind)
            hyper = _read_via("hyper", c.latest_open_time, conn, symbol, tf, include_forming=forming, kind=kind)
            assert canonical_cell(hyper) == canonical_cell(legacy), (kind, forming)


def test_hyper_table_exists_probes_the_persistent_relation(conn, hyper_probe):
    """table_exists is phase-agnostic: under source='hyper' it still probes the
    per-coin relation (present until the Phase-5 drop) — True for the probe, False
    for a never-ingested symbol. No 40M-row hypertable scan."""
    symbol, tf = hyper_probe
    with _use_source("hyper"):
        assert c.table_exists(conn, c.candles_table(symbol, tf)) is True
        assert c.table_exists(conn, c.candles_table("ZZNOPEXX", tf)) is False


def test_hyper_list_coin_tables_matches_legacy(conn, hyper_probe):
    """list_coin_tables is phase-agnostic too (it enumerates the per-coin relations,
    NOT a >20 s DISTINCT over the 40M-row hypertable): under source='hyper' it
    returns exactly the legacy set, with the probe present and tf/kind filters
    honoured."""
    symbol, tf = hyper_probe
    legacy = set(_read_via("legacy", c.list_coin_tables, conn))
    hyper = set(_read_via("hyper", c.list_coin_tables, conn))
    assert hyper == legacy
    assert (symbol, tf, "candles") in hyper
    with _use_source("hyper"):
        assert all(t == tf for _, t, _ in c.list_coin_tables(conn, tf))
        assert all(k == "candles" for _, _, k in c.list_coin_tables(conn, kind="candles"))


def test_hyper_read_candles_columns_none_matches_legacy(conn, hyper_probe):
    """columns=None must NOT leak tf/is_closed under hyper. Legacy `SELECT *` on a
    per-coin candle table yields the 7 CANDLE_COLUMNS; the hypertable's `SELECT *`
    would add tf/is_closed. This guards the rgcore `columns=None` capture path."""
    symbol, tf = hyper_probe
    start, end = _closed_window(conn, tf, 120)
    legacy = _read_via(
        "legacy", c.read_candles, conn, symbol, tf, start=start, end=end, include_forming=True, columns=None
    )
    hyper = _read_via(
        "hyper", c.read_candles, conn, symbol, tf, start=start, end=end, include_forming=True, columns=None
    )
    assert list(hyper.columns) == list(legacy.columns)  # no tf/is_closed leak, legacy order
    assert "tf" not in hyper.columns and "is_closed" not in hyper.columns
    shared = list(legacy.columns)
    assert len(hyper) == len(legacy) > 0
    assert canonical_rows(frame_to_rows(hyper, shared)) == canonical_rows(frame_to_rows(legacy, shared))


def test_hyper_joined_candle_columns_none_matches_legacy(conn, hyper_probe):
    """Same guard for the joined read's candle_columns=None path (`h.*`)."""
    symbol, tf = hyper_probe
    if not c.table_exists(conn, c.indicators_table(symbol, tf)):
        pytest.skip("no legacy indicator table for the probe symbol")
    start, end = _closed_window(conn, tf, 80)
    legacy = _read_via(
        "legacy",
        c.read_candles_with_indicators,
        conn,
        symbol,
        tf,
        start=start,
        end=end,
        include_forming=True,
        candle_columns=None,
    )
    hyper = _read_via(
        "hyper",
        c.read_candles_with_indicators,
        conn,
        symbol,
        tf,
        start=start,
        end=end,
        include_forming=True,
        candle_columns=None,
    )
    assert list(hyper.columns) == list(legacy.columns)
    assert "tf" not in hyper.columns and "is_closed" not in hyper.columns
    shared = list(legacy.columns)
    assert len(hyper) == len(legacy) > 0
    assert canonical_rows_f4(frame_to_rows(hyper, shared)) == canonical_rows_f4(frame_to_rows(legacy, shared))


def test_hyper_indicator_column_names_matches_legacy_fleetwide(conn, hyper_probe):
    """Not just the sample: EVERY per-coin `_indicators` table must carry the exact
    column set + ordinal order the hyper catalog returns (minus tf/is_closed).

    `2_indicator_engine.create_indicator_table` uses CREATE TABLE IF NOT EXISTS with
    no ALTER-migration path, so a coin whose table predates a newer indicator column
    could carry a stale set — and read_indicators(columns=None) /
    read_candles_with_indicators(indicator_columns=None) would then project a
    different feature vector under hyper vs legacy for that outlier. This is the
    fleet-wide cutover gate for that risk (one bulk catalog query, not N)."""
    expected = _read_via("hyper", c.indicator_column_names, conn, hyper_probe[0], hyper_probe[1])
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name LIKE '%indicators' "
            "ORDER BY table_name, ordinal_position"
        )
        rows = cur.fetchall()
    per_table: dict[str, list[str]] = {}
    for tname, cname in rows:
        if c._parse_coin_table(tname) is not None:  # only real {SYM}_{tf}_indicators tables
            per_table.setdefault(tname, []).append(cname)
    assert len(per_table) > 100, f"expected the full per-coin fleet, saw {len(per_table)} indicator tables"
    diverged = {t: cols for t, cols in per_table.items() if cols != expected}
    assert not diverged, (
        f"{len(diverged)} per-coin indicator tables diverge from the hyper column list: {list(diverged)[:5]}"
    )


@pytest.fixture(scope="module")
def hyper_sample(conn):
    """A spread of (symbol, tf) present in BOTH backends with ≥_MIN_ROWS in the
    comparison window — BTC/ETH/SOL plus a few smaller coins across timeframes, so
    the parity claim is not resting on a single pair. Skips if the hypertables are
    not populated here."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('candles')")
        if cur.fetchone()[0] is None:
            pytest.skip("candles hypertable absent (C-Gate Phase 0 not run on this box)")
    preferred = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    others = sorted({s for s, _, _ in c.list_coin_tables(conn, kind="candles") if s.endswith("USDT")} - set(preferred))
    # a handful spread across the alphabet = a mix of large- and small-cap coins
    smalls = others[:: max(1, len(others) // 4)][:4] if others else []
    sample: list[tuple[str, str]] = []
    for sym in preferred + smalls:
        for tf in ("5m", "1h", "4h", "1d"):
            if not c.table_exists(conn, c.candles_table(sym, tf)):
                continue
            start, end = _closed_window(conn, tf, 150)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM candles WHERE symbol = %s AND tf = %s AND open_time >= %s AND open_time <= %s",
                    (sym, tf, start, end),
                )
                if cur.fetchone()[0] >= _MIN_ROWS:
                    sample.append((sym, tf))
    if len(sample) < 3:
        pytest.skip("could not assemble a multi-coin / multi-tf hyper sample")
    return sample


def test_hyper_read_parity_across_coin_and_tf_sample(conn, hyper_sample):
    """The behaviour-neutral cutover claim, widened: for every (symbol, tf) in the
    sample the hyper read equals the legacy read — candles byte-for-byte, indicators
    at float4 precision (P3.12)."""
    checked_candles = checked_indicators = 0
    for symbol, tf in hyper_sample:
        start, end = _closed_window(conn, tf, 150)
        legacy = _read_via(
            "legacy",
            c.read_candles,
            conn,
            symbol,
            tf,
            start=start,
            end=end,
            include_forming=True,
            columns=_DIRECT_CANDLE_COLS,
        )
        hyper = _read_via(
            "hyper",
            c.read_candles,
            conn,
            symbol,
            tf,
            start=start,
            end=end,
            include_forming=True,
            columns=_DIRECT_CANDLE_COLS,
        )
        assert len(hyper) == len(legacy) > 0, (symbol, tf)
        assert canonical_rows(frame_to_rows(hyper, _DIRECT_CANDLE_COLS)) == canonical_rows(
            frame_to_rows(legacy, _DIRECT_CANDLE_COLS)
        ), (symbol, tf)
        checked_candles += 1

        if c.table_exists(conn, c.indicators_table(symbol, tf)):
            cols = _read_via("legacy", c.indicator_column_names, conn, symbol, tf)
            li = _read_via(
                "legacy", c.read_indicators, conn, symbol, tf, start=start, end=end, include_forming=True, columns=cols
            )
            hi = _read_via(
                "hyper", c.read_indicators, conn, symbol, tf, start=start, end=end, include_forming=True, columns=cols
            )
            assert len(hi) == len(li), (symbol, tf, "indicator len")
            assert canonical_rows_f4(frame_to_rows(hi, cols)) == canonical_rows_f4(frame_to_rows(li, cols)), (
                symbol,
                tf,
                "indicators",
            )
            checked_indicators += 1
    assert checked_candles >= 3
    print(f"\nhyper parity sample: {checked_candles} coin/tf candle reads, {checked_indicators} with indicators")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

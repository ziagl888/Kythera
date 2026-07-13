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

import math
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

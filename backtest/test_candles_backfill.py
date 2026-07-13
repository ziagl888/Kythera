# backtest/test_candles_backfill.py
"""
Tests for tools/candles_backfill.py — the C-Gate Phase 2 slice 2b historical
copy into the candles/indicators hypertables (T-2026-CU-9050-119).

DB-free: progress-file round-trip + the empty-payload guard.
DB-gated (KYTHERA_CANDLES_WRITE_PARITY, all writes rolled back): the candles copy
computes is_closed from the cutoff and is idempotent (ON CONFLICT DO NOTHING); the
indicators copy mirrors a real low-row table with tf + is_closed added.

Run with: pytest backtest/test_candles_backfill.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest
from psycopg2 import sql

from core import candles as c


def _load_backfill():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "candles_backfill.py")
    spec = importlib.util.spec_from_file_location("candles_backfill", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bf = _load_backfill()


# ── DB-free ───────────────────────────────────────────────────────────────────
def test_progress_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(bf, "PROGRESS_FILE", str(tmp_path / "prog.json"))
    assert bf._load_progress() == set()
    done = {("BTCUSDT", "1h", "candles"), ("ETHUSDT", "4h", "indicators")}
    bf._save_progress(done)
    assert bf._load_progress() == done
    bf._save_progress(done)  # idempotent re-save
    assert bf._load_progress() == done


def test_indicators_copy_sql_rejects_empty_payload(monkeypatch):
    monkeypatch.setattr(c, "indicator_column_names", lambda conn, s, tf: ["symbol", "open_time"])
    with pytest.raises(ValueError, match="no payload columns"):
        bf._indicators_copy_sql(None, "BTCUSDT", "1h")


# ── DB fixtures (VPS only) ─────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def conn():
    try:
        from core.database import db_connection
    except Exception as exc:
        pytest.skip(f"no database configuration: {exc}")
    try:
        with db_connection() as connection:
            yield connection
    except Exception as exc:
        pytest.skip(f"database not reachable: {exc}")


def _require_write_parity() -> None:
    if not os.getenv("KYTHERA_CANDLES_WRITE_PARITY"):
        pytest.skip("write-path test: set KYTHERA_CANDLES_WRITE_PARITY=1 in an owner session")


def test_copy_candles_splits_is_closed_and_is_idempotent(conn):
    from core.time import utc_now

    _require_write_parity()
    sym, tf = "ZZBACKFL", "1h"
    ctable = c.candles_table(sym, tf)
    cutoff = c.period_start(tf, utc_now())
    d = c.timeframe_delta(tf)
    old = [cutoff - i * d for i in range(1, 4)]  # 3 strictly-before-cutoff rows → closed
    forming = cutoff  # the current period's open → is_closed false
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "CREATE TEMP TABLE {t} (symbol text, open_time timestamptz, open double precision, "
                    "high double precision, low double precision, close double precision, "
                    "volume double precision, PRIMARY KEY (symbol, open_time)) ON COMMIT DROP"
                ).format(t=sql.Identifier(ctable))
            )
            for ot in [*old, forming]:
                cur.execute(
                    sql.SQL("INSERT INTO {t} VALUES (%s, %s, 1, 2, 0.5, 1.5, 10)").format(t=sql.Identifier(ctable)),
                    (sym, ot),
                )
        assert bf._copy_one(conn, sym, tf, "candles") == 4
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_closed, count(*) FROM candles WHERE symbol=%s AND tf=%s GROUP BY is_closed", (sym, tf)
            )
            assert dict(cur.fetchall()) == {True: 3, False: 1}
        # ON CONFLICT DO NOTHING → a second copy writes nothing
        assert bf._copy_one(conn, sym, tf, "candles") == 0
    finally:
        conn.rollback()


def test_copy_indicators_mirrors_real_table_with_tf_and_is_closed(conn):
    from core.time import utc_now

    _require_write_parity()
    # A real, low-row indicator table (1w has the fewest rows); read-only source.
    src = None
    for symbol, tf, _ in c.list_coin_tables(conn, "1w", kind="indicators"):
        if c.table_exists(conn, c.indicators_table(symbol, tf)):
            src = (symbol, tf)
            break
    if src is None:
        pytest.skip("no 1w indicator table available")
    sym, tf = src
    try:
        copied = bf._copy_one(conn, sym, tf, "indicators")
        assert copied > 0
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM indicators WHERE symbol=%s AND tf=%s", (sym, tf))
            assert cur.fetchone()[0] == copied
            # tf is the constant; is_closed is boolean; every row past the cutoff is closed
            cutoff = c.period_start(tf, utc_now())
            cur.execute(
                "SELECT bool_and(is_closed) FROM indicators WHERE symbol=%s AND tf=%s AND open_time < %s",
                (sym, tf, cutoff),
            )
            assert cur.fetchone()[0] is True
        # idempotent
        assert bf._copy_one(conn, sym, tf, "indicators") == 0
    finally:
        conn.rollback()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

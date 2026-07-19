# backtest/test_indicator_engine_skip.py — T-2026-CU-9050-174, DB-free.
#
# Pins the control logic added around process_coin_task:
#   1. the early-skip decision table: (indicator watermark, newest CLOSED candle)
#      -> skip before read_candles / compute as before
#   2. per-task transaction hygiene: the persistent worker connection ends its
#      transaction on EVERY exit (the old per-task close() rolled back via
#      PooledConnection.close(); review finding on PR #161)
#   3. positive-only table_exists cache (a miss re-probes, a hit never does)
#   4. a broken connection (rollback fails) is discarded so the next task
#      reconnects
#
# Run: python backtest/test_indicator_engine_skip.py

import datetime
import importlib.util
import os
import sys

import pandas as pd
from psycopg2.extensions import TRANSACTION_STATUS_IDLE, TRANSACTION_STATUS_INTRANS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# Offline cred shim: the engine hard-requires these at import (it never connects here).
for _k, _v in {
    "DB_PASSWORD": "test",
    "TELEGRAM_BOT_TOKEN": "test",
    "DB_HOST": "127.0.0.1",
    "DB_NAME": "t",
    "DB_USER": "t",
    "DB_PORT": "5432",
}.items():
    os.environ.setdefault(_k, _v)


def _load_engine():
    """Import the digit-prefixed engine module by path (mirrors the guard)."""
    path = os.path.join(REPO_ROOT, "2_indicator_engine.py")
    spec = importlib.util.spec_from_file_location("kythera_indicator_engine", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENGINE = _load_engine()

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
T1 = datetime.datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
T2 = datetime.datetime(2026, 1, 1, 2, 0, tzinfo=UTC)


class FakeConn:
    """Tracks transaction state the way process_coin_task observes it."""

    def __init__(self, rollback_raises=False):
        self.closed = 0
        self.rollbacks = 0
        self.commits = 0
        self.rollback_raises = rollback_raises
        self.status = TRANSACTION_STATUS_IDLE

    def get_transaction_status(self):
        return self.status

    def rollback(self):
        if self.rollback_raises:
            raise RuntimeError("connection broken")
        self.rollbacks += 1
        self.status = TRANSACTION_STATUS_IDLE

    def commit(self):
        self.commits += 1
        self.status = TRANSACTION_STATUS_IDLE

    def close(self):
        self.closed = 1


def _run_task(watermark, newest_closed, conn=None, table_probes=None):
    """Run process_coin_task with a stubbed DB layer.

    Returns (conn, calls) where calls records the latest_open_time probes and
    whether read_candles was reached.
    """
    conn = conn or FakeConn()
    ENGINE._WORKER["conn"] = conn
    ENGINE._WORKER["definitions"] = None
    if table_probes is None:
        ENGINE._WORKER["tables"] = set()
    calls = {"read": False, "probes": []}

    def fake_latest(c, sym, tf, *, include_forming, kind="candles"):
        calls["probes"].append((kind, include_forming))
        c.status = TRANSACTION_STATUS_INTRANS  # a SELECT opens a transaction
        return watermark if kind == "indicators" else newest_closed

    def fake_read(*a, **k):
        calls["read"] = True
        return pd.DataFrame()  # empty frame -> task returns right after

    def fake_table_exists(c, t):
        c.status = TRANSACTION_STATUS_INTRANS  # the to_regclass probe, too
        if table_probes is not None:
            table_probes.append(t)
        return True

    orig = (ENGINE.latest_open_time, ENGINE.read_candles, ENGINE.table_exists)
    ENGINE.latest_open_time, ENGINE.read_candles, ENGINE.table_exists = (
        fake_latest,
        fake_read,
        fake_table_exists,
    )
    try:
        ENGINE.process_coin_task(("TESTUSDT", "1h"))
    finally:
        ENGINE.latest_open_time, ENGINE.read_candles, ENGINE.table_exists = orig
        ENGINE._WORKER["conn"] = None
    return conn, calls


def test_skip_when_no_newer_closed_candle():
    conn, calls = _run_task(watermark=T1, newest_closed=T1)
    assert not calls["read"], "equal newest-closed candle must skip before read_candles"
    assert ("candles", False) in calls["probes"], "skip must probe CLOSED candles only"
    assert conn.status == TRANSACTION_STATUS_IDLE and conn.rollbacks >= 1, (
        "skip path left the persistent connection idle-in-transaction"
    )
    print("OK  skip: newest closed == watermark -> return before read_candles, txn ended")


def test_skip_when_candles_behind_watermark():
    conn, calls = _run_task(watermark=T1, newest_closed=T0)
    assert not calls["read"], "older newest-closed candle (e.g. delisted coin) must skip"
    assert conn.status == TRANSACTION_STATUS_IDLE
    print("OK  skip: newest closed < watermark -> skip, txn ended")


def test_skip_when_no_closed_candle_at_all():
    conn, calls = _run_task(watermark=T1, newest_closed=None)
    assert not calls["read"], "no closed candle must skip (== old empty-frame return)"
    assert conn.status == TRANSACTION_STATUS_IDLE
    print("OK  skip: no closed candle -> skip, txn ended")


def test_recompute_when_new_closed_candle():
    conn, calls = _run_task(watermark=T1, newest_closed=T2)
    assert calls["read"], "newer closed candle (late ingestion / normal advance) must recompute"
    assert conn.status == TRANSACTION_STATUS_IDLE, "read-only exit (empty frame) left the transaction open"
    print("OK  recompute: newest closed > watermark -> read_candles runs, txn ended")


def test_recompute_when_watermark_jumped_back():
    # Housekeeping gap-invalidation deletes indicator rows -> watermark < newest closed.
    conn, calls = _run_task(watermark=T0, newest_closed=T1)
    assert calls["read"], "watermark behind newest closed (gap-invalidation) must recompute"
    print("OK  recompute: watermark jumped back -> read_candles runs")


def test_first_run_never_skips_and_never_probes_candles():
    conn, calls = _run_task(watermark=None, newest_closed=None)
    assert calls["read"], "first run (no watermark) must do the full load"
    assert ("candles", False) not in calls["probes"], "first run must not pay the extra candles probe"
    print("OK  first run: no skip probe, full load path")


def test_table_exists_positive_cache():
    probes = []
    ENGINE._WORKER["tables"] = set()
    _run_task(watermark=T1, newest_closed=T1, table_probes=probes)
    first = len(probes)
    assert first == 2, f"first task must probe both tables, got {probes}"
    _run_task(watermark=T1, newest_closed=T1, table_probes=probes)
    assert len(probes) == first, f"second task must hit the positive cache, got {probes}"
    print("OK  table_exists: probed once per worker, positive cache hit on task 2")


def test_broken_connection_is_discarded():
    conn = FakeConn(rollback_raises=True)
    _run_task(watermark=T1, newest_closed=T1, conn=conn)
    assert conn.closed == 1, "failed rollback must discard (close) the connection"
    print("OK  broken connection: rollback failure -> discarded, next task reconnects")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    test_skip_when_no_newer_closed_candle()
    test_skip_when_candles_behind_watermark()
    test_skip_when_no_closed_candle_at_all()
    test_recompute_when_new_closed_candle()
    test_recompute_when_watermark_jumped_back()
    test_first_run_never_skips_and_never_probes_candles()
    test_table_exists_positive_cache()
    test_broken_connection_is_discarded()
    print("\nAlle T-174 Skip-/Connection-Guards bestanden.")

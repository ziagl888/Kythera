# backtest/test_candles_schema.py
"""
Unit tests for core.candles_schema — the C-Gate Phase-0 hypertable DDL
(T-2026-CU-9050-118, umbrella T-2026-CU-9050-018).

Pins three things, all DB-free:
  1. The indicators hypertable schema is derived from the ONE canonical source
     (2_indicator_engine.get_indicator_definitions) and matches it column-for-
     column, with REAL -> double precision and TEXT preserved.
  2. ensure_hypertables() executes exactly the Phase-0 DDL (two CREATE TABLE, two
     create_hypertable, two CREATE INDEX, one commit) and — the operator decision —
     NO compression / NO retention policy (those are deferred to Phase 5).
  3. The type mapping and column-name lowercasing (writer parity) are correct.

Run with: pytest backtest/test_candles_schema.py -v
      or:  python backtest/test_candles_schema.py
"""

from __future__ import annotations

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.config (pulled in transitively by the indicator engine) only READS env vars;
# dummy values make the import DB-free (mirror of test_gap_continuity.py).
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import core.candles_schema as cs  # noqa: E402


def _load_engine():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "2_indicator_engine.py")
    spec = importlib.util.spec_from_file_location("kythera_indicator_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fake DB connection that records executed SQL (no psycopg2, no network).
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, rec):
        self._rec = rec

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._rec.append((sql, params))


class _FakeConn:
    def __init__(self):
        self.executed: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _FakeCursor(self.executed)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_pg_type_mapping():
    assert cs._pg_type("REAL") == "double precision"
    assert cs._pg_type("real") == "double precision"
    assert cs._pg_type("TEXT") == "text"
    assert cs._pg_type(" Text ") == "text"


def test_build_indicators_ddl_with_injected_defs():
    """Fixed keys + is_closed + close, then each def; REAL->double, TEXT->text,
    names lowercased."""
    ddl = cs.build_indicators_ddl({"RSI_6": "REAL", "TREND_DIRECTION": "TEXT"})
    norm = " ".join(ddl.split())
    for fixed in (
        "symbol text NOT NULL",
        "tf text NOT NULL",
        "open_time timestamptz NOT NULL",
        "is_closed boolean NOT NULL DEFAULT false",
        "close double precision",
        "rsi_6 double precision",
        "trend_direction text",
        "PRIMARY KEY (symbol, tf, open_time)",
    ):
        assert fixed in norm, f"missing {fixed!r} in:\n{norm}"
    # Uppercase engine keys must NOT leak into the DDL.
    assert "RSI_6" not in ddl
    assert "TREND_DIRECTION" not in ddl


def test_indicators_ddl_matches_canonical_engine_definitions():
    eng = _load_engine()
    defs = eng.get_indicator_definitions()
    ddl = cs.build_indicators_ddl()  # None -> loads the same canonical defs
    norm = " ".join(ddl.split())

    real_defs = [n for n, t in defs.items() if t.upper() == "REAL"]
    text_defs = [n for n, t in defs.items() if t.upper() == "TEXT"]

    # Every canonical column appears, correctly typed and lowercased.
    for name in real_defs:
        assert f"{name.lower()} double precision" in norm, f"missing REAL col {name}"
    for name in text_defs:
        assert f"{name.lower()} text" in norm, f"missing TEXT col {name}"

    # Column accounting: 5 fixed cols (symbol, tf, open_time, is_closed, close) + defs.
    # doubles = close + every REAL def; text cols = symbol, tf + every TEXT def.
    assert norm.count("double precision") == 1 + len(real_defs)
    assert norm.count(" text ") + norm.count(" text,") == 2 + len(text_defs)


def test_ensure_hypertables_executes_phase0_ddl_only():
    conn = _FakeConn()
    cs.ensure_hypertables(conn)

    sqls = [s for s, _ in conn.executed]
    joined = " ".join(sqls).lower()

    assert conn.commits == 1
    assert conn.rollbacks == 0

    # Two tables, two hypertables, two indexes.
    assert sum("create table if not exists candles" in s.lower() for s in sqls) == 1
    assert sum("create table if not exists indicators" in s.lower() for s in sqls) == 1
    assert joined.count("create_hypertable") == 2
    assert joined.count("create index if not exists") == 2

    # create_hypertable is parameterised with each table name and 'open_time'.
    params = [p for _, p in conn.executed if p]
    assert ("candles",) in params
    assert ("indicators",) in params
    assert all("'open_time'" in s for s in sqls if "create_hypertable" in s.lower())

    # Operator decision: Phase 0 defers compression + retention — NEITHER may appear.
    assert "compress" not in joined, "compression must be deferred to Phase 5"
    assert "retention" not in joined, "retention stays unlimited / not set in Phase 0"


def test_ensure_hypertables_rolls_back_on_failure():
    class _BoomConn(_FakeConn):
        def cursor(self):
            raise RuntimeError("boom")

    conn = _BoomConn()
    raised = False
    try:
        cs.ensure_hypertables(conn)
    except RuntimeError:
        raised = True
    assert raised
    assert conn.rollbacks == 1
    assert conn.commits == 0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

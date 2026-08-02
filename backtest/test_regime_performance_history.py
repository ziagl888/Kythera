# backtest/test_regime_performance_history.py
"""
Snapshot history for bot_regime_performance (T-2026-KYT-9050-072).

`bot_regime_performance` is a snapshot: one row per cell, overwritten on every
analyzer run (measured 2026-08-02, zero cells with more than one row). So the
statistics the whitelist gate decided on at the time of any past event are gone,
and no gate variant — v1, v2 or a future one — can be checked against its own
past. T-2026-KYT-9050-007 had to score today's statistics against yesterday's
traffic and could not separate the parameter effect from cell drift; T-031 hit
the same wall.

What this file holds down:

  * the history write runs in a SAVEPOINT. It is a measurement aid; the upsert
    beneath it feeds the live gate. A failure here — permissions, disk — must not
    drag the main write into the rollback, and without a savepoint it would,
    because both sit in the same transaction.
  * a failure returns 0 and logs; it never raises into compute_performance.
  * the snapshot key is the calendar DAY, so several runs a day collapse to one
    row per cell instead of growing without bound.
  * the rows written are the SAME tuples the main upsert uses — one source, not
    a second computation that can drift.

Run with: pytest backtest/test_regime_performance_history.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_analyzer():
    spec = importlib.util.spec_from_file_location(
        "bot_regime_analyzer", os.path.join(_REPO, "27_bot_regime_analyzer.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bot_regime_analyzer"] = mod
    spec.loader.exec_module(mod)
    return mod


BRA = _load_analyzer()

ROW = ("ROM1", "TREND_UP", "ALT_NEUTRAL", "LONG", 30, 42, 0.5, 0.1, 0.05, 1.2, 0.3, -2.0, 3.0, datetime.now())


class _Cursor:
    def __init__(self, fail_on: str | None = None):
        self.statements: list[str] = []
        self.values_calls: list[list] = []
        self._fail_on = fail_on

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.statements.append(text)
        if self._fail_on and self._fail_on in text:
            raise RuntimeError("boom")


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1


def _run(cursor, rows=(ROW,)):
    with mock.patch.object(BRA.pg_extras, "execute_values") as ev:
        out = BRA.append_performance_history(_Conn(cursor), list(rows))
    return out, cursor, ev


# ── the savepoint, which is the whole safety story ───────────────────────────


def test_history_write_is_wrapped_in_a_savepoint():
    _, cur, _ = _run(_Cursor())
    joined = " | ".join(cur.statements)
    assert "SAVEPOINT regime_history" in joined
    assert "RELEASE SAVEPOINT regime_history" in joined


def test_a_failure_rolls_back_only_to_the_savepoint():
    """The money path must survive a broken measurement aid."""
    cur = _Cursor(fail_on="CREATE TABLE IF NOT EXISTS")
    out, cur, _ = _run(cur)
    assert out == 0
    assert any("ROLLBACK TO SAVEPOINT regime_history" in s for s in cur.statements)


def test_a_failure_never_raises_into_the_caller():
    cur = _Cursor(fail_on="DELETE FROM")
    out, _, _ = _run(cur)
    assert out == 0, "compute_performance must keep its upsert even when the history fails"


def test_the_history_write_does_not_commit():
    """Hard rule 8 — the caller owns the transaction."""
    conn = _Conn(_Cursor())
    with mock.patch.object(BRA.pg_extras, "execute_values"):
        BRA.append_performance_history(conn, [ROW])
    assert conn.commits == 0


# ── the snapshot key ─────────────────────────────────────────────────────────


def test_rows_are_stamped_with_todays_date_and_keep_the_original_tuple():
    out, _, ev = _run(_Cursor())
    assert out == 1
    written = ev.call_args[0][2]
    assert written[0][0] == datetime.now(timezone.utc).date()
    assert written[0][1:] == ROW, "the history must carry the SAME tuple as the main upsert"


def test_the_day_is_the_conflict_key_so_reruns_collapse():
    # The INSERT goes through pg_extras.execute_values, not cur.execute — assert
    # on the SQL it actually receives.
    _, _, ev = _run(_Cursor())
    insert = " ".join(str(ev.call_args[0][1]).split())
    assert "ON CONFLICT (snapshot_date, bot_name, regime, alt_context, direction, window_days)" in insert
    assert "DO UPDATE SET" in insert


def test_empty_input_is_a_no_op():
    conn = _Conn(_Cursor())
    with mock.patch.object(BRA.pg_extras, "execute_values") as ev:
        assert BRA.append_performance_history(conn, []) == 0
    ev.assert_not_called()


# ── retention ────────────────────────────────────────────────────────────────


def test_retention_deletes_older_than_the_configured_window():
    cur = _Cursor()
    with mock.patch.dict(os.environ, {BRA.HISTORY_RETENTION_ENV: "10"}), mock.patch.object(
        BRA.pg_extras, "execute_values"
    ):
        BRA.append_performance_history(_Conn(cur), [ROW])
    assert any("DELETE FROM" in s and "snapshot_date <" in s for s in cur.statements)


def test_a_bad_retention_value_falls_back_to_the_default():
    for bad in ("", "not-a-number", "0", "-5"):
        with mock.patch.dict(os.environ, {BRA.HISTORY_RETENTION_ENV: bad}):
            assert BRA._history_retention_days() == BRA.HISTORY_RETENTION_DEFAULT_DAYS


def test_retention_default_covers_more_than_a_year():
    """A year of hindsight plus buffer — shorter would re-create the very gap
    this table exists to close, just later."""
    assert BRA.HISTORY_RETENTION_DEFAULT_DAYS > 365
    cutoff = date(2026, 8, 2) - timedelta(days=BRA.HISTORY_RETENTION_DEFAULT_DAYS)
    assert cutoff < date(2025, 8, 2)

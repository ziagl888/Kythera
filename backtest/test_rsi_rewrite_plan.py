# backtest/test_rsi_rewrite_plan.py
"""
Unit tests for tools/recompute_indicators.rsi_rewrite_plan / rewrite_rsi_rows /
assert_wilder_engine (T-2026-CU-9050-099, P2.12 follow-up).

The RSI rewrite is the deliberate opposite of the P1.13 head-nulling: it DOES
change mid-band values (ewm(span) -> Wilder domain migration), but only on the
rsi_* columns, never inside the bot-2 tail window, and it must be idempotent —
a cell already holding the Wilder value is skipped, so a second run writes
nothing. These tests pin exactly those boundaries on synthetic frames, DB-free.

Run with: pytest backtest/test_rsi_rewrite_plan.py -v
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from tools.recompute_indicators import (  # noqa: E402
    _WILDER_WITNESS_RSI14,
    RSI_ABS_TOL,
    TAIL_ROWS,
    assert_wilder_engine,
    rewrite_rsi_rows,
    rsi_rewrite_plan,
)


def _times(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")


def _frame(times, **cols) -> pd.DataFrame:
    d = {"open_time": times}
    d.update(cols)
    return pd.DataFrame(d)


def test_domain_shift_cells_get_the_wilder_value():
    """Stored ewm values differing from the Wilder recompute are rewritten."""
    n = 12
    t = _times(n)
    db_rsi = np.full(n, 60.0)  # old ewm(span) domain
    rc_rsi = np.full(n, 55.2)  # Wilder recompute
    db = _frame(t, RSI_14=db_rsi)
    rc = _frame(t, RSI_14=rc_rsi)

    plan = rsi_rewrite_plan(db, rc, ["RSI_14"])

    expected_rows = n - TAIL_ROWS
    assert plan["cells"] == expected_rows
    assert plan["to_null"] == 0 and plan["null_fills"] == 0
    vals = plan["cols"]["RSI_14"]["values"]
    assert all(v == 55.2 for v in vals), "the WRITTEN value is the recompute, not the DB value"
    assert plan["delta_max"] == plan["delta_sum"] / expected_rows  # constant shift


def test_unchanged_cells_are_skipped_idempotent():
    """A cell already inside RSI_ABS_TOL of the Wilder value is not rewritten."""
    n = 10
    t = _times(n)
    rc_rsi = np.linspace(30.0, 70.0, n)
    db_rsi = rc_rsi + RSI_ABS_TOL / 10  # float4 round-trip noise, below tol
    plan = rsi_rewrite_plan(_frame(t, RSI_14=db_rsi), _frame(t, RSI_14=rc_rsi), ["RSI_14"])
    assert plan["cells"] == 0, "second run after execute must be a no-op"


def test_tail_rows_are_never_written():
    """Cells inside the newest TAIL_ROWS are excluded (bot-2 race)."""
    n = 10
    t = _times(n)
    db_rsi = np.full(n, 60.0)
    rc_rsi = np.full(n, 50.0)
    plan = rsi_rewrite_plan(_frame(t, RSI_14=db_rsi), _frame(t, RSI_14=rc_rsi), ["RSI_14"])
    times = plan["cols"]["RSI_14"]["times"]
    assert len(times) == n - TAIL_ROWS
    assert all(ts < t[n - TAIL_ROWS] for ts in times), "no write in the tail window"


def test_finite_to_nan_becomes_null_and_null_fill_gets_value():
    """Wilder-NaN over a stored value -> NULL; stored NULL under a finite Wilder -> filled."""
    n = 12
    t = _times(n)
    db_rsi = np.full(n, 50.0)
    db_rsi[3] = np.nan  # stored NULL, Wilder defined -> fill
    rc_rsi = np.full(n, 50.0)
    rc_rsi[1] = np.nan  # Wilder undefined (warmup/flat), stored finite -> NULL

    plan = rsi_rewrite_plan(_frame(t, RSI_14=db_rsi), _frame(t, RSI_14=rc_rsi), ["RSI_14"])

    assert plan["to_null"] == 1
    assert plan["null_fills"] == 1
    assert plan["cells"] == 2, "identical finite cells are untouched"
    by_time = dict(zip(plan["cols"]["RSI_14"]["times"], plan["cols"]["RSI_14"]["values"]))
    assert by_time[t[1]] is None, "NaN target travels as None -> SQL NULL"
    assert by_time[t[3]] == 50.0


def test_only_rsi_columns_are_considered():
    """A diverging non-RSI column in the frames is ignored by the plan."""
    n = 8
    t = _times(n)
    db = _frame(t, RSI_14=np.full(n, 50.0), WMA_7=np.full(n, 1.0))
    rc = _frame(t, RSI_14=np.full(n, 50.0), WMA_7=np.full(n, 9.0))
    plan = rsi_rewrite_plan(db, rc, ["RSI_14"])
    assert plan["cells"] == 0
    assert "WMA_7" not in plan["cols"]


class _FakeCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))


def test_rewrite_rsi_rows_batched_parameterised_update():
    """One UPDATE per column, times/values as parallel arrays, None preserved."""
    t = _times(4)
    cols_plan = {
        "RSI_14": {"times": [t[0], t[1]], "values": [55.2, None]},
        "RSI_9": {"times": [], "values": []},  # nothing pending -> skipped
    }
    cur = _FakeCursor()
    n = rewrite_rsi_rows(cur, '"FOOUSDT_1h_indicators"', cols_plan)

    assert n == 1
    assert len(cur.calls) == 1
    sql, params = cur.calls[0]
    assert 'SET "rsi_14" = u.val' in sql, "column lower-cased, value from unnest"
    assert "unnest(%s::timestamptz[])" in sql and "unnest(%s::float8[])" in sql
    assert "WHERE t.open_time = u.ot" in sql
    assert params == ([t[0], t[1]], [55.2, None]), "parallel arrays, None -> NULL"


def test_rewrite_rsi_rows_noop_when_empty():
    cur = _FakeCursor()
    assert rewrite_rsi_rows(cur, '"X_1h_indicators"', {"RSI_14": {"times": [], "values": []}}) == 0
    assert cur.calls == []


def test_engine_parity_witness_is_wilder_not_span():
    """The loaded engine must reproduce the Wilder witness; a span build must fail.

    Runs the real repo engine through assert_wilder_engine (must pass), then a
    fake span-based engine (must exit) — pinning that the guard actually
    discriminates the two domains instead of accepting anything.
    """
    assert_wilder_engine()  # repo engine is the T-095 Wilder build

    class _SpanEngine:
        @staticmethod
        def calculate_rsi(series, period=14):
            delta = series.diff()
            up = delta.clip(lower=0)
            down = -1 * delta.clip(upper=0)
            roll_up = up.ewm(span=period, adjust=False).mean()
            roll_down = down.ewm(span=period, adjust=False).mean()
            return 100.0 - (100.0 / (1.0 + roll_up / roll_down))

    try:
        assert_wilder_engine(_SpanEngine())
    except SystemExit as e:
        assert "parity check FAILED" in str(e)
    else:
        raise AssertionError("span-based engine must be rejected")


def test_witness_constant_is_in_rsi_range():
    assert 0.0 < _WILDER_WITNESS_RSI14 < 100.0

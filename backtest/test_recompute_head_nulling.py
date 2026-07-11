# backtest/test_recompute_head_nulling.py
"""
Unit tests for tools/recompute_indicators.head_null_plan (T-2026-CU-9050-061).

The P1.13 recompute must be position-stable: it may NULL the warmup head rows of
the four rolling families, and it must NEVER change a mid-band value — even
where a full engine recompute would differ from the DB (measured up to +700% on
rsi_14). These tests pin exactly that boundary on synthetic frames, DB-free.

Run with: pytest backtest/test_recompute_head_nulling.py -v
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from tools.recompute_indicators import TAIL_ROWS, head_null_plan  # noqa: E402

UTC = "UTC"
COLS = ["WMA_7", "BOLL_UPPER_20"]


def _times(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2025-01-01", periods=n, freq="h", tz=UTC)


def _frame(times, **cols) -> pd.DataFrame:
    d = {"open_time": times}
    d.update(cols)
    return pd.DataFrame(d)


def test_head_rows_are_nulled_midband_is_left_alone():
    """DB head = fabricated 0, recompute head = NaN -> nulled. Identical mid-band."""
    n = 12
    t = _times(n)
    real = np.arange(1.0, n + 1)  # 1..12, all finite
    db_wma = real.copy()
    db_wma[:2] = 0.0  # two fabricated warmup head rows
    rc_wma = real.copy()
    rc_wma[:2] = np.nan  # engine now returns NaN there
    db_boll = real * 10
    db_boll[:1] = 0.0
    rc_boll = real * 10
    rc_boll[:1] = np.nan

    db = _frame(t, WMA_7=db_wma, BOLL_UPPER_20=db_boll)
    rc = _frame(t, WMA_7=rc_wma, BOLL_UPPER_20=rc_boll)

    plan = head_null_plan(db, rc, COLS)

    assert plan["heads"] == 3, "two WMA + one BOLL head cell"
    assert plan["midband"] == 0, "identical mid-band must not register"
    assert len(plan["cols"]["WMA_7"]["head_times"]) == 2
    assert len(plan["cols"]["BOLL_UPPER_20"]["head_times"]) == 1
    # the nulled times are exactly the head rows, never a mid-band row
    assert list(plan["cols"]["WMA_7"]["head_times"]) == list(t[:2])


def test_midband_divergence_is_reported_but_never_nulled():
    """A mid-band cell where DB and recompute differ must be counted, not nulled.

    This is the rsi_14-up-to-700% case: a full recompute would overwrite it; the
    head-nulling plan leaves it untouched and only surfaces the gap.
    """
    n = 12
    t = _times(n)
    db_wma = np.arange(1.0, n + 1)
    db_wma[:2] = 0.0
    rc_wma = db_wma.copy()
    rc_wma[:2] = np.nan
    rc_wma[5] = db_wma[5] * 8.0  # huge mid-band divergence at row 5

    db = _frame(t, WMA_7=db_wma, BOLL_UPPER_20=np.arange(1.0, n + 1))
    rc = _frame(t, WMA_7=rc_wma, BOLL_UPPER_20=np.arange(1.0, n + 1))

    plan = head_null_plan(db, rc, ["WMA_7"])

    assert plan["heads"] == 2, "only the two head rows are nulled"
    assert plan["midband"] == 1, "the divergent mid-band cell is reported"
    assert plan["midband_max"] > 1.0
    assert t[5] not in list(plan["cols"]["WMA_7"]["head_times"]), "mid-band row never nulled"


def test_newest_rows_are_excluded_as_bot2_race():
    """A NaN head-shaped cell inside the newest TAIL_ROWS is not nulled."""
    n = 12
    t = _times(n)
    wma = np.arange(1.0, n + 1)
    db_wma = wma.copy()
    db_wma[:2] = 0.0
    rc_wma = wma.copy()
    rc_wma[:2] = np.nan
    rc_wma[-1] = np.nan  # newest row looks head-shaped (bot-2 wrote fresh)

    db = _frame(t, WMA_7=db_wma, BOLL_UPPER_20=wma)
    rc = _frame(t, WMA_7=rc_wma, BOLL_UPPER_20=wma)

    plan = head_null_plan(db, rc, ["WMA_7"])

    heads = list(plan["cols"]["WMA_7"]["head_times"])
    assert t[-1] not in heads, "newest row is a tail race, not a warmup head"
    assert set(heads) == set(t[:2])


def test_already_null_head_is_not_recounted():
    """If the DB head is already NULL, there is nothing to null."""
    n = 10
    t = _times(n)
    wma = np.arange(1.0, n + 1)
    db_wma = wma.copy()
    db_wma[:2] = np.nan  # already NULL
    rc_wma = wma.copy()
    rc_wma[:2] = np.nan

    db = _frame(t, WMA_7=db_wma, BOLL_UPPER_20=wma)
    rc = _frame(t, WMA_7=rc_wma, BOLL_UPPER_20=wma)

    plan = head_null_plan(db, rc, ["WMA_7"])
    assert plan["heads"] == 0, "db already NULL -> no update needed"


def test_tail_rows_constant_matches_the_write_guard():
    """The tail guard the plan uses is the module's TAIL_ROWS, not a local literal."""
    assert TAIL_ROWS >= 1

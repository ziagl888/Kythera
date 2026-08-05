# backtest/test_leg_composition_replay.py — the replay conventions, pinned.
"""Standalone, DB-free tests for tools/leg_composition_replay.py (T-2026-KYT-9050-104).

Every number the composition decision rests on is produced by four conventions:
first touch, entry candle excluded, tie-books-as-SL, and mark-to-market at the
horizon. Each of them can silently flip a leg from negative to positive
expectancy, so each of them is pinned here rather than left to a docstring.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.leg_composition_replay import (  # noqa: E402
    REGIME_CUTOFF_EPOCH,
    SL_GRID,
    TP_GRID,
    aggregate,
    outcome,
    replay_signal,
)


def candles(*bars: tuple[float, float, float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(high, low, close) arrays from (high, low, close) triples."""
    arr = np.array(bars, dtype=np.float64)
    return arr[:, 0], arr[:, 1], arr[:, 2]


# ── first touch ──────────────────────────────────────────────────────────────


def test_long_tp_first_touch_uses_the_wick():
    """A high that pierces the level counts, even if the candle closes below it."""
    high, low, close = candles((104.0, 99.0, 100.0), (106.0, 103.0, 103.5))
    i_tp, _, _, mfe, _ = replay_signal(high, low, close, entry=100.0, is_long=True)
    assert i_tp["3.0"] == 0  # +4% wick on bar 0 already clears a 3% target
    assert i_tp["5.0"] == 1
    assert mfe == pytest.approx(6.0)


def test_short_direction_is_mirrored():
    """For a SHORT the favourable excursion is measured off the low."""
    high, low, close = candles((101.0, 95.0, 96.0),)
    i_tp, i_sl, mark, mfe, mae = replay_signal(high, low, close, entry=100.0, is_long=False)
    assert i_tp["4.0"] == 0
    assert mfe == pytest.approx(5.0)
    assert mae == pytest.approx(1.0)
    assert mark == pytest.approx(4.0)  # closed at 96 on a short entered at 100


def test_level_never_reached_is_none_not_zero():
    """`None` and index 0 mean opposite things — a mix-up books every miss as an
    instant hit."""
    high, low, close = candles((101.0, 99.5, 100.0), (101.5, 99.0, 101.0))
    i_tp, i_sl, _, _, _ = replay_signal(high, low, close, entry=100.0, is_long=True)
    assert i_tp["3.0"] is None
    assert i_sl["3.0"] is None


# ── tie-breaking and ordering ────────────────────────────────────────────────


def test_tp_and_sl_in_the_same_candle_books_as_sl():
    """Intra-candle order is unknowable; the conservative read is the stop."""
    high, low, close = candles((106.0, 94.0, 100.0))
    i_tp, i_sl, _, _, _ = replay_signal(high, low, close, entry=100.0, is_long=True)
    assert i_tp["5.0"] == 0 and i_sl["5.0"] == 0
    assert outcome(i_tp["5.0"], i_sl["5.0"]) == "SL"


def test_outcome_prefers_tp_only_when_strictly_earlier():
    assert outcome(1, 3) == "TP"
    assert outcome(3, 1) == "SL"
    assert outcome(2, 2) == "SL"
    assert outcome(None, 4) == "SL"
    assert outcome(4, None) == "TP"
    assert outcome(None, None) == "OPEN"


def test_running_maximum_is_monotone_so_searchsorted_is_valid():
    """The vectorised first-touch relies on a monotone cumulative maximum.

    A dip after a peak must not reset the index — otherwise a trade that reached
    the target and pulled back would be scored as never having reached it.
    """
    high, low, close = candles((108.0, 100.0, 107.0), (102.0, 101.0, 101.5), (103.0, 100.5, 102.0))
    i_tp, _, _, mfe, _ = replay_signal(high, low, close, entry=100.0, is_long=True)
    assert i_tp["8.0"] == 0
    assert mfe == pytest.approx(8.0)


# ── mark-to-market ───────────────────────────────────────────────────────────


def test_unresolved_is_marked_at_the_last_close_not_dropped():
    """Dropping unresolved trades keeps only the ones that resolved — survivorship."""
    high, low, close = candles((101.0, 99.0, 100.5), (101.0, 98.0, 98.5))
    i_tp, i_sl, mark, _, _ = replay_signal(high, low, close, entry=100.0, is_long=True)
    assert outcome(i_tp["5.0"], i_sl["5.0"]) == "OPEN"
    assert mark == pytest.approx(-1.5)


# ── aggregation and the regime split ─────────────────────────────────────────


def _row(model, direction, cohort, i_tp, i_sl, mark=0.0):
    """Keys come from the grids themselves — hard-coding them here made this
    helper break the moment the grid was extended, which is a test coupled to a
    constant rather than to behaviour."""
    return {
        "model": model,
        "direction": direction,
        "cohort": cohort,
        "i_tp": {str(d): i_tp for d in TP_GRID},
        "i_sl": {str(s): i_sl for s in SL_GRID},
        "mark": mark,
        "mfe": 0.0,
        "mae": 0.0,
    }


def test_cohorts_are_never_pooled():
    """The pre/post split is the whole point — a pooled cell hid a real verdict once."""
    rows = [
        _row("X", "LONG", "pre", None, 1),  # stop-out before the cutoff
        _row("X", "LONG", "post", 1, None),  # winner after it
    ]
    agg = aggregate(rows)
    assert set(agg) == {"X|LONG|pre", "X|LONG|post"}
    assert agg["X|LONG|pre"]["grid"]["4.0/5.0"]["sl"] == 1
    assert agg["X|LONG|post"]["grid"]["4.0/5.0"]["tp"] == 1
    # and they must not average into one another
    assert agg["X|LONG|pre"]["grid"]["4.0/5.0"]["exp_pp"] < 0
    assert agg["X|LONG|post"]["grid"]["4.0/5.0"]["exp_pp"] > 0


def test_directions_are_separate_cells():
    """A leg can be profitable long and lossy short — AIM2 is exactly that."""
    rows = [_row("AIM2", "LONG", "post", 1, None), _row("AIM2", "SHORT", "post", None, 1)]
    agg = aggregate(rows)
    assert agg["AIM2|LONG|post"]["grid"]["4.0/5.0"]["exp_pp"] == pytest.approx(4.0 - 0.09)
    assert agg["AIM2|SHORT|post"]["grid"]["4.0/5.0"]["exp_pp"] == pytest.approx(-5.0 - 0.09)


def test_expectancy_carries_the_fee_and_the_mark():
    rows = [
        _row("X", "LONG", "post", 1, None),  # +4.0
        _row("X", "LONG", "post", None, 1),  # -5.0
        _row("X", "LONG", "post", None, None, mark=1.0),  # +1.0 marked
    ]
    agg = aggregate(rows)
    cell = agg["X|LONG|post"]["grid"]["4.0/5.0"]
    assert (cell["tp"], cell["sl"], cell["open"]) == (1, 1, 1)
    assert cell["exp_pp"] == pytest.approx((4.0 - 5.0 + 1.0) / 3 - 0.09)


def test_regime_cutoff_is_the_documented_instant():
    """2026-07-28 14:00Z — EXPOSURE_CAP + time-stop go-live."""
    from datetime import datetime, timezone

    assert REGIME_CUTOFF_EPOCH == int(datetime(2026, 7, 28, 14, 0, tzinfo=timezone.utc).timestamp())

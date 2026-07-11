# backtest/test_gap_continuity.py
"""
Unit tests for the indicator-engine gap-continuity guard (P2.13,
T-2026-CU-9050-092).

The engine loads a long lookback to warm the rolling windows but only persists
the recent tail. Rolling a window across a candle gap computes e.g. a
"200-period MA" over a real-time discontinuity — garbage indicators exactly on
the coins whose data is holey. `find_contaminating_gap` refuses to compute in
that case, but ONLY when the gap sits within MAX_INDICATOR_LOOKBACK bars of a row
that will actually be written — so an old, scrolled-out gap does not freeze the
coin forever (its MAX(open_time) would otherwise never advance).

These tests pin exactly that boundary on synthetic frames, DB-free.

Run with: pytest backtest/test_gap_continuity.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")


def _load_engine():
    """Load 2_indicator_engine.py under a valid module name (leading digit)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "2_indicator_engine.py")
    spec = importlib.util.spec_from_file_location("kythera_indicator_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


eng = _load_engine()

TF = "1h"
DELTA = pd.Timedelta(hours=1)


def _times(n: int, start: str = "2025-01-01 00:00") -> list[pd.Timestamp]:
    """n contiguous hourly UTC timestamps."""
    return list(pd.date_range(start, periods=n, freq="h", tz="UTC"))


def test_contiguous_frame_has_no_gap():
    """The golden-fixture case: a gap-free frame never triggers the guard."""
    ot = _times(500)
    save_start = ot[-5]  # persist only the recent tail
    assert eng.find_contaminating_gap(pd.Series(ot), DELTA, save_start) is None


def test_recent_gap_in_save_region_is_flagged():
    """A missing candle right before the persisted rows contaminates them."""
    ot = _times(300)
    # drop candle 297 -> gap between index 296 and (old) 298
    del ot[297]
    save_start = ot[-5]
    gap = eng.find_contaminating_gap(pd.Series(ot), DELTA, save_start)
    assert gap is not None
    # the reported boundary spans exactly the hole (2h instead of 1h)
    assert gap[1] - gap[0] == pd.Timedelta(hours=2)


def test_old_gap_outside_lookback_is_ignored():
    """A gap older than MAX_INDICATOR_LOOKBACK bars before the first persisted row
    has scrolled out of every written window — must NOT freeze the coin."""
    ot = _times(600)
    # drop an early candle: gap ~index 50, first persisted row is near index 595,
    # so the gap is >200 bars behind it -> harmless.
    del ot[50]
    save_start = ot[-5]
    assert eng.find_contaminating_gap(pd.Series(ot), DELTA, save_start) is None


def test_gap_exactly_at_lookback_boundary():
    """A gap just inside the MAX_INDICATOR_LOOKBACK window IS flagged; just outside
    is not. Pins the boundary rather than a happy-path far from it."""
    n = 400
    first_save_idx = n - 1  # only the very last row is persisted
    # inside: gap MAX_INDICATOR_LOOKBACK-1 bars before the last row
    ot_in = _times(n)
    del ot_in[first_save_idx - (eng.MAX_INDICATOR_LOOKBACK - 1)]
    save_start = ot_in[-1]
    assert eng.find_contaminating_gap(pd.Series(ot_in), DELTA, save_start) is not None

    # outside: gap MAX_INDICATOR_LOOKBACK+5 bars before the last row
    ot_out = _times(n)
    del ot_out[first_save_idx - (eng.MAX_INDICATOR_LOOKBACK + 5)]
    save_start2 = ot_out[-1]
    assert eng.find_contaminating_gap(pd.Series(ot_out), DELTA, save_start2) is None


def test_no_rows_to_persist_never_flags():
    """If save_start_filter is in the future, nothing is written -> no contamination."""
    ot = _times(300)
    del ot[290]  # a real gap, but...
    future = ot[-1] + pd.Timedelta(days=1)  # ...nothing at/after this
    assert eng.find_contaminating_gap(pd.Series(ot), DELTA, future) is None


def test_short_frame_is_safe():
    """A single-row / empty frame never raises and reports no gap."""
    assert eng.find_contaminating_gap(pd.Series([], dtype="datetime64[ns, UTC]"), DELTA, None) is None
    one = _times(1)
    assert eng.find_contaminating_gap(pd.Series(one), DELTA, one[0]) is None


def test_multi_candle_gap_is_flagged():
    """A multi-candle hole (several missing bars) reports the full span."""
    ot = _times(300)
    del ot[295:298]  # drop 3 consecutive candles
    save_start = ot[-3]
    gap = eng.find_contaminating_gap(pd.Series(ot), DELTA, save_start)
    assert gap is not None
    assert gap[1] - gap[0] == pd.Timedelta(hours=4)  # 3 missing -> 4h step


def test_sub_candle_jitter_is_not_a_gap():
    """A sub-candle timestamp wobble (< 1.5x delta) is tolerated, not flagged."""
    ot = _times(300)
    # nudge one timestamp forward by 20 min: 1h20m step < 1.5h threshold
    ot[298] = ot[298] + pd.Timedelta(minutes=20)
    save_start = ot[-5]
    assert eng.find_contaminating_gap(pd.Series(ot), DELTA, save_start) is None

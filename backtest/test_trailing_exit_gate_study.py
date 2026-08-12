"""DB-free tests for the pure logic of tools/trailing_exit_gate_study.py (T-139).

Covers the pieces a wrong sign or an off-by-one would silently corrupt: the tape flags,
the closed-candle state lookup, the first-fire semantics of the three gate variants
(including the at-fill exit and the grace period), and the paired t.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.trailing_exit_gate_study import (  # noqa: E402
    GateExit,
    first_gate_fire,
    paired_stats,
    tape_down_series,
    tape_state_at,
)


def _utc(*args: int) -> pd.Timestamp:
    return pd.Timestamp(datetime(*args, tzinfo=timezone.utc))


def _btc(closes: list[float], start_hour: int = 0) -> pd.DataFrame:
    times = [_utc(2026, 8, 1, start_hour) + pd.Timedelta(hours=i) for i in range(len(closes))]
    return pd.DataFrame({"open_time": times, "close": closes})


def test_td1_momentum_flag() -> None:
    # 4h momentum: close < close 4 candles earlier. Build 6 falling then rising closes.
    tape = tape_down_series(_btc([100, 100, 100, 100, 99, 101]))
    # candle idx 4 (close 99) vs idx 0 (100) -> down; idx 5 (101) vs idx 1 (100) -> up.
    assert bool(tape["TD1"].iloc[4]) is True
    assert bool(tape["TD1"].iloc[5]) is False
    # Warm-up (fewer than 4 candles of history) must be False, never NaN-truthy.
    assert not tape["TD1"].iloc[:4].any()


def test_td2_mean_flag_warmup_false() -> None:
    closes = [100.0] * 23 + [50.0, 49.0]
    tape = tape_down_series(_btc(closes))
    assert not tape["TD2"].iloc[:23].any()  # rolling(24) undefined -> False
    assert bool(tape["TD2"].iloc[24]) is True  # 49 far below the 24-candle mean


def test_tape_state_at_uses_last_closed_candle_only() -> None:
    tape = tape_down_series(_btc([100, 100, 100, 100, 90, 90]))
    close_of_down_candle = tape["close_time"].iloc[4]
    # One second BEFORE the down candle closes, its flag must not leak (hard rule 5).
    assert tape_state_at(tape, close_of_down_candle - pd.Timedelta(seconds=1), "TD1") != bool(
        tape["TD1"].iloc[4]
    ) or not bool(tape["TD1"].iloc[4])
    assert tape_state_at(tape, close_of_down_candle, "TD1") is True
    # Before any candle closed: False.
    assert tape_state_at(tape, _utc(2026, 7, 31, 0), "TD1") is False


def _tape_down_from(idx_down: set[int], n: int = 12) -> pd.DataFrame:
    times = [_utc(2026, 8, 1, 1) + pd.Timedelta(hours=i) for i in range(n)]
    return pd.DataFrame(
        {
            "close_time": times,
            "TD1": [i in idx_down for i in range(n)],
            "TD2": [False] * n,
        }
    )


def test_ga_fires_at_fill_when_tape_already_down() -> None:
    tape = _tape_down_from({0, 1, 2})
    filled = tape["close_time"].iloc[0] + pd.Timedelta(minutes=30)
    fire = first_gate_fire(filled, filled + pd.Timedelta(hours=5), 100.0, tape, "TD1", "G-A", pd.Series(dtype=float))
    assert fire == GateExit(instant=filled, at_fill=True)


def test_gb_requires_underwater_mark() -> None:
    tape = _tape_down_from({1, 2, 3})
    filled = tape["close_time"].iloc[0] + pd.Timedelta(minutes=10)
    closed = filled + pd.Timedelta(hours=6)
    hours = tape["close_time"]
    # Mark above entry at hour 1 (not underwater), below entry at hour 2.
    marks = pd.Series({hours.iloc[1]: 105.0, hours.iloc[2]: 95.0})
    fire = first_gate_fire(filled, closed, 100.0, tape, "TD1", "G-B", marks)
    assert fire is not None and fire.instant == hours.iloc[2] and not fire.at_fill
    # G-A ignores the mark and fires an hour earlier.
    fire_a = first_gate_fire(filled, closed, 100.0, tape, "TD1", "G-A", marks)
    assert fire_a is not None and fire_a.instant == hours.iloc[1]


def test_gc_grace_period_delays_the_fire() -> None:
    tape = _tape_down_from({0, 1, 2, 3})
    hours = tape["close_time"]
    filled = hours.iloc[0] - pd.Timedelta(minutes=30)  # 30 min before the first down close
    closed = filled + pd.Timedelta(hours=6)
    marks = pd.Series({hours.iloc[0]: 95.0, hours.iloc[1]: 95.0, hours.iloc[2]: 95.0})
    fire_b = first_gate_fire(filled, closed, 100.0, tape, "TD1", "G-B", marks)
    fire_c = first_gate_fire(filled, closed, 100.0, tape, "TD1", "G-C", marks)
    assert fire_b is not None and fire_b.instant == hours.iloc[0]  # 30 min in: G-B fires
    assert fire_c is not None and fire_c.instant == hours.iloc[1]  # G-C waits >= 1 h


def test_fire_never_at_or_after_actual_close() -> None:
    tape = _tape_down_from({5})
    filled = tape["close_time"].iloc[0] + pd.Timedelta(minutes=5)
    closed = tape["close_time"].iloc[5]  # actual close AT the down instant
    fire = first_gate_fire(filled, closed, 100.0, tape, "TD1", "G-A", pd.Series(dtype=float))
    assert fire is None  # instant >= closed_at is out of the trade's life


def test_paired_stats_matches_manual_t() -> None:
    deltas = np.array([1.0, 2.0, 3.0, -1.0, 0.0, 0.0])
    s = paired_stats(deltas)
    mean = deltas.mean()
    t_manual = mean / (deltas.std(ddof=1) / math.sqrt(len(deltas)))
    assert abs(s["mean"] - mean) < 1e-12
    assert abs(s["t"] - t_manual) < 1e-12
    assert paired_stats(np.array([]))["n"] == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK {name}")
    print("all tests passed")

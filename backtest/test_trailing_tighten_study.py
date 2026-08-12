"""DB-free tests for the pure logic of tools/trailing_tighten_study.py (T-141).

Exercises the counterfactual mechanics against the bot's own TrailingState: the live
baseline, the parameter switch (peak survives), the A3 activation-zero behaviour on a
never-armed loser, and the earlier-of overlay rule.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.trailing_tighten_study import (  # noqa: E402
    VARIANTS,
    overlay_outcome,
    replay_trail,
)


def _path(prices: list[float], start: float = 1000.0) -> tuple[np.ndarray, np.ndarray]:
    ts = start + 10.0 * np.arange(len(prices))
    return ts, np.array(prices, dtype=float)


def test_baseline_trail_closes_on_retrace() -> None:
    # LONG entry 100: peak 5 % at 105, live stop = 5 * 0.9 = 4.5 -> closes at 4.4.
    ts, price = _path([100, 102, 105, 104.6, 104.4])
    out = replay_trail(100.0, True, ts, price, None, None)
    assert out is not None
    close_ts, close_mark = out
    assert close_ts == ts[4]
    assert abs(close_mark - 4.4) < 1e-9


def test_a1_tightened_retrace_closes_earlier() -> None:
    ts, price = _path([100, 102, 105, 104.6, 104.4])
    out = replay_trail(100.0, True, ts, price, ts[0], VARIANTS["A1-retrace-half"])
    assert out is not None
    assert out[0] == ts[3]  # stop 5 * 0.95 = 4.75 -> mark 4.6 closes one tick earlier
    assert abs(out[1] - 4.6) < 1e-9


def test_never_armed_loser_baseline_never_closes_but_a3_does() -> None:
    # Peak 0.5 % never crosses the 2 % activation: the live trail is blind to this loser.
    ts, price = _path([100, 100.5, 99, 98])
    assert replay_trail(100.0, True, ts, price, None, None) is None
    out = replay_trail(100.0, True, ts, price, ts[0], VARIANTS["A3-activation-zero"])
    assert out is not None
    assert out[0] == ts[2]  # armed at peak 0.5, stop 0.45 -> mark −1 closes
    assert abs(out[1] - (-1.0)) < 1e-9


def test_peak_survives_the_switch() -> None:
    # Peak is made BEFORE the switch; the tightened stop must use it, not re-arm.
    ts, price = _path([100, 105, 104.8, 104.6])
    out = replay_trail(100.0, True, ts, price, ts[2], VARIANTS["A1-retrace-half"])
    assert out is not None
    # stop after switch = 5 * 0.95 = 4.75 -> the 104.6 tick (mark 4.6) closes.
    assert out[0] == ts[3] and abs(out[1] - 4.6) < 1e-9


def test_switch_only_applies_from_switch_instant() -> None:
    # Before the switch the live stop (4.5) governs: mark 4.6 must NOT close.
    ts, price = _path([100, 105, 104.6, 104.6])
    out_live = replay_trail(100.0, True, ts, price, None, None)
    assert out_live is None  # 4.6 > 4.5 -> live trail never closes on this path
    out = replay_trail(100.0, True, ts, price, ts[3], VARIANTS["A1-retrace-half"])
    assert out is not None and out[0] == ts[3]  # tightened stop 4.75 fires at the switch tick


def test_short_direction() -> None:
    # SHORT entry 100: price falls to 95 -> mark +5; rebound to 95.5 -> mark 4.5 closes.
    ts, price = _path([100, 97, 95, 95.5])
    out = replay_trail(100.0, False, ts, price, None, None)
    assert out is not None
    assert abs(out[1] - 4.5) < 1e-9


def test_overlay_earlier_of_rule() -> None:
    assert overlay_outcome((50.0, 1.2), 100.0, -3.0) == (1.2, "TIGHTENED_TRAIL")
    assert overlay_outcome((150.0, 1.2), 100.0, -3.0) == (-3.0, "UNCHANGED")
    assert overlay_outcome(None, 100.0, -3.0) == (-3.0, "UNCHANGED")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK {name}")
    print("all tests passed")

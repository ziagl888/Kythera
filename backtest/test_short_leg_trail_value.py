# backtest/test_short_leg_trail_value.py — T-2026-KYT-9050-062 pins.
#
# This tool exists because the previous measure was unfair by construction: scoring a
# take-profit leg against the FULL index move over its window penalises it for exiting
# at TP1 while the tape keeps running. Over a −50 % market that made nearly every SHORT
# leg look bad and could not separate "poor selection" from "TP truncates the trend".
#
# The fix is symmetry — leg and benchmark under the SAME trailing rule. These pins hold
# the pieces that carry that symmetry, because each of them silently breaks it:
#
#   1. The synthetic index bar. Its high/low must hang off the PREVIOUS close level,
#      not the new one; anchoring them to the new close invents an intrabar span the
#      market never had and moves the benchmark.
#   2. Direction. A short benchmarked with a long's sign flips the comparison rather
#      than merely blurring it.
#   3. Coverage. A window outside the index must yield None, never a silent 0.0 that
#      would read as "the market did nothing".
#   4. The never-armed fallback: if the trail never fires, the benchmark is the
#      close-to-close move, signed — not the peak it failed to reach.
#
# Runs without a DB:  python backtest/test_short_leg_trail_value.py

import os
import sys
from datetime import datetime, timedelta

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.short_leg_trail_value import benchmark_trail, build_index_ohlc, cluster_t  # noqa: E402

T0 = datetime(2026, 6, 1)


def _rows(ratios):
    """(ts, h_ratio, l_ratio, c_ratio) rows, one per hour from T0."""
    return [(T0 + timedelta(hours=i), h, low, c) for i, (h, low, c) in enumerate(ratios)]


# ------------------------------------------------------------------- index --
def test_bar_high_and_low_hang_off_the_previous_close_level():
    """A bar's span is its ratios applied to where the market WAS, not where it ended.

    Anchoring to the new close would fabricate an intrabar range around a level the
    tape only reached at the end of the bar.
    """
    idx = build_index_ohlc(_rows([(1.10, 0.95, 1.00), (1.20, 0.90, 1.50)]))
    # bar 0: prev level 1.0 → h 1.10, l 0.95, c 1.00
    assert abs(idx["h"][0] - 1.10) < 1e-12 and abs(idx["l"][0] - 0.95) < 1e-12
    assert abs(idx["c"][0] - 1.00) < 1e-12
    # bar 1: prev level is bar 0's CLOSE (1.00) → h 1.20, l 0.90, c 1.50
    assert abs(idx["h"][1] - 1.20) < 1e-12, idx["h"]
    assert abs(idx["l"][1] - 0.90) < 1e-12, idx["l"]
    assert abs(idx["c"][1] - 1.50) < 1e-12, idx["c"]


def test_index_close_compounds():
    idx = build_index_ohlc(_rows([(1.0, 1.0, 1.10), (1.0, 1.0, 1.10)]))
    assert abs(idx["c"][-1] - 1.21) < 1e-12, idx["c"]


def test_rows_with_a_missing_ratio_are_skipped():
    """LAG yields NULL on a symbol's first observation — those must not enter."""
    idx = build_index_ohlc([(T0, None, None, None), (T0 + timedelta(hours=1), 1.0, 1.0, 1.05)])
    assert len(idx["t"]) == 1
    assert abs(idx["c"][0] - 1.05) < 1e-12


def test_empty_input_gives_empty_arrays_not_a_crash():
    idx = build_index_ohlc([])
    assert len(idx["t"]) == 0 and len(idx["c"]) == 0


# --------------------------------------------------------------- benchmark --
def _flat_then_drop():
    """Flat bar, then a 10 % drop with no wick beyond it, then flat."""
    return build_index_ohlc(_rows([(1.0, 1.0, 1.00), (1.0, 0.90, 0.90), (1.0, 1.0, 1.00)]))


def test_short_benchmark_profits_when_the_index_falls():
    idx = _flat_then_drop()
    v = benchmark_trail(idx, T0, T0 + timedelta(hours=2), is_long=False, x=0.10, activation=2.0)
    assert v is not None and v > 0, v


def test_long_benchmark_loses_on_the_same_fall():
    """Same path, opposite sign — a sign slip here inverts every verdict."""
    idx = _flat_then_drop()
    short_v = benchmark_trail(idx, T0, T0 + timedelta(hours=2), is_long=False, x=0.10, activation=2.0)
    long_v = benchmark_trail(idx, T0, T0 + timedelta(hours=2), is_long=True, x=0.10, activation=2.0)
    assert long_v is not None and long_v < 0 < short_v, (long_v, short_v)


def test_window_outside_coverage_is_none_not_zero():
    """A zero would read as 'the market did nothing', which is a measurement, not a gap."""
    idx = _flat_then_drop()
    assert benchmark_trail(idx, T0 + timedelta(days=30), T0 + timedelta(days=31),
                           is_long=False, x=0.10, activation=2.0) is None
    assert benchmark_trail({"t": np.asarray([], dtype="datetime64[ns]"),
                            "h": np.asarray([]), "l": np.asarray([]), "c": np.asarray([])},
                           T0, T0 + timedelta(hours=1), is_long=False, x=0.10, activation=2.0) is None


def test_never_armed_falls_back_to_the_close_to_close_move():
    """A trail that never reaches activation must report what the window actually did."""
    # Rises 1 % — never near a 2 % activation for a LONG.
    idx = build_index_ohlc(_rows([(1.0, 1.0, 1.00), (1.0, 1.0, 1.01)]))
    v = benchmark_trail(idx, T0, T0 + timedelta(hours=1), is_long=True, x=0.10, activation=2.0)
    assert v is not None and abs(v - 1.0) < 1e-9, v


def test_entry_is_the_first_close_inside_the_window():
    """The mirror enters at market when it opens, so the benchmark must start there —
    not at the index origin."""
    idx = build_index_ohlc(_rows([(1.0, 1.0, 2.00), (1.0, 1.0, 2.00), (1.0, 1.0, 1.80)]))
    # Window starts at bar 1 (level 4.0 after compounding); a 10 % fall follows.
    v = benchmark_trail(idx, T0 + timedelta(hours=1), T0 + timedelta(hours=2),
                        is_long=False, x=0.10, activation=2.0)
    assert v is not None and v > 0, v


# --------------------------------------------------------------- inference --
def test_cluster_t_collapses_a_day_to_one_observation():
    same_day = [(T0.date(), 1.0), (T0.date(), 1.0), (T0.date(), 1.0)]
    n_days, mean, t = cluster_t(same_day)
    assert n_days == 1 and mean == 1.0
    assert t != t, "a single day cannot support a t — expected nan"


def test_cluster_t_weights_days_equally_not_trades():
    """One busy day must not outvote a quiet one; that is the whole point."""
    vals = [(T0.date(), 10.0)] * 100 + [((T0 + timedelta(days=1)).date(), 0.0)]
    n_days, mean, _t = cluster_t(vals)
    assert n_days == 2 and abs(mean - 5.0) < 1e-12, mean


if __name__ == "__main__":
    # Catches Exception, not just AssertionError — a crashing pin is a failing pin.
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

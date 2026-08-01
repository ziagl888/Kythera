# backtest/test_epoch_seconds.py — core.time.epoch_seconds is resolution-independent
"""T-2026-KYT-9050-008.

The replay generator feeds ``core/rub_features.rub_trend`` an epoch axis. Built
with the old ``astype("int64") / 1e9`` idiom that axis silently changes scale
with the pandas datetime resolution (ns → us as of pandas 3.0), which turns
``slope_trend`` — one of the fifteen RUB2 model inputs — into a value 1000x off
from what the live bot computes, while ``dist_to_trend`` still matches. These
tests pin the invariant on every resolution the DB readers can produce.

DB-free, standalone: ``python backtest/test_epoch_seconds.py``.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.rub_features import rub_trend  # noqa: E402
from core.time import epoch_seconds  # noqa: E402

HOURS = pd.date_range("2026-06-01", periods=240, freq="h", tz="UTC")


def test_seconds_scale_and_spacing():
    """The result is epoch SECONDS — hourly bars are 3600 apart, not 3.6e6 or 3.6e12."""
    ts = epoch_seconds(HOURS)
    assert ts.dtype == np.float64
    assert np.allclose(np.diff(ts), 3600.0)
    # Anchor against the stdlib, which is unambiguously in seconds.
    assert abs(ts[0] - HOURS[0].timestamp()) < 1e-6


def test_identical_across_datetime_resolutions():
    """ns / us / ms columns of the same instants give the same epoch axis.

    This is the mutation that the old idiom fails: ``astype("int64") / 1e9`` on
    the ``us`` variant returns 1/1000 of the ``ns`` variant.
    """
    ref = epoch_seconds(HOURS)
    for unit in ("ns", "us", "ms", "s"):
        col = pd.Series(HOURS).dt.tz_localize(None).astype(f"datetime64[{unit}]")
        assert np.array_equal(epoch_seconds(col), ref), unit
        # ...and the naive/aware pair of the same instants agrees too.
        assert np.array_equal(epoch_seconds(col.dt.tz_localize("UTC")), ref), unit


def test_accepts_the_shapes_the_readers_hand_over():
    """Series, Index and the raw ``.values`` array of a candle frame all work."""
    ref = epoch_seconds(HOURS)
    df = pd.DataFrame({"open_time": HOURS})
    for shape in (df["open_time"], pd.DatetimeIndex(HOURS), df["open_time"].values):
        assert np.array_equal(epoch_seconds(shape), ref)


def test_slope_trend_is_unchanged_by_the_column_resolution():
    """The reason this helper exists: rub_trend's slope must not follow the dtype.

    ``dist_to_trend`` is near-invariant under a rescaled epoch axis (the fitted
    value at the last point barely moves), so a resolution bug hides behind it —
    which is why this asserts the SLOPE.
    """
    closes = np.linspace(100.0, 130.0, len(HOURS))
    ref_dist, ref_slope = rub_trend(epoch_seconds(HOURS), closes, float(closes[-1]))
    for unit in ("ns", "us", "ms"):
        col = pd.Series(HOURS).dt.tz_localize(None).astype(f"datetime64[{unit}]")
        dist, slope = rub_trend(epoch_seconds(col), closes, float(closes[-1]))
        assert abs(slope - ref_slope) < 1e-12, unit
        assert abs(dist - ref_dist) < 1e-12, unit
    # Sanity on the magnitude: +30 over 239h on a ~130 close is ~+2.3 %/day.
    assert 0.02 < ref_slope < 0.03


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK  {name}")
    print("\nall epoch_seconds tests passed")

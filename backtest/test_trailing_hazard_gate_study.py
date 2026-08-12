"""DB-free tests for the pure logic of tools/trailing_hazard_gate_study.py (T-140).

Covers what a leak or a sign error would silently corrupt: the as-of vol lookup
(closed-candle boundary), the running-peak/drawdown feature, leakage (features at t are
invariant to future marks), the logistic trainer on separable data, the AUC rank
statistic, and the NaN-vol no-fire rule.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.trailing_hazard_gate_study import (  # noqa: E402
    VolSeries,
    auc_score,
    build_instant_features,
    first_fire_index,
    predict_proba,
    train_logistic,
)


def _utc(*args: int) -> pd.Timestamp:
    return pd.Timestamp(datetime(*args, tzinfo=timezone.utc))


def _tape(n: int = 12, td1: set[int] | None = None) -> pd.DataFrame:
    times = [_utc(2026, 8, 1, 1) + pd.Timedelta(hours=i) for i in range(n)]
    td1 = td1 or set()
    return pd.DataFrame({"close_time": times, "TD1": [i in td1 for i in range(n)], "TD2": [False] * n})


def _vol(n_candles: int = 200, start: pd.Timestamp | None = None) -> VolSeries:
    start = start or _utc(2026, 7, 31, 0)
    times = pd.DatetimeIndex([start + pd.Timedelta(minutes=5 * i) for i in range(n_candles)])
    closes = 100.0 + np.sin(np.arange(n_candles) * 0.7)
    return VolSeries(times, closes)


def test_vol_series_uses_only_closed_candles() -> None:
    start = _utc(2026, 8, 1, 0)
    times = pd.DatetimeIndex([start + pd.Timedelta(minutes=5 * i) for i in range(60)])
    vs = VolSeries(times, np.linspace(100, 101, 60))
    # Candle 59 opens at +295min and closes at +300min. One second before that close,
    # the as-of index must resolve to candle 58, never 59.
    t_before = start + pd.Timedelta(minutes=300) - pd.Timedelta(seconds=1)
    t_after = start + pd.Timedelta(minutes=300)
    idx_before = int(vs.close_times.searchsorted(t_before, side="right")) - 1
    idx_after = int(vs.close_times.searchsorted(t_after, side="right")) - 1
    assert idx_before == 58  # candle 59 has not closed yet one second earlier
    assert idx_after == 59  # at +300min candle 59 is closed and becomes visible
    # Warm-up: fewer than window returns -> NaN, and before any close -> NaN.
    assert np.isnan(vs.value_at(start))
    assert np.isnan(vs.value_at(start - pd.Timedelta(hours=1)))


def test_running_peak_and_drawdown() -> None:
    tape = _tape()
    hours = tape["close_time"]
    filled = hours.iloc[0] - pd.Timedelta(minutes=30)
    closed = hours.iloc[5]
    # LONG entry 100; marks 102, 104, 101 -> peak 2%, 4%, 4%; drawdown 0, 0, 3.
    marks = pd.Series({hours.iloc[0]: 102.0, hours.iloc[1]: 104.0, hours.iloc[2]: 101.0})
    f = build_instant_features(filled, closed, 100.0, True, tape, marks, _vol())
    body = f[~f["at_fill"]].reset_index(drop=True)
    assert np.allclose(body["mark_pct"], [2.0, 4.0, 1.0])
    assert np.allclose(body["drawdown_from_peak"], [0.0, 0.0, 3.0])
    assert np.allclose(body["hours_in_trade"], [0.5, 1.5, 2.5])


def test_features_do_not_leak_future_marks() -> None:
    tape = _tape()
    hours = tape["close_time"]
    filled = hours.iloc[0] - pd.Timedelta(minutes=30)
    closed = hours.iloc[5]
    marks_a = pd.Series({hours.iloc[0]: 102.0, hours.iloc[1]: 104.0, hours.iloc[2]: 101.0})
    # Same history up to hour 1, wildly different future at hour 2.
    marks_b = pd.Series({hours.iloc[0]: 102.0, hours.iloc[1]: 104.0, hours.iloc[2]: 50.0})
    fa = build_instant_features(filled, closed, 100.0, True, tape, marks_a, _vol())
    fb = build_instant_features(filled, closed, 100.0, True, tape, marks_b, _vol())
    common = ["mark_pct", "drawdown_from_peak", "hours_in_trade", "btc_td1"]
    assert fa.iloc[:3][common].equals(fb.iloc[:3][common])  # fill + hours 0,1 identical


def test_short_direction_sign() -> None:
    tape = _tape()
    hours = tape["close_time"]
    filled = hours.iloc[0] - pd.Timedelta(minutes=30)
    marks = pd.Series({hours.iloc[0]: 90.0})
    f = build_instant_features(filled, hours.iloc[2], 100.0, False, tape, marks, _vol())
    body = f[~f["at_fill"]]
    assert np.allclose(body["mark_pct"], [10.0])  # price fell 10% -> SHORT is +10%
    assert (f["is_long"] == 0.0).all()


def test_logistic_recovers_separable_signal() -> None:
    rng_x = np.linspace(-2, 2, 400).reshape(-1, 1)
    x = np.hstack([rng_x] + [np.zeros_like(rng_x)] * 5)
    y = (rng_x.ravel() > 0).astype(float)
    w, mu, sd = train_logistic(x, y)
    p = predict_proba(w, mu, sd, x)
    assert w[1] > 1.0  # strong positive weight on the separating feature
    assert p[0] < 0.1 and p[-1] > 0.9
    assert auc_score(y, p) > 0.99


def test_first_fire_skips_nan_vol() -> None:
    probs = np.array([0.9, 0.9, 0.4, 0.95])
    vols = np.array([np.nan, np.nan, 1.0, 1.0])
    assert first_fire_index(probs, vols, 0.8) == 3  # first two cannot fire despite P>θ
    assert first_fire_index(probs, vols, 0.99) is None


def test_instant_cap_limits_hourly_rows() -> None:
    tape = _tape(n=100)
    hours = tape["close_time"]
    filled = hours.iloc[0] - pd.Timedelta(minutes=30)
    closed = hours.iloc[99]
    marks = pd.Series({h: 100.0 for h in hours})
    f = build_instant_features(filled, closed, 100.0, True, tape, marks, _vol(3000))
    assert len(f) == 1 + 72  # fill + INSTANT_CAP


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK {name}")
    print("all tests passed")

# backtest/test_epd2_entry_from_ticker.py
"""
Unit tests for the EPD2 dataset builder's entry price (T-2026-CU-9050-035).

The builder used to estimate the post-spike entry as `close * (1 + p_chg/100)`,
reading `pump_dump_events.price_change_60s` as a realised price move. Since the
detector normalises that column to a per-60s RATE, the estimator is wrong by the
scale factor whenever the 60s window was stretched -- and the raw move cannot be
recovered from the event log, because the window length is not persisted.

The entry now comes from `ticker_10s`: the price actually traded at `spike_time`.
These tests pin the lookup, especially its refusal to guess.

Run with: pytest backtest/test_epd2_entry_from_ticker.py -v
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

from tools.epd2_build_dataset import TICKER_MAX_LAG_SEC, entry_from_ticker  # noqa: E402


def _ticks(start: str, step_s: int, prices: list[float]) -> tuple[np.ndarray, np.ndarray]:
    t0 = pd.Timestamp(start)
    ts = np.array(
        [(t0 + pd.Timedelta(seconds=step_s * i)).to_datetime64() for i in range(len(prices))],
        dtype="datetime64[ns]",
    )
    return ts, np.array(prices, dtype=np.float64)


def test_entry_is_the_traded_price_at_the_event():
    ts, px = _ticks("2026-07-10T12:00:00", 10, [100.0, 101.0, 109.0, 108.0])
    # Event lands exactly on the third tick.
    assert entry_from_ticker(ts, px, pd.Timestamp("2026-07-10T12:00:20")) == pytest.approx(109.0)


def test_entry_picks_the_nearest_tick_on_either_side():
    ts, px = _ticks("2026-07-10T12:00:00", 70, [100.0, 109.0, 108.0])
    # 12:01:00 is 10s before the 12:01:10 tick and 60s after the 12:00:00 one.
    assert entry_from_ticker(ts, px, pd.Timestamp("2026-07-10T12:01:00")) == pytest.approx(109.0)


def test_entry_refuses_a_tick_beyond_the_lag_budget():
    """No tick nearby means the entry is unknown -- guessing produces a wrong label."""
    ts, px = _ticks("2026-07-10T12:00:00", 10, [100.0, 101.0])
    far = pd.Timestamp("2026-07-10T12:00:10") + pd.Timedelta(seconds=TICKER_MAX_LAG_SEC + 1)
    assert entry_from_ticker(ts, px, far) is None


def test_entry_handles_an_empty_or_degenerate_series():
    empty_ts = np.empty(0, dtype="datetime64[ns]")
    empty_px = np.empty(0, dtype=np.float64)
    assert entry_from_ticker(empty_ts, empty_px, pd.Timestamp("2026-07-10T12:00:00")) is None

    ts, px = _ticks("2026-07-10T12:00:00", 10, [0.0])
    assert entry_from_ticker(ts, px, pd.Timestamp("2026-07-10T12:00:00")) is None


def test_entry_is_independent_of_the_p_chg_normalisation():
    """The regression this replaces: a stretched window shrank the estimated entry.

    Old estimator on a 9% move logged as a 7.714 rate over a 70s window:
        close * (1 + 7.714/100) = 107.714  -- but the market traded at 109.
    The ticker lookup returns the traded price regardless of how p_chg is scaled.
    """
    ts, px = _ticks("2026-07-10T12:00:00", 10, [100.0, 109.0])
    event = pd.Timestamp("2026-07-10T12:00:10")

    close, p_chg_rate = 100.0, 9.0 * 60 / 70
    old_estimate = close * (1.0 + p_chg_rate / 100.0)

    entry = entry_from_ticker(ts, px, event)
    assert entry == pytest.approx(109.0)
    assert old_estimate == pytest.approx(107.714, abs=1e-3)
    assert abs(entry - old_estimate) > 1.0, "the estimator's error must be visible"

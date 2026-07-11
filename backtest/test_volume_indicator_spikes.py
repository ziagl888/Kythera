# backtest/test_volume_indicator_spikes.py
"""
Unit tests for the Volume Indicator spike classification and HVN gate
(T-2026-CU-9050-085, AUDIT_TODO P2.42). DB-free: the pure helpers operate on
in-memory DataFrames, so no Binance/Postgres access is needed.

Pinned behaviour (each fails against the pre-fix code):
  P2.42(a) the NEWEST spike in the window decides — the old forward loop
           returned on the OLDEST spike.
  P2.42(b) a spike on the first in-period candle (i==0) is DISCARDED — the old
           code silently classified it as a sell.
  P2.42(c) the HVN gate bins prices into relative levels, so it no longer
           degenerates to "never fires" on fine-tick-size coins where every
           close is a unique float.

Run with: pytest backtest/test_volume_indicator_spikes.py -v
      or: python backtest/test_volume_indicator_spikes.py
"""

from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.strat_volume_indicator import (  # noqa: E402
    _classify_latest_volume_spike,
    _is_near_high_volume_node,
)


def _period(rows: list[tuple[float, float]]) -> pd.DataFrame:
    """rows are (close, volume), oldest first."""
    return pd.DataFrame(rows, columns=["close", "volume"])


# --- reference implementations of the PRE-FIX logic, to make each regression visible ---


def _old_classify(df_period: pd.DataFrame, spike_threshold: float) -> int:
    df_period = df_period.reset_index(drop=True)
    for i in range(len(df_period)):  # forward → oldest spike wins
        if df_period.iloc[i]["volume"] > spike_threshold:
            if i > 0 and df_period.iloc[i]["close"] > df_period.iloc[i - 1]["close"]:
                return 1
            return -1  # i==0 always fell through to sell
    return 0


def _old_hvn(df_hist: pd.DataFrame, latest_close: float, threshold_factor: float = 3) -> bool:
    volume_mean, volume_std = df_hist["volume"].mean(), df_hist["volume"].std()
    high_volume_threshold = volume_mean + threshold_factor * volume_std
    price_volume = df_hist.groupby("close")["volume"].sum().reset_index()  # raw float close
    hvn_prices = price_volume[price_volume["volume"] > high_volume_threshold]["close"].values
    proximity_threshold = latest_close * 0.01
    return any(abs(latest_close - p) <= proximity_threshold for p in hvn_prices)


# --- P2.42(a): newest spike wins ---


def test_newest_spike_decides_direction():
    # Oldest spike is a buy (close up), newest spike is a sell (close down).
    df = _period([(10.0, 50), (11.0, 200), (12.0, 50), (11.0, 200)])
    assert _classify_latest_volume_spike(df, spike_threshold=100) == -1
    # The old forward scan would have latched onto the oldest (buy) spike.
    assert _old_classify(df, 100) == 1


def test_newest_buy_spike():
    df = _period([(12.0, 200), (11.0, 50), (10.0, 60), (11.0, 200)])
    # newest spike (i=3) close 11 > i=2 close 10 → buy
    assert _classify_latest_volume_spike(df, spike_threshold=100) == 1


def test_no_spike_returns_zero():
    df = _period([(10.0, 5), (11.0, 6), (12.0, 7)])
    assert _classify_latest_volume_spike(df, spike_threshold=100) == 0


# --- P2.42(b): i==0 spike is discarded, not classified as sell ---


def test_lone_first_candle_spike_is_discarded():
    # Only the oldest candle spikes; it has no in-period predecessor.
    df = _period([(10.0, 200), (11.0, 50), (12.0, 60)])
    assert _classify_latest_volume_spike(df, spike_threshold=100) == 0
    # The old code defaulted this to a (spurious) sell.
    assert _old_classify(df, 100) == -1


def test_first_candle_spike_ignored_when_a_later_spike_exists():
    # i==0 spike must never outvote a real, classifiable later spike.
    df = _period([(10.0, 200), (11.0, 50), (12.0, 200)])
    assert _classify_latest_volume_spike(df, spike_threshold=100) == 1  # i=2 close 12 > i=1 close 11


# --- P2.42(c): HVN gate bins prices, no tick-size degeneration ---


def test_hvn_fires_on_fine_tick_coin_where_raw_groupby_fails():
    latest_close = 0.001
    rows: list[tuple[float, float]] = []
    # Background: 200 candles at widely spread, distinct prices (each in its own
    # bin), moderate volume, all >1% away from latest_close.
    for i in range(200):
        rows.append((0.0012 + i * 1e-5, 10.0))
    # Node zone: 40 candles at DISTINCT sub-tick closes that all fall into the
    # single 0.1% bin around latest_close. Per-candle volume stays below the
    # spike threshold; only their SUM makes the level a high-volume node.
    for i in range(40):
        rows.append((latest_close + (i - 20) * 2e-8, 11.0))
    df = pd.DataFrame(rows, columns=["close", "volume"])

    # Sanity: the crafted data is a genuine "sum crosses, single candle doesn't".
    thr = df["volume"].mean() + 3 * df["volume"].std()
    assert 11.0 < thr < 40 * 11.0

    assert _is_near_high_volume_node(df, latest_close) is True
    # The old raw-float groupby never accumulates the node → silent miss.
    assert _old_hvn(df, latest_close) is False


def test_hvn_still_fires_on_coarse_tick_coin():
    # A coarse-tick coin that revisits the exact same price forms a real node;
    # binning must not break the case the old code already handled.
    rows = [(99.5, 5.0)] * 50 + [(100.0, 8.0)] * 30
    df = pd.DataFrame(rows, columns=["close", "volume"])
    assert _is_near_high_volume_node(df, 100.0) is True
    assert _old_hvn(df, 100.0) is True


def test_hvn_false_when_price_far_from_any_node():
    rows = [(50.0, 8.0)] * 40 + [(99.5, 5.0)] * 40
    df = pd.DataFrame(rows, columns=["close", "volume"])
    # latest_close 70 is >1% from both the 50 node and the 99.5 cluster.
    assert _is_near_high_volume_node(df, 70.0) is False


def test_hvn_empty_or_degenerate_returns_false():
    assert _is_near_high_volume_node(pd.DataFrame(columns=["close", "volume"]), 100.0) is False
    df = pd.DataFrame([(100.0, 10.0)], columns=["close", "volume"])
    assert _is_near_high_volume_node(df, 0.0) is False


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\nAlle {len(fns)} Volume-Indicator-Tests bestanden.")

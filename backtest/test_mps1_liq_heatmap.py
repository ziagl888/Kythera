# backtest/test_mps1_liq_heatmap.py
"""Unit tests for tools/mps1_liq_heatmap.py — the MPS1 liquidation-heatmap
engine (T-2026-KYT-9050-073). DB-free: all candle/OI frames are synthetic.

Pinned contracts:
  * liquidation-price math per leverage tier (isolated USDT-M approximation);
  * SEED — a positive 5m ΔOI creates levels whose weights sum to the ΔOI
    notional, split 50/50 across sides and by tier weights within a side;
  * CONSUME — a bar whose [low, high] range crosses a level kills it;
  * DECAY — a relative OI drop shrinks all alive weights by the same factor;
  * EXPIRE — levels older than window_bars leave the histogram;
  * OI→candle alignment — the point stamped T belongs to the bar closing at T,
    missing sweeps yield NaN deltas (no seed, no decay, no crash);
  * NO LOOK-AHEAD — truncating the inputs reproduces the output prefix exactly.

Run with: pytest backtest/test_mps1_liq_heatmap.py -v
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mps1_liq_heatmap import (  # noqa: E402
    BAR_SECONDS,
    HeatmapConfig,
    align_oi_to_candles,
    build_heatmap,
    liq_prices,
)

T0 = pd.Timestamp("2026-06-15 00:00:00", tz="UTC")


def make_candles(ohlc: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Candle frame on the 5m grid from (open, high, low, close) tuples."""
    return pd.DataFrame(
        {
            "open_time": [T0 + pd.Timedelta(seconds=BAR_SECONDS * i) for i in range(len(ohlc))],
            "open": [r[0] for r in ohlc],
            "high": [r[1] for r in ohlc],
            "low": [r[2] for r in ohlc],
            "close": [r[3] for r in ohlc],
        }
    )


def flat_candles(n: int, price: float) -> pd.DataFrame:
    return make_candles([(price, price, price, price)] * n)


def make_oi(values: list[float | None], start_bar: int = 0) -> pd.DataFrame:
    """OI points stamped at the CLOSE of consecutive bars (None = missed sweep)."""
    rows = [
        (T0 + pd.Timedelta(seconds=BAR_SECONDS * (start_bar + i + 1)), v) for i, v in enumerate(values) if v is not None
    ]
    return pd.DataFrame(rows, columns=["ts", "oi_value_usdt"])


# ── Config & liquidation math ────────────────────────────────────────────────


def test_config_rejects_mismatched_tiers():
    with pytest.raises(ValueError):
        HeatmapConfig(leverage_tiers=(10.0, 25.0), tier_weights=(1.0,))
    with pytest.raises(ValueError):
        HeatmapConfig(tier_weights=(0.5, 0.3, 0.2, 0.2))  # sums to 1.2
    with pytest.raises(ValueError):
        HeatmapConfig(smooth_bins=2)  # must be odd


def test_liq_prices_math():
    cfg = HeatmapConfig(leverage_tiers=(10.0,), tier_weights=(1.0,), maintenance_margin=0.005)
    long_liq, short_liq = liq_prices(100.0, cfg)
    assert long_liq[0] == pytest.approx(90.5)  # 100·(1 − 0.1 + 0.005)
    assert short_liq[0] == pytest.approx(109.5)  # 100·(1 + 0.1 − 0.005)
    assert long_liq[0] < 100.0 < short_liq[0]


# ── OI → candle alignment ────────────────────────────────────────────────────


def test_alignment_maps_close_stamp_to_bar():
    candles = flat_candles(3, 100.0)
    oi = make_oi([1000.0, 1500.0, 1400.0])
    merged = align_oi_to_candles(candles, oi)
    assert merged["oi_value_usdt"].tolist() == [1000.0, 1500.0, 1400.0]
    assert np.isnan(merged["d_oi_usdt"].iloc[0])  # no prior point
    assert merged["d_oi_usdt"].iloc[1] == 500.0
    assert merged["d_oi_usdt"].iloc[2] == -100.0


def test_alignment_gap_attributes_delta_to_next_available():
    candles = flat_candles(4, 100.0)
    oi = make_oi([1000.0, None, None, 1600.0])  # two missed sweeps
    merged = align_oi_to_candles(candles, oi)
    assert np.isnan(merged["oi_value_usdt"].iloc[1])
    assert np.isnan(merged["d_oi_usdt"].iloc[2])
    assert merged["d_oi_usdt"].iloc[3] == 600.0  # whole gap delta on the later bar


# ── Seeding ──────────────────────────────────────────────────────────────────


def test_seed_weights_sum_to_delta_oi():
    cfg = HeatmapConfig()
    candles = flat_candles(3, 100.0)
    oi = make_oi([1000.0, 1000.0 + 8000.0, 9000.0])
    hm = build_heatmap(candles, oi, cfg)
    # After the +8000 bar: 4000 USDT of implied liq notional per side.
    assert hm["alive_usdt_above"].iloc[1] == pytest.approx(4000.0)
    assert hm["alive_usdt_below"].iloc[1] == pytest.approx(4000.0)
    # Flat OI afterwards: nothing seeded, nothing decayed/consumed.
    assert hm["alive_usdt_above"].iloc[2] == pytest.approx(4000.0)


def test_no_seed_without_oi_growth():
    hm = build_heatmap(flat_candles(3, 100.0), make_oi([1000.0, 900.0, 800.0]), HeatmapConfig())
    assert np.isnan(hm["upper_band"]).all()
    assert np.isnan(hm["alive_usdt_above"]).all()


# ── Consume / decay / expire ─────────────────────────────────────────────────


def _single_tier_cfg(**kw) -> HeatmapConfig:
    return HeatmapConfig(leverage_tiers=(10.0,), tier_weights=(1.0,), maintenance_margin=0.0, **kw)


def test_consumption_kills_crossed_levels():
    cfg = _single_tier_cfg()
    # Seed at 100 → long level 90, short level 110. Bar 2 dips to 89.
    candles = make_candles([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 89, 100), (100, 100, 100, 100)])
    oi = make_oi([1000.0, 2000.0, 2000.0, 2000.0])
    hm = build_heatmap(candles, oi, cfg)
    assert hm["alive_usdt_below"].iloc[1] == pytest.approx(500.0)
    assert hm["alive_usdt_below"].iloc[2] == pytest.approx(0.0)  # long level consumed
    assert hm["alive_usdt_above"].iloc[2] == pytest.approx(500.0)  # short side untouched


def test_decay_shrinks_weights_proportionally():
    cfg = _single_tier_cfg()
    candles = flat_candles(3, 100.0)
    oi = make_oi([1000.0, 2000.0, 1500.0])  # +1000 then −25 %
    hm = build_heatmap(candles, oi, cfg)
    assert hm["alive_usdt_above"].iloc[1] == pytest.approx(500.0)
    assert hm["alive_usdt_above"].iloc[2] == pytest.approx(375.0)  # ·(1 − 500/2000)
    assert hm["alive_usdt_below"].iloc[2] == pytest.approx(375.0)


def test_expiry_after_window():
    # A seed lives for exactly window_bars bars, birth bar included.
    cfg = _single_tier_cfg(window_bars=2)
    candles = flat_candles(5, 100.0)
    oi = make_oi([1000.0, 2000.0, 2000.0, 2000.0, 2000.0])
    hm = build_heatmap(candles, oi, cfg)
    assert hm["alive_usdt_above"].iloc[2] == pytest.approx(500.0)  # age 1, still alive
    # Age == window_bars: expired. Store is empty → totals are NaN, not 0.
    assert np.isnan(hm["alive_usdt_above"].iloc[3])
    assert np.isnan(hm["alive_usdt_above"].iloc[4])


# ── Bands ────────────────────────────────────────────────────────────────────


def test_band_picks_densest_cluster_per_side():
    cfg = _single_tier_cfg()
    # Two seeds: bar 1 at 100 with +1000, bar 2 at 100 with +3000 → both sides
    # cluster at the same prices (110 short / 90 long) with combined weight.
    candles = flat_candles(4, 100.0)
    oi = make_oi([1000.0, 2000.0, 5000.0, 5000.0])
    hm = build_heatmap(candles, oi, cfg)
    row = hm.iloc[3]
    assert row["upper_band"] == pytest.approx(110.0, rel=0.005)
    assert row["lower_band"] == pytest.approx(90.0, rel=0.005)
    assert row["upper_density_usdt"] == pytest.approx(2000.0)  # 500 + 1500
    assert row["upper_density_frac"] == pytest.approx(1.0)  # single cluster = all weight
    assert row["upper_band"] > row["close"] > row["lower_band"]


def test_band_ignores_levels_beyond_max_distance():
    cfg = _single_tier_cfg(max_band_distance=0.05)  # 10x levels sit ±10 % away
    candles = flat_candles(3, 100.0)
    oi = make_oi([1000.0, 2000.0, 2000.0])
    hm = build_heatmap(candles, oi, cfg)
    assert np.isnan(hm["upper_band"]).all()  # weight exists, but out of range
    assert hm["alive_usdt_above"].iloc[1] == pytest.approx(500.0)


# ── Robustness & look-ahead ──────────────────────────────────────────────────


def test_missing_oi_sweeps_do_not_crash_or_seed():
    cfg = _single_tier_cfg()
    candles = flat_candles(4, 100.0)
    oi = make_oi([1000.0, None, 2000.0, None])
    hm = build_heatmap(candles, oi, cfg)
    assert np.isnan(hm["alive_usdt_above"].iloc[1])  # gap bar: store still empty
    assert hm["alive_usdt_above"].iloc[2] == pytest.approx(500.0)
    assert len(hm) == 4


def test_truncation_across_chunk_boundary():
    # The vectorized band post-pass processes rows in chunks of 4096. A full
    # run with n > 4096 (two chunks) must reproduce the prefix of a truncated
    # run that fits in ONE chunk — pins the chunk-boundary slicing.
    n, k = 5000, 4000
    cfg = _single_tier_cfg(window_bars=20)
    candles = flat_candles(n, 100.0)
    oi = make_oi([1000.0 + 10.0 * i for i in range(n)])  # steady growth → steady seeds
    full = build_heatmap(candles, oi, cfg)
    truncated = build_heatmap(candles.iloc[:k], oi.iloc[:k], cfg)
    pd.testing.assert_frame_equal(full.iloc[:k].reset_index(drop=True), truncated)
    assert full["upper_band"].iloc[4095] == pytest.approx(full["upper_band"].iloc[4096])


def test_truncation_invariance_no_lookahead():
    rng = np.random.RandomState(42)
    n = 400
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    opens = np.roll(closes, 1)
    opens[0] = 100.0
    highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.002, n)))
    lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.002, n)))
    candles = make_candles(list(zip(opens, highs, lows, closes, strict=True)))
    oi_vals = list(10_000.0 * np.exp(np.cumsum(rng.normal(0, 0.05, n))))
    oi = make_oi(oi_vals)
    cfg = HeatmapConfig(window_bars=50)

    full = build_heatmap(candles, oi, cfg)
    k = 250
    truncated = build_heatmap(candles.iloc[:k], oi.iloc[:k], cfg)
    pd.testing.assert_frame_equal(full.iloc[:k].reset_index(drop=True), truncated)

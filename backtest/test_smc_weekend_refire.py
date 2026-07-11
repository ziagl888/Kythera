# backtest/test_smc_weekend_refire.py
"""Standalone (DB-free) guards for P2.45 on the two Forex/Metals SMC bots.

Three sub-fixes are pinned here:

  (a) Weekend-/stale-candle gate — 16_smc_forex_metals_bot.py + 17_mayank_bot.py.
      Forex/Metals stand still on weekends; the last closed candle freezes and
      keeps satisfying the structure/FVG condition while the (12h) cooldown
      expires underneath it → the bot re-fires the same frozen candle. A signal
      may now only fire on fresh data (is_stale_candle). 24/7 crypto is never
      stale, so the live crypto path is untouched.

  (b) FVG age limit — 16_smc_forex_metals_bot.find_unmitigated_fvgs. An FVG that
      is never mitigated stayed triggerable across the whole 300-candle history;
      it is now bounded to the last FVG_MAX_AGE bars.

  (c) SL/RR sanity guard — 17_mayank_bot.passes_sl_rr_guard. Mayank posted
      SL/TP with no check that the stop survives leverage or that the reward
      beats the risk.

Loader note (memory patch-dict-sys-modules-numpy-teardown): pandas/numpy are
imported BEFORE the patch.dict(sys.modules) block so a mocked numpy cannot be
torn down under them, and the combined suite run is the one that matters.

Run: py -3.13 backtest/test_smc_weekend_refire.py   (or: pytest -q)
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import re
import sys
import unittest.mock as mock

import numpy as np  # noqa: F401  (pre-seed before any sys.modules patch)
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UTC = datetime.timezone.utc


def _load(alias, filename):
    spec = importlib.util.spec_from_file_location(alias, os.path.join(REPO_ROOT, filename))
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "yfinance": mock.MagicMock(),
            "core.database": mock.MagicMock(),
            "core.config": mock.MagicMock(
                CH_SMC_METALS=-1, CH_SMC_FOREX=-2, CH_MAYANK=-3
            ),
            "core.market_utils": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


smc = _load("smc_forex_metals_bot", "16_smc_forex_metals_bot.py")
mayank = _load("mayank_bot", "17_mayank_bot.py")

SMC_SRC = (open(os.path.join(REPO_ROOT, "16_smc_forex_metals_bot.py"), encoding="utf-8").read())
MAYANK_SRC = (open(os.path.join(REPO_ROOT, "17_mayank_bot.py"), encoding="utf-8").read())


# ---------------------------------------------------------------------------
# (a) Weekend / stale-candle gate
# ---------------------------------------------------------------------------
def test_fresh_candle_is_not_stale():
    """A candle scanned a few minutes after its close is live → signal allowed."""
    t = datetime.datetime(2026, 7, 10, 12, 0, tzinfo=UTC)  # candle open
    now = t + datetime.timedelta(hours=1, minutes=5)       # 5 min after 1h close
    assert smc.is_stale_candle(pd.Timestamp("2026-07-10 12:00"), "1h", now) is False
    assert mayank.is_stale_candle(pd.Timestamp("2026-07-10 12:00"), "1h", now) is False


def test_one_period_late_is_tolerated():
    """Two-candle tolerance: a single missed period (live-feed lag) must not gate."""
    now = datetime.datetime(2026, 7, 10, 14, 0, tzinfo=UTC)  # 2h past a 1h candle's open
    # close = 13:00, now-close = 1h < 2*dur → still fresh
    assert smc.is_stale_candle(pd.Timestamp("2026-07-10 12:00"), "1h", now) is False


def test_weekend_frozen_candle_is_stale():
    """The bug scenario: a Friday candle scanned on the weekend, no fresh candle
    since — the 12h cooldown has long expired but there is nothing new to act on."""
    friday = pd.Timestamp("2026-07-10 20:00")  # last closed 1h candle before the weekend
    sunday = datetime.datetime(2026, 7, 12, 18, 0, tzinfo=UTC)
    assert smc.is_stale_candle(friday, "1h", sunday) is True
    assert mayank.is_stale_candle(friday, "1h", sunday) is True


def test_stale_boundary_is_exactly_two_periods_past_close():
    """close = open + dur; stale ⟺ now-close >= 2*dur ⟺ now >= open + 3*dur."""
    t = pd.Timestamp("2026-07-10 12:00")
    dur = datetime.timedelta(hours=1)
    just_before = datetime.datetime(2026, 7, 10, 12, 0, tzinfo=UTC) + 3 * dur - datetime.timedelta(seconds=1)
    exactly = datetime.datetime(2026, 7, 10, 12, 0, tzinfo=UTC) + 3 * dur
    assert smc.is_stale_candle(t, "1h", just_before) is False
    assert smc.is_stale_candle(t, "1h", exactly) is True


def test_stale_gate_scales_with_timeframe():
    """A 4h candle 3h past close is still fresh; a 1h candle 3h past close is stale."""
    t = pd.Timestamp("2026-07-10 12:00")
    now = datetime.datetime(2026, 7, 10, 15, 30, tzinfo=UTC)  # 3.5h after open
    assert smc.is_stale_candle(t, "1h", now) is True    # 1h: >= open+3h
    assert smc.is_stale_candle(t, "4h", now) is False   # 4h: needs open+12h


def test_stale_gate_is_wired_into_both_scan_loops():
    """Source guard: the gate must actually short-circuit the scan, not just exist."""
    assert re.search(r"if is_stale_candle\([^\n]*\):\s*\n\s*continue", SMC_SRC), (
        "16: is_stale_candle no longer guards run_smc_analysis"
    )
    assert re.search(r"if is_stale_candle\([^\n]*\):\s*\n\s*continue", MAYANK_SRC), (
        "17: is_stale_candle no longer guards analyze_strategy"
    )


# ---------------------------------------------------------------------------
# (b) FVG age limit (bot 16)
# ---------------------------------------------------------------------------
def _fvg_df(n, fvg_at):
    """Flat series with a single 3-candle bullish FVG planted at index `fvg_at`
    (high[i-2] < low[i], close[i-1] > open[i-1]), never mitigated afterwards."""
    rows = [(100.0, 100.5, 99.5, 100.0)] * n
    rows = [list(r) for r in rows]
    i = fvg_at
    rows[i - 2] = [100.0, 100.5, 99.5, 100.0]   # high 100.5 = gap bottom
    rows[i - 1] = [100.0, 101.0, 100.0, 101.0]  # bullish body
    rows[i] = [102.0, 103.0, 101.5, 102.5]      # low 101.5 = gap top (>100.5)
    # keep every later candle above the gap top so nothing mitigates it
    for j in range(i + 1, n):
        rows[j] = [102.5, 103.0, 102.0, 102.5]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"]).astype(float)


def test_recent_fvg_is_kept():
    df = _fvg_df(80, fvg_at=76)  # 4 bars old
    fvgs = smc.find_unmitigated_fvgs(df, "BULLISH")
    assert any(f["index"] == 76 for f in fvgs), "a fresh unmitigated FVG must survive"


def test_ancient_fvg_is_dropped_by_age_limit():
    df = _fvg_df(80, fvg_at=5)  # ~74 bars old, > FVG_MAX_AGE
    fvgs = smc.find_unmitigated_fvgs(df, "BULLISH")
    assert all(f["index"] != 5 for f in fvgs), "an ancient FVG must be aged out"


def test_age_limit_is_the_cause_divergence_canary():
    """The same ancient FVG re-appears once the age bound is lifted — proving the
    age limit (not some other filter) is what removes it."""
    df = _fvg_df(80, fvg_at=5)
    assert smc.find_unmitigated_fvgs(df, "BULLISH", max_age=len(df)) != []
    assert smc.find_unmitigated_fvgs(df, "BULLISH") == []


# ---------------------------------------------------------------------------
# (c) SL/RR sanity guard (bot 17)
# ---------------------------------------------------------------------------
def test_sound_geometry_passes():
    # LONG: entry 100, sl 99 (1% risk), tp1 102 (2% reward) → RR 2.0
    assert mayank.passes_sl_rr_guard(100.0, 99.0, 102.0, "LONG") is True
    # SHORT: entry 100, sl 101 (1% risk), tp1 98 (2% reward)
    assert mayank.passes_sl_rr_guard(100.0, 101.0, 98.0, "SHORT") is True


def test_stop_too_far_is_rejected():
    # 20% SL distance > MAX_SL_DIST (15%) → liquidation risk
    assert mayank.passes_sl_rr_guard(100.0, 80.0, 130.0, "LONG") is False


def test_upside_down_geometry_is_rejected():
    # LONG with TP below entry (reward <= 0)
    assert mayank.passes_sl_rr_guard(100.0, 99.0, 99.5, "LONG") is False
    # LONG with SL above entry (wrong side, risk <= 0)
    assert mayank.passes_sl_rr_guard(100.0, 101.0, 105.0, "LONG") is False


def test_reward_below_min_rr_is_rejected():
    # risk 2% (sl 98), reward 0.5% (tp 100.5) → RR 0.25 < MIN_RR (0.5)
    assert mayank.passes_sl_rr_guard(100.0, 98.0, 100.5, "LONG") is False


def test_sl_rr_guard_is_wired_into_both_branches():
    long_guard = re.search(r'passes_sl_rr_guard\([^\n]*"LONG"\):\s*\n(?:[^\n]*\n)*?\s*break', MAYANK_SRC)
    short_guard = re.search(r'passes_sl_rr_guard\([^\n]*"SHORT"\):\s*\n(?:[^\n]*\n)*?\s*break', MAYANK_SRC)
    assert long_guard, "17: SL/RR guard missing from the LONG branch"
    assert short_guard, "17: SL/RR guard missing from the SHORT branch"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("OK — SMC weekend-refire / FVG-age / SL-RR guards hold")

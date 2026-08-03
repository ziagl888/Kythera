"""DB-free tests for the FMR2 normalisation exit (K4).

Load-bearing logic: (1) the exit predicate fmr2_funding_normalized (SHORT exits
as soon as funding_cs_pctl<0.80 OR funding_z_30d<1.0; LONG symmetric) and
(2) the settlement walk simulate_normalization_exit (time stop after 9
settlements, normalisation exit at the settlement candle close, hard
catastrophe SL as a first-touch net, open_at_end).

No DB access — runs standalone (see __main__).
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.research_features import (  # noqa: E402
    FMR2_CATASTROPHE_SL_PCT,
    FMR2_TIME_STOP_SETTLEMENTS,
    fmr2_catastrophe_sl,
    fmr2_funding_normalized,
)
from tools.fmr1_build_dataset import simulate_normalization_exit  # noqa: E402


# --------------------------------------------------------------------------- #
# Exit predicate                                                               #
# --------------------------------------------------------------------------- #
def test_short_exit_predicate():
    # SHORT at the extreme (cs high, z high) → NOT normalised.
    assert fmr2_funding_normalized("SHORT", 0.99, 3.0) is False
    # cs back below 0.80 → normalised (OR branch 1).
    assert fmr2_funding_normalized("SHORT", 0.79, 3.0) is True
    # z back below 1.0 → normalised (OR branch 2), even if cs is still extreme.
    assert fmr2_funding_normalized("SHORT", 0.99, 0.9) is True
    # exactly at the thresholds (strict <): 0.80 / 1.0 are STILL extreme.
    assert fmr2_funding_normalized("SHORT", 0.80, 1.0) is False


def test_long_exit_predicate_symmetric():
    # LONG at the extreme (cs low, z low) → NOT normalised.
    assert fmr2_funding_normalized("LONG", 0.01, -3.0) is False
    # cs back above 0.20 → normalised.
    assert fmr2_funding_normalized("LONG", 0.21, -3.0) is True
    # z back above -1.0 → normalised.
    assert fmr2_funding_normalized("LONG", 0.01, -0.9) is True
    assert fmr2_funding_normalized("LONG", 0.20, -1.0) is False


def test_predicate_nan_is_fail_safe():
    # NaN in one quantity → both comparisons False → NOT normalised (keep holding).
    assert fmr2_funding_normalized("SHORT", float("nan"), float("nan")) is False
    assert fmr2_funding_normalized("LONG", float("nan"), float("nan")) is False
    # cs NaN, but z trips anyway → normalised (OR).
    assert fmr2_funding_normalized("SHORT", float("nan"), 0.5) is True


def test_catastrophe_sl_prices():
    frac = FMR2_CATASTROPHE_SL_PCT / 100.0
    assert math.isclose(fmr2_catastrophe_sl("LONG", 100.0), 100.0 * (1 - frac))
    assert math.isclose(fmr2_catastrophe_sl("SHORT", 100.0), 100.0 * (1 + frac))


# --------------------------------------------------------------------------- #
# Settlement walk fixture                                                     #
# --------------------------------------------------------------------------- #
def make_walk(n_settle=20, ev_pos=5, cs=None, rate=0.0001, price=100.0):
    """Hourly candles (flat at ``price``) + 8h settlement history.

    entry_idx = 8*ev_pos → entry candle 1h BEFORE the event settlement; the walk
    starts on the event settlement candle, the first exit-eligible settlement
    is 8h (8 candles) later. Return value: all arguments for simulate_normalization_exit.
    """
    t0 = pd.Timestamp("2026-03-01 00:00")
    f_ts = np.array([(t0 + pd.Timedelta(hours=8 * k)).to_datetime64() for k in range(n_settle)])
    f_rates = np.full(n_settle, rate, dtype=np.float64)
    if cs is None:
        cs = np.full(n_settle, 0.99, dtype=np.float64)
    else:
        cs = np.asarray(cs, dtype=np.float64)

    start = t0 - pd.Timedelta(hours=1)
    end = t0 + pd.Timedelta(hours=8 * (n_settle - 1) + 8)
    n_h = int((end - start) / pd.Timedelta(hours=1)) + 1
    times = np.array([(start + pd.Timedelta(hours=i)).to_datetime64() for i in range(n_h)])
    highs = np.full(n_h, price, dtype=np.float64)
    lows = np.full(n_h, price, dtype=np.float64)
    closes = np.full(n_h, price, dtype=np.float64)
    entry_idx = 8 * ev_pos
    return dict(
        times=times,
        highs=highs,
        lows=lows,
        closes=closes,
        entry_idx=entry_idx,
        f_ts=f_ts,
        f_rates=f_rates,
        cs_pctl=cs,
        ev_pos=ev_pos,
    )


def test_walk_time_stop_at_9_settlements():
    """cs stays extreme (0.99), z=NaN (constant rates) → never normalised →
    forced close after exactly FMR2_TIME_STOP_SETTLEMENTS settlements."""
    fx = make_walk()
    res = simulate_normalization_exit("SHORT", 100.0, **fx)
    assert res["exit_reason"] == "time_stop"
    assert res["settlements"] == FMR2_TIME_STOP_SETTLEMENTS
    assert res["net_pnl_pct"] is not None


def test_walk_normalized_exit():
    """cs drops back below 0.80 on the 3rd holding settlement (ev_pos+3) → exit there."""
    cs = np.full(20, 0.99)
    cs[5 + 3] = 0.5  # ev_pos=5 default → 3rd forward settlement
    fx = make_walk(cs=cs)
    res = simulate_normalization_exit("SHORT", 100.0, **fx)
    assert res["exit_reason"] == "normalized"
    assert res["settlements"] == 3


def test_walk_catastrophe_sl_first_touch():
    """SHORT, price spike >15% in the first holding candle → touch-based
    catastrophe SL hits before any settlement."""
    fx = make_walk()
    sl = fmr2_catastrophe_sl("SHORT", 100.0)  # 115.0
    fx["highs"][fx["entry_idx"] + 1] = sl + 5.0  # spike above the SL
    res = simulate_normalization_exit("SHORT", 100.0, **fx)
    assert res["exit_reason"] == "catastrophe_sl"
    assert res["settlements"] == 0
    # PnL = −15% (SHORT against the spike) minus fees.
    assert res["net_pnl_pct"] < -FMR2_CATASTROPHE_SL_PCT + 0.5


def test_walk_open_at_end():
    """No forward settlement in range (ev_pos = last) → trade stays open until
    the end of the data → label-bearing None."""
    fx = make_walk(n_settle=7, ev_pos=6)
    res = simulate_normalization_exit("SHORT", 100.0, **fx)
    assert res["exit_reason"] == "open_at_end"
    assert res["net_pnl_pct"] is None


def test_walk_normalized_prices_at_settlement_close():
    """Normalisation exit prices at the settlement candle close (not TP/SL).
    With a flat price == entry → PnL = pure fees (negative, small)."""
    cs = np.full(20, 0.99)
    cs[5 + 2] = 0.1
    fx = make_walk(cs=cs)
    res = simulate_normalization_exit("SHORT", 100.0, **fx)
    assert res["exit_reason"] == "normalized" and res["settlements"] == 2
    assert -0.2 < res["net_pnl_pct"] < 0.0  # round-trip fees only


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK  {name}")
    print("\nAll FMR2 exit tests green.")

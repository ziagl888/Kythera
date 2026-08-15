"""DB-free guards for tools/pump_long_sl_study.py (T-2026-KYT-9050-145).

Synthetic-candle checks of the SL path engine — entry causality, SL touch,
gap-through fills, MAE math, void handling and the funding sign. No DB.

Run: python -m pytest backtest/test_pump_long_sl_study.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("pump_long_sl_study", ROOT / "tools" / "pump_long_sl_study.py")
pls = importlib.util.module_from_spec(_spec)
sys.modules["pump_long_sl_study"] = pls
_spec.loader.exec_module(pls)

T0 = pd.Timestamp("2026-07-01 12:00", tz="UTC")


def _candles(rows):
    """rows: list of (minute_offset, open, high, low, close)."""
    return pd.DataFrame(
        {
            "open_time": [T0 + pd.Timedelta(minutes=m) for m, *_ in rows],
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
        }
    )


def test_entry_is_next_candle_open_and_void_when_feed_gap():
    c = _candles([(5, 100, 101, 99, 100), (10, 100, 100, 100, 100)])
    sim = pls.simulate_event(c, T0)
    assert sim is not None and sim["entry"] == 100.0
    assert sim["entry_t"] == T0 + pd.Timedelta(minutes=5)
    # Feed gap: first candle 20 min after the signal (> ENTRY_MAX_DELAY_MIN) => void.
    late = _candles([(20, 100, 101, 99, 100)])
    assert pls.simulate_event(late, T0) is None


def test_sl_touch_fills_at_stop_and_gap_through_fills_at_open():
    # Entry 100. Candle 2 dips to 89 (touches SL10 at 90 => fill 90). For SL15
    # (85) candle 3 OPENS at 80 (gap through) => fill at 80, not 85.
    rows = [(5, 100, 101, 99, 100), (10, 99, 100, 89, 95), (15, 80, 82, 78, 81)]
    # pad flat candles to fill the 24h horizon end
    rows += [(20 + i * 5, 95, 96, 94, 95) for i in range(4)]
    sim = pls.simulate_event(_candles(rows), T0)
    assert round(sim["ret_24h_sl10"], 6) == -10.0  # filled exactly at the stop
    assert round(sim["ret_24h_sl15"], 6) == -20.0  # gap-through: filled at open 80
    assert sim["exit_t_24h_sl10"] == T0 + pd.Timedelta(minutes=10)
    assert sim["exit_t_24h_sl15"] == T0 + pd.Timedelta(minutes=15)
    # A stop no candle low ever reaches behaves exactly like hold.
    assert sim["ret_24h_sl40"] == sim["hold_24h"]
    assert sim["exit_t_24h_sl40"] == sim["hold_exit_t_24h"]


def test_mae_and_hold_are_measured_from_entry():
    # Entry 100, deepest low 88 => MAE -12%; last close 110 => hold +10%.
    rows = [(5, 100, 101, 99, 100), (10, 99, 100, 88, 95), (15, 95, 111, 94, 110)]
    sim = pls.simulate_event(_candles(rows), T0)
    assert round(sim["mae_24h"], 6) == -12.0
    assert round(sim["hold_24h"], 6) == 10.0


def test_entry_candle_low_can_already_stop_the_trade():
    # The entry candle itself wicks to 85: SL10 must trigger on it, not be
    # skipped because it is the entry bar.
    rows = [(5, 100, 101, 85, 98), (10, 98, 99, 97, 98)]
    sim = pls.simulate_event(_candles(rows), T0)
    assert round(sim["ret_24h_sl10"], 6) == -10.0
    assert sim["exit_t_24h_sl10"] == T0 + pd.Timedelta(minutes=5)


def test_funding_sign_long_pays_positive_rates():
    fr = pd.DataFrame(
        {
            "funding_time": [T0 + pd.Timedelta(hours=8), T0 + pd.Timedelta(hours=16), T0 + pd.Timedelta(hours=40)],
            "funding_rate": [0.001, -0.0005, 0.002],  # last one lies beyond exit
        }
    )
    cost = pls.funding_cost_pct(fr, T0, T0 + pd.Timedelta(hours=24))
    # (0.001 - 0.0005) * 100 = +0.05%-points paid by the long; 40h event excluded.
    assert round(cost, 6) == 0.05


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("OK — pump_long_sl_study contracts hold")

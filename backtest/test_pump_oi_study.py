"""DB-free guards for tools/pump_oi_study.py (T-2026-KYT-9050-144).

Synthetic-frame checks of the causal feature math, the event conditions and
the dedupe/cooldown — the parts a wrong sign or an off-by-one window would
silently corrupt. No DB, no network.

Run: python -m pytest backtest/test_pump_oi_study.py -q
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("pump_oi_study", ROOT / "tools" / "pump_oi_study.py")
pos = importlib.util.module_from_spec(_spec)
sys.modules["pump_oi_study"] = pos
_spec.loader.exec_module(pos)


def _grid(rows):
    """rows: list of (hour_offset, symbol, oi, px)."""
    t0 = pd.Timestamp("2026-07-01 00:00", tz="UTC")
    return pd.DataFrame(
        {
            "ts": [t0 + pd.Timedelta(hours=h) for h, *_ in rows],
            "symbol": [r[1] for r in rows],
            "open_interest": [float(r[2]) for r in rows],
            "oi_value_usdt": [float(r[2]) * float(r[3]) for r in rows],
            "px": [float(r[3]) for r in rows],
        }
    )


def test_pump_and_oi_features_are_causal_and_correctly_scaled():
    # Price doubles between h=24 and h=28 (dpx_24h at h=28 vs h=4 => +100%);
    # OI rises 20% in the last 4h of that window. Grid extends far enough that
    # the 4h forward window exists at the assertion point.
    rows = []
    for h in range(41):
        px = 1.0 if h < 25 else 2.0
        oi = 100.0 if h < 27 else 120.0
        rows.append((h, "AAAUSDT", oi, px))
    g = pos.add_features(_grid(rows))
    at28 = g[(g["symbol"] == "AAAUSDT") & (g["ts"] == pd.Timestamp("2026-07-02 04:00", tz="UTC"))].iloc[0]
    assert at28["dpx_24h"] == 100.0, at28["dpx_24h"]  # px 2.0 vs 1.0 24h earlier
    assert round(at28["doi_4h"], 6) == 20.0, at28["doi_4h"]  # oi 120 vs 100 4h earlier
    # Forward returns are computed from FUTURE points only (flat price after 28h => 0).
    assert at28["fwd_4h"] == 0.0


def test_rollover_features_capture_build_then_bleed():
    # OI builds 100 -> 150 (peak) then bleeds to 130: build_24h vs oi_24h_ago,
    # offpeak vs the trailing peak — the APR/BR pattern the mechanic encodes.
    rows = []
    for h in range(31):
        if h < 20:
            oi = 100.0
        elif h < 26:
            oi = 150.0
        else:
            oi = 130.0
        rows.append((h, "BBBUSDT", oi, 1.0))
    g = pos.add_features(_grid(rows))
    at30 = g[(g["symbol"] == "BBBUSDT") & (g["ts"] == pd.Timestamp("2026-07-02 06:00", tz="UTC"))].iloc[0]
    assert round(at30["oi_build_24h"], 6) == 50.0, at30["oi_build_24h"]  # peak 150 vs 100 24h ago
    assert round(at30["oi_offpeak"], 4) == round((130 / 150 - 1) * 100, 4)  # ~-13.33% off peak
    # Would qualify M2 at the pre-registered thresholds:
    assert at30["oi_build_24h"] >= pos.ROLL_BUILD_24H
    assert at30["oi_offpeak"] <= pos.ROLL_OFFPEAK


def test_post_collapse_features_capture_pump_then_crash():
    # BLUAI pattern (registered extension M4): px 1.0 -> 2.0 (pump) -> 1.1
    # (collapse). At the end: 48h peak 2.0 was +100% over px_48h_ago, price
    # sits -45% off that peak — qualifies M4 at the registered thresholds.
    rows = []
    for h in range(53):
        if h < 30:
            px = 1.0
        elif h < 40:
            px = 2.0
        else:
            px = 1.1
        rows.append((h, "DDDUSDT", 100.0, px))
    g = pos.add_features(_grid(rows))
    at52 = g[(g["symbol"] == "DDDUSDT") & (g["ts"] == pd.Timestamp("2026-07-03 04:00", tz="UTC"))].iloc[0]
    assert round(at52["px_pump_48h"], 6) == 100.0, at52["px_pump_48h"]  # peak 2.0 vs 1.0 48h ago
    assert round(at52["px_retrace"], 4) == round((1.1 / 2.0 - 1) * 100, 4)  # -45% off peak
    assert at52["px_pump_48h"] >= pos.COLLAPSE_PUMP_48H
    assert at52["px_retrace"] <= pos.COLLAPSE_RETRACE


def test_dedupe_enforces_per_symbol_cooldown_first_wins():
    t0 = pd.Timestamp("2026-07-01 00:00", tz="UTC")
    ev = pd.DataFrame(
        {
            "ts": [t0, t0 + pd.Timedelta(hours=2), t0 + pd.Timedelta(hours=25), t0 + pd.Timedelta(hours=1)],
            "symbol": ["AAAUSDT", "AAAUSDT", "AAAUSDT", "CCCUSDT"],
        }
    )
    kept = pos.dedupe(ev)
    # AAA: first event wins, +2h suppressed (inside 24h), +25h kept; CCC untouched.
    assert len(kept) == 3
    aaa = kept[kept["symbol"] == "AAAUSDT"]["ts"].tolist()
    assert aaa == [t0, t0 + pd.Timedelta(hours=25)]


def test_report_side_signs_short_as_pump_fade():
    # A pump that retraces -10% over 4h must score POSITIVE for SHORT and
    # negative for LONG — the sign convention the whole verdict rests on.
    t0 = pd.Timestamp("2026-07-01 00:00", tz="UTC")
    ev = pd.DataFrame(
        {
            "ts": [t0 + pd.Timedelta(hours=i) * 30 for i in range(31)],
            "symbol": [f"S{i}USDT" for i in range(31)],
            "fwd_4h": [-10.0] * 31,
            "fwd_8h": [-10.0] * 31,
            "fwd_24h": [-10.0] * 31,
        }
    )
    short = pos.report_side("TEST", "SHORT", ev, None)
    long_ = pos.report_side("TEST", "LONG", ev, None)
    assert short["net_4h"] == round(10.0 - pos.FEE_RT, 3)
    assert long_["net_4h"] == round(-10.0 - pos.FEE_RT, 3)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("OK — pump_oi_study contracts hold")

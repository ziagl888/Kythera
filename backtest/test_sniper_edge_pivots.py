# backtest/test_sniper_edge_pivots.py
"""Standalone (DB-free) guard for T-2026-CU-9050-093 (P1.46-rest) on
25_smc_ml_sniper.py.

argrelextrema runs with the default mode='clip', so a pivot inside the last
PIVOT_WINDOW closed candles is flagged against clipped (boundary-repeated) right
neighbours: fewer than PIVOT_WINDOW real candles confirm it, and it can still
repaint once the next candles close (a later candle exceeds the level → the point
was never a pivot). 24_quasimodo_bot.py drops the whole last-PIVOT_WINDOW band;
here that is not a drop-in because the Three-Drive freshness gate hunts exactly
those fresh edge pivots. The operator-approved policy (option B) keeps a pivot
only once >= PIVOT_WINDOW//2 closed candles confirm it — halving the residual
repaint window while leaving TD a fresh-reversal entry. One shared filter feeds
both consumers (TD gate + find_breaker_setup).

Loader note: 25_smc_ml_sniper.py loads its XGB artifacts at import and calls
exit(1) on failure, so joblib.load is mocked to a valid artifact dict. pandas /
numpy / scipy are imported before the sys.modules patch (memory
patch-dict-sys-modules-numpy-teardown).

Run: python backtest/test_sniper_edge_pivots.py   (or: pytest -q)
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import unittest.mock as mock

import numpy as np
import pandas as pd  # noqa: F401  (pre-seed before any sys.modules patch)
import scipy.signal  # noqa: F401  (pre-seed C-ext before any sys.modules patch)
import scipy.stats  # noqa: F401  (pre-seed C-ext before any sys.modules patch)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PIVOT_WINDOW = 10
PIVOT_CONFIRM = PIVOT_WINDOW // 2


def _load_sniper():
    spec = importlib.util.spec_from_file_location(
        "smc_ml_sniper", os.path.join(REPO_ROOT, "25_smc_ml_sniper.py")
    )
    mod = importlib.util.module_from_spec(spec)
    fake_artifact = {
        "model": mock.MagicMock(),
        "features": ["dir_num"],
        "optimal_threshold": None,
        "calibrator_isotonic": None,
        "meta": {},
    }
    fake_joblib = mock.MagicMock()
    fake_joblib.load.return_value = fake_artifact
    with mock.patch.dict(
        "sys.modules",
        {
            "joblib": fake_joblib,
            "core.database": mock.MagicMock(),
            "core.config": mock.MagicMock(CH_SNIPER_BB=-1, CH_SNIPER_TD=-2),
            "core.market_utils": mock.MagicMock(COOLDOWN_MODULE_MAX_LEN=10),
            "core.trade_utils": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


sniper = _load_sniper()
SRC = open(os.path.join(REPO_ROOT, "25_smc_ml_sniper.py"), encoding="utf-8").read()


def _scan_body():
    body = re.search(r"def scan_market\(\):\n(.*?)\ndef ", SRC, re.DOTALL)
    assert body, "scan_market body not found"
    return body.group(1)


# ---------------------------------------------------------------------------
# The mechanism: an unconfirmed right-edge pivot repaints, a confirmed one does not
# ---------------------------------------------------------------------------
def _edge_filter(pivot_idx, last_closed, confirm=PIVOT_CONFIRM):
    """Verbatim shape of the filter in scan_market."""
    return pivot_idx[pivot_idx <= last_closed - confirm]


def test_unconfirmed_edge_peak_repaints_and_is_filtered():
    """A peak flagged by argrelextrema(mode='clip') within PIVOT_WINDOW of the
    right edge — clip compares it against the repeated boundary value, so it is
    accepted with fewer than PIVOT_WINDOW real right neighbours — can vanish when
    the next candle closes above it. The filter drops exactly that pivot before it
    can be scored. (The rightmost index is never flagged: clip makes it compare
    with itself, so the edge peak sits a couple candles in.)"""
    base = np.concatenate(
        [
            np.linspace(100.0, 112.0, 12),                   # early hump, idx 0..11
            np.linspace(112.0, 104.0, 8)[1:],                # decline, idx 12..18
            np.array([106.0, 110.0, 114.0, 118.0, 116.0, 114.0]),  # fresh top 118 at idx 22
        ]
    )
    frame_a = base.copy()
    last_a = len(frame_a) - 1                                 # 24
    piv_a = scipy.signal.argrelextrema(frame_a, np.greater, order=PIVOT_WINDOW)[0]
    edge = int(np.argmax(frame_a))                            # the 118 top, idx 22
    assert edge in piv_a, "clip must flag the unconfirmed edge peak — else the fixture is moot"
    assert last_a - edge < PIVOT_CONFIRM, "fixture peak is not actually near the edge"

    # frame_b: one more candle closes ABOVE the edge top → the pivot repaints away
    frame_b = np.append(frame_a, 120.0)
    piv_b = scipy.signal.argrelextrema(frame_b, np.greater, order=PIVOT_WINDOW)[0]
    assert edge not in piv_b, "fixture degenerated: the edge peak must actually repaint"

    # The filter on frame_a (last_closed = last_a) drops the unconfirmed edge peak
    assert edge not in _edge_filter(piv_a, last_a), "filter must drop the repaint-prone edge peak"


def test_confirmed_pivot_survives_the_filter():
    """A pivot with >= PIVOT_WINDOW//2 confirming closed candles is kept."""
    base = np.concatenate(
        [
            np.linspace(100.0, 120.0, 15),          # peak at idx 14
            np.linspace(120.0, 108.0, 12)[1:],      # >= PIVOT_WINDOW//2 candles after it
        ]
    )
    last = len(base) - 1
    piv = scipy.signal.argrelextrema(base, np.greater, order=PIVOT_WINDOW)[0]
    peak = int(piv[np.argmax(base[piv])])
    assert last - peak >= PIVOT_CONFIRM, "fixture peak is not confirmed — adjust the arrays"
    assert peak in _edge_filter(piv, last), "a confirmed pivot must survive the filter"


def test_filter_threshold_is_exactly_half_the_window():
    """Boundary: r == PIVOT_CONFIRM survives, r == PIVOT_CONFIRM - 1 is dropped."""
    last = 100
    idx = np.array([last - PIVOT_CONFIRM, last - PIVOT_CONFIRM + 1])
    kept = _edge_filter(idx, last)
    assert (last - PIVOT_CONFIRM) in kept, "the exactly-confirmed pivot must be kept"
    assert (last - PIVOT_CONFIRM + 1) not in kept, "a pivot one candle short must be dropped"


# ---------------------------------------------------------------------------
# Source guards
# ---------------------------------------------------------------------------
def test_edge_filter_present_in_scan_market():
    body = _scan_body()
    assert "PIVOT_CONFIRM = PIVOT_WINDOW // 2" in body, "edge-pivot confirm window changed or vanished"
    assert re.search(r"last_closed\s*=\s*len\(df\)\s*-\s*2", body), (
        "last_closed anchor lost — the forming candle is already dropped, newest closed is len(df)-2"
    )
    assert re.search(r"peak_idx\s*=\s*peak_idx\[peak_idx\s*<=\s*last_closed\s*-\s*PIVOT_CONFIRM\]", body), (
        "peak_idx edge filter lost — unconfirmed edge peaks would repaint"
    )
    assert re.search(r"trough_idx\s*=\s*trough_idx\[trough_idx\s*<=\s*last_closed\s*-\s*PIVOT_CONFIRM\]", body), (
        "trough_idx edge filter lost — unconfirmed edge troughs would repaint"
    )


def test_filter_runs_before_the_three_pivot_gate():
    """The filter must precede `len(peak_idx) < 3` so TD/BB see only kept pivots."""
    body = _scan_body()
    i_filter = body.index("last_closed - PIVOT_CONFIRM")
    i_gate = body.index("len(peak_idx) < 3")
    assert i_filter < i_gate, "edge filter must run before the pivot-count gate"


def test_forming_candle_drop_still_present():
    """Guard-of-a-guard: the P1.46 closed-candle slice must survive this edit."""
    body = _scan_body()
    assert re.search(r"c_highs,\s*c_lows\s*=\s*highs\[:-1\],\s*lows\[:-1\]", body), (
        "P1.46 forming-candle drop was lost — pivots would repaint on the forming candle"
    )


def test_bb_selector_still_present():
    """P2.39/T-089 find_breaker_setup must not regress."""
    body = _scan_body()
    assert "find_breaker_setup(peak_idx" in body, "BB LONG no longer uses find_breaker_setup"
    assert "find_breaker_setup(trough_idx" in body, "BB SHORT no longer uses find_breaker_setup"
    assert callable(sniper.find_breaker_setup), "find_breaker_setup vanished from the module"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("OK — sniper edge-pivot confirmation policy holds")

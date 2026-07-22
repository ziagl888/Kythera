"""DB-free tests for the Stoic 1-2-3 state machine (tools/research/stoic123).

Covers SPEC:
  AK1 — deterministic + lookahead-free (prefix-stability).
  AK2 — Boundary fixed before the Step-3 bar (boundary set at STEP2, entry at t>base_end).
  AK3 — the 5 distortions each have a guard test that catches them:
        wick-not-close, HTF-invented, boundary-after-break, skipped-retest, repaint.

Deterministic synthetic OHLCV, no ccxt. Runnable standalone or via pytest.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "research", "stoic123")
sys.path.insert(0, _DIR)

from params import StoicParams  # noqa: E402
from rules import compute_indicators, htf_location_series  # noqa: E402
from state_machine import _DirectionMachine, generate_signals  # noqa: E402

WARM = list(100 + 2.0 * np.sin(np.arange(30) / 1.5))
IMPULSE = [102, 104, 106, 108, 109]
BASE = [106, 105.6, 105.8, 105.5, 105.7, 105.6, 105.9, 105.7]
BREAKOUT = [109, 112]
ENTRY_IDX = 43  # where the clean bullish 1-2-3 completes (pinned from the tuned fixture)


def make_ohlcv(closes, start="2022-01-01", wick=0.15) -> pd.DataFrame:
    closes = np.asarray(closes, float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    hi = np.maximum(opens, closes) + wick
    lo = np.minimum(opens, closes) - wick
    dates = pd.date_range(start, periods=len(closes), freq="h")
    return pd.DataFrame({"date": dates, "open": opens, "high": hi, "low": lo, "close": closes})


def make_htf(n=60, slope=2.0, start="2021-12-01") -> pd.DataFrame:
    closes = 90 + np.arange(n) * slope  # slope>0 rising (long ok), <0 falling (short ok)
    dates = pd.date_range(start, periods=n, freq="D")
    return pd.DataFrame({"date": dates, "close": closes})


def bullish_123() -> pd.DataFrame:
    return make_ohlcv(WARM + IMPULSE + BASE + BREAKOUT)


# ------------------------------------------------------------- happy path
def test_bullish_123_enters_long():
    df, htf = bullish_123(), make_htf(slope=2.0)
    sig = generate_signals(df, htf).to_numpy()
    assert sig[ENTRY_IDX] == 1, f"no long entry at {ENTRY_IDX}: {np.where(sig != 0)[0].tolist()}"
    assert (sig[:ENTRY_IDX] == 0).all(), "signal fired before the 1-2-3 completed"


def test_bearish_123_enters_short():
    # mirror the path around 200 and flip the HTF trend down
    closes = [200 - c for c in (WARM + IMPULSE + BASE + BREAKOUT)]
    df, htf = make_ohlcv(closes), make_htf(slope=-2.0, start="2021-12-01")
    sig = generate_signals(df, htf).to_numpy()
    assert (sig == -1).any(), "bearish mirror produced no short entry"
    assert not (sig == 1).any(), "bearish mirror wrongly went long"


def test_deterministic():
    df, htf = bullish_123(), make_htf(slope=2.0)
    a = generate_signals(df, htf).to_numpy()
    b = generate_signals(df, htf).to_numpy()
    assert np.array_equal(a, b)


# ----------------------------------------------- AK1 + distortion #5 (repaint)
def test_prefix_stable_no_repaint():
    """Signals on a truncated series equal the prefix of signals on the full
    series -> a later bar never revises an earlier signal (no lookahead/repaint)."""
    df, htf = bullish_123(), make_htf(slope=2.0)
    full = generate_signals(df, htf).to_numpy()
    for m in (ENTRY_IDX, ENTRY_IDX + 1, len(df) - 1):
        short = generate_signals(df.iloc[:m], htf).to_numpy()
        assert np.array_equal(short, full[:m]), f"prefix drift at m={m}"


# ---------------------------------------------- AK2 + distortion #3 (boundary)
def test_boundary_fixed_before_step3_bar():
    """Drive the long machine bar-by-bar: the Boundary is set at STEP2 and the
    entry only fires at a bar strictly after base_end, so no post-break bar can
    move it."""
    df, htf = bullish_123(), make_htf(slope=2.0)
    ind = compute_indicators(df, StoicParams())
    loc = htf_location_series(ind, htf, StoicParams())
    lm = _DirectionMachine("long", ind, loc["htf_long_ok"].to_numpy(), StoicParams())
    boundary_at_step2 = None
    base_end = None
    for t in range(len(df)):
        done = lm.step(t)
        if lm.state == _DirectionMachine.STEP2 and boundary_at_step2 is None:
            boundary_at_step2 = lm.boundary
            base_end = t
        if done:
            assert boundary_at_step2 is not None, "entry without a fixed boundary (skipped STEP2)"
            assert t > base_end, "entry fired on the base-end bar, not strictly after it"
            # the boundary equals the base-window high, computed with bars <= base_end
            expected = float(ind.iloc[base_end - StoicParams().base_window + 1 : base_end + 1]["high"].max())
            assert abs(boundary_at_step2 - expected) < 1e-9
            return
    raise AssertionError("no entry produced")


# ---------------------------------------------- distortion #1 (wick, not close)
def test_wick_through_does_not_trigger_entry():
    """A bar whose HIGH pokes above the boundary but whose CLOSE does not clear
    it by k*ATR must not enter."""
    closes = WARM + IMPULSE + BASE + [105.9]  # a bar that closes back inside the base
    df = make_ohlcv(closes)
    # give that last bar a big wick well above the boundary
    df.loc[df.index[-1], "high"] = 130.0
    sig = generate_signals(df, make_htf(slope=2.0)).to_numpy()
    assert (sig == 0).all(), "a wick-through wrongly triggered an entry"


# --------------------------------------- distortion #2 (LTF-first, HTF-invented)
def test_htf_down_blocks_long_entry():
    """The exact bullish LTF 1-2-3, but the HTF trend is DOWN -> the location
    gate blocks Step 1, so no long entry is ever fabricated."""
    df = bullish_123()
    sig = generate_signals(df, make_htf(slope=-2.0)).to_numpy()
    assert not (sig == 1).any(), "long entered against a down HTF (location invented)"


# -------------------------------------------- distortion #4 (skipped retest)
def test_no_base_no_entry():
    """Break the MAs then run straight up with no consolidation -> no base ever
    forms, so WAIT->STEP3 cannot happen."""
    straight_up = WARM + list(np.linspace(102, 130, 25))  # monotone, never tight
    sig = generate_signals(make_ohlcv(straight_up), make_htf(slope=2.0)).to_numpy()
    assert (sig == 0).all(), "entered without a retest/base (skipped Step 2)"


# --------------------------------------------------------------------- runner
def _run() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{'OK' if not failed else 'FAILED'}: {len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())

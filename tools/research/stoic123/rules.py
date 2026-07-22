"""rules.py — Phase-1 primitives of the Stoic 1-2-3 system.

Deterministic, pure-pandas indicators and predicates the state machine reads:
moving averages, Wilder ATR, the "meaningful break" test (close beyond a level
by k*ATR — never a wick), the base/consolidation detector, and the HTF location
gate. No arch/ccxt, no lookahead helpers — every function reads only its inputs.

Invariants:
  * Every value at row t is a function of rows <= t (MAs and ATR are trailing;
    the base detector reads a trailing window). Nothing here peeks forward.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from params import StoicParams


def moving_average(close: pd.Series, period: int, kind: str = "ema") -> pd.Series:
    if kind == "sma":
        return close.rolling(period, min_periods=period).mean()
    return close.ewm(span=period, adjust=False).mean()


def wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR: SMA-seeded true range, then recursive smoothing.
    True range uses the previous close, so ATR at row t depends only on rows <= t.
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    # Wilder smoothing == EMA with alpha = 1/period (adjust=False), seeded on TR.
    atr = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    return atr


def compute_indicators(df: pd.DataFrame, p: StoicParams) -> pd.DataFrame:
    """Attach fast/slow MA + ATR to an OHLC(date) frame. Returns a copy."""
    out = df.copy().reset_index(drop=True)
    out["ma_fast"] = moving_average(out["close"], p.ma_fast, p.ma_type)
    out["ma_slow"] = moving_average(out["close"], p.ma_slow, p.ma_type)
    out["atr"] = wilder_atr(out, p.atr_period)
    return out


def meaningful_break(close: float, level: float, atr: float, k: float, direction: str) -> bool:
    """True iff CLOSE clears ``level`` by at least k*ATR in ``direction``.
    The whole point of distortion #1: a wick/high poke is not a break — only a
    close counts, and it must clear by a volatility-scaled margin."""
    if not np.isfinite(close) or not np.isfinite(level) or not np.isfinite(atr) or atr <= 0:
        return False
    if direction == "long":
        return close > level + k * atr
    return close < level - k * atr


def detect_base(
    window: pd.DataFrame, atr_at_end: float, p: StoicParams, direction: str, fast_ma_at_end: float
) -> dict | None:
    """Is the trailing ``window`` (base_window bars, closed) a consolidation/base?

    A base = range < base_max_range_atr * ATR. The boundary the Step-3 breakout
    must clear is the window high (long) / low (short). Optionally require the
    retest to have pulled back to the fast MA (a genuine retest, not drift).
    Returns the fixed boundary dict, or None. Reads only the given window ->
    the boundary is set from bars strictly before Step 3 (distortion #3).
    """
    if len(window) < p.base_window or not np.isfinite(atr_at_end) or atr_at_end <= 0:
        return None
    hi = float(window["high"].max())
    lo = float(window["low"].min())
    rng = hi - lo
    if rng > p.base_max_range_atr * atr_at_end:
        return None
    if p.retest_touch and np.isfinite(fast_ma_at_end):
        # a genuine retest pulls back into the fast MA from the impulse side
        if direction == "long" and lo > fast_ma_at_end:
            return None
        if direction == "short" and hi < fast_ma_at_end:
            return None
    boundary = hi if direction == "long" else lo
    return {"boundary": boundary, "base_high": hi, "base_low": lo, "range": rng}


def htf_location_series(df: pd.DataFrame, htf: pd.DataFrame, p: StoicParams) -> pd.DataFrame:
    """As-of HTF trend gate aligned to the LTF frame (distortion #2).

    For each LTF bar at date t, take the last CLOSED HTF bar with date <= t,
    and read whether the HTF trend is up/down (HTF MA rising/falling over
    htf_slope_lookback) and which side of the HTF MA price sits on. Returns a
    frame aligned to ``df`` with boolean ``htf_long_ok`` / ``htf_short_ok``.
    """
    h = htf.copy().reset_index(drop=True)
    h["htf_ma"] = moving_average(h["close"], p.htf_ma_period, p.htf_ma_type)
    h["htf_slope"] = h["htf_ma"] - h["htf_ma"].shift(p.htf_slope_lookback)
    h["htf_long_ok"] = h["htf_slope"] > 0
    h["htf_short_ok"] = h["htf_slope"] < 0
    if p.htf_require_price_side:
        h["htf_long_ok"] &= h["close"] > h["htf_ma"]
        h["htf_short_ok"] &= h["close"] < h["htf_ma"]

    left = df[["date"]].copy()
    left["date"] = pd.to_datetime(left["date"])
    h["date"] = pd.to_datetime(h["date"])
    merged = pd.merge_asof(
        left.sort_values("date"),
        h[["date", "htf_long_ok", "htf_short_ok"]].sort_values("date"),
        on="date",
        direction="backward",
    )
    merged["htf_long_ok"] = merged["htf_long_ok"].fillna(False).astype(bool)
    merged["htf_short_ok"] = merged["htf_short_ok"].fillna(False).astype(bool)
    return merged.reset_index(drop=True)

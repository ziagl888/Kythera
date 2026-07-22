"""features.py — pure, off-DB reconstruction of ``core.regime_logic.compute_features``.

``compute_features`` reads the last ~31 days of CLOSED 15m candles from the DB and
returns a single feature dict for one ``as_of``. This module vectorises the SAME
math over a full 15m OHLC history, yielding one feature row per candle — so a whole
regime timeline can be rebuilt off ccxt without a DB.

Fidelity contract (kept byte-parallel to compute_features):
  * returns  : close.pct_change(4)=1h, pct_change(16)=4h, ×100 (percent).
  * ATR      : True Range, EMA span=4 (1h) / span=16 (4h), ``adjust=False``.
  * atr_4h_pct = atr_4h / close × 100.
  * vola_p75/p40 : the 75th/40th percentile of ``atr_4h/close×100`` over the
                   trailing VOLA_LOOKBACK_DAYS window (rolling here vs. a fixed
                   read window in the DB path — see NOTE).
  * btcdom_return_24h : (dom[t] - dom[t-96]) / dom[t-96] × 100, aligned on open_time.

Every series here is CAUSAL (pct_change / ewm / trailing-rolling all look only
backward), so a feature row at candle t uses no information past t — the R1
closed-candle discipline holds by construction.

NOTE (documented deviation): ``compute_features`` re-reads a fresh 31-day window
per ``as_of`` and takes ``np.nanpercentile`` over it; we use a trailing rolling
quantile of the same length. The two differ only by the read's +1-day warmup
headroom and pandas-vs-numpy interpolation at the window edge — a slowly varying
threshold, immaterial to a *comparative* whipsaw study where all four variants
consume these identical features. This is a faithful reconstruction off ccxt, not
a byte-copy of the DB ``regime_history``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Mirror of core.regime_logic constants (imported where possible; re-stated here
# only for the rolling-window lengths, which are study-local).
from core.regime_logic import (
    VOLA_HIGH_PERCENTILE,
    VOLA_LOOKBACK_DAYS,
    VOLA_LOW_PERCENTILE,
)

_CANDLES_PER_DAY_15M = 96  # 24h / 15m
_VOLA_WINDOW = VOLA_LOOKBACK_DAYS * _CANDLES_PER_DAY_15M
_DOM_LAG_24H = 96  # 96 × 15m = 24h


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def build_feature_frame(btc: pd.DataFrame, btcdom: pd.DataFrame | None = None) -> pd.DataFrame:
    """Vectorise the compute_features math over a full 15m OHLC history.

    Args:
        btc:    OHLC frame (``open_time, open, high, low, close, ...``), 15m.
        btcdom: optional BTCDOM OHLC frame (only ``open_time, close`` used).
                If None, ``btcdom_return_24h`` is NaN → classifier falls back to
                ALT_NEUTRAL (same safe default as the live path).

    Returns a frame indexed by ``open_time`` with the feature columns the
    classifier consumes, plus ``btc_return_1h``. Rows whose trailing vola window
    is not yet full (first VOLA_LOOKBACK_DAYS) carry NaN percentiles and are the
    caller's warmup zone.
    """
    df = btc.sort_values("open_time").reset_index(drop=True).copy()
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")

    ret_1h = close.pct_change(4) * 100.0
    ret_4h = close.pct_change(16) * 100.0

    tr = _true_range(high, low, close)
    atr_1h = tr.ewm(span=4, adjust=False).mean()
    atr_4h = tr.ewm(span=16, adjust=False).mean()
    atr_1h_pct = atr_1h / close * 100.0
    atr_4h_pct = atr_4h / close * 100.0

    vola_series = atr_4h / close * 100.0
    # Trailing rolling percentile — min_periods keeps early rows NaN (warmup),
    # mirroring the DB path's "insufficient data" skip.
    vola_p75 = vola_series.rolling(_VOLA_WINDOW, min_periods=_CANDLES_PER_DAY_15M).quantile(
        VOLA_HIGH_PERCENTILE / 100.0
    )
    vola_p40 = vola_series.rolling(_VOLA_WINDOW, min_periods=_CANDLES_PER_DAY_15M).quantile(
        VOLA_LOW_PERCENTILE / 100.0
    )

    out = pd.DataFrame(
        {
            "open_time": df["open_time"].values,
            "btc_price": close.values,
            "btc_return_1h": ret_1h.values,
            "btc_return_4h": ret_4h.values,
            "btc_atr_1h_pct": atr_1h_pct.values,
            "btc_atr_4h_pct": atr_4h_pct.values,
            "vola_p75": vola_p75.values,
            "vola_p40": vola_p40.values,
        }
    )

    # ── BTCDOM 24h change, aligned on open_time ──
    if btcdom is not None and len(btcdom):
        dom = btcdom.sort_values("open_time").reset_index(drop=True)
        dom_close = pd.to_numeric(dom["close"], errors="coerce")
        dom_ret = (dom_close - dom_close.shift(_DOM_LAG_24H)) / dom_close.shift(_DOM_LAG_24H) * 100.0
        dom_frame = pd.DataFrame({"open_time": dom["open_time"].values, "btcdom_return_24h": dom_ret.values})
        out = out.merge(dom_frame, on="open_time", how="left")
    else:
        out["btcdom_return_24h"] = np.nan

    return out.set_index("open_time")


def feature_row(frame_row: pd.Series) -> dict:
    """One frame row → the dict shape the ``classify_*`` functions expect.

    ``btcdom_return_24h`` NaN is passed through as None so ``classify_alt_context``
    hits its documented ALT_NEUTRAL fallback (it checks ``is None``).
    """
    dom = frame_row.get("btcdom_return_24h")
    return {
        "btc_return_1h": _f(frame_row.get("btc_return_1h")),
        "btc_return_4h": _f(frame_row.get("btc_return_4h")),
        "btc_atr_1h_pct": _f(frame_row.get("btc_atr_1h_pct")),
        "btc_atr_4h_pct": _f(frame_row.get("btc_atr_4h_pct")),
        "btcdom_return_24h": None if dom is None or (isinstance(dom, float) and np.isnan(dom)) else float(dom),
    }


def _f(v) -> float | None:
    if v is None:
        return None
    v = float(v)
    return None if np.isnan(v) else v

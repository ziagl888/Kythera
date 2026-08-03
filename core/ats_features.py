# core/ats_features.py
"""Shared ATS/TSI detection and feature logic — ONE source for bot 12 and
the walkforward adapter (X-R1 rule: no train/serve skew, hard rule 7).

Provenance: the inline logic from 12_ai_ats_bot.check_tsi_crossovers
(TSI crossover pre-filter + 29-feature contract + live OBV/VWAP), lifted here
while building the ATS2 retrain adapter (T-2026-CU-9050-121). The bot calls
the same functions as the replay — the parity test backtest/test_ats_features
proves build_ats_features == the former serving construction.

No DB access: build_ats_features operates on a finished, chronologically
ascending 1h window (the newest CLOSED candle is the last row). The bot loads
500 candles live (read_candles_with_indicators, include_forming=
False) and normalises OBV to the window start — the replay passes through
exactly the same 500-candle window, so the OBV baseline is identical.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: Candle projection that bot 12 reads (NO 'open' — the ATS features don't
#: need it). Single source for the bot and replay read.
ATS_CANDLE_COLUMNS: tuple[str, ...] = ("open_time", "high", "low", "close", "volume")

#: Indicator columns that bot 12 joins from the *_1h_indicators table. The
#: two TSI columns serve crossover detection, the rest serve the features.
ATS_INDICATOR_COLUMNS: tuple[str, ...] = (
    "rsi_14",
    "rsi_6",
    "tsi_fast_12_7_7",
    "tsi_fast_12_7_7_signal",
    "ema_9",
    "ema_21",
    "ema_50",
    "ema_200",
    "kama_9",
    "kama_21",
    "kama_55",
    "macd_dif_normal_12_26_9",
    "macd_dea_normal_12_26_9",
    "atr_14",
    "boll_upper_20",
    "boll_lower_20",
    "donchian_upper_20",
    "donchian_lower_20",
    "trendline_slope",
    "support_price",
    "resistance_price",
)

TSI_LINE_COL = "tsi_fast_12_7_7"
TSI_SIGNAL_COL = "tsi_fast_12_7_7_signal"

#: Feature contract of the ATS ML in exactly the order that bot 12 enforces
#: (X_live[TSI_FEATURES]). Order is a contract here — do not sort.
ATS_FEATURES: list[str] = [
    "rsi_14",
    "rsi_6",
    "macd_hist",
    "atr_pct",
    "vol_ratio",
    "bb_width",
    "bb_pos",
    "dist_ema200",
    "dist_ema9_21",
    "rsi_ratio",
    "slope_norm",
    "dist_supp",
    "dist_res",
    "dist_kama9",
    "dist_kama21",
    "dist_kama55",
    "dist_kama9_21",
    "dist_donch_up",
    "dist_donch_low",
    "macd_cross_bearish",
    "ema9_21_cross_bearish",
    "kama9_21_cross_bearish",
    "bollinger_lower_break",
    "close_below_ema50",
    "obv_ratio",
    "close_to_vwap_pct",
    "obv_val",
    "volume_spike",
    "volume_trend_up",
]


def ats_cross(tsi_prev: float, sig_prev: float, tsi_curr: float, sig_curr: float) -> str | None:
    """TSI crossover pre-filter of bot 12. Returns 'LONG' | 'SHORT' | None.

    LONG  = TSI crosses the signal line from below to above,
    SHORT = from above to below. Checked on the most recent CLOSED candle
    (curr) against the second-to-last (prev).
    """
    long_cross = (tsi_prev <= sig_prev) and (tsi_curr > sig_curr)
    short_cross = (tsi_prev >= sig_prev) and (tsi_curr < sig_curr)
    if long_cross:
        return "LONG"
    if short_cross:
        return "SHORT"
    return None


def build_ats_features(df: pd.DataFrame) -> dict[str, float]:
    """The 29-feature contract (ATS_FEATURES) as a dict — bot 12 parity.

    `df`: chronologically ascending 1h window (ideally 500 candles like
    live) with ATS_CANDLE_COLUMNS + ATS_INDICATOR_COLUMNS, already numeric
    (the caller coerces + fillna(0) like bot 12). The newest closed
    candle is df.iloc[-1], the second-to-last df.iloc[-2].

    OBV/VWAP are computed on an internal copy (no side effect on
    the passed-in window); the OBV baseline is the window start (df.iloc[0]),
    exactly as in the bot — that's why the replay must pass through the same
    500-candle window that the live bot would see.
    """
    df = df.copy()

    # --- Live feature engineering (OBV, VWAP) like bot 12 ---
    obv_raw = (np.sign(df["close"].diff()) * df["volume"]).fillna(0).cumsum()
    df["obv"] = obv_raw - obv_raw.iloc[0]
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap_20"] = (df["volume"] * df["typical_price"]).rolling(20).sum() / df["volume"].rolling(20).sum()
    df["vwap_20"] = df["vwap_20"].fillna(df["close"])

    current_idx = -1
    prev_idx = -2
    row = df.iloc[current_idx]
    row_prev = df.iloc[prev_idx]

    vol_sma20 = df["volume"].rolling(20).mean().iloc[current_idx]
    if vol_sma20 == 0:
        vol_sma20 = 1.0

    features = {
        "rsi_14": row["rsi_14"],
        "rsi_6": row["rsi_6"],
        "macd_hist": row["macd_dif_normal_12_26_9"] - row["macd_dea_normal_12_26_9"],
        "atr_pct": (row["atr_14"] / row["close"]) * 100 if row["close"] else 0,
        "vol_ratio": row["volume"] / vol_sma20,
        "bb_width": (row["boll_upper_20"] - row["boll_lower_20"]) / row["boll_lower_20"] if row["boll_lower_20"] else 0,
        "bb_pos": (row["close"] - row["boll_lower_20"]) / (row["boll_upper_20"] - row["boll_lower_20"])
        if (row["boll_upper_20"] - row["boll_lower_20"]) != 0
        else 0,
        "dist_ema200": (row["close"] / row["ema_200"]) - 1 if row["ema_200"] else 0,
        "dist_ema9_21": (row["ema_9"] / row["ema_21"]) - 1 if row["ema_21"] else 0,
        "dist_kama9": (row["close"] / row["kama_9"]) - 1 if row["kama_9"] else 0,
        "dist_kama21": (row["close"] / row["kama_21"]) - 1 if row["kama_21"] else 0,
        "dist_kama55": (row["close"] / row["kama_55"]) - 1 if row["kama_55"] else 0,
        "dist_kama9_21": (row["kama_9"] / row["kama_21"]) - 1 if row["kama_21"] else 0,
        "dist_donch_up": (row["close"] / row["donchian_upper_20"]) - 1 if row["donchian_upper_20"] else 0,
        "dist_donch_low": (row["close"] / row["donchian_lower_20"]) - 1 if row["donchian_lower_20"] else 0,
        "rsi_ratio": row["rsi_6"] / row["rsi_14"] if row["rsi_14"] else 0,
        "slope_norm": (row["trendline_slope"] / row["close"]) * 1000 if row["close"] else 0,
        "dist_supp": (row["close"] - row["support_price"]) / row["close"] if row["close"] else 0,
        "dist_res": (row["resistance_price"] - row["close"]) / row["close"] if row["close"] else 0,
        "macd_cross_bearish": int(
            row_prev["macd_dif_normal_12_26_9"] >= row_prev["macd_dea_normal_12_26_9"]
            and row["macd_dif_normal_12_26_9"] < row["macd_dea_normal_12_26_9"]
        ),
        "ema9_21_cross_bearish": int(row_prev["ema_9"] >= row_prev["ema_21"] and row["ema_9"] < row["ema_21"]),
        "kama9_21_cross_bearish": int(row_prev["kama_9"] >= row_prev["kama_21"] and row["kama_9"] < row["kama_21"]),
        "bollinger_lower_break": int(row["close"] < row["boll_lower_20"]),
        "close_below_ema50": int(row["close"] < row["ema_50"]),
        "obv_ratio": row["obv"] / df["obv"].rolling(20).mean().iloc[current_idx]
        if df["obv"].rolling(20).mean().iloc[current_idx] != 0
        else 0,
        "close_to_vwap_pct": (row["close"] / row["vwap_20"]) - 1 if row["vwap_20"] else 0,
        "obv_val": row["obv"],
        "volume_spike": int(row["volume"] > vol_sma20 * 2),
        "volume_trend_up": int(df["volume"].rolling(5).mean().iloc[current_idx] > vol_sma20),
    }
    # Force native Python types (like core.rub_features.build_rub_features):
    # the bot sees the same float64 frame in pd.DataFrame([features]), but the
    # replay serialises the values as JSONL — np.float64 would otherwise need
    # a default=str fallback there, which would mangle the features into strings.
    return {k: (int(v) if k in _BINARY_FLAGS else float(v)) for k, v in features.items()}


#: The 7 binary 0/1 flags (allowed to be constant in small samples).
_BINARY_FLAGS: frozenset[str] = frozenset(
    {
        "macd_cross_bearish",
        "ema9_21_cross_bearish",
        "kama9_21_cross_bearish",
        "bollinger_lower_break",
        "close_below_ema50",
        "volume_spike",
        "volume_trend_up",
    }
)


def assert_features_alive(feat_df: pd.DataFrame) -> None:
    """Startup/test guard (pattern core.mis_features/core.atb2_features):

    * every ATS_FEATURES column must exist (P0.12 contract),
    * no CONTINUOUS feature column may be constant across the dataset
      (a dead/always-0 feature is a leakage/wiring bug like ABR1).
      The 6 binary flags (0/1) are deliberately excluded — they are allowed
      to be constant in a small sample.
    """
    missing = [c for c in ATS_FEATURES if c not in feat_df.columns]
    if missing:
        raise ValueError(f"ATS feature contract violated: missing columns {missing}")
    dead = []
    for c in ATS_FEATURES:
        if c in _BINARY_FLAGS:
            continue
        col = pd.to_numeric(feat_df[c], errors="coerce")
        if col.nunique(dropna=True) <= 1:
            dead.append(c)
    if dead:
        raise ValueError(f"ATS features constant (check leakage/wiring): {dead}")

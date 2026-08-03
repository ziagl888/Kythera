# core/sra_features.py — shared feature builder of the SRA2 generation.
#
# X-R1 rule: ONE builder for trainer and serving. Until T-2026-CU-9050-042 it
# only lived in tools/retrain_sra2.py, and 9_ai_sr_bot built its own vector.
# The two were NOT equivalent, even though they share column names:
#
#   * ``pct_ema9`` & co. are (close-ema9)/CLOSE*100 in the bot, but
#     (close-ema9)/EMA9*100 in the trainer — same column, different scale.
#   * ``macd_dif_pct``/``macd_dea_pct``/``atr_pct`` are not built by the bot at
#     all; it instead carries the raw columns (macd_dif_fast_9_21_9, atr_14)
#     that SRA2 deliberately dropped ("22 scale-free features").
#
# An SRA2 rollout against the bot's own vector would therefore have fed the
# model foreign numbers under familiar names. This module is the single
# source of the SRA2 feature semantics; the old bot vector remains alongside
# it only as the LEGACY contract of the SRA1 model deployed today.

from __future__ import annotations

import numpy as np
import pandas as pd

# The feature contract of the SRA2 generation (order = training order).
SRA2_FEATURES = [
    "rsi_9",
    "rsi_14",
    "rsi_24",
    "tsi_fast_12_7_7",
    "tsi_fast_12_7_7_signal",
    "macd_dif_pct",
    "macd_dea_pct",
    "atr_pct",
    "r_squared",
    "trend_direction_num",
    "pct_ema9",
    "pct_ema21",
    "pct_wma9",
    "pct_kama9",
    "pct_support",
    "pct_resist",
    "pct_boll_mid",
    "ema9_ema21_pct",
    "kama9_kama21_pct",
    "support_atr",
    "resist_atr",
    "boll_width_atr",
]

TREND_MAP = {"UP": 1, "DOWN": -1, "FLAT": 0, "SIDEWAYS": 0}


def pct(a, b) -> float:
    """(a - b) / b * 100. NaN stays NaN — XGBoost can handle it natively,
    a fake 0 would be a fabricated observation."""
    try:
        a, b = float(a), float(b)
        if b == 0 or pd.isna(a) or pd.isna(b):
            return np.nan
        return (a - b) / b * 100.0
    except (TypeError, ValueError):
        return np.nan


def build_sra2_features(ind: dict) -> dict:
    """Scale-free SRA2 features from a 1h indicator row.

    Returns exactly the keys from ``SRA2_FEATURES``. NaN stays NaN
    (XGBoost-native, live-consistent).
    """
    close = ind.get("close")
    atr = ind.get("atr_14")
    f = {
        "rsi_9": ind.get("rsi_9"),
        "rsi_14": ind.get("rsi_14"),
        "rsi_24": ind.get("rsi_24"),
        "tsi_fast_12_7_7": ind.get("tsi_fast_12_7_7"),
        "tsi_fast_12_7_7_signal": ind.get("tsi_fast_12_7_7_signal"),
        "macd_dif_pct": pct(ind.get("macd_dif_fast_9_21_9", 0) + (close or 0), close) if close else np.nan,
        "macd_dea_pct": pct(ind.get("macd_dea_fast_9_21_9", 0) + (close or 0), close) if close else np.nan,
        "atr_pct": pct((atr or 0) + (close or 0), close) if close and atr is not None else np.nan,
        "r_squared": ind.get("r_squared"),
        "trend_direction_num": TREND_MAP.get(str(ind.get("trend_direction", "")).upper(), 0),
        "pct_ema9": pct(close, ind.get("ema_9")),
        "pct_ema21": pct(close, ind.get("ema_21")),
        "pct_wma9": pct(close, ind.get("wma_9")),
        "pct_kama9": pct(close, ind.get("kama_9")),
        "pct_support": pct(close, ind.get("support_price")),
        "pct_resist": pct(close, ind.get("resistance_price")),
        "pct_boll_mid": pct(close, ind.get("boll_mid_20")),
        "ema9_ema21_pct": pct(ind.get("ema_9"), ind.get("ema_21")),
        "kama9_kama21_pct": pct(ind.get("kama_9"), ind.get("kama_21")),
    }
    # ATR-normalised distances (like bot P1.20: missing ATR → NaN, no fake 0)
    try:
        atr_f = float(atr) if atr is not None else np.nan
        close_f = float(close) if close is not None else np.nan
        if pd.notna(atr_f) and atr_f > 0 and pd.notna(close_f):
            sup, res = ind.get("support_price"), ind.get("resistance_price")
            bu, bl = ind.get("boll_upper_20"), ind.get("boll_lower_20")
            f["support_atr"] = (close_f - float(sup)) / atr_f if sup is not None else np.nan
            f["resist_atr"] = (float(res) - close_f) / atr_f if res is not None else np.nan
            f["boll_width_atr"] = (float(bu) - float(bl)) / atr_f if bu is not None and bl is not None else np.nan
        else:
            f["support_atr"] = f["resist_atr"] = f["boll_width_atr"] = np.nan
    except (TypeError, ValueError):
        f["support_atr"] = f["resist_atr"] = f["boll_width_atr"] = np.nan
    return f

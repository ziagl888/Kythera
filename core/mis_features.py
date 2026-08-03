# core/mis_features.py — shared MIS1 feature builder (bot + trainer + simulator).
#
# Background (Report 13 / Dossier MIS1): the legacy trainer
# (legacy_trainers/X5-analyze_indicators_v8.py) and 11_ai_mis_bot.py each kept
# their own COPY of the feature builder. Its `line_cols` loop matched by
# PREFIX over the already-mutated DataFrame columns and in doing so caught
# four derived columns (boll_*_dist_atr, ema_200_dist_atr, ema_9_cross_above_21)
# → their "dist_pct" is (close − small number)/small number ≈ coin price scale →
# ticker/price-class leakage, which the trees actually split on (13-P1).
#
# This module is the ONE source for both sides (X-R fix "trainer imports
# the bot's feature builder") and fixes:
#   * line_cols is now an EXPLICIT catalogue of the 38 raw indicator lines —
#     prefix accidents are structurally impossible.
#   * all remaining price-scale features normalised: atr_14 → atr_14_pct,
#     macd_hist → macd_hist_pct, macd_dif delta → macd_dif_pct_delta_1.
#     Result: EVERY feature is scale-free (%, ratio, oscillator, flag).
#   * imputation identical bot == trainer (P2.34 residual risk): first
#     replace(±inf → NaN), then fillna(0).
#   * cross flags per-symbol (the legacy trainer shifted rsi_14_cross_above_30 /
#     ema_9_cross_above_21 UNGROUPED across symbol boundaries) — this module
#     fundamentally operates on ONE symbol; multi-coin frames run through
#     add_advanced_features_multi (groupby-apply) and are therefore exactly
#     congruent with the bot path.

from __future__ import annotations

import numpy as np
import pandas as pd

# ── Raw columns from the DB (1h candles + indicator join) ────────────────────
# The catalogue is EXPLICIT — new indicator columns in the DB can never again
# slip unnoticed into the dist_pct loop.
EMA_PERIODS = [7, 9, 12, 21, 26, 34, 50, 55, 89, 99, 200]
WMA_PERIODS = [7, 9, 12, 21, 26, 34, 50, 55, 89, 99, 200]
KAMA_PERIODS = [7, 9, 12, 21, 26, 34, 50, 55, 89, 99]

RAW_LINE_COLS = (
    [f"ema_{p}" for p in EMA_PERIODS]
    + [f"wma_{p}" for p in WMA_PERIODS]
    + [f"kama_{p}" for p in KAMA_PERIODS]
    + ["boll_upper_20", "boll_mid_20", "boll_lower_20"]
    + ["donchian_upper_20", "donchian_mid_20", "donchian_lower_20"]
)  # 38 lines in price scale → one *_dist_pct feature each

RSI_COLS = ["rsi_6", "rsi_9", "rsi_12", "rsi_14", "rsi_24"]

# Input columns the builder strictly requires (after the SQL aliasing
# tsi_fast_12_7_7→tsi_fast, macd_*_normal_12_26_9→macd_dif/macd_dea).
REQUIRED_INPUT_COLS = ["close", "volume"] + RSI_COLS + RAW_LINE_COLS + ["tsi_fast", "macd_dif", "macd_dea", "atr_14"]

# Shared indicator columns for bot and simulator — ONE source (hard rule 7:
# trainer == serving). Raw DB names (no more SQL aliasing): read_candles_with_
# indicators supplies them, then MIS_RENAME_MAP reproduces the three aliases that
# add_advanced_features expects in REQUIRED_INPUT_COLS. R1: replaces the old
# MIS_SQL_INDICATOR_SELECT (i.-prefixed JOIN select list), so that neither
# 11_ai_mis nor tools/walkforward_sim reads the per-coin tables directly (C-gate phase 5).
MIS_INDICATOR_COLUMNS = (
    RSI_COLS + RAW_LINE_COLS + ["tsi_fast_12_7_7", "macd_dif_normal_12_26_9", "macd_dea_normal_12_26_9", "atr_14"]
)
MIS_RENAME_MAP = {
    "tsi_fast_12_7_7": "tsi_fast",
    "macd_dif_normal_12_26_9": "macd_dif",
    "macd_dea_normal_12_26_9": "macd_dea",
}

# ── Feature catalogue (explicit, ordered — artifact meta stores it too) ──────
DELTA_FEATURES = [f"{c}_delta_1" for c in RSI_COLS] + [
    "tsi_fast_delta_1",
    "macd_dif_pct_delta_1",
    "macd_hist_pct_delta_1",
]

BINARY_FLAG_FEATURES = [
    "above_ema_200",
    "rsi_14_above_50",
    "rsi_14_cross_above_30",
    "ema_9_cross_above_21",
]

FEATURE_COLS = (
    [f"{c}_dist_pct" for c in RAW_LINE_COLS]  # 38 — % distance price↔line
    + DELTA_FEATURES  # 8
    + ["volume_ratio_prev", "volume_ratio_sma20"]  # 2
    + RSI_COLS
    + ["tsi_fast", "macd_hist_pct"]  # 7
    + BINARY_FLAG_FEATURES  # 4
    + ["boll_upper_dist_atr", "boll_lower_dist_atr", "ema_200_dist_atr", "atr_14_pct"]  # 4
)  # = 63, all scale-free

# The 8 legacy columns needed ONLY for comparisons with the old 67-feature pkls
# (4 accident features + 4 unnormalised predecessors).
LEGACY_ONLY_COLS = [
    "boll_upper_dist_atr_dist_pct",
    "boll_lower_dist_atr_dist_pct",
    "ema_200_dist_atr_dist_pct",
    "ema_9_cross_above_21_dist_pct",
    "macd_dif_delta_1",
    "macd_hist",
    "macd_hist_delta_1",
    "atr_14",
]


def pct_distance(price_series: pd.Series, indicator_series: pd.Series) -> pd.Series:
    denominator = indicator_series.replace(0, np.nan)
    result = (price_series - indicator_series) / denominator * 100
    # P2.34: inf to NaN first — fillna(0) does not catch inf.
    return result.replace([np.inf, -np.inf], np.nan).fillna(0)


def add_advanced_features(df: pd.DataFrame, include_legacy: bool = False) -> pd.DataFrame:
    """Feature pipeline for ONE symbol (sorted chronologically ascending).

    `include_legacy=True` additionally generates the 8 LEGACY_ONLY_COLS, so
    retrain comparisons can feed the old 67-feature models exactly —
    the live bot leaves this off.

    Missing required columns are a hard error (no silent fillna across
    whole columns — the P0.12 failure mode).
    """
    missing = [c for c in REQUIRED_INPUT_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"MIS1 feature builder: required columns missing: {missing}")

    df = df.copy()
    if "open_time" in df.columns:
        df = df.sort_values("open_time").reset_index(drop=True)

    for c in REQUIRED_INPUT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    close = df["close"]

    # Volume ratios
    df["volume_ratio_prev"] = df["volume"] / df["volume"].shift(1)
    df["volume_sma20"] = df["volume"].rolling(20, min_periods=1).mean()
    df["volume_ratio_sma20"] = df["volume"] / df["volume_sma20"]

    # Scale-free MACD base (price scale ÷ close)
    df["macd_dif_pct"] = df["macd_dif"] / close.replace(0, np.nan) * 100
    df["macd_hist_pct"] = (df["macd_dif"] - df["macd_dea"]) / close.replace(0, np.nan) * 100

    # Deltas
    for col in RSI_COLS + ["tsi_fast"]:
        df[f"{col}_delta_1"] = df[col].diff(1)
    df["macd_dif_pct_delta_1"] = df["macd_dif_pct"].diff(1)
    df["macd_hist_pct_delta_1"] = df["macd_hist_pct"].diff(1)

    # Binary / cross flags
    df["above_ema_200"] = (close > df["ema_200"]).astype(int)
    df["rsi_14_above_50"] = (df["rsi_14"] > 50).astype(int)
    df["rsi_14_cross_above_30"] = ((df["rsi_14"].shift(1) < 30) & (df["rsi_14"] >= 30)).astype(int)
    df["ema_9_cross_above_21"] = ((df["ema_9"].shift(1) < df["ema_21"].shift(1)) & (df["ema_9"] > df["ema_21"])).astype(
        int
    )

    # ATR-normalised distances + scale-free ATR
    eps = 1e-8
    df["boll_upper_dist_atr"] = (close - df["boll_upper_20"]) / (df["atr_14"] + eps)
    df["boll_lower_dist_atr"] = (close - df["boll_lower_20"]) / (df["atr_14"] + eps)
    df["ema_200_dist_atr"] = (close - df["ema_200"]) / (df["atr_14"] + eps)
    df["atr_14_pct"] = df["atr_14"] / close.replace(0, np.nan) * 100

    # % distances ONLY over the explicit line catalogue (the leakage fix).
    # concat instead of 38 individual inserts (pandas fragmentation).
    dist = {f"{col}_dist_pct": pct_distance(close, df[col]) for col in RAW_LINE_COLS}

    if include_legacy:
        # Exact reproduction of the legacy builder's accident features — the
        # loop there ran AFTER the derived columns were generated.
        for col in ["boll_upper_dist_atr", "boll_lower_dist_atr", "ema_200_dist_atr", "ema_9_cross_above_21"]:
            dist[f"{col}_dist_pct"] = pct_distance(close, df[col])
        dist["macd_hist"] = df["macd_dif"] - df["macd_dea"]
        dist["macd_dif_delta_1"] = df["macd_dif"].diff(1)
        dist["macd_hist_delta_1"] = dist["macd_hist"].diff(1)

    df = pd.concat([df, pd.DataFrame(dist, index=df.index)], axis=1)

    # Imputation — MUST be identical in bot, trainer and simulator (P2.34).
    return df.replace([np.inf, -np.inf], np.nan).fillna(0)


def add_advanced_features_multi(df: pd.DataFrame, include_legacy: bool = False) -> pd.DataFrame:
    """Multi-coin frame (column `symbol`) — applies the builder per symbol,
    so deltas/crosses/rolling never compute across symbol boundaries."""
    if "symbol" not in df.columns:
        raise ValueError("add_advanced_features_multi expects a 'symbol' column")
    parts = [add_advanced_features(g, include_legacy=include_legacy) for _, g in df.groupby("symbol", sort=False)]
    return pd.concat(parts, ignore_index=True)


def assert_features_alive(df_features: pd.DataFrame, context: str = "") -> None:
    """Startup/training assertion "no feature constant" (P0.12 pattern).

    Continuous features must vary across the sample; constant binary flags
    are only worth a warning (legitimate over short windows) and are
    deliberately not checked here — the caller logs them if needed.
    """
    missing = [c for c in FEATURE_COLS if c not in df_features.columns]
    if missing:
        raise ValueError(f"MIS1 feature assertion{context}: columns missing: {missing}")
    continuous = [c for c in FEATURE_COLS if c not in BINARY_FLAG_FEATURES]
    constant = [c for c in continuous if df_features[c].nunique(dropna=False) <= 1]
    if constant:
        raise ValueError(f"MIS1 feature assertion{context}: constant features: {constant}")

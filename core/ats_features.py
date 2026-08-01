# core/ats_features.py
"""Geteilte ATS/TSI-Detektions- und Feature-Logik — EINE Quelle für Bot 12 und
den Walkforward-Adapter (X-R1-Regel: kein Train/Serve-Skew, harte Regel 7).

Herkunft: die inline-Logik aus 12_ai_ats_bot.check_tsi_crossovers
(TSI-Crossover-Vorfilter + 29-Feature-Vertrag + Live-OBV/VWAP), beim Bau des
ATS2-Retrain-Adapters (T-2026-CU-9050-121) hierher gehoben. Der Bot ruft
dieselben Funktionen wie der Replay — der Parity-Test backtest/test_ats_features
beweist build_ats_features == die frühere Serving-Konstruktion.

Kein DB-Zugriff: build_ats_features arbeitet auf einem fertigen, chronologisch
aufsteigenden 1h-Fenster (die neueste GESCHLOSSENE Kerze ist die letzte Zeile).
Der Bot lädt live 500 Kerzen (read_candles_with_indicators, include_forming=
False) und normalisiert OBV auf den Fensterstart — der Replay reicht exakt
dasselbe 500-Kerzen-Fenster durch, damit die OBV-Baseline identisch ist.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: Candle-Projektion, die Bot 12 liest (KEIN 'open' — die ATS-Features brauchen
#: es nicht). Single Source für Bot- und Replay-Read.
ATS_CANDLE_COLUMNS: tuple[str, ...] = ("open_time", "high", "low", "close", "volume")

#: Indikator-Spalten, die Bot 12 aus der *_1h_indicators-Tabelle joint. Die
#: beiden TSI-Spalten dienen der Crossover-Detektion, der Rest den Features.
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

#: Feature-Vertrag des ATS-ML in exakt der Reihenfolge, die Bot 12 erzwingt
#: (X_live[TSI_FEATURES]). Reihenfolge ist hier Vertrag — nicht sortieren.
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
    """TSI-Crossover-Vorfilter von Bot 12. Returns 'LONG' | 'SHORT' | None.

    LONG  = TSI kreuzt die Signallinie von unten nach oben,
    SHORT = von oben nach unten. Geprüft auf der jüngsten GESCHLOSSENEN Kerze
    (curr) gegen die vorletzte (prev).
    """
    long_cross = (tsi_prev <= sig_prev) and (tsi_curr > sig_curr)
    short_cross = (tsi_prev >= sig_prev) and (tsi_curr < sig_curr)
    if long_cross:
        return "LONG"
    if short_cross:
        return "SHORT"
    return None


def build_ats_features(df: pd.DataFrame) -> dict[str, float]:
    """Der 29-Feature-Vertrag (ATS_FEATURES) als dict — Bot-12-Parität.

    `df`: chronologisch aufsteigendes 1h-Fenster (idealerweise 500 Kerzen wie
    live) mit ATS_CANDLE_COLUMNS + ATS_INDICATOR_COLUMNS, bereits numerisch
    (der Aufrufer coerct + fillna(0) wie Bot 12). Die neueste geschlossene
    Kerze ist df.iloc[-1], die vorletzte df.iloc[-2].

    OBV/VWAP werden auf einer internen Kopie gerechnet (kein Seiteneffekt auf
    das übergebene Fenster); die OBV-Baseline ist der Fensterstart (df.iloc[0]),
    exakt wie im Bot — deshalb muss der Replay dasselbe 500-Kerzen-Fenster
    durchreichen, das der Live-Bot sähe.
    """
    df = df.copy()

    # --- Live Feature Engineering (OBV, VWAP) wie Bot 12 ---
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
    # Native Python-Typen erzwingen (wie core.rub_features.build_rub_features):
    # der Bot sieht in pd.DataFrame([features]) denselben float64-Frame, der
    # Replay serialisiert die Werte aber als JSONL — np.float64 bräuchte dort
    # sonst einen default=str-Fallback, der die Features zu Strings verstümmelte.
    return {k: (int(v) if k in _BINARY_FLAGS else float(v)) for k, v in features.items()}


#: Die 7 binären 0/1-Flags (dürfen in kleinen Stichproben konstant sein).
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
    """Startup-/Test-Guard (Muster core.mis_features/core.atb2_features):

    * jede ATS_FEATURES-Spalte muss existieren (P0.12-Vertrag),
    * keine KONTINUIERLICHE Feature-Spalte darf über den Datensatz konstant sein
      (ein totes/immer-0-Feature ist ein Leakage-/Verkabelungsbug wie ABR1).
      Die 6 binären Flags (0/1) sind bewusst ausgenommen — sie dürfen in einer
      kleinen Stichprobe konstant sein.
    """
    missing = [c for c in ATS_FEATURES if c not in feat_df.columns]
    if missing:
        raise ValueError(f"ATS-Feature-Vertrag verletzt: fehlende Spalten {missing}")
    dead = []
    for c in ATS_FEATURES:
        if c in _BINARY_FLAGS:
            continue
        col = pd.to_numeric(feat_df[c], errors="coerce")
        if col.nunique(dropna=True) <= 1:
            dead.append(c)
    if dead:
        raise ValueError(f"ATS-Features konstant (Leakage/Verkabelung prüfen): {dead}")

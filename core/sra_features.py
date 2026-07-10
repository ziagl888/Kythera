# core/sra_features.py — geteilter Feature-Builder der SRA2-Generation.
#
# X-R1-Regel: EIN Builder für Trainer und Serving. Bis T-2026-CU-9050-042 lebte
# er nur in tools/retrain_sra2.py, und 9_ai_sr_bot baute seinen eigenen Vektor.
# Die beiden waren NICHT äquivalent, obwohl sie Spaltennamen teilen:
#
#   * ``pct_ema9`` & Co. sind im Bot (close-ema9)/CLOSE*100, im Trainer
#     (close-ema9)/EMA9*100 — dieselbe Spalte, andere Grösse.
#   * ``macd_dif_pct``/``macd_dea_pct``/``atr_pct`` baut der Bot gar nicht; er
#     führt stattdessen die Roh-Spalten (macd_dif_fast_9_21_9, atr_14), die
#     SRA2 bewusst rausgeworfen hat ("22 skalenfreie Features").
#
# Ein SRA2-Rollout gegen den Bot-eigenen Vektor hätte das Modell also mit
# fremden Zahlen unter vertrauten Namen befragt. Dieser Modul ist die einzige
# Quelle der SRA2-Feature-Semantik; der alte Bot-Vektor bleibt daneben nur als
# LEGACY-Vertrag des heute deployten SRA1-Modells bestehen.

from __future__ import annotations

import numpy as np
import pandas as pd

# Der Feature-Vertrag der SRA2-Generation (Reihenfolge = Trainings-Reihenfolge).
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
    """(a - b) / b * 100. NaN bleibt NaN — XGBoost kann damit nativ umgehen,
    ein 0-Fake wäre eine erfundene Beobachtung."""
    try:
        a, b = float(a), float(b)
        if b == 0 or pd.isna(a) or pd.isna(b):
            return np.nan
        return (a - b) / b * 100.0
    except (TypeError, ValueError):
        return np.nan


def build_sra2_features(ind: dict) -> dict:
    """Skalenfreie SRA2-Features aus einer 1h-Indikator-Zeile.

    Liefert exakt die Schlüssel aus ``SRA2_FEATURES``. NaN bleibt NaN
    (XGBoost-nativ, live-konsistent).
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
    # ATR-normalisierte Distanzen (wie Bot P1.20: fehlt ATR → NaN, kein 0-Fake)
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

# core/rub_features.py
"""Geteilte RUB-Detektions- und Feature-Logik — EINE Quelle für Bot 13 und den
Walkforward-Adapter (X-R1-Regel: kein Train/Serve-Skew).

Herkunft: inline-Logik aus 13_ai_rub_bot.check_rubberband_conditions
(Regression, Vorfilter, 9-Feature-Vertrag), beim Bau des RUB2-Adapters
(2026-07-06, MODEL_INTENT §8) hierher gehoben. Der Bot ruft dieselben
Funktionen wie der Replay.
"""
from __future__ import annotations

import numpy as np

#: Feature-Vertrag des RUB-ML (Spaltennamen wie vom Alt-Trainer erwartet).
RUB_FEATURES = [
    'dist_to_trend', 'rsi', 'atr_pct', 'dist_ema200', 'slope_trend',
    'MACD_Line', 'MACD_Signal', 'TSI_Line', 'TSI_Signal',
]

#: Vorfilter-Schwellen (Rubberband-Bedingungen, Bot 13).
DIST_TREND_MIN = 0.08     # ±8 % von der 90d-Regression
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
TSI_EXTREME = 15
DC_TOUCH_TOL = 0.01       # close <= dc_lower*1.01 bzw. >= dc_upper*0.99


def rub_trend(ts_seconds: np.ndarray, closes: np.ndarray, curr_close: float):
    """Lineare Regression über das (bis zu 95d-)Fenster wie im Bot.

    Returns (dist_to_trend_pct, slope_pct_per_day). ts_seconds = Unix-Sekunden
    der Kerzen (aufsteigend), closes = zugehörige Schlusskurse.
    """
    A = np.vstack([ts_seconds, np.ones(len(ts_seconds))]).T
    slope, intercept = np.linalg.lstsq(A, closes.astype(float), rcond=None)[0]
    trend_val_curr = slope * ts_seconds[-1] + intercept
    dist_to_trend_pct = (curr_close - trend_val_curr) / trend_val_curr if trend_val_curr != 0 else 0.0
    slope_pct_per_day = (slope * 86400) / curr_close if curr_close != 0 else 0.0
    return float(dist_to_trend_pct), float(slope_pct_per_day)


def rub_event_type(dist_to_trend_pct, rsi, tsi_line, curr_close, dc_lower, dc_upper):
    """Rubberband-Vorfilter. Returns 'REVERSION_UP' | 'REVERSION_DOWN' | None."""
    if (dist_to_trend_pct <= -DIST_TREND_MIN and rsi < RSI_OVERSOLD
            and tsi_line < -TSI_EXTREME and curr_close <= dc_lower * (1 + DC_TOUCH_TOL)):
        return "REVERSION_UP"
    if (dist_to_trend_pct >= DIST_TREND_MIN and rsi > RSI_OVERBOUGHT
            and tsi_line > TSI_EXTREME and curr_close >= dc_upper * (1 - DC_TOUCH_TOL)):
        return "REVERSION_DOWN"
    return None


def build_rub_features(dist_to_trend_pct, slope_pct_per_day, curr_close,
                       rsi, tsi_line, tsi_signal, macd_line, macd_signal,
                       atr_14, ema_200) -> dict:
    """Der 9-Feature-Vertrag (RUB_FEATURES) als dict."""
    atr_pct = (atr_14 / curr_close) if curr_close > 0 else 0.0
    dist_ema200 = (curr_close - ema_200) / ema_200 if ema_200 > 0 else 0.0
    return {
        'dist_to_trend': float(dist_to_trend_pct),
        'rsi': float(rsi),
        'atr_pct': float(atr_pct),
        'dist_ema200': float(dist_ema200),
        'slope_trend': float(slope_pct_per_day),
        'MACD_Line': float(macd_line),
        'MACD_Signal': float(macd_signal),
        'TSI_Line': float(tsi_line),
        'TSI_Signal': float(tsi_signal),
    }

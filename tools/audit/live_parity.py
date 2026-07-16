# -*- coding: utf-8 -*-
"""Read-only live parity test: replicate 11_ai_mis_bot.py feature build for a few
symbols, verify all 67 model features are produced, run predict_proba for all 8
models, and inspect suspicious derived features. NO writes to DB."""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
BASE = r"C:\Users\Michael\PycharmProjects\crypto_trading_bot_v2"
os.chdir(BASE)
sys.path.insert(0, BASE)

import warnings
warnings.filterwarnings("ignore")
import joblib
import pandas as pd
import numpy as np
from core.candles import read_candles_with_indicators
from core.database import get_db_connection
from core.mis_features import MIS_INDICATOR_COLUMNS, MIS_RENAME_MAP

# --- replicate bot code exactly (copied from 11_ai_mis_bot.py) ---
def pct_distance(price_series, indicator_series):
    denominator = indicator_series.replace(0, np.nan)
    result = (price_series - indicator_series) / denominator * 100
    return result.fillna(0)

def add_advanced_features(df):
    if 'open_time' in df.columns:
        df = df.sort_values('open_time').reset_index(drop=True)
    df['volume_ratio_prev'] = df['volume'] / df['volume'].shift(1)
    df['volume_sma20'] = df['volume'].rolling(20, min_periods=1).mean()
    df['volume_ratio_sma20'] = df['volume'] / df['volume_sma20']
    delta_cols = ['rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24', 'tsi_fast', 'macd_dif']
    for col in delta_cols:
        if col in df.columns:
            df[f'{col}_delta_1'] = df[col].diff(1)
    if 'macd_dif' in df.columns and 'macd_dea' in df.columns:
        df['macd_hist'] = df['macd_dif'] - df['macd_dea']
        df['macd_hist_delta_1'] = df['macd_hist'].diff(1)
    else:
        df['macd_hist'] = 0.0
        df['macd_hist_delta_1'] = 0.0
    df['above_ema_200'] = (df['close'] > df.get('ema_200', df['close'])).astype(int)
    if 'rsi_14' in df.columns:
        df['rsi_14_above_50'] = (df['rsi_14'] > 50).astype(int)
        df['rsi_14_cross_above_30'] = ((df['rsi_14'].shift(1) < 30) & (df['rsi_14'] >= 30)).astype(int)
    else:
        df['rsi_14_above_50'] = 0
        df['rsi_14_cross_above_30'] = 0
    if 'ema_9' in df.columns and 'ema_21' in df.columns:
        df['ema_9_cross_above_21'] = (
                    (df['ema_9'].shift(1) < df['ema_21'].shift(1)) & (df['ema_9'] > df['ema_21'])).astype(int)
    else:
        df['ema_9_cross_above_21'] = 0
    eps = 1e-8
    if all(c in df.columns for c in ['close', 'atr_14']):
        df['boll_upper_dist_atr'] = (df['close'] - df.get('boll_upper_20', df['close'])) / (df['atr_14'] + eps)
        df['boll_lower_dist_atr'] = (df['close'] - df.get('boll_lower_20', df['close'])) / (df['atr_14'] + eps)
        df['ema_200_dist_atr'] = (df['close'] - df.get('ema_200', df['close'])) / (df['atr_14'] + eps)
    else:
        df['boll_upper_dist_atr'] = 0.0
        df['boll_lower_dist_atr'] = 0.0
        df['ema_200_dist_atr'] = 0.0
    price = df['close']
    line_cols = [c for c in df.columns if
                 c.startswith(('ema_', 'wma_', 'kama_', 'boll_', 'donchian_')) and not c.endswith('_dist_pct')]
    for col in line_cols:
        df[f'{col}_dist_pct'] = pct_distance(price, df[col])
    return df.fillna(0)

KEYS = ["8h_pump", "8h_dump", "24h_pump", "24h_dump", "72h_pump", "72h_dump", "168h_pump", "168h_dump"]
models = {k: joblib.load(f"pump_model_{k}_final.pkl") for k in KEYS}
feature_cols = models["8h_pump"].feature_names_in_

conn = get_db_connection()
# NOTE: no autocommit change needed, read-only SELECTs only.

symbols = ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
SUS = ["ema_9_cross_above_21_dist_pct", "boll_upper_dist_atr_dist_pct",
       "boll_lower_dist_atr_dist_pct", "ema_200_dist_atr_dist_pct",
       "above_ema_200_dist_pct"]

for symbol in symbols:
    # R1: h⋈i JOIN → core.candles, indicator side = the shared MIS_INDICATOR_COLUMNS
    # (+ MIS_RENAME_MAP aliases), same source as 11_ai_mis/walkforward. The API returns
    # ASC (newest 100), so the old `ORDER BY DESC LIMIT 100` + iloc[::-1] reverse is
    # gone. include_forming=True keeps the forming candle this parity check inspects.
    df = read_candles_with_indicators(
        conn,
        symbol,
        "1h",
        limit=100,
        include_forming=True,
        candle_columns=("open_time", "close", "volume"),
        indicator_columns=MIS_INDICATOR_COLUMNS,
    ).rename(columns=MIS_RENAME_MAP)
    columns = list(df.columns)

    # check indicator NaNs on last (forming) candle BEFORE fillna
    raw_last = df.iloc[-1]
    nan_ind = [c for c in columns if c not in ("open_time",) and pd.isna(raw_last[c])]
    prev_last = df.iloc[-2]
    nan_prev = [c for c in columns if c not in ("open_time",) and pd.isna(prev_last[c])]

    dff = add_advanced_features(df)
    built_cols = set(dff.columns)
    missing = [c for c in feature_cols if c not in built_cols]
    extra_generated = sorted(built_cols - set(columns) - set(feature_cols))

    cur_row = dff.iloc[-1:]
    X = cur_row[feature_cols].values.astype(float)

    print(f"\n===== {symbol} =====")
    print(f"last open_time (forming?): {df['open_time'].iloc[-1]}")
    print(f"NaN indicator cols on last row (before fillna->0): {len(nan_ind)} {nan_ind[:15]}")
    print(f"NaN indicator cols on second-to-last row: {len(nan_prev)} {nan_prev[:15]}")
    print(f"missing model features: {missing}")
    print(f"features built but NOT used by model (non-SQL): {extra_generated}")
    print("suspicious feature values (last row):")
    for c in SUS:
        if c in dff.columns:
            print(f"    {c:35s} = {float(cur_row[c].iloc[0]):.6g}")
    print(f"close={float(cur_row['close'].iloc[0])}")
    print("predict_proba per model:")
    for k in KEYS:
        p = models[k].predict_proba(X)[0, 1]
        print(f"    {k:10s} prob={p:.4f}")

# check the last-candle timestamps vs now
import datetime
print("\nnow utc:", datetime.datetime.now(datetime.timezone.utc))
conn.close()
print("DONE")

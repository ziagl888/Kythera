import pandas as pd
import pandas_ta as ta
import numpy as np
import json
import os
from sqlalchemy import create_engine, text
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# ========================= CONFIG =========================
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "dbfiller",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "cryptodata"
}

LOOKBACK_DAYS = 365
TREND_WINDOW_HOURS = 90 * 24
FUTURE_WINDOW_HOURS = 3 * 24
TARGET_MOVE_PCT = 0.10        # Ziel: 10% Bounce in Richtung Trend
COINS_FILE = 'coins.json'
OUTPUT_FILE = 'reversion_ml_training_data.csv'

# --- STRATEGIE PARAMETER ---
MIN_TREND_DISTANCE_PCT = 0.08  # Mindestens 8% weg von der Trendlinie
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
# =========================================================

def get_db_engine():
    url = f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
    return create_engine(url)

def load_coins():
    if not os.path.exists(COINS_FILE):
        return ["ETHUSDT", "BTCUSDT"] 
    with open(COINS_FILE, 'r') as f:
        return json.load(f)

def calculate_trend_vectorized(prices, timestamps):
    x = timestamps
    y = prices
    A = np.vstack([x, np.ones(len(x))]).T
    m, c = np.linalg.lstsq(A, y, rcond=None)[0]
    return m, c

def analyze_coin(engine, symbol):
    logging.info(f"--> Analysiere {symbol} für Mean Reversion...")
    
    total_days_load = LOOKBACK_DAYS + 90 + (200/24) + 5 
    query = text(f"""
        SELECT open_time, open, high, low, close, volume 
        FROM "{symbol}_1h"
        WHERE open_time > NOW() - INTERVAL '{total_days_load} days'
        ORDER BY open_time ASC
    """)
    
    try:
        df = pd.read_sql(query, engine)
    except Exception as e:
        logging.error(f"Fehler beim Laden von {symbol}: {e}")
        return []

    if df.empty: return []

    df['open_time'] = pd.to_datetime(df['open_time'], utc=True)
    df['ts'] = df['open_time'].apply(lambda x: x.timestamp()) 
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col])

    df['vol_avg_20'] = df['volume'].rolling(window=20).mean()

    # --- INDIKATOREN (genau wie bei dir, plus Donchian Logik) ---
    df['RSI'] = ta.rsi(df['close'], length=14)
    df['EMA_9'] = ta.ema(df['close'], length=9)
    df['EMA_21'] = ta.ema(df['close'], length=21)
    df['EMA_200'] = ta.ema(df['close'], length=200)
    
    df['dist_close_ema9_pct'] = (df['close'] - df['EMA_9']) / df['EMA_9']
    
    macd = ta.macd(df['close'], fast=9, slow=21, signal=9)
    df['MACD_Line'] = macd['MACD_9_21_9']
    df['MACD_Signal'] = macd['MACDs_9_21_9']
    
    tsi = ta.tsi(df['close'], fast=12, slow=7, signal=7)
    df['TSI_Line'] = tsi['TSI_7_12_7']
    df['TSI_Signal'] = tsi['TSIs_7_12_7']
    
    # Donchian Channels
    donchian = ta.donchian(df['high'], df['low'], length=20)
    dc_lower_col = next((col for col in donchian.columns if col.startswith('DCL_')), None)
    dc_upper_col = next((col for col in donchian.columns if col.startswith('DCU_')), None)

    if dc_lower_col and dc_upper_col:
        df['DC_Lower'] = donchian[dc_lower_col]
        df['DC_Upper'] = donchian[dc_upper_col]
    else:
        df['DC_Lower'], df['DC_Upper'] = np.nan, np.nan

    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    df['ATR_PCT'] = df['ATR'] / df['close']
    
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)

    results = []
    start_idx = TREND_WINDOW_HOURS 
    end_idx = len(df) - FUTURE_WINDOW_HOURS
    
    if start_idx >= end_idx: return []

    ts_values = df['ts'].values
    close_values = df['close'].values
    high_values = df['high'].values
    low_values = df['low'].values
    
    for i in range(start_idx, end_idx):
        slice_start = i - TREND_WINDOW_HOURS
        if slice_start < 0: continue
            
        subset_ts = ts_values[slice_start:i]
        subset_close = close_values[slice_start:i]
        if len(subset_ts) < 2: continue
        
        slope, intercept = calculate_trend_vectorized(subset_close, subset_ts)
        
        current_ts = ts_values[i]
        curr_close = close_values[i]
        trend_val_curr = slope * current_ts + intercept
        
        # 1. Distanz zur Trendlinie berechnen
        dist_to_trend_pct = (curr_close - trend_val_curr) / trend_val_curr
        
        row = df.iloc[i]
        event_type = None
        
        # --- REVERSION UP LOGIK (Preis tief unten, muss rauf) ---
        if dist_to_trend_pct <= -MIN_TREND_DISTANCE_PCT:
            # RSI im Keller, TSI extrem negativ, Preis kratzt am Donchian Lower
            if row['RSI'] < RSI_OVERSOLD and row['TSI_Line'] < -15 and curr_close <= row['DC_Lower'] * 1.01:
                event_type = "REVERSION_UP"
                
        # --- REVERSION DOWN LOGIK (Preis weit oben, muss runter) ---
        elif dist_to_trend_pct >= MIN_TREND_DISTANCE_PCT:
            # RSI überhitzt, TSI extrem positiv, Preis kratzt am Donchian Upper
            if row['RSI'] > RSI_OVERBOUGHT and row['TSI_Line'] > 15 and curr_close >= row['DC_Upper'] * 0.99:
                event_type = "REVERSION_DOWN"

        if event_type:
            future_start = i + 1
            future_end = i + 1 + FUTURE_WINDOW_HOURS
            success = 0
            
            # Zielprüfung (10% Bounce in die Gegenrichtung)
            if event_type == "REVERSION_UP":
                max_price = np.max(high_values[future_start:future_end])
                if (max_price - curr_close) / curr_close >= TARGET_MOVE_PCT:
                    success = 1
            else: # REVERSION_DOWN
                min_price = np.min(low_values[future_start:future_end])
                if (curr_close - min_price) / curr_close >= TARGET_MOVE_PCT:
                    success = 1

            results.append({
                "symbol": symbol,
                "event_type": event_type,
                "dist_to_trend": dist_to_trend_pct,
                "rsi": row['RSI'],
                "atr_pct": row['ATR_PCT'],
                "dist_ema200": (curr_close - row['EMA_200']) / row['EMA_200'],
                "slope_trend": (slope * 86400) / curr_close if curr_close != 0 else 0,
                "MACD_Line": row['MACD_Line'],
                "MACD_Signal": row['MACD_Signal'],
                "TSI_Line": row['TSI_Line'],
                "TSI_Signal": row['TSI_Signal'],
                "label_success": success
            })

    return results

def main():
    start_time = time.time()
    engine = get_db_engine()
    coins = load_coins()
    all_results = []
    
    logging.info(f"Starte Reversion-Datensammlung für {len(coins)} Coins...")
    
    for coin in coins:
        res = analyze_coin(engine, coin)
        all_results.extend(res)
        
    df_final = pd.DataFrame(all_results)
    if df_final.empty:
        logging.warning("Keine Reversion-Events gefunden.")
        return

    df_final.to_csv(OUTPUT_FILE, index=False)
    
    logging.info(f"Fertig! {len(df_final)} Events in {OUTPUT_FILE} gespeichert.")
    logging.info(f"Erfolgsquote im Datensatz: {df_final['label_success'].mean()*100:.2f}%")
    
    # Kurz-Statistik
    up_events = df_final[df_final['event_type'] == 'REVERSION_UP']
    down_events = df_final[df_final['event_type'] == 'REVERSION_DOWN']
    logging.info(f"Reversion UP (Longs): {len(up_events)} | Win-Rate: {up_events['label_success'].mean()*100:.2f}%")
    logging.info(f"Reversion DOWN (Shorts): {len(down_events)} | Win-Rate: {down_events['label_success'].mean()*100:.2f}%")

if __name__ == "__main__":
    main()
import pandas as pd
import pandas_ta as ta  # NEU: Für Indikatoren
import numpy as np
import json
import os
from sqlalchemy import create_engine, text
import time

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
TARGET_MOVE_PCT = 0.10
COINS_FILE = 'coins.json'
OUTPUT_FILE = 'ml_training_data.csv' # Wir speichern als CSV für einfacheres ML

def get_db_engine():
    url = f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
    return create_engine(url)

def load_coins():
    if not os.path.exists(COINS_FILE):
        print(f"Datei {COINS_FILE} nicht gefunden. Nutze Default-Liste.")
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
    print(f"--> Analysiere {symbol}...")
    
    # Benötigt genug Daten für Trend (90 Tage) + längste Indikatoren (z.B. EMA200) + Zukunft (3 Tage) + Puffer
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
        print(f"Fehler beim Laden von {symbol}: {e}")
        return []

    if df.empty:
        return []

    df['open_time'] = pd.to_datetime(df['open_time'], utc=True)
    df['ts'] = df['open_time'].apply(lambda x: x.timestamp()) 
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col])

    # 4. Volumen SMA (Berechnung sollte vor .dropna() erfolgen, da es selbst Lookback hat)
    df['vol_avg_20'] = df['volume'].rolling(window=20).mean()

    

    # ============================================================
    # FEATURE ENGINEERING (Indikatoren berechnen)
    # WIR VERWENDEN JETZT DIE EXAKTEN SPALTENNAMEN, DIE PANDAS_TA LIEFERT!
    # ============================================================
    
    # RSI (Standard)
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # EMAs
    df['EMA_9'] = ta.ema(df['close'], length=9)
    df['EMA_21'] = ta.ema(df['close'], length=21)
    df['EMA_50'] = ta.ema(df['close'], length=50)
    df['EMA_200'] = ta.ema(df['close'], length=200)
    
    # NEU: Abstand in % von EMA 9 zum Close
    df['dist_close_ema9_pct'] = (df['close'] - df['EMA_9']) / df['EMA_9']
    
    # NEU: Distanz in % von EMA9 zu EMA21
    df['dist_ema9_ema21_pct'] = (df['EMA_9'] - df['EMA_21']) / df['EMA_21']
    
    # NEU: KAMA9 & Abstand in % zum Close
    df['KAMA_9'] = ta.kama(df['close'], length=9)
    df['dist_close_kama9_pct'] = (df['close'] - df['KAMA_9']) / df['KAMA_9']

    # NEU: MACD (Basierend auf deinen Fehlermeldungen)
    macd = ta.macd(df['close'], fast=9, slow=21, signal=9)
    df['MACD_Line'] = macd['MACD_9_21_9'] # Korrekter Name basierend auf Warnung
    df['MACD_Signal'] = macd['MACDs_9_21_9'] # Korrekter Name basierend auf Warnung (kleines 's')
    
    # NEU: TSI (Basierend auf deinen Fehlermeldungen)
    tsi = ta.tsi(df['close'], fast=12, slow=7, signal=7)
    df['TSI_Line'] = tsi['TSI_7_12_7'] # Korrekter Name basierend auf Warnung (Parameterreihenfolge)
    df['TSI_Signal'] = tsi['TSIs_7_12_7'] # Korrekter Name basierend auf Warnung (Parameterreihenfolge und kleines 's')
    
    # NEU: Bollinger Bands (20)
    bbands = ta.bbands(df['close'], length=20, std=2.0)
    # Dynamische Suche, falls Standardname nicht passt
    bb_lower_col = next((col for col in bbands.columns if col.startswith('BBL_') and '20' in col), None)
    bb_upper_col = next((col for col in bbands.columns if col.startswith('BBU_') and '20' in col), None)
    bb_mid_col = next((col for col in bbands.columns if col.startswith('BBM_') and '20' in col), None)
    
    if not all([bb_lower_col, bb_upper_col, bb_mid_col]):
        print(f"Warnung: Bollinger Band Spalten nicht alle gefunden für {symbol}. Verfügbare BB Spalten: {bbands.columns.tolist()}")
        # Fallback zu NaN, um Fehler zu vermeiden
        df['BB_Lower'], df['BB_Upper'], df['BB_Mid'] = np.nan, np.nan, np.nan
    else:
        df['BB_Lower'] = bbands[bb_lower_col]
        df['BB_Upper'] = bbands[bb_upper_col]
        df['BB_Mid'] = bbands[bb_mid_col]

    df['dist_close_bb_lower_pct'] = (df['close'] - df['BB_Lower']) / df['close']
    df['dist_close_bb_upper_pct'] = (df['close'] - df['BB_Upper']) / df['close']
    # bb_position_relative nur berechnen, wenn BB_Lower und BB_Upper gültig sind und nicht 0
    df['bb_position_relative'] = df.apply(
        lambda row: (row['close'] - row['BB_Lower']) / (row['BB_Upper'] - row['BB_Lower']) 
        if pd.notna(row['BB_Lower']) and pd.notna(row['BB_Upper']) and (row['BB_Upper'] - row['BB_Lower']) != 0 else np.nan, axis=1
    )

    # NEU: Donchian Channels (20)
    donchian = ta.donchian(df['high'], df['low'], length=20)
    dc_lower_col = next((col for col in donchian.columns if col.startswith('DCL_') and '20' in col), None)
    dc_upper_col = next((col for col in donchian.columns if col.startswith('DCU_') and '20' in col), None)

    if not all([dc_lower_col, dc_upper_col]):
        print(f"Warnung: Donchian Channel Spalten nicht alle gefunden für {symbol}. Verfügbare DC Spalten: {donchian.columns.tolist()}")
        df['DC_Lower'], df['DC_Upper'] = np.nan, np.nan
    else:
        df['DC_Lower'] = donchian[dc_lower_col]
        df['DC_Upper'] = donchian[dc_upper_col]

    df['dist_close_dc_lower_pct'] = (df['close'] - df['DC_Lower']) / df['close']
    df['dist_close_dc_upper_pct'] = (df['close'] - df['DC_Upper']) / df['close']
    df['dc_position_relative'] = df.apply(
        lambda row: (row['close'] - row['DC_Lower']) / (row['DC_Upper'] - row['DC_Lower'])
        if pd.notna(row['DC_Lower']) and pd.notna(row['DC_Upper']) and (row['DC_Upper'] - row['DC_Lower']) != 0 else np.nan, axis=1
    )

    # ATR (Volatilität in % vom Preis) (Standard)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    df['ATR_PCT'] = df['ATR'] / df['close']
    
    # Drop rows where indicators are NaN (am Anfang)
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    

    results = []
    
    # Start Index muss sicherstellen, dass wir 90 Tage zurückschauen können
    # Der `df` sollte jetzt schon keine NaN-Indikatoren mehr enthalten
    start_idx = TREND_WINDOW_HOURS 
    end_idx = len(df) - FUTURE_WINDOW_HOURS
    
    if start_idx >= end_idx:
        # print(f"  Nicht genügend Daten für {symbol} nach NaN-Bereinigung. Verbleibende Samples: {len(df)}h")
        return []

    # Konvertierung zu numpy arrays für Speed
    ts_values = df['ts'].values
    close_values = df['close'].values
    high_values = df['high'].values
    low_values = df['low'].values
    vol_values = df['volume'].values
    vol_avg_values = df['vol_avg_20'].values
    
    # Indikator Arrays (für direkten Zugriff im Loop)
    rsi_values = df['RSI'].values
    ema50_values = df['EMA_50'].values
    ema200_values = df['EMA_200'].values
    atr_pct_values = df['ATR_PCT'].values
    
    # NEU: Indikator Arrays
    dist_close_ema9_pct_values = df['dist_close_ema9_pct'].values
    dist_ema9_ema21_pct_values = df['dist_ema9_ema21_pct'].values
    dist_close_kama9_pct_values = df['dist_close_kama9_pct'].values
    macd_line_values = df['MACD_Line'].values
    macd_signal_values = df['MACD_Signal'].values
    tsi_line_values = df['TSI_Line'].values
    tsi_signal_values = df['TSI_Signal'].values
    dist_close_bb_lower_pct_values = df['dist_close_bb_lower_pct'].values
    dist_close_bb_upper_pct_values = df['dist_close_bb_upper_pct'].values
    bb_position_relative_values = df['bb_position_relative'].values
    dist_close_dc_lower_pct_values = df['dist_close_dc_lower_pct'].values
    dist_close_dc_upper_pct_values = df['dist_close_dc_upper_pct'].values
    dc_position_relative_values = df['dc_position_relative'].values
    
    # Iteration durch die Candles (simulierter Live-Verlauf)
    for i in range(start_idx, end_idx):
        
        # Trend Line Calculation (90 Days back)
        slice_start = i - TREND_WINDOW_HOURS
        # Safety check for slice
        if slice_start < 0: continue
            
        subset_ts = ts_values[slice_start:i]
        subset_close = close_values[slice_start:i]
        
        # Sicherstellen, dass das Subset genügend Punkte für die Regression hat
        if len(subset_ts) < 2: # Eine Regression benötigt mindestens zwei Punkte
             continue
        
        slope, intercept = calculate_trend_vectorized(subset_close, subset_ts)
        
        current_ts = ts_values[i]
        prev_ts = ts_values[i-1]
        
        trend_val_curr = slope * current_ts + intercept
        trend_val_prev = slope * prev_ts + intercept
        
        curr_close = close_values[i]
        prev_close = close_values[i-1]
        
        event_type = None # "UP" oder "DOWN"
        
        # Logik: Trendbruch
        if prev_close < trend_val_prev and curr_close > trend_val_curr:
            event_type = "UP"
        elif prev_close > trend_val_prev and curr_close < trend_val_curr:
            event_type = "DOWN"
            
        if event_type:
            # === FEATURES SAMMELN ===
            vol_ratio = (vol_values[i] / vol_avg_values[i]) if vol_avg_values[i] > 0 else 0
            
            # Distanz zum EMA 200 in %
            dist_ema200 = (curr_close - ema200_values[i]) / ema200_values[i]
            
            # Steigung normalisieren (Prozentänderung pro Tag grob geschätzt)
            slope_pct_per_day = (slope * 86400) / curr_close if curr_close != 0 else 0
            
            # === TARGET (LABEL) BERECHNEN ===
            future_start = i + 1
            future_end = i + 1 + FUTURE_WINDOW_HOURS
            success = 0 # False
            
            # Sicherstellen, dass es genügend zukünftige Daten gibt
            if future_end <= len(high_values):
                if event_type == "UP":
                    max_price = np.max(high_values[future_start:future_end])
                    if (max_price - curr_close) / curr_close >= TARGET_MOVE_PCT:
                        success = 1
                else: # DOWN
                    min_price = np.min(low_values[future_start:future_end])
                    if (curr_close - min_price) / curr_close >= TARGET_MOVE_PCT:
                        success = 1

            results.append({
                "symbol": symbol,
                "event_type": event_type,   # Kategorial (UP/DOWN)
                "vol_ratio": vol_ratio,     # Numerisch
                "rsi": rsi_values[i],       # Numerisch
                "atr_pct": atr_pct_values[i], # Numerisch
                "dist_ema200": dist_ema200, # Numerisch
                "slope_trend": slope_pct_per_day, # Numerisch
                "hour_of_day": df['open_time'].iloc[i].hour, # Zeitlich
                
                # NEUE FEATURES
                "dist_close_ema9_pct": dist_close_ema9_pct_values[i],
                "dist_ema9_ema21_pct": dist_ema9_ema21_pct_values[i],
                "dist_close_kama9_pct": dist_close_kama9_pct_values[i],
                "MACD_Line": macd_line_values[i],
                "MACD_Signal": macd_signal_values[i],
                "TSI_Line": tsi_line_values[i],
                "TSI_Signal": tsi_signal_values[i],
                "dist_close_bb_lower_pct": dist_close_bb_lower_pct_values[i],
                "dist_close_bb_upper_pct": dist_close_bb_upper_pct_values[i],
                "bb_position_relative": bb_position_relative_values[i],
                "dist_close_dc_lower_pct": dist_close_dc_lower_pct_values[i],
                "dist_close_dc_upper_pct": dist_close_dc_upper_pct_values[i],
                "dc_position_relative": dc_position_relative_values[i],

                "label_success": success    # ZIELVARIABLE (Y)
            })

    return results

def main():
    start_time = time.time()
    engine = get_db_engine()
    coins = load_coins()
    all_results = []
    
    print(f"Sammle Trainingsdaten für ML mit erweiterten Indikatoren...")
    
    for coin in coins:
        res = analyze_coin(engine, coin)
        all_results.extend(res)
        
    # Speichern als CSV für ML Tools (Pandas/Scikit-Learn)
    df_final = pd.DataFrame(all_results)
    
    if df_final.empty:
        print("Keine Daten für ML-Training gesammelt. Überprüfen Sie die Konfiguration und die Datenbankdaten.")
        return

    df_final.to_csv(OUTPUT_FILE, index=False)
    
    print(f"\nFertig! {len(df_final)} Trainingsdaten in {OUTPUT_FILE} gespeichert.")
    print(f"Erfolgsquote im Datensatz: {df_final['label_success'].mean()*100:.2f}%")

    duration = (time.time() - start_time) / 60
    print(f"\nDatensammlung abgeschlossen in {duration:.1f} Minuten.")


if __name__ == "__main__":
    main()

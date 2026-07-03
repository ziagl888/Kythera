import json
import psycopg2
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta
import pandas_ta as pta
import multiprocessing as mp # Importieren des Multiprocessing Moduls
import os # Für os.cpu_count()

# --- Konfiguration ---
DB_CONFIG = {
    'dbname': 'cryptodata',
    'user': 'dbfiller',
    'password': os.getenv("DB_PASSWORD", ""),
    'host': 'localhost',
    'port': 5432
}
COINS_FILE = 'coins.json'
OUTPUT_FILE = 'break_retest_analysis_with_features.json'

# --- Parameter für die Analyse ---
DAYS_TO_LOOK_BACK = 365
PIVOT_WINDOW = 10
LEVEL_TOLERANCE = 0.005
RETEST_LOOKAHEAD = 24
RESULT_LOOKAHEAD = 12

# --- Funktionen (unverändert oder nur minimale Anpassungen) ---
# get_db_connection wird jetzt innerhalb des Worker-Prozesses aufgerufen
# load_coins bleibt gleich
# get_ohlcv_data bleibt gleich (mit dem Tz-aware Fix)

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)

def load_coins():
    with open(COINS_FILE, 'r') as f:
        data = json.load(f)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and 'coins' in data:
            return data['coins']
        else:
            raise ValueError("Format der coins.json nicht erkannt.")

def get_ohlcv_data(conn, symbol):
    table_name = f"{symbol}_1h"
    query = f"""
        SELECT open_time::text as open_time, open, high, low, close, volume
        FROM "{table_name}"
        WHERE open_time >= NOW() - INTERVAL '{DAYS_TO_LOOK_BACK} days'
        ORDER BY open_time ASC;
    """
    try:
        df = pd.read_sql(query, conn)
        df['open_time'] = pd.to_datetime(df['open_time'], utc=True)
        return df
    except Exception as e:
        # Fehlerbehandlung für fehlende Tabellen etc.
        if "relation" in str(e) and "does not exist" in str(e):
             print(f"  -> Tabelle für {symbol} existiert nicht. Überspringe.")
        else:
             print(f"Fehler beim Laden von {symbol}: {e}")
        return None

def calculate_technical_indicators(df):
    """Berechnet alle gewünschten technischen Indikatoren und Features mit pandas_ta."""
    df['open'] = pd.to_numeric(df['open'])
    df['high'] = pd.to_numeric(df['high'])
    df['low'] = pd.to_numeric(df['low'])
    df['close'] = pd.to_numeric(df['close'])
    df['volume'] = pd.to_numeric(df['volume'])

    df.ta.ema(length=9, append=True)
    df.ta.ema(length=21, append=True)
    df.ta.kama(length=9, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.tsi(fast=7, slow=12, signal=7, append=True)
    df.ta.bbands(length=20, append=True)
    df.ta.donchian(length=20, append=True)

    expected_pta_cols = {
        'EMA_9': np.nan, 'EMA_21': np.nan, 'KAMA_9': np.nan,
        'RSI_14': np.nan,
        'TSI_12_7': np.nan, 'TSIs_12_7_7': np.nan,
        'BBL_20_2': np.nan, 'BBM_20_2': np.nan, 'BBU_20_2': np.nan,
        'DCL_20': np.nan, 'DCM_20': np.nan, 'DCU_20': np.nan
    }
    for col, default_val in expected_pta_cols.items():
        if col not in df.columns:
            df[col] = default_val

    df.rename(columns={
        'EMA_9': 'ema9',
        'EMA_21': 'ema21',
        'KAMA_9': 'kama9',
        'RSI_14': 'rsi14',
        'TSI_12_7': 'tsi',
        'TSIs_12_7_7': 'tsi_signal',
        'BBL_20_2': 'boll_lower_20',
        'BBM_20_2': 'boll_mid_20',
        'BBU_20_2': 'boll_upper_20',
        'DCL_20': 'donchian_lower_20',
        'DCM_20': 'donchian_mid_20',
        'DCU_20': 'donchian_upper_20'
    }, inplace=True)

    df['dist_close_ema9_pct'] = ((df['close'] - df['ema9']) / df['ema9'] * 100).fillna(0)
    df['dist_ema9_ema21_pct'] = ((df['ema9'] - df['ema21']) / df['ema21'] * 100).fillna(0)
    df['dist_close_kama9_pct'] = ((df['close'] - df['kama9']) / df['kama9'] * 100).fillna(0)

    # FIX: Explizite Konvertierung zu Python int für JSON-Serialisierung
    df['rsi_below_30'] = (df['rsi14'] < 30).astype(int)
    df['rsi_above_70'] = (df['rsi14'] > 70).astype(int)

    df['tsi_above_0'] = (df['tsi'] > 0).astype(int)
    df['tsi_below_0'] = (df['tsi'] < 0).astype(int)

    df['dist_close_boll_upper_pct'] = ((df['close'] - df['boll_upper_20']) / df['boll_upper_20'] * 100).fillna(0)
    df['dist_close_boll_mid_pct'] = ((df['close'] - df['boll_mid_20']) / df['boll_mid_20'] * 100).fillna(0)
    df['dist_close_boll_lower_pct'] = ((df['close'] - df['boll_lower_20']) / df['boll_lower_20'] * 100).fillna(0)

    df['dist_close_donchian_upper_pct'] = ((df['close'] - df['donchian_upper_20']) / df['donchian_upper_20'] * 100).fillna(0)
    df['dist_close_donchian_mid_pct'] = ((df['close'] - df['donchian_mid_20']) / df['donchian_mid_20'] * 100).fillna(0)
    df['dist_close_donchian_lower_pct'] = ((df['close'] - df['donchian_lower_20']) / df['donchian_lower_20'] * 100).fillna(0)

    df['volume_avg_30'] = df['volume'].rolling(window=30, min_periods=1).mean()
    df['retest_volume_ratio_avg'] = (df['volume'] / df['volume_avg_30']).fillna(1)

    df = df.fillna(0)

    return df

def find_pivot_levels(df, window=PIVOT_WINDOW):
    """Findet lokale Highs und Lows als Levels."""
    df['high_pivot'] = df.iloc[argrelextrema(df['high'].values, np.greater_equal, order=window)[0]]['high']
    df['low_pivot'] = df.iloc[argrelextrema(df['low'].values, np.less_equal, order=window)[0]]['low']
    
    levels = []
    
    for idx, row in df.dropna(subset=['high_pivot']).iterrows():
        levels.append({'price': row['high_pivot'], 'type': 'resistance', 'index': idx, 'time': row['open_time']})
    
    for idx, row in df.dropna(subset=['low_pivot']).iterrows():
        levels.append({'price': row['low_pivot'], 'type': 'support', 'index': idx, 'time': row['open_time']})
        
    return levels

# --- Die angepasste analyze_coin Funktion für Multiprocessing ---
def analyze_coin_worker(symbol):
    """
    Diese Funktion wird von jedem Worker-Prozess aufgerufen.
    Sie öffnet ihre eigene DB-Verbindung, verarbeitet einen Coin und gibt Events zurück.
    """
    conn = None # Initialize conn to None
    try:
        conn = get_db_connection()
        print(f"Verarbeite {symbol}...")
        df = get_ohlcv_data(conn, symbol)
        
        if df is None or df.empty:
            return [] # Keine Daten, leere Liste von Events zurückgeben

        # Entferne Reihen mit fundamentalen NaN-Werten, die die Indikatorberechnung stören würden
        df.dropna(subset=['close', 'high', 'low', 'volume'], inplace=True)
        if df.empty:
            print(f"  -> {symbol} hat keine vollständigen OHLCV-Daten nach NaN-Entfernung. Überspringe.")
            return []

        df_with_indicators = calculate_technical_indicators(df.copy())
        
        min_data_points_required = max(PIVOT_WINDOW * 2, 30, RETEST_LOOKAHEAD + RESULT_LOOKAHEAD + 1)
        if len(df_with_indicators) < min_data_points_required:
            print(f"  -> Nicht genug Daten für {symbol} nach Indikatorberechnung. Benötigt: {min_data_points_required}, Vorhanden: {len(df_with_indicators)}. Überspringe.")
            return []

        levels = find_pivot_levels(df_with_indicators)
        events = []
        
        start_index = max(PIVOT_WINDOW * 2, 30) 

        for i in range(start_index, len(df_with_indicators) - RESULT_LOOKAHEAD - 1):
            current_candle = df_with_indicators.iloc[i]
            prev_candle = df_with_indicators.iloc[i-1]
            
            active_levels = [l for l in levels if l['index'] < (i - PIVOT_WINDOW)]
            
            for level in active_levels:
                lvl_price = level['price']
                lvl_type = level['type']
                
                if lvl_type == 'resistance':
                    break_condition = prev_candle['close'] < lvl_price and current_candle['close'] > lvl_price
                    
                    if break_condition:
                        for j in range(1, RETEST_LOOKAHEAD + 1):
                            retest_idx = i + j
                            if retest_idx >= len(df_with_indicators) - RESULT_LOOKAHEAD: break
                            
                            future_candle = df_with_indicators.iloc[retest_idx]
                            
                            upper_bound = lvl_price * (1 + LEVEL_TOLERANCE)
                            lower_bound = lvl_price * (1 - LEVEL_TOLERANCE)
                            
                            if future_candle['low'] <= upper_bound and future_candle['low'] >= lower_bound:
                                
                                result_candle = df_with_indicators.iloc[retest_idx + RESULT_LOOKAHEAD]
                                price_change_pct = (result_candle['close'] - lvl_price) / lvl_price
                                
                                outcome = "neutral"
                                if price_change_pct > 0.05: outcome = "continuation_success"
                                elif price_change_pct < -0.03: outcome = "failed_breakout"
                                
                                # FIX: Explizite Konvertierung zu Python int
                                features = {
                                    'dist_close_ema9_pct': float(future_candle['dist_close_ema9_pct']),
                                    'dist_ema9_ema21_pct': float(future_candle['dist_ema9_ema21_pct']),
                                    'dist_close_kama9_pct': float(future_candle['dist_close_kama9_pct']),
                                    'rsi14': float(future_candle['rsi14']),
                                    'rsi_below_30': int(future_candle['rsi_below_30']), # Konvertierung
                                    'rsi_above_70': int(future_candle['rsi_above_70']), # Konvertierung
                                    'tsi': float(future_candle['tsi']),
                                    'tsi_signal': float(future_candle['tsi_signal']),
                                    'tsi_above_0': int(future_candle['tsi_above_0']), # Konvertierung
                                    'tsi_below_0': int(future_candle['tsi_below_0']), # Konvertierung
                                    'dist_close_boll_upper_pct': float(future_candle['dist_close_boll_upper_pct']),
                                    'dist_close_boll_mid_pct': float(future_candle['dist_close_boll_mid_pct']),
                                    'dist_close_boll_lower_pct': float(future_candle['dist_close_boll_lower_pct']),
                                    'dist_close_donchian_upper_pct': float(future_candle['dist_close_donchian_upper_pct']),
                                    'dist_close_donchian_mid_pct': float(future_candle['dist_close_donchian_mid_pct']),
                                    'dist_close_donchian_lower_pct': float(future_candle['dist_close_donchian_lower_pct']),
                                    'retest_volume': float(future_candle['volume']),
                                    'retest_volume_ratio_avg': float(future_candle['retest_volume_ratio_avg']),
                                }

                                event_data = {
                                    'symbol': symbol,
                                    'type': 'LONG_BREAK_RETEST',
                                    'break_time': str(current_candle['open_time']),
                                    'retest_time': str(future_candle['open_time']),
                                    'level_price': float(lvl_price),
                                    'outcome_price_change': round(price_change_pct * 100, 2),
                                    'outcome_class': outcome,
                                }
                                event_data.update(features)
                                events.append(event_data)
                                break 

                elif lvl_type == 'support':
                    break_condition = prev_candle['close'] > lvl_price and current_candle['close'] < lvl_price
                    
                    if break_condition:
                        for j in range(1, RETEST_LOOKAHEAD + 1):
                            retest_idx = i + j
                            if retest_idx >= len(df_with_indicators) - RESULT_LOOKAHEAD: break
                            
                            future_candle = df_with_indicators.iloc[retest_idx]
                            
                            upper_bound = lvl_price * (1 + LEVEL_TOLERANCE)
                            lower_bound = lvl_price * (1 - LEVEL_TOLERANCE)
                            
                            if future_candle['high'] >= lower_bound and future_candle['high'] <= upper_bound:
                                
                                result_candle = df_with_indicators.iloc[retest_idx + RESULT_LOOKAHEAD]
                                price_change_pct = (lvl_price - result_candle['close']) / lvl_price
                                
                                outcome = "neutral"
                                if price_change_pct > 0.05: outcome = "continuation_success"
                                elif price_change_pct < -0.03: outcome = "failed_breakout"
                                
                                # FIX: Explizite Konvertierung zu Python int
                                features = {
                                    'dist_close_ema9_pct': float(future_candle['dist_close_ema9_pct']),
                                    'dist_ema9_ema21_pct': float(future_candle['dist_ema9_ema21_pct']),
                                    'dist_close_kama9_pct': float(future_candle['dist_close_kama9_pct']),
                                    'rsi14': float(future_candle['rsi14']),
                                    'rsi_below_30': int(future_candle['rsi_below_30']), # Konvertierung
                                    'rsi_above_70': int(future_candle['rsi_above_70']), # Konvertierung
                                    'tsi': float(future_candle['tsi']),
                                    'tsi_signal': float(future_candle['tsi_signal']),
                                    'tsi_above_0': int(future_candle['tsi_above_0']), # Konvertierung
                                    'tsi_below_0': int(future_candle['tsi_below_0']), # Konvertierung
                                    'dist_close_boll_upper_pct': float(future_candle['dist_close_boll_upper_pct']),
                                    'dist_close_boll_mid_pct': float(future_candle['dist_close_boll_mid_pct']),
                                    'dist_close_boll_lower_pct': float(future_candle['dist_close_boll_lower_pct']),
                                    'dist_close_donchian_upper_pct': float(future_candle['dist_close_donchian_upper_pct']),
                                    'dist_close_donchian_mid_pct': float(future_candle['dist_close_donchian_mid_pct']),
                                    'dist_close_donchian_lower_pct': float(future_candle['dist_close_donchian_lower_pct']),
                                    'retest_volume': float(future_candle['volume']),
                                    'retest_volume_ratio_avg': float(future_candle['retest_volume_ratio_avg']),
                                }

                                event_data = {
                                    'symbol': symbol,
                                    'type': 'SHORT_BREAK_RETEST',
                                    'break_time': str(current_candle['open_time']),
                                    'retest_time': str(future_candle['open_time']),
                                    'level_price': float(lvl_price),
                                    'outcome_price_change': round(price_change_pct * 100, 2),
                                    'outcome_class': outcome,
                                }
                                event_data.update(features)
                                events.append(event_data)
                                break 

        return events
    except Exception as e:
        print(f"Unerwarteter Fehler im Worker für {symbol}: {e}")
        return []
    finally:
        if conn:
            conn.close() # Stellen Sie sicher, dass die Verbindung geschlossen wird

# --- Die angepasste main Funktion für Multiprocessing ---
def main():
    coins = load_coins()
    all_events = []
    
    print(f"Starte Analyse für {len(coins)} Coins mit {os.cpu_count()} Prozessen...")
    
    # Erstellen eines Process Pools
    # Die Anzahl der Prozesse ist standardmäßig die Anzahl der CPU-Kerne
    # Manchmal ist es ratsam, N-1 Kerne zu verwenden, um das System responsiv zu halten
    with mp.Pool(processes=os.cpu_count()) as pool:
        # map() wendet analyze_coin_worker auf jedes Element in coins an
        # Die Ergebnisse werden gesammelt, sobald jeder Prozess fertig ist
        results = pool.map(analyze_coin_worker, coins)
    
    # Ergebnisse sammeln
    for coin_events in results:
        all_events.extend(coin_events)
            
    print(f"\nGesamtanalyse abgeschlossen. {len(all_events)} Events insgesamt gefunden.")

    # Ergebnisse speichern
    summary = {
        'total_events': len(all_events),
        'events': all_events
    }
    
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(summary, f, indent=4)
        
    print(f"\nErgebnisse in {OUTPUT_FILE} gespeichert.")

    # Kleine Statistik Ausgeben
    df_res = pd.DataFrame(all_events)
    if not df_res.empty:
        print("\n--- Statistik ---")
        print(df_res['outcome_class'].value_counts())
        print("\nDurchschnittlicher Profit pro Outcome:")
        print(df_res.groupby('outcome_class')['outcome_price_change'].mean())
        print("\nBeispiel für ein Event mit Features:")
        print(df_res.iloc[0].to_dict())

if __name__ == "__main__":
    # Wichtig: multiprocessing sollte so aufgerufen werden, damit es plattformübergreifend funktioniert
    # und keine rekursiven Spawns verursacht.
    mp.freeze_support() # Optional, aber gute Praxis für Windows-Executables
    main()

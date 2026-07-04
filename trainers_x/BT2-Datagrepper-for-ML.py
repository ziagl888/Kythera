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

    # FIX (P0.12): pandas_ta benennt seine Spalten versions-/parameterabhängig
    # (KAMA_9_2_30 statt KAMA_9, TSI_7_12_7 statt TSI_12_7, BBL_20_2.0_2.0 statt
    # BBL_20_2, DCL_20_20 statt DCL_20). Das alte Exakt-Matching fand 11 der 18
    # Feature-Quellspalten nie → NaN-Spalte angelegt → fillna(0) → 11 Features
    # konstant 0 im Trainingsdatensatz (Split-Count-Beweis im Live-Modell).
    # Jetzt: Prefix-Matching + hartes ValueError statt stillem Default.
    # 'TSIs_' muss vor 'TSI_' geprüft werden.
    prefix_to_canonical = [
        ('EMA_9', 'ema9'),
        ('EMA_21', 'ema21'),
        ('KAMA_9', 'kama9'),
        ('RSI_14', 'rsi14'),
        ('TSIs_', 'tsi_signal'),
        ('TSI_', 'tsi'),
        ('BBL_', 'boll_lower_20'),
        ('BBM_', 'boll_mid_20'),
        ('BBU_', 'boll_upper_20'),
        ('DCL_', 'donchian_lower_20'),
        ('DCM_', 'donchian_mid_20'),
        ('DCU_', 'donchian_upper_20'),
    ]
    rename_map = {}
    missing = []
    for prefix, canonical in prefix_to_canonical:
        col = next((c for c in df.columns if c.startswith(prefix)), None)
        if col is None:
            missing.append(f"{prefix}* -> {canonical}")
        else:
            rename_map[col] = canonical
    if missing:
        raise ValueError(f"pandas_ta-Spalten nicht gefunden: {missing}")
    df.rename(columns=rename_map, inplace=True)

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
            # FIX (P1.31): 'keine Daten' explizit von 'keine Events' unterscheiden,
            # damit main() die Coin-Abdeckung messen und bei Truncation abbrechen kann.
            return {'symbol': symbol, 'status': 'no_data', 'events': []}

        # Entferne Reihen mit fundamentalen NaN-Werten, die die Indikatorberechnung stören würden
        df.dropna(subset=['close', 'high', 'low', 'volume'], inplace=True)
        if df.empty:
            print(f"  -> {symbol} hat keine vollständigen OHLCV-Daten nach NaN-Entfernung. Überspringe.")
            return {'symbol': symbol, 'status': 'no_data', 'events': []}

        df_with_indicators = calculate_technical_indicators(df.copy())
        
        min_data_points_required = max(PIVOT_WINDOW * 2, 30, RETEST_LOOKAHEAD + RESULT_LOOKAHEAD + 1)
        if len(df_with_indicators) < min_data_points_required:
            print(f"  -> Nicht genug Daten für {symbol} nach Indikatorberechnung. Benötigt: {min_data_points_required}, Vorhanden: {len(df_with_indicators)}. Überspringe.")
            return {'symbol': symbol, 'status': 'no_data', 'events': []}

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

        return {'symbol': symbol, 'status': 'ok', 'events': events}
    except Exception as e:
        # FIX (P1.31): Fehler nicht mehr still als 'leere Events' maskieren.
        print(f"Unerwarteter Fehler im Worker für {symbol}: {e}")
        return {'symbol': symbol, 'status': 'error', 'events': []}
    finally:
        if conn:
            conn.close() # Stellen Sie sicher, dass die Verbindung geschlossen wird

# --- Die angepasste main Funktion für Multiprocessing ---
def _worker_init_low_priority():
    """Der VPS läuft an der Lastgrenze — Worker laufen mit BELOW_NORMAL."""
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


# FIX (P1.31): Mindest-Coin-Abdeckung — unter 80% wird hart abgebrochen statt
# still auf einem trunkierten Universum zu trainieren.
MIN_COIN_COVERAGE = 0.80
# CPU-Schutz: Live-Fleet auf demselben Host — max. 2 Worker statt cpu_count().
MAX_WORKERS = 2


def main():
    coins = load_coins()
    all_events = []

    print(f"Starte Analyse für {len(coins)} Coins mit {MAX_WORKERS} Prozessen (BELOW_NORMAL)...")

    with mp.Pool(processes=MAX_WORKERS, initializer=_worker_init_low_priority) as pool:
        results = pool.map(analyze_coin_worker, coins)

    # FIX (P1.31): Abdeckung messen, Skips loggen, bei Truncation abbrechen.
    ok = [r for r in results if r['status'] == 'ok']
    no_data = [r['symbol'] for r in results if r['status'] == 'no_data']
    errors = [r['symbol'] for r in results if r['status'] == 'error']
    if no_data:
        print(f"⚠️  {len(no_data)} Coins ohne Daten: {no_data[:20]}{'...' if len(no_data) > 20 else ''}")
    if errors:
        print(f"⚠️  {len(errors)} Coins mit Fehlern: {errors[:20]}{'...' if len(errors) > 20 else ''}")

    coverage = len(ok) / len(coins) if coins else 0.0
    if coverage < MIN_COIN_COVERAGE:
        raise SystemExit(
            f"ABBRUCH: nur {len(ok)}/{len(coins)} Coins ({coverage:.0%}) lieferten Daten "
            f"(Minimum {MIN_COIN_COVERAGE:.0%}). Kein Output geschrieben — vorher trainierte "
            f"diese Pipeline in so einem Fall still auf einem trunkierten Universum."
        )

    for r in ok:
        all_events.extend(r['events'])

    print(f"\nGesamtanalyse abgeschlossen. {len(all_events)} Events insgesamt gefunden "
          f"(Abdeckung {len(ok)}/{len(coins)} Coins).")

    # FIX (P0.12/X-R5): kein Feature darf über den gesamten Datensatz konstant
    # sein — genau so blieb der 11/18-Features-Bug drei Stufen lang unsichtbar.
    if all_events:
        df_check = pd.DataFrame(all_events)
        feature_cols = [c for c in df_check.columns
                        if c not in ('symbol', 'type', 'break_time', 'retest_time',
                                     'level_price', 'outcome_price_change', 'outcome_class')]
        constant_cols = [c for c in feature_cols if df_check[c].nunique(dropna=False) <= 1]
        if constant_cols:
            raise SystemExit(f"ABBRUCH: konstante Feature-Spalten im Trainingsdatensatz: {constant_cols}")

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

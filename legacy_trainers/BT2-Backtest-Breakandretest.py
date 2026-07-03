import os
import json
import psycopg2
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta

# --- Konfiguration ---
DB_CONFIG = {
    'dbname': 'cryptodata',
    'user': 'dbfiller',
    'password': os.getenv("DB_PASSWORD", ""),
    'host': 'localhost',
    'port': 5432
}
COINS_FILE = 'coins.json'
OUTPUT_FILE = 'break_retest_analysis.json'

# --- Parameter für die Analyse ---
DAYS_TO_LOOK_BACK = 365
PIVOT_WINDOW = 10  # Wie viele Kerzen links/rechts für ein Pivot High/Low
LEVEL_TOLERANCE = 0.005  # 0.5% Toleranzzone um das Level herum
RETEST_LOOKAHEAD = 24  # Wie viele Stunden nach dem Break darf der Retest passieren?
RESULT_LOOKAHEAD = 12  # Wie viele Stunden nach dem Retest schauen wir das Ergebnis an?

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)

def load_coins():
    with open(COINS_FILE, 'r') as f:
        data = json.load(f)
        # Annahme: coins.json ist eine Liste ["BTCUSDT", "ETHUSDT", ...] 
        # oder ein Dict {"coins": [...]}. Hier eine einfache Behandlung:
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and 'coins' in data:
            return data['coins']
        else:
            raise ValueError("Format der coins.json nicht erkannt.")

def get_ohlcv_data(conn, symbol):
    table_name = f"{symbol}_1h"
    
    # ÄNDERUNG: Wir casten open_time zu TEXT (::text), damit Pandas nicht abstürzt.
    # Wir laden es als String und konvertieren es danach kontrolliert.
    query = f"""
        SELECT open_time::text as open_time, open, high, low, close, volume
        FROM "{table_name}"
        WHERE open_time >= NOW() - INTERVAL '{DAYS_TO_LOOK_BACK} days'
        ORDER BY open_time ASC;
    """
    try:
        # UserWarning ignorieren oder wir fixen es pragmatisch durch den Text-Cast
        df = pd.read_sql(query, conn)
        
        # ÄNDERUNG: Explizite Konvertierung mit utc=True
        df['open_time'] = pd.to_datetime(df['open_time'], utc=True)
        
        return df
    except Exception as e:
        print(f"Fehler beim Laden von {symbol}: {e}")
        return None


def find_pivot_levels(df, window=PIVOT_WINDOW):
    """Findet lokale Highs und Lows als Levels."""
    # Find local highs
    df['high_pivot'] = df.iloc[argrelextrema(df['high'].values, np.greater_equal, order=window)[0]]['high']
    # Find local lows
    df['low_pivot'] = df.iloc[argrelextrema(df['low'].values, np.less_equal, order=window)[0]]['low']
    
    levels = []
    
    # Wir nehmen nur signifikante Levels in eine Liste auf
    # Für Highs
    for idx, row in df.dropna(subset=['high_pivot']).iterrows():
        levels.append({'price': row['high_pivot'], 'type': 'resistance', 'index': idx, 'time': row['open_time']})
    
    # Für Lows
    for idx, row in df.dropna(subset=['low_pivot']).iterrows():
        levels.append({'price': row['low_pivot'], 'type': 'support', 'index': idx, 'time': row['open_time']})
        
    return levels

def analyze_coin(symbol, df):
    levels = find_pivot_levels(df)
    events = []
    
    # Wir iterieren durch die Daten, aber erst ab einem gewissen Punkt, damit wir "alte" Levels haben
    start_index = PIVOT_WINDOW * 2 
    
    for i in range(start_index, len(df) - RESULT_LOOKAHEAD):
        current_candle = df.iloc[i]
        prev_candle = df.iloc[i-1]
        
        # Nur Levels betrachten, die "alt" genug sind (nicht gerade erst entstanden)
        active_levels = [l for l in levels if l['index'] < (i - PIVOT_WINDOW)]
        
        for level in active_levels:
            lvl_price = level['price']
            lvl_type = level['type']
            
            # --- LONG SETUP (Resistance Break & Retest) ---
            if lvl_type == 'resistance':
                # 1. Break: Close war unter Level, jetzt über Level
                # Oder starker Durchbruch
                break_condition = prev_candle['close'] < lvl_price and current_candle['close'] > lvl_price
                
                if break_condition:
                    # Wir haben einen Break. Suchen wir einen Retest in den nächsten X Kerzen
                    # Ein Retest bedeutet, der Preis kommt zurück in die Toleranzzone des Levels
                    retest_found = False
                    retest_index = -1
                    
                    for j in range(1, RETEST_LOOKAHEAD + 1):
                        if (i + j) >= len(df) - RESULT_LOOKAHEAD: break
                        
                        future_candle = df.iloc[i + j]
                        
                        # Retest Zone: Preis berührt das Level von oben (Low <= Level * (1+Tol))
                        # Aber schließt idealerweise nicht weit darunter (optional)
                        upper_bound = lvl_price * (1 + LEVEL_TOLERANCE)
                        lower_bound = lvl_price * (1 - LEVEL_TOLERANCE)
                        
                        if future_candle['low'] <= upper_bound and future_candle['low'] >= lower_bound:
                            retest_found = True
                            retest_index = i + j
                            
                            # ANALYSE DES RESULTATS NACH DEM RETEST
                            # Was passierte X Stunden nach dem Retest?
                            result_candle = df.iloc[retest_index + RESULT_LOOKAHEAD]
                            price_change_pct = (result_candle['close'] - lvl_price) / lvl_price
                            
                            outcome = "neutral"
                            if price_change_pct > 0.02: outcome = "continuation_success" # 2% Gewinn
                            elif price_change_pct < -0.01: outcome = "failed_breakout" # Unter Level gefallen
                            
                            events.append({
                                'symbol': symbol,
                                'type': 'LONG_BREAK_RETEST',
                                'break_time': str(current_candle['open_time']),
                                'retest_time': str(future_candle['open_time']),
                                'level_price': lvl_price,
                                'outcome_price_change': round(price_change_pct * 100, 2),
                                'outcome_class': outcome
                            })
                            break # Retest gefunden, Schleife abbrechen um Dopplungen zu vermeiden

            # --- SHORT SETUP (Support Break & Retest) ---
            elif lvl_type == 'support':
                # 1. Break: Close war über Level, jetzt unter Level
                break_condition = prev_candle['close'] > lvl_price and current_candle['close'] < lvl_price
                
                if break_condition:
                    for j in range(1, RETEST_LOOKAHEAD + 1):
                        if (i + j) >= len(df) - RESULT_LOOKAHEAD: break
                        
                        future_candle = df.iloc[i + j]
                        
                        # Retest Zone: Preis berührt das Level von unten (High >= Level * (1-Tol))
                        upper_bound = lvl_price * (1 + LEVEL_TOLERANCE)
                        lower_bound = lvl_price * (1 - LEVEL_TOLERANCE)
                        
                        if future_candle['high'] >= lower_bound and future_candle['high'] <= upper_bound:
                            retest_index = i + j
                            
                            # RESULTAT
                            result_candle = df.iloc[retest_index + RESULT_LOOKAHEAD]
                            price_change_pct = (lvl_price - result_candle['close']) / lvl_price # Short Gewinn wenn Preis fällt
                            
                            outcome = "neutral"
                            if price_change_pct > 0.02: outcome = "continuation_success"
                            elif price_change_pct < -0.01: outcome = "failed_breakout"
                            
                            events.append({
                                'symbol': symbol,
                                'type': 'SHORT_BREAK_RETEST',
                                'break_time': str(current_candle['open_time']),
                                'retest_time': str(future_candle['open_time']),
                                'level_price': lvl_price,
                                'outcome_price_change': round(price_change_pct * 100, 2),
                                'outcome_class': outcome
                            })
                            break 

    return events

def main():
    conn = get_db_connection()
    coins = load_coins()
    all_events = []
    
    print(f"Starte Analyse für {len(coins)} Coins...")
    
    for coin in coins:
        print(f"Verarbeite {coin}...")
        df = get_ohlcv_data(conn, coin)
        
        if df is not None and not df.empty:
            events = analyze_coin(coin, df)
            all_events.extend(events)
            print(f"  -> {len(events)} Events gefunden.")
        else:
            print(f"  -> Keine Daten.")

    conn.close()
    
    # Ergebnisse speichern
    summary = {
        'total_events': len(all_events),
        'events': all_events
    }
    
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(summary, f, indent=4)
        
    print(f"\nAnalyse abgeschlossen. Ergebnisse in {OUTPUT_FILE} gespeichert.")

    # Kleine Statistik Ausgeben
    df_res = pd.DataFrame(all_events)
    if not df_res.empty:
        print("\n--- Statistik ---")
        print(df_res['outcome_class'].value_counts())
        print("\nDurchschnittlicher Profit pro Outcome:")
        print(df_res.groupby('outcome_class')['outcome_price_change'].mean())

if __name__ == "__main__":
    main()

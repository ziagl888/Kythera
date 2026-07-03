import pandas as pd
import numpy as np
import json
import os
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import time

# ========================= DATABASE CONFIG =========================
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "dbfiller",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "cryptodata"
}

# ========================= SETTINGS =========================
LOOKBACK_DAYS = 365       # Wie weit in die Vergangenheit prüfen?
TREND_WINDOW_HOURS = 90 * 24  # 90 Tage für die Trendlinie
FUTURE_WINDOW_HOURS = 3 * 24  # 3 Tage Zukunft prüfen
TARGET_MOVE_PCT = 0.10    # 10% Bewegung
COINS_FILE = 'coins.json'
OUTPUT_FILE = 'trend_backtest_results.json'

def get_db_engine():
    """Erstellt die DB-Verbindung"""
    url = f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
    return create_engine(url)

def load_coins():
    if not os.path.exists(COINS_FILE):
        print(f"Datei {COINS_FILE} nicht gefunden. Nutze Default-Liste.")
        return ["ETHUSDT", "BTCUSDT"] 
    with open(COINS_FILE, 'r') as f:
        return json.load(f)

def calculate_trend_vectorized(prices, timestamps):
    """
    Berechnet Slope und Intercept mittels Numpy (schneller als scipy).
    x sind die timestamps (seconds), y sind die prices.
    """
    x = timestamps
    y = prices
    A = np.vstack([x, np.ones(len(x))]).T
    m, c = np.linalg.lstsq(A, y, rcond=None)[0]
    return m, c

def analyze_coin(engine, symbol):
    print(f"--> Analysiere {symbol}...")
    
    # 1. Daten laden (365 Tage + 90 Tage Vorlauf für die erste Trendlinie)
    # Wir brauchen mehr Daten als 365 Tage, damit wir am Tag 1 der Prüfung schon eine 90-Tage Historie haben.
    total_days_load = LOOKBACK_DAYS + 90 + 5 
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

    # Datentypen anpassen
    df['open_time'] = pd.to_datetime(df['open_time'], utc=True)
    # Timestamp in Sekunden für die Regression
    df['ts'] = df['open_time'].apply(lambda x: x.timestamp()) 
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col])

    # 2. Volumen Durchschnitt (SMA 20) vorberechnen
    df['vol_avg_20'] = df['volume'].rolling(window=20).mean()

    results = []
    
    # Wir starten die Schleife erst, wenn wir genug Daten für Trend (90d) und Vol (20h) haben
    start_index = TREND_WINDOW_HOURS
    # Wir hören auf, bevor wir keine "Zukunft" (3 Tage) mehr haben
    end_index = len(df) - FUTURE_WINDOW_HOURS

    # Um nicht JEDE Stunde eine Regression zu rechnen (dauert ewig), 
    # kann man hier optimieren. Aber für Genauigkeit machen wir es Schritt für Schritt.
    # Performance-Hinweis: Das hier kann bei vielen Coins dauern.
    
    # Iteration durch die Candles (simulierter Live-Verlauf)
    # Wir nutzen Indizes für schnellen Zugriff
    ts_values = df['ts'].values
    close_values = df['close'].values
    high_values = df['high'].values
    low_values = df['low'].values
    vol_values = df['volume'].values
    vol_avg_values = df['vol_avg_20'].values
    times = df['open_time'].values

    for i in range(start_index, end_index):
        # Der aktuelle Zeitpunkt der Prüfung ist "i".
        # Das Fenster für den Trend ist [i - 90 Tage : i]
        
        # Daten für Trendberechnung (die letzten 90 Tage VOR Kerze i)
        # slicing [start:end] ist exklusive end, also nehmen wir i+1 um Kerze i einzuschließen oder i?
        # Logik: "Daten der letzten 365 Tage". Trendberechnung auf den geschlossenen Kerzen.
        slice_start = i - TREND_WINDOW_HOURS
        slice_end = i 
        
        subset_ts = ts_values[slice_start:slice_end]
        subset_close = close_values[slice_start:slice_end]
        
        # Trendlinie berechnen
        slope, intercept = calculate_trend_vectorized(subset_close, subset_ts)
        
        # Trendwert für die AKTUELLE Kerze (i) und die VORHERIGE (i-1)
        current_ts = ts_values[i]
        prev_ts = ts_values[i-1]
        
        trend_val_curr = slope * current_ts + intercept
        trend_val_prev = slope * prev_ts + intercept
        
        curr_close = close_values[i]
        prev_close = close_values[i-1]
        
        # Event Detection
        event_type = None
        
        # Logik: Letzte Candle (prev) unter Trend, aktuelle (curr) über Trend
        if prev_close < trend_val_prev and curr_close > trend_val_curr:
            event_type = "BREAK_UP"
            
        # Logik: Letzte Candle (prev) über Trend, aktuelle (curr) unter Trend
        elif prev_close > trend_val_prev and curr_close < trend_val_curr:
            event_type = "BREAK_DOWN"
            
        if event_type:
            # Volumen Ratio prüfen
            curr_vol = vol_values[i]
            avg_vol = vol_avg_values[i]
            
            if avg_vol == 0 or np.isnan(avg_vol):
                vol_ratio = 0
            else:
                vol_ratio = curr_vol / avg_vol
            
            # Zukunft prüfen (nächste 3 Tage = 72 Stunden)
            # Slice: i+1 bis i+1+72
            future_start = i + 1
            future_end = i + 1 + FUTURE_WINDOW_HOURS
            
            success = False
            max_pct_change = 0.0
            
            if event_type == "BREAK_UP":
                # Suche nach Preisanstieg > 10%
                # Wir schauen auf die HIGHS der Zukunft
                future_highs = high_values[future_start:future_end]
                max_price = np.max(future_highs)
                pct_change = (max_price - curr_close) / curr_close
                max_pct_change = pct_change
                if pct_change >= TARGET_MOVE_PCT:
                    success = True
                    
            elif event_type == "BREAK_DOWN":
                # Suche nach Preisabfall > 10%
                # Wir schauen auf die LOWS der Zukunft
                future_lows = low_values[future_start:future_end]
                min_price = np.min(future_lows)
                # Bei Short ist Abfall positiv für uns, daher Logik umdrehen
                pct_change = (curr_close - min_price) / curr_close
                max_pct_change = pct_change
                if pct_change >= TARGET_MOVE_PCT:
                    success = True

            results.append({
                "symbol": symbol,
                "time": str(times[i]),
                "type": event_type,
                "close_price": float(curr_close),
                "vol_ratio": float(vol_ratio),
                "success": success,
                "max_change_3d": float(max_pct_change)
            })

    return results

def print_statistics(all_data):
    if not all_data:
        print("Keine Events gefunden.")
        return

    df = pd.DataFrame(all_data)
    
    print("\n" + "="*60)
    print("ERGEBNIS ANALYSE")
    print("="*60)
    
    total_events = len(df)
    total_success = len(df[df['success'] == True])
    global_rate = (total_success / total_events) * 100 if total_events > 0 else 0
    
    print(f"Gesamtanzahl Signale: {total_events}")
    print(f"Erfolgreiche Signale (>10% Move): {total_success}")
    print(f"Globale Erfolgsquote: {global_rate:.2f}%")
    print("-" * 60)
    
    # Volumen Analyse
    # Wir runden das Ratio ab, um Buckets zu bilden (3.5 -> 3.0)
    df['vol_bucket'] = df['vol_ratio'].astype(int)
    
    # Filtern auf Ratios ab 1 bis 20
    print(f"{'Volumen Ratio (x-fach)':<25} | {'Anzahl':<10} | {'Erfolgsquote':<10}")
    print("-" * 60)
    
    for v in range(1, 21):
        # Wir schauen uns alles an, was mindestens Volumen X hatte (oder genau X? Deine Anforderung sagt: "bei 3x, bei 4x")
        # Interpretation: "Bucket X" bedeutet Ratio >= X und < X+1
        bucket_df = df[df['vol_bucket'] == v]
        
        count = len(bucket_df)
        if count > 0:
            wins = len(bucket_df[bucket_df['success'] == True])
            rate = (wins / count) * 100
            print(f"{v}x bis {v+0.99}x Avg Vol     | {count:<10} | {rate:.2f}%")
        else:
            # Optional: Zeigen, dass keine Daten da waren
            pass

    # High Volume Cluster (z.B. alles über 5x zusammengefasst)
    high_vol_df = df[df['vol_ratio'] >= 5]
    if not high_vol_df.empty:
        wins = len(high_vol_df[high_vol_df['success'] == True])
        rate = (wins / len(high_vol_df)) * 100
        print("-" * 60)
        print(f"ZUSAMMENFASSUNG VOL > 5x  | {len(high_vol_df):<10} | {rate:.2f}%")

def main():
    start_time = time.time()
    engine = get_db_engine()
    coins = load_coins()
    
    all_results = []
    
    print(f"Starte Backtest für {len(coins)} Coins...")
    print(f"Logik: Trendbruch (90d Trend) -> Check 3 Tage Zukunft auf 10% Move")
    
    for coin in coins:
        coin_results = analyze_coin(engine, coin)
        all_results.extend(coin_results)
        
    # Ergebnisse speichern
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(all_results, f, indent=4)
        
    print_statistics(all_results)
    
    duration = (time.time() - start_time) / 60
    print(f"\nFertig in {duration:.1f} Minuten. Details in {OUTPUT_FILE}")

if __name__ == "__main__":
    main()

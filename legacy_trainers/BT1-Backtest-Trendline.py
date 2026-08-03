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
LOOKBACK_DAYS = 365       # How far into the past to check?
TREND_WINDOW_HOURS = 90 * 24  # 90 days for the trendline
FUTURE_WINDOW_HOURS = 3 * 24  # check 3 days into the future
TARGET_MOVE_PCT = 0.10    # 10% move
COINS_FILE = 'coins.json'
OUTPUT_FILE = 'trend_backtest_results.json'

def get_db_engine():
    """Creates the DB connection"""
    url = f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
    return create_engine(url)

def load_coins():
    if not os.path.exists(COINS_FILE):
        print(f"File {COINS_FILE} not found. Using default list.")
        return ["ETHUSDT", "BTCUSDT"]
    with open(COINS_FILE, 'r') as f:
        return json.load(f)

def calculate_trend_vectorized(prices, timestamps):
    """
    Calculates slope and intercept using numpy (faster than scipy).
    x are the timestamps (seconds), y are the prices.
    """
    x = timestamps
    y = prices
    A = np.vstack([x, np.ones(len(x))]).T
    m, c = np.linalg.lstsq(A, y, rcond=None)[0]
    return m, c

def analyze_coin(engine, symbol):
    print(f"--> Analysing {symbol}...")

    # 1. Load data (365 days + 90 days lead-in for the first trendline)
    # We need more data than 365 days so that on day 1 of the check we already have a 90-day history.
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
        print(f"Error loading {symbol}: {e}")
        return []

    if df.empty:
        return []

    # Adjust data types
    df['open_time'] = pd.to_datetime(df['open_time'], utc=True)
    # Timestamp in seconds for the regression
    df['ts'] = df['open_time'].apply(lambda x: x.timestamp())
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col])

    # 2. Precompute volume average (SMA 20)
    df['vol_avg_20'] = df['volume'].rolling(window=20).mean()

    results = []

    # We only start the loop once we have enough data for trend (90d) and vol (20h)
    start_index = TREND_WINDOW_HOURS
    # We stop before we run out of "future" (3 days)
    end_index = len(df) - FUTURE_WINDOW_HOURS

    # To avoid computing a regression EVERY hour (takes forever),
    # this could be optimised. But for accuracy we do it step by step.
    # Performance note: this can take a while with many coins.

    # Iteration through the candles (simulated live progression)
    # We use indices for fast access
    ts_values = df['ts'].values
    close_values = df['close'].values
    high_values = df['high'].values
    low_values = df['low'].values
    vol_values = df['volume'].values
    vol_avg_values = df['vol_avg_20'].values
    times = df['open_time'].values

    for i in range(start_index, end_index):
        # The current point in time of the check is "i".
        # The window for the trend is [i - 90 days : i]

        # Data for trend calculation (the last 90 days BEFORE candle i)
        # slicing [start:end] is exclusive of end, so do we take i+1 to include candle i, or i?
        # Logic: "data of the last 365 days". Trend calculation on the closed candles.
        slice_start = i - TREND_WINDOW_HOURS
        slice_end = i

        subset_ts = ts_values[slice_start:slice_end]
        subset_close = close_values[slice_start:slice_end]

        # Calculate trendline
        slope, intercept = calculate_trend_vectorized(subset_close, subset_ts)

        # Trend value for the CURRENT candle (i) and the PREVIOUS one (i-1)
        current_ts = ts_values[i]
        prev_ts = ts_values[i-1]

        trend_val_curr = slope * current_ts + intercept
        trend_val_prev = slope * prev_ts + intercept

        curr_close = close_values[i]
        prev_close = close_values[i-1]

        # Event Detection
        event_type = None

        # Logic: last candle (prev) below trend, current (curr) above trend
        if prev_close < trend_val_prev and curr_close > trend_val_curr:
            event_type = "BREAK_UP"

        # Logic: last candle (prev) above trend, current (curr) below trend
        elif prev_close > trend_val_prev and curr_close < trend_val_curr:
            event_type = "BREAK_DOWN"

        if event_type:
            # Check volume ratio
            curr_vol = vol_values[i]
            avg_vol = vol_avg_values[i]

            if avg_vol == 0 or np.isnan(avg_vol):
                vol_ratio = 0
            else:
                vol_ratio = curr_vol / avg_vol

            # Check the future (next 3 days = 72 hours)
            # Slice: i+1 to i+1+72
            future_start = i + 1
            future_end = i + 1 + FUTURE_WINDOW_HOURS

            success = False
            max_pct_change = 0.0

            if event_type == "BREAK_UP":
                # Look for a price rise > 10%
                # We look at the HIGHS of the future
                future_highs = high_values[future_start:future_end]
                max_price = np.max(future_highs)
                pct_change = (max_price - curr_close) / curr_close
                max_pct_change = pct_change
                if pct_change >= TARGET_MOVE_PCT:
                    success = True

            elif event_type == "BREAK_DOWN":
                # Look for a price drop > 10%
                # We look at the LOWS of the future
                future_lows = low_values[future_start:future_end]
                min_price = np.min(future_lows)
                # For a short, a drop is positive for us, so invert the logic
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
        print("No events found.")
        return

    df = pd.DataFrame(all_data)

    print("\n" + "="*60)
    print("RESULT ANALYSIS")
    print("="*60)

    total_events = len(df)
    total_success = len(df[df['success'] == True])
    global_rate = (total_success / total_events) * 100 if total_events > 0 else 0

    print(f"Total signals: {total_events}")
    print(f"Successful signals (>10% move): {total_success}")
    print(f"Global success rate: {global_rate:.2f}%")
    print("-" * 60)

    # Volume analysis
    # We round the ratio down to form buckets (3.5 -> 3.0)
    df['vol_bucket'] = df['vol_ratio'].astype(int)

    # Filter for ratios from 1 to 20
    print(f"{'Volume Ratio (x-fold)':<25} | {'Count':<10} | {'Success rate':<10}")
    print("-" * 60)

    for v in range(1, 21):
        # We look at everything that had at least volume X (or exactly X? Your requirement says: "at 3x, at 4x")
        # Interpretation: "Bucket X" means ratio >= X and < X+1
        bucket_df = df[df['vol_bucket'] == v]

        count = len(bucket_df)
        if count > 0:
            wins = len(bucket_df[bucket_df['success'] == True])
            rate = (wins / count) * 100
            print(f"{v}x to {v+0.99}x Avg Vol     | {count:<10} | {rate:.2f}%")
        else:
            # Optional: show that there was no data
            pass

    # High volume cluster (e.g. everything above 5x combined)
    high_vol_df = df[df['vol_ratio'] >= 5]
    if not high_vol_df.empty:
        wins = len(high_vol_df[high_vol_df['success'] == True])
        rate = (wins / len(high_vol_df)) * 100
        print("-" * 60)
        print(f"SUMMARY VOL > 5x  | {len(high_vol_df):<10} | {rate:.2f}%")

def main():
    start_time = time.time()
    engine = get_db_engine()
    coins = load_coins()

    all_results = []

    print(f"Starting backtest for {len(coins)} coins...")
    print(f"Logic: trend break (90d trend) -> check 3 days future for 10% move")

    for coin in coins:
        coin_results = analyze_coin(engine, coin)
        all_results.extend(coin_results)

    # Save results
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(all_results, f, indent=4)

    print_statistics(all_results)

    duration = (time.time() - start_time) / 60
    print(f"\nDone in {duration:.1f} minutes. Details in {OUTPUT_FILE}")

if __name__ == "__main__":
    main()

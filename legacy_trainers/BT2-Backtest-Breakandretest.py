import os
import json
import psycopg2
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from datetime import datetime, timedelta

# --- Configuration ---
DB_CONFIG = {
    'dbname': 'cryptodata',
    'user': 'dbfiller',
    'password': os.getenv("DB_PASSWORD", ""),
    'host': 'localhost',
    'port': 5432
}
COINS_FILE = 'coins.json'
OUTPUT_FILE = 'break_retest_analysis.json'

# --- Parameters for the analysis ---
DAYS_TO_LOOK_BACK = 365
PIVOT_WINDOW = 10  # how many candles left/right for a pivot high/low
LEVEL_TOLERANCE = 0.005  # 0.5% tolerance zone around the level
RETEST_LOOKAHEAD = 24  # how many hours after the break may the retest happen?
RESULT_LOOKAHEAD = 12  # how many hours after the retest do we look at the outcome?

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)

def load_coins():
    with open(COINS_FILE, 'r') as f:
        data = json.load(f)
        # Assumption: coins.json is a list ["BTCUSDT", "ETHUSDT", ...]
        # or a dict {"coins": [...]}. Simple handling here:
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and 'coins' in data:
            return data['coins']
        else:
            raise ValueError("coins.json format not recognised.")

def get_ohlcv_data(conn, symbol):
    table_name = f"{symbol}_1h"

    # CHANGE: we cast open_time to TEXT (::text) so pandas doesn't crash.
    # We load it as a string and then convert it in a controlled way.
    query = f"""
        SELECT open_time::text as open_time, open, high, low, close, volume
        FROM "{table_name}"
        WHERE open_time >= NOW() - INTERVAL '{DAYS_TO_LOOK_BACK} days'
        ORDER BY open_time ASC;
    """
    try:
        # ignore UserWarning, or we fix it pragmatically via the text cast
        df = pd.read_sql(query, conn)

        # CHANGE: explicit conversion with utc=True
        df['open_time'] = pd.to_datetime(df['open_time'], utc=True)

        return df
    except Exception as e:
        print(f"Error loading {symbol}: {e}")
        return None


def find_pivot_levels(df, window=PIVOT_WINDOW):
    """Finds local highs and lows as levels."""
    # Find local highs
    df['high_pivot'] = df.iloc[argrelextrema(df['high'].values, np.greater_equal, order=window)[0]]['high']
    # Find local lows
    df['low_pivot'] = df.iloc[argrelextrema(df['low'].values, np.less_equal, order=window)[0]]['low']

    levels = []

    # We only take significant levels into a list
    # For highs
    for idx, row in df.dropna(subset=['high_pivot']).iterrows():
        levels.append({'price': row['high_pivot'], 'type': 'resistance', 'index': idx, 'time': row['open_time']})

    # For lows
    for idx, row in df.dropna(subset=['low_pivot']).iterrows():
        levels.append({'price': row['low_pivot'], 'type': 'support', 'index': idx, 'time': row['open_time']})

    return levels

def analyze_coin(symbol, df):
    levels = find_pivot_levels(df)
    events = []

    # We iterate through the data, but only from a certain point on, so we have "old" levels
    start_index = PIVOT_WINDOW * 2

    for i in range(start_index, len(df) - RESULT_LOOKAHEAD):
        current_candle = df.iloc[i]
        prev_candle = df.iloc[i-1]

        # Only consider levels that are "old" enough (not just formed)
        active_levels = [l for l in levels if l['index'] < (i - PIVOT_WINDOW)]

        for level in active_levels:
            lvl_price = level['price']
            lvl_type = level['type']

            # --- LONG SETUP (resistance break & retest) ---
            if lvl_type == 'resistance':
                # 1. Break: close was below level, now above level
                # or a strong breakout
                break_condition = prev_candle['close'] < lvl_price and current_candle['close'] > lvl_price

                if break_condition:
                    # We have a break. Let's look for a retest in the next X candles
                    # A retest means the price comes back into the level's tolerance zone
                    retest_found = False
                    retest_index = -1

                    for j in range(1, RETEST_LOOKAHEAD + 1):
                        if (i + j) >= len(df) - RESULT_LOOKAHEAD: break

                        future_candle = df.iloc[i + j]

                        # Retest zone: price touches the level from above (low <= level * (1+tol))
                        # but ideally doesn't close far below it (optional)
                        upper_bound = lvl_price * (1 + LEVEL_TOLERANCE)
                        lower_bound = lvl_price * (1 - LEVEL_TOLERANCE)

                        if future_candle['low'] <= upper_bound and future_candle['low'] >= lower_bound:
                            retest_found = True
                            retest_index = i + j

                            # ANALYSIS OF THE OUTCOME AFTER THE RETEST
                            # What happened X hours after the retest?
                            result_candle = df.iloc[retest_index + RESULT_LOOKAHEAD]
                            price_change_pct = (result_candle['close'] - lvl_price) / lvl_price

                            outcome = "neutral"
                            if price_change_pct > 0.02: outcome = "continuation_success" # 2% profit
                            elif price_change_pct < -0.01: outcome = "failed_breakout" # fell below level

                            events.append({
                                'symbol': symbol,
                                'type': 'LONG_BREAK_RETEST',
                                'break_time': str(current_candle['open_time']),
                                'retest_time': str(future_candle['open_time']),
                                'level_price': lvl_price,
                                'outcome_price_change': round(price_change_pct * 100, 2),
                                'outcome_class': outcome
                            })
                            break # retest found, break loop to avoid duplicates

            # --- SHORT SETUP (support break & retest) ---
            elif lvl_type == 'support':
                # 1. Break: close was above level, now below level
                break_condition = prev_candle['close'] > lvl_price and current_candle['close'] < lvl_price

                if break_condition:
                    for j in range(1, RETEST_LOOKAHEAD + 1):
                        if (i + j) >= len(df) - RESULT_LOOKAHEAD: break

                        future_candle = df.iloc[i + j]

                        # Retest zone: price touches the level from below (high >= level * (1-tol))
                        upper_bound = lvl_price * (1 + LEVEL_TOLERANCE)
                        lower_bound = lvl_price * (1 - LEVEL_TOLERANCE)

                        if future_candle['high'] >= lower_bound and future_candle['high'] <= upper_bound:
                            retest_index = i + j

                            # OUTCOME
                            result_candle = df.iloc[retest_index + RESULT_LOOKAHEAD]
                            price_change_pct = (lvl_price - result_candle['close']) / lvl_price # short profit when price falls

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

    print(f"Starting analysis for {len(coins)} coins...")

    for coin in coins:
        print(f"Processing {coin}...")
        df = get_ohlcv_data(conn, coin)

        if df is not None and not df.empty:
            events = analyze_coin(coin, df)
            all_events.extend(events)
            print(f"  -> {len(events)} events found.")
        else:
            print(f"  -> No data.")

    conn.close()

    # Save results
    summary = {
        'total_events': len(all_events),
        'events': all_events
    }

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(summary, f, indent=4)

    print(f"\nAnalysis complete. Results saved to {OUTPUT_FILE}.")

    # Print a small statistic
    df_res = pd.DataFrame(all_events)
    if not df_res.empty:
        print("\n--- Statistics ---")
        print(df_res['outcome_class'].value_counts())
        print("\nAverage profit per outcome:")
        print(df_res.groupby('outcome_class')['outcome_price_change'].mean())

if __name__ == "__main__":
    main()

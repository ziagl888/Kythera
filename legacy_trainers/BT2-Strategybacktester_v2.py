import json
import pandas as pd
import numpy as np
import xgboost as xgb
import matplotlib.pyplot as plt
import seaborn as sns
import warnings 
import time # NEW: For time measurements

warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")


# --- Configuration ---
EVENTS_FILE = 'break_retest_analysis_with_features.json'
LONG_MODEL_FILE = 'bt2_model_LONG.json'
SHORT_MODEL_FILE = 'bt2_model_SHORT.json'

# Manually chosen thresholds
LONG_THRESHOLD = 0.6
SHORT_THRESHOLD = 0.8

SUCCESS_CLASS_IDX = 0 # PLEASE ADJUST IF DIFFERENT DURING TRAINING

def load_data(file_path):
    """Loads the events from the JSON file and ensures the data types."""
    print(f"Loading events from: {file_path}")
    start_load_time = time.time()
    with open(file_path, 'r') as f:
        data = json.load(f)
    
    df_events = pd.DataFrame(data['events'])
    
    df_events['retest_time'] = pd.to_datetime(df_events['retest_time'])

    non_feature_cols = ['symbol', 'type', 'break_time', 'retest_time', 'level_price', 'outcome_price_change', 'outcome_class']
    feature_columns = [col for col in df_events.columns if col not in non_feature_cols]

    print(f"Converting feature columns to numeric types (before: {df_events[feature_columns].dtypes.apply(lambda x: x.name).tolist()})")
    for col in feature_columns:
        df_events[col] = pd.to_numeric(df_events[col], errors='coerce').fillna(0.0).astype(float)
    print(f"After conversion: {df_events[feature_columns].dtypes.apply(lambda x: x.name).tolist()}")

    end_load_time = time.time()
    print(f"Loading and preparing the data completed in {end_load_time - start_load_time:.2f} seconds.")
    
    return df_events

def load_model(file_path):
    """Loads a trained XGBoost model."""
    print(f"Loading model from: {file_path}")
    model = xgb.XGBClassifier() 
    model.load_model(file_path) 
    return model

def backtest_strategy(df_events):
    """
    Simulates the strategy based on the models and thresholds
    and calculates the performance.
    Uses batch prediction for maximum efficiency.
    """
    start_backtest_time = time.time()
    print("Starting backtest strategy with batch prediction...")

    model_long = load_model(LONG_MODEL_FILE)
    model_short = load_model(SHORT_MODEL_FILE)

    features_to_drop = [
        'symbol', 'type', 'break_time', 'retest_time', 'level_price',
        'outcome_price_change', 'outcome_class'
    ]
    feature_columns = [col for col in df_events.columns if col not in features_to_drop]

    # --- Initialization of the new columns with NaN ---
    df_events['predicted_proba_success'] = np.nan
    df_events['threshold_used'] = np.nan

    # 1. Prepare the feature data for the prediction (ensure types)
    X_all_features = df_events[feature_columns].astype(float) # Guarantees float dtype

    # 2. LONG Trades
    long_mask = df_events['type'] == 'LONG_BREAK_RETEST'
    if long_mask.any(): # Only predict if there are LONG events
        print(f"Starting batch prediction for {long_mask.sum()} LONG events...")
        # Only pass the features of the LONG events to the LONG model
        long_pred_proba = model_long.predict_proba(X_all_features[long_mask])
        df_events.loc[long_mask, 'predicted_proba_success'] = long_pred_proba[:, SUCCESS_CLASS_IDX]
        df_events.loc[long_mask, 'threshold_used'] = LONG_THRESHOLD

    # 3. SHORT Trades
    short_mask = df_events['type'] == 'SHORT_BREAK_RETEST'
    if short_mask.any(): # Only predict if there are SHORT events
        print(f"Starting batch prediction for {short_mask.sum()} SHORT events...")
        # Only pass the features of the SHORT events to the SHORT model
        short_pred_proba = model_short.predict_proba(X_all_features[short_mask])
        df_events.loc[short_mask, 'predicted_proba_success'] = short_pred_proba[:, SUCCESS_CLASS_IDX]
        df_events.loc[short_mask, 'threshold_used'] = SHORT_THRESHOLD

    # --- Filter the trades based on the thresholds ---
    # Only consider events that actually have a predicted_proba_success value (i.e. were LONG/SHORT events)
    # and whose predicted_proba_success >= threshold_used.
    print("Filtering trades based on thresholds...")
    df_trades = df_events[
        (df_events['predicted_proba_success'].notna()) & # Ensure that a prediction was made at all
        (df_events['predicted_proba_success'] >= df_events['threshold_used'])
    ].copy() # .copy() prevents SettingWithCopyWarning

    end_backtest_time = time.time()
    print(f"Backtest strategy completed in {end_backtest_time - start_backtest_time:.2f} seconds.")

    return df_trades


def analyze_performance(df_trades):
    """Calculates and prints the performance metrics."""
    if df_trades.empty:
        print("No trades selected based on the thresholds.")
        return

    print("\n--- Performance Analysis ---")

    # Overall performance
    # NAMES CHANGED HERE
    total_profit_pct = df_trades['outcome_price_change'].sum()
    num_trades = len(df_trades)
    num_winning_trades = df_trades[df_trades['outcome_class'] == 'continuation_success'].shape[0]
    win_rate = (num_winning_trades / num_trades) * 100 if num_trades > 0 else 0
    avg_profit_per_trade = df_trades['outcome_price_change'].mean()

    print(f"\nTotal number of trades: {num_trades}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Average profit per trade: {avg_profit_per_trade:.2f}%")
    print(f"Total profit (sum of percentage changes): {total_profit_pct:.2f}%")

    # Performance by type (LONG/SHORT)
    print("\n--- Performance by trade type ---")
    for trade_type in ['LONG_BREAK_RETEST', 'SHORT_BREAK_RETEST']:
        df_type_trades = df_trades[df_trades['type'] == trade_type]
        if not df_type_trades.empty:
            type_num_trades = len(df_type_trades)
            # NAMES CHANGED HERE
            type_num_winning_trades = df_type_trades[df_type_trades['outcome_class'] == 'continuation_success'].shape[0]
            type_win_rate = (type_num_winning_trades / type_num_trades) * 100
            type_avg_profit = df_type_trades['outcome_price_change'].mean()
            type_total_profit = df_type_trades['outcome_price_change'].sum()

            print(f"\nType: {trade_type}")
            print(f"  Number of trades: {type_num_trades}")
            print(f"  Win Rate: {type_win_rate:.2f}%")
            print(f"  Average profit per trade: {type_avg_profit:.2f}%")
            print(f"  Total profit: {type_total_profit:.2f}%")
        else:
            print(f"\nType: {trade_type} - No trades executed.")

    # Visualization of the cumulative performance
    # Here the names are already correct, since they come from df_events
    df_trades['retest_time'] = pd.to_datetime(df_trades['retest_time'])
    df_trades = df_trades.sort_values(by='retest_time')
    df_trades['cumulative_profit'] = df_trades['outcome_price_change'].cumsum()

    plt.figure(figsize=(14, 7))
    plt.plot(df_trades['retest_time'], df_trades['cumulative_profit'])
    plt.title('Cumulative profit of the strategy')
    plt.xlabel('Date')
    plt.ylabel('Cumulative profit (%)')
    plt.grid(True)
    plt.show()


def main():
    df_events = load_data(EVENTS_FILE)
    
    df_events = df_events.sort_values(by='retest_time').reset_index(drop=True)

    df_selected_trades = backtest_strategy(df_events)
    analyze_performance(df_selected_trades)

if __name__ == "__main__":
    main()

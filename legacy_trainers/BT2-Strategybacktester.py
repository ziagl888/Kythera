import json
import pandas as pd
import numpy as np
import xgboost as xgb
import matplotlib.pyplot as plt
import seaborn as sns
import warnings 

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
    with open(file_path, 'r') as f:
        data = json.load(f)
    
    df_events = pd.DataFrame(data['events'])
    
    df_events['retest_time'] = pd.to_datetime(df_events['retest_time'])

    non_feature_cols = ['symbol', 'type', 'break_time', 'retest_time', 'level_price', 'outcome_price_change', 'outcome_class']
    feature_columns = [col for col in df_events.columns if col not in non_feature_cols]

    for col in feature_columns:
        df_events[col] = pd.to_numeric(df_events[col], errors='coerce').fillna(0.0).astype(float)
    
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
    """
    model_long = load_model(LONG_MODEL_FILE)
    model_short = load_model(SHORT_MODEL_FILE)

    features_to_drop = [
        'symbol', 'type', 'break_time', 'retest_time', 'level_price',
        'outcome_price_change', 'outcome_class'
    ]
    feature_columns = [col for col in df_events.columns if col not in features_to_drop]

    trade_signals = []

    for index, event in df_events.iterrows():
        # --- NEW: More robust creation of X_event with guaranteed numeric types ---
        # 1. Select the feature values as a NumPy array from the current row
        feature_values = event[feature_columns].values
        # 2. Create a new DataFrame with these values and the correct column names
        #    and ensure that the dtypes are float
        X_event = pd.DataFrame([feature_values], columns=feature_columns, dtype=float)
        # --- END NEW ---

        prediction_proba = None
        threshold = None

        if event['type'] == 'LONG_BREAK_RETEST':
            prediction_proba = model_long.predict_proba(X_event)[0, SUCCESS_CLASS_IDX]
            threshold = LONG_THRESHOLD
        elif event['type'] == 'SHORT_BREAK_RETEST':
            prediction_proba = model_short.predict_proba(X_event)[0, SUCCESS_CLASS_IDX]
            threshold = SHORT_THRESHOLD
        
        if prediction_proba is not None and threshold is not None:
            if prediction_proba >= threshold:
                trade_signals.append({
                    'symbol': event['symbol'],
                    'type': event['type'],
                    'retest_time': event['retest_time'],
                    'level_price': event['level_price'],
                    'predicted_proba_success': prediction_proba,
                    'actual_outcome_class': event['outcome_class'],
                    'actual_outcome_price_change': event['outcome_price_change'],
                    'is_trade_taken': True
                })

    df_trades = pd.DataFrame(trade_signals)
    return df_trades

def analyze_performance(df_trades):
    """Calculates and prints the performance metrics."""
    if df_trades.empty:
        print("No trades selected based on the thresholds.")
        return

    print("\n--- Performance Analysis ---")

    total_profit_pct = df_trades['actual_outcome_price_change'].sum()
    num_trades = len(df_trades)
    num_winning_trades = df_trades[df_trades['actual_outcome_class'] == 'continuation_success'].shape[0]
    win_rate = (num_winning_trades / num_trades) * 100 if num_trades > 0 else 0
    avg_profit_per_trade = df_trades['actual_outcome_price_change'].mean()

    print(f"\nTotal number of trades: {num_trades}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Average profit per trade: {avg_profit_per_trade:.2f}%")
    print(f"Total profit (sum of percentage changes): {total_profit_pct:.2f}%")

    print("\n--- Performance by trade type ---")
    for trade_type in ['LONG_BREAK_RETEST', 'SHORT_BREAK_RETEST']:
        df_type_trades = df_trades[df_trades['type'] == trade_type]
        if not df_type_trades.empty:
            type_num_trades = len(df_type_trades)
            type_num_winning_trades = df_type_trades[df_type_trades['actual_outcome_class'] == 'continuation_success'].shape[0]
            type_win_rate = (type_num_winning_trades / type_num_trades) * 100
            type_avg_profit = df_type_trades['actual_outcome_price_change'].mean()
            type_total_profit = df_type_trades['actual_outcome_price_change'].sum()

            print(f"\nType: {trade_type}")
            print(f"  Number of trades: {type_num_trades}")
            print(f"  Win Rate: {type_win_rate:.2f}%")
            print(f"  Average profit per trade: {type_avg_profit:.2f}%")
            print(f"  Total profit: {type_total_profit:.2f}%")
        else:
            print(f"\nType: {trade_type} - No trades executed.")

    df_trades['retest_time'] = pd.to_datetime(df_trades['retest_time'])
    df_trades = df_trades.sort_values(by='retest_time')
    df_trades['cumulative_profit'] = df_trades['actual_outcome_price_change'].cumsum()

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

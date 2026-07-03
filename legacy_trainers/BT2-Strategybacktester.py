import json
import pandas as pd
import numpy as np
import xgboost as xgb
import matplotlib.pyplot as plt
import seaborn as sns
import warnings 

warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")


# --- Konfiguration ---
EVENTS_FILE = 'break_retest_analysis_with_features.json'
LONG_MODEL_FILE = 'bt2_model_LONG.json'
SHORT_MODEL_FILE = 'bt2_model_SHORT.json'

# Manuell gewählte Thresholds
LONG_THRESHOLD = 0.6
SHORT_THRESHOLD = 0.8

SUCCESS_CLASS_IDX = 0 # BITTE ANPASSEN, WENN BEIM TRAINING ANDERS

def load_data(file_path):
    """Lädt die Events aus der JSON-Datei und stellt die Datentypen sicher."""
    print(f"Lade Events von: {file_path}")
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
    """Lädt ein trainiertes XGBoost-Modell."""
    print(f"Lade Modell von: {file_path}")
    model = xgb.XGBClassifier() 
    model.load_model(file_path) 
    return model

def backtest_strategy(df_events):
    """
    Simuliert die Strategie basierend auf den Modellen und Thresholds
    und berechnet die Performance.
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
        # --- NEU: Robustere Erstellung von X_event mit garantiert numerischen Typen ---
        # 1. Wähle die Feature-Werte als NumPy-Array aus der aktuellen Zeile
        feature_values = event[feature_columns].values
        # 2. Erstelle einen neuen DataFrame mit diesen Werten und den korrekten Spaltennamen
        #    und stelle sicher, dass die DTypes float sind
        X_event = pd.DataFrame([feature_values], columns=feature_columns, dtype=float)
        # --- ENDE NEU ---

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
    """Berechnet und gibt die Performance-Metriken aus."""
    if df_trades.empty:
        print("Keine Trades basierend auf den Thresholds ausgewählt.")
        return

    print("\n--- Performance Analyse ---")

    total_profit_pct = df_trades['actual_outcome_price_change'].sum()
    num_trades = len(df_trades)
    num_winning_trades = df_trades[df_trades['actual_outcome_class'] == 'continuation_success'].shape[0]
    win_rate = (num_winning_trades / num_trades) * 100 if num_trades > 0 else 0
    avg_profit_per_trade = df_trades['actual_outcome_price_change'].mean()

    print(f"\nGesamtzahl der Trades: {num_trades}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Durchschnittlicher Profit pro Trade: {avg_profit_per_trade:.2f}%")
    print(f"Gesamtprofit (Summe der prozentualen Veränderungen): {total_profit_pct:.2f}%")

    print("\n--- Performance nach Trade-Typ ---")
    for trade_type in ['LONG_BREAK_RETEST', 'SHORT_BREAK_RETEST']:
        df_type_trades = df_trades[df_trades['type'] == trade_type]
        if not df_type_trades.empty:
            type_num_trades = len(df_type_trades)
            type_num_winning_trades = df_type_trades[df_type_trades['actual_outcome_class'] == 'continuation_success'].shape[0]
            type_win_rate = (type_num_winning_trades / type_num_trades) * 100
            type_avg_profit = df_type_trades['actual_outcome_price_change'].mean()
            type_total_profit = df_type_trades['actual_outcome_price_change'].sum()

            print(f"\nTyp: {trade_type}")
            print(f"  Anzahl Trades: {type_num_trades}")
            print(f"  Win Rate: {type_win_rate:.2f}%")
            print(f"  Durchschnittlicher Profit pro Trade: {type_avg_profit:.2f}%")
            print(f"  Gesamtprofit: {type_total_profit:.2f}%")
        else:
            print(f"\nTyp: {trade_type} - Keine Trades ausgeführt.")

    df_trades['retest_time'] = pd.to_datetime(df_trades['retest_time'])
    df_trades = df_trades.sort_values(by='retest_time')
    df_trades['cumulative_profit'] = df_trades['actual_outcome_price_change'].cumsum()

    plt.figure(figsize=(14, 7))
    plt.plot(df_trades['retest_time'], df_trades['cumulative_profit'])
    plt.title('Kumulierter Profit der Strategie')
    plt.xlabel('Datum')
    plt.ylabel('Kumulierter Profit (%)')
    plt.grid(True)
    plt.show()

def main():
    df_events = load_data(EVENTS_FILE)
    
    df_events = df_events.sort_values(by='retest_time').reset_index(drop=True)

    df_selected_trades = backtest_strategy(df_events)
    analyze_performance(df_selected_trades)

if __name__ == "__main__":
    main()

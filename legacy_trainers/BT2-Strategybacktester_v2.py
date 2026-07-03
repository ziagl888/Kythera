import json
import pandas as pd
import numpy as np
import xgboost as xgb
import matplotlib.pyplot as plt
import seaborn as sns
import warnings 
import time # NEU: Für Zeitmessungen

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
    start_load_time = time.time()
    with open(file_path, 'r') as f:
        data = json.load(f)
    
    df_events = pd.DataFrame(data['events'])
    
    df_events['retest_time'] = pd.to_datetime(df_events['retest_time'])

    non_feature_cols = ['symbol', 'type', 'break_time', 'retest_time', 'level_price', 'outcome_price_change', 'outcome_class']
    feature_columns = [col for col in df_events.columns if col not in non_feature_cols]

    print(f"Konvertiere Feature-Spalten in numerische Typen (vorher: {df_events[feature_columns].dtypes.apply(lambda x: x.name).tolist()})")
    for col in feature_columns:
        df_events[col] = pd.to_numeric(df_events[col], errors='coerce').fillna(0.0).astype(float)
    print(f"Nach Konvertierung: {df_events[feature_columns].dtypes.apply(lambda x: x.name).tolist()}")

    end_load_time = time.time()
    print(f"Laden und Vorbereiten der Daten abgeschlossen in {end_load_time - start_load_time:.2f} Sekunden.")
    
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
    Verwendet Batch-Prediction für maximale Effizienz.
    """
    start_backtest_time = time.time()
    print("Starte Backtest-Strategie mit Batch-Prediction...")

    model_long = load_model(LONG_MODEL_FILE)
    model_short = load_model(SHORT_MODEL_FILE)

    features_to_drop = [
        'symbol', 'type', 'break_time', 'retest_time', 'level_price',
        'outcome_price_change', 'outcome_class'
    ]
    feature_columns = [col for col in df_events.columns if col not in features_to_drop]

    # --- Initialisierung der neuen Spalten mit NaN ---
    df_events['predicted_proba_success'] = np.nan
    df_events['threshold_used'] = np.nan

    # 1. Bereite die Feature-Daten für die Prediction vor (stelle Typen sicher)
    X_all_features = df_events[feature_columns].astype(float) # Garantiert float DType

    # 2. LONG Trades
    long_mask = df_events['type'] == 'LONG_BREAK_RETEST'
    if long_mask.any(): # Nur vorhersagen, wenn es LONG Events gibt
        print(f"Starte Batch-Prediction für {long_mask.sum()} LONG Events...")
        # Nur die Features der LONG Events an das LONG-Modell übergeben
        long_pred_proba = model_long.predict_proba(X_all_features[long_mask])
        df_events.loc[long_mask, 'predicted_proba_success'] = long_pred_proba[:, SUCCESS_CLASS_IDX]
        df_events.loc[long_mask, 'threshold_used'] = LONG_THRESHOLD
    
    # 3. SHORT Trades
    short_mask = df_events['type'] == 'SHORT_BREAK_RETEST'
    if short_mask.any(): # Nur vorhersagen, wenn es SHORT Events gibt
        print(f"Starte Batch-Prediction für {short_mask.sum()} SHORT Events...")
        # Nur die Features der SHORT Events an das SHORT-Modell übergeben
        short_pred_proba = model_short.predict_proba(X_all_features[short_mask])
        df_events.loc[short_mask, 'predicted_proba_success'] = short_pred_proba[:, SUCCESS_CLASS_IDX]
        df_events.loc[short_mask, 'threshold_used'] = SHORT_THRESHOLD
    
    # --- Filter die Trades basierend auf den Thresholds ---
    # Nur Events betrachten, die tatsächlich einen predicted_proba_success Wert haben (also LONG/SHORT Events waren)
    # und deren predicted_proba_success >= threshold_used ist.
    print("Filtere Trades basierend auf Thresholds...")
    df_trades = df_events[
        (df_events['predicted_proba_success'].notna()) & # Sicherstellen, dass überhaupt eine Prediction gemacht wurde
        (df_events['predicted_proba_success'] >= df_events['threshold_used'])
    ].copy() # .copy() verhindert SettingWithCopyWarning

    end_backtest_time = time.time()
    print(f"Backtest-Strategie abgeschlossen in {end_backtest_time - start_backtest_time:.2f} Sekunden.")

    return df_trades


def analyze_performance(df_trades):
    """Berechnet und gibt die Performance-Metriken aus."""
    if df_trades.empty:
        print("Keine Trades basierend auf den Thresholds ausgewählt.")
        return

    print("\n--- Performance Analyse ---")

    # Gesamte Performance
    # HIER DIE NAMEN GEÄNDERT
    total_profit_pct = df_trades['outcome_price_change'].sum()
    num_trades = len(df_trades)
    num_winning_trades = df_trades[df_trades['outcome_class'] == 'continuation_success'].shape[0]
    win_rate = (num_winning_trades / num_trades) * 100 if num_trades > 0 else 0
    avg_profit_per_trade = df_trades['outcome_price_change'].mean()

    print(f"\nGesamtzahl der Trades: {num_trades}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Durchschnittlicher Profit pro Trade: {avg_profit_per_trade:.2f}%")
    print(f"Gesamtprofit (Summe der prozentualen Veränderungen): {total_profit_pct:.2f}%")

    # Performance nach Typ (LONG/SHORT)
    print("\n--- Performance nach Trade-Typ ---")
    for trade_type in ['LONG_BREAK_RETEST', 'SHORT_BREAK_RETEST']:
        df_type_trades = df_trades[df_trades['type'] == trade_type]
        if not df_type_trades.empty:
            type_num_trades = len(df_type_trades)
            # HIER DIE NAMEN GEÄNDERT
            type_num_winning_trades = df_type_trades[df_type_trades['outcome_class'] == 'continuation_success'].shape[0]
            type_win_rate = (type_num_winning_trades / type_num_trades) * 100
            type_avg_profit = df_type_trades['outcome_price_change'].mean()
            type_total_profit = df_type_trades['outcome_price_change'].sum()

            print(f"\nTyp: {trade_type}")
            print(f"  Anzahl Trades: {type_num_trades}")
            print(f"  Win Rate: {type_win_rate:.2f}%")
            print(f"  Durchschnittlicher Profit pro Trade: {type_avg_profit:.2f}%")
            print(f"  Gesamtprofit: {type_total_profit:.2f}%")
        else:
            print(f"\nTyp: {trade_type} - Keine Trades ausgeführt.")

    # Visualisierung der kumulierten Performance
    # Hier sind die Namen bereits korrekt, da sie aus df_events kommen
    df_trades['retest_time'] = pd.to_datetime(df_trades['retest_time'])
    df_trades = df_trades.sort_values(by='retest_time')
    df_trades['cumulative_profit'] = df_trades['outcome_price_change'].cumsum()

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

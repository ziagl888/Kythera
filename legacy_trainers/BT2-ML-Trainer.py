import json
import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.model_selection import TimeSeriesSplit, GridSearchCV
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import classification_report, confusion_matrix, precision_score, recall_score, f1_score
import matplotlib.pyplot as plt
import seaborn as sns
import multiprocessing as mp
import time

# --- Konfiguration ---
INPUT_FILE = 'break_retest_analysis_with_features.json'

# Hyperparameter Grid
# Da wir sequentiell trainieren (aber mit Multi-Threading im Tree-Building),
# halten wir das Grid fokussiert.
param_grid = {
    'n_estimators': [100, 200],      # Anzahl Bäume
    'learning_rate': [0.05, 0.1],    # Wie schnell lernt es
    'max_depth': [4, 6],             # Tiefe der Bäume (Komplexität)
    'subsample': [0.8],              # Gegen Overfitting
    'colsample_bytree': [0.8],       # Gegen Overfitting
    # 'scale_pos_weight': [1, 5]     # Optional: Falls das Modell Success ignoriert, hier hochdrehen (nur Binary)
}

# Mindestanzahl Trades für Threshold-Optimierung
MIN_TRADES_FOR_CONSIDERATION = 100 

def load_and_prepare_data(input_file):
    print("Lade JSON Datei... (das kann bei 2.7 Mio Events kurz dauern)")
    with open(input_file, 'r') as f:
        data = json.load(f)
    
    df_events = pd.DataFrame(data['events'])
    
    # Konvertiere Zeitstempel
    df_events['retest_time'] = pd.to_datetime(df_events['retest_time'])
    
    # Unnötige Spalten für das Training entfernen
    features_to_drop = [
        'symbol', 'type', 'break_time', 'retest_time', 'level_price',
        'outcome_price_change', 'outcome_class'
    ]
    
    X = df_events.drop(columns=features_to_drop)
    y = df_events['outcome_class']
    
    # Encoding der Klassen (Neutral, Success, Fail -> 0, 1, 2)
    le = LabelEncoder()
    y_encoded = le.fit_transform(y)
    
    class_mapping = dict(zip(le.classes_, le.transform(le.classes_)))
    print(f"Klassen-Mapping: {class_mapping}")
    
    if 'continuation_success' not in class_mapping:
        raise ValueError("Klasse 'continuation_success' fehlt!")
    
    success_class_idx = class_mapping['continuation_success']
    
    return X, y_encoded, df_events, le, success_class_idx

def train_and_evaluate_model_with_gridsearch(X, y_encoded, df_events_filtered, le, success_class_idx, trade_type_name, param_grid):
    print(f"\n{'='*60}")
    print(f"START TRAINING: {trade_type_name}")
    print(f"{'='*60}")
    
    # TimeSeriesSplit stellt sicher, dass wir nicht in die Zukunft schauen
    tscv = TimeSeriesSplit(n_splits=3) # 3 Splits reichen bei der Datenmenge oft und sparen Zeit
    
    # XGBoost Estimator
    # n_jobs=-1 nutzt ALLE CPU-Kerne für das Training des Modells (Internes C++ Threading)
    # Das ist stabil auf Windows!
    estimator = xgb.XGBClassifier(
        objective='multi:softprob',
        num_class=len(le.classes_),
        eval_metric='mlogloss',
        use_label_encoder=False,
        random_state=42,
        tree_method='hist', # Sehr schnell für große Datenmengen
        n_jobs=-1           # WICHTIG: Interne Parallelisierung an
    )

    # GridSearchCV
    # n_jobs=1 verhindert den joblib/multiprocessing Absturz.
    # Wir verlassen uns auf die interne Power von XGBoost.
    grid_search = GridSearchCV(
        estimator=estimator,
        param_grid=param_grid,
        scoring='f1_macro', # Macro gewichtet kleine Klassen (Success) stärker als 'weighted'
        cv=tscv,
        n_jobs=1,           # WICHTIG: Externe Parallelisierung aus (verhindert Crash)
        verbose=1
    )

    start_time = time.time()
    print(f"Starte GridSearch für {trade_type_name}...")
    print(f"Anzahl Samples: {len(X)}")
    
    grid_search.fit(X, y_encoded)
    
    duration = time.time() - start_time
    print(f"GridSearch fertig in {duration/60:.2f} Minuten.")
    print(f"Beste Parameter: {grid_search.best_params_}")
    print(f"Bester Score (f1_macro): {grid_search.best_score_:.4f}")

    best_model = grid_search.best_estimator_

    # Vorhersage auf den ganzen Daten (um Threshold zu finden)
    # (Ideal wäre ein separates Hold-Out Set, aber TS-Split hat das validiert)
    print("Erstelle Vorhersagen für Threshold-Optimierung...")
    y_pred_proba = best_model.predict_proba(X)
    
    # Wir interessieren uns nur für die Wahrscheinlichkeit von "Success"
    all_y_pred_proba_success = y_pred_proba[:, success_class_idx]
    
    # Echte Profite für die Berechnung der Profitabilität
    all_outcome_price_changes = df_events_filtered['outcome_price_change'].values
    all_y_true = y_encoded

    # --- Threshold Optimierung ---
    print("\nOptimiere Threshold (Wahrscheinlichkeitsschwelle)...")
    thresholds = np.linspace(0.3, 0.98, 100) # Wir suchen ab 30% Wahrscheinlichkeit
    results = []

    for threshold in thresholds:
        # Welche Trades würden wir bei diesem Threshold nehmen?
        trade_indices = np.where(all_y_pred_proba_success >= threshold)[0]
        
        if len(trade_indices) < MIN_TRADES_FOR_CONSIDERATION:
            continue

        # Echte Ergebnisse dieser Trades
        selected_profits = all_outcome_price_changes[trade_indices]
        selected_labels = all_y_true[trade_indices]

        # Gewinnrate
        wins = np.sum(selected_labels == success_class_idx)
        win_rate = (wins / len(trade_indices)) * 100
        
        # Profitabilität
        avg_profit = np.mean(selected_profits)
        total_profit_sum = np.sum(selected_profits)

        results.append({
            'threshold': threshold,
            'num_trades': len(trade_indices),
            'win_rate': win_rate,
            'avg_profit_per_trade': avg_profit,
            'total_profit_score': total_profit_sum # Einfache Metrik: Gesamtprofit
        })

    if not results:
        print("Kein Threshold gefunden, der genügend Trades liefert.")
        return best_model, None, None

    results_df = pd.DataFrame(results)
    
    # Wir suchen den Threshold mit der besten Win-Rate, 
    # solange der Avg Profit positiv ist.
    best_row = results_df.loc[results_df['win_rate'].idxmax()]
    
    print(f"\n--- ERGEBNIS {trade_type_name} ---")
    print(f"Bester Threshold: {best_row['threshold']:.4f}")
    print(f"Erwartete Win-Rate: {best_row['win_rate']:.2f}%")
    print(f"Anzahl Trades (im Dataset): {int(best_row['num_trades'])}")
    print(f"Durchschnittsprofit pro Trade: {best_row['avg_profit_per_trade']:.2f}%")

    # Plot
    plt.figure(figsize=(10, 5))
    plt.plot(results_df['threshold'], results_df['win_rate'], label='Win Rate %')
    plt.plot(results_df['threshold'], results_df['avg_profit_per_trade'], label='Avg Profit %')
    plt.axvline(best_row['threshold'], color='red', linestyle='--', label='Best Threshold')
    plt.title(f"{trade_type_name} Optimierung")
    plt.xlabel("Threshold")
    plt.legend()
    plt.grid(True)
    plt.show()
    
    # Modell speichern (Dateiname z.B. "model_LONG.json")
    model_filename = f"bt2_model_{trade_type_name}.json"
    best_model.save_model(model_filename)
    print(f"Modell gespeichert als: {model_filename}")
    
    # Optional: Threshold auch speichern (z.B. in einer kleinen Textdatei)
    with open(f"bt2_threshold_{trade_type_name}.txt", "w") as f:
        f.write(str(best_row['threshold']))
    
    return best_model, best_row['threshold'], best_row

def main():
    print("Starte ML Trainer (Stable Version)...")
    X, y_encoded, df_events, le, success_class_idx = load_and_prepare_data(INPUT_FILE)
    
    # Feature Importance Helper
    def show_feature_importance(model, feature_names):
        importance = model.feature_importances_
        feat_imp = pd.DataFrame({'feature': feature_names, 'importance': importance})
        feat_imp = feat_imp.sort_values('importance', ascending=False).head(15)
        print("\nTop 15 Wichtigste Features:")
        print(feat_imp)

    # --- LONG MODEL ---
    mask_long = df_events['type'] == 'LONG_BREAK_RETEST'
    if mask_long.sum() > 100:
        X_long = X[mask_long]
        y_long = y_encoded[mask_long]
        df_long = df_events[mask_long]
        
        model_long, th_long, _ = train_and_evaluate_model_with_gridsearch(
            X_long, y_long, df_long, le, success_class_idx, "LONG", param_grid
        )
        if model_long:
            show_feature_importance(model_long, X.columns)
    else:
        print("Zu wenige LONG Events.")

    # --- SHORT MODEL ---
    mask_short = df_events['type'] == 'SHORT_BREAK_RETEST'
    if mask_short.sum() > 100:
        X_short = X[mask_short]
        y_short = y_encoded[mask_short]
        df_short = df_events[mask_short]
        
        model_short, th_short, _ = train_and_evaluate_model_with_gridsearch(
            X_short, y_short, df_short, le, success_class_idx, "SHORT", param_grid
        )
        if model_short:
            show_feature_importance(model_short, X.columns)
    else:
        print("Zu wenige SHORT Events.")

if __name__ == "__main__":
    mp.freeze_support()
    main()

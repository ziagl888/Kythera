import pandas as pd
import numpy as np
import joblib
from sklearn.model_selection import train_test_split
from sklearn.metrics import precision_score, recall_score, accuracy_score

# ========================= CONFIG =========================
DATA_FILE = 'ml_training_data.csv'
MODEL_FILE = 'trend_prediction_model.joblib'

def optimize_threshold():
    print("--- Starte Threshold Optimierung ---")
    
    # 1. Daten und Modell laden
    try:
        df = pd.read_csv(DATA_FILE)
        model = joblib.load(MODEL_FILE)
    except FileNotFoundError:
        print("Datei nicht gefunden. Bitte erst Training ausführen.")
        return

    # 2. Daten vorbereiten (identisch zum Training)
    df['event_type'] = df['event_type'].map({'UP': 1, 'DOWN': 0})
    features = [
        'event_type', 'vol_ratio', 'rsi', 'atr_pct', 'dist_ema200', 'slope_trend', 'hour_of_day',
        'dist_close_ema9_pct', 'dist_ema9_ema21_pct', 'dist_close_kama9_pct',
        'MACD_Line', 'MACD_Signal', 'TSI_Line', 'TSI_Signal',
        'dist_close_bb_lower_pct', 'dist_close_bb_upper_pct', 'bb_position_relative',
        'dist_close_dc_lower_pct', 'dist_close_dc_upper_pct', 'dc_position_relative'
    ]
    
    # Cleaning
    combined_df = pd.concat([df[features], df['label_success']], axis=1).dropna()
    X = combined_df[features]
    y = combined_df['label_success']
    
    # WICHTIG: Gleicher Random State wie beim Training, damit wir die Testdaten nutzen!
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    
    # 3. Wahrscheinlichkeiten vorhersagen
    # Wir holen uns nicht die Klasse (0/1), sondern die Wahrscheinlichkeit (0.0 bis 1.0)
    probs = model.predict_proba(X_test)[:, 1]

    print(f"\n{'Threshold':<10} | {'Precision (Win Rate)':<20} | {'Trades (Anzahl)':<15} | {'Recall':<10}")
    print("-" * 65)

    best_threshold = 0.5
    best_precision = 0.0

    # Wir testen Thresholds von 0.50 bis 0.95
    for thresh in np.arange(0.5, 0.96, 0.05):
        # Wenn Wahrscheinlichkeit > Threshold, dann Vorhersage = 1, sonst 0
        y_pred_custom = (probs >= thresh).astype(int)
        
        prec = precision_score(y_test, y_pred_custom, zero_division=0)
        rec = recall_score(y_test, y_pred_custom, zero_division=0)
        num_trades = np.sum(y_pred_custom)
        
        print(f"{thresh:.2f}       | {prec*100:.2f}%               | {num_trades:<15} | {rec*100:.2f}%")
        
        # Wir suchen den Punkt, wo wir noch min. 50 Trades im Testset haben, aber max Precision
        if num_trades > 50 and prec > best_precision:
            best_precision = prec
            best_threshold = thresh

    print("-" * 65)
    print(f"Empfehlung: Setze den Threshold in deinem Live-Bot auf {best_threshold:.2f}")
    print(f"Damit erwartete Win-Rate: {best_precision*100:.2f}%")

if __name__ == "__main__":
    optimize_threshold()

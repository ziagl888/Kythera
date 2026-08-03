import pandas as pd
import numpy as np
import joblib
from sklearn.model_selection import train_test_split
from sklearn.metrics import precision_score, recall_score, accuracy_score

# ========================= CONFIG =========================
DATA_FILE = 'ml_training_data.csv'
MODEL_FILE = 'trend_prediction_model.joblib'

def optimize_threshold():
    print("--- Start threshold optimization ---")
    
    # 1. Load data and model
    try:
        df = pd.read_csv(DATA_FILE)
        model = joblib.load(MODEL_FILE)
    except FileNotFoundError:
        print("File not found. Please run training first.")
        return

    # 2. Prepare data (identical to training)
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
    
    # IMPORTANT: same random state as in training, so we use the test data!
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    # 3. Predict probabilities
    # We don't get the class (0/1), but the probability (0.0 to 1.0)
    probs = model.predict_proba(X_test)[:, 1]

    print(f"\n{'Threshold':<10} | {'Precision (Win Rate)':<20} | {'Trades (Count)':<15} | {'Recall':<10}")
    print("-" * 65)

    best_threshold = 0.5
    best_precision = 0.0

    # We test thresholds from 0.50 to 0.95
    for thresh in np.arange(0.5, 0.96, 0.05):
        # If probability > threshold, then prediction = 1, else 0
        y_pred_custom = (probs >= thresh).astype(int)

        prec = precision_score(y_test, y_pred_custom, zero_division=0)
        rec = recall_score(y_test, y_pred_custom, zero_division=0)
        num_trades = np.sum(y_pred_custom)

        print(f"{thresh:.2f}       | {prec*100:.2f}%               | {num_trades:<15} | {rec*100:.2f}%")

        # We look for the point where we still have at least 50 trades in the test set, but max precision
        if num_trades > 50 and prec > best_precision:
            best_precision = prec
            best_threshold = thresh

    print("-" * 65)
    print(f"Recommendation: set the threshold in your live bot to {best_threshold:.2f}")
    print(f"Expected win rate with that: {best_precision*100:.2f}%")

if __name__ == "__main__":
    optimize_threshold()

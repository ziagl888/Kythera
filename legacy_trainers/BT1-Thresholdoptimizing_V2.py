import pandas as pd
import numpy as np
import joblib
from sklearn.model_selection import train_test_split
from sklearn.metrics import precision_score, recall_score, accuracy_score
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========================= CONFIG =========================
DATA_FILE = 'ml_training_data.csv'
LONG_MODEL_FILE = 'long_trend_prediction_model.joblib'
SHORT_MODEL_FILE = 'short_trend_prediction_model.joblib'

def optimize_threshold_for_model(model, X_test, y_test, model_name):
    """Optimises the threshold for a specific model."""
    logger.info(f"\n--- Starting threshold optimisation for {model_name} model ---")

    # Predict probabilities
    probs = model.predict_proba(X_test)[:, 1]

    logger.info(f"\n{'Threshold':<10} | {'Precision (Win Rate)':<20} | {'Trades (Count)':<15} | {'Recall':<10}")
    logger.info("-" * 65)

    best_threshold = 0.5
    best_precision = 0.0

    # We test thresholds from 0.50 to 0.95
    for thresh in np.arange(0.5, 0.96, 0.05):
        # If probability > threshold, then prediction = 1, otherwise 0
        y_pred_custom = (probs >= thresh).astype(int)

        # Make sure trades were made, to avoid division by zero
        num_trades = np.sum(y_pred_custom)
        if num_trades == 0:
            prec = 0.0
            rec = 0.0
        else:
            prec = precision_score(y_test, y_pred_custom, zero_division=0)
            rec = recall_score(y_test, y_pred_custom, zero_division=0)
        
        logger.info(f"{thresh:.2f}       | {prec*100:.2f}%               | {num_trades:<15} | {rec*100:.2f}%")
        
        # We look for the point where we still have at least 50 trades in the test set, but max precision
        # Criterion adjusted: more than 10 trades is already relevant too
        if num_trades > 10 and prec > best_precision:
            best_precision = prec
            best_threshold = thresh

    logger.info("-" * 65)
    logger.info(f"Recommendation for {model_name}: set the threshold in your live bot to {best_threshold:.2f}")
    logger.info(f"Expected win rate for {model_name}: {best_precision*100:.2f}%")
    return best_threshold, best_precision

def main():
    logger.info("--- Starting separate threshold optimisation for LONG and SHORT models ---")

    # 1. Load data and models
    try:
        df = pd.read_csv(DATA_FILE)
        long_model = joblib.load(LONG_MODEL_FILE)
        short_model = joblib.load(SHORT_MODEL_FILE)
    except FileNotFoundError:
        logger.error("Error: data or model file not found. Please run training first.")
        return

    # 2. Prepare data (identical to training)
    # Remove 'event_type_numeric' and 'event_type' from the features for the models
    features = [
        'vol_ratio', 'rsi', 'atr_pct', 'dist_ema200', 'slope_trend', 'hour_of_day',
        'dist_close_ema9_pct', 'dist_ema9_ema21_pct', 'dist_close_kama9_pct',
        'MACD_Line', 'MACD_Signal', 'TSI_Line', 'TSI_Signal',
        'dist_close_bb_lower_pct', 'dist_close_bb_upper_pct', 'bb_position_relative',
        'dist_close_dc_lower_pct', 'dist_close_dc_upper_pct', 'dc_position_relative'
    ]
    
    # Cleaning
    # We still need the event_type column here for the split
    df_cleaned = df[features + ['label_success', 'event_type']].dropna()

    # 3. Split data into Long and Short (test sets!)
    df_long_test = df_cleaned[df_cleaned['event_type'] == 'UP'].copy()
    df_short_test = df_cleaned[df_cleaned['event_type'] == 'DOWN'].copy()

    X_long_test_all = df_long_test[features]
    y_long_test_all = df_long_test['label_success']

    X_short_test_all = df_short_test[features]
    y_short_test_all = df_short_test['label_success']
    
    # IMPORTANT: Repeated train-test split, to evaluate only the test data
    # that the model has never seen during training.
    # We use the same random_state here as in training.
    _, X_long_test, _, y_long_test = train_test_split(X_long_test_all, y_long_test_all, test_size=0.2, random_state=42, stratify=y_long_test_all)
    _, X_short_test, _, y_short_test = train_test_split(X_short_test_all, y_short_test_all, test_size=0.2, random_state=42, stratify=y_short_test_all)

    # 4. Threshold optimisation for LONG model
    long_thresh, long_prec = optimize_threshold_for_model(long_model, X_long_test, y_long_test, "LONG")

    # 5. Threshold optimisation for SHORT model
    short_thresh, short_prec = optimize_threshold_for_model(short_model, X_short_test, y_short_test, "SHORT")

    logger.info(f"\n--- Final recommendations for the live bot ---")
    logger.info(f"LONG trade trigger threshold: {long_thresh:.2f} (expected win rate: {long_prec*100:.2f}%)")
    logger.info(f"SHORT trade trigger threshold: {short_thresh:.2f} (expected win rate: {short_prec*100:.2f}%)")

if __name__ == "__main__":
    main()
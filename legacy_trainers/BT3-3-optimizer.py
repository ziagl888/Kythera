import pandas as pd
import numpy as np
import joblib
from sklearn.model_selection import train_test_split
from sklearn.metrics import precision_score, recall_score
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

DATA_FILE = 'reversion_ml_training_data.csv'
LONG_MODEL_FILE = 'long_reversion_model.joblib'
SHORT_MODEL_FILE = 'short_reversion_model.joblib'

def optimize_threshold(model, X_test, y_test, model_name):
    logger.info(f"\n--- Threshold Optimierung: {model_name} ---")
    probs = model.predict_proba(X_test)[:, 1]

    best_threshold, best_precision = 0.5, 0.0

    logger.info(f"{'Thresh':<8} | {'Win Rate':<10} | {'Trades':<8}")
    for thresh in np.arange(0.5, 0.96, 0.05):
        y_pred_custom = (probs >= thresh).astype(int)
        num_trades = np.sum(y_pred_custom)
        
        if num_trades == 0: continue
            
        prec = precision_score(y_test, y_pred_custom, zero_division=0)
        logger.info(f"{thresh:.2f}     | {prec*100:6.2f}%   | {num_trades}")
        
        if num_trades > 5 and prec > best_precision:
            best_precision = prec
            best_threshold = thresh

    logger.info(f"Empfohlen für {model_name}: {best_threshold:.2f} (Erwartete Win-Rate: {best_precision*100:.2f}%)")
    return best_threshold

def main():
    try:
        df = pd.read_csv(DATA_FILE)
        long_model = joblib.load(LONG_MODEL_FILE)
        short_model = joblib.load(SHORT_MODEL_FILE)
    except Exception as e:
        logger.error(f"Dateien nicht gefunden: {e}")
        return

    features = [
        'dist_to_trend', 'rsi', 'atr_pct', 'dist_ema200', 'slope_trend',
        'MACD_Line', 'MACD_Signal', 'TSI_Line', 'TSI_Signal'
    ]
    
    df_cleaned = df[features + ['label_success', 'event_type']].dropna()
    
    df_long = df_cleaned[df_cleaned['event_type'] == 'REVERSION_UP']
    df_short = df_cleaned[df_cleaned['event_type'] == 'REVERSION_DOWN']

    _, X_long_test, _, y_long_test = train_test_split(df_long[features], df_long['label_success'], test_size=0.2, random_state=42, stratify=df_long['label_success'])
    _, X_short_test, _, y_short_test = train_test_split(df_short[features], df_short['label_success'], test_size=0.2, random_state=42, stratify=df_short['label_success'])

    optimize_threshold(long_model, X_long_test, y_long_test, "LONG REVERSION")
    optimize_threshold(short_model, X_short_test, y_short_test, "SHORT REVERSION")

if __name__ == "__main__":
    main()
import pandas as pd
import numpy as np
import joblib
from sklearn.model_selection import train_test_split
from sklearn.metrics import precision_score, recall_score, accuracy_score
import logging

# Konfiguriere Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========================= CONFIG =========================
DATA_FILE = 'ml_training_data.csv'
LONG_MODEL_FILE = 'long_trend_prediction_model.joblib'
SHORT_MODEL_FILE = 'short_trend_prediction_model.joblib'

def optimize_threshold_for_model(model, X_test, y_test, model_name):
    """Optimiert den Threshold für ein spezifisches Modell."""
    logger.info(f"\n--- Starte Threshold Optimierung für {model_name} Modell ---")
    
    # Wahrscheinlichkeiten vorhersagen
    probs = model.predict_proba(X_test)[:, 1]

    logger.info(f"\n{'Threshold':<10} | {'Precision (Win Rate)':<20} | {'Trades (Anzahl)':<15} | {'Recall':<10}")
    logger.info("-" * 65)

    best_threshold = 0.5
    best_precision = 0.0

    # Wir testen Thresholds von 0.50 bis 0.95
    for thresh in np.arange(0.5, 0.96, 0.05):
        # Wenn Wahrscheinlichkeit > Threshold, dann Vorhersage = 1, sonst 0
        y_pred_custom = (probs >= thresh).astype(int)
        
        # Sicherstellen, dass Trades gemacht wurden, um Division durch Null zu vermeiden
        num_trades = np.sum(y_pred_custom)
        if num_trades == 0:
            prec = 0.0
            rec = 0.0
        else:
            prec = precision_score(y_test, y_pred_custom, zero_division=0)
            rec = recall_score(y_test, y_pred_custom, zero_division=0)
        
        logger.info(f"{thresh:.2f}       | {prec*100:.2f}%               | {num_trades:<15} | {rec*100:.2f}%")
        
        # Wir suchen den Punkt, wo wir noch min. 50 Trades im Testset haben, aber max Precision
        # Kriterium angepasst: mehr als 10 Trades sind auch schon relevant
        if num_trades > 10 and prec > best_precision:
            best_precision = prec
            best_threshold = thresh

    logger.info("-" * 65)
    logger.info(f"Empfehlung für {model_name}: Setze den Threshold in deinem Live-Bot auf {best_threshold:.2f}")
    logger.info(f"Damit erwartete Win-Rate für {model_name}: {best_precision*100:.2f}%")
    return best_threshold, best_precision

def main():
    logger.info("--- Starte separate Threshold Optimierung für LONG und SHORT Modelle ---")
    
    # 1. Daten und Modelle laden
    try:
        df = pd.read_csv(DATA_FILE)
        long_model = joblib.load(LONG_MODEL_FILE)
        short_model = joblib.load(SHORT_MODEL_FILE)
    except FileNotFoundError:
        logger.error("Fehler: Daten- oder Modell-Datei nicht gefunden. Bitte zuerst Training ausführen.")
        return

    # 2. Daten vorbereiten (identisch zum Training)
    # Entferne 'event_type_numeric' und 'event_type' aus den Features für die Modelle
    features = [
        'vol_ratio', 'rsi', 'atr_pct', 'dist_ema200', 'slope_trend', 'hour_of_day',
        'dist_close_ema9_pct', 'dist_ema9_ema21_pct', 'dist_close_kama9_pct',
        'MACD_Line', 'MACD_Signal', 'TSI_Line', 'TSI_Signal',
        'dist_close_bb_lower_pct', 'dist_close_bb_upper_pct', 'bb_position_relative',
        'dist_close_dc_lower_pct', 'dist_close_dc_upper_pct', 'dc_position_relative'
    ]
    
    # Cleaning
    # Wir brauchen die event_type Spalte hier noch zur Aufteilung
    df_cleaned = df[features + ['label_success', 'event_type']].dropna()
    
    # 3. Daten aufteilen in Long und Short (Testsets!)
    df_long_test = df_cleaned[df_cleaned['event_type'] == 'UP'].copy()
    df_short_test = df_cleaned[df_cleaned['event_type'] == 'DOWN'].copy()

    X_long_test_all = df_long_test[features]
    y_long_test_all = df_long_test['label_success']

    X_short_test_all = df_short_test[features]
    y_short_test_all = df_short_test['label_success']
    
    # WICHTIG: Erneuter Train-Test-Split, um nur die Testdaten zu bewerten,
    # die das Modell im Training noch nie gesehen hat.
    # Wir nutzen hier den gleichen random_state wie im Training.
    _, X_long_test, _, y_long_test = train_test_split(X_long_test_all, y_long_test_all, test_size=0.2, random_state=42, stratify=y_long_test_all)
    _, X_short_test, _, y_short_test = train_test_split(X_short_test_all, y_short_test_all, test_size=0.2, random_state=42, stratify=y_short_test_all)

    # 4. Threshold Optimierung für LONG Modell
    long_thresh, long_prec = optimize_threshold_for_model(long_model, X_long_test, y_long_test, "LONG")
    
    # 5. Threshold Optimierung für SHORT Modell
    short_thresh, short_prec = optimize_threshold_for_model(short_model, X_short_test, y_short_test, "SHORT")
    
    logger.info(f"\n--- Endgültige Empfehlungen für den Live-Bot ---")
    logger.info(f"LONG Trade Trigger Threshold: {long_thresh:.2f} (erwartete Win Rate: {long_prec*100:.2f}%)")
    logger.info(f"SHORT Trade Trigger Threshold: {short_thresh:.2f} (erwartete Win Rate: {short_prec*100:.2f}%)")

if __name__ == "__main__":
    main()
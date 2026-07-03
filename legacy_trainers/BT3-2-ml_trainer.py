import pandas as pd
import numpy as np
import joblib
from sklearn.model_selection import train_test_split, GridSearchCV
from sklearn.metrics import classification_report, roc_auc_score, confusion_matrix, make_scorer
import xgboost as xgb
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# ========================= KONFIGURATION =========================
DATA_FILE = 'reversion_ml_training_data.csv' 
LONG_MODEL_FILE = 'long_reversion_model.joblib' 
SHORT_MODEL_FILE = 'short_reversion_model.joblib'

PARAM_GRID = {
    'n_estimators': [100, 200, 300],
    'learning_rate': [0.05, 0.1],
    'max_depth': [3, 5],
    'subsample': [0.7, 0.9],
    'colsample_bytree': [0.7, 0.9],
    'gamma': [0, 0.1]
}
CV_FOLDS = 3

def train_and_evaluate_model(X, y, model_type="GENERAL"):
    logger.info(f"\n--- Training {model_type} Reversion Modell ---")
    if len(X) < 50:
        logger.warning(f"Zu wenig Daten ({len(X)}) für {model_type}. Überspringe.")
        return None, None, None

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    
    scale_pos_weight_value = len(y_train[y_train == 0]) / max(1, len(y_train[y_train == 1]))
    
    xgb_model = xgb.XGBClassifier(
        objective='binary:logistic', eval_metric='logloss',
        random_state=42, scale_pos_weight=scale_pos_weight_value
    )

    grid_search = GridSearchCV(
        estimator=xgb_model, param_grid=PARAM_GRID,
        scoring=make_scorer(roc_auc_score), cv=CV_FOLDS, verbose=1, n_jobs=1
    )

    grid_search.fit(X_train, y_train)
    best_model = grid_search.best_estimator_

    y_pred = best_model.predict(X_test)
    y_proba = best_model.predict_proba(X_test)[:, 1]

    roc_auc = roc_auc_score(y_test, y_proba)
    logger.info(f"ROC AUC Score: {roc_auc:.4f}")
    
    cm = confusion_matrix(y_test, y_pred)
    logger.info(f"Confusion Matrix:\n{cm}")

    return best_model, grid_search.best_params_, roc_auc

def main():
    try:
        df = pd.read_csv(DATA_FILE)
    except FileNotFoundError:
        logger.error(f"Fehler: {DATA_FILE} fehlt. Führe erst reversion_datagrepper.py aus.")
        return

    features = [
        'dist_to_trend', 'rsi', 'atr_pct', 'dist_ema200', 'slope_trend',
        'MACD_Line', 'MACD_Signal', 'TSI_Line', 'TSI_Signal'
    ]
    
    df_cleaned = df[features + ['label_success', 'event_type']].dropna()
    
    df_long = df_cleaned[df_cleaned['event_type'] == 'REVERSION_UP']
    df_short = df_cleaned[df_cleaned['event_type'] == 'REVERSION_DOWN']

    long_model, _, _ = train_and_evaluate_model(df_long[features], df_long['label_success'], "LONG (UP)")
    if long_model:
        joblib.dump(long_model, LONG_MODEL_FILE)
        
    short_model, _, _ = train_and_evaluate_model(df_short[features], df_short['label_success'], "SHORT (DOWN)")
    if short_model:
        joblib.dump(short_model, SHORT_MODEL_FILE)

if __name__ == "__main__":
    main()
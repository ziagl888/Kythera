import pandas as pd
import numpy as np
import joblib
from sklearn.model_selection import train_test_split, GridSearchCV # NEU: GridSearchCV für Tuning
from sklearn.metrics import classification_report, roc_auc_score, confusion_matrix, make_scorer # make_scorer für GridSearchCV
import xgboost as xgb
import matplotlib.pyplot as plt
import seaborn as sns
import time
import logging

# Konfiguriere Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========================= KONFIGURATION =========================
DATA_FILE = 'ml_training_data.csv' 
LONG_MODEL_FILE = 'long_trend_prediction_model.joblib' 
SHORT_MODEL_FILE = 'short_trend_prediction_model.joblib'

# ========================= HYPERPARAMETER TUNING CONFIG =========================
# Dies ist das Gitter von Hyperparametern, das GridSearchCV durchsuchen wird.
# ACHTUNG: Ein größerer Suchraum erhöht die Rechenzeit exponentiell!
# Starte mit kleineren Bereichen/weniger Optionen und erweitere bei Bedarf.
PARAM_GRID = {
    'n_estimators': [100, 200, 300], # Anzahl der Boosting-Runden (Bäume)
    'learning_rate': [0.05, 0.1],     # Schrittgröße beim Update der Gewichte
    'max_depth': [3, 5],              # Maximale Tiefe eines Baumes
    'subsample': [0.7, 0.9],          # Anteil der Samples pro Baum
    'colsample_bytree': [0.7, 0.9],   # Anteil der Features pro Baum
    'gamma': [0, 0.1]                 # Minimaler Verlust-Reduktion für einen Split
}

# Cross-Validation Folds
CV_FOLDS = 3 # Anzahl der Folds für Cross-Validation (3 oder 5 sind Standard)

def train_and_evaluate_model(X, y, model_type="GENERAL"):
    """Trainiert und evaluiert ein XGBoost-Modell mit Hyperparameter-Tuning."""
    logger.info(f"\n--- Starte ML Modell Training für {model_type} Signale ---")

    if len(X) == 0:
        logger.warning(f"Keine Daten für {model_type} Modelltraining.")
        return None, None, None

    # Daten in Trainings- und Testsets aufteilen
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    logger.info(f"Trainingsset Größe für {model_type}: {len(X_train)} | Testset Größe: {len(X_test)}")
    logger.info(f"Verteilung der Erfolg-Labels im Trainingsset für {model_type}:\n{y_train.value_counts(normalize=True)}")
    logger.info(f"Verteilung der Erfolg-Labels im Testset für {model_type}:\n{y_test.value_counts(normalize=True)}")

    # Klassenungleichgewicht ausgleichen
    scale_pos_weight_value = len(y_train[y_train == 0]) / len(y_train[y_train == 1])
    logger.info(f"Scale_pos_weight für {model_type}: {scale_pos_weight_value:.2f}")

    # XGBoost Basis-Modell
    xgb_model = xgb.XGBClassifier(
        objective='binary:logistic',
        eval_metric='logloss',
        random_state=42,
        scale_pos_weight=scale_pos_weight_value,
        use_label_encoder=False # Für neuere Versionen nicht mehr nötig, aber schadet nicht.
    )

    # GridSearchCV für Hyperparameter-Tuning
    # Wir optimieren auf ROC AUC
    scorer = make_scorer(roc_auc_score)

    grid_search = GridSearchCV(
        estimator=xgb_model,
        param_grid=PARAM_GRID,
        scoring=scorer,
        cv=CV_FOLDS,
        verbose=1, # Zeigt Fortschritt an
        n_jobs=1 # Nutzt alle CPU-Kerne
    )

    logger.info(f"Starte Hyperparameter-Tuning für {model_type} Modell...")
    start_time_tune = time.time()
    grid_search.fit(X_train, y_train)
    end_time_tune = time.time()
    logger.info(f"Tuning abgeschlossen in {(end_time_tune - start_time_tune)/60:.1f} Minuten.")

    best_model = grid_search.best_estimator_
    logger.info(f"Beste Hyperparameter für {model_type}: {grid_search.best_params_}")
    logger.info(f"Bester ROC AUC Score auf Validierung (CV) für {model_type}: {grid_search.best_score_:.4f}")

    # Modell evaluieren auf dem Testset
    logger.info(f"\n--- Modell Evaluation für {model_type} auf dem Testset ---")
    y_pred = best_model.predict(X_test)
    y_proba = best_model.predict_proba(X_test)[:, 1]

    logger.info("\nKlassifikationsbericht (Precision, Recall, F1-Score):")
    logger.info(classification_report(y_test, y_pred))

    roc_auc = roc_auc_score(y_test, y_proba)
    logger.info(f"ROC AUC Score (Maß für Trennschärfe des Modells): {roc_auc:.4f}")
    
    cm = confusion_matrix(y_test, y_pred)
    logger.info("\nKonfusionsmatrix:")
    logger.info(f"\n{cm}")
    logger.info(f"  True Positives (TP): {cm[1,1]} -> Modell sagt Erfolg voraus, und es war ein Erfolg.")
    logger.info(f"  False Positives (FP): {cm[0,1]} -> Modell sagt Erfolg voraus, aber es war ein Misserfolg (Fehlalarm).")
    logger.info(f"  True Negatives (TN): {cm[0,0]} -> Modell sagt Misserfolg voraus, und es war ein Misserfolg.")
    logger.info(f"  False Negatives (FN): {cm[1,0]} -> Modell sagt Misserfolg voraus, aber es war ein Erfolg (verpasste Chance).")

    # Feature Importance anzeigen
    logger.info(f"\n--- Feature Importance für {model_type} ---")
    feature_importances = pd.Series(best_model.feature_importances_, index=X_train.columns)
    top_features = feature_importances.sort_values(ascending=False)
    logger.info(f"\n{top_features}")

    # Plotten der Feature Importance
    plt.figure(figsize=(12, max(7, len(X_train.columns) * 0.4)))
    sns.barplot(x=top_features.values, y=top_features.index, palette='viridis')
    plt.title(f'XGBoost Feature Importance ({model_type})')
    plt.xlabel('Wichtigkeit')
    plt.ylabel('Features')
    plt.tight_layout()
    plt.savefig(f'{model_type.lower()}_feature_importance.png') # Speichern des Plots
    # plt.show() # Optional, nur anzeigen wenn du es visuell sehen willst

    return best_model, grid_search.best_params_, roc_auc

def main():
    start_total_time = time.time()
    logger.info("--- Starte ML Modell Training mit separaten Modellen und Tuning ---")

    # 1. Daten laden
    try:
        df = pd.read_csv(DATA_FILE)
        logger.info(f"Daten erfolgreich aus {DATA_FILE} geladen. {len(df)} Zeilen gefunden.")
    except FileNotFoundError:
        logger.error(f"Fehler: {DATA_FILE} nicht gefunden. Bitte zuerst das Daten-Sammel-Skript ausführen.")
        return

    # 2. Daten vorbereiten (Feature Engineering & Label Definition)
    # Konvertiere 'event_type' in numerische Werte (für XGBoost)
    df['event_type_numeric'] = df['event_type'].map({'UP': 1, 'DOWN': 0})
    
    features = [
        # WICHTIG: 'event_type_numeric' wird HIER noch nicht aus den Features entfernt,
        # da es für die Aufteilung der Daten notwendig ist und erst später für die
        # Trainings-Features der Einzelmodelle eliminiert wird.
        'event_type_numeric',       
        'vol_ratio',                
        'rsi',                      
        'atr_pct',                  
        'dist_ema200',              
        'slope_trend',              
        'hour_of_day',              
        'dist_close_ema9_pct',      
        'dist_ema9_ema21_pct',      
        'dist_close_kama9_pct',     
        'MACD_Line',                
        'MACD_Signal',              
        'TSI_Line',                 
        'TSI_Signal',               
        'dist_close_bb_lower_pct',  
        'dist_close_bb_upper_pct',  
        'bb_position_relative',     
        'dist_close_dc_lower_pct',  
        'dist_close_dc_upper_pct',  
        'dc_position_relative'      
    ]
    
    # Cleaning
    initial_rows = len(df)
    # Hier wichtig: Den `event_type` für die Aufteilung VOR der NaN-Bereinigung behalten
    # Auch 'event_type_numeric' muss hier dabei sein, da es in 'features' ist
    df_cleaned = df[features + ['label_success', 'event_type']].dropna() 
    
    if len(df_cleaned) < initial_rows:
        logger.warning(f"Achtung: {initial_rows - len(df_cleaned)} Zeilen mit NaN-Werten entfernt. Verbleibende Samples: {len(df_cleaned)}")
        
    if len(df_cleaned) == 0:
        logger.error("Keine gültigen Daten nach NaN-Entfernung übrig. Training abgebrochen.")
        return

    logger.info(f"Anzahl verwendeter Features: {len(features)}")
    logger.info(f"Verwendete Features: {features}")
    logger.info(f"Anzahl Samples nach Bereinigung: {len(df_cleaned)}")

    # 3. Daten aufteilen in Long und Short
    df_long = df_cleaned[df_cleaned['event_type'] == 'UP'].copy()
    df_short = df_cleaned[df_cleaned['event_type'] == 'DOWN'].copy()

    # Entferne 'event_type_numeric' aus den Features, die an die einzelnen Modelle gehen
    # Es ist redundant, da das Modell nun nur noch UP- oder DOWN-Signale sieht.
    long_features = [f for f in features if f != 'event_type_numeric']
    short_features = [f for f in features if f != 'event_type_numeric']

    X_long = df_long[long_features]
    y_long = df_long['label_success']

    X_short = df_short[short_features]
    y_short = df_short['label_success']
    
    # 4. Long Modell trainieren
    long_model, long_best_params, long_roc_auc = train_and_evaluate_model(X_long, y_long, "LONG")
    if long_model:
        joblib.dump(long_model, LONG_MODEL_FILE)
        logger.info(f"Long Modell erfolgreich gespeichert als '{LONG_MODEL_FILE}'")
        
    # 5. Short Modell trainieren
    short_model, short_best_params, short_roc_auc = train_and_evaluate_model(X_short, y_short, "SHORT")
    if short_model:
        joblib.dump(short_model, SHORT_MODEL_FILE)
        logger.info(f"Short Modell erfolgreich gespeichert als '{SHORT_MODEL_FILE}'")
    
    total_duration = (time.time() - start_total_time) / 60
    logger.info(f"\n--- Gesamtes Training Abgeschlossen in {total_duration:.1f} Minuten ---")
    
    logger.info("\n--- Zusammenfassung der Ergebnisse ---")
    if long_model:
        logger.info(f"LONG Modell: Best Params: {long_best_params} | ROC AUC: {long_roc_auc:.4f}")
    if short_model:
        logger.info(f"SHORT Modell: Best Params: {short_best_params} | ROC AUC: {short_roc_auc:.4f}")

if __name__ == "__main__":
    main()

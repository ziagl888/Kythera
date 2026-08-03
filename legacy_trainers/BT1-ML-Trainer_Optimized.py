import pandas as pd
import numpy as np
import joblib
from sklearn.model_selection import train_test_split, GridSearchCV # NEW: GridSearchCV for tuning
from sklearn.metrics import classification_report, roc_auc_score, confusion_matrix, make_scorer # make_scorer for GridSearchCV
import xgboost as xgb
import matplotlib.pyplot as plt
import seaborn as sns
import time
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========================= CONFIGURATION =========================
DATA_FILE = 'ml_training_data.csv'
LONG_MODEL_FILE = 'long_trend_prediction_model.joblib'
SHORT_MODEL_FILE = 'short_trend_prediction_model.joblib'

# ========================= HYPERPARAMETER TUNING CONFIG =========================
# This is the grid of hyperparameters that GridSearchCV will search.
# CAUTION: A larger search space increases compute time exponentially!
# Start with smaller ranges/fewer options and expand as needed.
PARAM_GRID = {
    'n_estimators': [100, 200, 300], # number of boosting rounds (trees)
    'learning_rate': [0.05, 0.1],     # step size when updating the weights
    'max_depth': [3, 5],              # maximum depth of a tree
    'subsample': [0.7, 0.9],          # fraction of samples per tree
    'colsample_bytree': [0.7, 0.9],   # fraction of features per tree
    'gamma': [0, 0.1]                 # minimum loss reduction for a split
}

# Cross-validation folds
CV_FOLDS = 3 # number of folds for cross-validation (3 or 5 is standard)

def train_and_evaluate_model(X, y, model_type="GENERAL"):
    """Trains and evaluates an XGBoost model with hyperparameter tuning."""
    logger.info(f"\n--- Starting ML model training for {model_type} signals ---")

    if len(X) == 0:
        logger.warning(f"No data for {model_type} model training.")
        return None, None, None

    # Split data into training and test sets
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    logger.info(f"Training set size for {model_type}: {len(X_train)} | Test set size: {len(X_test)}")
    logger.info(f"Distribution of success labels in the training set for {model_type}:\n{y_train.value_counts(normalize=True)}")
    logger.info(f"Distribution of success labels in the test set for {model_type}:\n{y_test.value_counts(normalize=True)}")

    # Balance class imbalance
    scale_pos_weight_value = len(y_train[y_train == 0]) / len(y_train[y_train == 1])
    logger.info(f"Scale_pos_weight for {model_type}: {scale_pos_weight_value:.2f}")

    # XGBoost base model
    xgb_model = xgb.XGBClassifier(
        objective='binary:logistic',
        eval_metric='logloss',
        random_state=42,
        scale_pos_weight=scale_pos_weight_value,
        use_label_encoder=False # No longer needed for newer versions, but doesn't hurt.
    )

    # GridSearchCV for hyperparameter tuning
    # We optimise on ROC AUC
    scorer = make_scorer(roc_auc_score)

    grid_search = GridSearchCV(
        estimator=xgb_model,
        param_grid=PARAM_GRID,
        scoring=scorer,
        cv=CV_FOLDS,
        verbose=1, # shows progress
        n_jobs=1 # uses all CPU cores
    )

    logger.info(f"Starting hyperparameter tuning for {model_type} model...")
    start_time_tune = time.time()
    grid_search.fit(X_train, y_train)
    end_time_tune = time.time()
    logger.info(f"Tuning completed in {(end_time_tune - start_time_tune)/60:.1f} minutes.")

    best_model = grid_search.best_estimator_
    logger.info(f"Best hyperparameters for {model_type}: {grid_search.best_params_}")
    logger.info(f"Best ROC AUC score on validation (CV) for {model_type}: {grid_search.best_score_:.4f}")

    # Evaluate model on the test set
    logger.info(f"\n--- Model evaluation for {model_type} on the test set ---")
    y_pred = best_model.predict(X_test)
    y_proba = best_model.predict_proba(X_test)[:, 1]

    logger.info("\nClassification report (precision, recall, F1 score):")
    logger.info(classification_report(y_test, y_pred))

    roc_auc = roc_auc_score(y_test, y_proba)
    logger.info(f"ROC AUC score (measure of the model's discriminative power): {roc_auc:.4f}")

    cm = confusion_matrix(y_test, y_pred)
    logger.info("\nConfusion matrix:")
    logger.info(f"\n{cm}")
    logger.info(f"  True Positives (TP): {cm[1,1]} -> model predicts success, and it was a success.")
    logger.info(f"  False Positives (FP): {cm[0,1]} -> model predicts success, but it was a failure (false alarm).")
    logger.info(f"  True Negatives (TN): {cm[0,0]} -> model predicts failure, and it was a failure.")
    logger.info(f"  False Negatives (FN): {cm[1,0]} -> model predicts failure, but it was a success (missed opportunity).")

    # Show feature importance
    logger.info(f"\n--- Feature importance for {model_type} ---")
    feature_importances = pd.Series(best_model.feature_importances_, index=X_train.columns)
    top_features = feature_importances.sort_values(ascending=False)
    logger.info(f"\n{top_features}")

    # Plot the feature importance
    plt.figure(figsize=(12, max(7, len(X_train.columns) * 0.4)))
    sns.barplot(x=top_features.values, y=top_features.index, palette='viridis')
    plt.title(f'XGBoost Feature Importance ({model_type})')
    plt.xlabel('Importance')
    plt.ylabel('Features')
    plt.tight_layout()
    plt.savefig(f'{model_type.lower()}_feature_importance.png') # save the plot
    # plt.show() # optional, only show if you want to see it visually

    return best_model, grid_search.best_params_, roc_auc

def main():
    start_total_time = time.time()
    logger.info("--- Starting ML model training with separate models and tuning ---")

    # 1. Load data
    try:
        df = pd.read_csv(DATA_FILE)
        logger.info(f"Data successfully loaded from {DATA_FILE}. {len(df)} rows found.")
    except FileNotFoundError:
        logger.error(f"Error: {DATA_FILE} not found. Please run the data collection script first.")
        return

    # 2. Prepare data (feature engineering & label definition)
    # Convert 'event_type' into numeric values (for XGBoost)
    df['event_type_numeric'] = df['event_type'].map({'UP': 1, 'DOWN': 0})

    features = [
        # IMPORTANT: 'event_type_numeric' is not yet removed from the features HERE,
        # since it is needed for splitting the data and is only eliminated later
        # for the training features of the individual models.
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
    # Important here: keep `event_type` for the split BEFORE the NaN cleanup
    # 'event_type_numeric' must also be included here, since it is in 'features'
    df_cleaned = df[features + ['label_success', 'event_type']].dropna()

    if len(df_cleaned) < initial_rows:
        logger.warning(f"Note: {initial_rows - len(df_cleaned)} rows with NaN values removed. Remaining samples: {len(df_cleaned)}")

    if len(df_cleaned) == 0:
        logger.error("No valid data left after NaN removal. Training aborted.")
        return

    logger.info(f"Number of features used: {len(features)}")
    logger.info(f"Features used: {features}")
    logger.info(f"Number of samples after cleaning: {len(df_cleaned)}")

    # 3. Split data into long and short
    df_long = df_cleaned[df_cleaned['event_type'] == 'UP'].copy()
    df_short = df_cleaned[df_cleaned['event_type'] == 'DOWN'].copy()

    # Remove 'event_type_numeric' from the features passed to the individual models
    # It is redundant, since the model now only ever sees UP or DOWN signals.
    long_features = [f for f in features if f != 'event_type_numeric']
    short_features = [f for f in features if f != 'event_type_numeric']

    X_long = df_long[long_features]
    y_long = df_long['label_success']

    X_short = df_short[short_features]
    y_short = df_short['label_success']

    # 4. Train the long model
    long_model, long_best_params, long_roc_auc = train_and_evaluate_model(X_long, y_long, "LONG")
    if long_model:
        joblib.dump(long_model, LONG_MODEL_FILE)
        logger.info(f"Long model successfully saved as '{LONG_MODEL_FILE}'")

    # 5. Train the short model
    short_model, short_best_params, short_roc_auc = train_and_evaluate_model(X_short, y_short, "SHORT")
    if short_model:
        joblib.dump(short_model, SHORT_MODEL_FILE)
        logger.info(f"Short model successfully saved as '{SHORT_MODEL_FILE}'")

    total_duration = (time.time() - start_total_time) / 60
    logger.info(f"\n--- Entire training completed in {total_duration:.1f} minutes ---")

    logger.info("\n--- Summary of results ---")
    if long_model:
        logger.info(f"LONG model: Best Params: {long_best_params} | ROC AUC: {long_roc_auc:.4f}")
    if short_model:
        logger.info(f"SHORT model: Best Params: {short_best_params} | ROC AUC: {short_roc_auc:.4f}")

if __name__ == "__main__":
    main()

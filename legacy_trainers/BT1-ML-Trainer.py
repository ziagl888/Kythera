import pandas as pd
import numpy as np
import joblib # For saving/loading the model
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score, confusion_matrix
import xgboost as xgb
import matplotlib.pyplot as plt
import seaborn as sns

# ========================= CONFIGURATION =========================
DATA_FILE = 'ml_training_data.csv' 
MODEL_FILE = 'trend_prediction_model.joblib' 

def train_and_evaluate_model():
    print("--- Starting ML model training with extended indicators ---")

    # 1. Load data
    try:
        df = pd.read_csv(DATA_FILE)
        print(f"Data successfully loaded from {DATA_FILE}. {len(df)} rows found.")
    except FileNotFoundError:
        print(f"Error: {DATA_FILE} not found. Please run the data collection script first.")
        return

    # 2. Prepare data (feature engineering & label definition)
    # Convert 'event_type' to numeric values (for XGBoost)
    df['event_type'] = df['event_type'].map({'UP': 1, 'DOWN': 0})

    # Define the features (X) and the target variable (y)
    # NEW: the feature list was extended with the new indicators
    features = [
        'event_type',               # 0 for DOWN, 1 for UP
        'vol_ratio',                # Volume ratio to average
        'rsi',                      # RSI(14)
        'atr_pct',                  # ATR as % of price
        'dist_ema200',              # Distance to EMA200
        'slope_trend',              # Slope of the trend line
        'hour_of_day',              # Hour of the day

        # NEW INDICATOR FEATURES
        'dist_close_ema9_pct',      # Distance from close to EMA9
        'dist_ema9_ema21_pct',      # Distance from EMA9 to EMA21
        'dist_close_kama9_pct',     # Distance from close to KAMA9
        'MACD_Line',                # MACD line
        'MACD_Signal',              # MACD signal line
        'TSI_Line',                 # TSI line
        'TSI_Signal',               # TSI signal line
        'dist_close_bb_lower_pct',  # Distance to lower Bollinger Band
        'dist_close_bb_upper_pct',  # Distance to upper Bollinger Band
        'bb_position_relative',     # Relative position in the Bollinger Band (0=bottom, 1=top)
        'dist_close_dc_lower_pct',  # Distance to lower Donchian Channel
        'dist_close_dc_upper_pct',  # Distance to upper Donchian Channel
        'dc_position_relative'      # Relative position in the Donchian Channel (0=bottom, 1=top)
    ]
    X = df[features]
    y = df['label_success']

    # Check for NaN values (in case indicators had no values at the start or around gaps)
    initial_rows = len(X)
    combined_df = pd.concat([X, y], axis=1).dropna()
    X = combined_df[features]
    y = combined_df['label_success']

    if len(X) < initial_rows:
        print(f"Note: {initial_rows - len(X)} rows with NaN values removed. Remaining samples: {len(X)}")

    if len(X) == 0:
        print("No valid data left after NaN removal. Training aborted.")
        return

    print(f"Number of features used: {len(features)}")
    print(f"Features used: {features}")
    print(f"Number of samples after cleaning: {len(X)}")

    # 3. Split data into training and test sets
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    print(f"Training set size: {len(X_train)} | Test set size: {len(X_test)}")
    print(f"Distribution of success labels in the training set:\n{y_train.value_counts(normalize=True)}")
    print(f"Distribution of success labels in the test set:\n{y_test.value_counts(normalize=True)}")

    # 4. Train model (XGBoost classifier)
    scale_pos_weight_value = len(y_train[y_train == 0]) / len(y_train[y_train == 1])
    
    model = xgb.XGBClassifier(
        objective='binary:logistic',
        eval_metric='logloss',
        # use_label_encoder=False, # Bypass deprecated warning, no longer needed in newer versions
        random_state=42,         
        n_estimators=500,        
        learning_rate=0.05,      
        max_depth=5,             
        subsample=0.7,           
        colsample_bytree=0.7,    
        scale_pos_weight=scale_pos_weight_value 
    )
    
    print("\nStarting model training...")
    model.fit(X_train, y_train)
    print("Model training complete.")

    # 5. Evaluate model
    print("\n--- Model evaluation on the test set ---")
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    print("\nClassification report (precision, recall, F1 score):")
    print(classification_report(y_test, y_pred))

    roc_auc = roc_auc_score(y_test, y_proba)
    print(f"ROC AUC score (measure of the model's discriminative power): {roc_auc:.4f}")

    cm = confusion_matrix(y_test, y_pred)
    print("\nConfusion matrix:")
    print(cm)

    print("\nInterpretation of the confusion matrix:")
    print(f"  True Positives (TP): {cm[1,1]} -> model predicts success, and it was a success.")
    print(f"  False Positives (FP): {cm[0,1]} -> model predicts success, but it was a failure (false alarm).")
    print(f"  True Negatives (TN): {cm[0,0]} -> model predicts failure, and it was a failure.")
    print(f"  False Negatives (FN): {cm[1,0]} -> model predicts failure, but it was a success (missed opportunity).")

    # 6. Show feature importance
    print("\n--- Feature importance (importance of the indicators) ---")
    feature_importances = pd.Series(model.feature_importances_, index=X_train.columns)

    top_features = feature_importances.sort_values(ascending=False)
    print(top_features)

    plt.figure(figsize=(12, 9)) # Adjusted size for more features
    sns.barplot(x=top_features.values, y=top_features.index, palette='viridis')
    plt.title('XGBoost Feature Importance (with extended indicators)')
    plt.xlabel('Importance')
    plt.ylabel('Features')
    plt.tight_layout()
    plt.show()

    # 7. Save model
    joblib.dump(model, MODEL_FILE)
    print(f"\nModel successfully saved as '{MODEL_FILE}'")
    print("\n--- ML model training complete ---")

if __name__ == "__main__":
    train_and_evaluate_model()

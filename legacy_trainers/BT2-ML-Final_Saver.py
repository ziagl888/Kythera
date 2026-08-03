import json
import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import f1_score
from itertools import product
from datetime import datetime
import os

# ==================== CONFIGURATION ====================
INPUT_FILE = 'break_retest_analysis_with_features.json'

# Your desired thresholds for application (not for training!)
APPLICATION_THRESHOLDS = {
    'LONG': 0.79,
    'SHORT': 0.86
}

# Where should the models be saved?
MODEL_SAVE_DIR = "models"
os.makedirs(MODEL_SAVE_DIR, exist_ok=True)

# Features (must match exactly the ones you use during inference!)
FEATURE_COLUMNS = [
    'dist_close_ema9_pct', 'dist_ema9_ema21_pct', 'dist_close_kama9_pct',
    'rsi14', 'rsi_below_30', 'rsi_above_70',
    'tsi', 'tsi_signal', 'tsi_above_0', 'tsi_below_0',
    'dist_close_boll_upper_pct', 'dist_close_boll_mid_pct', 'dist_close_boll_lower_pct',
    'dist_close_donchian_upper_pct', 'dist_close_donchian_mid_pct', 'dist_close_donchian_lower_pct',
    'retest_volume', 'retest_volume_ratio_avg'
]

# Small hyperparameter grid (you can of course expand it)
PARAM_GRID = {
    'n_estimators': [150, 200, 300],
    'learning_rate': [0.05, 0.08, 0.1],
    'max_depth': [3, 4, 5],
    'subsample': [0.8, 0.9],
    'colsample_bytree': [0.8],
    'gamma': [0]
}

# =======================================================

def get_timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def load_and_prepare_data():
    with open(INPUT_FILE, 'r') as f:
        data = json.load(f)

    df = pd.DataFrame(data['events'])
    df['retest_time'] = pd.to_datetime(df['retest_time'])

    # Separate features and target variable
    X = df[FEATURE_COLUMNS]
    y_raw = df['outcome_class']

    le = LabelEncoder()
    y = le.fit_transform(y_raw)

    class_mapping = dict(zip(le.classes_, range(len(le.classes_))))
    success_idx = class_mapping.get('continuation_success')

    if success_idx is None:
        raise ValueError("Class 'continuation_success' not found!")

    print("Class mapping:", class_mapping)
    print(f"'continuation_success' index: {success_idx}")
    
    return X, y, df, le, success_idx, class_mapping


def train_best_model(X, y, success_idx, trade_type: str):
    """Perform coarse grid search and return best model"""

    param_combinations = list(product(*PARAM_GRID.values()))
    param_keys = list(PARAM_GRID.keys())

    print(f"→ Starting training for {trade_type} | {len(param_combinations)} combinations")

    best_score = -1
    best_params = None
    best_model = None

    tscv = TimeSeriesSplit(n_splits=5)

    for params_tuple in param_combinations:
        params = dict(zip(param_keys, params_tuple))

        model = xgb.XGBClassifier(
            objective='multi:softprob',
            num_class=len(np.unique(y)),
            eval_metric='mlogloss',
            random_state=42,
            tree_method='hist',
            **params
        )

        scores = []
        for train_idx, val_idx in tscv.split(X):
            X_tr, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_tr, y_val = y[train_idx], y[val_idx]

            model.fit(X_tr, y_tr)
            proba = model.predict_proba(X_val)[:, success_idx]
            # Simple metric: fraction of top predictions that are correct
            top_preds = (proba > 0.5).astype(int)
            score = f1_score((y_val == success_idx).astype(int), top_preds, zero_division=0)
            scores.append(score)

        avg_score = np.mean(scores)
        print(f"  Params: {params} → Avg F1 (success class): {avg_score:.4f}")

        if avg_score > best_score:
            best_score = avg_score
            best_params = params
            best_model = model  # last model with best parameters

    # Train final model on all data
    print(f"\n→ Best model for {trade_type}: {best_params}")
    print(f"   Best score (CV): {best_score:.4f}")
    
    final_model = xgb.XGBClassifier(
        objective='multi:softprob',
        num_class=len(np.unique(y)),
        eval_metric='mlogloss',
        random_state=42,
        tree_method='hist',
        **best_params
    )
    
    final_model.fit(X, y)
    return final_model, best_params, best_score


def main():
    print("=== ML Model Training & Saving ===\n")

    X, y, df_events, le, success_idx, class_mapping = load_and_prepare_data()

    # LONG model
    mask_long = df_events['type'] == 'LONG_BREAK_RETEST'
    if mask_long.any():
        print("\n" + "="*60)
        print("Training LONG model")
        print("="*60)
        model_long, params_long, score_long = train_best_model(
            X[mask_long], y[mask_long], success_idx, "LONG"
        )

        # Save
        version = get_timestamp()
        fname_long = f"{MODEL_SAVE_DIR}/long_break_retest_xgb_{version}.json"
        model_long.save_model(fname_long)
        print(f"LONG model saved to: {fname_long}")

        # Save metadata
        meta_long = {
            "type": "LONG",
            "timestamp": version,
            "params": params_long,
            "cv_score": float(score_long),
            "application_threshold": APPLICATION_THRESHOLDS['LONG'],
            "class_mapping": class_mapping,
            "success_class_index": success_idx,
            "feature_columns": FEATURE_COLUMNS
        }
        with open(f"{fname_long}.meta.json", 'w') as f:
            json.dump(meta_long, f, indent=2)
        print(f"Metadata saved: {fname_long}.meta.json")

    # SHORT model
    mask_short = df_events['type'] == 'SHORT_BREAK_RETEST'
    if mask_short.any():
        print("\n" + "="*60)
        print("Training SHORT model")
        print("="*60)
        model_short, params_short, score_short = train_best_model(
            X[mask_short], y[mask_short], success_idx, "SHORT"
        )

        version = get_timestamp()
        fname_short = f"{MODEL_SAVE_DIR}/short_break_retest_xgb_{version}.json"
        model_short.save_model(fname_short)
        print(f"SHORT model saved to: {fname_short}")

        meta_short = {
            "type": "SHORT",
            "timestamp": version,
            "params": params_short,
            "cv_score": float(score_short),
            "application_threshold": APPLICATION_THRESHOLDS['SHORT'],
            "class_mapping": class_mapping,
            "success_class_index": success_idx,
            "feature_columns": FEATURE_COLUMNS
        }
        with open(f"{fname_short}.meta.json", 'w') as f:
            json.dump(meta_short, f, indent=2)
        print(f"Metadata saved: {fname_short}.meta.json")


if __name__ == "__main__":
    try:
        main()
        print("\n" + "="*70)
        print("Training complete. Models + metadata have been saved.")
        print("Good luck with trading! 🚀")
    except Exception as e:
        print("ERROR during training:")
        print(e)
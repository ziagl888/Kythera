import json
import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.model_selection import TimeSeriesSplit, GridSearchCV
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import classification_report, confusion_matrix, precision_score, recall_score, f1_score
import matplotlib.pyplot as plt
import seaborn as sns
import multiprocessing as mp
import time

# --- Configuration ---
INPUT_FILE = 'break_retest_analysis_with_features.json'

# Hyperparameter grid
# Since we train sequentially (but with multi-threading in tree building),
# we keep the grid focused.
param_grid = {
    'n_estimators': [100, 200],      # number of trees
    'learning_rate': [0.05, 0.1],    # how fast it learns
    'max_depth': [4, 6],             # depth of the trees (complexity)
    'subsample': [0.8],              # against overfitting
    'colsample_bytree': [0.8],       # against overfitting
    # 'scale_pos_weight': [1, 5]     # optional: if the model ignores success, turn this up here (binary only)
}

# Minimum number of trades for threshold optimisation
MIN_TRADES_FOR_CONSIDERATION = 100

def load_and_prepare_data(input_file):
    print("Loading JSON file... (can take a moment with 2.7M events)")
    with open(input_file, 'r') as f:
        data = json.load(f)

    df_events = pd.DataFrame(data['events'])

    # Convert timestamps
    df_events['retest_time'] = pd.to_datetime(df_events['retest_time'])

    # Drop columns not needed for training
    features_to_drop = [
        'symbol', 'type', 'break_time', 'retest_time', 'level_price',
        'outcome_price_change', 'outcome_class'
    ]

    X = df_events.drop(columns=features_to_drop)
    y = df_events['outcome_class']

    # Encode the classes (Neutral, Success, Fail -> 0, 1, 2)
    le = LabelEncoder()
    y_encoded = le.fit_transform(y)

    class_mapping = dict(zip(le.classes_, le.transform(le.classes_)))
    print(f"Class mapping: {class_mapping}")

    if 'continuation_success' not in class_mapping:
        raise ValueError("Class 'continuation_success' is missing!")

    success_class_idx = class_mapping['continuation_success']

    return X, y_encoded, df_events, le, success_class_idx

def train_and_evaluate_model_with_gridsearch(X, y_encoded, df_events_filtered, le, success_class_idx, trade_type_name, param_grid):
    print(f"\n{'='*60}")
    print(f"START TRAINING: {trade_type_name}")
    print(f"{'='*60}")
    
    # TimeSeriesSplit ensures we don't look into the future
    tscv = TimeSeriesSplit(n_splits=3) # 3 splits is often enough for this amount of data and saves time

    # XGBoost estimator
    # n_jobs=-1 uses ALL CPU cores for training the model (internal C++ threading)
    # This is stable on Windows!
    estimator = xgb.XGBClassifier(
        objective='multi:softprob',
        num_class=len(le.classes_),
        eval_metric='mlogloss',
        use_label_encoder=False,
        random_state=42,
        tree_method='hist', # very fast for large amounts of data
        n_jobs=-1           # IMPORTANT: internal parallelisation on
    )

    # GridSearchCV
    # n_jobs=1 prevents the joblib/multiprocessing crash.
    # We rely on the internal power of XGBoost.
    grid_search = GridSearchCV(
        estimator=estimator,
        param_grid=param_grid,
        scoring='f1_macro', # macro weights small classes (success) more strongly than 'weighted'
        cv=tscv,
        n_jobs=1,           # IMPORTANT: external parallelisation off (prevents crash)
        verbose=1
    )

    start_time = time.time()
    print(f"Starting GridSearch for {trade_type_name}...")
    print(f"Number of samples: {len(X)}")

    grid_search.fit(X, y_encoded)

    duration = time.time() - start_time
    print(f"GridSearch done in {duration/60:.2f} minutes.")
    print(f"Best parameters: {grid_search.best_params_}")
    print(f"Best score (f1_macro): {grid_search.best_score_:.4f}")

    best_model = grid_search.best_estimator_

    # Predict on the whole data (to find the threshold)
    # (ideally this would be a separate hold-out set, but TS split validated it)
    print("Creating predictions for threshold optimisation...")
    y_pred_proba = best_model.predict_proba(X)

    # We're only interested in the probability of "Success"
    all_y_pred_proba_success = y_pred_proba[:, success_class_idx]

    # Real profits for the profitability calculation
    all_outcome_price_changes = df_events_filtered['outcome_price_change'].values
    all_y_true = y_encoded

    # --- Threshold optimisation ---
    print("\nOptimising threshold (probability threshold)...")
    thresholds = np.linspace(0.3, 0.98, 100) # we search from 30% probability
    results = []

    for threshold in thresholds:
        # Which trades would we take at this threshold?
        trade_indices = np.where(all_y_pred_proba_success >= threshold)[0]

        if len(trade_indices) < MIN_TRADES_FOR_CONSIDERATION:
            continue

        # Real results of these trades
        selected_profits = all_outcome_price_changes[trade_indices]
        selected_labels = all_y_true[trade_indices]

        # Win rate
        wins = np.sum(selected_labels == success_class_idx)
        win_rate = (wins / len(trade_indices)) * 100

        # Profitability
        avg_profit = np.mean(selected_profits)
        total_profit_sum = np.sum(selected_profits)

        results.append({
            'threshold': threshold,
            'num_trades': len(trade_indices),
            'win_rate': win_rate,
            'avg_profit_per_trade': avg_profit,
            'total_profit_score': total_profit_sum # simple metric: total profit
        })

    if not results:
        print("No threshold found that yields enough trades.")
        return best_model, None, None

    results_df = pd.DataFrame(results)

    # We look for the threshold with the best win rate,
    # as long as the avg profit is positive.
    best_row = results_df.loc[results_df['win_rate'].idxmax()]

    print(f"\n--- RESULT {trade_type_name} ---")
    print(f"Best threshold: {best_row['threshold']:.4f}")
    print(f"Expected win rate: {best_row['win_rate']:.2f}%")
    print(f"Number of trades (in dataset): {int(best_row['num_trades'])}")
    print(f"Average profit per trade: {best_row['avg_profit_per_trade']:.2f}%")

    # Plot
    plt.figure(figsize=(10, 5))
    plt.plot(results_df['threshold'], results_df['win_rate'], label='Win Rate %')
    plt.plot(results_df['threshold'], results_df['avg_profit_per_trade'], label='Avg Profit %')
    plt.axvline(best_row['threshold'], color='red', linestyle='--', label='Best Threshold')
    plt.title(f"{trade_type_name} optimisation")
    plt.xlabel("Threshold")
    plt.legend()
    plt.grid(True)
    plt.show()

    # Save model (filename e.g. "model_LONG.json")
    model_filename = f"bt2_model_{trade_type_name}.json"
    best_model.save_model(model_filename)
    print(f"Model saved as: {model_filename}")

    # Optional: also save the threshold (e.g. in a small text file)
    with open(f"bt2_threshold_{trade_type_name}.txt", "w") as f:
        f.write(str(best_row['threshold']))
    
    return best_model, best_row['threshold'], best_row

def main():
    print("Starting ML Trainer (Stable Version)...")
    X, y_encoded, df_events, le, success_class_idx = load_and_prepare_data(INPUT_FILE)

    # Feature importance helper
    def show_feature_importance(model, feature_names):
        importance = model.feature_importances_
        feat_imp = pd.DataFrame({'feature': feature_names, 'importance': importance})
        feat_imp = feat_imp.sort_values('importance', ascending=False).head(15)
        print("\nTop 15 most important features:")
        print(feat_imp)

    # --- LONG MODEL ---
    mask_long = df_events['type'] == 'LONG_BREAK_RETEST'
    if mask_long.sum() > 100:
        X_long = X[mask_long]
        y_long = y_encoded[mask_long]
        df_long = df_events[mask_long]

        model_long, th_long, _ = train_and_evaluate_model_with_gridsearch(
            X_long, y_long, df_long, le, success_class_idx, "LONG", param_grid
        )
        if model_long:
            show_feature_importance(model_long, X.columns)
    else:
        print("Too few LONG events.")

    # --- SHORT MODEL ---
    mask_short = df_events['type'] == 'SHORT_BREAK_RETEST'
    if mask_short.sum() > 100:
        X_short = X[mask_short]
        y_short = y_encoded[mask_short]
        df_short = df_events[mask_short]

        model_short, th_short, _ = train_and_evaluate_model_with_gridsearch(
            X_short, y_short, df_short, le, success_class_idx, "SHORT", param_grid
        )
        if model_short:
            show_feature_importance(model_short, X.columns)
    else:
        print("Too few SHORT events.")

if __name__ == "__main__":
    mp.freeze_support()
    main()

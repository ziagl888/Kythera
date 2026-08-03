import os
import pandas as pd
import numpy as np
from sqlalchemy import create_engine
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score, roc_auc_score
import joblib
from tqdm import tqdm
import warnings

# Suppress UserWarning from XGBoost regarding `use_label_encoder`
# In newer versions this is set to False by default and the parameter is deprecated
warnings.filterwarnings("ignore", category=UserWarning, module="xgboost")

# --- CONFIGURATION ---
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "dbfiller",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "cryptodata"
}
# Path under which the trained model is saved
MODEL_PATH = "master_trade_model_xgboost.pkl"

# Create the database engine
# This engine is used by all functions to access the database
ENGINE = create_engine(f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")

# --- HELPER FUNCTIONS ---

def calculate_trade_outcome(entry_price: float, direction: str, entry_idx: int, ohlcv_df: pd.DataFrame) -> int:
    """
    Calculates the outcome of a trade based on predefined TP/SL criteria
    over a future time period. Takes into account whether SL was hit before TP.

    Args:
        entry_price (float): The price at which the trade was opened.
        direction (str): The direction of the trade ('LONG' or 'SHORT').
        entry_idx (int): The index of the OHLCV candle corresponding to the trade entry.
        ohlcv_df (pd.DataFrame): DataFrame with OHLCV data for the coin,
                                  sorted continuously by `open_time`.

    Returns:
        int:
            0 = Fail (SL hit or time limit without TP)
            1 = Successful (+5% in 24h)
            2 = Very successful (+10% in 72h)
            3 = Super successful (+20% in 120h)
    """

    # Parameters for Take Profit and Stop Loss
    SL_PCT = 0.075      # 7.5% Stop Loss
    TP1_PCT = 0.05      # 5% Target
    TP1_HOURS = 24
    TP2_PCT = 0.10      # 10% Target
    TP2_HOURS = 72
    TP3_PCT = 0.20      # 20% Target
    TP3_HOURS = 120
    
    # Determine the maximum index for the future window
    # The maximum lookahead time is TP3_HOURS (120 hours)
    max_lookahead_idx = min(entry_idx + TP3_HOURS, len(ohlcv_df) - 1)

    # Extract the future OHLCV data, starting AFTER the entry candle
    future_data = ohlcv_df.iloc[entry_idx+1 : max_lookahead_idx+1].copy()

    if future_data.empty:
        return 0 # Not enough data in the future for analysis

    # Add a column for the number of hours passed
    future_data['hours_passed'] = np.arange(1, len(future_data) + 1)

    # Calculate SL and TP prices based on the trade direction
    if direction == 'LONG':
        sl_price = entry_price * (1 - SL_PCT)
        tp1_price = entry_price * (1 + TP1_PCT)
        tp2_price = entry_price * (1 + TP2_PCT)
        tp3_price = entry_price * (1 + TP3_PCT)

        # Find the index of the first candle where SL was hit (Low <= SL price)
        sl_hits = future_data[future_data['low'] <= sl_price]
        first_sl_idx = sl_hits.index[0] if not sl_hits.empty else None

        # Find candles where TPs were hit (High >= TP price)
        tp1_hits = future_data[future_data['high'] >= tp1_price]
        tp2_hits = future_data[future_data['high'] >= tp2_price]
        tp3_hits = future_data[future_data['high'] >= tp3_price]

    else: # SHORT
        sl_price = entry_price * (1 + SL_PCT)
        tp1_price = entry_price * (1 - TP1_PCT)
        tp2_price = entry_price * (1 - TP2_PCT)
        tp3_price = entry_price * (1 - TP3_PCT)

        # Find the index of the first candle where SL was hit (High >= SL price)
        sl_hits = future_data[future_data['high'] >= sl_price]
        first_sl_idx = sl_hits.index[0] if not sl_hits.empty else None

        # Find candles where TPs were hit (Low <= TP price)
        tp1_hits = future_data[future_data['low'] <= tp1_price]
        tp2_hits = future_data[future_data['low'] <= tp2_price]
        tp3_hits = future_data[future_data['low'] <= tp3_price]

    # Helper function to check whether a TP was hit within the time limit
    # and whether SL was not hit beforehand.
    def check_tp_condition(tp_hits_df: pd.DataFrame, target_hours: int) -> bool:
        if tp_hits_df.empty:
            return False

        # Filter hits that fall within the time limit
        tp_hits_in_time = tp_hits_df[tp_hits_df['hours_passed'] <= target_hours]
        if tp_hits_in_time.empty:
            return False

        # The first TP hit within the time limit
        first_tp_hit_idx = tp_hits_in_time.index[0]

        # If an SL was hit AND its index comes before the first TP hit,
        # then the SL was triggered first — the trade is a failure with respect to this TP.
        if first_sl_idx is not None and first_sl_idx < first_tp_hit_idx:
            return False

        return True

    # Evaluate the trade outcomes hierarchically (from "Super successful" downwards)
    if check_tp_condition(tp3_hits, TP3_HOURS):
        return 3 # Super successful
    elif check_tp_condition(tp2_hits, TP2_HOURS):
        return 2 # Very successful
    elif check_tp_condition(tp1_hits, TP1_HOURS):
        return 1 # Successful

    return 0 # Trade was not successful (SL hit or no TP reached)


def normalize_features_for_ml(df_indicators: pd.DataFrame) -> pd.DataFrame:
    """
    Normalises indicators as a percentage deviation from the 'close' price
    or using other sensible methods, to prepare them for the ML model.
    Creates new features and retains relevant originals.

    Args:
        df_indicators (pd.DataFrame): DataFrame with raw indicators and the 'close' column.

    Returns:
        pd.DataFrame: DataFrame with normalised features.
    """
    df = df_indicators.copy()
    
    if 'close' not in df.columns:
        raise ValueError("DataFrame must contain a 'close' column for normalization.")
    
    # Create a safe 'close' column to prevent division by zero or NaN
    df['close_safe'] = df['close'].replace(0, np.nan)
    # Fill NaNs: forward first, then backward, and if still NaN, with 1.0 (as fallback)
    df['close_safe'] = df['close_safe'].fillna(method='ffill').fillna(method='bfill').fillna(1.0)

    # Indicators that are present as absolute prices and should be normalised relative to 'close'
    price_based_indicators = [
        # EMAs, MAs, WMAs, SMMAs, KAMAs
        'ema_7', 'ema_9', 'ema_12', 'ema_21', 'ema_26', 'ema_34', 'ema_50', 'ema_55', 'ema_89', 'ema_99', 'ema_200',
        'ma_7', 'ma_10', 'ma_20', 'ma_25', 'ma_50', 'ma_99', 'ma_100', 'ma_200',
        'wma_7', 'wma_9', 'wma_12', 'wma_21', 'wma_26', 'wma_34', 'wma_50', 'wma_55', 'wma_89', 'wma_99', 'wma_200',
        'smma_10', 'smma_20', 'smma_25', 'smma_50', 'smma_99', 'smma_100', 'smma_200',
        'kama_7', 'kama_9', 'kama_12', 'kama_21', 'kama_26', 'kama_34', 'kama_50', 'kama_55', 'kama_89', 'kama_99',
        
        # Bollinger Bands
        'boll_upper_20', 'boll_mid_20', 'boll_lower_20',
        
        # Donchian Channels
        'donchian_upper_4', 'donchian_lower_4', 'donchian_mid_4',
        'donchian_upper_10', 'donchian_lower_10', 'donchian_mid_10',
        'donchian_upper_12', 'donchian_lower_12', 'donchian_mid_12',
        'donchian_upper_15', 'donchian_lower_15', 'donchian_mid_15',
        'donchian_upper_20', 'donchian_lower_20', 'donchian_mid_20',
        
        # Trendlines, Channels, Support/Resistance
        'trendline_intercept', 'channel_upper_price', 'channel_lower_price', 
        'trendline_price', 'mid_line', 'support_price', 'resistance_price', 'poc',
        
        # Fibonacci Levels
        'fib_support_0_236', 'fib_resistance_0_236',
        'fib_support_0_382', 'fib_resistance_0_382',
        'fib_support_0_5', 'fib_resistance_0_5',
        'fib_support_0_618', 'fib_resistance_0_618',
        'fib_support_0_786', 'fib_resistance_0_786',
        'fib_extension_1_272', 'fib_extension_1_618', 'fib_extension_2_618',
        
        # High Volume Nodes (assumed to be price levels)
        'hvn_1', 'hvn_2', 'hvn_3'
    ]

    # Features that are already scaled or handled differently (take over directly)
    features_as_is = [
        'rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24', # RSI is already 0-100
        'tsi_25_13_13', 'tsi_25_13_13_signal', 'tsi_fast_12_7_7', 'tsi_fast_12_7_7_signal', # TSI is -100 to 100
        'macd_dif_fast_9_21_9', 'macd_dea_fast_9_21_9', 'macd_dif_normal_12_26_9', 'macd_dea_normal_12_26_9', # MACD is already a difference
        'trendline_slope', # rate of change
        'r_squared', # 0-1
        'signal_conf', # The confidence of the original AI signal
        'direction_num' # 0 or 1
    ]

    # ATRs that should be normalised as a percentage of price (as percentage volatility)
    atr_indicators = ['atr_9', 'atr_14', 'atr_21']

    # Create DataFrame for the normalised features
    normalized_df = pd.DataFrame(index=df.index)

    # 1. Normalise price-based indicators: (Indicator - Close) / Close * 100
    for col in price_based_indicators:
        if col in df.columns:
            normalized_df[f'{col}_dist_pct'] = (df[col] - df['close']) / df['close_safe'] * 100

    # 2. Normalise ATRs: ATR / Close * 100 (as percentage volatility)
    for col in atr_indicators:
        if col in df.columns:
            normalized_df[f'{col}_pct_close'] = df[col] / df['close_safe'] * 100

    # 3. Take over features that don't need to be changed
    for col in features_as_is:
        if col in df.columns:
            normalized_df[col] = df[col]

    # 4. Treat 'trend_direction' as a categorical feature (one-hot encoding)
    if 'trend_direction' in df.columns:
        # Create dummy variables for 'trend_direction'
        # The categories 'UP', 'DOWN', 'SIDEWAYS' should be set explicitly here,
        # to ensure that the columns are created even when a category
        # does not occur in a subset (important for consistent feature sets).
        all_possible_directions = ['UP', 'DOWN', 'SIDEWAYS'] # Assumption: possible values
        direction_dummies = pd.get_dummies(df['trend_direction'], prefix='trend_dir')

        # Add missing dummy columns and fill with 0
        for d in all_possible_directions:
            col_name = f'trend_dir_{d}'
            if col_name not in direction_dummies.columns:
                direction_dummies[col_name] = 0

        normalized_df = pd.concat([normalized_df, direction_dummies], axis=1)

    # Remove the helper column 'close_safe'
    df = df.drop(columns=['close_safe'], errors='ignore')

    # Fill all remaining NaN values in the features with 0.
    normalized_df = normalized_df.fillna(0)

    return normalized_df


def fetch_and_process_data() -> pd.DataFrame:
    """
    Fetches signals and the corresponding OHLCV and indicator data from the database,
    calculates the trade outcomes and normalises the features.

    Returns:
        pd.DataFrame: A DataFrame with prepared data for the ML training,
                      including the 'target' label and normalised features.
    """
    print("Fetching signals from database...")
    # Fetch signals from the last 180 days (can be adjusted, the more data the better)
    signals = pd.read_sql("SELECT * FROM ai_signals WHERE timestamp > NOW() - INTERVAL '180 days' ORDER BY timestamp ASC", ENGINE)
    signals['timestamp'] = pd.to_datetime(signals['timestamp'])
    # Round signal timestamp to the nearest full hour to align it with 1h candles
    signals['join_time'] = signals['timestamp'].dt.round('1h')
    
    unique_coins = signals['symbol'].unique()
    training_data_rows = []
    
    print(f"Processing {len(unique_coins)} unique coins for training data generation...")
    
    # Iterate over each coin with a progress bar
    for coin in tqdm(unique_coins, desc="Processing Coins"):
        try:
            # 1. Fetch indicators for the current coin
            ind_query = f'SELECT * FROM "{coin}_1h_indicators" ORDER BY open_time ASC'
            indicators = pd.read_sql(ind_query, ENGINE)
            indicators['open_time'] = pd.to_datetime(indicators['open_time'])

            # 2. Fetch OHLCV data for the current coin (for future price movements)
            ohlcv_query = f'SELECT open_time, open, high, low, close FROM "{coin}_1h" ORDER BY open_time ASC'
            ohlcv = pd.read_sql(ohlcv_query, ENGINE)
            ohlcv['open_time'] = pd.to_datetime(ohlcv['open_time'])

            # Reset the index for easy `iloc`-based slicing
            ohlcv = ohlcv.reset_index(drop=True)

            # Create a map from `open_time` to `iloc` index for fast access
            time_to_idx = {t: i for i, t in enumerate(ohlcv['open_time'])}

            # Filter signals that belong to this coin
            coin_signals = signals[signals['symbol'] == coin].copy()

            # Iterate over each signal of the current coin
            for _, signal in coin_signals.iterrows():
                sig_time = signal['join_time']

                # Find the matching indicator row for the signal timestamp
                # The indicator candle used is the one already closed at signal time
                mask_ind = indicators['open_time'] == sig_time
                if not mask_ind.any():
                    continue # No indicators found for this timestamp

                # Create a copy of the indicator row
                indicator_row = indicators.loc[mask_ind].iloc[0].copy()

                # Add the original signal confidence and the numeric direction
                indicator_row['signal_conf'] = signal['confidence']
                indicator_row['direction_num'] = 1 if signal['direction'] == 'LONG' else 0

                # Find the start index in the OHLCV data for the outcome calculation
                if sig_time in time_to_idx:
                    entry_idx = time_to_idx[sig_time]

                    # Calculate the trade outcome based on the defined criteria
                    outcome_score = calculate_trade_outcome(
                        entry_price=signal['price'],
                        direction=signal['direction'],
                        entry_idx=entry_idx,
                        ohlcv_df=ohlcv
                    )
                    
                    indicator_row['trade_score'] = outcome_score
                    training_data_rows.append(indicator_row)
                    
        except Exception as e:
            # Ignore errors when processing a coin and continue
            # For debugging: print(f"Error processing coin {coin}: {e}")
            pass

    # Convert the collected data into a DataFrame
    raw_training_df = pd.DataFrame(training_data_rows)

    if raw_training_df.empty:
        print("No raw data found for training!")
        return pd.DataFrame()

    # Normalise the features for model training
    print("Normalizing features...")
    # The 'close' column is required in `normalize_features_for_ml` but is not kept as a feature.
    # 'signal_conf' and 'direction_num' are also used as features.
    # 'trade_score' is used to create the 'target' label.

    # Ensure that the columns for normalisation and the label are present
    required_cols = ['close', 'signal_conf', 'direction_num', 'trade_score']
    for col in required_cols:
        if col not in raw_training_df.columns:
            print(f"Warning: Missing column '{col}' in raw training data. Filling with 0.")
            raw_training_df[col] = 0 # Fallback, should not happen though

    # Perform the normalisation
    normalized_features_df = normalize_features_for_ml(raw_training_df)

    # Add the 'target' label and the original 'trade_score' back
    # (These are the target variables and not features)
    normalized_features_df['target'] = raw_training_df['target'] if 'target' in raw_training_df.columns else None # Target is recalculated later
    normalized_features_df['trade_score'] = raw_training_df['trade_score']
        
    return normalized_features_df


def train_model():
    """
    Runs the entire training process: fetching data, preparing it, and
    training and saving an XGBoost classification model.
    """
    df = fetch_and_process_data()

    if df.empty:
        print("No data found for training! Exiting.")
        return

    # --- Definition of the 'target' label ---
    # Here we define what counts as a "successful trade" for the model.
    # Option: (df['trade_score'] >= 1).astype(int) for at least 5% profit in 24h
    # Option: (df['trade_score'] >= 2).astype(int) for at least 10% profit in 72h (default)
    # Option: (df['trade_score'] >= 3).astype(int) for at least 20% profit in 120h

    # The model is trained to predict trades that were at least
    # "Very successful" (i.e., +10% in 72h without hitting the 7.5% SL)
    df['target'] = (df['trade_score'] >= 2).astype(int)
    
    print(f"\nTotal dataset size after processing: {len(df)} samples")
    print(f"Class distribution (Win/Loss):\n{df['target'].value_counts()}")
    
    # Remove columns that are not features (labels, IDs etc.)
    drop_from_features = ['target', 'trade_score']

    # All other columns in the DataFrame are our features
    feature_cols = [c for c in df.columns if c not in drop_from_features]

    X = df[feature_cols]
    y = df['target']

    # --- Train/Test Split ---
    # Important: `shuffle=False` for time series data, to avoid lookahead bias.
    # A `test_size` of 0.2 to 0.3 is common.
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=42, shuffle=False)
    
    print(f"\nTraining on {len(X_train)} samples, testing on {len(X_test)} samples.")
    print(f"Number of features used: {len(feature_cols)}")

    # --- XGBoost model training ---
    # Hyperparameters for XGBoost
    # These parameters can be further optimised through hyperparameter tuning.
    # 'scale_pos_weight' is crucial for unbalanced classes.
    scale_pos_weight_value = (y_train == 0).sum() / (y_train == 1).sum() if (y_train == 1).sum() > 0 else 1

    xgb_params = {
        'objective': 'binary:logistic',  # Binary classification (0 or 1)
        'eval_metric': 'logloss',        # Metric for evaluation during training
        'eta': 0.05,                     # Learning rate (also called `learning_rate`)
        'max_depth': 6,                  # Maximum tree depth
        'subsample': 0.7,                # Proportion of randomly selected data points per tree
        'colsample_bytree': 0.7,         # Proportion of randomly selected features per tree
        'min_child_weight': 1,           # Minimum number of instances required by a child node
        'random_state': 42,              # Seed for reproducibility
        'n_estimators': 1000,            # Maximum number of boosting rounds (trees)
        'n_jobs': -1,                    # Use all available CPU cores
        'scale_pos_weight': scale_pos_weight_value, # Weighting for positive class on unbalanced data
        # 'tree_method': 'hist',         # Can be faster for larger datasets
    }

    # Initialise the XGBoost classifier
    clf = xgb.XGBClassifier(**xgb_params)

    print("\nStarting XGBoost model training...")
    # Train the model with early stopping
    # Training is stopped when the `eval_metric` (logloss) on the validation set
    # does not improve for 50 consecutive rounds.
    clf.fit(X_train, y_train,
            early_stopping_rounds=50,
            eval_set=[(X_test, y_test)],
            verbose=False) # Set to True for detailed output per round

    # Predictions on the test set
    preds = clf.predict(X_test)
    probs = clf.predict_proba(X_test)[:, 1] # Probability for the positive class (1)

    print("\n--- XGBoost Model Evaluation on Test Set ---")
    print(f"Best iteration: {clf.best_iteration}") # The best iteration at which early stopping occurred
    print("Accuracy:", accuracy_score(y_test, preds))
    print("ROC AUC Score:", roc_auc_score(y_test, probs))
    print("\nClassification Report:")
    print(classification_report(y_test, preds))
    
    # Save the trained model and the feature list
    save_obj = {
        'model': clf,
        'features': feature_cols # Saving the feature columns is CRITICAL for consistent predictions
    }
    joblib.dump(save_obj, MODEL_PATH)
    print(f"\nXGBoost model and feature list saved to {MODEL_PATH}")

# --- SCRIPT START ---
if __name__ == "__main__":
    train_model()

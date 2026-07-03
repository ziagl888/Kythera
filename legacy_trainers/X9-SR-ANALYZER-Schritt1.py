import os
import pandas as pd
import numpy as np
import psycopg2
from datetime import datetime
from sklearn.metrics import roc_auc_score, accuracy_score, classification_report
import xgboost as xgb
import joblib

# ────────────────────────────────────────────────────────────────
# Konfiguration
# ────────────────────────────────────────────────────────────────
DB_CONFIG = {
    'dbname': 'cryptodata',
    'user': 'dbfiller',
    'password': os.getenv("DB_PASSWORD", ""),
    'host': 'localhost',
    'port': 5432
}

MIN_TRADES_PER_CLASS = 150
TEST_SPLIT_RATIO = 0.20          # Letzte 20% der Daten als Test

# ────────────────────────────────────────────────────────────────
def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def load_closed_trades():
    query = """
    SELECT 
        lfd, time, coin, direction, entry, posted, status
    FROM closed_trades3
    WHERE status IS NOT NULL 
      AND status != '' 
      AND time IS NOT NULL
    ORDER BY time ASC
    """
    with get_db_connection() as conn:
        df = pd.read_sql_query(query, conn)
    df['time'] = pd.to_datetime(df['time'])
    return df.sort_values('time').reset_index(drop=True)


def normalize_coin(coin: str) -> str:
    replacements = {
        'XRPUSDC': 'XRPUSDT',
        '1000PEPEUSDC': '1000PEPEUSDT',
        '1000SHIBUSDC': '1000SHIBUSDT',
        # Ergänze bei Bedarf
    }
    return replacements.get(coin, coin)


def get_indicators_at_time(coin: str, timestamp: pd.Timestamp):
    coin_norm = normalize_coin(coin)
    query = f"""
    SELECT * FROM "{coin_norm}_1h_indicators"
    WHERE open_time <= %s
    ORDER BY open_time DESC
    LIMIT 1
    """
    try:
        with get_db_connection() as conn:
            df = pd.read_sql_query(query, conn, params=(timestamp,))
        if df.empty:
            return None
        return df.iloc[0]
    except Exception as e:
        print(f"⚠️ Tabelle/Fehler für {coin} ({coin_norm}): {str(e)}")
        return None


def create_feature_row(trade_row):
    indicators = get_indicators_at_time(trade_row['coin'], trade_row['time'])
    if indicators is None:
        return None

    row = indicators.to_dict()
    close = row.get('close', np.nan)
    atr = row.get('atr_14', np.nan)

    if pd.isna(close) or close <= 0:
        return None

    features = {}

    # Basis-Indikatoren (numerisch)
    base_cols = [
        'rsi_9', 'rsi_14', 'rsi_24',
        'macd_dif_fast_9_21_9', 'macd_dea_fast_9_21_9',
        'tsi_fast_12_7_7', 'tsi_fast_12_7_7_signal',
        'atr_14', 'r_squared',
        'boll_upper_20', 'boll_mid_20', 'boll_lower_20',
        'donchian_upper_20', 'donchian_lower_20', 'donchian_mid_20',
        'support_price', 'resistance_price',
        'ema_9', 'ema_21', 'wma_9', 'wma_21', 'kama_9', 'kama_21',
        'close'
    ]

    for col in base_cols:
        val = row.get(col)
        features[col] = float(val) if pd.notna(val) else np.nan

    # Trend als Zahl
    trend_map = {'UP': 1.0, 'DOWN': -1.0, 'FLAT': 0.0, 'SIDEWAYS': 0.0}
    trend_val = str(row.get('trend_direction', '')).upper()
    features['trend_direction_num'] = trend_map.get(trend_val, 0.0)

    # Relative Distanzen (%)
    def pct(a, b):
        return (a - b) / close * 100 if pd.notna(b) and close > 0 else np.nan

    features.update({
        'pct_ema9':      pct(close, row.get('ema_9')),
        'pct_ema21':     pct(close, row.get('ema_21')),
        'pct_wma9':      pct(close, row.get('wma_9')),
        'pct_kama9':     pct(close, row.get('kama_9')),
        'pct_support':   pct(close, row.get('support_price')),
        'pct_resist':    pct(row.get('resistance_price'), close),
        'pct_boll_mid':  pct(close, row.get('boll_mid_20')),
        'ema9_ema21_pct': pct(row.get('ema_9'), row.get('ema_21')),
        'kama9_kama21_pct': pct(row.get('kama_9'), row.get('kama_21')),
    })

    # Neue ATR-normalisierte Abstände (sehr hilfreich!)
    if pd.notna(atr) and atr > 0:
        features.update({
            'support_atr':  (close - row.get('support_price', np.nan)) / atr,
            'resist_atr':   (row.get('resistance_price', np.nan) - close) / atr,
            'boll_width_atr': ((row.get('boll_upper_20', 0) - row.get('boll_lower_20', 0)) / atr),
        })

    features['is_long'] = 1.0 if trade_row['direction'].upper() == 'LONG' else 0.0

    return features


def prepare_dataset(direction_filter=None):
    """Lädt Daten, erstellt Features – optional nur LONG oder SHORT"""
    trades = load_closed_trades()
    if direction_filter:
        trades = trades[trades['direction'].str.upper() == direction_filter.upper()]

    print(f"\n=== Datensatz für {direction_filter or 'ALLE'} ===")
    print(f"Anzahl Trades: {len(trades)}")

    features_list = []
    labels = []
    times = []

    for _, trade in trades.iterrows():
        feat_dict = create_feature_row(trade)
        if feat_dict is None:
            continue

        status = str(trade['status']).strip()
        success = 1 if status in ['SL1', 'SL2', 'SL3', '4'] else 0

        features_list.append(feat_dict)
        labels.append(success)
        times.append(trade['time'])

    if not features_list:
        return None, None, None

    X = pd.DataFrame(features_list)
    y = np.array(labels)

    # Median-Imputation (robust gegen Ausreißer)
    X = X.fillna(X.median(numeric_only=True))

    print(f"Verwendbare Datensätze: {len(X)}")
    print(f"Win-Rate: {y.mean():.1%}")

    return X, y, pd.Series(times)


def train_and_evaluate(X, y, name="Model"):
    if len(X) < 300:
        print(f"{name}: Zu wenig Daten ({len(X)})")
        return None

    # Chronologischer Split
    split_idx = int(len(X) * (1 - TEST_SPLIT_RATIO))
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

    print(f"{name} - Train: {len(X_train)}, Test: {len(X_test)}")

    params = {
        'objective': 'binary:logistic',
        'eval_metric': 'auc',
        'max_depth': 4,
        'learning_rate': 0.025,
        'subsample': 0.82,
        'colsample_bytree': 0.75,
        'reg_lambda': 1.3,
        'reg_alpha': 0.1,
        'random_state': 42,
        'n_jobs': -1,
        'tree_method': 'hist',
    }

    model = xgb.XGBClassifier(**params)
    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        verbose=50
    )

    y_pred_proba = model.predict_proba(X_test)[:, 1]
    y_pred = (y_pred_proba > 0.50).astype(int)

    auc = roc_auc_score(y_test, y_pred_proba)
    acc = accuracy_score(y_test, y_pred)

    print(f"\n{name} - Ergebnisse (letzte {TEST_SPLIT_RATIO*100:.0f}% der Daten)")
    print(f"Test AUC:       {auc:.4f}")
    print(f"Test Accuracy:  {acc:.3f}")
    print(classification_report(y_test, y_pred, target_names=['Loss', 'Win >=T1']))

    # Top Features
    importance = pd.Series(model.feature_importances_, index=X.columns)
    print(f"\nTop 12 Features {name}:")
    print(importance.sort_values(ascending=False).head(12))

    return model


# ─── Hauptprogramm ───────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Tag 1 – Training mit chronologischem Split & getrennten Modellen ===\n")

    # 1. Alle Trades (Benchmark)
    print("Benchmark (LONG + SHORT zusammen):")
    X_all, y_all, times_all = prepare_dataset()
    if X_all is not None:
        model_all = train_and_evaluate(X_all, y_all, "Gesamt")

    # 2. Nur LONG
    print("\n" + "="*60)
    X_long, y_long, _ = prepare_dataset("LONG")
    if X_long is not None:
        model_long = train_and_evaluate(X_long, y_long, "LONG")

    # 3. Nur SHORT
    print("\n" + "="*60)
    X_short, y_short, _ = prepare_dataset("SHORT")
    if X_short is not None:
        model_short = train_and_evaluate(X_short, y_short, "SHORT")

    # Speichern der besten Modelle
    if 'model_long' in locals() and model_long is not None:
        joblib.dump(model_long, "trade_success_xgb_LONG_v1.model")
        print("LONG Modell gespeichert")
    if 'model_short' in locals() and model_short is not None:
        joblib.dump(model_short, "trade_success_xgb_SHORT_v1.model")
        print("SHORT Modell gespeichert")
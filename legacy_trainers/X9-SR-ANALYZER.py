import os
import pandas as pd
import numpy as np
import psycopg2
from datetime import datetime
from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.metrics import roc_auc_score, accuracy_score, classification_report
from sklearn.preprocessing import StandardScaler
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

MIN_TRADES_FOR_TRAINING = 300
TEST_SIZE = 0.20
RANDOM_STATE = 42

# Welche Indikator-Spalten sollen wir nehmen?
FEATURE_COLUMNS = [
    'rsi_9', 'rsi_14', 'rsi_24',
    'macd_dif_fast_9_21_9', 'macd_dea_fast_9_21_9',
    'tsi_fast_12_7_7', 'tsi_fast_12_7_7_signal',
    'atr_14',
    'boll_upper_20', 'boll_mid_20', 'boll_lower_20',
    'donchian_upper_20', 'donchian_lower_20', 'donchian_mid_20',
    'support_price', 'resistance_price',
    'ema_9', 'ema_21', 'wma_9', 'wma_21', 'kama_9', 'kama_21',
    'close', 'trend_direction', 'r_squared'
]

# ────────────────────────────────────────────────────────────────
def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def load_closed_trades():
    """Lädt alle closed trades + Richtung + entry time"""
    query = """
    SELECT 
        lfd, time, coin, direction, entry, posted, status
    FROM closed_trades3
    WHERE status IS NOT NULL 
      AND status != '' 
      AND time IS NOT NULL
    ORDER BY time
    """
    with get_db_connection() as conn:
        df = pd.read_sql_query(query, conn)
    return df


# def get_indicators_at_time(coin: str, timestamp: pd.Timestamp):
    # """Holt die Indikator-Zeile, die am nächsten an timestamp dran ist (≤ timestamp)"""
    # query = """
    # SELECT * FROM "{}_1h_indicators"
    # WHERE open_time <= %s
    # ORDER BY open_time DESC
    # LIMIT 1
    # """.format(coin.replace('/', ''))

    # with get_db_connection() as conn:
        # df = pd.read_sql_query(query, conn, params=(timestamp,))
    
    # if df.empty:
        # return None
    # return df.iloc[0]

def get_indicators_at_time(coin: str, timestamp: pd.Timestamp):
    table_name = f'"{coin}_1h_indicators"'
    query = f"""
    SELECT * FROM {table_name}
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
        print(f"⚠️ Fehler bei Coin {coin}: {str(e)} → Trade wird übersprungen")
        return None

# def create_feature_row(trade_row):
    # """Erzeugt Feature-Vektor für einen Trade"""
    # indicators = get_indicators_at_time(trade_row['coin'], trade_row['time'])
    
    # if indicators is None:
        # return None
    
    # row = indicators.to_dict()
    # close = row['close']
    
    # features = {}
    
    # # Direkte Indikatoren
    # for col in FEATURE_COLUMNS:
        # if col in row and pd.notna(row[col]):
            # features[col] = float(row[col])
        # else:
            # features[col] = np.nan
    
    # # ─── Relativ-Abstände in % ───────────────────────────────
    # def pct_diff(a, b):
        # return (a - b) / close * 100 if close != 0 else 0
    
    # important_levels = {
        # 'pct_ema9':     pct_diff(close, row.get('ema_9', np.nan)),
        # 'pct_ema21':    pct_diff(close, row.get('ema_21', np.nan)),
        # 'pct_wma9':     pct_diff(close, row.get('wma_9', np.nan)),
        # 'pct_kama9':    pct_diff(close, row.get('kama_9', np.nan)),
        # 'pct_support':  pct_diff(close, row.get('support_price', np.nan)),
        # 'pct_resist':   pct_diff(row.get('resistance_price', np.nan), close),
        # 'pct_boll_mid': pct_diff(close, row.get('boll_mid_20', np.nan)),
        # 'pct_boll_width': (row.get('boll_upper_20', 0) - row.get('boll_lower_20', 0)) / close * 100,
    # }
    
    # features.update(important_levels)
    
    # # EMA / KAMA Abstand zueinander
    # if 'ema_9' in row and 'ema_21' in row:
        # features['ema9_ema21_diff_pct'] = pct_diff(row['ema_9'], row['ema_21'])
    # if 'kama_9' in row and 'kama_21' in row:
        # features['kama9_kama21_diff_pct'] = pct_diff(row['kama_9'], row['kama_21'])
    
    # # Richtung als numerisch (für LONG/SHORT Unterscheidung)
    # features['is_long'] = 1 if trade_row['direction'].upper() == 'LONG' else 0
    
    # return features

def create_feature_row(trade_row):
    indicators = get_indicators_at_time(trade_row['coin'], trade_row['time'])
    
    if indicators is None:
        return None
    
    row = indicators.to_dict()
    close = row.get('close', np.nan)
    if pd.isna(close) or close == 0:
        return None
    
    features = {}
    
    # Numerische Features direkt übernehmen / konvertieren
    numeric_cols = [
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
    
    for col in numeric_cols:
        if col in row and pd.notna(row[col]):
            try:
                features[col] = float(row[col])
            except (ValueError, TypeError):
                features[col] = np.nan
        else:
            features[col] = np.nan
    
    # Trend Direction als numerisch kodieren
    if 'trend_direction' in row and pd.notna(row['trend_direction']):
        direction_map = {'UP': 1.0, 'DOWN': -1.0, 'FLAT': 0.0, 'SIDEWAYS': 0.0}
        trend_val = str(row['trend_direction']).upper()
        features['trend_direction_num'] = direction_map.get(trend_val, 0.0)
    
    # ─── Relative Distanzen (wie vorher) ───────────────────────────────
    def pct_diff(a, b):
        return (a - b) / close * 100 if pd.notna(close) and close != 0 else np.nan

    features['pct_ema9'] = pct_diff(close, row.get('ema_9', np.nan))
    features['pct_ema21'] = pct_diff(close, row.get('ema_21', np.nan))
    features['pct_wma9'] = pct_diff(close, row.get('wma_9', np.nan))
    features['pct_kama9'] = pct_diff(close, row.get('kama_9', np.nan))
    features['pct_support'] = pct_diff(close, row.get('support_price', np.nan))
    features['pct_resist'] = pct_diff(row.get('resistance_price', np.nan), close)
    features['pct_boll_mid'] = pct_diff(close, row.get('boll_mid_20', np.nan))
    
    if 'boll_upper_20' in row and 'boll_lower_20' in row:
        features['pct_boll_width'] = (row['boll_upper_20'] - row['boll_lower_20']) / close * 100
    
    if 'ema_9' in row and 'ema_21' in row:
        features['ema9_ema21_diff_pct'] = pct_diff(row['ema_9'], row['ema_21'])
    if 'kama_9' in row and 'kama_21' in row:
        features['kama9_kama21_diff_pct'] = pct_diff(row['kama_9'], row['kama_21'])
    
    features['is_long'] = 1.0 if trade_row['direction'].upper() == 'LONG' else 0.0
    
    return features


def prepare_dataset():
    trades = load_closed_trades()
    print(f"Gefundene abgeschlossene Trades: {len(trades)}")
    
    if len(trades) < MIN_TRADES_FOR_TRAINING:
        print("Zu wenig Daten für sinnvolles Training!")
        return None, None, None

    features_list = []
    labels = []

    for _, trade in trades.iterrows():
        feat_dict = create_feature_row(trade)
        if feat_dict is None:
            continue

        # Erfolgskriterium – HIER ANPASSEN NACH BEDARF!
        status = str(trade['status']).strip()
        success = 1 if status in ['SL1', 'SL2', 'SL3', '4'] else 0
        
        features_list.append(feat_dict)
        labels.append(success)

    X = pd.DataFrame(features_list)
    y = np.array(labels)
    
    print(f"Verwendbare Datensätze nach Join mit Indikatoren: {len(X)}")
    
    return X, y, trades


def train_xgboost_model(X, y):
    if len(X) < 100:
        print("Zu wenig verwendbare Daten → Abbruch")
        return None

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=TEST_SIZE, random_state=RANDOM_STATE, stratify=y
    )

    # XGBoost Parameter – eher konservativ
    params = {
        'objective': 'binary:logistic',
        'eval_metric': 'auc',
        'max_depth': 4,
        'learning_rate': 0.025,
        'subsample': 0.85,
        'colsample_bytree': 0.75,
        'reg_lambda': 1.2,
        'reg_alpha': 0.1,
        'random_state': RANDOM_STATE,
        'n_jobs': -1,
        'tree_method': 'hist',     # schneller & meist besser
        'device': 'cpu'            # 'cuda' wenn GPU vorhanden
    }

    model = xgb.XGBClassifier(**params)

    print("Training startet...")
    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        verbose=100
    )

    # Schnelle Evaluation
    y_pred_proba = model.predict_proba(X_test)[:, 1]
    y_pred = (y_pred_proba > 0.5).astype(int)

    auc = roc_auc_score(y_test, y_pred_proba)
    acc = accuracy_score(y_test, y_pred)

    print("\n" + "="*50)
    print(f"Test AUC:       {auc:.4f}")
    print(f"Test Accuracy:  {acc:.4f}")
    print(classification_report(y_test, y_pred, target_names=['Loss (0)', 'Win >= T1 (1)']))
    print("="*50 + "\n")

    # Feature Importance (Top 15)
    importance = pd.Series(model.feature_importances_, index=X.columns)
    print("Top 15 Features:")
    print(importance.sort_values(ascending=False).head(15))

    return model


def save_model(model, filename="trade_success_xgb_v1.model"):
    joblib.dump(model, filename)
    print(f"Modell gespeichert: {filename}")


# ─── Hauptprogramm ───────────────────────────────────────────────
if __name__ == "__main__":
    print("Starte Datensammlung & Feature-Engineering...")
    X, y, _ = prepare_dataset()

    if X is not None and len(X) >= MIN_TRADES_FOR_TRAINING:
        print("\nTraining XGBoost...")
        model = train_xgboost_model(X, y)

        if model is not None:
            save_model(model)
            # Optional: Modell mit joblib laden und später predict nutzen
            # loaded = joblib.load("trade_success_xgb_v1.model")
    else:
        print("Training abgebrochen - zu wenig Daten")
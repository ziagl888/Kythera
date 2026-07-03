import os
import asyncio
import asyncpg
import pandas as pd
import numpy as np
from xgboost import XGBClassifier
from sklearn.model_selection import train_test_split, StratifiedKFold
from sklearn.metrics import classification_report, precision_recall_curve
from imblearn.over_sampling import SMOTE
import joblib
import logging
from datetime import datetime
import pytz
import json
from pathlib import Path

# === Logging ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# === DB Config ===
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "dbfiller",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "cryptodata"
}

# === Coins laden ===
def load_coins() -> list[str]:
    coins_file = Path("coins.json")
    if not coins_file.exists():
        logger.error("coins.json nicht gefunden!")
        return []
    with open(coins_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [str(s).upper() + "USDT" if not str(s).upper().endswith("USDT") else str(s).upper() for s in data]

# === Prozentualer Abstand ===
def pct_distance(price_series: pd.Series, indicator_series: pd.Series) -> pd.Series:
    denominator = indicator_series.replace(0, np.nan)
    result = (price_series - indicator_series) / denominator * 100
    return result.fillna(0)

# === Erweiterte Features hinzufügen ===
def add_advanced_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(['symbol', 'open_time']).reset_index(drop=True)
    
    # Volume Features
    df['volume_ratio_prev'] = df.groupby('symbol')['volume'].transform(lambda x: x / x.shift(1))
    df['volume_sma20'] = df.groupby('symbol')['volume'].transform(lambda x: x.rolling(20, min_periods=1).mean())
    df['volume_ratio_sma20'] = df['volume'] / df['volume_sma20']
    
    # Deltas
    for col in ['rsi_14', 'macd_dif', 'tsi_fast']:
        df[f'{col}_delta_1'] = df.groupby('symbol')[col].diff(1)
        df[f'{col}_delta_3'] = df.groupby('symbol')[col].diff(3)
    
    # MACD Histogram
    df['macd_hist'] = df['macd_dif'] - df['macd_dea']
    df['macd_hist_delta_1'] = df.groupby('symbol')['macd_hist'].diff(1)
    
    # EMA Features
    df['ema_9_minus_ema_21'] = df['ema_9'] - df['ema_21']
    df['ema_9_cross_above_21'] = ((df['ema_9'].shift(1) < df['ema_21'].shift(1)) & (df['ema_9'] > df['ema_21'])).astype(int)
    
    # Binäre Features
    df['above_ema_200'] = (df['close'] > df['ema_200']).astype(int)
    df['rsi_14_above_50'] = (df['rsi_14'] > 50).astype(int)
    df['rsi_14_cross_above_30'] = ((df['rsi_14'].shift(1) < 30) & (df['rsi_14'] >= 30)).astype(int)
    
    # ATR-normalisierte Abstände
    df['boll_upper_dist_atr'] = (df['close'] - df['boll_upper_20']) / (df['atr_14'] + 1e-8)
    df['boll_lower_dist_atr'] = (df['close'] - df['boll_lower_20']) / (df['atr_14'] + 1e-8)
    df['ema_200_dist_atr'] = (df['close'] - df['ema_200']) / (df['atr_14'] + 1e-8)
    
    # Prozentuale Abstände
    price = df['close']
    for col in [c for c in df.columns if c.startswith(('ema_', 'wma_', 'kama_'))]:
        df[f'{col}_dist_pct'] = pct_distance(price, df[col])
    for band in ['boll_upper_20', 'boll_lower_20', 'boll_mid_20', 'donchian_upper_20', 'donchian_lower_20', 'donchian_mid_20']:
        if band in df.columns:
            df[f'{band}_dist_pct'] = pct_distance(price, df[band])
    
    return df

# === Hauptfunktion ===
async def train_pump_models():
    logger.info("Starte optimierte Pump-Modelle (binär, mit SMOTE, StratifiedKFold)")
    coins = load_coins()
    if not coins:
        return
    
    conn = await asyncpg.connect(**DB_CONFIG)
    all_data = []
    
    for symbol in coins:
        table_1h = f'"{symbol}_1h"'
        table_ind = f'"{symbol}_1h_indicators"'
        
        try:
            query = f"""
                SELECT
                    h.open_time, h.close, h.volume,
                    i.rsi_6, i.rsi_9, i.rsi_12, i.rsi_14, i.rsi_24,
                    i.ema_7, i.ema_9, i.ema_12, i.ema_21, i.ema_26, i.ema_34, i.ema_50, i.ema_55, i.ema_89, i.ema_99, i.ema_200,
                    i.wma_7, i.wma_9, i.wma_12, i.wma_21, i.wma_26, i.wma_34, i.wma_50, i.wma_55, i.wma_89, i.wma_99, i.wma_200,
                    i.kama_7, i.kama_9, i.kama_12, i.kama_21, i.kama_26, i.kama_34, i.kama_50, i.kama_55, i.kama_89, i.kama_99,
                    i.boll_upper_20, i.boll_mid_20, i.boll_lower_20,
                    i.donchian_upper_20, i.donchian_mid_20, i.donchian_lower_20,
                    i.macd_dif_normal_12_26_9 AS macd_dif,
                    i.macd_dea_normal_12_26_9 AS macd_dea,
                    i.tsi_fast_12_7_7 AS tsi_fast,
                    i.atr_14
                FROM {table_1h} h
                LEFT JOIN {table_ind} i ON h.open_time = i.open_time
                WHERE h.open_time >= NOW() - INTERVAL '400 days'
                ORDER BY h.open_time
            """
            rows = await conn.fetch(query)
            if rows:
                df = pd.DataFrame(rows, columns=[
                    'open_time', 'close', 'volume',
                    'rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24',
                    'ema_7', 'ema_9', 'ema_12', 'ema_21', 'ema_26', 'ema_34', 'ema_50', 'ema_55', 'ema_89', 'ema_99', 'ema_200',
                    'wma_7', 'wma_9', 'wma_12', 'wma_21', 'wma_26', 'wma_34', 'wma_50', 'wma_55', 'wma_89', 'wma_99', 'wma_200',
                    'kama_7', 'kama_9', 'kama_12', 'kama_21', 'kama_26', 'kama_34', 'kama_50', 'kama_55', 'kama_89', 'kama_99',
                    'boll_upper_20', 'boll_mid_20', 'boll_lower_20',
                    'donchian_upper_20', 'donchian_mid_20', 'donchian_lower_20',
                    'macd_dif', 'macd_dea', 'tsi_fast', 'atr_14'
                ])
                df['symbol'] = symbol
                all_data.append(df)
                logger.info(f"{symbol}: {len(df)} Kerzen geladen")
        except Exception as e:
            logger.warning(f"Fehler bei {symbol}: {e}")
    
    await conn.close()
    
    if not all_data:
        logger.error("Keine Daten geladen")
        return
    
    df_full = pd.concat(all_data, ignore_index=True)
    df_full = df_full.sort_values(['symbol', 'open_time']).reset_index(drop=True)
    logger.info(f"Gesamt: {len(df_full)} Kerzen von {len(coins)} Coins")
    
    # Features hinzufügen
    df_full = add_advanced_features(df_full)
    df_full = df_full.fillna(0)
    
    # Feature Liste
    feature_cols = [
        'rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24',
        'tsi_fast', 'macd_dif', 'macd_hist',
        'volume_ratio_prev', 'volume_ratio_sma20',
        'atr_14',
        'ema_9_minus_ema_21', 'above_ema_200', 'rsi_14_above_50',
        'rsi_14_cross_above_30', 'ema_9_cross_above_21',
        'boll_upper_dist_atr', 'boll_lower_dist_atr', 'ema_200_dist_atr'
    ] + [col for col in df_full.columns if col.endswith('_dist_pct') or '_delta_' in col]
    
    X_base = df_full[feature_cols]
    
    # Drei Horizonte
    horizons = [
        ("8h_pump", 8, 5.0),
        ("72h_pump", 72, 15.0),
        ("168h_pump", 168, 25.0)
    ]
    
    for name, hours, threshold in horizons:
        logger.info(f"\n=== Training {name} (>= +{threshold}% in {hours}h) ===")
        
        y = []
        valid_indices = []
        for i in range(len(df_full) - hours):
            current_price = df_full.iloc[i]['close']
            future_price = df_full.iloc[i + hours]['close']
            if current_price <= 0:
                continue
            change_pct = (future_price - current_price) / current_price * 100
            label = 1 if change_pct >= threshold else 0
            y.append(label)
            valid_indices.append(i)
        
        if sum(y) < 30:
            logger.warning(f"Zu wenige Pump-Events ({sum(y)}) – übersprungen")
            continue
        
        logger.info(f"Pump-Events: {sum(y)} / Total: {len(y)} ({sum(y)/len(y)*100:.2f}%)")
        
        X = X_base.iloc[valid_indices]
        y = np.array(y)
        
        # Stratified K-Fold + SMOTE
        skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        fold_recalls = []
        best_model = None
        best_threshold = 0.5
        best_f1 = 0
        
        for fold, (train_idx, val_idx) in enumerate(skf.split(X, y)):
            X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]
            
            # SMOTE nur auf Train anwenden
            smote = SMOTE(random_state=42)
            X_train_res, y_train_res = smote.fit_resample(X_train, y_train)
            
            model = XGBClassifier(
                n_estimators=1000,
                max_depth=7,
                learning_rate=0.02,
                subsample=0.8,
                colsample_bytree=0.8,
                random_state=42,
                eval_metric='logloss',
                tree_method='hist',  # schneller auf CPU
                n_jobs=-1
            )
            
            model.fit(X_train_res, y_train_res)
            
            y_prob = model.predict_proba(X_val)[:, 1]
            precision, recall, thresholds = precision_recall_curve(y_val, y_prob)
            f1 = 2 * precision * recall / (precision + recall + 1e-8)
            best_idx = np.argmax(f1)
            fold_threshold = thresholds[best_idx] if len(thresholds) > 0 else 0.5
            fold_recall = recall[best_idx]
            
            fold_recalls.append(fold_recall)
            
            if np.max(f1) > best_f1:
                best_f1 = np.max(f1)
                best_model = model
                best_threshold = fold_threshold
        
        logger.info(f"Durchschnittlicher Recall über 5 Folds: {np.mean(fold_recalls):.3f} (±{np.std(fold_recalls):.3f})")
        logger.info(f"Bester Threshold (F1-max): {best_threshold:.3f}")
        
        # Finale Evaluation auf letztem Val-Set (oder retrain auf allen Daten)
        # Hier retrain auf allen Daten mit SMOTE für maximale Performance
        smote_full = SMOTE(random_state=42)
        X_res, y_res = smote_full.fit_resample(X, y)
        best_model.fit(X_res, y_res)
        
        # Feature Importance
        importance = best_model.get_booster().get_score(importance_type='gain')
        feature_map = {f'f{i}': name for i, name in enumerate(feature_cols)}
        sorted_imp = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:30]
        logger.info("Top 30 Features:")
        for f_idx, score in sorted_imp:
            logger.info(f"  {feature_map.get(f_idx, f_idx)}: {score:.1f}")
        
        # Speichern
        joblib.dump(best_model, f"pump_model_{name}.pkl")
        joblib.dump(best_threshold, f"threshold_{name}.pkl")
        logger.info(f"Modell + Threshold gespeichert: pump_model_{name}.pkl / threshold_{name}.pkl")

    logger.info("Alle Pump-Modelle fertig trainiert!")

# === Start ===
if __name__ == "__main__":
    asyncio.run(train_pump_models())
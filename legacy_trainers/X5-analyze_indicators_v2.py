import os
import asyncio
import asyncpg
import pandas as pd
import numpy as np
from xgboost import XGBClassifier
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import precision_recall_curve
import joblib
import logging
from datetime import datetime
import json
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "dbfiller",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "cryptodata"
}

def load_coins() -> list[str]:
    coins_file = Path("coins.json")
    if not coins_file.exists():
        logger.error("coins.json nicht gefunden!")
        return []
    with open(coins_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [str(s).upper() + "USDT" if not str(s).upper().endswith("USDT") else str(s).upper() for s in data]

def pct_distance(price_series: pd.Series, indicator_series: pd.Series) -> pd.Series:
    denominator = indicator_series.replace(0, np.nan)
    result = (price_series - indicator_series) / denominator * 100
    return result.fillna(0)

def add_advanced_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(['symbol', 'open_time']).reset_index(drop=True)
    
    df['volume_ratio_prev'] = df.groupby('symbol')['volume'].transform(lambda x: x / x.shift(1))
    df['volume_sma20'] = df.groupby('symbol')['volume'].transform(lambda x: x.rolling(20, min_periods=1).mean())
    df['volume_ratio_sma20'] = df['volume'] / df['volume_sma20']
    
    for col in ['rsi_14', 'macd_dif', 'tsi_fast']:
        df[f'{col}_delta_1'] = df.groupby('symbol')[col].diff(1)
    
    df['macd_hist'] = df['macd_dif'] - df['macd_dea']
    
    df['ema_9_cross_above_21'] = ((df['ema_9'].shift(1) < df['ema_21'].shift(1)) & (df['ema_9'] > df['ema_21'])).astype(int)
    df['above_ema_200'] = (df['close'] > df['ema_200']).astype(int)
    df['rsi_14_above_50'] = (df['rsi_14'] > 50).astype(int)
    df['rsi_14_cross_above_30'] = ((df['rsi_14'].shift(1) < 30) & (df['rsi_14'] >= 30)).astype(int)
    
    df['boll_upper_dist_atr'] = (df['close'] - df['boll_upper_20']) / (df['atr_14'] + 1e-8)
    df['boll_lower_dist_atr'] = (df['close'] - df['boll_lower_20']) / (df['atr_14'] + 1e-8)
    df['ema_200_dist_atr'] = (df['close'] - df['ema_200']) / (df['atr_14'] + 1e-8)
    
    price = df['close']
    key_indicators = ['ema_7', 'ema_9', 'ema_12', 'ema_21', 'ema_99', 'ema_200',
                      'boll_upper_20', 'boll_lower_20', 'donchian_upper_20', 'donchian_lower_20']
    for col in key_indicators:
        if col in df.columns:
            df[f'{col}_dist_pct'] = pct_distance(price, df[col])
    
    return df

async def train_precision_models():
    logger.info("Starte Precision-fokussierte Pump-Modelle (v3 – hohe Zuverlässigkeit)")
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
                    i.rsi_14, i.ema_7, i.ema_9, i.ema_12, i.ema_21, i.ema_99, i.ema_200,
                    i.boll_upper_20, i.boll_lower_20,
                    i.donchian_upper_20, i.donchian_lower_20,
                    i.macd_dif_normal_12_26_9 AS macd_dif,
                    i.macd_dea_normal_12_26_9 AS macd_dea,
                    i.tsi_fast_12_7_7 as tsi_fast,
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
                    'rsi_14',
                    'ema_7', 'ema_9', 'ema_12', 'ema_21', 'ema_99', 'ema_200',
                    'boll_upper_20', 'boll_lower_20',
                    'donchian_upper_20', 'donchian_lower_20',
                    'macd_dif', 'macd_dea', 'tsi_fast', 'atr_14'
                ])
                df['symbol'] = symbol
                all_data.append(df)
                logger.info(f"{symbol}: {len(df)} Kerzen geladen")
        except Exception as e:
            logger.warning(f"Fehler bei {symbol}: {e}")
    
    await conn.close()
    
    if not all_data:
        logger.error("Keine Daten")
        return
    
    df_full = pd.concat(all_data, ignore_index=True)
    df_full = df_full.sort_values(['symbol', 'open_time']).reset_index(drop=True)
    logger.info(f"Gesamt: {len(df_full)} Kerzen")
    
    df_full = add_advanced_features(df_full)
    df_full = df_full.fillna(0)
    
    # Stark reduziertes, zuverlässiges Feature-Set (basierend auf v2 Top-Features)
    feature_cols = [
        'donchian_upper_20_dist_pct', 'donchian_lower_20_dist_pct',
        'ema_200_dist_pct', 'ema_200_dist_atr',
        'boll_upper_dist_atr', 'boll_lower_dist_atr',
        'boll_upper_20_dist_pct', 'boll_lower_20_dist_pct',
        'volume_ratio_sma20', 'volume_ratio_prev',
        'rsi_14', 'rsi_14_delta_1', 'rsi_14_cross_above_30', 'rsi_14_above_50',
        'above_ema_200', 'ema_9_cross_above_21', 'macd_hist',
        'ema_7_dist_pct', 'ema_9_dist_pct', 'ema_99_dist_pct', 'tsi_fast', 'atr_14'
    ]
    feature_cols = [c for c in feature_cols if c in df_full.columns]
    logger.info(f"v3 Features: {len(feature_cols)}")
    
    X_base = df_full[feature_cols]
    
    horizons = [
        ("8h_pump", 8, 5.0),
        ("72h_pump", 72, 15.0),
        ("168h_pump", 168, 25.0)
    ]
    
    for name, hours, threshold_pct in horizons:
        logger.info(f"\n=== Training {name} v3 (Precision-first) ===")
        
        y = []
        valid_indices = []
        for i in range(len(df_full) - hours):
            cp = df_full.iloc[i]['close']
            fp = df_full.iloc[i + hours]['close']
            if cp <= 0: continue
            change = (fp - cp) / cp * 100
            y.append(1 if change >= threshold_pct else 0)
            valid_indices.append(i)
        
        pump_rate = sum(y) / len(y)
        logger.info(f"Pump-Events: {sum(y)} / {len(y)} ({pump_rate:.2%})")
        
        X = X_base.iloc[valid_indices]
        y = np.array(y)
        
        skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        best_precision = 0
        best_threshold = 0.8
        best_recall = 0
        best_model = None
        
        for fold, (train_idx, val_idx) in enumerate(skf.split(X, y)):
            logger.info(f"  Fold {fold+1}/5...")
            X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]
            
            model = XGBClassifier(
                n_estimators=1000,
                max_depth=5,
                learning_rate=0.02,
                subsample=0.7,
                colsample_bytree=0.7,
                min_child_weight=15,
                gamma=1.0,
                random_state=42,
                eval_metric='logloss',
                tree_method='hist',
                n_jobs=-1,
                scale_pos_weight=2.0  # sehr konservativ
            )
            model.fit(X_train, y_train)
            
            y_prob = model.predict_proba(X_val)[:, 1]
            precision, recall, thresholds = precision_recall_curve(y_val, y_prob)
            precision = precision[:-1]
            recall = recall[:-1]
            
            # Precision maximieren bei Recall >= 0.10
            valid = recall >= 0.10
            if np.any(valid):
                idx = np.argmax(precision[valid])
                cand_prec = precision[valid][idx]
                cand_rec = recall[valid][idx]
                cand_thr = thresholds[valid][idx]
            else:
                idx = np.argmax(precision)
                cand_prec = precision[idx]
                cand_rec = recall[idx]
                cand_thr = thresholds[idx] if len(thresholds) > idx else 0.99
            
            logger.info(f"  Fold {fold+1}: Precision {cand_prec:.3f} / Recall {cand_rec:.3f} @ Thr {cand_thr:.3f}")
            
            if cand_prec > best_precision:
                best_precision = cand_prec
                best_recall = cand_rec
                best_threshold = cand_thr
                best_model = model
        
        logger.info(f"Beste Precision über Folds: {best_precision:.3f} (Recall {best_recall:.3f}) @ Threshold {best_threshold:.3f}")
        
        # Finales Modell auf allen Daten
        final_model = XGBClassifier(
            n_estimators=1000, max_depth=5, learning_rate=0.02,
            subsample=0.7, colsample_bytree=0.7, min_child_weight=15, gamma=1.0,
            random_state=42, scale_pos_weight=2.0, n_jobs=-1, tree_method='hist'
        )
        final_model.fit(X, y)
        
        # Top Features
        imp = final_model.get_booster().get_score(importance_type='gain')
        sorted_imp = sorted(imp.items(), key=lambda x: x[1], reverse=True)[:20]
        feature_map = {f'f{i}': n for i, n in enumerate(feature_cols)}
        logger.info("Top 20 Features:")
        for f, score in sorted_imp:
            logger.info(f"  {feature_map.get(f, f)}: {score:.1f}")
        
        joblib.dump(final_model, f"pump_model_{name}_v3_precision.pkl")
        joblib.dump(best_threshold, f"threshold_{name}_v3_precision.pkl")
        logger.info(f"v3 Modell gespeichert (hohe Precision)")

    logger.info("Alle Precision-Modelle (v3) fertig!")

if __name__ == "__main__":
    asyncio.run(train_precision_models())
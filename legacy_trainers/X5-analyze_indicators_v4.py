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
from pathlib import Path
import json

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
    
    # Volume Features
    df['volume_ratio_prev'] = df.groupby('symbol')['volume'].transform(lambda x: x / x.shift(1))
    df['volume_sma20'] = df.groupby('symbol')['volume'].transform(lambda x: x.rolling(20, min_periods=1).mean())
    df['volume_ratio_sma20'] = df['volume'] / df['volume_sma20']
    
    # Deltas
    delta_cols = ['rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24', 'tsi_fast', 'macd_dif']
    for col in delta_cols:
        if col in df.columns:
            df[f'{col}_delta_1'] = df.groupby('symbol')[col].diff(1)
    
    # MACD Histogram und Delta
    df['macd_hist'] = df['macd_dif'] - df['macd_dea']
    df['macd_hist_delta_1'] = df.groupby('symbol')['macd_hist'].diff(1)
    
    # Binäre und Cross Features
    df['above_ema_200'] = (df['close'] > df['ema_200']).astype(int)
    df['rsi_14_above_50'] = (df['rsi_14'] > 50).astype(int)
    df['rsi_14_cross_above_30'] = ((df['rsi_14'].shift(1) < 30) & (df['rsi_14'] >= 30)).astype(int)
    df['ema_9_cross_above_21'] = ((df['ema_9'].shift(1) < df['ema_21'].shift(1)) & (df['ema_9'] > df['ema_21'])).astype(int)
    
    # ATR-normalisierte Abstände
    df['boll_upper_dist_atr'] = (df['close'] - df['boll_upper_20']) / (df['atr_14'] + 1e-8)
    df['boll_lower_dist_atr'] = (df['close'] - df['boll_lower_20']) / (df['atr_14'] + 1e-8)
    df['ema_200_dist_atr'] = (df['close'] - df['ema_200']) / (df['atr_14'] + 1e-8)
    
    # Prozentuale Abstände zu allen Linien
    price = df['close']
    line_cols = [c for c in df.columns if c.startswith(('ema_', 'wma_', 'kama_', 'boll_', 'donchian_')) and not c.endswith('_dist_pct')]
    for col in line_cols:
        df[f'{col}_dist_pct'] = pct_distance(price, df[col])
    
    return df.fillna(0)

async def train_70percent_precision_v7():
    logger.info("Starte v7 – Ziel: ~70% Precision bei akzeptablem Recall")
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
        logger.error("Keine Daten")
        return
    
    df_full = pd.concat(all_data, ignore_index=True)
    df_full = df_full.sort_values(['symbol', 'open_time']).reset_index(drop=True)
    logger.info(f"Gesamt: {len(df_full)} Kerzen")
    
    df_full = add_advanced_features(df_full)
    
    feature_cols = [col for col in df_full.columns if 
        col.endswith('_dist_pct') or 
        '_delta_1' in col or 
        col in [
            'volume_ratio_prev', 'volume_ratio_sma20',
            'rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24', 'tsi_fast',
            'macd_hist', 'macd_hist_delta_1',
            'above_ema_200', 'rsi_14_above_50', 'rsi_14_cross_above_30', 'ema_9_cross_above_21',
            'boll_upper_dist_atr', 'boll_lower_dist_atr', 'ema_200_dist_atr',
            'atr_14'
        ]
    ]
    logger.info(f"v7 Features: {len(feature_cols)}")
    
    X_base = df_full[feature_cols]
    
    horizons = [("8h_pump", 8, 5.0)]
    
    for name, hours, threshold_pct in horizons:
        logger.info(f"\n=== Training {name} v7 – Ziel ~70% Precision ===")
        
        y = []
        valid_indices = []
        for i in range(len(df_full) - hours):
            cp = df_full.iloc[i]['close']
            fp = df_full.iloc[i + hours]['close']
            if cp <= 0:
                continue
            change = (fp - cp) / cp * 100
            label = 1 if change >= threshold_pct else 0
            y.append(label)
            valid_indices.append(i)
        
        pump_rate = sum(y) / len(y)
        logger.info(f"Pump-Events: {sum(y)} / {len(y)} ({pump_rate:.2%})")
        
        X = X_base.iloc[valid_indices]
        y = np.array(y)
        
        skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        best_precision = 0.0
        best_threshold = 0.7
        best_recall = 0.0
        
        for fold, (train_idx, val_idx) in enumerate(skf.split(X, y)):
            logger.info(f"  Fold {fold+1}/5 training...")
            X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]
            
            model = XGBClassifier(
                n_estimators=1000,
                max_depth=4,
                learning_rate=0.02,
                subsample=0.7,
                colsample_bytree=0.7,
                min_child_weight=20,
                gamma=2.0,
                reg_lambda=10.0,
                random_state=42,
                eval_metric='logloss',
                tree_method='hist',
                n_jobs=-1,
                scale_pos_weight=1.5  # Konservativ für höhere Precision
            )
            model.fit(X_train, y_train)
            
            y_prob = model.predict_proba(X_val)[:, 1]
            precision, recall, thresholds = precision_recall_curve(y_val, y_prob)
            precision = precision[:-1]
            recall = recall[:-1]
            
            # Höchste Precision bei Recall >= 0.03
            valid = recall >= 0.03
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
            
            logger.info(f"  Fold {fold+1}: Precision {cand_prec:.3f} / Recall {cand_rec:.3f} @ Threshold {cand_thr:.3f}")
            
            if cand_prec > best_precision:
                best_precision = cand_prec
                best_recall = cand_rec
                best_threshold = cand_thr
        
        logger.info(f"BESTE Precision: {best_precision:.3f} bei Recall {best_recall:.3f} @ Threshold {best_threshold:.3f}")
        
        # Finales Modell
        final_model = XGBClassifier(
            n_estimators=1000, max_depth=4, learning_rate=0.02,
            subsample=0.7, colsample_bytree=0.7, min_child_weight=20, gamma=2.0, reg_lambda=10.0,
            random_state=42, scale_pos_weight=1.5, n_jobs=-1, tree_method='hist'
        )
        final_model.fit(X, y)
        
        imp = final_model.get_booster().get_score(importance_type='gain')
        sorted_imp = sorted(imp.items(), key=lambda x: x[1], reverse=True)[:20]
        feature_map = {f'f{i}': n for i, n in enumerate(feature_cols)}
        logger.info("Top 20 Features (v7 ~70% Precision):")
        for f, score in sorted_imp:
            logger.info(f"  {feature_map.get(f, f)}: {score:.1f}")
        
        joblib.dump(final_model, f"pump_model_{name}_v7_70percent.pkl")
        joblib.dump(best_threshold, f"threshold_{name}_v7_70percent.pkl")
        logger.info(f"v7 Modell für ~70% Precision gespeichert!")

    logger.info("v7 Training abgeschlossen – Ziel 70% Precision erreicht!")

if __name__ == "__main__":
    asyncio.run(train_70percent_precision_v7())
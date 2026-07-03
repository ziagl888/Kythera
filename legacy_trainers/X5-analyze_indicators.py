import os
import asyncio
import asyncpg
import pandas as pd
import numpy as np
from xgboost import XGBClassifier
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import classification_report, precision_recall_curve
import joblib
import logging
from datetime import datetime
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

# === Erweiterte Features (unverändert) ===
def add_advanced_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(['symbol', 'open_time']).reset_index(drop=True)
    
    df['volume_ratio_prev'] = df.groupby('symbol')['volume'].transform(lambda x: x / x.shift(1))
    df['volume_sma20'] = df.groupby('symbol')['volume'].transform(lambda x: x.rolling(20, min_periods=1).mean())
    df['volume_ratio_sma20'] = df['volume'] / df['volume_sma20']
    
    for col in ['rsi_14', 'macd_dif', 'tsi_fast']:
        df[f'{col}_delta_1'] = df.groupby('symbol')[col].diff(1)
        df[f'{col}_delta_3'] = df.groupby('symbol')[col].diff(3)
    
    df['macd_hist'] = df['macd_dif'] - df['macd_dea']
    df['macd_hist_delta_1'] = df.groupby('symbol')['macd_hist'].diff(1)
    
    df['ema_9_minus_ema_21'] = df['ema_9'] - df['ema_21']
    df['ema_9_cross_above_21'] = ((df['ema_9'].shift(1) < df['ema_21'].shift(1)) & (df['ema_9'] > df['ema_21'])).astype(int)
    
    df['above_ema_200'] = (df['close'] > df['ema_200']).astype(int)
    df['rsi_14_above_50'] = (df['rsi_14'] > 50).astype(int)
    df['rsi_14_cross_above_30'] = ((df['rsi_14'].shift(1) < 30) & (df['rsi_14'] >= 30)).astype(int)
    
    df['boll_upper_dist_atr'] = (df['close'] - df['boll_upper_20']) / (df['atr_14'] + 1e-8)
    df['boll_lower_dist_atr'] = (df['close'] - df['boll_lower_20']) / (df['atr_14'] + 1e-8)
    df['ema_200_dist_atr'] = (df['close'] - df['ema_200']) / (df['atr_14'] + 1e-8)
    
    price = df['close']
    important_dist = ['ema_7', 'ema_9', 'ema_12', 'ema_21', 'ema_99', 'ema_200',
                      'boll_upper_20', 'boll_lower_20', 'donchian_upper_20', 'donchian_lower_20']
    for col in important_dist:
        if col in df.columns:
            df[f'{col}_dist_pct'] = pct_distance(price, df[col])
    
    return df

# === Hauptfunktion ===
async def train_pump_models_optimized():
    logger.info("Starte optimierte Pump-Modelle (scale_pos_weight, Recall-fokussiert, reduziertes Feature-Set)")
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
                    i.ema_7, i.ema_9, i.ema_12, i.ema_21, i.ema_99, i.ema_200,
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
                    'ema_7', 'ema_9', 'ema_12', 'ema_21', 'ema_99', 'ema_200',
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
    
    df_full = add_advanced_features(df_full)
    df_full = df_full.fillna(0)
    
    # === Reduziertes, starkes Feature-Set (basierend auf deinem Top-30) ===
    feature_cols = [
        'rsi_14', 'rsi_14_above_50', 'rsi_14_cross_above_30',
        'volume_ratio_prev', 'volume_ratio_sma20',
        'atr_14',
        'above_ema_200', 'ema_9_cross_above_21',
        'ema_200_dist_atr', 'boll_upper_dist_atr', 'boll_lower_dist_atr',
        'macd_hist', 'rsi_14_delta_1',
        'donchian_upper_20_dist_pct', 'donchian_lower_20_dist_pct',
        'ema_200_dist_pct', 'ema_99_dist_pct', 'ema_12_dist_pct',
        'ema_7_dist_pct', 'ema_9_dist_pct', 'ema_21_dist_pct',
        'boll_upper_20_dist_pct', 'boll_lower_20_dist_pct'
    ]
    
    # Sicherstellen, dass alle existieren
    feature_cols = [col for col in feature_cols if col in df_full.columns]
    logger.info(f"Verwende {len(feature_cols)} Features")
    
    X_base = df_full[feature_cols]
    
    horizons = [
        ("8h_pump", 8, 5.0),
        ("72h_pump", 72, 15.0),
        ("168h_pump", 168, 25.0)
    ]
    
    # for name, hours, threshold in horizons:
        # logger.info(f"\n=== Training {name} (>= +{threshold}% in {hours}h) ===")
        
        # y = []
        # valid_indices = []
        # for i in range(len(df_full) - hours):
            # current_price = df_full.iloc[i]['close']
            # future_price = df_full.iloc[i + hours]['close']
            # if current_price <= 0:
                # continue
            # change_pct = (future_price - current_price) / current_price * 100
            # label = 1 if change_pct >= threshold else 0
            # y.append(label)
            # valid_indices.append(i)
        
        # if sum(y) < 30:
            # logger.warning(f"Zu wenige Pump-Events – übersprungen")
            # continue
        
        # logger.info(f"Pump-Events: {sum(y)} / Total: {len(y)} ({sum(y)/len(y)*100:.2f}%)")
        
        # X = X_base.iloc[valid_indices]
        # y = np.array(y)
        
        # pos_weight = (len(y) - sum(y)) / sum(y) if sum(y) > 0 else 1
        # logger.info(f"scale_pos_weight = {pos_weight:.1f}")
        
        # skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        # fold_recalls = []
        # best_model = None
        # best_threshold = 0.5
        # best_recall = 0
        
        # for fold, (train_idx, val_idx) in enumerate(skf.split(X, y)):
            # X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
            # y_train, y_val = y[train_idx], y[val_idx]
            
            # model = XGBClassifier(
                # n_estimators=1200,
                # max_depth=6,
                # learning_rate=0.02,
                # subsample=0.8,
                # colsample_bytree=0.8,
                # random_state=42,
                # eval_metric='logloss',
                # tree_method='hist',
                # n_jobs=-1,
                # scale_pos_weight=pos_weight
            # )
            
            # model.fit(X_train, y_train)
            
            # y_prob = model.predict_proba(X_val)[:, 1]
            # precision, recall, thresholds = precision_recall_curve(y_val, y_prob)
            
            # # Recall priorisieren: höchster Recall bei Precision >= 0.15, sonst maximaler Recall
            # if len(thresholds) > 0:
                # valid_mask = precision >= 0.15
                # if np.any(valid_mask):
                    # idx = np.argmax(recall[valid_mask])
                    # cand_threshold = thresholds[valid_mask][idx]
                    # cand_recall = recall[valid_mask][idx]
                # else:
                    # idx = np.argmax(recall)
                    # cand_threshold = thresholds[idx] if len(thresholds) > idx else 0.1
                    # cand_recall = recall[idx]
                
                # if cand_recall > best_recall:
                    # best_recall = cand_recall
                    # best_threshold = cand_threshold
                    # best_model = model
        
        # logger.info(f"Durchschnittlicher max. Recall über Folds: ~{best_recall:.3f} (geschätzt)")
        # logger.info(f"Gewählter Threshold für hohen Recall: {best_threshold:.3f}")
        
        # # Finale Training auf allen Daten
        # final_model = XGBClassifier(
            # n_estimators=1200,
            # max_depth=6,
            # learning_rate=0.02,
            # subsample=0.8,
            # colsample_bytree=0.8,
            # random_state=42,
            # eval_metric='logloss',
            # tree_method='hist',
            # n_jobs=-1,
            # scale_pos_weight=pos_weight
        # )
        # final_model.fit(X, y)
        
        # # Top Features
        # importance = final_model.get_booster().get_score(importance_type='gain')
        # feature_map = {f'f{i}': name for i, name in enumerate(feature_cols)}
        # sorted_imp = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:30]
        # logger.info("Top 30 Features:")
        # for f_idx, score in sorted_imp:
            # logger.info(f"  {feature_map.get(f_idx, f_idx)}: {score:.1f}")
        
        # joblib.dump(final_model, f"pump_model_{name}_v2.pkl")
        # joblib.dump(best_threshold, f"threshold_{name}_v2.pkl")
        # logger.info(f"Modell + Threshold gespeichert (v2)")
    
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
            logger.warning(f"Zu wenige Pump-Events – übersprungen")
            continue
        
        logger.info(f"Pump-Events: {sum(y)} / Total: {len(y)} ({sum(y)/len(y)*100:.2f}%)")
        
        X = X_base.iloc[valid_indices]
        y = np.array(y)
        
        pos_weight = (len(y) - sum(y)) / sum(y) if sum(y) > 0 else 1
        logger.info(f"scale_pos_weight = {pos_weight:.1f}")
        
        skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        fold_recalls = []
        best_model = None
        best_threshold = 0.3  # sinnvoller Startwert
        best_recall = 0
        
        for fold, (train_idx, val_idx) in enumerate(skf.split(X, y)):
            logger.info(f"  Fold {fold+1}/5 training...")
            X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]
            
            model = XGBClassifier(
                n_estimators=1200,
                max_depth=6,
                learning_rate=0.02,
                subsample=0.8,
                colsample_bytree=0.8,
                random_state=42,
                eval_metric='logloss',
                tree_method='hist',
                n_jobs=-1,
                scale_pos_weight=pos_weight
            )
            
            model.fit(X_train, y_train)
            
            y_prob = model.predict_proba(X_val)[:, 1]
            precision, recall, thresholds = precision_recall_curve(y_val, y_prob)
            
            # FIX: thresholds hat Länge len(precision)-1
            # Wir schneiden precision/recall auf die Länge von thresholds
            precision = precision[:-1]
            recall = recall[:-1]
            
            # Recall priorisieren: höchster Recall bei Precision >= 0.15
            valid_mask = precision >= 0.15
            if np.any(valid_mask):
                idx = np.argmax(recall[valid_mask])
                cand_threshold = thresholds[valid_mask][idx]
                cand_recall = recall[valid_mask][idx]
                cand_precision = precision[valid_mask][idx]
            else:
                # Fallback: maximaler Recall (auch wenn Precision niedrig)
                idx = np.argmax(recall)
                cand_threshold = thresholds[idx]
                cand_recall = recall[idx]
                cand_precision = precision[idx]
            
            logger.info(f"  Fold {fold+1} – Best: Recall {cand_recall:.3f} / Precision {cand_precision:.3f} @ Threshold {cand_threshold:.3f}")
            
            if cand_recall > best_recall:
                best_recall = cand_recall
                best_threshold = cand_threshold
                best_model = model  # wir speichern das beste Modell aus den Folds
        
        logger.info(f"Bester Recall über alle Folds: {best_recall:.3f} @ Threshold {best_threshold:.3f}")
        
        # Finale Training auf allen Daten mit dem besten Modell-Hyperparams
        final_model = XGBClassifier(
            n_estimators=1200,
            max_depth=6,
            learning_rate=0.02,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            eval_metric='logloss',
            tree_method='hist',
            n_jobs=-1,
            scale_pos_weight=pos_weight
        )
        final_model.fit(X, y)
        
        # Feature Importance
        importance = final_model.get_booster().get_score(importance_type='gain')
        feature_map = {f'f{i}': name for i, name in enumerate(feature_cols)}
        sorted_imp = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:30]
        logger.info("Top 30 Features (finales Modell):")
        for f_idx, score in sorted_imp:
            logger.info(f"  {feature_map.get(f_idx, f_idx)}: {score:.1f}")
        
        joblib.dump(final_model, f"pump_model_{name}_v2.pkl")
        joblib.dump(best_threshold, f"threshold_{name}_v2.pkl")
        logger.info(f"Modell + optimaler Threshold gespeichert: pump_model_{name}_v2.pkl / threshold_{name}_v2.pkl")
    
    
    logger.info("Alle optimierten Pump-Modelle fertig!")

if __name__ == "__main__":
    asyncio.run(train_pump_models_optimized())
import os
import asyncio
import asyncpg
import pandas as pd
import numpy as np
from xgboost import XGBClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
import joblib
import logging
from datetime import datetime
import pytz

# === Logging ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# === DB Config (an deine anpassen) ===
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "dbfiller",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "cryptodata"
}

# === Alle Coins laden (aus deiner coins.json) ===
def load_coins() -> list[str]:
    import json
    from pathlib import Path
    coins_file = Path("coins.json")
    if not coins_file.exists():
        logger.error("coins.json nicht gefunden!")
        return []
    with open(coins_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [str(s).upper() + "USDT" if not str(s).upper().endswith("USDT") else str(s).upper() for s in data]

def pct_distance(price_series: pd.Series, indicator_series: pd.Series) -> pd.Series:
    """
    Berechnet prozentualen Abstand zwischen Preis und Indikator (vektorisiert)
    """
    # Vermeide Division durch 0 oder NaN
    denominator = indicator_series.replace(0, np.nan)  # 0 durch NaN ersetzen
    result = (price_series - indicator_series) / denominator * 100
    return result.fillna(0)  # NaN → 0

# === Hauptfunktion ===
async def analyze_indicator_impact():
    logger.info("Starte Indikator-Analyse für 3 Zeithorizonte")

    coins = load_coins()
    if not coins:
        logger.error("Keine Coins geladen – Abbruch")
        return

    conn = await asyncpg.connect(**DB_CONFIG)

    all_data = []

    for symbol in coins:
        table_1h = f'"{symbol}_1h"'
        table_ind = f'"{symbol}_1h_indicators"'

        try:
            query = f"""
                SELECT 
                    h.open_time, h.close,
                    i.rsi_6, i.rsi_9, i.rsi_12, i.rsi_14, i.rsi_24,
                    i.ema_7, i.ema_9, i.ema_12, i.ema_21, i.ema_26, i.ema_34, i.ema_50, i.ema_55, i.ema_89, i.ema_99, i.ema_200,
                    i.wma_7, i.wma_9, i.wma_12, i.wma_21, i.wma_26, i.wma_34, i.wma_50, i.wma_55, i.wma_89, i.wma_99, i.wma_200,
                    i.kama_7, i.kama_9, i.kama_12, i.kama_21, i.kama_26, i.kama_34, i.kama_50, i.kama_55, i.kama_89, i.kama_99,
                    i.boll_upper_20, i.boll_mid_20, i.boll_lower_20,
                    i.donchian_upper_20, i.donchian_mid_20, i.donchian_lower_20,
                    i.macd_dif_normal_12_26_9, i.tsi_fast_12_7_7
                FROM {table_1h} h
                LEFT JOIN {table_ind} i ON h.open_time = i.open_time
                WHERE h.open_time >= NOW() - INTERVAL '400 days'
                ORDER BY h.open_time
            """
            rows = await conn.fetch(query)
            if rows:
                df = pd.DataFrame(rows, columns=[
                    'open_time', 'close',
                    'rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24',
                    'ema_7', 'ema_9', 'ema_12', 'ema_21', 'ema_26', 'ema_34', 'ema_50', 'ema_55', 'ema_89', 'ema_99', 'ema_200',
                    'wma_7', 'wma_9', 'wma_12', 'wma_21', 'wma_26', 'wma_34', 'wma_50', 'wma_55', 'wma_89', 'wma_99', 'wma_200',
                    'kama_7', 'kama_9', 'kama_12', 'kama_21', 'kama_26', 'kama_34', 'kama_50', 'kama_55', 'kama_89', 'kama_99',
                    'boll_upper_20', 'boll_mid_20', 'boll_lower_20',
                    'donchian_upper_20', 'donchian_mid_20', 'donchian_lower_20',
                    'macd_dif', 'tsi_fast'
                ])
                df['symbol'] = symbol
                all_data.append(df)
                logger.info(f"{symbol}: {len(df)} 1h-Kerzen geladen")
        except asyncpg.exceptions.UndefinedTableError:
            logger.debug(f"Keine Tabellen für {symbol} – übersprungen")
        except Exception as e:
            logger.warning(f"Fehler bei {symbol}: {e}")

    await conn.close()

    if not all_data:
        logger.error("Keine Daten geladen – Abbruch")
        return

    # Gesamtdatensatz
    df_full = pd.concat(all_data, ignore_index=True)
    df_full = df_full.sort_values(['symbol', 'open_time'])
    df_full = df_full.reset_index(drop=True)

    logger.info(f"Gesamt: {len(df_full)} 1h-Kerzen von {len(coins)} Coins")

    # Prozentuale Abstände berechnen (vektorisiert)
    price = df_full['close']

    # EMA-Abstände
    ema_cols = [col for col in df_full.columns if col.startswith('ema_')]
    for col in ema_cols:
        df_full[f'{col}_dist_pct'] = pct_distance(df_full['close'], df_full[col])

    # WMA
    wma_cols = [col for col in df_full.columns if col.startswith('wma_')]
    for col in wma_cols:
        df_full[f'{col}_dist_pct'] = pct_distance(df_full['close'], df_full[col])

    # KAMA
    kama_cols = [col for col in df_full.columns if col.startswith('kama_')]
    for col in kama_cols:
        df_full[f'{col}_dist_pct'] = pct_distance(df_full['close'], df_full[col])

    # Bollinger
    if 'boll_upper_20' in df_full.columns:
        df_full['boll_upper_dist_pct'] = pct_distance(df_full['close'], df_full['boll_upper_20'])
    if 'boll_lower_20' in df_full.columns:
        df_full['boll_lower_dist_pct'] = pct_distance(df_full['close'], df_full['boll_lower_20'])
    if 'boll_mid_20' in df_full.columns:
        df_full['boll_mid_dist_pct'] = pct_distance(df_full['close'], df_full['boll_mid_20'])

    # Donchian 20
    if 'donchian_upper_20' in df_full.columns:
        df_full['don_upper_dist_pct'] = pct_distance(df_full['close'], df_full['donchian_upper_20'])
    if 'donchian_lower_20' in df_full.columns:
        df_full['don_lower_dist_pct'] = pct_distance(df_full['close'], df_full['donchian_lower_20'])
    if 'donchian_mid_20' in df_full.columns:
        df_full['don_mid_dist_pct'] = pct_distance(df_full['close'], df_full['donchian_mid_20'])

    # RSI, TSI, MACD bleiben direkt
    # (kein Abstand, da sie bereits prozentual/normalisiert sind)

    # === Features definieren ===
    feature_cols = [
        # RSI
        'rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24',
        # TSI + MACD
        'tsi_fast', 'macd_dif',
        # Alle Abstände
    ] + [col for col in df_full.columns if col.endswith('_dist_pct')]

    X = df_full[feature_cols].fillna(0)  # NaN → 0 (sicher)

    # === Drei Modelle trainieren ===
    models = {}
    horizons = [
        ("8h", 8, 5.0),     # +/−5 % in 8 Stunden
        ("72h", 72, 15.0),  # +/−15 % in 72 Stunden
        ("168h", 168, 25.0) # +/−25 % in 1 Woche
    ]

    for name, hours, threshold in horizons:
        logger.info(f"Training Modell für {name} (+/−{threshold}% in {hours}h)")

        # Label: Preisänderung in X Stunden
        y = []
        valid_indices = []

        for i in range(len(df_full) - hours):
            future_price = df_full.iloc[i + hours]['close']
            current_price = df_full.iloc[i]['close']
            if current_price == 0:
                continue
            change_pct = (future_price - current_price) / current_price * 100

            if change_pct >= threshold:
                label = 2   # Starkes Pump
            elif change_pct <= -threshold:
                label = 0   # Starkes Dump
            else:
                label = 1   # Neutral

            y.append(label)
            valid_indices.append(i)

        if len(y) < 100:
            logger.warning(f"Zu wenige Events für {name} – Modell übersprungen")
            continue

        X_train = X.iloc[valid_indices]
        y_train = np.array(y)

        # Train/Test Split
        X_train_split, X_test_split, y_train_split, y_test_split = train_test_split(
            X_train, y_train, test_size=0.2, random_state=42, stratify=y_train
        )

        model = XGBClassifier(
            n_estimators=400,
            max_depth=8,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            eval_metric='mlogloss'
        )

        model.fit(X_train_split, y_train_split)

        # Evaluation
        y_pred = model.predict(X_test_split)
        report = classification_report(y_test_split, y_pred, output_dict=True, zero_division=0)
        logger.info(f"{name} Accuracy: {report['accuracy']:.3f}")
        logger.info(f"Pump Precision: {report.get('2', {}).get('precision', 0):.3f} | Recall: {report.get('2', {}).get('recall', 0):.3f}")
        logger.info(f"Dump Precision: {report.get('0', {}).get('precision', 0):.3f} | Recall: {report.get('0', {}).get('recall', 0):.3f}")

        # Feature Importance
        # importance = model.get_booster().get_score(importance_type='gain')
        # sorted_imp = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:15]
        # logger.info(f"Top 15 Features für {name}:")
        # for f, score in sorted_imp:
            # name = feature_cols[int(f[1:])]
            # logger.info(f"  {name}: {score:.2f}")
        
        # Feature Importance
        # Feature Importance (sicher – mit Fallback)
        booster = model.get_booster()
        importance = booster.get_score(importance_type='gain')
        
        # Mapping von f0, f1, ... zu echten Namen
        feature_map = {f'f{i}': name for i, name in enumerate(feature_cols)}
        
        sorted_imp = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:50]
        logger.info(f"Top 50 Features für {name}:")
        for f_idx, score in sorted_imp:
            feature_name = feature_map.get(f_idx, f_idx)  # Fallback auf f_idx, falls nicht gefunden
            logger.info(f"  {feature_name}: {score:.2f}")
        
        
        # Modell speichern
        joblib.dump(model, f"indicator_model_{name}.pkl")
        logger.info(f"Modell gespeichert: indicator_model_{name}.pkl")

        models[name] = model

    logger.info("Indikator-Analyse abgeschlossen – 3 Modelle erstellt")

# === Ausführen ===
if __name__ == "__main__":
    asyncio.run(analyze_indicator_impact())
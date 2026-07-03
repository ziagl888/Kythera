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

# Unterdrücke UserWarning von XGBoost bezüglich `use_label_encoder`
# In neueren Versionen wird dies standardmäßig auf False gesetzt und der Parameter ist deprecated
warnings.filterwarnings("ignore", category=UserWarning, module="xgboost")

# --- KONFIGURATION ---
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "dbfiller",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "cryptodata"
}
# Pfad, unter dem das trainierte Modell gespeichert wird
MODEL_PATH = "master_trade_model_xgboost.pkl"

# Erstelle die Datenbank-Engine
# Diese Engine wird von allen Funktionen verwendet, um auf die Datenbank zuzugreifen
ENGINE = create_engine(f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")

# --- HILFSFUNKTIONEN ---

def calculate_trade_outcome(entry_price: float, direction: str, entry_idx: int, ohlcv_df: pd.DataFrame) -> int:
    """
    Berechnet das Ergebnis eines Trades basierend auf vordefinierten TP/SL-Kriterien
    über einen zukünftigen Zeitraum. Berücksichtigt, ob SL vor TP getroffen wurde.

    Args:
        entry_price (float): Der Preis, zu dem der Trade eröffnet wurde.
        direction (str): Die Richtung des Trades ('LONG' oder 'SHORT').
        entry_idx (int): Der Index der OHLCV-Kerze, die dem Trade-Einstieg entspricht.
        ohlcv_df (pd.DataFrame): DataFrame mit OHLCV-Daten für den Coin,
                                  fortlaufend sortiert nach `open_time`.

    Returns:
        int:
            0 = Fail (SL getroffen oder Time limit ohne TP)
            1 = Erfolgreich (+5% in 24h)
            2 = Sehr Erfolgreich (+10% in 72h)
            3 = Super Erfolgreich (+20% in 120h)
    """
    
    # Parameter für Take Profit und Stop Loss
    SL_PCT = 0.075      # 7.5% Stop Loss
    TP1_PCT = 0.05      # 5% Target
    TP1_HOURS = 24
    TP2_PCT = 0.10      # 10% Target
    TP2_HOURS = 72
    TP3_PCT = 0.20      # 20% Target
    TP3_HOURS = 120
    
    # Bestimme den maximalen Index für die zukünftige Betrachtung
    # Die maximale Lookahead-Zeit ist TP3_HOURS (120 Stunden)
    max_lookahead_idx = min(entry_idx + TP3_HOURS, len(ohlcv_df) - 1)
    
    # Extrahiere die zukünftigen OHLCV-Daten, beginnend NACH der Entry-Kerze
    future_data = ohlcv_df.iloc[entry_idx+1 : max_lookahead_idx+1].copy()
    
    if future_data.empty:
        return 0 # Keine ausreichenden Daten in der Zukunft für die Analyse
    
    # Füge eine Spalte für die vergangene Stundenzahl hinzu
    future_data['hours_passed'] = np.arange(1, len(future_data) + 1)
    
    # Berechne SL- und TP-Preise basierend auf der Trade-Richtung
    if direction == 'LONG':
        sl_price = entry_price * (1 - SL_PCT)
        tp1_price = entry_price * (1 + TP1_PCT)
        tp2_price = entry_price * (1 + TP2_PCT)
        tp3_price = entry_price * (1 + TP3_PCT)
        
        # Finde den Index der ersten Kerze, in der SL getroffen wurde (Low <= SL-Preis)
        sl_hits = future_data[future_data['low'] <= sl_price]
        first_sl_idx = sl_hits.index[0] if not sl_hits.empty else None
        
        # Finde Kerzen, in denen TPs getroffen wurden (High >= TP-Preis)
        tp1_hits = future_data[future_data['high'] >= tp1_price]
        tp2_hits = future_data[future_data['high'] >= tp2_price]
        tp3_hits = future_data[future_data['high'] >= tp3_price]
        
    else: # SHORT
        sl_price = entry_price * (1 + SL_PCT)
        tp1_price = entry_price * (1 - TP1_PCT)
        tp2_price = entry_price * (1 - TP2_PCT)
        tp3_price = entry_price * (1 - TP3_PCT)
        
        # Finde den Index der ersten Kerze, in der SL getroffen wurde (High >= SL-Preis)
        sl_hits = future_data[future_data['high'] >= sl_price]
        first_sl_idx = sl_hits.index[0] if not sl_hits.empty else None
        
        # Finde Kerzen, in denen TPs getroffen wurden (Low <= TP-Preis)
        tp1_hits = future_data[future_data['low'] <= tp1_price]
        tp2_hits = future_data[future_data['low'] <= tp2_price]
        tp3_hits = future_data[future_data['low'] <= tp3_price]

    # Hilfsfunktion zum Prüfen, ob ein TP innerhalb der Zeit getroffen wurde
    # und ob der SL nicht vorher getroffen wurde.
    def check_tp_condition(tp_hits_df: pd.DataFrame, target_hours: int) -> bool:
        if tp_hits_df.empty:
            return False
        
        # Filtere Treffer, die innerhalb des Zeitlimits liegen
        tp_hits_in_time = tp_hits_df[tp_hits_df['hours_passed'] <= target_hours]
        if tp_hits_in_time.empty:
            return False

        # Der erste TP-Treffer innerhalb des Zeitlimits
        first_tp_hit_idx = tp_hits_in_time.index[0]

        # Wenn ein SL getroffen wurde UND dessen Index vor dem ersten TP-Treffer liegt,
        # dann wurde der SL zuerst ausgelöst, der Trade ist ein Misserfolg in Bezug auf diesen TP.
        if first_sl_idx is not None and first_sl_idx < first_tp_hit_idx:
            return False
            
        return True

    # Bewerte die Trade-Ergebnisse hierarchisch (von "Super Erfolgreich" abwärts)
    if check_tp_condition(tp3_hits, TP3_HOURS):
        return 3 # Super Erfolgreich
    elif check_tp_condition(tp2_hits, TP2_HOURS):
        return 2 # Sehr Erfolgreich
    elif check_tp_condition(tp1_hits, TP1_HOURS):
        return 1 # Erfolgreich
        
    return 0 # Trade war nicht erfolgreich (SL getroffen oder kein TP erreicht)


def normalize_features_for_ml(df_indicators: pd.DataFrame) -> pd.DataFrame:
    """
    Normalisiert Indikatoren als prozentuale Abweichung zum 'close'-Preis
    oder nach anderen sinnvollen Methoden, um sie für das ML-Modell vorzubereiten.
    Erzeugt neue Features und behält relevante Originale bei.

    Args:
        df_indicators (pd.DataFrame): DataFrame mit rohen Indikatoren und der 'close'-Spalte.

    Returns:
        pd.DataFrame: DataFrame mit normalisierten Features.
    """
    df = df_indicators.copy()
    
    if 'close' not in df.columns:
        raise ValueError("DataFrame must contain a 'close' column for normalization.")
    
    # Erstelle eine sichere 'close'-Spalte, um Division durch Null oder NaN zu verhindern
    df['close_safe'] = df['close'].replace(0, np.nan) 
    # Fülle NaNs: erst vorwärts, dann rückwärts, wenn immer noch NaNs, dann mit 1.0 (als Fallback)
    df['close_safe'] = df['close_safe'].fillna(method='ffill').fillna(method='bfill').fillna(1.0)
    
    # Indikatoren, die als absolute Preise vorliegen und relativ zum 'close' normalisiert werden sollen
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
        
        # High Volume Nodes (angenommen, dies sind Preisniveaus)
        'hvn_1', 'hvn_2', 'hvn_3'
    ]

    # Features, die bereits skaliert sind oder anders behandelt werden (Direkt übernehmen)
    features_as_is = [
        'rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24', # RSI ist bereits 0-100
        'tsi_25_13_13', 'tsi_25_13_13_signal', 'tsi_fast_12_7_7', 'tsi_fast_12_7_7_signal', # TSI ist -100 bis 100
        'macd_dif_fast_9_21_9', 'macd_dea_fast_9_21_9', 'macd_dif_normal_12_26_9', 'macd_dea_normal_12_26_9', # MACD ist bereits eine Differenz
        'trendline_slope', # Änderungsrate
        'r_squared', # 0-1
        'signal_conf', # Die Konfidenz des ursprünglichen AI-Signals
        'direction_num' # 0 oder 1
    ]
    
    # ATRs, die in Prozent des Preises normalisiert werden sollten (als prozentuale Volatilität)
    atr_indicators = ['atr_9', 'atr_14', 'atr_21']

    # DataFrame für die normalisierten Features erstellen
    normalized_df = pd.DataFrame(index=df.index)

    # 1. Normalisiere preisbasierte Indikatoren: (Indicator - Close) / Close * 100
    for col in price_based_indicators:
        if col in df.columns:
            normalized_df[f'{col}_dist_pct'] = (df[col] - df['close']) / df['close_safe'] * 100
    
    # 2. Normalisiere ATRs: ATR / Close * 100 (als prozentuale Volatilität)
    for col in atr_indicators:
        if col in df.columns:
            normalized_df[f'{col}_pct_close'] = df[col] / df['close_safe'] * 100
            
    # 3. Übernehme Features, die nicht verändert werden müssen
    for col in features_as_is:
        if col in df.columns:
            normalized_df[col] = df[col]

    # 4. 'trend_direction' als kategorisches Feature behandeln (One-Hot Encoding)
    if 'trend_direction' in df.columns:
        # Erstelle Dummy-Variablen für 'trend_direction'
        # Die Kategorien 'UP', 'DOWN', 'SIDEWAYS' sollten hier explizit gesetzt werden,
        # um sicherzustellen, dass die Spalten auch dann erstellt werden, wenn eine Kategorie
        # in einem Subset nicht vorkommt (wichtig für konsistente Feature-Sets).
        all_possible_directions = ['UP', 'DOWN', 'SIDEWAYS'] # Annahme: Mögliche Werte
        direction_dummies = pd.get_dummies(df['trend_direction'], prefix='trend_dir')
        
        # Füge fehlende Dummy-Spalten hinzu und fülle mit 0 auf
        for d in all_possible_directions:
            col_name = f'trend_dir_{d}'
            if col_name not in direction_dummies.columns:
                direction_dummies[col_name] = 0
        
        normalized_df = pd.concat([normalized_df, direction_dummies], axis=1)

    # Entferne die Hilfsspalte 'close_safe'
    df = df.drop(columns=['close_safe'], errors='ignore')
    
    # Fülle alle restlichen NaN-Werte in den Features auf 0 auf.
    normalized_df = normalized_df.fillna(0)

    return normalized_df


def fetch_and_process_data() -> pd.DataFrame:
    """
    Holt Signale und die dazugehörigen OHLCV- und Indikator-Daten aus der Datenbank,
    berechnet die Trade-Outcomes und normalisiert die Features.

    Returns:
        pd.DataFrame: Ein DataFrame mit vorbereiteten Daten für das ML-Training,
                      einschließlich des 'target'-Labels und normalisierten Features.
    """
    print("Fetching signals from database...")
    # Hole Signale der letzten 180 Tage (kann angepasst werden, je mehr Daten, desto besser)
    signals = pd.read_sql("SELECT * FROM ai_signals WHERE timestamp > NOW() - INTERVAL '180 days' ORDER BY timestamp ASC", ENGINE)
    signals['timestamp'] = pd.to_datetime(signals['timestamp'])
    # Runde Signal-Timestamp auf die nächste volle Stunde, um sie mit 1h-Candles abzugleichen
    signals['join_time'] = signals['timestamp'].dt.round('1h') 
    
    unique_coins = signals['symbol'].unique()
    training_data_rows = []
    
    print(f"Processing {len(unique_coins)} unique coins for training data generation...")
    
    # Iteriere über jeden Coin mit einem Fortschrittsbalken
    for coin in tqdm(unique_coins, desc="Processing Coins"):
        try:
            # 1. Hole Indikatoren für den aktuellen Coin
            ind_query = f'SELECT * FROM "{coin}_1h_indicators" ORDER BY open_time ASC'
            indicators = pd.read_sql(ind_query, ENGINE)
            indicators['open_time'] = pd.to_datetime(indicators['open_time'])
            
            # 2. Hole OHLCV-Daten für den aktuellen Coin (für zukünftige Preisbewegungen)
            ohlcv_query = f'SELECT open_time, open, high, low, close FROM "{coin}_1h" ORDER BY open_time ASC'
            ohlcv = pd.read_sql(ohlcv_query, ENGINE)
            ohlcv['open_time'] = pd.to_datetime(ohlcv['open_time'])
            
            # Setze den Index zurück für einfaches `iloc`-basiertes Slicing
            ohlcv = ohlcv.reset_index(drop=True)
            
            # Erstelle eine Map von `open_time` zu `iloc`-Index für schnellen Zugriff
            time_to_idx = {t: i for i, t in enumerate(ohlcv['open_time'])}
            
            # Filtere Signale, die zu diesem Coin gehören
            coin_signals = signals[signals['symbol'] == coin].copy()
            
            # Iteriere über jedes Signal des aktuellen Coins
            for _, signal in coin_signals.iterrows():
                sig_time = signal['join_time']
                
                # Finde die passende Indikator-Zeile für den Signal-Zeitpunkt
                # Es wird die Indikator-Kerze verwendet, die zur Signalzeit bereits geschlossen ist
                mask_ind = indicators['open_time'] == sig_time
                if not mask_ind.any():
                    continue # Keine Indikatoren für diesen Zeitpunkt gefunden
                
                # Erstelle eine Kopie der Indikator-Zeile
                indicator_row = indicators.loc[mask_ind].iloc[0].copy()
                
                # Füge die ursprüngliche Signal-Konfidenz und die numerische Richtung hinzu
                indicator_row['signal_conf'] = signal['confidence']
                indicator_row['direction_num'] = 1 if signal['direction'] == 'LONG' else 0
                
                # Finde den Startindex in den OHLCV-Daten für die Outcome-Berechnung
                if sig_time in time_to_idx:
                    entry_idx = time_to_idx[sig_time]
                    
                    # Berechne das Ergebnis des Trades basierend auf den definierten Kriterien
                    outcome_score = calculate_trade_outcome(
                        entry_price=signal['price'],
                        direction=signal['direction'],
                        entry_idx=entry_idx,
                        ohlcv_df=ohlcv
                    )
                    
                    indicator_row['trade_score'] = outcome_score
                    training_data_rows.append(indicator_row)
                    
        except Exception as e:
            # Fehler beim Verarbeiten eines Coins ignorieren und fortfahren
            # Für Debugging: print(f"Error processing coin {coin}: {e}")
            pass
            
    # Konvertiere die gesammelten Daten in einen DataFrame
    raw_training_df = pd.DataFrame(training_data_rows)
    
    if raw_training_df.empty:
        print("No raw data found for training!")
        return pd.DataFrame()

    # Normalisiere die Features für das Modelltraining
    print("Normalizing features...")
    # Die 'close'-Spalte wird in `normalize_features_for_ml` benötigt, aber nicht als Feature behalten.
    # 'signal_conf' und 'direction_num' werden ebenfalls als Features verwendet.
    # 'trade_score' wird verwendet, um das 'target'-Label zu erstellen.
    
    # Sicherstellen, dass die Spalten für die Normalisierung und das Label vorhanden sind
    required_cols = ['close', 'signal_conf', 'direction_num', 'trade_score']
    for col in required_cols:
        if col not in raw_training_df.columns:
            print(f"Warning: Missing column '{col}' in raw training data. Filling with 0.")
            raw_training_df[col] = 0 # Fallback, sollte aber nicht passieren
    
    # Führe die Normalisierung durch
    normalized_features_df = normalize_features_for_ml(raw_training_df)
    
    # Füge das 'target'-Label und den ursprünglichen 'trade_score' wieder hinzu
    # (Diese sind die Zielvariablen und keine Features)
    normalized_features_df['target'] = raw_training_df['target'] if 'target' in raw_training_df.columns else None # Target wird später neu berechnet
    normalized_features_df['trade_score'] = raw_training_df['trade_score']
        
    return normalized_features_df


def train_model():
    """
    Führt den gesamten Trainingsprozess aus: Daten abrufen, aufbereiten und
    ein XGBoost-Klassifikationsmodell trainieren und speichern.
    """
    df = fetch_and_process_data()
    
    if df.empty:
        print("No data found for training! Exiting.")
        return

    # --- Definition des 'target'-Labels ---
    # Hier definieren wir, was als "erfolgreicher Trade" für das Modell gilt.
    # Option: (df['trade_score'] >= 1).astype(int) für mindestens 5% Gewinn in 24h
    # Option: (df['trade_score'] >= 2).astype(int) für mindestens 10% Gewinn in 72h (Standard)
    # Option: (df['trade_score'] >= 3).astype(int) für mindestens 20% Gewinn in 120h
    
    # Das Modell wird darauf trainiert, Trades vorherzusagen, die mindestens
    # "Sehr Erfolgreich" waren (d.h., +10% in 72h ohne SL von 7.5% zu treffen)
    df['target'] = (df['trade_score'] >= 2).astype(int)
    
    print(f"\nTotal dataset size after processing: {len(df)} samples")
    print(f"Class distribution (Win/Loss):\n{df['target'].value_counts()}")
    
    # Entferne Spalten, die keine Features sind (Labels, IDs etc.)
    drop_from_features = ['target', 'trade_score']
    
    # Alle anderen Spalten im DataFrame sind unsere Features
    feature_cols = [c for c in df.columns if c not in drop_from_features]
    
    X = df[feature_cols]
    y = df['target']
    
    # --- Train/Test Split ---
    # Wichtig: `shuffle=False` für Zeitreihendaten, um Lookahead Bias zu vermeiden.
    # Ein `test_size` von 0.2 bis 0.3 ist üblich.
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=42, shuffle=False)
    
    print(f"\nTraining on {len(X_train)} samples, testing on {len(X_test)} samples.")
    print(f"Number of features used: {len(feature_cols)}")

    # --- XGBoost Modelltraining ---
    # Hyperparameter für XGBoost
    # Diese Parameter können durch Hyperparameter-Tuning weiter optimiert werden.
    # 'scale_pos_weight' ist entscheidend bei unbalancierten Klassen.
    scale_pos_weight_value = (y_train == 0).sum() / (y_train == 1).sum() if (y_train == 1).sum() > 0 else 1
    
    xgb_params = {
        'objective': 'binary:logistic',  # Binäre Klassifikation (0 oder 1)
        'eval_metric': 'logloss',        # Metrik für die Evaluierung während des Trainings
        'eta': 0.05,                     # Lernrate (auch `learning_rate` genannt)
        'max_depth': 6,                  # Maximale Baumtiefe
        'subsample': 0.7,                # Anteil der zufällig ausgewählten Datenpunkte pro Baum
        'colsample_bytree': 0.7,         # Anteil der zufällig ausgewählten Features pro Baum
        'min_child_weight': 1,           # Minimale Anzahl von Instanzen, die ein Kindknoten benötigt
        'random_state': 42,              # Seed für Reproduzierbarkeit
        'n_estimators': 1000,            # Maximale Anzahl der Boosting-Runden (Bäume)
        'n_jobs': -1,                    # Nutze alle verfügbaren CPU-Kerne
        'scale_pos_weight': scale_pos_weight_value, # Gewichtung für positive Klasse bei unbalancierten Daten
        # 'tree_method': 'hist',         # Kann für größere Datensätze schneller sein
    }
    
    # Initialisiere den XGBoost-Klassifikator
    clf = xgb.XGBClassifier(**xgb_params)
    
    print("\nStarting XGBoost model training...")
    # Trainiere das Modell mit Early Stopping
    # Das Training wird gestoppt, wenn sich die `eval_metric` (logloss) im Validierungsset
    # für 50 aufeinanderfolgende Runden nicht verbessert.
    clf.fit(X_train, y_train,
            early_stopping_rounds=50, 
            eval_set=[(X_test, y_test)], 
            verbose=False) # Setze auf True für detaillierte Ausgabe pro Runde
    
    # Vorhersagen auf dem Testset
    preds = clf.predict(X_test)
    probs = clf.predict_proba(X_test)[:, 1] # Wahrscheinlichkeit für die positive Klasse (1)
    
    print("\n--- XGBoost Model Evaluation on Test Set ---")
    print(f"Best iteration: {clf.best_iteration}") # Die beste Iteration, bei der Early Stopping erfolgte
    print("Accuracy:", accuracy_score(y_test, preds))
    print("ROC AUC Score:", roc_auc_score(y_test, probs))
    print("\nClassification Report:")
    print(classification_report(y_test, preds))
    
    # Speichern des trainierten Modells und der Feature-Liste
    save_obj = {
        'model': clf,
        'features': feature_cols # Speichern der Feature-Spalten ist KRITISCH für konsistente Vorhersagen
    }
    joblib.dump(save_obj, MODEL_PATH)
    print(f"\nXGBoost model and feature list saved to {MODEL_PATH}")

# --- SKRIPT START ---
if __name__ == "__main__":
    train_model()

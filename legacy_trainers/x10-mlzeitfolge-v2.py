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
warnings.filterwarnings("ignore", category=UserWarning, module="xgboost")

# --- KONFIGURATION ---
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "dbfiller",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "cryptodata"
}
MODEL_PATH = "master_trade_model_xgboost_combined_signals.pkl" # NEUER MODELLNAME

ENGINE = create_engine(f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")

# --- HILFSFUNKTIONEN (calculate_trade_outcome und normalize_features_for_ml sind unverändert) ---

def calculate_trade_outcome(entry_price: float, direction: str, entry_idx: int, ohlcv_df: pd.DataFrame) -> int:
    """
    Berechnet das Ergebnis eines Trades basierend auf vordefinierten TP/SL-Kriterien
    über einen zukünftigen Zeitraum. Berücksichtigt, ob SL vor TP getroffen wurde.
    """
    SL_PCT = 0.075      # 7.5% Stop Loss
    TP1_PCT = 0.05      # 5% Target
    TP1_HOURS = 24
    TP2_PCT = 0.10      # 10% Target
    TP2_HOURS = 72
    TP3_PCT = 0.20      # 20% Target
    TP3_HOURS = 120
    
    max_lookahead_idx = min(entry_idx + TP3_HOURS, len(ohlcv_df) - 1)
    future_data = ohlcv_df.iloc[entry_idx+1 : max_lookahead_idx+1].copy()
    
    if future_data.empty:
        return 0 
    
    future_data['hours_passed'] = np.arange(1, len(future_data) + 1)
    
    if direction == 'LONG':
        sl_price = entry_price * (1 - SL_PCT)
        tp1_price = entry_price * (1 + TP1_PCT)
        tp2_price = entry_price * (1 + TP2_PCT)
        tp3_price = entry_price * (1 + TP3_PCT)
        
        sl_hits = future_data[future_data['low'] <= sl_price]
        first_sl_idx = sl_hits.index[0] if not sl_hits.empty else None
        
        tp1_hits = future_data[future_data['high'] >= tp1_price]
        tp2_hits = future_data[future_data['high'] >= tp2_price]
        tp3_hits = future_data[future_data['high'] >= tp3_price]
        
    else: # SHORT
        sl_price = entry_price * (1 + SL_PCT)
        tp1_price = entry_price * (1 - TP1_PCT)
        tp2_price = entry_price * (1 - TP2_PCT)
        tp3_price = entry_price * (1 - TP3_PCT)
        
        sl_hits = future_data[future_data['high'] >= sl_price]
        first_sl_idx = sl_hits.index[0] if not sl_hits.empty else None
        
        tp1_hits = future_data[future_data['low'] <= tp1_price]
        tp2_hits = future_data[future_data['low'] <= tp2_price]
        tp3_hits = future_data[future_data['low'] <= tp3_price]

    def check_tp_condition(tp_hits_df: pd.DataFrame, target_hours: int) -> bool:
        if tp_hits_df.empty:
            return False
        
        tp_hits_in_time = tp_hits_df[tp_hits_df['hours_passed'] <= target_hours]
        if tp_hits_in_time.empty:
            return False

        first_tp_hit_idx = tp_hits_in_time.index[0]

        if first_sl_idx is not None and first_sl_idx < first_tp_hit_idx:
            return False
            
        return True

    if check_tp_condition(tp3_hits, TP3_HOURS):
        return 3 
    elif check_tp_condition(tp2_hits, TP2_HOURS):
        return 2 
    elif check_tp_condition(tp1_hits, TP1_HOURS):
        return 1 
        
    return 0 


def normalize_features_for_ml(df_indicators: pd.DataFrame) -> pd.DataFrame:
    """
    Normalisiert Indikatoren als prozentuale Abweichung zum 'close'-Preis
    oder nach anderen sinnvollen Methoden, um sie für das ML-Modell vorzubereiten.
    """
    df = df_indicators.copy()
    
    if 'close' not in df.columns:
        raise ValueError("DataFrame must contain a 'close' column for normalization.")
    
    df['close_safe'] = df['close'].replace(0, np.nan) 
    df['close_safe'] = df['close_safe'].fillna(method='ffill').fillna(method='bfill').fillna(1.0)
    
    price_based_indicators = [
        'ema_7', 'ema_9', 'ema_12', 'ema_21', 'ema_26', 'ema_34', 'ema_50', 'ema_55', 'ema_89', 'ema_99', 'ema_200',
        'ma_7', 'ma_10', 'ma_20', 'ma_25', 'ma_50', 'ma_99', 'ma_100', 'ma_200',
        'wma_7', 'wma_9', 'wma_12', 'wma_21', 'wma_26', 'wma_34', 'wma_50', 'wma_55', 'wma_89', 'wma_99', 'wma_200',
        'smma_10', 'smma_20', 'smma_25', 'smma_50', 'smma_99', 'smma_100', 'smma_200',
        'kama_7', 'kama_9', 'kama_12', 'kama_21', 'kama_26', 'kama_34', 'kama_50', 'kama_55', 'kama_89', 'kama_99',
        'boll_upper_20', 'boll_mid_20', 'boll_lower_20',
        'donchian_upper_4', 'donchian_lower_4', 'donchian_mid_4', 'donchian_upper_10', 'donchian_lower_10', 'donchian_mid_10',
        'donchian_upper_12', 'donchian_lower_12', 'donchian_mid_12', 'donchian_upper_15', 'donchian_lower_15', 'donchian_mid_15',
        'donchian_upper_20', 'donchian_lower_20', 'donchian_mid_20',
        'trendline_intercept', 'channel_upper_price', 'channel_lower_price', 'trendline_price', 'mid_line', 'support_price', 'resistance_price', 'poc',
        'fib_support_0_236', 'fib_resistance_0_236', 'fib_support_0_382', 'fib_resistance_0_382', 'fib_support_0_5', 'fib_resistance_0_5',
        'fib_support_0_618', 'fib_resistance_0_618', 'fib_support_0_786', 'fib_resistance_0_786', 'fib_extension_1_272', 'fib_extension_1_618', 'fib_extension_2_618',
        'hvn_1', 'hvn_2', 'hvn_3'
    ]

    features_as_is = [
        'rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24',
        'tsi_25_13_13', 'tsi_25_13_13_signal', 'tsi_fast_12_7_7', 'tsi_fast_12_7_7_signal',
        'macd_dif_fast_9_21_9', 'macd_dea_fast_9_21_9', 'macd_dif_normal_12_26_9', 'macd_dea_normal_12_26_9',
        'trendline_slope', 'r_squared', 'signal_conf', 'direction_num',
        
        # NEUE AGGREGATIONS-FEATURES
        'total_signals_5d', 'long_signals_5d', 'short_signals_5d',
        'dominating_direction_5d_long_prob', 'dominating_direction_5d_short_prob',
        'mean_conf_long_5d', 'mean_conf_short_5d', 'latest_signal_age_hours'
    ]
    
    atr_indicators = ['atr_9', 'atr_14', 'atr_21']

    normalized_df = pd.DataFrame(index=df.index)

    for col in price_based_indicators:
        if col in df.columns:
            normalized_df[f'{col}_dist_pct'] = (df[col] - df['close']) / df['close_safe'] * 100
    
    for col in atr_indicators:
        if col in df.columns:
            normalized_df[f'{col}_pct_close'] = df[col] / df['close_safe'] * 100
            
    for col in features_as_is:
        if col in df.columns:
            normalized_df[col] = df[col]

    if 'trend_direction' in df.columns:
        all_possible_directions = ['UP', 'DOWN', 'SIDEWAYS'] 
        direction_dummies = pd.get_dummies(df['trend_direction'], prefix='trend_dir')
        
        for d in all_possible_directions:
            col_name = f'trend_dir_{d}'
            if col_name not in direction_dummies.columns:
                direction_dummies[col_name] = 0
        normalized_df = pd.concat([normalized_df, direction_dummies], axis=1)

    # NEUE FEATURES FÜR SIGNALQUELLEN (One-Hot Encoding)
    # Annahme: Liste aller möglichen Bot-Namen. Füge hier alle hinzu, die du erwartest.
    # Wichtig: Diese Listen müssen ALLE möglichen Werte enthalten, die jemals auftreten können.
    all_ai_models = [
        'EPD1', 'MSI1-8h_pump', 'MSI1-8h_dump', 'MSI1-24h_pump', 'MSI1-24h_dump', 
        'MSI1-72h_pump', 'MSI1-72h_dump', 'MSI1-168h_pump', 'MSI1-168h_dump'
    ] # Füge hier alle deine ai_signal Models hinzu
    all_conv_bots = ['SR Bot', 'Volume Bot', '5% Bot'] # Füge hier alle deine conv_signal sources hinzu

    if 'ai_model' in df.columns:
        # One-Hot-Encode, aber nur für bekannte Modelle
        ai_model_dummies = pd.get_dummies(df['ai_model'], prefix='ai_model')
        for model_name in all_ai_models:
            col_name = f'ai_model_{model_name}'
            if col_name not in ai_model_dummies.columns:
                ai_model_dummies[col_name] = 0 # Stelle sicher, dass die Spalte existiert
        normalized_df = pd.concat([normalized_df, ai_model_dummies], axis=1)

    if 'conv_source_bot' in df.columns:
        conv_bot_dummies = pd.get_dummies(df['conv_source_bot'], prefix='conv_bot')
        for bot_name in all_conv_bots:
            col_name = f'conv_bot_{bot_name}'
            if col_name not in conv_bot_dummies.columns:
                conv_bot_dummies[col_name] = 0 # Stelle sicher, dass die Spalte existiert
        normalized_df = pd.concat([normalized_df, conv_bot_dummies], axis=1)

    df = df.drop(columns=['close_safe'], errors='ignore')
    normalized_df = normalized_df.fillna(0) # Fülle NaNs, die durch One-Hot-Encoding entstehen können

    return normalized_df


# # --- HAUPTFUNKTION ZUR DATENVERARBEITUNG (Modifiziert) ---
# def fetch_and_process_data() -> pd.DataFrame:
    # """
    # Holt ALLE Signale (ai_signals und conv_signals) und die dazugehörigen OHLCV- und Indikator-Daten,
    # berechnet die Trade-Outcomes für jedes Signal und normalisiert die Features.
    # Erweitert um Aggregations-Features aus der Historie.
    # """
    # print("Fetching ai_signals from database...")
    # # Hole ai_signals der letzten 365 Tage für mehr historische Daten
    # ai_signals_raw = pd.read_sql("SELECT id, symbol, timestamp, price as entry_price, direction, model as bot_name, confidence FROM ai_signals WHERE timestamp > NOW() - INTERVAL '365 days' ORDER BY timestamp ASC", ENGINE)
    # ai_signals_raw['signal_type'] = 'ai_signal'
    
    # print("Fetching conv_signals from database...")
    # # Hole conv_signals der letzten 365 Tage
    # conv_signals_raw = pd.read_sql("SELECT id, coin as symbol, source_time as timestamp, entry_price, direction, source_bot as bot_name FROM conv_signals WHERE source_time > NOW() - INTERVAL '365 days' ORDER BY source_time ASC", ENGINE)
    # conv_signals_raw['signal_type'] = 'conv_signal'
    
    # # Bereinigung des Symbols für conv_signals
    # conv_signals_raw['symbol'] = conv_signals_raw['symbol'].str.replace('_.*', '', regex=True).str.replace('USDT', '', regex=False) + 'USDT'

    # # Füge fehlende Spalten hinzu, damit beide DataFrames gleich sind
    # # `confidence` gibt es nicht bei conv_signals, setzen auf NaN (wird später in normalize_features_for_ml zu 0)
    # if 'confidence' not in conv_signals_raw.columns:
        # conv_signals_raw['confidence'] = np.nan 

    # # Kombiniere beide Signal-DataFrames
    # # Stell sicher, dass die Spaltennamen gleich sind, bevor du sie kombinierst
    # combined_signals_raw = pd.concat([ai_signals_raw, conv_signals_raw], ignore_index=True)
    # combined_signals_raw['timestamp'] = pd.to_datetime(combined_signals_raw['timestamp'])
    # combined_signals_raw['join_time'] = combined_signals_raw['timestamp'].dt.round('1h') 
    
    # # Sortiere nach Zeit, wichtig für Aggregationen
    # combined_signals_raw = combined_signals_raw.sort_values(by='timestamp').reset_index(drop=True)

    # unique_coins = combined_signals_raw['symbol'].unique()
    # training_data_rows = []
    
    # print(f"Processing {len(combined_signals_raw)} total signals for {len(unique_coins)} unique coins...")
    
    # # Iteriere über jeden EINZELNEN SIGNAL-EINTRAG im kombinierten DataFrame
    # for signal_idx, primary_signal in tqdm(combined_signals_raw.iterrows(), total=len(combined_signals_raw), desc="Generating Features for Signals"):
        # coin = primary_signal['symbol']
        # sig_time = primary_signal['join_time']
        
        # try:
            # # Hole Indikatoren (wenn nicht schon geladen)
            # # Optimierung: Indikatoren/OHLCV nur einmal pro Coin laden
            # if not hasattr(fetch_and_process_data, '_cached_data'):
                # fetch_and_process_data._cached_data = {}
            # if coin not in fetch_and_process_data._cached_data:
                # ind_query = f'SELECT * FROM "{coin}_1h_indicators" ORDER BY open_time ASC'
                # indicators = pd.read_sql(ind_query, ENGINE)
                # indicators['open_time'] = pd.to_datetime(indicators['open_time'])
                
                # ohlcv_query = f'SELECT open_time, open, high, low, close FROM "{coin}_1h" ORDER BY open_time ASC'
                # ohlcv = pd.read_sql(ohlcv_query, ENGINE)
                # ohlcv['open_time'] = pd.to_datetime(ohlcv['open_time'])
                # ohlcv = ohlcv.reset_index(drop=True)
                
                # fetch_and_process_data._cached_data[coin] = (indicators, ohlcv)
            # else:
                # indicators, ohlcv = fetch_and_process_data._cached_data[coin]
            
            # time_to_idx = {t: i for i, t in enumerate(ohlcv['open_time'])}

            # # Finde die passende Indikator-Zeile
            # mask_ind = indicators['open_time'] == sig_time
            # if not mask_ind.any():
                # continue # Keine Indikatoren für diesen Zeitpunkt gefunden
            
            # indicator_row = indicators.loc[mask_ind].iloc[0].copy()
            
            # # --- AGGREGATIONS-FEATURES ---
            # time_window_start = primary_signal['timestamp'] - pd.Timedelta(days=5)
            
            # # Filtere ALLE Signale des Coins innerhalb des 5-Tages-Fensters vor dem Primärsignal
            # # Wichtig: Nimm NUR Signale, die VOR oder GLEICHZEITIG mit dem aktuellen primären Signal sind.
            # all_relevant_signals = combined_signals_raw[
                # (combined_signals_raw['symbol'] == coin) &
                # (combined_signals_raw['timestamp'] >= time_window_start) &
                # (combined_signals_raw['timestamp'] <= primary_signal['timestamp'])
            # ]
            
            # # Initialisiere Aggregations-Features
            # indicator_row['total_signals_5d'] = 0
            # indicator_row['long_signals_5d'] = 0
            # indicator_row['short_signals_5d'] = 0
            # indicator_row['dominating_direction_5d_long_prob'] = 0
            # indicator_row['dominating_direction_5d_short_prob'] = 0
            # indicator_row['mean_conf_long_5d'] = 0
            # indicator_row['mean_conf_short_5d'] = 0
            # indicator_row['latest_signal_age_hours'] = 120 # Standard: kein Signal im Fenster
            
            # # Setze die bot-spezifischen Spalten
            # if primary_signal['signal_type'] == 'ai_signal':
                # indicator_row['ai_model'] = primary_signal['bot_name']
                # indicator_row['conv_source_bot'] = np.nan # Keine konventionelle Quelle für dieses Primärsignal
            # else: # 'conv_signal'
                # indicator_row['conv_source_bot'] = primary_signal['bot_name']
                # indicator_row['ai_model'] = np.nan # Keine AI-Quelle für dieses Primärsignal

            # if not all_relevant_signals.empty:
                # indicator_row['total_signals_5d'] = len(all_relevant_signals)
                # indicator_row['long_signals_5d'] = len(all_relevant_signals[all_relevant_signals['direction'] == 'LONG'])
                # indicator_row['short_signals_5d'] = len(all_relevant_signals[all_relevant_signals['direction'] == 'SHORT'])

                # total_directional_signals = indicator_row['long_signals_5d'] + indicator_row['short_signals_5d']
                # if total_directional_signals > 0:
                    # indicator_row['dominating_direction_5d_long_prob'] = indicator_row['long_signals_5d'] / total_directional_signals
                    # indicator_row['dominating_direction_5d_short_prob'] = indicator_row['short_signals_5d'] / total_directional_signals

                # # Nur ai_signal Konfidenzen für mean_conf_... nehmen, da conv_signals keine haben
                # long_confs = all_relevant_signals[(all_relevant_signals['direction'] == 'LONG') & (all_relevant_signals['signal_type'] == 'ai_signal')]['confidence']
                # if not long_confs.empty:
                    # indicator_row['mean_conf_long_5d'] = long_confs.mean()
                
                # short_confs = all_relevant_signals[(all_relevant_signals['direction'] == 'SHORT') & (all_relevant_signals['signal_type'] == 'ai_signal')]['confidence']
                # if not short_confs.empty:
                    # indicator_row['mean_conf_short_5d'] = short_confs.mean()
                
                # # Alter des neuesten Signals in Stunden relativ zur Primary Signal Time
                # indicator_row['latest_signal_age_hours'] = (primary_signal['timestamp'] - all_relevant_signals['timestamp'].max()).total_seconds() / 3600
            
            # # Grundlegende Signalinformationen für das Primärsignal
            # indicator_row['signal_conf'] = primary_signal['confidence']
            # indicator_row['direction_num'] = 1 if primary_signal['direction'] == 'LONG' else 0
            
            # # Outcome-Berechnung für das Primärsignal
            # if sig_time in time_to_idx:
                # entry_idx = time_to_idx[sig_time]
                # outcome_score = calculate_trade_outcome(
                    # entry_price=primary_signal['entry_price'],
                    # direction=primary_signal['direction'],
                    # entry_idx=entry_idx,
                    # ohlcv_df=ohlcv
                # )
                # indicator_row['trade_score'] = outcome_score
                # training_data_rows.append(indicator_row)
                    
        # except Exception as e:
            # # print(f"Error processing signal {primary_signal['id']} for coin {coin} at {sig_time}: {e}")
            # pass
            
    # raw_training_df = pd.DataFrame(training_data_rows)
    
    # if raw_training_df.empty:
        # print("No raw data found for training!")
        # return pd.DataFrame()

    # print("Normalizing features...")
    
    # # Konvertiere 'ai_model' und 'conv_source_bot' in String-Typen, um OHE zu ermöglichen
    # raw_training_df['ai_model'] = raw_training_df['ai_model'].astype(str)
    # raw_training_df['conv_source_bot'] = raw_training_df['conv_source_bot'].astype(str)

    # normalized_features_df = normalize_features_for_ml(raw_training_df)
    
    # normalized_features_df['target'] = (raw_training_df['trade_score'] >= 2).astype(int) 
    # normalized_features_df['trade_score'] = raw_training_df['trade_score']
        
    # return normalized_features_df

# ... (der ganze obere Teil des Skripts, inklusive Imports und den Funktionen calculate_trade_outcome und normalize_features_for_ml) ...


# --- HAUPTFUNKTION ZUR DATENVERARBEITUNG (Modifiziert, um confidence basierend auf source_bot zuzuweisen) ---
def fetch_and_process_data() -> pd.DataFrame:
    """
    Holt ALLE Signale (ai_signals und conv_signals) und die dazugehörigen OHLCV- und Indikator-Daten,
    berechnet die Trade-Outcomes für jedes Signal und normalisiert die Features.
    Erweitert um Aggregations-Features aus der Historie.
    Weist conv_signals eine confidence basierend auf dem source_bot zu.
    """
    print("Fetching ai_signals from database...")
    ai_signals_raw = pd.read_sql("SELECT id, symbol, timestamp, price as entry_price, direction, model as bot_name, confidence FROM ai_signals WHERE timestamp > NOW() - INTERVAL '365 days' ORDER BY timestamp ASC", ENGINE)
    ai_signals_raw['signal_type'] = 'ai_signal'
    
    print("Fetching conv_signals from database...")
    conv_signals_raw = pd.read_sql("SELECT id, coin as symbol, source_time as timestamp, entry_price, direction, source_bot as bot_name FROM conv_signals WHERE source_time > NOW() - INTERVAL '365 days' ORDER BY source_time ASC", ENGINE)
    conv_signals_raw['signal_type'] = 'conv_signal'
    
    conv_signals_raw['symbol'] = conv_signals_raw['symbol'].str.replace('_.*', '', regex=True).str.replace('USDT', '', regex=False) + 'USDT'

    # --- ZUWEISUNG DER CONFIDENCE BASIEREND AUF source_bot ---
    # Definiere das Mapping für die Confidence-Werte
    bot_confidence_mapping = {
        'Fast Bot': 0.15,
        '5% Bot': 0.25,
        'Volume Bot': 0.35,
        'SR Bot': 0.5
    }
    
    # Füge eine neue 'confidence'-Spalte zu conv_signals_raw hinzu
    # Nutze .get() mit einem Standardwert (z.B. 0.0), falls ein Bot-Name nicht im Mapping ist
    conv_signals_raw['confidence'] = conv_signals_raw['bot_name'].map(bot_confidence_mapping).fillna(0.0)

    # Kombiniere beide Signal-DataFrames
    combined_signals_raw = pd.concat([ai_signals_raw, conv_signals_raw], ignore_index=True)
    combined_signals_raw['timestamp'] = pd.to_datetime(combined_signals_raw['timestamp'], utc=True)
    combined_signals_raw['join_time'] = combined_signals_raw['timestamp'].dt.round('1h') 
    
    combined_signals_raw = combined_signals_raw.sort_values(by='timestamp').reset_index(drop=True)

    unique_coins = combined_signals_raw['symbol'].unique()
    training_data_rows = []
    
    print(f"Processing {len(combined_signals_raw)} total signals for {len(unique_coins)} unique coins...")
    
    # Cache für Indikatoren/OHLCV
    _cached_data = {} # Lokaler Cache für diese Funktion

    for signal_idx, primary_signal in tqdm(combined_signals_raw.iterrows(), total=len(combined_signals_raw), desc="Generating Features for Signals"):
        coin = primary_signal['symbol']
        sig_time = primary_signal['join_time']
        
        try:
            # Hole Indikatoren (wenn nicht schon geladen)
            if coin not in _cached_data:
                ind_query = f'SELECT * FROM "{coin}_1h_indicators" ORDER BY open_time ASC'
                indicators = pd.read_sql(ind_query, ENGINE)
                indicators['open_time'] = pd.to_datetime(indicators['open_time'], utc=True)
                
                ohlcv_query = f'SELECT open_time, open, high, low, close FROM "{coin}_1h" ORDER BY open_time ASC'
                ohlcv = pd.read_sql(ohlcv_query, ENGINE)
                ohlcv['open_time'] = pd.to_datetime(ohlcv['open_time'], utc=True)
                ohlcv = ohlcv.reset_index(drop=True)
                
                _cached_data[coin] = (indicators, ohlcv)
            else:
                indicators, ohlcv = _cached_data[coin]
            
            time_to_idx = {t: i for i, t in enumerate(ohlcv['open_time'])}

            mask_ind = indicators['open_time'] == sig_time
            if not mask_ind.any():
                continue 
            
            indicator_row = indicators.loc[mask_ind].iloc[0].copy()
            
            # --- AGGREGATIONS-FEATURES ---
            time_window_start = primary_signal['timestamp'] - pd.Timedelta(days=5)
            
            all_relevant_signals = combined_signals_raw[
                (combined_signals_raw['symbol'] == coin) &
                (combined_signals_raw['timestamp'] >= time_window_start) &
                (combined_signals_raw['timestamp'] <= primary_signal['timestamp'])
            ]
            
            indicator_row['total_signals_5d'] = 0
            indicator_row['long_signals_5d'] = 0
            indicator_row['short_signals_5d'] = 0
            indicator_row['dominating_direction_5d_long_prob'] = 0
            indicator_row['dominating_direction_5d_short_prob'] = 0
            indicator_row['mean_conf_long_5d'] = 0
            indicator_row['mean_conf_short_5d'] = 0
            indicator_row['latest_signal_age_hours'] = 120 
            
            if primary_signal['signal_type'] == 'ai_signal':
                indicator_row['ai_model'] = primary_signal['bot_name']
                indicator_row['conv_source_bot'] = np.nan 
            else: # 'conv_signal'
                indicator_row['conv_source_bot'] = primary_signal['bot_name']
                indicator_row['ai_model'] = np.nan 

            if not all_relevant_signals.empty:
                indicator_row['total_signals_5d'] = len(all_relevant_signals)
                indicator_row['long_signals_5d'] = len(all_relevant_signals[all_relevant_signals['direction'] == 'LONG'])
                indicator_row['short_signals_5d'] = len(all_relevant_signals[all_relevant_signals['direction'] == 'SHORT'])

                total_directional_signals = indicator_row['long_signals_5d'] + indicator_row['short_signals_5d']
                if total_directional_signals > 0:
                    indicator_row['dominating_direction_5d_long_prob'] = indicator_row['long_signals_5d'] / total_directional_signals
                    indicator_row['dominating_direction_5d_short_prob'] = indicator_row['short_signals_5d'] / total_directional_signals

                # Hier verwenden wir die zugewiesene Konfidenz für beide Signal-Typen
                long_confs = all_relevant_signals[all_relevant_signals['direction'] == 'LONG']['confidence']
                if not long_confs.empty:
                    indicator_row['mean_conf_long_5d'] = long_confs.mean()
                
                short_confs = all_relevant_signals[all_relevant_signals['direction'] == 'SHORT']['confidence']
                if not short_confs.empty:
                    indicator_row['mean_conf_short_5d'] = short_confs.mean()
                
                indicator_row['latest_signal_age_hours'] = (primary_signal['timestamp'] - all_relevant_signals['timestamp'].max()).total_seconds() / 3600
            
            indicator_row['signal_conf'] = primary_signal['confidence']
            indicator_row['direction_num'] = 1 if primary_signal['direction'] == 'LONG' else 0
            
            if sig_time in time_to_idx:
                entry_idx = time_to_idx[sig_time]
                outcome_score = calculate_trade_outcome(
                    entry_price=primary_signal['entry_price'],
                    direction=primary_signal['direction'],
                    entry_idx=entry_idx,
                    ohlcv_df=ohlcv
                )
                indicator_row['trade_score'] = outcome_score
                training_data_rows.append(indicator_row)
                    
        except Exception as e:
            # print(f"Error processing signal {primary_signal['id']} for coin {coin} at {sig_time}: {e}")
            pass
            
    raw_training_df = pd.DataFrame(training_data_rows)
    
    if raw_training_df.empty:
        print("No raw data found for training!")
        return pd.DataFrame()

    print("Normalizing features...")
    
    raw_training_df['ai_model'] = raw_training_df['ai_model'].astype(str)
    raw_training_df['conv_source_bot'] = raw_training_df['conv_source_bot'].astype(str)

    normalized_features_df = normalize_features_for_ml(raw_training_df)
    
    normalized_features_df['target'] = (raw_training_df['trade_score'] >= 2).astype(int) 
    normalized_features_df['trade_score'] = raw_training_df['trade_score']
        
    return normalized_features_df


# ... (der Rest des Skripts, die Funktion train_model und der if __name__ == "__main__": Block, bleiben unverändert) ...


def train_model():
    """
    Führt den gesamten Trainingsprozess aus: Daten abrufen, aufbereiten und
    ein XGBoost-Klassifikationsmodell trainieren und speichern.
    """
    df = fetch_and_process_data()
    
    if df.empty:
        print("No data found for training! Exiting.")
        return

    print(f"\nTotal dataset size after processing: {len(df)} samples")
    print(f"Class distribution (Win/Loss):\n{df['target'].value_counts()}")
    
    drop_from_features = ['target', 'trade_score']
    feature_cols = [c for c in df.columns if c not in drop_from_features]
    
    X = df[feature_cols]
    y = df['target']
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=42, shuffle=False)
    
    print(f"\nTraining on {len(X_train)} samples, testing on {len(X_test)} samples.")
    print(f"Number of features used: {len(feature_cols)}")

    # scale_pos_weight_value = (y_train == 0).sum() / (y_train == 1).sum() if (y_train == 1).sum() > 0 else 1
    
    # xgb_params = {
        # 'objective': 'binary:logistic',
        # 'eval_metric': 'logloss',
        # 'eta': 0.05,
        # 'max_depth': 6,
        # 'subsample': 0.7,
        # 'colsample_bytree': 0.7,
        # 'min_child_weight': 1,
        # 'random_state': 42,
        # 'n_estimators': 1000,
        # 'n_jobs': -1,
        # 'scale_pos_weight': scale_pos_weight_value,
    # }
    
    # # clf = xgb.XGBClassifier(**xgb_params)
    
    # # print("\nStarting XGBoost model training...")
    # # clf.fit(X_train, y_train,
            # # early_stopping_rounds=50, 
            # # eval_set=[(X_test, y_test)], 
            # # verbose=False)
    
    # # ... (dein Code bis zur clf.fit() Zeile) ...

    # # Initialisiere den XGBoost-Klassifikator
   # clf = xgb.XGBClassifier(**xgb_params)

    # print("\nStarting XGBoost model training...")

    # # Trainiere das Modell mit Early Stopping (für XGBoost <= 1.6.2)
    # clf.fit(X_train, y_train,
            # early_stopping_rounds=50, # Das ist der frühere Wert
            # eval_set=[(X_test, y_test)],
            # verbose=False)
    
    
    scale_pos_weight_value = (y_train == 0).sum() / (y_train == 1).sum() if (y_train == 1).sum() > 0 else 1
    
    xgb_params = {
        'objective': 'binary:logistic',
        'eval_metric': 'logloss',
        'eta': 0.05,
        'max_depth': 6,
        'subsample': 0.7,
        'colsample_bytree': 0.7,
        'min_child_weight': 1,
        'random_state': 42,
        'n_estimators': 1000,
        'n_jobs': -1,
        'scale_pos_weight': scale_pos_weight_value,
        # 'early_stopping_rounds': 50 # HIERHER VERSCHOBEN
    }
    
    # ÄNDERUNG HIER: early_stopping_rounds wird direkt in den Konstruktor eingefügt
    clf = xgb.XGBClassifier(**xgb_params, early_stopping_rounds=50)
    
    print("\nStarting XGBoost model training...")
    
    # ÄNDERUNG HIER: Parameter aus .fit() entfernt
    clf.fit(X_train, y_train,
            eval_set=[(X_test, y_test)], 
            verbose=False)
    
    # ... (der Rest deines Codes bleibt gleich) ...

    
    preds = clf.predict(X_test)
    probs = clf.predict_proba(X_test)[:, 1]
    
    print("\n--- XGBoost Model Evaluation on Test Set ---")
    print(f"Best iteration: {clf.best_iteration}")
    print("Accuracy:", accuracy_score(y_test, preds))
    print("ROC AUC Score:", roc_auc_score(y_test, probs))
    print("\nClassification Report:")
    print(classification_report(y_test, preds))
    
    save_obj = {
        'model': clf,
        'features': feature_cols
    }
    joblib.dump(save_obj, MODEL_PATH)
    print(f"\nXGBoost model and feature list saved to {MODEL_PATH}")

# --- SKRIPT START ---
if __name__ == "__main__":
    train_model()

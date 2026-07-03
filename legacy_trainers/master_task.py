import os
import asyncio
import pandas as pd
import numpy as np
import joblib
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy import text
import warnings

# Unterdrücke UserWarning von XGBoost
warnings.filterwarnings("ignore", category=UserWarning, module="xgboost")

# --- KONFIGURATION ---
# Passe diese Werte an deine Umgebung an
AI_CHANNEL_ID = 0  # <--- DEINE KANAL ID HIER EINTRAGEN
MIN_CONFIDENCE = 0.90          # Nur Trades mit > 85% Wahrscheinlichkeit posten
MODEL_PATH = "master_trade_model_xgboost_combined_signals.pkl"

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "dbfiller",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "cryptodata"
}

# Mapping für Conv-Signale (muss identisch zum Training sein)
BOT_CONFIDENCE_MAPPING = {
    'Fast Bot': 0.25,
    '5% Bot': 0.45,
    'Volume Bot': 0.35,
    'SR Bot': 0.65
}

# Datenbank-Engine global initialisieren
db_url = f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
engine = create_engine(db_url)


# CREATE TABLE IF NOT EXISTS master_ai_processed_signals (
    # signal_type TEXT NOT NULL,      -- 'ai_signal' oder 'conv_signal'
    # signal_id BIGINT NOT NULL,      -- Die 'id' des Signals aus der Originaltabelle
    # processed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(), -- Wann es verarbeitet wurde
    # ml_confidence NUMERIC(5, 4),    -- Die tatsächliche ML-Konfidenz, als es gemeldet wurde
    # PRIMARY KEY (signal_type, signal_id)
# );

# -- Optional: Index für schnellen Zugriff, falls die Tabelle sehr groß wird
# CREATE INDEX IF NOT EXISTS idx_processed_at ON master_ai_processed_signals (processed_at);




# --- MODELL LADEN ---
try:
    saved_data = joblib.load(MODEL_PATH)
    MASTER_MODEL = saved_data['model']
    REQUIRED_FEATURES = saved_data['features']
    print(f"✅ ML-Modell geladen: {MODEL_PATH}")
    print(f"   Erwartete Features: {len(REQUIRED_FEATURES)}")
except Exception as e:
    print(f"❌ FEHLER beim Laden des Modells: {e}")
    MASTER_MODEL = None
    REQUIRED_FEATURES = []

# --- HILFSFUNKTIONEN ---

# WICHTIG: DIE FUNKTION normalize_features_for_ml MUSS HIERHER KOPIERT WERDEN
# (Der korrigierte Code aus unserer vorherigen Diskussion, der PerformanceWarnings behebt)
# Ich lasse sie hier aus Platzgründen weg, du musst sie einfügen!
def normalize_features_for_ml(df_indicators: pd.DataFrame) -> pd.DataFrame:
    # ... KOPIERE HIER DIE KORRIGIERTE normalize_features_for_ml FUNKTION HEREIN ...
    df = df_indicators.copy()
    if 'close' not in df.columns:
        df['close'] = 1.0 
    df['close_safe'] = df['close'].replace(0, np.nan) 
    #df['close_safe'] = df['close_safe'].fillna(method='ffill').fillna(method='bfill').fillna(1.0)
    df['close_safe'] = df['close_safe'].ffill().bfill().fillna(1.0)
    
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
        'total_signals_5d', 'long_signals_5d', 'short_signals_5d',
        'dominating_direction_5d_long_prob', 'dominating_direction_5d_short_prob',
        'mean_conf_long_5d', 'mean_conf_short_5d', 'latest_signal_age_hours'
    ]
    
    atr_indicators = ['atr_9', 'atr_14', 'atr_21']

    feature_parts = []

    for col in price_based_indicators:
        if col in df.columns:
            new_col_name = f'{col}_dist_pct'
            feature_parts.append(pd.Series((df[col] - df['close']) / df['close_safe'] * 100, name=new_col_name, index=df.index))
    
    for col in atr_indicators:
        if col in df.columns:
            new_col_name = f'{col}_pct_close'
            feature_parts.append(pd.Series(df[col] / df['close_safe'] * 100, name=new_col_name, index=df.index))
            
    for col in features_as_is:
        if col in df.columns:
            feature_parts.append(df[col])

    if 'trend_direction' in df.columns:
        all_possible_directions = ['UP', 'DOWN', 'SIDEWAYS', 'nan']
        direction_dummies = pd.get_dummies(df['trend_direction'], prefix='trend_dir')
        
        for d in all_possible_directions:
            col_name = f'trend_dir_{d}'
            if col_name not in direction_dummies.columns:
                direction_dummies[col_name] = 0
        
        feature_parts.append(direction_dummies)

    all_ai_models = ['EPD1', 'MSI1-8h_pump', 'MSI1-8h_dump', 'MSI1-24h_pump', 'MSI1-24h_dump', 'MSI1-72h_pump', 'MSI1-72h_dump', 'MSI1-168h_pump', 'MSI1-168h_dump', 'nan']
    all_conv_bots = ['SR Bot', 'Volume Bot', '5% Bot', 'nan',  'Fast Bot']

    if 'ai_model' in df.columns:
        ai_model_dummies = pd.get_dummies(df['ai_model'], prefix='ai_model')
        for model_name in all_ai_models:
            col_name = f'ai_model_{model_name}'
            if col_name not in ai_model_dummies.columns:
                ai_model_dummies[col_name] = 0 
        feature_parts.append(ai_model_dummies)

    if 'conv_source_bot' in df.columns:
        conv_bot_dummies = pd.get_dummies(df['conv_source_bot'], prefix='conv_bot')
        for bot_name in all_conv_bots:
            col_name = f'conv_bot_{bot_name}'
            if col_name not in conv_bot_dummies.columns:
                conv_bot_dummies[col_name] = 0 
        feature_parts.append(conv_bot_dummies)

    normalized_df = pd.concat(feature_parts, axis=1)

    normalized_df = normalized_df.fillna(0) 

    return normalized_df


# --- HAUPTTASK FÜR DEN BOT ---

async def check_master_trades(application):
    """
    Diese Funktion muss als Task registriert werden (z.B. alle 30 min).
    Sie aggregiert Signale, erstellt Features, sagt Vorhersagen und speichert den Zustand.
    """
    if MASTER_MODEL is None:
        print("⚠️ Master Task übersprungen: Modell nicht geladen.")
        return

    print("🔄 Starte Master-AI-Analyse...")
    current_time = datetime.utcnow()
    
    # 1. Hole Historische Signale (5 Tage) für den Kontext
    # Wir brauchen das für die Aggregations-Features
    five_days_ago = current_time - timedelta(days=5)
    
    # AI Signals History
    sql_ai_hist = f"""
        SELECT id, symbol, timestamp, price as entry_price, direction, model as bot_name, confidence
        FROM ai_signals WHERE timestamp > '{five_days_ago}' ORDER BY timestamp ASC
    """
    hist_ai = pd.read_sql(sql_ai_hist, engine)
    hist_ai['signal_type'] = 'ai_signal'
    hist_ai['timestamp'] = pd.to_datetime(hist_ai['timestamp'], utc=True)

    # Conv Signals History
    sql_conv_hist = f"""
        SELECT id, coin as symbol, source_time as timestamp, entry_price, direction, source_bot as bot_name
        FROM conv_signals WHERE source_time > '{five_days_ago}' ORDER BY source_time ASC
    """
    hist_conv = pd.read_sql(sql_conv_hist, engine)
    hist_conv['signal_type'] = 'conv_signal'
    hist_conv['timestamp'] = pd.to_datetime(hist_conv['timestamp'], utc=True)
    # Symbol Bereinigung
    hist_conv['symbol'] = hist_conv['symbol'].str.replace('_.*', '', regex=True).str.replace('USDT', '', regex=False) + 'USDT'
    # Confidence Mapping
    hist_conv['confidence'] = hist_conv['bot_name'].map(BOT_CONFIDENCE_MAPPING).fillna(0.0)

    # Kombiniere Historie
    hist_combined = pd.concat([hist_ai, hist_conv], ignore_index=True)
    hist_combined = hist_combined.sort_values(by='timestamp').reset_index(drop=True)


    # 2. Hole NEUE Signale (letzte 30 Min) - Das sind die Kandidaten für Alerts
    check_window = current_time - timedelta(minutes=30)
    
    # Neue AI Signale
    sql_ai_new = f"""
        SELECT id, symbol, timestamp, price as entry_price, direction, model as bot_name, confidence
        FROM ai_signals WHERE timestamp > '{check_window}' ORDER BY timestamp DESC
    """
    new_ai = pd.read_sql(sql_ai_new, engine)
    new_ai['signal_type'] = 'ai_signal'
    new_ai['timestamp'] = pd.to_datetime(new_ai['timestamp'], utc=True)

    # Neue Conv Signale
    sql_conv_new = f"""
        SELECT id, coin as symbol, source_time as timestamp, entry_price, direction, source_bot as bot_name
        FROM conv_signals WHERE source_time > '{check_window}' ORDER BY source_time DESC
    """
    new_conv = pd.read_sql(sql_conv_new, engine)
    new_conv['signal_type'] = 'conv_signal'
    new_conv['timestamp'] = pd.to_datetime(new_conv['timestamp'], utc=True)
    new_conv['symbol'] = new_conv['symbol'].str.replace('_.*', '', regex=True).str.replace('USDT', '', regex=False) + 'USDT'
    new_conv['confidence'] = new_conv['bot_name'].map(BOT_CONFIDENCE_MAPPING).fillna(0.0)

    # Alle Kandidaten
    candidates = pd.concat([new_ai, new_conv], ignore_index=True)
    candidates['join_time'] = candidates['timestamp'].dt.round('1h') # Für Indikator Join

    if candidates.empty:
        print("ℹ️ Keine neuen Signale zum Prüfen.")
        return

    # 3. Bereits verarbeitete Signale aus der DB abfragen
    # Wir holen alle, die in den letzten 5 Tagen verarbeitet wurden,
    # um sicherzustellen, dass wir keine alten Signale doppelt senden.
    processed_query = f"""
        SELECT signal_type, signal_id FROM master_ai_processed_signals
        WHERE processed_at > '{five_days_ago}'
    """
    processed_df = pd.read_sql(processed_query, engine)
    # Erstelle ein Set von (signal_type, signal_id) Tupeln für schnelle Prüfungen
    processed_signals_set = set(tuple(row) for row in processed_df[['signal_type', 'signal_id']].to_numpy())

    # Kandidaten filtern, die bereits verarbeitet wurden
    initial_candidates_count = len(candidates)
    candidates['is_processed'] = candidates.apply(lambda row: (row['signal_type'], row['id']) in processed_signals_set, axis=1)
    candidates = candidates[~candidates['is_processed']].drop(columns=['is_processed'])

    if candidates.empty:
        print(f"ℹ️ {initial_candidates_count} Signale in den letzten 30 Minuten, aber alle bereits verarbeitet.")
        return
    else:
        print(f"🔎 Analysiere {len(candidates)} neue, noch nicht verarbeitete Signale...")


    # Cache für DB Abfragen (Performance)
    # Beinhaltet die neueste Indikator- und OHLCV-Close-Row für jeden Coin
    cached_ohlcv_indicators = {}

    for _, signal in candidates.iterrows():
        coin = signal['symbol']
        join_time = signal['join_time']
        
        try:
            # Daten für Coin laden (nur wenn nicht im Cache)
            if coin not in cached_ohlcv_indicators:
                # Hole die letzte vollständige Stunde an Indikatoren/OHLCV
                sql_ind = f"SELECT * FROM \"{coin}_1h_indicators\" WHERE open_time <= '{join_time}' ORDER BY open_time DESC LIMIT 1"
                try:
                    ind_df = pd.read_sql(sql_ind, engine)
                    if ind_df.empty:
                        raise ValueError(f"No indicators for {coin} at {join_time}")
                    ind_df['open_time'] = pd.to_datetime(ind_df['open_time'], utc=True)
                    
                    sql_ohlcv = f"SELECT close FROM \"{coin}_1h\" WHERE open_time <= '{join_time}' ORDER BY open_time DESC LIMIT 1"
                    ohlcv_df = pd.read_sql(sql_ohlcv, engine)
                    if ohlcv_df.empty:
                        raise ValueError(f"No OHLCV close for {coin} at {join_time}")
                    
                    cached_ohlcv_indicators[coin] = (ind_df.iloc[0], ohlcv_df.iloc[0]['close'])
                except Exception as e:
                    # print(f"DEBUG: Could not fetch data for {coin}: {e}") # Zur Fehlersuche
                    continue # Tabelle existiert wohl nicht, oder Daten fehlen
            
            if coin not in cached_ohlcv_indicators: continue # Falls Fehler im Cache-Befüllen aufgetreten ist
            
            # Daten vorbereiten
            row = cached_ohlcv_indicators[coin][0].copy()
            close_price = cached_ohlcv_indicators[coin][1]
            row['close'] = close_price # Close Preis anfügen

            # --- AGGREGATION ---
            # Filtere Historie für diesen Coin bis zum Zeitpunkt des Signals
            context = hist_combined[
                (hist_combined['symbol'] == coin) & 
                (hist_combined['timestamp'] <= signal['timestamp'])
            ]
            
            # Features berechnen (Default-Werte, wenn kein Kontext vorhanden)
            row['total_signals_5d'] = len(context)
            row['long_signals_5d'] = len(context[context['direction'] == 'LONG'])
            row['short_signals_5d'] = len(context[context['direction'] == 'SHORT'])
            
            total_dir = row['long_signals_5d'] + row['short_signals_5d']
            row['dominating_direction_5d_long_prob'] = row['long_signals_5d'] / total_dir if total_dir > 0 else 0
            row['dominating_direction_5d_short_prob'] = row['short_signals_5d'] / total_dir if total_dir > 0 else 0

            longs = context[context['direction'] == 'LONG']
            shorts = context[context['direction'] == 'SHORT']
            
            row['mean_conf_long_5d'] = longs['confidence'].mean() if not longs.empty else 0
            row['mean_conf_short_5d'] = shorts['confidence'].mean() if not shorts.empty else 0
            
            row['latest_signal_age_hours'] = 120 # Default
            if not context.empty:
                diff = (signal['timestamp'] - context['timestamp'].max()).total_seconds() / 3600
                row['latest_signal_age_hours'] = max(0, diff) # Alter kann nicht negativ sein

            # Signal Spezifika
            row['signal_conf'] = signal['confidence']
            row['direction_num'] = 1 if signal['direction'] == 'LONG' else 0
            
            # Bot Name für OHE (sicherstellen, dass 'nan' als String behandelt wird)
            if signal['signal_type'] == 'ai_signal':
                row['ai_model'] = str(signal['bot_name'])
                row['conv_source_bot'] = 'nan'
            else:
                row['conv_source_bot'] = str(signal['bot_name'])
                row['ai_model'] = 'nan'

            # --- VORHERSAGE ---
            df_input = pd.DataFrame([row])
            df_input['ai_model'] = df_input['ai_model'].astype(str)
            df_input['conv_source_bot'] = df_input['conv_source_bot'].astype(str)
            
            df_normalized = normalize_features_for_ml(df_input)
            
            # Spaltenordnung erzwingen
            X = df_normalized.reindex(columns=REQUIRED_FEATURES, fill_value=0)
            
            # Prediction
            prob = MASTER_MODEL.predict_proba(X)[0][1] # Wahrscheinlichkeit für Klasse 1 (Win)
            
             # 4. Signal als verarbeitet markieren (in DB eintragen)
            try:
                with engine.connect() as conn:
                    conn.execute(
                        text(
                            """
                            INSERT INTO master_ai_processed_signals (signal_type, signal_id, ml_confidence)
                            VALUES (:signal_type, :signal_id, :ml_confidence)
                            ON CONFLICT (signal_type, signal_id) DO UPDATE SET processed_at = NOW(), ml_confidence = :ml_confidence
                            """
                        ),
                        {'signal_type': signal['signal_type'], 'signal_id': signal['id'], 'ml_confidence': float(prob)}
                    )
                    conn.commit() # Transaktion committen
            except Exception as db_e:
                print(f"❌ FEHLER beim Speichern des verarbeiteten Signals in DB: {db_e}")

            # --- ALERT ---
            if prob >= MIN_CONFIDENCE:
                is_pump = (signal['direction'] == "LONG")
                emoji = "💎 MASTER AI TRADE"
                color = "#00ff00" if is_pump else "#ff0066"
                
                # HTML Nachricht
                msg = f"""
<pre style="background:#151515; color:#e0e0e0; padding:15px; border-radius:10px; border-left: 5px solid {color};">
<b style="font-size:18px; color:#ffcc00;">{emoji}</b>
<b>{coin.replace('USDT','')}/USDT</b>

<b>Side:</b> <b style="color:{color};">{signal['direction']}</b>
<b>Entry:</b> <code>${signal['entry_price']:.6f}</code>
<b>Source:</b> {signal['bot_name']} (Conf: {signal['confidence']:.2f})

<b>🤖 AI Confidence: <b style="color:#00ffff;">{prob:.1%}</b></b>
<i>(Optimal {MIN_CONFIDENCE:.0%} filter applied)</i>

<b>Context (5d):</b>
• Signals: {row['total_signals_5d']:.0f} (L:{row['long_signals_5d']:.0f} / S:{row['short_signals_5d']:.0f})
• Long Dom: {row['dominating_direction_5d_long_prob']:.0%}
</pre>
"""             
                # Senden an Telegram
                try:
                    # await send_cornix_signal(coin, signal['direction'], 'master_combined_model') # Falls Cornix genutzt wird
                    # await application.bot.send_message(
                        # chat_id=AI_CHANNEL_ID,
                        # text=msg,
                        # parse_mode="HTML"
                    # )
                    print(f"✅ ALERT gesendet für {coin} (ID: {signal['id']}): {prob:.1%}")
                    
                   

                except Exception as e:
                    print(f"❌ TELEGRAM Sende-Fehler für {coin}: {e}")
            else:
                # print(f"   Skip {coin} (ID: {signal['id']}): {prob:.1%} < {MIN_CONFIDENCE:.0%}") # Debug Info
                pass

        except Exception as e:
            print(f"❌ Fehler bei Analyse von {coin} (ID: {signal.get('id', 'N/A')}): {e}")
            continue

    print("🏁 Analyse abgeschlossen.")

# --- BEISPIEL FÜR REGISTRIERUNG ---
# In deiner Haupt-Bot-Datei (main.py):
# 
# from master_task import check_master_trades
# ...
# # In main():
# app.job_queue.run_repeating(check_master_trades, interval=1800, first=20)

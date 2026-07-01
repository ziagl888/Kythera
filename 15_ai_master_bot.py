import datetime
import json
import logging
import os
import time
import warnings

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")
warnings.filterwarnings("ignore", category=UserWarning, module="xgboost")

from core import config as _kcfg  # channel ids
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.market_utils import get_max_leverage
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_MASTER_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
AI_CHANNEL_ID = _kcfg.CH_MASTER
MIN_CONFIDENCE = 0.80

MODEL_PATH = "master_trade_model_xgboost_combined_signals.pkl"

BOT_CONFIDENCE_MAPPING = {
    'Fast Bot': 0.65,
    '5% Bot': 0.80,
    'Volume Bot': 0.60,
    'SR Bot': 0.60,
    '5 Percent': 0.80,
    'Fast In And Out': 0.65,
    'Volume Indicator': 0.60,
    'Main Channel': 0.55,
    'Support Resistance': 0.60,
}

MASTER_MODEL = None
REQUIRED_FEATURES = []


def load_master_model():
    global MASTER_MODEL, REQUIRED_FEATURES
    if os.path.exists(MODEL_PATH):
        try:
            saved_data = joblib.load(MODEL_PATH)
            MASTER_MODEL = saved_data['model']
            REQUIRED_FEATURES = saved_data['features']
            logger.info(f"✅ Master ML model (AIM1) loaded. Expected features: {len(REQUIRED_FEATURES)}")
        except Exception as e:
            logger.error(f"❌ Error loading des Master-Modells: {e}")
            MASTER_MODEL = None
            REQUIRED_FEATURES = []
    else:
        logger.warning(f"⚠️ Master-Modell '{MODEL_PATH}' not found. Waiting for Bereitstellung.")


def normalize_features_for_ml(df_indicators: pd.DataFrame) -> pd.DataFrame:
    df = df_indicators.copy()
    if 'close' not in df.columns:
        df['close'] = 1.0
    df['close_safe'] = df['close'].replace(0, np.nan)
    df['close_safe'] = df['close_safe'].ffill().bfill().fillna(1.0)

    price_based_indicators = [
        'ema_7',
        'ema_9',
        'ema_12',
        'ema_21',
        'ema_26',
        'ema_34',
        'ema_50',
        'ema_55',
        'ema_89',
        'ema_99',
        'ema_200',
        'ma_7',
        'ma_10',
        'ma_20',
        'ma_25',
        'ma_50',
        'ma_99',
        'ma_100',
        'ma_200',
        'wma_7',
        'wma_9',
        'wma_12',
        'wma_21',
        'wma_26',
        'wma_34',
        'wma_50',
        'wma_55',
        'wma_89',
        'wma_99',
        'wma_200',
        'smma_10',
        'smma_20',
        'smma_25',
        'smma_50',
        'smma_99',
        'smma_100',
        'smma_200',
        'kama_7',
        'kama_9',
        'kama_12',
        'kama_21',
        'kama_26',
        'kama_34',
        'kama_50',
        'kama_55',
        'kama_89',
        'kama_99',
        'boll_upper_20',
        'boll_mid_20',
        'boll_lower_20',
        'donchian_upper_4',
        'donchian_lower_4',
        'donchian_mid_4',
        'donchian_upper_10',
        'donchian_lower_10',
        'donchian_mid_10',
        'donchian_upper_12',
        'donchian_lower_12',
        'donchian_mid_12',
        'donchian_upper_15',
        'donchian_lower_15',
        'donchian_mid_15',
        'donchian_upper_20',
        'donchian_lower_20',
        'donchian_mid_20',
        'trendline_intercept',
        'channel_upper_price',
        'channel_lower_price',
        'trendline_price',
        'mid_line',
        'support_price',
        'resistance_price',
        'poc',
        'fib_support_0_236',
        'fib_resistance_0_236',
        'fib_support_0_382',
        'fib_resistance_0_382',
        'fib_support_0_5',
        'fib_resistance_0_5',
        'fib_support_0_618',
        'fib_resistance_0_618',
        'fib_support_0_786',
        'fib_resistance_0_786',
        'fib_extension_1_272',
        'fib_extension_1_618',
        'fib_extension_2_618',
        'hvn_1',
        'hvn_2',
        'hvn_3',
    ]

    features_as_is = [
        'rsi_6',
        'rsi_9',
        'rsi_12',
        'rsi_14',
        'rsi_24',
        'tsi_25_13_13',
        'tsi_25_13_13_signal',
        'tsi_fast_12_7_7',
        'tsi_fast_12_7_7_signal',
        'macd_dif_fast_9_21_9',
        'macd_dea_fast_9_21_9',
        'macd_dif_normal_12_26_9',
        'macd_dea_normal_12_26_9',
        'trendline_slope',
        'r_squared',
        'signal_conf',
        'direction_num',
        'total_signals_5d',
        'long_signals_5d',
        'short_signals_5d',
        'dominating_direction_5d_long_prob',
        'dominating_direction_5d_short_prob',
        'mean_conf_long_5d',
        'mean_conf_short_5d',
        'latest_signal_age_hours',
    ]

    atr_indicators = ['atr_9', 'atr_14', 'atr_21']
    feature_parts = []

    for col in price_based_indicators:
        if col in df.columns:
            new_col_name = f'{col}_dist_pct'
            feature_parts.append(
                pd.Series((df[col] - df['close']) / df['close_safe'] * 100, name=new_col_name, index=df.index)
            )

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

    # FIX: Vorher fehlte das Komma after 'MIS1' → Python concat: 'MIS1MSI1-8h_pump'.
    # Zusätzlich war 'MSI1-*' ein Typo (gemeint war 'MIS1-*') → alle MIS-Horizon-Dummies
    # fehlten im One-Hot-Encoding und der Feature-Vektor war um 8 Dimensionen verschoben.
    all_ai_models = [
        'EPD1',
        'ATS1',
        'RUB1',
        'ABR1',
        'SRA1',
        'AIM1',
        'MIS1',
        'MIS1-8h_pump',
        'MIS1-8h_dump',
        'MIS1-24h_pump',
        'MIS1-24h_dump',
        'MIS1-72h_pump',
        'MIS1-72h_dump',
        'MIS1-168h_pump',
        'MIS1-168h_dump',
        'MIS1-8H',
        'MIS1-24H',
        'MIS1-72H',
        'MIS1-168H',
        'BR1H',
        'BR2H',
        'BR4H',
        'BR1D',
        'nan',
    ]
    all_conv_bots = [
        'SR Bot',
        'Volume Bot',
        '5% Bot',
        'nan',
        'Fast Bot',
        '5 Percent',
        'Fast In And Out',
        'Volume Indicator',
        'Main Channel',
        'Support Resistance',
    ]

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


def df_from_query(conn, query, params=None):
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        columns = [desc[0] for desc in cur.description]
        return pd.DataFrame(rows, columns=columns)


def process_master_trades():
    if MASTER_MODEL is None or not REQUIRED_FEATURES:
        logger.warning("Master Task skipped: Modell not loaded.")
        return

    logger.info("🔄 Starting Master-AI-Analyse (AIM1)...")
    conn = get_db_connection()
    current_time = datetime.datetime.now(datetime.timezone.utc)
    five_days_ago = current_time - datetime.timedelta(days=5)
    check_window = current_time - datetime.timedelta(minutes=5)

    try:
        # 1. HISTORIE LADEN (Letzte 5 Tage für Kontext)
        sql_ai_hist = """
                    SELECT
                        id,
                        coin as symbol,
                        time as timestamp,
                        entry,
                        direction,
                        model_name as bot_name,
                        confidence
                    FROM ml_predictions_master
                    WHERE time > %s
                    ORDER BY time DESC
                """
        hist_ai = df_from_query(conn, sql_ai_hist, (five_days_ago,))
        if not hist_ai.empty:
            hist_ai['signal_type'] = 'ai_signal'
            hist_ai['timestamp'] = pd.to_datetime(hist_ai['timestamp'], utc=True)

        sql_conv_hist = """
                    SELECT id, coin as symbol, time as timestamp, entry, direction, strategy as bot_name
                    FROM active_trades_master
                    WHERE time > %s
                    UNION ALL
                    SELECT id, coin as symbol, time as timestamp, entry, direction, strategy as bot_name
                    FROM closed_trades_master
                    WHERE time > %s
                    ORDER BY timestamp DESC
                """
        hist_conv = df_from_query(conn, sql_conv_hist, (five_days_ago, five_days_ago))

        if not hist_conv.empty:
            hist_conv['signal_type'] = 'conv_signal'
            hist_conv['timestamp'] = pd.to_datetime(hist_conv['timestamp'], utc=True)
            # FIX (#28): Vorher `.replace('_.*', '', regex=True).replace('USDT', '', regex=False) + 'USDT'`
            # Das brach bei Coins wo "USDT" nicht am Suffix stand (z.B. "USDCUSDT" → "C" → "CUSDT") und
            # bei neuen Coin-Namen mit Zahlen-Präfix. Sauberere Lösung: Nur den TF-Suffix entfernen,
            # das erhält den Coin-Namen 1:1 (BTCUSDT_1h → BTCUSDT, 1000PEPEUSDT_4h → 1000PEPEUSDT).
            hist_conv['symbol'] = hist_conv['symbol'].str.replace(r'_\d+[mhdwM]$', '', regex=True)
            hist_conv['confidence'] = hist_conv['bot_name'].map(BOT_CONFIDENCE_MAPPING).fillna(0.0)

        hist_combined = (
            pd.concat([hist_ai, hist_conv], ignore_index=True)
            if not hist_ai.empty or not hist_conv.empty
            else pd.DataFrame()
        )

        if not hist_combined.empty:
            hist_combined = hist_combined.sort_values(by='timestamp').reset_index(drop=True)

        # 2. NEUE SIGNALE LADEN (Letzte 30 Min)
        sql_ai_new = """
                    SELECT id, coin as symbol, time as timestamp, entry, direction, model_name as bot_name, confidence
                    FROM ml_predictions_master
                    WHERE time > %s AND model_name NOT LIKE 'AIM1'
                    ORDER BY time DESC
                """
        new_ai = df_from_query(conn, sql_ai_new, (check_window,))
        if not new_ai.empty:
            new_ai['signal_type'] = 'ai_signal'
            new_ai['timestamp'] = pd.to_datetime(new_ai['timestamp'], utc=True)

        sql_conv_new = """
                    SELECT id, coin as symbol, time as timestamp, entry, direction, strategy as bot_name
                    FROM active_trades_master WHERE time > %s
                    UNION ALL
                    SELECT id, coin as symbol, time as timestamp, entry, direction, strategy as bot_name
                    FROM closed_trades_master WHERE time > %s
                    ORDER BY timestamp DESC
                """
        new_conv = df_from_query(conn, sql_conv_new, (check_window, check_window))

        if not new_conv.empty:
            new_conv['signal_type'] = 'conv_signal'
            new_conv['timestamp'] = pd.to_datetime(new_conv['timestamp'], utc=True)
            # FIX (#28): siehe Kommentar im hist_conv-Block oben.
            new_conv['symbol'] = new_conv['symbol'].str.replace(r'_\d+[mhdwM]$', '', regex=True)
            new_conv['confidence'] = new_conv['bot_name'].map(BOT_CONFIDENCE_MAPPING).fillna(0.0)

        candidates = (
            pd.concat([new_ai, new_conv], ignore_index=True)
            if not new_ai.empty or not new_conv.empty
            else pd.DataFrame()
        )

        if candidates.empty:
            logger.info("ℹ️ No new signals to check.")
            return

        candidates['join_time'] = candidates['timestamp'].dt.tz_localize(None).dt.floor('h')

        # 3. BEREITS VERARBEITETE FILTERN
        sql_processed = "SELECT signal_type, signal_id FROM master_ai_processed_signals WHERE processed_at > %s"
        processed_df = df_from_query(conn, sql_processed, (five_days_ago,))

        processed_signals_set = set()
        if not processed_df.empty:
            processed_signals_set = set(tuple(row) for row in processed_df[['signal_type', 'signal_id']].to_numpy())

        initial_count = len(candidates)
        candidates['is_processed'] = candidates.apply(
            lambda row: (row['signal_type'], row['id']) in processed_signals_set, axis=1
        )
        candidates = candidates[~candidates['is_processed']].drop(columns=['is_processed'])

        if candidates.empty:
            logger.info(f"ℹ️ {initial_count} signals in the last 30 min but all already processed.")
            return

        logger.info(f"🔎 Analysing {len(candidates)} new, unprocessed signals...")

        cached_ohlcv_indicators = {}
        processed_inserts = []
        shadow_mode_inserts = []

        # 4. KANDIDATEN AUSWERTEN
        for _, signal in candidates.iterrows():
            coin = signal['symbol']
            join_time = signal['join_time']

            if coin not in cached_ohlcv_indicators:
                sql_ind = (
                    f"SELECT * FROM \"{coin}_1h_indicators\" WHERE open_time <= %s ORDER BY open_time DESC LIMIT 1"
                )
                sql_ohlcv = f"SELECT close FROM \"{coin}_1h\" WHERE open_time <= %s ORDER BY open_time DESC LIMIT 1"
                try:
                    ind_df = df_from_query(conn, sql_ind, (join_time,))
                    ohlcv_df = df_from_query(conn, sql_ohlcv, (join_time,))
                    if not ind_df.empty and not ohlcv_df.empty:
                        cached_ohlcv_indicators[coin] = (ind_df.iloc[0].to_dict(), ohlcv_df.iloc[0]['close'])
                except Exception:
                    conn.rollback()
                    continue

            if coin not in cached_ohlcv_indicators:
                continue

            row = cached_ohlcv_indicators[coin][0].copy()
            close_price = float(cached_ohlcv_indicators[coin][1])
            row['close'] = close_price

            if not hist_combined.empty:
                context = hist_combined[
                    (hist_combined['symbol'] == coin) & (hist_combined['timestamp'] <= signal['timestamp'])
                ]
            else:
                context = pd.DataFrame()

            row['total_signals_5d'] = len(context)
            row['long_signals_5d'] = len(context[context['direction'] == 'LONG']) if not context.empty else 0
            row['short_signals_5d'] = len(context[context['direction'] == 'SHORT']) if not context.empty else 0

            total_dir = row['long_signals_5d'] + row['short_signals_5d']
            row['dominating_direction_5d_long_prob'] = row['long_signals_5d'] / total_dir if total_dir > 0 else 0
            row['dominating_direction_5d_short_prob'] = row['short_signals_5d'] / total_dir if total_dir > 0 else 0

            longs = context[context['direction'] == 'LONG'] if not context.empty else pd.DataFrame()
            shorts = context[context['direction'] == 'SHORT'] if not context.empty else pd.DataFrame()

            row['mean_conf_long_5d'] = longs['confidence'].mean() if not longs.empty else 0
            row['mean_conf_short_5d'] = shorts['confidence'].mean() if not shorts.empty else 0

            row['latest_signal_age_hours'] = 120
            if not context.empty:
                diff = (signal['timestamp'] - context['timestamp'].max()).total_seconds() / 3600
                row['latest_signal_age_hours'] = max(0, diff)

            row['signal_conf'] = signal['confidence']
            row['direction_num'] = 1 if signal['direction'] == 'LONG' else 0

            if signal['signal_type'] == 'ai_signal':
                row['ai_model'] = str(signal['bot_name'])
                row['conv_source_bot'] = 'nan'
            else:
                row['conv_source_bot'] = str(signal['bot_name'])
                row['ai_model'] = 'nan'

            # ML VORHERSAGE
            df_input = pd.DataFrame([row])
            df_input['ai_model'] = df_input['ai_model'].astype(str)
            df_input['conv_source_bot'] = df_input['conv_source_bot'].astype(str)

            df_normalized = normalize_features_for_ml(df_input)
            X = df_normalized.reindex(columns=REQUIRED_FEATURES, fill_value=0)
            X = X.apply(pd.to_numeric, errors='coerce').fillna(0.0).astype('float32')

            prob = float(MASTER_MODEL.predict_proba(X)[0][1])

            processed_inserts.append((signal['signal_type'], signal['id'], prob))

            # --- SHADOW MODE & ALERTS ---
            if prob < 0.25:
                continue

            elif 0.25 <= prob < MIN_CONFIDENCE:
                shadow_mode_inserts.append(("AIM1", current_time, coin, signal['direction'], close_price, prob, False))

            elif prob >= MIN_CONFIDENCE:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                                    SELECT 1 FROM ai_signals
                                    WHERE symbol = %s AND direction = %s AND model = 'AIM1'
                                """,
                        (coin, signal['direction']),
                    )
                    trade_exists = cur.fetchone()

                if trade_exists:
                    logger.info(f"⏳ Skipping {coin} {signal['direction']}: Es läuft bereits ein aktiver AIM1 Trade!")
                    # Wir speichern es trotzdem in der Shadow-Tabelle (mit posted=False),
                    # damit wir in der Historie sehen, dass das ML-Modell den Trade gewollt hätte.
                    shadow_mode_inserts.append(
                        ("AIM1", current_time, coin, signal['direction'], close_price, prob, False)
                    )
                    continue  # Springt zum nächsten Coin in der Schleife

                # 💥 HIER IST DIE NEUE MAGIE: Frische, fehlerfreie Targets berechnen
                trade_setup = calculate_smart_targets(conn, coin, signal['direction'], close_price)

                entry1 = trade_setup['entry1']
                entry2 = trade_setup['entry2']
                sl = trade_setup['sl']
                targets = trade_setup['targets']

                emoji = "💎 MASTER AI TRADE (AIM1)"

                lev = get_max_leverage(coin, 20)

                lines = [
                    f"📈 Signal for {coin} 📈",
                    f"🚨 Direction: {signal['direction']}",
                    f"🚨 Leverage: {lev}",
                    "🚨 Margin: Cross",
                    f"🏦 CMP Entry: $ {entry1:.8f}",
                    f"🏦 Entry 2: $ {entry2:.8f}",
                ]
                for i, t in enumerate(targets[:3], 1):
                    lines.append(f"💰 TP{i}: $ {t:.8f}")
                lines += [f"💸 Stop Loss: $ {sl:.8f}", "🧠 Trade idea verified by Master AI module (AIM1) V3"]
                cornix_msg = "\n".join(lines)

                html_caption = f"""<pre><b>{emoji}</b>\n<b>{coin.replace('USDT', '')}/USDT</b>\n<b>→ Direction: <b>{signal['direction']}</b></b>\n<b>→ Source: {signal['bot_name']} (Conf {signal['confidence']:.2f})</b>\n<b>→ Master Confidence: <b>{prob:.1%}</b></b>\n<b>→ Long Dom 5d: {row['dominating_direction_5d_long_prob']:.0%}</b>\n<b>→ Time: {current_time.strftime('%H:%M')} UTC | Module: AIM1</b>\n\n{cornix_msg}</pre>"""

                chart_buf = generate_minichart_image(coin, minutes=240)

                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (AI_CHANNEL_ID, cornix_msg)
                    )
                    if chart_buf:
                        cur.execute(
                            "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                            (AI_CHANNEL_ID, html_caption, chart_buf),
                        )
                    else:
                        cur.execute(
                            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                            (AI_CHANNEL_ID, html_caption),
                        )

                    cur.execute(
                        """
                                                INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                            """,
                        (
                            coin,
                            entry1,
                            "AIM1",
                            signal['direction'],
                            float(prob),
                            float(entry1),
                            float(entry2),
                            float(sl),
                            json.dumps(targets),
                        ),
                    )

                shadow_mode_inserts.append(("AIM1", current_time, coin, signal['direction'], close_price, prob, True))

                logger.info(f"✅ AIM1 MASTER ALERT gesendet für {coin} (ID: {signal['id']}): {prob:.1%}")

        # 5. DB BATCH UPDATES AUSFÜHREN
        with conn.cursor() as cur:
            if processed_inserts:
                cur.executemany(
                    """
                    INSERT INTO master_ai_processed_signals (signal_type, signal_id, ml_confidence, processed_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (signal_type, signal_id) DO UPDATE SET processed_at = NOW(), ml_confidence = EXCLUDED.ml_confidence
                """,
                    processed_inserts,
                )

            if shadow_mode_inserts:
                cur.executemany(
                    """
                    INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                    VALUES (0, %s, %s, %s, %s, %s, %s, %s)
                """,
                    shadow_mode_inserts,
                )
        conn.commit()

    except Exception as e:
        logger.error(f"Critical error im Master Task: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    logger.info("🏁 AIM1 Master-Analyse stopped.")


def main():
    logger.info("=== 🧠 AI MASTER BOT (AIM1) GESTARTET ===")

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS master_ai_processed_signals (
                signal_type TEXT NOT NULL,
                signal_id BIGINT NOT NULL,
                processed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                ml_confidence NUMERIC(5, 4),
                PRIMARY KEY (signal_type, signal_id)
            );
        """)
    conn.commit()
    conn.close()

    load_master_model()

    while True:
        process_master_trades()
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

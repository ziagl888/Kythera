import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")
warnings.filterwarnings("ignore", category=UserWarning, module="pandas_ta")

import datetime
import json
import logging
import time

import numpy as np
import pandas as pd
import scipy.signal
import xgboost as xgb

from core import config as _kcfg  # channel ids
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - ABR1_BOT - %(message)s')
logger = logging.getLogger(__name__)

# 🛠️ CONFIGURATION
MODEL_ID = 'ABR1'
TARGET_CHANNEL_ID = _kcfg.CH_ABR1  # Dein ABR1 Channel
SG_LONG_MODEL_FILE = 'bt2_model_LONG.json'
SG_SHORT_MODEL_FILE = 'bt2_model_SHORT.json'
SG_COINS_FILE = 'coins.json'

# FIX: Die Thresholds LONG=0.60 / SHORT=0.80 sind asymmetrisch und bewusst streng
# für SHORT gewählt, weil die alten Backtests zeigten dass SHORT-Setups an
# Break&Retest-Leveln deutlich mehr False-Positives produzieren (insb. in Bull-
# Markt-Phasen wo der Trend gegen die Retest-Richtung läuft).
# ACHTUNG: Falls das Ergebnis bei Live-Trading stark von den Backtests abweicht,
# hier die Werte ggf. anpassen — oder kombiniert mit SUCCESS_CLASS_IDX (s. unten)
# prüfen, ob die Semantik bei der Modell-Version stimmt.
THRESHOLDS = {'LONG': 0.60, 'SHORT': 0.80}

# FIX: SUCCESS_CLASS_IDX wird in predict_proba[0, SUCCESS_CLASS_IDX] verwendet.
# Standard-Konvention bei XGBoost Binary-Classifier:
#   label=0 = Failure, label=1 = Success → SUCCESS_CLASS_IDX=1
# Diese Codebase nutzt jedoch SUCCESS_CLASS_IDX=0 aus historischen Gründen
# (alte Modelle hatten invertiertes Label). BITTE VERIFIZIEREN gegen das
# Training-Notebook: wenn dort `y=1` für gewinnende Trades steht, MUSS hier
# SUCCESS_CLASS_IDX=1 stehen — sonst wird die Probability für FAILURE als
# Success-Score interpretiert und alle Thresholds wirken invers.
SUCCESS_CLASS_IDX = 0
PIVOT_WINDOW = 10
RETEST_BACKWARD_LOOKUP_CANDLES = 24
LEVEL_TOLERANCE_PCT = 0.005
LIVE_DATA_HISTORY_HOURS = 240

FEATURE_COLUMNS = [
    'dist_close_ema9_pct',
    'dist_ema9_ema21_pct',
    'dist_close_kama9_pct',
    'rsi14',
    'rsi_below_30',
    'rsi_above_70',
    'tsi',
    'tsi_signal',
    'tsi_above_0',
    'tsi_below_0',
    'dist_close_boll_upper_pct',
    'dist_close_boll_mid_pct',
    'dist_close_boll_lower_pct',
    'dist_close_donchian_upper_pct',
    'dist_close_donchian_mid_pct',
    'dist_close_donchian_lower_pct',
    'retest_volume',
    'retest_volume_ratio_avg',
]

# Modelle global
MODELS = {'LONG': None, 'SHORT': None}


def load_models_and_coins():
    try:
        MODELS['LONG'] = xgb.XGBClassifier()
        MODELS['LONG'].load_model(SG_LONG_MODEL_FILE)
        MODELS['SHORT'] = xgb.XGBClassifier()
        MODELS['SHORT'].load_model(SG_SHORT_MODEL_FILE)
        logger.info("✅ ML Modelle loaded successfully.")
    except Exception as e:
        logger.critical(f"❌ ERROR: Could not load ML models: {e}")
        exit(1)

    try:
        with open(SG_COINS_FILE) as f:
            data = json.load(f)
            return data.get('coins', data) if isinstance(data, dict) else data
    except Exception:
        logger.warning("Konnte coins.json nicht laden, nutze leere Liste.")
        return []


def calculate_technical_indicators(df):
    """Berechnet alle Features für das Modell via pandas_ta"""

    # Sicherstellen, dass alles numerisch ist
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df.ta.ema(length=9, append=True)
    df.ta.ema(length=21, append=True)
    df.ta.kama(length=9, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.tsi(fast=7, slow=12, signal=7, append=True)
    df.ta.bbands(length=20, append=True)
    df.ta.donchian(length=20, append=True)

    expected_pta_cols = {
        'EMA_9': np.nan,
        'EMA_21': np.nan,
        'KAMA_9': np.nan,
        'RSI_14': np.nan,
        'TSI_12_7': np.nan,
        'TSIs_12_7_7': np.nan,
        'BBL_20_2': np.nan,
        'BBM_20_2': np.nan,
        'BBU_20_2': np.nan,
        'DCL_20': np.nan,
        'DCM_20': np.nan,
        'DCU_20': np.nan,
    }
    for col, default_val in expected_pta_cols.items():
        if col not in df.columns:
            df[col] = default_val

    df.rename(
        columns={
            'EMA_9': 'ema9',
            'EMA_21': 'ema21',
            'KAMA_9': 'kama9',
            'RSI_14': 'rsi14',
            'TSI_12_7': 'tsi',
            'TSIs_12_7_7': 'tsi_signal',
            'BBL_20_2': 'boll_lower_20',
            'BBM_20_2': 'boll_mid_20',
            'BBU_20_2': 'boll_upper_20',
            'DCL_20': 'donchian_lower_20',
            'DCM_20': 'donchian_mid_20',
            'DCU_20': 'donchian_upper_20',
        },
        inplace=True,
    )

    df['dist_close_ema9_pct'] = ((df['close'] - df['ema9']) / df['ema9'] * 100).fillna(0)
    df['dist_ema9_ema21_pct'] = ((df['ema9'] - df['ema21']) / df['ema21'] * 100).fillna(0)
    df['dist_close_kama9_pct'] = ((df['close'] - df['kama9']) / df['kama9'] * 100).fillna(0)
    df['rsi_below_30'] = (df['rsi14'] < 30).astype(int)
    df['rsi_above_70'] = (df['rsi14'] > 70).astype(int)
    df['tsi_above_0'] = (df['tsi'] > 0).astype(int)
    df['tsi_below_0'] = (df['tsi'] < 0).astype(int)
    df['dist_close_boll_upper_pct'] = ((df['close'] - df['boll_upper_20']) / df['boll_upper_20'] * 100).fillna(0)
    df['dist_close_boll_mid_pct'] = ((df['close'] - df['boll_mid_20']) / df['boll_mid_20'] * 100).fillna(0)
    df['dist_close_boll_lower_pct'] = ((df['close'] - df['boll_lower_20']) / df['boll_lower_20'] * 100).fillna(0)
    df['dist_close_donchian_upper_pct'] = (
        (df['close'] - df['donchian_upper_20']) / df['donchian_upper_20'] * 100
    ).fillna(0)
    df['dist_close_donchian_mid_pct'] = ((df['close'] - df['donchian_mid_20']) / df['donchian_mid_20'] * 100).fillna(0)
    df['dist_close_donchian_lower_pct'] = (
        (df['close'] - df['donchian_lower_20']) / df['donchian_lower_20'] * 100
    ).fillna(0)
    df['volume_avg_30'] = df['volume'].rolling(window=30, min_periods=1).mean()
    df['retest_volume_ratio_avg'] = (df['volume'] / df['volume_avg_30']).fillna(1)
    df['retest_volume'] = df['volume']

    return df.fillna(0)


def find_pivot_levels(df):
    if len(df) < PIVOT_WINDOW * 2 + 1:
        return []

    padded_high = np.pad(df['high'].values, (PIVOT_WINDOW, PIVOT_WINDOW), 'edge')
    padded_low = np.pad(df['low'].values, (PIVOT_WINDOW, PIVOT_WINDOW), 'edge')

    high_extrema_indices = scipy.signal.argrelextrema(padded_high, np.greater_equal, order=PIVOT_WINDOW)[0]
    low_extrema_indices = scipy.signal.argrelextrema(padded_low, np.less_equal, order=PIVOT_WINDOW)[0]

    levels = []
    for idx in high_extrema_indices:
        original_idx = idx - PIVOT_WINDOW
        if 0 <= original_idx < len(df):
            levels.append(
                {
                    'price': df.iloc[original_idx]['high'],
                    'type': 'resistance',
                    'index': original_idx,
                    'time': df.iloc[original_idx]['open_time'],
                }
            )
    for idx in low_extrema_indices:
        original_idx = idx - PIVOT_WINDOW
        if 0 <= original_idx < len(df):
            levels.append(
                {
                    'price': df.iloc[original_idx]['low'],
                    'type': 'support',
                    'index': original_idx,
                    'time': df.iloc[original_idx]['open_time'],
                }
            )
    return levels


def send_signal(conn, symbol, direction, prob, close_price):
    # Cooldown: 4h pro Coin/Direction. check_cooldown gibt True zurück wenn aktiv (blockiert).
    if check_cooldown(conn, MODEL_ID, symbol, direction, 4):
        logger.info(f"⏳ Cooldown active für {symbol} ({direction}).")
        return

    # Smart Targets: echte HVN/SR/Fib-basierte Entries, SL, 10 Targets — nicht mehr Dummy-Werte.
    trade_setup = calculate_smart_targets(conn, symbol, direction, close_price)
    entry1 = trade_setup['entry1']
    entry2 = trade_setup['entry2']
    sl = trade_setup['sl']
    targets = trade_setup['targets']

    lev = get_max_leverage(symbol, 20)

    lines = [
        f"📈 Signal for {symbol} 📈",
        f"🚨 Direction: {direction}",
        f"🚨 Leverage: {lev}",
        "🚨 Margin: Cross",
        f"🏦 CMP Entry: $ {entry1:.5f}",
        f"🏦 Entry 2: $ {entry2:.5f}",
    ]
    for i, t in enumerate(targets[:3], 1):
        lines.append(f"💰 TP{i}: $ {t:.5f}")
    lines += [f"💸 Stop Loss: $ {sl:.5f}", f"🧠 Trade idea generated by AI module {MODEL_ID}"]
    cornix_msg = "\n".join(lines)

    emoji = "🚀 AI ABR1 LONG SIGNAL" if direction == "LONG" else "💥 AI ABR1 SHORT SIGNAL"

    html = f"""<pre><b>{emoji}</b>\n<b>{symbol}</b>\n<b>→ Direction: {direction}</b>\n<b>→ ML Confidence: <b>{prob:.1%}</b></b>\n<b>→ Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M')} UTC | Modul: {MODEL_ID}</b>\n<b>→ Source: AI Break & Retest Model</b>\n\n{cornix_msg}</pre>"""

    chart_buf = generate_minichart_image(symbol, minutes=240)

    with conn.cursor() as cur:
        # Cornix Channel (Hier nutzt er den speziellen Rubberband Channel!)
        cur.execute(
            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (TARGET_CHANNEL_ID, cornix_msg)
        )
        if chart_buf:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                (TARGET_CHANNEL_ID, html, chart_buf),
            )
        else:
            cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (TARGET_CHANNEL_ID, html))

        cur.execute(
            """INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                symbol,
                float(entry1),
                MODEL_ID,
                direction,
                float(prob),
                float(entry1),
                float(entry2),
                float(sl),
                json.dumps(targets),
            ),
        )
    conn.commit()
    update_cooldown(conn, MODEL_ID, symbol, direction)
    logger.info(f"✅ {MODEL_ID} Signal für {symbol} in Outbox gelegt!")


def process_abr_logic(conn, symbol):
    try:
        query = f"""
            SELECT open_time, open, high, low, close, volume
            FROM "{symbol}_1h"
            WHERE open_time >= NOW() - INTERVAL '{LIVE_DATA_HISTORY_HOURS + 5} hours'
            ORDER BY open_time ASC;
        """
        df = pd.read_sql(query, conn)
        if df.empty or len(df) < max(PIVOT_WINDOW * 2, 30) + RETEST_BACKWARD_LOOKUP_CANDLES + 2:
            return

        df['open_time'] = pd.to_datetime(df['open_time'], utc=True)
        current_hour_utc = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
        df = df[df['open_time'] < current_hour_utc].tail(LIVE_DATA_HISTORY_HOURS).reset_index(drop=True)

        df_indicators = calculate_technical_indicators(df.copy())
        levels = find_pivot_levels(df_indicators)
        if not levels:
            return

        potential_retest_candle_indices = range(len(df_indicators) - 1, max(0, len(df_indicators) - 1 - 3), -1)

        for retest_idx in potential_retest_candle_indices:
            retest_candle = df_indicators.iloc[retest_idx]
            # FIX: Der frühere Filter `retest_candle['open_time'].minute != 0`
            # war wirkungslos — 1h-Kerzen haben IMMER minute=0. Die aktuelle
            # (laufende) Kerze wurde bereits oben via
            # `df = df[df['open_time'] < current_hour_utc]` weggeschnitten.

            for level in levels:
                if level['index'] >= retest_idx:
                    continue

                lvl_price = level['price']
                upper_bound = lvl_price * (1 + LEVEL_TOLERANCE_PCT)
                lower_bound = lvl_price * (1 - LEVEL_TOLERANCE_PCT)

                is_retest_long = retest_candle['low'] <= upper_bound and retest_candle['low'] >= lower_bound
                is_retest_short = retest_candle['high'] >= lower_bound and retest_candle['high'] <= upper_bound

                if not (is_retest_long or is_retest_short):
                    continue

                break_found = False
                direction = None
                search_start_idx = retest_idx - 1
                search_end_idx = max(level['index'], retest_idx - RETEST_BACKWARD_LOOKUP_CANDLES)

                for break_idx in range(search_start_idx, search_end_idx, -1):
                    b_candle = df_indicators.iloc[break_idx]
                    prev_b_candle = df_indicators.iloc[break_idx - 1] if break_idx > 0 else None
                    if prev_b_candle is None:
                        continue

                    if (
                        level['type'] == 'resistance'
                        and prev_b_candle['close'] < lvl_price
                        and b_candle['close'] > lvl_price
                    ):
                        break_found = True
                        direction = 'LONG'
                        break
                    elif (
                        level['type'] == 'support'
                        and prev_b_candle['close'] > lvl_price
                        and b_candle['close'] < lvl_price
                    ):
                        break_found = True
                        direction = 'SHORT'
                        break

                if break_found and direction:
                    current_model = MODELS[direction]
                    current_threshold = THRESHOLDS[direction]

                    X_event_features = retest_candle[FEATURE_COLUMNS].values
                    X_event = pd.DataFrame([X_event_features], columns=FEATURE_COLUMNS, dtype=float)

                    # FIX: Defensive Absicherung gegen NaN/Inf in den Features.
                    # Vorher konnte ein einziges NaN (z.B. bei frischen Coins mit
                    # Indikator-Warmup-Phase) die ML-Prediction crashen oder
                    # Garbage-Werte liefern. Jetzt: NaN/Inf → 0 (neutral).
                    X_event = X_event.replace([np.inf, -np.inf], np.nan).fillna(0)

                    prediction_proba = float(current_model.predict_proba(X_event)[0, SUCCESS_CLASS_IDX])

                    if prediction_proba >= 0.50:  # Grundbedingung aus deinem alten Script
                        logger.info(
                            f"ABR1 Break&Retest erkannt bei {symbol} | Dir: {direction} | Prob: {prediction_proba:.2f}"
                        )
                        if prediction_proba >= current_threshold:
                            send_signal(conn, symbol, direction, prediction_proba, retest_candle['close'])

    except Exception as e:
        logger.error(f"Error for {symbol}: {e}")


def main():
    logger.info("=== AI BREAK & RETEST BOT (ABR1) GESTARTET ===")
    coins = load_models_and_coins()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        # Läuft immer um Minute 10, wie von dir im alten JobQueue geplant
        if now.minute == 2:
            logger.info("Starting ABR1 Scan...")
            conn = get_db_connection()
            conn.autocommit = True
            try:
                for symbol in coins:
                    process_abr_logic(conn, symbol)
            finally:
                conn.close()
            logger.info("ABR1 Scan stopped.")
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

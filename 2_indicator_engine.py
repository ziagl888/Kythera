import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

import pandas as pd
import json
import datetime
import numpy as np
from psycopg2 import extras
import logging
import time
from concurrent.futures import ProcessPoolExecutor
from scipy import stats
import scipy.signal
import os
import sys

from core.config import INDICATOR_TIMEFRAMES, NUM_WORKERS
from core.database import get_db_connection
from core.market_utils import load_coins





STATE_FILE = 'indicator_state.json'

# --- Konfiguration ---
COINS_FILE = 'coins.json'
INDICATOR_SUFFIX = '_indicators'

import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - INDICATOR_ENGINE - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('indicator_calculation.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


# HILFSFUNKTIONEN
def get_timeframe_delta(timeframe):
    if timeframe == '1h': return pd.Timedelta(hours=1)
    if timeframe == '15m': return pd.Timedelta(minutes=15)
    if timeframe == '5m': return pd.Timedelta(minutes=5)
    if timeframe == '30m': return pd.Timedelta(minutes=30)
    if timeframe == '2h': return pd.Timedelta(hours=2)
    if timeframe == '4h': return pd.Timedelta(hours=4)
    if timeframe == '1d': return pd.Timedelta(days=1)
    if timeframe == '1w': return pd.Timedelta(weeks=1)
    return pd.Timedelta(hours=1)



def table_exists(conn, table_name):
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", (table_name,))
            result = cursor.fetchone()
            return result[0] is not None
    except Exception as e:
        logger.error(f"Error checking table existence {table_name}: {e}")
        conn.rollback()
        return False


def get_indicator_definitions():
    definitions = {
        "RSI_6": "REAL", "RSI_9": "REAL", "RSI_12": "REAL", "RSI_14": "REAL", "RSI_24": "REAL",
        "EMA_7": "REAL", "EMA_9": "REAL", "EMA_12": "REAL", "EMA_21": "REAL", "EMA_26": "REAL",
        "EMA_34": "REAL", "EMA_50": "REAL", "EMA_55": "REAL", "EMA_89": "REAL", "EMA_99": "REAL", "EMA_200": "REAL",
        "MA_7": "REAL", "MA_10": "REAL", "MA_20": "REAL", "MA_25": "REAL", "MA_50": "REAL",
        "MA_99": "REAL", "MA_100": "REAL", "MA_200": "REAL",
        "WMA_7": "REAL", "WMA_9": "REAL", "WMA_12": "REAL", "WMA_21": "REAL", "WMA_26": "REAL",
        "WMA_34": "REAL", "WMA_50": "REAL", "WMA_55": "REAL", "WMA_89": "REAL", "WMA_99": "REAL", "WMA_200": "REAL",
        "SMMA_10": "REAL", "SMMA_20": "REAL", "SMMA_25": "REAL", "SMMA_50": "REAL", "SMMA_99": "REAL",
        "SMMA_100": "REAL", "SMMA_200": "REAL",
        "KAMA_7": "REAL", "KAMA_9": "REAL", "KAMA_12": "REAL", "KAMA_21": "REAL", "KAMA_26": "REAL",
        "KAMA_34": "REAL", "KAMA_50": "REAL", "KAMA_55": "REAL", "KAMA_89": "REAL", "KAMA_99": "REAL",
        "ATR_9": "REAL", "ATR_14": "REAL", "ATR_21": "REAL",
        "TSI_25_13_13": "REAL", "TSI_25_13_13_SIGNAL": "REAL",
        "TSI_FAST_12_7_7": "REAL", "TSI_FAST_12_7_7_SIGNAL": "REAL",
        "HVN_1": "REAL", "HVN_2": "REAL", "HVN_3": "REAL", "POC": "REAL",
        "MACD_DIF_FAST_9_21_9": "REAL", "MACD_DEA_FAST_9_21_9": "REAL",
        "MACD_DIF_NORMAL_12_26_9": "REAL", "MACD_DEA_NORMAL_12_26_9": "REAL",
        "BOLL_UPPER_20": "REAL", "BOLL_MID_20": "REAL", "BOLL_LOWER_20": "REAL",
        "TRENDLINE_SLOPE": "REAL", "TRENDLINE_INTERCEPT": "REAL",
        "CHANNEL_UPPER_PRICE": "REAL", "CHANNEL_LOWER_PRICE": "REAL",
        "TRENDLINE_PRICE": "REAL", "MID_LINE": "REAL", "R_SQUARED": "REAL",
        "TREND_DIRECTION": "TEXT",
        "SUPPORT_PRICE": "REAL", "RESISTANCE_PRICE": "REAL"
    }
    for w in [4, 10, 12, 15, 20]:
        definitions[f"DONCHIAN_UPPER_{w}"] = "REAL"
        definitions[f"DONCHIAN_LOWER_{w}"] = "REAL"
        definitions[f"DONCHIAN_MID_{w}"] = "REAL"
    for level in [0.236, 0.382, 0.5, 0.618, 0.786]:
        l_str = str(level).replace('.', '_')
        definitions[f"FIB_SUPPORT_{l_str}"] = "REAL"
        definitions[f"FIB_RESISTANCE_{l_str}"] = "REAL"
    for ext in [1.272, 1.618, 2.618]:
        e_str = str(ext).replace('.', '_')
        definitions[f"FIB_EXTENSION_{e_str}"] = "REAL"
    return definitions


def create_indicator_table(conn, symbol, timeframe, definitions):
    table_name = f'"{symbol}_{timeframe}{INDICATOR_SUFFIX}"'
    cols_sql = ",\n".join([f"{n} {t}" for n, t in definitions.items()])
    sql = f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            symbol TEXT,
            open_time TIMESTAMP WITH TIME ZONE,
            close REAL,
            {cols_sql},
            PRIMARY KEY (symbol, open_time)
        );
        CREATE INDEX IF NOT EXISTS idx_{symbol}_{timeframe}_ind_ot ON {table_name} (open_time DESC);
    """
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def update_timeframe_state(timeframe, status):
    """Writes the current status to a JSON file so other scripts can read it."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
        else:
            state = {}

        state[timeframe] = {
            'status': status,
            'timestamp': datetime.datetime.now().isoformat()
        }

        # FIX (#45): Atomares Write via Temp + os.replace. Vorher wurde direkt
        # in die Zieldatei geschrieben — bei gleichzeitigem Read aus dem
        # Detector-Prozess konnte der Reader einen halbgeschriebenen JSON-File
        # sehen und abstürzen. Jetzt: kompletter Schreibvorgang in Temp-File,
        # dann atomarer Swap.
        tmp = STATE_FILE + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error(f"Error in status update for {timeframe}: {e}")

# MATHEMATIK & INDIKATOREN
def calculate_trendline_and_channel_robust_optimized(df):
    lookback = min(len(df), 100)
    subset = df.iloc[-lookback:].copy()
    y = subset['close'].values
    x = np.arange(len(y))

    # FIX (#6): Defensive gegen NaN-Output bei konstanten Preisen und gegen
    # Division-durch-0 bei y[0]==0 (theoretisch unmöglich bei echten Preisen,
    # aber möglich wenn die ersten Kerzen durch fehlerhafte Ingestion 0 enthalten).
    if len(y) < 2 or np.all(y == y[0]):
        # Konstante Serie → keine Trendaussage, return neutral
        slope, intercept, r_value = 0.0, float(y[0]) if len(y) > 0 else 0.0, 0.0
        trendline_values = np.full(len(df), intercept)
        std_dev = 0.0
    else:
        slope, intercept, r_value, p_value, std_err = stats.linregress(x, y)
        if not np.isfinite(slope):
            slope = 0.0
        if not np.isfinite(intercept):
            intercept = float(y[-1])
        if not np.isfinite(r_value):
            r_value = 0.0
        full_x = np.arange(len(df)) - (len(df) - lookback)
        trendline_values = slope * full_x + intercept
        residuals = y - (slope * x + intercept)
        std_dev = np.std(residuals) if len(residuals) > 0 else 0.0
        if not np.isfinite(std_dev):
            std_dev = 0.0

    upper_channel = trendline_values + (2 * std_dev)
    lower_channel = trendline_values - (2 * std_dev)

    # FIX (#6): Division-durch-0-safe — y[0]==0 würde den Threshold auf 0 setzen
    # und JEDE minimale Slope als "UP" klassifizieren.
    direction = "SIDEWAYS"
    base = float(y[0]) if len(y) > 0 and y[0] != 0 else float(y[-1]) if len(y) > 0 else 1.0
    threshold = 0.0001 * abs(base) if base != 0 else 1e-8
    if slope > threshold:
        direction = "UP"
    elif slope < -threshold:
        direction = "DOWN"

    return {
        "TRENDLINE_SLOPE": pd.Series(slope, index=df.index),
        "TRENDLINE_INTERCEPT": pd.Series(intercept, index=df.index),
        "TRENDLINE_PRICE": pd.Series(trendline_values, index=df.index),
        "CHANNEL_UPPER_PRICE": pd.Series(upper_channel, index=df.index),
        "CHANNEL_LOWER_PRICE": pd.Series(lower_channel, index=df.index),
        "MID_LINE": pd.Series(trendline_values, index=df.index),
        "R_SQUARED": pd.Series(r_value ** 2, index=df.index),
        "TREND_DIRECTION": pd.Series(direction, index=df.index)
    }


def get_hvn_poc_for_dataset(df, timeframe):
    try:
        prices = df['close'].values
        volumes = df['volume'].values if 'volume' in df.columns else np.ones(len(prices))
        bins = int(np.sqrt(len(prices)))
        hist, bin_edges = np.histogram(prices, bins=bins, weights=volumes)
        poc_idx = np.argmax(hist)
        poc_price = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2
        peaks, _ = scipy.signal.find_peaks(hist, distance=5)
        sorted_peaks = sorted(peaks, key=lambda i: hist[i], reverse=True)
        hvn_prices = []
        for idx in sorted_peaks[:4]:
            p = (bin_edges[idx] + bin_edges[idx + 1]) / 2
            if abs(p - poc_price) > (poc_price * 0.005):
                hvn_prices.append(p)
        while len(hvn_prices) < 3: hvn_prices.append(0)
        return {"POC": poc_price, "HVN_1": hvn_prices[0], "HVN_2": hvn_prices[1], "HVN_3": hvn_prices[2]}
    except Exception:
        return {"POC": 0, "HVN_1": 0, "HVN_2": 0, "HVN_3": 0}


def find_support_resistance(df, window=20):
    try:
        highs = df['high'].values
        lows = df['low'].values
        max_idx = scipy.signal.argrelextrema(highs, np.greater, order=window)[0]
        resistances = [(highs[i], df.index[i]) for i in max_idx]
        min_idx = scipy.signal.argrelextrema(lows, np.less, order=window)[0]
        supports = [(lows[i], df.index[i]) for i in min_idx]
        resistances.sort(key=lambda x: x[1], reverse=True)
        supports.sort(key=lambda x: x[1], reverse=True)
        return supports, resistances
    except Exception:
        return [], []


def calc_fibonacci_levels_dynamic(df, timeframe='1h'):
    try:
        max_price = df['high'].max()
        min_price = df['low'].min()
        diff = max_price - min_price
        fibs = {'support': [], 'resistance': [], 'extensions': []}
        for level in [0.236, 0.382, 0.5, 0.618, 0.786]:
            price = max_price - (diff * level)
            fibs['support'].append({'level': level, 'price': price})
            fibs['resistance'].append({'level': level, 'price': price})
        for ext in [1.272, 1.618, 2.618]:
            price = max_price + (diff * (ext - 1))
            fibs['extensions'].append({'level': ext, 'price': price})
        return fibs
    except Exception:
        return {'support': [], 'resistance': [], 'extensions': []}


def calculate_wma(series, period):
    weights = np.arange(1, period + 1)
    sum_weights = weights.sum()
    return series.rolling(window=period).apply(lambda x: np.dot(x, weights) / sum_weights, raw=True).fillna(0)


def calculate_smma(series, period):
    return series.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def calculate_rsi(series, period=14):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    roll_up = up.ewm(span=period, adjust=False).mean()
    roll_down = down.ewm(span=period, adjust=False).mean()
    rs = roll_up / roll_down
    # FIX: Vorher `100 - (100 / (1 + rs)).fillna(0)` → fillna(0) nur auf das
    # INNERE Ergebnis angewandt. Bei NaN-rs wurde daraus `100 - 0 = 100` →
    # RSI zeigte fälschlich 100 (max overbought) an Stellen ohne Daten,
    # was false SHORTs triggerte. Jetzt die GESAMTE Formel mit fillna(50) (neutral).
    return (100.0 - (100.0 / (1.0 + rs))).fillna(50)


def calculate_kama(series, period=10, fast=2, slow=30):
    """
    Kaufman's Adaptive Moving Average.

    Bootstrap: die ersten `period-1` Werte sind undefined (NaN), der Wert bei
    Index `period-1` wird als SMA der ersten `period` Closes initialisiert.
    Das vermeidet verzerrte KAMA-Werte am Anfang (vorher: kama[:period] = close[:period]
    was zu künstlich volatiler KAMA in den ersten Bars führte).
    """
    closes = series.values
    kama = np.full_like(closes, np.nan, dtype=float)
    if len(closes) <= period:
        return pd.Series(kama, index=series.index)

    # SMA-Bootstrap am Index period-1
    kama[period - 1] = float(np.mean(closes[:period]))
    fast_sc = 2 / (fast + 1)
    slow_sc = 2 / (slow + 1)

    for i in range(period, len(closes)):
        change = abs(closes[i] - closes[i - period])
        volatility = np.sum(np.abs(np.diff(closes[i - period:i + 1])))
        er = change / volatility if volatility != 0 else 0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama[i] = kama[i - 1] + sc * (closes[i] - kama[i - 1])
    return pd.Series(kama, index=series.index)


def calculate_indicators_optimized(df, timeframe):
    df = df.sort_values('open_time')
    close = df['close']
    high = df['high']
    low = df['low']
    results = {}

    for p in [6, 9, 12, 14, 24]: results[f'RSI_{p}'] = calculate_rsi(close, p)
    for p in [7, 9, 12, 21, 26, 34, 50, 55, 89, 99, 200]: results[f'EMA_{p}'] = close.ewm(span=p,
                                                                                          adjust=False).mean().fillna(0)
    for p in [7, 10, 20, 25, 50, 99, 100, 200]: results[f'MA_{p}'] = close.rolling(window=p).mean().fillna(0)
    for p in [7, 9, 12, 21, 26, 34, 50, 55, 89, 99, 200]: results[f'WMA_{p}'] = calculate_wma(close, period=p)
    for p in [10, 20, 25, 50, 99, 100, 200]: results[f'SMMA_{p}'] = calculate_smma(close, period=p)
    for p in [7, 9, 12, 21, 26, 34, 50, 55, 89, 99]: results[f'KAMA_{p}'] = calculate_kama(close, period=p)

    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    results['BOLL_MID_20'] = mid.fillna(0)
    results['BOLL_UPPER_20'] = (mid + 2 * std).fillna(0)
    results['BOLL_LOWER_20'] = (mid - 2 * std).fillna(0)

    for w in [4, 10, 12, 15, 20]:
        results[f'DONCHIAN_UPPER_{w}'] = high.rolling(w).max().fillna(0)
        results[f'DONCHIAN_LOWER_{w}'] = low.rolling(w).min().fillna(0)
        results[f'DONCHIAN_MID_{w}'] = ((results[f'DONCHIAN_UPPER_{w}'] + results[f'DONCHIAN_LOWER_{w}']) / 2).fillna(0)

    def calc_macd(fast, slow, sig):
        f_ema = close.ewm(span=fast, adjust=False).mean()
        s_ema = close.ewm(span=slow, adjust=False).mean()
        dif = f_ema - s_ema
        dea = dif.ewm(span=sig, adjust=False).mean()
        return dif, dea

    results['MACD_DIF_FAST_9_21_9'], results['MACD_DEA_FAST_9_21_9'] = calc_macd(9, 21, 9)
    results['MACD_DIF_NORMAL_12_26_9'], results['MACD_DEA_NORMAL_12_26_9'] = calc_macd(12, 26, 9)

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    for p in [9, 14, 21]: results[f'ATR_{p}'] = tr.ewm(alpha=1 / p, adjust=False).mean().fillna(0)

    def calc_tsi(r, s):
        diff = close.diff()
        smooth = diff.ewm(span=r, adjust=False).mean()
        double = smooth.ewm(span=s, adjust=False).mean()
        abs_smooth = diff.abs().ewm(span=r, adjust=False).mean()
        abs_double = abs_smooth.ewm(span=s, adjust=False).mean()
        return 100 * (double / abs_double).fillna(0)

    results['TSI_25_13_13'] = calc_tsi(25, 13)
    results['TSI_25_13_13_SIGNAL'] = results['TSI_25_13_13'].ewm(span=13, adjust=False).mean()
    results['TSI_FAST_12_7_7'] = calc_tsi(12, 7)
    results['TSI_FAST_12_7_7_SIGNAL'] = results['TSI_FAST_12_7_7'].ewm(span=7, adjust=False).mean()

    trend_data = calculate_trendline_and_channel_robust_optimized(df)
    results.update(trend_data)

    hvn_data = get_hvn_poc_for_dataset(df, timeframe)
    for k, v in hvn_data.items(): results[k] = v

    try:
        sup, res = find_support_resistance(df)
        # FIX: Vorher sup[0][0]/res[0][0] = einfach der ZEITLICH neueste Pivot,
        # egal wo preislich → führte oft dazu, dass SUPPORT_PRICE > RESISTANCE_PRICE
        # (wenn z.B. der jüngste High-Pivot unter dem jüngsten Low-Pivot lag).
        # Jetzt: Support = nächster Pivot-Tief UNTER dem aktuellen Preis,
        # Resistance = nächster Pivot-Hoch ÜBER dem aktuellen Preis.
        last_close = float(df['close'].iloc[-1]) if not df['close'].empty else 0
        valid_sup = [p for p, _ in sup if p < last_close]
        valid_res = [p for p, _ in res if p > last_close]
        # Den nächstgelegenen nehmen (größter Support < Preis, kleinste Resistance > Preis)
        results['SUPPORT_PRICE'] = max(valid_sup) if valid_sup else (sup[0][0] if sup else 0)
        results['RESISTANCE_PRICE'] = min(valid_res) if valid_res else (res[0][0] if res else 0)
    except Exception:
        results['SUPPORT_PRICE'] = 0
        results['RESISTANCE_PRICE'] = 0

    fibs = calc_fibonacci_levels_dynamic(df, timeframe=timeframe)
    for lvl in [0.236, 0.382, 0.5, 0.618, 0.786]:
        l_str = str(lvl).replace('.', '_')
        results[f"FIB_SUPPORT_{l_str}"] = next((i['price'] for i in fibs['support'] if i['level'] == lvl), 0)
        results[f"FIB_RESISTANCE_{l_str}"] = next((i['price'] for i in fibs['resistance'] if i['level'] == lvl), 0)

    for ext in [1.272, 1.618, 2.618]:
        e_str = str(ext).replace('.', '_')
        results[f"FIB_EXTENSION_{e_str}"] = next((i['price'] for i in fibs['extensions'] if i['level'] == ext), 0)

    indicators_df = pd.DataFrame(results, index=df.index)
    indicators_df['open_time'] = df['open_time']
    indicators_df['close'] = df['close']
    indicators_df['symbol'] = df['symbol'].iloc[0] if not df.empty else ''
    return indicators_df


def write_indicators_to_db_optimized(conn, df, symbol, timeframe, definitions):
    table_name = f'"{symbol}_{timeframe}{INDICATOR_SUFFIX}"'
    valid_cols = ['symbol', 'open_time', 'close'] + list(definitions.keys())
    for col in valid_cols:
        if col not in df.columns: df[col] = 0

    df_to_write = df[valid_cols].copy()
    data_values = [tuple(x) for x in df_to_write.to_numpy()]
    cols_str = ', '.join(valid_cols)
    update_cols = [c for c in valid_cols if c not in ['symbol', 'open_time']]
    update_sql = ", ".join([f"{c} = EXCLUDED.{c}" for c in update_cols])

    sql = f"""
        INSERT INTO {table_name} ({cols_str})
        VALUES %s
        ON CONFLICT (symbol, open_time)
        DO UPDATE SET {update_sql}
    """
    with conn.cursor() as cur:
        extras.execute_values(cur, sql, data_values)
    conn.commit()


# DB-WORKER
def process_coin_task(args):
    """Wrapper Funktion für den ProcessPoolExecutor"""

    # --- NEU: DIESER BLOCK MUSS GENAU HIER REIN! ---
    import warnings
    warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

    symbol, timeframe = args

    try:
        # Jeder CPU-Kern macht seine eigene saubere DB-Verbindung auf
        conn = get_db_connection()
    except Exception as e:
        logger.error(f"DB Connect Error in Worker: {e}")
        return

    try:
        definitions = get_indicator_definitions()
        ohlcv_table = f'"{symbol}_{timeframe}"'
        ind_table = f'"{symbol}_{timeframe}{INDICATOR_SUFFIX}"'

        if not table_exists(conn, ohlcv_table):
            return  # Keine Rohdaten da, skippingn

        if not table_exists(conn, ind_table):
            create_indicator_table(conn, symbol, timeframe, definitions)

        with conn.cursor() as cur:
            cur.execute(f"SELECT MAX(open_time) FROM {ind_table}")
            last_ind_time = cur.fetchone()[0]


        if last_ind_time is None:
            start_fetch_time = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
            lookback_candles = 3000  # Voller Load nur beim allerersten Mal!
        else:
            if not isinstance(last_ind_time, pd.Timestamp):
                last_ind_time = pd.Timestamp(last_ind_time)
            if last_ind_time.tzinfo is None:
                last_ind_time = last_ind_time.tz_localize('UTC')
            start_fetch_time = last_ind_time
            lookback_candles = 1000

        tf_delta = get_timeframe_delta(timeframe)
        load_start = start_fetch_time - (tf_delta * lookback_candles)
        save_start_filter = start_fetch_time - (tf_delta * 5)
        # FIX: Hier war vorher ein Copy-Paste-Dubletten-Block, der load_start IMMER
        # auf 3000 Kerzen setzte (statt der 1000 beim inkrementellen Lauf).
        # Dadurch wurde bei JEDEM 30-Min-Zyklus 3× so viel geladen wie nötig.

        sql = f"SELECT * FROM {ohlcv_table} WHERE open_time >= %s ORDER BY open_time ASC"
        df_raw = pd.read_sql(sql, conn, params=(load_start,))

        if df_raw.empty or len(df_raw) < 50:
            return

        df_raw['open_time'] = pd.to_datetime(df_raw['open_time'], utc=True)
        if 'symbol' not in df_raw.columns:
            df_raw['symbol'] = symbol

        df_ind = calculate_indicators_optimized(df_raw, timeframe)
        df_save = df_ind[df_ind['open_time'] >= save_start_filter]

        if not df_save.empty:
            write_indicators_to_db_optimized(conn, df_save, symbol, timeframe, definitions)

    except Exception as e:
        logger.error(f"Error {symbol} ({timeframe}): {e}")
        conn.rollback()
    finally:
        conn.close()


# HAUPTSCHLEIFE (WATCHDOG READY)
def main():
    logger.info("=== INDICATOR ENGINE GESTARTET ===")

    # Unsere exakten Trigger-Minuten (+2 Minuten Puffer)
    target_minutes = [2, 32]

    # Beim Start setzen wir einmal alles auf 'unknown' oder 'working'
    for tf in INDICATOR_TIMEFRAMES:
        update_timeframe_state(tf, 'waiting_for_trigger')

    while True:
        now = datetime.datetime.now()

        # Prüfen, ob wir in einer der magischen Minuten sind
        if now.minute in target_minutes:
            symbols = load_coins()
            if not symbols:
                logger.warning("Keine Coins in coins.json gefunden. Waiting...")
                time.sleep(60)
                continue

            logger.info(f"⏰ Zeit-Trigger {now.strftime('%H:%M')} erreicht! Starting Berechnungen...")
            start_time = time.time()

            # --- WICHTIG: Wir verarbeiten die Timeframes jetzt aftereinander! ---
            # So springt '30m' auf 'updated', sobald es fertig ist, und Skript 3 kann schon starten.
            for current_tf in INDICATOR_TIMEFRAMES:
                logger.info(f"⚙️ Starting Berechnungen für Timeframe: {current_tf}...")
                update_timeframe_state(current_tf, 'working')

                # Wir bauen die Tasks NUR für den aktuellen Timeframe
                tasks = [(s, current_tf) for s in symbols]

                with ProcessPoolExecutor(max_workers=NUM_WORKERS) as exe:
                    list(exe.map(process_coin_task, tasks))

                # Timeframe ist fertig -> Gib ihn für Ebene 3 (Detectors) frei!
                update_timeframe_state(current_tf, 'updated')
                logger.info(f"✅ Timeframe {current_tf} erfolgreich completed und freigegeben!")

            duration = time.time() - start_time
            logger.info(f"🏁 Kompletter Indikator-Zyklus completed in {duration:.1f} Sekunden!")

            # Schlafe für 65 seconds. Dadurch stellen wir sicher, dass wir in Minute '3'
            # oder '18' aufwachen und den Trigger nicht doppelt auslösen!
            logger.info("Schlafe bis zum nächsten Trigger...")
            time.sleep(65)

        else:
            # Wir sind nicht in einer Trigger-Minute. Einfach kurz warten und wieder prüfen.
            # (10 Sekunden sind optimal: Es kostet 0% CPU und trifft die Minute genau genug)
            time.sleep(10)


if __name__ == "__main__":
    main()
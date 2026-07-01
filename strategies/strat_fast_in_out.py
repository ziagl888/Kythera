from core.market_utils import get_max_leverage, is_trade_already_active
# strategies/strat_fast_in_out.py
import logging
import datetime
import os
import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

logger = logging.getLogger(__name__)

# Die Spalten, die du brauchst
REQUIRED_COLUMNS = ['rsi_9', 'rsi_14', 'tsi_fast_12_7_7', 'tsi_fast_12_7_7_signal', 'ema_9', 'ema_12', 'ema_21',
                    'ema_26', 'ema_55', 'ema_89', 'wma_9', 'wma_12', 'close', 'kama_9', 'kama_12', 'kama_21',
                    'macd_dif_fast_9_21_9', 'macd_dea_fast_9_21_9', 'donchian_mid_4', 'boll_mid_20', 'atr_14',
                    'support_price', 'resistance_price']


def check_recent_trades(conn, direction, hours=3, count=500):
    """
    Prüft den Richtungs-Cooldown.

    Der Cooldown blockiert weitere signals in Richtung X, wenn in den letzten
    `hours` Stunden bereits `count` Trades dieser Richtung als WIN geschlossen
    wurden. Das soll Over-Exposure in einseitigen Marktphasen begrenzen.

    Änderungen gegenüber Vorgänger:
    1. Zählt ALLE Wins (TP1-TP4), nicht nur TP1.
       Vorher: WHERE status = '1'  → nur TP1-Hits
       Jetzt:  WHERE status IN ('1','2','3','4')  → alle erfolgreichen Trades
       Grund: status='1' war willkürlich — ein TP2/3/4-Hit ist ein ebenso
       klares "haben wir gut gespielt"-Signal wie TP1.

    2. Count-Schwelle von 250 auf 500 verdoppelt.
       Grund: Bei 570 Coins × 6 klassischen Bots sind 250 Wins in 3h normaler
       Alltag bei einseitigem Markt. Die alte Schwelle hat legitime Trend-
       Fortsetzungen blockiert (siehe Log: 27 SHORT-Blocks in Folge weil
       der Markt bearish war).

    Wenn du den Cooldown wieder aggressiver willst: count=300 im Call unten.
    Wenn du ihn deaktivieren willst: count=999999.
    """
    time_threshold = datetime.datetime.now() - datetime.timedelta(hours=hours)
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT COUNT(*) FROM closed_trades_master
            WHERE status IN ('1','2','3','4') AND direction = %s AND posted >= %s;
        """, (direction, time_threshold))
        return cursor.fetchone()[0] > count



def evaluate_conditions(data, direction):
    """Checks indicators for LONG or SHORT"""
    if data is None or data.empty: return False
    if not all(col in data.columns for col in REQUIRED_COLUMNS): return False

    # df_indicators kommt DESC sortiert aus dem Detector (iloc[0] = NEUESTE Kerze!)
    last_row = data.iloc[0]

    try:
        if direction == 'LONG':
            if not (55 <= last_row['rsi_9'] <= 75): return False
            if not (last_row['ema_9'] > last_row['ema_21']): return False
            if not (last_row['close'] < last_row['resistance_price'] * 0.95): return False
            return True

        elif direction == 'SHORT':
            # FIX: Vorher war die Bedingung `rsi_9 >= 75 OR rsi_9 <= 45` → das öffnet
            # SHORT-signals sowohl bei Overbought (75+) als auch bei schwachem
            # Downtrend (45-) gleichzeitig. Bei 75+ würdest du genau dort shorten,
            # wo LONG-Setups laufen. Korrekt: nur im "schwach/bearish"-RSI-Bereich.
            if not (last_row['rsi_9'] <= 45): return False
            if not (last_row['ema_9'] < last_row['ema_21']): return False
            if not (last_row['close'] > last_row['support_price'] * 0.95): return False
            return True

    except Exception as e:
        logger.error(f"Error for Bedingungsprüfung: {e}")
        return False

    return False


def analyze_coin(conn, symbol, df_indicators, live_price):
    """
    Das ist die Hauptfunktion, die vom Detector aufgerufen wird.
    Sie gibt ein fertiges Signal-Dictionary zurück oder None.
    """


    for direction in ['LONG', 'SHORT']:
        # 1. Indikatoren checken
        if not evaluate_conditions(df_indicators, direction):
            continue

        # 2. Cooldown checken
        if check_recent_trades(conn, direction):
            logger.info(f"[{symbol}] Zu viele {direction} Trades. Cooldown active.")
            continue

        # 3. Ist der Trade schon aktiv?
        if is_trade_already_active(conn, symbol, direction, 'Fast In And Out'):
            logger.info(f"[{symbol}] {direction} Trade läuft bereits.")
            continue

        # 4. TP / SL berechnen (wie in deinem Script 3 & 4)
        # FIX: Vorher iloc[-1] → das war die ÄLTESTE Kerze (df ist DESC sortiert aus
        # dem Detector, iloc[0] = neueste). Die SL-Berechnung nutzte damit ATR
        # von vor 10 Tagen, was bei volatilen Coins zu völlig falschen SLs führte.
        atr_14 = float(df_indicators['atr_14'].iloc[0])
        lev = get_max_leverage(symbol, 20)

        if direction == 'LONG':
            target1 = live_price * 1.0125
            sl_calc = live_price - (2.5 * atr_14)
            sl = live_price * 0.975 if ((live_price - 2.5 * atr_14) / live_price) - 1 <= -0.025 else sl_calc
        else:  # SHORT
            target1 = live_price * (1 - 0.0125)
            sl_calc = live_price + (2.5 * atr_14)
            sl = live_price * 1.025 if ((live_price + 2.5 * atr_14) / live_price) - 1 >= 0.025 else sl_calc

        # 5. Live-Preis Check (Sind wir schon ins Target oder SL gerauscht?)
        if direction == 'LONG' and (live_price <= sl or live_price >= target1):
            continue
        if direction == 'SHORT' and (live_price >= sl or live_price <= target1):
            continue

        margin = 'Cross'

        return {
            "strategy": "Fast In And Out",
            "coin": symbol,
            "direction": direction,
            "margin": margin,
            "entry": live_price,
            "lev": lev,
            "target1": target1,
            "sl": sl
        }

    return None  # Kein Signal gefunden
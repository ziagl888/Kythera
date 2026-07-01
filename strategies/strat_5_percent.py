from core.market_utils import get_max_leverage, is_trade_already_active
# strategies/strat_5_percent.py
import logging
import datetime
import os
import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ['rsi_9', 'rsi_14', 'tsi_fast_12_7_7', 'tsi_fast_12_7_7_signal', 'ema_9', 'ema_12', 'ema_21',
                    'ema_26', 'ema_55', 'ema_89', 'wma_9', 'wma_12', 'close', 'kama_9', 'kama_12', 'kama_21',
                    'macd_dif_fast_9_21_9', 'macd_dea_fast_9_21_9', 'donchian_mid_4', 'boll_mid_20', 'atr_14',
                    'support_price', 'resistance_price']



def check_recent_trades(conn, direction, hours=3, count=500):
    """
    Prüft den Richtungs-Cooldown.

    Siehe Dokumentation in strat_fast_in_out.py:check_recent_trades für Details.
    Zählt jetzt ALLE Wins (nicht nur TP1) und hat Schwelle 500 statt 250.
    """
    time_threshold = datetime.datetime.now() - datetime.timedelta(hours=hours)
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT COUNT(*) FROM closed_trades_master
            WHERE status IN ('1','2','3','4') AND direction = %s AND posted >= %s;
        """, (direction, time_threshold))
        return cursor.fetchone()[0] > count



def evaluate_conditions(data, direction):
    if data is None or data.empty: return False
    if not all(col in data.columns for col in REQUIRED_COLUMNS): return False

    # df_indicators kommt DESC sortiert aus dem Detector (iloc[0] = NEUESTE Kerze!)
    last_row = data.iloc[0]

    try:
        if direction == 'LONG':
            if not (55 <= last_row['rsi_9'] <= 75): return False
            if not (55 <= last_row['rsi_14'] <= 75): return False
            if not (5 <= last_row['tsi_fast_12_7_7'] <= 40): return False
            if not (last_row['tsi_fast_12_7_7_signal'] > 5): return False
            if not (last_row['tsi_fast_12_7_7'] > last_row['tsi_fast_12_7_7_signal']): return False
            if not (last_row['ema_9'] > last_row['ema_21']): return False
            if not (last_row['ema_12'] > last_row['ema_26']): return False
            if not (last_row['close'] > last_row['ema_55']): return False
            if not (last_row['close'] > last_row['ema_89']): return False
            if not (last_row['close'] > last_row['ema_200']): return False
            if not (last_row['wma_9'] > last_row['ema_21']): return False
            if not (last_row['wma_12'] > last_row['ema_26']): return False
            if not (last_row['ema_21'] > last_row['ema_55']): return False
            if not (last_row['ema_21'] > last_row['ema_89']): return False
            if not (last_row['ema_21'] > last_row['ema_200']): return False
            if not (last_row['ema_9'] > last_row['wma_21']): return False
            if not (last_row['ema_12'] > last_row['wma_26']): return False
            if not (last_row['close'] > last_row['kama_9']): return False
            if not (last_row['close'] > last_row['kama_12']): return False
            if not (last_row['close'] > last_row['kama_21']): return False
            if not (last_row['macd_dif_fast_9_21_9'] > last_row['macd_dea_fast_9_21_9']): return False
            if not (last_row['close'] > last_row['donchian_mid_4']): return False
            if not (last_row['close'] > last_row['boll_mid_20']): return False
            if not (last_row['close'] < last_row['resistance_price'] * 0.95): return False
            if not (last_row['close'] >= last_row['support_price'] * 0.999): return False
            return True

        elif direction == 'SHORT':
            # FIX: Vorher `>=75 or <=45` → öffnet bei Overbought UND bei schwachem
            # Downtrend gleichzeitig. Korrekt: SHORT nur wenn RSI im bearish-Bereich.
            if not (last_row['rsi_9'] <= 45): return False
            if not (last_row['rsi_14'] <= 45): return False
            if not (-40 <= last_row['tsi_fast_12_7_7'] <= -5): return False
            if not (last_row['tsi_fast_12_7_7_signal'] < -5): return False
            if not (last_row['tsi_fast_12_7_7'] < last_row['tsi_fast_12_7_7_signal']): return False
            if not (last_row['ema_9'] < last_row['ema_21']): return False
            if not (last_row['ema_12'] < last_row['ema_26']): return False
            if not (last_row['close'] < last_row['ema_55']): return False
            if not (last_row['close'] < last_row['ema_89']): return False
            if not (last_row['close'] < last_row['ema_200']): return False
            if not (last_row['wma_9'] < last_row['ema_21']): return False
            if not (last_row['wma_12'] < last_row['ema_26']): return False
            if not (last_row['ema_12'] < last_row['ema_55']): return False
            if not (last_row['ema_21'] < last_row['ema_89']): return False
            if not (last_row['ema_21'] < last_row['ema_200']): return False
            if not (last_row['ema_9'] < last_row['wma_21']): return False
            if not (last_row['ema_12'] < last_row['wma_26']): return False
            if not (last_row['close'] < last_row['kama_9']): return False
            if not (last_row['close'] < last_row['kama_12']): return False
            if not (last_row['close'] < last_row['kama_21']): return False
            if not (last_row['macd_dif_fast_9_21_9'] < last_row['macd_dea_fast_9_21_9']): return False
            if not (last_row['close'] < last_row['donchian_mid_4']): return False
            if not (last_row['close'] < last_row['boll_mid_20']): return False
            if not (last_row['close'] > last_row['support_price'] * 0.95): return False
            if not (last_row['close'] <= last_row['resistance_price'] * 0.999): return False
            return True

    except Exception as e:
        logger.error(f"Error for Bedingungsprüfung (5% Bot): {e}")
        return False

    return False


def analyze_coin(conn, symbol, df_indicators, live_price):

    for direction in ['LONG', 'SHORT']:
        if not evaluate_conditions(df_indicators, direction): continue

        # Cooldown-Parameter — leicht asymmetrisch weil die Strategy
        # bullish-biased ist und LONG-Trades erfahrungsgemäß häufiger wegen
        # Marktdrift triggern. SHORT-Seite wird etwas lockerer behandelt.
        # Werte verdoppelt von 200/250 auf 400/500 — die alte Schwelle hat
        # bei einseitigen Märkten legitime Trend-Fortsetzungen blockiert.
        hours = 4 if direction == 'LONG' else 3
        count = 400 if direction == 'LONG' else 500
        if check_recent_trades(conn, direction, hours=hours, count=count): continue
        if is_trade_already_active(conn, symbol, direction, '5 Percent'): continue

        atr_14 = float(df_indicators['atr_14'].iloc[0])
        lev = get_max_leverage(symbol, 20)

        if direction == 'LONG':
            target1 = live_price * 1.0125
            target2 = live_price * 1.025
            target3 = live_price * 1.0375
            target4 = live_price * 1.05
            sl_calc = live_price - (3.5 * atr_14)
            sl = live_price * 0.95 if ((live_price - 3.5 * atr_14) / live_price) - 1 <= -0.05 else sl_calc
            if live_price <= sl or live_price >= target1: continue

        else:  # SHORT
            target1 = live_price * (1 - 0.0125)
            target2 = live_price * (1 - 0.025)
            target3 = live_price * (1 - 0.0375)
            target4 = live_price * (1 - 0.05)
            sl_calc = live_price + (3.5 * atr_14)
            sl = live_price * 1.05 if ((live_price + 3.5 * atr_14) / live_price) - 1 >= 0.05 else sl_calc
            if live_price >= sl or live_price <= target1: continue

        margin = 'Cross'

        return {
            "strategy": "5 Percent",
            "coin": symbol,
            "direction": direction,
            "margin": margin,
            "entry": live_price,
            "lev": lev,
            "target1": target1,
            "target2": target2,
            "target3": target3,
            "target4": target4,
            "sl": sl
        }

    return None
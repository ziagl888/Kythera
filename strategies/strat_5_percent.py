from core.market_utils import get_max_leverage, is_trade_already_active
# strategies/strat_5_percent.py
import logging
import datetime
import os
import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

logger = logging.getLogger(__name__)

# FIX P2.43: ema_200/wma_21/wma_26 added — the conditions below use them,
# but the column check didn't cover them → if they were missing from the df, a
# KeyError was thrown that was silently swallowed as "no signal".
REQUIRED_COLUMNS = ['rsi_9', 'rsi_14', 'tsi_fast_12_7_7', 'tsi_fast_12_7_7_signal', 'ema_9', 'ema_12', 'ema_21',
                    'ema_26', 'ema_55', 'ema_89', 'ema_200', 'wma_9', 'wma_12', 'wma_21', 'wma_26', 'close',
                    'kama_9', 'kama_12', 'kama_21',
                    'macd_dif_fast_9_21_9', 'macd_dea_fast_9_21_9', 'donchian_mid_4', 'boll_mid_20', 'atr_14',
                    'support_price', 'resistance_price']



def check_recent_trades(conn, direction, hours=3, count=500):
    """
    Checks the direction cooldown.

    See documentation in strat_fast_in_out.py:check_recent_trades for details.
    Now counts ALL wins (not just TP1) and has threshold 500 instead of 250.
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

    # df_indicators comes DESC sorted from the detector (iloc[0] = NEWEST candle!)
    last_row = data.iloc[0]

    # T-2026-CU-9050-084 (P1.12): support_price/resistance_price are window-global
    # and are now written only to the newest CLOSED bar (NaN on the forming bar and
    # every older bar). last_row is the forming bar — read the S/R level from the
    # newest bar that still carries it (first non-null in this DESC frame) so the
    # headroom guards keep working. Value is unchanged whenever the forming bar is
    # present. The per-bar indicator checks below stay on last_row.
    sr_idx = data['support_price'].first_valid_index()
    sr_row = data.loc[sr_idx] if sr_idx is not None else last_row

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
            if not (last_row['close'] < sr_row['resistance_price'] * 0.95): return False
            if not (last_row['close'] >= sr_row['support_price'] * 0.999): return False
            return True

        elif direction == 'SHORT':
            # FIX: previously `>=75 or <=45` → opens on overbought AND on weak
            # downtrend at the same time. Correct: SHORT only when RSI is in the bearish range.
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
            # FIX P2.43: was `ema_12 < ema_55` — typo, the LONG mirror (line 56)
            # checks `ema_21 > ema_55`.
            if not (last_row['ema_21'] < last_row['ema_55']): return False
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
            # FIX P1.14: headroom guard had the sign flipped — `close >
            # support*0.95` is quasi always true (no-op). SHORT only when there is
            # still ≥5% headroom down to support (mirror of the LONG check, line 67).
            if not (last_row['close'] > sr_row['support_price'] * 1.05): return False
            if not (last_row['close'] <= sr_row['resistance_price'] * 0.999): return False
            return True

    except Exception as e:
        logger.error(f"Error in condition check (5% bot): {e}")
        return False

    return False


def analyze_coin(conn, symbol, df_indicators, live_price, cycle=None):

    for direction in ['LONG', 'SHORT']:
        if not evaluate_conditions(df_indicators, direction): continue

        # Cooldown parameters — slightly asymmetric because the strategy
        # is bullish-biased and LONG trades trigger more often from experience due to
        # market drift. The SHORT side is treated somewhat more loosely.
        # Values doubled from 200/250 to 400/500 — the old threshold
        # blocked legitimate trend continuations in one-sided markets.
        hours = 4 if direction == 'LONG' else 3
        count = 400 if direction == 'LONG' else 500
        # T-2026-CU-9050-172 (4a): check_recent_trades is coin-independent — with
        # DetectorCycle the same query code path runs once per cycle and
        # (direction, hours, count) is memoised; unchanged per call without a
        # cycle. Guard stays read-only + AND-combined (P2.44 argument).
        if cycle is not None:
            recent_blocked = cycle.memo(
                ('recent_trades', direction, hours, count),
                lambda: check_recent_trades(conn, direction, hours=hours, count=count),
            )
        else:
            recent_blocked = check_recent_trades(conn, direction, hours=hours, count=count)
        if recent_blocked: continue
        if cycle is not None:
            trade_active = cycle.is_trade_active(symbol, direction, '5 Percent')
        else:
            trade_active = is_trade_already_active(conn, symbol, direction, '5 Percent')
        if trade_active: continue

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

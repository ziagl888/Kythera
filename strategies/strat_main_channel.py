from core.market_utils import calculate_obv, find_support_resistance_zones, get_max_leverage, is_trade_already_active
# strategies/strat_main_channel.py
import logging
import pandas as pd
import os
import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

logger = logging.getLogger(__name__)



def analyze_coin(conn, symbol, df_indicators, live_price):
    """Kernlogik für den Main Channel Bot"""
    if len(df_indicators) < 50: return None  # Braucht genug Historie für Divergenz

    # Note: df_indicators is sorted DESC (index 0 is the most recent candle!)
    current_row = df_indicators.iloc[0]

    # T-2026-CU-9050-084 (P1.12): support_price/resistance_price are window-global
    # and are now written only to the newest CLOSED bar (NaN on the forming bar and
    # every older bar). Read the level from the newest bar that still carries it
    # (first non-null in this DESC frame) instead of the fixed iloc[1]. With the
    # forming bar present — the normal case — that IS iloc[1] (the newest closed
    # bar), so the level is unchanged; if the forming bar is missing it stays on the
    # newest closed bar instead of silently reading a NULLed row.
    sr_idx = df_indicators['support_price'].first_valid_index()
    sr_row = df_indicators.loc[sr_idx] if sr_idx is not None else None

    support_price_prev = float(sr_row['support_price']) if sr_row is not None and pd.notna(sr_row['support_price']) else 0
    resistance_price_prev = float(sr_row['resistance_price']) if sr_row is not None and pd.notna(sr_row['resistance_price']) else 0

    close_price_current = current_row['close']
    support_price_hit = resistance_price_hit = 0

    if support_price_prev <= close_price_current <= support_price_prev * 1.0075:
        support_price_hit = support_price_prev
    elif resistance_price_prev * 0.9925 <= close_price_current <= resistance_price_prev:
        resistance_price_hit = resistance_price_prev

    if support_price_hit == 0 and resistance_price_hit == 0: return None

    open_time_hit = current_row.name
    RSI_9_HIT, RSI_14_HIT = current_row['rsi_9'], current_row['rsi_14']

    first_hit_row = None
    # Searching rückwärts (ab der 5. neuesten Kerze) after dem ersten Hit
    for i in range(5, len(df_indicators)):
        row = df_indicators.iloc[i]
        c_price = row['close']
        if support_price_hit <= c_price <= support_price_hit * 1.0075 or resistance_price_hit * 0.9925 <= c_price <= resistance_price_hit:
            first_hit_row = row
            break

    if first_hit_row is None: return None

    open_time_1st_hit = first_hit_row.name
    RSI_9_1ST_HIT, RSI_14_1ST_HIT = first_hit_row['rsi_9'], first_hit_row['rsi_14']

    # We now need the raw OHLCV data for exact zones and OBV
    df_ohlcv = pd.read_sql_query(f"""
        SELECT open_time, open, high, low, close, volume FROM "{symbol}_1h"
        WHERE open_time <= %s ORDER BY open_time DESC LIMIT 480
    """, conn, params=(open_time_hit,))

    if len(df_ohlcv) < 200: return None
    df_ohlcv = df_ohlcv.sort_values(by='open_time', ascending=True)
    support_zones, resistance_zones = find_support_resistance_zones(df_ohlcv)

    entry = live_price
    lev = get_max_leverage(symbol, 20)

    # ========================== LONG LOGIK ==========================
    if support_price_hit > 0:
        if is_trade_already_active(conn, symbol, 'LONG', 'Main Channel'): return None
        if RSI_9_HIT > RSI_9_1ST_HIT and RSI_14_HIT > RSI_14_1ST_HIT:
            if calculate_obv(conn, symbol, open_time_1st_hit, open_time_hit) > 0:

                targets = sorted([zone[0] for zone in resistance_zones], key=lambda x: abs(x - entry))[:4]
                while len(targets) < 4: targets.append(0.0)
                t1, t2, t3, t4 = targets

                # FIX P0.7: 0 gefundene Zonen → t1==0 lief ungeguarded in die
                # Interpolation und erzeugte LONG-TPs UNTER dem Entry (TP1 =
                # 0.75·Entry). Ohne echte Zonen gibt es kein valides Signal.
                if t1 == 0:
                    return None

                # Ziel-Interpolation (Aus deinem Script 1)
                if t2 == 0:
                    x = (t1 - entry) / 4;
                    t4 = t1;
                    t1 = entry + x;
                    t2 = entry + (2 * x);
                    t3 = entry + (3 * x)
                elif t3 == 0:
                    x = (t1 - entry) / 2;
                    t4 = t2;
                    t2 = t1;
                    t1 = entry + x
                    y = (t4 - t2) / 2;
                    t3 = t2 + y

                # Dynamischer SL: ATR-basiert (3× ATR unter Entry) mit Safety-Cap
                # capped at 5% and min 1% from entry. Old fixed 2.5% SL ignored
                # volatility differences between BTC and meme coins entirely.
                atr_14 = current_row.get('atr_14', 0) or 0
                atr_14 = float(atr_14)
                if atr_14 > 0:
                    atr_sl = entry - (3.0 * atr_14)
                    # Cap between 1% and 5% distance from entry
                    atr_sl = min(atr_sl, entry * 0.99)   # mindestens 1% Abstand
                    atr_sl = max(atr_sl, entry * 0.95)   # maximal 5% Abstand
                    sl = float(atr_sl)
                else:
                    # Fallback when no ATR available
                    sl = float(entry * 0.975)

                return {
                    "strategy": "Main Channel", "coin": symbol, "direction": "LONG",
                    "entry": entry, "lev": lev, "target1": t1, "target2": t2, "target3": t3, "target4": t4, "sl": sl
                }

    # ========================== SHORT LOGIK ==========================
    elif resistance_price_hit > 0:
        if is_trade_already_active(conn, symbol, 'SHORT', 'Main Channel'): return None
        if RSI_9_HIT < RSI_9_1ST_HIT and RSI_14_HIT < RSI_14_1ST_HIT:
            if calculate_obv(conn, symbol, open_time_1st_hit, open_time_hit) < 0:

                targets = sorted([zone[0] for zone in support_zones], key=lambda x: abs(x - entry))[:4]
                while len(targets) < 4: targets.append(0.0)
                t1, t2, t3, t4 = targets

                # FIX P0.7: siehe LONG-Pfad — ohne Zonen kein Signal (SHORT-
                # Interpolation hätte TPs bei -25/-50/-75% erzeugt).
                if t1 == 0:
                    return None

                # Ziel-Interpolation (Aus deinem Script 1)
                if t2 == 0:
                    x = (entry - t1) / 4;
                    t4 = t1;
                    t1 = entry - x;
                    t2 = entry - (2 * x);
                    t3 = entry - (3 * x)
                elif t3 == 0:
                    x = (entry - t1) / 2;
                    t4 = t2;
                    t2 = t1;
                    t1 = entry - x
                    y = (t2 - t4) / 2;
                    t3 = t2 - y

                # Dynamischer SL: ATR-basiert (3× ATR über Entry) mit Safety-Cap
                atr_14 = current_row.get('atr_14', 0) or 0
                atr_14 = float(atr_14)
                if atr_14 > 0:
                    atr_sl = entry + (3.0 * atr_14)
                    atr_sl = max(atr_sl, entry * 1.01)   # mindestens 1% Abstand
                    atr_sl = min(atr_sl, entry * 1.05)   # maximal 5% Abstand
                    sl = float(atr_sl)
                else:
                    sl = float(entry * 1.025)

                margin = 'Cross'

                return {
                    "strategy": "Main Channel", "coin": symbol, "direction": "SHORT", "margin": margin,
                    "entry": entry, "lev": lev, "target1": t1, "target2": t2, "target3": t3, "target4": t4, "sl": sl
                }

    return None

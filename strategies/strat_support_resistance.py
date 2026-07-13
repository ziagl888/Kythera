from core.candles import read_candles
from core.market_utils import calculate_obv, find_support_resistance_zones, get_max_leverage, is_trade_already_active
# strategies/strat_support_resistance.py
import logging
import pandas as pd
import warnings
import os
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

logger = logging.getLogger(__name__)


def analyze_coin(conn, symbol, df_indicators, live_price):
    if len(df_indicators) < 50: return None
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

    if support_price_prev <= close_price_current <= support_price_prev * 1.0075: support_price_hit = support_price_prev
    elif resistance_price_prev * 0.9925 <= close_price_current <= resistance_price_prev: resistance_price_hit = resistance_price_prev
    if support_price_hit == 0 and resistance_price_hit == 0: return None

    open_time_hit = current_row.name
    RSI_9_HIT, RSI_14_HIT = current_row['rsi_9'], current_row['rsi_14']

    first_hit_row = None
    for i in range(5, len(df_indicators)):
        row = df_indicators.iloc[i]
        c_price = row['close']
        if support_price_hit <= c_price <= support_price_hit * 1.0075 or resistance_price_hit * 0.9925 <= c_price <= resistance_price_hit:
            first_hit_row = row
            break
    if first_hit_row is None: return None

    open_time_1st_hit = first_hit_row.name
    RSI_9_1ST_HIT, RSI_14_1ST_HIT = first_hit_row['rsi_9'], first_hit_row['rsi_14']

    # R1 (T-2026-CU-9050-108): via core.candles — newest 480 CLOSED 1h candles up to
    # the hit bar (`end=`), already ASC (no more sort_values).
    df_ohlcv = read_candles(
        conn, symbol, "1h", limit=480, end=open_time_hit, include_forming=False,
        columns=("open_time", "open", "high", "low", "close", "volume"),
    )
    if len(df_ohlcv) < 200: return None
    support_zones, resistance_zones = find_support_resistance_zones(df_ohlcv)

    entry = live_price
    lev = get_max_leverage(symbol, 20)
    margin = 'Cross'

    if support_price_hit > 0:
        if is_trade_already_active(conn, symbol, 'LONG', 'Support Resistance'): return None
        if RSI_9_HIT > RSI_9_1ST_HIT and RSI_14_HIT > RSI_14_1ST_HIT:
            if calculate_obv(conn, symbol, open_time_1st_hit, open_time_hit) > 0:
                targets = sorted([zone[0] for zone in resistance_zones], key=lambda x: abs(x - entry))[:4]
                while len(targets) < 4: targets.append(0.0)
                t1, t2, t3, t4 = targets
                # FIX P0.7: 0 Zonen → t1==0 erzeugte LONG-TPs unter dem Entry
                if t1 == 0: return None
                if t2 == 0: x=(t1-entry)/4; t4=t1; t1=entry+x; t2=entry+(2*x); t3=entry+(3*x)
                elif t3 == 0: x=(t1-entry)/2; t4=t2; t2=t1; t1=entry+x; y=(t4-t2)/2; t3=t2+y
                sl = float(entry * 0.975)
                return {"strategy": "Support Resistance", "coin": symbol, "direction": "LONG", "margin": margin, "entry": entry, "lev": lev, "target1": t1, "target2": t2, "target3": t3, "target4": t4, "sl": sl}

    elif resistance_price_hit > 0:
        if is_trade_already_active(conn, symbol, 'SHORT', 'Support Resistance'): return None
        if RSI_9_HIT < RSI_9_1ST_HIT and RSI_14_HIT < RSI_14_1ST_HIT:
            if calculate_obv(conn, symbol, open_time_1st_hit, open_time_hit) < 0:
                targets = sorted([zone[0] for zone in support_zones], key=lambda x: abs(x - entry))[:4]
                while len(targets) < 4: targets.append(0.0)
                t1, t2, t3, t4 = targets
                # FIX P0.7: 0 Zonen → t1==0 erzeugte SHORT-TPs bei -25/-50/-75%
                if t1 == 0: return None
                if t2 == 0: x=(entry-t1)/4; t4=t1; t1=entry-x; t2=entry-(2*x); t3=entry-(3*x)
                elif t3 == 0: x=(entry-t1)/2; t4=t2; t2=t1; t1=entry-x; y=(t2-t4)/2; t3=t2-y
                sl = float(entry * 1.025)
                return {"strategy": "Support Resistance", "coin": symbol, "direction": "SHORT", "margin": margin, "entry": entry, "lev": lev, "target1": t1, "target2": t2, "target3": t3, "target4": t4, "sl": sl}
    return None

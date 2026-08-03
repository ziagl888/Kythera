from core.market_utils import get_max_leverage, is_trade_already_active
# strategies/strat_fast_in_out.py
import logging
import datetime
import os
import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

logger = logging.getLogger(__name__)

# The columns you need
REQUIRED_COLUMNS = ['rsi_9', 'rsi_14', 'tsi_fast_12_7_7', 'tsi_fast_12_7_7_signal', 'ema_9', 'ema_12', 'ema_21',
                    'ema_26', 'ema_55', 'ema_89', 'wma_9', 'wma_12', 'close', 'kama_9', 'kama_12', 'kama_21',
                    'macd_dif_fast_9_21_9', 'macd_dea_fast_9_21_9', 'donchian_mid_4', 'boll_mid_20', 'atr_14',
                    'support_price', 'resistance_price']


def check_recent_trades(conn, direction, hours=3, count=500):
    """
    Checks the direction cooldown.

    The cooldown blocks further signals in direction X if in the last
    `hours` hours `count` trades in that direction have already closed as
    WIN. This is meant to limit over-exposure during one-sided market phases.

    Changes vs. the predecessor:
    1. Counts ALL wins (TP1-TP4), not just TP1.
       Before: WHERE status = '1'  → only TP1 hits
       Now:    WHERE status IN ('1','2','3','4')  → all successful trades
       Reason: status='1' was arbitrary — a TP2/3/4 hit is just as clear a
       "we played this well" signal as TP1.

    2. Count threshold doubled from 250 to 500.
       Reason: with 570 coins × 6 classic bots, 250 wins in 3h is normal
       everyday behaviour during a one-sided market. The old threshold
       blocked legitimate trend continuations (see log: 27 SHORT blocks in a
       row because the market was bearish).

    If you want the cooldown more aggressive again: count=300 in the call below.
    If you want to disable it: count=999999.
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
            if not (last_row['ema_9'] > last_row['ema_21']): return False
            if not (last_row['close'] < sr_row['resistance_price'] * 0.95): return False
            return True

        elif direction == 'SHORT':
            # FIX: previously the condition was `rsi_9 >= 75 OR rsi_9 <= 45` → that opens
            # SHORT signals both on overbought (75+) and on a weak
            # downtrend (45-) at the same time. At 75+ you'd be shorting exactly
            # where LONG setups run. Correct: only in the "weak/bearish" RSI range.
            if not (last_row['rsi_9'] <= 45): return False
            if not (last_row['ema_9'] < last_row['ema_21']): return False
            # FIX P1.14: headroom guard had the sign flipped — `close >
            # support*0.95` is practically always true (no-op). What's meant is: SHORT
            # only if there's still ≥5% headroom to support (mirror of the LONG check
            # `close < resistance*0.95`).
            if not (last_row['close'] > sr_row['support_price'] * 1.05): return False
            return True

    except Exception as e:
        logger.error(f"Error checking conditions: {e}")
        return False

    return False


def analyze_coin(conn, symbol, df_indicators, live_price, cycle=None):
    """
    This is the main function called by the detector.
    It returns a finished signal dictionary or None.
    """


    for direction in ['LONG', 'SHORT']:
        # 1. Check indicators
        if not evaluate_conditions(df_indicators, direction):
            continue

        # 2. Check cooldown
        # T-2026-CU-9050-172 (4a): check_recent_trades is coin-independent — with
        # DetectorCycle the same query code path runs once per cycle and
        # (direction, hours, count) and is memoised; unchanged per call without
        # a cycle. Guard stays read-only + AND-combined (P2.44 argument).
        if cycle is not None:
            recent_blocked = cycle.memo(
                ('recent_trades', direction, 3, 500),  # defaults from check_recent_trades
                lambda: check_recent_trades(conn, direction),
            )
        else:
            recent_blocked = check_recent_trades(conn, direction)
        if recent_blocked:
            logger.info(f"[{symbol}] Too many {direction} trades. Cooldown active.")
            continue

        # 3. Is the trade already active? (cycle snapshot or single query)
        if cycle is not None:
            trade_active = cycle.is_trade_active(symbol, direction, 'Fast In And Out')
        else:
            trade_active = is_trade_already_active(conn, symbol, direction, 'Fast In And Out')
        if trade_active:
            logger.info(f"[{symbol}] {direction} trade already running.")
            continue

        # 4. Compute TP / SL (as in your script 3 & 4)
        # FIX: previously iloc[-1] → that was the OLDEST candle (df is DESC sorted from
        # the detector, iloc[0] = newest). The SL calculation therefore used ATR
        # from 10 days ago, which led to completely wrong SLs on volatile coins.
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

        # 5. Live price check (have we already run into target or SL?)
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

    return None  # No signal found

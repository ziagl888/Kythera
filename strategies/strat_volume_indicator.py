from core.market_utils import check_cooldown, get_max_leverage, is_trade_already_active
# strategies/strat_volume_indicator.py
import logging
import pandas as pd
import datetime
import os
import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

logger = logging.getLogger(__name__)



def detect_high_volume_zone(conn, symbol, latest_close, latest_open_time, threshold_factor=3):
    try:
        start_time = latest_open_time - datetime.timedelta(days=90)
        df_hist = pd.read_sql_query(
            f'SELECT open_time, close, volume FROM "{symbol}_30m" WHERE open_time >= %s AND open_time < %s ORDER BY open_time ASC',
            conn, params=(start_time, latest_open_time))
        if df_hist.empty: return False

        volume_mean, volume_std = df_hist['volume'].mean(), df_hist['volume'].std()
        high_volume_threshold = volume_mean + threshold_factor * volume_std

        price_volume = df_hist.groupby('close')['volume'].sum().reset_index()
        hvn_prices = price_volume[price_volume['volume'] > high_volume_threshold]['close'].values

        proximity_threshold = latest_close * 0.01
        for hvn_price in hvn_prices:
            if abs(latest_close - hvn_price) <= proximity_threshold: return True
        return False
    except Exception:
        return False


def detect_volume_spike_in_period(conn, symbol, open_time_1st_hit, open_time_hit):
    try:
        df_period = pd.read_sql_query(
            f'SELECT open_time, close, volume FROM "{symbol}_30m" WHERE open_time >= %s AND open_time <= %s ORDER BY open_time ASC',
            conn, params=(open_time_1st_hit, open_time_hit))
        if df_period.empty: return 0

        hist_start = open_time_1st_hit - datetime.timedelta(days=10)
        df_hist = pd.read_sql_query(
            f'SELECT open_time, close, volume FROM "{symbol}_30m" WHERE open_time < %s AND open_time >= %s', conn,
            params=(open_time_1st_hit, hist_start))
        if df_hist.empty: return 0

        volume_mean, volume_std = df_hist['volume'].mean(), df_hist['volume'].std()
        spike_threshold = volume_mean + 3 * volume_std

        # FIX (#12): Previously used df_period.loc[index - 1, 'close'] —
        # this depends on the DataFrame index. With a non-reset or
        # non-sequential index (e.g. after filtering) this raises KeyError.
        # Jetzt: index explizit resetten + mit iloc after Position arbeiten.
        df_period = df_period.reset_index(drop=True)
        for i in range(len(df_period)):
            if df_period.iloc[i]['volume'] > spike_threshold:
                if i > 0 and df_period.iloc[i]['close'] > df_period.iloc[i - 1]['close']:
                    return 1  # Buy Spike
                else:
                    return -1  # Sell Spike
        return 0
    except Exception:
        return 0


def analyze_coin(conn, symbol, df_indicators, live_price):
    # This bot only needs the 30m indicator table for timestamps/prices
    if df_indicators.empty: return None

    current_row = df_indicators.iloc[0]
    latest_open_time = current_row.name  # da index_col='open_time'
    close_price = current_row['close']

    # 1. HVN Check
    if not detect_high_volume_zone(conn, symbol, close_price, latest_open_time):
        return None

    # 2. Spike Check (Letzte 5 Tage)
    five_days_ago = latest_open_time - datetime.timedelta(days=5)
    volume_spike = detect_volume_spike_in_period(conn, symbol, five_days_ago, latest_open_time)

    entry = live_price
    lev = get_max_leverage(symbol, 20)
    margin = 'Cross'

    # FIX P1.16: Ohne Cooldown refeuerte ein bis zu 5 Tage alter Volume-Spike
    # alle 30 min dasselbe Signal (Serien-Reentry). Jetzt 12h-Sperre pro
    # (Coin, Direction) über das zentrale trade_cooldowns-System.
    #
    # FIX T-2026-CU-9050-024: trade_cooldowns.module is varchar(10) — the
    # original tag 'Volume Indicator' (16 chars) made every update_cooldown
    # throw StringDataRightTruncation BEFORE the signal dict was returned,
    # silencing this bot entirely from 2026-07-04 to 2026-07-09. The tag must
    # stay <= 10 chars ('VolIndic' matches the core/bot_naming display alias).
    # No cooldown-row migration needed: no write with the long tag ever
    # succeeded.
    module_tag = 'VolIndic'
    cd_hours = 12

    if volume_spike == 1:  # LONG
        if is_trade_already_active(conn, symbol, 'LONG', 'Volume Indicator'): return None
        # check_cooldown returned True wenn die Sperre NOCH aktiv ist → skip.
        if check_cooldown(conn, module_tag, symbol, 'LONG', cd_hours): return None
        sl = float(entry * 0.95)
        t1, t2, t3, t4 = float(entry * 1.025), float(entry * 1.050), float(entry * 1.075), float(entry * 1.10)
        # No update_cooldown here: the cooldown is requested via
        # 'cooldown_module' and written by write_signal_atomic (3_detectors)
        # in the SAME transaction as active_trades + outbox. Writing it here —
        # even with commit=False — was not atomic: another strategy's signal
        # on the same coin/cycle commits first and would persist the pending
        # cooldown although THIS signal was never written.
        return {"strategy": "Volume Indicator", "coin": symbol, "direction": "LONG", "margin": margin, "entry": entry, "lev": lev,
                "target1": t1, "target2": t2, "target3": t3, "target4": t4, "sl": sl,
                "cooldown_module": module_tag}

    elif volume_spike == -1:  # SHORT
        if is_trade_already_active(conn, symbol, 'SHORT', 'Volume Indicator'): return None
        if check_cooldown(conn, module_tag, symbol, 'SHORT', cd_hours): return None
        sl = float(entry * 1.05)
        t1, t2, t3, t4 = float(entry * 0.975), float(entry * 0.95), float(entry * 0.925), float(entry * 0.9)
        # Cooldown via 'cooldown_module' — see LONG branch above.
        return {"strategy": "Volume Indicator", "coin": symbol, "direction": "SHORT", "margin": margin, "entry": entry, "lev": lev,
                "target1": t1, "target2": t2, "target3": t3, "target4": t4, "sl": sl,
                "cooldown_module": module_tag}

    return None

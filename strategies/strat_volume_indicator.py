from core.market_utils import check_cooldown, get_max_leverage, is_trade_already_active
# strategies/strat_volume_indicator.py
import logging
import pandas as pd
import datetime
import os
import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

logger = logging.getLogger(__name__)

# HVN (high-volume-node) tuning. Both are relative to price so the gate behaves
# identically across tick sizes (P2.42c).
HVN_BIN_PCT = 0.001       # price-level bin width = 0.1% of the latest close
HVN_PROXIMITY_PCT = 0.01  # "near a node" = within 1% of the latest close


def _is_near_high_volume_node(df_hist, latest_close, threshold_factor=3):
    """True if ``latest_close`` sits within ``HVN_PROXIMITY_PCT`` of a
    high-volume price node in ``df_hist`` (columns: close, volume).

    P2.42(c): the old code grouped volume by the RAW float close. On
    fine-tick-size coins almost every candle has a unique close, so each group
    held ~one candle and could never exceed a per-candle mean+kσ volume
    threshold — the HVN gate silently never fired for high-precision symbols.
    Prices are now binned into relative levels (0.1% of price) before the volume
    is summed, so a level accumulates the volume of every candle that traded
    there, independent of tick size.
    """
    if df_hist.empty or latest_close <= 0:
        return False

    volume_mean, volume_std = df_hist['volume'].mean(), df_hist['volume'].std()
    high_volume_threshold = volume_mean + threshold_factor * volume_std

    bin_size = latest_close * HVN_BIN_PCT
    if bin_size <= 0:
        return False
    price_bins = (df_hist['close'] / bin_size).round() * bin_size
    binned_volume = df_hist.groupby(price_bins)['volume'].sum()
    hvn_prices = binned_volume[binned_volume > high_volume_threshold].index.values

    proximity_threshold = latest_close * HVN_PROXIMITY_PCT
    for hvn_price in hvn_prices:
        if abs(latest_close - hvn_price) <= proximity_threshold:
            return True
    return False


def _classify_latest_volume_spike(df_period, spike_threshold):
    """Classify the MOST RECENT volume spike in ``df_period`` (columns: close,
    volume) as buy (+1), sell (-1) or none (0).

    P2.42(a): iterate newest→oldest so the *latest* spike decides the signal.
    The old forward loop returned on the OLDEST spike in the window — a 5-day-old
    spike outvoted a fresh one.

    P2.42(b): a spike on the very first in-period candle (i==0) has no in-period
    predecessor to compare its close against. It is now discarded instead of
    being silently classified as a sell (the old ``else`` branch defaulted i==0
    to -1). If the only spike is at i==0 the result is 0 (no signal).
    """
    # Index reset so positional iloc lookups are independent of the source index
    # (a filtered/non-sequential index otherwise breaks the i-1 predecessor read).
    df_period = df_period.reset_index(drop=True)
    for i in range(len(df_period) - 1, -1, -1):
        if df_period.iloc[i]['volume'] > spike_threshold:
            if i == 0:
                continue  # no in-period predecessor → cannot classify direction
            if df_period.iloc[i]['close'] > df_period.iloc[i - 1]['close']:
                return 1  # Buy Spike
            return -1  # Sell Spike
    return 0


def detect_high_volume_zone(conn, symbol, latest_close, latest_open_time, threshold_factor=3):
    try:
        start_time = latest_open_time - datetime.timedelta(days=90)
        df_hist = pd.read_sql_query(
            f'SELECT open_time, close, volume FROM "{symbol}_30m" WHERE open_time >= %s AND open_time < %s ORDER BY open_time ASC',
            conn, params=(start_time, latest_open_time))
        if df_hist.empty: return False
        return _is_near_high_volume_node(df_hist, latest_close, threshold_factor)
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
        return _classify_latest_volume_spike(df_period, spike_threshold)
    except Exception:
        return 0


def analyze_coin(conn, symbol, df_indicators, live_price):
    # This bot only needs the 30m indicator table for timestamps/prices
    if df_indicators.empty: return None

    current_row = df_indicators.iloc[0]
    latest_open_time = current_row.name  # da index_col='open_time'
    close_price = current_row['close']

    # P2.44: evaluate the cheap, side-effect-free guards BEFORE the expensive
    # 90d×30m HVN read. All four gates (spike, active-trade, cooldown, HVN) are
    # read-only and AND-combined, so the set of emitted signals is invariant to
    # their evaluation order — only *when* each runs changes. The 90d HVN read
    # (detect_high_volume_zone) is by far the heaviest query and used to run
    # first for every one of ~530 coins each cycle; it now runs only when a
    # signal is otherwise emittable (spike present, not already active, not on
    # cooldown). This does NOT alter the P1.16 cooldown contract below.

    # 1. Spike Check (last 5 days) — ~15d of 30m rows, far cheaper than the 90d HVN read.
    five_days_ago = latest_open_time - datetime.timedelta(days=5)
    volume_spike = detect_volume_spike_in_period(conn, symbol, five_days_ago, latest_open_time)
    if volume_spike == 0:
        return None
    direction = 'LONG' if volume_spike == 1 else 'SHORT'

    # 2. Cheap per-direction guards (both read-only DB lookups).
    if is_trade_already_active(conn, symbol, direction, 'Volume Indicator'):
        return None

    # FIX P1.16 (DO NOT TOUCH the cooldown contract — T-2026-CU-9050-085 only
    # reordered this read, it did not change the 12h duration, the 'VolIndic'
    # tag, or the write path): without the cooldown a volume spike up to 5 days
    # old refired the same signal every 30 min (serial re-entry). The 12h lock
    # per (Coin, Direction) goes through the central trade_cooldowns system.
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
    # check_cooldown returns True while the lock is STILL active → skip.
    if check_cooldown(conn, module_tag, symbol, direction, cd_hours):
        return None

    # 3. Expensive HVN Check (90d×30m read) — reached only when a signal is emittable.
    if not detect_high_volume_zone(conn, symbol, close_price, latest_open_time):
        return None

    entry = live_price
    lev = get_max_leverage(symbol, 20)
    margin = 'Cross'

    if direction == 'LONG':
        sl = float(entry * 0.95)
        t1, t2, t3, t4 = float(entry * 1.025), float(entry * 1.050), float(entry * 1.075), float(entry * 1.10)
    else:  # SHORT
        sl = float(entry * 1.05)
        t1, t2, t3, t4 = float(entry * 0.975), float(entry * 0.95), float(entry * 0.925), float(entry * 0.9)

    # No update_cooldown here: the cooldown is requested via 'cooldown_module'
    # and written by write_signal_atomic (3_detectors) in the SAME transaction
    # as active_trades + outbox. Writing it here — even with commit=False — was
    # not atomic: another strategy's signal on the same coin/cycle commits first
    # and would persist the pending cooldown although THIS signal was never written.
    return {"strategy": "Volume Indicator", "coin": symbol, "direction": direction, "margin": margin, "entry": entry, "lev": lev,
            "target1": t1, "target2": t2, "target3": t3, "target4": t4, "sl": sl,
            "cooldown_module": module_tag}

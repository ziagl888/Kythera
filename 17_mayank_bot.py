import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import logging
import os
import time

import matplotlib

matplotlib.use('Agg')  # P3.8: headless VPS has no display — set before pyplot import
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

from core import config as _kcfg  # channel ids
from core.database import get_db_connection
from core.market_utils import calculate_pivots, check_cooldown, update_cooldown
from core.yfinance_fetch import download_with_retry

logging.basicConfig(level=logging.INFO, format='%(asctime)s - TRADFI_SMC_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
SMC_CHANNEL_ID = _kcfg.CH_MAYANK  # Your desired channel
TIMEFRAMES = ['1h', '4h']
# ASSETS = {
#    'XAUUSD=X': 'GOLD',
#    'USDJPY=X': 'USDJPY'
# }
ASSETS = {
    'GC=F': 'GOLD',  # Comex gold futures (most accurate gold chart on YFinance)
    'SI=F': 'SILVER',  # Comex Silver Futures (if you want silver too)
    'JPY=X': 'USDJPY',  # USD/JPY
    'EURUSD=X': 'EURUSD',  # EUR/USD (if you want to scan that too)
}
CHART_DIR = "generated_charts"
os.makedirs(CHART_DIR, exist_ok=True)

# P2.45(a): candle duration per TF for weekend/stale-candle gate. Mayank trades
# exclusively forex/metals (yfinance), which freeze on weekends — the
# last closed candle freezes and holds the FVG condition, while the
# 12h cooldown underneath runs out → refire on stale candle. Deliberately kept as
# local copy to 16_smc_forex_metals_bot (both bots are standalone
# scripts and already duplicate fetch/chart logic); a signal may only fire with
# fresh data (see is_stale_candle).
CANDLE_DURATION = {
    '1h': datetime.timedelta(hours=1),
    '4h': datetime.timedelta(hours=4),
}

# P2.45(c): minimal sanity for SL distance and risk-reward. Mayank posts
# SL = last-low*0.998 and TP = next pivot (fallback ±1/2%), without checking
# if the stop is close enough to entry (liquidation risk under leverage) or
# if the next take-profit even beats the risk. Two conservative
# gates in spirit of 15% SL-distance cap from calculate_smart_targets (P2.27).
MAX_SL_DIST = 0.15  # 15% — same cap as ROM1 path (P2.27)
MIN_RR = 0.5  # next TP must offer >= half risk as reward (sanity floor)


def is_stale_candle(candle_open_time, tf, now=None):
    """P2.45(a): True if the last closed candle is stale —
    at least two full candle durations have passed since its close, without
    a new one forming (weekend / stuck yfinance feed). Two-candle
    tolerance against single live lag; a weekend exceeds it by a
    multiple for 1h/4h."""
    dur = CANDLE_DURATION.get(tf)
    if dur is None:
        return False
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    ts = pd.Timestamp(candle_open_time)
    if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')
    else:
        ts = ts.tz_convert('UTC')
    close_time = ts.to_pydatetime() + dur
    return (now - close_time) >= 2 * dur


def passes_sl_rr_guard(entry, sl, tp1, direction):
    """P2.45(c): True if SL distance and risk-reward are plausible. The stop
    must be on the correct side and closer than MAX_SL_DIST to entry, the
    next take-profit must be on the correct side and worth at least MIN_RR times
    the risk. Catches degenerate geometries (SL >> TP, TP ≈ entry,
    reversed stop) without trimming normal pivot ladders."""
    entry = float(entry)
    sl = float(sl)
    tp1 = float(tp1)
    if entry <= 0:
        return False
    risk = (entry - sl) if direction == "LONG" else (sl - entry)
    reward = (tp1 - entry) if direction == "LONG" else (entry - tp1)
    if risk <= 0 or reward <= 0:
        return False
    if (risk / entry) > MAX_SL_DIST:
        return False
    if (reward / risk) < MIN_RR:
        return False
    return True


# 📊 DATA FETCHING (YFinance)
def fetch_yfinance_data(ticker, tf):
    """Fetches TradFi data and resamples it if needed."""
    try:
        yf_interval = '1h'
        period = '60d'
        resample_tf = None

        if tf == '1h':
            yf_interval = '1h'
        elif tf == '4h':
            yf_interval = '1h'
            resample_tf = '4h'

        # T-2026-KYT-9050-084: bounded retry — see 16_smc_forex_metals_bot.py. A
        # transient Yahoo failure used to cost this ticker/timeframe the whole
        # cycle silently; the helper retries and logs an explicit WARNING on
        # final failure.
        df = download_with_retry(ticker, yf_interval, period, tf=tf, logger=logger)
        if df.empty:
            return df

        # YFinance MultiIndex Header Fix
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if resample_tf:
            df = (
                df.resample(resample_tf)
                .agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'})
                .dropna()
            )

        df = df.reset_index()
        col_map = {
            'Datetime': 'open_time',
            'Date': 'open_time',
            'Open': 'open',
            'High': 'high',
            'Low': 'low',
            'Close': 'close',
            'Volume': 'volume',
        }
        df.rename(columns=col_map, inplace=True)

        if df['open_time'].dt.tz is not None:
            df['open_time'] = df['open_time'].dt.tz_convert('UTC').dt.tz_localize(None)

        for c in ['open', 'high', 'low', 'close']:
            df[c] = df[c].astype(float)

        # Drop the very last candle if it has not yet closed
        if not df.empty:
            df = df.iloc[:-1].reset_index(drop=True)

        return df
    except Exception as e:
        logger.error(f"YFinance Error for {ticker} ({tf}): {e}")
        return pd.DataFrame()


# 🧠 TRADINGVIEW PIVOT POINTS
# calculate_pivots now comes from core.market_utils (refactoring).
# Signature is identical: calculate_pivots(df, window=5) -> (supports, resistances)
# each with [(idx, price), ...]


def is_touching_pivot(price, pivots, max_idx, threshold=0.0005):
    """Checks if price touches a pivot (0.05% tolerance)."""
    for idx, p_val in pivots:
        if idx < max_idx:  # pivot must have formed before the current candle
            if abs(price - p_val) / p_val <= threshold:
                return True
    return False


# 🎯 STRATEGY & CHARTING
# 🎯 STRATEGY & CHARTING
def generate_setup_chart(df, symbol, tf, fvg, supports, resistances, direction):
    """Generates the chart of the last 7 days including pivot lines (which end on mitigation)."""
    try:
        # For 7 days: 168 candles at 1h, 42 at 4h
        lookback = 168 if tf == '1h' else 42
        PADDING_CANDLES = 12  # spacing on right

        start_plot_idx = max(0, len(df) - lookback)
        plot_df = df.iloc[start_plot_idx:].copy()

        # Remove timezone for mplfinance
        plot_df['open_time'] = pd.to_datetime(plot_df['open_time']).dt.tz_localize(None)
        plot_df.set_index('open_time', inplace=True)

        # Generate right-hand spacing (padding) and prevent NaN/object errors
        if len(plot_df) > 1:
            time_step = plot_df.index[-1] - plot_df.index[-2]
            future_dates = [plot_df.index[-1] + time_step * i for i in range(1, PADDING_CANDLES + 1)]
            empty_df = pd.DataFrame(index=future_dates, columns=plot_df.columns)
            plot_df = pd.concat([plot_df, empty_df])
            plot_df = plot_df.astype(float)  # 💥 IMPORTANT: prevents the chart crash!

        mc = mpf.make_marketcolors(up='#00ff88', down='#ff4466', edge='inherit', wick='inherit')
        # Hide the grid completely
        s = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds', gridstyle='')

        alines = []
        colors = []
        linewidths = []

        end_time = plot_df.index[-1]  # This is now the "future" on the right side

        # 1. Draw pivot lines (mitigation logic)
        for idx, val in supports:
            if idx >= start_plot_idx:
                pivot_time = pd.to_datetime(df['open_time'].iloc[idx]).tz_localize(None)
                line_end_time = end_time

                # Check if line was later broken/mitigated
                for i in range(idx + 1, len(df)):
                    if df['low'].iloc[i] <= val:
                        line_end_time = pd.to_datetime(df['open_time'].iloc[i]).tz_localize(None)
                        break

                alines.append([(pivot_time, float(val)), (line_end_time, float(val))])
                colors.append('#ffd700')  # gold for support
                linewidths.append(0.8)

        for idx, val in resistances:
            if idx >= start_plot_idx:
                pivot_time = pd.to_datetime(df['open_time'].iloc[idx]).tz_localize(None)
                line_end_time = end_time

                # Check if line was later broken/mitigated
                for i in range(idx + 1, len(df)):
                    if df['high'].iloc[i] >= val:
                        line_end_time = pd.to_datetime(df['open_time'].iloc[i]).tz_localize(None)
                        break

                alines.append([(pivot_time, float(val)), (line_end_time, float(val))])
                colors.append('#00ffff')  # cyan for resistance
                linewidths.append(0.8)

        # 2. Draw FVG box/lines
        fvg_color = '#00ff88' if direction == "LONG" else '#ff4466'
        fvg_start_time = pd.to_datetime(df['open_time'].iloc[fvg['index'] - 2]).tz_localize(None)

        # FVG Top
        alines.append([(fvg_start_time, float(fvg['top'])), (end_time, float(fvg['top']))])
        colors.append(fvg_color)
        linewidths.append(2.0)

        # FVG Bottom
        alines.append([(fvg_start_time, float(fvg['bottom'])), (end_time, float(fvg['bottom']))])
        colors.append(fvg_color)
        linewidths.append(2.0)

        filename = f"{CHART_DIR}/SMC_PIVOT_{symbol}_{tf}_{int(time.time())}.png"

        mpf.plot(
            plot_df,
            type='candle',
            style=s,
            # One line style for all lines, individual widths
            alines=dict(alines=alines, colors=colors, linewidths=linewidths, linestyle='--'),
            title=f"\nSMC Pivot Retest: {symbol} ({tf})",
            figsize=(14, 8),
            tight_layout=True,
            savefig=filename,
            returnfig=False,
        )
        return filename
    except Exception as e:
        logger.error(f"Chart Error for {symbol}: {e}")
        return None
    finally:
        # Close the figure left open by mpf.plot — prevents RAM leak.
        plt.close('all')


def analyze_strategy():
    logger.info("🔍 Analysing SMC Pivot Strategy...")

    for ticker, symbol_name in ASSETS.items():
        for tf in TIMEFRAMES:
            try:
                df = fetch_yfinance_data(ticker, tf)
                if df.empty or len(df) < 50:
                    continue

                supports, resistances = calculate_pivots(df, window=5)

                # We analyse the very last closed candle
                curr_idx = len(df) - 1
                curr_candle = df.iloc[curr_idx]
                curr_low = curr_candle['low']
                curr_high = curr_candle['high']
                curr_price = curr_candle['close']

                # P2.45(a): no re-signal on a frozen weekend candle
                # — otherwise 12h cooldown would refire same candle over weekend.
                if is_stale_candle(curr_candle['open_time'], tf):
                    continue

                # 🟢 LONG SETUP CHECK
                # Searching for bullish FVGs in the past
                for i in range(2, curr_idx):
                    # Is it a bullish FVG? (High[i-2] < Low[i])
                    if df['high'].iloc[i - 2] < df['low'].iloc[i] and df['close'].iloc[i - 1] > df['open'].iloc[i - 1]:
                        gap_top = df['low'].iloc[i]
                        gap_bottom = df['high'].iloc[i - 2]

                        # CONDITION 1: did candle i-2 (or i-1) touch a support pivot?
                        candle_1_low = df['low'].iloc[i - 2]
                        if is_touching_pivot(candle_1_low, supports, i - 2, threshold=0.001):
                            # CONDITION 2: has the FVG NOT yet been "fully closed"?
                            # Fully closed means price fell to gap_bottom or below
                            was_closed_before = False
                            for j in range(i + 1, curr_idx):
                                if df['low'].iloc[j] <= gap_bottom:
                                    was_closed_before = True
                                    break

                            if not was_closed_before:
                                # CONDITION 3: has the CURRENT (last closed) candle fully closed the FVG?
                                if curr_low <= gap_bottom:
                                    # FIX: cooldown check before sending, otherwise the
                                    # bot fires the same signal hourly, as long as the
                                    # FVG criterion is met.
                                    #
                                    # FIX T-2026-CU-9050-024: no symbol in the tag —
                                    # trade_cooldowns.module is varchar(10) on the live DB
                                    # and the old f"MAYANK_{symbol}_{tf}" (>=14 chars) made
                                    # every update_cooldown throw AFTER the outbox insert:
                                    # cooldown never persisted, the same FVG re-posted
                                    # every scan. The symbol already lives in the `coin`
                                    # key column, so (module, coin, direction) stays unique.
                                    module_tag = f"MAYANK_{tf.upper()}"
                                    with get_db_connection() as _cd_conn:
                                        if check_cooldown(_cd_conn, module_tag, symbol_name, "LONG", 12):
                                            logger.info(f"⏳ Cooldown active for {symbol_name} ({tf}) LONG. Skip.")
                                            break

                                    logger.info(
                                        f"🚀 BINGO LONG! {symbol_name} ({tf}) has fully closed the FVG at {gap_bottom:.3f}!"
                                    )

                                    # Calculate targets (next resistance pivots above)
                                    targets = sorted([val for idx, val in resistances if val > curr_price])[:8]
                                    if not targets:
                                        targets = [curr_price * 1.01, curr_price * 1.02]  # fallback for TradFi

                                    sl = curr_low * 0.998  # Just below the last low

                                    # P2.45(c): SL/RR sanity. SL and TP1 are
                                    # identical for every FVG of this scan (from curr_low
                                    # resp. curr_price/resistances) → a fail blocks
                                    # the whole scan, hence break.
                                    if not passes_sl_rr_guard(curr_price, sl, targets[0], "LONG"):
                                        logger.info(
                                            f"🚫 SL/RR guard rejects {symbol_name} ({tf}) LONG "
                                            f"(entry {curr_price:.4f}, sl {sl:.4f}, tp1 {targets[0]:.4f})"
                                        )
                                        break

                                    chart_path = generate_setup_chart(
                                        df,
                                        symbol_name,
                                        tf,
                                        {'top': gap_top, 'bottom': gap_bottom, 'index': i},
                                        supports,
                                        resistances,
                                        "LONG",
                                    )

                                    msg = f"""<pre><b>🎯 SMC PIVOT RETEST</b>\n<b>{symbol_name} | {tf} Chart</b>\n<b>→ Action: <b>LONG</b></b>\n<b>→ Entry: {curr_price:.4f}</b>\n<b>→ FVG Fully Closed: {gap_bottom:.4f}</b>\n<b>→ Stop Loss: {sl:.4f}</b>\n<b>→ Targets:</b> {', '.join([f'{t:.3f}' for t in targets[:3]])}</pre>"""

                                    # Send via Telegram (outbox logic, as in other scripts)
                                    with get_db_connection() as conn:
                                        with conn.cursor() as cur:
                                            if chart_path:
                                                cur.execute(
                                                    "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                                                    (SMC_CHANNEL_ID, msg, chart_path),
                                                )
                                            else:
                                                cur.execute(
                                                    "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                                                    (SMC_CHANNEL_ID, msg),
                                                )
                                        conn.commit()
                                        # Set cooldown AFTER successful send
                                        update_cooldown(conn, module_tag, symbol_name, "LONG")
                                    break  # Nur einmal triggern

                # 🔴 SHORT SETUP CHECK
                for i in range(2, curr_idx):
                    # Is it a bearish FVG? (low[i-2] > high[i])
                    if df['low'].iloc[i - 2] > df['high'].iloc[i] and df['close'].iloc[i - 1] < df['open'].iloc[i - 1]:
                        gap_top = df['low'].iloc[i - 2]
                        gap_bottom = df['high'].iloc[i]

                        # CONDITION 1: did candle i-2 touch a resistance pivot?
                        candle_1_high = df['high'].iloc[i - 2]
                        if is_touching_pivot(candle_1_high, resistances, i - 2, threshold=0.001):
                            # CONDITION 2: has the FVG NOT yet been "fully closed"? (price rose to gap_top)
                            was_closed_before = False
                            for j in range(i + 1, curr_idx):
                                if df['high'].iloc[j] >= gap_top:
                                    was_closed_before = True
                                    break

                            if not was_closed_before:
                                # CONDITION 3: has the CURRENT candle fully closed the FVG?
                                if curr_high >= gap_top:
                                    # FIX: cooldown check before sending (see LONG above).
                                    # FIX T-2026-CU-9050-024: no symbol in the tag
                                    # (varchar(10)); symbol lives in the coin key column —
                                    # see the LONG branch.
                                    module_tag = f"MAYANK_{tf.upper()}"
                                    with get_db_connection() as _cd_conn:
                                        if check_cooldown(_cd_conn, module_tag, symbol_name, "SHORT", 12):
                                            logger.info(f"⏳ Cooldown active for {symbol_name} ({tf}) SHORT. Skip.")
                                            break

                                    logger.info(
                                        f"💥 BINGO SHORT! {symbol_name} ({tf}) has fully closed the FVG at {gap_top:.3f}!"
                                    )

                                    # targets (next support pivots below)
                                    targets = sorted([val for idx, val in supports if val < curr_price], reverse=True)[
                                        :8
                                    ]
                                    if not targets:
                                        targets = [curr_price * 0.99, curr_price * 0.98]

                                    sl = curr_high * 1.002

                                    # P2.45(c): SL/RR sanity (see the LONG branch).
                                    if not passes_sl_rr_guard(curr_price, sl, targets[0], "SHORT"):
                                        logger.info(
                                            f"🚫 SL/RR guard rejects {symbol_name} ({tf}) SHORT "
                                            f"(entry {curr_price:.4f}, sl {sl:.4f}, tp1 {targets[0]:.4f})"
                                        )
                                        break

                                    chart_path = generate_setup_chart(
                                        df,
                                        symbol_name,
                                        tf,
                                        {'top': gap_top, 'bottom': gap_bottom, 'index': i},
                                        supports,
                                        resistances,
                                        "SHORT",
                                    )

                                    msg = f"""<pre><b>🎯 SMC PIVOT RETEST</b>\n<b>{symbol_name} | {tf} Chart</b>\n<b>→ Action: <b>SHORT</b></b>\n<b>→ Entry: {curr_price:.4f}</b>\n<b>→ FVG Fully Closed: {gap_top:.4f}</b>\n<b>→ Stop Loss: {sl:.4f}</b>\n<b>→ Targets:</b> {', '.join([f'{t:.3f}' for t in targets[:3]])}</pre>"""

                                    with get_db_connection() as conn:
                                        with conn.cursor() as cur:
                                            if chart_path:
                                                cur.execute(
                                                    "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                                                    (SMC_CHANNEL_ID, msg, chart_path),
                                                )
                                            else:
                                                cur.execute(
                                                    "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                                                    (SMC_CHANNEL_ID, msg),
                                                )
                                        conn.commit()
                                        # Set cooldown AFTER successful send
                                        update_cooldown(conn, module_tag, symbol_name, "SHORT")
                                    break

            except Exception as e:
                logger.error(f"Error analysing {ticker} ({tf}): {e}")


def main():
    logger.info("=== 🏦 TRADFI PIVOT SMC BOT STARTED ===")

    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)

            # Runs at minute :01 every hour (then 1h and 4h candles guaranteed closed and available on YFinance)
            if now.minute == 1:
                analyze_strategy()
                logger.info("🏁 Run stopped. Sleeping for 60 seconds...")
                time.sleep(60)
            else:
                time.sleep(10)

        except KeyboardInterrupt:
            logger.info("🛑 Bot stopped (Ctrl+C).")
            break
        except Exception as e:
            logger.error(f"Critical error in the main loop: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()

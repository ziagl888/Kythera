import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import json
import logging
import os
import time
from datetime import datetime, timezone

import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd
import scipy.signal

from core import config as _kcfg  # channel ids
from core.candles import read_candles
from core.database import get_db_connection
from core.live_price import get_live_price, get_live_prices_batch
from core.market_utils import load_coins

# 🛠️ CONFIGURATION
logging.basicConfig(level=logging.INFO, format='%(asctime)s - INST_PATTERN_BOT - %(message)s')
logger = logging.getLogger(__name__)

# 🔴 ENTER THE NEW CHANNEL FOR INSTITUTIONAL PATTERNS HERE
INSTITUTIONAL_CHANNEL_ID = _kcfg.CH_INSTITUTIONAL

COINS_FILE = "coins.json"
CHART_DIR = "institutional_charts"
os.makedirs(CHART_DIR, exist_ok=True)

TIMEFRAME = '1h'
LOOKBACK_CANDLES = 300  # how far back we look
ZONE_TOLERANCE = 0.005  # 0.5% tolerance for the entry zone at the QML

# FIX: ALERTED_QMS must be persisted, otherwise the bot fires ~500 duplicate
# alerts after EVERY restart (one alert per already-active pattern).
# This blocks Telegram Flood Control for the whole outbox for hours.
ALERTED_QMS_FILE = "alerted_qms.json"
ALERTED_QMS = set()


def load_alerted_qms():
    """Loads already-reported pattern IDs from JSON."""
    global ALERTED_QMS
    if not os.path.exists(ALERTED_QMS_FILE):
        ALERTED_QMS = set()
        logger.info("📂 No alerted_qms.json found → starting fresh.")
        return
    try:
        with open(ALERTED_QMS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        ALERTED_QMS = set(data)
        logger.info(f"✅ {len(ALERTED_QMS)} known pattern IDs loaded.")
    except Exception as e:
        logger.error(f"Error loading {ALERTED_QMS_FILE}: {e}")
        ALERTED_QMS = set()


def save_alerted_qms():
    """Persists the pattern IDs to disk atomically."""
    try:
        tmp = ALERTED_QMS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(ALERTED_QMS), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ALERTED_QMS_FILE)
    except Exception as e:
        logger.error(f"Error saving {ALERTED_QMS_FILE}: {e}")


# 📡 DATA & HELPER FUNCTIONS


def send_telegram_alert(conn, message, image_path):
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                (INSTITUTIONAL_CHANNEL_ID, message, image_path),
            )
        conn.commit()
    except Exception as e:
        logger.error(f"Error sending to the outbox: {e}")


# 🧠 INSTITUTIONAL STRUCTURE (PIVOTS)
def get_alternating_pivots(df, window=5):
    """
    Finds highs and lows and enforces that they strictly alternate (H, L, H, L).
    This is mandatory to detect structure patterns like Quasimodo.
    """
    highs = df['high'].values
    lows = df['low'].values

    peak_idx = scipy.signal.argrelextrema(highs, np.greater, order=window)[0]
    trough_idx = scipy.signal.argrelextrema(lows, np.less, order=window)[0]

    # Collect all pivots: format (index, type(1=High, -1=Low), price)
    raw_pivots = [(i, 1, highs[i]) for i in peak_idx] + [(i, -1, lows[i]) for i in trough_idx]
    raw_pivots.sort(key=lambda x: x[0])

    if not raw_pivots:
        return []

    alt_pivots = [raw_pivots[0]]
    for i in range(1, len(raw_pivots)):
        curr_idx, curr_type, curr_price = raw_pivots[i]
        last_idx, last_type, last_price = alt_pivots[-1]

        if curr_type == last_type:
            # Two identical pivots in a row? Keep the more extreme one!
            if (curr_type == 1 and curr_price > last_price) or (curr_type == -1 and curr_price < last_price):
                alt_pivots[-1] = raw_pivots[i]
        else:
            alt_pivots.append(raw_pivots[i])

    return alt_pivots


# 🎨 CHART GENERATOR
def generate_qm_chart(df, symbol, pattern_type, p1, p2, p3, p4, qm_level):
    """
    Draws the chart, connects the pivot points into a zig-zag pattern
    and draws a horizontal line for the Quasimodo entry level.
    """
    try:
        # 1. Start a bit before the first pivot
        start_idx = max(0, p1[0] - 20)
        plot_df = df.iloc[start_idx:].copy()

        # 2. Convert timestamp robustly (without timezone!)
        plot_df['open_time'] = pd.to_datetime(plot_df['open_time']).dt.tz_localize(None)
        plot_df.set_index('open_time', inplace=True)

        # 3. Padding (empty space to the right in the future for the retest)
        time_step = plot_df.index[-1] - plot_df.index[-2]
        future_dates = [plot_df.index[-1] + time_step * i for i in range(1, 15)]
        empty_df = pd.DataFrame(np.nan, index=future_dates, columns=plot_df.columns).astype(float)
        plot_df = pd.concat([plot_df, empty_df])

        # 4. Parse timestamps for the zig-zag line exactly
        def get_dt(idx):
            return pd.to_datetime(df['open_time'].iloc[idx]).tz_localize(None)

        seq_lines = [
            (get_dt(p1[0]), float(p1[2])),
            (get_dt(p2[0]), float(p2[2])),
            (get_dt(p3[0]), float(p3[2])),
            (get_dt(p4[0]), float(p4[2])),
        ]

        # 5. Styling and colours
        color_theme = '#ff4466' if "BEARISH" in pattern_type else '#00ff88'
        mc = mpf.make_marketcolors(up='#26a69a', down='#ef5350', edge='inherit', wick='inherit')
        s = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds', gridstyle=':')

        rel_filename = f"{CHART_DIR}/{symbol}_QM_{int(time.time())}.png"
        abs_filename = os.path.abspath(rel_filename)

        mpf.plot(
            plot_df,
            type='candle',
            style=s,
            alines=dict(alines=seq_lines, colors=color_theme, linewidths=2, linestyle='-'),
            hlines=dict(hlines=[float(qm_level)], colors=[color_theme], linewidths=2, linestyle='--'),
            title=f"\n{symbol} | {pattern_type} Quasimodo (QML: {qm_level:.4f})",
            figsize=(12, 7),
            tight_layout=True,
            savefig=abs_filename,
            returnfig=False,
        )

        logger.info(f"Chart generated successfully: {abs_filename}")
        return abs_filename

    except Exception as e:
        logger.error(f"Chart Error for {symbol}: {e}", exc_info=True)
        return None
    finally:
        # Closes the figure left open by mpf.plot — prevents RAM leak.
        plt.close('all')


# 🕵️ PATTERN SCANNER
def scan_institutional_patterns():
    conn = get_db_connection()
    coins = load_coins()

    logger.info(f"🔍 Scanning {len(coins)} coins for institutional patterns...")

    # R1: live price for the QML-proximity gate — batch ticker (1 call/cycle),
    # per-coin HTTP→DB fallback on miss (core.live_price).
    price_map = get_live_prices_batch()

    try:
        for symbol in coins:
            # R1: detect on CLOSED candles only; core.candles returns ASC (no reverse)
            # and drops the forming bar, so the pivots no longer repaint.
            df = read_candles(
                conn,
                symbol,
                TIMEFRAME,
                limit=LOOKBACK_CANDLES,
                include_forming=False,
                columns=("open_time", "open", "high", "low", "close"),
            )

            if len(df) < 100:
                continue

            # core.candles yields raw NUMERIC (Decimal/object); cast to float for the
            # scipy pivot search and the price math below.
            for _c in ("open", "high", "low", "close"):
                df[_c] = df[_c].astype(float)

            pivots = get_alternating_pivots(df, window=5)

            if len(pivots) < 4:
                continue

            # Detection is done on closed candles; the QML-proximity gate below needs
            # the live price, fetched AFTER structure resolved (no per-scan overhead).
            current_price = price_map.get(symbol) or get_live_price(symbol, conn)
            if not current_price:
                continue

            # We always analyse packets of 4 consecutive pivots
            for i in range(len(pivots) - 3):
                p1, p2, p3, p4 = pivots[i], pivots[i + 1], pivots[i + 2], pivots[i + 3]

                # --- 🔴 BEARISH QUASIMODO (SHORT SETUP) ---
                # Structure must be: High, Low, Higher High, Lower Low
                if p1[1] == 1 and p2[1] == -1 and p3[1] == 1 and p4[1] == -1:
                    H, L, HH, LL = p1[2], p2[2], p3[2], p4[2]

                    if HH > H and LL < L:  # QM confirmation
                        qm_level = H
                        # FIX: previously {p1[0]} = candle index → shifts with
                        # every new candle → same pattern gets a new ID and
                        # is reported again. Now unix timestamp of the pivot candle.
                        pivot_ts = int(pd.to_datetime(df['open_time'].iloc[p1[0]]).timestamp())
                        pattern_id = f"{symbol}_BEAR_QM_{pivot_ts}"

                        # Check whether the current price is approaching the QML from below
                        # We trigger the alert if it lies within ZONE_TOLERANCE
                        dist_to_qml = (qm_level - current_price) / qm_level

                        if 0 <= dist_to_qml <= ZONE_TOLERANCE and pattern_id not in ALERTED_QMS:
                            ALERTED_QMS.add(pattern_id)
                            logger.info(f"🚨 BEARISH QM found at {symbol}! QML: {qm_level}")

                            chart_path = generate_qm_chart(df, symbol, "BEARISH", p1, p2, p3, p4, qm_level)
                            msg = f"""<b>🏛 INSTITUTIONAL PA DETECTED</b>
<b>{symbol.replace('USDT', '')} | {TIMEFRAME}</b>

📉 <b>Pattern:</b> BEARISH QUASIMODO (QM)
🎯 <b>Entry Zone (QML):</b> <code>${qm_level:.4f}</code>
💵 <b>Current Price:</b> ${current_price:.4f}

<i>Explanation: Price grabbed liquidity above the old high (HH), then aggressively broke market structure (LL). We are now retesting the origin of the move (QML). Look for Short setups!</i>"""
                            send_telegram_alert(conn, msg, chart_path)

                # --- 🟢 BULLISH QUASIMODO (LONG SETUP) ---
                # Structure must be: Low, High, Lower Low, Higher High
                elif p1[1] == -1 and p2[1] == 1 and p3[1] == -1 and p4[1] == 1:
                    L, H, LL, HH = p1[2], p2[2], p3[2], p4[2]

                    if LL < L and HH > H:  # QM confirmation
                        qm_level = L
                        # FIX: same fix as BEARISH above — timestamp instead of index.
                        pivot_ts = int(pd.to_datetime(df['open_time'].iloc[p1[0]]).timestamp())
                        pattern_id = f"{symbol}_BULL_QM_{pivot_ts}"

                        # Check whether the current price is falling into the QML from above
                        dist_to_qml = (current_price - qm_level) / qm_level

                        if 0 <= dist_to_qml <= ZONE_TOLERANCE and pattern_id not in ALERTED_QMS:
                            ALERTED_QMS.add(pattern_id)
                            logger.info(f"🚀 BULLISH QM found at {symbol}! QML: {qm_level}")

                            chart_path = generate_qm_chart(df, symbol, "BULLISH", p1, p2, p3, p4, qm_level)
                            msg = f"""<b>🏛 INSTITUTIONAL PA DETECTED</b>
<b>{symbol.replace('USDT', '')} | {TIMEFRAME}</b>

📈 <b>Pattern:</b> BULLISH QUASIMODO (QM)
🎯 <b>Entry Zone (QML):</b> <code>${qm_level:.4f}</code>
💵 <b>Current Price:</b> ${current_price:.4f}

<i>Explanation: Price grabbed liquidity below the old low (LL), then strongly broke market structure to the upside (HH). We are now retesting the demand zone (QML). Look for Long setups!</i>"""
                            send_telegram_alert(conn, msg, chart_path)

    except Exception as e:
        logger.error(f"Critical error in the scanner: {e}", exc_info=True)
    finally:
        # FIX: persist the pattern IDs after every scan, so they are not
        # reported again on restart.
        save_alerted_qms()
        conn.close()


def main():
    logger.info("=== 🏛 INSTITUTIONAL PATTERN BOT STARTED ===")
    # FIX: load known pattern IDs at startup.
    load_alerted_qms()
    while True:
        now = datetime.now(timezone.utc)
        # Scans every hour precisely at minute :05
        if now.minute == 5:
            scan_institutional_patterns()
            logger.info("Scan stopped. Sleeping for 55 minutes...")
            time.sleep(3300)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C).")

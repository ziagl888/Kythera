import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import logging
import time

import pandas as pd

from core import config as _kcfg  # channel ids
from core.candles import read_candles

# --- Import our own DB connection ---
from core.database import get_db_connection
from core.market_utils import calculate_pivots, check_cooldown, get_max_leverage, update_cooldown
from core.trade_utils import cap_leverage_to_sl

logging.basicConfig(level=logging.INFO, format='%(asctime)s - BTC_SNIPER - %(message)s')
logger = logging.getLogger(__name__)

# 🛠️ CONFIGURATION FOR CORNIX & STRATEGY
CHANNEL_ID = _kcfg.CH_BTC_SMC  # 🔴 ENTER YOUR NEW CHANNEL HERE!
SYMBOL = 'BTCUSDT'
TIMEFRAME = '1h'

# The winning parameters from the grid-search backtest
EMA_PERIOD = 21

# SL is computed dynamically: 1.0 × ATR(14) with a floor of 0.4% and a cap of 1.2%.
# This keeps the strategy tight in a calm market (high R:R), but it also
# adapts automatically to volatility (e.g. after CPI releases, halving events).
SL_ATR_MULT = 1.0
SL_PCT_FLOOR = 0.004  # 0.4% — minimum SL (historically optimised floor)
SL_PCT_CAP = 0.012  # 1.2% — cap prevents SLs from getting too wide in high-vol phases

# FIX P0.5 (audit): 100x with a 0.4-1.2% SL liquidated at ~-0.9% BEFORE the SL —
# every stop was -100% margin. Now 25x plus additionally cap_leverage_to_sl (R4).
DESIRED_LEVERAGE = 25  # gets capped against max_leverage.json

MIN_RR_RATIO = 1.25  # minimum risk-reward
MAX_PIVOT_AGE = 120  # no ancient stale targets
MAX_FVG_AGE = 48  # FVG must be filled within 2 days

# Cooldown/dedupe (P2.46): without this lock, the bot re-fires the same
# setup one 1h candle later on gap-filler lag. 12h is the fleet default for
# sub-daily timeframes (P1.27 pattern, cf. 16_smc_forex_metals COOLDOWN_HOURS.get(tf, 12))
# and exceeds the candle duration (1h), so the 1h-offset duplicate signal is
# reliably blocked. The tag carries no symbol — that lives in the coin key column;
# "BTCSMC_1H" (9 characters) fits in trade_cooldowns.module varchar(10) (T-024 trap).
COOLDOWN_TAG = "BTCSMC_1H"
COOLDOWN_HOURS = 12


def calculate_atr(df, period=14):
    """Average True Range for dynamic SL calculation."""
    high = df['high']
    low = df['low']
    close = df['close']
    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def calculate_dynamic_sl_pct(df, curr_close):
    """Returns the SL distance as a fraction of the price (e.g. 0.006 = 0.6%).
    Basis: ATR × SL_ATR_MULT, capped between SL_PCT_FLOOR and SL_PCT_CAP.
    """
    atr_series = calculate_atr(df, 14)
    atr_val = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
    if atr_val <= 0 or curr_close <= 0:
        return SL_PCT_FLOOR
    sl_pct = (atr_val * SL_ATR_MULT) / curr_close
    sl_pct = max(SL_PCT_FLOOR, min(SL_PCT_CAP, sl_pct))
    return sl_pct


# 📡 CORNIX SIGNAL GENERATOR
def send_cornix_signal(direction, entry, sl, tp, rr, lev):
    """Generates a clean text signal that Cornix understands 100%.

    Returns True if the signal was posted, False if the cooldown
    suppressed it (P2.46) or a DB error occurred. The cooldown upsert shares
    the outbox insert's transaction (commit=False + a single conn.commit),
    so signal and dedupe marker are persisted atomically — a partial commit
    would let the next scan post the same setup again.
    """

    emoji = "🟢" if direction == "LONG" else "🔴"

    # Standard Cornix Parsing Format
    msg = f"""{emoji} <b>SMC Sniper Setup</b>
Symbol: {SYMBOL}
Direction: {direction}
Leverage: {lev}

Entry: {entry:.2f}
Take-Profit 1: {tp:.2f}
Stop-Loss: {sl:.2f}

<i>Risk/Reward: 1 : {rr:.2f} | Strategy: EMA21 + FVG Pivot Retest</i>"""

    try:
        with get_db_connection() as conn:
            # P2.46: on gap-filler lag, the same FVG setup re-triggers the bot one
            # candle later. check_cooldown returns True as long as the lock is
            # active → then don't post again.
            if check_cooldown(conn, COOLDOWN_TAG, SYMBOL, direction, COOLDOWN_HOURS):
                logger.info(f"⏳ Cooldown active for {SYMBOL} {direction}. Skip.")
                return False
            with conn.cursor() as cur:
                # We send it as plain text into the outbox, HTML formatted
                cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (CHANNEL_ID, msg))
            # Set the cooldown in the same commit as the outbox insert (caller-commit contract).
            update_cooldown(conn, COOLDOWN_TAG, SYMBOL, direction, commit=False)
            conn.commit()
        logger.info(f"Cornix signal sent! {direction} @ {entry:.2f} (R:R {rr:.2f}, Lev {lev})")
        return True
    except Exception as e:
        logger.error(f"Error sending the signal: {e}")
        return False


# 📊 DATA FETCHING (LOCAL DATABASE)
def fetch_db_data():
    # C-gate follow-up (T-2026-KYT-9050-068): the raw
    # `SELECT ... FROM "{SYMBOL}_{TIMEFRAME}"` read the per-coin table directly,
    # bypassing core.candles. Since the write-primary cutover on 2026-07-16
    # (KYTHERA_CANDLES_WRITE_PRIMARY=hyper), nobody writes to this table anymore
    # — `BTCUSDT_1h` ends exactly at open_time 2026-07-16 16:00 UTC. Since then
    # the bot got a frozen frame and stayed silent (empty input → no output,
    # not wrong output). Via core.candles it follows the backend switch.
    try:
        conn = get_db_connection()
        try:
            # `limit` returns the NEWEST n candles, sorted ascending — exactly
            # what the old DESC-LIMIT-500 + reverse produced. The former
            # `.iloc[:-1]` drop is DELIBERATELY gone: it removed the running candle
            # that the raw SELECT brought along. `include_forming=False` already
            # excludes it (hard rule 5); an additional drop would discard the newest
            # CLOSED candle and delay every signal by one candle.
            df = read_candles(
                conn,
                SYMBOL,
                TIMEFRAME,
                limit=500,
                include_forming=False,
                columns=("open_time", "open", "high", "low", "close"),
            )
        finally:
            conn.close()

        if df.empty:
            return df

        df = df.reset_index(drop=True)
        for c in ['open', 'high', 'low', 'close']:
            df[c] = df[c].astype(float)

        return df
    except Exception as e:
        logger.error(f"Error loading DB data: {e}")
        return pd.DataFrame()


# 🧠 SMC MATH


def is_touching_pivot(price, pivots, max_idx, threshold=0.001):
    for p_idx, p_val in reversed(pivots):
        if p_idx > max_idx - 5:
            continue
        if p_idx < max_idx - MAX_PIVOT_AGE:
            break
        if abs(price - p_val) / p_val <= threshold:
            return True
    return False


# 🚀 CORE ENGINE
def analyze_market():
    logger.info("🔍 Analysing BTCUSDT 1h chart for sniper setups...")
    df = fetch_db_data()

    if df.empty or len(df) < 200:
        return

    highs = df['high'].values
    lows = df['low'].values
    opens = df['open'].values
    closes = df['close'].values

    # EMA 21 for the trend
    ema_values = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean().values

    supports, resistances = calculate_pivots(df, window=5)

    # We analyse right now the VERY LAST closed candle
    curr_idx = len(df) - 1
    curr_low = lows[curr_idx]
    curr_high = highs[curr_idx]
    curr_price = closes[curr_idx]
    curr_ema = ema_values[curr_idx]

    # We look for FVGs that formed in the last 48 candles
    search_start = max(2, curr_idx - MAX_FVG_AGE)

    # 🟢 1. CHECK LONG SETUPS
    if curr_price > curr_ema:  # trend filter
        for i in range(search_start, curr_idx):
            # Is it a bullish FVG?
            if highs[i - 2] < lows[i] and closes[i - 1] > opens[i - 1]:
                gap_bottom = highs[i - 2]
                candle_1_low = lows[i - 2]

                # Was it started at a pivot?
                if is_touching_pivot(candle_1_low, supports, i - 2):
                    # Was it already closed BEFORE the current candle?
                    was_closed_before = any(lows[j] <= gap_bottom for j in range(i + 1, curr_idx))

                    if not was_closed_before:
                        # Did the CURRENT candle fully close the FVG just now?
                        if curr_low <= gap_bottom:
                            # SETUP FOUND! Searching for targets...
                            valid_res = [
                                val
                                for p_idx, val in resistances
                                if curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val > curr_price
                            ]

                            if valid_res:
                                target = min(valid_res)
                                sl_pct = calculate_dynamic_sl_pct(df, curr_price)
                                sl = curr_low * (1.0 - sl_pct)
                                risk = curr_price - sl
                                reward = target - curr_price
                                rr = reward / risk

                                if risk > 0 and rr >= MIN_RR_RATIO:
                                    lev = cap_leverage_to_sl(get_max_leverage(SYMBOL, DESIRED_LEVERAGE), curr_price, sl)
                                    if send_cornix_signal("LONG", curr_price, sl, target, rr, lev):
                                        logger.info(
                                            f"🎯 BINGO LONG! FVG fully closed at {gap_bottom:.2f} | SL pct {sl_pct * 100:.2f}%"
                                        )
                                    return  # prevents us from posting multiple setups in the same pass

    # 🔴 2. CHECK SHORT SETUPS
    if curr_price < curr_ema:  # trend filter
        for i in range(search_start, curr_idx):
            # Is it a bearish FVG?
            if lows[i - 2] > highs[i] and closes[i - 1] < opens[i - 1]:
                gap_top = lows[i - 2]
                candle_1_high = highs[i - 2]

                # Was it started at a pivot?
                if is_touching_pivot(candle_1_high, resistances, i - 2):
                    # Was it already closed BEFORE the current candle?
                    was_closed_before = any(highs[j] >= gap_top for j in range(i + 1, curr_idx))

                    if not was_closed_before:
                        # Did the CURRENT candle fully close the FVG just now?
                        if curr_high >= gap_top:
                            # SETUP FOUND! Searching for targets...
                            valid_sup = [
                                val
                                for p_idx, val in supports
                                if curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val < curr_price
                            ]

                            if valid_sup:
                                target = max(valid_sup)
                                sl_pct = calculate_dynamic_sl_pct(df, curr_price)
                                sl = curr_high * (1.0 + sl_pct)
                                risk = sl - curr_price
                                reward = curr_price - target
                                rr = reward / risk

                                if risk > 0 and rr >= MIN_RR_RATIO:
                                    lev = cap_leverage_to_sl(get_max_leverage(SYMBOL, DESIRED_LEVERAGE), curr_price, sl)
                                    if send_cornix_signal("SHORT", curr_price, sl, target, rr, lev):
                                        logger.info(
                                            f"🎯 BINGO SHORT! FVG fully closed at {gap_top:.2f} | SL pct {sl_pct * 100:.2f}%"
                                        )
                                    return


# ⏰ MAIN LOOP
def main():
    logger.info("=== 🎯 BTC SNIPER BOT (CORNIX EDITION) STARTED ===")
    logger.info(
        f"Parameter: EMA {EMA_PERIOD} | SL dynamic (ATR×{SL_ATR_MULT}, {SL_PCT_FLOOR * 100:.1f}%–{SL_PCT_CAP * 100:.1f}%) | Min R:R {MIN_RR_RATIO}"
    )

    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)

            # Checking always at minute :01 (when the 1h candle is guaranteed closed and in the DB)
            if now.minute == 1:
                analyze_market()
                logger.info("🏁 Pass stopped. Sleeping for 55 minutes...")
                time.sleep(3300)  # sleep for 55 minutes to save CPU
            else:
                time.sleep(10)  # short check every 10 seconds

        except KeyboardInterrupt:
            logger.info("🛑 Bot is stopping (CTRL+C).")
            break
        except Exception as e:
            logger.error(f"Critical error in the main loop: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()

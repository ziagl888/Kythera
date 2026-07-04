"""
29_ufi1_bot.py — UFI1 (Uptrend Fib Inversion Bot 1)

Strategy (backtest-validated, 535 coins, 1 year, short-only):
  1. Detects a significant swing-high (≥60% distance to local low)
  2. Coin dumps from top → local low
  3. Retraces back to the 0.382 Fib level
  4. Candle closes BELOW the Fib level (bearish rejection = confirmation)
  5. Entry SHORT, SL = swing-high + 3%, TP1 = Fib extension 1.0 (old low)

Backtest: WR 54.2%, Avg +0.83R, Total +278R, SL-Rate 17.1% (334 Trades / 1 Jahr)

Checks all 1D candles every 4 hours (daily granularity, candle checked against live price).
Cooldown: 48h per coin (prevents multiple entries on the same setup).

Channel: _kcfg.CH_UFI1
Identifier: UFI1
Watchdog: start_delay=183
"""

from __future__ import annotations

import json
import time
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from core import config as _kcfg  # channel ids
from core.database import get_db_connection
from core.logging_setup import setup_logging
from core.market_utils import check_cooldown, get_max_leverage, load_coins, update_cooldown
from core.trade_utils import cap_leverage_to_sl

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
CHANNEL_ID = _kcfg.CH_UFI1  # UFI1 Trading Channel
MODULE_NAME = "UFI1"
SLEEP_SECONDS = 4 * 3600  # every 4 hours
COOLDOWN_HOURS = 48  # 48h cooldown per coin

# Strategy parameters (from backtest optimisation)
MIN_SWING_PCT = 60.0  # minimum swing size for valid setup
FIB_ENTRY_LEVEL = 0.382  # only 0.382 entry level
FIB_ENTRY_TOL = 0.02  # ±2% tolerance around Fib level
MAX_RETRACE_BARS = 15  # max daily candles for retracement search
SL_BUFFER = 0.03  # SL = swing-high + 3%
SWING_LOOKBACK = 5  # candles for swing detection

# Data lookback
DAILY_BARS_LOOKBACK = 120  # load 120 daily candles

# ─────────────────────────────────────────────────────────────────────────────
logger = setup_logging("UFI1_BOT")


# ─────────────────────────────────────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────────────────────────────────────


def load_daily_ohlcv(conn, symbol: str) -> pd.DataFrame | None:
    """Loads daily OHLCV data for a coin."""
    since = datetime.now(timezone.utc) - timedelta(days=DAILY_BARS_LOOKBACK)
    table = f"{symbol}_1d"
    try:
        df = pd.read_sql_query(
            f'SELECT open_time, open, high, low, close, volume '
            f'FROM "{table}" WHERE open_time >= %s ORDER BY open_time ASC',
            conn,
            params=(since,),
        )
    except Exception:
        return None

    if df.empty or len(df) < SWING_LOOKBACK * 2 + 5:
        return None

    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.set_index("open_time")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["high", "low", "close"])
    return df


def get_live_price(conn, symbol: str) -> float | None:
    """Fetches the current price from the 1h table."""
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT close FROM "{symbol}_1h" ORDER BY open_time DESC LIMIT 1')
            row = cur.fetchone()
        if row:
            return float(row[0])
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# FIBONACCI
# ─────────────────────────────────────────────────────────────────────────────


def fib_retracement_price(swing_high: float, swing_low: float, level: float) -> float:
    """Calculates the price of a Fibonacci retracement level."""
    return swing_high - (swing_high - swing_low) * level


def fib_extension_price(swing_high: float, swing_low: float, level: float) -> float:
    """
    Fib extension below the low (SHORT target).
    Level 1.0 = old low (repetition of the swing)
    """
    return swing_low - (swing_high - swing_low) * (level - 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# SETUP DETECTION
# ─────────────────────────────────────────────────────────────────────────────


def find_ufi1_setup(df: pd.DataFrame, live_price: float | None = None) -> dict | None:
    """
    Searches for a valid UFI1 SHORT setup in daily candles.

    Flow:
      1. Detect swing-highs (5 candles left+right)
      2. Find local low after the swing-high
      3. Swing must be ≥ MIN_SWING_PCT
      4. Search for retracement to 0.382 level ±2%
      5. Last candle must close BELOW the Fib level (confirmation)
      6. Setup must not be "stale" (price already through target)

    Returns dict with setup details, or None.
    """
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    dates = df.index.tolist()
    n = len(df)

    # Find swing-highs
    swing_high_idxs = []
    for i in range(SWING_LOOKBACK, n - SWING_LOOKBACK):
        window = highs[i - SWING_LOOKBACK : i + SWING_LOOKBACK + 1]
        if highs[i] == max(window) and list(window).count(highs[i]) == 1:
            swing_high_idxs.append(i)

    for sh_idx in reversed(swing_high_idxs):  # most recent swings first
        sh_price = highs[sh_idx]

        # Find local low after swing-high
        search_end = min(sh_idx + MAX_RETRACE_BARS * 2, n)
        seg_lows = lows[sh_idx + 1 : search_end]
        if len(seg_lows) == 0:
            continue
        min_offset = int(np.argmin(seg_lows))
        min_low_idx = sh_idx + 1 + min_offset
        min_low = lows[min_low_idx]

        # Swing large enough?
        swing_pct = (sh_price - min_low) / sh_price * 100
        if swing_pct < MIN_SWING_PCT:
            continue

        # Calculate Fib 0.382 level
        fib_entry_price = fib_retracement_price(sh_price, min_low, FIB_ENTRY_LEVEL)
        fib_tp1_price = fib_extension_price(sh_price, min_low, 1.0)  # = old low

        # Search for retracement entry after the low
        search_end2 = min(min_low_idx + MAX_RETRACE_BARS, n)
        for j in range(min_low_idx + 1, search_end2):
            candle_high = highs[j]
            candle_close = closes[j]

            # Candle high must be near the Fib level
            dist = abs(candle_high - fib_entry_price) / fib_entry_price
            if dist > FIB_ENTRY_TOL:
                # Also check close
                dist_close = abs(candle_close - fib_entry_price) / fib_entry_price
                if dist_close > FIB_ENTRY_TOL:
                    continue

            # CONFIRMATION: candle must close BELOW the Fib level
            if candle_close >= fib_entry_price:
                continue

            entry_price = candle_close
            sl_price = sh_price * (1 + SL_BUFFER)
            tp1_price = fib_tp1_price

            # Sanity checks
            if tp1_price >= entry_price:
                continue  # TP must be below entry
            if sl_price <= entry_price:
                continue  # SL must be above entry

            # Stale check: use live price if available, else last daily close.
            # Setup is stale if:
            #   a) Price is already AT or BELOW TP1 (move already done)
            #   b) Price is already ABOVE SL (setup invalidated)
            #   c) Price has fallen more than 15% below the entry candle
            #      (entry price is no longer realistic — would be chasing)
            check_price = live_price if live_price is not None else closes[-1]
            if check_price <= tp1_price * 1.02:
                continue  # price already at/near TP1 — too late
            if check_price >= sl_price:
                continue  # SL level already breached — invalid
            if check_price < entry_price * 0.85:
                continue  # price dropped >15% past entry candle — chasing

            return {
                "entry_price": float(entry_price),
                "sl_price": float(sl_price),
                "tp1_price": float(tp1_price),
                "swing_high": float(sh_price),
                "swing_low": float(min_low),
                "swing_pct": float(swing_pct),
                "entry_date": dates[j],
                "fib_level": FIB_ENTRY_LEVEL,
            }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL POSTING
# ─────────────────────────────────────────────────────────────────────────────


def post_signal(conn, symbol: str, setup: dict, live_price: float) -> None:
    """
    Writes the signal to telegram_outbox and ai_signals.
    Cornix format: identical to all other AI bots.
    """
    entry = live_price  # live price as entry (current market)
    sl = setup["sl_price"]
    tp1 = setup["tp1_price"]
    # FIX P0.6 (R4): 20x mit ~34% SL-Distanz (sl = swing_high*1.03, Entry tief
    # im Retracement) liquidiert isoliert bei ~+5% — lange vor dem SL. Hebel
    # wird deshalb gegen die SL-Distanz gecappt (ergibt hier typisch 1-2x).
    lev = cap_leverage_to_sl(get_max_leverage(symbol, 20), entry, sl)

    # Cornix plain-text (sent by 4_telegram_bot.py)
    cornix_lines = [
        f"📈 Signal for {symbol} 📈",
        "",
        "🚨 Direction: SHORT",
        f"🚨 Leverage: {lev}",
        "🚨 Margin: Cross",
        f"🏦 CMP Entry: $ {entry:.8f}",
        f"💰 TP1: $ {tp1:.8f}",
        "",
        f"💸 Stop Loss: $ {sl:.8f}",
        "",
        "🧠 UFI1 Strategy - V1",
    ]
    cornix_msg = "\n".join(cornix_lines)

    # HTML info message (posted alongside Cornix plain-text)
    swing_pct = setup["swing_pct"]
    fib_entry = setup["entry_price"]
    html_msg = (
        f"<pre><b>📉 UFI1 — Fib Inversion SHORT</b>\n"
        f"<b>{symbol.replace('USDT', '')}/USDT</b>\n"
        f"→ Swing high: ${setup['swing_high']:.8f} (−{swing_pct:.0f}% drop)\n"
        f"→ Entry fib: 0.382 retracement (${fib_entry:.8f})\n"
        f"→ Confirmation: candle closes below Fib level\n"
        f"→ TP1: ${tp1:.8f} (swing low)\n"
        f"→ SL: ${sl:.8f} (above swing high)\n\n"
        f"{cornix_msg}</pre>"
    )

    with conn.cursor() as cur:
        # 1. Cornix plain-text → sent by 4_telegram_bot.py as trade signal
        cur.execute(
            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
            (CHANNEL_ID, cornix_msg),
        )
        # 2. HTML info message with details
        cur.execute(
            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
            (CHANNEL_ID, html_msg),
        )
        # 3. ai_signals entry → 8_ai_trade_monitor.py handles lifecycle tracking
        cur.execute(
            """
            INSERT INTO ai_signals
                (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                symbol,
                float(entry),
                MODULE_NAME,
                "SHORT",
                1.0,  # rule-based = full confidence
                float(entry),
                float(entry),  # no second entry
                float(sl),
                json.dumps([float(tp1)]),
            ),
        )

    conn.commit()
    logger.info(
        f"✅ UFI1 signal posted: {symbol} SHORT @ {entry:.6f} | TP1={tp1:.6f} | SL={sl:.6f} | Swing={swing_pct:.0f}%"
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────────────────────────────────────


def scan_all_coins() -> None:
    """Scans all coins for UFI1 SHORT setups."""
    logger.info("🔍 UFI1 scan starting...")
    coins = load_coins()
    n_signals = 0
    n_cooldown = 0
    n_setup = 0

    conn = None
    try:
        conn = get_db_connection()

        for symbol in coins:
            try:
                # Check cooldown
                if check_cooldown(conn, MODULE_NAME, symbol, "SHORT", COOLDOWN_HOURS):
                    n_cooldown += 1
                    continue

                # Fetch live price first — needed for stale-setup detection
                live_price = get_live_price(conn, symbol)

                # Load daily candles
                df = load_daily_ohlcv(conn, symbol)
                if df is None:
                    continue

                # Search for setup (live_price used for stale check)
                setup = find_ufi1_setup(df, live_price)
                if setup is None:
                    continue

                n_setup += 1

                # Duplicate check: no active UFI1 trade for this coin
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM ai_signals WHERE symbol = %s AND model = %s AND direction = 'SHORT'",
                        (symbol, MODULE_NAME),
                    )
                    if cur.fetchone():
                        logger.debug(f"⏭ {symbol}: active UFI1 trade exists — skipped")
                        continue

                # Post signal (use live_price as entry, fallback to setup entry)
                effective_entry = live_price if live_price is not None else setup["entry_price"]
                post_signal(conn, symbol, setup, effective_entry)
                update_cooldown(conn, MODULE_NAME, symbol, "SHORT")
                n_signals += 1

            except Exception as e:
                logger.error(f"Error processing {symbol}: {e}", exc_info=True)
                try:
                    conn.rollback()
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"Critical scan error: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()

    logger.info(f"✅ UFI1 scan complete: {n_signals} signals | {n_setup} setups found | {n_cooldown} coins in cooldown")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    logger.info("=== 📉 UFI1 BOT STARTED ===")
    logger.info(f"   Channel:    {CHANNEL_ID}")
    logger.info(f"   Min-swing:  {MIN_SWING_PCT}%")
    logger.info(f"   Entry-Fib:  {FIB_ENTRY_LEVEL} (±{FIB_ENTRY_TOL * 100:.0f}%)")
    logger.info(f"   SL-Buffer:  {SL_BUFFER * 100:.0f}%")
    logger.info(f"   Cooldown:   {COOLDOWN_HOURS}h")

    while True:
        try:
            scan_all_coins()
        except Exception as e:
            logger.error(f"Unhandled error in main loop: {e}", exc_info=True)
        logger.info(f"💤 Next scan in {SLEEP_SECONDS // 3600}h")
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("UFI1 bot stopped manually (Ctrl+C).")

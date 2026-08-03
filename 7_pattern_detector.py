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
from scipy import stats

from core import config as _kcfg  # channel ids
from core.candles import read_candles
from core.config import TELEGRAM_CHANNELS
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.signal_post import LEG_LIVE, LEG_SHADOW, route_legacy_leg
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - PATTERN_DET - %(message)s')
logger = logging.getLogger(__name__)

# ========================= GLOBALS =========================
CHART_DIR = "generated_charts"
os.makedirs(CHART_DIR, exist_ok=True)

PATTERN_TIMEFRAMES = ['1h', '2h', '4h', '1d']
ALERTED_PATTERNS = set()
ALERTED_RETESTS = set()
ACTIVE_PATTERNS = {}
ACTIVE_PATTERNS_FILE = "active_patterns.json"


def get_coins():
    try:
        with open('coins.json') as f:
            return json.load(f)
    except Exception:
        return []


def send_to_outbox(conn, message, image_path=None):
    target_channel = TELEGRAM_CHANNELS.get('Pattern Detector')
    if not target_channel:
        logger.error("No channel for Pattern Detector found in config.py!")
        return

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
            (target_channel, message, image_path),
        )
    conn.commit()


def load_active_patterns():
    global ACTIVE_PATTERNS
    if not os.path.exists(ACTIVE_PATTERNS_FILE):
        logger.info("📂 No active_patterns.json found → starting fresh.")
        ACTIVE_PATTERNS = {}
        return

    try:
        with open(ACTIVE_PATTERNS_FILE) as f:
            data = json.load(f)

        ACTIVE_PATTERNS = {}
        for pid, info in data.items():
            info_copy = info.copy()
            if 'break_time' in info_copy and info_copy['break_time']:
                info_copy['break_time'] = pd.to_datetime(info_copy['break_time'])
            ACTIVE_PATTERNS[pid] = info_copy

        logger.info(f"✅ {len(ACTIVE_PATTERNS)} active patterns loaded from JSON.")
    except Exception as e:
        logger.error(f"❌ Error loading from active_patterns.json: {e}")
        ACTIVE_PATTERNS = {}


def save_active_patterns():
    """FIX: Atomic write via temp-file + os.replace.
    Previously, writing was done directly to the target file — with concurrent read
    by another process, the reader could see a half-written JSON file (race condition).
    Now: write temp-file completely, then atomically rename. Reader always sees
    either the old or new version, never an inconsistent intermediate state.
    """
    try:
        serializable = {}
        for pid, info in ACTIVE_PATTERNS.items():
            info_copy = info.copy()
            if 'break_time' in info_copy and isinstance(info_copy['break_time'], (pd.Timestamp, datetime)):
                info_copy['break_time'] = info_copy['break_time'].isoformat()
            serializable[pid] = info_copy

        tmp = ACTIVE_PATTERNS_FILE + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(serializable, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ACTIVE_PATTERNS_FILE)
    except Exception as e:
        logger.error(f"❌ Error saving active_patterns.json: {e}")


def generate_pattern_chart(df, symbol, tf, pattern_name, line_highs, line_lows, start_idx, current_idx):
    try:
        MAX_CANDLES = 168
        PADDING_CANDLES = 12

        start_plot = max(0, current_idx - MAX_CANDLES)
        plot_df = df.iloc[start_plot : current_idx + 1].copy()
        plot_df['open_time'] = pd.to_datetime(plot_df['open_time']).dt.tz_localize(None)
        plot_df.set_index('open_time', inplace=True)

        if len(plot_df) > 1:
            time_step = plot_df.index[-1] - plot_df.index[-2]
            future_dates = [plot_df.index[-1] + time_step * i for i in range(1, PADDING_CANDLES + 1)]
            empty_df = pd.DataFrame(np.nan, index=future_dates, columns=plot_df.columns).astype(float)
            plot_df = pd.concat([plot_df, empty_df])

        global_indices = np.arange(start_plot, start_plot + len(plot_df))
        y_highs = [float(line_highs['m'] * idx + line_highs['b']) for idx in global_indices]
        y_lows = [float(line_lows['m'] * idx + line_lows['b']) for idx in global_indices]

        mc = mpf.make_marketcolors(up='#00ff88', down='#ff4466', edge='inherit', wick='inherit')
        s = mpf.make_mpf_style(
            marketcolors=mc, base_mpf_style='nightclouds', gridaxis='horizontal', rc={'grid.alpha': 0.15}
        )

        apds = [
            mpf.make_addplot(y_highs, color='#00ffff', width=1.5, linestyle='-'),
            mpf.make_addplot(y_lows, color='#ffd700', width=1.5, linestyle='-'),
        ]

        filename = f"{CHART_DIR}/{symbol}_{tf}_{pattern_name.replace(' ', '_')}_{int(time.time())}.png"

        mpf.plot(
            plot_df,
            type='candle',
            style=s,
            volume=True,
            addplot=apds,
            title=f"\n{pattern_name}: {symbol.replace('USDT', '')} ({tf}) | Last 7 Days",
            figsize=(12, 8),
            tight_layout=True,
            savefig=filename,
            returnfig=False,
        )
        return filename
    except Exception as e:
        logger.error(f"Error generating chart for {symbol}: {e}")
        return None
    finally:
        # Closes the figure left open by mpf.plot — prevents RAM leak.
        plt.close('all')


def process_ai_trade(conn, symbol, direction, module, live_price, chart_path=None):
    # Cooldown time per module (pattern breakouts have longer validity than
    # fast intraday signals). Module tags: BR1H, BR2H, BR4H, BR1D, ABR1, RUB1.
    cd_hours_map = {
        'BR1H': 6,
        'BR1Hv2': 6,  # Versioning rule 2026-07-06: gate revert, new generation
        'BR2H': 12,
        'BR4H': 24,
        'BR1D': 72,
        'ABR1': 6,
        'ABR2': 6,  # Versioning rule 2026-07-06: new generation, same cooldown
        'RUB1': 4,
        'RUB2': 4,
    }
    cd_hours = cd_hours_map.get(module, 6)

    # check_cooldown returns True when the cooldown is still active (trade blocked).
    if check_cooldown(conn, module, symbol, direction, cd_hours):
        logger.info(f"⏳ Cooldown active for {symbol} ({module} {direction}). Ignoring signal.")
        return
    trade_setup = calculate_smart_targets(conn, symbol, direction, live_price)

    entry1 = trade_setup['entry1']
    entry2 = trade_setup['entry2']
    sl = trade_setup['sl']
    targets = trade_setup['targets']

    lev = get_max_leverage(symbol, 20)

    # T-2026-KYT-9050-033 (audit T-032): fleet lifecycle gate. Default LIVE ⇒ no
    # behaviour change until a (module, direction) leg is in the shadow_gate
    # register. BR SHORT legs (2h/4h) + BR1D/BR1Hv2 (both) are parked → SHADOW:
    # monitored trade (ai_signals) instead of Cornix. Purely additive on the post
    # branch (rule 4: no double post/orphan). BR posts carry confidence=1.0 (no model);
    # ai_signals stores the full target list → n_show=len(targets) mirrors that.
    _route = route_legacy_leg(conn, module, direction, symbol, 1.0, entry1, entry2, sl, targets, n_show=len(targets))
    if _route != LEG_LIVE:
        if _route == LEG_SHADOW:
            conn.commit()
        return

    # Build Cornix message
    lines = [
        f"📈 Signal for {symbol} 📈",
        f"🚨 Direction: {direction}",
        f"🚨 Leverage: {lev}",
        "🚨 Margin: Cross",
        f"🏦 CMP Entry: $ {entry1:.8f}",
        # T-2026-KYT-9050-042: entry2 is still computed and stored, but no longer
        # published — the fleet trades single-entry (arm B). See core/signal_post.py.
    ]
    for i, target in enumerate(targets, 1):
        lines.append(f"💰 TP{i}: $ {target:.8f}")
    lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module} V3"]
    cornix_msg = "\n".join(lines)

    # Dynamic channel selection
    if module.startswith('BR'):
        target_channel = _kcfg.CH_PATTERN_BR
    elif module.startswith('ABR'):
        target_channel = _kcfg.CH_ABR1
    elif module.startswith('RUB'):
        target_channel = _kcfg.CH_DISABLED
    else:
        target_channel = _kcfg.CH_PUMP_AI

    # Telegram Outbox
    # FIX double post (operator report 2026-07-06, same error class as
    # 18_ai_abr1_bot): chart caption without an embedded Cornix block — Cornix
    # otherwise parsed BOTH messages as standalone signals.
    html_caption = f"<b>🚀 AI {module} {direction} SIGNAL</b>\n<b>{symbol.replace('USDT', '')}</b>\n→ Direction: {direction}\n→ Confidence: <b>Retest done</b>"

    with conn.cursor() as cur:
        cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (target_channel, cornix_msg))
        if chart_path:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                (target_channel, html_caption, chart_path),
            )
        else:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (target_channel, html_caption)
            )

        # DB for the monitor
        cur.execute(
            """
                INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                symbol,
                float(entry1),
                module,
                direction,
                1.0,
                float(entry1),
                float(entry2),
                float(sl),
                json.dumps(targets),
            ),
        )

    conn.commit()
    update_cooldown(conn, module, symbol, direction)
    logger.info(f"✅ AI trade for {symbol} generated and written to DB (ai_signals & outbox).")


def analyze_patterns(current_hour):
    conn = get_db_connection()
    try:
        coins = get_coins()
        for symbol in coins:
            for tf in PATTERN_TIMEFRAMES:
                if tf == '2h' and current_hour % 2 != 0:
                    continue
                if tf == '4h' and current_hour % 4 != 0:
                    continue
                if tf == '1d' and current_hour != 0:
                    continue

                try:
                    # R1: detection on CLOSED candles (include_forming=False).
                    # core.candles delivers ASC → the previous DESC reversal is gone.
                    # The breakout candle was already iloc[-2] (closed); without the
                    # forming candle it is now the last row (current_idx below
                    # = len(df) - 1). `iloc[:-4]` stays: the pivot confirm buffer
                    # (rolling(9,center) needs 4 candles to the right) only loses the
                    # previous forming repaint at the edge.
                    df = read_candles(
                        conn,
                        symbol,
                        tf,
                        limit=168,
                        include_forming=False,
                        columns=("open_time", "open", "high", "low", "close", "volume"),
                    )
                    if len(df) < 50:
                        continue

                    # 1. Find pivots
                    df['Pivot_High'] = df['high'] == df['high'].rolling(window=9, center=True).max()
                    df['Pivot_Low'] = df['low'] == df['low'].rolling(window=9, center=True).min()
                    confirmed_df = df.iloc[:-4]
                    highs = confirmed_df[confirmed_df['Pivot_High']]
                    lows = confirmed_df[confirmed_df['Pivot_Low']]

                    if len(highs) >= 2 and len(lows) >= 2:
                        recent_highs = highs.tail(3)
                        recent_lows = lows.tail(3)
                        slope_h, intercept_h, _, _, _ = stats.linregress(recent_highs.index, recent_highs['high'])
                        slope_l, intercept_l, _, _, _ = stats.linregress(recent_lows.index, recent_lows['low'])

                        avg_price = df['close'].mean()
                        m_high_pct = (slope_h / avg_price) * 100
                        m_low_pct = (slope_l / avg_price) * 100

                        pattern_name = None
                        flat_th = 0.02
                        if m_high_pct < -flat_th and m_low_pct > flat_th:
                            pattern_name = "Symmetrical Triangle"
                        elif abs(m_high_pct) <= flat_th and m_low_pct > flat_th:
                            pattern_name = "Ascending Triangle"
                        elif m_high_pct < -flat_th and abs(m_low_pct) <= flat_th:
                            pattern_name = "Descending Triangle"
                        elif m_high_pct > flat_th and m_low_pct > flat_th and abs(m_high_pct - m_low_pct) < 0.05:
                            pattern_name = "Ascending Channel"
                        elif m_high_pct < -flat_th and m_low_pct < -flat_th and abs(m_high_pct - m_low_pct) < 0.05:
                            pattern_name = "Descending Channel"

                        if pattern_name:
                            # R1: forming candle is no longer in the frame → the most
                            # recent closed (breakout candle) is len(df) - 1 (previously -2).
                            current_idx = len(df) - 1
                            prev_idx = current_idx - 1

                            c_open = df['open'].iloc[current_idx]
                            c_close = df['close'].iloc[current_idx]
                            c_low = df['low'].iloc[current_idx]
                            c_high = df['high'].iloc[current_idx]
                            p_close = df['close'].iloc[prev_idx]

                            up_curr = slope_h * current_idx + intercept_h
                            low_curr = slope_l * current_idx + intercept_l
                            up_prev = slope_h * prev_idx + intercept_h
                            low_prev = slope_l * prev_idx + intercept_l

                            pivot_time_str = pd.to_datetime(df['open_time'].iloc[recent_highs.index[-1]]).strftime(
                                '%Y%m%d%H%M'
                            )
                            pattern_id = f"{symbol}_{tf}_{pattern_name}_{pivot_time_str}"

                            line_highs_dict = {'m': slope_h, 'b': intercept_h}
                            line_lows_dict = {'m': slope_l, 'b': intercept_l}
                            start_plot_idx = min(recent_highs.index[0], recent_lows.index[0])

                            breakout_dir = None
                            if c_close > up_curr and p_close <= up_prev:
                                breakout_dir = "BULLISH BREAKOUT 🟢"
                            elif c_close < low_curr and p_close >= low_prev:
                                breakout_dir = "BEARISH BREAKOUT 🔴"

                            if breakout_dir:
                                if pattern_id not in ALERTED_PATTERNS:
                                    ALERTED_PATTERNS.add(pattern_id)
                                    ACTIVE_PATTERNS[pattern_id] = {
                                        "direction": breakout_dir,
                                        "break_time": df['open_time'].iloc[current_idx],
                                        "break_idx": current_idx,
                                        "retest_occurred": False,
                                    }
                                    chart_path = generate_pattern_chart(
                                        df,
                                        symbol,
                                        tf,
                                        pattern_name,
                                        line_highs_dict,
                                        line_lows_dict,
                                        start_plot_idx,
                                        current_idx,
                                    )
                                    msg = f"<b>📐 PATTERN BREAKOUT</b>\n<b>{symbol.replace('USDT', '')} | {tf} Chart</b>\n→ Pattern: {pattern_name}\n→ Action: {breakout_dir}\n→ Breakout Price: <code>${c_close:,.4f}</code>\n<i>Waiting for retest...</i>"
                                    send_to_outbox(conn, msg, chart_path)
                                    logger.info(f"🚀 {breakout_dir} detected for {symbol} ({pattern_name})!")

                            elif pattern_id in ACTIVE_PATTERNS:
                                tracked = ACTIVE_PATTERNS[pattern_id]
                                is_bullish = "BULLISH" in tracked["direction"]
                                line = up_curr if is_bullish else low_curr
                                break_idx = tracked.get("break_idx", current_idx - 5)

                                candles_since_break = current_idx - break_idx
                                max_candles = {'1h': 30, '2h': 20, '4h': 15, '1d': 8}.get(tf, 20)

                                if candles_since_break > max_candles:
                                    del ACTIVE_PATTERNS[pattern_id]
                                    continue

                                touch_tol = 0.008
                                fail_tol = 0.022
                                min_body_pct = 0.004
                                retest_key = f"{pattern_id}_retest"

                                # 1. Fakeout
                                if (is_bullish and c_close < line * (1 - fail_tol)) or (
                                    not is_bullish and c_close > line * (1 + fail_tol)
                                ):
                                    if retest_key not in ALERTED_RETESTS:
                                        ALERTED_RETESTS.add(retest_key)
                                        chart_path = generate_pattern_chart(
                                            df,
                                            symbol,
                                            tf,
                                            pattern_name,
                                            line_highs_dict,
                                            line_lows_dict,
                                            start_plot_idx,
                                            current_idx,
                                        )
                                        msg = f"<b>❌ FAKEOUT RETEST</b>\n<b>{symbol.replace('USDT', '')} | {tf}</b>\n→ Pattern: {pattern_name}\n→ Deep break back into pattern!"
                                        send_to_outbox(conn, msg, chart_path)
                                    del ACTIVE_PATTERNS[pattern_id]
                                    continue

                                # 2. Retest-Touch + Successful?
                                touched = (is_bullish and c_low <= line * (1 + touch_tol)) or (
                                    not is_bullish and c_high >= line * (1 - touch_tol)
                                )
                                closed_correct = (is_bullish and c_close > line) or (not is_bullish and c_close < line)

                                if touched and closed_correct:
                                    if not tracked.get("retest_occurred", False):
                                        tracked["retest_occurred"] = True
                                        tracked["retest_price"] = float((c_high + c_low) / 2)
                                        tracked["retest_idx"] = current_idx
                                        ALERTED_RETESTS.add(retest_key)

                                        chart_path = generate_pattern_chart(
                                            df,
                                            symbol,
                                            tf,
                                            pattern_name,
                                            line_highs_dict,
                                            line_lows_dict,
                                            start_plot_idx,
                                            current_idx,
                                        )
                                        msg = f"<b>🔄 RETEST DETECTED 📍</b>\n<b>{symbol.replace('USDT', '')} | {tf}</b>\n→ Pattern: {pattern_name}\n→ Retest at <code>${tracked['retest_price']:.4f}</code>\n<i>Waiting for strong confirmation...</i>"
                                        send_to_outbox(conn, msg, chart_path)
                                        logger.info(
                                            f"🔄 Retest DETECTED {symbol} {tf} @ ${tracked['retest_price']:.4f}"
                                        )

                                    # Strong confirmation?
                                    body_pct = abs(c_close - c_open) / c_open
                                    strong_bull = c_close > c_open and body_pct >= min_body_pct
                                    strong_bear = c_close < c_open and body_pct >= min_body_pct

                                    if (is_bullish and strong_bull) or (not is_bullish and strong_bear):
                                        chart_path = generate_pattern_chart(
                                            df,
                                            symbol,
                                            tf,
                                            pattern_name,
                                            line_highs_dict,
                                            line_lows_dict,
                                            start_plot_idx,
                                            current_idx,
                                        )
                                        direction = "LONG" if is_bullish else "SHORT"
                                        module_name = f"BR{tf.upper()}"
                                        # direction gate REMOVED (operator 2026-07-06): both
                                        # directions run again; BR1H posts as BR1Hv2
                                        # (versioning rule), until the planned ML gate sits
                                        # above the BR signals.
                                        if module_name == 'BR1H':
                                            module_name = 'BR1Hv2'
                                        process_ai_trade(conn, symbol, direction, module_name, c_close, chart_path)
                                        logger.info(f"✅ SUCCESSFUL RETEST + TRADE triggered {symbol} {tf}")
                                        del ACTIVE_PATTERNS[pattern_id]
                                        continue

                except Exception as e:
                    logger.error(f"Error for {symbol} ({tf}): {e}")

        save_active_patterns()
    finally:
        conn.close()


def main():
    logger.info("=== PATTERN DETECTOR (1h+) STARTED ===")
    load_active_patterns()

    while True:
        now = datetime.now(timezone.utc)

        if now.minute == 3:
            current_hour = now.hour
            logger.info(f"⏰ Time trigger reached! Hour: {current_hour} UTC")

            analyze_patterns(current_hour)

            logger.info("🏁 Pattern scan stopped. Sleeping for 60 seconds...")
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")

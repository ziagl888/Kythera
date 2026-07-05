import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import time
import logging
import pandas as pd
import numpy as np
import scipy.signal
import requests
import json
import websocket
import threading

from core.database import get_db_connection
from core.market_utils import calculate_pivots

logging.basicConfig(level=logging.INFO, format='%(asctime)s - PIVOT_SIMULATOR - %(message)s')
logger = logging.getLogger(__name__)

from core import config as _kcfg  # channel ids

# 🛠️ PAPER TRADING KONFIGURATION
CHANNEL_ID = _kcfg.CH_PAPER
SYMBOL = 'BTCUSDT'

# Kapital-Simulation
CAPITAL = 100000.0
TRADE_MARGIN = 100.0  # Wir riskieren 100 USDT pro Trade
LEVERAGE = 100  # 100x Hebel -> 10.000 USDT Nominal-Positionsgröße
TAKER_FEE = 0.0004  # 0.04% pro Trade (Ein- und Ausstieg)

ACTIVE_TRADES = {}
TRADE_COUNTER = 1
COOLDOWNS = {}

# --- DIE GEWINNER-PARAMETER AUS DEM BACKTEST ---
EMA_PERIOD = 21
SL_PCT = 0.002  # 0.4% Stop-Loss
MIN_RR_RATIO = 0.9  # Minimum Risk-Reward
MAX_PIVOT_AGE = 120  # Keine Asbach-Uralt-Ziele
MAX_FVG_AGE = 48  # FVG muss innerhalb von 48 Kerzen gefüllt werden


# 📊 DATENBESCHAFFUNG (LIVE VON BINANCE)
def fetch_klines(symbol, interval, limit=300):
    try:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
        res = requests.get(url, timeout=5)
        res.raise_for_status()
        data = res.json()

        df = pd.DataFrame(data, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'
        ])
        for c in ['open', 'high', 'low', 'close']:
            df[c] = df[c].astype(float)

        return df.iloc[:-1].reset_index(drop=True)
    except Exception as e:
        logger.error(f"Fehler beim Laden der Klines für {interval}: {e}")
        return pd.DataFrame()


# 🧠 TRADINGVIEW PIVOT POINTS

def check_cooldown(strategy_name):
    last_time = COOLDOWNS.get(strategy_name, 0)
    if time.time() - last_time < 100:
        return False
    COOLDOWNS[strategy_name] = time.time()
    return True


# 💼 PAPER TRADING ENGINE
def execute_trade(direction, price, sl, target, strategy, rr):
    global CAPITAL, TRADE_COUNTER, ACTIVE_TRADES

    trade_id = f"#{TRADE_COUNTER:04d}"
    TRADE_COUNTER += 1

    ACTIVE_TRADES[trade_id] = {
        'direction': direction,
        'entry': price,
        'sl': sl,
        'tp': target,
        'strategy': strategy
    }

    color = "🟢" if direction == "LONG" else "🔴"
    msg = f"""<pre style="background:#1e1e1e; color:#ffffff; padding:10px; border-left:6px solid {'#00ff00' if direction == 'LONG' else '#ff0000'};">
{color} <b>OPEN {direction}</b> {int(TRADE_MARGIN)} USDT BTCUSDT.P
<b>TRADE_ID:</b> {trade_id}
<b>STRATEGY:</b> {strategy}
<b>ENTRY:</b> {price:.2f}
<b>SL:</b> {sl:.2f} | <b>TP:</b> {target:.2f}
<b>R:R RATIO:</b> 1 : {rr:.2f}
<b>KAPITAL:</b> {CAPITAL:,.2f} USDT
</pre>"""

    logger.info(f"OPEN {trade_id} | {direction} | Entry: {price} | R:R: {rr:.2f} | Strategy: {strategy}")
    send_telegram(msg)


def monitor_live_trades(live_price):
    global CAPITAL, ACTIVE_TRADES

    trades_to_close = []

    for tid, trade in ACTIVE_TRADES.items():
        direction = trade['direction']
        sl = trade['sl']
        tp = trade['tp']
        entry = trade['entry']

        is_closed = False
        reason = ""

        if direction == "LONG":
            if live_price <= sl:
                is_closed, reason = True, "SL HIT 🛑"
            elif live_price >= tp:
                is_closed, reason = True, "TARGET HIT 🎯"
        elif direction == "SHORT":
            if live_price >= sl:
                is_closed, reason = True, "SL HIT 🛑"
            elif live_price <= tp:
                is_closed, reason = True, "TARGET HIT 🎯"

        if is_closed:
            nominal_size = TRADE_MARGIN * LEVERAGE
            qty = nominal_size / entry

            if direction == "LONG":
                raw_pnl = (live_price - entry) * qty
            else:
                raw_pnl = (entry - live_price) * qty

            fee = nominal_size * TAKER_FEE * 2
            net_pnl = raw_pnl - fee

            CAPITAL += net_pnl

            emoji = "🤑" if net_pnl > 0 else "🩸"
            msg = f"""<pre style="background:#1e1e1e; color:#ffffff; padding:10px; border-left:6px solid {'#00ff00' if net_pnl > 0 else '#ff0000'};">
🏁 <b>CLOSE {direction}</b> {int(TRADE_MARGIN)} USDT BTCUSDT.P
<b>TRADE_ID:</b> {tid}
<b>STRATEGY:</b> {trade['strategy']}
<b>REASON:</b> {reason} @ {live_price:.2f}
<b>NET PNL:</b> {net_pnl:+.2f} USDT {emoji}
<b>KAPITAL:</b> {CAPITAL:,.2f} USDT
</pre>"""

            logger.info(f"CLOSE {tid} | {reason} | PnL: {net_pnl:+.2f} | Kapital: {CAPITAL:,.2f}")
            send_telegram(msg)
            trades_to_close.append(tid)

    for tid in trades_to_close:
        del ACTIVE_TRADES[tid]


# 🎯 PIVOT RETEST STRATEGIE (MIT BACKTEST-REGELN)
def run_smc_analysis(tf):
    df = fetch_klines(SYMBOL, tf, 300)
    if df.empty or len(df) < 50: return

    # 1. EMA 21 berechnen
    df['ema21'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    curr_ema = df['ema21'].iloc[-1]

    supports, resistances = calculate_pivots(df, window=5)

    curr_idx = len(df) - 1
    curr_candle = df.iloc[curr_idx]
    curr_low = curr_candle['low']
    curr_high = curr_candle['high']
    curr_price = curr_candle['close']

    search_start = max(2, curr_idx - MAX_FVG_AGE)

    def is_touching_pivot_local(price, pivots, max_idx, threshold=0.001):
        for p_idx, p_val in reversed(pivots):
            if p_idx > max_idx - 5: continue
            if p_idx < max_idx - MAX_PIVOT_AGE: break
            if abs(price - p_val) / p_val <= threshold:
                return True
        return False

    # 🟢 LONG SETUP PRÜFEN
    if curr_price > curr_ema:
        for i in range(search_start, curr_idx):
            if df['high'].iloc[i - 2] < df['low'].iloc[i] and df['close'].iloc[i - 1] > df['open'].iloc[i - 1]:
                gap_bottom = df['high'].iloc[i - 2]
                candle_1_low = df['low'].iloc[i - 2]

                if is_touching_pivot_local(candle_1_low, supports, i - 2):
                    was_closed_before = any(df['low'].iloc[j] <= gap_bottom for j in range(i + 1, curr_idx))

                    if not was_closed_before and curr_low <= gap_bottom:
                        valid_res = [val for p_idx, val in resistances if
                                     curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val > curr_price]

                        if valid_res:
                            target = min(valid_res)
                            sl = curr_low * (1.0 - SL_PCT)  # 0.4% Puffer
                            risk = curr_price - sl
                            reward = target - curr_price

                            if risk > 0 and (reward / risk) >= MIN_RR_RATIO:
                                strategy_name = f"PIVOT_{tf}_FVG_CLOSED_LONG"
                                if check_cooldown(strategy_name):
                                    execute_trade("LONG", curr_price, sl, target, strategy_name, (reward / risk))
                                    return  # Nur 1 Trade pro Durchlauf

    # 🔴 SHORT SETUP PRÜFEN
    if curr_price < curr_ema:
        for i in range(search_start, curr_idx):
            if df['low'].iloc[i - 2] > df['high'].iloc[i] and df['close'].iloc[i - 1] < df['open'].iloc[i - 1]:
                gap_top = df['low'].iloc[i - 2]
                candle_1_high = df['high'].iloc[i - 2]

                if is_touching_pivot_local(candle_1_high, resistances, i - 2):
                    was_closed_before = any(df['high'].iloc[j] >= gap_top for j in range(i + 1, curr_idx))

                    if not was_closed_before and curr_high >= gap_top:
                        valid_sup = [val for p_idx, val in supports if
                                     curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val < curr_price]

                        if valid_sup:
                            target = max(valid_sup)
                            sl = curr_high * (1.0 + SL_PCT)  # 0.4% Puffer
                            risk = sl - curr_price
                            reward = curr_price - target

                            if risk > 0 and (reward / risk) >= MIN_RR_RATIO:
                                strategy_name = f"PIVOT_{tf}_FVG_CLOSED_SHORT"
                                if check_cooldown(strategy_name):
                                    execute_trade("SHORT", curr_price, sl, target, strategy_name, (reward / risk))
                                    return


# 🌐 WEBSOCKET VERBINDUNG
def on_message(ws, message):
    try:
        data = json.loads(message)
        if 'data' not in data: return

        stream_name = data['stream']
        kline = data['data']['k']

        live_price = float(kline['c'])
        is_closed = kline['x']

        monitor_live_trades(live_price)

        if is_closed:
            if '1m' in stream_name:
                threading.Thread(target=run_smc_analysis, args=('1m',)).start()
            if '5m' in stream_name:
                threading.Thread(target=run_smc_analysis, args=('5m',)).start()

    except Exception as e:
        logger.error(f"Websocket Message Fehler: {e}")


def on_error(ws, error):
    logger.error(f"WebSocket Fehler: {error}")


def on_close(ws, close_status_code, close_msg):
    logger.warning("WebSocket geschlossen. Verbinde neu...")
    time.sleep(5)
    start_websocket()


def on_open(ws):
    logger.info("✅ Binance WebSocket verbunden!")
    send_telegram(
        "🚀 <b>PIVOT FVG SIMULATOR (SNIPER EDITION) GESTARTET</b>\nEMA21 | 0.4% SL | R:R 1.25\nPairs: BTCUSDT | TF: 1m, 5m")


def start_websocket():
    ws_url = "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_1m/btcusdt@kline_5m"
    ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    ws.run_forever()


if __name__ == "__main__":
    try:
        start_websocket()
    except KeyboardInterrupt:
        logger.info("Simulation beendet.")
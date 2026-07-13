import warnings

warnings.filterwarnings("ignore")

import time
import os
import json
import pandas as pd
import numpy as np
import scipy.signal
import logging

# --- Eigene DB Connection importieren ---
from core.candles import read_candles
from core.database import get_db_connection
from core import config as _kcfg  # channel ids

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 🛠️ BACKTEST KONFIGURATION & FILTER
# ==========================================
TIMEFRAME = '1h'
CHANNEL_ID = _kcfg.CH_MAYANK  # Deine SMC Telegram Outbox (bitte anpassen falls nötig)
OUTPUT_FILE = "mass_backtest_results.txt"

START_CAPITAL = 100000.0
TRADE_MARGIN = 1000.0  # Fester Einsatz für diesen Massentest
LEVERAGE = 100  # Hebel (100x)
TAKER_FEE = 0.0004  # 0.04% Handelsgebühr pro Richtung (0.08% total)

# --- DIE ANGEPASSTEN SMC FILTER ---
EMA_PERIOD = 21
MAX_PIVOT_AGE = 120
MAX_FVG_AGE = 48
MIN_RR_RATIO = 1.5
SL_PCT = 0.004  # <--- NEU: 0.4% Stop-Loss (mehr Luft für Krypto-Wicks)


# ==========================================
# 📡 TELEGRAM & FILE HELPERS
# ==========================================
def send_to_outbox(symbol, trades, win_rate, pnl, max_dd):
    try:
        color = "#00ff88" if pnl > 0 else "#ff4466"
        emoji = "🤑" if pnl > 0 else "🩸"
        msg = f"""<pre style="background:#1e1e1e; color:#ffffff; padding:10px; border-left:6px solid {color};">
<b>📊 SMC MASS BACKTEST: {symbol}</b>
<b>Timeframe:</b> {TIMEFRAME} | <b>Margin:</b> ${TRADE_MARGIN:,.0f}
<b>Stop-Loss:</b> {SL_PCT * 100:.1f}%

<b>Trades:</b> {trades}
<b>Win-Rate:</b> {win_rate:.2f}%
<b>Max DD:</b> {max_dd:.2f}%
<b>Net PnL:</b> ${pnl:+,.2f} {emoji}
</pre>"""
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (CHANNEL_ID, msg))
            conn.commit()
    except Exception as e:
        logger.error(f"Outbox error bei {symbol}: {e}")


def write_to_file(line):
    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ==========================================
# 📊 DATA FETCHING (LOKALE DATENBANK)
# ==========================================
def fetch_db_data(symbol):
    try:
        conn = get_db_connection()
        # Über core.candles: GESCHLOSSENE Kerzen, ASC (include_forming=False).
        df = read_candles(
            conn, symbol, TIMEFRAME, include_forming=False, columns=('open_time', 'open', 'high', 'low', 'close')
        )
        conn.close()

        for c in ['open', 'high', 'low', 'close']: df[c] = df[c].astype(float)
        df.dropna(inplace=True)
        return df.reset_index(drop=True)
    except Exception:
        # Tabelle existiert nicht oder ist leer
        return pd.DataFrame()


# ==========================================
# 🚀 TURBO BACKTEST ENGINE
# ==========================================
def run_simulation(symbol, df):
    capital = START_CAPITAL

    highs = df['high'].values
    lows = df['low'].values
    opens = df['open'].values
    closes = df['close'].values

    # EMA berechnen
    ema_values = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean().values

    # Pivot Punkte im Voraus berechnen
    peak_idx = scipy.signal.argrelextrema(highs, np.greater, order=5)[0]
    trough_idx = scipy.signal.argrelextrema(lows, np.less, order=5)[0]
    resistances = [(int(idx), float(highs[idx])) for idx in peak_idx]
    supports = [(int(idx), float(lows[idx])) for idx in trough_idx]

    active_trades = []
    active_bull_fvgs = []
    active_bear_fvgs = []

    wins = 0
    losses = 0
    max_capital = capital
    max_drawdown = 0.0

    for curr_idx in range(50, len(df)):
        curr_low = lows[curr_idx]
        curr_high = highs[curr_idx]
        curr_price = closes[curr_idx]

        # -------------------------------------------------
        # 1. AKTIVE TRADES PRÜFEN (SL & TP)
        # -------------------------------------------------
        trades_to_remove = []
        for trade in active_trades:
            direction = trade['direction']
            entry = trade['entry']
            sl = trade['sl']
            tp = trade['tp']

            is_closed = False

            if direction == "LONG":
                if curr_low <= sl:
                    is_closed, exit_price = True, sl
                elif curr_high >= tp:
                    is_closed, exit_price = True, tp
            elif direction == "SHORT":
                if curr_high >= sl:
                    is_closed, exit_price = True, sl
                elif curr_low <= tp:
                    is_closed, exit_price = True, tp

            if is_closed:
                nominal_size = TRADE_MARGIN * LEVERAGE
                qty = nominal_size / entry

                if direction == "LONG":
                    raw_pnl = (exit_price - entry) * qty
                else:
                    raw_pnl = (entry - exit_price) * qty

                fee = nominal_size * TAKER_FEE * 2
                net_pnl = raw_pnl - fee

                capital += net_pnl

                if net_pnl > 0:
                    wins += 1
                else:
                    losses += 1

                if capital > max_capital: max_capital = capital
                drawdown = (max_capital - capital) / max_capital * 100
                if drawdown > max_drawdown: max_drawdown = drawdown

                trades_to_remove.append(trade)

        for t in trades_to_remove:
            active_trades.remove(t)

        # -------------------------------------------------
        # 2. NEUE FVGs ERKENNEN
        # -------------------------------------------------
        c = curr_idx - 1

        def is_touching_pivot_local(price, pivots, max_idx, threshold=0.001):
            for p_idx, p_val in reversed(pivots):
                if p_idx > max_idx - 5: continue
                if p_idx < max_idx - MAX_PIVOT_AGE: break
                if abs(price - p_val) / p_val <= threshold:
                    return True
            return False

        if highs[c - 2] < lows[c] and closes[c - 1] > opens[c - 1]:
            candle_1_low = lows[c - 2]
            if is_touching_pivot_local(candle_1_low, supports, c - 2):
                active_bull_fvgs.append({'top': lows[c], 'bottom': highs[c - 2], 'created_at': c})

        if lows[c - 2] > highs[c] and closes[c - 1] < opens[c - 1]:
            candle_1_high = highs[c - 2]
            if is_touching_pivot_local(candle_1_high, resistances, c - 2):
                active_bear_fvgs.append({'top': lows[c - 2], 'bottom': highs[c], 'created_at': c})

        # -------------------------------------------------
        # 3. FVGs SCHLIESSEN & TRADES AUSLÖSEN
        # -------------------------------------------------
        surviving_bull_fvgs = []
        for fvg in active_bull_fvgs:
            if curr_idx - fvg['created_at'] > MAX_FVG_AGE: continue

            if curr_low <= fvg['bottom']:
                valid_res = [val for p_idx, val in resistances if
                             curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val > curr_price]

                if valid_res:
                    target = min(valid_res)
                    sl = curr_low * (1.0 - SL_PCT)  # NEU: Variabler, breiterer SL
                    risk = curr_price - sl
                    reward = target - curr_price

                    if risk > 0 and (reward / risk) >= MIN_RR_RATIO:
                        if curr_price > ema_values[curr_idx]:
                            active_trades.append({'direction': 'LONG', 'entry': curr_price, 'sl': sl, 'tp': target})
            else:
                surviving_bull_fvgs.append(fvg)
        active_bull_fvgs = surviving_bull_fvgs

        surviving_bear_fvgs = []
        for fvg in active_bear_fvgs:
            if curr_idx - fvg['created_at'] > MAX_FVG_AGE: continue

            if curr_high >= fvg['top']:
                valid_sup = [val for p_idx, val in supports if
                             curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val < curr_price]

                if valid_sup:
                    target = max(valid_sup)
                    sl = curr_high * (1.0 + SL_PCT)  # NEU: Variabler, breiterer SL
                    risk = sl - curr_price
                    reward = curr_price - target

                    if risk > 0 and (reward / risk) >= MIN_RR_RATIO:
                        if curr_price < ema_values[curr_idx]:
                            active_trades.append({'direction': 'SHORT', 'entry': curr_price, 'sl': sl, 'tp': target})
            else:
                surviving_bear_fvgs.append(fvg)
        active_bear_fvgs = surviving_bear_fvgs

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    net_pnl = capital - START_CAPITAL

    return total_trades, win_rate, net_pnl, max_drawdown


def main():
    try:
        with open('coins.json', 'r') as f:
            coins = json.load(f)
    except Exception as e:
        logger.error(f"Could not load coins.json: {e}")
        return

    # Datei initialisieren (Überschreibt alte Ergebnisse)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("=" * 75 + "\n")
        f.write(f"📊 SMC MASS BACKTEST | Margin: ${TRADE_MARGIN:,.0f} | SL: {SL_PCT * 100}%\n")
        f.write("=" * 75 + "\n")
        f.write(f"{'Coin':<15} | {'Trades':<8} | {'Win Rate':<10} | {'Max DD':<8} | {'Netto PnL':<12}\n")
        f.write("-" * 75 + "\n")

    logger.info(f"🚀 Starting Massen-Backtest für {len(coins)} Coins. Margin: ${TRADE_MARGIN:,.0f} | SL: {SL_PCT * 100}%")

    total_pnl = 0.0
    processed = 0

    for symbol in coins:
        df = fetch_db_data(symbol)
        if df.empty or len(df) < 200:
            logger.warning(f"⏩ Skipping {symbol} (Insufficient data in DB)")
            continue

        trades, win_rate, pnl, max_dd = run_simulation(symbol, df)

        # Nur loggen/senden, wenn der Coin überhaupt Trades generiert hat
        if trades > 0:
            pnl_str = f"${pnl:+,.2f}"
            log_line = f"{symbol:<15} | {trades:<8} | {win_rate:>5.2f} %   | {max_dd:>5.2f} % | {pnl_str}"
            print(log_line)

            # 1. In die TXT schreiben
            write_to_file(log_line)

            # 2. Live in die Telegram Outbox schicken
            #send_to_outbox(symbol, trades, win_rate, pnl, max_dd)

            total_pnl += pnl

        processed += 1
        time.sleep(0.1)  # Kurze Pause, um die DB/CPU nicht zu überlasten

    # Abschluss-Statistik
    summary = "-" * 75 + f"\nGESAMT PNL ({processed} Coins getestet): ${total_pnl:+,.2f}\n" + "=" * 75
    write_to_file(summary)
    print(summary)

    # Letzte Nachricht an Telegram
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                            (CHANNEL_ID,
                             f"🏁 <b>Massen-Backtest completed!</b>\nGesamt-Profit: <b>${total_pnl:+,.2f}</b>"))
            conn.commit()
    except:
        pass


if __name__ == "__main__":
    main()
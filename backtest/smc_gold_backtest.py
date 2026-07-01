import warnings

warnings.filterwarnings("ignore")

import time
import pandas as pd
import numpy as np
import scipy.signal
import yfinance as yf
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 🛠️ BACKTEST KONFIGURATION & FILTER
# ==========================================
TICKER = 'GC=F'  # Gold Futures
TIMEFRAME = '1h'
PERIOD = '730d'  # 2 Jahre Historie

START_CAPITAL = 100000.0
TRADE_MARGIN = 100.0  # Einsatz pro Trade
LEVERAGE = 100  # Hebel
TAKER_FEE = 0.0004  # 0.04% Handelsgebühr pro Richtung (0.08% total)

# --- DIE SMC FILTER ---
MAX_PIVOT_AGE = 120
MAX_FVG_AGE = 48
MIN_RR_RATIO = 1.5

# Wir testen diese Varianten aftereinander durch (None = Ohne Filter)
EMA_VARIANTS = [None, 21, 55, 200]


# ==========================================
# 📊 DATA FETCHING
# ==========================================
def fetch_data():
    logger.info(f"Loading historical {TIMEFRAME} Daten for {TICKER}...")
    df = yf.download(TICKER, interval=TIMEFRAME, period=PERIOD, progress=False)

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.reset_index()
    col_map = {'Datetime': 'open_time', 'Date': 'open_time', 'Open': 'open', 'High': 'high', 'Low': 'low',
               'Close': 'close'}
    df.rename(columns=col_map, inplace=True)

    for c in ['open', 'high', 'low', 'close']:
        df[c] = df[c].astype(float)

    df.dropna(inplace=True)
    return df.reset_index(drop=True)


# ==========================================
# 🚀 TURBO BACKTEST ENGINE
# ==========================================
def run_simulation(df, ema_period):
    capital = START_CAPITAL

    # Rohe Numpy-Arrays für maximale Geschwindigkeit
    highs = df['high'].values
    lows = df['low'].values
    opens = df['open'].values
    closes = df['close'].values

    # EMA berechnen, falls gewünscht
    if ema_period:
        ema_values = df['close'].ewm(span=ema_period, adjust=False).mean().values
    else:
        ema_values = None

    # Pivot Punkte EINMAL im Voraus berechnen
    peak_idx = scipy.signal.argrelextrema(highs, np.greater, order=5)[0]
    trough_idx = scipy.signal.argrelextrema(lows, np.less, order=5)[0]
    resistances = [(int(idx), float(highs[idx])) for idx in peak_idx]
    supports = [(int(idx), float(lows[idx])) for idx in trough_idx]

    def is_touching_pivot(price, pivots, max_idx, threshold=0.001):
        for p_idx, p_val in reversed(pivots):
            if p_idx > max_idx - 5: continue
            if p_idx < max_idx - MAX_PIVOT_AGE: break
            if abs(price - p_val) / p_val <= threshold:
                return True
        return False

    active_trades = []
    active_bull_fvgs = []
    active_bear_fvgs = []

    wins = 0
    losses = 0
    max_capital = capital
    max_drawdown = 0.0

    # Haupt-Schleife
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

        # Bullish FVG
        if highs[c - 2] < lows[c] and closes[c - 1] > opens[c - 1]:
            candle_1_low = lows[c - 2]
            if is_touching_pivot(candle_1_low, supports, c - 2):
                active_bull_fvgs.append({'top': lows[c], 'bottom': highs[c - 2], 'created_at': c})

        # Bearish FVG
        if lows[c - 2] > highs[c] and closes[c - 1] < opens[c - 1]:
            candle_1_high = highs[c - 2]
            if is_touching_pivot(candle_1_high, resistances, c - 2):
                active_bear_fvgs.append({'top': lows[c - 2], 'bottom': highs[c], 'created_at': c})

        # -------------------------------------------------
        # 3. FVGs SCHLIESSEN & TRADES AUSLÖSEN (Mit Trend-Filter)
        # -------------------------------------------------
        surviving_bull_fvgs = []
        for fvg in active_bull_fvgs:
            if curr_idx - fvg['created_at'] > MAX_FVG_AGE:
                continue

            if curr_low <= fvg['bottom']:
                # FVG Fully Closed -> Prüfen auf Targets
                valid_res = [val for p_idx, val in resistances if
                             curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val > curr_price]

                if valid_res:
                    target = min(valid_res)
                    sl = curr_low * 0.998
                    risk = curr_price - sl
                    reward = target - curr_price

                    if risk > 0 and (reward / risk) >= MIN_RR_RATIO:
                        # === TREND FILTER (LONG) ===
                        if ema_period is None or curr_price > ema_values[curr_idx]:
                            active_trades.append({'direction': 'LONG', 'entry': curr_price, 'sl': sl, 'tp': target})
            else:
                surviving_bull_fvgs.append(fvg)
        active_bull_fvgs = surviving_bull_fvgs

        surviving_bear_fvgs = []
        for fvg in active_bear_fvgs:
            if curr_idx - fvg['created_at'] > MAX_FVG_AGE:
                continue

            if curr_high >= fvg['top']:
                # FVG Fully Closed -> Prüfen auf Targets
                valid_sup = [val for p_idx, val in supports if
                             curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val < curr_price]

                if valid_sup:
                    target = max(valid_sup)
                    sl = curr_high * 1.002
                    risk = sl - curr_price
                    reward = curr_price - target

                    if risk > 0 and (reward / risk) >= MIN_RR_RATIO:
                        # === TREND FILTER (SHORT) ===
                        if ema_period is None or curr_price < ema_values[curr_idx]:
                            active_trades.append({'direction': 'SHORT', 'entry': curr_price, 'sl': sl, 'tp': target})
            else:
                surviving_bear_fvgs.append(fvg)
        active_bear_fvgs = surviving_bear_fvgs

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    net_pnl = capital - START_CAPITAL

    return {
        'name': f"EMA {ema_period}" if ema_period else "BASELINE (No EMA)",
        'trades': total_trades,
        'win_rate': win_rate,
        'pnl': net_pnl,
        'max_dd': max_drawdown
    }


def main():
    df = fetch_data()
    if df.empty:
        logger.error("No data found!")
        return

    print("=" * 65)
    print(f"📊 SMC MULTI-TREND BACKTEST: {TICKER} ({TIMEFRAME}) | 2 Jahre")
    print("=" * 65)

    results = []
    start_time = time.time()

    for ema in EMA_VARIANTS:
        res = run_simulation(df, ema)
        results.append(res)

    end_time = time.time()

    # Tabelle ausgeben
    print(f"{'Strategie':<20} | {'Trades':<8} | {'Win Rate':<10} | {'Max DD':<8} | {'Netto PnL':<12}")
    print("-" * 65)
    for r in results:
        pnl_str = f"${r['pnl']:+,.2f}"
        print(f"{r['name']:<20} | {r['trades']:<8} | {r['win_rate']:>5.2f} %   | {r['max_dd']:>5.2f} % | {pnl_str}")
    print("=" * 65)
    print(f"⏱️ Total computation time: {end_time - start_time:.2f} Sekunden")


if __name__ == "__main__":
    main()
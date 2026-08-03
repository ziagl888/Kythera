import warnings

warnings.filterwarnings("ignore")

import time
import itertools
import pandas as pd
import numpy as np
import scipy.signal
import logging

# --- Import our own DB connection ---
from core.candles import read_candles
from core.database import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 🛠️ BACKTEST CONFIGURATION & GRID PARAMETERS
# ==========================================
SYMBOL = 'BTCUSDT'
TIMEFRAME = '1h'
OUTPUT_FILE = "btc_optimization_results.txt"

START_CAPITAL = 100000.0
TRADE_MARGIN = 1000.0  # Fixed stake per trade
LEVERAGE = 100  # Leverage (100x)
TAKER_FEE = 0.0004  # 0.04% trading fee per direction (0.08% total)

MAX_PIVOT_AGE = 120
MAX_FVG_AGE = 48

# --- 🎛️ THE PARAMETERS FOR THE GRID SEARCH ---
SL_PCTS = [0.002, 0.004, 0.005, 0.0075, 0.01, 0.0125, 0.015]  # Stop-loss spacing (0.2% to 1.5%)
EMA_PERIODS = [9, 12, 15, 21, 25, 29, 33]  # Trend filter
MIN_RR_RATIOS = [0.9, 1.0, 1.15, 1.25, 1.5]  # Risk-reward ratios


# ==========================================
# 📊 DATA FETCHING (LOCAL DATABASE)
# ==========================================
def fetch_db_data():
    logger.info(f"Loading historical {TIMEFRAME} data for {SYMBOL} from the database...")
    try:
        conn = get_db_connection()
        # Via core.candles: CLOSED candles, ASC (include_forming=False).
        df = read_candles(
            conn, SYMBOL, TIMEFRAME, include_forming=False, columns=('open_time', 'open', 'high', 'low', 'close')
        )
        conn.close()

        for c in ['open', 'high', 'low', 'close']: df[c] = df[c].astype(float)
        df.dropna(inplace=True)
        return df.reset_index(drop=True)
    except Exception as e:
        logger.error(f"Error loading the DB data: {e}")
        return pd.DataFrame()


# ==========================================
# 🚀 TURBO BACKTEST ENGINE
# ==========================================
def run_simulation(df, sl_pct, ema_period, min_rr_ratio):
    capital = START_CAPITAL

    highs = df['high'].values
    lows = df['low'].values
    opens = df['open'].values
    closes = df['close'].values

    # Calculate EMA
    ema_values = df['close'].ewm(span=ema_period, adjust=False).mean().values

    # Calculate pivot points in advance
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
        # 1. CHECK ACTIVE TRADES (SL & TP)
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
        # 2. DETECT NEW FVGs
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
        # 3. CLOSE FVGs & TRIGGER TRADES
        # -------------------------------------------------
        surviving_bull_fvgs = []
        for fvg in active_bull_fvgs:
            if curr_idx - fvg['created_at'] > MAX_FVG_AGE: continue

            if curr_low <= fvg['bottom']:
                valid_res = [val for p_idx, val in resistances if
                             curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val > curr_price]

                if valid_res:
                    target = min(valid_res)
                    sl = curr_low * (1.0 - sl_pct)  # Dynamic SL
                    risk = curr_price - sl
                    reward = target - curr_price

                    if risk > 0 and (reward / risk) >= min_rr_ratio:  # Dynamic R:R
                        if curr_price > ema_values[curr_idx]:  # Dynamic EMA
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
                    sl = curr_high * (1.0 + sl_pct)  # Dynamic SL
                    risk = sl - curr_price
                    reward = curr_price - target

                    if risk > 0 and (reward / risk) >= min_rr_ratio:  # Dynamic R:R
                        if curr_price < ema_values[curr_idx]:  # Dynamic EMA
                            active_trades.append({'direction': 'SHORT', 'entry': curr_price, 'sl': sl, 'tp': target})
            else:
                surviving_bear_fvgs.append(fvg)
        active_bear_fvgs = surviving_bear_fvgs

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    net_pnl = capital - START_CAPITAL

    return {
        'sl': sl_pct,
        'ema': ema_period,
        'rr': min_rr_ratio,
        'trades': total_trades,
        'win_rate': win_rate,
        'max_dd': max_drawdown,
        'pnl': net_pnl
    }


def main():
    df = fetch_db_data()
    if df.empty:
        logger.error("No data found for BTCUSDT!")
        return

    combinations = list(itertools.product(SL_PCTS, EMA_PERIODS, MIN_RR_RATIOS))
    total_runs = len(combinations)

    print("=" * 85)
    print(f"🧠 SMC BTC HYPERPARAMETER OPTIMIZATION | {total_runs} combinations")
    print(f"Margin: ${TRADE_MARGIN:,.0f} | History: {len(df)} 1h candles")
    print("=" * 85)

    results = []
    start_time = time.time()

    for idx, (sl, ema, rr) in enumerate(combinations, 1):
        if idx % 20 == 0 or idx == total_runs:
            print(f"⏳ Calculating combination {idx}/{total_runs}...")

        res = run_simulation(df, sl, ema, rr)
        results.append(res)

    end_time = time.time()

    # Sort results by highest profit
    results.sort(key=lambda x: x['pnl'], reverse=True)

    # Write to file
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("=" * 85 + "\n")
        f.write(f"🧠 SMC BTC HYPERPARAMETER RESULTS | Sorted by profit\n")
        f.write("=" * 85 + "\n")
        header = f"{'SL %':<8} | {'EMA':<5} | {'R:R':<6} | {'Trades':<6} | {'Win Rate':<9} | {'Max DD':<7} | {'Net PnL':<12}"
        f.write(header + "\n")
        f.write("-" * 85 + "\n")

        for r in results:
            line = f"{r['sl'] * 100:>5.2f}%  | {r['ema']:<5} | {r['rr']:<6.2f} | {r['trades']:<6} | {r['win_rate']:>6.2f} % | {r['max_dd']:>5.2f}% | ${r['pnl']:+,.2f}"
            f.write(line + "\n")

    # Print top 10 to console
    print("\n" + "=" * 85)
    print("🏆 THE TOP 10 MOST PROFITABLE COMBINATIONS FOR BTCUSDT")
    print("=" * 85)
    print(header)
    print("-" * 85)

    for r in results[:10]:
        print(
            f"{r['sl'] * 100:>5.2f}%  | {r['ema']:<5} | {r['rr']:<6.2f} | {r['trades']:<6} | {r['win_rate']:>6.2f} % | {r['max_dd']:>5.2f}% | ${r['pnl']:+,.2f}")

    print("=" * 85)
    print(f"✅ Optimisation complete in {end_time - start_time:.2f} seconds.")
    print(f"📁 Complete list saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
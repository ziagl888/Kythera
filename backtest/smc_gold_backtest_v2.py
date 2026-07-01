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
LEVERAGE = 100  # Hebel (100x)
TAKER_FEE = 0.0004  # 0.04% Handelsgebühr pro Order

EMA_PERIOD = 21  # Unser bewährter Gewinner-Filter
MAX_PIVOT_AGE = 120
MAX_FVG_AGE = 48
MIN_RR_RATIO = 1.5  # Gilt für den Abstand zum ERSTEN Target (TP1)

TRADE_MARGINS = [100.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0]


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

    for c in ['open', 'high', 'low', 'close']: df[c] = df[c].astype(float)
    df.dropna(inplace=True)
    return df.reset_index(drop=True)


# ==========================================
# 🚀 TURBO BACKTEST ENGINE (with scale-out)
# ==========================================
def run_simulation(df, trade_margin):
    capital = START_CAPITAL

    highs = df['high'].values
    lows = df['low'].values
    opens = df['open'].values
    closes = df['close'].values

    ema_values = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean().values

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
        # 1. AKTIVE TRADES PRÜFEN (Moving SL & Scale Out)
        # -------------------------------------------------
        trades_to_remove = []
        for trade in active_trades:
            direction = trade['direction']

            # --- A) STOP LOSS PRÜFEN (Konservativ zuerst) ---
            sl_hit = False
            if direction == 'LONG' and curr_low <= trade['current_sl']:
                sl_hit = True
            elif direction == 'SHORT' and curr_high >= trade['current_sl']:
                sl_hit = True

            if sl_hit:
                exit_price = trade['current_sl']
                remaining_pct = trade['chunks_remaining'] * 0.25

                if direction == 'LONG':
                    raw_pnl = (exit_price - trade['entry']) * (trade['qty'] * remaining_pct)
                else:
                    raw_pnl = (trade['entry'] - exit_price) * (trade['qty'] * remaining_pct)

                fee = trade['nominal_size'] * remaining_pct * TAKER_FEE
                net_pnl = raw_pnl - fee

                capital += net_pnl
                trade['realized_pnl'] += net_pnl

                if trade['realized_pnl'] > 0:
                    wins += 1
                else:
                    losses += 1

                if capital > max_capital: max_capital = capital
                drawdown = (max_capital - capital) / max_capital * 100
                if drawdown > max_drawdown: max_drawdown = drawdown

                trades_to_remove.append(trade)
                continue

            # --- B) TARGETS PRÜFEN (Scale Out 25%) ---
            while trade['chunks_remaining'] > 0:
                tp_idx = trade['tps_hit']
                next_tp = trade['targets'][tp_idx]

                tp_hit = False
                if direction == 'LONG' and curr_high >= next_tp:
                    tp_hit = True
                elif direction == 'SHORT' and curr_low <= next_tp:
                    tp_hit = True

                if tp_hit:
                    exit_price = next_tp
                    if direction == 'LONG':
                        raw_pnl = (exit_price - trade['entry']) * (trade['qty'] * 0.25)
                    else:
                        raw_pnl = (trade['entry'] - exit_price) * (trade['qty'] * 0.25)

                    fee = trade['nominal_size'] * 0.25 * TAKER_FEE
                    net_pnl = raw_pnl - fee

                    capital += net_pnl
                    trade['realized_pnl'] += net_pnl

                    trade['tps_hit'] += 1
                    trade['chunks_remaining'] -= 1

                    # Trailing Stop Loss Logic
                    if trade['tps_hit'] == 1:
                        trade['current_sl'] = trade['entry']
                    elif trade['tps_hit'] > 1:
                        trade['current_sl'] = trade['targets'][trade['tps_hit'] - 2]

                    if capital > max_capital: max_capital = capital
                    drawdown = (max_capital - capital) / max_capital * 100
                    if drawdown > max_drawdown: max_drawdown = drawdown
                else:
                    break  # Keine weiteren Targets in dieser Kerze getroffen

            if trade['chunks_remaining'] == 0:
                if trade['realized_pnl'] > 0:
                    wins += 1
                else:
                    losses += 1
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
                valid_res = sorted([val for p_idx, val in resistances if
                                    curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val > curr_price])

                if valid_res:
                    target1 = valid_res[0]
                    sl = curr_low * 0.998
                    risk = curr_price - sl
                    reward1 = target1 - curr_price

                    if risk > 0 and (reward1 / risk) >= MIN_RR_RATIO:
                        if curr_price > ema_values[curr_idx]:
                            # 4 Targets generieren
                            targets = valid_res[:4]
                            while len(targets) < 4:
                                step = targets[-1] - curr_price if len(targets) == 1 else targets[-1] - targets[-2]
                                targets.append(targets[-1] + step)

                            nominal_size = trade_margin * LEVERAGE
                            open_fee = nominal_size * TAKER_FEE
                            capital -= open_fee  # Open Fee sofort abziehen

                            active_trades.append({
                                'direction': 'LONG', 'entry': curr_price, 'initial_sl': sl, 'current_sl': sl,
                                'targets': targets, 'tps_hit': 0, 'chunks_remaining': 4,
                                'qty': nominal_size / curr_price, 'nominal_size': nominal_size,
                                'realized_pnl': -open_fee  # Startet im Minus wegen Gebühr
                            })
            else:
                surviving_bull_fvgs.append(fvg)
        active_bull_fvgs = surviving_bull_fvgs

        surviving_bear_fvgs = []
        for fvg in active_bear_fvgs:
            if curr_idx - fvg['created_at'] > MAX_FVG_AGE: continue

            if curr_high >= fvg['top']:
                valid_sup = sorted([val for p_idx, val in supports if
                                    curr_idx - MAX_PIVOT_AGE <= p_idx <= curr_idx - 5 and val < curr_price],
                                   reverse=True)

                if valid_sup:
                    target1 = valid_sup[0]
                    sl = curr_high * 1.002
                    risk = sl - curr_price
                    reward1 = curr_price - target1

                    if risk > 0 and (reward1 / risk) >= MIN_RR_RATIO:
                        if curr_price < ema_values[curr_idx]:
                            # 4 Targets generieren
                            targets = valid_sup[:4]
                            while len(targets) < 4:
                                step = curr_price - targets[-1] if len(targets) == 1 else targets[-2] - targets[-1]
                                targets.append(targets[-1] - step)

                            nominal_size = trade_margin * LEVERAGE
                            open_fee = nominal_size * TAKER_FEE
                            capital -= open_fee

                            active_trades.append({
                                'direction': 'SHORT', 'entry': curr_price, 'initial_sl': sl, 'current_sl': sl,
                                'targets': targets, 'tps_hit': 0, 'chunks_remaining': 4,
                                'qty': nominal_size / curr_price, 'nominal_size': nominal_size,
                                'realized_pnl': -open_fee
                            })
            else:
                surviving_bear_fvgs.append(fvg)
        active_bear_fvgs = surviving_bear_fvgs

    total_trades = wins + losses
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    net_pnl = capital - START_CAPITAL

    return {
        'margin': trade_margin,
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

    print("=" * 80)
    print(f"📊 SMC MULTI-TARGET & TRAILING SL BACKTEST: {TICKER} (1h) | 2 Jahre | EMA {EMA_PERIOD}")
    print("=" * 80)

    results = []
    start_time = time.time()

    for margin in TRADE_MARGINS:
        res = run_simulation(df, margin)
        results.append(res)

    end_time = time.time()

    print(f"{'Einsatz (Margin)':<16} | {'Trades':<8} | {'Win Rate':<10} | {'Max DD':<8} | {'Netto PnL':<12}")
    print("-" * 80)
    for r in results:
        pnl_str = f"${r['pnl']:+,.2f}"
        margin_str = f"${r['margin']:,.0f}"
        print(f"{margin_str:<16} | {r['trades']:<8} | {r['win_rate']:>5.2f} %   | {r['max_dd']:>5.2f} % | {pnl_str}")
    print("=" * 80)
    print(f"⏱️ Total computation time: {end_time - start_time:.2f} Sekunden")


if __name__ == "__main__":
    main()
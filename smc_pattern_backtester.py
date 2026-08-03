import warnings

warnings.filterwarnings("ignore")

import json
import logging
from datetime import timedelta

import numpy as np
import pandas as pd
import scipy.signal

from core.candles import read_candles_with_indicators
from core.database import get_db_connection
from core.time import utc_now

logging.basicConfig(level=logging.INFO, format='%(asctime)s - BACKTESTER - %(message)s')
logger = logging.getLogger(__name__)

# ── KNOWN BACKTEST LIMITATIONS (P3.6, T-2026-CU-9050-096) — read before trusting
# any win-rate/PnL this script prints. Documented, deliberately NOT "fixed" here:
#   1. Fees NOT applied. FEE_RATE below is declared but never referenced — every
#      reported outcome is gross, so real WR/expectancy is lower.
#   2. Survivorship bias. get_coins() reads today's coins.json but backtests 1-2
#      years back; delisted symbols that would have lost are absent from the set.
#   3. No capital / concurrency model. Trades are scored independently; there is
#      no shared equity, no margin cap and no limit on simultaneous positions, so
#      aggregate PnL overstates a real, capital-bounded run.
TIMEFRAMES = ['1h', '4h']
PIVOT_WINDOW = 10  # Larger window for real swing points
RR_RATIO = 2.0  # Risk-reward 1:2
FEE_RATE = 0.0008  # 0.04% maker + 0.04% taker (incl. slippage) — DECLARED BUT UNUSED, see limitation 1 above


def get_coins():
    try:
        with open('coins.json') as f:
            data = json.load(f)
            return [
                c.upper()
                for c in (data.get('coins', data) if isinstance(data, dict) else data)
                if c.upper().endswith("USDT")
            ]
    except Exception:
        return []


def run_backtest():
    coins = get_coins()
    conn = get_db_connection()

    results = []

    for tf in TIMEFRAMES:
        logger.info(f"🚀 Starting backtest for timeframe {tf}...")

        for idx, symbol in enumerate(coins, 1):
            if idx % 50 == 0:
                logger.info(f"Processing Coin {idx}/{len(coins)}: {symbol} ({tf})")

            try:
                # Price + RSI via core.candles: CLOSED candles, ASC
                # (include_forming=False).
                df = read_candles_with_indicators(
                    conn,
                    symbol,
                    tf,
                    start=utc_now() - timedelta(days=730),
                    include_forming=False,
                    candle_columns=('open_time', 'open', 'high', 'low', 'close'),
                    indicator_columns=['rsi_14'],
                )
                if len(df) < 500:
                    continue

                df.ffill(inplace=True)
                for c in ['open', 'high', 'low', 'close', 'rsi_14']:
                    df[c] = df[c].astype(float)

                highs, lows, closes, opens = df['high'].values, df['low'].values, df['close'].values, df['open'].values
                rsis = df['rsi_14'].values

                peak_idx = scipy.signal.argrelextrema(highs, np.greater, order=PIVOT_WINDOW)[0]
                trough_idx = scipy.signal.argrelextrema(lows, np.less, order=PIVOT_WINDOW)[0]

                # =======================================================
                # STRATEGY 1: LIQUIDITY SWEEP (Turtle Soup)
                # =======================================================
                sweep_wins = 0
                sweep_losses = 0

                for p_idx in peak_idx:
                    pivot_high = highs[p_idx]
                    for i in range(p_idx + PIVOT_WINDOW, min(p_idx + 40, len(df))):
                        # Wick breaks high, but body closes below
                        if highs[i] > pivot_high and closes[i] < pivot_high and opens[i] < pivot_high:
                            entry = closes[i]
                            sl = highs[i] * 1.002
                            dist = sl - entry
                            if dist <= 0:
                                continue
                            tp = entry - (dist * RR_RATIO)

                            for j in range(i + 1, len(df)):
                                if highs[j] >= sl:
                                    sweep_losses += 1
                                    break
                                elif lows[j] <= tp:
                                    sweep_wins += 1
                                    break
                            break

                for p_idx in trough_idx:
                    pivot_low = lows[p_idx]
                    for i in range(p_idx + PIVOT_WINDOW, min(p_idx + 40, len(df))):
                        if lows[i] < pivot_low and closes[i] > pivot_low and opens[i] > pivot_low:
                            entry = closes[i]
                            sl = lows[i] * 0.998
                            dist = entry - sl
                            if dist <= 0:
                                continue
                            tp = entry + (dist * RR_RATIO)

                            for j in range(i + 1, len(df)):
                                if lows[j] <= sl:
                                    sweep_losses += 1
                                    break
                                elif highs[j] >= tp:
                                    sweep_wins += 1
                                    break
                            break

                results.append(
                    {
                        'Pattern': '1. Liquidity Sweep',
                        'TF': tf,
                        'Symbol': symbol,
                        'Wins': sweep_wins,
                        'Losses': sweep_losses,
                    }
                )

                # =======================================================
                # STRATEGY 2: THREE-DRIVE DIVERGENCE
                # =======================================================
                td_wins = 0
                td_losses = 0

                for i in range(2, len(peak_idx)):
                    p1, p2, p3 = peak_idx[i - 2], peak_idx[i - 1], peak_idx[i]
                    if p3 - p1 > 100:
                        continue

                    if highs[p1] < highs[p2] < highs[p3]:
                        if rsis[p1] > rsis[p2] > rsis[p3]:
                            entry = closes[p3]
                            sl = highs[p3] * 1.005
                            dist = sl - entry
                            if dist <= 0:
                                continue
                            tp = entry - (dist * RR_RATIO)

                            for j in range(p3 + 1, len(df)):
                                if highs[j] >= sl:
                                    td_losses += 1
                                    break
                                elif lows[j] <= tp:
                                    td_wins += 1
                                    break

                results.append(
                    {'Pattern': '2. Three-Drive Div', 'TF': tf, 'Symbol': symbol, 'Wins': td_wins, 'Losses': td_losses}
                )

                # =======================================================
                # STRATEGY 3: BREAKER BLOCK (Support/Resistance Flip)
                # =======================================================
                bb_wins = 0
                bb_losses = 0

                # Bullish breaker (resistance becomes support)
                for p_idx in peak_idx:
                    pivot_res = highs[p_idx]
                    breakout_idx = -1

                    # Searching for breakout above resistance
                    for i in range(p_idx + PIVOT_WINDOW, min(p_idx + 60, len(df))):
                        if closes[i] > pivot_res:
                            breakout_idx = i
                            break

                    # If breakout occurred, waiting for first retest
                    if breakout_idx != -1:
                        for j in range(breakout_idx + 1, min(breakout_idx + 40, len(df))):
                            if lows[j] <= pivot_res:  # Price falls back to old high
                                entry = pivot_res
                                sl = entry * 0.99  # 1% stop loss below support
                                tp = entry * 1.02  # 2% take profit (1:2 RR)

                                for k in range(j + 1, len(df)):
                                    if lows[k] <= sl:
                                        bb_losses += 1
                                        break
                                    elif highs[k] >= tp:
                                        bb_wins += 1
                                        break
                                break  # Only trade the first retest

                # Bearish breaker (support becomes resistance)
                for p_idx in trough_idx:
                    pivot_sup = lows[p_idx]
                    breakdown_idx = -1

                    # Searching for breakdown below support
                    for i in range(p_idx + PIVOT_WINDOW, min(p_idx + 60, len(df))):
                        if closes[i] < pivot_sup:
                            breakdown_idx = i
                            break

                    # If breakdown occurred, waiting for first retest from below
                    if breakdown_idx != -1:
                        for j in range(breakdown_idx + 1, min(breakdown_idx + 40, len(df))):
                            if highs[j] >= pivot_sup:  # Price rises to old low
                                entry = pivot_sup
                                sl = entry * 1.01  # 1% stop loss above resistance
                                tp = entry * 0.98  # 2% take profit (1:2 RR)

                                for k in range(j + 1, len(df)):
                                    if highs[k] >= sl:
                                        bb_losses += 1
                                        break
                                    elif lows[k] <= tp:
                                        bb_wins += 1
                                        break
                                break

                results.append(
                    {'Pattern': '3. Breaker Block', 'TF': tf, 'Symbol': symbol, 'Wins': bb_wins, 'Losses': bb_losses}
                )

            except Exception:
                pass

    conn.close()

    # --- ANALYSIS ---
    res_df = pd.DataFrame(results)
    if res_df.empty:
        logger.warning("No results found!")
        return

    # Aggregation over all coins
    summary = res_df.groupby(['Pattern', 'TF']).agg({'Wins': 'sum', 'Losses': 'sum'}).reset_index()
    summary['Total_Trades'] = summary['Wins'] + summary['Losses']

    # Prevent division by zero
    summary = summary[summary['Total_Trades'] > 0].copy()

    summary['Win_Rate_%'] = (summary['Wins'] / summary['Total_Trades'] * 100).round(2)

    # PnL calculation
    # Since RR is 1:2: 1 win = +2R, 1 loss = -1R.
    summary['Net_R_Profit'] = (summary['Wins'] * 2.0) - summary['Losses']

    # Sort by best results
    summary = summary.sort_values(by=['Net_R_Profit'], ascending=False)

    print("\n" + "=" * 80)
    print("📊 INSTITUTIONAL PATTERN BACKTEST RESULTS (RR 1:2)")
    print("=" * 80)
    print(summary.to_string(index=False))
    print("=" * 80)
    print("Note: 'Net_R_Profit' shows the pure risk-reward units.")
    print("Example: Net_R_Profit of 500 means you would have won 500 times your risked money.\n")


if __name__ == "__main__":
    run_backtest()

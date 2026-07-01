import warnings

warnings.filterwarnings("ignore")

import json
import logging

import numpy as np
import pandas as pd
import scipy.signal

from core.database import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - BACKTESTER - %(message)s')
logger = logging.getLogger(__name__)

TIMEFRAMES = ['1h', '4h']
PIVOT_WINDOW = 10  # Größeres Window für echte Swing-Points
RR_RATIO = 2.0  # Risk-Reward 1:2
FEE_RATE = 0.0008  # 0.04% Maker + 0.04% Taker (inkl. Slippage) pro Trade


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
        logger.info(f"🚀 Starting Backtest für Timeframe {tf}...")

        for idx, symbol in enumerate(coins, 1):
            if idx % 50 == 0:
                logger.info(f"Processing Coin {idx}/{len(coins)}: {symbol} ({tf})")

            try:
                # Hole Preis + RSI
                query = f"""
                    SELECT t1.open_time, t1.open, t1.high, t1.low, t1.close, t2.rsi_14
                    FROM "{symbol}_{tf}" t1
                    LEFT JOIN "{symbol}_{tf}_indicators" t2 ON t1.open_time = t2.open_time
                    WHERE t1.open_time >= NOW() - INTERVAL '2 years'
                    ORDER BY t1.open_time ASC
                """
                df = pd.read_sql_query(query, conn)
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
                # STRATEGIE 1: LIQUIDITY SWEEP (Turtle Soup)
                # =======================================================
                sweep_wins = 0
                sweep_losses = 0

                for p_idx in peak_idx:
                    pivot_high = highs[p_idx]
                    for i in range(p_idx + PIVOT_WINDOW, min(p_idx + 40, len(df))):
                        # Wick bricht Hoch, aber Body schließt darunter
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
                # STRATEGIE 2: THREE-DRIVE DIVERGENCE
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
                # STRATEGIE 3: BREAKER BLOCK (Support/Resistance Flip)
                # =======================================================
                bb_wins = 0
                bb_losses = 0

                # Bullish Breaker (Resistance wird Support)
                for p_idx in peak_idx:
                    pivot_res = highs[p_idx]
                    breakout_idx = -1

                    # Searching after Breakout über Resistance
                    for i in range(p_idx + PIVOT_WINDOW, min(p_idx + 60, len(df))):
                        if closes[i] > pivot_res:
                            breakout_idx = i
                            break

                    # Wenn Breakout stattfand, waiting for den ersten Retest
                    if breakout_idx != -1:
                        for j in range(breakout_idx + 1, min(breakout_idx + 40, len(df))):
                            if lows[j] <= pivot_res:  # Preis fällt auf altes Hoch zurück
                                entry = pivot_res
                                sl = entry * 0.99  # 1% Stop Loss unter Support
                                tp = entry * 1.02  # 2% Take Profit (1:2 RR)

                                for k in range(j + 1, len(df)):
                                    if lows[k] <= sl:
                                        bb_losses += 1
                                        break
                                    elif highs[k] >= tp:
                                        bb_wins += 1
                                        break
                                break  # Nur den ersten Retest handeln

                # Bearish Breaker (Support wird Resistance)
                for p_idx in trough_idx:
                    pivot_sup = lows[p_idx]
                    breakdown_idx = -1

                    # Searching after Breakdown unter Support
                    for i in range(p_idx + PIVOT_WINDOW, min(p_idx + 60, len(df))):
                        if closes[i] < pivot_sup:
                            breakdown_idx = i
                            break

                    # Wenn Breakdown stattfand, waiting for ersten Retest von unten
                    if breakdown_idx != -1:
                        for j in range(breakdown_idx + 1, min(breakdown_idx + 40, len(df))):
                            if highs[j] >= pivot_sup:  # Preis steigt an altes Tief
                                entry = pivot_sup
                                sl = entry * 1.01  # 1% Stop Loss über Resistance
                                tp = entry * 0.98  # 2% Take Profit (1:2 RR)

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

    # --- AUSWERTUNG ---
    res_df = pd.DataFrame(results)
    if res_df.empty:
        logger.warning("No results found!")
        return

    # Aggregation über alle Coins
    summary = res_df.groupby(['Pattern', 'TF']).agg({'Wins': 'sum', 'Losses': 'sum'}).reset_index()
    summary['Total_Trades'] = summary['Wins'] + summary['Losses']

    # Verhindern von Division by Zero
    summary = summary[summary['Total_Trades'] > 0].copy()

    summary['Win_Rate_%'] = (summary['Wins'] / summary['Total_Trades'] * 100).round(2)

    # PnL Berechnung
    # Da RR 1:2 ist: 1 Win = +2R, 1 Loss = -1R.
    summary['Net_R_Profit'] = (summary['Wins'] * 2.0) - summary['Losses']

    # Sortieren after den besten Ergebnissen
    summary = summary.sort_values(by=['Net_R_Profit'], ascending=False)

    print("\n" + "=" * 80)
    print("📊 INSTITUTIONAL PATTERN BACKTEST RESULTS (RR 1:2)")
    print("=" * 80)
    print(summary.to_string(index=False))
    print("=" * 80)
    print("Hinweis: 'Net_R_Profit' zeigt die reinen Risk-Reward Einheiten.")
    print("Beispiel: Net_R_Profit von 500 bedeutet, du hättest 500 mal dein riskiertes Geld gewonnen.\n")


if __name__ == "__main__":
    run_backtest()

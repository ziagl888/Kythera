import os
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import asyncpg
import asyncio

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "dbfiller",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "cryptodata"
}

END_DATE = datetime(2025, 12, 15)
START_DATE = END_DATE - timedelta(days=300) 

# --- KONFIGURATION SHORT ONLY ZIELE ---
# Wir nutzen dieselben konservativen Ziele wie bei Long für maximale Trefferquote
TP_SHORT_PCT = 0.025   # 2.5% Take Profit
SL_SHORT_PCT = 0.015   # 1.5% Stop Loss

MAX_HOLD_HOURS = 96      

with open("coins.json", "r", encoding="utf-8") as f:
    raw_coins = json.load(f)
coins = [c.upper() + "USDT" if not c.upper().endswith("USDT") else c.upper() for c in raw_coins]
all_signals = []

async def export_signals(pool, coin):
    price_table = f'"{coin}_1h_X"'
    ind_table = f'"{coin}_1h_indicators"'
    
    async with pool.acquire() as conn:
        try:
            # SQL Query (Identisch zum Long Script)
            rows = await conn.fetch(f"""
                SELECT 
                    p.open_time, p.high, p.low, p.close, p.volume, -- 0-4
                    i.rsi_14, i.rsi_6, i.tsi_fast_12_7_7, i.tsi_fast_12_7_7_signal, -- 5-8
                    i.ema_9, i.ema_21, i.ema_50, i.ema_200, -- 9-12
                    i.kama_9, i.kama_21, i.kama_55, -- 13-15
                    i.macd_dif_normal_12_26_9, i.macd_dea_normal_12_26_9, -- 16-17
                    i.atr_14, i.boll_upper_20, i.boll_lower_20, -- 18-20
                    i.donchian_upper_20, i.donchian_lower_20, -- 21-22
                    i.trendline_slope, i.support_price, i.resistance_price -- 23-25
                FROM {price_table} p
                LEFT JOIN {ind_table} i ON p.open_time = i.open_time
                WHERE p.open_time >= $1 AND p.open_time <= $2
                ORDER BY p.open_time
            """, START_DATE, END_DATE)
            
            if len(rows) < 150: return
            
            df_coin = pd.DataFrame(rows, columns=[
                'open_time', 'high', 'low', 'close', 'volume',
                'rsi_14', 'rsi_6', 'tsi_fast_12_7_7', 'tsi_fast_12_7_7_signal',
                'ema_9', 'ema_21', 'ema_50', 'ema_200',
                'kama_9', 'kama_21', 'kama_55',
                'macd_dif_normal_12_26_9', 'macd_dea_normal_12_26_9',
                'atr_14', 'boll_upper_20', 'boll_lower_20',
                'donchian_upper_20', 'donchian_lower_20',
                'trendline_slope', 'support_price', 'resistance_price'
            ])
            
            # Numeric conversion & filling
            cols_to_numeric = ['high', 'low', 'close', 'volume', 'rsi_14', 'rsi_6', 
                               'tsi_fast_12_7_7', 'tsi_fast_12_7_7_signal',
                               'ema_9', 'ema_21', 'ema_50', 'ema_200', 
                               'kama_9', 'kama_21', 'kama_55',
                               'macd_dif_normal_12_26_9', 'macd_dea_normal_12_26_9',
                               'atr_14', 'boll_upper_20', 'boll_lower_20', 
                               'donchian_upper_20', 'donchian_lower_20',
                               'trendline_slope', 'support_price', 'resistance_price']

            for col in cols_to_numeric:
                df_coin[col] = pd.to_numeric(df_coin[col], errors='coerce')

            # Essential fills
            df_coin['close'] = df_coin['close'].fillna(method='ffill').fillna(method='bfill')
            df_coin['volume'] = df_coin['volume'].fillna(0)
            
            # Check TSI validity
            if df_coin['tsi_fast_12_7_7'].isna().all(): return

            # --- Indikatorberechnung (OBV und VWAP) ---
            df_coin['obv'] = (np.sign(df_coin['close'].diff()) * df_coin['volume']).fillna(0).cumsum()
            df_coin['typical_price'] = (df_coin['high'] + df_coin['low'] + df_coin['close']) / 3
            df_coin['vwap_20'] = (df_coin['volume'] * df_coin['typical_price']).rolling(20).sum() / df_coin['volume'].rolling(20).sum()
            df_coin['vwap_20'] = df_coin['vwap_20'].fillna(df_coin['close'])

            # --- Feature Engineering ---
            # Fill NaNs for calculation
            for col in df_coin.columns:
                if col not in ['open_time']:
                    df_coin[col] = df_coin[col].fillna(0)

            # Calculation Series
            close_s = df_coin['close']
            vol_s = df_coin['volume']
            
            # 1. Volume Features
            vol_sma20 = vol_s.rolling(20).mean().replace(0, 1)
            vol_ratio = vol_s / vol_sma20
            
            # 2. Bollinger
            bb_width = (df_coin['boll_upper_20'] - df_coin['boll_lower_20']) / df_coin['boll_lower_20'].replace(0, 1)
            bb_pos = (close_s - df_coin['boll_lower_20']) / (df_coin['boll_upper_20'] - df_coin['boll_lower_20']).replace(0, 1)
            
            # 3. EMA Dists
            dist_ema200 = (close_s / df_coin['ema_200'].replace(0, 1)) - 1
            dist_ema9_21 = (df_coin['ema_9'] / df_coin['ema_21'].replace(0, 1)) - 1
            
            # 4. KAMA Dists
            dist_kama9 = (close_s / df_coin['kama_9'].replace(0, 1)) - 1
            dist_kama21 = (close_s / df_coin['kama_21'].replace(0, 1)) - 1
            dist_kama55 = (close_s / df_coin['kama_55'].replace(0, 1)) - 1
            dist_kama9_21 = (df_coin['kama_9'] / df_coin['kama_21'].replace(0, 1)) - 1
            
            # 5. Donchian
            dist_donch_up = (close_s / df_coin['donchian_upper_20'].replace(0, 1)) - 1
            dist_donch_low = (close_s / df_coin['donchian_lower_20'].replace(0, 1)) - 1
            
            # 6. RSI Ratio
            rsi_ratio = df_coin['rsi_6'] / df_coin['rsi_14'].replace(0, 1)
            
            # 7. Slope
            slope_norm = (df_coin['trendline_slope'] / close_s.replace(0, 1)) * 1000
            
            # 8. Supp/Res
            dist_supp = (close_s - df_coin['support_price']) / close_s.replace(0, 1)
            dist_res = (df_coin['resistance_price'] - close_s) / close_s.replace(0, 1)

            # 9. Bearish Flags
            macd_cross_bearish = ( (df_coin['macd_dif_normal_12_26_9'].shift(1) >= df_coin['macd_dea_normal_12_26_9'].shift(1)) & 
                                   (df_coin['macd_dif_normal_12_26_9'] < df_coin['macd_dea_normal_12_26_9']) ).astype(int)
            ema9_21_cross_bearish = ( (df_coin['ema_9'].shift(1) >= df_coin['ema_21'].shift(1)) & 
                                      (df_coin['ema_9'] < df_coin['ema_21']) ).astype(int)
            kama9_21_cross_bearish = ( (df_coin['kama_9'].shift(1) >= df_coin['kama_21'].shift(1)) & 
                                       (df_coin['kama_9'] < df_coin['kama_21']) ).astype(int)
            bollinger_lower_break = (close_s < df_coin['boll_lower_20']).astype(int)
            close_below_ema50 = (close_s < df_coin['ema_50']).astype(int)

            # 10. New Volume Features
            obv_sma20 = df_coin['obv'].rolling(20).mean().replace(0, 1)
            obv_ratio = df_coin['obv'] / obv_sma20
            close_to_vwap_pct = (close_s / df_coin['vwap_20'].replace(0, 1)) - 1
            volume_spike = (vol_s > vol_sma20 * 2).astype(int)
            volume_trend_up = (vol_s.rolling(5).mean() > vol_s.rolling(20).mean()).astype(int)

            # Add to DF
            df_coin['vol_ratio'] = vol_ratio
            df_coin['bb_width'] = bb_width
            df_coin['bb_pos'] = bb_pos
            df_coin['dist_ema200'] = dist_ema200
            df_coin['dist_ema9_21'] = dist_ema9_21
            df_coin['dist_kama9'] = dist_kama9
            df_coin['dist_kama21'] = dist_kama21
            df_coin['dist_kama55'] = dist_kama55
            df_coin['dist_kama9_21'] = dist_kama9_21
            df_coin['dist_donch_up'] = dist_donch_up
            df_coin['dist_donch_low'] = dist_donch_low
            df_coin['rsi_ratio'] = rsi_ratio
            df_coin['slope_norm'] = slope_norm
            df_coin['dist_supp'] = dist_supp
            df_coin['dist_res'] = dist_res
            df_coin['macd_cross_bearish'] = macd_cross_bearish
            df_coin['ema9_21_cross_bearish'] = ema9_21_cross_bearish
            df_coin['kama9_21_cross_bearish'] = kama9_21_cross_bearish
            df_coin['bollinger_lower_break'] = bollinger_lower_break
            df_coin['close_below_ema50'] = close_below_ema50
            df_coin['obv_ratio'] = obv_ratio
            df_coin['close_to_vwap_pct'] = close_to_vwap_pct
            df_coin['obv_val'] = df_coin['obv']
            df_coin['volume_spike'] = volume_spike
            df_coin['volume_trend_up'] = volume_trend_up

            # Simulation Loop
            for i in range(50, len(df_coin) - MAX_HOLD_HOURS):
                row = df_coin.iloc[i]
                prev = df_coin.iloc[i-1]
                
                tsi_val = row['tsi_fast_12_7_7']
                tsi_sig = row['tsi_fast_12_7_7_signal']
                prev_tsi_val = prev['tsi_fast_12_7_7']
                prev_tsi_sig = prev['tsi_fast_12_7_7_signal']
                
                signal = None
                # SHORT Logic
                if tsi_val < tsi_sig and prev_tsi_val >= prev_tsi_sig: 
                    signal = "short"
                
                if signal == "short":
                    entry_price = row['close']
                    result_pnl = 0
                    outcome = "timeout"
                    
                    take_profit_pct = TP_SHORT_PCT
                    stop_loss_pct = SL_SHORT_PCT

                    # Future Loop
                    future_slice = df_coin.iloc[i+1 : i+1+MAX_HOLD_HOURS]
                    
                    # Vektorisierte Prüfung für TP/SL wäre schneller, aber Loop ist hier okay
                    for _, fut_row in future_slice.iterrows():
                        low = fut_row['low']
                        high = fut_row['high']
                        
                        if low <= entry_price * (1 - take_profit_pct):
                            result_pnl = take_profit_pct; outcome = "tp"; break
                        if high >= entry_price * (1 + stop_loss_pct):
                            result_pnl = -stop_loss_pct; outcome = "sl"; break
                    
                    if outcome == "timeout":
                        exit_price = future_slice.iloc[-1]['close']
                        raw_pnl = (entry_price - exit_price) / entry_price
                        result_pnl = raw_pnl
                    
                    # Feature Extraction
                    feat_dict = {
                        "rsi_14": row['rsi_14'], "rsi_6": row['rsi_6'],
                        "macd_hist": row['macd_dif_normal_12_26_9'] - row['macd_dea_normal_12_26_9'],
                        "atr_pct": (row['atr_14'] / row['close']) * 100 if row['close'] else 0,
                        "vol_ratio": row['vol_ratio'], "bb_width": row['bb_width'], "bb_pos": row['bb_pos'],
                        "dist_ema200": row['dist_ema200'], "dist_ema9_21": row['dist_ema9_21'],
                        "dist_kama9": row['dist_kama9'], "dist_kama21": row['dist_kama21'], 
                        "dist_kama55": row['dist_kama55'], "dist_kama9_21": row['dist_kama9_21'], 
                        "dist_donch_up": row['dist_donch_up'], "dist_donch_low": row['dist_donch_low'],
                        "rsi_ratio": row['rsi_ratio'], "slope_norm": row['slope_norm'], 
                        "dist_supp": row['dist_supp'], "dist_res": row['dist_res'],
                        "macd_cross_bearish": row['macd_cross_bearish'],
                        "ema9_21_cross_bearish": row['ema9_21_cross_bearish'],
                        "kama9_21_cross_bearish": row['kama9_21_cross_bearish'],
                        "bollinger_lower_break": row['bollinger_lower_break'],
                        "close_below_ema50": row['close_below_ema50'],
                        "obv_ratio": row['obv_ratio'], "close_to_vwap_pct": row['close_to_vwap_pct'],
                        "obv_val": row['obv_val'], "volume_spike": row['volume_spike'],
                        "volume_trend_up": row['volume_trend_up']
                    }

                    all_signals.append({
                        "coin": coin.replace("USDT", ""),
                        "direction": "short",
                        "entry_time": row['open_time'],
                        "outcome": outcome,
                        "pnl_pct": result_pnl * 100,
                        "pnl_$": result_pnl * 100 * 20,
                        **feat_dict
                    })

        except Exception as e: 
            print(f"Fehler bei Coin {coin}: {e}")

async def main():
    pool = await asyncpg.create_pool(**DB_CONFIG)
    print("Starte SHORT-ONLY Export (2.5/1.5)...")
    for i, coin in enumerate(coins):
        if i % 50 == 0: print(f"{i} Coins processed...")
        await export_signals(pool, coin)
    await pool.close()
    
    if all_signals:
        df = pd.DataFrame(all_signals)
        df.to_csv("tsi_signals_short_only.csv", index=False)
        print(f"\nFertig! {len(all_signals)} Short-Trades exportiert in 'tsi_signals_short_only.csv'")

asyncio.run(main())

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

TP_LONG_PCT = 0.025
SL_LONG_PCT = 0.015

MAX_HOLD_HOURS = 96      

with open("coins.json", "r", encoding="utf-8") as f:
    raw_coins = json.load(f)
coins = [c.upper() + "USDT" if not c.upper().endswith("USDT") else c.upper() for c in raw_coins]
all_signals = []

async def export_signals(pool, coin):
    price_table = f'"{coin}_1h"'
    ind_table = f'"{coin}_1h_indicators"'
    
    async with pool.acquire() as conn:
        try:
            # SQL Query OHNE OBV und VWAP, da wir sie im Python berechnen
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
            
            # Daten als Pandas DataFrame für einfachere Berechnungen
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
            
            # Sicherstellen, dass die Spalten numerisch sind
            df_coin['high'] = pd.to_numeric(df_coin['high'], errors='coerce')
            df_coin['low'] = pd.to_numeric(df_coin['low'], errors='coerce')
            df_coin['close'] = pd.to_numeric(df_coin['close'], errors='coerce')
            df_coin['volume'] = pd.to_numeric(df_coin['volume'], errors='coerce')

            # NaNs in essentiellen Spalten füllen
            df_coin['close'] = df_coin['close'].fillna(df_coin['close'].median())
            df_coin['volume'] = df_coin['volume'].fillna(0)
            
            # RSI, TSI für NaN Check
            tsi_fast = pd.to_numeric(df_coin['tsi_fast_12_7_7'], errors='coerce')
            tsi_fast_signal = pd.to_numeric(df_coin['tsi_fast_12_7_7_signal'], errors='coerce')
            if np.all(np.isnan(tsi_fast)) or np.all(np.isnan(tsi_fast_signal)): return

            # --- Indikatorberechnung (OBV und VWAP) ---
            # On-Balance Volume (OBV)
            df_coin['obv'] = (np.sign(df_coin['close'].diff()) * df_coin['volume']).fillna(0).cumsum()

            # Volume Weighted Average Price (VWAP) - 20 Perioden
            # Wir berechnen den Typical Price für den VWAP
            df_coin['typical_price'] = (df_coin['high'] + df_coin['low'] + df_coin['close']) / 3
            df_coin['vwap_20'] = (df_coin['volume'] * df_coin['typical_price']).rolling(20).sum() / df_coin['volume'].rolling(20).sum()
            df_coin['vwap_20'] = df_coin['vwap_20'].fillna(df_coin['close']) # Fülle initiales NaN mit Close
            
            # Konvertiere zurück zu numpy array für konsistente Verarbeitung
            data = df_coin.to_numpy(dtype=object)

            # --- Feature Engineering (wie gehabt, aber jetzt mit obv und vwap_20 in 'data') ---
            # Die Indizes müssen entsprechend den neuen Spalten in df_coin angepasst werden.
            # Neue Indizes der SQL-Felder (nach dem DataFrame-Umbau)
            close_idx = df_coin.columns.get_loc('close') # 3
            vol_idx = df_coin.columns.get_loc('volume') # 4
            rsi14_idx = df_coin.columns.get_loc('rsi_14') # 5
            rsi6_idx = df_coin.columns.get_loc('rsi_6') # 6
            tsi_val_idx = df_coin.columns.get_loc('tsi_fast_12_7_7') # 7
            tsi_sig_idx = df_coin.columns.get_loc('tsi_fast_12_7_7_signal') # 8
            ema9_idx = df_coin.columns.get_loc('ema_9') # 9
            ema21_idx = df_coin.columns.get_loc('ema_21') # 10
            ema50_idx = df_coin.columns.get_loc('ema_50') # 11
            ema200_idx = df_coin.columns.get_loc('ema_200') # 12
            kama9_idx = df_coin.columns.get_loc('kama_9') # 13
            kama21_idx = df_coin.columns.get_loc('kama_21') # 14
            kama55_idx = df_coin.columns.get_loc('kama_55') # 15
            macd_dif_idx = df_coin.columns.get_loc('macd_dif_normal_12_26_9') # 16
            macd_dea_idx = df_coin.columns.get_loc('macd_dea_normal_12_26_9') # 17
            atr_idx = df_coin.columns.get_loc('atr_14') # 18
            boll_upper_idx = df_coin.columns.get_loc('boll_upper_20') # 19
            boll_lower_idx = df_coin.columns.get_loc('boll_lower_20') # 20
            donchian_upper_idx = df_coin.columns.get_loc('donchian_upper_20') # 21
            donchian_lower_idx = df_coin.columns.get_loc('donchian_lower_20') # 22
            slope_idx = df_coin.columns.get_loc('trendline_slope') # 23
            support_idx = df_coin.columns.get_loc('support_price') # 24
            resistance_idx = df_coin.columns.get_loc('resistance_price') # 25
            
            # Neu berechnete Indikatoren
            obv_idx = df_coin.columns.get_loc('obv') # 26 (Nach typical_price und vwap_20)
            vwap_20_idx = df_coin.columns.get_loc('vwap_20') # 27
            
            # --- Indikatoren als Pandas Series extrahieren und NaNs füllen ---
            close_s = pd.to_numeric(df_coin['close'], errors='coerce').fillna(df_coin['close'].median())
            vol_s = pd.to_numeric(df_coin['volume'], errors='coerce').fillna(0)
            macd_dif_s = pd.to_numeric(df_coin['macd_dif_normal_12_26_9'], errors='coerce').fillna(0)
            macd_dea_s = pd.to_numeric(df_coin['macd_dea_normal_12_26_9'], errors='coerce').fillna(0)
            ema9_s = pd.to_numeric(df_coin['ema_9'], errors='coerce').fillna(close_s)
            ema21_s = pd.to_numeric(df_coin['ema_21'], errors='coerce').fillna(close_s)
            ema50_s = pd.to_numeric(df_coin['ema_50'], errors='coerce').fillna(close_s)
            ema200_s = pd.to_numeric(df_coin['ema_200'], errors='coerce').fillna(close_s)
            kama9_s = pd.to_numeric(df_coin['kama_9'], errors='coerce').fillna(close_s)
            kama21_s = pd.to_numeric(df_coin['kama_21'], errors='coerce').fillna(close_s)
            kama55_s = pd.to_numeric(df_coin['kama_55'], errors='coerce').fillna(close_s)
            atr_s = pd.to_numeric(df_coin['atr_14'], errors='coerce').fillna(0)
            boll_upper_s = pd.to_numeric(df_coin['boll_upper_20'], errors='coerce').fillna(close_s)
            boll_lower_s = pd.to_numeric(df_coin['boll_lower_20'], errors='coerce').fillna(close_s)
            donchian_upper_s = pd.to_numeric(df_coin['donchian_upper_20'], errors='coerce').fillna(close_s)
            donchian_lower_s = pd.to_numeric(df_coin['donchian_lower_20'], errors='coerce').fillna(close_s)
            slope_s = pd.to_numeric(df_coin['trendline_slope'], errors='coerce').fillna(0)
            support_s = pd.to_numeric(df_coin['support_price'], errors='coerce').fillna(close_s)
            resistance_s = pd.to_numeric(df_coin['resistance_price'], errors='coerce').fillna(close_s)
            obv_s = pd.to_numeric(df_coin['obv'], errors='coerce').fillna(0)
            vwap_20_s = pd.to_numeric(df_coin['vwap_20'], errors='coerce').fillna(close_s)
            rsi14_s = pd.to_numeric(df_coin['rsi_14'], errors='coerce').fillna(50)
            rsi6_s = pd.to_numeric(df_coin['rsi_6'], errors='coerce').fillna(50)


            vol_sma20 = vol_s.rolling(20).mean().fillna(0)
            vol_ratio = vol_s / vol_sma20.replace(0, 1) # Ersetze 0 durch 1, um Division by Zero zu vermeiden
            
            bb_width = (boll_upper_s - boll_lower_s) / boll_lower_s.replace(0, 1)
            bb_pos = (close_s - boll_lower_s) / (boll_upper_s - boll_lower_s).replace(0, 1)
            
            dist_ema200 = (close_s / ema200_s.replace(0, 1)) - 1
            dist_ema9_21 = (ema9_s / ema21_s.replace(0, 1)) - 1
            
            dist_kama9 = (close_s / kama9_s.replace(0, 1)) - 1
            dist_kama21 = (close_s / kama21_s.replace(0, 1)) - 1
            dist_kama55 = (close_s / kama55_s.replace(0, 1)) - 1
            dist_kama9_21 = (kama9_s / kama21_s.replace(0, 1)) - 1
            
            dist_donch_up = (close_s / donchian_upper_s.replace(0, 1)) - 1
            dist_donch_low = (close_s / donchian_lower_s.replace(0, 1)) - 1
            
            rsi_ratio = rsi6_s / rsi14_s.replace(0, 1)
            
            slope_norm = (slope_s / close_s.replace(0, 1)) * 1000
            
            dist_supp = (close_s - support_s) / close_s.replace(0, 1)
            dist_res = (resistance_s - close_s) / close_s.replace(0, 1)

            # === BEARISH-SPEZIFISCHE BINARY FEATURES ===
            macd_cross_bearish = ( (macd_dif_s.shift(1) >= macd_dea_s.shift(1)) & (macd_dif_s < macd_dea_s) ).astype(int).fillna(0)
            ema9_21_cross_bearish = ( (ema9_s.shift(1) >= ema21_s.shift(1)) & (ema9_s < ema21_s) ).astype(int).fillna(0)
            kama9_21_cross_bearish = ( (kama9_s.shift(1) >= kama21_s.shift(1)) & (kama9_s < kama21_s) ).astype(int).fillna(0)
            bollinger_lower_break = (close_s < boll_lower_s).astype(int).fillna(0)
            close_below_ema50 = (close_s < ema50_s).astype(int).fillna(0)

            # === NEUE VOLUME-SPEZIFISCHE FEATURES ===
            obv_sma20 = obv_s.rolling(20).mean().fillna(0)
            obv_ratio = obv_s / obv_sma20.replace(0, 1)
            
            close_to_vwap_pct = (close_s / vwap_20_s.replace(0, 1)) - 1
            
            vol_20_sma = vol_s.rolling(20).mean().fillna(0)
            volume_spike = (vol_s > vol_20_sma * 2).astype(int).fillna(0)
            volume_trend_up = (vol_s.rolling(5).mean() > vol_s.rolling(20).mean()).astype(int).fillna(0)


            # Alle berechneten Features in einen DataFrame packen, um sie leicht an data anzuhängen
            calculated_features_df = pd.DataFrame({
                'vol_ratio': vol_ratio, 'bb_width': bb_width, 'bb_pos': bb_pos,
                'dist_ema200': dist_ema200, 'dist_ema9_21': dist_ema9_21, 
                'dist_kama9': dist_kama9, 'dist_kama21': dist_kama21, 'dist_kama55': dist_kama55,
                'dist_kama9_21': dist_kama9_21, 'dist_donch_up': dist_donch_up, 'dist_donch_low': dist_donch_low,
                'rsi_ratio': rsi_ratio, 'slope_norm': slope_norm, 'dist_supp': dist_supp, 'dist_res': dist_res,
                'macd_cross_bearish': macd_cross_bearish, 'ema9_21_cross_bearish': ema9_21_cross_bearish,
                'kama9_21_cross_bearish': kama9_21_cross_bearish, 'bollinger_lower_break': bollinger_lower_break,
                'close_below_ema50': close_below_ema50,
                'obv_ratio': obv_ratio, 'close_to_vwap_pct': close_to_vwap_pct, 'obv_val': obv_s,
                'volume_spike': volume_spike, 'volume_trend_up': volume_trend_up
            })
            
            # Daten und berechnete Features zusammenführen
            df_final = pd.concat([df_coin, calculated_features_df], axis=1)
            data = df_final.to_numpy(dtype=object) # Konvertiere zurück zu numpy

            # Helper zum Extrahieren der Features
            def get_features(r_df): # Jetzt nimmt es einen DataFrame-Row
                return {
                    "rsi_14": pd.to_numeric(r_df['rsi_14'], errors='coerce'), "rsi_6": pd.to_numeric(r_df['rsi_6'], errors='coerce'),
                    "macd_hist": pd.to_numeric(r_df['macd_dif_normal_12_26_9'], errors='coerce') - pd.to_numeric(r_df['macd_dea_normal_12_26_9'], errors='coerce') if pd.to_numeric(r_df['macd_dif_normal_12_26_9'], errors='coerce') is not None else 0,
                    "atr_pct": (pd.to_numeric(r_df['atr_14'], errors='coerce') / pd.to_numeric(r_df['close'], errors='coerce')) * 100 if pd.to_numeric(r_df['close'], errors='coerce') else 0,
                    
                    "vol_ratio": r_df['vol_ratio'], "bb_width": r_df['bb_width'], "bb_pos": r_df['bb_pos'],
                    "dist_ema200": r_df['dist_ema200'], "dist_ema9_21": r_df['dist_ema9_21'],
                    "dist_kama9": r_df['dist_kama9'], "dist_kama21": r_df['dist_kama21'], "dist_kama55": r_df['dist_kama55'],
                    "dist_kama9_21": r_df['dist_kama9_21'], 
                    "dist_donch_up": r_df['dist_donch_up'], "dist_donch_low": r_df['dist_donch_low'],
                    "rsi_ratio": r_df['rsi_ratio'], "slope_norm": r_df['slope_norm'], 
                    "dist_supp": r_df['dist_supp'], "dist_res": r_df['dist_res'],
                    
                    "macd_cross_bearish": r_df['macd_cross_bearish'],
                    "ema9_21_cross_bearish": r_df['ema9_21_cross_bearish'],
                    "kama9_21_cross_bearish": r_df['kama9_21_cross_bearish'],
                    "bollinger_lower_break": r_df['bollinger_lower_break'],
                    "close_below_ema50": r_df['close_below_ema50'],
                    
                    "obv_ratio": r_df['obv_ratio'],
                    "close_to_vwap_pct": r_df['close_to_vwap_pct'],
                    "obv_val": r_df['obv_val'],
                    "volume_spike": r_df['volume_spike'],
                    "volume_trend_up": r_df['volume_trend_up']
                }

            # Simulation (NUR LONG TRADES)
            # Iteration jetzt über den DataFrame
            for i in range(50, len(df_final) - MAX_HOLD_HOURS):
                row_df = df_final.iloc[i]
                prev_df = df_final.iloc[i-1]
                
                tsi_val = pd.to_numeric(row_df['tsi_fast_12_7_7'], errors='coerce')
                tsi_sig = pd.to_numeric(row_df['tsi_fast_12_7_7_signal'], errors='coerce')
                prev_tsi_val = pd.to_numeric(prev_df['tsi_fast_12_7_7'], errors='coerce')
                prev_tsi_sig = pd.to_numeric(prev_df['tsi_fast_12_7_7_signal'], errors='coerce')
                
                if np.isnan(tsi_val) or np.isnan(tsi_sig) or np.isnan(prev_tsi_val) or np.isnan(prev_tsi_sig): continue

                signal = None
                if tsi_val > tsi_sig and prev_tsi_val <= prev_tsi_sig: signal = "long"
                
                if signal == "long":
                    entry_price = pd.to_numeric(row_df['close'], errors='coerce')
                    if np.isnan(entry_price): continue

                    result_pnl = 0
                    outcome = "timeout"
                    
                    take_profit_pct = TP_LONG_PCT
                    stop_loss_pct = SL_LONG_PCT

                    for f in range(1, MAX_HOLD_HOURS + 1):
                        future_row_df = df_final.iloc[i + f]
                        high = pd.to_numeric(future_row_df['high'], errors='coerce')
                        low = pd.to_numeric(future_row_df['low'], errors='coerce')
                        
                        if np.isnan(high) or np.isnan(low): break 
                        
                        if high >= entry_price * (1 + take_profit_pct):
                            result_pnl = take_profit_pct; outcome = "tp"; break
                        if low <= entry_price * (1 - stop_loss_pct):
                            result_pnl = -stop_loss_pct; outcome = "sl"; break
                    
                    if outcome == "timeout":
                        exit_price = pd.to_numeric(df_final.iloc[i + MAX_HOLD_HOURS]['close'], errors='coerce')
                        if np.isnan(exit_price): continue
                        raw_pnl = (exit_price - entry_price) / entry_price
                        result_pnl = raw_pnl
                    
                    all_signals.append({
                        "coin": coin.replace("USDT", ""),
                        "direction": "long",
                        "entry_time": row_df['open_time'],
                        "outcome": outcome,
                        "pnl_pct": result_pnl * 100,
                        "pnl_$": result_pnl * 100 * 20,
                        **get_features(row_df) # Hier den DataFrame-Row übergeben
                    })

        except Exception as e: 
            print(f"Fehler bei Coin {coin}: {e}")
            pass

async def main():
    pool = await asyncpg.create_pool(**DB_CONFIG)
    print("Starte LONG-ONLY Export (2.5/1.5) mit erweiterten Features (OBV/VWAP berechnet)...")
    for i, coin in enumerate(coins):
        if i % 50 == 0: print(f"{i} Coins processed...")
        await export_signals(pool, coin)
    await pool.close()
    
    if all_signals:
        df = pd.DataFrame(all_signals)
        df.to_csv("tsi_signals_long_only.csv", index=False)
        print(f"\nFertig! {len(all_signals)} Long-Trades exportiert in 'tsi_signals_long_only.csv'")

asyncio.run(main())

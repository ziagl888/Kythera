import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import matplotlib

matplotlib.use('Agg')
import datetime
import logging
import os
import time

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import yfinance as yf

from core import config as _kcfg  # channel ids
from core.database import get_db_connection
from core.market_utils import calculate_pivots, check_cooldown, update_cooldown

logging.basicConfig(level=logging.INFO, format='%(asctime)s - SMC_BOT - %(message)s')
logger = logging.getLogger(__name__)

CHART_DIR = "generated_charts"
os.makedirs(CHART_DIR, exist_ok=True)

# 🛠️ CONFIGURATION
SMC_TIMEFRAMES = ['15m', '30m', '1h', '2h', '4h', '1d', '1w']

# P1.27: Cooldown muss mindestens eine Kerzendauer betragen — sonst refired
# dieselbe 1d/1w-Kerze innerhalb ihrer eigenen Laufzeit mehrfach (12h-Default
# deckt nur 15m..4h ab). 1d → 24h, 1w → 7d.
COOLDOWN_HOURS = {'1d': 24, '1w': 168}

MARKETS = {
    "METALS": {
        "channel_id": _kcfg.CH_SMC_METALS,
        "source": "database",
        "pairs": ['XAUUSDT', 'XAGUSDT', 'PAXGUSDT', 'BTCUSDT', 'ETHUSDT', 'TRXUSDT', 'SOLUSDT', 'XRPUSDT'],
    },
    "FOREX": {
        "channel_id": _kcfg.CH_SMC_FOREX,
        "source": "yfinance",
        "pairs": [
            'EURUSD=X',
            'GBPUSD=X',
            'JPY=X',
            'AUDUSD=X',
            'USDCAD=X',
            'USDCHF=X',
            'NZDUSD=X',
            'EURGBP=X',
            'GBPJPY=X',
            'GC=F',
            'SI=F',
        ],
    },
}


# 📊 DATA FETCHING
def fetch_db_data(conn, symbol, tf):
    try:
        query = (
            f'SELECT open_time, open, high, low, close, volume FROM "{symbol}_{tf}" ORDER BY open_time DESC LIMIT 300'
        )
        df = pd.read_sql_query(query, conn)
        if df.empty:
            return df
        # Sort database data chronologically!
        df = df.iloc[::-1].reset_index(drop=True)
        return df
    except Exception as e:
        logger.error(f"Error loading aus DB für {symbol} ({tf}): {e}")
        return pd.DataFrame()


def fetch_yfinance_data(ticker, tf):
    try:
        yf_interval, period, resample_tf = '1h', '60d', None

        if tf == '15m':
            yf_interval = '15m'
            period = '30d'
        elif tf == '30m':
            yf_interval = '30m'
            period = '30d'
        elif tf == '1h':
            yf_interval = '1h'
            period = '60d'
        elif tf == '2h':
            yf_interval, resample_tf = '1h', '2h'
        elif tf == '4h':
            yf_interval, resample_tf = '1h', '4h'
        elif tf == '1d':
            yf_interval, period = '1d', '200d'
        elif tf == '1w':
            yf_interval, period = '1wk', '400d'
        else:
            return pd.DataFrame()

        df = yf.download(ticker, interval=yf_interval, period=period, progress=False)
        if df.empty:
            return df

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if resample_tf:
            df = (
                df.resample(resample_tf)
                .agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'})
                .dropna()
            )

        df = df.reset_index()
        col_map = {
            'Datetime': 'open_time',
            'Date': 'open_time',
            'Open': 'open',
            'High': 'high',
            'Low': 'low',
            'Close': 'close',
            'Volume': 'volume',
        }
        df.rename(columns=col_map, inplace=True)

        # Timezone magic: ensure everything is exactly UTC
        if df['open_time'].dt.tz is not None:
            df['open_time'] = df['open_time'].dt.tz_convert('UTC').dt.tz_localize(None)

        for c in ['open', 'high', 'low', 'close']:
            df[c] = df[c].astype(float)

        # Drop only NaN (invalid) values that arise on weekends.
        # P1.27: Die forming (letzte) Kerze wird NICHT mehr hier behalten — sie
        # wird zentral in run_smc_analysis für beide Datenquellen abgeschnitten.
        df.dropna(subset=['close'], inplace=True)
        df = df.reset_index(drop=True)

        return df
    except Exception as e:
        logger.error(f"YFinance Error for {ticker} ({tf}): {e}")
        return pd.DataFrame()


# 🧠 SMC MATHEMATIK


def find_unmitigated_fvgs(df, direction="BULLISH"):
    fvgs = []
    for i in range(2, len(df) - 1):
        if direction == "BULLISH":
            if df['high'].iloc[i - 2] < df['low'].iloc[i] and df['close'].iloc[i - 1] > df['open'].iloc[i - 1]:
                fvgs.append({'top': df['low'].iloc[i], 'bottom': df['high'].iloc[i - 2], 'index': i})
        else:
            if df['low'].iloc[i - 2] > df['high'].iloc[i] and df['close'].iloc[i - 1] < df['open'].iloc[i - 1]:
                fvgs.append({'top': df['low'].iloc[i - 2], 'bottom': df['high'].iloc[i], 'index': i})

    unmitigated = []
    curr_idx = len(df) - 1
    for fvg in fvgs:
        is_mitigated = False
        # P1.26: The mitigation scan must stop BEFORE the current candle. The FVG
        # entry trigger in run_smc_analysis re-evaluates the very same predicate
        # (low <= top / high >= bottom) on df.iloc[curr_idx]. Including curr_idx
        # here removed exactly those FVGs that would fire, so the entry could
        # never trigger (dead code). Older candles still mitigate as before.
        for j in range(fvg['index'] + 1, curr_idx):
            if direction == "BULLISH" and df['low'].iloc[j] <= fvg['top']:
                is_mitigated = True
                break
            if direction == "BEARISH" and df['high'].iloc[j] >= fvg['bottom']:
                is_mitigated = True
                break
        if not is_mitigated:
            unmitigated.append(fvg)
    return unmitigated


# 🎨 PROFICHART (SMC DESIGN)
def generate_smc_chart(df, symbol, tf, setup_type, level_data, is_bullish, supports, resistances):
    try:
        LOOKBACK = 100
        PADDING_CANDLES = 12

        start_plot_idx = max(0, len(df) - LOOKBACK)
        plot_df = df.iloc[start_plot_idx:].copy()

        plot_df['open_time'] = pd.to_datetime(plot_df['open_time']).dt.tz_localize(None)
        plot_df.set_index('open_time', inplace=True)

        if len(plot_df) > 1:
            time_step = plot_df.index[-1] - plot_df.index[-2]
            future_dates = [plot_df.index[-1] + time_step * i for i in range(1, PADDING_CANDLES + 1)]
            empty_df = pd.DataFrame(index=future_dates, columns=plot_df.columns)
            plot_df = pd.concat([plot_df, empty_df]).astype(float)

        mc = mpf.make_marketcolors(up='#00ff88', down='#ff4466', edge='inherit', wick='inherit')
        s = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds', gridstyle='')

        alines = []
        colors = []
        linewidths = []
        end_time = plot_df.index[-1]

        for idx, val in supports:
            if idx >= start_plot_idx:
                pivot_time = pd.to_datetime(df['open_time'].iloc[idx]).tz_localize(None)
                line_end_time = end_time
                for i in range(idx + 1, len(df)):
                    if df['low'].iloc[i] <= val:
                        line_end_time = pd.to_datetime(df['open_time'].iloc[i]).tz_localize(None)
                        break
                alines.append([(pivot_time, float(val)), (line_end_time, float(val))])
                colors.append('#ffd700')
                linewidths.append(0.8)

        for idx, val in resistances:
            if idx >= start_plot_idx:
                pivot_time = pd.to_datetime(df['open_time'].iloc[idx]).tz_localize(None)
                line_end_time = end_time
                for i in range(idx + 1, len(df)):
                    if df['high'].iloc[i] >= val:
                        line_end_time = pd.to_datetime(df['open_time'].iloc[i]).tz_localize(None)
                        break
                alines.append([(pivot_time, float(val)), (line_end_time, float(val))])
                colors.append('#00ffff')
                linewidths.append(0.8)

        color_theme = '#00ff88' if is_bullish else '#ff4466'

        if setup_type == "FVG":
            start_time = pd.to_datetime(df['open_time'].iloc[level_data['index'] - 2]).tz_localize(None)
            alines.append([(start_time, float(level_data['top'])), (end_time, float(level_data['top']))])
            alines.append([(start_time, float(level_data['bottom'])), (end_time, float(level_data['bottom']))])
            colors.extend([color_theme, color_theme])
            linewidths.extend([2.0, 2.0])
            title = f"SMC Mitigation FVG: {symbol} ({tf})"

        elif setup_type == "STRUCTURE":
            start_time = pd.to_datetime(df['open_time'].iloc[level_data['index']]).tz_localize(None)
            val = float(level_data['price'])
            alines.append([(start_time, val), (end_time, val)])
            colors.append(color_theme)
            linewidths.append(2.5)
            title = f"SMC Structure Shift: {symbol} ({tf})"

        filename = os.path.abspath(f"{CHART_DIR}/SMC_{setup_type}_{symbol}_{tf}_{int(time.time())}.png")

        mpf.plot(
            plot_df,
            type='candle',
            style=s,
            alines=dict(alines=alines, colors=colors, linewidths=linewidths, linestyle='--'),
            title=f"\n{title}",
            figsize=(12, 8),
            tight_layout=True,
            savefig=filename,
            returnfig=False,
        )
        return filename
    except Exception as e:
        logger.error(f"Chart Error: {e}")
        return None
    finally:
        # Schließt die von mpf.plot offen gelassene Figure — verhindert RAM-Leak.
        plt.close('all')


# 🚀 CORE ENGINE
# FIX (#33/#34/#51): Die eigene is_cooled_down-Funktion entfernt.
# Vorher hatte sie drei Probleme:
#   1. Sie vermischte Check + Update (Seiteneffekt beim Check) → wenn der Send
#      daafter fehlschlug, blieb trotzdem ein Cooldown-Eintrag stehen.
#   2. `except: return True` → bei DB-Fehlern durfte alles traden.
#   3. module=f"SMC_{tf}" erlaubte dasselbe Coin/Direction gleichzeitig auf
#      1h UND 4h zu feuern (doppeltes Signal).
# Jetzt: check_cooldown/update_cooldown aus market_utils, Cooldown-Key ohne
# TF-Suffix (TF-übergreifend), update erst NACH erfolgreichem Send.


def send_signal(conn, channel, symbol, direction, price, targets, sl, setup_type, chart_path, tf, details):
    targets = [float(t) for t in targets]
    sl = float(sl)
    module_tag = f"SMC_{tf.upper()}"
    emoji = "🏦 SMC STRUCTURE SHIFT" if setup_type == "STRUCTURE" else "🏦 SMC MITIGATION (FVG)"

    lines = [
        f"📈 SMC Signal for {symbol} 📈",
        f"🚨 Direction: {direction}",
        "🚨 Leverage: 20x-10x",
        f"🏦 CMP Entry: $ {price:.5f}",
        f"🏦 Entry 2: $ {price * (0.98 if direction == 'LONG' else 1.02):.5f}",
    ]
    for i, t in enumerate(targets[:3], 1):
        lines.append(f"💰 TP{i}: $ {t:.5f}")
    lines += [f"💸 Stop Loss: $ {sl:.5f}", f"🧠 Trade idea generated by AI module {module_tag}"]
    cornix_msg = "\n".join(lines)

    html = f"""<pre><b>{emoji}</b>\n<b>{symbol} | {tf} Chart</b>\n<b>→ Direction: <b>{direction}</b></b>\n<b>→ Detail: {details}</b>\n<b>→ Entry: {price:.5f}</b>\n<b>→ Targets:</b> {', '.join([f'{t:.5f}' for t in targets[:3]])}</pre>\n<pre>{cornix_msg}</pre>"""

    with conn.cursor() as cur:
        if chart_path:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                (channel, html, chart_path),
            )
        else:
            cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (channel, html))
    conn.commit()


def run_smc_analysis():
    logger.info("🔍 Starting SMC Scan (FVG & Structure Shifts)...")
    conn = get_db_connection()
    conn.autocommit = True

    for _market_name, config in MARKETS.items():
        channel_id = config["channel_id"]
        source = config["source"]
        pairs = config["pairs"]

        for symbol in pairs:
            display_name = symbol.replace('=X', '').replace('=F', '').replace('USDT', '')
            if symbol == 'GC=F':
                display_name = 'GOLD'
            elif symbol == 'SI=F':
                display_name = 'SILVER'
            elif symbol == 'JPY=X':
                display_name = 'USDJPY'

            for tf in SMC_TIMEFRAMES:
                try:
                    df = fetch_db_data(conn, symbol, tf) if source == "database" else fetch_yfinance_data(symbol, tf)
                    # P1.27: letzte (noch offene) Kerze für BEIDE Datenquellen droppen —
                    # sonst hält eine forming 1d/1w-Kerze die Signal-Bedingung tagelang
                    # und refired über den Cooldown die ganze Woche.
                    df = df.iloc[:-1].reset_index(drop=True)
                    if df.empty or len(df) < 50:
                        continue

                    # P1.27: TF-abhängiger Cooldown (≥ eine Kerzendauer).
                    cd_hours = COOLDOWN_HOURS.get(tf, 12)

                    curr_idx = len(df) - 1
                    curr_candle = df.iloc[curr_idx]
                    price = float(curr_candle['close'])

                    supports, resistances = calculate_pivots(df, window=5)

                    # 1. STRUCTURE SHIFTS (BOS / CHoCH)
                    recent_res = [r for r in resistances if r[0] < curr_idx - 1]
                    recent_sup = [s for s in supports if s[0] < curr_idx - 1]

                    if recent_res and recent_sup:
                        last_res_idx, last_res_val = recent_res[-1]
                        last_sup_idx, last_sup_val = recent_sup[-1]

                        # BULLISH SHIFT
                        if price > last_res_val and df['open'].iloc[curr_idx] <= last_res_val:
                            # Cooldown-Key ohne TF → gleicher Coin/Direction kann
                            # nicht gleichzeitig auf 1h und 4h feuern.
                            cd_key = "SMC_BOS"
                            if not check_cooldown(conn, cd_key, display_name, 'LONG', cd_hours):
                                logger.info(f"🏛️ BULLISH SHIFT: {display_name} ({tf}) breaks {last_res_val:.4f}")
                                tgts = sorted([v for i, v in resistances if v > price])[:5] or [
                                    price * 1.01,
                                    price * 1.02,
                                ]
                                sl = last_sup_val * 0.998
                                c_path = generate_smc_chart(
                                    df,
                                    display_name,
                                    tf,
                                    "STRUCTURE",
                                    {'price': last_res_val, 'index': last_res_idx},
                                    True,
                                    supports,
                                    resistances,
                                )
                                send_signal(
                                    conn,
                                    channel_id,
                                    display_name,
                                    "LONG",
                                    price,
                                    tgts,
                                    sl,
                                    "STRUCTURE",
                                    c_path,
                                    tf,
                                    f"Break of Swing High @ {last_res_val:.5f}",
                                )
                                update_cooldown(conn, cd_key, display_name, 'LONG')

                        # BEARISH SHIFT
                        elif price < last_sup_val and df['open'].iloc[curr_idx] >= last_sup_val:
                            cd_key = "SMC_BOS"
                            if not check_cooldown(conn, cd_key, display_name, 'SHORT', cd_hours):
                                logger.info(f"🏛️ BEARISH SHIFT: {display_name} ({tf}) breaks {last_sup_val:.4f}")
                                tgts = sorted([v for i, v in supports if v < price], reverse=True)[:5] or [
                                    price * 0.99,
                                    price * 0.98,
                                ]
                                sl = last_res_val * 1.002
                                c_path = generate_smc_chart(
                                    df,
                                    display_name,
                                    tf,
                                    "STRUCTURE",
                                    {'price': last_sup_val, 'index': last_sup_idx},
                                    False,
                                    supports,
                                    resistances,
                                )
                                send_signal(
                                    conn,
                                    channel_id,
                                    display_name,
                                    "SHORT",
                                    price,
                                    tgts,
                                    sl,
                                    "STRUCTURE",
                                    c_path,
                                    tf,
                                    f"Break of Swing Low @ {last_sup_val:.5f}",
                                )
                                update_cooldown(conn, cd_key, display_name, 'SHORT')

                    # 2. FVG MITIGATION
                    bull_fvgs = find_unmitigated_fvgs(df, "BULLISH")
                    for fvg in bull_fvgs[-2:]:
                        if curr_candle['low'] <= fvg['top'] and price > (fvg['bottom'] * 0.999):
                            cd_key = "SMC_FVG"
                            if not check_cooldown(conn, cd_key, display_name, 'LONG', cd_hours):
                                tgts = sorted([v for i, v in resistances if v > price])[:5] or [
                                    price * 1.01,
                                    price * 1.02,
                                ]
                                sl = fvg['bottom'] * 0.998
                                c_path = generate_smc_chart(
                                    df, display_name, tf, "FVG", fvg, True, supports, resistances
                                )
                                send_signal(
                                    conn,
                                    channel_id,
                                    display_name,
                                    "LONG",
                                    price,
                                    tgts,
                                    sl,
                                    "FVG",
                                    c_path,
                                    tf,
                                    f"Tapped into BISI @ {fvg['top']:.5f}",
                                )
                                update_cooldown(conn, cd_key, display_name, 'LONG')

                    bear_fvgs = find_unmitigated_fvgs(df, "BEARISH")
                    for fvg in bear_fvgs[-2:]:
                        if curr_candle['high'] >= fvg['bottom'] and price < (fvg['top'] * 1.001):
                            cd_key = "SMC_FVG"
                            if not check_cooldown(conn, cd_key, display_name, 'SHORT', cd_hours):
                                tgts = sorted([v for i, v in supports if v < price], reverse=True)[:5] or [
                                    price * 0.99,
                                    price * 0.98,
                                ]
                                sl = fvg['top'] * 1.002
                                c_path = generate_smc_chart(
                                    df, display_name, tf, "FVG", fvg, False, supports, resistances
                                )
                                send_signal(
                                    conn,
                                    channel_id,
                                    display_name,
                                    "SHORT",
                                    price,
                                    tgts,
                                    sl,
                                    "FVG",
                                    c_path,
                                    tf,
                                    f"Tapped into SIBI @ {fvg['bottom']:.5f}",
                                )
                                update_cooldown(conn, cd_key, display_name, 'SHORT')

                except Exception as e:
                    logger.error(f"Error for Analyse von {symbol} ({tf}): {e}")

    conn.close()


def main():
    logger.info("=== 🏦 SMC TRADFI & METALS BOT GESTARTET ===")
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        # 💥 FIX 3: Auf Minute 05, 20, 35, 50 verschoben, um YFinance Puffer zu geben!
        if now.minute in [5, 20, 35, 50]:
            run_smc_analysis()
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

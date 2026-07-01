import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import time
import datetime
import logging
import pandas as pd
import numpy as np
import scipy.signal
import yfinance as yf
import mplfinance as mpf
import os

from core.market_utils import calculate_pivots, check_cooldown, update_cooldown

from core.database import get_db_connection
from core import config as _kcfg  # channel ids

logging.basicConfig(level=logging.INFO, format='%(asctime)s - TRADFI_SMC_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
SMC_CHANNEL_ID = _kcfg.CH_MAYANK  # Dein gewünschter Channel
TIMEFRAMES = ['1h', '4h']
#ASSETS = {
#    'XAUUSD=X': 'GOLD',
#    'USDJPY=X': 'USDJPY'
#}
ASSETS = {
    'GC=F': 'GOLD',      # Comex Gold Futures (Genauester Chart für Gold bei YFinance)
    'SI=F': 'SILVER',    # Comex Silver Futures (falls du Silber auch willst)
    'JPY=X': 'USDJPY',   # USD/JPY
    'EURUSD=X': 'EURUSD' # EUR/USD (falls du den auch scannen willst)
}
CHART_DIR = "generated_charts"
os.makedirs(CHART_DIR, exist_ok=True)


# 📊 DATA FETCHING (YFinance)
def fetch_yfinance_data(ticker, tf):
    """Fetches TradFi data and resamples it if needed."""
    try:
        yf_interval = '1h'
        period = '60d'
        resample_tf = None

        if tf == '1h':
            yf_interval = '1h'
        elif tf == '4h':
            yf_interval = '1h'
            resample_tf = '4h'

        df = yf.download(ticker, interval=yf_interval, period=period, progress=False)
        if df.empty: return df

        # YFinance MultiIndex Header Fix
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if resample_tf:
            df = df.resample(resample_tf).agg({
                'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
            }).dropna()

        df = df.reset_index()
        col_map = {'Datetime': 'open_time', 'Date': 'open_time', 'Open': 'open', 'High': 'high', 'Low': 'low',
                   'Close': 'close', 'Volume': 'volume'}
        df.rename(columns=col_map, inplace=True)

        if df['open_time'].dt.tz is not None:
            df['open_time'] = df['open_time'].dt.tz_convert('UTC').dt.tz_localize(None)

        for c in ['open', 'high', 'low', 'close']:
            df[c] = df[c].astype(float)

        # Drop the very last candle if it has not yet closed
        if not df.empty:
            df = df.iloc[:-1].reset_index(drop=True)

        return df
    except Exception as e:
        logger.error(f"YFinance Error for {ticker} ({tf}): {e}")
        return pd.DataFrame()


# 🧠 TRADINGVIEW PIVOT POINTS
# calculate_pivots kommt jetzt aus core.market_utils (Refactoring).
# Signature is identical: calculate_pivots(df, window=5) -> (supports, resistances)
# mit jeweils [(idx, price), ...]


def is_touching_pivot(price, pivots, max_idx, threshold=0.0005):
    """Checks if price touches a pivot (0.05% tolerance)."""
    for idx, p_val in pivots:
        if idx < max_idx:  # Pivot muss vor der aktuellen Kerze entstanden sein
            if abs(price - p_val) / p_val <= threshold:
                return True
    return False


# 🎯 STRATEGIE & CHARTING
# 🎯 STRATEGIE & CHARTING
def generate_setup_chart(df, symbol, tf, fvg, supports, resistances, direction):
    """Generiert den Chart der letzten 7 Tage inklusive Pivot-Linien (die bei Mitigation enden)."""
    try:
        # Für 7 Tage: 168 Kerzen bei 1h, 42 bei 4h
        lookback = 168 if tf == '1h' else 42
        PADDING_CANDLES = 12  # Abstand after rechts

        start_plot_idx = max(0, len(df) - lookback)
        plot_df = df.iloc[start_plot_idx:].copy()

        # Zeitzone für mplfinance entfernen
        plot_df['open_time'] = pd.to_datetime(plot_df['open_time']).dt.tz_localize(None)
        plot_df.set_index('open_time', inplace=True)

        # Rechter Abstand (Padding) erzeugen und NaN/Object Fehler verhindern
        if len(plot_df) > 1:
            time_step = plot_df.index[-1] - plot_df.index[-2]
            future_dates = [plot_df.index[-1] + time_step * i for i in range(1, PADDING_CANDLES + 1)]
            empty_df = pd.DataFrame(index=future_dates, columns=plot_df.columns)
            plot_df = pd.concat([plot_df, empty_df])
            plot_df = plot_df.astype(float)  # 💥 WICHTIG: Verhindert den Chart-Crash!

        mc = mpf.make_marketcolors(up='#00ff88', down='#ff4466', edge='inherit', wick='inherit')
        # Gitter komplett ausblenden
        s = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds', gridstyle='')

        alines = []
        colors = []
        linewidths = []

        end_time = plot_df.index[-1]  # Das ist jetzt die "Zukunft" auf der rechten Seite

        # 1. Pivot Linien einzeichnen (Mitigation Logik)
        for idx, val in supports:
            if idx >= start_plot_idx:
                pivot_time = pd.to_datetime(df['open_time'].iloc[idx]).tz_localize(None)
                line_end_time = end_time

                # Prüfe, ob die Linie später gebrochen/mitigiert wurde
                for i in range(idx + 1, len(df)):
                    if df['low'].iloc[i] <= val:
                        line_end_time = pd.to_datetime(df['open_time'].iloc[i]).tz_localize(None)
                        break

                alines.append([(pivot_time, float(val)), (line_end_time, float(val))])
                colors.append('#ffd700')  # Gold für Support
                linewidths.append(0.8)

        for idx, val in resistances:
            if idx >= start_plot_idx:
                pivot_time = pd.to_datetime(df['open_time'].iloc[idx]).tz_localize(None)
                line_end_time = end_time

                # Prüfe, ob die Linie später gebrochen/mitigiert wurde
                for i in range(idx + 1, len(df)):
                    if df['high'].iloc[i] >= val:
                        line_end_time = pd.to_datetime(df['open_time'].iloc[i]).tz_localize(None)
                        break

                alines.append([(pivot_time, float(val)), (line_end_time, float(val))])
                colors.append('#00ffff')  # Cyan für Resistance
                linewidths.append(0.8)

        # 2. FVG Box/Linien einzeichnen
        fvg_color = '#00ff88' if direction == "LONG" else '#ff4466'
        fvg_start_time = pd.to_datetime(df['open_time'].iloc[fvg['index'] - 2]).tz_localize(None)

        # FVG Top
        alines.append([(fvg_start_time, float(fvg['top'])), (end_time, float(fvg['top']))])
        colors.append(fvg_color)
        linewidths.append(2.0)

        # FVG Bottom
        alines.append([(fvg_start_time, float(fvg['bottom'])), (end_time, float(fvg['bottom']))])
        colors.append(fvg_color)
        linewidths.append(2.0)

        filename = f"{CHART_DIR}/SMC_PIVOT_{symbol}_{tf}_{int(time.time())}.png"

        mpf.plot(
            plot_df,
            type='candle',
            style=s,
            # Ein generelles linestyle für alle Linien, individuelle Breiten
            alines=dict(alines=alines, colors=colors, linewidths=linewidths, linestyle='--'),
            title=f"\nSMC Pivot Retest: {symbol} ({tf})",
            figsize=(14, 8),
            tight_layout=True,
            savefig=filename,
            returnfig=False
        )
        return filename
    except Exception as e:
        logger.error(f"Chart Error for {symbol}: {e}")
        return None


def analyze_strategy():
    logger.info("🔍 Analysing SMC Pivot Strategie...")

    for ticker, symbol_name in ASSETS.items():
        for tf in TIMEFRAMES:
            try:
                df = fetch_yfinance_data(ticker, tf)
                if df.empty or len(df) < 50: continue

                supports, resistances = calculate_pivots(df, window=5)

                # Wir analysieren die allerletzte geschlossene Kerze
                curr_idx = len(df) - 1
                curr_candle = df.iloc[curr_idx]
                curr_low = curr_candle['low']
                curr_high = curr_candle['high']
                curr_price = curr_candle['close']

                # 🟢 LONG SETUP PRÜFEN
                # Searching after bullischen FVGs in der Vergangenheit
                for i in range(2, curr_idx):
                    # Ist es ein bullisches FVG? (High[i-2] < Low[i])
                    if df['high'].iloc[i - 2] < df['low'].iloc[i] and df['close'].iloc[i - 1] > df['open'].iloc[i - 1]:
                        gap_top = df['low'].iloc[i]
                        gap_bottom = df['high'].iloc[i - 2]

                        # BEDINGUNG 1: Hat Kerze i-2 (oder i-1) einen Support-Pivot berührt?
                        candle_1_low = df['low'].iloc[i - 2]
                        if is_touching_pivot(candle_1_low, supports, i - 2, threshold=0.001):

                            # BEDINGUNG 2: Wurde das FVG bis jetzt noch NICHT "fully closed"?
                            # Fully closed heißt, der Preis ist auf gap_bottom oder tiefer gefallen
                            was_closed_before = False
                            for j in range(i + 1, curr_idx):
                                if df['low'].iloc[j] <= gap_bottom:
                                    was_closed_before = True
                                    break

                            if not was_closed_before:
                                # BEDINGUNG 3: Hat die JETZIGE (letzte geschlossene) Kerze das FVG fully closed?
                                if curr_low <= gap_bottom:
                                    # FIX: Cooldown-Check vor dem Senden, sonst feuert
                                    # der Bot stündlich das gleiche Signal, solange das
                                    # FVG-Kriterium erfüllt ist.
                                    module_tag = f"MAYANK_{symbol_name}_{tf.upper()}"
                                    with get_db_connection() as _cd_conn:
                                        if check_cooldown(_cd_conn, module_tag, symbol_name, "LONG", 12):
                                            logger.info(
                                                f"⏳ Cooldown active für {symbol_name} ({tf}) LONG. Skip.")
                                            break

                                    logger.info(
                                        f"🚀 BINGO LONG! {symbol_name} ({tf}) hat das FVG bei {gap_bottom:.3f} fully closed!")

                                    # Targets berechnen (nächste Resistance Pivots after oben)
                                    targets = sorted([val for idx, val in resistances if val > curr_price])[:8]
                                    if not targets: targets = [curr_price * 1.01,
                                                               curr_price * 1.02]  # Fallback für TradFi

                                    sl = curr_low * 0.998  # Knapp unter das letzte Tief
                                    chart_path = generate_setup_chart(df, symbol_name, tf,
                                                                      {'top': gap_top, 'bottom': gap_bottom,
                                                                       'index': i}, supports, resistances, "LONG")

                                    msg = f"""<pre><b>🎯 SMC PIVOT RETEST</b>\n<b>{symbol_name} | {tf} Chart</b>\n<b>→ Action: <b>LONG</b></b>\n<b>→ Entry: {curr_price:.4f}</b>\n<b>→ FVG Fully Closed: {gap_bottom:.4f}</b>\n<b>→ Stop Loss: {sl:.4f}</b>\n<b>→ Targets:</b> {', '.join([f'{t:.3f}' for t in targets[:3]])}</pre>"""

                                    # Senden via Telegram (Outbox Logik, wie in anderen Skripten)
                                    with get_db_connection() as conn:
                                        with conn.cursor() as cur:
                                            if chart_path:
                                                cur.execute(
                                                    "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                                                    (SMC_CHANNEL_ID, msg, chart_path))
                                            else:
                                                cur.execute(
                                                    "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                                                    (SMC_CHANNEL_ID, msg))
                                        conn.commit()
                                        # Cooldown setzen NACH erfolgreichem Send
                                        update_cooldown(conn, module_tag, symbol_name, "LONG")
                                    break  # Nur einmal triggern

                # 🔴 SHORT SETUP PRÜFEN
                for i in range(2, curr_idx):
                    # Ist es ein bärisches FVG? (Low[i-2] > High[i])
                    if df['low'].iloc[i - 2] > df['high'].iloc[i] and df['close'].iloc[i - 1] < df['open'].iloc[i - 1]:
                        gap_top = df['low'].iloc[i - 2]
                        gap_bottom = df['high'].iloc[i]

                        # BEDINGUNG 1: Hat Kerze i-2 einen Resistance-Pivot berührt?
                        candle_1_high = df['high'].iloc[i - 2]
                        if is_touching_pivot(candle_1_high, resistances, i - 2, threshold=0.001):

                            # BEDINGUNG 2: Wurde das FVG bis jetzt noch NICHT "fully closed"? (Preis stieg auf gap_top)
                            was_closed_before = False
                            for j in range(i + 1, curr_idx):
                                if df['high'].iloc[j] >= gap_top:
                                    was_closed_before = True
                                    break

                            if not was_closed_before:
                                # BEDINGUNG 3: Hat die JETZIGE Kerze das FVG fully closed?
                                if curr_high >= gap_top:
                                    # FIX: Cooldown-Check vor dem Senden (siehe LONG oben).
                                    module_tag = f"MAYANK_{symbol_name}_{tf.upper()}"
                                    with get_db_connection() as _cd_conn:
                                        if check_cooldown(_cd_conn, module_tag, symbol_name, "SHORT", 12):
                                            logger.info(
                                                f"⏳ Cooldown active für {symbol_name} ({tf}) SHORT. Skip.")
                                            break

                                    logger.info(
                                        f"💥 BINGO SHORT! {symbol_name} ({tf}) hat das FVG bei {gap_top:.3f} fully closed!")

                                    # Targets (nächste Support Pivots after unten)
                                    targets = sorted([val for idx, val in supports if val < curr_price], reverse=True)[
                                        :8]
                                    if not targets: targets = [curr_price * 0.99, curr_price * 0.98]

                                    sl = curr_high * 1.002
                                    chart_path = generate_setup_chart(df, symbol_name, tf,
                                                                      {'top': gap_top, 'bottom': gap_bottom,
                                                                       'index': i}, supports, resistances, "SHORT")

                                    msg = f"""<pre><b>🎯 SMC PIVOT RETEST</b>\n<b>{symbol_name} | {tf} Chart</b>\n<b>→ Action: <b>SHORT</b></b>\n<b>→ Entry: {curr_price:.4f}</b>\n<b>→ FVG Fully Closed: {gap_top:.4f}</b>\n<b>→ Stop Loss: {sl:.4f}</b>\n<b>→ Targets:</b> {', '.join([f'{t:.3f}' for t in targets[:3]])}</pre>"""

                                    with get_db_connection() as conn:
                                        with conn.cursor() as cur:
                                            if chart_path:
                                                cur.execute(
                                                    "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                                                    (SMC_CHANNEL_ID, msg, chart_path))
                                            else:
                                                cur.execute(
                                                    "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                                                    (SMC_CHANNEL_ID, msg))
                                        conn.commit()
                                        # Cooldown setzen NACH erfolgreichem Send
                                        update_cooldown(conn, module_tag, symbol_name, "SHORT")
                                    break

            except Exception as e:
                logger.error(f"Error for Analyse von {ticker} ({tf}): {e}")


def main():
    logger.info("=== 🏦 TRADFI PIVOT SMC BOT GESTARTET ===")

    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)

            # Läuft zur Minute :01 jeder Stunde (dann sind 1h und 4h Kerzen garantiert geschlossen und bei YFinance verfügbar)
            if now.minute == 1:
                analyze_strategy()
                logger.info("🏁 Durchlauf stopped. Schlafe für 60 seconds...")
                time.sleep(60)
            else:
                time.sleep(10)

        except KeyboardInterrupt:
            logger.info("🛑 Bot wird stopped (STRG+C).")
            break
        except Exception as e:
            logger.error(f"Critical error im Main-Loop: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
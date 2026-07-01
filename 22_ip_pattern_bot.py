import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import json
import logging
import os
import time
from datetime import datetime, timezone

import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd
import scipy.signal

from core import config as _kcfg  # channel ids
from core.database import get_db_connection
from core.market_utils import load_coins

# 🛠️ CONFIGURATION
logging.basicConfig(level=logging.INFO, format='%(asctime)s - INST_PATTERN_BOT - %(message)s')
logger = logging.getLogger(__name__)

# 🔴 HIER DEN NEUEN CHANNEL FÜR INSTITUTIONAL PATTERNS EINTRAGEN
INSTITUTIONAL_CHANNEL_ID = _kcfg.CH_INSTITUTIONAL

COINS_FILE = "coins.json"
CHART_DIR = "institutional_charts"
os.makedirs(CHART_DIR, exist_ok=True)

TIMEFRAME = '1h'
LOOKBACK_CANDLES = 300  # Wie weit wir in die Vergangenheit schauen
ZONE_TOLERANCE = 0.005  # 0.5% Toleranz für den Entry-Bereich am QML

# FIX: ALERTED_QMS muss persistiert werden, sonst feuert der Bot after JEDEM
# Restart ~500 Duplicate-Alerts (ein Alert pro bereits aktivem Pattern).
# Dadurch blockiert Telegram Flood Control die komplette Outbox für Stunden.
ALERTED_QMS_FILE = "alerted_qms.json"
ALERTED_QMS = set()


def load_alerted_qms():
    """Loads already-reported pattern IDs from JSON."""
    global ALERTED_QMS
    if not os.path.exists(ALERTED_QMS_FILE):
        ALERTED_QMS = set()
        logger.info("📂 No alerted_qms.json found → starting fresh.")
        return
    try:
        with open(ALERTED_QMS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        ALERTED_QMS = set(data)
        logger.info(f"✅ {len(ALERTED_QMS)} known pattern IDs loaded.")
    except Exception as e:
        logger.error(f"Error loading von {ALERTED_QMS_FILE}: {e}")
        ALERTED_QMS = set()


def save_alerted_qms():
    """Speichert die Pattern-IDs atomar auf Disk."""
    try:
        tmp = ALERTED_QMS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(ALERTED_QMS), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ALERTED_QMS_FILE)
    except Exception as e:
        logger.error(f"Error saving von {ALERTED_QMS_FILE}: {e}")


# 📡 DATEN & HILFSFUNKTIONEN


def send_telegram_alert(conn, message, image_path):
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                (INSTITUTIONAL_CHANNEL_ID, message, image_path),
            )
        conn.commit()
    except Exception as e:
        logger.error(f"Error sending in die Outbox: {e}")


# 🧠 INSTITUTIONELLE STRUKTUR (PIVOTS)
def get_alternating_pivots(df, window=5):
    """
    Findet Hochs und Tiefs und erzwingt, dass sie sich streng abwechseln (H, L, H, L).
    Das ist zwingend nötig, um Struktur-Patterns wie Quasimodo zu erkennen.
    """
    highs = df['high'].values
    lows = df['low'].values

    peak_idx = scipy.signal.argrelextrema(highs, np.greater, order=window)[0]
    trough_idx = scipy.signal.argrelextrema(lows, np.less, order=window)[0]

    # Collect all pivots: format (index, type(1=High, -1=Low), price)
    raw_pivots = [(i, 1, highs[i]) for i in peak_idx] + [(i, -1, lows[i]) for i in trough_idx]
    raw_pivots.sort(key=lambda x: x[0])

    if not raw_pivots:
        return []

    alt_pivots = [raw_pivots[0]]
    for i in range(1, len(raw_pivots)):
        curr_idx, curr_type, curr_price = raw_pivots[i]
        last_idx, last_type, last_price = alt_pivots[-1]

        if curr_type == last_type:
            # Zwei gleiche Pivots hintereinander? Behalte das extremere!
            if (curr_type == 1 and curr_price > last_price) or (curr_type == -1 and curr_price < last_price):
                alt_pivots[-1] = raw_pivots[i]
        else:
            alt_pivots.append(raw_pivots[i])

    return alt_pivots


# 🎨 CHART GENERATOR
def generate_qm_chart(df, symbol, pattern_type, p1, p2, p3, p4, qm_level):
    """
    Zeichnet den Chart, verbindet die Pivot-Punkte zu einem Zick-Zack-Muster
    und zieht eine horizontale Linie für das Quasimodo-Einstiegslevel.
    """
    try:
        # 1. Starten etwas vor dem ersten Pivot
        start_idx = max(0, p1[0] - 20)
        plot_df = df.iloc[start_idx:].copy()

        # 2. Zeitstempel robust konvertieren (ohne Zeitzone!)
        plot_df['open_time'] = pd.to_datetime(plot_df['open_time']).dt.tz_localize(None)
        plot_df.set_index('open_time', inplace=True)

        # 3. Padding (leerer Platz rechts in der Zukunft für den Retest)
        time_step = plot_df.index[-1] - plot_df.index[-2]
        future_dates = [plot_df.index[-1] + time_step * i for i in range(1, 15)]
        empty_df = pd.DataFrame(np.nan, index=future_dates, columns=plot_df.columns).astype(float)
        plot_df = pd.concat([plot_df, empty_df])

        # 4. Zeitstempel für die Zick-Zack-Linie exakt parsen
        def get_dt(idx):
            return pd.to_datetime(df['open_time'].iloc[idx]).tz_localize(None)

        seq_lines = [
            (get_dt(p1[0]), float(p1[2])),
            (get_dt(p2[0]), float(p2[2])),
            (get_dt(p3[0]), float(p3[2])),
            (get_dt(p4[0]), float(p4[2])),
        ]

        # 5. Styling and colours
        color_theme = '#ff4466' if "BEARISH" in pattern_type else '#00ff88'
        mc = mpf.make_marketcolors(up='#26a69a', down='#ef5350', edge='inherit', wick='inherit')
        s = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds', gridstyle=':')

        rel_filename = f"{CHART_DIR}/{symbol}_QM_{int(time.time())}.png"
        abs_filename = os.path.abspath(rel_filename)

        mpf.plot(
            plot_df,
            type='candle',
            style=s,
            alines=dict(alines=seq_lines, colors=color_theme, linewidths=2, linestyle='-'),
            hlines=dict(hlines=[float(qm_level)], colors=[color_theme], linewidths=2, linestyle='--'),
            title=f"\n{symbol} | {pattern_type} Quasimodo (QML: {qm_level:.4f})",
            figsize=(12, 7),
            tight_layout=True,
            savefig=abs_filename,
            returnfig=False,
        )

        logger.info(f"Chart erfolgreich generiert: {abs_filename}")
        return abs_filename

    except Exception as e:
        logger.error(f"Chart Error for {symbol}: {e}", exc_info=True)
        return None
    finally:
        # Schließt die von mpf.plot offen gelassene Figure — verhindert RAM-Leak.
        plt.close('all')


# 🕵️ PATTERN SCANNER
def scan_institutional_patterns():
    conn = get_db_connection()
    coins = load_coins()

    logger.info(f"🔍 Scanne {len(coins)} Coins auf Institutional Patterns...")

    try:
        for symbol in coins:
            df = pd.read_sql_query(
                f'SELECT open_time, open, high, low, close FROM "{symbol}_{TIMEFRAME}" ORDER BY open_time DESC LIMIT {LOOKBACK_CANDLES}',
                conn,
            )

            if len(df) < 100:
                continue

            # Umdrehen (älteste zuerst)
            df = df.iloc[::-1].reset_index(drop=True)
            pivots = get_alternating_pivots(df, window=5)

            if len(pivots) < 4:
                continue

            current_price = df['close'].iloc[-1]

            # Wir analysieren immer Pakete von 4 aufeinanderfolgenden Pivots
            for i in range(len(pivots) - 3):
                p1, p2, p3, p4 = pivots[i], pivots[i + 1], pivots[i + 2], pivots[i + 3]

                # --- 🔴 BEARISH QUASIMODO (SHORT SETUP) ---
                # Struktur muss sein: High, Low, Higher High, Lower Low
                if p1[1] == 1 and p2[1] == -1 and p3[1] == 1 and p4[1] == -1:
                    H, L, HH, LL = p1[2], p2[2], p3[2], p4[2]

                    if HH > H and LL < L:  # QM Bestätigung
                        qm_level = H
                        # FIX: Vorher {p1[0]} = Kerzen-Index → verschiebt sich mit
                        # jedem neuen Candle → gleiches Pattern bekommt neue ID und
                        # wird erneut gemeldet. Jetzt Unix-Timestamp des Pivot-Candles.
                        pivot_ts = int(pd.to_datetime(df['open_time'].iloc[p1[0]]).timestamp())
                        pattern_id = f"{symbol}_BEAR_QM_{pivot_ts}"

                        # Prüfen, ob der aktuelle Preis gerade von unten ans QML herankommt
                        # Wir triggern den Alert, wenn er innerhalb der ZONE_TOLERANCE liegt
                        dist_to_qml = (qm_level - current_price) / qm_level

                        if 0 <= dist_to_qml <= ZONE_TOLERANCE and pattern_id not in ALERTED_QMS:
                            ALERTED_QMS.add(pattern_id)
                            logger.info(f"🚨 BEARISH QM bei {symbol} gefunden! QML: {qm_level}")

                            chart_path = generate_qm_chart(df, symbol, "BEARISH", p1, p2, p3, p4, qm_level)
                            msg = f"""<b>🏛 INSTITUTIONAL PA DETECTED</b>
<b>{symbol.replace('USDT', '')} | {TIMEFRAME}</b>

📉 <b>Pattern:</b> BEARISH QUASIMODO (QM)
🎯 <b>Entry Zone (QML):</b> <code>${qm_level:.4f}</code>
💵 <b>Current Price:</b> ${current_price:.4f}

<i>Explanation: Price grabbed liquidity above the old high (HH), then aggressively broke market structure (LL). We are now retesting the origin of the move (QML). Look for Short setups!</i>"""
                            send_telegram_alert(conn, msg, chart_path)

                # --- 🟢 BULLISH QUASIMODO (LONG SETUP) ---
                # Struktur muss sein: Low, High, Lower Low, Higher High
                elif p1[1] == -1 and p2[1] == 1 and p3[1] == -1 and p4[1] == 1:
                    L, H, LL, HH = p1[2], p2[2], p3[2], p4[2]

                    if LL < L and HH > H:  # QM Bestätigung
                        qm_level = L
                        # FIX: Gleicher Fix wie BEARISH oben — Timestamp statt Index.
                        pivot_ts = int(pd.to_datetime(df['open_time'].iloc[p1[0]]).timestamp())
                        pattern_id = f"{symbol}_BULL_QM_{pivot_ts}"

                        # Prüfen, ob der aktuelle Preis gerade von oben ins QML fällt
                        dist_to_qml = (current_price - qm_level) / qm_level

                        if 0 <= dist_to_qml <= ZONE_TOLERANCE and pattern_id not in ALERTED_QMS:
                            ALERTED_QMS.add(pattern_id)
                            logger.info(f"🚀 BULLISH QM bei {symbol} gefunden! QML: {qm_level}")

                            chart_path = generate_qm_chart(df, symbol, "BULLISH", p1, p2, p3, p4, qm_level)
                            msg = f"""<b>🏛 INSTITUTIONAL PA DETECTED</b>
<b>{symbol.replace('USDT', '')} | {TIMEFRAME}</b>

📈 <b>Pattern:</b> BULLISH QUASIMODO (QM)
🎯 <b>Entry Zone (QML):</b> <code>${qm_level:.4f}</code>
💵 <b>Current Price:</b> ${current_price:.4f}

<i>Explanation: Price grabbed liquidity below the old low (LL), then strongly broke market structure to the upside (HH). We are now retesting the demand zone (QML). Look for Long setups!</i>"""
                            send_telegram_alert(conn, msg, chart_path)

    except Exception as e:
        logger.error(f"Critical error im Scanner: {e}", exc_info=True)
    finally:
        # FIX: Nach jedem Scan die Pattern-IDs persistieren, damit sie bei
        # Restart nicht erneut gemeldet werden.
        save_alerted_qms()
        conn.close()


def main():
    logger.info("=== 🏛 INSTITUTIONAL PATTERN BOT GESTARTET ===")
    # FIX: Bekannte Pattern-IDs beim Start laden.
    load_alerted_qms()
    while True:
        now = datetime.now(timezone.utc)
        # Scannt jede Stunde pünktlich zur Minute :05
        if now.minute == 5:
            scan_institutional_patterns()
            logger.info("Scan stopped. Schlafe 55 Minuten...")
            time.sleep(3300)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C).")

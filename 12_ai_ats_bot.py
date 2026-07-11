import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import os
import time

import joblib
import numpy as np
import pandas as pd

from core import config as _kcfg  # channel ids
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_ATS_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & CHANNELS ---
AI_CHANNEL_ID = _kcfg.CH_ATS

# --- LOAD ML MODELS ---
TSI_MODEL_LONG_PATH = "model_tsi_long_robust.pkl"
TSI_MODEL_SHORT_PATH = "model_tsi_short_robust.pkl"
# Operating-Band (Audit Report 13/16): Die Confidence-Kalibrierung ist durch den
# OBV-Train/Serve-Skew INVERTIERT — Bucket 0.6-0.7 hat live 71% WR, 0.8-0.9 nur 57%.
# Deshalb: Posten nur im empirisch besten Band [0.60, 0.80); >=0.80 geht in Shadow.
TSI_THRESH_LONG = 0.60
TSI_THRESH_SHORT = 0.60
TSI_PROB_CAP = 0.80

TSI_FEATURES = [
    "rsi_14",
    "rsi_6",
    "macd_hist",
    "atr_pct",
    "vol_ratio",
    "bb_width",
    "bb_pos",
    "dist_ema200",
    "dist_ema9_21",
    "rsi_ratio",
    "slope_norm",
    "dist_supp",
    "dist_res",
    "dist_kama9",
    "dist_kama21",
    "dist_kama55",
    "dist_kama9_21",
    "dist_donch_up",
    "dist_donch_low",
    "macd_cross_bearish",
    "ema9_21_cross_bearish",
    "kama9_21_cross_bearish",
    "bollinger_lower_break",
    "close_below_ema50",
    "obv_ratio",
    "close_to_vwap_pct",
    "obv_val",
    "volume_spike",
    "volume_trend_up",
]

MODEL_LONG = None
MODEL_SHORT = None


def load_models():
    """Loads the TSI models once at startup (or hourly)."""
    global MODEL_LONG, MODEL_SHORT
    try:
        if os.path.exists(TSI_MODEL_LONG_PATH):
            MODEL_LONG = joblib.load(TSI_MODEL_LONG_PATH)
        else:
            logger.warning(f"Modell fehlt: {TSI_MODEL_LONG_PATH}")

        if os.path.exists(TSI_MODEL_SHORT_PATH):
            MODEL_SHORT = joblib.load(TSI_MODEL_SHORT_PATH)
        else:
            logger.warning(f"Modell fehlt: {TSI_MODEL_SHORT_PATH}")

        if MODEL_LONG and MODEL_SHORT:
            logger.info("✅ TSI Sniper Modelle (ATS1) loaded successfully.")
    except Exception as e:
        logger.error(f"❌ Error loading der TSI Modelle: {e}")


# --- HAUPT CHECKER FUNKTION ---
def check_tsi_crossovers():
    if not MODEL_LONG or not MODEL_SHORT:
        logger.error("Modelle not loaded. Skipping Scan.")
        return

    conn = get_db_connection()
    try:
        with open('coins.json') as f:
            coins = json.load(f)
    except Exception as e:
        logger.error(f"Could not load coins.json: {e}")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    logger.info(f"🔍 Starting TSI Sniper (ATS1) Scan für {len(coins)} Coins...")

    for symbol in coins:
        try:
            # 1. FIX: Vorher nur 50 Kerzen → OBV startete immer bei 0 und akkumulierte
            # über 50 Candles — im Training wurde OBV über die gesamte Historie
            # cumuliert, daher systematischer Feature-Drift (train/test mismatch).
            # Jetzt laden wir 500 Kerzen UND normalisieren OBV auf `obv - obv.iloc[0]`,
            # sodass der absolute Wert unabhängig vom Datenfenster-Start ist.
            query = f"""
                SELECT
                    p.open_time, p.high, p.low, p.close, p.volume,
                    i.rsi_14, i.rsi_6, i.tsi_fast_12_7_7, i.tsi_fast_12_7_7_signal,
                    i.ema_9, i.ema_21, i.ema_50, i.ema_200,
                    i.kama_9, i.kama_21, i.kama_55,
                    i.macd_dif_normal_12_26_9, i.macd_dea_normal_12_26_9,
                    i.atr_14, i.boll_upper_20, i.boll_lower_20,
                    i.donchian_upper_20, i.donchian_lower_20,
                    i.trendline_slope, i.support_price, i.resistance_price
                FROM "{symbol}_1h" p
                LEFT JOIN "{symbol}_1h_indicators" i ON p.open_time = i.open_time
                ORDER BY p.open_time DESC LIMIT 500
            """
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
                if len(rows) < 50:
                    continue
                columns = [desc[0] for desc in cur.description]
                df = pd.DataFrame(rows, columns=columns)

            # Umdrehen (Index 0 = Alt, Index -1 = Neu)
            df = df.iloc[::-1].reset_index(drop=True)

            # Alle Spalten zu Float konvertieren
            num_cols = [c for c in df.columns if c != 'open_time']
            for col in num_cols:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

            # 2. CROSSOVER PRÜFEN (Vorletzte Kerze vs Drittletzte Kerze)
            # Da Bot 8 Min after Stunde läuft, ist Index -1 die offene Kerze.
            # Index -2 ist die completede. Index -3 ist die davor.
            current_idx = -2
            prev_idx = -3

            tsi_curr = df.iloc[current_idx]['tsi_fast_12_7_7']
            sig_curr = df.iloc[current_idx]['tsi_fast_12_7_7_signal']
            tsi_prev = df.iloc[prev_idx]['tsi_fast_12_7_7']
            sig_prev = df.iloc[prev_idx]['tsi_fast_12_7_7_signal']

            long_cross = (tsi_prev <= sig_prev) and (tsi_curr > sig_curr)
            short_cross = (tsi_prev >= sig_prev) and (tsi_curr < sig_curr)

            if not (long_cross or short_cross):
                continue

            direction = "LONG" if long_cross else "SHORT"

            # 3. LIVE FEATURE ENGINEERING (OBV, VWAP)
            # FIX: OBV auf Startpunkt normalisieren → absoluter Wert ist egal,
            # entscheidend sind die relativen Veränderungen über das Fenster.
            obv_raw = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
            df['obv'] = obv_raw - obv_raw.iloc[0]
            df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
            df['vwap_20'] = (df['volume'] * df['typical_price']).rolling(20).sum() / df['volume'].rolling(20).sum()
            df['vwap_20'] = df['vwap_20'].fillna(df['close'])

            row = df.iloc[current_idx]
            row_prev = df.iloc[prev_idx]
            current_price = float(row['close'])

            vol_sma20 = df['volume'].rolling(20).mean().iloc[current_idx]
            if vol_sma20 == 0:
                vol_sma20 = 1.0

            features = {
                "rsi_14": row['rsi_14'],
                "rsi_6": row['rsi_6'],
                "macd_hist": row['macd_dif_normal_12_26_9'] - row['macd_dea_normal_12_26_9'],
                "atr_pct": (row['atr_14'] / row['close']) * 100 if row['close'] else 0,
                "vol_ratio": row['volume'] / vol_sma20,
                "bb_width": (row['boll_upper_20'] - row['boll_lower_20']) / row['boll_lower_20']
                if row['boll_lower_20']
                else 0,
                "bb_pos": (row['close'] - row['boll_lower_20']) / (row['boll_upper_20'] - row['boll_lower_20'])
                if (row['boll_upper_20'] - row['boll_lower_20']) != 0
                else 0,
                "dist_ema200": (row['close'] / row['ema_200']) - 1 if row['ema_200'] else 0,
                "dist_ema9_21": (row['ema_9'] / row['ema_21']) - 1 if row['ema_21'] else 0,
                "dist_kama9": (row['close'] / row['kama_9']) - 1 if row['kama_9'] else 0,
                "dist_kama21": (row['close'] / row['kama_21']) - 1 if row['kama_21'] else 0,
                "dist_kama55": (row['close'] / row['kama_55']) - 1 if row['kama_55'] else 0,
                "dist_kama9_21": (row['kama_9'] / row['kama_21']) - 1 if row['kama_21'] else 0,
                "dist_donch_up": (row['close'] / row['donchian_upper_20']) - 1 if row['donchian_upper_20'] else 0,
                "dist_donch_low": (row['close'] / row['donchian_lower_20']) - 1 if row['donchian_lower_20'] else 0,
                "rsi_ratio": row['rsi_6'] / row['rsi_14'] if row['rsi_14'] else 0,
                "slope_norm": (row['trendline_slope'] / row['close']) * 1000 if row['close'] else 0,
                "dist_supp": (row['close'] - row['support_price']) / row['close'] if row['close'] else 0,
                "dist_res": (row['resistance_price'] - row['close']) / row['close'] if row['close'] else 0,
                "macd_cross_bearish": int(
                    row_prev['macd_dif_normal_12_26_9'] >= row_prev['macd_dea_normal_12_26_9']
                    and row['macd_dif_normal_12_26_9'] < row['macd_dea_normal_12_26_9']
                ),
                "ema9_21_cross_bearish": int(row_prev['ema_9'] >= row_prev['ema_21'] and row['ema_9'] < row['ema_21']),
                "kama9_21_cross_bearish": int(
                    row_prev['kama_9'] >= row_prev['kama_21'] and row['kama_9'] < row['kama_21']
                ),
                "bollinger_lower_break": int(row['close'] < row['boll_lower_20']),
                "close_below_ema50": int(row['close'] < row['ema_50']),
                "obv_ratio": row['obv'] / df['obv'].rolling(20).mean().iloc[current_idx]
                if df['obv'].rolling(20).mean().iloc[current_idx] != 0
                else 0,
                "close_to_vwap_pct": (row['close'] / row['vwap_20']) - 1 if row['vwap_20'] else 0,
                "obv_val": row['obv'],
                "volume_spike": int(row['volume'] > vol_sma20 * 2),
                "volume_trend_up": int(df['volume'].rolling(5).mean().iloc[current_idx] > vol_sma20),
            }

            # Prediction DataFrame erstellen (Spaltenreihenfolge erzwingen)
            X_live = pd.DataFrame([features])
            X_live = X_live[TSI_FEATURES].fillna(0)

            if long_cross:
                prob_profit = float(MODEL_LONG.predict_proba(X_live)[0, 1])
                threshold = TSI_THRESH_LONG
            else:
                prob_profit = float(MODEL_SHORT.predict_proba(X_live)[0, 1])
                threshold = TSI_THRESH_SHORT

            module_tag = "ATS1"

            # --- SHADOW MODE LOGGING ---
            if prob_profit < 0.25:
                continue

            elif 0.25 <= prob_profit < threshold or prob_profit >= TSI_PROB_CAP:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                        VALUES (0, %s, %s, %s, %s, %s, %s, False)
                    """,
                        (module_tag, now, symbol, direction, float(current_price), prob_profit),
                    )
                conn.commit()

            elif prob_profit >= threshold:
                # 🔥 TRADE AUSFÜHREN
                # Cooldown: 4h Sperre pro Coin/Direction. check_cooldown returned True
                # wenn Cooldown NOCH AKTIV ist → skippen.
                if check_cooldown(conn, module_tag, symbol, direction, 4):
                    logger.info(f"⏳ Cooldown active für {symbol} {direction} → skipped.")
                    continue

                logger.info(f"🔥 TRADE EXECUTE: {symbol} {direction} (ML {prob_profit:.1%})")

                is_long = direction == "LONG"
                entry1 = current_price
                entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
                supps, resis = get_hvn_and_sr_levels(conn, symbol, current_price)

                if is_long:
                    sl = (
                        max([x for x in supps if x < entry2 * 0.99])
                        if any(x < entry2 * 0.99 for x in supps)
                        else entry2 * 0.975
                    )
                    t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
                else:
                    sl = (
                        min([x for x in resis if x > entry2 * 1.01])
                        if any(x > entry2 * 1.01 for x in resis)
                        else entry2 * 1.025
                    )
                    t_cands = sorted([x for x in supps if x > 0 and x < (entry1 * 0.99)], reverse=True)

                # FIX: echte Zonen + ggf. 5%-Target wenn letzte Zone zu nah
                targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)
                # P2.31: publish AND track exactly the same targets. The Cornix block
                # shows the first n_show TPs; the AI monitor (8_ai_trade_monitor) scores
                # whatever is stored in ai_signals.targets. Storing the full 20-zone list
                # made the monitor score phantom TPs the subscriber never saw.
                n_show = 3

                lev = get_max_leverage(symbol, 20)
                # Cornix Text
                lines = [
                    f"📈 Signal for {symbol} 📈",
                    f"🚨 Direction: {direction}",
                    f"🚨 Leverage: {lev}",
                    "🚨 Margin: Cross",
                    f"🏦 CMP Entry: $ {entry1:.8f}",
                    f"🏦 Entry 2: $ {entry2:.8f}",
                ]
                for i, t in enumerate(targets[:n_show], 1):
                    lines.append(f"💰 TP{i}: $ {t:.8f}")
                lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module_tag} V3"]
                cornix_msg = "\n".join(lines)

                # HTML für Chart
                emoji = "🚀 TSI-SNIPER LONG" if is_long else "💥 TSI-SNIPER SHORT"
                vol_trend_str = "JA" if features['volume_trend_up'] else "NEIN"

                # FIX Doppel-Post (2026-07-06, Flotten-Sweep): Caption ohne
                # eingebetteten Cornix-Block — Cornix parste beide Nachrichten.
                html_caption = f"""<pre><b>{emoji}</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n<b>→ Direction: {direction}</b>\n<b>→ Confidence: <b>{prob_profit:.1%}</b> (Thresh {threshold})</b>\n<b>→ Price: {current_price:.4f}</b>\n<b>→ Vol Trend Up: {vol_trend_str} | Spike: {features['volume_spike']}</b>\n<b>→ Time: {now.strftime('%H:%M')} UTC | Modul: ATS1 V3</b></pre>"""

                chart_buf = generate_minichart_image(symbol, minutes=240)
                with conn.cursor() as cur:
                    # Cornix Channel
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (AI_CHANNEL_ID, cornix_msg)
                    )
                    # Chart Channel
                    if chart_buf:
                        cur.execute(
                            "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                            (AI_CHANNEL_ID, html_caption, chart_buf),
                        )
                    else:
                        cur.execute(
                            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                            (AI_CHANNEL_ID, html_caption),
                        )

                    # AI Signal Monitor

                    cur.execute(
                        """
                                    INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                        (
                            symbol,
                            entry1,
                            module_tag,
                            direction,
                            float(prob_profit),
                            float(entry1),
                            float(entry2),
                            float(sl),
                            json.dumps(targets[:n_show]),
                        ),
                    )
                    # Master Log
                    cur.execute(
                        """INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted) VALUES (0, %s, %s, %s, %s, %s, %s, True)""",
                        (module_tag, now, symbol, direction, float(current_price), float(prob_profit)),
                    )

                conn.commit()
                # Cooldown setzen, damit gleicher Coin/Direction nicht sofort wieder feuert
                update_cooldown(conn, module_tag, symbol, direction)

        except Exception as e:
            logger.error(f"Error for {symbol} in ATS1: {e}")
            if conn:
                conn.rollback()

    if conn:
        conn.close()
    logger.info("🏁 ATS1 Model Check stopped.")


def main():
    logger.info("=== 🎯 AI TSI SNIPER (ATS1) GESTARTET ===")

    # 1. Modelle laden
    load_models()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        # Der Bot soll exakt 8 Minuten after der vollen Stunde laufen
        if now.minute == 13:
            check_tsi_crossovers()
            # Schlafen, damit er nicht mehrfach in Minute 8 triggert
            time.sleep(60)
        else:
            # Checkt alle 10 Sekunden, ob Minute 8 erreicht ist
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

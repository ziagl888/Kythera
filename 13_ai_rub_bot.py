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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_RUB_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & CHANNELS ---
# Hier kannst du den speziellen Rubberband-Kanal setzen
RUBBERBAND_CHANNEL_ID = _kcfg.CH_RUBBERBAND

# --- LOAD ML MODELS ---
MODEL_LONG_PATH = 'long_reversion_model.joblib'
MODEL_SHORT_PATH = 'short_reversion_model.joblib'
REVERSION_THRESH_LONG = 0.75
REVERSION_THRESH_SHORT = 0.85

MODEL_LONG = None
MODEL_SHORT = None


def load_models():
    """Loads the Mean Reversion models."""
    global MODEL_LONG, MODEL_SHORT
    try:
        if os.path.exists(MODEL_LONG_PATH):
            MODEL_LONG = joblib.load(MODEL_LONG_PATH)
        else:
            logger.warning(f"Modell fehlt: {MODEL_LONG_PATH}")

        if os.path.exists(MODEL_SHORT_PATH):
            MODEL_SHORT = joblib.load(MODEL_SHORT_PATH)
        else:
            logger.warning(f"Modell fehlt: {MODEL_SHORT_PATH}")

        if MODEL_LONG and MODEL_SHORT:
            logger.info("✅ Rubberband Modelle (RUB1) loaded successfully.")
    except Exception as e:
        logger.error(f"❌ Error loading der Rubberband Modelle: {e}")


# --- HAUPT CHECKER FUNKTION ---
def check_rubberband_conditions():
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
    logger.info(f"🔍 Starting Rubberband (RUB1) Scan für {len(coins)} Coins...")

    for symbol in coins:
        try:
            # USDT Filter (Verhindert Error for USDC Paaren)
            if 'USDT_' in symbol:
                continue

            # 1. 90 Tage Daten für die Trendberechnung holen
            # P1.19: Forming-Candle ausschließen (open_time < date_trunc('hour', NOW())),
            # sonst rechnet die Regression mit der offenen Kerze aus ~2 min Daten.
            query_90d = f"""
                SELECT open_time, close
                FROM "{symbol}_1h"
                WHERE open_time >= NOW() - INTERVAL '95 days'
                  AND open_time < date_trunc('hour', NOW())
                ORDER BY open_time ASC
            """

            # 2. Letzte Indikatoren holen
            # P1.19: closed-candle-Filter — LIMIT 1 lieferte sonst die offene Kerze
            # (Partial-Indikatoren). close mitziehen, damit curr_close aus DERSELBEN
            # geschlossenen Kerze stammt wie die Indikatoren (nicht aus dem 90d-Array).
            query_ind = f"""
                SELECT
                    close,
                    rsi_14, tsi_fast_12_7_7, tsi_fast_12_7_7_signal,
                    macd_dif_normal_12_26_9, macd_dea_normal_12_26_9,
                    atr_14, ema_200, donchian_lower_20, donchian_upper_20
                FROM "{symbol}_1h_indicators"
                WHERE open_time < date_trunc('hour', NOW())
                ORDER BY open_time DESC LIMIT 1
            """

            with conn.cursor() as cur:
                # 1. Trend Daten
                cur.execute(query_90d)
                rows_90d = cur.fetchall()
                if len(rows_90d) < 50:
                    continue
                df_90d = pd.DataFrame(rows_90d, columns=['open_time', 'close'])

                # 2. Indikator Daten
                cur.execute(query_ind)
                row_ind = cur.fetchone()
                if not row_ind:
                    continue
                columns_ind = [desc[0] for desc in cur.description]
                ind = dict(zip(columns_ind, row_ind, strict=False))

            # --- TRENDBERECHNUNG ---
            # Timestamp-Array für lineare Regression
            df_90d['ts'] = pd.to_datetime(df_90d['open_time'], utc=True).apply(lambda x: x.timestamp())
            ts_values = df_90d['ts'].values
            close_values = df_90d['close'].values.astype(float)

            # Lineare Regression (Numpy Least Squares)
            A = np.vstack([ts_values, np.ones(len(ts_values))]).T
            slope, intercept = np.linalg.lstsq(A, close_values, rcond=None)[0]

            curr_ts = ts_values[-1]
            # P1.19: curr_close aus der geschlossenen Indikator-Kerze (ind['close']),
            # nicht aus dem 90d-Preis-Array — so mischen dist_to_trend + alle ML-Features
            # nicht mehr Live-Preis mit Partial-Indikatoren. Fallback auf die (nun
            # ebenfalls geschlossene) letzte 90d-Kerze, falls close NaN/fehlt.
            try:
                curr_close = float(ind['close'])
                if not np.isfinite(curr_close):
                    curr_close = float(close_values[-1])
            except (TypeError, ValueError, KeyError):
                curr_close = float(close_values[-1])
            trend_val_curr = slope * curr_ts + intercept

            # Wie weit sind wir prozentual vom Trend entfernt?
            dist_to_trend_pct = (curr_close - trend_val_curr) / trend_val_curr if trend_val_curr != 0 else 0
            slope_pct_per_day = (slope * 86400) / curr_close if curr_close != 0 else 0

            # --- INDIKATOREN AUSLESEN ---
            def get_f(key, default=0.0, ind=ind):
                val = ind.get(key)
                # FIX: Vorher wurde nur auf `None` geprüft. pandas/postgres können aber
                # NaN/Inf liefern (insbesondere bei frischen Coins mit wenig Historie).
                # Wenn diese in die ML-Features fließen, crasht predict_proba oder
                # liefert unbrauchbare Werte. Jetzt: auch NaN/Inf → default.
                try:
                    if val is None:
                        return default
                    fv = float(val)
                    if not np.isfinite(fv):
                        return default
                    return fv
                except (TypeError, ValueError):
                    return default

            rsi = get_f('rsi_14', 50)
            tsi_line = get_f('tsi_fast_12_7_7')
            tsi_signal = get_f('tsi_fast_12_7_7_signal')
            macd_line = get_f('macd_dif_normal_12_26_9')
            macd_signal = get_f('macd_dea_normal_12_26_9')
            atr_14 = get_f('atr_14')
            ema_200 = get_f('ema_200', curr_close)
            dc_lower = get_f('donchian_lower_20', curr_close)
            dc_upper = get_f('donchian_upper_20', curr_close)

            atr_pct = (atr_14 / curr_close) if curr_close > 0 else 0
            dist_ema200 = (curr_close - ema_200) / ema_200 if ema_200 > 0 else 0

            # --- VORFILTERUNG (RUBBERBAND BEDINGUNGEN) ---
            event_type = None

            # Bedingung LONG: Preis extrem weit UNTER dem Trend (-8%), RSI Oversold, TSI tief, am Donchian Boden
            if dist_to_trend_pct <= -0.08 and rsi < 30 and tsi_line < -15 and curr_close <= dc_lower * 1.01:
                event_type = "REVERSION_UP"

            # Bedingung SHORT: Preis extrem weit ÜBER dem Trend (+8%), RSI Overbought, TSI hoch, am Donchian Dach
            elif dist_to_trend_pct >= 0.08 and rsi > 70 and tsi_line > 15 and curr_close >= dc_upper * 0.99:
                event_type = "REVERSION_DOWN"

            if not event_type:
                continue

            # --- ML FEATURES BERECHNEN ---
            features = pd.DataFrame(
                [
                    {
                        'dist_to_trend': dist_to_trend_pct,
                        'rsi': rsi,
                        'atr_pct': atr_pct,
                        'dist_ema200': dist_ema200,
                        'slope_trend': slope_pct_per_day,
                        'MACD_Line': macd_line,
                        'MACD_Signal': macd_signal,
                        'TSI_Line': tsi_line,
                        'TSI_Signal': tsi_signal,
                    }
                ]
            )

            is_long = event_type == "REVERSION_UP"
            model = MODEL_LONG if is_long else MODEL_SHORT
            threshold = REVERSION_THRESH_LONG if is_long else REVERSION_THRESH_SHORT

            if model is None:
                continue

            direction = "LONG" if is_long else "SHORT"
            module_tag = "RUB1"

            # FIX: Cooldown-Check VOR der teuren ML-Prediction.
            # Vorher lief predict_proba auch dann, wenn der Coin noch im Cooldown war
            # (bei 500 Coins × mehreren Event-Typen = viel verschwendete CPU).
            # Der Shadow-Log unterhalb bleibt erhalten — er dokumentiert alle
            # potenziellen Trades, auch die abgelehnten. Beim Skip durch Cooldown
            # loggen wir weiterhin fürs Monitoring.
            if check_cooldown(conn, module_tag, symbol, direction, 4):
                logger.debug(f"RUB1 Prediction für {symbol} {direction} im Cooldown — skip.")
                continue

            # Prediction (teuer, erst after Cooldown-Check)
            prob = model.predict_proba(features)[0, 1]

            logger.info(f"RUB1 Trigger: {symbol} {direction} | ML-Conf: {prob:.1%} (Thresh: {threshold:.2f})")

            # --- SHADOW MODE LOGGING ---
            # Direction-Gate (Audit Report 14 D.5): RUB1 LONG 48,7% WR vs SHORT 63,9% —
            # die LONG-Seite ist Messer-Fangen und geht nur noch in den Shadow-Log.
            if prob < threshold or is_long:
                # Ablegen in Master Tabelle (als abgelehnter Trade)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                        VALUES (0, %s, %s, %s, %s, %s, %s, False)
                    """,
                        (module_tag, now, symbol, direction, float(curr_close), float(prob)),
                    )
                conn.commit()
                continue

            # 🔥 TRADE AUSFÜHREN
            logger.info(f"🔥 RUB1 TRADE EXECUTE: {symbol} {direction} (ML {prob:.1%})")

            entry1 = curr_close
            entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
            supps, resis = get_hvn_and_sr_levels(conn, symbol, curr_close)

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

            # FIX: Vorher `while len(targets) < 20: append last*1.02` → extrapolierte
            # bis +48% über Entry, bei Mean-Reversion-Bots absurd. Jetzt nur noch:
            # echte Zonen nehmen, und ggf. EIN 5%-Target anhängen wenn das letzte
            # zu nah am Entry liegt.
            targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)

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
            for i, t in enumerate(targets[:3], 1):
                lines.append(f"💰 TP{i}: $ {t:.8f}")
            lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module_tag}"]
            cornix_msg = "\n".join(lines)

            # HTML für Chart
            emoji = "🚀 RUBBERBAND MEAN REVERSION LONG" if is_long else "💥 RUBBERBAND MEAN REVERSION SHORT"
            dist_str = f"{dist_to_trend_pct * 100:+.2f}%"

            html_caption = f"""<pre><b>{emoji}</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n<b>→ Direction: {direction}</b>\n<b>→ Confidence: <b>{prob:.1%}</b> (Thresh {threshold})</b>\n<b>→ Price: {curr_close:.4f}</b>\n<b>→ Trend Distance: <b>{dist_str}</b></b>\n<b>→ Time: {now.strftime('%H:%M')} UTC | Modul: {module_tag}</b>\n\n{cornix_msg}</pre>"""

            chart_buf = generate_minichart_image(symbol, minutes=240)
            with conn.cursor() as cur:
                # Cornix Channel (Hier nutzt er den speziellen Rubberband Channel!)
                cur.execute(
                    "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                    (RUBBERBAND_CHANNEL_ID, cornix_msg),
                )
                # Chart Channel
                if chart_buf:
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                        (RUBBERBAND_CHANNEL_ID, html_caption, chart_buf),
                    )
                else:
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                        (RUBBERBAND_CHANNEL_ID, html_caption),
                    )

                # AI Signal Monitor

                cur.execute(
                    """
                                INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                    (
                        symbol,
                        float(entry1),
                        module_tag,
                        direction,
                        float(prob),
                        float(entry1),
                        float(entry2),
                        float(sl),
                        json.dumps(targets),
                    ),
                )
                # Master Log
                cur.execute(
                    """INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted) VALUES (0, %s, %s, %s, %s, %s, %s, True)""",
                    (module_tag, now, symbol, direction, float(curr_close), float(prob)),
                )

            conn.commit()
            update_cooldown(conn, module_tag, symbol, direction)

        except Exception as e:
            logger.error(f"Error for {symbol} in RUB1: {e}")
            if conn:
                conn.rollback()

    if conn:
        conn.close()
    logger.info("🏁 RUB1 Model Check stopped.")


def main():
    logger.info("=== 🎯 AI RUBBERBAND BOT (RUB1) GESTARTET ===")

    # Modelle laden
    load_models()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        # Der Bot soll exakt 12 Minuten after der vollen Stunde laufen
        if now.minute == 10:
            check_rubberband_conditions()
            # Schlafen, damit er nicht mehrfach in Minute 12 triggert
            time.sleep(60)
        else:
            # Checkt alle 10 Sekunden, ob Minute 12 erreicht ist
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

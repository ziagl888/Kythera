import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import time

import numpy as np
import pandas as pd
import xgboost as xgb

from core import config as _kcfg  # channel ids
from core.charting import generate_minichart_image

# --- CORE IMPORTE ---
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels

# Logging Setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_SR_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- LOAD MODELS ---
# try:
#    MODEL_LONG = joblib.load("trade_success_xgb_LONG_v1.model")
#    MODEL_SHORT = joblib.load("trade_success_xgb_SHORT_v1.model")
#    logger.info("✅ XGBoost Modelle (SRA1) loaded successfully!")
# except Exception as e:
#    logger.error(f"❌ Error loading der Modelle: {e}")
#    exit(1)

# --- MODELLE LADEN (AKTUALISIERT FÜR NATIVES JSON FORMAT) ---
try:
    # Wir erstellen leere Container und laden das Modell hinein
    MODEL_LONG = xgb.XGBClassifier()
    MODEL_LONG.load_model("trade_success_xgb_LONG_v2.json")

    MODEL_SHORT = xgb.XGBClassifier()
    MODEL_SHORT.load_model("trade_success_xgb_SHORT_v2.json")

    logger.info("✅ XGBoost models (SRA1) loaded in native JSON format!")
except Exception as e:
    logger.error(f"❌ Error loading der neuen Modelle: {e}")
    # Fallback auf alte Methode, falls Dateien noch nicht da sind
    exit(1)


# FEATURE & INDIKATOR HELFER


def get_indicators_at_time(conn, coin, timestamp):
    """Holt die 1h Indikatoren zum Zeitpunkt des Trades aus der DB."""
    table_name = f'"{coin}_1h_indicators"'
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT * FROM {table_name}
                WHERE open_time <= %s
                ORDER BY open_time DESC LIMIT 1
            """,
                (timestamp,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row, strict=False))
    except Exception as e:
        logger.debug(f"Indikatoren-DB-Fehler für {coin}: {e}")
        return None


def create_feature_row(direction, indicators):
    """Erstellt das Feature-Dict für XGBoost basierend auf deiner Modell-Logik."""
    close = indicators.get('close', np.nan)
    if pd.isna(close) or close <= 0:
        return None

    features = {}
    base_cols = [
        'rsi_9',
        'rsi_14',
        'rsi_24',
        'macd_dif_fast_9_21_9',
        'macd_dea_fast_9_21_9',
        'tsi_fast_12_7_7',
        'tsi_fast_12_7_7_signal',
        'atr_14',
        'r_squared',
        'boll_upper_20',
        'boll_mid_20',
        'boll_lower_20',
        'donchian_upper_20',
        'donchian_lower_20',
        'donchian_mid_20',
        'support_price',
        'resistance_price',
        'ema_9',
        'ema_21',
        'wma_9',
        'wma_21',
        'kama_9',
        'kama_21',
        'close',
    ]

    for col in base_cols:
        val = indicators.get(col)
        features[col] = float(val) if pd.notna(val) else np.nan

    trend_map = {'UP': 1.0, 'DOWN': -1.0, 'FLAT': 0.0, 'SIDEWAYS': 0.0}
    features['trend_direction_num'] = trend_map.get(str(indicators.get('trend_direction', '')).upper(), 0.0)

    def pct(a, b):
        return (a - b) / close * 100 if pd.notna(b) and close > 0 else np.nan

    features.update(
        {
            'pct_ema9': pct(close, indicators.get('ema_9')),
            'pct_ema21': pct(close, indicators.get('ema_21')),
            'pct_wma9': pct(close, indicators.get('wma_9')),
            'pct_kama9': pct(close, indicators.get('kama_9')),
            'pct_support': pct(close, indicators.get('support_price')),
            'pct_resist': pct(indicators.get('resistance_price'), close),
            'pct_boll_mid': pct(close, indicators.get('boll_mid_20')),
            'ema9_ema21_pct': pct(indicators.get('ema_9'), indicators.get('ema_21')),
            'kama9_kama21_pct': pct(indicators.get('kama_9'), indicators.get('kama_21')),
        }
    )

    atr = indicators.get('atr_14', np.nan)
    # FIX P1.20: ATR-Features IMMER emittieren — fehlt ATR, hatte der
    # Feature-Vektor 35 statt 38 Spalten, predict_proba warf und die ganze
    # Scan-Iteration brach ab. XGBoost kann mit NaN nativ umgehen.
    if pd.notna(atr) and atr > 0:
        features.update(
            {
                'support_atr': (close - indicators.get('support_price', np.nan)) / atr,
                'resist_atr': (indicators.get('resistance_price', np.nan) - close) / atr,
                'boll_width_atr': ((indicators.get('boll_upper_20', 0) - indicators.get('boll_lower_20', 0)) / atr),
            }
        )
    else:
        features.update({'support_atr': np.nan, 'resist_atr': np.nan, 'boll_width_atr': np.nan})

    features['is_long'] = 1.0 if direction.upper() == 'LONG' else 0.0
    return features


# TARGET CALCULATOR

# POSTING LOGIK


def process_ai_trade(conn, symbol, direction, module, live_price, confidence, chart_path=None) -> bool:
    """Calculates trade details, writes to outbox and monitor.

    Returns True wenn der Trade wirklich gepostet wurde, False wenn der
    interne Cooldown den Post unterdrückt hat (P2.30: der Caller schrieb
    vorher posted=True in ml_predictions_master, obwohl nie gepostet wurde).
    """
    target_channel = _kcfg.CH_AI_SR  # Dein Ziel-Kanal

    # FIX: Vorher eigener Cooldown-Check mit `pd.Timestamp.utcnow().tz_localize(None)`
    # → crashes in newer pandas versions (utcnow is tz-aware there) and mixes
    # tz-aware/tz-naive Vergleiche. Jetzt: saubere Version aus market_utils.
    if check_cooldown(conn, module, symbol, direction, 4):
        return False

    # 2. Level & Targets
    is_long = direction == "LONG"
    entry1 = float(live_price)
    entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
    supps, resis = get_hvn_and_sr_levels(conn, symbol, live_price)

    if is_long:
        sl = max([x for x in supps if x < entry2 * 0.99]) if any(x < entry2 * 0.99 for x in supps) else entry2 * 0.975
        t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
    else:
        sl = min([x for x in resis if x > entry2 * 1.01]) if any(x > entry2 * 1.01 for x in resis) else entry2 * 1.025
        t_cands = sorted([x for x in supps if x > 0 and x < (entry1 * 0.99)], reverse=True)

    # FIX: echte Zonen + ggf. 5%-Target wenn letzte Zone zu nah
    targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)
    lev = get_max_leverage(symbol, 20)
    # 3. Cornix & Telegram
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
    lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module} V3"]
    cornix_msg = "\n".join(lines)

    html_caption = f"<b>💥 AI {module} {direction} SIGNAL</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n→ Direction: {direction}\n→ ML Confidence: <b>{confidence:.1%}</b>\n→ Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M')} UTC\n\n<pre>{cornix_msg}</pre>"

    with conn.cursor() as cur:
        # Cornix Text
        cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (target_channel, cornix_msg))
        # Chart Image
        if chart_path:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                (target_channel, html_caption, chart_path),
            )
        # Monitor

        cur.execute(
            """
                        INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
            (
                symbol,
                float(entry1),
                module,
                direction,
                float(confidence),
                float(entry1),
                float(entry2),
                float(sl),
                json.dumps(targets),
            ),
        )
    # FIX (Review Batch 4): Cooldown in DERSELBEN Transaktion wie Outbox +
    # ai_signals setzen. Vorher lief update_cooldown NACH conn.commit() —
    # warf der Cooldown-Upsert (z.B. lock_timeout), blieb der Post committed,
    # aber ohne Cooldown und ohne master-Log → der nächste Scan-Pass hat
    # denselben Trade erneut gepostet (Doppel-Exposure bei Cornix).
    update_cooldown(conn, module, symbol, direction, commit=False)
    conn.commit()
    logger.info(f"🚀 {module} Trade für {symbol} erfolgreich abgefeuert!")
    return True


# MAIN LOOP


def main():
    logger.info("=== 🧠 ML SR BOT (SRA1) AKTIVIERT ===")
    module_name = 'SRA1'

    while True:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # 1. Frische S&R Trades aus der Master-Tabelle suchen
                cur.execute("""
                    SELECT id, time, coin, direction, entry
                    FROM active_trades_master
                    WHERE strategy = 'Support Resistance'
                    AND posted >= NOW() - INTERVAL '60 minutes'
                """)
                fresh_trades = cur.fetchall()

                for trade in fresh_trades:
                    t_id, t_time, coin, direction, entry = trade

                    # FIX P1.20: per-Trade-Isolation. Vorher riss EIN kaputter
                    # Trade (z.B. predict-Fehler) die ganze Iteration ab und der
                    # Pass-Rollback verwarf auch die Shadow-Inserts aller schon
                    # verarbeiteten Trades. Jetzt: commit pro Trade, rollback
                    # betrifft nur den einen.
                    try:
                        # 2. Duplikatprüfung in Master-Log
                        cur.execute(
                            "SELECT 1 FROM ml_predictions_master WHERE trade_id = %s AND model_name = %s",
                            (t_id, module_name),
                        )
                        if cur.fetchone():
                            continue

                        # 3. Indikatoren & Features
                        inds = get_indicators_at_time(conn, coin, t_time)
                        if not inds:
                            continue

                        features = create_feature_row(direction, inds)
                        if not features:
                            continue

                        # 4. XGBoost Vorhersage
                        X = pd.DataFrame([features])
                        model = MODEL_LONG if direction == 'LONG' else MODEL_SHORT
                        conf = float(model.predict_proba(X)[0, 1])

                        # 5. Klassifizierung & Schatten-Log
                        posted = False
                        if conf >= 0.65:
                            # FIX (Review Batch 4): NaN-ATR-Vektoren nicht live posten.
                            # P1.20 lässt fehlende ATR-Features als NaN durch, damit der
                            # Scan nicht mehr crasht — aber das Modell hat im Training nie
                            # NaN in diesen Spalten gesehen, die Confidence darauf ist
                            # unkalibriert. Solche Rows nur shadow-loggen, kein Cornix-Post.
                            if pd.isna(features.get('support_atr', np.nan)):
                                logger.info(f"⚠️ {coin} {direction} conf {conf:.1%} — ATR fehlt, nur Shadow-Log.")
                            else:
                                logger.info(f"🎯 Treffer! {coin} {direction} hat {conf:.1%} Confidence.")
                                chart_p = generate_minichart_image(coin, minutes=240)
                                # FIX P2.30: posted aus dem Rückgabewert — False wenn der
                                # interne 4h-Cooldown den Post unterdrückt hat.
                                posted = process_ai_trade(conn, coin, direction, module_name, entry, conf, chart_p)

                        # Alles >= 0.35 in die Master-History loggen
                        if conf >= 0.35:
                            cur.execute(
                                """
                                INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                                (t_id, module_name, t_time, coin, direction, entry, conf, posted),
                            )
                        else:
                            # Unter 0.45 nur als "erledigt" markieren (minimales Log)
                            cur.execute(
                                "INSERT INTO ml_predictions_master (trade_id, model_name, coin, confidence, posted) VALUES (%s, %s, %s, %s, False)",
                                (t_id, module_name, coin, conf),
                            )
                        conn.commit()
                    except Exception as trade_err:
                        logger.error(f"SRA1: Fehler bei Trade {t_id} ({coin} {direction}): {trade_err}")
                        # Rollback guarded — auf einer toten Connection (DB-Restart)
                        # wirft rollback() selbst und würde sonst bis aus main()
                        # durchschlagen und den Prozess killen.
                        try:
                            conn.rollback()
                        except Exception:
                            logger.error("SRA1: rollback fehlgeschlagen — Pass-Abbruch, Connection wird erneuert.")
                            break

            conn.commit()
        except Exception as e:
            logger.error(f"Fehler im Loop: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass  # tote Connection — close() im finally gibt den Slot frei
        finally:
            if conn:
                conn.close()

        time.sleep(300)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

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
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_MIS_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & CHANNELS (Dynamisches Routing) ---
# Hier trägst du die 4 unterschiedlichen Channel-IDs ein!
MIS_CHANNELS = {
    "8H": _kcfg.CH_MIS_8H,  # 👈 Channel für 8h
    "24H": _kcfg.CH_MIS_24H,  # 👈 Channel für 24h
    "72H": _kcfg.CH_MIS_72H,  # 👈 Channel für 72h
    "168H": _kcfg.CH_MIS_168H,  # 👈 Channel für 168h
}

# --- LOAD ML MODELS ---
PUMP_MODELS = {
    "8h_pump": {
        "model_path": "pump_model_8h_pump_final.pkl",
        "threshold_path": "threshold_8h_pump_final.pkl",
        "model": None,
        "threshold": 0.5,
        "loaded": False,
    },
    "8h_dump": {
        "model_path": "pump_model_8h_dump_final.pkl",
        "threshold_path": "threshold_8h_dump_final.pkl",
        "model": None,
        "threshold": 0.5,
        "loaded": False,
    },
    "24h_pump": {
        "model_path": "pump_model_24h_pump_final.pkl",
        "threshold_path": "threshold_24h_pump_final.pkl",
        "model": None,
        "threshold": 0.5,
        "loaded": False,
    },
    "24h_dump": {
        "model_path": "pump_model_24h_dump_final.pkl",
        "threshold_path": "threshold_24h_dump_final.pkl",
        "model": None,
        "threshold": 0.5,
        "loaded": False,
    },
    "72h_pump": {
        "model_path": "pump_model_72h_pump_final.pkl",
        "threshold_path": "threshold_72h_pump_final.pkl",
        "model": None,
        "threshold": 0.5,
        "loaded": False,
    },
    "72h_dump": {
        "model_path": "pump_model_72h_dump_final.pkl",
        "threshold_path": "threshold_72h_dump_final.pkl",
        "model": None,
        "threshold": 0.5,
        "loaded": False,
    },
    "168h_pump": {
        "model_path": "pump_model_168h_pump_final.pkl",
        "threshold_path": "threshold_168h_pump_final.pkl",
        "model": None,
        "threshold": 0.5,
        "loaded": False,
    },
    "168h_dump": {
        "model_path": "pump_model_168h_dump_final.pkl",
        "threshold_path": "threshold_168h_dump_final.pkl",
        "model": None,
        "threshold": 0.5,
        "loaded": False,
    },
}


def load_pump_models():
    """Lädt alle 8 ML Modelle + Thresholds"""
    loaded_count = 0
    for _horizon, cfg in PUMP_MODELS.items():
        if os.path.exists(cfg["model_path"]):
            try:
                cfg["model"] = joblib.load(cfg["model_path"])
                if os.path.exists(cfg["threshold_path"]):
                    cfg["threshold"] = float(joblib.load(cfg["threshold_path"]))
                else:
                    cfg["threshold"] = 0.60
                cfg["loaded"] = True
                loaded_count += 1
            except Exception as e:
                logger.error(f"Error loading von {cfg['model_path']}: {e}")
        else:
            logger.warning(f"Modell fehlt: {cfg['model_path']}")

    logger.info(f"✅ {loaded_count}/8 Multi-Horizon Modelle (MIS1) loaded successfully.")

    # FIX: Thresholds explizit loggen, damit Drift zwischen Modell-File und
    # Threshold-File sofort auffällt (Thresholds sind separate pkl-Files und
    # können leicht "vergessen werden" mit zu updaten beim Re-Training).
    thresh_summary = ", ".join(f"{h}={cfg['threshold']:.2f}" for h, cfg in PUMP_MODELS.items() if cfg["loaded"])
    logger.info(f"MIS1 Thresholds: {thresh_summary}")


def pct_distance(price_series: pd.Series, indicator_series: pd.Series) -> pd.Series:
    denominator = indicator_series.replace(0, np.nan)
    result = (price_series - indicator_series) / denominator * 100
    return result.fillna(0)


def add_advanced_features(df: pd.DataFrame) -> pd.DataFrame:
    if 'open_time' in df.columns:
        df = df.sort_values('open_time').reset_index(drop=True)

    df['volume_ratio_prev'] = df['volume'] / df['volume'].shift(1)
    df['volume_sma20'] = df['volume'].rolling(20, min_periods=1).mean()
    df['volume_ratio_sma20'] = df['volume'] / df['volume_sma20']

    delta_cols = ['rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24', 'tsi_fast', 'macd_dif']
    for col in delta_cols:
        if col in df.columns:
            df[f'{col}_delta_1'] = df[col].diff(1)

    if 'macd_dif' in df.columns and 'macd_dea' in df.columns:
        df['macd_hist'] = df['macd_dif'] - df['macd_dea']
        df['macd_hist_delta_1'] = df['macd_hist'].diff(1)
    else:
        df['macd_hist'] = 0.0
        df['macd_hist_delta_1'] = 0.0

    df['above_ema_200'] = (df['close'] > df.get('ema_200', df['close'])).astype(int)

    if 'rsi_14' in df.columns:
        df['rsi_14_above_50'] = (df['rsi_14'] > 50).astype(int)
        df['rsi_14_cross_above_30'] = ((df['rsi_14'].shift(1) < 30) & (df['rsi_14'] >= 30)).astype(int)
    else:
        df['rsi_14_above_50'] = 0
        df['rsi_14_cross_above_30'] = 0

    if 'ema_9' in df.columns and 'ema_21' in df.columns:
        df['ema_9_cross_above_21'] = (
            (df['ema_9'].shift(1) < df['ema_21'].shift(1)) & (df['ema_9'] > df['ema_21'])
        ).astype(int)
    else:
        df['ema_9_cross_above_21'] = 0

    eps = 1e-8
    if all(c in df.columns for c in ['close', 'atr_14']):
        df['boll_upper_dist_atr'] = (df['close'] - df.get('boll_upper_20', df['close'])) / (df['atr_14'] + eps)
        df['boll_lower_dist_atr'] = (df['close'] - df.get('boll_lower_20', df['close'])) / (df['atr_14'] + eps)
        df['ema_200_dist_atr'] = (df['close'] - df.get('ema_200', df['close'])) / (df['atr_14'] + eps)
    else:
        df['boll_upper_dist_atr'] = 0.0
        df['boll_lower_dist_atr'] = 0.0
        df['ema_200_dist_atr'] = 0.0

    price = df['close']
    line_cols = [
        c
        for c in df.columns
        if c.startswith(('ema_', 'wma_', 'kama_', 'boll_', 'donchian_')) and not c.endswith('_dist_pct')
    ]
    for col in line_cols:
        df[f'{col}_dist_pct'] = pct_distance(price, df[col])

    return df.fillna(0)


# 🛡️ COOLDOWN CHECK


def check_mis_models():
    conn = get_db_connection()
    conn.autocommit = True

    try:
        with open('coins.json') as f:
            coins = json.load(f)
    except Exception as e:
        logger.error(f"Could not load coins.json: {e}")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    logger.info(f"🔍 Starting MIS1 Model Check für {len(coins)} Coins...")

    # FIX: Einmalig VOR der Coin-Schleife prüfen ob überhaupt ein Modell geladen ist.
    # Vorher stand der Check in der Schleife mit `return` → der ganze Scan brach
    # ab sobald ein einziger Coin kein Modell fand.
    model_sample = next((cfg["model"] for cfg in PUMP_MODELS.values() if cfg["loaded"]), None)
    if not model_sample:
        logger.error("No MIS1 model loaded. Scan aborted.")
        return
    feature_cols = model_sample.feature_names_in_

    for symbol in coins:
        try:
            query = f"""
                SELECT
                    h.open_time, h.close, h.volume,
                    i.rsi_6, i.rsi_9, i.rsi_12, i.rsi_14, i.rsi_24,
                    i.ema_7, i.ema_9, i.ema_12, i.ema_21, i.ema_26, i.ema_34, i.ema_50, i.ema_55, i.ema_89, i.ema_99, i.ema_200,
                    i.wma_7, i.wma_9, i.wma_12, i.wma_21, i.wma_26, i.wma_34, i.wma_50, i.wma_55, i.wma_89, i.wma_99, i.wma_200,
                    i.kama_7, i.kama_9, i.kama_12, i.kama_21, i.kama_26, i.kama_34, i.kama_50, i.kama_55, i.kama_89, i.kama_99,
                    i.boll_upper_20, i.boll_mid_20, i.boll_lower_20,
                    i.donchian_upper_20, i.donchian_mid_20, i.donchian_lower_20,
                    i.tsi_fast_12_7_7 AS tsi_fast,
                    i.macd_dif_normal_12_26_9 AS macd_dif,
                    i.macd_dea_normal_12_26_9 AS macd_dea,
                    i.atr_14
                FROM "{symbol}_1h" h
                LEFT JOIN "{symbol}_1h_indicators" i ON h.open_time = i.open_time
                ORDER BY h.open_time DESC LIMIT 100
            """
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
                if len(rows) < 10:
                    continue
                columns = [desc[0] for desc in cur.description]
                df = pd.DataFrame(rows, columns=columns)

            df = df.iloc[::-1].reset_index(drop=True)

            df_features = add_advanced_features(df)
            df_current = df_features.iloc[-1:]
            current_price = float(df_current['close'].iloc[0])

            # feature_cols wird einmalig vor der Schleife aus model_sample gezogen

            missing = [c for c in feature_cols if c not in df_current.columns]
            if missing:
                continue

            X_current = df_current[feature_cols].values

            # Alle Modelle für diesen Coin testen
            candidates = []
            for horizon, cfg in PUMP_MODELS.items():
                if not cfg["loaded"]:
                    continue

                try:
                    prob = cfg["model"].predict_proba(X_current)[0, 1]
                    if prob >= 0.25:
                        direction = "LONG" if "pump" in horizon.lower() else "SHORT"
                        clean_horizon = horizon.upper().replace("_PUMP", "").replace("_DUMP", "")
                        candidates.append((prob, clean_horizon, direction, cfg["threshold"]))
                except Exception:
                    pass

            if not candidates:
                continue

            candidates.sort(reverse=True, key=lambda x: x[0])
            best_prob, best_horizon, best_direction, best_threshold = candidates[0]
            module_tag = f"MIS1-{best_horizon}"

            # 1. Aktiver Trade Check — prüft ob ein nicht-geschlossener Trade für
            #    genau dieses Modul/Coin/Richtung läuft. Der Cooldown-Check weiter
            #    unten verhindert zusätzlich zu schnelle Folgesignale im Horizon-Fenster.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM ai_signals
                    WHERE symbol = %s AND direction = %s AND model = %s
                """,
                    (symbol, best_direction, module_tag),
                )
                trade_exists = cur.fetchone()

            if trade_exists:
                continue  # Skippingn, Trade läuft live im AI Monitor

            # --- LOGIK ANWENDEN ---
            if best_prob < 0.25:
                pass
            elif 0.25 <= best_prob < best_threshold:
                # Shadow Mode
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1 FROM ml_predictions_master
                        WHERE coin = %s AND direction = %s AND model_name = %s AND time > NOW() - INTERVAL '4 hours'
                    """,
                        (symbol, best_direction, module_tag),
                    )
                    if not cur.fetchone():
                        cur.execute(
                            """
                            INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                            VALUES (0, %s, %s, %s, %s, %s, %s, False)
                        """,
                            (module_tag, now, symbol, best_direction, float(current_price), float(best_prob)),
                        )
            elif best_prob >= best_threshold:
                # 💥 Hard Cooldown Check (8h, 24h, 72h, 168h Sperre je after Modell)
                # check_cooldown returned True wenn Cooldown NOCH AKTIV ist → dann skippen.
                cd_hours = int(best_horizon.replace("H", ""))
                if check_cooldown(conn, module_tag, symbol, best_direction, cd_hours):
                    continue

                logger.info(f"🚀 MIS1 Trade gefunden: {symbol} {best_direction} | {module_tag} (Conf: {best_prob:.1%})")

                # 💥 SMARTE TARGETS (Die neue Core Funktion!)
                trade_setup = calculate_smart_targets(conn, symbol, best_direction, current_price)

                entry1 = trade_setup['entry1']
                entry2 = trade_setup['entry2']
                sl = trade_setup['sl']
                targets = trade_setup['targets']

                is_long = best_direction == "LONG"
                lev = get_max_leverage(symbol, 20)
                emoji = "🚀 PUMP SIGNAL (MIS)" if is_long else "💥 DUMP SIGNAL (MIS)"
                strength = "STRONG" if best_prob >= best_threshold + 0.1 else "MODERATE"

                # RRR (Risk Reward Ratio) Berechnung
                avg_entry = (entry1 + entry2) / 2
                risk_pct = abs((sl - avg_entry) / avg_entry)
                reward_pct = abs((targets[0] - avg_entry) / avg_entry) if targets else 0.01
                rrr = reward_pct / risk_pct if risk_pct > 0 else 0.01

                # Cornix Text
                cornix_msg = f"""📈 Signal for {symbol} 📈
🚨 Direction: {best_direction}
🚨 Leverage: {lev}
🚨 Margin: Cross
🏦 CMP Entry: $ {entry1:.8f}
🏦 Entry 2: $ {entry2:.8f}"""

                for i, t in enumerate(targets[:5], 1):
                    cornix_msg += f"\n💰 TP{i}: $ {t:.8f}"

                cornix_msg += (
                    f"\n💸 Stop Loss: $ {sl:.8f}\n🧠 AI Confidence: {best_prob * 100:.1f}% ({module_tag} Filter)"
                )

                # HTML Visualisierung
                html_caption = f"""<pre>
<b>{emoji}</b>
<b>├─ Coin:</b> <b>{symbol}</b>
<b>├─ Action:</b> <b>{best_direction}</b>
<b>├─ Horizon:</b> <b>{best_horizon}</b>
<b>├─ RRR (T1):</b> <b>1:{rrr:.2f}</b>
<b>└─ ML Confidence:</b> <b>{strength} – {best_prob:.1%}</b>

<b>├─ Entry 1:</b> <b>${entry1:,.8f}</b>
<b>└─ Entry 2:</b> <b>${entry2:,.8f}</b>

<b>├─ Take Profits:</b>
"""
                for i, t in enumerate(targets[:5], 1):
                    pct = abs((t - entry1) / entry1 * 100) * int(lev.replace('x', ''))
                    t_col = "#00ff88" if i <= 2 else "#88ff88"
                    html_caption += f"<b style=\"color:{t_col};\">   T{i}:</b> <b>${t:,.8f}</b> → <b style=\"color:lime;\">+{pct:.1f}%</b>\n"

                sl_loss = risk_pct * 100 * int(lev.replace('x', ''))
                html_caption += f"""<b>└─ Stop Loss:</b> <b>${sl:,.8f}</b> → <b>-{sl_loss:.1f}%</b>

<b>--- CORNIX FORMAT ---</b>
{cornix_msg}</pre>"""

                # Target Channel Routing
                target_channel = MIS_CHANNELS.get(best_horizon, _kcfg.CH_MIS_8H)  # Fallback

                chart_buf = generate_minichart_image(symbol, minutes=240)

                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                        (target_channel, cornix_msg),
                    )

                    if chart_buf:
                        cur.execute(
                            "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                            (target_channel, html_caption, chart_buf),
                        )
                    else:
                        cur.execute(
                            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                            (target_channel, html_caption),
                        )

                    cur.execute(
                        """
                        INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                        (
                            symbol,
                            float(entry1),
                            module_tag,
                            best_direction,
                            float(best_prob),
                            float(entry1),
                            float(entry2),
                            float(sl),
                            json.dumps(targets),
                        ),
                    )

                    cur.execute(
                        """
                        INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                        VALUES (0, %s, %s, %s, %s, %s, %s, True)
                    """,
                        (module_tag, now, symbol, best_direction, float(current_price), float(best_prob)),
                    )

                # Cooldown setzen damit der gleiche Coin/Direction nicht sofort wieder feuert
                update_cooldown(conn, module_tag, symbol, best_direction)

        except Exception as e:
            logger.error(f"Error for {symbol} in MIS1: {e}")

    if conn:
        conn.close()
    logger.info("🏁 MIS1 Model Check stopped.")


def main():
    logger.info("=== 🧠 AI MIS BOT (Multi-Horizon) GESTARTET ===")

    # Tabellen Setup für Cooldown
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_cooldowns (
                module VARCHAR(50),
                coin VARCHAR(20),
                direction VARCHAR(10),
                last_posted_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (module, coin, direction)
            );
        """)
    conn.commit()
    conn.close()

    load_pump_models()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        if now.minute == 11:
            check_mis_models()
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

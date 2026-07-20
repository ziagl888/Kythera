import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import os
import time

import joblib
import pandas as pd

from core import config as _kcfg  # channel ids
from core import shadow_gate
from core.ats_features import (
    ATS_CANDLE_COLUMNS,
    ATS_FEATURES,
    ATS_INDICATOR_COLUMNS,
    TSI_LINE_COL,
    TSI_SIGNAL_COL,
    ats_cross,
    build_ats_features,
)
from core.candles import read_candles_with_indicators, window_start
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.signal_post import log_prediction, post_shadow_ai_signal
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels, hvn_sr_trade_geometry

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

# Feature-Vertrag + Serving-Konstruktion liegen in core.ats_features (EINE Quelle
# mit dem ATS2-Replay/Trainer, harte Regel 7). Alias für die Lesbarkeit unten.
TSI_FEATURES = ATS_FEATURES

MODEL_LONG = None
MODEL_SHORT = None

# ATS2-Shadow (T-2026-CU-9050-125): der Retrain von ATS1 läuft PARALLEL zum
# weiter-live ATS1 und postet nie live — nur überwachte Shadow-Trades (Contract-
# Artefakt aus staging_models/, geladen wenn vorhanden). Der Feature-Vektor ist
# identisch zum ATS1-Serving (build_ats_features / ATS_FEATURES), daher scored
# ATS2 exakt dieselbe Event-Population.
SHADOW_ATS2: dict[str, object | None] = {"LONG": None, "SHORT": None}


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

    # Shadow-Modelle fail-soft nachladen — fehlen sie, läuft Bot 12 unverändert.
    for d in ("LONG", "SHORT"):
        SHADOW_ATS2[d] = shadow_gate.load_shadow_artifact("ATS2", d)
    if any(SHADOW_ATS2.values()):
        loaded = [d for d, m in SHADOW_ATS2.items() if m is not None]
        logger.info(f"👻 ATS2 Shadow-Modelle geladen: {', '.join(loaded)}")


def _emit_ats2_shadow(conn, symbol, direction, is_long, feature_row, entry1, now):
    """ATS2-Shadow-Emission (T-2026-CU-9050-125) — rein additiv, nie live.

    Gleiches TSI-Crossover-Event und derselbe Feature-Vektor wie der Live-ATS1-
    Score. Feuert ATS2 auf der ROHEN prob >= optimal_threshold, wird die
    IDENTISCHE HVN/S-R-Geometrie wie im Live-Pfad gebaut und ein überwachter
    Shadow-Trade (kein Cornix) unter Tag ``ATS2`` geschrieben. Unter Threshold:
    nur die Prediction-Zeile wie heute. Jeder Fehler bleibt hier gekapselt —
    der Live-ATS1-Pfad darf davon NIE betroffen sein.
    """
    if not shadow_gate.shadow_posting_enabled() or not shadow_gate.is_shadow("ATS2", direction):
        return
    art = SHADOW_ATS2.get(direction)
    if art is None:
        return
    try:
        prob = shadow_gate.score_artifact(art, feature_row)
        thr = shadow_gate.artifact_threshold(art)
        if thr is not None and prob < thr:
            if prob >= 0.25:  # SHADOW_FLOOR-Parität mit dem ATS1-Prediction-Log
                log_prediction(conn, "ATS2", symbol, direction, entry1, prob, posted=False)
                conn.commit()
            return
        supps, resis = get_hvn_and_sr_levels(conn, symbol, entry1)
        entry2, sl, t_cands = hvn_sr_trade_geometry(entry1, is_long, supps, resis)
        targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)
        if not targets:
            return
        if post_shadow_ai_signal(conn, "ATS2", symbol, direction, prob, entry1, entry2, sl, targets, n_show=3):
            conn.commit()
    except Exception as e:
        logger.warning(f"ATS2 Shadow für {symbol} {direction} fehlgeschlagen: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


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
            # R1: Erkennung auf GESCHLOSSENEN Kerzen (include_forming=False). Die
            # TSI-Crossover-Detektion lief schon auf iloc[-2] (geschlossen); ohne die
            # forming Kerze ist die jüngste geschlossene jetzt iloc[-1]. core.candles
            # liefert ASC → die bisherige DESC-Umkehr entfällt. (Transitional: der
            # 500er-OBV-Baseline-Start verschiebt sich um genau eine Kerze — bis zum
            # ATS-Retrain vernachlässigbar, §5 q6.)
            df = read_candles_with_indicators(
                conn,
                symbol,
                "1h",
                limit=500,
                # Lower open_time bound → the hyper read excludes old chunks
                # instead of scanning all 126 (T-2026-CU-9050-181). Window ≫ 500
                # candles, so the newest 500 closed candles (and thus the OBV
                # iloc[0] baseline) are byte-identical to the un-bounded read.
                start=window_start("1h", 500),
                include_forming=False,
                candle_columns=ATS_CANDLE_COLUMNS,
                indicator_columns=ATS_INDICATOR_COLUMNS,
            )
            if len(df) < 50:
                continue

            # Alle Spalten zu Float konvertieren
            num_cols = [c for c in df.columns if c != 'open_time']
            for col in num_cols:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

            # 2. CROSSOVER PRÜFEN (letzte GESCHLOSSENE Kerze vs vorletzte)
            # R1: der Frame enthält keine forming Kerze mehr (include_forming=False),
            # daher ist Index -1 die jüngste GESCHLOSSENE Kerze, Index -2 die davor.
            # (Detektion bleibt auf derselben Kerze wie zuvor iloc[-2].)
            current_idx = -1
            prev_idx = -2

            direction = ats_cross(
                df.iloc[prev_idx][TSI_LINE_COL],
                df.iloc[prev_idx][TSI_SIGNAL_COL],
                df.iloc[current_idx][TSI_LINE_COL],
                df.iloc[current_idx][TSI_SIGNAL_COL],
            )
            if direction is None:
                continue
            long_cross = direction == "LONG"

            # 3. LIVE FEATURE ENGINEERING — EINE Quelle mit dem ATS2-Trainer/Replay
            # (core.ats_features.build_ats_features, harte Regel 7). OBV-Normalisierung
            # auf den 500-Kerzen-Fensterstart, VWAP, der 29-Feature-Vertrag und die
            # Reihenfolge liegen dort; tools/walkforward_sim.run_ats ruft dieselbe
            # Funktion — der Parity-Test beweist trainer==serving.
            current_price = float(df.iloc[current_idx]['close'])
            features = build_ats_features(df)

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

            # ATS2-Shadow (T-2026-CU-9050-125): neue Generation parallel scoren
            # und überwacht mit-tracken, BEVOR die ATS1-Band-Logik greift — der
            # ATS2-Score ist von der ATS1-Entscheidung unabhängig.
            _emit_ats2_shadow(conn, symbol, direction, long_cross, features, current_price, now)

            # ATS1 stummgeschaltet (T-2026-CU-9050-127, Operator Michi): ist das
            # ATS1-Bein per shadow_gate auf SILENT gesetzt, läuft der Bot NUR für
            # die ATS2-Shadow-Sammlung — kein ATS1-Ausgang (weder Shadow-Log noch
            # Live-Post). Default-LIVE ⇒ No-op, solange ATS1 nicht stummgeschaltet ist.
            if not shadow_gate.is_live(module_tag, direction):
                continue

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
                supps, resis = get_hvn_and_sr_levels(conn, symbol, current_price)
                # EINE Quelle mit dem ATS2-Replay (core.trade_utils.hvn_sr_trade_geometry;
                # byte-identisch zur bisherigen inline-Geometrie) → Replay-Geometrie ==
                # Live-Geometrie (harte Regel 7). Entry2 = ±5 %, SL/TP aus HVN/SR-Leveln.
                entry2, sl, t_cands = hvn_sr_trade_geometry(entry1, is_long, supps, resis)

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

        # P3.10: comments corrected to match code — fires at minute 13 (not 8).
        if now.minute == 13:
            check_tsi_crossovers()
            # Schlafen, damit er nicht mehrfach in Minute 13 triggert
            time.sleep(60)
        else:
            # Checkt alle 10 Sekunden, ob Minute 13 erreicht ist
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

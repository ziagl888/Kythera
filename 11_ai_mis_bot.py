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
from core.mis_features import (
    BINARY_FLAG_FEATURES,
    MIS_SQL_INDICATOR_SELECT,
    add_advanced_features,
    assert_features_alive,
)
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
# MIS2 (Operator-Entscheide 2026-07-06, docs/MODEL_INTENT.md §1):
#   * Move-Label-Modelle (±5%/8h, ±10%/24h, ±15%/72h, ±25%/168h) ersetzen die
#     alten MIS1-Modelle KOMPLETT — MIS1 ist abgeschaltet, kein Legacy-Fallback.
#   * NUR die Pump-Seite ist deploybar (alle 4 Horizonte mit Out-of-Time-Ertrag);
#     die Dump-Seite erkennt Dumps zwar gut, verdient aber mit der Short-Geometrie
#     nichts — sie wird separat überarbeitet (eigener Task).
#   * Basis-Mix nach Testlage: Close-Labels für 8h/24h/168h, Wick für 72h.
#   Artefakt = dict(model, features, optimal_threshold, calibrator_isotonic, meta)
#   aus tools/retrain_from_replay.py --label-mode move.
MODEL_GENERATION = "MIS2"
PUMP_MODELS = {
    key: {
        "artifact_path": f"mis2_model_{key}.pkl",
        "model": None,
        "threshold": 0.5,
        "features": None,
        "calibrator": None,
        "loaded": False,
    }
    for key in ("8h_pump", "24h_pump", "72h_pump", "168h_pump")
}


def load_pump_models():
    """Lädt die MIS2-Move-Artefakte (kein Legacy-Fallback — MIS1 ist aus)."""
    loaded_count = 0
    for key, cfg in PUMP_MODELS.items():
        try:
            if os.path.exists(cfg["artifact_path"]):
                art = joblib.load(cfg["artifact_path"])
                cfg["model"] = art["model"]
                cfg["threshold"] = float(art["optimal_threshold"])
                cfg["features"] = list(art["features"])
                cfg["calibrator"] = art.get("calibrator_isotonic")
                cfg["loaded"] = True
                loaded_count += 1
            else:
                logger.warning(f"Modell fehlt: {cfg['artifact_path']}")
        except Exception as e:
            logger.error(f"Error loading von {key}: {e}")

    logger.info(f"✅ {loaded_count}/{len(PUMP_MODELS)} Multi-Horizon Modelle ({MODEL_GENERATION}) loaded successfully.")

    # FIX: Thresholds explizit loggen, damit Drift zwischen Modell-File und
    # Threshold-File sofort auffällt.
    thresh_summary = ", ".join(f"{h}={cfg['threshold']:.2f}" for h, cfg in PUMP_MODELS.items() if cfg["loaded"])
    logger.info(f"{MODEL_GENERATION} Thresholds: {thresh_summary}")


def startup_feature_selfcheck():
    """P0.12-Muster (wie 18_ai_abr1_bot): Feature-Pipeline auf echten Daten von
    bis zu 3 Coins rechnen und hart abbrechen, wenn ein kontinuierliches Feature
    konstant ist oder ein geladenes Modell Features verlangt, die der (bereinigte)
    Builder nicht mehr liefert — Legacy-67-Feature-Modelle mit den Leakage-
    Spalten werden dabei entladen statt still mit fillna(0)-Nullen zu scoren."""
    try:
        with open('coins.json') as f:
            coins = json.load(f)
    except Exception as e:
        logger.critical(f"Selbsttest: coins.json nicht ladbar: {e}")
        exit(1)

    conn = get_db_connection()
    try:
        frames = []
        for symbol in coins[:10]:
            df = _fetch_mis_frame(conn, symbol)
            if df is None or len(df) < 30:
                continue
            frames.append(add_advanced_features(df))
            if len(frames) >= 3:
                break
        if not frames:
            logger.critical("❌ Feature-Selbsttest: keine verwertbaren Daten gefunden — Abbruch.")
            exit(1)
        sample = pd.concat(frames, ignore_index=True)
        try:
            assert_features_alive(sample, context=" (Bot-Startup)")
        except ValueError as e:
            logger.critical(f"❌ {e}")
            exit(1)
        constant_flags = [c for c in BINARY_FLAG_FEATURES if sample[c].nunique(dropna=False) <= 1]
        if constant_flags:
            logger.warning(f"Selbsttest: Binär-Flags konstant über die Stichprobe (kann legitim sein): {constant_flags}")

        for key, cfg in PUMP_MODELS.items():
            if not cfg["loaded"]:
                continue
            missing = [c for c in (cfg["features"] or []) if c not in sample.columns]
            if missing:
                logger.critical(
                    f"❌ {key}: Modell verlangt Features, die der Builder nicht liefert "
                    f"(vermutlich Legacy-Leakage-Spalten, Report 13): {missing[:6]}… — Modell entladen."
                )
                cfg["loaded"] = False
                cfg["model"] = None
        if not any(cfg["loaded"] for cfg in PUMP_MODELS.values()):
            logger.critical("❌ Kein kompatibles MIS1-Modell übrig — Abbruch.")
            exit(1)
        n_ok = sum(1 for cfg in PUMP_MODELS.values() if cfg["loaded"])
        logger.info(f"✅ Feature-Selbsttest bestanden ({len(sample)} Zeilen, {len(frames)} Coins, {n_ok} Modelle kompatibel).")
    finally:
        conn.close()


# 🛡️ COOLDOWN CHECK


def _fetch_mis_frame(conn, symbol):
    """Letzte 100 1h-Kerzen + Indikator-Join — Spaltenliste kommt aus
    core.mis_features (eine Quelle für Bot, Trainer und Simulator)."""
    query = f"""
        SELECT
            h.open_time, h.close, h.volume,
            {MIS_SQL_INDICATOR_SELECT}
        FROM "{symbol}_1h" h
        LEFT JOIN "{symbol}_1h_indicators" i ON h.open_time = i.open_time
        ORDER BY h.open_time DESC LIMIT 100
    """
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
        if len(rows) < 10:
            return None
        columns = [desc[0] for desc in cur.description]
        df = pd.DataFrame(rows, columns=columns)
    return df.iloc[::-1].reset_index(drop=True)


def check_mis_models():
    # FIX P2.32: kein autocommit mehr — Outbox-Post, ai_signals-Insert und
    # master-Log gehören pro Signal in EINE Transaktion (Commit übernimmt
    # update_cooldown bzw. der explizite Commit im Shadow-Pfad). Vorher
    # konnte ein Crash mittendrin einen gePOSTeten Trade ohne Tracking
    # hinterlassen.
    conn = get_db_connection()

    try:
        with open('coins.json') as f:
            coins = json.load(f)
    except Exception as e:
        logger.error(f"Could not load coins.json: {e}")
        conn.close()  # Pool-Slot freigeben (Review Batch 4)
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    logger.info(f"🔍 Starting MIS1 Model Check für {len(coins)} Coins...")

    # FIX: Einmalig VOR der Coin-Schleife prüfen ob überhaupt ein Modell geladen ist.
    # Vorher stand der Check in der Schleife mit `return` → der ganze Scan brach
    # ab sobald ein einziger Coin kein Modell fand.
    if not any(cfg["loaded"] for cfg in PUMP_MODELS.values()):
        logger.error("No MIS1 model loaded. Scan aborted.")
        conn.close()  # Pool-Slot freigeben (Review Batch 4)
        return

    conn_dead = False
    for symbol in coins:
        try:
            df = _fetch_mis_frame(conn, symbol)
            if df is None:
                continue

            df_features = add_advanced_features(df)
            # FIX P1.17: Modell-Features aus der letzten GESCHLOSSENEN Kerze (iloc[-2]),
            # nicht aus der laufenden (iloc[-1]). Die offene Kerze liefert stale/verzerrte
            # Volume-/Indikator-Partials → strukturell verzerrte Features auf jeder
            # Prediction. Vorbild: 12_ai_ats_bot.py nutzt current_idx=-2.
            # Der Entry-Preis bleibt bewusst live (aktueller Kurs aus iloc[-1]).
            df_current = df_features.iloc[-2:-1]
            current_price = float(df_features['close'].iloc[-1])

            # Alle Modelle für diesen Coin testen — Feature-Auswahl je Modell
            # NAMENSBASIERT über das DataFrame (P1.18: `.values` hatte die
            # sklearn-Namensvalidierung deaktiviert; Kompatibilität wurde beim
            # Startup-Selbsttest bereits hart geprüft).
            candidates = []
            for horizon, cfg in PUMP_MODELS.items():
                if not cfg["loaded"]:
                    continue

                try:
                    X_current = df_current[cfg["features"]]
                    prob = float(cfg["model"].predict_proba(X_current)[0, 1])
                    if prob >= 0.25:
                        direction = "LONG" if "pump" in horizon.lower() else "SHORT"
                        clean_horizon = horizon.upper().replace("_PUMP", "").replace("_DUMP", "")
                        # Kalibrierte Confidence (Isotonic aus dem Retrain-Artefakt)
                        # für Anzeige/Logging; das GATING läuft weiter über die rohe
                        # Probability, denn der Threshold wurde auf rohen Val-Probs
                        # gewählt (tools/retrain_from_replay.py).
                        if cfg["calibrator"] is not None:
                            conf = float(np.clip(cfg["calibrator"].predict([prob])[0], 0.0, 1.0))
                        else:
                            conf = prob
                        candidates.append((prob, clean_horizon, direction, cfg["threshold"], conf))
                except Exception as e:
                    logger.error(f"{symbol} {horizon}: predict fehlgeschlagen: {e}")

            if not candidates:
                continue

            # FIX P2.33: nach Abstand zur MODELL-EIGENEN Schwelle ranken, nicht
            # nach roher Probability — die 8 Modelle sind unterschiedlich
            # kalibriert, ein 0.55er unter-Schwelle-Kandidat verdrängte sonst
            # ein 0.52er über-Schwelle-Signal.
            candidates.sort(reverse=True, key=lambda x: x[0] - x[3])
            best_prob, best_horizon, best_direction, best_threshold, best_conf = candidates[0]
            module_tag = f"{MODEL_GENERATION}-{best_horizon}"  # z. B. MIS2-72H (Versionierungs-Regel)

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
                            (module_tag, now, symbol, best_direction, float(current_price), float(best_conf)),
                        )
                conn.commit()  # P2.32: Shadow-Insert explizit committen (autocommit ist aus)
            elif best_prob >= best_threshold:
                # 💥 Hard Cooldown Check (8h, 24h, 72h, 168h Sperre je after Modell)
                # check_cooldown returned True wenn Cooldown NOCH AKTIV ist → dann skippen.
                cd_hours = int(best_horizon.replace("H", ""))
                if check_cooldown(conn, module_tag, symbol, best_direction, cd_hours):
                    continue

                logger.info(f"🚀 MIS1 Trade gefunden: {symbol} {best_direction} | {module_tag} (raw {best_prob:.3f} / kalibriert {best_conf:.1%})")

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
                    f"\n💸 Stop Loss: $ {sl:.8f}\n🧠 AI Confidence: {best_conf * 100:.1f}% ({module_tag} Filter)"
                )

                # HTML Visualisierung
                html_caption = f"""<pre>
<b>{emoji}</b>
<b>├─ Coin:</b> <b>{symbol}</b>
<b>├─ Action:</b> <b>{best_direction}</b>
<b>├─ Horizon:</b> <b>{best_horizon}</b>
<b>├─ RRR (T1):</b> <b>1:{rrr:.2f}</b>
<b>└─ ML Confidence:</b> <b>{strength} – {best_conf:.1%}</b>

<b>├─ Entry 1:</b> <b>${entry1:,.8f}</b>
<b>└─ Entry 2:</b> <b>${entry2:,.8f}</b>

<b>├─ Take Profits:</b>
"""
                for i, t in enumerate(targets[:5], 1):
                    pct = abs((t - entry1) / entry1 * 100) * int(lev.replace('x', ''))
                    t_col = "#00ff88" if i <= 2 else "#88ff88"
                    html_caption += f"<b style=\"color:{t_col};\">   T{i}:</b> <b>${t:,.8f}</b> → <b style=\"color:lime;\">+{pct:.1f}%</b>\n"

                sl_loss = risk_pct * 100 * int(lev.replace('x', ''))
                # FIX Doppel-Post (2026-07-06, Flotten-Sweep): Caption ohne
                # eingebetteten Cornix-Block — Cornix parste beide Nachrichten.
                html_caption += f"""<b>└─ Stop Loss:</b> <b>${sl:,.8f}</b> → <b>-{sl_loss:.1f}%</b></pre>"""

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
                            float(best_conf),
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
                        (module_tag, now, symbol, best_direction, float(current_price), float(best_conf)),
                    )

                # Cooldown setzen damit der gleiche Coin/Direction nicht sofort wieder feuert.
                # P2.32: update_cooldown committed (default commit=True) und schließt damit
                # die EINE Transaktion aus Outbox-Posts + ai_signals + master-Log atomar ab.
                update_cooldown(conn, module_tag, symbol, best_direction)

        except Exception as e:
            logger.error(f"Error for {symbol} in MIS1: {e}")
        finally:
            # P2.32 + Review Batch 4: Transaktion pro Coin IMMER schließen.
            # (a) Eine aborted Transaktion würde sonst alle folgenden Coins
            #     vergiften ("current transaction is aborted", vgl. P1.23).
            # (b) Eine offene Read-Transaktion über den ganzen 538-Coin-Scan
            #     friert NOW() (= transaction_timestamp) auf den Scan-Start ein
            #     → telegram_outbox.created_at rückdatiert (Orchestrator-
            #     Staleness-Filter verwirft die Signale still) und Cooldowns
            #     werden um die Scan-Dauer verkürzt.
            # Nach einem Commit-Pfad ist der rollback ein No-op.
            try:
                conn.rollback()
            except Exception:
                logger.error("MIS1: rollback fehlgeschlagen (tote Connection) — Scan-Abbruch.")
                conn_dead = True
        if conn_dead:
            break

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
    # P0.12-Muster: Feature-Pipeline + Modell-Kompatibilität hart prüfen,
    # BEVOR der Scan-Loop startet (inkompatible Legacy-Modelle werden entladen).
    startup_feature_selfcheck()

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

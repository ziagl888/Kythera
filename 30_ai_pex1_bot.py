# 30_ai_pex1_bot.py — PEX1 "Pump-Exhaustion-Short" (Report 15, S6).
"""
Short-only ML-Bot auf Pump-Erschöpfung: konsumiert die Events des
10_pump_dump_detector (pump_dump_events, volume_ratio >= 5 — Gate live wie im
Training gespiegelt, Report 13 EPD1-P0) und shortet in die Erschöpfung, wenn
das Binär-Modell (tools/pex1_build_dataset.py + tools/new_models_train.py
--strategy pex1) TP1-vor-SL über der Val-Schwelle sieht.

Eigenschaften:
  * NUR SHORT — die EPD1-Richtungs-Asymmetrie (SHORT 76,5% vs LONG 50,2% WR)
    ist die Evidenz-Basis; eine Long-Seite existiert bewusst nicht.
  * Geometrie: calculate_smart_targets (SR-basiert) — exakt die Label-Geometrie.
  * Posting in CH_NEW_IDEAS; NEW_IDEAS_LIVE_POSTING=0 schaltet auf Shadow-only
    (ml_predictions_master, posted=false).
  * Ohne Artefakt (pex1_model.pkl) läuft der Bot im Idle-Modus — Code kann vor
    dem VPS-Training deployt werden.

Watchdog: start_delay=191.
"""

import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import os
import time

import numpy as np
import pandas as pd

from core import config as _kcfg
from core.database import get_db_connection
from core.market_utils import check_cooldown, update_cooldown
from core.model_artifacts import calibrated_confidence, load_artifact, maybe_reload
from core.research_features import (
    CONTEXT_MIN_CANDLES,
    PEX1_FEATURES,
    PEX1_MIN_PUMP_PCHG_60S,
    PEX1_MIN_VOL_RATIO,
    assert_features_alive,
    build_pex1_row,
    fetch_context_frame,
)
from core.signal_post import has_open_ai_signal, log_prediction, post_ai_signal
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - PEX1_BOT - %(message)s')
logger = logging.getLogger(__name__)

MODEL_ID = "PEX1"
ARTIFACT_PATH = "pex1_model.pkl"
TARGET_CHANNEL_ID = _kcfg.CH_NEW_IDEAS
LIVE_POSTING = os.getenv("NEW_IDEAS_LIVE_POSTING", "1") == "1"
SHADOW_FLOOR = 0.25  # darunter nicht mal Shadow loggen (MIS-Konvention)
COOLDOWN_HOURS = 4  # je Coin — gespiegelt im Trainings-Dedup
ARTIFACT_RETRY_S = 1800  # Idle-Modus: alle 30 min auf frisches Deploy prüfen

ARTIFACT = load_artifact(ARTIFACT_PATH, PEX1_FEATURES, MODEL_ID)


def ensure_artifact() -> None:
    global ARTIFACT
    if ARTIFACT["loaded"]:
        ARTIFACT = maybe_reload(ARTIFACT, PEX1_FEATURES)
    elif time.time() - ARTIFACT["loaded_at"] > ARTIFACT_RETRY_S:
        ARTIFACT = load_artifact(ARTIFACT_PATH, PEX1_FEATURES, MODEL_ID)


def detect_spike_time_offset_h(conn) -> int:
    """pump_dump_events.spike_time ist TIMESTAMP ohne TZ — je nach Session-TZ
    des Detectors kann dort Lokalzeit statt UTC stehen. Der Offset wird gegen
    die Wanduhr gemessen (Events laufen bei 538 Coins quasi kontinuierlich auf),
    damit spike_age korrekt ist. Watermark-Vergleiche bleiben in Roh-Domäne."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(spike_time) FROM pump_dump_events")
        row = cur.fetchone()
    if not row or row[0] is None:
        return 0
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    diff_h = (row[0] - now).total_seconds() / 3600.0
    return int(np.clip(round(diff_h), -12, 12))


def startup_feature_selfcheck() -> None:
    """P0.12-Muster: Kontext-Pipeline auf echten Daten von 3 Coins rechnen und
    hart abbrechen, wenn ein kontinuierliches Feature konstant ist."""
    try:
        with open("coins.json") as f:
            coins = json.load(f)
    except Exception as e:
        logger.critical(f"Selbsttest: coins.json nicht ladbar: {e}")
        exit(1)

    conn = get_db_connection()
    try:
        rows = []
        dummy_event = {
            "volume_ratio": 6.0,
            "price_change_60s": 2.0,
            "buy_pressure": 0.8,
            "volatility": 0.01,
        }
        used = 0
        for symbol in coins[:15]:
            res = fetch_context_frame(conn, symbol)
            if res is None:
                continue
            df, idx = res
            for back in range(0, 8):
                if idx - back >= CONTEXT_MIN_CANDLES - 1:
                    rows.append(build_pex1_row(dummy_event, df, idx - back))
            used += 1
            if used >= 3:
                break
        # Event-Features sind im Selbsttest konstruktionsbedingt konstant (Dummy).
        assert_features_alive(
            rows,
            PEX1_FEATURES,
            binary_ok={"ev_volume_ratio", "ev_price_change_60s", "ev_buy_pressure", "ev_volatility"},
            context=" (PEX1-Startup)",
        )
        logger.info(f"✅ Feature-Selbsttest bestanden ({len(rows)} Zeilen, {used} Coins).")
    except ValueError as e:
        logger.critical(f"❌ {e}")
        exit(1)
    finally:
        conn.close()


def fetch_new_events(conn, watermark) -> list[dict]:
    """Neue Pump-Events über den Live-Gates (Spiegel des Trainings-Samplings)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, spike_time, volume_ratio, price_change_60s, buy_pressure, volatility
            FROM pump_dump_events
            WHERE spike_time > %s
              AND volume_ratio >= %s
              AND price_change_60s >= %s
            ORDER BY spike_time ASC
            """,
            (watermark, PEX1_MIN_VOL_RATIO, PEX1_MIN_PUMP_PCHG_60S),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def process_event(conn, event: dict, offset_h: int) -> None:
    symbol = str(event["symbol"]).upper()
    if not symbol.endswith("USDT"):
        return
    direction = "SHORT"

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    spike_utc = event["spike_time"] - datetime.timedelta(hours=offset_h)
    spike_age_min = max(0.0, (now - spike_utc).total_seconds() / 60.0)
    if spike_age_min > 30.0:
        return  # stale Event (Bot-Downtime/Catch-up) — Exhaustion-These verfallen

    if check_cooldown(conn, MODEL_ID, symbol, direction, COOLDOWN_HOURS):
        return
    if has_open_ai_signal(conn, symbol, direction, ARTIFACT["tag"]):
        return

    # Feature-Kerze relativ zur EVENT-Zeit (floor-1 wie im Training) — ein über
    # eine Stundengrenze verarbeitetes Event sähe sonst eine spätere Kerze, bei
    # PEX1 wäre das die Pump-Kerze selbst (Review-Fix 2026-07-06).
    res = fetch_context_frame(conn, symbol, as_of=spike_utc)
    if res is None:
        return
    df, idx = res

    feature_row = build_pex1_row(event, df, idx)
    missing = [c for c in ARTIFACT["features"] if c not in feature_row]
    if missing:
        raise ValueError(f"Feature-Vertrag verletzt — fehlend: {missing}")
    X = pd.DataFrame([{c: feature_row[c] for c in ARTIFACT["features"]}], dtype=float)
    X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

    prob = float(ARTIFACT["model"].predict_proba(X)[0, 1])
    conf = calibrated_confidence(ARTIFACT, prob)
    live_price = float(df["close"].iloc[-1])

    logger.info(
        f"PEX1 Pump-Event {symbol} | vol_ratio {float(event['volume_ratio']):.1f} | "
        f"Prob {prob:.3f} (Gate {ARTIFACT['threshold']:.2f})"
    )

    if prob >= ARTIFACT["threshold"] and LIVE_POSTING:
        setup = calculate_smart_targets(conn, symbol, direction, live_price)
        post_ai_signal(
            conn,
            TARGET_CHANNEL_ID,
            ARTIFACT["tag"],
            symbol,
            direction,
            conf,
            setup["entry1"],
            setup["entry2"],
            setup["sl"],
            setup["targets"],
            source_desc="AI Pump Exhaustion Model",
            extra_info_lines=[
                f"Pump: +{float(event['price_change_60s']):.1f}%/60s, vol×{float(event['volume_ratio']):.1f}"
            ],
        )
        log_prediction(conn, ARTIFACT["tag"], symbol, direction, live_price, conf, posted=True)
    else:
        if prob >= ARTIFACT["threshold"]:
            logger.info(f"👻 SHADOW-Post {symbol} (p={prob:.2f}) — Live-Posting deaktiviert.")
        if prob >= SHADOW_FLOOR:
            log_prediction(conn, ARTIFACT["tag"], symbol, direction, live_price, conf, posted=False)
    # Cooldown auf JEDEM gescorten Event — Spiegel des unbedingten 4h-Dedups im
    # Training; nur so sieht das Modell live dieselbe Event-Verteilung
    # (Review-Fix 2026-07-06). update_cooldown committet die Transaktion atomar.
    update_cooldown(conn, MODEL_ID, symbol, direction)


def main() -> None:
    global LIVE_POSTING
    logger.info("=== 💥 AI PEX1 BOT (Pump-Exhaustion-Short, S6) GESTARTET ===")
    if TARGET_CHANNEL_ID == 0:
        logger.warning("CH_NEW_IDEAS nicht gesetzt — erzwinge Shadow-only-Modus.")
        LIVE_POSTING = False
    logger.info(f"Posting: {'LIVE' if LIVE_POSTING else 'SHADOW-ONLY'}")

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_cooldowns (
                module VARCHAR(50), coin VARCHAR(20), direction VARCHAR(10),
                last_posted_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (module, coin, direction)
            );
        """)
        # Poll-Pfad läuft jede Minute auf spike_time — ohne Index wäre das ein
        # Seq-Scan pro Zyklus (Tabelle bleibt dank P1.40-Retention klein, aber
        # der Index macht den Watermark-Scan konstant billig).
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pde_spike_time ON pump_dump_events (spike_time)")
        cur.execute("SELECT MAX(spike_time) FROM pump_dump_events")
        row = cur.fetchone()
    conn.commit()
    # Nur Events NACH dem Start verarbeiten (kein Replay alter Pumps beim Boot).
    watermark = row[0] if row and row[0] else datetime.datetime(2000, 1, 1)
    conn.close()

    startup_feature_selfcheck()

    while True:
        ensure_artifact()
        if not ARTIFACT["loaded"]:
            time.sleep(60)
            continue

        conn = get_db_connection()
        try:
            offset_h = detect_spike_time_offset_h(conn)
            events = fetch_new_events(conn, watermark)
            conn_dead = False
            for event in events:
                watermark = max(watermark, event["spike_time"])
                try:
                    process_event(conn, event, offset_h)
                except Exception as e:
                    logger.error(f"Error für {event.get('symbol')}: {e}")
                finally:
                    # P2.32-Muster: Transaktion pro Event IMMER schließen —
                    # nach einem Commit-Pfad ist der Rollback ein No-op.
                    try:
                        conn.rollback()
                    except Exception:
                        logger.error("Rollback fehlgeschlagen (tote Connection) — Zyklus-Abbruch.")
                        conn_dead = True
                if conn_dead:
                    break
        except Exception as e:
            logger.error(f"PEX1-Scan-Fehler: {e}")
        finally:
            conn.close()
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

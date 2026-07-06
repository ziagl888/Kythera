# 32_ai_trm1_bot.py — TRM1 "Transition-Resolution-Modell" (Report 15, S10).
"""
Läuft NUR im TRANSITION-Regime (44,5% der Zeit — E8) und sagt die
Auflösungsrichtung voraus (TREND_UP / TREND_DOWN / keine handelbare Auflösung)
aus den regime_history-Rohfeatures. Prognostiziert das Modell eine Trend-
Auflösung über der Val-Schwelle, postet der Bot ein BTCUSDT-Signal in diese
Richtung (BULL→LONG, BEAR→SHORT) — messbar über ai_signals wie jeder andere Bot
(Operator-Entscheid 2026-07-06).

Klassen-Vertrag (core/research_features): 0 = OTHER (CHOP/HIGH_VOLA/keine
Auflösung), 1 = TREND_UP, 2 = TREND_DOWN. Trainer: tools/trm1_build_dataset.py
+ tools/new_models_train.py --strategy trm1.

Läuft alle 5 Minuten (Raster des 26_regime_detector, +4 min versetzt).
Watchdog: start_delay=207.
"""

import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
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
    TRM1_CLASS_DOWN,
    TRM1_CLASS_UP,
    TRM1_FEATURES,
    TRM1_WINDOW_CHECKS,
    assert_features_alive,
    build_trm1_row,
    fetch_context_frame,
)
from core.signal_post import has_open_ai_signal, log_prediction, post_ai_signal
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - TRM1_BOT - %(message)s')
logger = logging.getLogger(__name__)

MODEL_ID = "TRM1"
ARTIFACT_PATH = "trm1_model.pkl"
TARGET_CHANNEL_ID = _kcfg.CH_NEW_IDEAS
LIVE_POSTING = os.getenv("NEW_IDEAS_LIVE_POSTING", "1") == "1"
SHADOW_FLOOR = 0.25
TRADE_SYMBOL = "BTCUSDT"
COOLDOWN_HOURS = 12  # höchstens ein Signal je Richtung/Episode-Zeitfenster
ARTIFACT_RETRY_S = 1800

ARTIFACT = load_artifact(ARTIFACT_PATH, TRM1_FEATURES, MODEL_ID)


def ensure_artifact() -> None:
    global ARTIFACT
    if ARTIFACT["loaded"]:
        ARTIFACT = maybe_reload(ARTIFACT, TRM1_FEATURES)
    elif time.time() - ARTIFACT["loaded_at"] > ARTIFACT_RETRY_S:
        ARTIFACT = load_artifact(ARTIFACT_PATH, TRM1_FEATURES, MODEL_ID)


def fetch_regime_state(conn) -> tuple[str, float] | None:
    """Debounced-Regime + Minuten seit Regime-Beginn aus regime_current."""
    with conn.cursor() as cur:
        cur.execute("SELECT regime, since FROM regime_current WHERE id = 1")
        row = cur.fetchone()
    if row is None:
        return None
    regime, since = str(row[0]).upper(), row[1]
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if since.tzinfo is not None:
        since = since.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    minutes = max(0.0, (now - since).total_seconds() / 60.0)
    return regime, minutes


def fetch_regime_window(conn, limit: int = TRM1_WINDOW_CHECKS) -> list[dict]:
    """Letzte Checks aus regime_history, chronologisch ASC (ts = naive UTC)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts, regime, btc_return_1h, btc_return_4h, btc_atr_1h_pct,
                   btc_atr_4h_pct, btcdom_return_24h, confidence_btc, confidence_alt
            FROM regime_history ORDER BY ts DESC LIMIT %s
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    return rows[::-1]


def startup_feature_selfcheck() -> bool:
    """P0.12-Muster: Features über echte regime_history-Fenster rechnen.

    Rückgabe False bei (noch) zu wenig regime_history — der Aufrufer wartet
    dann statt zu crashen (frisches Setup füllt sich alle 5 min selbst; ein
    exit(1) würde einen ~2h-Watchdog-Restart-Loop erzeugen, Review-Fix
    2026-07-06). Kaputte Features bleiben ein harter Abbruch."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, regime, btc_return_1h, btc_return_4h, btc_atr_1h_pct,
                       btc_atr_4h_pct, btcdom_return_24h, confidence_btc, confidence_alt
                FROM regime_history ORDER BY ts DESC LIMIT 200
                """
            )
            cols = [d[0] for d in cur.description]
            hist = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()][::-1]
        if len(hist) < TRM1_WINDOW_CHECKS + 10:
            logger.warning("Selbsttest: zu wenig regime_history-Zeilen — läuft 26_regime_detector? Warte.")
            return False
        rows = []
        for end in range(TRM1_WINDOW_CHECKS, len(hist), 5):
            window = hist[end - TRM1_WINDOW_CHECKS : end]
            rows.append(build_trm1_row(window, minutes_in_transition=float(end)))
        # Fraktions-/Konfidenz-Features dürfen über ruhige Phasen konstant sein.
        assert_features_alive(
            rows,
            TRM1_FEATURES,
            binary_ok={
                "frac_up_1h",
                "frac_down_1h",
                "frac_chop_1h",
                "frac_highvola_1h",
                "confidence_btc",
                "confidence_alt",
                "btcdom_return_24h",
            },
            context=" (TRM1-Startup)",
        )
        logger.info(f"✅ Feature-Selbsttest bestanden ({len(rows)} Fenster).")
        return True
    except ValueError as e:
        logger.critical(f"❌ {e}")
        exit(1)
    finally:
        conn.close()


def run_check() -> None:
    conn = get_db_connection()
    try:
        state = fetch_regime_state(conn)
        if state is None:
            logger.warning("regime_current leer — läuft 26_regime_detector?")
            return
        regime, minutes_in = state
        if regime != "TRANSITION":
            return

        window = fetch_regime_window(conn)
        if len(window) < 2:
            return
        feature_row = build_trm1_row(window, minutes_in)
        missing = [c for c in ARTIFACT["features"] if c not in feature_row]
        if missing:
            raise ValueError(f"Feature-Vertrag verletzt — fehlend: {missing}")
        X = pd.DataFrame([{c: feature_row[c] for c in ARTIFACT["features"]}], dtype=float)
        X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

        proba = ARTIFACT["model"].predict_proba(X)[0]
        p_up, p_down = float(proba[TRM1_CLASS_UP]), float(proba[TRM1_CLASS_DOWN])
        direction = "LONG" if p_up >= p_down else "SHORT"
        prob = max(p_up, p_down)
        if prob < SHADOW_FLOOR:
            return
        conf = calibrated_confidence(ARTIFACT, prob)

        res = fetch_context_frame(conn, TRADE_SYMBOL)
        if res is None:
            return
        df, _ = res
        live_price = float(df["close"].iloc[-1])

        logger.info(
            f"TRM1 TRANSITION seit {minutes_in:.0f} min | P(up)={p_up:.3f} "
            f"P(down)={p_down:.3f} (Gate {ARTIFACT['threshold']:.2f})"
        )

        if check_cooldown(conn, MODEL_ID, TRADE_SYMBOL, direction, COOLDOWN_HOURS):
            return
        if has_open_ai_signal(conn, TRADE_SYMBOL, direction, ARTIFACT["tag"]):
            return
        # Kein Self-Hedge (Review-Fix 2026-07-06): kippt die Prognose im
        # 5-min-Takt, während der Gegen-Trade noch offen ist, würde TRM1 sonst
        # gleichzeitige Gegenpositionen auf BTCUSDT posten — dann nur Shadow.
        opposite = "SHORT" if direction == "LONG" else "LONG"
        allow_post = not has_open_ai_signal(conn, TRADE_SYMBOL, opposite, ARTIFACT["tag"])
        if not allow_post and prob >= ARTIFACT["threshold"]:
            logger.info(f"⛔ Gegenposition ({opposite}) offen — {direction}-Signal nur als Shadow.")

        if prob >= ARTIFACT["threshold"] and LIVE_POSTING and allow_post:
            setup = calculate_smart_targets(conn, TRADE_SYMBOL, direction, live_price)
            post_ai_signal(
                conn,
                TARGET_CHANNEL_ID,
                ARTIFACT["tag"],
                TRADE_SYMBOL,
                direction,
                conf,
                setup["entry1"],
                setup["entry2"],
                setup["sl"],
                setup["targets"],
                source_desc="AI Transition Resolution Model",
                extra_info_lines=[
                    f"Regime: TRANSITION seit {minutes_in:.0f} min",
                    f"Auflösung: {'TREND_UP' if direction == 'LONG' else 'TREND_DOWN'}",
                ],
            )
            log_prediction(conn, ARTIFACT["tag"], TRADE_SYMBOL, direction, live_price, conf, posted=True)
            update_cooldown(conn, MODEL_ID, TRADE_SYMBOL, direction)  # committet atomar
        else:
            if prob >= ARTIFACT["threshold"] and not LIVE_POSTING:
                logger.info(f"👻 SHADOW-Post {direction} (p={prob:.2f}) — Live-Posting deaktiviert.")
            log_prediction(conn, ARTIFACT["tag"], TRADE_SYMBOL, direction, live_price, conf, posted=False)
            conn.commit()
    except Exception as e:
        logger.error(f"TRM1-Check-Fehler: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def main() -> None:
    global LIVE_POSTING
    logger.info("=== 🧭 AI TRM1 BOT (Transition-Resolution, S10) GESTARTET ===")
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
    conn.commit()
    conn.close()

    while not startup_feature_selfcheck():
        time.sleep(600)  # regime_history füllt sich alle 5 min von selbst

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        # +4 min zum 5-min-Raster des Regime-Detectors — dessen Check ist dann
        # sicher geschrieben, bevor wir lesen.
        if now.minute % 5 == 4:
            ensure_artifact()
            if ARTIFACT["loaded"]:
                run_check()
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

# 32_ai_trm1_bot.py — TRM1 "transition resolution model" (Report 15, S10).
"""
Runs ONLY in the TRANSITION regime (44.5% of the time — E8) and predicts the
resolution direction (TREND_UP / TREND_DOWN / no tradeable resolution)
from the regime_history raw features. If the model predicts a trend
resolution above the val threshold, the bot posts a BTCUSDT signal in that
direction (BULL→LONG, BEAR→SHORT) — measurable via ai_signals like any other bot
(operator decision 2026-07-06).

Class contract (core/research_features): 0 = OTHER (CHOP/HIGH_VOLA/no
resolution), 1 = TREND_UP, 2 = TREND_DOWN. Trainer: tools/trm1_build_dataset.py
+ tools/new_models_train.py --strategy trm1.

Runs every 5 minutes (grid of 26_regime_detector, offset by +4 min).
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
from core.live_price import get_live_price
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
TARGET_CHANNEL_ID = _kcfg.CH_TRM1  # per-bot override, fallback CH_NEW_IDEAS
LIVE_POSTING = os.getenv("NEW_IDEAS_LIVE_POSTING", "1") == "1"
SHADOW_FLOOR = 0.25
TRADE_SYMBOL = "BTCUSDT"
COOLDOWN_HOURS = 12  # at most one signal per direction/episode time window
ARTIFACT_RETRY_S = 1800

ARTIFACT = load_artifact(ARTIFACT_PATH, TRM1_FEATURES, MODEL_ID)


def ensure_artifact() -> None:
    global ARTIFACT
    if ARTIFACT["loaded"]:
        ARTIFACT = maybe_reload(ARTIFACT, TRM1_FEATURES)
    elif time.time() - ARTIFACT["loaded_at"] > ARTIFACT_RETRY_S:
        ARTIFACT = load_artifact(ARTIFACT_PATH, TRM1_FEATURES, MODEL_ID)


def fetch_regime_state(conn) -> tuple[str, float] | None:
    """Debounced regime + minutes since regime start from regime_current."""
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
    """Latest checks from regime_history, chronological ASC (ts = naive UTC)."""
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
    """P0.12 pattern: run the features over real regime_history windows.

    Returns False when there's (still) too little regime_history — the caller
    then waits instead of crashing (a fresh setup fills itself every 5 min; an
    exit(1) would create a ~2h watchdog restart loop, review fix
    2026-07-06). Broken features remain a hard abort."""
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
            logger.warning("Self-test: too few regime_history rows — is 26_regime_detector running? Waiting.")
            return False
        rows = []
        for end in range(TRM1_WINDOW_CHECKS, len(hist), 5):
            window = hist[end - TRM1_WINDOW_CHECKS : end]
            rows.append(build_trm1_row(window, minutes_in_transition=float(end)))
        # Fraction/confidence features are allowed to be constant over calm phases.
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
        logger.info(f"✅ Feature self-test passed ({len(rows)} windows).")
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
            logger.warning("regime_current empty — is 26_regime_detector running?")
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
            raise ValueError(f"Feature contract violated — missing: {missing}")
        X = pd.DataFrame([{c: feature_row[c] for c in ARTIFACT["features"]}], dtype=float)
        X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

        proba = ARTIFACT["model"].predict_proba(X)[0]
        p_up, p_down = float(proba[TRM1_CLASS_UP]), float(proba[TRM1_CLASS_DOWN])
        direction = "LONG" if p_up >= p_down else "SHORT"
        prob = max(p_up, p_down)
        if prob < SHADOW_FLOOR:
            return
        conf = calibrated_confidence(ARTIFACT, prob)

        # Since T-2026-KYT-9050-011 the context frame no longer feeds the
        # entry price; it stays the data-freshness guard TRM1 previously
        # stayed silent behind: enough 1h history for BTCUSDT AND a join that's
        # no staler than CONTEXT_MAX_STALENESS_H (ingestion is alive). TRM1 features
        # come from regime_history, not from this frame.
        if fetch_context_frame(conn, TRADE_SYMBOL) is None:
            return
        # Entry anchor = LIVE price (core.live_price, core.candles contract 2:
        # detection on closed candles, price separate). The frame has carried
        # only closed candles since block 5 (T-2026-CU-9050-112) — its
        # last close would be up to ~59 min old.
        live_price = get_live_price(TRADE_SYMBOL, conn)
        if live_price is None:
            # Without a price anchor, no signal and no prediction log; TRM1 checks
            # again every 5 min anyway. No cooldown — that's only set here on
            # the post path (unchanged).
            logger.warning(f"{TRADE_SYMBOL}: no live price (Binance + DB fallback) — check skipped.")
            return

        logger.info(
            f"TRM1 TRANSITION for {minutes_in:.0f} min | P(up)={p_up:.3f} "
            f"P(down)={p_down:.3f} (gate {ARTIFACT['threshold']:.2f})"
        )

        if check_cooldown(conn, MODEL_ID, TRADE_SYMBOL, direction, COOLDOWN_HOURS):
            return
        if has_open_ai_signal(conn, TRADE_SYMBOL, direction, ARTIFACT["tag"]):
            return
        # No self-hedge (review fix 2026-07-06): if the forecast flips on the
        # 5-min cadence while the counter-trade is still open, TRM1 would otherwise
        # post simultaneous counter-positions on BTCUSDT — then only shadow.
        opposite = "SHORT" if direction == "LONG" else "LONG"
        allow_post = not has_open_ai_signal(conn, TRADE_SYMBOL, opposite, ARTIFACT["tag"])
        if not allow_post and prob >= ARTIFACT["threshold"]:
            logger.info(f"⛔ Counter-position ({opposite}) open — {direction} signal shadow-only.")

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
                    f"Regime: TRANSITION for {minutes_in:.0f} min",
                    f"Resolution: {'TREND_UP' if direction == 'LONG' else 'TREND_DOWN'}",
                ],
            )
            log_prediction(conn, ARTIFACT["tag"], TRADE_SYMBOL, direction, live_price, conf, posted=True)
            update_cooldown(conn, MODEL_ID, TRADE_SYMBOL, direction)  # commits atomically
        else:
            if prob >= ARTIFACT["threshold"] and not LIVE_POSTING:
                logger.info(f"👻 SHADOW post {direction} (p={prob:.2f}) — live posting disabled.")
            log_prediction(conn, ARTIFACT["tag"], TRADE_SYMBOL, direction, live_price, conf, posted=False)
            conn.commit()
    except Exception as e:
        logger.error(f"TRM1 check error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def main() -> None:
    global LIVE_POSTING
    logger.info("=== 🧭 AI TRM1 BOT (Transition Resolution, S10) STARTED ===")
    if TARGET_CHANNEL_ID == 0:
        logger.warning("Neither CH_TRM1 nor CH_NEW_IDEAS set — forcing shadow-only mode.")
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
        time.sleep(600)  # regime_history fills itself every 5 min

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        # +4 min offset from the regime detector's 5-min grid — its check is
        # then guaranteed to be written before we read.
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
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")

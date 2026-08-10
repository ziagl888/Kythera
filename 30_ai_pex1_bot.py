# 30_ai_pex1_bot.py — PEX1 "pump-exhaustion short" (report 15, S6).
"""
Short-only ML bot on pump exhaustion: consumes events from
10_pump_dump_detector (pump_dump_events, volume_ratio >= 5 — gate live as mirrored
in training, report 13 EPD1-P0) and shorts into exhaustion when the binary model
(tools/pex1_build_dataset.py + tools/new_models_train.py --strategy pex1) sees
TP1-before-SL above the val threshold.

Features:
  * SHORT ONLY — the EPD1 direction asymmetry (SHORT 76.5% vs LONG 50.2% WR)
    is the evidence basis; a long side deliberately does not exist.
  * Geometry: calculate_smart_targets (SR-based) — exact label geometry.
  * Posting to CH_NEW_IDEAS; NEW_IDEAS_LIVE_POSTING=0 switches to shadow-only
    (ml_predictions_master, posted=false).
  * Without artifact (pex1_model.pkl) the bot runs in idle mode — code can be
    deployed before VPS training.

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
from core.live_price import get_live_price
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
TARGET_CHANNEL_ID = _kcfg.CH_PEX1  # per-bot override, fallback CH_NEW_IDEAS
LIVE_POSTING = os.getenv("NEW_IDEAS_LIVE_POSTING", "1") == "1"
SHADOW_FLOOR = 0.25  # below that do not even log shadow (MIS convention)
COOLDOWN_HOURS = 4  # per coin — mirrored in training dedup
ARTIFACT_RETRY_S = 1800  # idle mode: check for fresh deploy every 30 min

ARTIFACT = load_artifact(ARTIFACT_PATH, PEX1_FEATURES, MODEL_ID)


def ensure_artifact() -> None:
    global ARTIFACT
    if ARTIFACT["loaded"]:
        ARTIFACT = maybe_reload(ARTIFACT, PEX1_FEATURES)
    elif time.time() - ARTIFACT["loaded_at"] > ARTIFACT_RETRY_S:
        ARTIFACT = load_artifact(ARTIFACT_PATH, PEX1_FEATURES, MODEL_ID)


def spike_time_to_utc_naive(value, offset_h: int):
    """A spike_time value → naive UTC, aware as well as naive.

    T-2026-KYT-9050-061: the live column is `timestamp WITH time zone` (the
    repo DDL in 10_pump_dump_detector.py says `TIMESTAMP`, the table was
    aged at some point — read-only measured 2026-08-01). psycopg2 thus delivers
    AWARE datetimes for it, and any subtraction against a naive `now` threw
    `can't subtract offset-naive and offset-aware datetimes`.

    Aware ⇒ true instant, convert to UTC and strip tzinfo; the measured
    offset is then per definition irrelevant. Naive ⇒ legacy dump or old
    column, subtract offset (domain comes from detect_spike_time_offset_h).
    Both yield the same domain as `now` and as `fetch_context_frame`."""
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is not None:
        return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value - datetime.timedelta(hours=offset_h)


def detect_spike_time_offset_h(conn) -> int:
    """Domain offset of the `pump_dump_events.spike_time` column in hours.

    Historically the column was TIMESTAMP without TZ, and depending on the
    detector's session TZ, it held local time instead of UTC. The offset is
    measured against wall-clock time (events run on 538 coins quasi continuously)
    so spike_age is correct. Watermark comparisons remain in raw domain.

    If the column is `timestamptz` (live state since aging), the driver delivers
    a true instant — then there is no domain question and the offset is 0.
    Previously this function subtracted a naive `now` from exactly this aware
    value and tore apart EVERY scan cycle (T-2026-KYT-9050-061: ~1x/min since
    at least 2026-07-19, 8166 failures in the four most recent logs/watchdog_debug_*,
    not a single successful scan)."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(spike_time) FROM pump_dump_events")
        row = cur.fetchone()
    if not row or row[0] is None:
        return 0
    if getattr(row[0], "tzinfo", None) is not None:
        return 0
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    diff_h = (row[0] - now).total_seconds() / 3600.0
    return int(np.clip(round(diff_h), -12, 12))


def startup_feature_selfcheck() -> None:
    """P0.12 pattern: run context pipeline on real data from 3 coins and
    hard-stop if a continuous feature is constant."""
    try:
        with open("coins.json") as f:
            coins = json.load(f)
    except Exception as e:
        logger.critical(f"Self-test: coins.json not loadable: {e}")
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
        # Event features are by construction constant in the self-test (dummy).
        assert_features_alive(
            rows,
            PEX1_FEATURES,
            binary_ok={"ev_volume_ratio", "ev_price_change_60s", "ev_buy_pressure", "ev_volatility"},
            context=" (PEX1 startup)",
        )
        logger.info(f"✅ Feature self-test passed ({len(rows)} rows, {used} coins).")
    except ValueError as e:
        logger.critical(f"❌ {e}")
        exit(1)
    finally:
        conn.close()


def fetch_new_events(conn, watermark) -> list[dict]:
    """New pump events via the live gates (mirror of training sampling)."""
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
    # Same normalisation as in the offset detector — the aware/naive mix would have
    # thrown here too just as it did there (T-2026-KYT-9050-061).
    spike_utc = spike_time_to_utc_naive(event["spike_time"], offset_h)
    if spike_utc is None:
        return
    spike_age_min = max(0.0, (now - spike_utc).total_seconds() / 60.0)
    if spike_age_min > 30.0:
        return  # stale event (bot downtime/catch-up) — exhaustion thesis expires

    if check_cooldown(conn, MODEL_ID, symbol, direction, COOLDOWN_HOURS):
        return
    if has_open_ai_signal(conn, symbol, direction, ARTIFACT["tag"]):
        return

    # Feature candle relative to event time (floor-1 as in training) — an event
    # processed across an hour boundary would otherwise see a later candle, for
    # PEX1 that would be the pump candle itself (review fix 2026-07-06).
    res = fetch_context_frame(conn, symbol, as_of=spike_utc)
    if res is None:
        return
    df, idx = res

    feature_row = build_pex1_row(event, df, idx)
    missing = [c for c in ARTIFACT["features"] if c not in feature_row]
    if missing:
        raise ValueError(f"Feature contract violated — missing: {missing}")
    X = pd.DataFrame([{c: feature_row[c] for c in ARTIFACT["features"]}], dtype=float)
    X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

    prob = float(ARTIFACT["model"].predict_proba(X)[0, 1])
    conf = calibrated_confidence(ARTIFACT, prob)

    logger.info(
        f"PEX1 Pump-Event {symbol} | vol_ratio {float(event['volume_ratio']):.1f} | "
        f"Prob {prob:.3f} (Gate {ARTIFACT['threshold']:.2f})"
    )

    # Entry anchor = LIVE price (core.live_price, core.candles contract 2:
    # detection on closed candles, price separately). `df` carries since
    # block 5 (T-2026-CU-9050-112) ONLY closed candles — `df["close"].iloc[-1]`
    # would be the last CLOSED 1h candle and thus up to ~59 min old
    # (T-2026-KYT-9050-011). The feature candle (idx, floor-1 join) is
    # unaffected: live price feeds exclusively to entry/geometry.
    live_price = get_live_price(symbol, conn)
    if live_price is None:
        # Neither Binance REST nor DB fallback deliver a price: no signal
        # and no prediction log (a shadow row without entry price is worthless
        # for analysis). The cooldown runs anyway — the event WAS scored, and
        # the unconditional 4h dedup of training hangs on scoring, not posting.
        logger.warning(f"{symbol}: no live price (Binance + DB fallback) — signal skipped.")
        update_cooldown(conn, MODEL_ID, symbol, direction)
        return

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
            logger.info(f"👻 SHADOW post {symbol} (p={prob:.2f}) — live posting disabled.")
        if prob >= SHADOW_FLOOR:
            log_prediction(conn, ARTIFACT["tag"], symbol, direction, live_price, conf, posted=False)
    # Cooldown on EVERY scored event — mirror of the unconditional 4h dedup in
    # training; only this way does the model see the same event distribution live
    # (review fix 2026-07-06). update_cooldown commits the transaction atomically.
    update_cooldown(conn, MODEL_ID, symbol, direction)


# Loop state. Module-level since T-2026-KYT-9050-135, when the loop itself moved
# into 46_signal_consumer_runner.py: this was a local of main(), and a poll core
# the runner calls cannot keep it there. Scope is unchanged — one instance per
# bot module, invisible to the other four consumers in that process. It is the
# anti-replay guard of this bot: startup() seeds it from MAX(spike_time), so a
# poll before a successful startup would replay every old pump. The runner
# therefore never polls an unarmed bot.
watermark: datetime.datetime | None = None


def startup() -> None:
    """One-time init: DDL/index, watermark seed, feature self-check.

    Everything main() did before entering its loop (T-2026-KYT-9050-135).
    """
    global LIVE_POSTING, watermark
    logger.info("=== 💥 AI PEX1 BOT (pump exhaustion short, S6) STARTED ===")
    if TARGET_CHANNEL_ID == 0:
        logger.warning("Neither CH_PEX1 nor CH_NEW_IDEAS set — forcing shadow-only mode.")
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
        # Poll path runs every minute on spike_time — without an index that would be a
        # seq scan per cycle (table stays small thanks to P1.40 retention, but
        # the index makes the watermark scan constantly cheap).
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pde_spike_time ON pump_dump_events (spike_time)")
        cur.execute("SELECT MAX(spike_time) FROM pump_dump_events")
        row = cur.fetchone()
    conn.commit()
    # Only process events AFTER startup (no replay of old pumps on boot).
    # Sentinel AWARE (T-2026-KYT-9050-061): `pump_dump_events.spike_time` is live
    # `timestamptz`, so MAX() delivers aware — a naive sentinel would at empty
    # table on first `max(watermark, event[...])` run against an aware value
    # and throw the same offset-naive/aware TypeError. The SQL comparison
    # remains in the raw domain of the column (cast done by Postgres).
    watermark = row[0] if row and row[0] else datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    conn.close()

    startup_feature_selfcheck()


def run_poll() -> None:
    """One poll cycle — the body of main()'s loop, unchanged.

    The idle-mode branch is the one statement that had to change shape: it used
    to be ``time.sleep(60); continue`` inside the loop, i.e. "skip this cycle".
    Returning IS that, now that the caller owns the sleep.
    """
    global watermark
    ensure_artifact()
    if not ARTIFACT["loaded"]:
        return

    conn = get_db_connection()
    try:
        offset_h = detect_spike_time_offset_h(conn)
        events = fetch_new_events(conn, watermark)
        conn_dead = False
        for event in events:
            # fetch_new_events delivers ORDER BY spike_time ASC and filters
            # strictly `> watermark` — the assignment is thus monotonic. A
            # max() over both values would be the second place where an
            # aware sentinel and a naive legacy column could meet
            # (T-2026-KYT-9050-061); not needed here.
            watermark = event["spike_time"]
            try:
                process_event(conn, event, offset_h)
            except Exception as e:
                logger.error(f"Error for {event.get('symbol')}: {e}")
            finally:
                # P2.32 pattern: close transaction per event ALWAYS —
                # after a commit path the rollback is a no-op.
                try:
                    conn.rollback()
                except Exception:
                    logger.error("Rollback failed (dead connection) — cycle abort.")
                    conn_dead = True
            if conn_dead:
                break
    except Exception as e:
        logger.error(f"PEX1 scan error: {e}")
    finally:
        conn.close()


def main() -> None:
    """Standalone entry. In the fleet 46_signal_consumer_runner.py drives the
    two functions above instead; the file stays runnable for debugging."""
    startup()
    while True:
        run_poll()
        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")

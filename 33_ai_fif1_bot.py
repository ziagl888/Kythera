# 33_ai_fif1_bot.py — FIF1 "FIFO filter model" (Report 15, S11).
"""
Meta-classifier over the Fast-In-And-Out signal stream: the bot reads NEW
FIFO signals from active_trades_master (the live FIFO path in 3_detectors.py
stays untouched — clean A/B comparison), uses the model to estimate the
win probability from entry-time features (regime, direction,
market context, signal burst density) and posts only the top signals under the
tag FIF1 to CH_NEW_IDEAS — with the ORIGINAL geometry of the FIFO signal
(entry/TP1/SL unchanged), so the selection is the only difference.

Evidence (Report 15 E6): FIFO has 111k labelled trades, median +1.25%,
avg −0.13% — the problem is selection, not tails. Trainer:
tools/fif1_build_dataset.py + tools/new_models_train.py --strategy fif1.

Non-posted candidates are also written as shadow rows
(ml_predictions_master, posted=false) — that is the A/B basis.

Watchdog: start_delay=215.
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
from core import shadow_gate
from core.database import get_db_connection
from core.model_artifacts import calibrated_confidence, load_artifact, maybe_reload
from core.research_features import (
    CONTEXT_MIN_CANDLES,
    FIF1_FEATURES,
    REGIME_FEATURES,
    assert_features_alive,
    build_fif1_row,
    fetch_context_frame,
)
from core.signal_post import has_open_ai_signal, log_prediction, post_ai_signal_gated

logging.basicConfig(level=logging.INFO, format='%(asctime)s - FIF1_BOT - %(message)s')
logger = logging.getLogger(__name__)

MODEL_ID = "FIF1"
ARTIFACT_PATH = "fif1_model.pkl"
TARGET_CHANNEL_ID = _kcfg.CH_FIF1  # per-bot override, fallback CH_NEW_IDEAS
LIVE_POSTING = os.getenv("NEW_IDEAS_LIVE_POSTING", "1") == "1"
SIGNAL_MAX_AGE_MIN = 10  # never process signals older than this (idle catch-up guard)
SOURCE_STRATEGY = "Fast In And Out"
ARTIFACT_RETRY_S = 1800

ARTIFACT = load_artifact(ARTIFACT_PATH, FIF1_FEATURES, MODEL_ID)


def ensure_artifact() -> None:
    global ARTIFACT
    if ARTIFACT["loaded"]:
        ARTIFACT = maybe_reload(ARTIFACT, FIF1_FEATURES)
    elif time.time() - ARTIFACT["loaded_at"] > ARTIFACT_RETRY_S:
        ARTIFACT = load_artifact(ARTIFACT_PATH, FIF1_FEATURES, MODEL_ID)


def fetch_latest_regime(conn) -> tuple[dict | None, float]:
    """Latest regime_history row + age in minutes (ts = naive UTC)."""
    with conn.cursor() as cur:
        cur.execute("SELECT ts, regime, confidence FROM regime_history ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
    if row is None:
        return None, 360.0
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    ts = row[0]
    if ts.tzinfo is not None:
        ts = ts.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    age_min = max(0.0, (now - ts).total_seconds() / 60.0)
    return {"regime": row[1], "confidence": row[2]}, age_min


def fifo_burst_counts(conn, symbol: str, direction: str) -> tuple[int, int]:
    """Signal burst density from BOTH master tables (trades move from
    active to closed). Time comparison DB-side — the time columns have carried
    naive UTC since the R3 flip (3_detectors.write_signal_atomic), NOW() casts under
    the UTC session into the same domain. The consistency hinges on exactly this
    pair: writer and session TZ had to be switched together
    (T-2026-KYT-9050-005, docs/UTC_POLICY.md §4).

    Restart effect: the 1h/24h windows see the rows from BEFORE the restart as
    +2/3h in the future, so the burst density is too high for up to 24 h;
    after that the window runs empty."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM active_trades_master
                WHERE strategy = %(s)s AND coin = %(c)s AND direction = %(d)s
                  AND time > NOW() - INTERVAL '24 hours')
            + (SELECT COUNT(*) FROM closed_trades_master
                WHERE strategy = %(s)s AND coin = %(c)s AND direction = %(d)s
                  AND time > NOW() - INTERVAL '24 hours'),
              (SELECT COUNT(*) FROM active_trades_master
                WHERE strategy = %(s)s AND time > NOW() - INTERVAL '1 hour')
            + (SELECT COUNT(*) FROM closed_trades_master
                WHERE strategy = %(s)s AND time > NOW() - INTERVAL '1 hour')
            """,
            {"s": SOURCE_STRATEGY, "c": symbol, "d": direction},
        )
        row = cur.fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def startup_feature_selfcheck() -> None:
    """P0.12 pattern: run the feature pipeline on real data from 3 coins."""
    import json

    try:
        with open("coins.json") as f:
            coins = json.load(f)
    except Exception as e:
        logger.critical(f"Self-test: coins.json not loadable: {e}")
        exit(1)

    conn = get_db_connection()
    try:
        regime_row, regime_age = fetch_latest_regime(conn)
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        rows, used = [], 0
        for symbol in coins[:15]:
            res = fetch_context_frame(conn, symbol)
            if res is None:
                continue
            df, idx = res
            for back in range(0, 8):
                if idx - back >= CONTEXT_MIN_CANDLES - 1:
                    rows.append(
                        build_fif1_row(
                            "LONG",
                            df,
                            idx - back,
                            regime_row,
                            regime_age,
                            fifo_same_dir_24h=back,
                            fifo_fleet_1h=back,
                            ts=now - datetime.timedelta(hours=back),
                        )
                    )
            used += 1
            if used >= 3:
                break
        assert_features_alive(
            rows,
            FIF1_FEATURES,
            binary_ok={"side_short", *REGIME_FEATURES},
            context=" (FIF1-Startup)",
        )
        logger.info(f"✅ Feature self-test passed ({len(rows)} rows, {used} coins).")
    except ValueError as e:
        logger.critical(f"❌ {e}")
        exit(1)
    finally:
        conn.close()


def signal_key(sig: dict) -> tuple:
    """Table-agnostic dedupe key: a signal moves from the monitor within
    seconds from active_ to closed_trades_master (with a NEW serial id) — the
    id therefore doesn't work as a union watermark."""
    return (
        str(sig["coin"]).upper(),
        str(sig["direction"]).upper(),
        str(sig["time"]),
        float(sig["entry"] or 0),
    )


def fetch_recent_signals(conn) -> list[dict]:
    """FIFO signals from the last SIGNAL_MAX_AGE_MIN minutes from BOTH
    master tables (review fixes 2026-07-06):
      * UNION with closed: fast resolvers (SL/TP < 60s after insert) would
        otherwise disappear from active before the next poll — exactly the
        losers the filter is meant to learn would be systematically missing live.
      * Time window instead of id watermark: after idle/outage phases no
        backlog of day-old signals with expired original geometry is posted
        (analogous to the 30-min guard in Bot 30). Time comparison DB-side — the
        time columns have carried naive UTC since the R3 flip, NOW() casts under the
        UTC session into the same domain (T-2026-KYT-9050-005). Rows from before the
        restart appear to be +2/3h in the future; the window has no
        upper bound, so they are all present at the first poll after the restart —
        and are marked exactly there as `seen` by the startup marking in main(),
        before the loop posts. After ~3h they fall out the bottom."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, time, coin, direction, entry, target1, sl
            FROM active_trades_master
            WHERE strategy = %(s)s AND time > NOW() - INTERVAL '{SIGNAL_MAX_AGE_MIN} minutes'
            UNION ALL
            SELECT id, time, coin, direction, entry, target1, sl
            FROM closed_trades_master
            WHERE strategy = %(s)s AND time > NOW() - INTERVAL '{SIGNAL_MAX_AGE_MIN} minutes'
            ORDER BY time ASC
            """,
            {"s": SOURCE_STRATEGY},
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def process_signal(conn, sig: dict) -> None:
    symbol = str(sig["coin"]).upper()
    direction = str(sig["direction"]).upper()
    entry = float(sig["entry"] or 0)
    target1 = float(sig["target1"] or 0)
    sl = float(sig["sl"] or 0)
    if entry <= 0 or target1 <= 0 or sl <= 0 or direction not in ("LONG", "SHORT"):
        return

    res = fetch_context_frame(conn, symbol)
    if res is None:
        return
    df, idx = res
    regime_row, regime_age = fetch_latest_regime(conn)
    same_dir_24h, fleet_1h = fifo_burst_counts(conn, symbol, direction)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    # same_dir contains the current signal itself (already in active_trades) →
    # −1, so the feature counts "other signals before it" as in training.
    feature_row = build_fif1_row(
        direction,
        df,
        idx,
        regime_row,
        regime_age,
        fifo_same_dir_24h=max(0, same_dir_24h - 1),
        fifo_fleet_1h=max(0, fleet_1h - 1),
        ts=now,
    )
    missing = [c for c in ARTIFACT["features"] if c not in feature_row]
    if missing:
        raise ValueError(f"Feature contract violated — missing: {missing}")
    X = pd.DataFrame([{c: feature_row[c] for c in ARTIFACT["features"]}], dtype=float)
    X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

    prob = float(ARTIFACT["model"].predict_proba(X)[0, 1])
    conf = calibrated_confidence(ARTIFACT, prob)

    logger.info(
        f"FIF1 candidate {symbol} {direction} (FIFO #{sig['id']}) | prob {prob:.3f} (gate {ARTIFACT['threshold']:.2f})"
    )

    # FIF1 was superseded by TSM1 (T-2026-CU-9050-183) → SILENT parked the live post
    # without setting CH_FIF1=0. T-2026-KYT-9050-033 (audit T-032) revived FIF1 as SHADOW:
    # the bot used to have only a LIVE-or-nothing branch, now it routes through
    # post_ai_signal_gated (pattern like Bot 9/10/12) — the shadow_gate lifecycle
    # decides LIVE (Cornix to CH_FIF1) vs. SHADOW (monitored, no Cornix) vs.
    # SILENT/RETIRED (no-op). LIVE additionally needs the NEW_IDEAS master switch;
    # SHADOW doesn't (no Cornix anyway). ORIGINAL FIFO geometry stays (selection
    # is the only difference from the source signal).
    tag = ARTIFACT["tag"]
    st = shadow_gate.leg_status(tag, direction)
    fire = prob >= ARTIFACT["threshold"] and not has_open_ai_signal(conn, symbol, direction, tag)
    if fire and ((st == shadow_gate.LIVE and LIVE_POSTING) or st == shadow_gate.SHADOW):
        outcome = post_ai_signal_gated(
            conn,
            tag,
            direction,
            TARGET_CHANNEL_ID,
            symbol,
            conf,
            entry,
            entry,
            sl,
            [target1],
            "AI FIFO Filter Model",
            n_show=1,
            dedup_hours=0,
            extra_info_lines=[f"Source: Fast In And Out #{sig['id']}"],
        )
        if outcome == "live":
            log_prediction(conn, tag, symbol, direction, entry, conf, posted=True, dedup_hours=0)
        elif outcome is None:
            # SILENT/RETIRED or shadow dedup: keep logging for A/B completeness.
            log_prediction(conn, tag, symbol, direction, entry, conf, posted=False, dedup_hours=0)
        # outcome == "shadow": post_shadow_ai_signal already logged the prediction.
    else:
        if prob >= ARTIFACT["threshold"] and st == shadow_gate.LIVE and not LIVE_POSTING:
            logger.info(f"👻 SHADOW post {symbol} {direction} (p={prob:.2f}) — live posting disabled.")
        # dedup_hours=0: EVERY FIFO candidate is logged (A/B completeness) —
        # dedup is handled by the seen set of the time-window polling.
        log_prediction(conn, tag, symbol, direction, entry, conf, posted=False, dedup_hours=0)
    conn.commit()


# Loop state. Module-level since T-2026-KYT-9050-135, when the loop itself moved
# into 46_signal_consumer_runner.py: this was a local of main(), and a poll core
# the runner calls cannot keep it there. Scope is unchanged — one instance per
# bot module, invisible to the other four consumers in that process. It is the
# anti-replay guard of this bot: startup() marks everything currently in the
# window as seen, so a poll before a successful startup would post the whole
# window a second time. The runner therefore never polls an unarmed bot.
seen: dict[tuple, datetime.datetime] = {}


def startup() -> None:
    """One-time init: indices, feature self-check, seen-set of the live window.

    Everything main() did before entering its loop (T-2026-KYT-9050-135).
    """
    global LIVE_POSTING, seen
    logger.info("=== 🎛️ AI FIF1 BOT (FIFO filter, S11) STARTED ===")
    if TARGET_CHANNEL_ID == 0:
        logger.warning("Neither CH_FIF1 nor CH_NEW_IDEAS set — forcing shadow-only mode.")
        LIVE_POSTING = False
    logger.info(f"Posting: {'LIVE' if LIVE_POSTING else 'SHADOW-ONLY'}")

    startup_feature_selfcheck()

    conn = get_db_connection()
    with conn.cursor() as cur:
        # The time-window polling scans both master tables every minute —
        # without a (strategy, time) index these would be seq scans over the
        # fleet's largest trade table (closed: 111k+ FIFO rows). The same index
        # also carries fifo_burst_counts.
        cur.execute("CREATE INDEX IF NOT EXISTS idx_atm_strategy_time ON active_trades_master (strategy, time)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ctm_strategy_time ON closed_trades_master (strategy, time)")
    conn.commit()
    # Don't retroactively process signals that occurred BEFORE the bot start:
    # mark the current window as seen (also prevents double posts
    # after a quick bot restart within the window).
    seen = {}
    now0 = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    for sig in fetch_recent_signals(conn):
        seen[signal_key(sig)] = now0
    conn.rollback()
    conn.close()
    logger.info(f"Start: {len(seen)} signals in the window marked as seen.")


def run_poll() -> None:
    """One poll cycle — the body of main()'s loop, unchanged.

    The idle-mode branch is the one statement that had to change shape: it used
    to be ``time.sleep(60); continue`` inside the loop, i.e. "skip this cycle".
    Returning IS that, now that the caller owns the sleep.
    """
    global seen
    ensure_artifact()
    if not ARTIFACT["loaded"]:
        return

    conn = get_db_connection()
    try:
        signals = fetch_recent_signals(conn)
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        conn_dead = False
        for sig in signals:
            key = signal_key(sig)
            if key in seen:
                continue
            seen[key] = now  # process-once, no retry loop even on error
            try:
                process_signal(conn, sig)
            except Exception as e:
                logger.error(f"Error for {sig.get('coin')} (FIFO #{sig.get('id')}): {e}")
            finally:
                try:
                    conn.rollback()  # P2.32 pattern; a no-op after commit
                except Exception:
                    logger.error("Rollback failed (dead connection) — aborting cycle.")
                    conn_dead = True
            if conn_dead:
                break
        # Trim the seen set (window + margin — stays constantly small)
        cutoff = now - datetime.timedelta(minutes=SIGNAL_MAX_AGE_MIN * 3)
        seen = {k: v for k, v in seen.items() if v >= cutoff}
    except Exception as e:
        logger.error(f"FIF1 poll error: {e}")
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

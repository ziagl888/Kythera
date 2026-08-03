# 31_ai_fmr1_bot.py — FMR1 "Funding-Extreme Mean-Reversion" (Report 15, S8).
"""
Cross-sectional funding bot: coins in the top funding percentile (overheated
longs paying) are SHORT candidates, coins in the bottom percentile LONG —
classic carry/crowding unwind edge, orthogonal to the rest of the fleet.
A binary model (tools/fmr1_build_dataset.py + tools/new_models_train.py
--strategy fmr1) gates candidates on TP1-before-SL.

Data paths:
  * LIVE: current rates cross-sectional from ONE REST call
    (GET /fapi/v1/premiumIndex, lastFundingRate); settlement history per
    candidate from GET /fapi/v1/fundingRate — the bot is thus independent of
    funding_rates table backfill state.
  * TRAINING: funding_rates table (tools/backfill_funding_rates.py) — same
    source (Binance settlements), same statistics features (core/research_features).
    Known, documented REST skew: live gates the *running* rate, in
    training the *settled* — details docs/NEW_IDEAS_BOTS.md.

Runs hourly (minute 19). Watchdog: start_delay=199.
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
import requests

from core import config as _kcfg
from core.database import get_db_connection
from core.live_price import get_live_price
from core.market_utils import check_cooldown, update_cooldown
from core.model_artifacts import calibrated_confidence, load_artifact, maybe_reload
from core.research_features import (
    CONTEXT_MIN_CANDLES,
    FMR1_FEATURES,
    FMR1_LONG_PCTL,
    FMR1_SHORT_PCTL,
    assert_features_alive,
    build_fmr1_row,
    fetch_context_frame,
    funding_stats,
)
from core.shadow_gate import (
    SHADOW,
    artifact_threshold,
    leg_status,
    load_shadow_artifact,
    score_artifact,
    shadow_posting_enabled,
)
from core.signal_post import has_open_ai_signal, log_prediction, post_ai_signal, post_shadow_ai_signal
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - FMR1_BOT - %(message)s')
logger = logging.getLogger(__name__)

MODEL_ID = "FMR1"
ARTIFACT_PATH = "fmr1_model.pkl"
TARGET_CHANNEL_ID = _kcfg.CH_FMR1  # per-Bot-Override, Fallback CH_NEW_IDEAS
LIVE_POSTING = os.getenv("NEW_IDEAS_LIVE_POSTING", "1") == "1"
SHADOW_FLOOR = 0.25
COOLDOWN_HOURS = 24  # Funding trades are slow (hold until normalisation)
SCAN_MINUTE = 19  # own minute (2/3/10/11/13 etc. are occupied)
ARTIFACT_RETRY_S = 1800
MAX_CANDIDATES_PER_SIDE = 40  # safety cap (5% of ~540 coins ≈ 27)

PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
FUNDING_HISTORY_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

ARTIFACT = load_artifact(ARTIFACT_PATH, FMR1_FEATURES, MODEL_ID)


def ensure_artifact() -> None:
    global ARTIFACT
    if ARTIFACT["loaded"]:
        ARTIFACT = maybe_reload(ARTIFACT, FMR1_FEATURES)
    elif time.time() - ARTIFACT["loaded_at"] > ARTIFACT_RETRY_S:
        ARTIFACT = load_artifact(ARTIFACT_PATH, FMR1_FEATURES, MODEL_ID)


def load_coin_set() -> set[str]:
    with open("coins.json") as f:
        data = json.load(f)
    coins = data.get("coins", data) if isinstance(data, dict) else data
    return {c.upper() for c in coins if c.upper().endswith("USDT")}


def fetch_cross_section(coin_set: set[str]) -> pd.DataFrame | None:
    """Current funding rates of all coins (one request) + percentile rank."""
    try:
        resp = requests.get(PREMIUM_INDEX_URL, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        logger.error(f"premiumIndex fetch failed: {e}")
        return None
    df = pd.DataFrame(
        [
            {"symbol": r["symbol"], "rate": float(r.get("lastFundingRate") or 0.0)}
            for r in rows
            if r.get("symbol") in coin_set
        ]
    )
    if len(df) < 50:
        logger.error(f"Cross-section too thin ({len(df)} coins) — scan skipped.")
        return None
    df["pctl"] = df["rate"].rank(pct=True)
    return df


def fetch_funding_history(symbol: str) -> list[float] | None:
    """Settlement history (ASC) for statistics features — REST so the
    live path doesn't hang on backfill cron."""
    try:
        resp = requests.get(FUNDING_HISTORY_URL, params={"symbol": symbol, "limit": 100}, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:
        logger.warning(f"{symbol}: fundingRate history not loadable: {e}")
        return None
    rates = [float(r["fundingRate"]) for r in sorted(rows, key=lambda r: r["fundingTime"])]
    return rates if len(rates) >= 10 else None


def startup_feature_selfcheck() -> None:
    """P0.12 pattern: context features on real data + REST reachability."""
    coin_set = load_coin_set()
    cs = fetch_cross_section(coin_set)
    if cs is None:
        # NO exit(1): a transient Binance/network failure at boot would
        # otherwise create a watchdog restart loop (review fix 2026-07-06).
        # The scan skips cleanly if fetch_cross_section()==None anyway.
        logger.warning("Self-test: funding cross-section not available — scan skips until Binance responds.")

    conn = get_db_connection()
    try:
        rows, used = [], 0
        dummy_stats = {
            "funding_rate_bps": 12.0,
            "funding_z_30d": 2.5,
            "funding_delta_8h_bps": 1.0,
            "funding_sum_3d_bps": 30.0,
        }
        for symbol in sorted(coin_set)[:15]:
            res = fetch_context_frame(conn, symbol)
            if res is None:
                continue
            df, idx = res
            for back in range(0, 8):
                if idx - back >= CONTEXT_MIN_CANDLES - 1:
                    rows.append(build_fmr1_row(dummy_stats, 0.97, "SHORT", df, idx - back))
            used += 1
            if used >= 3:
                break
        assert_features_alive(
            rows,
            FMR1_FEATURES,
            binary_ok={
                "funding_rate_bps",
                "funding_cs_pctl",
                "funding_z_30d",
                "funding_delta_8h_bps",
                "funding_sum_3d_bps",
                "side_short",
            },
            context=" (FMR1-startup)",
        )
        cs_note = f"{len(cs)} coins in cross-section" if cs is not None else "cross-section pending"
        logger.info(f"✅ Feature self-test passed ({len(rows)} rows, {used} coins, {cs_note}).")
    except ValueError as e:
        logger.critical(f"❌ {e}")
        exit(1)
    finally:
        conn.close()


# ── K4 / FMR2 Klasse-(A)-Shadow (T-2026-CU-9050-149) ─────────────────────────
# FMR2 = normalisation-exit retrain alongside live FMR1 bot. FMR2_FEATURES ==
# FMR1_FEATURES → the same `build_fmr1_row` feature row is additionally
# scored with the FMR2 model and written as a monitored-but-not-posted shadow trade
# under tag "FMR2". Purely additive to the non-posting branch; the FMR1 live
# path is never affected (own tag, own dedup, all encapsulated).
_FMR2_ART = None
_FMR2_LOADED = False


def _fmr2_artifact():
    """Loads the FMR2 shadow artefact ONCE (fail-soft; one model for both
    directions, side_short is a feature). If missing from staging_models/, the
    bot continues without FMR2 leg (hard rule 2)."""
    global _FMR2_ART, _FMR2_LOADED
    if not _FMR2_LOADED:
        _FMR2_ART = load_shadow_artifact("FMR2", "SHORT")  # direction-agnostic
        _FMR2_LOADED = True
        if _FMR2_ART is None:
            logger.info("FMR2 shadow artefact missing (staging_models/fmr2_model.pkl) — no FMR2 leg.")
    return _FMR2_ART


def _emit_fmr2_shadow(conn, symbol: str, direction: str, feature_row: dict, live_price: float) -> None:
    """Scores the SAME FMR1 feature row with the FMR2 model and emits per
    the shadow-emit rule (§3 SHADOW_MODE_POSTING). Fully encapsulated."""
    if not shadow_posting_enabled():
        return
    if leg_status("FMR2", direction) != SHADOW:
        return
    art = _fmr2_artifact()
    if art is None:
        return
    if has_open_ai_signal(conn, symbol, direction, "FMR2"):
        return
    prob = score_artifact(art, feature_row)
    thr = artifact_threshold(art)
    if thr is not None and prob < thr:
        # below threshold: prediction log only as today (no shadow trade)
        log_prediction(conn, "FMR2", symbol, direction, live_price, prob, posted=False)
        return
    setup = calculate_smart_targets(conn, symbol, direction, live_price)
    if post_shadow_ai_signal(
        conn,
        "FMR2",
        symbol,
        direction,
        prob,
        setup["entry1"],
        setup["entry2"],
        setup["sl"],
        setup["targets"],
    ):
        logger.info(f"👻 FMR2-Shadow {symbol} {direction} | p={prob:.3f} (gate {thr}) — monitored, not posted.")


def process_candidate(conn, symbol: str, direction: str, rate: float, pctl: float) -> None:
    if check_cooldown(conn, MODEL_ID, symbol, direction, COOLDOWN_HOURS):
        return
    if has_open_ai_signal(conn, symbol, direction, ARTIFACT["tag"]):
        return

    rates = fetch_funding_history(symbol)
    if rates is None:
        return
    # Cross-section provides the RUNNING rate — it does not replace the last element
    # of settlement history but is appended (most current state).
    stats = funding_stats(rates + [rate])

    res = fetch_context_frame(conn, symbol)
    if res is None:
        return
    df, idx = res

    feature_row = build_fmr1_row(stats, pctl, direction, df, idx)
    missing = [c for c in ARTIFACT["features"] if c not in feature_row]
    if missing:
        raise ValueError(f"Feature contract violated — missing: {missing}")
    X = pd.DataFrame([{c: feature_row[c] for c in ARTIFACT["features"]}], dtype=float)
    X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

    prob = float(ARTIFACT["model"].predict_proba(X)[0, 1])
    conf = calibrated_confidence(ARTIFACT, prob)

    logger.info(
        f"FMR1 funding extreme {symbol} {direction} | rate {rate * 1e4:+.1f} bps "
        f"(pctl {pctl:.2f}) | prob {prob:.3f} (gate {ARTIFACT['threshold']:.2f})"
    )

    # Entry anchor = LIVE price (core.live_price, core.candles contract 2:
    # detection on closed candles, price separate). `df` carries since
    # block 5 (T-2026-CU-9050-112) ONLY closed candles — `df["close"].iloc[-1]`
    # would be the last CLOSED 1h candle and thus up to ~59 min old
    # (T-2026-KYT-9050-011). The feature candle (idx, floor-1 join) is unaffected:
    # live price feeds exclusively entry/geometry — also that
    # of the FMR2 shadow leg, which uses the same anchor.
    live_price = get_live_price(symbol, conn)
    if live_price is None:
        # Neither Binance REST nor DB fallback provide a price: no signal,
        # no FMR2 shadow, no prediction log (a shadow row without
        # entry price is worthless for evaluation). Cooldown runs
        # anyway — the candidate WAS scored, and the mandatory 24h dedup
        # of training hangs on scoring, not posting.
        logger.warning(f"{symbol} {direction}: no live price (Binance + DB fallback) — signal skipped.")
        update_cooldown(conn, MODEL_ID, symbol, direction)
        return

    # FMR2 (K4) class-(A) shadow — BEFORE FMR1 posting logic, independent, encapsulated:
    # the live FMR1 path must never be affected by an FMR2 error.
    try:
        _emit_fmr2_shadow(conn, symbol, direction, feature_row, live_price)
    except Exception as e:  # pragma: no cover - defensive, bot must not die
        logger.warning(f"FMR2 shadow {symbol} {direction} skipped: {e}")

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
            source_desc="AI Funding Mean-Reversion Model",
            extra_info_lines=[f"Funding: {rate * 1e4:+.1f} bps, Perzentil {pctl:.0%}"],
        )
        log_prediction(conn, ARTIFACT["tag"], symbol, direction, live_price, conf, posted=True)
    else:
        if prob >= ARTIFACT["threshold"]:
            logger.info(f"👻 SHADOW-post {symbol} {direction} (p={prob:.2f}) — live posting disabled.")
        if prob >= SHADOW_FLOOR:
            log_prediction(conn, ARTIFACT["tag"], symbol, direction, live_price, conf, posted=False)
    # Cooldown on EVERY scored candidate — mirror of the mandatory
    # 24h dedup in training (review fix 2026-07-06); commits atomically.
    update_cooldown(conn, MODEL_ID, symbol, direction)


def run_scan() -> None:
    coin_set = load_coin_set()
    cs = fetch_cross_section(coin_set)
    if cs is None:
        return

    shorts = cs[cs["pctl"] >= FMR1_SHORT_PCTL].nlargest(MAX_CANDIDATES_PER_SIDE, "rate")
    longs = cs[cs["pctl"] <= FMR1_LONG_PCTL].nsmallest(MAX_CANDIDATES_PER_SIDE, "rate")
    logger.info(f"🔍 FMR1-Scan: {len(cs)} coins | {len(shorts)} SHORT / {len(longs)} LONG candidates.")

    conn = get_db_connection()
    conn_dead = False
    try:
        for frame, direction in ((shorts, "SHORT"), (longs, "LONG")):
            for row in frame.itertuples():
                try:
                    process_candidate(conn, row.symbol, direction, float(row.rate), float(row.pctl))
                except Exception as e:
                    logger.error(f"Error for {row.symbol}: {e}")
                finally:
                    try:
                        conn.rollback()  # P2.32 pattern; no-op after commit
                    except Exception:
                        logger.error("Rollback failed (dead connection) — scan abort.")
                        conn_dead = True
                if conn_dead:
                    break
            if conn_dead:
                break
    finally:
        conn.close()
    logger.info("🏁 FMR1-Scan stopped.")


def main() -> None:
    global LIVE_POSTING
    logger.info("=== 💸 AI FMR1 BOT (Funding-Extreme Mean-Reversion, S8) STARTED ===")
    if TARGET_CHANNEL_ID == 0:
        logger.warning("Neither CH_FMR1 nor CH_NEW_IDEAS set — force shadow-only mode.")
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

    startup_feature_selfcheck()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        if now.minute == SCAN_MINUTE:
            ensure_artifact()
            if ARTIFACT["loaded"]:
                run_scan()
            else:
                logger.info("No FMR1 artefact — scan skipped (idle mode).")
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")

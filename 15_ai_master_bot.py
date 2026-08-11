"""
15_ai_master_bot.py — AIM2 master meta gate (replaces AIM1, docs/AIM2_DESIGN.md).

AIM1 was retired on 2026-07-05 (audit: reliably inverted calibration,
grade F — audit_reports/dossiers/AIM1.md). AIM2 keeps the slot, channel and
posting flow, but the model + feature build are new:

  * Features exclusively via core.aim2_features.build_feature_row —
    the same builder as in the trainer (tools/aim2_build_dataset.py). No
    more train/serve skew (P0.13 error mode).
  * Candidates/swarm ONLY posted=true and WITHOUT AIM1/AIM2 (F6 self-feedback fix).
  * ml_predictions_master/*_trades_master times are naive UTC (R3 flip,
    T-2026-KYT-9050-005 — previously PG local time, hence the R07-AIM1-a fix);
    domain conversion is centralized in core.time. Candle join strictly on the
    last CLOSED 1h candle.
  * Calibrated probability (isotonic from the artifact), threshold from the
    artifact (val operating point), parity guard against dead vocabulary.
  * SHADOW-FIRST: without AIM2_LIVE_POSTING=1 in the environment the bot only
    writes shadow rows (posted=false) — rollout gate 2 from the design doc.
"""

import datetime
import hashlib
import json
import logging
import os
import time
import warnings
from typing import Any

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")
warnings.filterwarnings("ignore", category=UserWarning, module="xgboost")

from core import config as _kcfg  # channel ids
from core.aim2_features import (
    ATR_COLS,
    CONV_CONFIDENCE_MAPPING,
    MARKET_ABS_COLS,
    MARKET_PRICE_COLS,
    TRAIL_WIN_SQL,
    TRAIL_WINDOW_DAYS,
    build_feature_row,
    parity_nonzero_share,
)
from core.aim2_topn import MODEL_TAG as TOPN_TAG
from core.aim2_topn import TopNCandidate, effective_min_prob, select_topn
from core.aim2_topn import load_config as load_topn_config
from core.candles import history_start, read_candles_with_indicators, timeframe_delta
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.market_utils import get_max_leverage
from core.prob_floor import load_prob_floor
from core.signal_post import has_open_ai_signal, post_ai_signal
from core.time import legacy_naive_to_utc, r3_history_mode, utc_now_naive, utc_to_legacy_naive
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_MASTER_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
AI_CHANNEL_ID = _kcfg.CH_MASTER
TOPN_CHANNEL_ID = _kcfg.CH_AIM2_TOPN  # own channel; 0 = unset → shadow-only
MODEL_PATH = "master_meta_model_aim2.pkl"
MODEL_NAME = "AIM2"
LIVE_POSTING = os.getenv("AIM2_LIVE_POSTING", "0") == "1"  # Default: shadow-only
SHADOW_FLOOR = 0.25  # below this, don't even log shadow
# Posting floor above the artifact threshold (T-2026-CU-9050-171): on the
# realized trades of both artifact eras (splits before/after 14.07.), the
# segment below p=0.70 is null-EV (avg +0.18%, CI95 [−0.27, 0.67]) at ~72% of
# volume; from 0.70 avg 1.0–2.2%/trade. The effective gate is ALWAYS
# max(ARTIFACT["threshold"], MIN_PROB) — the floor can only tighten it.
# Shadow logging (SHADOW_FLOOR) stays untouched, data collection runs at full rate.
MIN_PROB = load_prob_floor("AIM2_MIN_PROB", 0.70)
MODEL_RELOAD_S = 24 * 3600  # R07-AIM1-b: reload model daily
CANDIDATE_WINDOW_MIN = 60  # P2.35: catch-up after downtime; dedup_key prevents double processing
MAX_JOIN_STALENESS_H = 3
PARITY_MIN_NONZERO = 0.40  # OOD guard (P0.13): too many zero features → do not act

IND_COLS = MARKET_PRICE_COLS + MARKET_ABS_COLS + ATR_COLS + ["trend_direction"]

ARTIFACT: dict[str, Any] = {
    "model": None,
    "features": [],
    "threshold": 0.80,
    "calibrator": None,
    "vocab": set(),
    "loaded_at": 0.0,
}


def load_model() -> None:
    if not os.path.exists(MODEL_PATH):
        logger.warning(
            f"⚠️ AIM2 artifact '{MODEL_PATH}' not found — bot waits "
            f"(deploy from staging_models is an operator decision)."
        )
        ARTIFACT["model"] = None
        return
    try:
        saved = joblib.load(MODEL_PATH)
        ARTIFACT["model"] = saved["model"]
        ARTIFACT["features"] = saved["features"]
        ARTIFACT["threshold"] = float(saved.get("threshold", 0.80))
        ARTIFACT["calibrator"] = saved.get("calibrator")
        ARTIFACT["vocab"] = set(saved.get("vocab_sources", []))
        ARTIFACT["loaded_at"] = time.time()
        logger.info(
            f"✅ AIM2 artifact loaded: {len(ARTIFACT['features'])} features, "
            f"threshold {ARTIFACT['threshold']} (effective gate "
            f"{max(ARTIFACT['threshold'], MIN_PROB):.2f}, floor {MIN_PROB:.2f}), "
            f"vocabulary {len(ARTIFACT['vocab'])} sources, "
            f"posting={'LIVE' if LIVE_POSTING else 'SHADOW-ONLY'}"
        )
    except Exception as e:
        logger.error(f"❌ Error loading the AIM2 artifact: {e}")
        ARTIFACT["model"] = None


def to_utc_naive(series: pd.Series) -> pd.Series:
    """Signal times as naive UTC.

    Until the R3 flip (T-2026-KYT-9050-005) this function subtracted out
    PG local time (Europe/Bucharest) — after the flip the writers write
    UTC, so a fixed compensation would be a second shift. The
    decision on how to read OLD rows lives in exactly one place:
    core.time.R3_CUTOVER_UTC (docs/UTC_POLICY.md §6)."""
    return legacy_naive_to_utc(series)


def df_from_query(conn, query, params=None):
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        columns = [desc[0] for desc in cur.description]
        return pd.DataFrame(rows, columns=columns)


_CONV_ID_MOD = (1 << 63) - 1  # BIGINT-safe (signed 64-bit) namespace for the conv hash


def conv_signal_identity(source, symbol, direction, ts, entry) -> int:
    """Stable, table-agnostic identity of a conv signal as BIGINT.

    A conv signal migrates within seconds from active_ to
    closed_trades_master — with a NEW serial id, but unchanged identity
    (5_trade_monitor.close_trade copies time/coin/direction/entry/strategy 1:1;
    closed_trades_master.id is its own SEQUENCE). The per-table `id` is
    therefore NOT suitable as a dedup key — same diagnosis as
    33_ai_fif1_bot.signal_key. id-based keys would cause two errors:
      * the closed form (new id, same open `time` → still in the candidate
        window) would be re-scored as a fresh candidate → double post, and
      * unrelated active/closed rows with a coincidentally equal id would evict
        each other from the processed set (the P2.35 collision).
    Dedup therefore uses the migration-stable fields instead of the id."""
    raw = "|".join(
        (
            str(source),
            str(symbol).upper(),
            str(direction).upper(),
            pd.Timestamp(ts).isoformat(),
            format(float(entry) if pd.notna(entry) else 0.0, ".10g"),
        )
    )
    return int(hashlib.md5(raw.encode()).hexdigest(), 16) % _CONV_ID_MOD


def dedup_key(source_type, sid, source, symbol, direction, ts, entry) -> tuple[str, int]:
    """(signal_type, signal_id) for master_ai_processed_signals.

    ai: ml_predictions_master.id is stable (posted rows never migrate) → direct
    id. conv: table-agnostic identity hash (see conv_signal_identity),
    because the per-table serial id does not survive the active→closed migration."""
    if source_type == "ai":
        return ("ai_signal", int(sid))
    return ("conv", conv_signal_identity(source, symbol, direction, ts, entry))


def load_signal_stream(conn, since_utc_naive) -> pd.DataFrame:
    """Posted AI + conv signals (swarm base AND candidate superset).
    Identical definition as in the trainer: posted=true, without AIM1/AIM2.

    The AIM2-TOPN tag is also excluded: the top-N rows are meta-gate outputs
    of the same pipeline, not a base signal — leaving them as a candidate
    or back in the swarm would be the same F6 self-feedback loop
    as with AIM1/AIM2 (T-2026-CU-9050-051).

    The lower window bound goes into the SQL in the column's domain (after the
    R3 flip = UTC, so unchanged). It is only converted when a
    cutover constant is set — core.time.utc_to_legacy_naive."""
    since_bound = utc_to_legacy_naive(pd.Timestamp(since_utc_naive).to_pydatetime())

    ai = df_from_query(
        conn,
        """
        SELECT id, model_name AS source, time, coin, direction, entry, confidence
        FROM ml_predictions_master
        WHERE posted = true AND model_name NOT IN ('AIM1', 'AIM2', %s) AND time > %s
        """,
        (TOPN_TAG, since_bound),
    )
    if not ai.empty:
        ai["source_type"] = "ai"

    conv = df_from_query(
        conn,
        """
        SELECT id, strategy AS source, time, coin, direction, entry, NULL AS confidence
        FROM active_trades_master WHERE time > %s
        UNION ALL
        SELECT id, strategy AS source, time, coin, direction, entry, NULL AS confidence
        FROM closed_trades_master WHERE time > %s
        """,
        (since_bound, since_bound),
    )
    if not conv.empty:
        conv["source_type"] = "conv"

    stream = (
        pd.concat([d for d in (ai, conv) if not d.empty], ignore_index=True)
        if (not ai.empty or not conv.empty)
        else pd.DataFrame()
    )
    if stream.empty:
        return stream

    stream["ts"] = to_utc_naive(stream["time"])
    stream = stream.dropna(subset=["ts", "coin", "direction"])
    stream["symbol"] = stream["coin"].astype(str).str.upper().str.replace(r"_\d+[mhdwM]$", "", regex=True)
    stream = stream[stream["symbol"].str.endswith("USDT")]
    stream["direction"] = stream["direction"].astype(str).str.upper()
    stream = stream[stream["direction"].isin(["LONG", "SHORT"])]
    stream["entry"] = pd.to_numeric(stream["entry"], errors="coerce")
    stream["confidence"] = pd.to_numeric(stream["confidence"], errors="coerce")
    return stream.sort_values("ts").reset_index(drop=True)


def swarm_stats(stream: pd.DataFrame, symbol: str, ts, direction: str) -> dict:
    out = {
        "total_5d": 0,
        "long_5d": 0,
        "short_5d": 0,
        "latest_age_h": 120.0,
        "confl_same_dir_4h": 0,
        "distinct_src_same_dir_4h": 0,
    }
    if stream.empty:
        return out
    g = stream[(stream["symbol"] == symbol) & (stream["ts"] < ts) & (stream["ts"] >= ts - pd.Timedelta(days=5))]
    if g.empty:
        return out
    out["total_5d"] = len(g)
    out["long_5d"] = int((g["direction"] == "LONG").sum())
    out["short_5d"] = out["total_5d"] - out["long_5d"]
    out["latest_age_h"] = float((ts - g["ts"].max()).total_seconds() / 3600.0)
    g4 = g[(g["ts"] >= ts - pd.Timedelta(hours=4)) & (g["direction"] == direction)]
    out["confl_same_dir_4h"] = len(g4)
    out["distinct_src_same_dir_4h"] = int(g4["source"].nunique())
    return out


def load_trail_map(conn) -> dict:
    """Trailing WR per AI source (30d, deduplicated) — semantics == trainer (TRAIL_WIN_SQL)."""
    df = df_from_query(
        conn,
        f"""
        SELECT model, count(*) AS n, sum(CASE WHEN win THEN 1 ELSE 0 END) AS wins
        FROM (
            SELECT model, bool_or({TRAIL_WIN_SQL}) AS win
            FROM closed_ai_signals
            WHERE close_time > now()::timestamp - INTERVAL '{TRAIL_WINDOW_DAYS} days'
            GROUP BY model, symbol, direction, open_time, close_time
        ) d GROUP BY model
        """,
    )
    if df.empty:
        return {}
    return {r["model"]: (float(r["wins"]) / float(r["n"]) if r["n"] else 0.5, int(r["n"])) for _, r in df.iterrows()}


def load_market_row(conn, symbol: str, ts) -> tuple[dict, float] | None:
    """Last CLOSED 1h candle before floor(ts) — indicators + close."""
    floor_utc = pd.Timestamp(ts).floor("h").tz_localize("UTC").to_pydatetime()
    # R1: detection on the last CLOSED candle before floor(ts). The API `end`
    # is inclusive, the open_times are hour-aligned → `end = floor_utc - 1h`
    # exactly reproduces the previous strict `open_time < floor_utc` bound.
    try:
        as_of_end = floor_utc - timeframe_delta("1h")
        df = read_candles_with_indicators(
            conn,
            symbol,
            "1h",
            limit=1,
            # As-of read: newest closed 1h row at/before `as_of_end`. Candidates are
            # filtered to signals from the last CANDIDATE_WINDOW_MIN (60) minutes, so
            # `ts` — and thus `as_of_end` — is always ~now; the 60-day floor of
            # history_start cannot truncate the lookup while it prunes ~120 chunks.
            start=history_start("1h", 1, anchor=as_of_end),
            end=as_of_end,
            include_forming=False,
            candle_columns=("open_time", "close"),
            indicator_columns=list(IND_COLS),
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    row = df.iloc[-1]
    open_time = pd.Timestamp(row["open_time"])
    if open_time.tzinfo is not None:
        open_time = open_time.tz_convert("UTC").tz_localize(None)
    if (pd.Timestamp(ts).floor("h") - open_time) > pd.Timedelta(hours=MAX_JOIN_STALENESS_H):
        return None  # data gap — better no judgment than one on old candles
    close = float(row["close"]) if pd.notna(row["close"]) else 0.0
    if close <= 0:
        return None
    return {c: row[c] for c in IND_COLS}, close


def load_latest_regime(conn) -> tuple[dict | None, float]:
    df = df_from_query(
        conn,
        """
        SELECT ts, regime, alt_context, confidence, confidence_btc, confidence_alt,
               btc_return_1h, btc_return_4h, btc_atr_1h_pct, btc_atr_4h_pct, btcdom_return_24h
        FROM regime_history ORDER BY ts DESC LIMIT 1
        """,
    )
    if df.empty:
        return None, 360.0
    row = df.iloc[0].to_dict()
    now_utc = utc_now_naive()  # regime_history.ts is naive UTC; utcnow() is deprecated
    age_min = max(0.0, (now_utc - pd.Timestamp(row["ts"]).to_pydatetime()).total_seconds() / 60.0)
    return row, age_min


#: Wall-clock of the last emitted TOPN starvation warning (throttle state).
#: The scan runs every ~60 s; an unthrottled line would add ~1,440 entries/day to a
#: log that is already read with `grep -a`. Hourly keeps "this leg is starving"
#: visible within one hour of onset without drowning the file.
_TOPN_STARVE_LOG_INTERVAL_S = 3600.0
_topn_starve_logged_at = 0.0


def _log_topn_starvation(scored: int, floor: float) -> None:
    """Warn that AIM2-TOPN is enabled but nothing clears its floor.

    T-2026-KYT-9050-101: the leg was gated LIVE from 2026-07-11 and produced zero
    rows and zero log lines for 24 days, because ``DEFAULT_MIN_PROB = 0.95`` was
    never calibrated against the artifact in force (measured: p99 of the calibrated
    probabilities is 0.84, and no threshold yields the 1-3 posts/day the module was
    specified for). Nothing in the code said so — the only TOPN line was at startup.
    A silent live leg is indistinguishable from a healthy quiet one, which is what
    this fixes.

    Deliberately WARNING, not INFO: an enabled leg producing nothing is a defect
    state, not a status update.
    """
    global _topn_starve_logged_at
    now = time.time()
    if now - _topn_starve_logged_at < _TOPN_STARVE_LOG_INTERVAL_S:
        return
    _topn_starve_logged_at = now
    logger.warning(
        f"⚠️ AIM2-TOPN enabled but starving: {scored} candidate(s) scored this cycle, "
        f"none reached the effective floor {floor:.2f} — no TOPN post is possible at "
        f"this floor. Recalibrate with tools/aim2_topn_calibrate.py (gate/floor change "
        f"is an operator decision)."
    )


def count_topn_posts_24h(conn, now_utc_naive) -> int:
    """Rolling 24h counter of AIM2-TOPN rows in ml_predictions_master.

    Counts shadow AND live (every selection writes a row), so the
    hard daily cap kicks in the same way in shadow as it does live — the shadow is
    thereby a faithful preview. `time` carries naive UTC since the R3 flip; the cutoff
    goes through utc_to_legacy_naive identically to load_signal_stream."""
    since_bound = utc_to_legacy_naive(pd.Timestamp(now_utc_naive - pd.Timedelta(hours=24)).to_pydatetime())
    df = df_from_query(
        conn,
        "SELECT count(*) AS n FROM ml_predictions_master WHERE model_name = %s AND time > %s",
        (TOPN_TAG, since_bound),
    )
    if df.empty:
        return 0
    return int(df.iloc[0]["n"])


def process_master_trades():
    if ARTIFACT["model"] is None:
        logger.warning("AIM2 skipped: artifact not loaded.")
        return

    logger.info("🔄 Starting master meta analysis (AIM2)…")
    conn = get_db_connection()
    current_time = datetime.datetime.now(datetime.timezone.utc)
    now_utc_naive = pd.Timestamp(current_time).tz_convert("UTC").tz_localize(None)

    try:
        stream = load_signal_stream(conn, now_utc_naive - pd.Timedelta(days=5))
        if stream.empty:
            logger.info("ℹ️ No signals in the 5-day window.")
            return

        candidates = stream[stream["ts"] > now_utc_naive - pd.Timedelta(minutes=CANDIDATE_WINDOW_MIN)]
        if candidates.empty:
            logger.info("ℹ️ No new signals in the candidate window.")
            return

        processed_df = df_from_query(
            conn,
            "SELECT signal_type, signal_id FROM master_ai_processed_signals WHERE processed_at > %s",
            ((current_time - datetime.timedelta(days=5)),),
        )
        processed = (
            {(str(t), int(i)) for t, i in zip(processed_df["signal_type"], processed_df["signal_id"], strict=True)}
            if not processed_df.empty
            else set()
        )
        # Table-agnostic dedup key: conv id is not stable across the active→closed
        # migration (dedup_key). Compute the key once per candidate,
        # carry it as a column and reuse it for the processed insert.
        candidates = candidates.assign(
            dkey=[
                dedup_key(st, sid, src, sym, dr, ts, en)
                for st, sid, src, sym, dr, ts, en in zip(
                    candidates["source_type"],
                    candidates["id"],
                    candidates["source"],
                    candidates["symbol"],
                    candidates["direction"],
                    candidates["ts"],
                    candidates["entry"],
                    strict=True,
                )
            ]
        )
        candidates = candidates[[k not in processed for k in candidates["dkey"]]]
        if candidates.empty:
            logger.info("ℹ️ All candidates already processed.")
            return

        logger.info(f"🔎 Analyzing {len(candidates)} new source signals…")
        trail_map = load_trail_map(conn)
        regime_row, regime_age = load_latest_regime(conn)
        market_cache: dict[str, tuple[dict, float] | None] = {}

        processed_inserts, shadow_inserts = [], []
        vocab_misses = 0

        # AIM2-TOPN (T-2026-CU-9050-051): own high-conviction channel behind a
        # default-off gate. We collect the strong, trusted
        # candidates of this cycle and select the top-N AFTER the loop
        # under the rolling 24h cap. topn_min: never below the base gate.
        topn_cfg = load_topn_config()
        topn_min = effective_min_prob(topn_cfg.min_prob, ARTIFACT["threshold"], MIN_PROB)
        topn_pool: list[TopNCandidate] = []

        for signal in candidates.itertuples():
            coin, ts, direction = signal.symbol, signal.ts, signal.direction

            if coin not in market_cache:
                market_cache[coin] = load_market_row(conn, coin, ts)
            market = market_cache[coin]
            if market is None:
                continue
            market_row, close_price = market

            if signal.source_type == "ai":
                wr, n = trail_map.get(signal.source, (0.5, 0))
                conf = float(signal.confidence) if pd.notna(signal.confidence) else 0.5
            else:
                wr, n = 0.5, 0
                conf = CONV_CONFIDENCE_MAPPING.get(signal.source, 0.5)

            src_entry = float(signal.entry) if pd.notna(signal.entry) and signal.entry else 0.0
            drift = ((close_price - src_entry) / src_entry * 100.0) if src_entry > 0 else 0.0
            drift = max(-50.0, min(50.0, drift))

            feats = build_feature_row(
                market_row,
                close_price,
                regime_row,
                regime_age,
                swarm_stats(stream, coin, ts, direction),
                {
                    "name": signal.source,
                    "type": signal.source_type,
                    "conf": conf,
                    "trail_wr_30d": wr,
                    "trail_n_30d": n,
                    "entry_drift_pct": drift,
                    "direction": direction,
                },
            )

            if ARTIFACT["vocab"] and str(signal.source) not in ARTIFACT["vocab"]:
                vocab_misses += 1  # P0.13 guard: new source the model never saw

            X = (
                pd.DataFrame([feats])
                .reindex(columns=ARTIFACT["features"], fill_value=0.0)
                .apply(pd.to_numeric, errors="coerce")
                .fillna(0.0)
                .astype("float32")
            )
            raw = float(ARTIFACT["model"].predict_proba(X)[0][1])
            prob = (
                float(ARTIFACT["calibrator"].predict(np.array([raw]))[0]) if ARTIFACT["calibrator"] is not None else raw
            )

            parity = parity_nonzero_share(X.iloc[0].tolist(), ARTIFACT["features"])
            trusted = parity >= PARITY_MIN_NONZERO
            if not trusted:
                logger.warning(
                    f"⚠️ Parity guard {coin}/{signal.source}: only {parity:.0%} "
                    f"non-zero features → OOD suspicion, no posting."
                )

            processed_inserts.append((*signal.dkey, prob))

            if topn_cfg.enabled and trusted and prob >= topn_min:
                topn_pool.append(
                    TopNCandidate(
                        coin=coin,
                        direction=direction,
                        prob=prob,
                        trusted=trusted,
                        source=str(signal.source),
                        close_price=close_price,
                    )
                )

            if prob < SHADOW_FLOOR:
                continue

            wants_post = prob >= max(ARTIFACT["threshold"], MIN_PROB) and trusted
            if not (wants_post and LIVE_POSTING):
                shadow_inserts.append((MODEL_NAME, current_time, coin, direction, close_price, prob, False))
                if wants_post:
                    logger.info(
                        f"👻 SHADOW post {coin} {direction} (p={prob:.2f}, "
                        f"source {signal.source}) — live posting disabled."
                    )
                continue

            # --- LIVE POSTING (only with AIM2_LIVE_POSTING=1 after the shadow phase) ---
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM ai_signals WHERE symbol = %s AND direction = %s AND model = %s",
                    (coin, direction, MODEL_NAME),
                )
                if cur.fetchone():
                    logger.info(f"⏳ Skip {coin} {direction}: active {MODEL_NAME} trade running.")
                    shadow_inserts.append((MODEL_NAME, current_time, coin, direction, close_price, prob, False))
                    continue

            trade_setup = calculate_smart_targets(conn, coin, direction, close_price)
            entry1, entry2 = trade_setup['entry1'], trade_setup['entry2']
            sl, targets = trade_setup['sl'], trade_setup['targets']
            lev = get_max_leverage(coin, 20)

            # How many TPs Cornix gets — and therefore how many are persisted below
            # (P2.31, T-2026-KYT-9050-100). ONE local instead of two literal slices:
            # the count drove the Cornix block while the insert stored the whole
            # calculate_smart_targets list, which is the persist ≠ publish gap.
            # Deliberately NOT core.trade_utils.N_PUBLISHED_TARGETS: that constant is
            # the target the THINNER has to reach for the own-geometry legs, and AIM2
            # is not thinned (its geometry is already ATR-spaced). Binding to it would
            # let a future thinning change silently rewrite AIM2's Cornix message.
            n_show = 3

            lines = [
                f"📈 Signal for {coin} 📈",
                f"🚨 Direction: {direction}",
                f"🚨 Leverage: {lev}",
                "🚨 Margin: Cross",
                f"🏦 CMP Entry: $ {entry1:.8f}",
                # T-2026-KYT-9050-042: entry2 is still computed and stored, but no
                # longer published — single-entry (arm B). See core/signal_post.py.
            ]
            for i, t in enumerate(targets[:n_show], 1):
                lines.append(f"💰 TP{i}: $ {t:.8f}")
            lines += [f"💸 Stop Loss: $ {sl:.8f}", "🧠 Trade idea verified by Master AI module (AIM2) V1"]
            cornix_msg = "\n".join(lines)

            html_caption = (
                f"<pre><b>💎 MASTER AI TRADE (AIM2)</b>\n"
                f"<b>{coin.replace('USDT', '')}/USDT</b>\n"
                f"<b>→ Direction: <b>{direction}</b></b>\n"
                f"<b>→ Source: {signal.source} (Conf {conf:.2f})</b>\n"
                f"<b>→ Master Confidence (calibrated): <b>{prob:.1%}</b></b>\n"
                f"<b>→ Time: {current_time.strftime('%H:%M')} UTC | Module: AIM2</b>\n\n"
                f"{cornix_msg}</pre>"
            )
            chart_buf = generate_minichart_image(coin, minutes=240)

            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (AI_CHANNEL_ID, cornix_msg)
                )
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
                cur.execute(
                    """
                    INSERT INTO ai_signals (symbol, price, model, direction, confidence,
                                            entry1, entry2, sl, targets)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        coin,
                        entry1,
                        MODEL_NAME,
                        direction,
                        float(prob),
                        float(entry1),
                        float(entry2),
                        float(sl),
                        # P2.31 (T-2026-KYT-9050-100): exactly the published slice.
                        # Monitor 8 scores `len(stored)` and closes at ALL TARGETS HIT
                        # when the last one is reached — storing the full list let it
                        # score rungs Cornix never received. Measured on 2,389 closed
                        # AIM2 trades since 2026-07-11: 89.4 % persisted MORE than the
                        # 3 published (46 % stored 10), and 6.4 % of trades were scored
                        # `targets_hit > 3`. The Cornix message is byte-identical either
                        # way — this changes the book, not what is traded.
                        json.dumps(targets[:n_show]),
                    ),
                )
            shadow_inserts.append((MODEL_NAME, current_time, coin, direction, close_price, prob, True))
            logger.info(f"✅ AIM2 MASTER ALERT {coin} {direction} (p={prob:.1%}, source {signal.source})")

        if vocab_misses:
            logger.warning(
                f"⚠️ {vocab_misses} candidates from sources outside the "
                f"training vocabulary — if this recurs: retrain (P0.13 guard)."
            )

        # --- AIM2-TOPN: top-N of the day under a rolling 24h cap ---
        # Starvation visibility (T-2026-KYT-9050-101): the ONLY TOPN log line used to
        # be at startup, so an enabled leg that never clears its floor emitted nothing
        # and said nothing. AIM2-TOPN ran gated LIVE for 24 days with zero rows and
        # zero log lines before anyone looked. Scored candidates but an empty pool is
        # the signal that the floor is unreachable — throttled so it stays readable.
        if topn_cfg.enabled and processed_inserts and not topn_pool:
            _log_topn_starvation(len(processed_inserts), topn_min)

        if topn_cfg.enabled and topn_pool:
            posts_24h = count_topn_posts_24h(conn, now_utc_naive)
            selected = select_topn(topn_pool, topn_cfg.n, topn_min, posts_24h)
            topn_live = topn_cfg.live and TOPN_CHANNEL_ID != 0
            if topn_cfg.live and TOPN_CHANNEL_ID == 0:
                logger.warning("AIM2-TOPN: live gate on, but CH_AIM2_TOPN unset → shadow-only.")
            for cand in selected:
                # If a TOPN trade for coin/direction is already running: book the slot
                # as shadow (keeps the cap honest), but do not post twice.
                if topn_live and has_open_ai_signal(conn, cand.coin, cand.direction, TOPN_TAG):
                    shadow_inserts.append(
                        (TOPN_TAG, current_time, cand.coin, cand.direction, cand.close_price, cand.prob, False)
                    )
                    logger.info(f"⏳ AIM2-TOPN Skip {cand.coin} {cand.direction}: active trade running.")
                    continue
                if topn_live:
                    setup = calculate_smart_targets(conn, cand.coin, cand.direction, cand.close_price)
                    post_ai_signal(
                        conn,
                        TOPN_CHANNEL_ID,
                        TOPN_TAG,
                        cand.coin,
                        cand.direction,
                        cand.prob,
                        setup["entry1"],
                        setup["entry2"],
                        setup["sl"],
                        setup["targets"],
                        source_desc=f"AIM2 Top-{topn_cfg.n} of the day · source {cand.source}",
                        extra_info_lines=[f"Master confidence (calibrated): {cand.prob:.1%}"],
                    )
                    shadow_inserts.append(
                        (TOPN_TAG, current_time, cand.coin, cand.direction, cand.close_price, cand.prob, True)
                    )
                    logger.info(
                        f"💎 AIM2-TOPN LIVE {cand.coin} {cand.direction} (p={cand.prob:.1%}, source {cand.source})"
                    )
                else:
                    shadow_inserts.append(
                        (TOPN_TAG, current_time, cand.coin, cand.direction, cand.close_price, cand.prob, False)
                    )
                    logger.info(
                        f"👻 AIM2-TOPN SHADOW {cand.coin} {cand.direction} "
                        f"(p={cand.prob:.1%}, source {cand.source}) — live posting disabled."
                    )

        with conn.cursor() as cur:
            if processed_inserts:
                cur.executemany(
                    """
                    INSERT INTO master_ai_processed_signals (signal_type, signal_id, ml_confidence, processed_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (signal_type, signal_id)
                    DO UPDATE SET processed_at = NOW(), ml_confidence = EXCLUDED.ml_confidence
                    """,
                    processed_inserts,
                )
            if shadow_inserts:
                cur.executemany(
                    """
                    INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                    VALUES (0, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    shadow_inserts,
                )
        conn.commit()

    except Exception as e:
        logger.error(f"Critical error in the master task: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    logger.info("🏁 AIM2 master analysis finished.")


def startup() -> None:
    """One-time init: processed-signals table, model load, TOPN operating point.

    Everything main() did before entering its loop (T-2026-KYT-9050-135).
    """
    logger.info(f"=== 🧠 AI MASTER BOT (AIM2) STARTED — {'LIVE-POSTING' if LIVE_POSTING else 'SHADOW-ONLY'} ===")
    # R3: which reading interprets the signal stream is logged —
    # a silent wrong assumption about the history would be exactly the P0.13 error mode.
    logger.info(f"    R3 time domain: {r3_history_mode()} (docs/UTC_POLICY.md §6)")
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS master_ai_processed_signals (
                signal_type TEXT NOT NULL,
                signal_id BIGINT NOT NULL,
                processed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                ml_confidence NUMERIC(5, 4),
                PRIMARY KEY (signal_type, signal_id)
            );
        """)
    conn.commit()
    conn.close()

    load_model()

    # AFTER load_model() on purpose (T-2026-KYT-9050-103): the effective floor is
    # computed from ARTIFACT["threshold"], which only holds the real value once the
    # pkl is loaded. Logged from above it read the module-level initialisation
    # default (0.80) and printed a floor derived from a placeholder — the very
    # failure class T-2026-KYT-9050-101 set out to remove, just moved instead of
    # removed. Caught in the live startup log after the 2026-08-04 restart
    # ("artifact 0.80" next to "threshold 0.67"), not by a test.
    _topn = load_topn_config()
    if _topn.enabled:
        _topn_mode = "LIVE" if (_topn.live and TOPN_CHANNEL_ID != 0) else "SHADOW-ONLY"
        _topn_floor = effective_min_prob(_topn.min_prob, ARTIFACT["threshold"], MIN_PROB)
        logger.info(
            f"    AIM2-TOPN active — N={_topn.n}, effective floor={_topn_floor:.2f} "
            f"(configured {_topn.min_prob:.2f}, artifact {ARTIFACT['threshold']:.2f}, "
            f"bot floor {MIN_PROB:.2f}), posting={_topn_mode}"
        )
    else:
        logger.info("    AIM2-TOPN disabled (AIM2_TOPN_ENABLED≠1) — base AIM2 unchanged.")


def run_poll() -> None:
    """One poll cycle — the body of main()'s loop, unchanged.

    The 24 h reload check stays part of the cycle (R07-AIM1-b): moving it to
    startup would pin the process to the artifact it booted with.
    """
    if time.time() - ARTIFACT["loaded_at"] > MODEL_RELOAD_S:
        load_model()
    process_master_trades()


def main():
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
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly…")

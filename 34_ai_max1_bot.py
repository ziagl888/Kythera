# 34_ai_max1_bot.py — MAX1 "High-Conviction Rubberband Short" (T-2026-CU-9050-067).
"""
Standalone SHORT clone of the RUB2 path (13_ai_rub_bot.py) with a high-conviction
throttle: at most 1-3 of the day's strongest rubberband shorts, posted into the
MAIN channel under its own model tag MAX1.

Operator decision (Michi, 2026-07-11): RUB2 itself is NOT throttled and keeps
posting unchanged in its own channel (T-2026-CU-9050-050 → wontfix). MAX1 is a
second, selective consumer of the same edge — see docs/MODEL_INTENT.md §8a.

Properties:
  * Same detection + features as RUB2-SHORT — `core/rub_features.py` and
    `core/funding_features.py` are IMPORTED, never modified (X-R1 rule): bot,
    trainer and walkforward replay stay one source. Trade geometry likewise comes
    from the shared `hvn_sr_trade_geometry` — that is the geometry the RUB2 labels
    were replayed with, so its win-rate carries over.
  * Own artifact (`max1_model_SHORT.pkl`): a copy of the RUB2-SHORT model whose
    meta.model_id is MAX1 (tools/make_max1_artifact.py). The tag is read from that
    meta, never from a constant (harte Regel 6 / Falle 16). Without the artifact
    the bot idles (Falle 3) — promoting it out of staging_models/ is Michi's call.
  * Throttle in `core/max1_gate.py` (pure, DB-free, unit-tested): high probability
    floor + hard rolling-24h cap, applied to the candidates of a whole scan.
  * Posting behind MAX1_LIVE_POSTING (default OFF) → shadow rows only. Unset
    channel forces shadow too. Exactly ONE Cornix-parseable message per signal via
    the audited `core.signal_post.post_ai_signal` (harte Regel 4).
  * Cooldown/dedupe live in MAX1's OWN namespace (tag = MAX1): MAX1 and RUB2 never
    block each other, and both firing on the same coin is possible BY DESIGN
    (double exposure — documented in MODEL_INTENT §8a).

Scan runs at minute 15 — the same closed 1h candle RUB2 scores at minute 10
(features and entry price come from that candle, not from the live price, so the
offset changes nothing but keeps the two full-fleet scans off each other's DB).

Two things to know when reading the SHADOW numbers (they feed Michi's threshold
decision, so they must not be over-read):
  * Shadow frequency is an UPPER bound. A live post writes an ai_signals row, which
    suppresses further selections of that coin until the trade closes; in shadow no
    such row exists, so only the 4h cooldown throttles a repeat. Shadow can therefore
    show slightly MORE posts/day than live would produce — never fewer.
  * MAX1 scans the FULL coin universe from coins.json, not the curated
    config.MAIN_CHANNEL_COINS list. The main channel will see alts it does not see
    today. Restricting the universe would be a separate operator decision.

Watchdog: start_delay=223.
"""

import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import time

import numpy as np
import pandas as pd

from core import config as _kcfg
from core.database import get_db_connection
from core.funding_features import FUNDING_FEATURES, funding_features_cached
from core.market_utils import check_cooldown, update_cooldown
from core.max1_gate import (
    ARTIFACT_PATH,
    DIRECTION,
    MODEL_ID,
    Max1Candidate,
    load_config,
    select_signals,
)
from core.model_artifacts import calibrated_confidence, load_artifact, maybe_reload
from core.rub_features import RUB_FEATURES, build_rub_features, rub_event_type, rub_trend
from core.signal_post import has_open_ai_signal, log_prediction, post_ai_signal
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels, hvn_sr_trade_geometry

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_MAX1_BOT - %(message)s')
logger = logging.getLogger(__name__)

TARGET_CHANNEL_ID = _kcfg.CH_MAX1  # per-bot override, falls back to CH_MAIN
EXPECTED_FEATURES = RUB_FEATURES + FUNDING_FEATURES  # 9 rub + 6 funding, RUB2 contract
COOLDOWN_HOURS = 4  # per coin — same frequency lock RUB2/the replay use
ARTIFACT_RETRY_S = 1800  # idle mode: look for a fresh deploy every 30 min
SCAN_MINUTE = 15

ARTIFACT = load_artifact(ARTIFACT_PATH, EXPECTED_FEATURES, MODEL_ID)


def ensure_artifact() -> None:
    """Daily reload of a loaded artifact, 30-min retry while idle (PEX1 pattern)."""
    global ARTIFACT
    if ARTIFACT["loaded"]:
        ARTIFACT = maybe_reload(ARTIFACT, EXPECTED_FEATURES)
    elif time.time() - ARTIFACT["loaded_at"] > ARTIFACT_RETRY_S:
        ARTIFACT = load_artifact(ARTIFACT_PATH, EXPECTED_FEATURES, MODEL_ID)


def cooldown_tags() -> list[str]:
    """Current tag plus the default tag while they differ (transitional dedup).

    The cooldown key IS the model tag, and the tag flips on a retrain rollout
    (MAX1 → MAX2). Without the old tag in the check, a fresh MAX2 signal would no
    longer be blocked by the MAX1 cooldown row of the same coin. Same tags (today)
    ⇒ the second query disappears. Pattern: 13_ai_rub_bot.py.
    """
    tag = ARTIFACT["tag"]
    return [tag] if tag == MODEL_ID else [tag, MODEL_ID]


def count_posts_24h(conn) -> int:
    """Rolling-24h count of MAX1 selections in ml_predictions_master.

    Contract of the MAX1 tag in that table: a row exists for a SELECTION only —
    shadow or live — never for a rejected candidate. That is what makes this count
    the honest cap ledger and the shadow a faithful preview of live. Candidates
    below the gate need no MAX1 row: the RUB2 scan already persists the identical
    prediction under its own tag.

    Time domain (R3, Falle 9): psycopg2 hands `log_prediction`'s aware-UTC value to
    a `timestamp without time zone` column, so PG stores it rotated into the session
    timezone. LOCALTIMESTAMP is that same session-local domain — comparing against it
    is self-consistent whatever the server is set to, without hardcoding a zone.
    """
    tags = cooldown_tags()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM ml_predictions_master
            WHERE model_name = ANY(%s)
              AND time > LOCALTIMESTAMP - INTERVAL '24 hours'
            """,
            (tags,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def score_symbol(conn, symbol: str, now: datetime.datetime, gate: float) -> Max1Candidate | None:
    """Run the RUB2-SHORT path for one coin. Returns a candidate above `gate`, else None.

    Mirrors 13_ai_rub_bot.check_rubberband_conditions for the SHORT direction —
    same closed-candle queries (P1.19), same prefilter, same features, same as-of
    funding at the candle close. Deliberately a clone, not a refactor of bot 13:
    RUB2 is live and stays untouched (T-2026-CU-9050-050).
    """
    # 90d closes for the regression. P1.19: exclude the forming candle, otherwise
    # the regression fits on an open candle made of ~2 minutes of data.
    query_90d = f"""
        SELECT open_time, close
        FROM "{symbol}_1h"
        WHERE open_time >= NOW() - INTERVAL '95 days'
          AND open_time < date_trunc('hour', NOW())
        ORDER BY open_time ASC
    """
    # P1.19: closed-candle filter — LIMIT 1 would otherwise return the open candle
    # (partial indicators). `close` comes from the SAME closed candle as the
    # indicators, so features and entry price never mix live price with partials.
    query_ind = f"""
        SELECT
            close,
            rsi_14, tsi_fast_12_7_7, tsi_fast_12_7_7_signal,
            macd_dif_normal_12_26_9, macd_dea_normal_12_26_9,
            atr_14, ema_200, donchian_lower_20, donchian_upper_20
        FROM "{symbol}_1h_indicators"
        WHERE open_time < date_trunc('hour', NOW())
        ORDER BY open_time DESC LIMIT 1
    """

    with conn.cursor() as cur:
        cur.execute(query_90d)
        rows_90d = cur.fetchall()
        if len(rows_90d) < 50:
            return None
        df_90d = pd.DataFrame(rows_90d, columns=['open_time', 'close'])

        cur.execute(query_ind)
        row_ind = cur.fetchone()
        if not row_ind:
            return None
        columns_ind = [desc[0] for desc in cur.description]
        ind = dict(zip(columns_ind, row_ind, strict=False))

    df_90d['ts'] = pd.to_datetime(df_90d['open_time'], utc=True).apply(lambda x: x.timestamp())
    ts_values = df_90d['ts'].values
    close_values = df_90d['close'].values.astype(float)

    try:
        curr_close = float(ind['close'])
        if not np.isfinite(curr_close):
            curr_close = float(close_values[-1])
    except (TypeError, ValueError, KeyError):
        curr_close = float(close_values[-1])

    dist_to_trend_pct, slope_pct_per_day = rub_trend(ts_values, close_values, curr_close)

    def get_f(key, default=0.0):
        val = ind.get(key)
        try:
            if val is None:
                return default
            fv = float(val)
            if not np.isfinite(fv):  # NaN/Inf (fresh coins) would poison predict_proba
                return default
            return fv
        except (TypeError, ValueError):
            return default

    rsi = get_f('rsi_14', 50)
    tsi_line = get_f('tsi_fast_12_7_7')
    tsi_signal = get_f('tsi_fast_12_7_7_signal')
    macd_line = get_f('macd_dif_normal_12_26_9')
    macd_signal = get_f('macd_dea_normal_12_26_9')
    atr_14 = get_f('atr_14')
    ema_200 = get_f('ema_200', curr_close)
    dc_lower = get_f('donchian_lower_20', curr_close)
    dc_upper = get_f('donchian_upper_20', curr_close)

    # SHORT-only: REVERSION_UP (the LONG event) is not MAX1's business.
    if rub_event_type(dist_to_trend_pct, rsi, tsi_line, curr_close, dc_lower, dc_upper) != "REVERSION_DOWN":
        return None

    tag = ARTIFACT["tag"]
    if has_open_ai_signal(conn, symbol, DIRECTION, tag):
        return None  # trade still open in the AI monitor — no second position
    if any(check_cooldown(conn, t, symbol, DIRECTION, COOLDOWN_HOURS) for t in cooldown_tags()):
        return None

    base_features = build_rub_features(
        dist_to_trend_pct,
        slope_pct_per_day,
        curr_close,
        rsi,
        tsi_line,
        tsi_signal,
        macd_line,
        macd_signal,
        atr_14,
        ema_200,
    )
    # Funding as-of the CANDLE CLOSE (hh:00), not the scan time — the trainer
    # decided at the candle boundary, and on settlement hours a scan-time as-of
    # would slip exactly one funding print between training and serving (PR #9).
    # Missing history ⇒ columns absent ⇒ 0 below, mirroring the trainer's fillna(0);
    # safe because load_artifact validated the feature NAMES hard.
    #
    # The CACHED path (T-2026-CU-9050-055), not bot 13's hand-rolled 95d load: same
    # values, one DB roundtrip per symbol per settlement instead of per scan, and its
    # 110d window is the one that actually covers the 270 prints fund_pctl_90d needs
    # (95d clips them). The trainer computed the features on the full history, so the
    # wider window is the better train/serve parity, not a deviation from it.
    ts_decision = now.replace(minute=0, second=0, microsecond=0)
    base_features.update(funding_features_cached(conn, symbol, ts_decision))

    ml_input = pd.DataFrame([base_features]).reindex(columns=ARTIFACT["features"]).fillna(0)
    prob = float(ARTIFACT["model"].predict_proba(ml_input)[0, 1])

    logger.info(f"MAX1 Trigger: {symbol} {DIRECTION} | ML-Conf: {prob:.1%} (Gate: {gate:.3f})")
    if prob < gate:
        return None
    return Max1Candidate(symbol=symbol, prob=prob, close_price=curr_close)


def post_candidate(conn, cand: Max1Candidate, live: bool) -> None:
    """Post (or shadow-log) one selected candidate. Commits via update_cooldown.

    Geometry is the shared `hvn_sr_trade_geometry` — the same entry2/SL/target
    construction the RUB2 replay labelled against (MODEL_INTENT §8), so the
    measured win rate is about the trades this actually places.
    """
    tag = ARTIFACT["tag"]
    entry1 = cand.close_price
    supps, resis = get_hvn_and_sr_levels(conn, cand.symbol, entry1)
    entry2, sl, t_cands = hvn_sr_trade_geometry(entry1, is_long=False, supps=supps, resis=resis)
    targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long=False, min_pct=0.05)
    # The persisted confidence is the RAW probability — the domain the gate and the
    # 044 threshold curve live in, and what RUB2 writes for the identical model. The
    # calibrated value is shown in the info message only; persisting it instead would
    # put the shadow rows Michi picks the final threshold from on a different scale.
    cal = calibrated_confidence(ARTIFACT, cand.prob)

    if live:
        post_ai_signal(
            conn,
            TARGET_CHANNEL_ID,
            tag,
            cand.symbol,
            DIRECTION,
            cand.prob,
            entry1,
            entry2,
            sl,
            targets,
            source_desc="High-Conviction Rubberband Short (MAX1)",
            extra_info_lines=[f"Kalibriert: {cal:.1%} (Gate läuft auf der rohen Probability)"],
        )
        logger.info(f"🔥 MAX1 LIVE {cand.symbol} {DIRECTION} (p={cand.prob:.1%}, kal. {cal:.1%})")
    else:
        logger.info(
            f"👻 MAX1 SHADOW {cand.symbol} {DIRECTION} (p={cand.prob:.1%}, kal. {cal:.1%}) — Live-Posting deaktiviert."
        )

    log_prediction(
        conn,
        tag,
        cand.symbol,
        DIRECTION,
        entry1,
        cand.prob,
        posted=live,
        dedup_hours=COOLDOWN_HOURS,
        legacy_tag=MODEL_ID,
    )
    # Cooldown on every SELECTION, shadow included — the shadow must throttle
    # exactly like live, otherwise it is not a preview. update_cooldown(commit=True)
    # closes outbox + ai_signals + prediction row + cooldown atomically.
    update_cooldown(conn, tag, cand.symbol, DIRECTION)


def _rollback(conn) -> bool:
    """Close the failed transaction. False = the connection itself is dead.

    P2.32 pattern: a rollback on a dead connection RAISES. Without this guard that
    exception escapes run_scan, kills the process and hands the bot to the watchdog
    backoff — instead of just ending this scan cycle and reconnecting on the next.
    """
    try:
        conn.rollback()
        return True
    except Exception:
        logger.error("Rollback fehlgeschlagen (tote Connection) — Zyklus-Abbruch.")
        return False


def run_scan() -> None:
    cfg = load_config()
    live = cfg.live and TARGET_CHANNEL_ID != 0
    # The gate never sits below the model's own operating point: MAX1 exists to be
    # MORE selective than RUB2, never less.
    gate = max(cfg.min_prob, float(ARTIFACT["threshold"]))

    try:
        with open('coins.json') as f:
            coins = json.load(f)
    except Exception as e:
        logger.error(f"Could not load coins.json: {e}")
        return

    conn = get_db_connection()
    try:
        posts_24h = count_posts_24h(conn)
        if posts_24h >= cfg.max_per_day:
            logger.info(f"🚦 MAX1 Tages-Kappe erreicht ({posts_24h}/{cfg.max_per_day} in 24h) — Scan übersprungen.")
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        logger.info(
            f"🔍 MAX1-Scan über {len(coins)} Coins (Gate {gate:.3f}, {posts_24h}/{cfg.max_per_day} Posts in 24h)…"
        )

        pool: list[Max1Candidate] = []
        for symbol in coins:
            if 'USDT_' in symbol:  # skip USDC-quoted pairs (RUB2 filter)
                continue
            try:
                cand = score_symbol(conn, symbol, now, gate)
                if cand is not None:
                    pool.append(cand)
            except Exception as e:
                logger.error(f"Error for {symbol} in MAX1: {e}")
                if not _rollback(conn):
                    return  # dead connection — abort the cycle instead of logging it 530×

        selected = select_signals(pool, cfg.max_per_day, gate, posts_24h)
        if pool and not selected:
            logger.info(f"🚦 {len(pool)} Kandidaten über Gate, aber kein freier Slot (Kappe {cfg.max_per_day}/24h).")
        for cand in selected:
            try:
                post_candidate(conn, cand, live)
            except Exception as e:
                logger.error(f"Error posting {cand.symbol} in MAX1: {e}")
                if not _rollback(conn):
                    return
    except Exception as e:
        logger.error(f"MAX1-Scan-Fehler: {e}")
        _rollback(conn)
    finally:
        conn.close()
    logger.info("🏁 MAX1-Scan beendet.")


def main() -> None:
    cfg = load_config()
    logger.info("=== 💎 AI MAX1 BOT (High-Conviction Rubberband Short) GESTARTET ===")
    if cfg.live and TARGET_CHANNEL_ID == 0:
        logger.warning("MAX1: Live-Gate an, aber weder CH_MAX1 noch CH_MAIN gesetzt → Shadow-only.")
    mode = "LIVE" if (cfg.live and TARGET_CHANNEL_ID != 0) else "SHADOW-ONLY"
    logger.info(
        f"    Posting={mode} | min_prob={cfg.min_prob:.2f} | Kappe={cfg.max_per_day}/24h | "
        f"Artefakt={'geladen (' + ARTIFACT['tag'] + ')' if ARTIFACT['loaded'] else 'fehlt → Idle-Modus'}"
    )

    while True:
        # Outside the minute gate, so the idle retry really is every ARTIFACT_RETRY_S
        # and a fresh deploy is picked up without waiting for the next scan hour.
        ensure_artifact()
        now = datetime.datetime.now(datetime.timezone.utc)
        if now.minute == SCAN_MINUTE:
            if ARTIFACT["loaded"]:
                run_scan()
            else:
                logger.info("MAX1 idle — kein Artefakt deployt (Promotion aus staging_models ist Operator-Entscheid).")
            time.sleep(60)  # don't trigger twice inside the same minute
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell gestoppt (Strg+C). Fahre sauber herunter…")

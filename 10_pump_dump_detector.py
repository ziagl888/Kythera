import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import os
import time
from collections import deque

import joblib
import numpy as np
import pandas as pd
import requests

from core import config as _kcfg  # channel ids
from core import shadow_gate, ticker_10s
from core.candles import read_indicators
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.funding_features import FUNDING_FEATURES, funding_features_cached
from core.market_utils import get_max_leverage
from core.model_artifacts import load_artifact, maybe_reload
from core.signal_post import (
    LEG_LIVE,
    LEG_SHADOW,
    has_open_ai_signal,
    log_prediction,
    post_ai_signal_gated,
    route_legacy_leg,
)
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels

logging.basicConfig(level=logging.INFO, format='%(asctime)s - PUMP_DUMP_DETECTOR - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & CHANNELS ---
# 10s persistence for microstructure features (PEX1 V2): kill-switch per env,
# so ops can silence the writer without code deploy (pattern P1.34).
TICKER_10S_PERSIST = os.getenv("KYTHERA_TICKER_10S_PERSIST", "1") == "1"

MARKET_CHANNEL_ID = _kcfg.CH_PUMP_MARKET
AI_CHANNEL_ID = _kcfg.CH_PUMP_AI
MAIN_CHANNEL_ID = _kcfg.CH_PUMP_MAIN
SENTIMENT_CHANNEL_ID = _kcfg.CH_MARKET_DATA

ROUND_LEVEL_CONFIG = {
    "BTCUSDT": {"step": 500, "decimals": 0},
    "ETHUSDT": {"step": 100, "decimals": 0},
    "BNBUSDT": {"step": 50, "decimals": 0},
    "SOLUSDT": {"step": 10, "decimals": 1},
    "XRPUSDT": {"step": 0.1, "decimals": 3},
    "BTCDOMUSDT": {"step": 100, "decimals": 0},
}

# P1.39: window edge guard for time-based lookups.
#
# Bucket timestamps sit exactly on the 10s grid (`_tick_epoch % 10`). When a
# window is measured against the anchor `bucket_anchor` (= timestamp of the most
# recent bucket), each target time falls exactly on a grid point — the guard then
# only absorbs float/parse noise at the boundary.
#
# 5s is less than half the bucket spacing (10s), so the guard can never let in
# an extra bucket. That's precisely why it's so small.
#
# IMPORTANT: the guard does NOT replace the anchor. Measured against the wall
# clock `now`, the target time drifts by the phase offset [0,10) between grid
# point and call time, and the window would swing between 6 and 7 buckets — a
# model feature that jumps without the market moving. That's why all feature
# lookups anchor to `bucket_anchor`, not to `now`.
WINDOW_EDGE_GUARD = 5

# T-2026-CU-9050-035: the 5s guard above is only valid where the 10s grid is
# actually dense. Measured on 421_350 real anchors from a live 1minute.json
# snapshot (2026-07-10, 6h window): the bucket spacing is bimodal — median 10s,
# but p90 = 70s, and only 62.7% of gaps are <= 15s. The detector polls ~530
# symbols per REST round-trip, so under load it simply does not produce a bucket
# every 10s.
#
# Consequence for `p_chg_60s`: demanding a bucket at exactly anchor-60s +/- 5s
# resolved for 61.3% of anchors — the other 38.7% returned early and were never
# scored. Widening the tolerance instead (e.g. 20s) would reintroduce the very
# mislabelling P1.39 removed: a bucket 80s old is not a 60s reference.
#
# Fix: pick the bucket whose age is CLOSEST to 60s inside [45s, 150s] and
# normalise the observed price change to a per-60s rate. The feature keeps its
# meaning at any cadence. Same measurement: 97.7% of anchors resolve, the chosen
# dt has median 60s (p90 80s), and the resulting scale factor 60/dt has median
# 1.00 (p10 0.75) — on a dense grid this is a no-op by construction.
P_CHG_WINDOW_TARGET_SEC = 60
P_CHG_WINDOW_MIN_SEC = 45
P_CHG_WINDOW_MAX_SEC = 150

# Baseline warmup for the volume paths. Pre-P1.39 this was `len(volumes_10s) >=
# 360`, where `volumes_10s` spanned the ENTIRE 1440-entry deque (~4h) — a warmup
# check that is always true once the bot is running. P1.39 kept the literal 360
# but moved it onto a list that now only spans the last 3600s, silently turning a
# warmup gate into a "one bucket per 10s for a full hour" density requirement.
# Real density is ~193 buckets/hour, so the gate passed for 0 of 421_350 anchors:
# the Volume-Explosion alert would never fire again. gate on the window actually
# COVERING an hour plus a sample floor instead of on a bucket count.
HOUR_WINDOW_MIN_COVERAGE_SEC = 3000
HOUR_WINDOW_MIN_SAMPLES = 30

# --- ML MODEL FOR 10 SECONDS ---
#
# TWO generations, two formats (T-2026-CU-9050-042):
#
#   LEGACY (live today) — pump_dump_model.pkl is a RAW 3-class model.
#       Success = class 2 (pump) or class 0 (dump), features as a POSITIONAL
#       array, no meta, no threshold. It posts under the constant EPD2.
#
#   EPD2-RETRAIN — epd2_model_{LONG,SHORT}.pkl are dict artifacts
#       (tools/retrain_from_replay.py --strategy epd): per direction a BINARY
#       model (success = predict_proba[:, 1]), features BY NAME including the
#       6 funding columns, threshold and model_id in the meta.
#
# If the artifacts are present, they win; the tag then comes from meta.model_id
# (hard rule 6) instead of EPD_LEGACY_TAG. Without artifacts, the legacy path
# continues unchanged — this session does not change the live semantics.
ML_MODEL_PATH = "pump_dump_model.pkl"
EPD2_ARTIFACT_PATHS = {"LONG": "epd2_model_LONG.pkl", "SHORT": "epd2_model_SHORT.pkl"}

# Tag under which the bot posts before the retrain rollout — default_tag for an
# artifact without model_id AND transitional dedup key (see log_prediction).
EPD_LEGACY_TAG = "EPD2"
# Posting threshold of the legacy model. An EPD2 artifact brings its own.
EPD_LEGACY_THRESHOLD = 0.60
# Lower bound of the shadow band (below that: junk, no log at all).
EPD_SHADOW_THRESHOLD = 0.25

# The 10 base features in the order the LEGACY model expects them as a
# positional array. Order is a contract here — do not sort.
EPD_BASE_FEATURES = [
    "vol_ratio",
    "p_chg_60s",
    "buy_pres",
    "volat",
    "sample_fill",
    "rsi",
    "tsi",
    "macd",
    "e9_dist",
    "e21_dist",
]
# What the serving builder CAN deliver (P0.12 contract): base + funding.
EPD_EXPECTED_FEATURES = EPD_BASE_FEATURES + list(FUNDING_FEATURES)

_ml_model = None
_ml_model_time = None
_epd2: dict[str, dict] = {}

# EPD3 (T-2026-CU-9050-125): the epd2 retrain, originally run PARALLEL as a shadow
# from staging_models/. IMPORTANT: bot 10 already posts the legacy EPD leg under tag
# "EPD2" (EPD_LEGACY_TAG); this generation therefore gets the collision-free tag
# "EPD3" (analogous to RUB3) — otherwise a shadow trade would suppress a LIVE post
# via the active-trade check `model IN ('EPD2','EPD2')`.
# The "never live" of the original design no longer holds: LONG went live 2026-07-25
# (T-2026-KYT-9050-037) and SHORT 2026-08-03 (T-2026-KYT-9050-085), both operator
# decisions — so both legs now load from the repo ROOT, not from staging. The routing
# is owned by shadow_gate; this module must not hardcode either location.
_shadow_epd3: dict[str, object | None] = {"LONG": None, "SHORT": None}


def load_epd2_artifacts():
    """Loads the retrain artifacts (dict format) — empty, while none are deployed."""
    for direction, path in EPD2_ARTIFACT_PATHS.items():
        _epd2[direction] = load_artifact(path, EPD_EXPECTED_FEATURES, EPD_LEGACY_TAG)
    # EPD3 artifacts (fail-soft; tag EPD3, files epd3_*). shadow_gate decides per leg
    # whether that is the repo root (LIVE) or staging_models/ (SHADOW).
    for direction in ("LONG", "SHORT"):
        _shadow_epd3[direction] = shadow_gate.load_shadow_artifact("EPD3", direction)
    if any(_shadow_epd3.values()):
        loaded = [f"{d} ({shadow_gate.leg_status('EPD3', d)})" for d, m in _shadow_epd3.items() if m is not None]
        logger.info(f"👻 EPD3 models loaded: {', '.join(loaded)}")
    return {d: a for d, a in _epd2.items() if a["loaded"]}


def _emit_epd3_shadow(conn, symbol, base_features, now, current_price):
    """EPD3 emission via shadow_gate routing (T-2026-CU-9050-125/185).

    Builds the IDENTICAL 16-feature vector as the live EPD2 path (base_features
    + funding as-of, cached — rule 7), scores the artifacts per direction, takes the
    strongest candidate and emits at prob>=threshold via post_ai_signal_gated.
    Lifecycle per direction (shadow_gate): EPD3 LONG is LIVE (@0.76, T-2026-KYT-9050-037
    operator decision — volume cap, no edge filter); EPD3 SHORT is LIVE too since
    2026-08-03 (@0.6737, T-2026-KYT-9050-085 operator decision — the T-033 park is
    lifted). BOTH legs therefore post Cornix to CH_PUMP_AI and load their artifact
    from the repo ROOT (challenger-distinct epd3_model_{LONG,SHORT}.pkl).

    ⚠ SELECTION CAVEAT (measured in T-085, NOT fixed here): this function scores both
    directions, takes the STRONGEST by RAW probability and then checks only THAT
    direction's threshold. The two thresholds differ (LONG 0.76, SHORT 0.6737) and the
    raw scores of two different models are not comparable, so a valid signal of one leg
    can be swallowed by a sub-threshold score of the other. Live evidence: after the
    LONG artifact was promoted on 2026-07-25 09:25 (threshold 0.76 replacing a
    null-threshold staging dump), LONG emitted ZERO signals for the following nine days
    because it kept losing the max() to SHORT — whose emissions were then discarded as
    shadow. Follow-up: filter candidates against their OWN threshold first, then take
    the strongest.

    ⚠ KILL SWITCH: despite both legs being LIVE, the whole emission still sits behind
    shadow_posting_enabled() — setting KYTHERA_SHADOW_POSTING=0 silences real Cornix
    posting for EPD3, not just shadow traffic. The env var predates the promotions and
    keeps its shadow-era name.

    ⚠ REPORT NOTE (T-085): realized_lifecycle_bucket (23_market_tracker.py) buckets a
    closed trade by the leg's CURRENT lifecycle, not by how it was posted at the time.
    The SHORT unpark therefore moves the ~5.7k pre-2026-08-03 EPD3 SHORT trades — all
    of them shadow, never sent to Cornix — into the ACTIVE block of the 4h realised-PnL
    report. Their numbers carry the T-009 phantom-win inflation, so early post-flip
    EPD3 SHORT figures in that block are NOT live performance.

    Geometry = same HVN/S-R construction as the live path (deliberately duplicated).
    Errors remain encapsulated.
    """
    if not shadow_gate.shadow_posting_enabled():
        return
    arts = {d: a for d, a in _shadow_epd3.items() if a is not None}
    if not arts:
        return
    try:
        feats = {**base_features, **funding_features_cached(conn, symbol, now)}
        cands = [(shadow_gate.score_artifact(a, feats), d, a) for d, a in arts.items()]
        best_prob, best_dir, best_art = max(cands, key=lambda c: c[0])
        thr = shadow_gate.artifact_threshold(best_art)
        if thr is not None and best_prob < thr:
            return
        # Hot-path guard (P1.41 lesson): bot 10 runs on a 10s tick, and the 900s
        # timer is reset only in the live-trade branch — without this early-out
        # the expensive HVN/S-R geometry (DB query) ran on EVERY tick, as long as an
        # EPD3 trade for this coin is open. (The original wording "LONG threshold is
        # null → always fires" described the pre-2026-07-25 staging artifact; since the
        # T-037 promotion LONG carries 0.76 and SHORT 0.6737 — the early-out is still
        # required, the reason is now simply the emission rate.) The has_open check in
        # post_shadow_ai_signal would come only AFTER the geometry — so move it up here.
        if has_open_ai_signal(conn, symbol, best_dir, "EPD3"):
            return
        is_long = best_dir == "LONG"
        entry1 = current_price
        entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
        supps, resis = get_hvn_and_sr_levels(conn, symbol, current_price)
        if is_long:
            sl = (
                max([x for x in supps if x < entry2 * 0.99])
                if any(x < entry2 * 0.99 for x in supps)
                else entry2 * 0.975
            )
            t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
        else:
            sl = (
                min([x for x in resis if x > entry2 * 1.01])
                if any(x > entry2 * 1.01 for x in resis)
                else entry2 * 1.025
            )
            t_cands = sorted([x for x in supps if x > 0 and x < (entry1 * 0.99)], reverse=True)
        targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)
        if not targets:
            return
        outcome = post_ai_signal_gated(
            conn,
            "EPD3",
            best_dir,
            _kcfg.CH_PUMP_AI,  # EPD3 → pump-AI channel: LONG live (T-037), SHORT live (T-085)
            symbol,
            best_prob,
            entry1,
            entry2,
            sl,
            targets,
            source_desc="AI EPD3 Pump/Dump retrain",
            n_show=3,
        )
        if outcome is not None:
            conn.commit()
    except Exception as e:
        logger.warning(f"EPD3 shadow for {symbol} failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def load_pump_model():
    """Loads the small, fast ML model (Cached for 1 hour)."""
    global _ml_model, _ml_model_time
    now = datetime.datetime.now(datetime.timezone.utc)

    if _ml_model is None or _ml_model_time is None or (now - _ml_model_time).total_seconds() > 3600:
        if os.path.exists(ML_MODEL_PATH):
            try:
                _ml_model = joblib.load(ML_MODEL_PATH)
                _ml_model_time = now
                logger.info(f"✅ ML model '{ML_MODEL_PATH}' for fast Pump/Dump Detector loaded")
            except Exception as e:
                logger.error(f"Error loading the ML model: {e}")
                _ml_model = None
        else:
            logger.warning(f"⚠️ Model {ML_MODEL_PATH} not found – waiting for training...")
            _ml_model = None
            _ml_model_time = now
    return _ml_model


# --- IN-MEMORY STATE ---
# Bucket history per coin (T-2026-CU-9050-165): the largest time window in the
# detector is 3600s (+20s tolerance) — on a dense 10s grid that's 362
# buckets. 720 covers 2h (double headroom for cadence gaps); the old
# 1440 (~4h) was never reached by any lookup (all windows are
# time-based and break off earlier) and only doubled the state dump & RAM.
BUCKET_DEQUE_MAXLEN = 720
ONE_MINUTE_DATA = {}
ROUND_BREAK_STATE = {}
PRICE_VOLUME_ALERT_STATE = {}
PUMP_DUMP_STATE = {}

# --- CACHE LOGIC FOR RESTARTS ---
DATA_FILE = "1minute.json"
STATE_FILE = "pump_dump_state.json"


def load_state_from_disk():
    """Loads historical 10s candles and cooldowns from disk at startup."""
    global ONE_MINUTE_DATA, PUMP_DUMP_STATE, PRICE_VOLUME_ALERT_STATE

    # 1. Load candles
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                raw_data = json.load(f)
            for symbol, entries in raw_data.items():
                dq = deque(maxlen=BUCKET_DEQUE_MAXLEN)
                for entry in entries[-BUCKET_DEQUE_MAXLEN:]:
                    # Fill epoch cache once at load (old files without
                    # an 'e' field) — otherwise the first tick pays the parse for the
                    # entire loaded history in the hot path.
                    if "e" not in entry:
                        ts = _parse_bucket_ts(entry)
                        if ts is not None:
                            entry["e"] = ts.timestamp()
                    dq.append(entry)
                ONE_MINUTE_DATA[symbol] = dq
            logger.info(f"✅ Cache: {len(ONE_MINUTE_DATA)} coins from {DATA_FILE} loaded.")
        except Exception as e:
            logger.error(f"Error loading from {DATA_FILE}: {e}")

    # 2. Load cooldowns and pump/dump state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                raw_state = json.load(f)

            for symbol, data in raw_state.items():
                PUMP_DUMP_STATE[symbol] = {
                    "avg_volume": float(data.get("avg_volume", 0)),
                    "last_alert_time": datetime.datetime.fromisoformat(data["last_alert_time"])
                    if data.get("last_alert_time")
                    else datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc),
                    "volume_samples": deque(data.get("volume_samples", [])[-360:], maxlen=360),
                }

                # Also restore the market-alert cooldown
                if data.get("pv_last_alert"):
                    PRICE_VOLUME_ALERT_STATE[symbol] = {
                        "last_alert_time": datetime.datetime.fromisoformat(data["pv_last_alert"])
                    }
            logger.info(f"✅ Cache: ML states from {STATE_FILE} loaded. (No blind phase!)")
        except Exception as e:
            logger.error(f"Error loading from {STATE_FILE}: {e}")


def save_state_to_disk():
    """Saves the current state bulletproof to disk (atomic write)."""
    try:
        # 1. Save candles (atomic write)
        # Compact instead of indent=2 (T-2026-CU-9050-165): the dump ran every 5min
        # over 527 coins × 720 buckets — with pretty-print >100MB and ~9s of pure
        # serialisation; compact keeps the file less than half the size and the
        # CPU spike accordingly shorter. No human reader, pure
        # restart cache.
        raw_data = {sym: list(dq) for sym, dq in ONE_MINUTE_DATA.items()}
        tmp_data_file = DATA_FILE + ".tmp"
        with open(tmp_data_file, "w", encoding="utf-8") as f:
            json.dump(raw_data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()  # Forces the OS to flush the buffer to disk
            os.fsync(f.fileno())
        # Atomically rename file (replaces old file immediately)
        os.replace(tmp_data_file, DATA_FILE)

        # 2. Save states (atomic write)
        save_state = {}
        for sym, state in PUMP_DUMP_STATE.items():
            pv_time = PRICE_VOLUME_ALERT_STATE.get(sym, {}).get(
                "last_alert_time", datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
            )

            save_state[sym] = {
                "avg_volume": state["avg_volume"],
                "last_alert_time": state["last_alert_time"].isoformat(),
                "pv_last_alert": pv_time.isoformat(),
                "volume_samples": list(state["volume_samples"]),
            }

        tmp_state_file = STATE_FILE + ".tmp"
        with open(tmp_state_file, "w", encoding="utf-8") as f:
            json.dump(save_state, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_state_file, STATE_FILE)

        logger.info("💾 state backup saved successfully (atomic).")
    except Exception as e:
        logger.error(f"Error saving the states: {e}")


# --- HELPER FUNCTIONS ---


def get_indicators_at_time(conn, coin):
    """Fetches the last CLOSED 1h indicators for the ML features.

    R1: include_forming=False — the detection must not calculate on the forming
    candle (previously: DESC LIMIT 1 without bound = partial indicators of the
    running candle).
    """
    try:
        df = read_indicators(
            conn,
            coin,
            "1h",
            limit=1,
            include_forming=False,
            columns=("open_time", "rsi_14", "tsi_fast_12_7_7", "macd_dif_normal_12_26_9", "ema_9", "ema_21"),
        )
        if not df.empty:
            return df.iloc[-1].drop("open_time").to_dict()
    except Exception:
        pass
    return None


def send_outbox(conn, channel, text, chart_path=None):
    """Pushes a message into the outbox."""
    try:
        with conn.cursor() as cur:
            if chart_path:
                cur.execute(
                    "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                    (channel, text, chart_path),
                )
            else:
                cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (channel, text))
        conn.commit()
    except Exception as e:
        logger.error(f"outbox error: {e}")
        conn.rollback()


# 1. ROUND LEVEL BREAKER
def check_round_levels(conn, symbol, current_price, prev_price):
    if symbol not in ROUND_LEVEL_CONFIG:
        return

    cfg = ROUND_LEVEL_CONFIG[symbol]
    step = cfg["step"]
    decimals = cfg["decimals"]

    prev_bucket = int(prev_price / step)
    curr_bucket = int(current_price / step)

    if prev_bucket == curr_bucket:
        return

    direction = "upwards" if current_price > prev_price else "downwards"
    crossed_level = curr_bucket * step if direction == "upwards" else prev_bucket * step

    # Cooldown Check
    state = ROUND_BREAK_STATE.get(symbol, {})
    if (
        state.get("last_level", 0) == crossed_level
        and (
            datetime.datetime.now(datetime.timezone.utc)
            - state.get("last_break_time", datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc))
        ).total_seconds()
        < 180
    ):
        return

    logger.info(f"🚧 ROUND LEVEL BREAK: {symbol} crossed {crossed_level} {direction}")

    html = f"""<pre><b>ROUND LEVEL BREAK</b>\n<b>{symbol.replace('USDT', '')}/USDT</b> breaks <b>{crossed_level:,.{decimals}f}</b> <b>{direction.upper()}</b>\n<b>→ Price:</b> <code>${current_price:,.{decimals}f}</code>\n<b>→ Time:</b> {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')} UTC\n</pre>"""

    chart_buf = generate_minichart_image(symbol, minutes=60)
    send_outbox(conn, MARKET_CHANNEL_ID, html, chart_buf)

    if (
        state.get("last_level", 0) == crossed_level
        and (
            datetime.datetime.now(datetime.timezone.utc)
            - state.get("last_break_time", datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc))
        ).total_seconds()
        < 600
    ):
        ROUND_BREAK_STATE[symbol] = {
            "last_level": crossed_level,
            "last_break_time": datetime.datetime.now(datetime.timezone.utc),
            "direction": direction,
        }
        return
    send_outbox(conn, SENTIMENT_CHANNEL_ID, html, chart_buf)

    ROUND_BREAK_STATE[symbol] = {
        "last_level": crossed_level,
        "last_break_time": datetime.datetime.now(datetime.timezone.utc),
        "direction": direction,
    }


def _parse_bucket_ts(entry: dict) -> datetime.datetime | None:
    """Parses the 't' timestamp of a bucket entry. None on error."""
    try:
        ts_str = entry.get("t", "")
        return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _bucket_epoch(entry: dict) -> float | None:
    """Epoch seconds of a bucket — the comparison key of all window lookups.

    New buckets carry 'e' from creation onward (main loop); old buckets from a
    1minute.json predating the field (and test fixtures) get parsed ONCE on first
    access and cached in-place. Previously fromisoformat ran fresh on EVERY
    comparison — at 527 coins × ≤MAXLEN buckets × ~10 scans/tick that was the
    dominant CPU cost of the entire bot (T-2026-CU-9050-165: 4.3s of the 10s
    tick budget just for the window scans; with epoch floats 1.2s). None on an
    unparsable timestamp — callers skip the bucket as before.
    """
    e = entry.get("e")
    if e is not None:
        return e
    ts = _parse_bucket_ts(entry)
    if ts is None:
        return None
    e = ts.timestamp()
    entry["e"] = e
    return e


def _anchor_epoch(anchor: "datetime.datetime | float") -> float:
    """Anchor (datetime or already an epoch float) → epoch seconds.

    No isinstance against datetime.datetime: the test suite freezes the class
    via MagicMock(wraps=...) — isinstance against the mock would raise TypeError,
    even though the passed-through instances are real datetimes. The check
    therefore runs against the builtins (float/int); anything else is a datetime.
    """
    if isinstance(anchor, (int, float)):
        return float(anchor)
    return anchor.timestamp()


def _find_bucket_before(
    data: list, now: "datetime.datetime | float", seconds_ago: int, tolerance: int = 20
) -> dict | None:
    """Finds the bucket that lies approximately `seconds_ago` seconds in the past.

    Walks the data from back to front and takes the first bucket whose timestamp
    falls inside the window [seconds_ago - tolerance, seconds_ago + tolerance].
    This makes the code robust against:
      - restarts with loaded old state history
      - gaps in the buckets (WebSocket outages etc.)
      - mixed data from different runs

    Returns None if no bucket was found in the desired time window — in that
    case the caller should skip the corresponding lookback comparison.

    tolerance=20 means: ±20 seconds tolerance. For 10s buckets that's plenty;
    larger tolerances would distort the percentage calculation.
    """
    if not data:
        return None

    target = _anchor_epoch(now) - seconds_ago

    # Walk from back to front — the newest matching bucket wins
    for entry in reversed(data):
        e = _bucket_epoch(entry)
        if e is None:
            continue
        if abs(e - target) <= tolerance:
            return entry
        # If we are already further in the past than target+tolerance,
        # there is no more match
        if e < target - tolerance:
            return None

    return None


def _find_bucket_nearest(
    data: list,
    anchor: "datetime.datetime | float",
    seconds_ago: int,
    min_age: int,
    max_age: int,
) -> tuple[dict, float] | None:
    """Bucket whose age is closest to `seconds_ago`, restricted to [min_age, max_age].

    Returns ``(entry, age_seconds)`` or ``None`` when the buffer holds no bucket
    in the admissible age band. `age_seconds` is the TRUE elapsed time between
    that bucket and `anchor` — callers normalise their rate with it instead of
    pretending the bucket sits exactly `seconds_ago` in the past.

    Why not `_find_bucket_before` with a wide tolerance: that one returns the
    NEWEST bucket inside the band and discards how old it really is. At a 70s
    cadence it hands back an 80s-old bucket and the caller labels the result
    "60s". Here the age travels with the bucket, so the caller can be honest.

    The band, not a symmetric tolerance, is what bounds the noise: a reference
    only 20s old would get its move scaled by 3x. `min_age` keeps the scale
    factor sane, `max_age` keeps the window recognisably short-term.
    """
    if not data:
        return None

    anchor_e = _anchor_epoch(anchor)
    best: tuple[dict, float] | None = None
    for entry in reversed(data):
        e = _bucket_epoch(entry)
        if e is None:
            continue
        age = anchor_e - e
        if age > max_age:
            break  # chronological: everything further back is older still
        if age < min_age:
            continue
        if best is None or abs(age - seconds_ago) < abs(best[1] - seconds_ago):
            best = (entry, age)
    return best


def _window_coverage_sec(buckets: list, anchor: "datetime.datetime | float") -> float:
    """How far back the oldest bucket of `buckets` actually reaches from `anchor`.

    A bucket COUNT says nothing about the span it covers once the cadence varies;
    this is the honest warmup signal for "do we have an hour of baseline yet".
    """
    anchor_e = _anchor_epoch(anchor)
    for entry in buckets:  # chronological, oldest first
        e = _bucket_epoch(entry)
        if e is not None:
            return anchor_e - e
    return 0.0


def _find_bucket_range(data: list, now: "datetime.datetime | float", seconds_ago: int, tolerance: int = 20) -> list:
    """Returns all buckets that lie in the range [now - seconds_ago, now].

    Robust against gaps and old state data — uses exclusively the
    timestamps of the buckets, not their position in the list.
    """
    if not data:
        return []

    cutoff = _anchor_epoch(now) - seconds_ago - tolerance
    result = []

    for entry in reversed(data):
        e = _bucket_epoch(entry)
        if e is None:
            continue
        if e < cutoff:
            break
        result.append(entry)

    return list(reversed(result))  # chronological again


def _scan_hour_and_lookbacks(
    data: list,
    anchor: "datetime.datetime | float",
    lookback_secs: "list[int]",
    hour_sec: int,
    hour_tol: int,
    lb_tol: int,
) -> "tuple[list, dict[int, dict | None]]":
    """ONE reverse pass instead of 1×_find_bucket_range(hour) + N×_find_bucket_before(lb).

    T-2026-KYT-9050-019: the hour scan (volume explosion + ML baseline) and the
    6 price-move lookbacks scan the same deque back to ~3600s every tick in the
    steady state (no alert) — together ~886 bucket iterations/coin/tick. This
    function folds both into one traversal (~362 iter). It is constructed to be
    BYTE-IDENTICAL to the individual calls (fuzz test test_scan_windows_matches_originals):

      - hour_buckets == _find_bucket_range(data, anchor, hour_sec, tolerance=hour_tol)
      - lb_refs[sb]  == _find_bucket_before(data, anchor, sb,       tolerance=lb_tol)
                        for each sb in lookback_secs

    Anchoring, None semantics (missing bucket ⇒ None ⇒ caller skips) and the
    "newest match in band" choice of _find_bucket_before remain unchanged.
    """
    anchor_e = _anchor_epoch(anchor)
    lb_pending = sorted(lookback_secs)  # ascending seconds = ascending target age
    lb_refs: dict[int, dict | None] = {sb: None for sb in lookback_secs}
    if not data:
        return [], lb_refs

    hour_cutoff = anchor_e - hour_sec - hour_tol
    hour_rev: list = []  # newest-first, flipped to chronological at the end
    hour_done = False
    lb_idx = 0

    for entry in reversed(data):
        e = _bucket_epoch(entry)
        if e is None:
            continue

        # (a) hour window: collect while e >= cutoff, then done (break-equivalent)
        if not hour_done:
            if e >= hour_cutoff:
                hour_rev.append(entry)
            else:
                hour_done = True

        # (b) lookback references: age increases monotonically with the scan. For
        #     the currently shallowest open band, this is exactly
        #     _find_bucket_before(tol=lb_tol):
        #       age < sb-tol  → not yet in band, this entry is too young → break
        #       age <= sb+tol → newest match, keep it and close the band
        #       age >  sb+tol → band skipped ⇒ None, close the band
        #     After a match/None, check the same entry against the next (deeper)
        #     band (continue), until it is too young (break).
        age = anchor_e - e
        while lb_idx < len(lb_pending):
            sb = lb_pending[lb_idx]
            if age < sb - lb_tol:
                break
            if age <= sb + lb_tol:
                lb_refs[sb] = entry
            # else: age > sb+tol → lb_refs[sb] stays None
            lb_idx += 1

        if hour_done and lb_idx >= len(lb_pending):
            break

    return list(reversed(hour_rev)), lb_refs


# (seconds_back, min_pct) per extreme-move lookback (10s buckets: 12/18/30/42/60/360
# buckets = 120…3600s). The seconds column is AT THE SAME TIME the lookback list for
# _scan_hour_and_lookbacks — one source, so the single-pass scan and the alert loop
# never drift apart.
PRICE_MOVE_LOOKBACKS = [(120, 3.0), (180, 4.0), (300, 5.0), (420, 7.5), (600, 10.0), (3600, 20.0)]
PRICE_MOVE_LOOKBACK_SECS = [sb for sb, _ in PRICE_MOVE_LOOKBACKS]


# 2. EXTREME MOVE & PUMP/DUMP DETECTOR
def process_coin_logics(conn, symbol):
    # T-2026-KYT-9050-019: read the deque directly instead of copying it into a
    # list per coin/tick. `data` is touched exclusively via `data[-1]` (O(1) at
    # the end) and `reversed(data)` (in the _find_bucket_* helpers), never
    # sliced — the deque supports both natively. The loop is single-threaded and
    # main() appends the fresh bucket BEFORE this call, so there is no
    # concurrent mutation that the snapshot copy would need to guard against.
    data = ONE_MINUTE_DATA[symbol]
    if len(data) < 36:
        return  # Need some history

    # Invariants of this function (P1.39 — please read before any "simplification"):
    #   1. Windows are selected via TIMESTAMPS, never via list indices.
    #      `data` can have gaps, and `volumes_10s`-style filtered lists
    #      have positions that don't correspond to any point in time.
    #   2. Every `_find_bucket_*` call anchors on `bucket_anchor`, never on `now`.
    #      Otherwise the window drifts with the call time (see below).
    #   3. `now` stays wall clock — the staleness check, the two alert cooldowns
    #      and `pump_dump_events.spike_time` MUST stay pinned to it.
    #   4. If a bucket is missing, the tick is skipped. NO substitute value
    #      (0, last price, …) is invented as a model feature.
    now = datetime.datetime.now(datetime.timezone.utc)
    current_price = float(data[-1]["p"])
    current_vol = float(data[-1]["v10s"])

    # STALE-DATA CHECK: the last bucket must be fresh (< 60s old).
    # After a restart, the history loaded from 1minute.json can be up to
    # 4 hours old. When new buckets then come in, the code must NOT treat
    # the old ones as "2 minutes ago". If the data is stale, we skip this
    # cycle — by the next tick the newest bucket will be fresh again.
    # P1.39: ALL bucket lookups anchor on the newest bucket timestamp,
    # not on the wall clock. The bucket stamps are floored to the 10s grid
    # (`_tick_epoch - _tick_epoch % 10`), `now` is the call time somewhere
    # inside the grid — and the detector iterates ~530 coins after a
    # REST round-trip, so the offset also drifts across the batch.
    # Measured against `now`, the 60s boundary would sometimes sit before, sometimes
    # behind the bucket from 60s ago: the window would flip between 6 and 7
    # buckets depending on the call time, and a model feature would jump
    # without the market moving. Measured against the bucket stamp, every
    # target time falls exactly on a grid point. `now` remains responsible for
    # everything wall-clock-like (staleness, alert gates, spike_time).
    #
    # T-2026-KYT-9050-019: the anchor is the cached epoch float (`_bucket_epoch`),
    # not `_parse_bucket_ts`. The newest bucket carries 'e' from creation in the
    # main loop, so the earlier ISO parse here re-read it needlessly per coin per
    # tick. T-165 had identified fromisoformat as the dominant CPU cost and had
    # already switched the window helpers to 'e' — this one anchor spot was
    # left over. A float anchor is also more robust: all _find_bucket_* helpers
    # normalise it via _anchor_epoch (no isinstance against datetime).
    bucket_anchor = _bucket_epoch(data[-1])
    if bucket_anchor is None:
        return

    latest_age_sec = now.timestamp() - bucket_anchor
    if latest_age_sec > 60:
        # Data gap too large — not meaningful for pump detection.
        # Can happen after a restart or after a WS outage.
        logger.debug(f"{symbol}: stale data ({latest_age_sec:.0f}s old), skipping")
        return

    # If the current volume measurement is invalid due to a 24h rollover,
    # we skip volume-based checks entirely (price check keeps running).
    current_vol_valid = bool(data[-1].get("v10s_valid", True))
    # P1.39: the flat `prices`/`volumes_10s` lists are gone. Every consumer now
    # gets its window via _find_bucket_before/_find_bucket_range from `data`
    # and filters `v10s_valid` itself — positions in a filtered list say nothing
    # about the time.

    # -- Initialize States --
    if symbol not in PRICE_VOLUME_ALERT_STATE:
        PRICE_VOLUME_ALERT_STATE[symbol] = {
            "last_alert_time": datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
        }
    if symbol not in PUMP_DUMP_STATE:
        PUMP_DUMP_STATE[symbol] = {
            "avg_volume": 0.0,
            "volume_samples": deque(maxlen=360),
            "last_alert_time": datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc),
        }

    pv_state = PRICE_VOLUME_ALERT_STATE[symbol]
    pd_state = PUMP_DUMP_STATE[symbol]
    # Only include valid volume measurements in the baseline samples.
    if current_vol_valid:
        pd_state["volume_samples"].append(current_vol)

    # Hour window + the 6 price-move lookbacks in ONE reverse pass
    # (T-2026-KYT-9050-019, builds on T-2026-CU-9050-165): volume explosion (A2),
    # the ML path (B) and the 6 _find_bucket_before lookbacks of the extreme-move loop
    # scanned the same deque back to ~3600s — in the steady state ~886 bucket
    # iterations/coin every tick. `_scan_hour_and_lookbacks` folds them into ~362 and
    # is byte-identical to the individual calls (fuzz pin test_scan_windows_matches_originals).
    hour_buckets, lb_refs = _scan_hour_and_lookbacks(
        data, bucket_anchor, PRICE_MOVE_LOOKBACK_SECS, 3600, WINDOW_EDGE_GUARD, 20
    )
    hour_vols = [float(e["v10s"]) for e in hour_buckets if e.get("v10s_valid", True)]
    hour_covered = _window_coverage_sec(hour_buckets, bucket_anchor)

    # A) EXTREME MOVE (Market Channel)
    if (now - pv_state["last_alert_time"]).total_seconds() >= 300:
        alerted = False
        use_ext_cooldown = False

        # 1. Price Move
        # IMPORTANT: the lookback is TIMESTAMP-based, not index-based.
        # `seconds_back` is the window distance in SECONDS (120…3600). After a
        # restart, the deque can mix old + new buckets — then data[-12] is
        # NO LONGER "120 seconds ago". So we look up the comparison bucket
        # by timestamp (here via lb_refs from the shared reverse pass).
        for seconds_back, min_pct in PRICE_MOVE_LOOKBACKS:
            # Reference bucket `seconds_back` seconds ago (±20s tolerance) — precomputed
            # in the shared reverse pass above (byte-identical to the earlier
            # _find_bucket_before(data, bucket_anchor, seconds_back, tolerance=20)).
            past_entry = lb_refs[seconds_back]
            if past_entry is None:
                # No bucket in the desired time window — either too little
                # history or a data gap. Skip this lookback.
                continue

            past_price = float(past_entry.get("p", 0))
            if past_price <= 0:
                continue

            chg_pct = (current_price / past_price - 1) * 100
            if abs(chg_pct) >= min_pct:
                direction = "PUMP" if chg_pct > 0 else "DUMP"
                mins, secs = divmod(seconds_back, 60)
                t_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

                # Dead-cat-bounce check: 10-minute trend vs. signal direction
                # Time-based: look up the bucket from 600s (= 10min) ago
                dead_cat = False
                chg_10m = None
                bucket_10m_ago = _find_bucket_before(data, bucket_anchor, 600, tolerance=30)
                if bucket_10m_ago is not None:
                    price_10m = float(bucket_10m_ago.get("p", 0))
                    if price_10m > 0:
                        chg_10m = (current_price / price_10m - 1) * 100
                        # PUMP with a negative 10m trend → dead-cat bounce
                        # DUMP with a positive 10m trend → short dip in an uptrend
                        if chg_pct > 0 and chg_10m < -1.0:
                            dead_cat = True
                        elif chg_pct < 0 and chg_10m > 1.0:
                            dead_cat = True

                # Find the spike region: start = most extreme point in the window,
                # end = current point in time (= last bucket).
                # For PUMP: start = lowest, end = current_high.
                # For DUMP: start = highest, end = current_low.
                # IMPORTANT: here too time-based instead of data[-lookback:], otherwise
                # the old bug is reproduced after a restart.
                spike_window = _find_bucket_range(data, bucket_anchor, seconds_back, tolerance=20)
                if not spike_window:
                    # No usable window — skip the alert
                    continue

                spike_prices = [float(e["p"]) for e in spike_window]
                if chg_pct > 0:
                    spike_idx = spike_prices.index(min(spike_prices))
                else:
                    spike_idx = spike_prices.index(max(spike_prices))

                # Parse out the timestamp of the spike-start bucket
                spike_start_dt = _parse_bucket_ts(spike_window[spike_idx])

                # Spike end: timestamp of the last bucket in the window (= now).
                spike_end_dt = _parse_bucket_ts(spike_window[-1])

                # Sanity check: spike_start must not lie before (now - 2*seconds_back).
                # If it does → data inconsistency, better post no spike label
                # than a wrong one.
                if spike_start_dt is not None:
                    age_sec = (now - spike_start_dt).total_seconds()
                    if age_sec > seconds_back * 2 or age_sec < 0:
                        logger.warning(
                            f"{symbol}: spike_start inconsistent "
                            f"(age={age_sec:.0f}s, expected ≤{seconds_back * 2}s) — "
                            f"label suppressed"
                        )
                        spike_start_dt = None

                # Combined start→end label for the caption
                # (matches the two vertical lines in the chart).
                if spike_start_dt is not None and spike_end_dt is not None:
                    spike_range_label = (
                        f"{spike_start_dt.strftime('%H:%M:%S')} → {spike_end_dt.strftime('%H:%M:%S')} UTC"
                    )
                elif spike_start_dt is not None:
                    spike_range_label = spike_start_dt.strftime('%H:%M:%S UTC')
                else:
                    spike_range_label = None

                # HTML caption extended: spike range + optional dead-cat warning + 10m trend
                extra_lines = ""
                if spike_range_label:
                    extra_lines += f'\n<b>→ Spike: {spike_range_label}</b>'
                if chg_10m is not None:
                    extra_lines += f'\n<b>→ 10m trend: <b>{chg_10m:+.2f}%</b></b>'
                if dead_cat:
                    extra_lines += (
                        '\n<b>⚠ ATTENTION: DEAD CAT BOUNCE (short bounce in a downtrend)</b>'
                        if chg_pct > 0
                        else '\n<b>⚠ ATTENTION: DIP IN UPTREND (short dip in an uptrend)</b>'
                    )

                html = (
                    f'<pre>'
                    f'<b>'
                    f'{"🚀" if chg_pct > 0 else "💥"} {direction} DETECTED</b>\n'
                    f'<b>{symbol.replace("USDT", "")}/USDT</b>\n'
                    f'<b>→ <b>{chg_pct:+.2f}% in {t_str}</b></b>\n'
                    f'<b>→ Price: <code>${current_price:,.8f}</code></b>'
                    f'{extra_lines}'
                    f'\n</pre>'
                )

                # Chart with spike region (start + end vertical lines
                # and shaded area in between)
                chart_path = generate_minichart_image(
                    symbol,
                    minutes=240,
                    spike_start=spike_start_dt,
                    spike_end=spike_end_dt,
                )
                send_outbox(conn, MARKET_CHANNEL_ID, html, chart_path)

                alerted = True
                if abs(chg_pct) >= 10.0:
                    use_ext_cooldown = True
                break

        # 2. Volume explosion
        #
        # P1.39: time-based instead of index-based. The old form had two
        # bugs: (a) `volumes_10s` is FILTERED on `v10s_valid`, `prices`
        # is not — `volumes_10s[-18:]` and `prices[-18:]` therefore pointed at
        # different points in time as soon as a single bucket was invalid;
        # (b) with WS gaps, 18 buckets are not 3 minutes and 360 not one
        # hour. Both silently shifted the window without anything standing out.
        if not alerted:
            rec_buckets = _find_bucket_range(data, bucket_anchor, 180, tolerance=WINDOW_EDGE_GUARD)
            rec_vols = [float(e["v10s"]) for e in rec_buckets if e.get("v10s_valid", True)]
            # Same band logic as the ML path below: a 3m reference that really is
            # ~3m old, carrying its true age, instead of demanding a grid point
            # that a 70s cadence never produces (T-2026-CU-9050-035).
            ref_3m = _find_bucket_nearest(data, bucket_anchor, 180, 150, 300)

            # Warmup gate: the hour window must COVER an hour and carry enough
            # samples — not contain 360 buckets, which no real cadence does.
            # hour_buckets/hour_vols/hour_covered: computed once per tick above.
            if (
                ref_3m is not None
                and rec_vols
                and len(hour_vols) >= HOUR_WINDOW_MIN_SAMPLES
                and hour_covered >= HOUR_WINDOW_MIN_COVERAGE_SEC
            ):
                bucket_3m, window_3m_sec = ref_3m
                price_3m = float(bucket_3m["p"])
                # Normalised to a per-180s rate, same reasoning as p_chg_60s.
                p_chg_3m = (current_price / price_3m - 1) * 100 * (180.0 / window_3m_sec) if price_3m > 0 else 0

                if abs(p_chg_3m) >= 2.0:
                    avg_hr_vol = sum(hour_vols) / len(hour_vols)
                    if avg_hr_vol > 0:
                        # Mean instead of sum/18: with gaps, rec_vols has
                        # fewer buckets, and /18 would have suppressed the factor.
                        vol_factor = (sum(rec_vols) / len(rec_vols)) / avg_hr_vol
                        if vol_factor >= 12.0:
                            pres = "BUY PRESSURE" if p_chg_3m >= 2.0 else "SELL PRESSURE"
                            # `%/3m` not `%`: p_chg_3m is a normalised rate, not the
                            # raw move over a window that may have been 4 minutes.
                            html = f"""<pre><b>📈 VOLUME EXPLOSION</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n<b>→ <b>{vol_factor:.1f}× in last 3min ({pres} {p_chg_3m:+.2f}%/3m)</b></b>\n<b>→ Price: <code>${current_price:,.8f}</code></b></pre>"""
                            send_outbox(conn, MARKET_CHANNEL_ID, html, generate_minichart_image(symbol, minutes=240))
                            alerted = True

        if alerted:
            pv_state["last_alert_time"] = now + datetime.timedelta(seconds=(900 - 300) if use_ext_cooldown else 0)

    # B) ML PUMP/DUMP DETECTOR (AI channel) - FAST 10 FEAT MODEL
    #
    # P1.39: the four model features below were calculated index-based
    # (`prices[-7:]` as "60s", the volume_samples deque as "1h"). With a
    # WS gap, "-7" did not mean 60 seconds — the model got a silently
    # stretched window. Now everything goes via _find_bucket_before/_find_bucket_range.
    #
    # CAUTION (OPUS-HANDOFF §4 trap 2): vol_ratio/p_chg_60s/buy_pres/volat are
    # model inputs AND are logged as such in pump_dump_events, from which
    # tools/epd2_build_dataset.py trains. This change shifts the serving
    # distribution against the currently deployed EPD2 artifact, until a
    # retrain on the new definitions is rolled out (operator decision
    # 2026-07-09, follow-up task). Gaps were rare before, but most likely
    # exactly at spike moments (WS load) — the old value was wrong there,
    # not just different.
    if len(pd_state["volume_samples"]) < 60:
        return

    # Baseline time-based: all valid buckets of the last hour. The deque
    # counts ticks, not seconds — after a gap it spanned more than
    # one hour. It now only remains a warmup gate and completeness feature.
    # hour_buckets/hour_vols/hour_covered: computed once per tick above
    # (tolerance=WINDOW_EDGE_GUARD, identical anchor as here).
    #
    # Same warmup floor as the Volume-Explosion path (T-2026-CU-9050-035). `not
    # hour_vols` alone let a single surviving bucket become the entire baseline
    # after a gap, and `vol_ratio = current_vol / avg_volume` is both a model
    # input AND the pump_dump_events insert gate below — a one-sample denominator
    # writes garbage events and scores the model out of distribution.
    if len(hour_vols) < HOUR_WINDOW_MIN_SAMPLES or hour_covered < HOUR_WINDOW_MIN_COVERAGE_SEC:
        return
    avg_volume = sum(hour_vols) / len(hour_vols)
    pd_state["avg_volume"] = avg_volume
    if avg_volume <= 0:
        return

    # 60s reference bucket, chosen by closest true age inside the admissible band
    # (T-2026-CU-9050-035). Still time-based, still anchored on `bucket_anchor`,
    # and still refusing to invent a value: if the buffer holds nothing between
    # 45s and 150s back, the tick is skipped exactly as before.
    ref = _find_bucket_nearest(
        data,
        bucket_anchor,
        P_CHG_WINDOW_TARGET_SEC,
        P_CHG_WINDOW_MIN_SEC,
        P_CHG_WINDOW_MAX_SEC,
    )
    if ref is None:
        return
    bucket_60s, window_sec = ref

    price_60s = float(bucket_60s["p"])
    if price_60s <= 0:
        return

    # buy_pres and volat describe the SAME span p_chg_60s is measured over —
    # `window_sec`, not a nominal 60. Three features that claim to describe one
    # window must actually share it; the P1.39 review found the opposite and that
    # is the bug class this whole task descends from.
    #
    # Note they are deliberately NOT rate-normalised: a fraction of up-moves and a
    # coefficient of variation are not per-second quantities. Their distribution
    # therefore depends on the cadence (a 45s window holds fewer diffs than a
    # 150s one). EPD3 is fitted on exactly this definition, so the dependency is
    # in-sample rather than a train/serve skew.
    window_60s = _find_bucket_range(data, bucket_anchor, int(window_sec), tolerance=WINDOW_EDGE_GUARD)
    rec_prices = [float(e["p"]) for e in window_60s]
    if len(rec_prices) < 2:
        return

    vol_ratio = current_vol / avg_volume
    # Normalise the observed move to a per-60s rate. On a dense grid window_sec
    # is 60 and this is the identity; on a stretched one it reports the rate the
    # window actually implies instead of silently over-reporting the move.
    p_chg_raw = (current_price / price_60s - 1) * 100
    p_chg_60s = p_chg_raw * (P_CHG_WINDOW_TARGET_SEC / window_sec)
    buy_pres = sum(1 for j in range(1, len(rec_prices)) if rec_prices[j] > rec_prices[j - 1]) / (len(rec_prices) - 1)
    volat = np.std(rec_prices) / np.mean(rec_prices) if np.mean(rec_prices) > 0 else 0

    # Display only (alert caption), not a model feature — a missing bucket
    # has no consequences here.
    bucket_5m = _find_bucket_before(data, bucket_anchor, 300, tolerance=30)
    change_5min = (current_price / float(bucket_5m["p"]) - 1) * 100 if bucket_5m and float(bucket_5m["p"]) > 0 else 0

    # Save event to DB — but ONLY if it would survive the housekeeping retention
    # (thresholds centralised in core/config.py, the retention DELETE in
    # 6_housekeeping.py uses the same values). Previously EVERY 10s tick per
    # symbol was written (~4.6M rows/day, the largest table in the DB) and
    # later deleted again for >99% — pure WAL/vacuum churn (P1.40).
    # Steady-state training data unchanged: the trainer only samples
    # vol_ratio >= 5, and rows below the gate would have been deleted by
    # housekeeping anyway before the next training run (only the
    # transient window until then is skipped).
    # CREATE TABLE has run once in main() since P1.40, no longer per tick.
    if vol_ratio >= _kcfg.PUMP_EVENT_MIN_VOL_RATIO and abs(p_chg_60s) >= _kcfg.PUMP_EVENT_MIN_ABS_PCHG_60S:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pump_dump_events (symbol, spike_time, volume_ratio, price_change_60s, buy_pressure, volatility) VALUES (%s, %s, %s, %s, %s, %s)",
                (symbol, now, float(vol_ratio), float(p_chg_60s), float(buy_pres), float(volat)),
            )
            conn.commit()

    if (now - pd_state["last_alert_time"]).total_seconds() < 900:
        return

    # FIX (Audit Report 13, EPD1-P0): the trainer exclusively samples events with
    # volume_ratio >= 5 — without this gate the model is queried out-of-distribution
    # on every 10s tick (calibration corr≈0). The gate mirrors the training.
    if vol_ratio < 5.0:
        return

    # Fetch model: EPD2 artifacts if deployed, otherwise the legacy model.
    # maybe_reload also runs over NOT loaded contracts — so the bot picks up
    # a later artifact deploy within the 24h window without a restart.
    for _d in list(_epd2):
        _epd2[_d] = maybe_reload(_epd2[_d], EPD_EXPECTED_FEATURES)
    epd2 = {d: a for d, a in _epd2.items() if a["loaded"]}
    model = None if epd2 else load_pump_model()
    if model is None and not epd2:
        return  # idle mode (trap 3)

    # Indicator fetch AFTER cooldown/vol_ratio/model gate (T-2026-CU-9050-014):
    # the values flow exclusively into features_array below. Previously the
    # query ran on EVERY 10s tick per symbol (~108 queries/s against the
    # *_indicators tables), even though >99% of ticks early-return at the gates
    # above. The pump_dump_events insert does not use indicators.
    inds = get_indicators_at_time(conn, symbol) or {}
    rsi = float(inds.get('rsi_14', 50))
    tsi = float(inds.get('tsi_fast_12_7_7', 0))
    macd = float(inds.get('macd_dif_normal_12_26_9', 0))
    ema9 = float(inds.get('ema_9', current_price))
    ema21 = float(inds.get('ema_21', current_price))
    e9_dist = (current_price - ema9) / ema9 * 100 if ema9 > 0 else 0
    e21_dist = (current_price - ema21) / ema21 * 100 if ema21 > 0 else 0

    # --- ML CHECK ---
    base_features = {
        "vol_ratio": vol_ratio,
        "p_chg_60s": p_chg_60s,
        "buy_pres": buy_pres,
        "volat": volat,
        "sample_fill": len(pd_state["volume_samples"]) / 360.0,
        "rsi": rsi,
        "tsi": tsi,
        "macd": macd,
        "e9_dist": e9_dist,
        "e21_dist": e21_dist,
    }

    # EPD3 shadow (T-2026-CU-9050-125): score the staging retrain in parallel +
    # track it under monitoring, independent of the live EPD path (no Cornix, tag EPD3).
    _emit_epd3_shadow(conn, symbol, base_features, now, current_price)

    try:
        if epd2:
            # EPD2: one binary model per direction. Funding features as-of NOW —
            # the dataset builder (tools/epd2_build_dataset.py:231) takes them as-of
            # the event timestamp, and the event is exactly this tick.
            #
            # The load is cached (funding_features_cached): the 900s timer does gate
            # this stretch, but is only set in the live-trade branch — a coin that
            # permanently predicts in the shadow band would otherwise pull the query
            # on EVERY 10s tick. The cache key comes from the data (until the
            # next settlement, which can change the result) and is therefore
            # value-neutral — see core/funding_features.next_feature_change.
            feats = {**base_features, **funding_features_cached(conn, symbol, now)}
            candidates = []
            for direction, art in epd2.items():
                # Missing funding HISTORY ⇒ columns missing ⇒ 0 here, like fillna(0)
                # in the trainer (train_binary). This is serving parity, not a P0.12
                # breach: load_artifact hard-validated the feature NAMES.
                ml_input = pd.DataFrame([feats]).reindex(columns=art["features"]).fillna(0)
                candidates.append((float(art["model"].predict_proba(ml_input)[0, 1]), direction, art))
            best_prob, best_direction, best_art = max(candidates, key=lambda c: c[0])
            module_tag = best_art["tag"]
            post_threshold = float(best_art["threshold"])
        else:
            # LEGACY: ONE 3-class model, positional feature array.
            # T-2026-CU-9050-060 (F3): post-P1.13 a young coin's warmup rows read
            # rsi_14 = NaN where the engine previously fabricated 50. The legacy
            # pkl is an XGBClassifier, and XGBoost does NOT raise on NaN — it
            # routes NaN down untrained default branches and scores an input the
            # trainer never produced (verified against the production pickle).
            # Impute per the legacy trainer's own NULL contract instead
            # (legacy_trainers/zzz.py:7609-7617: rsi -> 50, everything else -> 0;
            # the ema-dists collapse to 0 there via ema := price): train/serve
            # parity, same principle as the EPD2 branch's fillna(0) — whose 0 is
            # ITS trainer's contract (train_binary) and stays untouched above.
            # Serving values are identical to what this model saw its whole
            # pre-P1.13 life, so live semantics do not change.
            imputed = {c: (v if np.isfinite(v) else (50.0 if c == "rsi" else 0.0)) for c, v in base_features.items()}
            features_array = np.array([[imputed[c] for c in EPD_BASE_FEATURES]])
            prob = model.predict_proba(features_array)[0]
            classes = list(model.classes_)

            prob_dump = prob[classes.index(0)] if 0 in classes else 0
            prob_pump = prob[classes.index(2)] if 2 in classes else 0

            best_prob = max(prob_pump, prob_dump)
            best_direction = "LONG" if prob_pump >= prob_dump else "SHORT"
            module_tag = EPD_LEGACY_TAG
            post_threshold = EPD_LEGACY_THRESHOLD
    except Exception as e:
        logger.error(f"Prediction error in HF loop: {e}")
        return

    # Active trade check (T-2026-CU-9050-055) — checks whether a non-closed trade
    # is already running for exactly this module/coin/direction.
    # The 900s timer above is a FREQUENCY lock and lives only in memory; an
    # EPD trade regularly runs longer, and without this check the follow-up
    # signal would open a SECOND live position next to the first. Pattern:
    # 11_ai_mis_bot.py:318. The check runs AFTER the prediction, because the
    # direction is only established from the argmax (operator decision 2026-07-10:
    # symbol+direction like the siblings, no direction-agnostic key).
    # It deliberately locks the shadow log too — as with MIS/RUB, the trade is
    # what counts, not the row.
    #
    # The tag is also the dedupe key and flips on the EPD3 rollout; without the
    # old tag in the IN, an open EPD2 position would no longer block the EPD3 signal.
    # As long as the tags match (today), the IN is a no-op.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM ai_signals
            WHERE symbol = %s AND direction = %s AND model IN (%s, %s)
        """,
            (symbol, best_direction, module_tag, EPD_LEGACY_TAG),
        )
        if cur.fetchone():
            return  # trade is running live in the AI monitor

    # === APPLY LOGIC ===
    # EPD2 (operator 2026-07-06): direction gate removed — both sides trade
    # again (intent: momentum riding in both directions).
    #
    # module_tag and post_threshold come from the prediction block above: from
    # the deployed artifact's meta (model_id / optimal_threshold) if present,
    # otherwise from the legacy constants. Never a constant over a loaded
    # artifact — otherwise an EPD3 retrain would silently merge with the
    # EPD2 statistics (rule 6).

    if best_prob < EPD_SHADOW_THRESHOLD:
        pass  # ignore junk

    elif EPD_SHADOW_THRESHOLD <= best_prob < post_threshold:
        # shadow mode: log to the master table.
        #
        # P1.41: the 900s gate above (`last_alert_time`) does NOT throttle this
        # branch — the timer is reset only in the live-trade branch below. A coin
        # that permanently predicts in the shadow band (0.25..0.60) would thus
        # fire an INSERT on EVERY qualifying 10s tick (up to 8640 rows/day/symbol).
        # Setting the timer here too would be wrong: that would also suppress
        # genuine live signals for the same coin for 900s. Instead log_prediction
        # dedupes the shadow rows itself (4h per module/coin/direction) — the same
        # path bots 30-33 use. Commit stays with the caller (hard rule 8).
        log_prediction(
            conn,
            module_tag,
            symbol,
            best_direction,
            float(current_price),
            float(best_prob),
            posted=False,
            # The dedup key is the tag. It flips on the EPD3 rollout, and the new
            # generation would start its 4h window at zero → duplicate shadow rows
            # for the same coin. Same tag (today) ⇒ no-op.
            legacy_tag=EPD_LEGACY_TAG,
        )
        conn.commit()

    elif best_prob >= post_threshold:
        # Direction gate REMOVED (operator 2026-07-06): both directions trade
        # again (the audit batch had put LONG into shadow after report 14 D.5).

        # 🔥 BINGO! Execute the trade
        emoji = "🚀 EARLY PUMP DETECTION" if best_direction == "LONG" else "💥 EARLY DUMP ALERT"

        is_long = best_direction == "LONG"
        entry1 = current_price
        entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
        supps, resis = get_hvn_and_sr_levels(conn, symbol, current_price)

        if is_long:
            sl = (
                max([x for x in supps if x < entry2 * 0.99])
                if any(x < entry2 * 0.99 for x in supps)
                else entry2 * 0.975
            )
            t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
        else:
            sl = (
                min([x for x in resis if x > entry2 * 1.01])
                if any(x > entry2 * 1.01 for x in resis)
                else entry2 * 1.025
            )
            t_cands = sorted([x for x in supps if x > 0 and x < (entry1 * 0.99)], reverse=True)

        # FIX: real zones + a 5% target if applicable when the last zone is too close
        targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)

        # T-2026-KYT-9050-033 (audit T-032): fleet lifecycle gate for the legacy EPD2
        # direct-post leg. Default LIVE ⇒ no behaviour change. EPD2 is parked in BOTH
        # directions → SHADOW (monitored trade instead of Cornix); the EPD3 retrain
        # (both directions LIVE since T-037/T-085) runs separately via
        # _emit_epd3_shadow/post_ai_signal_gated. Purely additive on the post branch (rule 4).
        # n_show=len(targets): the legacy EPD2 LIVE path stores the FULL target list
        # in ai_signals (json.dumps(targets), Cornix shows only [:3]) — the parked
        # shadow mirrors that for audit continuity with the historical EPD2 series.
        _route = route_legacy_leg(
            conn, module_tag, best_direction, symbol, best_prob, entry1, entry2, sl, targets, n_show=len(targets)
        )
        if _route != LEG_LIVE:
            if _route == LEG_SHADOW:
                conn.commit()
            return

        lev = get_max_leverage(symbol, 20)

        lines = [
            f"📈 Signal for {symbol} 📈",
            f"🚨 Direction: {best_direction}",
            f"🚨 Leverage: {lev}",
            "🚨 Margin: Cross",
            f"🏦 CMP Entry: $ {entry1:.8f}",
            # T-2026-KYT-9050-042: entry2 is still computed and stored, but no longer
            # published — the fleet trades single-entry (arm B). See core/signal_post.py.
        ]
        for i, t in enumerate(targets[:3], 1):
            lines.append(f"💰 TP{i}: $ {t:.8f}")
        lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module_tag}"]
        cornix_msg = "\n".join(lines)

        html_caption = f"""<pre><b>{emoji}</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n<b>→ Direction: <b>{best_direction}</b></b>\n<b>→ Price: <code>${current_price:,.8f}</code> <b>({change_5min:+.2f}% / 5m)</b></b>\n<b>→ Volume: <b>{vol_ratio:.1f}×</b> above avg</b>\n<b>→ ML-Confidence: <b>{best_prob:.1%}</b> / Module: {module_tag} V3</b>\n<b>→ Time: {now.strftime('%H:%M')} UTC</b>\n\n{cornix_msg}</pre>"""

        chart_buf = generate_minichart_image(symbol, minutes=240)
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
                    "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (AI_CHANNEL_ID, html_caption)
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
                    float(best_prob),
                    float(entry1),
                    float(entry2),
                    float(sl),
                    json.dumps(targets),
                ),
            )
            # Archive in the prediction master for live trades too!
            cur.execute(
                """INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted) VALUES (0, %s, %s, %s, %s, %s, %s, True)""",
                (module_tag, now, symbol, best_direction, float(current_price), float(best_prob)),
            )
        conn.commit()

        logger.info(f"🤖 AI-Trade gesendet: {symbol} {best_direction} via {module_tag} (Conf: {best_prob:.1%})")
        pd_state["last_alert_time"] = now


# MAIN LOOP
def main():
    logger.info("=== 🏎️ 10-SEC HIGH FREQUENCY DETECTOR STARTED ===")

    # Check retrain artifacts once at startup. None there → legacy path
    # (today's state); the artifacts win as soon as they are deployed.
    loaded = load_epd2_artifacts()
    logger.info(
        f"EPD artifacts: {sorted(loaded)} loaded"
        if loaded
        else f"No EPD2 artifacts — legacy model '{ML_MODEL_PATH}' under tag {EPD_LEGACY_TAG}."
    )

    session = requests.Session()
    conn = get_db_connection()

    # 💥 THE FIX: enable autocommit so a small error doesn't take down the whole bot!
    conn.autocommit = True

    # P1.40: create the table ONCE at startup instead of per symbol per 10s tick
    # (previously ~108 CREATE-IF-NOT-EXISTS statements/second against the catalog).
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS pump_dump_events (symbol VARCHAR(20), spike_time TIMESTAMP, volume_ratio REAL, price_change_60s REAL, buy_pressure REAL, volatility REAL, rsi_14 REAL, tsi REAL, macd_dif REAL, ema9_distance_pct REAL, ema21_distance_pct REAL)"""
        )

    # 10s ticker persistence (hypertable) — ensure the schema once at startup.
    # If that fails (e.g. extension missing), the detector keeps running WITHOUT
    # persistence — pump detection is the primary job, not data collection.
    ticker_10s_ok = False
    if TICKER_10S_PERSIST:
        try:
            ticker_10s.ensure_schema(conn)
            ticker_10s_ok = True
        except Exception as e:
            logger.error(f"❌ ticker_10s schema not available — persistence disabled: {e}")

    # 1. Load state and cache (no more cold-start blindness!)
    load_state_from_disk()

    # 2. Initial model load
    load_pump_model()

    # 3. Load coins
    try:
        with open("coins.json") as f:
            coins = json.load(f)
            logger.info(f"✅ {len(coins)} coins from coins.json loaded.")
    except Exception as e:
        logger.error(f"❌ Error loading from coins.json: {e}")
        return

    last_save_time = time.time()

    try:
        while True:
            try:
                now = datetime.datetime.now(datetime.timezone.utc)

                # Save state every 5 minutes
                if time.time() - last_save_time > 300:
                    save_state_to_disk()
                    last_save_time = time.time()

                # Timing: sync exactly to the 10-second mark
                seconds = now.second
                sleep_time = (10 - seconds % 10) if seconds % 10 != 0 else 10
                time.sleep(sleep_time)

                res = session.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=5)
                if res.status_code != 200:
                    continue
                raw_data = res.json()

                # Floored to the 10s mark: only this way does a second writer
                # (double start) produce IDENTICAL (symbol, ts) keys, and ON CONFLICT
                # DO NOTHING against uq_ticker_10s_symbol_ts actually dedupes —
                # raw now() stamps never collide due to µs jitter. The price is
                # then "sample shortly after the mark", acceptable on a 10s grid.
                _tick_epoch = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
                tick_dt = datetime.datetime.fromtimestamp(_tick_epoch - _tick_epoch % 10, tz=datetime.timezone.utc)
                ts_str = tick_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                tick_rows = []  # (ts, symbol, price, vol_10s, vol_valid) for ticker_10s

                for item in raw_data:
                    symbol = item["symbol"]
                    if symbol not in coins:
                        continue

                    price = float(item["lastPrice"])
                    cum_vol = float(item["volume"])

                    # /ticker/24hr returns a rolling 24h volume, NOT a monotonic
                    # cumulative. On rollover (old trades fall out of the 24h window)
                    # cum_vol can get smaller → delta negative. In that case the
                    # delta is not meaningful and we mark it invalid
                    # (v10s_valid=False), so pump detection ignores this measurement.
                    prev_vol = (
                        ONE_MINUTE_DATA[symbol][-1]["cum_vol"]
                        if symbol in ONE_MINUTE_DATA and ONE_MINUTE_DATA[symbol]
                        else None
                    )
                    if prev_vol is None:
                        v10s = 0.0
                        v10s_valid = False  # first data point — no delta possible
                    else:
                        raw_delta = cum_vol - prev_vol
                        if raw_delta < 0:
                            # 24h rollover: measurement not usable
                            v10s = 0.0
                            v10s_valid = False
                        else:
                            v10s = raw_delta
                            v10s_valid = True

                    # 'e' = epoch seconds of the grid stamp — comparison key
                    # of the window lookups (_bucket_epoch), 't' remains the
                    # ISO format for dump readability and legacy consumers.
                    entry = {
                        "t": ts_str,
                        "e": tick_dt.timestamp(),
                        "p": price,
                        "v10s": v10s,
                        "v10s_valid": v10s_valid,
                        "cum_vol": cum_vol,
                    }
                    if ticker_10s_ok:
                        # Also persist v10s_valid=False (rollover marker) —
                        # the builder filters itself, just like process_coin_logics.
                        tick_rows.append((tick_dt, symbol, price, v10s, v10s_valid))

                    if symbol not in ONE_MINUTE_DATA:
                        ONE_MINUTE_DATA[symbol] = deque(maxlen=BUCKET_DEQUE_MAXLEN)

                    if len(ONE_MINUTE_DATA[symbol]) > 0:
                        prev_price = ONE_MINUTE_DATA[symbol][-1]["p"]
                        check_round_levels(conn, symbol, price, prev_price)

                    ONE_MINUTE_DATA[symbol].append(entry)
                    process_coin_logics(conn, symbol)

                # ONE batched insert per tick (all coins) — never stop the loop,
                # a lost tick is acceptable, a dead detector is not.
                if tick_rows:
                    try:
                        ticker_10s.insert_ticks(conn, tick_rows)
                    except Exception as e:
                        logger.error(f"ticker_10s insert failed (tick discarded): {e}")

            except Exception as e:
                logger.error(f"HF loop error: {e}")
                # Review fix (PR #9): without a rollback, the connection stays in
                # InFailedSqlTransaction after a DB error and EVERY following
                # insert (pump_dump_events, outbox, ticker_10s) fails —
                # the loop would keep running but be functionally dead. Same
                # pattern as send_outbox.
                try:
                    conn.rollback()
                except Exception:
                    pass
                time.sleep(5)

    except KeyboardInterrupt:
        # Catches Ctrl+C when it happens during time.sleep() in the loop
        logger.info("🛑 Shutdown signal (Ctrl+C) received in loop!")
    finally:
        # Always executed when the while loop is left
        if conn:
            conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Bot manually stopped (Ctrl+C). Saving data...")
    finally:
        # This here is the absolute life insurance:
        # No matter WHERE the bot crashes or is aborted, it saves the data!
        save_state_to_disk()
        logger.info("✅ Cache successfully saved. Shutting down cleanly.")

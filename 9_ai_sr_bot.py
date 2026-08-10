import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import time

import numpy as np
import pandas as pd

from core import config as _kcfg  # channel ids
from core import shadow_gate
from core.candles import read_indicators
from core.charting import generate_minichart_image

# --- CORE IMPORTS ---
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.model_artifacts import load_artifact_json, maybe_reload
from core.signal_post import LEG_LIVE, LEG_SHADOW, has_open_ai_signal, post_ai_signal_gated, route_legacy_leg
from core.sra_features import SRA2_FEATURES, build_sra2_features
from core.trade_utils import N_PUBLISHED_TARGETS, ensure_min_tp_distance, get_hvn_and_sr_levels, thin_targets

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_SR_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- MODEL ARTIFACTS (native XGB-JSON + optional metadata/calib sidecars) ---
#
# The filenames are generation-free SLOTS — the operator copies the promoted
# artifact here. The generation lives exclusively in the sidecar metadata
# (`*_meta.json` → model_id), never in the filename and never in a source-code
# constant (hard rule 6, T-2026-CU-9050-042). An SRA3 retrain thus posts as
# SRA3, instead of silently blending with SRA2 in the same per-bot statistics
# on which orchestrator gating decides.
SRA_ARTIFACT_PATHS = {
    'LONG': "trade_success_xgb_LONG_v2.json",
    'SHORT': "trade_success_xgb_SHORT_v2.json",
}
# The tag under which this bot previously posted BOTH directions before T-2026-CU-9050-042.
# Two roles: default_tag for today's legacy artifact (no metadata ⇒ no
# model_id) AND transitional dedup key — see check_cooldown/deduplication.
SRA_LEGACY_TAG = 'SRA1'
# Posting threshold of the legacy model. An artifact with metadata brings its own
# (optimal_threshold from the validation slice) and overrides it.
# 0.65→0.70 (T-2026-CU-9050-171): on the realised SRA1 trades (n=748,
# 03–07/2026) the segment 0.65–0.70 is net negative (Ø −0.10 %); from 0.70
# 62 % of the trades remain with MORE total PnL (302 vs. 274) and WR 52→55.5 %.
SRA_LEGACY_THRESHOLD = 0.70
# Shadow log floor: everything above it lands in ml_predictions_master.
SRA_SHADOW_THRESHOLD = 0.35

MODELS: dict[str, dict] = {}

# SRA2 shadow (T-2026-CU-9050-125): the SRA2 retrain was "not deployable" BECAUSE
# the label source closed_trades3 is dead — a pure TRAINING problem. Shadow
# serving circumvents this: it scores the live S/R candidate stream via the shared
# core.sra_features builder + the staging artifact and lets the AI monitor collect
# fresh outcomes (closed_ai_signals) — exactly the labels the dead tracker no
# longer provides. SRA1 remains untouched live.
SHADOW_SRA2: dict[str, object | None] = {"LONG": None, "SHORT": None}


def sra_expected_features() -> list[str]:
    """The feature names this bot CAN build — legacy vector ∪ SRA2 contract.

    The hard feature contract (P0.12): if an artifact requires a column that is
    missing here, it will not be loaded and the bot idles — never fillna(0),
    because the model would never have seen this column as 0 in training. The
    list is DERIVED from the builders, not written separately (two lists drift).
    """
    dummy: dict = dict.fromkeys(SRA_INDICATOR_COLS, 1.0)
    dummy['trend_direction'] = 'UP'
    legacy = create_feature_row('LONG', dummy) or {}
    return list(dict.fromkeys([*legacy.keys(), *SRA2_FEATURES]))


def build_serving_row(direction, indicators) -> dict | None:
    """The feature frame for an artifact WITH metadata.

    Caution: shared column names with DIFFERENT semantics: the old bot vector
    computes ``pct_*`` against the close, the SRA2 builder against the reference
    value (ema9, wma9, …). The SRA2 builder therefore deliberately wins (on the
    right in the merge) — it defines the semantics on which the artifact was
    trained. The legacy path below never touches this frame.
    """
    legacy = create_feature_row(direction, indicators)
    if not legacy:
        return None
    return {**legacy, **build_sra2_features(indicators)}


def load_models() -> None:
    """Loads both direction artifacts. If one is missing, only that direction
    idles (trap 3: a bot without an artifact starts and does nothing instead of
    falling into the watchdog restart loop — previously this was an `exit(1)`)."""
    expected = sra_expected_features()
    for direction, path in SRA_ARTIFACT_PATHS.items():
        MODELS[direction] = load_artifact_json(path, expected, SRA_LEGACY_TAG, SRA_LEGACY_THRESHOLD)
    if not any(a['loaded'] for a in MODELS.values()):
        logger.error("❌ No SRA artifact loadable — bot runs in idle mode until deploy.")

    # SRA2 shadow models from staging_models/ fail-soft reload.
    for direction in ("LONG", "SHORT"):
        SHADOW_SRA2[direction] = shadow_gate.load_shadow_artifact("SRA2", direction)
    if any(SHADOW_SRA2.values()):
        loaded = [d for d, m in SHADOW_SRA2.items() if m is not None]
        logger.info(f"👻 SRA2 shadow models loaded: {', '.join(loaded)}")


def _emit_max2(conn, coin, prob, entry1, entry2, sl, targets) -> None:
    """MAX2 (T-2026-KYT-9050-020): the SRA2 LONG trade into the main channel.

    MAX2 is NOT its own model and not its own process — it is a fork of the
    SRA2 LONG emission (call from _emit_sra2_shadow): when SRA2 LONG fires
    (prob>=threshold) for a coin from config.MAIN_CHANNEL_COINS, the SAME
    trade (same prob + entry/SL/target geometry) is additionally emitted under
    tag "MAX2" to CH_MAIN. Replaces the retired classic "main channel"
    detector (3_detectors.py), which ran on exactly this coin whitelist — the
    only filter is the whitelist, exactly as before (operator decision Michi).

    LONG-only: SRA2 SHORT is a dead shadow leg (threshold=None, labels dead
    since 23.02). MAX2 posts default-LIVE (leg_status("MAX2","LONG")=LIVE) —
    this is collision-free with the SRA2 post to CH_AI_SR, BECAUSE CH_AI_SR is
    NOT Cornix-executed (informational/orchestrator, operator-confirmed);
    otherwise it would be a rule-4 double trade across 37 coins. Own tag ⇒
    own cooldown/dedup namespace via has_open("MAX2") (rule 6). Errors remain
    encapsulated; the already-committed SRA2 post is untouched (own txn, own
    rollback).
    """
    if not shadow_gate.shadow_posting_enabled() or shadow_gate.leg_status("MAX2", "LONG") not in (
        shadow_gate.LIVE,
        shadow_gate.SHADOW,
    ):
        return
    try:
        if has_open_ai_signal(conn, coin, "LONG", "MAX2"):
            return
        outcome = post_ai_signal_gated(
            conn,
            "MAX2",
            "LONG",
            _kcfg.CH_MAIN,
            coin,
            prob,
            entry1,
            entry2,
            sl,
            targets,
            source_desc="AI MAX2 (SRA2 S/R, Main-Channel-Filter)",
            n_show=3,
        )
        if outcome is not None:
            conn.commit()
    except Exception as e:
        logger.warning(f"MAX2 for {coin} LONG failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def _emit_sra2_shadow(conn, coin, direction, t_time, live_price) -> None:
    """SRA2 emission via the shadow_gate routing (T-2026-CU-9050-125/185).

    Scores the same S/R candidates as the live SRA1 path via the shared
    core.sra_features builder + the SRA2 artifact and emits on prob>=threshold
    via post_ai_signal_gated: the LIVE leg SRA2 LONG (@0.6424, T-185, artifact
    in repo root) emits a Cornix-format message to CH_AI_SR (coexisting with
    SRA1) — Cornix is NOT subscribed on CH_AI_SR (informational/orchestrator),
    which is why the MAX2 fork can collision-free mirror the same trade to
    CH_MAIN (see _emit_max2). The SHADOW leg SRA2 SHORT (threshold=None,
    staging) remains a monitored shadow trade (no Cornix). Geometry = the same
    HVN/S-R construction as process_ai_trade (deliberately duplicated to avoid
    touching the live path). Errors remain encapsulated.
    """
    if not shadow_gate.shadow_posting_enabled() or shadow_gate.leg_status("SRA2", direction) not in (
        shadow_gate.LIVE,
        shadow_gate.SHADOW,
    ):
        return
    art = SHADOW_SRA2.get(direction)
    if art is None:
        return
    try:
        inds = get_indicators_at_time(conn, coin, t_time)
        if not inds:
            return
        serving = build_serving_row(direction, inds)
        if not serving:
            return
        prob = shadow_gate.score_artifact(art, serving)
        thr = shadow_gate.artifact_threshold(art)
        if thr is not None and prob < thr:
            return
        # Deduplication guard for the LIVE leg (SRA2 LONG, T-185): post_ai_signal (live)
        # does NO has_open check — only post_shadow_ai_signal did that internally.
        # So explicitly check here before the expensive geometry (like bot 10).
        if has_open_ai_signal(conn, coin, direction, "SRA2"):
            return
        is_long = direction == "LONG"
        entry1 = float(live_price)
        entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
        supps, resis = get_hvn_and_sr_levels(conn, coin, live_price)
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
        targets = ensure_min_tp_distance(
            thin_targets(t_cands[:20], entry1, is_long, keep=N_PUBLISHED_TARGETS),
            entry1,
            is_long,
            min_pct=0.05,
        )
        if not targets:
            return
        outcome = post_ai_signal_gated(
            conn,
            "SRA2",
            direction,
            _kcfg.CH_AI_SR,  # LIVE leg SRA2 LONG → SRA channel (T-185); SHORT remains shadow
            coin,
            prob,
            entry1,
            entry2,
            sl,
            targets,
            source_desc="AI SRA2 S/R Meta-Filter",
            n_show=3,
        )
        if outcome is not None:
            conn.commit()
        # MAX2 (T-2026-KYT-9050-020): fork the same SRA2 LONG trade coin-filtered into
        # the main channel — replaces the retired classic main-channel bot that ran
        # on exactly MAIN_CHANNEL_COINS. LONG only (SRA2 SHORT is dead).
        if direction == "LONG" and coin in _kcfg.MAIN_CHANNEL_COINS:
            _emit_max2(conn, coin, prob, entry1, entry2, sl, targets)
    except Exception as e:
        logger.warning(f"SRA2 shadow for {coin} {direction} failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


# FEATURE & INDICATOR HELPERS


def get_indicators_at_time(conn, coin, timestamp):
    """Fetches the 1h indicators for the last CLOSED candle <= timestamp.

    R1: include_forming=False — features/detection never run on the forming
    candle. If a trade fired mid-current-hour, `<= timestamp` would otherwise
    deliver the partial indicators of that hour.
    """
    try:
        df = read_indicators(conn, coin, "1h", limit=1, end=timestamp, include_forming=False)
        if df.empty:
            return None
        return df.iloc[-1].to_dict()
    except Exception as e:
        logger.debug(f"indicators DB error for {coin}: {e}")
        return None


# Raw indicator columns that create_feature_row takes over 1:1. Module constant
# so that sra_expected_features() can derive the feature contract FROM the builder
# instead of writing it separately (two lists drift apart).
SRA_INDICATOR_COLS = [
    'rsi_9',
    'rsi_14',
    'rsi_24',
    'macd_dif_fast_9_21_9',
    'macd_dea_fast_9_21_9',
    'tsi_fast_12_7_7',
    'tsi_fast_12_7_7_signal',
    'atr_14',
    'r_squared',
    'boll_upper_20',
    'boll_mid_20',
    'boll_lower_20',
    'donchian_upper_20',
    'donchian_lower_20',
    'donchian_mid_20',
    'support_price',
    'resistance_price',
    'ema_9',
    'ema_21',
    'wma_9',
    'wma_21',
    'kama_9',
    'kama_21',
    'close',
]


def create_feature_row(direction, indicators):
    """Creates the feature dict for XGBoost based on your model logic."""
    close = indicators.get('close', np.nan)
    if pd.isna(close) or close <= 0:
        return None

    features = {}
    base_cols = SRA_INDICATOR_COLS

    for col in base_cols:
        val = indicators.get(col)
        features[col] = float(val) if pd.notna(val) else np.nan

    trend_map = {'UP': 1.0, 'DOWN': -1.0, 'FLAT': 0.0, 'SIDEWAYS': 0.0}
    features['trend_direction_num'] = trend_map.get(str(indicators.get('trend_direction', '')).upper(), 0.0)

    def pct(a, b):
        return (a - b) / close * 100 if pd.notna(b) and close > 0 else np.nan

    features.update(
        {
            'pct_ema9': pct(close, indicators.get('ema_9')),
            'pct_ema21': pct(close, indicators.get('ema_21')),
            'pct_wma9': pct(close, indicators.get('wma_9')),
            'pct_kama9': pct(close, indicators.get('kama_9')),
            'pct_support': pct(close, indicators.get('support_price')),
            'pct_resist': pct(indicators.get('resistance_price'), close),
            'pct_boll_mid': pct(close, indicators.get('boll_mid_20')),
            'ema9_ema21_pct': pct(indicators.get('ema_9'), indicators.get('ema_21')),
            'kama9_kama21_pct': pct(indicators.get('kama_9'), indicators.get('kama_21')),
        }
    )

    atr = indicators.get('atr_14', np.nan)
    # FIX P1.20: ATR features ALWAYS emit — if ATR is missing, the feature
    # vector had 35 instead of 38 columns, predict_proba threw and the entire
    # scan iteration broke. XGBoost can handle NaN natively.
    if pd.notna(atr) and atr > 0:
        features.update(
            {
                'support_atr': (close - indicators.get('support_price', np.nan)) / atr,
                'resist_atr': (indicators.get('resistance_price', np.nan) - close) / atr,
                'boll_width_atr': ((indicators.get('boll_upper_20', 0) - indicators.get('boll_lower_20', 0)) / atr),
            }
        )
    else:
        features.update({'support_atr': np.nan, 'resist_atr': np.nan, 'boll_width_atr': np.nan})

    features['is_long'] = 1.0 if direction.upper() == 'LONG' else 0.0
    return features


# TARGET CALCULATOR

# POSTING LOGIC


def process_ai_trade(conn, symbol, direction, module, live_price, confidence, chart_path=None) -> bool:
    """Calculates trade details, writes to outbox and monitor.

    Returns True if the trade was actually posted, False if the internal
    cooldown suppressed the post (P2.30: the caller wrote posted=True in
    ml_predictions_master before, even though nothing was posted).
    """
    target_channel = _kcfg.CH_AI_SR  # Your target channel

    # FIX: Previously own cooldown check with `pd.Timestamp.utcnow().tz_localize(None)`
    # → crashes in newer pandas versions (utcnow is tz-aware there) and mixes
    # tz-aware/tz-naive comparisons. Now: clean version from market_utils.
    #
    # Transitional dedup (T-2026-CU-9050-042): the cooldown key IS the tag, and
    # the tag changes on retrain rollout (SRA1 → SRA2). A fresh SRA1 cooldown row
    # would no longer block an SRA2 signal on the same coin, and Cornix would
    # open a second live position next to the first. So additionally check against
    # the old tag; as long as the tags are the same (today, legacy artifact
    # without metadata), the second query is skipped.
    cooldown_tags = [module] if module == SRA_LEGACY_TAG else [module, SRA_LEGACY_TAG]
    if any(check_cooldown(conn, t, symbol, direction, 4) for t in cooldown_tags):
        return False

    # 2. Levels & targets
    is_long = direction == "LONG"
    entry1 = float(live_price)
    entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
    supps, resis = get_hvn_and_sr_levels(conn, symbol, live_price)

    if is_long:
        sl = max([x for x in supps if x < entry2 * 0.99]) if any(x < entry2 * 0.99 for x in supps) else entry2 * 0.975
        t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
    else:
        sl = min([x for x in resis if x > entry2 * 1.01]) if any(x > entry2 * 1.01 for x in resis) else entry2 * 1.025
        t_cands = sorted([x for x in supps if x > 0 and x < (entry1 * 0.99)], reverse=True)

    # FIX: real zones + if needed 5% target if last zone is too close
    targets = ensure_min_tp_distance(
        thin_targets(t_cands[:20], entry1, is_long, keep=N_PUBLISHED_TARGETS),
        entry1,
        is_long,
        min_pct=0.05,
    )
    # P2.31: publish AND track exactly the same targets. The Cornix block below
    # shows the first n_show TPs; the AI monitor (8_ai_trade_monitor) scores
    # whatever is stored in ai_signals.targets. Storing the full 20-zone list made
    # the monitor score phantom TPs the subscriber never saw. Single source for both.
    n_show = 3
    lev = get_max_leverage(symbol, 20)
    # 3. Cornix & telegram
    lines = [
        f"📈 Signal for {symbol} 📈",
        f"🚨 Direction: {direction}",
        f"🚨 Leverage: {lev}",
        "🚨 Margin: Cross",
        f"🏦 CMP Entry: $ {entry1:.8f}",
        # T-2026-KYT-9050-042: entry2 is still computed and stored, but no longer
        # published — the fleet trades single-entry (arm B). See core/signal_post.py.
    ]
    for i, t in enumerate(targets[:n_show], 1):
        lines.append(f"💰 TP{i}: $ {t:.8f}")
    lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module} V3"]
    cornix_msg = "\n".join(lines)

    # FIX double post (2026-07-06, fleet sweep): caption without embedded Cornix
    # block — Cornix would otherwise parse both messages as signals.
    html_caption = f"<b>💥 AI {module} {direction} SIGNAL</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n→ Direction: {direction}\n→ ML Confidence: <b>{confidence:.1%}</b>\n→ Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M')} UTC"

    # T-2026-KYT-9050-033 (audit T-032): fleet-lifecycle gate. Default LIVE ⇒ no
    # behaviour change. SRA1 is parked in both directions → SHADOW (monitored
    # trade instead of Cornix); SRA2 is the live successor (see shadow_gate).
    # Purely additive (rule 4). Return False ⇒ the caller logs ml_predictions_master
    # posted=False (as with cooldown suppression). Note: post_shadow_ai_signal
    # additionally logs ONE shadow prediction (trade_id=0) — knowingly accepted;
    # the monitored ai_signals trade (audit data source) remains singular via
    # has_open.
    _route = route_legacy_leg(conn, module, direction, symbol, confidence, entry1, entry2, sl, targets, n_show=n_show)
    if _route != LEG_LIVE:
        if _route == LEG_SHADOW:
            conn.commit()
        return False

    with conn.cursor() as cur:
        # Cornix text
        cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (target_channel, cornix_msg))
        # Chart image
        if chart_path:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                (target_channel, html_caption, chart_path),
            )
        # Monitor

        cur.execute(
            """
                        INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
            (
                symbol,
                float(entry1),
                module,
                direction,
                float(confidence),
                float(entry1),
                float(entry2),
                float(sl),
                json.dumps(targets[:n_show]),
            ),
        )
    # FIX (Review Batch 4): cooldown in THE SAME transaction as outbox +
    # ai_signals set. Previously update_cooldown ran AFTER conn.commit() —
    # if the cooldown upsert threw (e.g., lock_timeout), the post remained
    # committed but without cooldown and without master log → the next scan pass
    # posted the same trade again (double exposure at Cornix).
    update_cooldown(conn, module, symbol, direction, commit=False)
    conn.commit()
    logger.info(f"🚀 {module} trade for {symbol} successfully fired!")
    return True


# MAIN LOOP


def startup() -> None:
    """One-time init: load both direction artifacts.

    Everything main() did before entering its loop (T-2026-KYT-9050-135).
    """
    logger.info("=== 🧠 ML SR BOT ACTIVATED ===")
    load_models()


def run_poll() -> None:
    """One poll cycle — the body of main()'s loop, unchanged.

    The reload lives INSIDE the cycle on purpose: maybe_reload is what picks up
    an artifact deploy without a restart, so it has to run per poll (it applies
    its own 24 h age check), not once at startup.
    """
    # Daily reload picks up an artifact deploy without a restart; a failed
    # reload does not discard a loaded artifact.
    expected = sra_expected_features()
    for direction in SRA_ARTIFACT_PATHS:
        MODELS[direction] = maybe_reload(MODELS[direction], expected)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # 1. Fresh S&R trades from the master table
            cur.execute("""
                SELECT id, time, coin, direction, entry
                FROM active_trades_master
                WHERE strategy = 'Support Resistance'
                AND posted >= NOW() - INTERVAL '60 minutes'
            """)
            fresh_trades = cur.fetchall()

            for trade in fresh_trades:
                t_id, t_time, coin, direction, entry = trade

                # FIX P1.20: per-trade isolation. Previously ONE broken trade
                # (e.g., predict error) tore the entire iteration off and the
                # pass rollback discarded also the shadow inserts of all already-
                # processed trades. Now: commit per trade, rollback only affects
                # that one.
                try:
                    # SRA2 shadow (T-2026-CU-9050-125): score the same candidates
                    # independent of the SRA1 live path + monitored track.
                    _emit_sra2_shadow(conn, coin, direction, t_time, entry)

                    artifact = MODELS.get(direction)
                    if not artifact or not artifact['loaded']:
                        continue  # Idle mode for that direction (trap 3)
                    # The posting tag comes from the artifact metadata (load_artifact_json
                    # sets tag = meta.model_id); without metadata it remains the named
                    # legacy tag. It carries ai_signals.model, ml_predictions_master
                    # .model_name and the cooldown key — all three must name the same
                    # generation, otherwise the per-bot statistics mix two models
                    # (rule 6).
                    module_name = artifact['tag']

                    # 2. Deduplication check in master log
                    #    Transitional: also check against the old tag. Without it an
                    #    SRA2 rollout would hold every already-processed trade as new
                    #    and would post it a second time.
                    cur.execute(
                        "SELECT 1 FROM ml_predictions_master WHERE trade_id = %s AND model_name IN (%s, %s)",
                        (t_id, module_name, SRA_LEGACY_TAG),
                    )
                    if cur.fetchone():
                        continue

                    # 3. Active trade check (T-2026-CU-9050-055) — checks whether for
                    #    exactly this module/coin/direction an already-running
                    #    unclosed trade exists. The 4h cooldown in the post path is
                    #    a FREQUENCY lock, not a position guard: an SRA trade
                    #    regularly runs longer, and without this check the follow-up
                    #    signal would open a SECOND live position next to the first
                    #    (the same lesson as RUB, T-2026-CU-9050-043).
                    #    Pattern: 11_ai_mis_bot.py:318. The deduplication check above
                    #    protects only against the same trade_id, not against a NEW
                    #    setup trade on a coin that is already open.
                    #
                    #    The tag is also the dedup key and flips on SRA2 rollout;
                    #    without the old tag in the IN an open SRA1 position would
                    #    no longer block the SRA2 signal. Same tags (today) ⇒ no-op.
                    cur.execute(
                        "SELECT 1 FROM ai_signals WHERE symbol = %s AND direction = %s AND model IN (%s, %s)",
                        (coin, direction, module_name, SRA_LEGACY_TAG),
                    )
                    if cur.fetchone():
                        continue  # Trade runs live in AI monitor

                    # 4. Indicators & features
                    inds = get_indicators_at_time(conn, coin, t_time)
                    if not inds:
                        continue

                    features = create_feature_row(direction, inds)
                    if not features:
                        continue

                    # 5. XGBoost prediction
                    #    Artifact WITH metadata: frame from the shared SRA2 builder,
                    #    aligned to the contract — selection AND order. No fillna
                    #    across columns: load_artifact_json has hard-validated the
                    #    names (P0.12); missing VALUES remain NaN, the model knows
                    #    this from training.
                    #    Artifact WITHOUT metadata: the legacy vector, unchanged —
                    #    it is the contract of today's deployed SRA1 model.
                    if artifact['features']:
                        serving = build_serving_row(direction, inds)
                        if not serving:
                            continue
                        X = pd.DataFrame([serving])[artifact['features']]
                    else:
                        X = pd.DataFrame([features])
                    conf = float(artifact['model'].predict_proba(X)[0, 1])

                    # 6. Classification & shadow log
                    posted = False
                    if conf >= artifact['threshold']:
                        # FIX (Review Batch 4): do not post NaN-ATR vectors live.
                        # P1.20 allows missing ATR features to pass as NaN so the
                        # scan no longer crashes — but the model has never seen NaN
                        # in these columns in training, the confidence on it is
                        # uncalibrated. Only shadow-log such rows, no Cornix post.
                        if pd.isna(features.get('support_atr', np.nan)):
                            logger.info(f"⚠️ {coin} {direction} conf {conf:.1%} — ATR missing, shadow-log only.")
                        else:
                            logger.info(f"🎯 Hit! {coin} {direction} has {conf:.1%} confidence.")
                            chart_p = generate_minichart_image(coin, minutes=240)
                            # FIX P2.30: posted from the return value — False if the
                            # internal 4h cooldown suppressed the post.
                            posted = process_ai_trade(conn, coin, direction, module_name, entry, conf, chart_p)

                    # Everything >= SRA_SHADOW_THRESHOLD into the master history
                    if conf >= SRA_SHADOW_THRESHOLD:
                        cur.execute(
                            """
                            INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                            (t_id, module_name, t_time, coin, direction, entry, conf, posted),
                        )
                    else:
                        # Below that only marked "done" (minimal log)
                        cur.execute(
                            "INSERT INTO ml_predictions_master (trade_id, model_name, coin, confidence, posted) VALUES (%s, %s, %s, %s, False)",
                            (t_id, module_name, coin, conf),
                        )
                    conn.commit()
                except Exception as trade_err:
                    logger.error(f"SRA1: error at trade {t_id} ({coin} {direction}): {trade_err}")
                    # Rollback guarded — on a dead connection (DB restart)
                    # rollback() itself throws and would otherwise propagate out
                    # of main() and kill the process.
                    try:
                        conn.rollback()
                    except Exception:
                        logger.error("SRA1: rollback failed — pass abort, connection is renewed.")
                        break

        conn.commit()
    except Exception as e:
        logger.error(f"error in loop: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass  # dead connection — close() in finally returns the slot
    finally:
        if conn:
            conn.close()


def main():
    """Standalone entry. In the fleet 46_signal_consumer_runner.py drives the
    two functions above instead; the file stays runnable for debugging."""
    startup()
    while True:
        run_poll()
        time.sleep(300)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")

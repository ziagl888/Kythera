import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")
warnings.filterwarnings("ignore", category=UserWarning, module="pandas_ta")

import datetime
import json
import logging
import os
import time

import joblib
import numpy as np
import pandas as pd
import pandas_ta  # noqa: F401 — registers the df.ta accessor (regression from 052ba4c:
import requests

# the ruff cleanup removed the function-local import from b6735d9 as "unused",
# which made calculate_technical_indicators die with AttributeError on EVERY coin)
import scipy.signal
import xgboost as xgb

from core import config as _kcfg  # channel ids
from core.candles import read_candles
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.signal_post import LEG_LIVE, LEG_SHADOW, route_legacy_leg
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - ABR1_BOT - %(message)s')
logger = logging.getLogger(__name__)

# 🛠️ CONFIGURATION
MODEL_ID = 'ABR1'
TARGET_CHANNEL_ID = _kcfg.CH_ABR1  # your ABR1 channel
SG_LONG_MODEL_FILE = 'bt2_model_LONG.json'
SG_SHORT_MODEL_FILE = 'bt2_model_SHORT.json'
SG_COINS_FILE = 'coins.json'

# FIX: the thresholds LONG=0.60 / SHORT=0.80 are asymmetric and deliberately
# strict for SHORT, because the old backtests showed that SHORT setups at
# break&retest levels produce noticeably more false positives (especially in
# bull-market phases where the trend runs against the retest direction).
# CAUTION: if the live-trading result deviates strongly from the backtests,
# adjust the values here if needed — or, combined with SUCCESS_CLASS_IDX (see
# below), check whether the semantics match the model version.
THRESHOLDS = {'LONG': 0.60, 'SHORT': 0.80}

# SUCCESS_CLASS_IDX selects the "success" class column in
# predict_proba[0, SUCCESS_CLASS_IDX]. The bt2 model is NOT a binary
# classifier, but 3-class (multi:softprob). Verified against the training
# code (BT2-ML-Trainer.py / BT2-ML-Final_Saver.py, 2025-12): the data grepper
# assigns the string labels
#   continuation_success (price_change > +5%) = trade works out → WIN
#   failed_breakout      (price_change < -3%) = trade fails     → LOSS
#   neutral              (in between)                           → sideways
# and the trainer encodes them via sklearn LabelEncoder ALPHABETICALLY:
#   continuation_success = 0, failed_breakout = 1, neutral = 2
# (success_idx = class_mapping['continuation_success'] = 0, trained
# identically for the LONG AND SHORT model).
# → SUCCESS_CLASS_IDX = 0 is CORRECT. Do NOT set it to 1 — 1 is the
#   LOSS class (failed_breakout).
SUCCESS_CLASS_IDX = 0
PIVOT_WINDOW = 10
RETEST_BACKWARD_LOOKUP_CANDLES = 24
LEVEL_TOLERANCE_PCT = 0.005
LIVE_DATA_HISTORY_HOURS = 240

# ── LONG funding gate (experiment, operator sign-off 2026-07-06 evening) ────
# Report 21 Addendum 2: the only rule that survives the out-of-sample test —
# LONG only if the mean of the last 3 funding rates is STRICTLY above the
# Binance default (+1.0 bps/8h): fund_24h > +3 bps → +1.12%/trade, 74% WR
# (n=119/year across 100 coins; test window +0.69%, n=17 — thin, hence
# experiment with its own tracking tag and review after 4–6 weeks). Fail
# CLOSED: without funding data, LONG stays shut.
FUNDING_GATE_LONG_BPS = 3.0
# SHORT mirror finding (same study, 33.5k SHORT events): fund_24h > +1.5 bps
# is consistently toxic for SHORTs in train AND test (−1.2%/trade) — exactly
# the zone where the LONG gate opens. Hence VETO on the model gate. Unlike
# the LONG gate, this is fail-OPEN: without funding data the validated model
# signal applies (the veto is a safety net, not the primary gate).
FUNDING_VETO_SHORT_BPS = 1.5
FUNDING_GATE_TAG = 'ABR2'  # generation-2 tag; the direction column separates the sides
_funding_cache: dict = {}  # symbol -> (monotonic_ts, mean_bps | None)


def get_funding_24h_bps(symbol):
    """Mean of the last 3 settled funding rates in bps (30-min cache).
    None on API error — the caller treats that as 'gate shut'."""
    now = time.monotonic()
    hit = _funding_cache.get(symbol)
    if hit is not None and now - hit[0] < 1800:
        return hit[1]
    mean_bps = None
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 3},
            timeout=10,
        )
        r.raise_for_status()
        rates = [float(x["fundingRate"]) for x in r.json()]
        if rates:
            mean_bps = sum(rates) / len(rates) * 1e4
    except Exception as e:
        logger.warning(f"⚠️ Funding check {symbol} failed (gate stays shut): {e}")
    _funding_cache[symbol] = (now, mean_bps)
    return mean_bps


FEATURE_COLUMNS = [
    'dist_close_ema9_pct',
    'dist_ema9_ema21_pct',
    'dist_close_kama9_pct',
    'rsi14',
    'rsi_below_30',
    'rsi_above_70',
    'tsi',
    'tsi_signal',
    'tsi_above_0',
    'tsi_below_0',
    'dist_close_boll_upper_pct',
    'dist_close_boll_mid_pct',
    'dist_close_boll_lower_pct',
    'dist_close_donchian_upper_pct',
    'dist_close_donchian_mid_pct',
    'dist_close_donchian_lower_pct',
    'retest_volume',
    'retest_volume_ratio_avg',
]

# Binary flags may legitimately be constant over a single coin window
# (e.g. RSI never below 30) — the startup self-test therefore doesn't hard-check them.
BINARY_FLAG_FEATURES = {'rsi_below_30', 'rsi_above_70', 'tsi_above_0', 'tsi_below_0'}

# FIX (P0.12): pandas_ta names its columns version-/parameter-dependently
# (KAMA_9_2_30 instead of KAMA_9, TSI_7_12_7 instead of TSI_12_7, BBL_20_2.0_2.0
# instead of BBL_20_2, DCL_20_20 instead of DCL_20). The old exact matching
# never found 11 of the 18 feature source columns → NaN → fillna(0) → the
# model actually ran on only 7 features (split-count proof in the audit).
# Prefix matching as in 14_ai_atb_bot.py:197-211. 'TSIs_' must come before 'TSI_'.
PTA_PREFIX_TO_CANONICAL = [
    ('EMA_9', 'ema9'),
    ('EMA_21', 'ema21'),
    ('KAMA_9', 'kama9'),
    ('RSI_14', 'rsi14'),
    ('TSIs_', 'tsi_signal'),
    ('TSI_', 'tsi'),
    ('BBL_', 'boll_lower_20'),
    ('BBM_', 'boll_mid_20'),
    ('BBU_', 'boll_upper_20'),
    ('DCL_', 'donchian_lower_20'),
    ('DCM_', 'donchian_mid_20'),
    ('DCU_', 'donchian_upper_20'),
]


def resolve_pta_columns(df):
    """Maps pandas_ta output columns to the canonical names by prefix.

    Raises ValueError if a source column is missing — no more silent fillna(0).
    """
    rename_map = {}
    missing = []
    for prefix, canonical in PTA_PREFIX_TO_CANONICAL:
        col = next((c for c in df.columns if c.startswith(prefix)), None)
        if col is None:
            missing.append(f"{prefix}* → {canonical}")
        else:
            rename_map[col] = canonical
    if missing:
        raise ValueError(f"pandas_ta columns not found: {missing}")
    return df.rename(columns=rename_map)


# Models are global — one contract per direction: {model, features, threshold,
# success_idx, calibrator}. The contract comes from the artifact's meta.json
# (fix R13-ABR1-5: no more hardcoding what training already determines).
MODELS = {'LONG': None, 'SHORT': None}


def _load_model_contract(direction, model_file):
    """Loads model + contract. New artifacts (tools/retrain_from_replay.py)
    bring a *_meta.json with model_type='binary...', features, threshold —
    success there is predict_proba[:, 1]. Without meta.json: legacy 3-class
    model (multi:softprob, success = class 0, thresholds from THRESHOLDS)."""
    model = xgb.XGBClassifier()
    model.load_model(model_file)

    meta_path = model_file.replace('.json', '_meta.json')
    calib_path = model_file.replace('.json', '_calib.pkl')
    calibrator = None
    if os.path.exists(calib_path):
        calibrator = joblib.load(calib_path)

    if os.path.exists(meta_path):
        with open(meta_path, encoding='utf-8') as f:
            meta = json.load(f)
        if not str(meta.get('model_type', '')).startswith('binary'):
            raise ValueError(f"{meta_path}: unexpected model_type {meta.get('model_type')!r}")
        features = meta.get('features')
        if not features:
            raise ValueError(f"{meta_path}: feature list missing — regenerate the artifact with the current trainer")
        contract = {
            'model': model,
            'features': list(features),
            'threshold': float(meta['optimal_threshold']),
            'success_idx': 1,
            'calibrator': calibrator,
            # New generation posts under its own tag (operator rule 2026-07-06);
            # older binary metas without model_id are also retrain generation 2.
            'model_id': meta.get('model_id', 'ABR2'),
        }
        logger.info(
            f"✅ {direction}: binary model ({meta_path}), {len(features)} features, "
            f"threshold {contract['threshold']:.2f}, calibrator: {'yes' if calibrator else 'no'}"
        )
        return contract

    logger.warning(
        f"⚠️ {direction}: no {meta_path} found — legacy 3-class contract "
        f"(success_idx={SUCCESS_CLASS_IDX}, threshold {THRESHOLDS[direction]:.2f}). "
        f"Per audit/retrain (Report 19), the legacy model is practically blind as a gate."
    )
    return {
        'model': model,
        'features': list(FEATURE_COLUMNS),
        'threshold': float(THRESHOLDS[direction]),
        'success_idx': SUCCESS_CLASS_IDX,
        'calibrator': calibrator,
        'model_id': MODEL_ID,  # legacy model stays measurable under ABR1
    }


def load_models_and_coins():
    try:
        MODELS['LONG'] = _load_model_contract('LONG', SG_LONG_MODEL_FILE)
        MODELS['SHORT'] = _load_model_contract('SHORT', SG_SHORT_MODEL_FILE)
        logger.info("✅ ML models loaded successfully.")
    except Exception as e:
        logger.critical(f"❌ ERROR: Could not load ML models: {e}")
        exit(1)

    try:
        with open(SG_COINS_FILE) as f:
            data = json.load(f)
            return data.get('coins', data) if isinstance(data, dict) else data
    except Exception:
        logger.warning("Could not load coins.json, using empty list.")
        return []


def calculate_technical_indicators(df):
    """Calculates all features for the model via pandas_ta"""

    # ensure everything is numeric
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df.ta.ema(length=9, append=True)
    df.ta.ema(length=21, append=True)
    df.ta.kama(length=9, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.tsi(fast=7, slow=12, signal=7, append=True)
    df.ta.bbands(length=20, append=True)
    df.ta.donchian(length=20, append=True)

    # FIX (P0.12): prefix matching instead of exact names + hard ValueError on
    # a missing source column (previously: create a NaN column → fillna(0) →
    # feature silently constant 0).
    df = resolve_pta_columns(df)

    df['dist_close_ema9_pct'] = ((df['close'] - df['ema9']) / df['ema9'] * 100).fillna(0)
    df['dist_ema9_ema21_pct'] = ((df['ema9'] - df['ema21']) / df['ema21'] * 100).fillna(0)
    df['dist_close_kama9_pct'] = ((df['close'] - df['kama9']) / df['kama9'] * 100).fillna(0)
    df['rsi_below_30'] = (df['rsi14'] < 30).astype(int)
    df['rsi_above_70'] = (df['rsi14'] > 70).astype(int)
    df['tsi_above_0'] = (df['tsi'] > 0).astype(int)
    df['tsi_below_0'] = (df['tsi'] < 0).astype(int)
    df['dist_close_boll_upper_pct'] = ((df['close'] - df['boll_upper_20']) / df['boll_upper_20'] * 100).fillna(0)
    df['dist_close_boll_mid_pct'] = ((df['close'] - df['boll_mid_20']) / df['boll_mid_20'] * 100).fillna(0)
    df['dist_close_boll_lower_pct'] = ((df['close'] - df['boll_lower_20']) / df['boll_lower_20'] * 100).fillna(0)
    df['dist_close_donchian_upper_pct'] = (
        (df['close'] - df['donchian_upper_20']) / df['donchian_upper_20'] * 100
    ).fillna(0)
    df['dist_close_donchian_mid_pct'] = ((df['close'] - df['donchian_mid_20']) / df['donchian_mid_20'] * 100).fillna(0)
    df['dist_close_donchian_lower_pct'] = (
        (df['close'] - df['donchian_lower_20']) / df['donchian_lower_20'] * 100
    ).fillna(0)
    df['volume_avg_30'] = df['volume'].rolling(window=30, min_periods=1).mean()
    df['retest_volume_ratio_avg'] = (df['volume'] / df['volume_avg_30']).fillna(1)
    df['retest_volume'] = df['volume']

    return df.fillna(0)


def startup_feature_selfcheck(coins):
    """FIX (P0.12): startup assertion "no feature constant".

    Calculates the feature pipeline on real data for a few coins and hard-aborts
    if a continuous feature is constant or columns are missing — exactly the
    failure mode that let the model run unnoticed on 7/18 features for months.
    Binary flags only trigger a warning (legitimately constant over short windows).
    """
    conn = get_db_connection()
    try:
        frames = []
        for symbol in coins[:10]:
            try:
                df = read_candles(
                    conn,
                    symbol,
                    "1h",
                    limit=LIVE_DATA_HISTORY_HOURS,
                    include_forming=False,
                    columns=("open_time", "open", "high", "low", "close", "volume"),
                )
            except Exception as e:
                logger.warning(f"Self-test: {symbol} not loadable ({e}), next coin.")
                continue
            if len(df) < 60:
                continue
            frames.append(calculate_technical_indicators(df.copy())[FEATURE_COLUMNS])
            if len(frames) >= 3:
                break

        if not frames:
            logger.critical("❌ Feature self-test: no usable data found — aborting.")
            exit(1)

        sample = pd.concat(frames, ignore_index=True)
        continuous = [c for c in FEATURE_COLUMNS if c not in BINARY_FLAG_FEATURES]
        constant = [c for c in continuous if sample[c].nunique(dropna=False) <= 1]
        if constant:
            logger.critical(f"❌ Feature self-test failed — constant features: {constant}. Aborting.")
            exit(1)
        constant_flags = [c for c in BINARY_FLAG_FEATURES if sample[c].nunique(dropna=False) <= 1]
        if constant_flags:
            logger.warning(f"Self-test: binary flags constant across the sample (can be legitimate): {constant_flags}")
        logger.info(f"✅ Feature self-test passed ({len(sample)} rows, {len(frames)} coins, 18/18 features variable).")
    finally:
        conn.close()


def find_pivot_levels(df):
    """FIX (R07-ABR1-b, detector rework 2026-07): only CONFIRMED pivots.

    The old 'edge' padding + greater_equal declared the last PIVOT_WINDOW
    candles unconfirmed pivots, which could vanish again with the next candle
    (repainting) — such levels never occurred during training (BT2 data
    grepper, without padding). Now: a pivot needs PIVOT_WINDOW candles on
    BOTH sides; the edge is hard-excluded (argrelextrema otherwise clips
    against itself at the edge).
    """
    if len(df) < PIVOT_WINDOW * 2 + 1:
        return []

    high_extrema_indices = scipy.signal.argrelextrema(df['high'].values, np.greater_equal, order=PIVOT_WINDOW)[0]
    low_extrema_indices = scipy.signal.argrelextrema(df['low'].values, np.less_equal, order=PIVOT_WINDOW)[0]

    first_confirmed = PIVOT_WINDOW
    last_confirmed = len(df) - 1 - PIVOT_WINDOW

    levels = []
    for idx, price_col, lvl_type in (
        (high_extrema_indices, 'high', 'resistance'),
        (low_extrema_indices, 'low', 'support'),
    ):
        for original_idx in idx:
            if first_confirmed <= original_idx <= last_confirmed:
                levels.append(
                    {
                        'price': df.iloc[original_idx][price_col],
                        'type': lvl_type,
                        'index': int(original_idx),
                        'time': df.iloc[original_idx]['open_time'],
                    }
                )
    return levels


# Setup geometry features (detector rework 2026-07): the 18 FEATURE_COLUMNS
# are generic indicator distances of the retest candle — the break&retest
# setup itself (level distance, break strength, age) was invisible to the
# model. These features are supplied by find_break_retest_setups() and feed
# into the NEW binary models (the feature list comes from their meta.json).
GEOMETRY_FEATURES = [
    'setup_dist_close_level_pct',
    'setup_break_strength_pct',
    'setup_candles_since_break',
    'setup_level_age_candles',
    'setup_retest_wick_pct',
]


def find_break_retest_setups(df, retest_idx, levels):
    """Shared break&retest detection for the bot AND the walk-forward simulator
    (tools/walkforward_sim.py imports this function — one source, no skew).

    Checks whether the candle at retest_idx is the FIRST retest of a fresh,
    valid level break. Fixes three bugs of the old inline logic:

    1. DIRECTION COUPLING: previously the retest was a pure touch gate
       (is_retest_long OR is_retest_short), the direction came solely from
       the break — a high touch from BELOW on a resistance that broke
       upward (= failed breakout, the LOSS class during training) was
       signalled as LONG. Now: LONG requires a low touch from above AND a
       close above the level; SHORT mirrors this (trainer semantics,
       BT2 data grepper lines 215/272).
    2. HOLD CHECK: all closes between break and retest must stay on the
       break side of the level — a breakout that failed in the meantime
       invalidates the setup.
    3. FIRST TOUCH: the trainer only labels the first retest after the
       break; an earlier band touch between break and retest invalidates it.

    Additionally, trainer semantics for the level age: the break must occur
    AFTER the full pivot confirmation (break_idx > level_index + PIVOT_WINDOW).

    Returns: list of setups (max. 1 per direction; if there are multiple
    candidates, the freshest break wins) including GEOMETRY_FEATURES for the model.
    """
    retest = df.iloc[retest_idx]
    setups = {}

    for level in levels:
        lvl_price = level['price']
        upper_bound = lvl_price * (1 + LEVEL_TOLERANCE_PCT)
        lower_bound = lvl_price * (1 - LEVEL_TOLERANCE_PCT)

        if level['type'] == 'resistance':
            direction = 'LONG'
            # retest from ABOVE: low touches the band, close holds above the level.
            if not (lower_bound <= retest['low'] <= upper_bound and retest['close'] > lvl_price):
                continue
        else:
            direction = 'SHORT'
            # retest from BELOW: high touches the band, close holds below the level.
            if not (lower_bound <= retest['high'] <= upper_bound and retest['close'] < lvl_price):
                continue

        # break search backwards; the level must be confirmed before the break.
        search_end_idx = max(level['index'] + PIVOT_WINDOW, retest_idx - RETEST_BACKWARD_LOOKUP_CANDLES)
        break_idx = None
        for j in range(retest_idx - 1, search_end_idx, -1):
            if j <= 0:
                break
            c_close = df.iloc[j]['close']
            prev_close = df.iloc[j - 1]['close']
            if direction == 'LONG':
                if prev_close < lvl_price < c_close:
                    break_idx = j
                    break
                if c_close <= lvl_price:
                    break  # close below the level after the break → breakout failed
                if df.iloc[j]['low'] <= upper_bound:
                    break  # earlier band touch → retest would not be the first
            else:
                if prev_close > lvl_price > c_close:
                    break_idx = j
                    break
                if c_close >= lvl_price:
                    break
                if df.iloc[j]['high'] >= lower_bound:
                    break
        if break_idx is None:
            continue

        candles_since_break = retest_idx - break_idx
        break_close = df.iloc[break_idx]['close']
        if direction == 'LONG':
            dist_close_level = (retest['close'] - lvl_price) / lvl_price * 100
            break_strength = (break_close - lvl_price) / lvl_price * 100
            retest_wick = (retest['close'] - retest['low']) / retest['close'] * 100
        else:
            dist_close_level = (lvl_price - retest['close']) / lvl_price * 100
            break_strength = (lvl_price - break_close) / lvl_price * 100
            retest_wick = (retest['high'] - retest['close']) / retest['close'] * 100

        setup = {
            'direction': direction,
            'level_price': float(lvl_price),
            'level_type': level['type'],
            'break_idx': int(break_idx),
            'features': {
                'setup_dist_close_level_pct': float(dist_close_level),
                'setup_break_strength_pct': float(break_strength),
                'setup_candles_since_break': float(candles_since_break),
                'setup_level_age_candles': float(retest_idx - level['index']),
                'setup_retest_wick_pct': float(retest_wick),
            },
        }
        best = setups.get(direction)
        if best is None or candles_since_break < best['features']['setup_candles_since_break']:
            setups[direction] = setup

    return list(setups.values())


def send_signal(conn, symbol, direction, prob, close_price, model_tag_override=None, funding_bps=None):
    # cooldown: 4h per coin/direction. check_cooldown returns True if active (blocking).
    if check_cooldown(conn, MODEL_ID, symbol, direction, 4):
        logger.info(f"⏳ Cooldown active for {symbol} ({direction}).")
        return

    # smart targets: real HVN/SR/Fib-based entries, SL, 10 targets — no more dummy values.
    trade_setup = calculate_smart_targets(conn, symbol, direction, close_price)
    entry1 = trade_setup['entry1']
    entry2 = trade_setup['entry2']
    sl = trade_setup['sl']
    targets = trade_setup['targets']

    lev = get_max_leverage(symbol, 20)

    # versioning rule (operator 2026-07-06): revised models post under a new
    # tag (ABR2, ...) so old/new are separately measurable in trackers. The
    # tag comes from the artifact contract; legacy models stay ABR1.
    model_tag = MODELS[direction].get('model_id', MODEL_ID) if MODELS.get(direction) else MODEL_ID
    if model_tag_override:
        model_tag = model_tag_override  # e.g. funding-gate LONG posts as generation 2

    # T-2026-KYT-9050-033 (audit T-032): fleet lifecycle gate. Default LIVE ⇒ no
    # behaviour change. ABR2 is parked in both directions → SHADOW (monitored
    # trade instead of Cornix); ABR1 (legacy fallback tag) stays default LIVE. Purely
    # additive on the post branch (rule 4). ai_signals stores the full target list →
    # n_show=len(targets); confidence is prob as in the live path.
    _route = route_legacy_leg(
        conn, model_tag, direction, symbol, prob, entry1, entry2, sl, targets, n_show=len(targets)
    )
    if _route != LEG_LIVE:
        if _route == LEG_SHADOW:
            conn.commit()
        return

    lines = [
        f"📈 Signal for {symbol} 📈",
        f"🚨 Direction: {direction}",
        f"🚨 Leverage: {lev}",
        "🚨 Margin: Cross",
        f"🏦 CMP Entry: $ {entry1:.5f}",
        # T-2026-KYT-9050-042: entry2 is still computed and stored, but no longer
        # published — the fleet trades single-entry (arm B). See core/signal_post.py.
    ]
    for i, t in enumerate(targets[:3], 1):
        lines.append(f"💰 TP{i}: $ {t:.5f}")
    lines += [f"💸 Stop Loss: $ {sl:.5f}", f"🧠 Trade idea generated by AI module {model_tag}"]
    cornix_msg = "\n".join(lines)

    emoji = f"🚀 AI {model_tag} LONG SIGNAL" if direction == "LONG" else f"💥 AI {model_tag} SHORT SIGNAL"

    # FIX double-post (operator report 2026-07-06): the info message must NOT
    # contain the Cornix block again — Cornix parsed both messages as
    # standalone signals (duplicate position).
    # Funding line ONLY in the info message (the Cornix message stays the
    # only parsable one — double-post rule 2026-07-06 untouched).
    funding_line = f"\n<b>→ Funding-Gate: {funding_bps:+.2f} bps/8h (24h mean)</b>" if funding_bps is not None else ""
    html = f"""<pre><b>{emoji}</b>\n<b>{symbol}</b>\n<b>→ Direction: {direction}</b>\n<b>→ ML Confidence: <b>{prob:.1%}</b></b>{funding_line}\n<b>→ Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M')} UTC | Module: {model_tag}</b>\n<b>→ Source: AI Break & Retest Model</b></pre>"""

    chart_buf = generate_minichart_image(symbol, minutes=240)

    with conn.cursor() as cur:
        # Cornix channel (here it uses the special Rubberband channel!)
        cur.execute(
            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (TARGET_CHANNEL_ID, cornix_msg)
        )
        if chart_buf:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                (TARGET_CHANNEL_ID, html, chart_buf),
            )
        else:
            cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (TARGET_CHANNEL_ID, html))

        cur.execute(
            """INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                symbol,
                float(entry1),
                model_tag,
                direction,
                float(prob),
                float(entry1),
                float(entry2),
                float(sl),
                json.dumps(targets),
            ),
        )
    conn.commit()
    update_cooldown(conn, MODEL_ID, symbol, direction)
    logger.info(f"✅ {MODEL_ID} signal for {symbol} placed in outbox!")


def process_abr_logic(conn, symbol):
    try:
        # R1: detection runs on the most recent CLOSED candles. For 1h,
        # include_forming=False is exactly the previous
        # `open_time < current_hour_utc` cut (1h open_times always have
        # minute=0); limit=LIVE_DATA_HISTORY_HOURS replaces the `.tail()`,
        # the previous +5h overfetch is gone.
        df = read_candles(
            conn,
            symbol,
            "1h",
            limit=LIVE_DATA_HISTORY_HOURS,
            include_forming=False,
            columns=("open_time", "open", "high", "low", "close", "volume"),
        )
        if df.empty or len(df) < max(PIVOT_WINDOW * 2, 30) + RETEST_BACKWARD_LOOKUP_CANDLES + 2:
            return

        df['open_time'] = pd.to_datetime(df['open_time'], utc=True)

        df_indicators = calculate_technical_indicators(df.copy())
        levels = find_pivot_levels(df_indicators)
        if not levels:
            return

        # FIX (R07-ABR1-a, detector rework 2026-07): ONLY the most recent
        # closed candle is a retest candidate. The bot runs hourly — each
        # candle is checked exactly once; the old 3-candle window produced
        # up to 3h of stale entries and duplicate evaluations.
        # (The forming candle was cut off above via open_time < current_hour_utc
        # — 1h candles always have minute=0.)
        retest_idx = len(df_indicators) - 1
        retest_candle = df_indicators.iloc[retest_idx]

        for setup in find_break_retest_setups(df_indicators, retest_idx, levels):
            direction = setup['direction']
            contract = MODELS[direction]

            # Strictly serve the artifact's feature contract: indicator
            # features of the retest candle + setup geometry. Missing
            # features are a hard error — NO silent fillna(0) over missing
            # columns (X-R5 pattern, hid the 11-features bug for 3 stages).
            feature_row = {**retest_candle[FEATURE_COLUMNS].to_dict(), **setup['features']}
            missing = [c for c in contract['features'] if c not in feature_row]
            if missing:
                raise ValueError(f"Feature contract violated — missing: {missing}")
            X_event = pd.DataFrame([{c: feature_row[c] for c in contract['features']}], dtype=float)

            # defensive safeguard against NaN/Inf in computed VALUES
            # (e.g. indicator warmup for fresh coins): NaN/Inf → 0 (neutral).
            X_event = X_event.replace([np.inf, -np.inf], np.nan).fillna(0)

            prediction_proba = float(contract['model'].predict_proba(X_event)[0, contract['success_idx']])

            # calibrated confidence only for display — the gate runs on
            # the raw probability, which is also what the threshold was chosen on.
            display_proba = prediction_proba
            if contract['calibrator'] is not None:
                display_proba = float(contract['calibrator'].predict([prediction_proba])[0])

            logger.info(
                f"ABR1 break&retest detected for {symbol} | Dir: {direction} | "
                f"Level: {setup['level_price']:.6f} | Prob: {prediction_proba:.2f} "
                f"(Gate {contract['threshold']:.2f})"
            )
            # Gates per direction (as of 2026-07-06 evening):
            #   SHORT — binary model gate on raw probability (v2 contract).
            #   LONG  — funding-gate EXPERIMENT (Report 21 Addendum 2): the
            #           ML gate is demonstrably blind for LONG, but
            #           fund_24h > +3 bps survives as the only rule for the
            #           out-of-sample test. The legacy model contract now only
            #           serves the confidence display; fail-closed without funding.
            if direction == 'LONG':
                fund_bps = get_funding_24h_bps(symbol)
                if fund_bps is not None and fund_bps > FUNDING_GATE_LONG_BPS:
                    logger.info(f"🟢 LONG funding gate open for {symbol}: {fund_bps:+.2f} bps")
                    send_signal(
                        conn,
                        symbol,
                        direction,
                        display_proba,
                        retest_candle['close'],
                        model_tag_override=FUNDING_GATE_TAG,
                        funding_bps=fund_bps,
                    )
                elif fund_bps is not None:
                    logger.info(
                        f"⛔ LONG funding gate shut for {symbol}: {fund_bps:+.2f} bps (limit {FUNDING_GATE_LONG_BPS:+.1f})"
                    )
            elif prediction_proba >= contract['threshold']:
                # SHORT funding veto (2026-07-06, Report 21 Addendum 2 mirror
                # test): at fund_24h > +1.5 bps the zone is consistently
                # unprofitable — veto despite the model gate. Fail-open (see constant).
                fund_bps = get_funding_24h_bps(symbol)
                if fund_bps is not None and fund_bps > FUNDING_VETO_SHORT_BPS:
                    logger.info(
                        f"⛔ SHORT funding veto for {symbol}: {fund_bps:+.2f} bps "
                        f"(> {FUNDING_VETO_SHORT_BPS:+.1f}, model prob {prediction_proba:.2f})"
                    )
                else:
                    send_signal(conn, symbol, direction, display_proba, retest_candle['close'], funding_bps=fund_bps)

    except Exception as e:
        logger.error(f"Error for {symbol}: {e}")


def main():
    logger.info("=== AI BREAK & RETEST BOT (ABR1) STARTED ===")
    coins = load_models_and_coins()
    startup_feature_selfcheck(coins)

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        # P3.10: comment corrected to match code — fires at minute 2 (not 10).
        if now.minute == 2:
            logger.info("Starting ABR1 Scan...")
            conn = get_db_connection()
            conn.autocommit = True
            try:
                for symbol in coins:
                    process_abr_logic(conn, symbol)
            finally:
                conn.close()
            logger.info("ABR1 Scan stopped.")
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")

import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import os
import time

import joblib
import numpy as np
import pandas as pd

from core import config as _kcfg  # channel ids
from core.candles import history_start, read_candles_with_indicators
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.live_price import get_live_price, get_live_prices_batch
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.mis_features import (
    BINARY_FLAG_FEATURES,
    MIS_INDICATOR_COLUMNS,
    MIS_RENAME_MAP,
    add_advanced_features,
    assert_features_alive,
)
from core.signal_post import LEG_LIVE, LEG_SHADOW, route_legacy_leg
from core.trade_utils import calculate_smart_targets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_MIS_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & CHANNELS (dynamic routing) ---
# Enter the 4 different channel IDs here!
MIS_CHANNELS = {
    "8H": _kcfg.CH_MIS_8H,  # 👈 channel for 8h
    "24H": _kcfg.CH_MIS_24H,  # 👈 channel for 24h
    "72H": _kcfg.CH_MIS_72H,  # 👈 channel for 72h
    "168H": _kcfg.CH_MIS_168H,  # 👈 channel for 168h
}

# --- LOAD ML MODELS ---
# MIS2 (operator decisions 2026-07-06, docs/MODEL_INTENT.md §1):
#   * Move-label models (±5%/8h, ±10%/24h, ±15%/72h, ±25%/168h) replace the
#     old MIS1 models COMPLETELY — MIS1 is switched off, no legacy fallback.
#   * ONLY the pump side is deployable (all 4 horizons with out-of-time return);
#     the dump side detects dumps well, but earns nothing with the short
#     geometry — it is being reworked separately (own task).
#   * Base mix per test findings: close labels for 8h/24h/168h, wick for 72h.
#   Artifact = dict(model, features, optimal_threshold, calibrator_isotonic, meta)
#   from tools/retrain_from_replay.py --label-mode move.
# MODEL_GENERATION is now ONLY the loud fallback tag if an artifact carries no
# meta.model_id — the posting tag comes from the artifact (versioning
# rule, T-2026-CU-9050-030). The file names are deliberately generation-free SLOTS
# (operator decision 2026-07-09): a MIS3 rollout overwrites mis2_model_*.pkl,
# and the bot posts purely based on the new meta.model_id as MIS3-72H.
MODEL_GENERATION = "MIS2"
PUMP_MODELS = {
    key: {
        "artifact_path": f"mis2_model_{key}.pkl",  # slot name, NO generation info
        "model": None,
        "threshold": 0.5,
        "features": None,
        "calibrator": None,
        "generation": MODEL_GENERATION,
        "loaded": False,
    }
    for key in ("8h_pump", "24h_pump", "72h_pump", "168h_pump", "8h_dump", "24h_dump", "72h_dump", "168h_dump")
}

# MIS2 SHORT rules per horizon (operator decisions 2026-07-06 evening +
# geometry study V2, staging_models/mis2_dump_geometry_study_v2.json):
#   * Entry = LIMIT sell "bounce_pct" ABOVE the signal price — sell into the
#     upward jerk (it previously tore out the stops; fill rate 78-88 %).
#   * TP is calculated from SIGNAL PRICE (the move forecast counts from signal
#     time), SL from entry. A single TP — exactly the simulated geometry.
#   * Leverage: hard 20x posting per operator decision (cross margin, small
#     positions on a large account) — deliberately NO cap_leverage_to_sl, even
#     though SL sits 12-16 % above the isolated liquidation distance.
#   * 8H is negative per the study (−0,24 %/Trade) — operator wants the
#     live proof ("maybe the models aren't 100% right"), documented in
#     docs/MODEL_INTENT.md §1.
DUMP_RULES = {
    "8H": {"bounce_pct": 5.0, "tp_pct": 5.0, "sl_pct": 5.0},  # study: −0,24 %/trade
    "24H": {"bounce_pct": 5.0, "tp_pct": 10.0, "sl_pct": 16.0},  # study: +0,49 %/trade
    "72H": {"bounce_pct": 5.0, "tp_pct": 15.0, "sl_pct": 12.0},  # study: +0,72 %/trade
    "168H": {"bounce_pct": 5.0, "tp_pct": 16.7, "sl_pct": 12.0},  # study: +0,27 %/trade
}


def load_pump_models():
    """Loads the MIS2 move artifacts (no legacy fallback — MIS1 is off)."""
    loaded_count = 0
    for key, cfg in PUMP_MODELS.items():
        try:
            if os.path.exists(cfg["artifact_path"]):
                art = joblib.load(cfg["artifact_path"])
                cfg["model"] = art["model"]
                cfg["threshold"] = float(art["optimal_threshold"])
                cfg["features"] = list(art["features"])
                cfg["calibrator"] = art.get("calibrator_isotonic")
                # Posting tag from the artifact meta (versioning rule): a
                # MIS3 retrain in the same slot must post as MIS3-*, otherwise
                # its per-bot statistics merge with MIS2 and the
                # orchestrator gating decides on the new generation
                # based on the performance of the old one (T-2026-CU-9050-030).
                model_id = str((art.get("meta") or {}).get("model_id") or "").strip()
                if model_id:
                    cfg["generation"] = model_id
                else:
                    logger.error(
                        f"⚠️ {cfg['artifact_path']}: meta.model_id missing — posting under fallback tag "
                        f"{MODEL_GENERATION}. A retrain artifact WITHOUT model_id tags its trades incorrectly."
                    )
                    cfg["generation"] = MODEL_GENERATION
                cfg["loaded"] = True
                loaded_count += 1
            else:
                logger.warning(f"Model missing: {cfg['artifact_path']}")
        except Exception as e:
            logger.error(f"Error loading {key}: {e}")

    generations = sorted({cfg["generation"] for cfg in PUMP_MODELS.values() if cfg["loaded"]})
    logger.info(
        f"✅ {loaded_count}/{len(PUMP_MODELS)} multi-horizon models ({'/'.join(generations)}) loaded successfully."
    )
    if len(generations) > 1:
        # Mixed rollout (e.g. 72H already MIS3, rest still MIS2). Not an error —
        # every signal posts under the generation OF ITS OWN model —, but make it visible.
        logger.warning(f"Mixed model generations loaded: {generations}")

    # FIX: log thresholds explicitly so drift between the model file and the
    # threshold file is immediately noticeable.
    thresh_summary = ", ".join(f"{h}={cfg['threshold']:.2f}" for h, cfg in PUMP_MODELS.items() if cfg["loaded"])
    logger.info(f"{'/'.join(generations) or MODEL_GENERATION} Thresholds: {thresh_summary}")


# --- LOAD MIS1 MODELS (revive, T-2026-KYT-9050-034) ---
# Operator decision (Michi): restore the MIS1 generation (audit T-032: MIS1-24H/72H/168H
# LONG + MIS1-8H SHORT realised BETTER than the new MIS2 move generation)
# EXACTLY — no retrain. The artifacts sit unchanged in the repo
# root: pump_model_{key}_final.pkl (bare 67-feature XGBClassifier) + threshold_
# {key}_final.pkl. They are fed with the SHARED builder, but in
# include_legacy=True mode — which reproduces the 8 LEGACY_ONLY_COLS that these
# models additionally expect on top of the 63 clean features (verified: 0 missing
# across all 8 models). MIS1 runs PARALLEL to MIS2 under its own tags MIS1-* →
# collision-free (own active-trade check + cooldown per tag). Which MIS1 legs
# post live is controlled EXCLUSIVELY by the shadow_gate register (core/shadow_gate.py):
# the good legs are default LIVE, the weak ones are parked there on SHADOW.
MIS1_GENERATION = "MIS1"
MIS1_MODELS = {
    key: {
        "model_path": f"pump_model_{key}_final.pkl",
        "threshold_path": f"threshold_{key}_final.pkl",
        "model": None,
        "threshold": 0.5,
        "features": None,
        "generation": MIS1_GENERATION,
        "loaded": False,
    }
    for key in ("8h_pump", "24h_pump", "72h_pump", "168h_pump", "8h_dump", "24h_dump", "72h_dump", "168h_dump")
}


def load_mis1_models():
    """Loads the MIS1 legacy artifacts (bare XGBClassifier + separate threshold
    pkl) — exact restoration of the path that produced the live success rate
    measured by the audit (99e9de3^). The feature contract comes from
    feature_names_in_ (67 features); the selfcheck strictly checks compatibility
    against the include_legacy builder before the scan starts."""
    loaded_count = 0
    for key, cfg in MIS1_MODELS.items():
        try:
            if not os.path.exists(cfg["model_path"]):
                logger.warning(f"MIS1 model missing: {cfg['model_path']}")
                continue
            cfg["model"] = joblib.load(cfg["model_path"])
            if os.path.exists(cfg["threshold_path"]):
                cfg["threshold"] = float(joblib.load(cfg["threshold_path"]))
            else:
                # No separate threshold file → conservative default (as in 99e9de3^).
                cfg["threshold"] = 0.60
                logger.warning(f"MIS1 threshold missing ({cfg['threshold_path']}) → default 0.60")
            cfg["features"] = list(getattr(cfg["model"], "feature_names_in_", []))
            cfg["loaded"] = True
            loaded_count += 1
        except Exception as e:
            logger.error(f"Error loading MIS1 {key}: {e}")
    thresh_summary = ", ".join(f"{h}={cfg['threshold']:.2f}" for h, cfg in MIS1_MODELS.items() if cfg["loaded"])
    logger.info(f"✅ {loaded_count}/{len(MIS1_MODELS)} MIS1 models (revive) loaded. Thresholds: {thresh_summary}")


def startup_feature_selfcheck():
    """P0.12 pattern (like 18_ai_abr1_bot): run the feature pipeline on real data
    from up to 3 coins and abort hard if a continuous feature is constant, or a
    loaded model requires features the (cleaned) builder no longer provides —
    legacy 67-feature models with the leakage columns are unloaded in that case
    instead of silently scoring with fillna(0) zeros."""
    try:
        with open('coins.json') as f:
            coins = json.load(f)
    except Exception as e:
        logger.critical(f"Self-test: coins.json not loadable: {e}")
        exit(1)

    conn = get_db_connection()
    try:
        frames = []
        for symbol in coins[:10]:
            df = _fetch_mis_frame(conn, symbol)
            if df is None or len(df) < 30:
                continue
            # include_legacy=True: SUPERSET (71 columns) — contains the 63 clean
            # MIS2 features AND the 8 LEGACY_ONLY_COLS that the MIS1 revive models
            # additionally need (T-2026-KYT-9050-034). Additively neutral for MIS2
            # (the 8 extra columns are never selected); the feature-alive
            # assertion still checks the 63 clean FEATURE_COLS.
            frames.append(add_advanced_features(df, include_legacy=True))
            if len(frames) >= 3:
                break
        if not frames:
            logger.critical("❌ Feature self-test: no usable data found — aborting.")
            exit(1)
        sample = pd.concat(frames, ignore_index=True)
        try:
            assert_features_alive(sample, context=" (Bot-Startup)")
        except ValueError as e:
            logger.critical(f"❌ {e}")
            exit(1)
        constant_flags = [c for c in BINARY_FLAG_FEATURES if sample[c].nunique(dropna=False) <= 1]
        if constant_flags:
            logger.warning(f"Self-test: binary flags constant across the sample (can be legitimate): {constant_flags}")

        for key, cfg in PUMP_MODELS.items():
            if not cfg["loaded"]:
                continue
            missing = [c for c in (cfg["features"] or []) if c not in sample.columns]
            if missing:
                logger.critical(
                    f"❌ {key}: model requires features the builder does not provide "
                    f"(likely legacy leakage columns, report 13): {missing[:6]}… — unloading model."
                )
                cfg["loaded"] = False
                cfg["model"] = None
        if not any(cfg["loaded"] for cfg in PUMP_MODELS.values()):
            logger.critical("❌ No compatible MIS2 model left — aborting.")
            exit(1)

        # Check MIS1 revive models (T-2026-KYT-9050-034) against the include_legacy
        # superset. Additive: if a MIS1 model fails, ONLY it is unloaded — the
        # bot keeps running with MIS2 (and the remaining MIS1 legs), NO hard abort.
        for key, cfg in MIS1_MODELS.items():
            if not cfg["loaded"]:
                continue
            missing = [c for c in (cfg["features"] or []) if c not in sample.columns]
            if missing:
                logger.error(
                    f"❌ MIS1 {key}: model requires features that even the include_legacy "
                    f"builder does not provide: {missing[:6]}… — unloading MIS1 model (MIS2 unaffected)."
                )
                cfg["loaded"] = False
                cfg["model"] = None

        n_ok = sum(1 for cfg in PUMP_MODELS.values() if cfg["loaded"])
        n_ok_mis1 = sum(1 for cfg in MIS1_MODELS.values() if cfg["loaded"])
        logger.info(
            f"✅ Feature self-test passed ({len(sample)} rows, {len(frames)} coins, "
            f"{n_ok} MIS2 + {n_ok_mis1} MIS1 models compatible)."
        )
    finally:
        conn.close()


# 🛡️ COOLDOWN CHECK


def _fetch_mis_frame(conn, symbol):
    """Last 100 CLOSED 1h candles + indicator join — the column catalogue comes
    from core.mis_features (one source for bot, trainer and simulator).

    R1 (block 4): reads closed candles via core.candles (ASC, forming bar
    dropped — no manual reverse anymore). The API delivers the RAW
    indicator names; MIS_RENAME_MAP then reproduces the three tsi/macd aliases
    so the frame stays byte-identical to the shared MIS_INDICATOR_COLUMNS list (and
    thus to tools/walkforward_sim.py) and add_advanced_features finds its
    REQUIRED_INPUT_COLS (hard rule 7, ONE source in core.mis_features)."""
    df = read_candles_with_indicators(
        conn,
        symbol,
        "1h",
        limit=100,
        # TimescaleDB chunk-exclusion hint (T-2026-CU-9050-180): bound the read to
        # a window that comfortably holds the newest 100 closed 1h candles so the
        # returned rows are unchanged while ~120 of 126 chunks are pruned.
        start=history_start("1h", 100),
        include_forming=False,
        candle_columns=("open_time", "close", "volume"),
        indicator_columns=MIS_INDICATOR_COLUMNS,
    )
    if len(df) < 10:
        return None
    return df.rename(columns=MIS_RENAME_MAP)


def _score_models_batched(collected_frames, models):
    """Batched MIS2 inference (T-2026-CU-9050-186).

    ``collected_frames``: list of the per-coin 1-row feature DataFrames
    (``df_features.iloc[-1:]``) in scan order — already built by
    ``add_advanced_features`` PER COIN, never concatenated before feature
    building (rolling windows must not cross coin boundaries).
    ``models``: mapping horizon-key -> cfg for the LOADED models.

    Returns ``{key: np.ndarray}`` where the array holds ``predict_proba[:, 1]``
    for every collected coin IN THE SAME ORDER. ``NaN`` marks a coin/model whose
    inference failed — the caller skips exactly that (coin, model), reproducing
    the old per-coin ``try/except`` that dropped a single failing prediction.

    Why this is behaviour-neutral: XGBoost scores each row independently, so one
    ``predict_proba`` over the stacked matrix yields the identical per-row
    probability as scoring each row alone — it only amortises the ~66ms per-call
    overhead (sklearn name-validation + DMatrix build) across all coins (527x8 =
    4216 calls/scan -> 8). The per-model column selection ``cfg["features"]``
    fixes column order identically to the single-row path.

    Fast path = one batched call per model. On ANY batch exception (e.g. a single
    corrupt row poisoning the concat/predict) it falls back to per-row scoring
    for THAT model only, so the failure semantics stay exactly as before: a bad
    coin loses just its own prediction, every other coin still scores.
    """
    n = len(collected_frames)
    probs_by_model = {}
    for key, cfg in models.items():
        feats = cfg["features"]
        try:
            X_all = pd.concat([f[feats] for f in collected_frames], axis=0)
            probs_by_model[key] = np.asarray(cfg["model"].predict_proba(X_all)[:, 1], dtype=float)
        except Exception as e:
            logger.error(f"MIS batch predict {key} failed — per-coin fallback: {e}")
            arr = np.full(n, np.nan, dtype=float)
            for i, f in enumerate(collected_frames):
                try:
                    arr[i] = float(cfg["model"].predict_proba(f[feats])[0, 1])
                except Exception as e2:
                    logger.error(f"{key} row {i}: predict failed: {e2}")
            probs_by_model[key] = arr
    return probs_by_model


def _mis_geometry(conn, generation, symbol, direction, horizon, current_price):
    """Trade geometry per generation → (entry1, entry2, sl, targets, entry_filled, expiry_hours).

    * MIS1 revive (T-2026-KYT-9050-034): EXACTLY the old path (99e9de3^) —
      ``calculate_smart_targets`` for BOTH directions, immediate CMP entry
      (entry_filled=True, no expiry). This is the geometry that produced the
      MIS1 success rate measured by audit T-032.
    * MIS2/MIS3: LONG = smart targets, SHORT = study-validated DUMP_RULES
      bracket geometry (limit entry, entry_filled=False, expiry=horizon) —
      unchanged.
    """
    is_long = direction == "LONG"
    if generation == MIS1_GENERATION or is_long:
        s = calculate_smart_targets(conn, symbol, direction, current_price)
        return s["entry1"], s["entry2"], s["sl"], s["targets"], True, None
    rules = DUMP_RULES[horizon]
    entry1 = current_price * (1 + rules["bounce_pct"] / 100.0)  # limit sell into the bounce
    entry2 = entry1  # single entry — exactly the simulated geometry
    sl = entry1 * (1 + rules["sl_pct"] / 100.0)
    targets = [current_price * (1 - rules["tp_pct"] / 100.0)]  # TP from signal price
    return entry1, entry2, sl, targets, False, int(horizon.replace("H", ""))


def _post_mis_live_leg(
    conn,
    module_tag,
    best_horizon,
    best_direction,
    best_prob,
    best_threshold,
    best_conf,
    symbol,
    current_price,
    now,
    entry1,
    entry2,
    sl,
    targets,
    *,
    entry_filled,
    expiry_hours,
):
    """Shared LIVE post body for MIS signals (MIS2 + MIS1 revive, T-034).

    Builds Cornix + HTML visualisation (WITHOUT an embedded Cornix block — rule 4,
    double-trade fix 2026-07-06), writes telegram_outbox + ai_signals +
    ml_predictions_master and sets the cooldown (update_cooldown commits the ONE
    transaction). ``entry_filled``/``expiry_hours`` carry the entry semantics of the
    respective geometry (MIS2 SHORT = limit; MIS1 + MIS2 LONG = CMP immediately)."""
    is_long = best_direction == "LONG"
    lev = get_max_leverage(symbol, 20)
    emoji = "🚀 PUMP SIGNAL (MIS)" if is_long else "💥 DUMP SIGNAL (MIS)"
    strength = "STRONG" if best_prob >= best_threshold + 0.1 else "MODERATE"

    # RRR (risk reward ratio) calculation
    avg_entry = (entry1 + entry2) / 2
    risk_pct = abs((sl - avg_entry) / avg_entry)
    reward_pct = abs((targets[0] - avg_entry) / avg_entry) if targets else 0.01
    rrr = reward_pct / risk_pct if risk_pct > 0 else 0.01

    # P2.31: publish AND track exactly the same targets. The Cornix block shows the
    # first n_show TPs; the AI monitor (8_ai_trade_monitor) scores whatever is stored
    # in ai_signals.targets. Storing the full target list made the monitor score
    # phantom TPs the subscriber never saw.
    n_show = 5

    # Cornix text
    # T-2026-KYT-9050-042: entry2 is still computed and stored, but no longer
    # published — the fleet trades single-entry (arm B). See core/signal_post.py.
    cornix_msg = f"""📈 Signal for {symbol} 📈
🚨 Direction: {best_direction}
🚨 Leverage: {lev}
🚨 Margin: Cross
🏦 CMP Entry: $ {entry1:.8f}"""

    for i, t in enumerate(targets[:n_show], 1):
        cornix_msg += f"\n💰 TP{i}: $ {t:.8f}"

    cornix_msg += f"\n💸 Stop Loss: $ {sl:.8f}\n🧠 AI Confidence: {best_conf * 100:.1f}% ({module_tag} Filter)"

    # HTML Visualisierung
    html_caption = f"""<pre>
<b>{emoji}</b>
<b>├─ Coin:</b> <b>{symbol}</b>
<b>├─ Action:</b> <b>{best_direction}</b>
<b>├─ Horizon:</b> <b>{best_horizon}</b>
<b>├─ RRR (T1):</b> <b>1:{rrr:.2f}</b>
<b>└─ ML Confidence:</b> <b>{strength} – {best_conf:.1%}</b>

<b>└─ Entry 1:</b> <b>${entry1:,.8f}</b>

<b>├─ Take Profits:</b>
"""
    for i, t in enumerate(targets[:n_show], 1):
        pct = abs((t - entry1) / entry1 * 100) * int(lev.replace('x', ''))
        t_col = "#00ff88" if i <= 2 else "#88ff88"
        html_caption += (
            f"<b style=\"color:{t_col};\">   T{i}:</b> <b>${t:,.8f}</b> → <b style=\"color:lime;\">+{pct:.1f}%</b>\n"
        )

    sl_loss = risk_pct * 100 * int(lev.replace('x', ''))
    # FIX double post (2026-07-06, fleet sweep): caption without an embedded
    # Cornix block — Cornix would otherwise parse both messages (rule 4).
    html_caption += f"""<b>└─ Stop Loss:</b> <b>${sl:,.8f}</b> → <b>-{sl_loss:.1f}%</b></pre>"""

    # Target channel routing
    target_channel = MIS_CHANNELS.get(best_horizon, _kcfg.CH_MIS_8H)  # Fallback
    chart_buf = generate_minichart_image(symbol, minutes=240)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
            (target_channel, cornix_msg),
        )
        if chart_buf:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                (target_channel, html_caption, chart_buf),
            )
        else:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                (target_channel, html_caption),
            )

        # entry_filled/expiry_hours come from the geometry: MIS2 SHORT = limit entry
        # (+5 % above market, entry_filled=FALSE until the monitor sees the fill,
        # expiry_hours=horizon); MIS1 + LONG = CMP entry (filled immediately, no expiry).
        cur.execute(
            """
            INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets, entry_filled, expiry_hours)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
            (
                symbol,
                float(entry1),
                module_tag,
                best_direction,
                float(best_conf),
                float(entry1),
                float(entry2),
                float(sl),
                json.dumps(targets[:n_show]),
                entry_filled,
                expiry_hours,
            ),
        )

        cur.execute(
            """
            INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
            VALUES (0, %s, %s, %s, %s, %s, %s, True)
        """,
            (module_tag, now, symbol, best_direction, float(current_price), float(best_conf)),
        )

    # Set cooldown so the same coin/direction does not fire again immediately.
    # P2.32: update_cooldown commits (default commit=True) and thereby closes the
    # ONE transaction of outbox posts + ai_signals + master log atomically.
    update_cooldown(conn, module_tag, symbol, best_direction)


def _process_mis_candidates(conn, idx, symbol, current_price, now, models, probs, legacy_generation):
    """Per-coin candidate selection + emission for ONE generation (MIS2 or MIS1 revive).

    Builds the candidates per loaded model from the batched probabilities
    (``probs``), ranks by distance to the model's OWN threshold (P2.33), checks
    the active-trade check + cooldown, fetches the geometry generation-dependent
    (:func:`_mis_geometry`) and routes via the shadow_gate register
    (:func:`route_legacy_leg`). ``legacy_generation`` is the constant generation
    for the active-trade legacy tag (MIS2→MIS3 rename protection; a no-op for MIS1)."""
    candidates = []
    for horizon, cfg in models.items():
        if not cfg["loaded"]:
            continue
        arr = probs.get(horizon)
        if arr is None:
            continue
        prob = arr[idx]
        # NaN = inference for this (coin, model) failed (batch fallback);
        # exactly as the old per-coin try/except dropped this one prediction.
        if not np.isfinite(prob):
            continue
        prob = float(prob)
        if prob >= 0.25:
            direction = "LONG" if "pump" in horizon.lower() else "SHORT"
            clean_horizon = horizon.upper().replace("_PUMP", "").replace("_DUMP", "")
            # Calibrated confidence (isotonic from the retrain artifact) for display/
            # logging; GATING still runs on the raw probability, because the
            # threshold was chosen on raw val probs. MIS1 models carry no
            # calibrator (bare XGBClassifier) → conf = raw probability.
            if cfg.get("calibrator") is not None:
                conf = float(np.clip(cfg["calibrator"].predict([prob])[0], 0.0, 1.0))
            else:
                conf = prob
            candidates.append((prob, clean_horizon, direction, cfg["threshold"], conf, cfg["generation"]))

    if not candidates:
        return

    # FIX P2.33: rank by distance to the MODEL'S OWN threshold, not by raw
    # probability — the 8 models are calibrated differently.
    candidates.sort(reverse=True, key=lambda x: x[0] - x[3])
    best_prob, best_horizon, best_direction, best_threshold, best_conf, best_generation = candidates[0]
    # Generation comes from the artifact meta of the WINNING model (T-2026-CU-9050-030);
    # for MIS1 revive it is the constant "MIS1".
    module_tag = f"{best_generation}-{best_horizon}"

    # 1. Active trade check — runs via the tag. legacy_tag catches the MIS2→MIS3
    #    rename (an open old position keeps blocking); for MIS1 legacy_tag ==
    #    module_tag → the IN is a no-op.
    legacy_tag = f"{legacy_generation}-{best_horizon}"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ai_signals WHERE symbol = %s AND direction = %s AND model IN (%s, %s)",
            (symbol, best_direction, module_tag, legacy_tag),
        )
        if cur.fetchone():
            return  # trade is running live in the AI monitor

    # --- APPLY LOGIC ---
    if best_prob < 0.25:
        return
    if best_prob < best_threshold:
        # Shadow mode (log sub-threshold prediction, no trade).
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM ml_predictions_master
                WHERE coin = %s AND direction = %s AND model_name = %s AND time > NOW() - INTERVAL '4 hours'
            """,
                (symbol, best_direction, module_tag),
            )
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                    VALUES (0, %s, %s, %s, %s, %s, %s, False)
                """,
                    (module_tag, now, symbol, best_direction, float(current_price), float(best_conf)),
                )
        conn.commit()  # P2.32: explicitly commit the shadow insert (autocommit is off)
        return

    # best_prob >= best_threshold → trade candidate.
    # 💥 Hard cooldown check (horizon lock per model). True = cooldown active → skip.
    cd_hours = int(best_horizon.replace("H", ""))
    if check_cooldown(conn, module_tag, symbol, best_direction, cd_hours):
        return

    logger.info(
        f"🚀 {best_generation} trade found: {symbol} {best_direction} | {module_tag} "
        f"(raw {best_prob:.3f} / calibrated {best_conf:.1%})"
    )

    entry1, entry2, sl, targets, entry_filled, expiry_hours = _mis_geometry(
        conn, best_generation, symbol, best_direction, best_horizon, current_price
    )

    # Fleet lifecycle gate (T-2026-KYT-9050-033/034). Default LIVE ⇒ no
    # behaviour change. The shadow_gate register controls which (tag, direction)
    # legs post live (Cornix) vs. run as a monitored shadow trade — for MIS2
    # AND the MIS1 revive legs. Purely additive on the post branch (rule 4).
    _route = route_legacy_leg(
        conn, module_tag, best_direction, symbol, float(best_conf), entry1, entry2, sl, targets, n_show=5
    )
    if _route != LEG_LIVE:
        if _route == LEG_SHADOW:
            conn.commit()
        return

    _post_mis_live_leg(
        conn,
        module_tag,
        best_horizon,
        best_direction,
        best_prob,
        best_threshold,
        best_conf,
        symbol,
        current_price,
        now,
        entry1,
        entry2,
        sl,
        targets,
        entry_filled=entry_filled,
        expiry_hours=expiry_hours,
    )


def check_mis_models():
    # FIX P2.32: no more autocommit — the outbox post, ai_signals insert and
    # master log belong in ONE transaction per signal (the commit is handled by
    # update_cooldown or the explicit commit in the shadow path). Before this,
    # a crash mid-way could leave a POSTED trade without tracking.
    conn = get_db_connection()

    try:
        with open('coins.json') as f:
            coins = json.load(f)
    except Exception as e:
        logger.error(f"Could not load coins.json: {e}")
        conn.close()  # release pool slot (review batch 4)
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    logger.info(f"🔍 Starting MIS1 Model Check for {len(coins)} coins...")

    # FIX: check ONCE before the coin loop whether any model is loaded at all.
    # Before, the check was inside the loop with `return` → the whole scan
    # aborted as soon as a single coin found no model.
    # T-2026-KYT-9050-034: only abort the scan if NEITHER MIS2 NOR MIS1 has a model
    # loaded — MIS1 is now a first-class generation (revive) that should also
    # run if (hypothetically) the MIS2 slots were empty.
    if not any(cfg["loaded"] for cfg in PUMP_MODELS.values()) and not any(
        cfg["loaded"] for cfg in MIS1_MODELS.values()
    ):
        logger.error("No MIS model loaded (neither MIS2 nor MIS1). Scan aborted.")
        conn.close()  # release pool slot (review batch 4)
        return

    # R1: live entry price via batch ticker (1 call/cycle), per-coin HTTP→DB fallback.
    price_map = get_live_prices_batch()

    conn_dead = False

    # === PHASE A: per-coin feature build (UNCHANGED math), collect for batched scoring ===
    # add_advanced_features stays PER COIN — its rolling windows must never span
    # coin boundaries. We only collect the finished 1-row feature frames so the
    # 8 models can be scored in one batched predict_proba each below.
    collected = []  # list of (symbol, df_current, current_price) in scan order
    for symbol in coins:
        try:
            df = _fetch_mis_frame(conn, symbol)
            if df is None:
                continue

            # include_legacy=True (T-2026-KYT-9050-034): SUPERSET frame (71 columns)
            # that serves BOTH generations — MIS2 selects its 63 clean
            # features by name (additively neutral, the 8 extra columns are never
            # chosen), the MIS1 revive models select their 67 (63 clean + 8 LEGACY_ONLY_COLS).
            # ONE feature build per coin for both generations — no duplicate DB read.
            df_features = add_advanced_features(df, include_legacy=True)
            # FIX P1.17 + R1: model features from the last CLOSED candle. The frame now
            # holds only closed candles (include_forming=False), so that is iloc[-1]
            # (was iloc[-2] when the forming bar was still the last row) — the forming
            # bar's stale/partial volume+indicator values are gone by construction.
            # Kept as a 1-row DataFrame for sklearn name validation, not a Series.
            # The entry price stays LIVE — batch ticker, not the candle close.
            df_current = df_features.iloc[-1:]
            live_price = price_map.get(symbol) or get_live_price(symbol, conn)
            if not live_price:
                continue
            collected.append((symbol, df_current, float(live_price)))
        except Exception as e:
            logger.error(f"Error building MIS features for {symbol}: {e}")
        finally:
            # Keep the read transaction clean between coins (P2.32): an aborted read
            # would poison every following coin, and a single open read transaction
            # across the whole scan would freeze NOW() on the scan start.
            try:
                conn.rollback()
            except Exception:
                logger.error("MIS1: rollback failed (dead connection) — aborting scan.")
                conn_dead = True
        if conn_dead:
            break

    if conn_dead or not collected:
        if conn:
            conn.close()
        logger.info("🏁 MIS1 Model Check stopped.")
        return

    # === PHASE B: ONE predict_proba per model over ALL collected coins ===
    # Was 527 coins x 8 models = 4216 single-row calls per scan; now 8 batched
    # calls. Row-independent XGBoost => byte-identical per-coin probabilities
    # (T-2026-CU-9050-186). Order of `probs[key]` matches `collected`.
    frames_for_score = [dc for (_, dc, _) in collected]
    loaded_models = {h: cfg for h, cfg in PUMP_MODELS.items() if cfg["loaded"]}
    probs_by_model = _score_models_batched(frames_for_score, loaded_models)

    # MIS1 revive (T-2026-KYT-9050-034): the same batched inference over the MIS1
    # models. The feature selection in _score_models_batched is name-based
    # (cfg["features"] = 67 MIS1 features), the superset frame delivers all of them.
    loaded_mis1 = {h: cfg for h, cfg in MIS1_MODELS.items() if cfg["loaded"]}
    probs_by_mis1 = _score_models_batched(frames_for_score, loaded_mis1) if loaded_mis1 else {}

    # === PHASE C: per-coin candidate build + posting (identical logic, per-coin txn) ===
    for idx, (symbol, _df_current, current_price) in enumerate(collected):
        try:
            # MIS2/MIS3 (existing generation): unchanged candidate selection + emit,
            # now via the shared processor. legacy_generation=MODEL_GENERATION
            # catches the MIS2→MIS3 rename in the active-trade check.
            _process_mis_candidates(
                conn, idx, symbol, current_price, now, PUMP_MODELS, probs_by_model, MODEL_GENERATION
            )
            # MIS1 revive (T-2026-KYT-9050-034): parallel generation under its own
            # tags MIS1-*, EXACTLY the same processing — only MIS1 models + MIS1
            # geometry (calculate_smart_targets both directions, see _mis_geometry).
            # Collision-free: own active-trade check + cooldown per tag; the
            # shadow_gate register controls which MIS1 legs post live.
            if loaded_mis1:
                _process_mis_candidates(
                    conn, idx, symbol, current_price, now, MIS1_MODELS, probs_by_mis1, MIS1_GENERATION
                )

        except Exception as e:
            logger.error(f"Error for {symbol} in MIS check: {e}")
        finally:
            # P2.32 + review batch 4: ALWAYS close the transaction per coin.
            # (a) An aborted transaction would otherwise poison all following coins
            #     ("current transaction is aborted", cf. P1.23).
            # (b) An open read transaction across the whole 538-coin scan
            #     freezes NOW() (= transaction_timestamp) at the scan start
            #     → telegram_outbox.created_at is backdated (the orchestrator
            #     staleness filter silently discards the signals) and cooldowns
            #     get shortened by the scan duration.
            # After a commit path, the rollback is a no-op.
            try:
                conn.rollback()
            except Exception:
                logger.error("MIS1: rollback failed (dead connection) — aborting scan.")
                conn_dead = True
        if conn_dead:
            break

    if conn:
        conn.close()
    logger.info("🏁 MIS1 Model Check stopped.")


def main():
    logger.info("=== 🧠 AI MIS BOT (Multi-Horizon) STARTED ===")

    # Table setup for cooldown
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_cooldowns (
                module VARCHAR(50),
                coin VARCHAR(20),
                direction VARCHAR(10),
                last_posted_at TIMESTAMP WITH TIME ZONE,
                PRIMARY KEY (module, coin, direction)
            );
        """)
    conn.commit()
    conn.close()

    load_pump_models()
    load_mis1_models()  # MIS1 revive (T-2026-KYT-9050-034): load legacy artifacts in parallel
    # P0.12 pattern: strictly check feature pipeline + model compatibility,
    # BEFORE the scan loop starts (incompatible legacy models are unloaded).
    # Checks both generations against the include_legacy superset.
    startup_feature_selfcheck()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        if now.minute == 11:
            check_mis_models()
            time.sleep(60)
        else:
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")

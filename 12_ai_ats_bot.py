import warnings

warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import datetime
import json
import logging
import os
import time

import joblib
import pandas as pd

from core import config as _kcfg  # channel ids
from core import shadow_gate
from core.ats_features import (
    ATS_CANDLE_COLUMNS,
    ATS_FEATURES,
    ATS_INDICATOR_COLUMNS,
    TSI_LINE_COL,
    TSI_SIGNAL_COL,
    ats_cross,
    build_ats_features,
)
from core.candles import history_start, read_candles_with_indicators
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.signal_post import has_open_ai_signal, log_prediction, post_ai_signal_gated
from core.trade_utils import (
    N_PUBLISHED_TARGETS,
    ensure_min_tp_distance,
    get_hvn_and_sr_levels,
    hvn_sr_trade_geometry,
    thin_targets,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_ATS_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & CHANNELS ---
AI_CHANNEL_ID = _kcfg.CH_ATS

# --- LOAD ML MODELS ---
TSI_MODEL_LONG_PATH = "model_tsi_long_robust.pkl"
TSI_MODEL_SHORT_PATH = "model_tsi_short_robust.pkl"
# Operating band (Audit Report 13/16): confidence calibration is INVERTED due to
# OBV train/serve skew — bucket 0.6-0.7 has live 71% WR, 0.8-0.9 only 57%.
# Therefore: post only in empirically best band [0.60, 0.80); >=0.80 goes to shadow.
TSI_THRESH_LONG = 0.60
TSI_THRESH_SHORT = 0.60
TSI_PROB_CAP = 0.80

# Feature contract + serving construction live in core.ats_features (ONE source
# with ATS2-replay/trainer, hard rule 7). Alias for readability below.
TSI_FEATURES = ATS_FEATURES

MODEL_LONG = None
MODEL_SHORT = None

# ATS2-shadow (T-2026-CU-9050-125): ATS1 retrain runs PARALLEL to ongoing
# live ATS1 and never posts live — only monitored shadow trades (contract
# artifact from staging_models/, loaded if present). Feature vector is
# identical to ATS1 serving (build_ats_features / ATS_FEATURES), so ATS2
# scores exactly the same event population.
SHADOW_ATS2: dict[str, object | None] = {"LONG": None, "SHORT": None}


def load_models():
    """Loads the TSI models once at startup (or hourly)."""
    global MODEL_LONG, MODEL_SHORT
    try:
        if os.path.exists(TSI_MODEL_LONG_PATH):
            MODEL_LONG = joblib.load(TSI_MODEL_LONG_PATH)
        else:
            logger.warning(f"Model missing: {TSI_MODEL_LONG_PATH}")

        if os.path.exists(TSI_MODEL_SHORT_PATH):
            MODEL_SHORT = joblib.load(TSI_MODEL_SHORT_PATH)
        else:
            logger.warning(f"Model missing: {TSI_MODEL_SHORT_PATH}")

        if MODEL_LONG and MODEL_SHORT:
            logger.info("✅ TSI Sniper models (ATS1) loaded successfully.")
    except Exception as e:
        logger.error(f"❌ Error loading TSI models: {e}")

    # Shadow models fail-soft loaded — if missing, bot 12 runs unchanged.
    for d in ("LONG", "SHORT"):
        SHADOW_ATS2[d] = shadow_gate.load_shadow_artifact("ATS2", d)
    if any(SHADOW_ATS2.values()):
        loaded = [d for d, m in SHADOW_ATS2.items() if m is not None]
        logger.info(f"👻 ATS2 shadow models loaded: {', '.join(loaded)}")


def _emit_ats2(conn, symbol, direction, is_long, feature_row, entry1, now):
    """ATS2 emission via shadow_gate routing (T-2026-CU-9050-125 → -033).

    Same TSI-crossover event and same feature vector as live ATS1 score.
    Fires ATS2 on RAW prob >= optimal_threshold, builds IDENTICAL HVN/S-R geometry
    like the live path and emits via ``post_ai_signal_gated``.

    T-2026-KYT-9050-033 (Audit T-032): ATS2 is SHADOW→LIVE promoted. Same function
    now routes both states (pattern like bot 9 _emit_sra2_shadow, bot 10
    _emit_epd3_shadow): shadow_gate decides LIVE (Cornix to CH_ATS + ai_signals)
    vs. SHADOW (monitored, no Cornix). Gate guard lets LIVE **and** SHADOW through;
    SILENT/RETIRED is filtered out. Under threshold: only the prediction line as before.
    Every error stays encapsulated — the live ATS1 path must NEVER be affected.

    DEPLOY PRECONDITION (Michi, hard rule 2): the LIVE leg loads its artifact from
    repo ROOT (shadow_artifact_path). As long as ats2_model_{LONG,SHORT}.pkl lies in
    staging_models/ instead of root, the loader returns None (``art is None`` → return)
    and ATS2 stays silent — promotion becomes active only with artifact move + restart.
    """
    if not shadow_gate.shadow_posting_enabled() or shadow_gate.leg_status("ATS2", direction) not in (
        shadow_gate.LIVE,
        shadow_gate.SHADOW,
    ):
        return
    art = SHADOW_ATS2.get(direction)
    if art is None:
        return
    try:
        prob = shadow_gate.score_artifact(art, feature_row)
        thr = shadow_gate.artifact_threshold(art)
        if thr is not None and prob < thr:
            if prob >= 0.25:  # SHADOW_FLOOR parity with ATS1 prediction log
                log_prediction(conn, "ATS2", symbol, direction, entry1, prob, posted=False)
                conn.commit()
            return
        # has_open guard (Review T-2026-KYT-9050-033, CRITICAL): the LIVE branch of
        # post_ai_signal_gated is post_ai_signal — it does NO has_open check and
        # NO cooldown. Without this guard, a persisting TSI crossover on EVERY 60s
        # scan would fire a double LIVE ATS2 post (rule-4 double trade) as long as
        # the crossover candle remains the newest closed one (~1 h). Pattern:
        # bot 9 _emit_sra2_shadow:199 / bot 10 _emit_epd3_shadow:201. Also covers
        # the SHADOW branch (saves expensive geometry before post_shadow's own has_open).
        if has_open_ai_signal(conn, symbol, direction, "ATS2"):
            return
        supps, resis = get_hvn_and_sr_levels(conn, symbol, entry1)
        entry2, sl, t_cands = hvn_sr_trade_geometry(entry1, is_long, supps, resis)
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
            "ATS2",
            direction,
            AI_CHANNEL_ID,  # LIVE leg → CH_ATS (T-033); SHADOW leg stays monitored-only
            symbol,
            prob,
            entry1,
            entry2,
            sl,
            targets,
            source_desc="AI ATS2 TSI-Sniper",
            n_show=3,
        )
        if outcome is not None:
            conn.commit()
    except Exception as e:
        logger.warning(f"ATS2 emission for {symbol} {direction} failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


# --- MAIN CHECKER FUNCTION ---
def check_tsi_crossovers():
    if not MODEL_LONG or not MODEL_SHORT:
        logger.error("Models not loaded. Skipping scan.")
        return

    conn = get_db_connection()
    try:
        with open('coins.json') as f:
            coins = json.load(f)
    except Exception as e:
        logger.error(f"Could not load coins.json: {e}")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    logger.info(f"🔍 Starting TSI Sniper (ATS1) scan for {len(coins)} coins...")

    for symbol in coins:
        try:
            # 1. FIX: previously only 50 candles → OBV always started at 0 and accumulated
            # over 50 candles — in training OBV was cumulated over entire history,
            # so systematic feature drift (train/test mismatch). Now we load 500 candles
            # AND normalize OBV to `obv - obv.iloc[0]`, so the absolute value is
            # independent of the data window start. R1: detection on CLOSED candles
            # (include_forming=False). TSI-crossover detection already ran on iloc[-2]
            # (closed); without the forming candle, the newest closed is now iloc[-1].
            # core.candles delivers ASC → previous DESC reversal is gone. (Transitional:
            # the 500-candle OBV baseline start shifts by exactly one candle — negligible
            # until ATS-retrain, §5 q6.)
            df = read_candles_with_indicators(
                conn,
                symbol,
                "1h",
                limit=500,
                # TimescaleDB chunk-exclusion hint (T-2026-CU-9050-180): the window
                # (safety=3 → ~62.5 d) holds well over the newest 500 closed 1h
                # candles for any listed pair, so the returned rows — and the OBV
                # baseline at iloc[0] below — are unchanged while the read stops
                # scanning all 126 chunks. The `< 50` guard does NOT enforce this
                # (it deliberately accepts short-history coins); parity rests on the
                # window margin — see history_start's parity caveat.
                start=history_start("1h", 500),
                include_forming=False,
                candle_columns=ATS_CANDLE_COLUMNS,
                indicator_columns=ATS_INDICATOR_COLUMNS,
            )
            if len(df) < 50:
                continue

            # Convert all columns to float
            num_cols = [c for c in df.columns if c != 'open_time']
            for col in num_cols:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

            # 2. CHECK CROSSOVER (newest CLOSED candle vs previous)
            # R1: frame no longer contains forming candle (include_forming=False),
            # so index -1 is the newest CLOSED candle, index -2 is the one before.
            # (Detection stays on the same candle as before iloc[-2].)
            current_idx = -1
            prev_idx = -2

            direction = ats_cross(
                df.iloc[prev_idx][TSI_LINE_COL],
                df.iloc[prev_idx][TSI_SIGNAL_COL],
                df.iloc[current_idx][TSI_LINE_COL],
                df.iloc[current_idx][TSI_SIGNAL_COL],
            )
            if direction is None:
                continue
            long_cross = direction == "LONG"

            # 3. LIVE FEATURE ENGINEERING — ONE source shared with the ATS2 trainer/replay
            # (core.ats_features.build_ats_features, hard rule 7). OBV normalisation
            # on the 500-candle window start, VWAP, the 29-feature contract and the
            # ordering all live there; tools/walkforward_sim.run_ats calls the same
            # function — the parity test proves trainer==serving.
            current_price = float(df.iloc[current_idx]['close'])
            features = build_ats_features(df)

            # Prediction DataFrame erstellen (Spaltenreihenfolge erzwingen)
            X_live = pd.DataFrame([features])
            X_live = X_live[TSI_FEATURES].fillna(0)

            if long_cross:
                prob_profit = float(MODEL_LONG.predict_proba(X_live)[0, 1])
                threshold = TSI_THRESH_LONG
            else:
                prob_profit = float(MODEL_SHORT.predict_proba(X_live)[0, 1])
                threshold = TSI_THRESH_SHORT

            module_tag = "ATS1"

            # ATS2 (T-2026-CU-9050-125 → -033): new generation scores in parallel and
            # emits BEFORE ATS1 band logic applies — ATS2 score is independent of ATS1
            # decision. Since T-033, ATS2 is LIVE promoted; shadow_gate routing in _emit_ats2
            # decides LIVE (Cornix) vs. SHADOW.
            _emit_ats2(conn, symbol, direction, long_cross, features, current_price, now)

            # ATS1 muted (T-2026-CU-9050-127, operator Michi): if ATS1 leg is set to
            # SILENT via shadow_gate, bot runs ONLY for ATS2 shadow collection — no ATS1
            # output (neither shadow log nor live post). Default LIVE ⇒ no-op as long as
            # ATS1 is not muted.
            if not shadow_gate.is_live(module_tag, direction):
                continue

            # --- SHADOW MODE LOGGING ---
            if prob_profit < 0.25:
                continue

            elif 0.25 <= prob_profit < threshold or prob_profit >= TSI_PROB_CAP:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                        VALUES (0, %s, %s, %s, %s, %s, %s, False)
                    """,
                        (module_tag, now, symbol, direction, float(current_price), prob_profit),
                    )
                conn.commit()

            elif prob_profit >= threshold:
                # 🔥 EXECUTE TRADE
                # Cooldown: 4h lock per coin/direction. check_cooldown returns True
                # if cooldown is STILL ACTIVE → skip.
                if check_cooldown(conn, module_tag, symbol, direction, 4):
                    logger.info(f"⏳ Cooldown active for {symbol} {direction} → skipped.")
                    continue

                logger.info(f"🔥 TRADE EXECUTE: {symbol} {direction} (ML {prob_profit:.1%})")

                is_long = direction == "LONG"
                entry1 = current_price
                supps, resis = get_hvn_and_sr_levels(conn, symbol, current_price)
                # ONE source with ATS2 replay (core.trade_utils.hvn_sr_trade_geometry;
                # byte-identical to previous inline geometry) → replay geometry ==
                # live geometry (hard rule 7). Entry2 = ±5 %, SL/TP from HVN/SR levels.
                entry2, sl, t_cands = hvn_sr_trade_geometry(entry1, is_long, supps, resis)

                # FIX: real zones + possibly 5% target if last zone too close
                targets = ensure_min_tp_distance(
                    thin_targets(t_cands[:20], entry1, is_long, keep=N_PUBLISHED_TARGETS),
                    entry1,
                    is_long,
                    min_pct=0.05,
                )
                # P2.31: publish AND track exactly the same targets. The Cornix block
                # shows the first n_show TPs; the AI monitor (8_ai_trade_monitor) scores
                # whatever is stored in ai_signals.targets. Storing the full 20-zone list
                # made the monitor score phantom TPs the subscriber never saw.
                n_show = 3

                lev = get_max_leverage(symbol, 20)
                # Cornix Text
                lines = [
                    f"📈 Signal for {symbol} 📈",
                    f"🚨 Direction: {direction}",
                    f"🚨 Leverage: {lev}",
                    "🚨 Margin: Cross",
                    f"🏦 CMP Entry: $ {entry1:.8f}",
                    # T-2026-KYT-9050-042: entry2 is still computed and stored, but no
                    # longer published — single-entry (arm B). See core/signal_post.py.
                ]
                for i, t in enumerate(targets[:n_show], 1):
                    lines.append(f"💰 TP{i}: $ {t:.8f}")
                lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module_tag} V3"]
                cornix_msg = "\n".join(lines)

                # HTML for chart
                emoji = "🚀 TSI-SNIPER LONG" if is_long else "💥 TSI-SNIPER SHORT"
                vol_trend_str = "YES" if features['volume_trend_up'] else "NO"

                # FIX double post (2026-07-06, fleet sweep): caption without
                # embedded Cornix block — Cornix parsed both messages.
                html_caption = f"""<pre><b>{emoji}</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n<b>→ Direction: {direction}</b>\n<b>→ Confidence: <b>{prob_profit:.1%}</b> (Thresh {threshold})</b>\n<b>→ Price: {current_price:.4f}</b>\n<b>→ Vol Trend Up: {vol_trend_str} | Spike: {features['volume_spike']}</b>\n<b>→ Time: {now.strftime('%H:%M')} UTC | Modul: ATS1 V3</b></pre>"""

                chart_buf = generate_minichart_image(symbol, minutes=240)
                with conn.cursor() as cur:
                    # Cornix Channel
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (AI_CHANNEL_ID, cornix_msg)
                    )
                    # Chart Channel
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

                    # AI Signal Monitor

                    cur.execute(
                        """
                                    INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                        (
                            symbol,
                            entry1,
                            module_tag,
                            direction,
                            float(prob_profit),
                            float(entry1),
                            float(entry2),
                            float(sl),
                            json.dumps(targets[:n_show]),
                        ),
                    )
                    # Master Log
                    cur.execute(
                        """INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted) VALUES (0, %s, %s, %s, %s, %s, %s, True)""",
                        (module_tag, now, symbol, direction, float(current_price), float(prob_profit)),
                    )

                conn.commit()
                # Cooldown setzen, damit gleicher Coin/Direction nicht sofort wieder feuert
                update_cooldown(conn, module_tag, symbol, direction)

        except Exception as e:
            logger.error(f"Error for {symbol} in ATS1: {e}")
            if conn:
                conn.rollback()

    if conn:
        conn.close()
    logger.info("🏁 ATS1 Model Check stopped.")


def main():
    logger.info("=== 🎯 AI TSI SNIPER (ATS1) GESTARTET ===")

    # 1. Modelle laden
    load_models()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        # P3.10: comments corrected to match code — fires at minute 13 (not 8).
        if now.minute == 13:
            check_tsi_crossovers()
            # Schlafen, damit er nicht mehrfach in Minute 13 triggert
            time.sleep(60)
        else:
            # Checkt alle 10 Sekunden, ob Minute 13 erreicht ist
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

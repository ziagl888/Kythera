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
from core import shadow_gate
from core.candles import read_candles, read_indicators
from core.charting import generate_minichart_image
from core.database import get_db_connection
from core.funding_features import funding_features_asof, load_funding
from core.market_utils import check_cooldown, get_max_leverage, update_cooldown
from core.rub_features import build_rub_features, rub_event_type, rub_trend
from core.signal_post import LEG_LIVE, LEG_SHADOW, post_shadow_ai_signal, route_legacy_leg
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels

logging.basicConfig(level=logging.INFO, format='%(asctime)s - AI_RUB_BOT - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & CHANNELS ---
# You can set the special Rubberband channel here
RUBBERBAND_CHANNEL_ID = _kcfg.CH_RUBBERBAND

# --- LOAD ML MODELS ---
# RUB1-Revive (T-2026-KYT-9050-037, operator decision Michi from bot_results.xlsx):
# Bot 13 runs both directions again on the original legacy reversion models
# and posts them live under the original tag RUB1 (LONG 2.48% / SHORT 0.78%,
# historically both positive). This reverts (a) the T-030 LONG tag rename (→ RUB2)
# and (b) the PR-#9 removal of the legacy SHORT branch (rub2_model_SHORT retrain).
# The RUB2 retrain is benched: RUB2 remains in the shadow_gate register as SHADOW (both
# directions, block E); the RUB3/RUB4 LONG challenger runs unchanged as shadow.
MODEL_LONG_PATH = 'long_reversion_model.joblib'
MODEL_SHORT_PATH = 'short_reversion_model.joblib'
# Original RUB1 thresholds on the raw predict_proba of the legacy models (9 rub
# features, NO funding — parity with pre-PR-#9 logic, git 07c8874^). Deliberately
# not reinvented.
REVERSION_THRESH_LONG = 0.75
REVERSION_THRESH_SHORT = 0.85
# Posting tag for both directions: the original RUB1. The legacy models carry no
# artifact metadata, so a named constant (no more meta.model_id lookup).
RUB_TAG = "RUB1"
# The tag under which this bot last posted (RUB2 generation, T-030…T-033) and
# under which open trades/cooldowns may still exist. Only for the transitional
# dedup across the tag switch RUB2 → RUB1 (active trade check + cooldown, rule 4) —
# to prevent double-posting while old RUB2 positions are still open.
RUB_LEGACY_TAG = "RUB2"

MODEL_LONG = None
MODEL_SHORT = None

# RUB3-Shadow (T-2026-CU-9050-125): the rub2_model_LONG retrain was "not deployable"
# (no positive LONG operating point). The LIVE LONG leg continues to run the legacy
# model and posts under tag "RUB2"; the retrain shadow therefore runs in PARALLEL
# under its own generation tag "RUB3" (operator decision Michi, rule 6) — never live,
# only monitored shadow trades to show whether the clean retrain beats the legacy LONG
# (regime question §8/part 3). The tag differs per direction from any potential future
# RUB3-SHORT.
SHADOW_RUB3_LONG = None

# RUB4 (T-2026-CU-9050-164): funding-gated RUB LONG as a shadow experiment.
# Retrospectively (123 closed RUB LONG trades), the ABR1 funding gate turns the
# aggregate from −2.9%/trade to positive (+1.6%), but only 6/123 trades pass it
# → thin, must be forward-validated. RUB4 emits the same RUB3 candidate,
# but ONLY if fund_24h > +3 bps (ABR1 LONG threshold) — own tag so the
# report can compare gated (RUB4) vs. ungated (RUB3). Purely additive, never live.
FUNDING_GATE_LONG_BPS = 3.0
RUB4_GATED_LONG_TAG = "RUB4"


def funding_gate_open(fund_24h_bps) -> bool:
    """True if the ABR1 funding gate is open (fund_24h > +3 bps). Pure →
    DB-free testable. None (no funding data) ⇒ gate closed (no RUB4 post)."""
    return fund_24h_bps is not None and fund_24h_bps > FUNDING_GATE_LONG_BPS


def load_models():
    """Load the Mean Reversion models (RUB1 Legacy, both directions)."""
    global MODEL_LONG, MODEL_SHORT
    try:
        if os.path.exists(MODEL_LONG_PATH):
            MODEL_LONG = joblib.load(MODEL_LONG_PATH)
            logger.info("✅ Rubberband LONG model (Legacy RUB1) loaded successfully.")
        else:
            logger.warning(f"Model missing: {MODEL_LONG_PATH} — LONG side disabled.")
    except Exception as e:
        logger.error(f"❌ Error loading LONG model: {e} — LONG side disabled.")

    try:
        if os.path.exists(MODEL_SHORT_PATH):
            MODEL_SHORT = joblib.load(MODEL_SHORT_PATH)
            logger.info("✅ Rubberband SHORT model (Legacy RUB1) loaded successfully.")
        else:
            logger.warning(f"Model missing: {MODEL_SHORT_PATH} — SHORT side disabled.")
    except Exception as e:
        logger.error(f"❌ Error loading SHORT model: {e} — SHORT side disabled.")

    global SHADOW_RUB3_LONG
    SHADOW_RUB3_LONG = shadow_gate.load_shadow_artifact("RUB3", "LONG")
    if SHADOW_RUB3_LONG is not None:
        logger.info("👻 RUB3 (rub2_model_LONG) shadow model loaded.")


def _emit_rub3_shadow(conn, symbol, curr_close, base_features, now):
    """RUB3 shadow emission (T-2026-CU-9050-125) — purely additive, never live.

    Scores the same LONG prefilter candidate as the live legacy LONG path, but
    with the clean rub2_model_LONG retrain (15 features = 9 rub + 6 funding,
    funding as-of the candle close like the SHORT side/replay). Threshold is
    null (no deployable operating point) → each candidate is tracked as a monitored
    shadow trade under tag ``RUB3`` (no Cornix). Geometry = same LONG HVN/S-R
    construction as the live path (deliberately duplicated). Errors remain
    encapsulated — the live RUB path must never be affected.
    """
    if not shadow_gate.shadow_posting_enabled() or not shadow_gate.is_shadow("RUB3", "LONG"):
        return
    if SHADOW_RUB3_LONG is None:
        return
    try:
        feats = dict(base_features)
        ts_decision = now.replace(minute=0, second=0, microsecond=0)
        fund_by_sym = load_funding(conn, [symbol], since=now - datetime.timedelta(days=95))
        feats.update(funding_features_asof(fund_by_sym, symbol, ts_decision))
        prob = shadow_gate.score_artifact(SHADOW_RUB3_LONG, feats)
        thr = shadow_gate.artifact_threshold(SHADOW_RUB3_LONG)
        if thr is not None and prob < thr:
            return
        entry1 = curr_close
        entry2 = entry1 * 0.95
        supps, resis = get_hvn_and_sr_levels(conn, symbol, curr_close)
        sl = max([x for x in supps if x < entry2 * 0.99]) if any(x < entry2 * 0.99 for x in supps) else entry2 * 0.975
        t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
        targets = ensure_min_tp_distance(t_cands[:20], entry1, True, min_pct=0.05)
        if not targets:
            return
        wrote = post_shadow_ai_signal(conn, "RUB3", symbol, "LONG", prob, entry1, entry2, sl, targets, n_show=3)
        # RUB4 funding gate variant: same setup, but ONLY if fund_24h > +3 bps
        # (feats["fund_24h"] is already computed). Tests whether the gate
        # saves the RUB LONG side. Own tag, fail-safe to silence if not SHADOW.
        if funding_gate_open(feats.get("fund_24h")) and shadow_gate.is_shadow(RUB4_GATED_LONG_TAG, "LONG"):
            if post_shadow_ai_signal(
                conn, RUB4_GATED_LONG_TAG, symbol, "LONG", prob, entry1, entry2, sl, targets, n_show=3
            ):
                wrote = True
        if wrote:
            conn.commit()
    except Exception as e:
        logger.warning(f"RUB3 shadow for {symbol} failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


# --- MAIN CHECKER FUNCTION ---
def check_rubberband_conditions():
    # Decoupled guard (PR-#9 pattern, preserved): a missing legacy model for one
    # direction must not shut down the other. Direction guards in the loop
    # skip the non-loadable side individually (MODEL_LONG / MODEL_SHORT is None).
    if not (MODEL_LONG or MODEL_SHORT):
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
    logger.info(f"🔍 Starting Rubberband (RUB1) scan for {len(coins)} coins...")

    for symbol in coins:
        try:
            # USDT filter (prevents error for USDC pairs)
            if 'USDT_' in symbol:
                continue

            # 1. Fetch 90 days of data for trend calculation — detection runs on
            # closed candles (R1). core.candles with include_forming=False replaces
            # the previous `open_time < date_trunc('hour', NOW())` filter (P1.19):
            # the central closed-cutoff is identical for 1h (period_start = hour floor).
            df_90d = read_candles(
                conn,
                symbol,
                "1h",
                start=now - datetime.timedelta(days=95),
                include_forming=False,
                columns=("open_time", "close"),
            )
            if len(df_90d) < 50:
                continue

            # 2. Last closed indicator candle — include close so curr_close
            # comes from the SAME candle as the indicators (P1.19). include_forming=False
            # replaces the closed-candle filter; open_time only for ordering, removed after.
            df_ind = read_indicators(
                conn,
                symbol,
                "1h",
                limit=1,
                include_forming=False,
                columns=(
                    "open_time",
                    "close",
                    "rsi_14",
                    "tsi_fast_12_7_7",
                    "tsi_fast_12_7_7_signal",
                    "macd_dif_normal_12_26_9",
                    "macd_dea_normal_12_26_9",
                    "atr_14",
                    "ema_200",
                    "donchian_lower_20",
                    "donchian_upper_20",
                ),
            )
            if df_ind.empty:
                continue
            ind = df_ind.iloc[-1].drop("open_time").to_dict()

            # --- TREND CALCULATION ---
            # Regression + prefilter + feature building have lived in core/rub_features
            # since the RUB2 adapter (2026-07-06) — ONE source for both bot and
            # walkforward replay (X-R1 rule), like find_break_retest_setups in ABR.
            df_90d['ts'] = pd.to_datetime(df_90d['open_time'], utc=True).apply(lambda x: x.timestamp())
            ts_values = df_90d['ts'].values
            close_values = df_90d['close'].values.astype(float)

            # P1.19: curr_close from the closed indicator candle (ind['close']),
            # not from the 90d price array — so dist_to_trend + all ML features
            # no longer mix live price with partial indicators. Fallback to the (now
            # also closed) last 90d candle if close is NaN/missing.
            try:
                curr_close = float(ind['close'])
                if not np.isfinite(curr_close):
                    curr_close = float(close_values[-1])
            except (TypeError, ValueError, KeyError):
                curr_close = float(close_values[-1])

            dist_to_trend_pct, slope_pct_per_day = rub_trend(ts_values, close_values, curr_close)

            # --- READ INDICATORS ---
            def get_f(key, default=0.0, ind=ind):
                val = ind.get(key)
                # FIX: Previously only checked for `None`. But pandas/postgres can
                # deliver NaN/Inf (especially for fresh coins with little history).
                # If these flow into ML features, predict_proba crashes or
                # delivers unusable values. Now: also NaN/Inf → default.
                try:
                    if val is None:
                        return default
                    fv = float(val)
                    if not np.isfinite(fv):
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

            # --- PREFILTERING (RUBBERBAND CONDITIONS) — shared source ---
            event_type = rub_event_type(dist_to_trend_pct, rsi, tsi_line, curr_close, dc_lower, dc_upper)
            if not event_type:
                continue

            # --- CALCULATE ML FEATURES — shared source ---
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

            is_long = event_type == "REVERSION_UP"
            direction = "LONG" if is_long else "SHORT"
            # Posting tag for both directions: the original RUB1 (T-2026-KYT-9050-037
            # revive). Both sides run the legacy reversion model again without
            # artifact metadata, so a single named constant — no direction-dependent
            # meta.model_id lookup anymore (that applied to the RUB2-SHORT generation,
            # now benched). The RUB3/RUB4 LONG challenger posts under its own
            # tag (see _emit_rub3_shadow) and doesn't collide with RUB1.
            module_tag = RUB_TAG

            # 1. Active trade check (T-2026-CU-9050-043) — checks whether for this exact
            #    module/coin/direction a non-closed trade is already running.
            #    The cooldown underneath is a frequency lock (4h), not a position
            #    guard: a RUB trade in mean reversion regularly runs longer than
            #    its cooldown, and without this check the next signal would open a
            #    SECOND live position beside the first. Pattern: 11_ai_mis_bot.py.
            #
            #    The check runs on the tag, and the tag switches with the RUB1 revive
            #    (RUB2 → RUB1, T-037). Without the old tag in the IN, an still-open
            #    RUB2 position would no longer block the same coin/direction → possible
            #    double-post across the tag switch (rule 4).
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM ai_signals
                    WHERE symbol = %s AND direction = %s AND model IN (%s, %s)
                """,
                    (symbol, direction, module_tag, RUB_LEGACY_TAG),
                )
                trade_exists = cur.fetchone()

            if trade_exists:
                continue  # Trade is running live in AI Monitor

            # 2. FIX: Cooldown check BEFORE the expensive ML prediction.
            # Previously predict_proba ran even when the coin was still on cooldown
            # (with 500 coins × multiple event types = a lot of wasted CPU).
            # The shadow log below is preserved — it documents all potential
            # trades, including rejected ones. When skipping due to cooldown
            # we still log for monitoring.
            # Transitional dedup (T-037 revive): the cooldown key is the tag, and it
            # switches with the RUB1 revive (RUB2 → RUB1). A fresh RUB2 cooldown row
            # would no longer block a RUB1 signal on the same coin. So
            # check additionally against the old tag. The same transitional logic
            # supports the active trade check above — both locks must survive the
            # generation switch, otherwise the protection breaks at the other place.
            cooldown_tags = [module_tag] if module_tag == RUB_LEGACY_TAG else [module_tag, RUB_LEGACY_TAG]
            if any(check_cooldown(conn, t, symbol, direction, 4) for t in cooldown_tags):
                logger.debug(f"RUB1 prediction for {symbol} {direction} on cooldown — skip.")
                continue

            # Prediction (expensive, only after cooldown check). Both directions run the
            # original legacy reversion model on the 9 rub features (NO funding) with
            # their original threshold — parity with pre-PR-#9 RUB1 logic (git 07c8874^).
            if is_long:
                if MODEL_LONG is None:
                    continue
                threshold = REVERSION_THRESH_LONG
                prob = MODEL_LONG.predict_proba(pd.DataFrame([base_features]))[0, 1]
            else:
                if MODEL_SHORT is None:
                    continue
                threshold = REVERSION_THRESH_SHORT
                prob = MODEL_SHORT.predict_proba(pd.DataFrame([base_features]))[0, 1]

            logger.info(f"RUB1 Trigger: {symbol} {direction} | ML-Conf: {prob:.1%} (Thresh: {threshold:.2f})")

            # RUB3 shadow (T-2026-CU-9050-125): score the same LONG candidate with the
            # clean retrain + track it monitored, independent of the live path.
            if is_long:
                _emit_rub3_shadow(conn, symbol, curr_close, base_features, now)

            # --- SHADOW MODE LOGGING ---
            # Direction gate removed (operator 2026-07-06): LONG trades again
            # (audit batch had placed LONG in shadow per report 14 D.5).
            if prob < threshold:
                # Store in master table (as rejected trade)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                        VALUES (0, %s, %s, %s, %s, %s, %s, False)
                    """,
                        (module_tag, now, symbol, direction, float(curr_close), float(prob)),
                    )
                conn.commit()
                continue

            # 🔥 EXECUTE TRADE
            logger.info(f"🔥 RUB1 TRADE EXECUTE: {symbol} {direction} (ML {prob:.1%})")

            entry1 = curr_close
            entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
            supps, resis = get_hvn_and_sr_levels(conn, symbol, curr_close)

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

            # FIX: Previously `while len(targets) < 20: append last*1.02` → extrapolated
            # up to +48% above entry, absurd for mean-reversion bots. Now only:
            # take real zones, and if needed add ONE 5% target if the last one
            # is too close to entry.
            targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)
            # P2.31: publish AND track exactly the same targets. The Cornix block
            # shows the first n_show TPs; the AI monitor (8_ai_trade_monitor) scores
            # whatever is stored in ai_signals.targets. Storing the full 20-zone list
            # made the monitor score phantom TPs the subscriber never saw.
            n_show = 3

            # Fleet lifecycle gate (T-2026-KYT-9050-033) at the emission point.
            # module_tag == RUB1 is explicitly LIVE in the register (T-037, defense-in-depth)
            # ⇒ route_legacy_leg returns LEG_LIVE and the bot posts as below (Cornix
            # + ai_signals). The benched RUB2 generation remains SHADOW; the
            # RUB3/RUB4 LONG challenger unchanged shadow (above, _emit_rub3_shadow).
            # Purely additive (rule 4).
            _route = route_legacy_leg(
                conn, module_tag, direction, symbol, prob, entry1, entry2, sl, targets, n_show=n_show
            )
            if _route != LEG_LIVE:
                if _route == LEG_SHADOW:
                    conn.commit()
                continue

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
            lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module_tag}"]
            cornix_msg = "\n".join(lines)

            # HTML for chart
            emoji = "🚀 RUBBERBAND MEAN REVERSION LONG" if is_long else "💥 RUBBERBAND MEAN REVERSION SHORT"
            dist_str = f"{dist_to_trend_pct * 100:+.2f}%"

            # FIX double-post (2026-07-06, same error class as bots 18/7):
            # Chart caption without embedded Cornix block.
            html_caption = f"""<pre><b>{emoji}</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n<b>→ Direction: {direction}</b>\n<b>→ Confidence: <b>{prob:.1%}</b> (Thresh {threshold})</b>\n<b>→ Price: {curr_close:.4f}</b>\n<b>→ Trend Distance: <b>{dist_str}</b></b>\n<b>→ Time: {now.strftime('%H:%M')} UTC | Modul: {module_tag}</b></pre>"""

            chart_buf = generate_minichart_image(symbol, minutes=240)
            with conn.cursor() as cur:
                # Cornix Channel (Here it uses the special Rubberband channel!)
                cur.execute(
                    "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                    (RUBBERBAND_CHANNEL_ID, cornix_msg),
                )
                # Chart Channel
                if chart_buf:
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                        (RUBBERBAND_CHANNEL_ID, html_caption, chart_buf),
                    )
                else:
                    cur.execute(
                        "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                        (RUBBERBAND_CHANNEL_ID, html_caption),
                    )

                # AI Signal Monitor

                cur.execute(
                    """
                                INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                    (
                        symbol,
                        float(entry1),
                        module_tag,
                        direction,
                        float(prob),
                        float(entry1),
                        float(entry2),
                        float(sl),
                        json.dumps(targets[:n_show]),
                    ),
                )
                # Master Log
                cur.execute(
                    """INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted) VALUES (0, %s, %s, %s, %s, %s, %s, True)""",
                    (module_tag, now, symbol, direction, float(curr_close), float(prob)),
                )

            conn.commit()
            update_cooldown(conn, module_tag, symbol, direction)

        except Exception as e:
            logger.error(f"Error for {symbol} in RUB1: {e}")
            if conn:
                conn.rollback()

    if conn:
        conn.close()
    logger.info("🏁 RUB1 Model Check stopped.")


def main():
    logger.info("=== 🎯 AI RUBBERBAND BOT (RUB1) STARTED ===")

    # Load models
    load_models()

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)

        # P3.10: comments corrected to match code — fires at minute 10 (not 12).
        if now.minute == 10:
            check_rubberband_conditions()
            # Sleep so it doesn't trigger multiple times in minute 10
            time.sleep(60)
        else:
            # Check every 10 seconds if minute 10 is reached
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped (Ctrl+C). Shutting down cleanly...")

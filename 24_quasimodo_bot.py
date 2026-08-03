import warnings

warnings.filterwarnings("ignore")

import json
import logging
import os
import time
from datetime import datetime, timezone

import joblib
import matplotlib

matplotlib.use('Agg')  # P3.8: headless VPS has no display — set before pyplot import
import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd
import scipy.signal

from core import config as _kcfg  # channel ids

# --- Import own DB connection ---
from core.candles import history_start, read_candles_with_indicators
from core.database import get_db_connection
from core.live_price import get_live_price, get_live_prices_batch
from core.market_utils import check_cooldown, get_max_leverage, load_coins, update_cooldown
from core.signal_post import LEG_LIVE, LEG_SHADOW, route_legacy_leg

# 🛠️ CONFIGURATION
logging.basicConfig(level=logging.INFO, format='%(asctime)s - QM_SNIPER - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_CHANNEL_ID = _kcfg.CH_INSTITUTIONAL

COINS_FILE = "coins.json"
CHART_DIR = "generated_charts"
os.makedirs(CHART_DIR, exist_ok=True)

# PARKED 4h (audit report 14/16): QM_4H is net negative (−277, 54.9% WR)
# and is on the stop list; QM_1H keeps running (redesign candidate).
TIMEFRAMES = ['1h']
MODEL_PATHS = {'1h': "qm_xgboost_model_1h.pkl"}

MIN_CONFIDENCE = 0.65  # FIX: previously 0.40 → far too low, poor expected value.
ZONE_TOLERANCE = 0.005  # FIX: previously 0.01 (1%) → too wide. 0.5% is a clean retest range.
PIVOT_WINDOW = 5

PRICE_BASED_INDICATORS = [
    'ema_9',
    'ema_21',
    'ema_50',
    'ema_200',
    'kama_21',
    'wma_21',
    'donchian_upper_20',
    'donchian_lower_20',
    'donchian_mid_20',
    'boll_upper_20',
    'boll_lower_20',
]
ABSOLUTE_INDICATORS = ['rsi_14', 'tsi_25_13_13', 'macd_dif_normal_12_26_9', 'macd_dea_normal_12_26_9']

# 🧠 LOAD DUAL ML MODELS
ML_MODELS = {}
for tf, path in MODEL_PATHS.items():
    try:
        ml_data = joblib.load(path)
        # Posting tag from the artifact meta, otherwise derived (T-2026-CU-9050-030).
        # Since T-2026-CU-9050-061 qm_ml_trainer.py writes the model_id
        # f"QM2_{tf.upper()}" into the meta — a QM2 retrain thereby posts as QM2_1H
        # instead of silently merging into the old tag QM_1H with the QM1 statistics
        # that the orchestrator gating decides on (rule 6). The derived
        # QM_1H remains the fallback for old artifacts without model_id.
        # The orchestrator already recognises QM2_1H (QM\d*_ in BOT_IDENTIFICATION_PATTERNS).
        meta = ml_data.get('meta') or {}
        model_id = str(meta.get('model_id') or ml_data.get('model_id') or "").strip()
        ML_MODELS[tf] = {
            'model': ml_data['model'],
            'features': ml_data['features'],
            'tag': model_id or f"QM_{tf.upper()}",
        }
        logger.info(
            f"✅ ML model for {tf} loaded successfully. Features: {len(ml_data['features'])}, "
            f"Tag: {ML_MODELS[tf]['tag']}{'' if model_id else ' (derived — artifact without model_id)'}"
        )
    except Exception as e:
        logger.critical(f"❌ Could not load model for {tf} ({path}): {e}")
        exit(1)


def scan_market():
    coins = load_coins()
    conn = get_db_connection()
    conn.autocommit = True  # Prevents database locks
    now = datetime.now(timezone.utc)

    # R1: live price for the QM-proximity/entry gates — batch ticker (1 call/cycle),
    # per-coin HTTP→DB fallback on miss (core.live_price).
    price_map = get_live_prices_batch()

    for tf in TIMEFRAMES:
        module_tag = ML_MODELS[tf]['tag']  # artifact tag, not derived from tf
        # Transitional dedup (T-2026-CU-9050-030): the active-trade check runs on
        # the tag, and that changes on the QM2 rollout (QM_1H → QM2_1H). Without the old tag,
        # an open QM_1H position would no longer block the same coin/direction — the
        # QM2 run would open a SECOND live position next to it. legacy_tag is exactly the
        # tag this bot would have posted before the fix; as long as no QM2 artifact
        # is deployed, both are identical and the IN is a no-op.
        legacy_tag = f"QM_{tf.upper()}"
        logger.info(f"🔍 Starting QM scan for timeframe: {tf}")

        current_model = ML_MODELS[tf]['model']
        expected_features = ML_MODELS[tf]['features']

        for symbol in coins:
            try:
                # R1: detect on CLOSED candles only. read_candles_with_indicators does
                # the candle⋈indicator JOIN, returns ASC and drops the forming bar, so
                # pivots no longer repaint and the manual DESC-reverse is gone.
                # 't1.volume' is kept in candle_columns (needed by the chart); 'symbol'
                # is EXCLUDED so the float-cast loop below stays valid.
                indicator_cols = PRICE_BASED_INDICATORS + ABSOLUTE_INDICATORS + ['atr_14', 'trend_direction']
                df = read_candles_with_indicators(
                    conn,
                    symbol,
                    tf,
                    limit=100,
                    # TimescaleDB chunk-exclusion hint (T-2026-CU-9050-180): window
                    # scoped to `tf` holds the newest 100 closed candles unchanged
                    # while pruning the bulk of the 126 chunks.
                    start=history_start(tf, 100),
                    include_forming=False,
                    candle_columns=("open_time", "open", "high", "low", "close", "volume"),
                    indicator_columns=indicator_cols,
                )
                if len(df) < 50:
                    continue

                df.ffill(inplace=True)
                df.bfill(inplace=True)

                for c in df.columns:
                    if c not in ['open_time', 'trend_direction']:
                        df[c] = df[c].astype(float)

                highs, lows, closes = df['high'].values, df['low'].values, df['close'].values
                # Detection is on closed candles; the QML-proximity/SL/zone gates and the
                # entry need the LIVE price — batch ticker with per-coin HTTP→DB fallback.
                current_price = price_map.get(symbol) or get_live_price(symbol, conn)
                if not current_price:
                    continue
                current_price = float(current_price)

                # P1.24 + R1: the frame already holds only CLOSED candles, so the pivot
                # search runs on the full array — no forming-bar slice.
                c_highs, c_lows = highs, lows

                peak_idx = scipy.signal.argrelextrema(c_highs, np.greater, order=PIVOT_WINDOW)[0]
                trough_idx = scipy.signal.argrelextrema(c_lows, np.less, order=PIVOT_WINDOW)[0]

                # P1.24: discard edge pivots — argrelextrema (mode='clip') lets
                # unconfirmed pivots through at the right edge; a pivot needs PIVOT_WINDOW
                # following candles to confirm.
                max_confirmed_idx = len(c_highs) - 1 - PIVOT_WINDOW
                peak_idx = peak_idx[peak_idx <= max_confirmed_idx]
                trough_idx = trough_idx[trough_idx <= max_confirmed_idx]

                raw_pivots = [(i, 1, c_highs[i]) for i in peak_idx] + [(i, -1, c_lows[i]) for i in trough_idx]
                raw_pivots.sort(key=lambda x: x[0])

                alt_pivots = []
                for p in raw_pivots:
                    if not alt_pivots:
                        alt_pivots.append(p)
                    elif alt_pivots[-1][1] == p[1]:
                        if (p[1] == 1 and p[2] > alt_pivots[-1][2]) or (p[1] == -1 and p[2] < alt_pivots[-1][2]):
                            alt_pivots[-1] = p
                    else:
                        alt_pivots.append(p)

                if len(alt_pivots) < 4:
                    continue

                p1, p2, p3, p4 = alt_pivots[-4], alt_pivots[-3], alt_pivots[-2], alt_pivots[-1]
                direction, qm_level, sl_level, tp_level = None, 0, 0, 0

                if p1[1] == 1 and p2[1] == -1 and p3[1] == 1 and p4[1] == -1:
                    H, L, HH, LL = p1[2], p2[2], p3[2], p4[2]
                    if HH > H and LL < L:
                        qm_level, sl_level, tp_level, direction = H, HH * 1.003, LL, 'SHORT'

                elif p1[1] == -1 and p2[1] == 1 and p3[1] == -1 and p4[1] == 1:
                    L, H, LL, HH = p1[2], p2[2], p3[2], p4[2]
                    if LL < L and HH > H:
                        qm_level, sl_level, tp_level, direction = L, LL * 0.997, HH, 'LONG'

                if direction:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT 1 FROM ai_signals
                            WHERE symbol = %s AND direction = %s AND model IN (%s, %s)
                        """,
                            (symbol, direction, module_tag, legacy_tag),
                        )
                        trade_exists = cur.fetchone()

                    if trade_exists:
                        continue

                    dist_to_qml = abs(current_price - qm_level) / qm_level

                    if direction == 'SHORT' and current_price >= sl_level:
                        continue
                    if direction == 'LONG' and current_price <= sl_level:
                        continue

                    # FIX: real retest confirmation instead of a mere proximity check.
                    # Previously the bot fired as soon as the current price was within
                    # 1% of the QML — even if the level was never touched.
                    # Now: the last 3 closed candles must have
                    # touched the QML (high/low within the zone) AND the current
                    # price must be on the "correct" side of the level.
                    touched_recently = False
                    zone_upper = qm_level * (1 + ZONE_TOLERANCE)
                    zone_lower = qm_level * (1 - ZONE_TOLERANCE)
                    for k in range(0, min(3, len(df))):  # R1: last 3 closed candles (forming dropped → from k=0)
                        c_high_k = highs[-1 - k]
                        c_low_k = lows[-1 - k]
                        if c_low_k <= zone_upper and c_high_k >= zone_lower:
                            touched_recently = True
                            break

                    if not touched_recently:
                        continue

                    # Additionally: price must now be moving on the trade side of the QML.
                    # SHORT setup: QM_level is resistance → current price should
                    # be below it or slightly above, but not far away after breaking above.
                    # LONG setup:  QM_level is support → current price above it.
                    if direction == 'SHORT' and current_price > zone_upper:
                        continue
                    if direction == 'LONG' and current_price < zone_lower:
                        continue

                    if dist_to_qml <= ZONE_TOLERANCE * 2:  # real zone stays more generous
                        feature_idx = len(df) - 1  # R1: last row is now the last CLOSED candle (forming dropped)
                        close_prev = closes[feature_idx]

                        features = {
                            'dir_num': 1 if direction == 'LONG' else 0,
                            'atr_14_pct': (df['atr_14'].iloc[feature_idx] / close_prev) * 100,
                        }

                        for ind in ABSOLUTE_INDICATORS:
                            features[ind] = df[ind].iloc[feature_idx]

                        for ind in PRICE_BASED_INDICATORS:
                            features[f"{ind}_dist_pct"] = ((df[ind].iloc[feature_idx] - close_prev) / close_prev) * 100

                        trend = str(df['trend_direction'].iloc[feature_idx])
                        features['trend_UP'] = 1 if trend == 'UP' else 0
                        features['trend_DOWN'] = 1 if trend == 'DOWN' else 0
                        features['trend_SIDEWAYS'] = 1 if trend == 'SIDEWAYS' else 0

                        # T-2026-CU-9050-060 (F4): impute non-finite values (inf/NaN → 0)
                        # like every core/*_features.py builder — and like this bot's own
                        # trainer, which fits and scores on .fillna(0) frames
                        # (qm_ml_trainer.py:321/353/378): exact NaN parity (inf→0 is
                        # deliberately stricter — bare fillna(0) leaves inf, which is
                        # unreachable here by construction). The
                        # XGB model would NOT crash on NaN — it routes NaN down untrained
                        # default branches, a silent skew. Reachable: ffill().bfill()
                        # above leaves NaN in all-NaN columns, which arise not only from
                        # frozen windows (those yield 0 pivots, the scan bails earlier)
                        # but also when the LEFT JOIN finds no indicator rows for the
                        # whole window (engine outage/coverage gap) while price pivots
                        # still exist.
                        features = {k: (float(v) if np.isfinite(v) else 0.0) for k, v in features.items()}

                        ml_input = pd.DataFrame([features])
                        for col in expected_features:
                            if col not in ml_input.columns:
                                ml_input[col] = 0
                        ml_input = ml_input[expected_features]

                        prob = current_model.predict_proba(ml_input)[0][1]
                        confidence = prob * 100

                        logger.info(f"🔎 {symbol} {direction} at the QML ({tf}). AI Confidence: {confidence:.1f}%")

                        if prob >= 0.25:
                            is_posted = bool(prob >= MIN_CONFIDENCE)
                            # Shadow-log cooldown (only write to the log once every 4h per setup)
                            with conn.cursor() as cur:
                                cur.execute(
                                    """
                                    SELECT 1 FROM ml_predictions_master
                                    WHERE coin = %s AND direction = %s AND model_name = %s AND time > NOW() - INTERVAL '4 hours'
                                """,
                                    (symbol, direction, module_tag),
                                )
                                if not cur.fetchone():
                                    cur.execute(
                                        """
                                        INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                                        VALUES (0, %s, %s, %s, %s, %s, %s, %s)
                                    """,
                                        (
                                            module_tag,
                                            now,
                                            symbol,
                                            direction,
                                            float(current_price),
                                            float(prob),
                                            is_posted,
                                        ),
                                    )

                        if prob >= MIN_CONFIDENCE:
                            # 💥 HARD COOLDOWN: 4h lock for 1h setups, 12h lock for 4h setups
                            # check_cooldown returned True if cooldown is STILL ACTIVE → then skip.
                            cd_hours = 4 if tf == '1h' else 12
                            if check_cooldown(conn, module_tag, symbol, direction, cd_hours):
                                continue

                            logger.info(f"🟢 TRADE PASSED! {symbol} ({tf}) is being traded (Conf: {confidence:.1f}%)")
                            send_cornix_signal(
                                conn,
                                df,
                                symbol,
                                direction,
                                current_price,
                                sl_level,
                                tp_level,
                                confidence,
                                p1,
                                p2,
                                p3,
                                p4,
                                module_tag=module_tag,
                            )
                            update_cooldown(conn, module_tag, symbol, direction)
                        else:
                            if prob >= 0.25:
                                logger.warning(
                                    f"🔴 TRADE BLOCKED! {symbol} ({tf}) (Conf: {confidence:.1f}% < {MIN_CONFIDENCE * 100}%)"
                                )

            except Exception as e:
                # P3.7: was logger.debug → invisible. Match bot 29: surface the
                # coin-level failure and roll the connection back so a poisoned
                # transaction does not abort every following coin's query.
                logger.error(f"Error for {symbol} ({tf}): {e}", exc_info=True)
                try:
                    conn.rollback()
                except Exception:
                    pass

    conn.close()


def generate_qm_chart(df, symbol, direction, p1, p2, p3, p4, qm_level):
    """
    Draws the chart, connects the 4 Quasimodo pivots as a zigzag
    and draws a horizontal line for the entry level (qm_level).

    FIX: restores the old functionality — volume subplot and
    explicit column filter. Without a filter, mplfinance picks up all
    indicator columns, causing crashes or incorrect rendering.
    """
    try:
        start_idx = max(0, p1[0] - 20)

        # FIX: explicitly take only OHLCV columns — otherwise mplfinance gets
        # confused by extra indicator columns (rsi_14, ema_*, etc.) present in df.
        plot_df = df.iloc[start_idx:][['open_time', 'open', 'high', 'low', 'close', 'volume']].copy()

        plot_df['open_time'] = pd.to_datetime(plot_df['open_time']).dt.tz_localize(None)
        plot_df.set_index('open_time', inplace=True)

        # Padding on the right for the retest zone
        if len(plot_df) >= 2:
            time_step = plot_df.index[-1] - plot_df.index[-2]
            future_dates = [plot_df.index[-1] + time_step * i for i in range(1, 15)]
            empty_df = pd.DataFrame(index=future_dates, columns=plot_df.columns)
            plot_df = pd.concat([plot_df, empty_df]).astype(float)

        def get_dt(idx):
            return pd.to_datetime(df['open_time'].iloc[idx]).tz_localize(None)

        seq_lines = [
            (get_dt(p1[0]), float(p1[2])),
            (get_dt(p2[0]), float(p2[2])),
            (get_dt(p3[0]), float(p3[2])),
            (get_dt(p4[0]), float(p4[2])),
        ]

        # Color theme: accept direction parameter or legacy string ("BEARISH"/"SHORT")
        is_short = direction == 'SHORT' or "SHORT" in str(direction).upper() or "BEARISH" in str(direction).upper()
        color_theme = '#ff4466' if is_short else '#00ff88'

        # FIX: volume='in' makes sure the bars move into the subplot
        mc = mpf.make_marketcolors(up='#26a69a', down='#ef5350', edge='inherit', wick='inherit', volume='in')
        s = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds', gridstyle=':')

        abs_filename = os.path.abspath(f"{CHART_DIR}/{symbol}_QM_{int(time.time())}.png")

        # FIX: volume=True enables the subplot, panel_ratios controls the size
        mpf.plot(
            plot_df,
            type='candle',
            style=s,
            alines=dict(alines=seq_lines, colors=color_theme, linewidths=2, linestyle='-'),
            hlines=dict(hlines=[float(qm_level)], colors=[color_theme], linewidths=2, linestyle='--'),
            title=f"\n{symbol} | {direction} Quasimodo (Entry: {qm_level:.4f})",
            figsize=(12, 8),
            tight_layout=True,
            volume=True,
            panel_ratios=(4, 1),
            savefig=abs_filename,
            returnfig=False,
        )
        return abs_filename

    except Exception as e:
        logger.error(f"QM Chart Error for {symbol}: {e}", exc_info=True)
        return None
    finally:
        # Closes the figure left open by mpf.plot — prevents a RAM leak.
        plt.close('all')


def send_cornix_signal(conn, df, symbol, direction, entry, sl, tp, confidence, p1, p2, p3, p4, *, module_tag):
    lev = get_max_leverage(symbol, 20)
    # FIX T-2026-CU-9050-030: the tag comes from the caller (artifact model_id) — deriving
    # it here again as f"QM_{tf}" would write every trade of a new generation
    # under the old tag and merge it in ai_signals with the previous generation
    # (rule 6). Deliberately a REQUIRED keyword: a future call site that
    # forgets it fails loudly with TypeError instead of silently taking the old tag —
    # the same pattern as 25_smc_ml_sniper.py (T-2026-CU-9050-026).

    target_dist = tp - entry
    tp1 = entry + (target_dist * 0.5)
    tp2 = tp

    targets = [float(tp1), float(tp2)]

    # T-2026-KYT-9050-033 (audit T-032): fleet lifecycle gate. Default LIVE ⇒ no
    # behaviour change. QM_1H SHORT is parked → SHADOW (monitored trade instead of
    # Cornix); LONG stays live. QM_4H is no longer run by the bot anyway. Purely additive
    # on the post branch (rule 4). Entry is a single entry (entry1==entry2); the
    # ai_signals confidence is prob as in the live path (= confidence/100).
    _route = route_legacy_leg(
        conn, module_tag, direction, symbol, confidence / 100, entry, entry, sl, targets, n_show=len(targets)
    )
    if _route != LEG_LIVE:
        if _route == LEG_SHADOW:
            conn.commit()
        return

    cornix_msg = f"""📈 Signal for {symbol} 📈
🚨 Direction: {direction}
🚨 Leverage: {lev}
🚨 Margin: Cross
🏦 CMP Entry: $ {entry:.6f}
💰 TP1: $ {tp1:.6f}
💰 TP2: $ {tp2:.6f}
💸 Stop Loss: $ {sl:.6f}
🧠 AI Confidence: {confidence:.1f}% ({module_tag} Filter)"""

    chart_path = generate_qm_chart(df, symbol, direction, p1, p2, p3, p4, entry)

    # FIX double post (2026-07-06, fleet sweep): caption without an embedded
    # Cornix block — otherwise Cornix parsed both messages as signals.
    html_caption = f"<b>🚀 AI {module_tag} SNIPER SIGNAL</b>\n<b>{symbol.replace('USDT', '')}</b>\n→ Pattern: {direction} Quasimodo\n→ Win Probability: <b>{confidence:.1f}%</b>"

    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (TELEGRAM_CHANNEL_ID, cornix_msg)
            )

            if chart_path:
                cur.execute(
                    "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                    (TELEGRAM_CHANNEL_ID, html_caption, chart_path),
                )

            cur.execute(
                """
                INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    symbol,
                    float(entry),
                    module_tag,
                    direction,
                    float(confidence / 100),
                    float(entry),
                    float(entry),
                    float(sl),
                    json.dumps(targets),
                ),
            )

        conn.commit()
        logger.info(f"✅ Trade for {symbol} ({module_tag}) written to ai_signals & outbox.")
    except Exception as e:
        logger.error(f"Telegram/DB Error: {e}")
        conn.rollback()


def main():
    logger.info(f"=== 🎯 DUAL QM ML SNIPER STARTED (Threshold: {MIN_CONFIDENCE * 100}%) ===")

    # Ensure the cooldown table exists
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

    while True:
        try:
            scan_market()
            logger.info("Radar scan stopped. Sleeping 3 minutes...")
        except Exception as e:
            logger.error(f"Error in the main loop: {e}")

        time.sleep(180)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manually stopped.")

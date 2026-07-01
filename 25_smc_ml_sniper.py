import warnings

warnings.filterwarnings("ignore")

import json
import logging
import os
import time
from datetime import datetime, timezone

import joblib
import mplfinance as mpf
import numpy as np
import pandas as pd
import scipy.signal

from core import config as _kcfg  # channel ids

# --- Eigene DB Connection importieren ---
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, load_coins, update_cooldown
from core.trade_utils import calculate_smart_targets

# 🛠️ CONFIGURATION
logging.basicConfig(level=logging.INFO, format='%(asctime)s - SMC_SNIPER - %(message)s')
logger = logging.getLogger(__name__)
SMC_CHANNELS = {
    'bb': _kcfg.CH_SNIPER_BB,  # 👈 Channel-ID für Breaker Block
    'td': _kcfg.CH_SNIPER_TD,  # 👈 Channel-ID für Three-Drive (Bitte anpassen!)
}

COINS_FILE = "coins.json"
CHART_DIR = "generated_charts"
os.makedirs(CHART_DIR, exist_ok=True)

TIMEFRAMES = ['1h', '4h']
PIVOT_WINDOW = 10

# 💥 Die optimalen Thresholds aus deinem Training (RR = 1:2)
THRESHOLDS = {
    'bb': 0.40,  # Breaker Block
    'td': 0.30,  # Three-Drive
}

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

# 🧠 LOAD MODELS
MODELS = {'bb': {}, 'td': {}}
for tf in TIMEFRAMES:
    for strategy in ['bb', 'td']:
        path = f"{strategy}_xgboost_model_{tf}.pkl"
        try:
            data = joblib.load(path)
            MODELS[strategy][tf] = {'model': data['model'], 'features': data['features']}
            logger.info(f"✅ ML-Modell ({strategy.upper()} | {tf}) geladen.")
        except Exception as e:
            logger.critical(f"❌ Could not load model ({path}): {e}")
            exit(1)


def evaluate_and_trade(conn, df, symbol, tf, strategy_code, direction, current_price, features_dict, p1, p2, p3=None):
    module_tag = f"{strategy_code.upper()}_{tf.upper()}"
    model_data = MODELS[strategy_code][tf]
    now = datetime.now(timezone.utc)

    # 1. Cooldown / Active Trade Check
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM ai_signals
            WHERE symbol = %s AND direction = %s AND model = %s
        """,
            (symbol, direction, module_tag),
        )
        if cur.fetchone():
            return

    # 2. ML Vorhersage
    ml_input = pd.DataFrame([features_dict])
    for col in model_data['features']:
        if col not in ml_input.columns:
            ml_input[col] = 0
    ml_input = ml_input[model_data['features']]

    prob = model_data['model'].predict_proba(ml_input)[0][1]
    confidence = prob * 100
    min_thresh = THRESHOLDS[strategy_code]

    # 3. Shadow Log
    if prob >= 0.25:
        is_posted = bool(prob >= min_thresh)
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
                    (module_tag, now, symbol, direction, float(current_price), float(prob), is_posted),
                )

    # 4. ECHTER TRADE AUSFÜHREN
    if prob >= min_thresh:
        # check_cooldown returned True wenn Cooldown NOCH AKTIV ist → dann skippen.
        cd_hours = 4 if tf == '1h' else 12
        if check_cooldown(conn, module_tag, symbol, direction, cd_hours):
            return

        # Nutze die neuen dynamischen Targets & Stop Loss Logik
        setup = calculate_smart_targets(conn, symbol, direction, current_price)

        logger.info(f"🟢 TRADE PASSED! {symbol} ({module_tag}) wird getradet (Conf: {confidence:.1f}%)")
        send_cornix_signal(
            conn,
            df,
            symbol,
            tf,
            strategy_code,
            direction,
            setup['entry1'],
            setup['entry2'],
            setup['sl'],
            setup['targets'],
            confidence,
            p1,
            p2,
            p3,
        )
        update_cooldown(conn, module_tag, symbol, direction)


def scan_market():
    coins = load_coins()
    conn = get_db_connection()
    conn.autocommit = True

    for tf in TIMEFRAMES:
        logger.info(f"🔍 Starting SMC-Scan (BB & TD) für Timeframe: {tf}")

        for symbol in coins:
            try:
                fields = ["t1.open_time", "t1.open", "t1.high", "t1.low", "t1.close", "t1.volume"]
                for ind in PRICE_BASED_INDICATORS + ABSOLUTE_INDICATORS + ['atr_14', 'trend_direction']:
                    fields.append(f"t2.{ind}")

                query = f"""
                    SELECT {', '.join(fields)}
                    FROM "{symbol}_{tf}" t1
                    LEFT JOIN "{symbol}_{tf}_indicators" t2 ON t1.open_time = t2.open_time
                    ORDER BY t1.open_time DESC LIMIT 150
                """
                df = pd.read_sql_query(query, conn)
                if len(df) < 100:
                    continue

                df = df.iloc[::-1].reset_index(drop=True)
                df.ffill(inplace=True)
                df.bfill(inplace=True)

                for c in df.columns:
                    if c not in ['open_time', 'trend_direction']:
                        df[c] = df[c].astype(float)

                highs, lows, closes = df['high'].values, df['low'].values, df['close'].values
                rsis = df['rsi_14'].values
                current_price = closes[-1]

                peak_idx = scipy.signal.argrelextrema(highs, np.greater, order=PIVOT_WINDOW)[0]
                trough_idx = scipy.signal.argrelextrema(lows, np.less, order=PIVOT_WINDOW)[0]

                if len(peak_idx) < 3 or len(trough_idx) < 3:
                    continue

                # 1. THREE-DRIVE DIVERGENCE (TD)
                # FIX: Vorher keine zeitliche Begrenzung → peak_idx[-3] konnte 300
                # Kerzen zurückliegen → kein echtes Three-Drive mehr, nur zufällige
                # Tops über Monate verteilt. Jetzt: Pattern muss kompakt sein.
                MAX_TD_SPAN = 50  # Drive 1 bis Drive 3 max 50 Kerzen

                # 1a. Bearish Drive (Short)
                p_peak3 = peak_idx[-1]
                if len(df) - p_peak3 <= PIVOT_WINDOW + 2:
                    p1, p2, p3 = peak_idx[-3], peak_idx[-2], peak_idx[-1]
                    if (p3 - p1) <= MAX_TD_SPAN and highs[p1] < highs[p2] < highs[p3]:
                        if rsis[p1] > rsis[p2] > rsis[p3]:
                            feats = extract_ml_features(df, p3, 'SHORT')
                            evaluate_and_trade(
                                conn,
                                df,
                                symbol,
                                tf,
                                'td',
                                'SHORT',
                                current_price,
                                feats,
                                (p1, 1, highs[p1]),
                                (p2, 1, highs[p2]),
                                (p3, 1, highs[p3]),
                            )

                # 1b. Bullish Drive (Long) - NEU!
                p_trough3 = trough_idx[-1]
                if len(df) - p_trough3 <= PIVOT_WINDOW + 2:
                    p1, p2, p3 = trough_idx[-3], trough_idx[-2], trough_idx[-1]
                    if (p3 - p1) <= MAX_TD_SPAN and lows[p1] > lows[p2] > lows[p3]:
                        if rsis[p1] < rsis[p2] < rsis[p3]:
                            feats = extract_ml_features(df, p3, 'LONG')
                            evaluate_and_trade(
                                conn,
                                df,
                                symbol,
                                tf,
                                'td',
                                'LONG',
                                current_price,
                                feats,
                                (p1, -1, lows[p1]),
                                (p2, -1, lows[p2]),
                                (p3, -1, lows[p3]),
                            )

                # 2. BREAKER BLOCK (BB)
                # FIX: Der alte Check feuerte sobald `current_price ~= pivot_res`,
                # selbst wenn der Breakout 100 Kerzen zurücklag. Jetzt:
                #   - Breakout muss innerhalb der letzten 20 Kerzen stattgefunden haben
                #   - Preis muss von oben (über pivot_res) zurück zum Level kommen
                #   - Zwischendurch darf der Preis das Level nicht massiv verletzt haben
                MAX_BB_AGE = 20  # Breakout darf max 20 Kerzen alt sein
                p_res = peak_idx[-2]
                pivot_res = highs[p_res]
                if current_price >= pivot_res * 0.995 and current_price <= pivot_res * 1.005:
                    breakout_idx = -1
                    for i in range(p_res + 1, len(df) - 1):
                        if closes[i] > pivot_res:
                            breakout_idx = i
                            break
                    # Breakout muss existieren UND frisch sein
                    if breakout_idx != -1 and (len(df) - 1 - breakout_idx) <= MAX_BB_AGE:
                        # Nach dem Breakout muss der Preis mindestens einmal oberhalb
                        # des Levels gelaufen sein — sonst war es kein echter Break
                        peak_after_breakout = max(highs[breakout_idx : len(df) - 1])
                        if peak_after_breakout > pivot_res * 1.003:  # min 0.3% drüber
                            feats = extract_ml_features(df, len(df) - 2, 'LONG')
                            evaluate_and_trade(
                                conn,
                                df,
                                symbol,
                                tf,
                                'bb',
                                'LONG',
                                current_price,
                                feats,
                                (p_res, 1, pivot_res),
                                (breakout_idx, 1, highs[breakout_idx]),
                                (len(df) - 1, 1, current_price),
                            )

                p_sup = trough_idx[-2]
                pivot_sup = lows[p_sup]
                if current_price <= pivot_sup * 1.005 and current_price >= pivot_sup * 0.995:
                    breakdown_idx = -1
                    for i in range(p_sup + 1, len(df) - 1):
                        if closes[i] < pivot_sup:
                            breakdown_idx = i
                            break
                    # Breakdown muss frisch sein UND tief genug gegangen sein
                    if breakdown_idx != -1 and (len(df) - 1 - breakdown_idx) <= MAX_BB_AGE:
                        trough_after_breakdown = min(lows[breakdown_idx : len(df) - 1])
                        if trough_after_breakdown < pivot_sup * 0.997:  # min 0.3% drunter
                            feats = extract_ml_features(df, len(df) - 2, 'SHORT')
                            evaluate_and_trade(
                                conn,
                                df,
                                symbol,
                                tf,
                                'bb',
                                'SHORT',
                                current_price,
                                feats,
                                (p_sup, -1, pivot_sup),
                                (breakdown_idx, -1, lows[breakdown_idx]),
                                (len(df) - 1, -1, current_price),
                            )

            except Exception as e:
                logger.debug(f"Error for {symbol} ({tf}): {e}")

    conn.close()


def extract_ml_features(df, idx, direction):
    close_prev = df['close'].iloc[idx]
    features = {'dir_num': 1 if direction == 'LONG' else 0, 'atr_14_pct': (df['atr_14'].iloc[idx] / close_prev) * 100}
    for ind in ABSOLUTE_INDICATORS:
        features[ind] = df[ind].iloc[idx]
    for ind in PRICE_BASED_INDICATORS:
        features[f"{ind}_dist_pct"] = ((df[ind].iloc[idx] - close_prev) / close_prev) * 100

    trend = str(df['trend_direction'].iloc[idx])
    features['trend_UP'] = 1 if trend == 'UP' else 0
    features['trend_DOWN'] = 1 if trend == 'DOWN' else 0
    features['trend_SIDEWAYS'] = 1 if trend == 'SIDEWAYS' else 0
    return features


def send_cornix_signal(
    conn, df, symbol, tf, strategy_code, direction, entry1, entry2, sl, targets, confidence, p1, p2, p3=None
):
    lev = get_max_leverage(symbol, 20)
    module_tag = f"{strategy_code.upper()}_{tf.upper()}"
    strategy_name = "Breaker Block" if strategy_code == 'bb' else "Three-Drive"

    # 💥 NEU: Bestimme den richtigen Channel für dieses Pattern
    target_channel = SMC_CHANNELS.get(strategy_code, list(SMC_CHANNELS.values())[0])

    # --- CORNIX TEXT ---
    cornix_msg = f"""📈 Signal for {symbol} 📈
🚨 Direction: {direction}
🚨 Leverage: {lev}
🚨 Margin: Cross
🏦 CMP Entry: $ {entry1:.6f}
🏦 Entry 2: $ {entry2:.6f}"""

    for i, t in enumerate(targets[:5], 1):
        cornix_msg += f"\n💰 TP{i}: $ {t:.6f}"

    cornix_msg += f"\n💸 Stop Loss: $ {sl:.6f}\n🧠 AI Confidence: {confidence:.1f}% ({module_tag} Filter)"

    # --- HTML CAPTION ---
    avg_entry = (entry1 + entry2) / 2
    risk_pct = abs((sl - avg_entry) / avg_entry)
    reward_pct = abs((targets[0] - avg_entry) / avg_entry) if targets else 0.01
    rrr = reward_pct / risk_pct if risk_pct > 0 else 0.01

    html_caption = f"""<pre>
<b>🚀 AI {module_tag} SIGNAL</b>
<b>├─ Coin:</b> <b>{symbol}</b>
<b>├─ Pattern:</b> <b>{strategy_name}</b>
<b>├─ Action:</b> <b>{direction}</b>
<b>├─ RRR (T1):</b> <b>1:{rrr:.2f}</b>
<b>└─ AI Confidence:</b> <b>{confidence:.1f}%</b>

<b>├─ Entry 1:</b> <b>${entry1:,.8f}</b>
<b>└─ Entry 2:</b> <b>${entry2:,.8f}</b>

<b>├─ Take Profits:</b>
"""
    for i, t in enumerate(targets[:5], 1):
        pct = abs((t - entry1) / entry1 * 100) * int(lev.replace('x', ''))
        color = "#00ff88" if i <= 2 else "#88ff88"
        html_caption += (
            f"<b style=\"color:{color};\">   T{i}:</b> <b>${t:,.8f}</b> → <b style=\"color:lime;\">+{pct:.1f}%</b>\n"
        )

    sl_loss = risk_pct * 100 * int(lev.replace('x', ''))
    html_caption += f"""<b>└─ Stop Loss:</b> <b>${sl:,.8f}</b> → <b>-{sl_loss:.1f}%</b>

<b>--- CORNIX FORMAT ---</b>
{cornix_msg}
</pre>"""

    chart_path = generate_smc_chart(df, symbol, tf, strategy_name, direction, entry1, p1, p2, p3)

    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (target_channel, cornix_msg)
            )

            if chart_path:
                cur.execute(
                    "INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                    (target_channel, html_caption, chart_path),
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
                    direction,
                    float(confidence / 100),
                    float(entry1),
                    float(entry2),
                    float(sl),
                    json.dumps(targets),
                ),
            )

    except Exception as e:
        logger.error(f"Telegram/DB Error: {e}")


# ==========================================
# 🕵️ CHART GENERATION
# ==========================================
def generate_smc_chart(df, symbol, tf, strategy_name, direction, entry, p1, p2, p3=None):
    """Zeichnet SMC-Pattern (Three-Drive oder Breaker Block).

    FIX: Stellt die volle alte Funktionalität wieder her:
      - Explizite OHLCV + RSI-Spalten (sonst verwirrt mplfinance sich)
      - Three-Drive: p1→p2→p3 als Zickzack + RSI-Subplot auf Panel 2
      - Breaker Block: Zone (fill_between) + Star-Marker auf Pivots
      - Volume-Subplot in beiden Fällen
    """
    try:
        start_idx = max(0, p1[0] - 25)

        # FIX: Explizit nur die benötigten Spalten — sonst drückt mplfinance
        # alle Indikator-Spalten ins Rendering.
        plot_df = df.iloc[start_idx:][['open_time', 'open', 'high', 'low', 'close', 'volume', 'rsi_14']].copy()

        plot_df['open_time'] = pd.to_datetime(plot_df['open_time']).dt.tz_localize(None)
        plot_df.set_index('open_time', inplace=True)

        if len(plot_df) > 1:
            time_step = plot_df.index[-1] - plot_df.index[-2]
            future_dates = [plot_df.index[-1] + time_step * i for i in range(1, 10)]
            empty_df = pd.DataFrame(index=future_dates, columns=plot_df.columns)
            plot_df = pd.concat([plot_df, empty_df]).astype(float)

        def get_dt(idx):
            return pd.to_datetime(df['open_time'].iloc[idx]).tz_localize(None)

        color_theme = '#00ff88' if direction == "LONG" else '#ff4466'
        # FIX: volume='in' für den Subplot
        mc = mpf.make_marketcolors(up='#26a69a', down='#ef5350', edge='inherit', wick='inherit', volume='in')
        s = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds', gridstyle=':')

        apds = []
        alines = []
        hlines = []
        kwargs_fb = {}
        panel_ratios = (4, 1)  # default: candle + volume

        if strategy_name == "Three-Drive":
            # Zick-Zack der 3 Pivots + RSI-Subplot
            alines.append([(get_dt(p1[0]), float(p1[2])), (get_dt(p2[0]), float(p2[2])), (get_dt(p3[0]), float(p3[2]))])
            apds.append(mpf.make_addplot(plot_df['rsi_14'], panel=2, color='cyan', ylabel='RSI (14)'))
            panel_ratios = (4, 1, 1.5)

        elif strategy_name == "Breaker Block":
            # Horizontale Zone + Marker-Sternchen auf Pivots
            y_center = float(p1[2])
            y_lower = [y_center * 0.997] * len(plot_df)
            y_upper = [y_center * 1.003] * len(plot_df)
            kwargs_fb = dict(y1=y_lower, y2=y_upper, color=color_theme, alpha=0.15)

            hlines.append(y_center)

            marker_array = [np.nan] * len(plot_df)
            for p in [p1, p2, p3]:
                if p and 0 <= (p[0] - start_idx) < len(plot_df):
                    val = float(p[2])
                    marker_array[p[0] - start_idx] = val

            apds.append(mpf.make_addplot(marker_array, type='scatter', markersize=200, marker='*', color='yellow'))

        abs_filename = os.path.abspath(f"{CHART_DIR}/{symbol}_{strategy_name.replace(' ', '_')}_{int(time.time())}.png")

        kwargs = dict(
            type='candle',
            style=s,
            title=f"\n{symbol} | {strategy_name} ({tf})",
            figsize=(12, 8),
            tight_layout=True,
            savefig=abs_filename,
            returnfig=False,
            volume=True,
            panel_ratios=panel_ratios,
        )

        if apds:
            kwargs['addplot'] = apds
        if alines:
            kwargs['alines'] = dict(alines=alines, colors=color_theme, linewidths=2, linestyle='-')
        if hlines:
            kwargs['hlines'] = dict(hlines=hlines, colors=[color_theme], linewidths=1, linestyle='--')
        if kwargs_fb:
            kwargs['fill_between'] = kwargs_fb

        mpf.plot(plot_df, **kwargs)
        return abs_filename

    except Exception as e:
        logger.error(f"SMC Chart Error for {symbol}: {e}", exc_info=True)
        return None


def main():
    logger.info("=== 🎯 SMC ML SNIPER (BB & TD) GESTARTET ===")

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
            logger.info("Radar-Scan stopped. Schlafe 3 Minuten...")
        except Exception as e:
            logger.error(f"Fehler in der Main-Loop: {e}")

        time.sleep(180)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot manuell stopped.")

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

# --- Eigene DB Connection importieren ---
from core.database import get_db_connection
from core.market_utils import check_cooldown, get_max_leverage, load_coins, update_cooldown

# 🛠️ CONFIGURATION
logging.basicConfig(level=logging.INFO, format='%(asctime)s - QM_SNIPER - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_CHANNEL_ID = _kcfg.CH_INSTITUTIONAL

COINS_FILE = "coins.json"
CHART_DIR = "generated_charts"
os.makedirs(CHART_DIR, exist_ok=True)

# PARKED 4h (Audit Report 14/16): QM_4H ist netto negativ (−277, 54,9% WR)
# und steht auf der Stopp-Liste; QM_1H läuft weiter (Redesign-Kandidat).
TIMEFRAMES = ['1h']
MODEL_PATHS = {'1h': "qm_xgboost_model_1h.pkl"}

MIN_CONFIDENCE = 0.65  # FIX: Vorher 0.40 → viel zu niedrig, schlechter Erwartungswert.
ZONE_TOLERANCE = 0.005  # FIX: Vorher 0.01 (1%) → zu weit. 0.5% ist sauberer Retest-Bereich.
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
        # Posting-Tag aus der Artefakt-Meta, sonst abgeleitet (T-2026-CU-9050-030).
        # Seit T-2026-CU-9050-061 schreibt qm_ml_trainer.py die model_id
        # f"QM2_{tf.upper()}" in die Meta — ein QM2-Retrain postet damit als QM2_1H
        # statt still unter dem Alt-Tag QM_1H mit der QM1-Statistik zu verschmelzen,
        # auf der das Orchestrator-Gating entscheidet (Regel 6). Der abgeleitete
        # QM_1H bleibt der Fallback für Alt-Artefakte ohne model_id.
        # Der Orchestrator erkennt QM2_1H bereits (QM\d*_ in BOT_IDENTIFICATION_PATTERNS).
        meta = ml_data.get('meta') or {}
        model_id = str(meta.get('model_id') or ml_data.get('model_id') or "").strip()
        ML_MODELS[tf] = {
            'model': ml_data['model'],
            'features': ml_data['features'],
            'tag': model_id or f"QM_{tf.upper()}",
        }
        logger.info(
            f"✅ ML-Modell für {tf} loaded successfully. Features: {len(ml_data['features'])}, "
            f"Tag: {ML_MODELS[tf]['tag']}{'' if model_id else ' (abgeleitet — Artefakt ohne model_id)'}"
        )
    except Exception as e:
        logger.critical(f"❌ Could not load model for {tf} nicht laden ({path}): {e}")
        exit(1)


def scan_market():
    coins = load_coins()
    conn = get_db_connection()
    conn.autocommit = True  # Verhindert Datenbank-Locks
    now = datetime.now(timezone.utc)

    for tf in TIMEFRAMES:
        module_tag = ML_MODELS[tf]['tag']  # Artefakt-Tag, nicht aus tf abgeleitet
        # Transitionaler Dedup (T-2026-CU-9050-030): der Active-Trade-Check läuft über
        # den Tag, und der wechselt beim QM2-Rollout (QM_1H → QM2_1H). Ohne den Alt-Tag
        # blockte eine offene QM_1H-Position denselben Coin/Direction nicht mehr — der
        # QM2-Lauf öffnete eine ZWEITE Live-Position daneben. legacy_tag ist exakt das
        # Tag, das dieser Bot vor dem Fix gepostet hätte; solange kein QM2-Artefakt
        # deployt ist, sind beide identisch und das IN ist ein No-op.
        legacy_tag = f"QM_{tf.upper()}"
        logger.info(f"🔍 Starting QM-Scan für Timeframe: {tf}")

        current_model = ML_MODELS[tf]['model']
        expected_features = ML_MODELS[tf]['features']

        for symbol in coins:
            try:
                # 💥 FIX: 't1.volume' im Select hinzugefügt, sonst crashed der Chart!
                fields = ["t1.open_time", "t1.open", "t1.high", "t1.low", "t1.close", "t1.volume"]
                for ind in PRICE_BASED_INDICATORS + ABSOLUTE_INDICATORS + ['atr_14', 'trend_direction']:
                    fields.append(f"t2.{ind}")

                query = f"""
                    SELECT {', '.join(fields)}
                    FROM "{symbol}_{tf}" t1
                    LEFT JOIN "{symbol}_{tf}_indicators" t2 ON t1.open_time = t2.open_time
                    ORDER BY t1.open_time DESC LIMIT 100
                """

                df = pd.read_sql_query(query, conn)
                if len(df) < 50:
                    continue

                df = df.iloc[::-1].reset_index(drop=True)
                df.ffill(inplace=True)
                df.bfill(inplace=True)

                for c in df.columns:
                    if c not in ['open_time', 'trend_direction']:
                        df[c] = df[c].astype(float)

                highs, lows, closes = df['high'].values, df['low'].values, df['close'].values
                current_price = closes[-1]

                # P1.24: Pivot-Suche nur auf abgeschlossenen Kerzen — die letzte Zeile
                # ist die laufende (forming) Kerze; sonst repaintet der Pivot und der
                # Bot weicht von der Trainer-Geometrie ab.
                c_highs, c_lows = highs[:-1], lows[:-1]

                peak_idx = scipy.signal.argrelextrema(c_highs, np.greater, order=PIVOT_WINDOW)[0]
                trough_idx = scipy.signal.argrelextrema(c_lows, np.less, order=PIVOT_WINDOW)[0]

                # P1.24: Kanten-Pivots verwerfen — argrelextrema (mode='clip') lässt am
                # rechten Rand unbestätigte Pivots durch; ein Pivot braucht PIVOT_WINDOW
                # nachfolgende Kerzen zur Bestätigung.
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

                    # FIX: Echte Retest-Bestätigung statt bloßer Nähe-Check.
                    # Vorher feuerte der Bot schon wenn der aktuelle Preis innerhalb
                    # 1% vom QML war — auch wenn das Level nie berührt wurde.
                    # Jetzt: Die letzten 3 geschlossenen Kerzen müssen das QML
                    # berührt haben (high/low innerhalb der Zone) UND der aktuelle
                    # Preis muss sich auf der "richtigen" Seite des Levels befinden.
                    touched_recently = False
                    zone_upper = qm_level * (1 + ZONE_TOLERANCE)
                    zone_lower = qm_level * (1 - ZONE_TOLERANCE)
                    for k in range(1, min(4, len(df))):  # letzte 3 geschlossene Kerzen
                        c_high_k = highs[-1 - k]
                        c_low_k = lows[-1 - k]
                        if c_low_k <= zone_upper and c_high_k >= zone_lower:
                            touched_recently = True
                            break

                    if not touched_recently:
                        continue

                    # Zusätzlich: Preis muss sich jetzt auf der Trade-Seite des QML bewegen.
                    # SHORT-Setup: QM_level ist Resistance → aktueller Preis sollte
                    # drunter oder leicht darüber sein, aber nicht weit weg after oben gebrochen.
                    # LONG-Setup:  QM_level ist Support → aktueller Preis darüber.
                    if direction == 'SHORT' and current_price > zone_upper:
                        continue
                    if direction == 'LONG' and current_price < zone_lower:
                        continue

                    if dist_to_qml <= ZONE_TOLERANCE * 2:  # echte Zone bleibt großzügiger
                        feature_idx = len(df) - 2
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

                        logger.info(f"🔎 {symbol} {direction} am QML ({tf}). AI Confidence: {confidence:.1f}%")

                        if prob >= 0.25:
                            is_posted = bool(prob >= MIN_CONFIDENCE)
                            # Shadow-Log Cooldown (Nur alle 4h einmal ins Log schreiben pro Setup)
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
                            # 💥 HARD COOLDOWN: 4h Sperre für 1h Setups, 12h Sperre für 4h Setups
                            # check_cooldown returned True wenn Cooldown NOCH AKTIV ist → dann skippen.
                            cd_hours = 4 if tf == '1h' else 12
                            if check_cooldown(conn, module_tag, symbol, direction, cd_hours):
                                continue

                            logger.info(f"🟢 TRADE PASSED! {symbol} ({tf}) wird getradet (Conf: {confidence:.1f}%)")
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
                                    f"🔴 TRADE GEBLOCKT! {symbol} ({tf}) (Conf: {confidence:.1f}% < {MIN_CONFIDENCE * 100}%)"
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
    Zeichnet den Chart, verbindet die 4 Quasimodo-Pivots als Zick-Zack
    und zieht eine horizontale Linie für das Einstiegs-Level (qm_level).

    FIX: Stellt die alte Funktionalität wieder her — Volume-Subplot und
    expliziter Spalten-Filter. Ohne Filter nimmt mplfinance sämtliche
    Indikator-Spalten mit, was zu Crashes oder falschem Rendering führt.
    """
    try:
        start_idx = max(0, p1[0] - 20)

        # FIX: Explizit nur OHLCV-Spalten nehmen — sonst verwirrt sich mplfinance
        # an zusätzlichen Indikator-Spalten (rsi_14, ema_*, etc.) die in df stecken.
        plot_df = df.iloc[start_idx:][['open_time', 'open', 'high', 'low', 'close', 'volume']].copy()

        plot_df['open_time'] = pd.to_datetime(plot_df['open_time']).dt.tz_localize(None)
        plot_df.set_index('open_time', inplace=True)

        # Padding rechts für Retest-Zone
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

        # Color-Theme: Direction-Parameter oder Legacy-String ("BEARISH"/"SHORT") akzeptieren
        is_short = direction == 'SHORT' or "SHORT" in str(direction).upper() or "BEARISH" in str(direction).upper()
        color_theme = '#ff4466' if is_short else '#00ff88'

        # FIX: volume='in' sorgt dafür, dass die Balken in den Subplot wandern
        mc = mpf.make_marketcolors(up='#26a69a', down='#ef5350', edge='inherit', wick='inherit', volume='in')
        s = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds', gridstyle=':')

        abs_filename = os.path.abspath(f"{CHART_DIR}/{symbol}_QM_{int(time.time())}.png")

        # FIX: volume=True aktiviert den Subplot, panel_ratios regelt die Größe
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
        # Schließt die von mpf.plot offen gelassene Figure — verhindert RAM-Leak.
        plt.close('all')


def send_cornix_signal(conn, df, symbol, direction, entry, sl, tp, confidence, p1, p2, p3, p4, *, module_tag):
    lev = get_max_leverage(symbol, 20)
    # FIX T-2026-CU-9050-030: das Tag kommt vom Caller (Artefakt-model_id) — es hier
    # wieder als f"QM_{tf}" abzuleiten, würde jeden Trade einer neuen Generation
    # unter dem Alt-Tag schreiben und ihn in ai_signals mit der Vorgänger-Generation
    # verschmelzen (Regel 6). Bewusst PFLICHT-Keyword: eine künftige Aufrufstelle, die
    # ihn vergisst, scheitert laut mit TypeError, statt still den Alt-Tag zu nehmen —
    # dasselbe Muster wie 25_smc_ml_sniper.py (T-2026-CU-9050-026).

    target_dist = tp - entry
    tp1 = entry + (target_dist * 0.5)
    tp2 = tp

    targets = [float(tp1), float(tp2)]

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

    # FIX Doppel-Post (2026-07-06, Flotten-Sweep): Caption ohne eingebetteten
    # Cornix-Block — Cornix parste sonst beide Nachrichten als Signale.
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
        logger.info(f"✅ Trade für {symbol} ({module_tag}) in ai_signals & Outbox geschrieben.")
    except Exception as e:
        logger.error(f"Telegram/DB Error: {e}")
        conn.rollback()


def main():
    logger.info(f"=== 🎯 DUAL QM ML SNIPER GESTARTET (Threshold: {MIN_CONFIDENCE * 100}%) ===")

    # Sicherstellen, dass die Cooldown-Tabelle existiert
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

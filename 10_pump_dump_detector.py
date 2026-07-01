import warnings
warnings.filterwarnings("ignore", message=".*SQLAlchemy connectable.*")

import time
import datetime
import logging
import joblib
import pandas as pd
import numpy as np
import scipy.signal
import requests
import json
import os
from collections import deque

from core.database import get_db_connection
from core.charting import generate_minichart_image
from core.market_utils import get_max_leverage
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels
from core import config as _kcfg  # channel ids

logging.basicConfig(level=logging.INFO, format='%(asctime)s - PUMP_DUMP_DETECTOR - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & CHANNELS ---
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
    "BTCDOMUSDT": {"step": 100, "decimals": 0}
}

# --- ML MODEL FOR 10 SECONDS ---
ML_MODEL_PATH = "pump_dump_model.pkl"

_ml_model = None
_ml_model_time = None


def load_pump_model():
    """Loads the small, fast ML model (Cached for 1 hour)."""
    global _ml_model, _ml_model_time
    now = datetime.datetime.now(datetime.timezone.utc)

    if _ml_model is None or _ml_model_time is None or (now - _ml_model_time).total_seconds() > 3600:
        if os.path.exists(ML_MODEL_PATH):
            try:
                _ml_model = joblib.load(ML_MODEL_PATH)
                _ml_model_time = now
                logger.info(f"✅ ML-Modell '{ML_MODEL_PATH}' for fast Pump/Dump Detector loaded")
            except Exception as e:
                logger.error(f"Error loading des ML-Modells: {e}")
                _ml_model = None
        else:
            logger.warning(f"⚠️ Modell {ML_MODEL_PATH} not found – waiting for Training...")
            _ml_model = None
            _ml_model_time = now
    return _ml_model


# --- IN-MEMORY STATE ---
ONE_MINUTE_DATA = {}
ROUND_BREAK_STATE = {}
PRICE_VOLUME_ALERT_STATE = {}
PUMP_DUMP_STATE = {}

# --- CACHE LOGIK FÜR NEUSTARTS ---
DATA_FILE = "1minute.json"
STATE_FILE = "pump_dump_state.json"


def load_state_from_disk():
    """Loads historical 10s candles and cooldowns from disk at startup."""
    global ONE_MINUTE_DATA, PUMP_DUMP_STATE, PRICE_VOLUME_ALERT_STATE

    # 1. Kerzen laden
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            for symbol, entries in raw_data.items():
                dq = deque(maxlen=1440)
                for entry in entries[-1440:]:
                    dq.append(entry)
                ONE_MINUTE_DATA[symbol] = dq
            logger.info(f"✅ Cache: {len(ONE_MINUTE_DATA)} Coins aus {DATA_FILE} geladen.")
        except Exception as e:
            logger.error(f"Error loading von {DATA_FILE}: {e}")

    # 2. Load cooldowns and pump/dump state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                raw_state = json.load(f)

            for symbol, data in raw_state.items():
                PUMP_DUMP_STATE[symbol] = {
                    "avg_volume": float(data.get("avg_volume", 0)),
                    "last_alert_time": datetime.datetime.fromisoformat(data["last_alert_time"]) if data.get(
                        "last_alert_time") else datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc),
                    "volume_samples": deque(data.get("volume_samples", [])[-360:], maxlen=360)
                }

                # Also restore the market-alert cooldown
                if data.get("pv_last_alert"):
                    PRICE_VOLUME_ALERT_STATE[symbol] = {
                        "last_alert_time": datetime.datetime.fromisoformat(data["pv_last_alert"])
                    }
            logger.info(f"✅ Cache: ML-States aus {STATE_FILE} geladen. (Keine Blind-Phase!)")
        except Exception as e:
            logger.error(f"Error loading von {STATE_FILE}: {e}")


def save_state_to_disk():
    """Speichert den aktuellen Zustand kugelsicher auf die Festplatte (Atomic Write)."""
    try:
        # 1. Kerzen speichern (Atomic Write)
        raw_data = {sym: list(dq) for sym, dq in ONE_MINUTE_DATA.items()}
        tmp_data_file = DATA_FILE + ".tmp"
        with open(tmp_data_file, "w", encoding="utf-8") as f:
            json.dump(raw_data, f, indent=2, ensure_ascii=False)
            f.flush()  # Zwingt das OS, den Puffer auf die Platte zu schreiben
            os.fsync(f.fileno())
        # Atomically rename file (replaces old file immediately)
        os.replace(tmp_data_file, DATA_FILE)

        # 2. States speichern (Atomic Write)
        save_state = {}
        for sym, state in PUMP_DUMP_STATE.items():
            pv_time = PRICE_VOLUME_ALERT_STATE.get(sym, {}).get("last_alert_time", datetime.datetime(1970, 1, 1,
                                                                                                     tzinfo=datetime.timezone.utc))

            save_state[sym] = {
                "avg_volume": state["avg_volume"],
                "last_alert_time": state["last_alert_time"].isoformat(),
                "pv_last_alert": pv_time.isoformat(),
                "volume_samples": list(state["volume_samples"])
            }

        tmp_state_file = STATE_FILE + ".tmp"
        with open(tmp_state_file, "w", encoding="utf-8") as f:
            json.dump(save_state, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_state_file, STATE_FILE)

        logger.info("💾 State backup saved successfully (atomic).")
    except Exception as e:
        logger.error(f"Error saving der States: {e}")



# --- HELPER FUNKTIONEN ---

def get_indicators_at_time(conn, coin):
    """Holt die aktuellsten 1h Indikatoren für die ML Features."""
    try:
        with conn.cursor() as cur:
            cur.execute(f'''
                SELECT rsi_14, tsi_fast_12_7_7, macd_dif_normal_12_26_9, ema_9, ema_21
                FROM "{coin}_1h_indicators"
                ORDER BY open_time DESC LIMIT 1
            ''')
            row = cur.fetchone()
            if row:
                columns = [desc[0] for desc in cur.description]
                return dict(zip(columns, row))
    except Exception:
        pass
    return None


def send_outbox(conn, channel, text, chart_path=None):
    """Schiebt eine Nachricht in die Outbox."""
    try:
        with conn.cursor() as cur:
            if chart_path:
                cur.execute("INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                            (channel, text, chart_path))
            else:
                cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)", (channel, text))
        conn.commit()
    except Exception as e:
        logger.error(f"Outbox error: {e}")
        conn.rollback()


# 1. ROUND LEVEL BREAKER
def check_round_levels(conn, symbol, current_price, prev_price):
    if symbol not in ROUND_LEVEL_CONFIG: return

    cfg = ROUND_LEVEL_CONFIG[symbol]
    step = cfg["step"]
    decimals = cfg["decimals"]

    prev_bucket = int(prev_price / step)
    curr_bucket = int(current_price / step)

    if prev_bucket == curr_bucket: return

    direction = "upwards" if current_price > prev_price else "downwards"
    crossed_level = curr_bucket * step if direction == "upwards" else prev_bucket * step

    # Cooldown Check
    state = ROUND_BREAK_STATE.get(symbol, {})
    if state.get("last_level", 0) == crossed_level and (
            datetime.datetime.now(datetime.timezone.utc) - state.get("last_break_time", datetime.datetime(1970, 1, 1,
                                                                                                          tzinfo=datetime.timezone.utc))).total_seconds() < 180:
        return

    logger.info(f"🚧 ROUND LEVEL BREAK: {symbol} crossed {crossed_level} {direction}")

    color = "#00ff00" if direction == "upwards" else "#ff0066"
    html = f"""<pre><b>ROUND LEVEL BREAK</b>\n<b>{symbol.replace('USDT', '')}/USDT</b> breaks <b>{crossed_level:,.{decimals}f}</b> <b>{direction.upper()}</b>\n<b>→ Price:</b> <code>${current_price:,.{decimals}f}</code>\n<b>→ Time:</b> {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')} UTC\n</pre>"""

    chart_buf = generate_minichart_image(symbol, minutes=60)
    send_outbox(conn, MARKET_CHANNEL_ID, html, chart_buf)

    if state.get("last_level", 0) == crossed_level and (
            datetime.datetime.now(datetime.timezone.utc) - state.get("last_break_time", datetime.datetime(1970, 1, 1,
                                                                                                          tzinfo=datetime.timezone.utc))).total_seconds() < 600:
        ROUND_BREAK_STATE[symbol] = {"last_level": crossed_level,
                                     "last_break_time": datetime.datetime.now(datetime.timezone.utc),
                                     "direction": direction}
        return
    send_outbox(conn, SENTIMENT_CHANNEL_ID, html, chart_buf)

    ROUND_BREAK_STATE[symbol] = {"last_level": crossed_level,
                                 "last_break_time": datetime.datetime.now(datetime.timezone.utc),
                                 "direction": direction}


def _parse_bucket_ts(entry: dict) -> datetime.datetime | None:
    """Parst den 't'-Zeitstempel eines Bucket-Eintrags. None bei Fehler."""
    try:
        ts_str = entry.get("t", "")
        return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _find_bucket_before(data: list, now: datetime.datetime,
                       seconds_ago: int, tolerance: int = 20) -> dict | None:
    """Sucht den Bucket der ca. `seconds_ago` Sekunden in der Vergangenheit liegt.

    Geht die Daten von hinten after vorne durch und nimmt den ersten Bucket
    dessen Timestamp im Fenster [seconds_ago - tolerance, seconds_ago + tolerance]
    liegt. Das macht den Code robust gegen:
      - Restarts mit geladener alter State-Historie
      - Lücken in den Buckets (WebSocket-Ausfälle etc.)
      - Gemischte Daten aus unterschiedlichen Läufen

    Returns None wenn kein Bucket im gewünschten Zeitfenster gefunden wurde
    — in diesem Fall soll der Caller den entsprechenden Lookback-Vergleich
    skippingn.

    tolerance=20 heißt: ±20 Sekunden Toleranz. Bei 10s-Buckets reicht das
    völlig; größere Toleranzen würden die Prozent-Berechnung verzerren.
    """
    if not data:
        return None

    target_dt = now - datetime.timedelta(seconds=seconds_ago)
    tolerance_delta = datetime.timedelta(seconds=tolerance)

    # Von hinten after vorne durchgehen — der neueste passending Bucket gewinnt
    for entry in reversed(data):
        entry_ts = _parse_bucket_ts(entry)
        if entry_ts is None:
            continue
        if abs(entry_ts - target_dt) <= tolerance_delta:
            return entry
        # Wenn wir schon weiter in der Vergangenheit sind als target+tolerance,
        # gibt es keinen Treffer mehr
        if entry_ts < target_dt - tolerance_delta:
            return None

    return None


def _find_bucket_range(data: list, now: datetime.datetime,
                      seconds_ago: int, tolerance: int = 20) -> list:
    """Gibt alle Buckets zurück die im Zeitraum [now - seconds_ago, now] liegen.

    Robust gegen Lücken und alte State-Daten — nutzt ausschließlich die
    Timestamps der Buckets, nicht deren Position in der Liste.
    """
    if not data:
        return []

    cutoff = now - datetime.timedelta(seconds=seconds_ago + tolerance)
    result = []

    for entry in reversed(data):
        entry_ts = _parse_bucket_ts(entry)
        if entry_ts is None:
            continue
        if entry_ts < cutoff:
            break
        result.append(entry)

    return list(reversed(result))  # wieder chronologisch


# 2. EXTREME MOVE & PUMP/DUMP DETECTOR
def process_coin_logics(conn, symbol):
    data = list(ONE_MINUTE_DATA[symbol])
    if len(data) < 36: return  # Brauchen etwas Historie

    now = datetime.datetime.now(datetime.timezone.utc)
    current_price = float(data[-1]["p"])
    current_vol = float(data[-1]["v10s"])

    # STALE-DATA-CHECK: Der letzte Bucket muss frisch sein (< 60s alt).
    # Nach einem Restart kann die aus 1minute.json geladene Historie bis zu
    # 4 Stunden alt sein. Wenn dann neue Buckets reinkommen, darf der Code
    # die alten NICHT als "vor 2 Minuten" behandeln. Wenn die Daten stale
    # sind, skippingn wir diesen Cycle — beim nächsten Tick ist der neueste
    # Bucket schon wieder frisch.
    latest_ts = _parse_bucket_ts(data[-1])
    if latest_ts is None:
        return
    latest_age_sec = (now - latest_ts).total_seconds()
    if latest_age_sec > 60:
        # Daten-Gap zu groß — nicht aussagekräftig für Pump-Detection.
        # Kann after Restart oder after WS-Ausfall passieren.
        logger.debug(f"{symbol}: stale data ({latest_age_sec:.0f}s alt), skipping")
        return

    # Wenn die aktuelle Volume-Messung durch einen 24h-Rollover ungültig ist,
    # skippingn wir Volume-basierte Checks komplett (Price-Check läuft weiter).
    current_vol_valid = bool(data[-1].get("v10s_valid", True))
    prices = [float(e["p"]) for e in data]
    # Nur gültige Volume-Deltas für Baseline und Analyse verwenden.
    volumes_10s = [float(e["v10s"]) for e in data if e.get("v10s_valid", True)]

    # -- Initialize States --
    if symbol not in PRICE_VOLUME_ALERT_STATE: PRICE_VOLUME_ALERT_STATE[symbol] = {
        "last_alert_time": datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)}
    if symbol not in PUMP_DUMP_STATE: PUMP_DUMP_STATE[symbol] = {"avg_volume": 0.0, "volume_samples": deque(maxlen=360),
                                                                 "last_alert_time": datetime.datetime(1970, 1, 1,
                                                                                                      tzinfo=datetime.timezone.utc)}

    pv_state = PRICE_VOLUME_ALERT_STATE[symbol]
    pd_state = PUMP_DUMP_STATE[symbol]
    # Nur gültige Volume-Messungen in die Baseline-Samples aufnehmen.
    if current_vol_valid:
        pd_state["volume_samples"].append(current_vol)

    # A) EXTREME MOVE (Market Channel)
    if (now - pv_state["last_alert_time"]).total_seconds() >= 300:
        alerted = False
        use_ext_cooldown = False

        # 1. Price Move
        # WICHTIG: Der Lookback ist ZEITSTEMPEL-basiert, nicht Index-basiert.
        # Das tuple (lookback, min_pct, _) bedeutet: "vor `lookback` BUCKETS"
        # was bei 10s-Buckets `lookback * 10` SEKUNDEN entspricht.
        # Nach einem Restart kann die Deque aber alte + neue Buckets mischen
        # — dann ist data[-12] NICHT mehr "vor 120 Sekunden". Deshalb suchen
        # wir den Vergleichs-Bucket per Zeitstempel.
        for lookback, min_pct, _ in [(12, 3.0, 2), (18, 4.0, 3), (30, 5.0, 5), (42, 7.5, 7), (60, 10.0, 10),
                                     (360, 20.0, 60)]:
            seconds_back = lookback * 10

            # Bucket vor genau `seconds_back` Sekunden suchen (mit 20s Toleranz)
            past_entry = _find_bucket_before(data, now, seconds_back, tolerance=20)
            if past_entry is None:
                # Kein Bucket im gewünschten Zeitfenster — entweder zu wenig
                # Historie oder Daten-Lücke. Diesen Lookback skippingn.
                continue

            past_price = float(past_entry.get("p", 0))
            if past_price <= 0:
                continue

            chg_pct = (current_price / past_price - 1) * 100
            if abs(chg_pct) >= min_pct:
                direction = "PUMP" if chg_pct > 0 else "DUMP"
                col = "#00ff00" if chg_pct > 0 else "#ff0066"
                mins, secs = divmod(lookback * 10, 60)
                t_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

                # Dead-Cat-Bounce-Check: 10-Minuten-Trend vs. Signal-Richtung
                # Zeit-basiert: Bucket von vor 600s (= 10min) suchen
                dead_cat = False
                chg_10m = None
                bucket_10m_ago = _find_bucket_before(data, now, 600, tolerance=30)
                if bucket_10m_ago is not None:
                    price_10m = float(bucket_10m_ago.get("p", 0))
                    if price_10m > 0:
                        chg_10m = (current_price / price_10m - 1) * 100
                        # PUMP bei negativem 10m-Trend → Dead-Cat-Bounce
                        # DUMP bei positivem 10m-Trend → kurzer Einbruch im Aufwärtstrend
                        if chg_pct > 0 and chg_10m < -1.0:
                            dead_cat = True
                        elif chg_pct < 0 and chg_10m > 1.0:
                            dead_cat = True

                # Spike-Region finden: Start = extremster Punkt im Window,
                # End = aktueller Zeitpunkt (= letzter Bucket).
                # Bei PUMP: Start = lowest, End = current_high.
                # Bei DUMP: Start = highest, End = current_low.
                # WICHTIG: auch hier zeit-basiert statt data[-lookback:], sonst
                # wird after Restart wieder der alte Bug produziert.
                spike_window = _find_bucket_range(data, now, seconds_back, tolerance=20)
                if not spike_window:
                    # Kein brauchbares Fenster — Alert skippen
                    continue

                spike_prices = [float(e["p"]) for e in spike_window]
                if chg_pct > 0:
                    spike_idx = spike_prices.index(min(spike_prices))
                else:
                    spike_idx = spike_prices.index(max(spike_prices))

                # Timestamp des Spike-Start-Buckets ausparsen
                spike_start_dt = _parse_bucket_ts(spike_window[spike_idx])
                spike_time_label = (
                    spike_start_dt.strftime("%H:%M:%S UTC")
                    if spike_start_dt else None
                )

                # Spike-End: Timestamp des letzten Buckets im Window (= jetzt).
                spike_end_dt = _parse_bucket_ts(spike_window[-1])

                # Sanity-Check: spike_start darf nicht vor (now - 2*seconds_back) liegen.
                # Falls doch → Daten-Inkonsistenz, lieber kein Spike-Label posten
                # als einen falschen.
                if spike_start_dt is not None:
                    age_sec = (now - spike_start_dt).total_seconds()
                    if age_sec > seconds_back * 2 or age_sec < 0:
                        logger.warning(
                            f"{symbol}: spike_start inkonsistent "
                            f"(age={age_sec:.0f}s, erwartet ≤{seconds_back*2}s) — "
                            f"Label unterdrückt"
                        )
                        spike_start_dt = None
                        spike_time_label = None

                # Kombiniertes Start→End Label für die Caption
                # (matched die beiden vertikalen Linien im Chart).
                if spike_start_dt is not None and spike_end_dt is not None:
                    spike_range_label = (
                        f"{spike_start_dt.strftime('%H:%M:%S')} → "
                        f"{spike_end_dt.strftime('%H:%M:%S')} UTC"
                    )
                elif spike_start_dt is not None:
                    spike_range_label = spike_start_dt.strftime('%H:%M:%S UTC')
                else:
                    spike_range_label = None

                # HTML-Caption erweitert: Spike-Range + optional Dead-Cat-Warning + 10m-Trend
                extra_lines = ""
                if spike_range_label:
                    extra_lines += f'\n<b>→ Spike: {spike_range_label}</b>'
                if chg_10m is not None:
                    trend_col = "#00ff88" if chg_10m > 0 else "#ff6688"
                    extra_lines += f'\n<b>→ 10m trend: <b>{chg_10m:+.2f}%</b></b>'
                if dead_cat:
                    extra_lines += (
                        '\n<b>⚠ ATTENTION: DEAD CAT BOUNCE '
                        '(kurzer Bounce im Abwärtstrend)</b>'
                        if chg_pct > 0 else
                        '\n<b>⚠ ATTENTION: DIP IN UPTREND '
                        '(kurzer Einbruch im Aufwärtstrend)</b>'
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

                # Chart mit Spike-Region (Start + End vertikale Linien
                # und schattierte Fläche dazwischen)
                chart_path = generate_minichart_image(
                    symbol, minutes=240,
                    spike_start=spike_start_dt,
                    spike_end=spike_end_dt,
                )
                send_outbox(conn, MARKET_CHANNEL_ID, html, chart_path)

                alerted = True
                if abs(chg_pct) >= 10.0: use_ext_cooldown = True
                break

        # 2. Volume Explosion
        if not alerted and len(volumes_10s) >= 360:
            rec_vols, rec_prices = volumes_10s[-18:], prices[-18:]
            p_chg_3m = (current_price / rec_prices[0] - 1) * 100 if rec_prices[0] > 0 else 0

            if abs(p_chg_3m) >= 2.0:
                avg_hr_vol = sum(volumes_10s[-360:]) / 360
                if avg_hr_vol > 0:
                    vol_factor = sum(rec_vols) / (avg_hr_vol * 18)
                    if vol_factor >= 12.0:
                        col = "#00ff88" if p_chg_3m >= 2.0 else "#ff6688"
                        pres = "BUY PRESSURE" if p_chg_3m >= 2.0 else "SELL PRESSURE"
                        html = f"""<pre><b>📈 VOLUME EXPLOSION</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n<b>→ <b>{vol_factor:.1f}× in last 3min ({pres} {p_chg_3m:+.2f}%)</b></b>\n<b>→ Price: <code>${current_price:,.8f}</code></b></pre>"""
                        send_outbox(conn, MARKET_CHANNEL_ID, html, generate_minichart_image(symbol, minutes=240))
                        alerted = True

        if alerted: pv_state["last_alert_time"] = now + datetime.timedelta(
            seconds=(900 - 300) if use_ext_cooldown else 0)

    # B) ML PUMP/DUMP DETECTOR (AI Channel) - FAST 10 FEAT MODEL
    if len(pd_state["volume_samples"]) < 60: return

    if len(pd_state["volume_samples"]) == 360:
        pd_state["avg_volume"] = sum(pd_state["volume_samples"]) / 360
    elif pd_state["avg_volume"] == 0:
        pd_state["avg_volume"] = sum(pd_state["volume_samples"]) / len(pd_state["volume_samples"])

    if pd_state["avg_volume"] <= 0: return

    vol_ratio = current_vol / pd_state["avg_volume"]
    rec_prices = prices[-7:]
    p_chg_60s = (rec_prices[-1] / rec_prices[0] - 1) * 100 if len(rec_prices) >= 7 else 0
    buy_pres = sum(1 for j in range(1, len(rec_prices)) if rec_prices[j] > rec_prices[j - 1]) / max(1,
                                                                                                    len(rec_prices) - 1)
    volat = np.std(rec_prices) / np.mean(rec_prices) if np.mean(rec_prices) > 0 else 0
    change_5min = (current_price / float(data[-30]["p"]) - 1) * 100 if len(data) >= 30 else 0

    inds = get_indicators_at_time(conn, symbol) or {}
    rsi = float(inds.get('rsi_14', 50))
    tsi = float(inds.get('tsi_fast_12_7_7', 0))
    macd = float(inds.get('macd_dif_normal_12_26_9', 0))
    ema9 = float(inds.get('ema_9', current_price))
    ema21 = float(inds.get('ema_21', current_price))
    e9_dist = (current_price - ema9) / ema9 * 100 if ema9 > 0 else 0
    e21_dist = (current_price - ema21) / ema21 * 100 if ema21 > 0 else 0

    # Event in DB speichern (wird vom Housekeeping später gereinigt)
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS pump_dump_events (symbol VARCHAR(20), spike_time TIMESTAMP, volume_ratio REAL, price_change_60s REAL, buy_pressure REAL, volatility REAL, rsi_14 REAL, tsi REAL, macd_dif REAL, ema9_distance_pct REAL, ema21_distance_pct REAL)""")
        cur.execute(
            "INSERT INTO pump_dump_events (symbol, spike_time, volume_ratio, price_change_60s, buy_pressure, volatility) VALUES (%s, %s, %s, %s, %s, %s)",
            (symbol, now, float(vol_ratio), float(p_chg_60s), float(buy_pres), float(volat)))
        conn.commit()

    if (now - pd_state["last_alert_time"]).total_seconds() < 900: return

    # Modell holen (nur das 10-Feature Modell!)
    model = load_pump_model()
    if model is None: return

    # --- ML CHECK (Schnelles 10-Feature Modell) ---
    features_array = np.array([[
        vol_ratio, p_chg_60s, buy_pres, volat,
        len(pd_state["volume_samples"]) / 360.0,
        rsi, tsi, macd, e9_dist, e21_dist
    ]])

    try:
        prob = model.predict_proba(features_array)[0]
        classes = list(model.classes_)

        prob_dump = prob[classes.index(0)] if 0 in classes else 0
        prob_pump = prob[classes.index(2)] if 2 in classes else 0

        best_prob = max(prob_pump, prob_dump)
        best_direction = "LONG" if prob_pump >= prob_dump else "SHORT"
    except Exception as e:
        logger.error(f"Prediction Fehler in HF Loop: {e}")
        return

    # === LOGIK ANWENDEN ===
    module_tag = "EPD1"

    if best_prob < 0.25:
        pass  # Schrott ignorieren

    elif 0.25 <= best_prob < 0.60:
        # Shadow Mode: Ablegen in Master Tabelle
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted)
                VALUES (0, %s, %s, %s, %s, %s, %s, False)
            """, (module_tag, now, symbol, best_direction, float(current_price), float(best_prob)))
        conn.commit()

    elif best_prob >= 0.60:
        # 🔥 BINGO! Trade ausführen
        color = "#00ff00" if best_direction == "LONG" else "#ff0066"
        emoji = "🚀 EARLY PUMP DETECTION" if best_direction == "LONG" else "💥 EARLY DUMP ALERT"

        is_long = (best_direction == "LONG")
        entry1 = current_price
        entry2 = entry1 * 0.95 if is_long else entry1 * 1.05
        supps, resis = get_hvn_and_sr_levels(conn, symbol, current_price)

        if is_long:
            sl = max([x for x in supps if x < entry2 * 0.99]) if any(
                x < entry2 * 0.99 for x in supps) else entry2 * 0.975
            t_cands = sorted([x for x in resis if x > (entry1 * 1.01)])
        else:
            sl = min([x for x in resis if x > entry2 * 1.01]) if any(
                x > entry2 * 1.01 for x in resis) else entry2 * 1.025
            t_cands = sorted([x for x in supps if x > 0 and x < (entry1 * 0.99)], reverse=True)

        # FIX: echte Zonen + ggf. 5%-Target wenn letzte Zone zu nah
        targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)

        lev = get_max_leverage(symbol, 20)

        lines = [f"📈 Signal for {symbol} 📈", f"🚨 Direction: {best_direction}", f"🚨 Leverage: {lev}", f"🚨 Margin: Cross",
                 f"🏦 CMP Entry: $ {entry1:.8f}", f"🏦 Entry 2: $ {entry2:.8f}"]
        for i, t in enumerate(targets[:3], 1): lines.append(f"💰 TP{i}: $ {t:.8f}")
        lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module_tag}"]
        cornix_msg = "\n".join(lines)

        chg_color = "#00ff00" if change_5min > 0 else "#ff0066"
        html_caption = f"""<pre><b>{emoji}</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n<b>→ Direction: <b>{best_direction}</b></b>\n<b>→ Price: <code>${current_price:,.8f}</code> <b>({change_5min:+.2f}% / 5m)</b></b>\n<b>→ Volume: <b>{vol_ratio:.1f}×</b> above avg</b>\n<b>→ ML-Confidence: <b>{best_prob:.1%}</b> / Modul: {module_tag} V3</b>\n<b>→ Time: {now.strftime('%H:%M')} UTC</b>\n\n{cornix_msg}</pre>"""

        chart_buf = generate_minichart_image(symbol, minutes=240)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                        (AI_CHANNEL_ID, cornix_msg))
            if chart_buf:
                cur.execute("INSERT INTO telegram_outbox (channel_id, message, image_path) VALUES (%s, %s, %s)",
                            (AI_CHANNEL_ID, html_caption, chart_buf))
            else:
                cur.execute("INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                            (AI_CHANNEL_ID, html_caption))


            cur.execute("""
                            INSERT INTO ai_signals (symbol, price, model, direction, confidence, entry1, entry2, sl, targets)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (symbol, float(entry1), module_tag, best_direction, float(best_prob), float(entry1), float(entry2), float(sl), json.dumps(targets)))
            # Auch bei Live-Trades im Prediction-Master archivieren!
            cur.execute(
                """INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted) VALUES (0, %s, %s, %s, %s, %s, %s, True)""",
                (module_tag, now, symbol, best_direction, float(current_price), float(best_prob)))
        conn.commit()

        logger.info(f"🤖 AI-Trade gesendet: {symbol} {best_direction} via {module_tag} (Conf: {best_prob:.1%})")
        pd_state["last_alert_time"] = now


# MAIN LOOP
def main():
    logger.info("=== 🏎️ 10-SEC HIGH FREQUENCY DETECTOR GESTARTET ===")

    session = requests.Session()
    conn = get_db_connection()

    # 💥 DER FIX: Autocommit aktivieren, damit ein kleiner Fehler nicht den ganzen Bot lahmlegt!
    conn.autocommit = True

    # 1. State und Cache laden (Keine Kaltstart-Blindheit mehr!)
    load_state_from_disk()

    # 2. Initiale Modell-Ladung
    load_pump_model()

    # 3. Coins laden
    try:
        with open("coins.json", "r") as f:
            coins = json.load(f)
            logger.info(f"✅ {len(coins)} Coins aus coins.json geladen.")
    except Exception as e:
        logger.error(f"❌ Error loading von coins.json: {e}")
        return

    last_save_time = time.time()

    try:
        while True:
            try:
                now = datetime.datetime.now(datetime.timezone.utc)

                # State alle 5 Minuten sichern
                if time.time() - last_save_time > 300:
                    save_state_to_disk()
                    last_save_time = time.time()

                # Timing: Exakt auf die 10-Sekunden-Marke synchronisieren
                seconds = now.second
                sleep_time = (10 - seconds % 10) if seconds % 10 != 0 else 10
                time.sleep(sleep_time)

                res = session.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=5)
                if res.status_code != 200: continue
                raw_data = res.json()

                ts_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                for item in raw_data:
                    symbol = item["symbol"]
                    if symbol not in coins: continue

                    price = float(item["lastPrice"])
                    cum_vol = float(item["volume"])

                    # /ticker/24hr liefert rollierendes 24h-Volumen, KEIN monotones
                    # Cumulative. Bei Rollover (alte Trades fallen aus dem 24h-Window)
                    # kann cum_vol kleiner werden → Delta negativ. In dem Fall ist
                    # das Delta nicht aussagekräftig und wir markieren es als ungültig
                    # (v10s_valid=False), damit Pump-Detection diese Messung ignoriert.
                    prev_vol = ONE_MINUTE_DATA[symbol][-1]["cum_vol"] if symbol in ONE_MINUTE_DATA and ONE_MINUTE_DATA[symbol] else None
                    if prev_vol is None:
                        v10s = 0.0
                        v10s_valid = False  # erster Datenpunkt — kein Delta möglich
                    else:
                        raw_delta = cum_vol - prev_vol
                        if raw_delta < 0:
                            # 24h-Rollover: Messung nicht verwertbar
                            v10s = 0.0
                            v10s_valid = False
                        else:
                            v10s = raw_delta
                            v10s_valid = True

                    entry = {
                        "t": ts_str, "p": price, "v10s": v10s,
                        "v10s_valid": v10s_valid, "cum_vol": cum_vol
                    }

                    if symbol not in ONE_MINUTE_DATA:
                        ONE_MINUTE_DATA[symbol] = deque(maxlen=1440)

                    if len(ONE_MINUTE_DATA[symbol]) > 0:
                        prev_price = ONE_MINUTE_DATA[symbol][-1]["p"]
                        check_round_levels(conn, symbol, price, prev_price)

                    ONE_MINUTE_DATA[symbol].append(entry)
                    process_coin_logics(conn, symbol)

            except Exception as e:
                logger.error(f"HF Loop Error: {e}")
                time.sleep(5)

    except KeyboardInterrupt:
        # Fängt das Strg+C ab, wenn es während time.sleep() im Loop passiert
        logger.info("🛑 Shutdown-Signal (STRG+C) im Loop empfangen!")
    finally:
        # Wird IMMER ausgeführt, wenn die while-Schleife verlassen wird
        if conn:
            conn.close()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Bot manuell stopped (Strg+C). Rette Daten...")
    finally:
        # Das hier ist die absolute Lebensversicherung:
        # Egal WO der Bot abstürzt oder abgebrochen wird, er rettet die Daten!
        save_state_to_disk()
        logger.info("✅ Cache erfolgreich gesichert. Fahre sauber herunter.")
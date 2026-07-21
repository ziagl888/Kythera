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
from core.signal_post import has_open_ai_signal, log_prediction, post_ai_signal_gated
from core.trade_utils import ensure_min_tp_distance, get_hvn_and_sr_levels

logging.basicConfig(level=logging.INFO, format='%(asctime)s - PUMP_DUMP_DETECTOR - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG & CHANNELS ---
# 10s-Persistenz für Microstructure-Features (PEX1 V2): Kill-Switch per Env,
# damit Ops den Schreiber ohne Code-Deploy stilllegen kann (Muster P1.34).
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

# P1.39: Fenster-Randschutz für die zeit-basierten Lookups.
#
# Die Bucket-Stempel liegen exakt auf dem 10s-Raster (`_tick_epoch % 10`). Wird
# ein Fenster gegen den Anker `bucket_anchor` (= Stempel des jüngsten Buckets)
# gemessen, fällt jeder Zielzeitpunkt exakt auf einen Rasterpunkt — der Guard
# absorbiert dann nur noch Float-/Parse-Rauschen an der Grenze.
#
# 5s ist kleiner als der halbe Bucket-Abstand (10s), der Guard kann also nie
# einen zusätzlichen Bucket hereinlassen. Genau dafür ist er klein gewählt.
#
# WICHTIG: der Guard ersetzt NICHT den Anker. Gegen die Wanduhr `now` gemessen
# wandert der Zielzeitpunkt um den Phasenversatz [0,10) zwischen Rasterpunkt und
# Aufrufzeit, und das Fenster kippte zwischen 6 und 7 Buckets — ein Modell-
# Feature, das springt, ohne dass sich der Markt bewegt hat. Deshalb ankern alle
# Feature-Lookups auf `bucket_anchor`, nicht auf `now`.
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
# the Volume-Explosion alert would never fire again. Gate on the window actually
# COVERING an hour plus a sample floor instead of on a bucket count.
HOUR_WINDOW_MIN_COVERAGE_SEC = 3000
HOUR_WINDOW_MIN_SAMPLES = 30

# --- ML MODEL FOR 10 SECONDS ---
#
# ZWEI Generationen, zwei Formate (T-2026-CU-9050-042):
#
#   LEGACY (heute live) — pump_dump_model.pkl ist ein ROHES 3-Klassen-Modell.
#       Erfolg = Klasse 2 (Pump) bzw. Klasse 0 (Dump), Features als POSITIONALES
#       Array, kein Meta, kein Threshold. Es postet unter der Konstanten EPD2.
#
#   EPD2-RETRAIN — epd2_model_{LONG,SHORT}.pkl sind dict-Artefakte
#       (tools/retrain_from_replay.py --strategy epd): je Richtung ein BINÄRES
#       Modell (Erfolg = predict_proba[:, 1]), Features NACH NAMEN inkl. der
#       6 Funding-Spalten, Threshold und model_id in der Meta.
#
# Liegen die Artefakte, gewinnen sie; der Tag kommt dann aus meta.model_id
# (harte Regel 6) statt aus EPD_LEGACY_TAG. Ohne Artefakte läuft der Legacy-Pfad
# unverändert weiter — diese Session ändert die Live-Semantik nicht.
ML_MODEL_PATH = "pump_dump_model.pkl"
EPD2_ARTIFACT_PATHS = {"LONG": "epd2_model_LONG.pkl", "SHORT": "epd2_model_SHORT.pkl"}

# Tag, unter dem der Bot vor dem Retrain-Rollout postet — default_tag für ein
# Artefakt ohne model_id UND transitionaler Dedup-Key (siehe log_prediction).
EPD_LEGACY_TAG = "EPD2"
# Posting-Schwelle des Legacy-Modells. Ein EPD2-Artefakt bringt seine eigene mit.
EPD_LEGACY_THRESHOLD = 0.60
# Untergrenze des Shadow-Bands (darunter: Schrott, gar kein Log).
EPD_SHADOW_THRESHOLD = 0.25

# Die 10 Basis-Features in der Reihenfolge, in der das LEGACY-Modell sie als
# positionales Array erwartet. Reihenfolge ist hier Vertrag — nicht sortieren.
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
# Was der Serving-Builder liefern KANN (P0.12-Vertrag): Basis + Funding.
EPD_EXPECTED_FEATURES = EPD_BASE_FEATURES + list(FUNDING_FEATURES)

_ml_model = None
_ml_model_time = None
_epd2: dict[str, dict] = {}

# EPD3-Shadow (T-2026-CU-9050-125): der epd2-Retrain (beide Richtungen "nicht
# deploybar" im 4,5-Monats-Fenster ohne Alt-Pump-Phase) läuft PARALLEL als Shadow
# aus staging_models/. WICHTIG: Bot 10 postet das LIVE-EPD-Bein bereits unter Tag
# "EPD2" (EPD_LEGACY_TAG); der Shadow bekommt deshalb den kollisionsfreien Tag
# "EPD3" (analog RUB3) — sonst würde ein Shadow-Trade über den Active-Trade-Check
# `model IN ('EPD2','EPD2')` einen LIVE-Post unterdrücken. Nie live; der AI-Monitor
# sammelt die frischen Outcomes (closed_ai_signals) für die Wiedervorlage.
_shadow_epd3: dict[str, object | None] = {"LONG": None, "SHORT": None}


def load_epd2_artifacts():
    """Lädt die Retrain-Artefakte (dict-Format) — leer, solange keine deployt sind."""
    for direction, path in EPD2_ARTIFACT_PATHS.items():
        _epd2[direction] = load_artifact(path, EPD_EXPECTED_FEATURES, EPD_LEGACY_TAG)
    # EPD3-Shadow-Modelle aus staging_models/ (fail-soft; Tag EPD3, Datei epd2_*).
    for direction in ("LONG", "SHORT"):
        _shadow_epd3[direction] = shadow_gate.load_shadow_artifact("EPD3", direction)
    if any(_shadow_epd3.values()):
        loaded = [d for d, m in _shadow_epd3.items() if m is not None]
        logger.info(f"👻 EPD3 Shadow-Modelle geladen: {', '.join(loaded)}")
    return {d: a for d, a in _epd2.items() if a["loaded"]}


def _emit_epd3_shadow(conn, symbol, base_features, now, current_price):
    """EPD3-Emission über das shadow_gate-Routing (T-2026-CU-9050-125/185).

    Baut den IDENTISCHEN 16-Feature-Vektor wie der Live-EPD2-Pfad (base_features
    + Funding as-of, gecacht — Regel 7), scored die Artefakte je Richtung, nimmt den
    stärksten Kandidaten und emittiert bei prob>=threshold via post_ai_signal_gated:
    das LIVE-Bein EPD3 SHORT (@0.6737, T-185, Artefakt im Repo-Root) postet Cornix an
    CH_PUMP_AI (koexistierend mit EPD2), das SHADOW-Bein EPD3 LONG (threshold=None,
    staging) bleibt ein überwachter Shadow-Trade (kein Cornix). Der Live-SHORT feuert
    nur, wenn das Modell SHORT als stärkste Richtung über seinem Threshold wählt.
    Geometrie = dieselbe HVN/S-R-Konstruktion wie der Live-Pfad (bewusst dupliziert).
    Fehler bleiben gekapselt.
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
        # Hot-Path-Guard (P1.41-Lehre): Bot 10 läuft je 10s-Tick, und der 900s-
        # Timer wird nur im Live-Trade-Zweig zurückgesetzt — ohne diesen Early-Out
        # liefe die teure HVN/S-R-Geometrie (DB-Query) auf JEDEM Tick, solange ein
        # EPD3-Shadow-Trade dieses Coins offen ist (LONG-Threshold ist null → feuert
        # immer). Der has_open-Check in post_shadow_ai_signal käme erst NACH der
        # Geometrie — deshalb hier vorziehen.
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
            _kcfg.CH_PUMP_AI,  # LIVE-Leg EPD3 SHORT → Pump-AI-Channel (T-185); LONG bleibt Shadow
            symbol,
            best_prob,
            entry1,
            entry2,
            sl,
            targets,
            source_desc="AI EPD3 Pump/Dump Retrain",
            n_show=3,
        )
        if outcome is not None:
            conn.commit()
    except Exception as e:
        logger.warning(f"EPD3 Shadow für {symbol} fehlgeschlagen: {e}")
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
# Bucket-Historie pro Coin (T-2026-CU-9050-165): das größte Zeitfenster im
# Detector ist 3600s (+20s Toleranz) — bei dichtem 10s-Raster sind das 362
# Buckets. 720 deckt 2h (doppeltes Headroom für Cadence-Lücken); die alten
# 1440 (~4h) wurden von keinem Lookup je erreicht (alle Fenster sind
# zeitbasiert und brechen früher ab) und verdoppelten nur State-Dump & RAM.
BUCKET_DEQUE_MAXLEN = 720
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
            with open(DATA_FILE, encoding="utf-8") as f:
                raw_data = json.load(f)
            for symbol, entries in raw_data.items():
                dq = deque(maxlen=BUCKET_DEQUE_MAXLEN)
                for entry in entries[-BUCKET_DEQUE_MAXLEN:]:
                    # Epoch-Cache einmalig beim Load füllen (Alt-Dateien ohne
                    # 'e'-Feld) — sonst zahlt der erste Tick den Parse für die
                    # gesamte geladene Historie im Hot Path.
                    if "e" not in entry:
                        ts = _parse_bucket_ts(entry)
                        if ts is not None:
                            entry["e"] = ts.timestamp()
                    dq.append(entry)
                ONE_MINUTE_DATA[symbol] = dq
            logger.info(f"✅ Cache: {len(ONE_MINUTE_DATA)} Coins aus {DATA_FILE} geladen.")
        except Exception as e:
            logger.error(f"Error loading von {DATA_FILE}: {e}")

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
            logger.info(f"✅ Cache: ML-States aus {STATE_FILE} geladen. (Keine Blind-Phase!)")
        except Exception as e:
            logger.error(f"Error loading von {STATE_FILE}: {e}")


def save_state_to_disk():
    """Speichert den aktuellen Zustand kugelsicher auf die Festplatte (Atomic Write)."""
    try:
        # 1. Kerzen speichern (Atomic Write)
        # Kompakt statt indent=2 (T-2026-CU-9050-165): der Dump lief alle 5min
        # über 527 Coins × 720 Buckets — mit Pretty-Print >100MB und ~9s reine
        # Serialisierung; kompakt ist die Datei weniger als halb so groß und der
        # CPU-Spike entsprechend kürzer. Kein menschlicher Leser, reiner
        # Restart-Cache.
        raw_data = {sym: list(dq) for sym, dq in ONE_MINUTE_DATA.items()}
        tmp_data_file = DATA_FILE + ".tmp"
        with open(tmp_data_file, "w", encoding="utf-8") as f:
            json.dump(raw_data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()  # Zwingt das OS, den Puffer auf die Platte zu schreiben
            os.fsync(f.fileno())
        # Atomically rename file (replaces old file immediately)
        os.replace(tmp_data_file, DATA_FILE)

        # 2. States speichern (Atomic Write)
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

        logger.info("💾 State backup saved successfully (atomic).")
    except Exception as e:
        logger.error(f"Error saving der States: {e}")


# --- HELPER FUNKTIONEN ---


def get_indicators_at_time(conn, coin):
    """Holt die letzten GESCHLOSSENEN 1h-Indikatoren für die ML-Features.

    R1: include_forming=False — die Erkennung darf nicht auf der forming Kerze
    rechnen (vorher: DESC LIMIT 1 ohne Bound = Partial-Indikatoren der laufenden
    Kerze).
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
    """Schiebt eine Nachricht in die Outbox."""
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
        logger.error(f"Outbox error: {e}")
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
    """Parst den 't'-Zeitstempel eines Bucket-Eintrags. None bei Fehler."""
    try:
        ts_str = entry.get("t", "")
        return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _bucket_epoch(entry: dict) -> float | None:
    """Epoch-Sekunden eines Buckets — der Vergleichsschlüssel aller Fenster-Lookups.

    Neue Buckets tragen 'e' ab Erzeugung (main-Loop), Alt-Buckets aus einer
    1minute.json von vor dem Feld (und Test-Fixtures) werden beim ersten Zugriff
    EINMAL geparst und in-place gecacht. Vorher lief fromisoformat auf JEDEM
    Vergleich neu — bei 527 Coins × ≤MAXLEN Buckets × ~10 Scans/Tick war das der
    dominante CPU-Posten des gesamten Bots (T-2026-CU-9050-165: 4,3s von 10s
    Tick-Budget nur für die Fenster-Scans; mit Epoch-Floats 1,2s). None bei
    unparsebarem Timestamp — Caller überspringen den Bucket wie bisher.
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
    """Anker (datetime oder bereits Epoch-Float) → Epoch-Sekunden.

    Kein isinstance gegen datetime.datetime: die Testsuite friert die Klasse
    per MagicMock(wraps=...) ein — isinstance gegen den Mock würfe TypeError,
    obwohl die durchgereichten Instanzen echte datetimes sind. Der Check läuft
    deshalb gegen die builtins (float/int), alles andere ist ein datetime.
    """
    if isinstance(anchor, (int, float)):
        return float(anchor)
    return anchor.timestamp()


def _find_bucket_before(
    data: list, now: "datetime.datetime | float", seconds_ago: int, tolerance: int = 20
) -> dict | None:
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

    target = _anchor_epoch(now) - seconds_ago

    # Von hinten after vorne durchgehen — der neueste passending Bucket gewinnt
    for entry in reversed(data):
        e = _bucket_epoch(entry)
        if e is None:
            continue
        if abs(e - target) <= tolerance:
            return entry
        # Wenn wir schon weiter in der Vergangenheit sind als target+tolerance,
        # gibt es keinen Treffer mehr
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
    """Gibt alle Buckets zurück die im Zeitraum [now - seconds_ago, now] liegen.

    Robust gegen Lücken und alte State-Daten — nutzt ausschließlich die
    Timestamps der Buckets, nicht deren Position in der Liste.
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

    return list(reversed(result))  # wieder chronologisch


def _scan_hour_and_lookbacks(
    data: list,
    anchor: "datetime.datetime | float",
    lookback_secs: "list[int]",
    hour_sec: int,
    hour_tol: int,
    lb_tol: int,
) -> "tuple[list, dict[int, dict | None]]":
    """EIN Reverse-Pass statt 1×_find_bucket_range(hour) + N×_find_bucket_before(lb).

    T-2026-KYT-9050-019: der Stunden-Scan (Volume-Explosion + ML-Baseline) und die
    6 Price-Move-Lookbacks scannen im Steady State (kein Alert) jeden Tick dieselbe
    Deque bis ~3600s zurück — zusammen ~886 Bucket-Iterationen/Coin/Tick. Diese
    Funktion faltet beide in eine Traversierung (~362 Iter). Sie ist BYTE-IDENTISCH
    zu den Einzelaufrufen konstruiert (Fuzz-Test test_scan_windows_matches_originals):

      - hour_buckets == _find_bucket_range(data, anchor, hour_sec, tolerance=hour_tol)
      - lb_refs[sb]  == _find_bucket_before(data, anchor, sb,       tolerance=lb_tol)
                        für jedes sb in lookback_secs

    Ankern, None-Semantik (fehlender Bucket ⇒ None ⇒ Caller skippt) und die
    „jüngster Treffer im Band"-Wahl von _find_bucket_before bleiben unverändert.
    """
    anchor_e = _anchor_epoch(anchor)
    lb_pending = sorted(lookback_secs)  # aufsteigende Sekunden = aufsteigendes Ziel-Alter
    lb_refs: dict[int, dict | None] = {sb: None for sb in lookback_secs}
    if not data:
        return [], lb_refs

    hour_cutoff = anchor_e - hour_sec - hour_tol
    hour_rev: list = []  # newest-first, am Ende chronologisch gedreht
    hour_done = False
    lb_idx = 0

    for entry in reversed(data):
        e = _bucket_epoch(entry)
        if e is None:
            continue

        # (a) Stunden-Fenster: sammeln solange e >= cutoff, dann dicht (break-Äquiv.)
        if not hour_done:
            if e >= hour_cutoff:
                hour_rev.append(entry)
            else:
                hour_done = True

        # (b) Lookback-Referenzen: age steigt monoton mit dem Scan. Für die aktuell
        #     flachste offene Bande gilt exakt _find_bucket_before(tol=lb_tol):
        #       age < sb-tol  → noch nicht im Band, dieser Entry ist zu jung → break
        #       age <= sb+tol → jüngster Treffer, festhalten und Bande schließen
        #       age >  sb+tol → Band übersprungen ⇒ None, Bande schließen
        #     Nach einem Match/None denselben Entry gegen die nächste (tiefere)
        #     Bande prüfen (continue), bis er zu jung ist (break).
        age = anchor_e - e
        while lb_idx < len(lb_pending):
            sb = lb_pending[lb_idx]
            if age < sb - lb_tol:
                break
            if age <= sb + lb_tol:
                lb_refs[sb] = entry
            # sonst: age > sb+tol → lb_refs[sb] bleibt None
            lb_idx += 1

        if hour_done and lb_idx >= len(lb_pending):
            break

    return list(reversed(hour_rev)), lb_refs


# (seconds_back, min_pct) je Extreme-Move-Lookback (10s-Buckets: 12/18/30/42/60/360
# Buckets = 120…3600s). Die Sekunden-Spalte ist ZUGLEICH die Lookback-Liste für
# _scan_hour_and_lookbacks — eine Quelle, damit Single-Pass-Scan und Alert-Schleife
# nie auseinanderdriften.
PRICE_MOVE_LOOKBACKS = [(120, 3.0), (180, 4.0), (300, 5.0), (420, 7.5), (600, 10.0), (3600, 20.0)]
PRICE_MOVE_LOOKBACK_SECS = [sb for sb, _ in PRICE_MOVE_LOOKBACKS]


# 2. EXTREME MOVE & PUMP/DUMP DETECTOR
def process_coin_logics(conn, symbol):
    # T-2026-KYT-9050-019: die Deque direkt lesen statt sie pro Coin/Tick in eine
    # Liste zu kopieren. `data` wird ausschliesslich über `data[-1]` (O(1) am
    # Ende) und `reversed(data)` (in den _find_bucket_*-Helfern) angefasst, nie
    # gesliced — beides kann die Deque nativ. Der Loop ist single-threaded und
    # main() appended den frischen Bucket VOR diesem Aufruf, es gibt also keine
    # nebenläufige Mutation, gegen die die Snapshot-Kopie schützen müsste.
    data = ONE_MINUTE_DATA[symbol]
    if len(data) < 36:
        return  # Brauchen etwas Historie

    # Invarianten dieser Funktion (P1.39 — bitte vor jeder "Vereinfachung" lesen):
    #   1. Fenster werden über ZEITSTEMPEL ausgewählt, nie über Listen-Indizes.
    #      `data` kann Lücken haben, und `volumes_10s`-artige gefilterte Listen
    #      haben Positionen, die keinem Zeitpunkt entsprechen.
    #   2. Jeder `_find_bucket_*`-Aufruf ankert auf `bucket_anchor`, nie auf `now`.
    #      Sonst wandert das Fenster mit dem Aufrufzeitpunkt (siehe unten).
    #   3. `now` bleibt Wanduhr — Staleness-Check, die beiden Alert-Cooldowns und
    #      `pump_dump_events.spike_time` MÜSSEN daran hängen bleiben.
    #   4. Fehlt ein Bucket, wird der Tick übersprungen. Es wird KEIN Ersatzwert
    #      (0, letzter Preis, …) als Modell-Feature erfunden.
    now = datetime.datetime.now(datetime.timezone.utc)
    current_price = float(data[-1]["p"])
    current_vol = float(data[-1]["v10s"])

    # STALE-DATA-CHECK: Der letzte Bucket muss frisch sein (< 60s alt).
    # Nach einem Restart kann die aus 1minute.json geladene Historie bis zu
    # 4 Stunden alt sein. Wenn dann neue Buckets reinkommen, darf der Code
    # die alten NICHT als "vor 2 Minuten" behandeln. Wenn die Daten stale
    # sind, skippingn wir diesen Cycle — beim nächsten Tick ist der neueste
    # Bucket schon wieder frisch.
    # P1.39: ALLE Bucket-Lookups ankern auf dem jüngsten Bucket-Zeitstempel,
    # nicht auf der Wanduhr. Die Bucket-Stempel sind auf das 10s-Raster gefloort
    # (`_tick_epoch - _tick_epoch % 10`), `now` ist der Aufrufzeitpunkt irgendwo
    # innerhalb des Rasters — und der Detector iteriert ~530 Coins nach einem
    # REST-Roundtrip, der Versatz wandert also auch noch über den Batch.
    # Gegen `now` gemessen läge die 60s-Grenze mal vor, mal hinter dem Bucket von
    # vor 60s: das Fenster kippte je nach Aufrufzeitpunkt zwischen 6 und 7
    # Buckets, und ein Modell-Feature sprang, ohne dass sich der Markt bewegte.
    # Gegen den Bucket-Stempel liegt jeder Zielzeitpunkt exakt auf einem
    # Rasterpunkt. `now` bleibt für alles Wanduhr-Artige zuständig (Staleness,
    # Alert-Gates, spike_time).
    #
    # T-2026-KYT-9050-019: der Anker ist der gecachte Epoch-Float (`_bucket_epoch`),
    # nicht `_parse_bucket_ts`. Der neueste Bucket trägt 'e' ab Erzeugung im
    # main-Loop, hier las der frühere ISO-Parse also pro Coin pro Tick umsonst neu.
    # T-165 hatte fromisoformat als dominanten CPU-Posten identifiziert und in den
    # Fenster-Helfern bereits auf 'e' umgestellt — diese eine Anker-Stelle blieb
    # übrig. Ein Float-Anker ist zudem robuster: alle _find_bucket_*-Helfer
    # normalisieren ihn über _anchor_epoch (kein isinstance gegen datetime).
    bucket_anchor = _bucket_epoch(data[-1])
    if bucket_anchor is None:
        return

    latest_age_sec = now.timestamp() - bucket_anchor
    if latest_age_sec > 60:
        # Daten-Gap zu groß — nicht aussagekräftig für Pump-Detection.
        # Kann after Restart oder after WS-Ausfall passieren.
        logger.debug(f"{symbol}: stale data ({latest_age_sec:.0f}s alt), skipping")
        return

    # Wenn die aktuelle Volume-Messung durch einen 24h-Rollover ungültig ist,
    # skippingn wir Volume-basierte Checks komplett (Price-Check läuft weiter).
    current_vol_valid = bool(data[-1].get("v10s_valid", True))
    # P1.39: die flachen `prices`/`volumes_10s`-Listen sind weg. Jeder Konsument
    # holt sein Fenster jetzt über _find_bucket_before/_find_bucket_range aus
    # `data` und filtert `v10s_valid` selbst — Positionen in einer gefilterten
    # Liste sagen nichts über die Zeit aus.

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
    # Nur gültige Volume-Messungen in die Baseline-Samples aufnehmen.
    if current_vol_valid:
        pd_state["volume_samples"].append(current_vol)

    # Stunden-Fenster + die 6 Price-Move-Lookbacks in EINEM Reverse-Pass
    # (T-2026-KYT-9050-019, baut auf T-2026-CU-9050-165 auf): Volume-Explosion (A2),
    # der ML-Pfad (B) und die 6 _find_bucket_before-Lookbacks der Extreme-Move-Schleife
    # scannten dieselbe Deque bis ~3600s zurück — im Steady State jeden Tick ~886
    # Bucket-Iterationen/Coin. `_scan_hour_and_lookbacks` faltet sie in ~362 und ist
    # byte-identisch zu den Einzelaufrufen (Fuzz-Pin test_scan_windows_matches_originals).
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
        # WICHTIG: Der Lookback ist ZEITSTEMPEL-basiert, nicht Index-basiert.
        # `seconds_back` ist der Fensterabstand in SEKUNDEN (120…3600). Nach einem
        # Restart kann die Deque alte + neue Buckets mischen — dann ist data[-12]
        # NICHT mehr "vor 120 Sekunden". Deshalb suchen wir den Vergleichs-Bucket
        # per Zeitstempel (hier via lb_refs aus dem gemeinsamen Reverse-Pass).
        for seconds_back, min_pct in PRICE_MOVE_LOOKBACKS:
            # Referenz-Bucket vor `seconds_back` Sekunden (±20s Toleranz) — im
            # gemeinsamen Reverse-Pass oben vorberechnet (byte-identisch zum früheren
            # _find_bucket_before(data, bucket_anchor, seconds_back, tolerance=20)).
            past_entry = lb_refs[seconds_back]
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
                mins, secs = divmod(seconds_back, 60)
                t_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

                # Dead-Cat-Bounce-Check: 10-Minuten-Trend vs. Signal-Richtung
                # Zeit-basiert: Bucket von vor 600s (= 10min) suchen
                dead_cat = False
                chg_10m = None
                bucket_10m_ago = _find_bucket_before(data, bucket_anchor, 600, tolerance=30)
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
                spike_window = _find_bucket_range(data, bucket_anchor, seconds_back, tolerance=20)
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
                            f"(age={age_sec:.0f}s, erwartet ≤{seconds_back * 2}s) — "
                            f"Label unterdrückt"
                        )
                        spike_start_dt = None

                # Kombiniertes Start→End Label für die Caption
                # (matched die beiden vertikalen Linien im Chart).
                if spike_start_dt is not None and spike_end_dt is not None:
                    spike_range_label = (
                        f"{spike_start_dt.strftime('%H:%M:%S')} → {spike_end_dt.strftime('%H:%M:%S')} UTC"
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
                    extra_lines += f'\n<b>→ 10m trend: <b>{chg_10m:+.2f}%</b></b>'
                if dead_cat:
                    extra_lines += (
                        '\n<b>⚠ ATTENTION: DEAD CAT BOUNCE (kurzer Bounce im Abwärtstrend)</b>'
                        if chg_pct > 0
                        else '\n<b>⚠ ATTENTION: DIP IN UPTREND (kurzer Einbruch im Aufwärtstrend)</b>'
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

        # 2. Volume Explosion
        #
        # P1.39: zeit-basiert statt index-basiert. Die alte Form hatte zwei
        # Fehler: (a) `volumes_10s` ist auf `v10s_valid` GEFILTERT, `prices`
        # nicht — `volumes_10s[-18:]` und `prices[-18:]` zeigten also auf
        # unterschiedliche Zeitpunkte, sobald ein einziger Bucket ungültig war;
        # (b) bei WS-Lücken sind 18 Buckets nicht 3 Minuten und 360 nicht eine
        # Stunde. Beides schob das Fenster still, ohne dass irgendwas auffiel.
        if not alerted:
            rec_buckets = _find_bucket_range(data, bucket_anchor, 180, tolerance=WINDOW_EDGE_GUARD)
            rec_vols = [float(e["v10s"]) for e in rec_buckets if e.get("v10s_valid", True)]
            # Same band logic as the ML path below: a 3m reference that really is
            # ~3m old, carrying its true age, instead of demanding a grid point
            # that a 70s cadence never produces (T-2026-CU-9050-035).
            ref_3m = _find_bucket_nearest(data, bucket_anchor, 180, 150, 300)

            # Warmup gate: the hour window must COVER an hour and carry enough
            # samples — not contain 360 buckets, which no real cadence does.
            # hour_buckets/hour_vols/hour_covered: einmal pro Tick oben berechnet.
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
                        # Mittelwert statt sum/18: bei Lücken hat rec_vols
                        # weniger Buckets, und /18 hätte den Faktor gedrückt.
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

    # B) ML PUMP/DUMP DETECTOR (AI Channel) - FAST 10 FEAT MODEL
    #
    # P1.39: die vier Modell-Features unten wurden index-basiert gerechnet
    # (`prices[-7:]` als "60s", der volume_samples-Deque als "1h"). Bei einer
    # WS-Lücke bedeutete "-7" nicht 60 Sekunden — das Modell bekam ein still
    # gedehntes Fenster. Jetzt alles über _find_bucket_before/_find_bucket_range.
    #
    # ACHTUNG (OPUS-HANDOFF §4 Falle 2): vol_ratio/p_chg_60s/buy_pres/volat sind
    # Modell-Inputs UND werden so in pump_dump_events geloggt, woraus
    # tools/epd2_build_dataset.py trainiert. Diese Änderung verschiebt die
    # Serving-Verteilung gegen das aktuell deployte EPD2-Artefakt, bis ein
    # Retrain auf den neuen Definitionen ausgerollt ist (Operator-Entscheid
    # 2026-07-09, Folge-Task). Gaps waren vorher selten, aber genau in den
    # Spike-Momenten (WS-Last) am wahrscheinlichsten — der alte Wert war dort
    # falsch, nicht bloss anders.
    if len(pd_state["volume_samples"]) < 60:
        return

    # Baseline zeit-basiert: alle gültigen Buckets der letzten Stunde. Der Deque
    # zählt Ticks, nicht Sekunden — nach einer Lücke spannte er über mehr als
    # eine Stunde. Er bleibt nur noch Warmup-Gate und Vollständigkeits-Feature.
    # hour_buckets/hour_vols/hour_covered: einmal pro Tick oben berechnet
    # (tolerance=WINDOW_EDGE_GUARD, identischer Anker wie hier).
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

    # Nur Anzeige (Alert-Caption), kein Modell-Feature — fehlender Bucket ist
    # hier folgenlos.
    bucket_5m = _find_bucket_before(data, bucket_anchor, 300, tolerance=30)
    change_5min = (current_price / float(bucket_5m["p"]) - 1) * 100 if bucket_5m and float(bucket_5m["p"]) > 0 else 0

    # Event in DB speichern — aber NUR wenn es die Housekeeping-Retention
    # ueberleben wuerde (Schwellen zentral in core/config.py, dieselben Werte
    # nutzt der Retention-DELETE in 6_housekeeping.py). Vorher wurde JEDER
    # 10s-Tick pro Symbol geschrieben (~4,6M Rows/Tag, groesste Tabelle der DB)
    # und spaeter zu >99% wieder geloescht — reine WAL-/Vacuum-Churn (P1.40).
    # Steady-State-Trainingsdaten unveraendert: der Trainer sampelt nur
    # vol_ratio >= 5, und Rows unterhalb des Gates haette das Housekeeping
    # ohnehin vor dem naechsten Trainingslauf geloescht (lediglich das
    # transiente Fenster bis dahin entfaellt).
    # CREATE TABLE laeuft seit P1.40 einmalig in main(), nicht mehr pro Tick.
    if vol_ratio >= _kcfg.PUMP_EVENT_MIN_VOL_RATIO and abs(p_chg_60s) >= _kcfg.PUMP_EVENT_MIN_ABS_PCHG_60S:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pump_dump_events (symbol, spike_time, volume_ratio, price_change_60s, buy_pressure, volatility) VALUES (%s, %s, %s, %s, %s, %s)",
                (symbol, now, float(vol_ratio), float(p_chg_60s), float(buy_pres), float(volat)),
            )
            conn.commit()

    if (now - pd_state["last_alert_time"]).total_seconds() < 900:
        return

    # FIX (Audit Report 13, EPD1-P0): Der Trainer sampelt ausschließlich Events mit
    # volume_ratio >= 5 — ohne dieses Gate wird das Modell auf jedem 10s-Tick
    # out-of-distribution befragt (Kalibrierung corr≈0). Gate spiegelt das Training.
    if vol_ratio < 5.0:
        return

    # Modell holen: EPD2-Artefakte wenn deployt, sonst das Legacy-Modell.
    # maybe_reload läuft auch über NICHT geladene Contracts — so nimmt der Bot
    # einen späteren Artefakt-Deploy im 24h-Fenster auf, ohne Restart.
    for _d in list(_epd2):
        _epd2[_d] = maybe_reload(_epd2[_d], EPD_EXPECTED_FEATURES)
    epd2 = {d: a for d, a in _epd2.items() if a["loaded"]}
    model = None if epd2 else load_pump_model()
    if model is None and not epd2:
        return  # Idle-Modus (Falle 3)

    # Indikator-Fetch NACH Cooldown-/vol_ratio-/Model-Gate (T-2026-CU-9050-014):
    # die Werte fliessen ausschliesslich in features_array unten. Vorher lief
    # die Query auf JEDEM 10s-Tick pro Symbol (~108 Queries/s gegen die
    # *_indicators-Tabellen), obwohl >99% der Ticks an den Gates daruber
    # early-returnen. Der pump_dump_events-Insert nutzt keine Indikatoren.
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

    # EPD3-Shadow (T-2026-CU-9050-125): den staging-Retrain parallel scoren +
    # überwacht tracken, unabhängig vom Live-EPD-Pfad (kein Cornix, Tag EPD3).
    _emit_epd3_shadow(conn, symbol, base_features, now, current_price)

    try:
        if epd2:
            # EPD2: je Richtung ein binäres Modell. Funding-Features as-of JETZT —
            # der Dataset-Builder (tools/epd2_build_dataset.py:231) nimmt sie as-of
            # dem Event-Zeitstempel, und das Event ist genau dieser Tick.
            #
            # Der Load ist gecacht (funding_features_cached): der 900s-Timer sperrt
            # zwar vor dieser Strecke, wird aber nur im Live-Trade-Zweig gesetzt —
            # ein Coin, der dauerhaft im Shadow-Band predictet, zöge die Query sonst
            # auf JEDEM 10s-Tick. Der Cache-Schluessel kommt aus den Daten (bis zur
            # naechsten Abrechnung, die das Ergebnis aendern kann) und ist deshalb
            # wertneutral — siehe core/funding_features.next_feature_change.
            feats = {**base_features, **funding_features_cached(conn, symbol, now)}
            candidates = []
            for direction, art in epd2.items():
                # Fehlende Funding-HISTORIE ⇒ Spalten fehlen ⇒ hier 0, wie fillna(0)
                # im Trainer (train_binary). Das ist Serving-Parität, kein P0.12-Bruch:
                # load_artifact hat die Feature-NAMEN hart validiert.
                ml_input = pd.DataFrame([feats]).reindex(columns=art["features"]).fillna(0)
                candidates.append((float(art["model"].predict_proba(ml_input)[0, 1]), direction, art))
            best_prob, best_direction, best_art = max(candidates, key=lambda c: c[0])
            module_tag = best_art["tag"]
            post_threshold = float(best_art["threshold"])
        else:
            # LEGACY: EIN 3-Klassen-Modell, positionales Feature-Array.
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
        logger.error(f"Prediction Fehler in HF Loop: {e}")
        return

    # Aktiver Trade Check (T-2026-CU-9050-055) — prüft, ob für genau dieses
    # Modul/Coin/Richtung bereits ein nicht-geschlossener Trade läuft.
    # Der 900s-Timer oben ist eine FREQUENZ-Sperre und lebt nur im Speicher; ein
    # EPD-Trade läuft regelmässig länger, und ohne diesen Check öffnete das
    # Folgesignal eine ZWEITE Live-Position neben der ersten. Muster:
    # 11_ai_mis_bot.py:318. Der Check läuft NACH der Prediction, weil die
    # Richtung erst aus dem argmax feststeht (Operator-Entscheid 2026-07-10:
    # symbol+direction wie bei den Geschwistern, kein richtungsagnostischer Key).
    # Er sperrt bewusst auch den Shadow-Log — wie bei MIS/RUB ist der Trade das,
    # was zählt, nicht die Zeile.
    #
    # Der Tag ist zugleich der Dedupe-Key und kippt beim EPD3-Rollout; ohne den
    # Alt-Tag im IN blockte eine offene EPD2-Position das EPD3-Signal nicht mehr.
    # Solange die Tags übereinstimmen (heute), ist das IN ein No-op.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM ai_signals
            WHERE symbol = %s AND direction = %s AND model IN (%s, %s)
        """,
            (symbol, best_direction, module_tag, EPD_LEGACY_TAG),
        )
        if cur.fetchone():
            return  # Trade läuft live im AI Monitor

    # === LOGIK ANWENDEN ===
    # EPD2 (Operator 2026-07-06): Richtungs-Gate entfernt — beide Seiten handeln
    # wieder (Intent: Momentum-Mitfahren in beide Richtungen).
    #
    # module_tag und post_threshold kommen aus dem Prediction-Block oben: bei
    # deploytem Artefakt aus dessen Meta (model_id / optimal_threshold), sonst aus
    # den Legacy-Konstanten. Nie eine Konstante über einem geladenen Artefakt —
    # sonst verschmilzt ein EPD3-Retrain still mit der EPD2-Statistik (Regel 6).

    if best_prob < EPD_SHADOW_THRESHOLD:
        pass  # Schrott ignorieren

    elif EPD_SHADOW_THRESHOLD <= best_prob < post_threshold:
        # Shadow Mode: Ablegen in Master Tabelle.
        #
        # P1.41: das 900s-Gate oben (`last_alert_time`) bremst diesen Zweig NICHT
        # — zurückgesetzt wird der Timer nur im Live-Trade-Zweig unten. Ein Coin,
        # der dauerhaft im Shadow-Band (0.25..0.60) predictet, feuerte damit auf
        # JEDEM qualifizierenden 10s-Tick einen INSERT (bis 8640 Rows/Tag/Symbol).
        # Den Timer hier mitzusetzen wäre falsch: das würde auch echte Live-Signale
        # desselben Coins 900s lang unterdrücken. Stattdessen dedupt log_prediction
        # die Shadow-Zeilen selbst (4h je Modul/Coin/Richtung) — derselbe Pfad, den
        # die Bots 30-33 nutzen. Commit bleibt beim Caller (harte Regel 8).
        log_prediction(
            conn,
            module_tag,
            symbol,
            best_direction,
            float(current_price),
            float(best_prob),
            posted=False,
            # Der Dedup-Key ist der Tag. Beim EPD3-Rollout kippt er, und die neue
            # Generation begänne ihr 4h-Fenster bei null → doppelte Shadow-Zeilen
            # für denselben Coin. Gleicher Tag (heute) ⇒ No-op.
            legacy_tag=EPD_LEGACY_TAG,
        )
        conn.commit()

    elif best_prob >= post_threshold:
        # Direction-Gate ENTFERNT (Operator 2026-07-06): beide Richtungen handeln
        # wieder (Audit-Batch hatte LONG nach Report 14 D.5 in den Shadow gelegt).

        # 🔥 BINGO! Trade ausführen
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

        # FIX: echte Zonen + ggf. 5%-Target wenn letzte Zone zu nah
        targets = ensure_min_tp_distance(t_cands[:20], entry1, is_long, min_pct=0.05)

        lev = get_max_leverage(symbol, 20)

        lines = [
            f"📈 Signal for {symbol} 📈",
            f"🚨 Direction: {best_direction}",
            f"🚨 Leverage: {lev}",
            "🚨 Margin: Cross",
            f"🏦 CMP Entry: $ {entry1:.8f}",
            f"🏦 Entry 2: $ {entry2:.8f}",
        ]
        for i, t in enumerate(targets[:3], 1):
            lines.append(f"💰 TP{i}: $ {t:.8f}")
        lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by AI module {module_tag}"]
        cornix_msg = "\n".join(lines)

        html_caption = f"""<pre><b>{emoji}</b>\n<b>{symbol.replace('USDT', '')}/USDT</b>\n<b>→ Direction: <b>{best_direction}</b></b>\n<b>→ Price: <code>${current_price:,.8f}</code> <b>({change_5min:+.2f}% / 5m)</b></b>\n<b>→ Volume: <b>{vol_ratio:.1f}×</b> above avg</b>\n<b>→ ML-Confidence: <b>{best_prob:.1%}</b> / Modul: {module_tag} V3</b>\n<b>→ Time: {now.strftime('%H:%M')} UTC</b>\n\n{cornix_msg}</pre>"""

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
            # Auch bei Live-Trades im Prediction-Master archivieren!
            cur.execute(
                """INSERT INTO ml_predictions_master (trade_id, model_name, time, coin, direction, entry, confidence, posted) VALUES (0, %s, %s, %s, %s, %s, %s, True)""",
                (module_tag, now, symbol, best_direction, float(current_price), float(best_prob)),
            )
        conn.commit()

        logger.info(f"🤖 AI-Trade gesendet: {symbol} {best_direction} via {module_tag} (Conf: {best_prob:.1%})")
        pd_state["last_alert_time"] = now


# MAIN LOOP
def main():
    logger.info("=== 🏎️ 10-SEC HIGH FREQUENCY DETECTOR GESTARTET ===")

    # Retrain-Artefakte einmal beim Start prüfen. Keine da → Legacy-Pfad
    # (der heutige Zustand); die Artefakte gewinnen, sobald sie deployt sind.
    loaded = load_epd2_artifacts()
    logger.info(
        f"EPD-Artefakte: {sorted(loaded)} geladen"
        if loaded
        else f"Keine EPD2-Artefakte — Legacy-Modell '{ML_MODEL_PATH}' unter Tag {EPD_LEGACY_TAG}."
    )

    session = requests.Session()
    conn = get_db_connection()

    # 💥 DER FIX: Autocommit aktivieren, damit ein kleiner Fehler nicht den ganzen Bot lahmlegt!
    conn.autocommit = True

    # P1.40: Tabelle EINMAL beim Start anlegen statt pro Symbol pro 10s-Tick
    # (vorher ~108 CREATE-IF-NOT-EXISTS-Statements/Sekunde gegen den Katalog).
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS pump_dump_events (symbol VARCHAR(20), spike_time TIMESTAMP, volume_ratio REAL, price_change_60s REAL, buy_pressure REAL, volatility REAL, rsi_14 REAL, tsi REAL, macd_dif REAL, ema9_distance_pct REAL, ema21_distance_pct REAL)"""
        )

    # 10s-Ticker-Persistenz (Hypertable) — Schema einmalig beim Start sicherstellen.
    # Schlägt das fehl (z.B. Extension fehlt), läuft der Detector OHNE Persistenz
    # weiter — die Pump-Detection ist der Primärjob, nicht das Daten-Sammeln.
    ticker_10s_ok = False
    if TICKER_10S_PERSIST:
        try:
            ticker_10s.ensure_schema(conn)
            ticker_10s_ok = True
        except Exception as e:
            logger.error(f"❌ ticker_10s-Schema nicht verfügbar — Persistenz deaktiviert: {e}")

    # 1. State und Cache laden (Keine Kaltstart-Blindheit mehr!)
    load_state_from_disk()

    # 2. Initiale Modell-Ladung
    load_pump_model()

    # 3. Coins laden
    try:
        with open("coins.json") as f:
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
                if res.status_code != 200:
                    continue
                raw_data = res.json()

                # Auf die 10s-Marke gefloort: nur so erzeugt ein zweiter Writer
                # (Doppelstart) IDENTISCHE (symbol, ts)-Keys und ON CONFLICT
                # DO NOTHING gegen uq_ticker_10s_symbol_ts dedupliziert wirklich —
                # rohe now()-Stempel kollidieren wegen µs-Jitter nie. Preis ist
                # dann "Sample kurz nach der Marke", akzeptiert bei 10s-Raster.
                _tick_epoch = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
                tick_dt = datetime.datetime.fromtimestamp(_tick_epoch - _tick_epoch % 10, tz=datetime.timezone.utc)
                ts_str = tick_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                tick_rows = []  # (ts, symbol, price, vol_10s, vol_valid) für ticker_10s

                for item in raw_data:
                    symbol = item["symbol"]
                    if symbol not in coins:
                        continue

                    price = float(item["lastPrice"])
                    cum_vol = float(item["volume"])

                    # /ticker/24hr liefert rollierendes 24h-Volumen, KEIN monotones
                    # Cumulative. Bei Rollover (alte Trades fallen aus dem 24h-Window)
                    # kann cum_vol kleiner werden → Delta negativ. In dem Fall ist
                    # das Delta nicht aussagekräftig und wir markieren es als ungültig
                    # (v10s_valid=False), damit Pump-Detection diese Messung ignoriert.
                    prev_vol = (
                        ONE_MINUTE_DATA[symbol][-1]["cum_vol"]
                        if symbol in ONE_MINUTE_DATA and ONE_MINUTE_DATA[symbol]
                        else None
                    )
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

                    # 'e' = Epoch-Sekunden des Grid-Stempels — Vergleichsschlüssel
                    # der Fenster-Lookups (_bucket_epoch), 't' bleibt das
                    # ISO-Format für Dump-Lesbarkeit und Alt-Konsumenten.
                    entry = {
                        "t": ts_str,
                        "e": tick_dt.timestamp(),
                        "p": price,
                        "v10s": v10s,
                        "v10s_valid": v10s_valid,
                        "cum_vol": cum_vol,
                    }
                    if ticker_10s_ok:
                        # Auch v10s_valid=False persistieren (Rollover-Marker) —
                        # der Builder filtert selbst, genau wie process_coin_logics.
                        tick_rows.append((tick_dt, symbol, price, v10s, v10s_valid))

                    if symbol not in ONE_MINUTE_DATA:
                        ONE_MINUTE_DATA[symbol] = deque(maxlen=BUCKET_DEQUE_MAXLEN)

                    if len(ONE_MINUTE_DATA[symbol]) > 0:
                        prev_price = ONE_MINUTE_DATA[symbol][-1]["p"]
                        check_round_levels(conn, symbol, price, prev_price)

                    ONE_MINUTE_DATA[symbol].append(entry)
                    process_coin_logics(conn, symbol)

                # EIN batched Insert pro Tick (alle Coins) — nie den Loop stoppen,
                # ein verlorener Tick ist akzeptabel, ein toter Detector nicht.
                if tick_rows:
                    try:
                        ticker_10s.insert_ticks(conn, tick_rows)
                    except Exception as e:
                        logger.error(f"ticker_10s-Insert fehlgeschlagen (Tick verworfen): {e}")

            except Exception as e:
                logger.error(f"HF Loop Error: {e}")
                # Review-Fix (PR #9): ohne Rollback bleibt die Connection nach
                # einem DB-Fehler in InFailedSqlTransaction und JEDER folgende
                # Insert (pump_dump_events, Outbox, ticker_10s) schlägt fehl —
                # der Loop liefe weiter, wäre aber funktional tot. Muster wie
                # send_outbox.
                try:
                    conn.rollback()
                except Exception:
                    pass
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

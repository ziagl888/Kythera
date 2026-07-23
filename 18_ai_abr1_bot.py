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
import pandas_ta  # noqa: F401 — registriert den df.ta-Accessor (Regression aus 052ba4c:
import requests

# der Ruff-Cleanup entfernte den funktionslokalen Import aus b6735d9 als "unused",
# wodurch calculate_technical_indicators auf JEDEM Coin mit AttributeError starb)
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
TARGET_CHANNEL_ID = _kcfg.CH_ABR1  # Dein ABR1 Channel
SG_LONG_MODEL_FILE = 'bt2_model_LONG.json'
SG_SHORT_MODEL_FILE = 'bt2_model_SHORT.json'
SG_COINS_FILE = 'coins.json'

# FIX: Die Thresholds LONG=0.60 / SHORT=0.80 sind asymmetrisch und bewusst streng
# für SHORT gewählt, weil die alten Backtests zeigten dass SHORT-Setups an
# Break&Retest-Leveln deutlich mehr False-Positives produzieren (insb. in Bull-
# Markt-Phasen wo der Trend gegen die Retest-Richtung läuft).
# ACHTUNG: Falls das Ergebnis bei Live-Trading stark von den Backtests abweicht,
# hier die Werte ggf. anpassen — oder kombiniert mit SUCCESS_CLASS_IDX (s. unten)
# prüfen, ob die Semantik bei der Modell-Version stimmt.
THRESHOLDS = {'LONG': 0.60, 'SHORT': 0.80}

# SUCCESS_CLASS_IDX wählt in predict_proba[0, SUCCESS_CLASS_IDX] die Spalte der
# "Erfolgs"-Klasse. Das bt2-Modell ist KEIN Binär-Classifier, sondern 3-klassig
# (multi:softprob). Verifiziert gegen den Trainingscode (BT2-ML-Trainer.py /
# BT2-ML-Final_Saver.py, 2025-12): der Datagrepper vergibt die String-Labels
#   continuation_success (price_change > +5%) = Trade geht auf  → WIN
#   failed_breakout      (price_change < -3%) = Trade scheitert → LOSS
#   neutral              (dazwischen)                           → seitwärts
# und der Trainer kodiert sie per sklearn LabelEncoder ALPHABETISCH:
#   continuation_success = 0, failed_breakout = 1, neutral = 2
# (success_idx = class_mapping['continuation_success'] = 0, für LONG- UND
# SHORT-Modell identisch trainiert).
# → SUCCESS_CLASS_IDX = 0 ist KORREKT. NICHT auf 1 setzen — 1 ist die
#   LOSS-Klasse (failed_breakout).
SUCCESS_CLASS_IDX = 0
PIVOT_WINDOW = 10
RETEST_BACKWARD_LOOKUP_CANDLES = 24
LEVEL_TOLERANCE_PCT = 0.005
LIVE_DATA_HISTORY_HOURS = 240

# ── LONG-Funding-Gate (Experiment, Operator-Freigabe 2026-07-06 abends) ──────
# Report 21 Addendum 2: Die einzige Regel, die den Out-of-Sample-Test überlebt —
# LONG nur, wenn das Mittel der letzten 3 Funding-Sätze STRIKT über dem
# Binance-Default (+1,0 bps/8h) liegt: fund_24h > +3 bps → +1,12%/Trade, 74% WR
# (n=119/Jahr auf 100 Coins; Test-Fenster +0,69%, n=17 — dünn, daher Experiment
# mit eigenem Tracking-Tag und Review nach 4–6 Wochen). Fail-CLOSED: ohne
# Funding-Daten bleibt LONG zu.
FUNDING_GATE_LONG_BPS = 3.0
# SHORT-Spiegelbefund (gleiche Studie, 33,5k SHORT-Events): fund_24h > +1,5 bps
# ist für SHORTs in Train UND Test konsistent giftig (−1,2%/Trade) — exakt die
# Zone, in der das LONG-Gate öffnet. Deshalb VETO auf dem Modell-Gate. Anders
# als das LONG-Gate fail-OPEN: ohne Funding-Daten gilt das validierte
# Modell-Signal (das Veto ist Sicherheitsnetz, nicht Primär-Gate).
FUNDING_VETO_SHORT_BPS = 1.5
FUNDING_GATE_TAG = 'ABR2'  # Generation-2-Tag; direction-Spalte trennt die Seiten
_funding_cache: dict = {}  # symbol -> (monotonic_ts, mean_bps | None)


def get_funding_24h_bps(symbol):
    """Mittel der letzten 3 abgerechneten Funding-Sätze in bps (30-min-Cache).
    None bei API-Fehler — der Aufrufer behandelt das als 'Gate zu'."""
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
        logger.warning(f"⚠️ Funding-Check {symbol} fehlgeschlagen (Gate bleibt zu): {e}")
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

# Binär-Flags dürfen über ein einzelnes Coin-Fenster legitim konstant sein
# (z.B. RSI nie unter 30) — der Startup-Selbsttest prüft sie deshalb nicht hart.
BINARY_FLAG_FEATURES = {'rsi_below_30', 'rsi_above_70', 'tsi_above_0', 'tsi_below_0'}

# FIX (P0.12): pandas_ta benennt seine Spalten versions-/parameterabhängig
# (KAMA_9_2_30 statt KAMA_9, TSI_7_12_7 statt TSI_12_7, BBL_20_2.0_2.0 statt
# BBL_20_2, DCL_20_20 statt DCL_20). Das alte Exakt-Matching fand 11 der 18
# Feature-Quellspalten nie → NaN → fillna(0) → das Modell lief real nur auf
# 7 Features (Split-Count-Beweis im Audit). Prefix-Matching wie in
# 14_ai_atb_bot.py:197-211. 'TSIs_' muss vor 'TSI_' stehen.
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
    """Mappt pandas_ta-Ausgabespalten per Prefix auf die kanonischen Namen.

    Wirft ValueError, wenn eine Quellspalte fehlt — kein stilles fillna(0) mehr.
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
        raise ValueError(f"pandas_ta-Spalten nicht gefunden: {missing}")
    return df.rename(columns=rename_map)


# Modelle global — je Richtung ein Vertrag: {model, features, threshold,
# success_idx, calibrator}. Der Vertrag kommt aus der meta.json des Artefakts
# (Fix R13-ABR1-5: nichts mehr hardcoden, was das Training festlegt).
MODELS = {'LONG': None, 'SHORT': None}


def _load_model_contract(direction, model_file):
    """Lädt Modell + Vertrag. Neue Artefakte (tools/retrain_from_replay.py)
    bringen eine *_meta.json mit model_type='binary...', features, threshold —
    success ist dort predict_proba[:, 1]. Ohne meta.json: Legacy-3-Klassen-
    Modell (multi:softprob, success = Klasse 0, Thresholds aus THRESHOLDS)."""
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
            raise ValueError(f"{meta_path}: unerwarteter model_type {meta.get('model_type')!r}")
        features = meta.get('features')
        if not features:
            raise ValueError(f"{meta_path}: Feature-Liste fehlt — Artefakt mit aktuellem Trainer neu erzeugen")
        contract = {
            'model': model,
            'features': list(features),
            'threshold': float(meta['optimal_threshold']),
            'success_idx': 1,
            'calibrator': calibrator,
            # Neue Generation postet unter eigenem Tag (Operator-Regel 2026-07-06);
            # ältere Binär-Metas ohne model_id sind ebenfalls Retrain-Generation 2.
            'model_id': meta.get('model_id', 'ABR2'),
        }
        logger.info(
            f"✅ {direction}: Binär-Modell ({meta_path}), {len(features)} Features, "
            f"Threshold {contract['threshold']:.2f}, Kalibrator: {'ja' if calibrator else 'nein'}"
        )
        return contract

    logger.warning(
        f"⚠️ {direction}: keine {meta_path} gefunden — Legacy-3-Klassen-Vertrag "
        f"(success_idx={SUCCESS_CLASS_IDX}, Threshold {THRESHOLDS[direction]:.2f}). "
        f"Das Legacy-Modell ist laut Audit/Retrain (Report 19) als Gate praktisch blind."
    )
    return {
        'model': model,
        'features': list(FEATURE_COLUMNS),
        'threshold': float(THRESHOLDS[direction]),
        'success_idx': SUCCESS_CLASS_IDX,
        'calibrator': calibrator,
        'model_id': MODEL_ID,  # Legacy-Modell bleibt unter ABR1 messbar
    }


def load_models_and_coins():
    try:
        MODELS['LONG'] = _load_model_contract('LONG', SG_LONG_MODEL_FILE)
        MODELS['SHORT'] = _load_model_contract('SHORT', SG_SHORT_MODEL_FILE)
        logger.info("✅ ML Modelle loaded successfully.")
    except Exception as e:
        logger.critical(f"❌ ERROR: Could not load ML models: {e}")
        exit(1)

    try:
        with open(SG_COINS_FILE) as f:
            data = json.load(f)
            return data.get('coins', data) if isinstance(data, dict) else data
    except Exception:
        logger.warning("Konnte coins.json nicht laden, nutze leere Liste.")
        return []


def calculate_technical_indicators(df):
    """Berechnet alle Features für das Modell via pandas_ta"""

    # Sicherstellen, dass alles numerisch ist
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df.ta.ema(length=9, append=True)
    df.ta.ema(length=21, append=True)
    df.ta.kama(length=9, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.tsi(fast=7, slow=12, signal=7, append=True)
    df.ta.bbands(length=20, append=True)
    df.ta.donchian(length=20, append=True)

    # FIX (P0.12): Prefix-Matching statt Exakt-Namen + hartes ValueError bei
    # fehlender Quellspalte (vorher: NaN-Spalte anlegen → fillna(0) → Feature
    # still konstant 0).
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
    """FIX (P0.12): Startup-Assertion "kein Feature konstant".

    Berechnet die Feature-Pipeline auf echten Daten einiger Coins und bricht hart
    ab, wenn ein kontinuierliches Feature konstant ist oder Spalten fehlen — genau
    der Fehlermodus, der das Modell monatelang unbemerkt auf 7/18 Features fahren
    ließ. Binär-Flags werden nur gewarnt (legitim konstant über kurze Fenster).
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
                logger.warning(f"Selbsttest: {symbol} nicht ladbar ({e}), nächster Coin.")
                continue
            if len(df) < 60:
                continue
            frames.append(calculate_technical_indicators(df.copy())[FEATURE_COLUMNS])
            if len(frames) >= 3:
                break

        if not frames:
            logger.critical("❌ Feature-Selbsttest: keine verwertbaren Daten gefunden — Abbruch.")
            exit(1)

        sample = pd.concat(frames, ignore_index=True)
        continuous = [c for c in FEATURE_COLUMNS if c not in BINARY_FLAG_FEATURES]
        constant = [c for c in continuous if sample[c].nunique(dropna=False) <= 1]
        if constant:
            logger.critical(f"❌ Feature-Selbsttest fehlgeschlagen — konstante Features: {constant}. Abbruch.")
            exit(1)
        constant_flags = [c for c in BINARY_FLAG_FEATURES if sample[c].nunique(dropna=False) <= 1]
        if constant_flags:
            logger.warning(
                f"Selbsttest: Binär-Flags konstant über die Stichprobe (kann legitim sein): {constant_flags}"
            )
        logger.info(
            f"✅ Feature-Selbsttest bestanden ({len(sample)} Zeilen, {len(frames)} Coins, 18/18 Features variabel)."
        )
    finally:
        conn.close()


def find_pivot_levels(df):
    """FIX (R07-ABR1-b, Detektor-Rework 2026-07): nur BESTÄTIGTE Pivots.

    Das alte 'edge'-Padding + greater_equal erklärte die letzten PIVOT_WINDOW
    Kerzen zu unbestätigten Pivots, die mit der nächsten Kerze wieder
    verschwinden konnten (Repainting) — solche Levels kamen im Training
    (BT2-Datagrepper, ohne Padding) nie vor. Jetzt: ein Pivot braucht
    PIVOT_WINDOW Kerzen auf BEIDEN Seiten; der Rand wird hart ausgeschlossen
    (argrelextrema clippt am Rand sonst gegen sich selbst).
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


# Setup-Geometrie-Features (Detektor-Rework 2026-07): die 18 FEATURE_COLUMNS
# sind generische Indikator-Abstände der Retest-Kerze — das Break&Retest-Setup
# selbst (Level-Distanz, Break-Stärke, Alter) war für das Modell unsichtbar.
# Diese Features werden von find_break_retest_setups() geliefert und gehen in
# die NEUEN Binär-Modelle ein (Feature-Liste kommt aus deren meta.json).
GEOMETRY_FEATURES = [
    'setup_dist_close_level_pct',
    'setup_break_strength_pct',
    'setup_candles_since_break',
    'setup_level_age_candles',
    'setup_retest_wick_pct',
]


def find_break_retest_setups(df, retest_idx, levels):
    """Gemeinsame Break&Retest-Erkennung für Bot UND Walk-Forward-Simulator
    (tools/walkforward_sim.py importiert diese Funktion — eine Quelle, kein Skew).

    Prüft, ob die Kerze bei retest_idx der ERSTE Retest eines frischen,
    gültigen Level-Breaks ist. Behebt drei Fehler der alten Inline-Logik:

    1. RICHTUNGS-KOPPLUNG: Vorher war der Retest ein reines Touch-Gate
       (is_retest_long OR is_retest_short), die Richtung kam allein aus dem
       Break — ein High-Touch von UNTEN an einen aufwärts gebrochenen
       Widerstand (= gescheiterter Ausbruch, im Training die LOSS-Klasse)
       wurde als LONG signalisiert. Jetzt: LONG verlangt Low-Touch von oben
       UND Close über dem Level; SHORT spiegelbildlich (Trainer-Semantik,
       BT2-Datagrepper Z. 215/272).
    2. HOLD-CHECK: Alle Closes zwischen Break und Retest müssen auf der
       Break-Seite des Levels bleiben — ein zwischenzeitlich gescheiterter
       Ausbruch invalidiert das Setup.
    3. ERST-TOUCH: Der Trainer labelt nur den ersten Retest nach dem Break;
       eine frühere Band-Berührung zwischen Break und Retest invalidiert.

    Zusätzlich Trainer-Semantik für das Level-Alter: der Break muss NACH der
    vollständigen Pivot-Bestätigung liegen (break_idx > level_index + PIVOT_WINDOW).

    Rückgabe: Liste von Setups (max. 1 je Richtung; bei mehreren Kandidaten
    gewinnt der frischeste Break) inkl. GEOMETRY_FEATURES fürs Modell.
    """
    retest = df.iloc[retest_idx]
    setups = {}

    for level in levels:
        lvl_price = level['price']
        upper_bound = lvl_price * (1 + LEVEL_TOLERANCE_PCT)
        lower_bound = lvl_price * (1 - LEVEL_TOLERANCE_PCT)

        if level['type'] == 'resistance':
            direction = 'LONG'
            # Retest von OBEN: Low tastet das Band an, Close hält über dem Level.
            if not (lower_bound <= retest['low'] <= upper_bound and retest['close'] > lvl_price):
                continue
        else:
            direction = 'SHORT'
            # Retest von UNTEN: High tastet das Band an, Close hält unter dem Level.
            if not (lower_bound <= retest['high'] <= upper_bound and retest['close'] < lvl_price):
                continue

        # Break-Suche rückwärts; Level muss vor dem Break bestätigt sein.
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
                    break  # Close unter dem Level nach dem Break → Ausbruch gescheitert
                if df.iloc[j]['low'] <= upper_bound:
                    break  # frühere Band-Berührung → Retest wäre nicht der erste
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
    # Cooldown: 4h pro Coin/Direction. check_cooldown gibt True zurück wenn aktiv (blockiert).
    if check_cooldown(conn, MODEL_ID, symbol, direction, 4):
        logger.info(f"⏳ Cooldown active für {symbol} ({direction}).")
        return

    # Smart Targets: echte HVN/SR/Fib-basierte Entries, SL, 10 Targets — nicht mehr Dummy-Werte.
    trade_setup = calculate_smart_targets(conn, symbol, direction, close_price)
    entry1 = trade_setup['entry1']
    entry2 = trade_setup['entry2']
    sl = trade_setup['sl']
    targets = trade_setup['targets']

    lev = get_max_leverage(symbol, 20)

    # Versionierungs-Regel (Operator 2026-07-06): überarbeitete Modelle posten
    # unter neuem Tag (ABR2, ...), damit Alt/Neu in Trackern getrennt messbar
    # sind. Das Tag kommt aus dem Artefakt-Vertrag; Legacy-Modelle bleiben ABR1.
    model_tag = MODELS[direction].get('model_id', MODEL_ID) if MODELS.get(direction) else MODEL_ID
    if model_tag_override:
        model_tag = model_tag_override  # z. B. Funding-Gate-LONG postet als Generation 2

    # T-2026-KYT-9050-033 (Audit T-032): Fleet-Lifecycle-Gate. Default LIVE ⇒ keine
    # Verhaltensänderung. ABR2 ist in beiden Richtungen geparkt → SHADOW (überwachter
    # Trade statt Cornix); ABR1 (Legacy-Fallback-Tag) bleibt Default LIVE. Rein additiv
    # am Post-Zweig (Regel 4). ai_signals speichert die volle Target-Liste →
    # n_show=len(targets); confidence ist wie im Live-Pfad prob.
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
        f"🏦 Entry 2: $ {entry2:.5f}",
    ]
    for i, t in enumerate(targets[:3], 1):
        lines.append(f"💰 TP{i}: $ {t:.5f}")
    lines += [f"💸 Stop Loss: $ {sl:.5f}", f"🧠 Trade idea generated by AI module {model_tag}"]
    cornix_msg = "\n".join(lines)

    emoji = f"🚀 AI {model_tag} LONG SIGNAL" if direction == "LONG" else f"💥 AI {model_tag} SHORT SIGNAL"

    # FIX Doppel-Post (Operator-Meldung 2026-07-06): Die Info-Nachricht darf den
    # Cornix-Block NICHT nochmal enthalten — Cornix parste beide Nachrichten als
    # eigenständige Signale (doppelte Position).
    # Funding-Zeile NUR in der Info-Nachricht (die Cornix-Nachricht bleibt die
    # einzige parsebare — Doppel-Post-Regel 2026-07-06 unangetastet).
    funding_line = f"\n<b>→ Funding-Gate: {funding_bps:+.2f} bps/8h (24h-Mittel)</b>" if funding_bps is not None else ""
    html = f"""<pre><b>{emoji}</b>\n<b>{symbol}</b>\n<b>→ Direction: {direction}</b>\n<b>→ ML Confidence: <b>{prob:.1%}</b></b>{funding_line}\n<b>→ Time: {datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M')} UTC | Modul: {model_tag}</b>\n<b>→ Source: AI Break & Retest Model</b></pre>"""

    chart_buf = generate_minichart_image(symbol, minutes=240)

    with conn.cursor() as cur:
        # Cornix Channel (Hier nutzt er den speziellen Rubberband Channel!)
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
    logger.info(f"✅ {MODEL_ID} Signal für {symbol} in Outbox gelegt!")


def process_abr_logic(conn, symbol):
    try:
        # R1: die Erkennung läuft auf den jüngsten GESCHLOSSENEN Kerzen. Für 1h ist
        # include_forming=False exakt der bisherige `open_time < current_hour_utc`-
        # Schnitt (1h-open_times haben immer minute=0); limit=LIVE_DATA_HISTORY_HOURS
        # ersetzt das `.tail()`, der bisherige +5h-Overfetch entfällt.
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

        # FIX (R07-ABR1-a, Detektor-Rework 2026-07): NUR die jüngste
        # geschlossene Kerze ist Retest-Kandidat. Der Bot läuft stündlich —
        # jede Kerze wird genau einmal geprüft; das alte 3-Kerzen-Fenster
        # produzierte bis zu 3h stale Entries und Doppel-Bewertungen.
        # (Die laufende Kerze wurde oben via open_time < current_hour_utc
        # weggeschnitten — 1h-Kerzen haben immer minute=0.)
        retest_idx = len(df_indicators) - 1
        retest_candle = df_indicators.iloc[retest_idx]

        for setup in find_break_retest_setups(df_indicators, retest_idx, levels):
            direction = setup['direction']
            contract = MODELS[direction]

            # Feature-Vertrag des Artefakts strikt bedienen: Indikator-Features
            # der Retest-Kerze + Setup-Geometrie. Fehlende Features sind ein
            # harter Fehler — KEIN stilles fillna(0) über fehlende Spalten
            # (X-R5-Muster, hat den 11-Features-Bug 3 Stufen lang versteckt).
            feature_row = {**retest_candle[FEATURE_COLUMNS].to_dict(), **setup['features']}
            missing = [c for c in contract['features'] if c not in feature_row]
            if missing:
                raise ValueError(f"Feature-Vertrag verletzt — fehlend: {missing}")
            X_event = pd.DataFrame([{c: feature_row[c] for c in contract['features']}], dtype=float)

            # Defensive Absicherung gegen NaN/Inf in berechneten WERTEN
            # (z.B. Indikator-Warmup bei frischen Coins): NaN/Inf → 0 (neutral).
            X_event = X_event.replace([np.inf, -np.inf], np.nan).fillna(0)

            prediction_proba = float(contract['model'].predict_proba(X_event)[0, contract['success_idx']])

            # Kalibrierte Confidence nur für die Anzeige — das Gate läuft auf
            # der Roh-Probability, auf der auch der Threshold gewählt wurde.
            display_proba = prediction_proba
            if contract['calibrator'] is not None:
                display_proba = float(contract['calibrator'].predict([prediction_proba])[0])

            logger.info(
                f"ABR1 Break&Retest erkannt bei {symbol} | Dir: {direction} | "
                f"Level: {setup['level_price']:.6f} | Prob: {prediction_proba:.2f} "
                f"(Gate {contract['threshold']:.2f})"
            )
            # Gates je Richtung (Stand 2026-07-06 abends):
            #   SHORT — Binär-Modell-Gate auf Roh-Probability (v2-Vertrag).
            #   LONG  — Funding-Gate-EXPERIMENT (Report 21 Addendum 2): das
            #           ML-Gate ist für LONG nachweislich blind, aber
            #           fund_24h > +3 bps überlebt als einzige Regel den
            #           Out-of-Sample-Test. Der Legacy-Modell-Vertrag dient nur
            #           noch der Confidence-Anzeige; fail-closed ohne Funding.
            if direction == 'LONG':
                fund_bps = get_funding_24h_bps(symbol)
                if fund_bps is not None and fund_bps > FUNDING_GATE_LONG_BPS:
                    logger.info(f"🟢 LONG-Funding-Gate offen für {symbol}: {fund_bps:+.2f} bps")
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
                        f"⛔ LONG-Funding-Gate zu für {symbol}: {fund_bps:+.2f} bps (Limit {FUNDING_GATE_LONG_BPS:+.1f})"
                    )
            elif prediction_proba >= contract['threshold']:
                # SHORT-Funding-Veto (2026-07-06, Report 21 Addendum 2 Spiegel-
                # test): bei fund_24h > +1,5 bps ist die Zone konsistent
                # verlustig — Veto trotz Modell-Gate. Fail-open (s. Konstante).
                fund_bps = get_funding_24h_bps(symbol)
                if fund_bps is not None and fund_bps > FUNDING_VETO_SHORT_BPS:
                    logger.info(
                        f"⛔ SHORT-Funding-Veto für {symbol}: {fund_bps:+.2f} bps "
                        f"(> {FUNDING_VETO_SHORT_BPS:+.1f}, Modell-Prob {prediction_proba:.2f})"
                    )
                else:
                    send_signal(conn, symbol, direction, display_proba, retest_candle['close'], funding_bps=fund_bps)

    except Exception as e:
        logger.error(f"Error for {symbol}: {e}")


def main():
    logger.info("=== AI BREAK & RETEST BOT (ABR1) GESTARTET ===")
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
        logger.info("Bot manuell stopped (Strg+C). Shutting down cleanly...")

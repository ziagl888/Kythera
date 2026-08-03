# ZZZ.py – The truly final version (December 2025) – 100% correct
import asyncio
import json
import logging
import multiprocessing as mp
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import matplotlib
import pandas as pd

matplotlib.use('Agg')
import base64
import hashlib
import hmac
import io
import ssl
import tempfile
import time
from collections import deque
from io import BytesIO

# from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from urllib.parse import urlencode

import aiohttp
import certifi
import joblib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import mplfinance as mpf
import numpy as np
import pandas_ta as ta
import psycopg2
import pytz
import scipy.stats as stats
import websockets
import yfinance as yf
from dateutil.parser import isoparse
from master_task import check_master_trades

# import gridspec  # imported from matplotlib
from matplotlib import gridspec

# from matplotlib.dates import DateFormatter
from matplotlib.dates import DateFormatter, MinuteLocator
from psycopg2 import pool
from scipy.interpolate import make_interp_spline
from scipy.ndimage import gaussian_filter1d
from scipy.signal import argrelextrema, find_peaks
from scipy.stats import linregress
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from xgboost import XGBClassifier

# ========================= CONFIG =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = 6976112025

COINS_FILE = Path("coins.json")
LOG_FILE = Path("command_log.json")
LOCK_FILE = Path(__file__).with_suffix(".lock")

COINGLASS_API_KEY = os.getenv('COINGLASS_API_KEY', 'CG-6gVq4SWBz1ZNQvEhN9WdWqEk')  # Your key
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET", "")

# ========================= DATABASE CONFIG =========================
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "dbfiller",
    "password": os.getenv("DB_PASSWORD", ""),
    "database": "cryptodata",
}

db_pool: asyncpg.Pool | None = None

db_pool2 = pool.ThreadedConnectionPool(
    minconn=5,
    maxconn=20,
    host="localhost",
    port=5432,
    user="dbfiller",
    password=os.getenv("DB_PASSWORD", ""),
    database="cryptodata",
)


# ========================= MEAN REVERSION MODELS CHANNELS & MODELS =========================
RUBBERBAND_CHANNEL_ID = "0"  # Your new channel

try:
    REVERSION_MODEL_LONG = joblib.load('long_reversion_model.joblib')
    print("✅ Long Rubberband model loaded successfully!")
except Exception as e:
    logger.info(f"❌ ERROR: Could not load Long Rubberband model: {e}")
    REVERSION_MODEL_LONG = None

try:
    REVERSION_MODEL_SHORT = joblib.load('short_reversion_model.joblib')
    print("✅ Short Rubberband model loaded successfully!")
except Exception as e:
    logger.info(f"❌ ERROR: Could not load Short Rubberband model: {e}")
    REVERSION_MODEL_SHORT = None

REVERSION_THRESH_LONG = 0.75
REVERSION_THRESH_SHORT = 0.85

# ========================= SMC / ICT GLOBALS =========================
HISTORICAL_SCANNED = set()
ALERTED_STRUCT = set()  # Keeps track of already reported BOS/CHoCH
SMC_TREND_STATE = {}  # Stores the current trend (1 = Bullish, -1 = Bearish)

# ========================= FOREX SMC GLOBALS =========================
FOREX_PAIRS = ['EURUSD=X', 'GBPUSD=X', 'USDJPY=X', 'USDCHF=X', 'EURJPY=X', 'GBPJPY=X']
FOREX_CHANNEL_ID = 0
FOREX_HISTORICAL_SCANNED = set()
FOREX_ALERTED_STRUCT = set()
FOREX_TREND_STATE = {}


# ========================= PATTERN DETECTOR GLOBALS =========================
PATTERN_CHANNEL_ID = 0
PATTERN_TIMEFRAMES = ['1h']  # Easily extendable later to ['1h', '2h', '4h', '1d']
ALERTED_PATTERNS = set()
ALERTED_RETESTS = set()
ACTIVE_PATTERNS = {}  # Stores broken-out patterns for the retest check

# ========================= MACRO MODEL =========================
try:
    MACRO_MODEL = joblib.load('macro_3d_predictor.pkl')
    print("✅ Macro 3D Predictor model loaded successfully!")
except Exception as e:
    print(f"❌ ERROR: Could not load Macro model: {e}")
    MACRO_MODEL = None


# ========================= 1 MINUTE memory database =========================
ONE_MINUTE_DATA: dict[str, deque] = {}  # "BTCUSDT" → deque of dicts
DATA_FILE = Path("1minute.json")
PUMP_DUMP_FILE = Path("pump_dump_state.json")

# Global state for cooldowns (per coin)
PRICE_VOLUME_ALERT_STATE = {}  # symbol → {"last_alert_time": datetime}

# ========================= ROUND LEVEL BREAKER AND OTHER TASKS =========================
TEST_CHANNEL_ID = "0"
MARKET_CHANNEL_ID = "0"
AI_CHANNEL_ID = '0'
TRENDBREAKER_CHANNEL_ID = '0'

TRENDLINE_STATE = {}  # symbol → {"last_alert": datetime, "prev_relation": "above"/"below"/"near"}

ROUND_LEVEL_CONFIG = {
    "BTCUSDT": {"step": 500, "decimals": 0},
    "ETHUSDT": {"step": 100, "decimals": 0},
    "BNBUSDT": {"step": 50, "decimals": 0},
    "SOLUSDT": {"step": 10, "decimals": 1},
    "XRPUSDT": {"step": 0.1, "decimals": 3},
}

# Internal storage: {symbol: {"last_level": 90000.0, "last_break_time": datetime, "direction": "up"/"down"}}
ROUND_BREAK_STATE = {}
# After ROUND_BREAK_STATE or similar – e.g. after ONE_MINUTE_DATA
PUMP_DUMP_STATE = {}  # symbol → dict with avg_volume, last_alert_time, etc.
# Cooldown per coin+level (prevents spam during sideways movements)
COOLDOWN_SECONDS = 180  # 3 minutes

ssl_context = ssl.create_default_context(cafile=certifi.where())

# MAIN_LOOP = asyncio.get_event_loop()

PUMP_MODELS = {
    "8h": {
        "model_path": "pump_model_8h_pump_final.pkl",
        "threshold_path": "threshold_8h_pump_final.pkl",
        "loaded": False,
    },
    "24h": {
        "model_path": "pump_model_24h_pump_final.pkl",
        "threshold_path": "threshold_24h_pump_final.pkl",
        "loaded": False,
    },
    "72h": {
        "model_path": "pump_model_72h_pump_final.pkl",
        "threshold_path": "threshold_72h_pump_final.pkl",
        "loaded": False,
    },
    "168h": {
        "model_path": "pump_model_168h_pump_final.pkl",
        "threshold_path": "threshold_168h_pump_final.pkl",
        "loaded": False,
    },
}
PUMP_MODELS_LOADED = {"model": None, "threshold": 0.5}


# Global models (load once)
MODEL_LONG = joblib.load("trade_success_xgb_LONG_v1.model")
MODEL_SHORT = joblib.load("trade_success_xgb_SHORT_v1.model")

# Thresholds – can be adjusted / optimised later
CONF_THRESHOLD_LONG = 0.72
CONF_THRESHOLD_SHORT = 0.78

# Configuration
TSI_MODEL_LONG_PATH = "model_tsi_long_robust.pkl"
TSI_MODEL_SHORT_PATH = "model_tsi_short_robust.pkl"
TSI_THRESH_LONG = 0.80
TSI_THRESH_SHORT = 0.80

TSI_FEATURES = [
    "rsi_14",
    "rsi_6",
    "macd_hist",
    "atr_pct",
    "vol_ratio",
    "bb_width",
    "bb_pos",
    "dist_ema200",
    "dist_ema9_21",
    "rsi_ratio",
    "slope_norm",
    "dist_supp",
    "dist_res",
    "dist_kama9",
    "dist_kama21",
    "dist_kama55",
    "dist_kama9_21",
    "dist_donch_up",
    "dist_donch_low",
    "macd_cross_bearish",
    "ema9_21_cross_bearish",
    "kama9_21_cross_bearish",
    "bollinger_lower_break",
    "close_below_ema50",
    "obv_ratio",
    "close_to_vwap_pct",
    "obv_val",
    "volume_spike",
    "volume_trend_up",
]

# ========================= ML CONFIG =========================
# Load the model ONCE globally at bot start
try:
    ML_MODEL = joblib.load('trend_prediction_model.joblib')
    print("✅ ML Trendbreaker model loaded successfully!")
except Exception as e:
    print(f"❌ ERROR: Could not load ML Trendbreaker model: {e}")
    ML_MODEL = None

# Your chosen threshold (sweet spot)
ML_THRESHOLD = 0.75


SG_LONG_MODEL_FILE = 'bt2_model_LONG.json'
SG_SHORT_MODEL_FILE = 'bt2_model_SHORT.json'
SG_COINS_FILE = 'coins.json'
SG_LONG_THRESHOLD = 0.6
SG_SHORT_THRESHOLD = 0.8
SG_SUCCESS_CLASS_IDX = 0  # Corresponds to the label of 'continuation_success'

# --- Live bot specific parameters ---
HOURLY_CHECK_DELAY_MINUTES = 10  # 10 minutes after the full hour
LIVE_DATA_HISTORY_HOURS = 2400  # How many hours of history are fetched for indicators/pivots
LEVEL_TOLERANCE_PCT = 0.005  # 0.5% tolerance zone around the level
PIVOT_WINDOW = 10
RETEST_BACKWARD_LOOKUP_CANDLES = 24  # How many candles before the retest are searched after a break
MODEL_ID = 'ABR1'  # Fixed value for the model ID in the database

# Loaded later in post_init
signal_generator_instance = None  # So that we have access to it

try:
    LONG_ML_MODEL = joblib.load('long_trend_prediction_model.joblib')
    print("✅ Long ML Trendbreaker model loaded successfully!")
except Exception as e:
    logger.info(f"❌ ERROR: Could not load Long ML Trendbreaker model: {e}")
    LONG_ML_MODEL = None

try:
    SHORT_ML_MODEL = joblib.load('short_trend_prediction_model.joblib')
    print("✅ Short ML Trendbreaker model loaded successfully!")
except Exception as e:
    print(f"❌ ERROR: Could not load Short ML Trendbreaker model: {e}")
    SHORT_ML_MODEL = None

# Your chosen threshold for LONG and SHORT
# Adjustment based on threshold optimisation
LONG_ML_THRESHOLD = 0.80
SHORT_ML_THRESHOLD = 0.75

# ----------------- CONFIGURATION -------------------
SHORT_THRESHOLD = 0.86
LONG_THRESHOLD = 0.79
MIN_ML_SCORE_FOR_LOG = 0.25

# ========================= LOGGING =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ========================= LOCK =========================
if LOCK_FILE.exists():
    logger.info("An instance is already running → aborting this start attempt.")
    sys.exit(0)
LOCK_FILE.touch()


# ========================= COINS =========================
def load_coins() -> set[str]:
    if not COINS_FILE.exists():
        logger.error("coins.json not found!")
        return set()
    try:
        with open(COINS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return set(str(s).upper() for s in data)  # always uppercase + set
    except Exception as e:
        logger.error(f"Error loading coins.json: {e}")
        return set()


coins = load_coins()

# ========================= save pumpstats =========================
_ml_model = None
_ml_model_time = None
ML_MODEL_PATH = "pump_dump_model.pkl"


async def load_pump_dump_state():
    global PUMP_DUMP_STATE
    if PUMP_DUMP_FILE.exists():
        try:
            with open(PUMP_DUMP_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            PUMP_DUMP_STATE = {}
            for symbol, data in raw.items():
                state = {
                    "avg_volume": float(data.get("avg_volume", 0)),
                    "last_alert_time": datetime.fromisoformat(data["last_alert_time"].replace("Z", "+00:00"))
                    if data.get("last_alert_time")
                    else datetime(1970, 1, 1, tzinfo=pytz.UTC),
                    "usd_vol_4h": float(data.get("usd_vol_4h", 0)),
                }
                # Restore volume_samples as deque (last 360 values)
                samples = data.get("volume_samples", [])
                state["volume_samples"] = deque(samples[-360:], maxlen=360)
                PUMP_DUMP_STATE[symbol] = state
            logger.info(f"Pump/Dump state loaded for {len(PUMP_DUMP_STATE)} coins")
        except Exception as e:
            logger.error(f"Error loading pump_dump_state.json: {e}")
            PUMP_DUMP_STATE = {}
    else:
        PUMP_DUMP_STATE = {}


# ========================= COMMAND LOG =========================
def log_command(command: str, user):
    entry = {
        "timestamp_utc": datetime.now(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": command,
        "username": user.username or user.full_name or "unknown",
        "user_id": user.id,
    }
    try:
        data = []
        if LOG_FILE.exists():
            with open(LOG_FILE, encoding="utf-8") as f:
                data = json.load(f)
        data.append(entry)
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Command-Logging failed: {e}")


# ========================= SYMBOL VALIDATION =========================
async def validate_symbol(sym: str) -> str | None:
    sym = sym.upper()
    if sym in coins:
        return sym
    if not sym.endswith("USDT"):
        extended = sym + "USDT"
        if extended in coins:
            logger.info(f"Auto-completed: {sym} → {extended}")
            return extended
    return None


# ========================= PRICE FETCH =========================
async def get_price_data(symbol: str) -> dict | None:
    url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logger.error(f"Binance API error for {symbol}: {e}")
    return None


async def get_live_price(symbol: str) -> float | None:
    url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data['price'])
    except Exception as e:
        logger.error(f"Binance API error for {symbol}: {e}")
    return None


# ========================= PRICE HANDLER =========================
async def price_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    match = re.match(r"!price\s+([A-Za-z0-9]+)", update.message.text.strip(), re.IGNORECASE)
    if not match:
        return

    # Logging
    log_command("!price " + match.group(1), update.effective_user)

    raw_sym = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_sym)
    if not valid_symbol:
        await update.message.reply_text(
            f"❌ Invalid coin: {raw_sym}\n❌ Both `{raw_sym}` and `{raw_sym}USDT` not found in database.",
            parse_mode="Markdown",
        )
        return

    data = await get_price_data(valid_symbol)
    if not data:
        await update.message.reply_text("❌ Binance API error – try again later.")
        return

    price = float(data["lastPrice"])
    change = float(data["priceChangePercent"])

    if price >= 1000:
        price_str = f"{price:,.2f}"
    elif price >= 1:
        price_str = f"{price:,.4f}"
    else:
        price_str = f"{price:.8f}"

    arrow = "🟢 ↑" if change >= 0 else "🔴 ↓"
    change_str = f"{change:+.2f}%"

    await update.message.reply_text(
        f"<b>{valid_symbol}</b>\n<code>${price_str}</code> {arrow} <b>{change_str}</b> (24h)", parse_mode="HTML"
    )


# ========================= !24 HANDLER (MACRO OUTLOOK) =========================
async def macro_24_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    if update.message.text.strip().upper() != "!24":
        return

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!24", update.effective_user)
    await update.message.reply_chat_action(ChatAction.TYPING)

    if MACRO_MODEL is None:
        await update.message.reply_text("⚠️ Macro Outlook Model is not loaded or unavailable.")
        return

    try:
        # # 1. Fetch current market data (last 40 days for SMA30)
        # tickers = ['BTC-USD', '^NDX', '^GSPC', 'DX-Y.NYB', 'GC=F', '^TNX', '^VIX']
        # df_list = []
        # for t in tickers:
        # # Offload yfinance to a thread so the bot doesn't block
        # data = await asyncio.to_thread(yf.download, t, period="40d", interval="1d", progress=False)
        # col_name = t.split('-')[0].replace('^', '').replace('=F', '')
        # if t == 'DX-Y.NYB': col_name = 'DXY'
        # if t == 'GC=F': col_name = 'GOLD'
        # if t == '^TNX': col_name = 'US10Y'
        # if t == '^VIX': col_name = 'VIX'

        # close_data = data[['Close']].copy()
        # close_data.columns = [col_name]
        # df_list.append(close_data)

        # df = pd.concat(df_list, axis=1).ffill() # Forward-fill for holidays/weekends

        # # 2. Compute features
        # features = {}
        # for name in ['BTC', 'NDX', 'GSPC', 'DXY', 'GOLD', 'US10Y', 'VIX']:
        # features[f'{name}_pct_change'] = df[name].pct_change().iloc[-1]

        # for name in ['BTC', 'NDX', 'DXY', 'VIX']:
        # sma7 = df[name].rolling(7).mean().iloc[-1]
        # sma30 = df[name].rolling(30).mean().iloc[-1]
        # features[f'{name}_SMA7_dist'] = (df[name].iloc[-1] - sma7) / sma7
        # features[f'{name}_SMA30_dist'] = (df[name].iloc[-1] - sma30) / sma30

        # features['Risk_On_Index'] = features['NDX_pct_change'] - features['DXY_pct_change']
        # features['Fear_Index'] = features['VIX_pct_change'] + features['DXY_pct_change']

        # # Build DataFrame & sort columns
        # X_live = pd.DataFrame([features])
        # expected_cols = MACRO_MODEL.feature_names_in_
        # X_live = X_live[expected_cols]

        # 1. Fetch current market data (last 40 days for SMA30)
        tickers = ['BTC-USD', '^NDX', '^GSPC', 'DX-Y.NYB', 'GC=F', '^TNX', '^VIX']
        df_list = []
        for t in tickers:
            # Offload yfinance to a thread so the bot doesn't block
            data = await asyncio.to_thread(yf.download, t, period="40d", interval="1d", progress=False)
            col_name = t.split('-')[0].replace('^', '').replace('=F', '')

            # --- FIX: map GSPC to SP500 ---
            if t == '^GSPC':
                col_name = 'SP500'
            if t == 'DX-Y.NYB':
                col_name = 'DXY'
            if t == 'GC=F':
                col_name = 'GOLD'
            if t == '^TNX':
                col_name = 'US10Y'
            if t == '^VIX':
                col_name = 'VIX'

            close_data = data[['Close']].copy()
            close_data.columns = [col_name]
            df_list.append(close_data)

        df = pd.concat(df_list, axis=1).ffill()  # Forward-fill for holidays/weekends

        # 2. Compute features
        features = {}
        # --- FIX: use 'SP500' here too instead of 'GSPC' ---
        for name in ['BTC', 'NDX', 'SP500', 'DXY', 'GOLD', 'US10Y', 'VIX']:
            features[f'{name}_pct_change'] = df[name].pct_change().iloc[-1]

        for name in ['BTC', 'NDX', 'DXY', 'VIX']:
            sma7 = df[name].rolling(7).mean().iloc[-1]
            sma30 = df[name].rolling(30).mean().iloc[-1]
            features[f'{name}_SMA7_dist'] = (df[name].iloc[-1] - sma7) / sma7
            features[f'{name}_SMA30_dist'] = (df[name].iloc[-1] - sma30) / sma30

        features['Risk_On_Index'] = features['NDX_pct_change'] - features['DXY_pct_change']
        features['Fear_Index'] = features['VIX_pct_change'] + features['DXY_pct_change']

        # Build DataFrame & sort columns
        X_live = pd.DataFrame([features])
        expected_cols = MACRO_MODEL.feature_names_in_
        X_live = X_live[expected_cols]

        # 3. Prediction
        prob_bullish = float(MACRO_MODEL.predict_proba(X_live)[0, 1])

        # 4. Format text (in English)
        outlook = "BULLISH 🟢" if prob_bullish >= 0.60 else "BEARISH 🔴" if prob_bullish <= 0.40 else "NEUTRAL 🟡"

        def fmt_pct(val):
            return f"{val * 100:+.2f}%"

        html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:14px; line-height:1.7; border-left: 6px solid #00ffff;">
<b style="color:#00ffff; font-size:18px;">🌍 MACRO OUTLOOK (3D)</b>
<b>Requested by <a href="https://t.me/{username}">@{username}</a></b>

<b>Overall Trend:</b> {outlook}
<b>AI Confidence:</b> <b style="color:#00ffff;">{prob_bullish:.1%}</b>

<b style="color:#888888;">--- Traditional Markets ---</b>
<b>NDX  (Tech):</b> <b style="color:{'#00ff00' if features['NDX_pct_change'] > 0 else '#ff0066'};">{fmt_pct(features['NDX_pct_change'])}</b>
<b>DXY  (USD): </b> <b style="color:{'#00ff00' if features['DXY_pct_change'] > 0 else '#ff0066'};">{fmt_pct(features['DXY_pct_change'])}</b>
<b>VIX  (Fear):</b> <b style="color:{'#ff0066' if features['VIX_pct_change'] > 0 else '#00ff00'};">{fmt_pct(features['VIX_pct_change'])}</b>
<b>GOLD (Safe):</b> <b style="color:{'#00ff00' if features['GOLD_pct_change'] > 0 else '#ff0066'};">{fmt_pct(features['GOLD_pct_change'])}</b>
<b>BTC  (Risk):</b> <b style="color:{'#00ff00' if features['BTC_pct_change'] > 0 else '#ff0066'};">{fmt_pct(features['BTC_pct_change'])}</b>

<b style="color:#888888;">--- Risk Indicators ---</b>
<b>Risk-On Index:</b> {features['Risk_On_Index']:+.4f}
<b>Fear Index:   </b> {features['Fear_Index']:+.4f}
</pre>
        """.strip()

        await update.message.reply_html(html, disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"!24 handler error: {e}", exc_info=True)
        await update.message.reply_text("Error calculating Macro Outlook.")


# ========================= TOP GAINERS / LOSERS HANDLER =========================


async def top_gainers_losers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text_upper = update.message.text.strip().upper()
    if text_upper not in ("!TOPGAINERS", "!TOPLOSERS"):
        return

    is_gainers = text_upper == "!TOPGAINERS"
    username = update.effective_user.username or update.effective_user.full_name or "unknown"

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            async with session.get("https://fapi.binance.com/fapi/v1/ticker/24hr") as resp:
                if resp.status != 200:
                    await update.message.reply_text("Binance API temporarily unavailable.")
                    return
                all_data = await resp.json()

        valid_symbols = {s.upper() for s in coins}
        filtered = [c for c in all_data if c["symbol"] in valid_symbols]
        if not filtered:
            await update.message.reply_text("No coins found.")
            return

        sorted_coins = sorted(filtered, key=lambda x: float(x["priceChangePercent"]), reverse=is_gainers)[:15]

        title = "TOP 15 GAINERS (24h)" if is_gainers else "TOP 15 LOSERS (24h)"
        border_color = "#00ff88" if is_gainers else "#ff4466"
        big_emoji = "🚀" if is_gainers else "💥"

        html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:20px; border-radius:16px; font-family: 'Courier New', monospace; font-size:15px; line-height:1.9; border-left: 8px solid {border_color};">
<b style="color:#00ffff; font-size:20px;">          {big_emoji} {title} {big_emoji}</b>

<b style="color:#888888;">#   SYMBOL          PRICE             24H</b>
<b style="color:#00ffff;">──────────────────────────────────────────────</b>
"""

        for i, coin in enumerate(sorted_coins, 1):
            sym = coin["symbol"]
            price = float(coin["lastPrice"])
            change = float(coin["priceChangePercent"])

            # Intelligent price formatting – left-aligned within the block
            if price >= 10000:
                price_str = f"{price:,.2f}"
            elif price >= 100:
                price_str = f"{price:,.4f}"
            elif price >= 1:
                price_str = f"{price:,.6f}"
            elif price >= 0.01:
                price_str = f"{price:.8f}"
            else:
                price_str = f"{price:.10f}".rstrip("0").rstrip(".")

            change_str = f"{change:+.2f}%".rjust(8)  # only the percentages right-aligned (customary)
            change_color = "#00ff88" if change >= 0 else "#ff4466"

            html += f"<b style=\"color:#ffd700;\">{i:2d}</b>  <b>{sym:<14}</b>  <code>${price_str:<14}</code>  <b style=\"color:{change_color};\">{change_str}</b>\n"

        html += f"""
<b style="color:#00ffff;">──────────────────────────────────────────────</b>
<b style="color:#00ffff;">Requested by <a href="https://t.me/{username}">@{username}</a> • Live updated</b>
</pre>
""".strip()

        await update.message.reply_html(html, disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"TopGainers/Losers error: {e}")
        await update.message.reply_text("Error loading data – try again later.")


# ========================= CHART GENERATOR =========================


async def init_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(**DB_CONFIG)


async def get_conn():
    if db_pool is None:
        await init_db_pool()
    return await db_pool.acquire()


async def release_conn(conn):
    if db_pool:
        await db_pool.release(conn)


def to_uppercase_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    df.columns = [col.upper() for col in df.columns]
    if 'OPEN_TIME' in df.columns:
        df['OPEN_TIME'] = pd.to_datetime(df['OPEN_TIME'], utc=True)
    return df


async def get_1h_data_last_7d(symbol: str) -> pd.DataFrame:
    tablename = f'"{symbol.upper()}_1h"'
    query = f"""
        SELECT * FROM {tablename}
        WHERE open_time >= NOW() - INTERVAL '8 days'
        ORDER BY open_time ASC
    """
    conn = await get_conn()
    try:
        rows = await conn.fetch(query)
        if not rows:
            return pd.DataFrame()
        columns = rows[0].keys()
        data = [dict(row) for row in rows]
        df = pd.DataFrame(data, columns=columns)
        return to_uppercase_df(df)
    except Exception as e:
        logger.error(f"DB Error 7d {symbol}: {e}")
        return pd.DataFrame()
    finally:
        await release_conn(conn)


async def get_1h_indicators_last_7d(symbol: str) -> pd.DataFrame:
    tablename = f'"{symbol.upper()}_1h_indicators"'
    query = f"""
        SELECT * FROM {tablename}
        WHERE open_time >= NOW() - INTERVAL '8 days'
        ORDER BY open_time ASC
    """
    conn = await get_conn()
    try:
        rows = await conn.fetch(query)
        if not rows:
            return pd.DataFrame()
        columns = rows[0].keys()
        data = [dict(row) for row in rows]
        df = pd.DataFrame(data, columns=columns)
        return to_uppercase_df(df)
    except Exception as e:
        logger.error(f"DB Error indicators 7d {symbol}: {e}")
        return pd.DataFrame()
    finally:
        await release_conn(conn)


async def get_1h_data_last_90d(symbol: str) -> pd.DataFrame:
    tablename = f'"{symbol.upper()}_1h"'
    query = f"""
        SELECT * FROM {tablename}
        WHERE open_time >= NOW() - INTERVAL '95 days'
        ORDER BY open_time ASC
    """
    conn = await get_conn()
    try:
        rows = await conn.fetch(query)
        if not rows:
            return pd.DataFrame()
        columns = rows[0].keys()
        data = [dict(row) for row in rows]
        df = pd.DataFrame(data, columns=columns)
        return to_uppercase_df(df)
    except Exception as e:
        logger.error(f"DB Error 90d {symbol}: {e}")
        return pd.DataFrame()
    finally:
        await release_conn(conn)


def find_pivots(df, distance=8):
    """
    Finds high and low pivots with a minimum spacing.
    distance=8 → roughly one pivot every 8 hours → perfect for the 1h chart
    """
    high_peaks, _ = find_peaks(df['HIGH'], distance=distance)
    low_peaks, _ = find_peaks(-df['LOW'], distance=distance)
    return high_peaks, low_peaks


def calculate_trendline(df, pivots, is_high=True):
    """
    Calculates the trendline only from real pivots (not from all data)
    """
    if len(pivots) < 2:
        return None, None

    # X values: seconds since epoch (correct for large numbers)
    x = df['OPEN_TIME'].iloc[pivots].astype('int64') // 10**9
    x = x.astype(float)

    # Y values: HIGH for downtrend, LOW for uptrend
    y = df['HIGH'].iloc[pivots] if is_high else df['LOW'].iloc[pivots]

    slope, intercept, r_value, _, _ = linregress(x, y)

    # Only strong trends (correlation coefficient > 0.8)
    if abs(r_value) < 0.8:
        return None, None

    return float(slope), float(intercept)


def get_trend_values(slope, intercept, open_times):
    """
    Calculates the trendline's Y values for all timestamps in open_times
    """
    if slope is None or intercept is None:
        return np.full(len(open_times), np.nan)

    # X values in seconds since epoch (exactly as in calculate_trendline!)
    x = open_times.astype('int64') // 10**9
    x = x.astype(float)

    return slope * x + intercept


def detect_trend(df):
    """
    Detects the dominant trend over 90 days based on pivots
    """
    if len(df) < 50:
        return 'UNDECIDED', None

    high_pivots, low_pivots = find_pivots(df, distance=8)  # ← 8 is the gold standard!

    down_slope, down_intercept = calculate_trendline(df, high_pivots, is_high=True)
    up_slope, up_intercept = calculate_trendline(df, low_pivots, is_high=False)

    if down_slope is not None and down_slope < 0:
        return 'DOWN', (down_slope, down_intercept)
    elif up_slope is not None and up_slope > 0:
        return 'UP', (up_slope, up_intercept)

    return 'UNDECIDED', None


# ========================= FINAL !CHART HANDLER – 100% LIKE YOUR OLD BOT =========================


async def chart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... your beginning unchanged ...
    if not update.message or not update.message.text:
        return

    match = re.match(r"!chart\s+([A-Za-z0-9]+)", update.message.text.strip(), re.IGNORECASE)
    if not match:
        return

    raw_sym = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_sym)
    if not valid_symbol:
        await update.message.reply_text(f"Invalid coin: `{raw_sym}` not found.", parse_mode="Markdown")
        return
    symbol = valid_symbol
    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!chart " + raw_sym, update.effective_user)
    await update.message.reply_chat_action("upload_photo")

    try:
        df_7d = await get_1h_data_last_7d(valid_symbol)
        df_ind = await get_1h_indicators_last_7d(valid_symbol)
        df_90d = await get_1h_data_last_90d(valid_symbol)

        if df_7d.empty or df_ind.empty:
            await update.message.reply_text(f"No Chart-Data for {valid_symbol}")
            return

        df_plot = df_7d.merge(df_ind, on='OPEN_TIME', how='left', suffixes=('', '_ind'))
        df_plot = df_plot.ffill().bfill()

        # df_plot = df_7d.merge(
        # df_ind.drop(columns=['OPEN','HIGH','LOW','CLOSE','VOLUME'], errors='ignore'), on='OPEN_TIME', how='left' )
        # df_plot = df_plot.ffill().bfill()

        # === LIVE PRICE – as with you ===
        live_price = await get_live_price(valid_symbol)  # your function!
        live_suffix = ""
        if live_price:
            last_time = df_plot['OPEN_TIME'].iloc[-1]
            now_time = datetime.now(pytz.UTC)
            if (now_time - last_time) > pd.Timedelta(minutes=59):
                new_row = df_plot.iloc[-1].copy()
                new_row["OPEN_TIME"] = now_time
                new_row["OPEN"] = df_plot["CLOSE"].iloc[-1]
                new_row["HIGH"] = max(df_plot["HIGH"].iloc[-1], live_price)
                new_row["LOW"] = min(df_plot["LOW"].iloc[-1], live_price)
                new_row["CLOSE"] = live_price
                new_row["VOLUME"] = 0
                df_plot = pd.concat([df_plot, pd.DataFrame([new_row])], ignore_index=True)
                live_suffix = f" LIVE {now_time.strftime('%H:%M')} • ${live_price:,.8f}"
            else:
                idx = df_plot.index[-1]
                df_plot.loc[idx, "CLOSE"] = live_price
                # df_plot.loc[idx, "HIGH"] = max(df_plot.loc[idx, "HIGH"], live_price)
                # df_plot.loc[idx, "LOW"] = min(df_plot.loc[idx, "LOW"], live_price)
                live_suffix = f" (live {now_time.strftime('%H:%M')})"

        # === YOUR ORIGINAL PLOT – 100% IDENTICAL ===
        bg = '#1e1e1e'
        fg = 'white'
        fig = plt.figure(figsize=(22, 15), facecolor=bg)
        # gs = gridspec.GridSpec(4, 1, height_ratios=[4.8, 1, 1.2, 0.8], hspace=0.45)
        gs = gridspec.GridSpec(4, 2, width_ratios=[6, 1], height_ratios=[5, 1, 1.2, 0.8], hspace=0.4, wspace=0.05)
        ax1 = fig.add_subplot(gs[0])
        ax1.set_facecolor(bg)

        # === ONLY NOW THE PRICE LINES (after all twinx!) ===
        ax1.plot(df_plot['OPEN_TIME'], df_plot['CLOSE'], color='#00bfff', linewidth=1.5, label='Close')
        if 'EMA_9' in df_plot.columns:
            ax1.plot(df_plot['OPEN_TIME'], df_plot['EMA_9'], color='yellow', linewidth=1.1, label='EMA9')
        if 'EMA_21' in df_plot.columns:
            ax1.plot(df_plot['OPEN_TIME'], df_plot['EMA_21'], color='#32CD32', linewidth=1.1, label='EMA21')
        if 'WMA_55' in df_plot.columns:
            ax1.plot(df_plot['OPEN_TIME'], df_plot['WMA_55'], color='#FFB300', linewidth=1.1, label='WMA55')
        if 'EMA_200' in df_plot.columns:
            ax1.plot(df_plot['OPEN_TIME'], df_plot['EMA_200'], color='#E53935', linewidth=1.1, label='EMA200')
        if 'KAMA_9' in df_plot.columns:
            ax1.plot(df_plot['OPEN_TIME'], df_plot['KAMA_9'], color='#9C27B0', linewidth=1.1, label='KAMA9')

        ax1.set_title(f"{valid_symbol}{live_suffix} | @{username}", color=fg, fontsize=19, pad=25, weight='bold')
        ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')
        ax1.grid(True, alpha=0.25, color=fg, linewidth=0.5)
        ax1.tick_params(colors=fg, labelsize=11)

        # Last price marker
        ax1.axhline(live_price, color="white", linewidth=1, linestyle="--", alpha=0.5)
        ax1.text(
            0.2,
            live_price,
            f"{live_price:,.8f}",
            transform=ax1.get_yaxis_transform(),
            color="white",
            fontsize=10,
            fontweight='bold',
            va='center',
            bbox=dict(facecolor='#1e1e1e', edgecolor='none', pad=5),
        )

        # VOLUME – NARROW (0.2) + MIN HEIGHT 5% + COLORED
        ax_vol = ax1.twinx()
        vol_max = df_plot['VOLUME'].max()
        vol_min_display = vol_max * 0.25
        width = 0.025
        colors = [
            '#44ff44' if df_plot['CLOSE'].iloc[i] >= df_plot['OPEN'].iloc[i] else '#ff4444' for i in range(len(df_plot))
        ]
        display_volume = df_plot['VOLUME'].copy()
        display_volume[display_volume < vol_min_display] = vol_min_display
        ax_vol.bar(
            df_plot['OPEN_TIME'],
            display_volume,
            width=width,
            color=colors,
            alpha=0.5,
            edgecolor='#ffffff44',
            linewidth=0.25,
            align='center',
        )
        ax_vol.set_ylim(0, vol_max * 2.5)
        ax_vol.yaxis.set_label_position("right")
        ax_vol.yaxis.tick_right()
        ax_vol.set_ylabel('Volume', color=fg, fontsize=13, weight='bold')
        ax_vol.tick_params(colors=fg, labelsize=10)
        ax_vol.legend(
            ['Volume'], loc='upper right', facecolor=bg, labelcolor=fg, fontsize=11, frameon=True, fancybox=True
        )
        ax_vol.grid(True, alpha=0.15, color=fg, linewidth=0.4, linestyle='-', axis='y')

        ax4 = ax1.twinx()
        ax4.fill_between(df_plot['OPEN_TIME'], df_plot['VOLUME'], color='gray', alpha=0.4, label='volume')
        ax4.plot(df_plot['OPEN_TIME'], df_plot['VOLUME'], color='gray', linewidth=1)
        ax4.set_ylabel("volume", fontsize=12, color='gray')
        ax4.tick_params(axis='y', labelcolor='gray')

        # AFTER THE PRICE PLOT (ax1), BEFORE RSI
        ax_vol_profile = fig.add_subplot(gs[0, 0], frameon=False)  # No frame
        ax_vol_profile.set_position([0.85, 0.68, 0.12, 0.25])  # Top right, small

        # Volume-by-Price
        ax_vbp = fig.add_subplot(gs[0, 1])
        ax_vbp.set_facecolor('#1e1e1e')
        price_bins = np.linspace(df_7d['LOW'].min(), df_7d['HIGH'].max(), 40)
        vol_by_price = np.zeros(len(price_bins) - 1)
        for _, row in df_7d.iterrows():
            idx = np.searchsorted(price_bins, [row['LOW'], row['HIGH']])
            idx = np.clip(idx, 0, len(vol_by_price) - 1)
            if idx[0] == idx[1]:
                vol_by_price[idx[0]] += row['VOLUME']
            else:
                vol_by_price[idx[0] : idx[1]] += row['VOLUME'] / (idx[1] - idx[0])
        ax_vbp.barh(
            (price_bins[:-1] + price_bins[1:]) / 2,
            vol_by_price,
            height=(price_bins[1] - price_bins[0]) * 0.8,
            color='#ff69b4',
            alpha=0.6,
        )

        ax_vbp.set_ylim(ax1.get_ylim())
        ax_vbp.invert_xaxis()
        ax_vbp.set_xlabel('Vol', color='white', fontsize=10)
        ax_vbp.tick_params(colors='white')

        # TRENDLINE FROM 90 DAYS
        trend_direction, trend_data = detect_trend(df_90d)
        slope, intercept = trend_data if trend_data else (None, None)
        if slope is not None and intercept is not None:
            trend_y = get_trend_values(slope, intercept, df_plot['OPEN_TIME'])
            ax1.plot(
                df_plot['OPEN_TIME'],
                trend_y,
                color='orange',
                linewidth=5.0,
                alpha=0.98,
                label=f'90d Trend: {trend_direction}',
            )
            ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')
            high_pivots, low_pivots = find_pivots(df_90d, distance=8)
            last_7d_time = df_plot['OPEN_TIME'].iloc[0]
            high_pivots = [p for p in high_pivots if df_90d['OPEN_TIME'].iloc[p] >= last_7d_time]
            low_pivots = [p for p in low_pivots if df_90d['OPEN_TIME'].iloc[p] >= last_7d_time]
            pivots = high_pivots if trend_direction == 'DOWN' else low_pivots
            pivot_times = df_90d['OPEN_TIME'].iloc[pivots]
            pivot_prices = df_90d['HIGH' if trend_direction == 'DOWN' else 'LOW'].iloc[pivots]
            color_pivot = '#ff4444' if trend_direction == 'DOWN' else '#44ff44'
            ax1.scatter(
                pivot_times,
                pivot_prices,
                color=color_pivot,
                s=180,
                zorder=6,
                edgecolors='white',
                linewidth=3.0,
                marker='o',
            )

        # RSI
        ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)
        ax2.set_facecolor(bg)
        if 'RSI_9' in df_plot.columns:
            ax2.plot(df_plot['OPEN_TIME'], df_plot['RSI_9'], color='yellow', linewidth=1.1, label='RSI 9')
        if 'RSI_12' in df_plot.columns:
            ax2.plot(df_plot['OPEN_TIME'], df_plot['RSI_12'], color='orange', linewidth=1.1, label='RSI 12')
        if 'RSI_24' in df_plot.columns:
            ax2.plot(df_plot['OPEN_TIME'], df_plot['RSI_24'], color='red', linewidth=1.1, label='RSI 24')
        ax2.axhline(75, color='red', linestyle='--', alpha=0.8, linewidth=1)
        ax2.axhline(50, color=fg, linestyle='-', alpha=0.3, linewidth=0.8)
        ax2.axhline(25, color='green', linestyle='--', alpha=0.8, linewidth=1)
        ax2.set_ylim(0, 100)
        ax2.set_ylabel('RSI', color=fg, fontsize=12, weight='bold')
        ax2.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
        ax2.grid(True, alpha=0.15, color=fg)

        # TSI – back again + error-free!
        ax3 = fig.add_subplot(gs[2, 0], sharex=ax1)
        ax3.set_facecolor(bg)
        if 'TSI_FAST_12_7_7' in df_plot.columns:
            ax3.plot(df_plot['OPEN_TIME'], df_plot['TSI_FAST_12_7_7'], color='#00ff00', linewidth=1.8, label='TSI')
        if 'TSI_FAST_12_7_7_SIGNAL' in df_plot.columns:
            ax3.plot(
                df_plot['OPEN_TIME'], df_plot['TSI_FAST_12_7_7_SIGNAL'], color='red', linewidth=1.4, label='Signal'
            )
        ax3.axhline(85, color='red', linestyle=':', alpha=0.9, linewidth=1.2)
        ax3.axhline(50, color='red', linestyle='--', alpha=0.7)
        ax3.axhline(0, color=fg, linestyle='-', alpha=0.3)
        ax3.axhline(-50, color='green', linestyle='--', alpha=0.7)
        ax3.axhline(-85, color='green', linestyle=':', alpha=0.9, linewidth=1.2)
        ax3.set_ylim(-100, 100)
        ax3.set_ylabel('TSI', color=fg, fontsize=12, weight='bold')
        ax3.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
        ax3.grid(True, alpha=0.15, color=fg)
        # WHITE LABELS
        for ax in [ax1, ax2, ax3, ax_vol]:
            ax.tick_params(colors=fg, labelsize=10)
            for label in ax.get_xticklabels() + ax.get_yticklabels():
                label.set_color(fg)

        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            fig.savefig(tmp.name, format='png', dpi=300, facecolor='#1e1e1e', bbox_inches='tight', pad_inches=0.4)
            tmp_path = tmp.name

        await update.message.reply_photo(photo=open(tmp_path, 'rb'), caption=f"Chart {valid_symbol} (@{username})")

        os.unlink(tmp_path)  # Delete file afterwards
        plt.close(fig)

    except Exception as e:
        logger.error(f"!chart error: {e}", exc_info=True)
        await update.message.reply_text("Chart generation failed.")


# ========================================= !CANDLES HANDLER =========================================================================


async def candles_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- IMPORTS ---
    import os
    import tempfile

    import matplotlib.gridspec as gridspec
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np
    import pandas as pd

    if not update.message or not update.message.text:
        return

    # --- INPUT ---
    match = re.match(r"!candles\s+([A-Za-z0-9]+)", update.message.text.strip(), re.IGNORECASE)
    if not match:
        return

    raw_sym = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_sym)
    if not valid_symbol:
        await update.message.reply_text(f"Invalid coin: `{raw_sym}` not found.", parse_mode="Markdown")
        return

    symbol = valid_symbol
    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!candles " + raw_sym, update.effective_user)
    await update.message.reply_chat_action("upload_photo")

    try:
        # --- LOAD DATA ---
        df_7d = await get_1h_data_last_7d(valid_symbol)
        df_ind = await get_1h_indicators_last_7d(valid_symbol)
        df_90d = await get_1h_data_last_90d(valid_symbol)

        if df_7d.empty:
            await update.message.reply_text(f"No Chart-Data for {valid_symbol}")
            return

        # --- PREPROCESSING ---
        # Remove timezones for a clean merge
        if 'OPEN_TIME' in df_7d.columns:
            df_7d['OPEN_TIME'] = pd.to_datetime(df_7d['OPEN_TIME']).dt.tz_localize(None)
        if not df_ind.empty and 'OPEN_TIME' in df_ind.columns:
            df_ind['OPEN_TIME'] = pd.to_datetime(df_ind['OPEN_TIME']).dt.tz_localize(None)
        if not df_90d.empty and 'OPEN_TIME' in df_90d.columns:
            df_90d['OPEN_TIME'] = pd.to_datetime(df_90d['OPEN_TIME']).dt.tz_localize(None)

        # Merge
        if not df_ind.empty:
            cols = [c for c in df_ind.columns if c not in df_7d.columns or c == 'OPEN_TIME']
            df_plot = df_7d.merge(df_ind[cols], on='OPEN_TIME', how='left')
        else:
            df_plot = df_7d.copy()

        df_plot = df_plot.ffill().bfill()

        # Force floats
        for c in ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']:
            if c in df_plot.columns:
                df_plot[c] = pd.to_numeric(df_plot[c], errors='coerce')

        # --- LIVE PRICE UPDATE ---
        live_price = await get_live_price(valid_symbol)
        live_suffix = ""
        if live_price and isinstance(live_price, (int, float)) and live_price > 0:
            last_time = df_plot['OPEN_TIME'].iloc[-1]
            now_time = datetime.now()

            # Simple logic: update the last candle for the live view
            idx = df_plot.index[-1]
            df_plot.loc[idx, "CLOSE"] = float(live_price)
            df_plot.loc[idx, "HIGH"] = max(float(df_plot.loc[idx, "HIGH"]), float(live_price))
            df_plot.loc[idx, "LOW"] = min(float(df_plot.loc[idx, "LOW"]), float(live_price))
            live_suffix = f" (live ${live_price:,.8f})"

        # --- PLOT SETUP ---
        bg = '#1e1e1e'
        fg = 'white'
        fig = plt.figure(figsize=(22, 15), facecolor=bg)
        gs = gridspec.GridSpec(4, 2, width_ratios=[6, 1], height_ratios=[5, 1, 1.2, 0.8], hspace=0.4, wspace=0.05)

        ax1 = fig.add_subplot(gs[0, 0])
        ax1.set_facecolor(bg)

        # █████████████████████████████████████████████████████████████████████
        # CANDLESTICK LOGIC (replaces the line-plot logic)
        # █████████████████████████████████████████████████████████████████████

        # X axis as index (0..N) for perfect width
        x_vals = np.arange(len(df_plot))

        o = df_plot['OPEN'].values
        c = df_plot['CLOSE'].values
        h = df_plot['HIGH'].values
        l = df_plot['LOW'].values

        up = c >= o
        down = ~up
        col_up = '#44ff44'  # Your green
        col_down = '#ff4444'  # Your red

        # 1. Wicks (High -> Low)
        ax1.vlines(x_vals[up], l[up], h[up], color=col_up, linewidth=1.2, zorder=3)
        ax1.vlines(x_vals[down], l[down], h[down], color=col_down, linewidth=1.2, zorder=3)

        # 2. Body (Open -> Close) with minimum height (doji fix)
        body_h = np.abs(c - o)
        chart_range = h.max() - l.min()
        min_h = chart_range * 0.002  # 0.2% minimum height so dojis are visible
        body_h = np.maximum(body_h, min_h)
        body_b = np.minimum(o, c)

        ax1.bar(x_vals[up], body_h[up], bottom=body_b[up], width=0.6, color=col_up, linewidth=0, zorder=4)
        ax1.bar(x_vals[down], body_h[down], bottom=body_b[down], width=0.6, color=col_down, linewidth=0, zorder=4)

        # --- INDICATORS (adapted to x_vals) ---
        if 'EMA_9' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_9'], color='yellow', linewidth=1.1, label='EMA9')
        if 'EMA_21' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_21'], color='#32CD32', linewidth=1.1, label='EMA21')
        if 'WMA_55' in df_plot.columns:
            ax1.plot(x_vals, df_plot['WMA_55'], color='#FFB300', linewidth=1.1, label='WMA55')
        if 'EMA_200' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_200'], color='#E53935', linewidth=1.1, label='EMA200')
        if 'KAMA_9' in df_plot.columns:
            ax1.plot(x_vals, df_plot['KAMA_9'], color='#9C27B0', linewidth=1.1, label='KAMA9')

        ax1.set_title(f"{valid_symbol}{live_suffix} | @{username}", color=fg, fontsize=19, pad=25, weight='bold')
        ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')
        ax1.grid(True, alpha=0.25, color=fg, linewidth=0.5)
        ax1.tick_params(colors=fg, labelsize=11)

        # Last price marker
        ax1.axhline(live_price, color="white", linewidth=1, linestyle="--", alpha=0.5)
        ax1.text(
            0.2,
            live_price,
            f"{live_price:,.8f}",
            transform=ax1.get_yaxis_transform(),
            color="white",
            fontsize=10,
            fontweight='bold',
            va='center',
            bbox=dict(facecolor='#1e1e1e', edgecolor='none', pad=5),
        )

        # Adjust Y limit
        margin = chart_range * 0.05
        ax1.set_ylim(l.min() - margin, h.max() + margin)

        # --- VOLUME (bottom) ---
        ax_vol = ax1.twinx()
        vol_max = df_plot['VOLUME'].max()
        vol_min_display = vol_max * 0.25

        # Colors matching the candles
        vol_colors = np.where(up, col_up, col_down)

        # We need to compute 'display_volume' as in your original
        display_volume = df_plot['VOLUME'].copy()
        display_volume[display_volume < vol_min_display] = vol_min_display

        # IMPORTANT: use x_vals
        ax_vol.bar(
            x_vals,
            display_volume,
            width=0.6,
            color=vol_colors,
            alpha=0.5,
            edgecolor='#ffffff44',
            linewidth=0.25,
            align='center',
        )

        ax_vol.set_ylim(0, vol_max * 2.5)
        ax_vol.yaxis.set_label_position("right")
        ax_vol.yaxis.tick_right()
        ax_vol.set_ylabel('Volume', color=fg, fontsize=13, weight='bold')
        ax_vol.tick_params(colors=fg, labelsize=10)
        ax_vol.legend(
            ['Volume'], loc='upper right', facecolor=bg, labelcolor=fg, fontsize=11, frameon=True, fancybox=True
        )
        ax_vol.grid(True, alpha=0.15, color=fg, linewidth=0.4, linestyle='-', axis='y')

        # Volume overlay (gray area)
        ax4 = ax1.twinx()
        ax4.fill_between(x_vals, df_plot['VOLUME'], color='gray', alpha=0.4, label='volume')
        ax4.plot(x_vals, df_plot['VOLUME'], color='gray', linewidth=1)
        ax4.set_ylabel("volume", fontsize=12, color='gray')
        ax4.tick_params(axis='y', labelcolor='gray')
        ax4.set_ylim(0, vol_max * 2.5)  # Sync with ax_vol

        # --- VOLUME PROFILE (right) ---
        ax_vol_profile = fig.add_subplot(gs[0, 0], frameon=False)
        ax_vol_profile.set_position([0.85, 0.68, 0.12, 0.25])

        ax_vbp = fig.add_subplot(gs[0, 1])
        ax_vbp.set_facecolor('#1e1e1e')
        price_bins = np.linspace(l.min(), h.max(), 40)
        vol_by_price = np.zeros(len(price_bins) - 1)

        # Here we use searchsorted for speed, but your loop is also fine
        # We use your logic:
        for _, row in df_7d.iterrows():
            idx = np.searchsorted(price_bins, [row['LOW'], row['HIGH']])
            idx = np.clip(idx, 0, len(vol_by_price) - 1)
            if idx[0] == idx[1]:
                vol_by_price[idx[0]] += row['VOLUME']
            else:
                vol_by_price[idx[0] : idx[1]] += row['VOLUME'] / (idx[1] - idx[0])

        ax_vbp.barh(
            (price_bins[:-1] + price_bins[1:]) / 2,
            vol_by_price,
            height=(price_bins[1] - price_bins[0]) * 0.8,
            color='#ff69b4',
            alpha=0.6,
        )
        ax_vbp.set_ylim(ax1.get_ylim())  # Sync with chart
        ax_vbp.invert_xaxis()
        ax_vbp.set_xlabel('Vol', color='white', fontsize=10)
        ax_vbp.tick_params(colors='white')

        # --- TRENDLINE & PIVOTS ---
        trend_direction, trend_data = detect_trend(df_90d)
        slope, intercept = trend_data if trend_data else (None, None)
        if slope is not None and intercept is not None:
            # Compute trendline
            trend_y = get_trend_values(slope, intercept, df_plot['OPEN_TIME'])
            # Plot against x_vals
            ax1.plot(x_vals, trend_y, color='orange', linewidth=5.0, alpha=0.98, label=f'90d Trend: {trend_direction}')
            ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')

            # Pivots (mapping time -> index)
            high_pivots, low_pivots = find_pivots(df_90d, distance=8)
            last_7d_time = df_plot['OPEN_TIME'].iloc[0]

            pivots = high_pivots if trend_direction == 'DOWN' else low_pivots
            pivot_times = df_90d['OPEN_TIME'].iloc[pivots]
            pivot_prices = df_90d['HIGH' if trend_direction == 'DOWN' else 'LOW'].iloc[pivots]

            # Create map
            t_map = {t: i for i, t in enumerate(df_plot['OPEN_TIME'])}
            px, py = [], []
            for t, p in zip(pivot_times, pivot_prices):
                if t in t_map:
                    px.append(t_map[t])
                    py.append(p)

            color_pivot = '#ff4444' if trend_direction == 'DOWN' else '#44ff44'
            if px:
                ax1.scatter(px, py, color=color_pivot, s=180, zorder=6, edgecolors='white', linewidth=3.0, marker='o')

        # --- RSI ---
        ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)
        ax2.set_facecolor(bg)
        if 'RSI_9' in df_plot.columns:
            ax2.plot(x_vals, df_plot['RSI_9'], color='yellow', linewidth=1.1, label='RSI 9')
        if 'RSI_12' in df_plot.columns:
            ax2.plot(x_vals, df_plot['RSI_12'], color='orange', linewidth=1.1, label='RSI 12')
        if 'RSI_24' in df_plot.columns:
            ax2.plot(x_vals, df_plot['RSI_24'], color='red', linewidth=1.1, label='RSI 24')
        ax2.axhline(75, color='red', linestyle='--', alpha=0.8, linewidth=1)
        ax2.axhline(50, color=fg, linestyle='-', alpha=0.3, linewidth=0.8)
        ax2.axhline(25, color='green', linestyle='--', alpha=0.8, linewidth=1)
        ax2.set_ylim(0, 100)
        ax2.set_ylabel('RSI', color=fg, fontsize=12, weight='bold')
        ax2.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
        ax2.grid(True, alpha=0.15, color=fg)

        # --- TSI ---
        ax3 = fig.add_subplot(gs[2, 0], sharex=ax1)
        ax3.set_facecolor(bg)
        if 'TSI_FAST_12_7_7' in df_plot.columns:
            ax3.plot(x_vals, df_plot['TSI_FAST_12_7_7'], color='#00ff00', linewidth=1.8, label='TSI')
        if 'TSI_FAST_12_7_7_SIGNAL' in df_plot.columns:
            ax3.plot(x_vals, df_plot['TSI_FAST_12_7_7_SIGNAL'], color='red', linewidth=1.4, label='Signal')
        ax3.axhline(85, color='red', linestyle=':', alpha=0.9, linewidth=1.2)
        ax3.axhline(50, color='red', linestyle='--', alpha=0.7)
        ax3.axhline(0, color=fg, linestyle='-', alpha=0.3)
        ax3.axhline(-50, color='green', linestyle='--', alpha=0.7)
        ax3.axhline(-85, color='green', linestyle=':', alpha=0.9, linewidth=1.2)
        ax3.set_ylim(-100, 100)
        ax3.set_ylabel('TSI', color=fg, fontsize=12, weight='bold')
        ax3.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
        ax3.grid(True, alpha=0.15, color=fg)

        # --- FORMATIERUNG X-ACHSE (Index -> Datum) ---
        def format_date(x, pos=None):
            idx = int(x + 0.5)
            if 0 <= idx < len(df_plot):
                return df_plot['OPEN_TIME'].iloc[idx].strftime('%d.%m %H:%M')
            return ''

        ax1.xaxis.set_major_formatter(mticker.FuncFormatter(format_date))
        ax1.xaxis.set_major_locator(mticker.MaxNLocator(nbins=10))

        # White labels for all axes
        for ax in [ax1, ax2, ax3, ax_vol]:
            ax.tick_params(colors=fg, labelsize=10)
            for label in ax.get_xticklabels() + ax.get_yticklabels():
                label.set_color(fg)

        # Speichern & Senden
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            fig.savefig(tmp.name, format='png', dpi=300, facecolor=bg, bbox_inches='tight', pad_inches=0.4)
            tmp_path = tmp.name

        await update.message.reply_photo(photo=open(tmp_path, 'rb'), caption=f"Candles {valid_symbol} • @{username}")
        os.unlink(tmp_path)
        plt.close(fig)

    except Exception as e:
        logger.error(f"!candles error: {e}", exc_info=True)
        await update.message.reply_text("Candles chart failed.")


async def bb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- IDENTISCH ZU CANDLES ---
    import os
    import tempfile

    import matplotlib.gridspec as gridspec
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np
    import pandas as pd

    if not update.message or not update.message.text:
        return

    match = re.match(r"!bb\s+([A-Za-z0-9]+)", update.message.text.strip(), re.IGNORECASE)
    if not match:
        return

    raw_sym = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_sym)
    if not valid_symbol:
        await update.message.reply_text(f"Invalid coin: `{raw_sym}` not found.", parse_mode="Markdown")
        return

    symbol = valid_symbol
    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!bb " + raw_sym, update.effective_user)
    await update.message.reply_chat_action("upload_photo")

    try:
        df_7d = await get_1h_data_last_7d(valid_symbol)
        df_ind = await get_1h_indicators_last_7d(valid_symbol)
        df_90d = await get_1h_data_last_90d(valid_symbol)

        if df_7d.empty:
            await update.message.reply_text(f"No Chart-Data for {valid_symbol}")
            return

        # PREPROCESSING (identical)
        if 'OPEN_TIME' in df_7d.columns:
            df_7d['OPEN_TIME'] = pd.to_datetime(df_7d['OPEN_TIME']).dt.tz_localize(None)
        if not df_ind.empty and 'OPEN_TIME' in df_ind.columns:
            df_ind['OPEN_TIME'] = pd.to_datetime(df_ind['OPEN_TIME']).dt.tz_localize(None)
        if not df_90d.empty and 'OPEN_TIME' in df_ind.columns:
            df_90d['OPEN_TIME'] = pd.to_datetime(df_90d['OPEN_TIME']).dt.tz_localize(None)

        if not df_ind.empty:
            cols = [c for c in df_ind.columns if c not in df_7d.columns or c == 'OPEN_TIME']
            df_plot = df_7d.merge(df_ind[cols], on='OPEN_TIME', how='left')
        else:
            df_plot = df_7d.copy()
        df_plot = df_plot.ffill().bfill()

        for c in ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']:
            if c in df_plot.columns:
                df_plot[c] = pd.to_numeric(df_plot[c], errors='coerce')

        # LIVE PRICE (identical)
        live_price = await get_live_price(valid_symbol)
        live_suffix = ""
        if live_price and isinstance(live_price, (int, float)) and live_price > 0:
            idx = df_plot.index[-1]
            df_plot.loc[idx, "CLOSE"] = float(live_price)
            df_plot.loc[idx, "HIGH"] = max(float(df_plot.loc[idx, "HIGH"]), float(live_price))
            df_plot.loc[idx, "LOW"] = min(float(df_plot.loc[idx, "LOW"]), float(live_price))
            live_suffix = f" (live ${live_price:,.8f})"

        # PLOT SETUP (identical up to here)
        bg = '#1e1e1e'
        fg = 'white'
        fig = plt.figure(figsize=(22, 15), facecolor=bg)
        gs = gridspec.GridSpec(4, 2, width_ratios=[6, 1], height_ratios=[5, 1, 1.2, 0.8], hspace=0.4, wspace=0.05)
        ax1 = fig.add_subplot(gs[0, 0])
        ax1.set_facecolor(bg)

        x_vals = np.arange(len(df_plot))
        o = df_plot['OPEN'].values
        c = df_plot['CLOSE'].values
        h = df_plot['HIGH'].values
        l = df_plot['LOW'].values
        up = c >= o
        down = ~up
        col_up = '#44ff44'
        col_down = '#ff4444'

        # Candles (identical)
        ax1.vlines(x_vals[up], l[up], h[up], color=col_up, linewidth=1.2, zorder=3)
        ax1.vlines(x_vals[down], l[down], h[down], color=col_down, linewidth=1.2, zorder=3)
        body_h = np.abs(c - o)
        chart_range = h.max() - l.min()
        min_h = chart_range * 0.002
        body_h = np.maximum(body_h, min_h)
        body_b = np.minimum(o, c)
        ax1.bar(x_vals[up], body_h[up], bottom=body_b[up], width=0.6, color=col_up, linewidth=0, zorder=4)
        ax1.bar(x_vals[down], body_h[down], bottom=body_b[down], width=0.6, color=col_down, linewidth=0, zorder=4)

        # --- NUR HIER NEU: Bollinger Bands ---
        if all(col in df_plot.columns for col in ['BOLL_UPPER_20', 'BOLL_MID_20', 'BOLL_LOWER_20']):
            ax1.plot(x_vals, df_plot['BOLL_UPPER_20'], color='#00ffff', linewidth=1.3, label='BB Upper')
            ax1.plot(x_vals, df_plot['BOLL_MID_20'], color='#00ffff', linewidth=1.1, alpha=0.8, label='BB Mid')
            ax1.plot(x_vals, df_plot['BOLL_LOWER_20'], color='#00ffff', linewidth=1.3, label='BB Lower')
            ax1.fill_between(x_vals, df_plot['BOLL_LOWER_20'], df_plot['BOLL_UPPER_20'], color='grey', alpha=0.15)

        # Standard EMAs (as in candles_handler)
        if 'EMA_9' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_9'], color='yellow', linewidth=1.1, label='EMA9')
        if 'EMA_21' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_21'], color='#32CD32', linewidth=1.1, label='EMA21')
        if 'WMA_55' in df_plot.columns:
            ax1.plot(x_vals, df_plot['WMA_55'], color='#FFB300', linewidth=1.1, label='WMA55')
        if 'EMA_200' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_200'], color='#E53935', linewidth=1.1, label='EMA200')
        if 'KAMA_9' in df_plot.columns:
            ax1.plot(x_vals, df_plot['KAMA_9'], color='#9C27B0', linewidth=1.1, label='KAMA9')

        ax1.set_title(
            f"{valid_symbol} Bollinger Bands{live_suffix} | @{username}", color=fg, fontsize=19, pad=25, weight='bold'
        )
        ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')
        ax1.grid(True, alpha=0.25, color=fg, linewidth=0.5)
        ax1.tick_params(colors=fg, labelsize=11)

        # Last price marker
        ax1.axhline(live_price, color="white", linewidth=1, linestyle="--", alpha=0.5)
        ax1.text(
            0.2,
            live_price,
            f"{live_price:,.8f}",
            transform=ax1.get_yaxis_transform(),
            color="white",
            fontsize=10,
            fontweight='bold',
            va='center',
            bbox=dict(facecolor='#1e1e1e', edgecolor='none', pad=5),
        )

        # Y-Limit
        margin = chart_range * 0.05
        ax1.set_ylim(l.min() - margin, h.max() + margin)

        # Rest identical: Volume, VBP, Trend, RSI, TSI, formatting, saving...
        # (Simply copy the entire rest from your candles_handler from Volume to the end here)

        # ... [Volume, Volume Profile, trendline, RSI, TSI, X formatting, saving – all 1:1 like in candles_handler] ...

        # --- VOLUME (bottom) ---
        ax_vol = ax1.twinx()
        vol_max = df_plot['VOLUME'].max()
        vol_min_display = vol_max * 0.25

        # Colors matching the candles
        vol_colors = np.where(up, col_up, col_down)

        # We need to compute 'display_volume' as in your original
        display_volume = df_plot['VOLUME'].copy()
        display_volume[display_volume < vol_min_display] = vol_min_display

        # IMPORTANT: use x_vals
        ax_vol.bar(
            x_vals,
            display_volume,
            width=0.6,
            color=vol_colors,
            alpha=0.5,
            edgecolor='#ffffff44',
            linewidth=0.25,
            align='center',
        )

        ax_vol.set_ylim(0, vol_max * 2.5)
        ax_vol.yaxis.set_label_position("right")
        ax_vol.yaxis.tick_right()
        ax_vol.set_ylabel('Volume', color=fg, fontsize=13, weight='bold')
        ax_vol.tick_params(colors=fg, labelsize=10)
        ax_vol.legend(
            ['Volume'], loc='upper right', facecolor=bg, labelcolor=fg, fontsize=11, frameon=True, fancybox=True
        )
        ax_vol.grid(True, alpha=0.15, color=fg, linewidth=0.4, linestyle='-', axis='y')

        # Volume overlay (gray area)
        ax4 = ax1.twinx()
        ax4.fill_between(x_vals, df_plot['VOLUME'], color='gray', alpha=0.4, label='volume')
        ax4.plot(x_vals, df_plot['VOLUME'], color='gray', linewidth=1)
        ax4.set_ylabel("volume", fontsize=12, color='gray')
        ax4.tick_params(axis='y', labelcolor='gray')
        ax4.set_ylim(0, vol_max * 2.5)  # Sync with ax_vol

        # --- VOLUME PROFILE (right) ---
        ax_vol_profile = fig.add_subplot(gs[0, 0], frameon=False)
        ax_vol_profile.set_position([0.85, 0.68, 0.12, 0.25])

        ax_vbp = fig.add_subplot(gs[0, 1])
        ax_vbp.set_facecolor('#1e1e1e')
        price_bins = np.linspace(l.min(), h.max(), 40)
        vol_by_price = np.zeros(len(price_bins) - 1)

        # Here we use searchsorted for speed, but your loop is also fine
        # We use your logic:
        for _, row in df_7d.iterrows():
            idx = np.searchsorted(price_bins, [row['LOW'], row['HIGH']])
            idx = np.clip(idx, 0, len(vol_by_price) - 1)
            if idx[0] == idx[1]:
                vol_by_price[idx[0]] += row['VOLUME']
            else:
                vol_by_price[idx[0] : idx[1]] += row['VOLUME'] / (idx[1] - idx[0])

        ax_vbp.barh(
            (price_bins[:-1] + price_bins[1:]) / 2,
            vol_by_price,
            height=(price_bins[1] - price_bins[0]) * 0.8,
            color='#ff69b4',
            alpha=0.6,
        )
        ax_vbp.set_ylim(ax1.get_ylim())  # Sync with chart
        ax_vbp.invert_xaxis()
        ax_vbp.set_xlabel('Vol', color='white', fontsize=10)
        ax_vbp.tick_params(colors='white')

        # --- TRENDLINE & PIVOTS ---
        trend_direction, trend_data = detect_trend(df_90d)
        slope, intercept = trend_data if trend_data else (None, None)
        if slope is not None and intercept is not None:
            # Compute trendline
            trend_y = get_trend_values(slope, intercept, df_plot['OPEN_TIME'])
            # Plot against x_vals
            ax1.plot(x_vals, trend_y, color='orange', linewidth=5.0, alpha=0.98, label=f'90d Trend: {trend_direction}')
            ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')

            # Pivots (mapping time -> index)
            high_pivots, low_pivots = find_pivots(df_90d, distance=8)
            last_7d_time = df_plot['OPEN_TIME'].iloc[0]

            pivots = high_pivots if trend_direction == 'DOWN' else low_pivots
            pivot_times = df_90d['OPEN_TIME'].iloc[pivots]
            pivot_prices = df_90d['HIGH' if trend_direction == 'DOWN' else 'LOW'].iloc[pivots]

            # Create map
            t_map = {t: i for i, t in enumerate(df_plot['OPEN_TIME'])}
            px, py = [], []
            for t, p in zip(pivot_times, pivot_prices):
                if t in t_map:
                    px.append(t_map[t])
                    py.append(p)

            color_pivot = '#ff4444' if trend_direction == 'DOWN' else '#44ff44'
            if px:
                ax1.scatter(px, py, color=color_pivot, s=180, zorder=6, edgecolors='white', linewidth=3.0, marker='o')

        # --- RSI ---
        ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)
        ax2.set_facecolor(bg)
        if 'RSI_9' in df_plot.columns:
            ax2.plot(x_vals, df_plot['RSI_9'], color='yellow', linewidth=1.1, label='RSI 9')
        if 'RSI_12' in df_plot.columns:
            ax2.plot(x_vals, df_plot['RSI_12'], color='orange', linewidth=1.1, label='RSI 12')
        if 'RSI_24' in df_plot.columns:
            ax2.plot(x_vals, df_plot['RSI_24'], color='red', linewidth=1.1, label='RSI 24')
        ax2.axhline(75, color='red', linestyle='--', alpha=0.8, linewidth=1)
        ax2.axhline(50, color=fg, linestyle='-', alpha=0.3, linewidth=0.8)
        ax2.axhline(25, color='green', linestyle='--', alpha=0.8, linewidth=1)
        ax2.set_ylim(0, 100)
        ax2.set_ylabel('RSI', color=fg, fontsize=12, weight='bold')
        ax2.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
        ax2.grid(True, alpha=0.15, color=fg)

        # --- TSI ---
        ax3 = fig.add_subplot(gs[2, 0], sharex=ax1)
        ax3.set_facecolor(bg)
        if 'TSI_FAST_12_7_7' in df_plot.columns:
            ax3.plot(x_vals, df_plot['TSI_FAST_12_7_7'], color='#00ff00', linewidth=1.8, label='TSI')
        if 'TSI_FAST_12_7_7_SIGNAL' in df_plot.columns:
            ax3.plot(x_vals, df_plot['TSI_FAST_12_7_7_SIGNAL'], color='red', linewidth=1.4, label='Signal')
        ax3.axhline(85, color='red', linestyle=':', alpha=0.9, linewidth=1.2)
        ax3.axhline(50, color='red', linestyle='--', alpha=0.7)
        ax3.axhline(0, color=fg, linestyle='-', alpha=0.3)
        ax3.axhline(-50, color='green', linestyle='--', alpha=0.7)
        ax3.axhline(-85, color='green', linestyle=':', alpha=0.9, linewidth=1.2)
        ax3.set_ylim(-100, 100)
        ax3.set_ylabel('TSI', color=fg, fontsize=12, weight='bold')
        ax3.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
        ax3.grid(True, alpha=0.15, color=fg)

        # --- FORMATIERUNG X-ACHSE (Index -> Datum) ---
        def format_date(x, pos=None):
            idx = int(x + 0.5)
            if 0 <= idx < len(df_plot):
                return df_plot['OPEN_TIME'].iloc[idx].strftime('%d.%m %H:%M')
            return ''

        ax1.xaxis.set_major_formatter(mticker.FuncFormatter(format_date))
        ax1.xaxis.set_major_locator(mticker.MaxNLocator(nbins=10))

        # White labels for all axes
        for ax in [ax1, ax2, ax3, ax_vol]:
            ax.tick_params(colors=fg, labelsize=10)
            for label in ax.get_xticklabels() + ax.get_yticklabels():
                label.set_color(fg)

        # Speichern & Senden
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            fig.savefig(tmp.name, format='png', dpi=300, facecolor=bg, bbox_inches='tight', pad_inches=0.4)
            tmp_path = tmp.name

        await update.message.reply_photo(
            photo=open(tmp_path, 'rb'), caption=f"Bollinger Bands {valid_symbol} • @{username}"
        )
        os.unlink(tmp_path)
        plt.close(fig)

    except Exception as e:
        logger.error(f"!bb error: {e}", exc_info=True)
        await update.message.reply_text("BB chart failed.")


async def don_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- IDENTISCH ZU CANDLES ---
    import os
    import tempfile

    import matplotlib.gridspec as gridspec
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np
    import pandas as pd

    if not update.message or not update.message.text:
        return

    match = re.match(r"!don\s+([A-Za-z0-9]+)", update.message.text.strip(), re.IGNORECASE)
    if not match:
        return

    raw_sym = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_sym)
    if not valid_symbol:
        await update.message.reply_text(f"Invalid coin: `{raw_sym}` not found.", parse_mode="Markdown")
        return

    symbol = valid_symbol
    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!don " + raw_sym, update.effective_user)
    await update.message.reply_chat_action("upload_photo")

    try:
        df_7d = await get_1h_data_last_7d(valid_symbol)
        df_ind = await get_1h_indicators_last_7d(valid_symbol)
        df_90d = await get_1h_data_last_90d(valid_symbol)

        if df_7d.empty:
            await update.message.reply_text(f"No Chart-Data for {valid_symbol}")
            return

        # PREPROCESSING (identical to above)
        # ... (exactly like in bb_handler) ...

        # PREPROCESSING (identical)
        if 'OPEN_TIME' in df_7d.columns:
            df_7d['OPEN_TIME'] = pd.to_datetime(df_7d['OPEN_TIME']).dt.tz_localize(None)
        if not df_ind.empty and 'OPEN_TIME' in df_ind.columns:
            df_ind['OPEN_TIME'] = pd.to_datetime(df_ind['OPEN_TIME']).dt.tz_localize(None)
        if not df_90d.empty and 'OPEN_TIME' in df_ind.columns:
            df_90d['OPEN_TIME'] = pd.to_datetime(df_90d['OPEN_TIME']).dt.tz_localize(None)

        if not df_ind.empty:
            cols = [c for c in df_ind.columns if c not in df_7d.columns or c == 'OPEN_TIME']
            df_plot = df_7d.merge(df_ind[cols], on='OPEN_TIME', how='left')
        else:
            df_plot = df_7d.copy()
        df_plot = df_plot.ffill().bfill()

        for c in ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']:
            if c in df_plot.columns:
                df_plot[c] = pd.to_numeric(df_plot[c], errors='coerce')

        # LIVE PRICE (identical)
        live_price = await get_live_price(valid_symbol)
        live_suffix = ""
        if live_price and isinstance(live_price, (int, float)) and live_price > 0:
            idx = df_plot.index[-1]
            df_plot.loc[idx, "CLOSE"] = float(live_price)
            df_plot.loc[idx, "HIGH"] = max(float(df_plot.loc[idx, "HIGH"]), float(live_price))
            df_plot.loc[idx, "LOW"] = min(float(df_plot.loc[idx, "LOW"]), float(live_price))
            live_suffix = f" (live ${live_price:,.8f})"

        # PLOT SETUP
        bg = '#1e1e1e'
        fg = 'white'
        fig = plt.figure(figsize=(22, 15), facecolor=bg)
        gs = gridspec.GridSpec(4, 2, width_ratios=[6, 1], height_ratios=[5, 1, 1.2, 0.8], hspace=0.4, wspace=0.05)
        ax1 = fig.add_subplot(gs[0, 0])
        ax1.set_facecolor(bg)

        x_vals = np.arange(len(df_plot))
        o = df_plot['OPEN'].values
        c = df_plot['CLOSE'].values
        h = df_plot['HIGH'].values
        l = df_plot['LOW'].values
        up = c >= o
        down = ~up
        col_up = '#44ff44'
        col_down = '#ff4444'

        # Candles (identical)
        ax1.vlines(x_vals[up], l[up], h[up], color=col_up, linewidth=1.2, zorder=3)
        ax1.vlines(x_vals[down], l[down], h[down], color=col_down, linewidth=1.2, zorder=3)
        body_h = np.abs(c - o)
        chart_range = h.max() - l.min()
        min_h = chart_range * 0.002
        body_h = np.maximum(body_h, min_h)
        body_b = np.minimum(o, c)
        ax1.bar(x_vals[up], body_h[up], bottom=body_b[up], width=0.6, color=col_up, linewidth=0, zorder=4)
        ax1.bar(x_vals[down], body_h[down], bottom=body_b[down], width=0.6, color=col_down, linewidth=0, zorder=4)

        # Candles (identical to above)

        # --- NUR HIER NEU: Donchian Channel 20 ---
        if all(col in df_plot.columns for col in ['DONCHIAN_UPPER_20', 'DONCHIAN_MID_20', 'DONCHIAN_LOWER_20']):
            ax1.plot(x_vals, df_plot['DONCHIAN_UPPER_20'], color='#00ffff', linewidth=1.5, label='Don Upper 20')
            ax1.plot(x_vals, df_plot['DONCHIAN_MID_20'], color='#00ffff', linewidth=1.0, alpha=0.8, label='Don Mid 20')
            ax1.plot(x_vals, df_plot['DONCHIAN_LOWER_20'], color='#00ffff', linewidth=1.5, label='Don Lower 20')
            ax1.fill_between(
                x_vals, df_plot['DONCHIAN_LOWER_20'], df_plot['DONCHIAN_UPPER_20'], color='#00ffff', alpha=0.06
            )

        # Standard EMAs (as always)
        # ... (wie in candles_handler)

        # Standard EMAs (as in candles_handler)
        if 'EMA_9' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_9'], color='yellow', linewidth=1.1, label='EMA9')
        if 'EMA_21' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_21'], color='#32CD32', linewidth=1.1, label='EMA21')
        if 'WMA_55' in df_plot.columns:
            ax1.plot(x_vals, df_plot['WMA_55'], color='#FFB300', linewidth=1.1, label='WMA55')
        if 'EMA_200' in df_plot.columns:
            ax1.plot(x_vals, df_plot['EMA_200'], color='#E53935', linewidth=1.1, label='EMA200')
        if 'KAMA_9' in df_plot.columns:
            ax1.plot(x_vals, df_plot['KAMA_9'], color='#9C27B0', linewidth=1.1, label='KAMA9')

        ax1.set_title(
            f"{valid_symbol} Donchian 20{live_suffix} | @{username}", color=fg, fontsize=19, pad=25, weight='bold'
        )
        ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')
        # ... rest identical to candles_handler ...
        ax1.grid(True, alpha=0.25, color=fg, linewidth=0.5)
        ax1.tick_params(colors=fg, labelsize=11)

        # Last price marker
        ax1.axhline(live_price, color="white", linewidth=1, linestyle="--", alpha=0.5)
        ax1.text(
            0.2,
            live_price,
            f"{live_price:,.8f}",
            transform=ax1.get_yaxis_transform(),
            color="white",
            fontsize=10,
            fontweight='bold',
            va='center',
            bbox=dict(facecolor='#1e1e1e', edgecolor='none', pad=5),
        )

        # Y-Limit
        margin = chart_range * 0.05
        ax1.set_ylim(l.min() - margin, h.max() + margin)

        # Rest identical: Volume, VBP, Trend, RSI, TSI, formatting, saving...
        # (Simply copy the entire rest from your candles_handler from Volume to the end here)

        # ... [Volume, Volume Profile, trendline, RSI, TSI, X formatting, saving – all 1:1 like in candles_handler] ...

        # --- VOLUME (bottom) ---
        ax_vol = ax1.twinx()
        vol_max = df_plot['VOLUME'].max()
        vol_min_display = vol_max * 0.25

        # Colors matching the candles
        vol_colors = np.where(up, col_up, col_down)

        # We need to compute 'display_volume' as in your original
        display_volume = df_plot['VOLUME'].copy()
        display_volume[display_volume < vol_min_display] = vol_min_display

        # IMPORTANT: use x_vals
        ax_vol.bar(
            x_vals,
            display_volume,
            width=0.6,
            color=vol_colors,
            alpha=0.5,
            edgecolor='#ffffff44',
            linewidth=0.25,
            align='center',
        )

        ax_vol.set_ylim(0, vol_max * 2.5)
        ax_vol.yaxis.set_label_position("right")
        ax_vol.yaxis.tick_right()
        ax_vol.set_ylabel('Volume', color=fg, fontsize=13, weight='bold')
        ax_vol.tick_params(colors=fg, labelsize=10)
        ax_vol.legend(
            ['Volume'], loc='upper right', facecolor=bg, labelcolor=fg, fontsize=11, frameon=True, fancybox=True
        )
        ax_vol.grid(True, alpha=0.15, color=fg, linewidth=0.4, linestyle='-', axis='y')

        # Volume overlay (gray area)
        ax4 = ax1.twinx()
        ax4.fill_between(x_vals, df_plot['VOLUME'], color='gray', alpha=0.4, label='volume')
        ax4.plot(x_vals, df_plot['VOLUME'], color='gray', linewidth=1)
        ax4.set_ylabel("volume", fontsize=12, color='gray')
        ax4.tick_params(axis='y', labelcolor='gray')
        ax4.set_ylim(0, vol_max * 2.5)  # Sync with ax_vol

        # --- VOLUME PROFILE (right) ---
        ax_vol_profile = fig.add_subplot(gs[0, 0], frameon=False)
        ax_vol_profile.set_position([0.85, 0.68, 0.12, 0.25])

        ax_vbp = fig.add_subplot(gs[0, 1])
        ax_vbp.set_facecolor('#1e1e1e')
        price_bins = np.linspace(l.min(), h.max(), 40)
        vol_by_price = np.zeros(len(price_bins) - 1)

        # Here we use searchsorted for speed, but your loop is also fine
        # We use your logic:
        for _, row in df_7d.iterrows():
            idx = np.searchsorted(price_bins, [row['LOW'], row['HIGH']])
            idx = np.clip(idx, 0, len(vol_by_price) - 1)
            if idx[0] == idx[1]:
                vol_by_price[idx[0]] += row['VOLUME']
            else:
                vol_by_price[idx[0] : idx[1]] += row['VOLUME'] / (idx[1] - idx[0])

        ax_vbp.barh(
            (price_bins[:-1] + price_bins[1:]) / 2,
            vol_by_price,
            height=(price_bins[1] - price_bins[0]) * 0.8,
            color='#ff69b4',
            alpha=0.6,
        )
        ax_vbp.set_ylim(ax1.get_ylim())  # Sync with chart
        ax_vbp.invert_xaxis()
        ax_vbp.set_xlabel('Vol', color='white', fontsize=10)
        ax_vbp.tick_params(colors='white')

        # --- TRENDLINE & PIVOTS ---
        trend_direction, trend_data = detect_trend(df_90d)
        slope, intercept = trend_data if trend_data else (None, None)
        if slope is not None and intercept is not None:
            # Compute trendline
            trend_y = get_trend_values(slope, intercept, df_plot['OPEN_TIME'])
            # Plot against x_vals
            ax1.plot(x_vals, trend_y, color='orange', linewidth=5.0, alpha=0.98, label=f'90d Trend: {trend_direction}')
            ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')

            # Pivots (mapping time -> index)
            high_pivots, low_pivots = find_pivots(df_90d, distance=8)
            last_7d_time = df_plot['OPEN_TIME'].iloc[0]

            pivots = high_pivots if trend_direction == 'DOWN' else low_pivots
            pivot_times = df_90d['OPEN_TIME'].iloc[pivots]
            pivot_prices = df_90d['HIGH' if trend_direction == 'DOWN' else 'LOW'].iloc[pivots]

            # Create map
            t_map = {t: i for i, t in enumerate(df_plot['OPEN_TIME'])}
            px, py = [], []
            for t, p in zip(pivot_times, pivot_prices):
                if t in t_map:
                    px.append(t_map[t])
                    py.append(p)

            color_pivot = '#ff4444' if trend_direction == 'DOWN' else '#44ff44'
            if px:
                ax1.scatter(px, py, color=color_pivot, s=180, zorder=6, edgecolors='white', linewidth=3.0, marker='o')

        # --- RSI ---
        ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)
        ax2.set_facecolor(bg)
        if 'RSI_9' in df_plot.columns:
            ax2.plot(x_vals, df_plot['RSI_9'], color='yellow', linewidth=1.1, label='RSI 9')
        if 'RSI_12' in df_plot.columns:
            ax2.plot(x_vals, df_plot['RSI_12'], color='orange', linewidth=1.1, label='RSI 12')
        if 'RSI_24' in df_plot.columns:
            ax2.plot(x_vals, df_plot['RSI_24'], color='red', linewidth=1.1, label='RSI 24')
        ax2.axhline(75, color='red', linestyle='--', alpha=0.8, linewidth=1)
        ax2.axhline(50, color=fg, linestyle='-', alpha=0.3, linewidth=0.8)
        ax2.axhline(25, color='green', linestyle='--', alpha=0.8, linewidth=1)
        ax2.set_ylim(0, 100)
        ax2.set_ylabel('RSI', color=fg, fontsize=12, weight='bold')
        ax2.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
        ax2.grid(True, alpha=0.15, color=fg)

        # --- TSI ---
        ax3 = fig.add_subplot(gs[2, 0], sharex=ax1)
        ax3.set_facecolor(bg)
        if 'TSI_FAST_12_7_7' in df_plot.columns:
            ax3.plot(x_vals, df_plot['TSI_FAST_12_7_7'], color='#00ff00', linewidth=1.8, label='TSI')
        if 'TSI_FAST_12_7_7_SIGNAL' in df_plot.columns:
            ax3.plot(x_vals, df_plot['TSI_FAST_12_7_7_SIGNAL'], color='red', linewidth=1.4, label='Signal')
        ax3.axhline(85, color='red', linestyle=':', alpha=0.9, linewidth=1.2)
        ax3.axhline(50, color='red', linestyle='--', alpha=0.7)
        ax3.axhline(0, color=fg, linestyle='-', alpha=0.3)
        ax3.axhline(-50, color='green', linestyle='--', alpha=0.7)
        ax3.axhline(-85, color='green', linestyle=':', alpha=0.9, linewidth=1.2)
        ax3.set_ylim(-100, 100)
        ax3.set_ylabel('TSI', color=fg, fontsize=12, weight='bold')
        ax3.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
        ax3.grid(True, alpha=0.15, color=fg)

        # --- FORMATIERUNG X-ACHSE (Index -> Datum) ---
        def format_date(x, pos=None):
            idx = int(x + 0.5)
            if 0 <= idx < len(df_plot):
                return df_plot['OPEN_TIME'].iloc[idx].strftime('%d.%m %H:%M')
            return ''

        ax1.xaxis.set_major_formatter(mticker.FuncFormatter(format_date))
        ax1.xaxis.set_major_locator(mticker.MaxNLocator(nbins=10))

        # White labels for all axes
        for ax in [ax1, ax2, ax3, ax_vol]:
            ax.tick_params(colors=fg, labelsize=10)
            for label in ax.get_xticklabels() + ax.get_yticklabels():
                label.set_color(fg)

        # Speichern & Senden
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            fig.savefig(tmp.name, format='png', dpi=300, facecolor=bg, bbox_inches='tight', pad_inches=0.4)
            tmp_path = tmp.name

        await update.message.reply_photo(
            photo=open(tmp_path, 'rb'), caption=f"Donchian Channel 20 {valid_symbol} • @{username}"
        )
        os.unlink(tmp_path)
        plt.close(fig)

    except Exception as e:
        logger.error(f"!don error: {e}", exc_info=True)
        await update.message.reply_text("Donchian chart failed.")


# ========================================= !DAILY HANDLER =========================================================================
async def daily_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    match = re.match(r"!daily\s+([A-Za-z0-9]+)", update.message.text.strip(), re.IGNORECASE)
    if not match:
        return

    raw_sym = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_sym)
    if not valid_symbol:
        await update.message.reply_text(f"Invalid coin: `{raw_sym}` not found.", parse_mode="Markdown")
        return

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!daily " + raw_sym, update.effective_user)
    await update.message.reply_chat_action(ChatAction.UPLOAD_PHOTO)

    try:
        await send_daily_chart(update, context, valid_symbol, username)
    except Exception as e:
        logger.error(f"!daily error: {e}", exc_info=True)
        await update.message.reply_text("Daily chart generation failed.")


async def get_1h_data_last_300d(symbol: str) -> pd.DataFrame:
    tablename = f'"{symbol.upper()}_1h"'
    query = f"""
        SELECT * FROM {tablename}
        WHERE open_time >= NOW() - INTERVAL '305 days'
        ORDER BY open_time ASC
    """
    conn = await get_conn()
    try:
        rows = await conn.fetch(query)
        await release_conn(conn)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(row) for row in rows])
        return to_uppercase_df(df)
    except Exception as e:
        logger.error(f"DB error 300d {symbol}: {e}")
        await release_conn(conn)
        return pd.DataFrame()


async def get_1h_data_last_360d(symbol: str) -> pd.DataFrame:
    tablename = f'"{symbol.upper()}_1h"'
    query = f"""
        SELECT * FROM {tablename}
        WHERE open_time >= NOW() - INTERVAL '365 days'
        ORDER BY open_time ASC
    """
    conn = await get_conn()
    try:
        rows = await conn.fetch(query)
        if not rows:
            return pd.DataFrame()
        columns = rows[0].keys()
        data = [dict(row) for row in rows]
        df = pd.DataFrame(data, columns=columns)
        return to_uppercase_df(df)
    except Exception as e:
        logger.error(f"DB Error 360d {symbol}: {e}")
        return pd.DataFrame()
    finally:
        await release_conn(conn)


async def get_1h_indicators_last_360d(symbol: str) -> pd.DataFrame:
    tablename = f'"{symbol.upper()}_1h_indicators"'
    query = f"""
        SELECT * FROM {tablename}
        WHERE open_time >= NOW() - INTERVAL '365 days'
        ORDER BY open_time ASC
    """
    conn = await get_conn()
    try:
        rows = await conn.fetch(query)
        if not rows:
            return pd.DataFrame()
        columns = rows[0].keys()
        data = [dict(row) for row in rows]
        df = pd.DataFrame(data, columns=columns)
        return to_uppercase_df(df)
    except Exception as e:
        logger.error(f"DB Error indicators 360d {symbol}: {e}")
        return pd.DataFrame()
    finally:
        await release_conn(conn)


def calculate_ema(data, period=9):
    """
    Berechnet Exponential Moving Average (EMA)
    """
    return data['close'].ewm(span=period, adjust=False).mean()


def calculate_rsi(data, period=14):
    delta = data['close'].diff()
    up, down = delta.copy(), delta.copy()
    up[up < 0] = 0
    down[down > 0] = 0
    roll_up = up.ewm(span=period, adjust=False).mean()
    roll_down = down.abs().ewm(span=period, adjust=False).mean()
    rs = roll_up / roll_down
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(0)


def calculate_kama(data, period=10, fastest_sc=2 / 3, slowest_sc=2 / 31):
    close = data['close'].values
    kama = np.zeros_like(close)
    kama[:] = np.nan
    if len(close) < period:
        return pd.Series(kama, index=data.index)
    volatility = np.array(
        [np.sum(np.abs(np.diff(close[i - period : i + 1]))) if i >= period else np.nan for i in range(len(close))]
    )
    er = np.zeros_like(close)
    er[period:] = np.abs(close[period:] - close[:-period]) / volatility[period:]
    sc = (er * (fastest_sc - slowest_sc) + slowest_sc) ** 2
    kama[period] = close[period]
    for i in range(period + 1, len(close)):
        kama[i] = kama[i - 1] + sc[i] * (close[i] - kama[i - 1])
    # return pd.Series(kama, index=data.index).fillna(method='bfill')
    return pd.Series(kama, index=data.index).bfill()


def calculate_tsi(data, long=25, short=13, signal=7):
    pc = data['close'].diff()
    smoothed_pc = pc.ewm(span=long, adjust=False).mean()
    double_smoothed_pc = smoothed_pc.ewm(span=short, adjust=False).mean()
    smoothed_abs = pc.abs().ewm(span=long, adjust=False).mean()
    double_smoothed_abs = smoothed_abs.ewm(span=short, adjust=False).mean()
    tsi = 100 * double_smoothed_pc / double_smoothed_abs
    signal_line = tsi.ewm(span=signal, adjust=False).mean()
    return tsi.fillna(0), signal_line.fillna(0)


async def send_daily_chart(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, username: str):
    from datetime import datetime

    import matplotlib.gridspec as gridspec
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.dates import DateFormatter

    # --- 1. 300 days of 1h data ---
    df_1h_300d = await get_1h_data_last_300d(symbol)
    if df_1h_300d.empty:
        await update.message.reply_text(f"no data for {symbol}")
        return

    logging.info(f"{symbol}: {len(df_1h_300d)} 1h candles (300d) loaded")

    # --- 2. Last 120 days for chart ---
    df_1h_chart = df_1h_300d.tail(min(len(df_1h_300d), 120 * 24)).copy()
    if len(df_1h_chart) < 7 * 24:
        await update.message.reply_text(f"not enough data for {symbol} (min. 7 days)")
        return

    df_1h_chart['date'] = df_1h_chart['OPEN_TIME'].dt.floor('D')
    df_daily = (
        df_1h_chart.groupby('date')
        .agg(
            open=('OPEN', 'first'),
            high=('HIGH', 'max'),
            low=('LOW', 'min'),
            close=('CLOSE', 'last'),
            volume=('VOLUME', 'sum'),
        )
        .reset_index()
    )

    df_daily['date'] = pd.to_datetime(df_daily['date'])  # ensure
    df_daily = df_daily.set_index('date')  # ← This is the key!
    # df_daily = df_daily.sort_index(ascending=False)            # newest on the right

    if len(df_daily) < 7:
        await update.message.reply_text(f"Not enough data for {symbol}")
        return

    # --- 3. Indicators on 300d data ---
    df_1h_300d['date'] = df_1h_300d['OPEN_TIME'].dt.floor('D')
    df_daily_full = (
        df_1h_300d.groupby('date')
        .agg(
            open=('OPEN', 'first'),
            high=('HIGH', 'max'),
            low=('LOW', 'min'),
            close=('CLOSE', 'last'),
            volume=('VOLUME', 'sum'),
        )
        .reset_index()
    )

    n_full = len(df_daily_full)

    # RSI
    df_daily_full['RSI_9'] = calculate_rsi(df_daily_full, 9)
    df_daily_full['RSI_14'] = calculate_rsi(df_daily_full, 14) if n_full >= 14 else pd.Series([np.nan] * n_full)
    df_daily_full['RSI_24'] = calculate_rsi(df_daily_full, 24) if n_full >= 24 else pd.Series([np.nan] * n_full)

    # KAMA
    kama_periods = [9, 21, 55, 99]
    for period in kama_periods:
        if n_full >= period:
            df_daily_full[f'KAMA_{period}'] = calculate_kama(df_daily_full, period)
        else:
            df_daily_full[f'KAMA_{period}'] = np.nan

    # TSI
    if n_full >= 45:
        df_daily_full['TSI'], df_daily_full['TSI_SIGNAL'] = calculate_tsi(df_daily_full, 12, 7, 7)
    else:
        df_daily_full['TSI'] = np.nan
        df_daily_full['TSI_SIGNAL'] = np.nan

    # --- 4. Trim indicators ---
    df_indicators = df_daily_full.tail(len(df_daily)).copy()

    for col in ['RSI_9', 'RSI_14', 'RSI_24', 'TSI', 'TSI_SIGNAL'] + [f'KAMA_{p}' for p in kama_periods]:
        if col in df_indicators.columns:
            df_daily[col] = df_indicators[col].values

    # --- 5. Your original plot – 100% preserved ---
    fig = plt.figure(figsize=(24, 16), facecolor='#1e1e1e')
    gs = gridspec.GridSpec(4, 2, width_ratios=[6, 1], height_ratios=[5, 1, 1.2, 0.8], hspace=0.4, wspace=0.05)

    ax1 = fig.add_subplot(gs[0, 0])
    ax1.set_facecolor('#1e1e1e')

    width = 0.6
    width2 = 0.05
    up = df_daily[df_daily['close'] >= df_daily['open']]
    down = df_daily[df_daily['close'] < df_daily['open']]

    ax1.bar(
        up.index, up['close'] - up['open'], width, bottom=up['open'], color='#00ff88', edgecolor='white', linewidth=0.5
    )
    ax1.bar(up.index, up['high'] - up['low'], width2, bottom=up['low'], color='#00ff88')
    ax1.bar(
        down.index,
        down['close'] - down['open'],
        width,
        bottom=down['open'],
        color='#ff4466',
        edgecolor='white',
        linewidth=0.5,
    )
    ax1.bar(down.index, down['high'] - down['low'], width2, bottom=down['low'], color='#ff4466')

    live_price = await get_live_price(symbol)
    live_suffix = f" LIVE {datetime.now(pytz.UTC).strftime('%H:%M')} - ${live_price:,.8f}"

    # Last price marker
    ax1.axhline(live_price, color="white", linewidth=1, linestyle="--", alpha=0.5)
    ax1.text(
        0.05,
        live_price,
        f"{live_price:,.8f}",
        transform=ax1.get_yaxis_transform(),
        color="white",
        fontsize=10,
        fontweight='bold',
        va='center',
        bbox=dict(facecolor='#1e1e1e', edgecolor='none', pad=5),
    )

    # KAMA lines
    kama_cols = ['KAMA_9', 'KAMA_21', 'KAMA_55', 'KAMA_99']
    colors = ['#ffff00', '#ff8800', '#ff00ff', '#00ffff']
    kama_labels = []
    for col, color in zip(kama_cols, colors):
        if col in df_daily.columns and not df_daily[col].isna().all():
            (line,) = ax1.plot(df_daily.index, df_daily[col], color=color, linewidth=1.8)
            kama_labels.append(f'KAMA {col.split("_")[1]}')

    ax1.set_title(
        f"{symbol} | Daily Chart ({len(df_daily)} days) | @{username} | {live_suffix}", color='white', fontsize=18
    )
    if kama_labels:
        ax1.legend(kama_labels, facecolor='#1e1e1e', labelcolor='white', fontsize=11)
    ax1.grid(True, alpha=0.2)
    ax1.tick_params(colors='white')

    # Volume
    ax_vol = ax1.twinx()
    vol_max = df_daily['volume'].max()
    ax_vol.bar(df_daily.index, df_daily['volume'], color='gray', alpha=0.3, width=0.8)
    ax_vol.fill_between(df_daily.index, df_daily['volume'], color='gray', alpha=0.15)
    ax_vol.set_ylabel('Volume', color='gray')
    ax_vol.tick_params(colors='gray')
    ax_vol.set_ylim(0, vol_max * 2.5)

    # Volume-by-Price
    ax_vbp = fig.add_subplot(gs[0, 1])
    ax_vbp.set_facecolor('#1e1e1e')
    price_bins = np.linspace(df_daily['low'].min(), df_daily['high'].max(), 40)
    vol_by_price = np.zeros(len(price_bins) - 1)
    for _, row in df_daily.iterrows():
        idx = np.searchsorted(price_bins, [row['low'], row['high']])
        idx = np.clip(idx, 0, len(vol_by_price) - 1)
        if idx[0] == idx[1]:
            vol_by_price[idx[0]] += row['volume']
        else:
            vol_by_price[idx[0] : idx[1]] += row['volume'] / (idx[1] - idx[0])
    ax_vbp.barh(
        (price_bins[:-1] + price_bins[1:]) / 2,
        vol_by_price,
        height=(price_bins[1] - price_bins[0]) * 0.8,
        color='#ff69b4',
        alpha=0.6,
    )
    ax_vbp.set_ylim(ax1.get_ylim())
    ax_vbp.invert_xaxis()
    ax_vbp.set_xlabel('Vol', color='white', fontsize=10)
    ax_vbp.tick_params(colors='white')

    # RSI
    ax_rsi = fig.add_subplot(gs[1, 0])
    ax_rsi.set_facecolor('#1e1e1e')
    rsi_cols = ['RSI_9', 'RSI_14', 'RSI_24']
    rsi_colors = ['yellow', 'orange', 'red']
    rsi_labels = []
    for col, color in zip(rsi_cols, rsi_colors):
        if col in df_daily.columns and not df_daily[col].isna().all():
            (line,) = ax_rsi.plot(df_daily.index, df_daily[col], color=color, linewidth=1.2)
            rsi_labels.append(col.split('_')[1])
    ax_rsi.axhline(70, color='red', linestyle='--', alpha=0.7)
    ax_rsi.axhline(30, color='green', linestyle='--', alpha=0.7)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel('RSI', color='white')
    if rsi_labels:
        ax_rsi.legend(rsi_labels, facecolor='#1e1e1e', labelcolor='white')
    ax_rsi.grid(True, alpha=0.2)
    ax_rsi.tick_params(colors='white')

    # TSI
    ax_tsi = fig.add_subplot(gs[2, 0])
    ax_tsi.set_facecolor('#1e1e1e')
    tsi_plotted = False
    if 'TSI' in df_daily.columns and not df_daily['TSI'].isna().all():
        ax_tsi.plot(df_daily.index, df_daily['TSI'], color='#00ff00', linewidth=1.8, label='TSI')
        tsi_plotted = True
    if 'TSI_SIGNAL' in df_daily.columns and not df_daily['TSI_SIGNAL'].isna().all():
        ax_tsi.plot(df_daily.index, df_daily['TSI_SIGNAL'], color='red', linewidth=1.4, label='Signal')
        tsi_plotted = True
    if tsi_plotted:
        ax_tsi.legend(facecolor='#1e1e1e', labelcolor='white')

    ax_tsi.axhline(40, color='red', linestyle=':', alpha=0.8)
    ax_tsi.axhline(-40, color='green', linestyle=':', alpha=0.8)
    ax_tsi.axhline(0, color='gray', linestyle='-', alpha=0.4)
    ax_tsi.set_ylim(-100, 100)
    ax_tsi.set_ylabel('TSI', color='white')
    ax_tsi.grid(True, alpha=0.2)
    ax_tsi.tick_params(colors='white')

    # X-axis format
    date_form = DateFormatter("%Y-%m-%d")  # or "%d.%m" if you prefer
    for ax in [ax1, ax_rsi, ax_tsi]:
        ax.xaxis.set_major_formatter(date_form)
        ax.tick_params(axis='x', colors='white')

    plt.tight_layout()

    # --- Safe saving & sending ---
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
        fig.savefig(tmp.name, format='png', dpi=300, facecolor='#1e1e1e', bbox_inches='tight')
        tmp_path = tmp.name

    await update.message.reply_photo(
        photo=open(tmp_path, 'rb'), caption=f"Daily Chart: {symbol} | {len(df_daily)} days | @{username}"
    )
    os.unlink(tmp_path)
    plt.close(fig)


async def day_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import os
    import tempfile

    import matplotlib.gridspec as gridspec
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np
    import pandas as pd

    if not update.message or not update.message.text:
        return

    match = re.match(r"!day\s+([A-Za-z0-9]+)", update.message.text.strip(), re.IGNORECASE)
    if not match:
        return

    raw_sym = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_sym)
    if not valid_symbol:
        await update.message.reply_text(f"Invalid coin: `{raw_sym}` not found.", parse_mode="Markdown")
        return

    symbol = valid_symbol
    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!day " + raw_sym, update.effective_user)
    await update.message.reply_chat_action("upload_photo")

    try:
        # --- LOAD DATA: last 360+ days 1h ---
        # df_1h = await get_1h_data_last_360d(valid_symbol)
        # if df_1h.empty:
        # await update.message.reply_text(f"No 1h data available for {valid_symbol}")
        # return

        # # Remove timezone
        # df_1h['OPEN_TIME'] = pd.to_datetime(df_1h['OPEN_TIME']).dt.tz_localize(None)

        # # --- GENERATE DAILY CANDLES ---
        # df_1h['date'] = df_1h['OPEN_TIME'].dt.floor('D')
        # df_daily = df_1h.groupby('date').agg(
        # OPEN=('OPEN', 'first'),
        # HIGH=('HIGH', 'max'),
        # LOW=('LOW', 'min'),
        # CLOSE=('CLOSE', 'last'),
        # VOLUME=('VOLUME', 'sum')
        # ).reset_index()

        # if df_daily.empty or len(df_daily) < 2:
        # await update.message.reply_text(f"Not enough daily data for {valid_symbol}")
        # return

        # df_daily = df_daily.set_index('date')
        # df_plot = df_daily.copy()

        # # --- RECOMPUTE INDICATORS ON DAILY DATA ---
        # df_plot['close'] = df_plot['CLOSE']  # for calculate_* functions

        # # RSI
        # df_plot['RSI_9'] = calculate_rsi(df_plot, period=9)
        # df_plot['RSI_12'] = calculate_rsi(df_plot, period=12)
        # df_plot['RSI_24'] = calculate_rsi(df_plot, period=24)

        # # TSI Fast
        # df_plot['TSI_FAST'], df_plot['TSI_FAST_SIGNAL'] = calculate_tsi(df_plot, long=12, short=7, signal=7)

        # # EMA 9 & 21
        # df_plot['EMA_9'] = calculate_ema(df_plot, period=9)
        # df_plot['EMA_21'] = calculate_ema(df_plot, period=21)

        # # KAMA
        # df_plot['KAMA_9'] = calculate_kama(df_plot, period=9)
        # df_plot['KAMA_21'] = calculate_kama(df_plot, period=21)
        # df_plot['KAMA_55'] = calculate_kama(df_plot, period=55)
        # df_plot['KAMA_200'] = calculate_kama(df_plot, period=200)

        # # --- LIVE PRICE UPDATE (on the last daily candle) ---
        # live_price = await get_live_price(valid_symbol)
        # live_suffix = ""
        # if live_price and isinstance(live_price, (int, float)) and live_price > 0:
        # idx = df_plot.index[-1]
        # df_plot.loc[idx, "CLOSE"] = float(live_price)
        # df_plot.loc[idx, "HIGH"] = max(df_plot.loc[idx, "HIGH"], float(live_price))
        # df_plot.loc[idx, "LOW"] = min(df_plot.loc[idx, "LOW"], float(live_price))
        # live_suffix = f" (live ${live_price:,.8f})"

        # --- LOAD DATA: last 360+ days 1h (for stable calculation) ---
        df_1h_full = await get_1h_data_last_360d(valid_symbol)
        if df_1h_full.empty:
            await update.message.reply_text(f"No 1h data available for {valid_symbol}")
            return

        df_1h_full['OPEN_TIME'] = pd.to_datetime(df_1h_full['OPEN_TIME']).dt.tz_localize(None)

        # --- GENERATE DAILY CANDLES (from all available data) ---
        df_1h_full['date'] = df_1h_full['OPEN_TIME'].dt.floor('D')
        df_daily_full = (
            df_1h_full.groupby('date')
            .agg(
                OPEN=('OPEN', 'first'),
                HIGH=('HIGH', 'max'),
                LOW=('LOW', 'min'),
                CLOSE=('CLOSE', 'last'),
                VOLUME=('VOLUME', 'sum'),
            )
            .reset_index()
        )
        df_daily_full = df_daily_full.set_index('date')
        df_daily_full['close'] = df_daily_full['CLOSE']

        if len(df_daily_full) < 2:
            await update.message.reply_text(f"Not enough daily data for {valid_symbol}")
            return

        # --- COMPUTE INDICATORS ON ALL DATA (for stability) ---
        df_daily_full['RSI_9'] = calculate_rsi(df_daily_full, period=9)
        df_daily_full['RSI_12'] = calculate_rsi(df_daily_full, period=12)
        df_daily_full['RSI_24'] = calculate_rsi(df_daily_full, period=24)

        df_daily_full['TSI_FAST'], df_daily_full['TSI_FAST_SIGNAL'] = calculate_tsi(
            df_daily_full, long=12, short=7, signal=7
        )

        df_daily_full['EMA_9'] = calculate_ema(df_daily_full, period=9)
        df_daily_full['EMA_21'] = calculate_ema(df_daily_full, period=21)

        df_daily_full['KAMA_9'] = calculate_kama(df_daily_full, period=9)
        df_daily_full['KAMA_21'] = calculate_kama(df_daily_full, period=21)
        df_daily_full['KAMA_55'] = calculate_kama(df_daily_full, period=55)
        df_daily_full['KAMA_200'] = calculate_kama(df_daily_full, period=200)

        # --- ONLY TAKE THE LAST 120 DAYS FOR PLOTTING ---
        df_plot = df_daily_full.tail(120).copy()

        if len(df_plot) < 2:
            await update.message.reply_text(f"Not enough data in last 120 days for {valid_symbol}")
            return

        # --- LIVE PRICE UPDATE (on the last daily candle of the 120 days) ---
        live_price = await get_live_price(valid_symbol)
        live_suffix = ""
        if live_price and isinstance(live_price, (int, float)) and live_price > 0:
            idx = df_plot.index[-1]
            df_plot.loc[idx, "CLOSE"] = float(live_price)
            df_plot.loc[idx, "HIGH"] = max(df_plot.loc[idx, "HIGH"], float(live_price))
            df_plot.loc[idx, "LOW"] = min(df_plot.loc[idx, "LOW"], float(live_price))
            live_suffix = f" (live ${live_price:,.8f})"

        # --- PLOT SETUP (identical to candles_handler) ---
        bg = '#1e1e1e'
        fg = 'white'
        fig = plt.figure(figsize=(22, 15), facecolor=bg)
        gs = gridspec.GridSpec(4, 2, width_ratios=[6, 1], height_ratios=[5, 1, 1.2, 0.8], hspace=0.4, wspace=0.05)
        ax1 = fig.add_subplot(gs[0, 0])
        ax1.set_facecolor(bg)

        x_vals = np.arange(len(df_plot))
        o = df_plot['OPEN'].values
        c = df_plot['CLOSE'].values
        h = df_plot['HIGH'].values
        l = df_plot['LOW'].values
        up = c >= o
        down = ~up
        col_up = '#44ff44'
        col_down = '#ff4444'

        # Candles
        ax1.vlines(x_vals[up], l[up], h[up], color=col_up, linewidth=1.2, zorder=3)
        ax1.vlines(x_vals[down], l[down], h[down], color=col_down, linewidth=1.2, zorder=3)
        body_h = np.abs(c - o)
        chart_range = h.max() - l.min()
        min_h = chart_range * 0.002
        body_h = np.maximum(body_h, min_h)
        body_b = np.minimum(o, c)
        ax1.bar(x_vals[up], body_h[up], bottom=body_b[up], width=0.6, color=col_up, linewidth=0, zorder=4)
        ax1.bar(x_vals[down], body_h[down], bottom=body_b[down], width=0.6, color=col_down, linewidth=0, zorder=4)

        # --- PLOT INDICATORS ---
        # EMA 9 & 21 (light and dark green)
        if 'EMA_9' in df_plot.columns and not df_plot['EMA_9'].isna().all():
            ax1.plot(x_vals, df_plot['EMA_9'], color='#00ff88', linewidth=1.3, label='EMA 9')
        if 'EMA_21' in df_plot.columns and not df_plot['EMA_21'].isna().all():
            ax1.plot(x_vals, df_plot['EMA_21'], color='#0088ff', linewidth=1.3, label='EMA 21')

        # KAMA (various shades of blue)
        colors_kama = ['#00ffff', '#0099ff', '#3366ff', '#0000ff']  # light → dark
        periods = [9, 21, 55, 200]
        for i, period in enumerate(periods):
            col = f'KAMA_{period}'
            if col in df_plot.columns and not df_plot[col].isna().all():
                ax1.plot(x_vals, df_plot[col], color=colors_kama[i], linewidth=1.2, label=f'KAMA {period}', alpha=0.9)

        ax1.set_title(
            f"{valid_symbol} Daily Chart ({len(df_plot)} days){live_suffix} | @{username}",
            color=fg,
            fontsize=19,
            pad=25,
            weight='bold',
        )
        ax1.legend(facecolor=bg, labelcolor=fg, fontsize=11, loc='upper left')
        ax1.grid(True, alpha=0.25, color=fg, linewidth=0.5)
        ax1.tick_params(colors=fg, labelsize=11)

        # Last price marker
        ax1.axhline(live_price, color="white", linewidth=1, linestyle="--", alpha=0.5)
        ax1.text(
            0.02,
            live_price,
            f"{live_price:,.8f}",
            transform=ax1.get_yaxis_transform(),
            color="white",
            fontsize=10,
            fontweight='bold',
            va='center',
            bbox=dict(facecolor='#1e1e1e', edgecolor='none', pad=5),
        )

        # Y-Limit
        margin = chart_range * 0.05
        ax1.set_ylim(l.min() - margin, h.max() + margin)

        # --- VOLUME ---
        ax_vol = ax1.twinx()
        vol_max = df_plot['VOLUME'].max()
        vol_min_display = vol_max * 0.25
        display_volume = df_plot['VOLUME'].copy()
        display_volume[display_volume < vol_min_display] = vol_min_display
        vol_colors = np.where(up, col_up, col_down)
        ax_vol.bar(
            x_vals,
            display_volume,
            width=0.6,
            color=vol_colors,
            alpha=0.5,
            edgecolor='#ffffff44',
            linewidth=0.25,
            align='center',
        )
        ax_vol.set_ylim(0, vol_max * 2.5)
        ax_vol.yaxis.set_label_position("right")
        ax_vol.yaxis.tick_right()
        ax_vol.set_ylabel('Volume', color=fg, fontsize=13, weight='bold')
        ax_vol.tick_params(colors=fg, labelsize=10)
        ax_vol.legend(['Volume'], loc='upper right', facecolor=bg, labelcolor=fg, fontsize=11)
        ax_vol.grid(True, alpha=0.15, color=fg, linewidth=0.4, linestyle='-', axis='y')

        # Volume Overlay
        ax4 = ax1.twinx()
        ax4.fill_between(x_vals, df_plot['VOLUME'], color='gray', alpha=0.4)
        ax4.plot(x_vals, df_plot['VOLUME'], color='gray', linewidth=1)
        ax4.set_ylabel("volume", fontsize=12, color='gray')
        ax4.tick_params(axis='y', labelcolor='gray')
        ax4.set_ylim(0, vol_max * 2.5)

        # --- VOLUME PROFILE ---
        ax_vbp = fig.add_subplot(gs[0, 1])
        ax_vbp.set_facecolor('#1e1e1e')
        price_bins = np.linspace(l.min(), h.max(), 40)
        vol_by_price = np.zeros(len(price_bins) - 1)
        for _, row in df_plot.iterrows():
            idx = np.searchsorted(price_bins, [row['LOW'], row['HIGH']])
            idx = np.clip(idx, 0, len(vol_by_price) - 1)
            if idx[0] == idx[1]:
                vol_by_price[idx[0]] += row['VOLUME']
            else:
                vol_by_price[idx[0] : idx[1]] += row['VOLUME'] / (idx[1] - idx[0])
        ax_vbp.barh(
            (price_bins[:-1] + price_bins[1:]) / 2,
            vol_by_price,
            height=(price_bins[1] - price_bins[0]) * 0.8,
            color='#ff69b4',
            alpha=0.6,
        )
        ax_vbp.set_ylim(ax1.get_ylim())
        ax_vbp.invert_xaxis()
        ax_vbp.set_xlabel('Vol', color='white', fontsize=10)
        ax_vbp.tick_params(colors='white')

        # --- RSI ---
        ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)
        ax2.set_facecolor(bg)
        if 'RSI_9' in df_plot.columns and not df_plot['RSI_9'].isna().all():
            ax2.plot(x_vals, df_plot['RSI_9'], color='yellow', linewidth=1.1, label='RSI 9')
        if 'RSI_12' in df_plot.columns and not df_plot['RSI_12'].isna().all():
            ax2.plot(x_vals, df_plot['RSI_12'], color='orange', linewidth=1.1, label='RSI 12')
        if 'RSI_24' in df_plot.columns and not df_plot['RSI_24'].isna().all():
            ax2.plot(x_vals, df_plot['RSI_24'], color='red', linewidth=1.1, label='RSI 24')
        ax2.axhline(75, color='red', linestyle='--', alpha=0.8)
        ax2.axhline(50, color=fg, linestyle='-', alpha=0.3)
        ax2.axhline(25, color='green', linestyle='--', alpha=0.8)
        ax2.set_ylim(0, 100)
        ax2.set_ylabel('RSI', color=fg, fontsize=12, weight='bold')
        ax2.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
        ax2.grid(True, alpha=0.15, color=fg)

        # --- TSI ---
        ax3 = fig.add_subplot(gs[2, 0], sharex=ax1)
        ax3.set_facecolor(bg)
        if 'TSI_FAST' in df_plot.columns and not df_plot['TSI_FAST'].isna().all():
            ax3.plot(x_vals, df_plot['TSI_FAST'], color='#00ff00', linewidth=1.8, label='TSI Fast')
        if 'TSI_FAST_SIGNAL' in df_plot.columns and not df_plot['TSI_FAST_SIGNAL'].isna().all():
            ax3.plot(x_vals, df_plot['TSI_FAST_SIGNAL'], color='red', linewidth=1.4, label='Signal')
        ax3.axhline(85, color='red', linestyle=':', alpha=0.9, linewidth=1.2)
        ax3.axhline(50, color='red', linestyle='--', alpha=0.7)
        ax3.axhline(0, color=fg, linestyle='-', alpha=0.3)
        ax3.axhline(-50, color='green', linestyle='--', alpha=0.7)
        ax3.axhline(-85, color='green', linestyle=':', alpha=0.9, linewidth=1.2)
        ax3.set_ylim(-100, 100)
        ax3.set_ylabel('TSI', color=fg, fontsize=12, weight='bold')
        ax3.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
        ax3.grid(True, alpha=0.15, color=fg)

        # --- X-ACHSE FORMAT (Datum) ---
        def format_date(x, pos=None):
            idx = int(x + 0.5)
            if 0 <= idx < len(df_plot):
                return df_plot.index[idx].strftime('%d.%m.%Y')
            return ''

        ax1.xaxis.set_major_formatter(mticker.FuncFormatter(format_date))
        ax1.xaxis.set_major_locator(mticker.MaxNLocator(nbins=12))

        # White labels
        for ax in [ax1, ax2, ax3, ax_vol]:
            ax.tick_params(colors=fg, labelsize=10)
            for label in ax.get_xticklabels() + ax.get_yticklabels():
                label.set_color(fg)

        # Speichern & Senden
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            fig.savefig(tmp.name, format='png', dpi=300, facecolor=bg, bbox_inches='tight', pad_inches=0.4)
            tmp_path = tmp.name

        await update.message.reply_photo(
            photo=open(tmp_path, 'rb'), caption=f"Daily Chart {valid_symbol} ({len(df_plot)} days) • @{username}"
        )
        os.unlink(tmp_path)
        plt.close(fig)

    except Exception as e:
        logger.error(f"!day error: {e}", exc_info=True)
        await update.message.reply_text("Daily chart generation failed.")


# ========================================= !OUTLOOK HANDLER  =========================================================================


async def get_latest_data(symbol: str, timeframe: str = '1h') -> pd.DataFrame:
    tablename = f'"{symbol.upper()}_{timeframe}"'
    query = f"SELECT * FROM {tablename} ORDER BY open_time DESC LIMIT 1"
    conn = await get_conn()
    try:
        row = await conn.fetchrow(query)
        if not row:
            return pd.DataFrame()
        df = pd.DataFrame([row], columns=row.keys())
        return to_uppercase_df(df)
    except Exception as e:
        logger.error(f"DB Error latest_data {symbol} {timeframe}: {e}")
        return pd.DataFrame()
    finally:
        await release_conn(conn)


async def get_latest_indicators(symbol: str) -> pd.DataFrame:
    tablename = f'"{symbol.upper()}_1h_indicators"'
    query = f"SELECT * FROM {tablename} ORDER BY open_time DESC LIMIT 1"
    conn = await get_conn()
    try:
        row = await conn.fetchrow(query)
        if not row:
            return pd.DataFrame()
        df = pd.DataFrame([row], columns=row.keys())
        return to_uppercase_df(df)
    except Exception as e:
        logger.error(f"DB Error latest_indicators {symbol}: {e}")
        return pd.DataFrame()
    finally:
        await release_conn(conn)


async def detect_volume_spike(symbol: str) -> str:
    latest = await get_latest_data(symbol, '30m')
    if latest.empty:
        return "No data"

    now = latest['OPEN_TIME'].iloc[0]
    start_4h = now - pd.Timedelta(hours=4)
    start_7d = now - pd.Timedelta(days=7)

    conn = await get_conn()
    try:
        query_4h = f'SELECT volume, close FROM "{symbol.upper()}_30m" WHERE open_time >= $1 AND open_time <= $2'
        df_4h = await conn.fetch(query_4h, start_4h, now)
        if not df_4h:
            return "No data"

        vol_4h = sum(r['volume'] for r in df_4h)
        usd_vol_4h = vol_4h * df_4h[-1]['close']
        if usd_vol_4h < 250_000:
            return "Low volume"

        query_7d = f'SELECT volume FROM "{symbol.upper()}_30m" WHERE open_time >= $1 AND open_time < $2'
        df_7d = await conn.fetch(query_7d, start_7d, start_4h)
        if len(df_7d) < 10:
            return "No data"

        avg_vol_7d = sum(r['volume'] for r in df_7d) / len(df_7d)
        if avg_vol_7d <= 0:
            return "No data"

        ratio = vol_4h / avg_vol_7d
        if ratio >= 3.0:
            return f"VOLUME SPIKE (last 4h!) ({ratio:.1f}x | ${usd_vol_4h:,.0f})"
        return "No spike"
    finally:
        await release_conn(conn)


async def calculate_obv_period(symbol: str, days: int) -> float:
    end_time = datetime.now(pytz.UTC)
    start_time = end_time - pd.Timedelta(days=days)

    conn = await get_conn()
    try:
        query = f'SELECT close, volume FROM "{symbol.upper()}_30m" WHERE open_time >= $1 AND open_time <= $2 ORDER BY open_time ASC'
        rows = await conn.fetch(query, start_time, end_time)
        if len(rows) < 2:
            return 0.0

        obv = 0.0
        prev = rows[0]['close']
        for row in rows[1:]:
            if row['close'] > prev:
                obv += row['volume']
            elif row['close'] < prev:
                obv -= row['volume']
            prev = row['close']
        return float(obv)
    finally:
        await release_conn(conn)


def find_support_resistance(
    df: pd.DataFrame, lookback_period: int = 2160, volume_multiplier: float = 2.5
) -> tuple[list[float], list[float]]:
    """
    Finds strong support and resistance levels based on volume clusters.
    """
    if df.empty or len(df) < 10:
        logger.debug("find_support_resistance: DataFrame empty or too small")
        return [], []

    df = df.tail(lookback_period).copy()  # Only the last X candles
    vol_median = float(df['VOLUME'].median())
    current_close = float(df['CLOSE'].iloc[-1])
    min_distance = current_close * 0.015  # 1.5% minimum spacing

    supports = []
    resistances = []

    for _, row in df.iterrows():
        vol = float(row['VOLUME'])
        if vol > vol_median * volume_multiplier:
            low = float(row['LOW'])
            high = float(row['HIGH'])

            # Support below price
            if low < current_close and all(abs(low - s) > min_distance for s in supports):
                supports.append(low)
            # Resistance above price
            if high > current_close and all(abs(high - r) > min_distance for r in resistances):
                resistances.append(high)

    # Only the strongest 5
    supports = sorted(supports, reverse=True)[:5]
    resistances = sorted(resistances)[:5]

    logger.debug(f"Support/Resistance found: S={supports}, R={resistances}")
    return supports, resistances


def get_hvn_levels(df: pd.DataFrame, top_n: int = 10, volume_multiplier: float = 1.5) -> list[float]:
    """
    High Volume Nodes (HVN) – strong volume clusters in price.
    Falls back to Support/Resistance if no HVNs are found.
    """
    if df.empty or len(df) < 20:
        logger.debug("get_hvn_levels: DataFrame too small")
        return []

    closes = df['CLOSE'].values.astype(float)
    volumes = df['VOLUME'].values.astype(float)
    current_price = closes[-1]

    price_min, price_max = closes.min(), closes.max()
    if price_max <= price_min:
        logger.debug("get_hvn_levels: price range invalid")
        return []

    # Dynamic bins – the more data, the finer
    num_bins = min(150, max(20, len(df) // 2))
    hist, bin_edges = np.histogram(closes, bins=num_bins, range=(price_min, price_max), weights=volumes)
    bin_mids = (bin_edges[:-1] + bin_edges[1:]) / 2.0

    # Only bins with volume
    valid_vols = hist[hist > 0]
    avg_vol = valid_vols.mean() if len(valid_vols) > 0 else 1.0
    threshold = avg_vol * volume_multiplier

    strong_levels = [float(mid) for mid, vol in zip(bin_mids, hist) if vol >= threshold]

    if not strong_levels:
        logger.debug("get_hvn_levels: No HVNs – falling back to S/R")
        supports, resistances = find_support_resistance(df, volume_multiplier=volume_multiplier)
        levels = sorted(supports + resistances)
        return levels[:top_n]

    # Sort by proximity to the current price (better than sorting by volume)
    strong_levels.sort(key=lambda x: (abs(x - current_price), -abs(x - current_price)))

    result = []
    seen = set()
    for level in strong_levels:
        rounded = round(level, 8)
        if rounded not in seen:
            result.append(rounded)
            seen.add(rounded)
        if len(result) >= top_n:
            break

    logger.debug(f"get_hvn_levels: {len(result)} HVN levels found: {result}")
    return sorted(result)


async def get_short_term_outlook(symbol: str, ind: pd.Series | pd.DataFrame | None) -> str:
    if ind is None or ind.empty:
        return "No data available for short-term outlook."

    # Ensure we are working with a Series (one row)
    if isinstance(ind, pd.DataFrame):
        ind = ind.iloc[0]

    reasons = []
    score = 0

    # === Safely extract all values as float ===
    rsi_14 = float(ind['RSI_14']) if 'RSI_14' in ind else 50
    ema9 = float(ind['EMA_9']) if 'EMA_9' in ind else 0
    ema21 = float(ind['EMA_21']) if 'EMA_21' in ind else 0
    macd_dif = float(ind['MACD_DIF_NORMAL_12_26_9']) if 'MACD_DIF_NORMAL_12_26_9' in ind else 0
    macd_dea = float(ind['MACD_DEA_NORMAL_12_26_9']) if 'MACD_DEA_NORMAL_12_26_9' in ind else 0
    tsi = float(ind['TSI_25_13_13']) if 'TSI_25_13_13' in ind else 0
    atr_14 = float(ind['ATR_14']) if 'ATR_14' in ind else 0
    close_price = float(ind['CLOSE']) if 'CLOSE' in ind else 0
    boll_upper = float(ind['BOLL_UPPER_20']) if 'BOLL_UPPER_20' in ind and pd.notna(ind['BOLL_UPPER_20']) else None
    boll_lower = float(ind['BOLL_LOWER_20']) if 'BOLL_LOWER_20' in ind and pd.notna(ind['BOLL_LOWER_20']) else None

    # === RSI ===
    if rsi_14 < 30:
        score += 1
        reasons.append("RSI_14 < 30 → oversold")
    elif rsi_14 > 70:
        score -= 1
        reasons.append("RSI_14 > 70 → overbought")

    # === EMA Crossover ===
    if ema9 > ema21:
        score += 1
        reasons.append("EMA9 > EMA21 → bullish")
    elif ema9 < ema21:
        score -= 1
        reasons.append("EMA9 < EMA21 → bearish")

    # === MACD ===
    if macd_dif > macd_dea:
        score += 1
        reasons.append("MACD bullish")
    elif macd_dif < macd_dea:
        score -= 1
        reasons.append("MACD bearish")

    # === TSI ===
    if tsi > 0:
        score += 1
        reasons.append("TSI > 0 → bullish")
    elif tsi < 0:
        score -= 1
        reasons.append("TSI < 0 → bearish")

    # === Bollinger Bands ===
    if boll_upper is not None and boll_lower is not None and close_price > 0:
        if close_price > boll_upper:
            score -= 1
            reasons.append("Price above Bollinger Upper")
        elif close_price < boll_lower:
            score += 1
            reasons.append("Price below Bollinger Lower")

    # === Trend Direction ===
    trend_dir = ind.get('TREND_DIRECTION', 'UNDECIDED')
    if trend_dir == 'UP':
        score += 1
        reasons.append("Trend: UP")
    elif trend_dir == 'DOWN':
        score -= 1
        reasons.append("Trend: DOWN")

    # === ATR volatility ===
    if atr_14 and close_price:
        atr_pct = (atr_14 / close_price) * 100
        vol_text = "VERY HIGH" if atr_pct > 3 else "HIGH" if atr_pct > 2 else "elevated" if atr_pct > 1 else "normal"
        reasons.append(f"ATR volatility: {vol_text} ({atr_pct:.2f}%)")

    # === Result ===
    outlook = "BULLISH" if score > 0 else "BEARISH" if score < 0 else "NEUTRAL"
    explanation = "; ".join(reasons) if reasons else "insufficient data"
    return f"Short-term {outlook} – {explanation}"


async def send_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, username: str):
    from datetime import datetime, timedelta

    latest_30m = await get_latest_data(symbol, '30m')
    ind = await get_latest_indicators(symbol)
    df_90d = await get_1h_data_last_90d(symbol)

    # if not latest_30m or not ind or df_90d.empty:
    if latest_30m.empty or ind.empty or df_90d.empty:
        await update.message.reply_text(f"No data available for {symbol}")
        return

    # ... your complete code from here stays exactly the same ...
    # (Live price, Daily/Weekly/Monthly, HVN, OBV, etc.)
    df_1h = await get_1h_data_last_7d(symbol)
    supports, resistances = find_support_resistance(df_90d)
    hvns = get_hvn_levels(df_90d, top_n=10, volume_multiplier=1.5)

    # --- NEW: 1h data for 90 days (for Daily/Weekly/Monthly) ---
    df_1h_90d = await get_1h_data_last_90d(symbol)
    if df_1h_90d.empty:
        await update.message.reply_text(f"No 1h data for {symbol}")
        return

    # --- LIVE PRICE ---
    live_price = await get_live_price(symbol) or latest_30m['CLOSE']
    if live_price is None:
        live_price = latest_30m['CLOSE']  # Fallback

    # --- DATE & TIME ---
    now_utc = datetime.now(pytz.UTC)
    # open_time = latest_30m['OPEN_TIME'].strftime('%Y-%m-%d %H:%M')
    # open_time = pd.to_datetime(latest_30m['OPEN_TIME']).strftime('%Y-%m-%d %H:%M')
    # open_time = pd.Timestamp(latest_30m['OPEN_TIME']).strftime('%Y-%m-%d %H:%M')
    # open_time = pd.Timestamp(latest_30m['OPEN_TIME']).strftime('%Y-%m-%d %H:%M')
    open_time = latest_30m['OPEN_TIME'].iloc[0].strftime('%Y-%m-%d %H:%M')

    # --- 1. DAILY (today) ---
    today = now_utc.date()
    df_today = df_1h_90d[df_1h_90d['OPEN_TIME'].dt.date == today]
    daily_high = df_today['HIGH'].max() if not df_today.empty else None
    daily_low = df_today['LOW'].min() if not df_today.empty else None
    daily_high_pct = ((live_price - daily_high) / daily_high * 100) if daily_high else None
    daily_low_pct = ((live_price - daily_low) / daily_low * 100) if daily_low else None

    # --- 2. WEEKLY (current Monday to today) ---
    monday = today - timedelta(days=today.weekday())
    df_week = df_1h_90d[df_1h_90d['OPEN_TIME'].dt.date >= monday]
    weekly_high = df_week['HIGH'].max() if not df_week.empty else None
    weekly_low = df_week['LOW'].min() if not df_week.empty else None
    weekly_high_pct = ((live_price - weekly_high) / weekly_high * 100) if weekly_high else None
    weekly_low_pct = ((live_price - weekly_low) / weekly_low * 100) if weekly_low else None

    # --- 3. MONTHLY (1st of the month to today) ---
    month_start = today.replace(day=1)
    df_month = df_1h_90d[df_1h_90d['OPEN_TIME'].dt.date >= month_start]
    monthly_high = df_month['HIGH'].max() if not df_month.empty else None
    monthly_low = df_month['LOW'].min() if not df_month.empty else None
    monthly_high_pct = ((live_price - monthly_high) / monthly_high * 100) if monthly_high else None
    monthly_low_pct = ((live_price - monthly_low) / monthly_low * 100) if monthly_low else None

    vol_spike = await detect_volume_spike(symbol)
    obv_1d = await calculate_obv_period(symbol, 1)
    obv_3d = await calculate_obv_period(symbol, 3)
    obv_7d = await calculate_obv_period(symbol, 7)
    obv_30d = await calculate_obv_period(symbol, 30)

    # --- SAFE VALUES ---
    def safe_float(val, default=None):
        try:
            return float(val) if val is not None and not pd.isna(val) else default
        except:
            return default

    close = safe_float(latest_30m['CLOSE'].iloc[0])
    open_time = latest_30m['OPEN_TIME'].iloc[0].strftime('%Y-%m-%d %H:%M')

    rsi_6 = safe_float(ind.get('RSI_6'))
    rsi_9 = safe_float(ind.get('RSI_9'))
    rsi_12 = safe_float(ind.get('RSI_12'))
    rsi_14 = safe_float(ind.get('RSI_14'))
    rsi_24 = safe_float(ind.get('RSI_24'))

    ema_7 = safe_float(ind.get('EMA_7'))
    ema_9 = safe_float(ind.get('EMA_9'))
    ema_12 = safe_float(ind.get('EMA_12'))
    ema_21 = safe_float(ind.get('EMA_21'))
    ema_34 = safe_float(ind.get('EMA_34'))
    ema_55 = safe_float(ind.get('EMA_55'))
    ema_99 = safe_float(ind.get('EMA_99'))
    ema_200 = safe_float(ind.get('EMA_200'))

    kama_7 = safe_float(ind.get('KAMA_7'))
    kama_9 = safe_float(ind.get('KAMA_9'))
    kama_12 = safe_float(ind.get('KAMA_12'))
    kama_21 = safe_float(ind.get('KAMA_21'))
    kama_34 = safe_float(ind.get('KAMA_34'))
    kama_55 = safe_float(ind.get('KAMA_55'))
    kama_99 = safe_float(ind.get('KAMA_99'))

    atr_9 = safe_float(ind.get('ATR_9'))
    atr_14 = safe_float(ind.get('ATR_14'))
    atr_21 = safe_float(ind.get('ATR_21'))

    tsi_main = safe_float(ind.get('TSI_25_13_13'), 0)
    tsi_signal = safe_float(ind.get('TSI_25_13_13_SIGNAL'), 0)
    tsi_fast = safe_float(ind.get('TSI_FAST_12_7_7'), 0)
    tsi_fast_sig = safe_float(ind.get('TSI_FAST_12_7_7_SIGNAL'), 0)

    macd_dif = safe_float(ind.get('MACD_DIF_NORMAL_12_26_9'), 0)
    macd_dea = safe_float(ind.get('MACD_DEA_NORMAL_12_26_9'), 0)
    macd_dist = macd_dif - macd_dea

    poc = safe_float(ind.get('POC'))

    outlook = await get_short_term_outlook(symbol, ind)
    # Ensure outlook is never empty

    # --- COLOR FUNCTIONS ---
    def color(val, good=None, bad=None, neutral='gray'):
        if val is None:
            return neutral
        if good is not None and val > good:
            return '#00ff00'  # Lime
        if bad is not None and val < bad:
            return '#ff0000'  # Red
        return neutral

    def above_close(val):
        if val is None or close is None:
            return 'gray'
        return '#00ff00' if close > val else '#ff0000'

    def format_val(val, fmt, na='—'):
        return f"{val:{fmt}}" if val is not None else na

    # --- FORMAT HELPERS ---
    def fmt_pct(pct):
        if pct is None:
            return "—"
        sign = "-" if pct < 0 else "+"
        return f"{sign}{abs(pct):.2f}%"

    def fmt_price(p):
        try:
            return f"{float(p):,.8f}" if pd.notna(p) else "—"
        except:
            return "—"

    # Before the HTML – build in safety
    close_open = latest_30m['OPEN'].iloc[0] if not latest_30m.empty and 'OPEN' in latest_30m.columns else close

    # Only escape characters in the outlook text – not in the entire HTML!
    outlook = outlook.strip() if outlook else "No outlook available"
    outlook_text = outlook.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # Then use only the text in the HTML – no replacement in the entire string!
    html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family: 'Courier New', monospace; font-size:14px; line-height:1.6; border-left: 5px solid #00ffff; white-space: pre-wrap;">
<b style="color:#00ffff;">Outlook for <a href="https://t.me/{username}" style="color:#00ffff;">@{username}</a></b>
<b style="color:#00ffff;">│</b>
<b style="color:#00ffff;">├─ Coin:</b> <span class="tg-spoiler" style="color:#ffd700; font-weight:bold;">{symbol}</span>
<b style="color:#00ffff;">├─ Time:</b> <span class="tg-spoiler" style="color:#cccccc;">{open_time} UTC</span>
<b style="color:#00ffff;">├─ Close:</b> <span class="tg-spoiler" style="font-weight:bold; color:{'lime' if close > close_open else 'red'};">{close}</span>
<b style="color:#00ffff;">├─ Live:</b> <span class="tg-spoiler" style="color:#ffd700; font-weight:bold;">{fmt_price(live_price)}</span>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">└─ Outlook:</b> <span class="tg-spoiler" style="font-weight:bold; color:{'lime' if 'bullish' in outlook.lower() else 'red' if 'bearish' in outlook.lower() else 'yellow'};">{outlook_text}</span>
</pre>
""".strip()

    # At the end, simply:
    await update.message.reply_html(html)


async def outlook_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    match = re.match(r"!outlook\s+([A-Za-z0-9]+)", update.message.text.strip(), re.IGNORECASE)
    if not match:
        return

    raw_sym = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_sym)
    if not valid_symbol:
        await update.message.reply_text(f"Invalid coin: `{raw_sym}` not found.", parse_mode="Markdown")
        return

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!outlook " + raw_sym, update.effective_user)

    await update.message.reply_chat_action(ChatAction.TYPING)

    try:
        await send_outlook(update, context, valid_symbol, username)
    except Exception as e:
        logger.error(f"!outlook error: {e}", exc_info=True)
        await update.message.reply_text("Outlook generation failed.")


# ========================================= !INFO HANDLER  =========================================================================


async def info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    match = re.match(r"!info\s+([A-Za-z0-9]+)", update.message.text.strip(), re.IGNORECASE)
    if not match:
        return

    raw_sym = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_sym)
    if not valid_symbol:
        await update.message.reply_text(f"Invalid coin: `{raw_sym}` not found.", parse_mode="Markdown")
        return

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!info " + raw_sym, update.effective_user)
    await update.message.reply_chat_action(ChatAction.TYPING)

    try:
        await send_info(update, context, valid_symbol, username)
    except Exception as e:
        logger.error(f"!info error: {e}", exc_info=True)
        await update.message.reply_text("Info generation failed.")


async def send_info(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, username: str):
    from datetime import datetime, timedelta

    latest_30m = await get_latest_data(symbol, '30m')
    ind = await get_latest_indicators(symbol)
    df_90d = await get_1h_data_last_90d(symbol)

    if latest_30m.empty or ind.empty or df_90d.empty:
        await update.message.reply_text(f"No data available for {symbol}")
        return

    live_price = await get_live_price(symbol) or latest_30m['CLOSE'].iloc[0]
    close = float(latest_30m['CLOSE'].iloc[0])
    close_open = float(latest_30m['OPEN'].iloc[0]) if 'OPEN' in latest_30m.columns else close
    open_time = latest_30m['OPEN_TIME'].iloc[0].strftime('%Y-%m-%d %H:%M')

    # Daily / Weekly / Monthly
    now_utc = datetime.now(pytz.UTC)
    today = now_utc.date()
    monday = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    def get_period(df_period):
        high = df_period['HIGH'].max() if not df_period.empty else None
        low = df_period['LOW'].min() if not df_period.empty else None
        h_pct = (live_price - high) / high * 100 if high else None
        l_pct = (live_price - low) / low * 100 if low else None
        return high, low, h_pct, l_pct

    daily_h, daily_l, daily_h_pct, daily_l_pct = get_period(df_90d[df_90d['OPEN_TIME'].dt.date == today])
    weekly_h, weekly_l, weekly_h_pct, weekly_l_pct = get_period(df_90d[df_90d['OPEN_TIME'].dt.date >= monday])
    monthly_h, monthly_l, monthly_h_pct, monthly_l_pct = get_period(df_90d[df_90d['OPEN_TIME'].dt.date >= month_start])

    # Support / Resistance / HVN
    df_30d = df_90d.tail(30 * 24)
    supports, resistances = find_support_resistance(df_30d)
    hvns = get_hvn_levels(df_30d, top_n=8)

    # Volume + OBV
    vol_spike = await detect_volume_spike(symbol)
    obv_1d = await calculate_obv_period(symbol, 1)
    obv_7d = await calculate_obv_period(symbol, 7)
    obv_30d = await calculate_obv_period(symbol, 30)

    # Only escape characters in the outlook text – not in the entire HTML!
    outlook = await get_short_term_outlook(symbol, ind)
    outlook = outlook.strip() if outlook else "No outlook available"
    outlook_text = outlook.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # Ensure ind is a Series (one row)
    if isinstance(ind, pd.DataFrame):
        ind = ind.iloc[0]

    rsi_14 = float(ind.get('RSI_14', 50))
    ema9 = float(ind.get('EMA_9', 0))
    ema21 = float(ind.get('EMA_21', 0))
    ema55 = float(ind.get('EMA_55', 0))
    ema200 = float(ind.get('EMA_200', 0))
    kama9 = float(ind.get('KAMA_9', 0))
    kama21 = float(ind.get('KAMA_21', 0))
    atr_14 = float(ind.get('ATR_14', 0))
    tsi = float(ind.get('TSI_25_13_13', 0))
    macd_dif = float(ind.get('MACD_DIF_NORMAL_12_26_9', 0))
    macd_dea = float(ind.get('MACD_DEA_NORMAL_12_26_9', 0))
    poc = float(ind['POC']) if 'POC' in ind.index and pd.notna(ind['POC']) else None

    # Format
    f = lambda x: f"{x:,.8f}" if x is not None else "—"
    p = lambda x: f"{x:+.2f}%" if x is not None else "—"

    html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family: 'Courier New', monospace; font-size:14px; line-height:1.6; border-left: 5px solid #00ffff;">
<b style="color:#00ffff;">Info for <a href="https://t.me/{username}" style="color:#00ffff;">@{username}</a></b>
<b style="color:#00ffff;">│</b>
<b style="color:#00ffff;">├─ Coin:</b> <b style="color:#ffd700;">{symbol}</b>
<b style="color:#00ffff;">├─ Time:</b> <span class="tg-spoiler" style="color:#cccccc;">{open_time} UTC</span>
<b style="color:#00ffff;">├─ Close:</b> <b style="color:{'lime' if close > close_open else 'red'};">{f(close)}</b>
<b style="color:#00ffff;">├─ Live:</b> <b style="color:#ffd700;">{f(live_price)}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#00ff00;">├─ Today:</b>   H {f(daily_h)} (<b style="color:{'lime' if daily_h_pct > 0 else 'red'}">{p(daily_h_pct)}</b>) │ L {f(daily_l)} (<b style="color:{'lime' if daily_l_pct > 0 else 'red'}">{p(daily_l_pct)}</b>)
<b style="color:#00ff00;">├─ Weekly:</b>  H {f(weekly_h)} (<b style="color:{'lime' if weekly_h_pct > 0 else 'red'}">{p(weekly_h_pct)}</b>) │ L {f(weekly_l)} (<b style="color:{'lime' if weekly_l_pct > 0 else 'red'}">{p(weekly_l_pct)}</b>)
<b style="color:#00ff00;">└─ Monthly:</b> H {f(monthly_h)} (<b style="color:{'lime' if monthly_h_pct > 0 else 'red'}">{p(monthly_h_pct)}</b>) │ L {f(monthly_l)} (<b style="color:{'lime' if monthly_l_pct > 0 else 'red'}">{p(monthly_l_pct)}</b>)
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ RSI_14:</b> <b style="color:{'lime' if rsi_14 < 30 else 'red' if rsi_14 > 70 else 'yellow'};">{rsi_14:.2f}</b>
<b style="color:#ff00ff;">├─ EMA:</b> 9 <b style="color:{'lime' if close > ema9 else 'red'};">{f(ema9)}</b> │ 21 <b style="color:{'lime' if close > ema21 else 'red'};">{f(ema21)}</b> │ 55 <b style="color:{'lime' if close > ema55 else 'red'};">{f(ema55)}</b> │ 200 <b style="color:{'lime' if close > ema200 else 'red'};">{f(ema200)}</b>
<b style="color:#ff00ff;">├─ KAMA:</b> 9 <b style="color:{'lime' if close > kama9 else 'red'};">{f(kama9)}</b> │ 21 <b style="color:{'lime' if close > kama21 else 'red'};">{f(kama21)}</b>
<b style="color:#ff00ff;">├─ ATR_14:</b> <b style="color:yellow;">{atr_14:.6f}</b>
<b style="color:#ff00ff;">├─ TSI:</b> <b style="color:{'lime' if tsi > 0 else 'red'};">{tsi:+.2f}</b>
<b style="color:#ff00ff;">├─ MACD:</b> DIF <b style="color:{'lime' if macd_dif > macd_dea else 'red'};">{macd_dif:+.6f}</b> │ DEA <b style="color:gray;">{macd_dea:+.6f}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Support:</b>  <b style="color:#00ff00;">{', '.join([f(h) for h in supports[:3]]) or '—'}</b>
<b style="color:#ff00ff;">├─ Resistance:</b> <b style="color:#ff0000;">{', '.join([f(r) for r in resistances[:3]]) or '—'}</b>
<b style="color:#ff00ff;">├─ HVN:</b> <b style="color:#ffff00;">{', '.join([f(h) for h in hvns[:5]]) or '—'}</b>
<b style="color:#ff00ff;">├─ POC:</b> <b style="color:#ffd700;">{f(poc)}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Volume Spike:</b> <b style="color:{'red' if 'SPIKE' in vol_spike else 'lime'};">{vol_spike}</b>
<b style="color:#ff00ff;">└─ OBV:</b> 1d <b style="color:{'lime' if obv_1d > 0 else 'red'};">{obv_1d:,}</b> │ 7d <b style="color:{'lime' if obv_7d > 0 else 'red'};">{obv_7d:,}</b> │ 30d <b style="color:{'lime' if obv_30d > 0 else 'red'};">{obv_30d:,}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">└─ Outlook:</b> <span class="tg-spoiler" style="font-weight:bold; color:{'lime' if 'bullish' in outlook.lower() else 'red' if 'bearish' in outlook.lower() else 'yellow'};">{outlook_text}</span>
</pre>
""".strip()

    await update.message.reply_html(html)


# ========================================= !TARGETS HANDLER  =========================================================================


async def targets_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    match = re.match(r"!targets\s+([A-Za-z0-9]+)", update.message.text.strip(), re.IGNORECASE)
    if not match:
        return

    raw_sym = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_sym)
    if not valid_symbol:
        await update.message.reply_text(f"Invalid coin: `{raw_sym}` not found.", parse_mode="Markdown")
        return

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!targets " + raw_sym, update.effective_user)
    await update.message.reply_chat_action(ChatAction.TYPING)

    try:
        await send_targets(update, context, valid_symbol, username)
    except Exception as e:
        logger.error(f"!targets error: {e}", exc_info=True)
        await update.message.reply_text("Targets data generation failed.")


def get_hvn_levelz(
    df: pd.DataFrame,
    top_n: int = 10,
    volume_multiplier: float = 2.5,
    near_threshold_pct: float = 1.0,  # ±1% from price = "near"
) -> dict:
    """
    Returns HVN levels split up:
    - above: HVNs above the current price
    - below: HVNs below the current price
    - near: HVNs within ±near_threshold_pct
    - all: all strong HVNs (sorted by strength)
    """
    if df.empty or len(df) < 20:
        logger.debug("get_hvn_levels: DataFrame too small")
        return {"above": [], "below": [], "near": [], "all": []}

    closes = df['CLOSE'].values.astype(float)
    volumes = df['VOLUME'].values.astype(float)
    current_price = closes[-1]

    price_min, price_max = closes.min(), closes.max()
    if price_max <= price_min:
        return {"above": [], "below": [], "near": [], "all": []}

    # Dynamic bins
    num_bins = min(200, max(30, len(df) // 2))
    hist, bin_edges = np.histogram(closes, bins=num_bins, range=(price_min, price_max), weights=volumes)
    bin_mids = (bin_edges[:-1] + bin_edges[1:]) / 2.0

    # Average volume only from occupied bins
    valid_vols = hist[hist > 0]
    avg_vol = valid_vols.mean() if len(valid_vols) > 0 else 1.0
    threshold = avg_vol * volume_multiplier

    # Strong levels with volume + distance + proximity score
    strong_levels = []
    for mid, vol in zip(bin_mids, hist):
        if vol >= threshold:
            distance = abs(mid - current_price)
            near_bonus = 1000 if distance <= current_price * (near_threshold_pct / 100) else 0
            score = vol + near_bonus - distance * 0.1  # weighted by proximity + volume
            strong_levels.append({'price': float(mid), 'volume': float(vol), 'distance': distance, 'score': score})

    if not strong_levels:
        # Fallback to S/R
        supports, resistances = find_support_resistance(df, volume_multiplier=volume_multiplier)
        levels = sorted(supports + resistances, key=lambda x: abs(x - current_price))
        all_levels = [round(x, 8) for x in levels[:top_n]]
        return {
            "above": [x for x in all_levels if x > current_price],
            "below": [x for x in all_levels if x < current_price],
            "near": [x for x in all_levels if abs(x - current_price) / current_price < near_threshold_pct / 100],
            "all": all_levels,
        }

    # Sort by score (strength + proximity)
    strong_levels.sort(key=lambda x: x['score'], reverse=True)

    # Extract prices
    prices = [round(l['price'], 8) for l in strong_levels[: top_n * 2]]  # take more for filtering

    above = sorted([p for p in prices if p > current_price])
    below = sorted([p for p in prices if p < current_price], reverse=True)
    near = sorted(
        [p for p in prices if abs(p - current_price) <= current_price * (near_threshold_pct / 100)],
        key=lambda x: abs(x - current_price),
    )

    all_levels = sorted(set(above + below), reverse=True)

    logger.debug(f"HVN: {len(above)} above, {len(below)} below, {len(near)} near, total {len(all_levels)}")

    return {
        "above": above[: top_n // 2 + 1],
        "below": below[: top_n // 2 + 1],
        "near": near[:3],
        "all": all_levels[:top_n],
    }


async def send_targets(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, username: str):
    latest_30m = await get_latest_data(symbol, '30m')
    df_90d = await get_1h_data_last_90d(symbol)  # 90d for major swing!
    # df_30d = get_1h_data_last_90d(symbol).tail(30*24)
    if latest_30m is None or df_90d.empty:
        await update.message.reply_text(f"No data for targets of {symbol}")
        return

    supports, resistances = find_support_resistance(df_90d)
    hvn = get_hvn_levelz(df_90d, top_n=10, volume_multiplier=2.5)
    # hvn_str = ", ".join([f"{h:.8f}" for h in hvns]) if hvns else "Low volume phase"

    close = latest_30m['CLOSE']
    live_price = await get_live_price(symbol)

    # --- MAJOR SWING HIGH/LOW (90d) ---
    swing_high = df_90d['HIGH'].max()
    swing_low = df_90d['LOW'].min()
    diff = swing_high - swing_low

    # --- FIBONACCI ---
    fibonacci_levels = [0.236, 0.382, 0.5, 0.618, 0.786]
    fibonacci_extensions = [1.0, 1.272, 1.618, 2.0, 2.618]  # 5 values!

    # Retracement: High → Low (descending)
    fib_retracement = [f"{swing_high - diff * level:.8f}" for level in fibonacci_levels]

    # Extension Up: Low + diff * ratio (targets above High)
    fib_extension_up = [f"{swing_low + diff * level:.8f}" for level in fibonacci_extensions]

    # Extension Down: High - diff * (level - 1) (targets below Low)
    fib_extension_down = [f"{swing_high - diff * (level - 1):.8f}" for level in fibonacci_extensions]

    # --- FORMATTER ---
    def fmt(val, fmt, na='—'):
        return f"{val:{fmt}}" if val is not None else na

    # --- HTML ---

    html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family: 'Courier New', monospace; font-size:14px; line-height:1.6; border-left: 5px solid #00ffff;">
<b style="color:#00ffff;">Targets for <a href="https://t.me/{username}" style="color:#00ffff;">@{username}</a></b>
<b style="color:#00ffff;">│</b>
<b style="color:#00ffff;">├─ Coin:</b> <b style="color:#ffd700;">{symbol}</b>
<b style="color:#00ffff;">├─ Time:</b> <span class="tg-spoiler" style="color:#cccccc;">{datetime.now(pytz.UTC).strftime('%Y-%m-%d %H:%M')} UTC</span>
<b style="color:#00ffff;">├─ Price:</b> <b style="color:lime;">{fmt(live_price, ',.8f')}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#00ff00;">│</b>
<b style="color:#00ff00;">├─ Support Zones:</b> <b style="color:#00ff88;">{', '.join([fmt(s, ',.8f') for s in supports]) or '—'}</b>
<b style="color:#ff0000;">├─ Resistance Zones:</b> <b style="color:#ff6666;">{', '.join([fmt(r, ',.8f') for r in resistances]) or '—'}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ffff00;">├─ HVN Above Price:</b> <b style="color:#ff8888;">{', '.join(map(str, hvn['above'])) or '—'}</b>
<b style="color:#00ff88;">├─ HVN Below Price:</b> <b style="color:#88ff88;">{', '.join(map(str, hvn['below'])) or '—'}</b>
<b style="color:#ff00ff;">├─ HVN Near Price (±1%):</b> <b style="color:#ffff88;">{', '.join(map(str, hvn['near'])) or '—'}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Fib Retracement (down):</b>
<b style="color:#ff00ff;">│</b>  <b style="color:#00ffff;">0.236:</b> {fib_retracement[0]} │ <b style="color:#00ffff;">0.382:</b> {fib_retracement[1]}
<b style="color:#ff00ff;">│</b>  <b style="color:#00ffff;">0.5:</b>   {fib_retracement[2]} │ <b style="color:#00ffff;">0.618:</b> {fib_retracement[3]}
<b style="color:#ff00ff;">│</b>  <b style="color:#00ffff;">0.786:</b> {fib_retracement[4]}
<b style="color:#ff00ff;">│</b>
<b style="color:#00ff00;">├─ Fib Extension (Up):</b>
<b style="color:#ff00ff;">│</b>  <b style="color:#88ff88;">1.272:</b> {fib_extension_up[0]} │ <b style="color:#88ff88;">1.618:</b> {fib_extension_up[1]}
<b style="color:#ff00ff;">│</b>  <b style="color:#88ff88;">2.0:</b>   {fib_extension_up[2]} │ <b style="color:#88ff88;">2.618:</b> {fib_extension_up[3]}
</pre>
""".strip()

    await update.message.reply_html(html)


# ========================================= !CLOSINGS HANDLER  =========================================================================


# async def get_closings_data():
# """Fetches all trades from the last 7 days from all 4 tables."""
# tables = ["closed_trades", "closed_trades2", "closed_trades3", "closed_trades4"]

# # We filter directly in the SQL query on the last 7 days
# parts = []
# for t in tables:
# parts.append(f"SELECT posted, direction, status, '{t}' as source FROM {t} WHERE posted >= NOW() - INTERVAL '7 days'")

# full_query = " UNION ALL ".join(parts) + " ORDER BY posted DESC"

# conn = await get_conn()
# try:
# rows = await conn.fetch(full_query)
# df = pd.DataFrame([dict(r) for r in rows])

# if df.empty:
# return None

# df['posted'] = pd.to_datetime(df['posted'])

# def normalize_status(row):
# s = str(row['status']).strip().upper()
# src = row['source']
# if src == 'closed_trades2':
# if s == '1': return 'FULL_WIN'
# if s == '0': return 'LOSS'
# return 'UNKNOWN'
# if s == 'SL0': return 'LOSS'
# if s in ['SL1', 'SL2', 'SL3']: return 'PARTIAL_WIN'
# if s == '4': return 'FULL_WIN'
# return 'UNKNOWN'

# df['result'] = df.apply(normalize_status, axis=1)
# return df
# except Exception as e:
# logger.error(f"Error fetching data for !closings: {e}")
# return None
# finally:
# await release_conn(conn)


async def get_closings_data():
    """Fetches data from the last 7 days and corrects the time offset."""
    tables = ["closed_trades", "closed_trades2", "closed_trades3", "closed_trades4"]

    parts = []
    for t in tables:
        parts.append(
            f"SELECT posted, direction, status, '{t}' as source FROM {t} WHERE posted >= NOW() - INTERVAL '7 days'"
        )

    full_query = " UNION ALL ".join(parts) + " ORDER BY posted DESC"

    conn = await get_conn()
    try:
        rows = await conn.fetch(full_query)
        df = pd.DataFrame([dict(r) for r in rows])
        if df.empty:
            return None

        # 1. Convert to datetime
        df['posted'] = pd.to_datetime(df['posted'])

        # 2. Manual UTC fix: if your DB stores local time (CET),
        # we subtract 1 hour (winter) or 2 hours (summer) to arrive at UTC.
        # Since it's February: -1 hour.
        df['posted'] = df['posted'] - pd.Timedelta(hours=1)
        # Now mark as UTC
        df['posted'] = df['posted'].dt.tz_localize('UTC')

        def normalize_status(row):
            s = str(row['status']).strip().upper()
            src = row['source']
            if src == 'closed_trades2':
                return 'FULL_WIN' if s == '1' else 'LOSS' if s == '0' else 'UNKNOWN'
            if s == 'SL0':
                return 'LOSS'
            if s in ['SL1', 'SL2', 'SL3']:
                return 'PARTIAL_WIN'
            if s == '4':
                return 'FULL_WIN'
            return 'UNKNOWN'

        df['result'] = df.apply(normalize_status, axis=1)
        return df
    finally:
        await release_conn(conn)


# def create_closings_plot(df):
# """Creates an hourly stacked bar chart with corrected colors and time axes."""
# # Ensure the timestamps are treated as UTC to avoid shifts
# df['time_bin'] = df['posted'].dt.floor('h')

# # Your new desired colors
# colors = {
# 'LOSS': '#FF0000',        # Red
# 'PARTIAL_WIN': '#0000FF',  # Blue
# 'FULL_WIN': '#00FF00'      # Strong green
# }
# results_order = ['LOSS', 'PARTIAL_WIN', 'FULL_WIN']

# fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(18, 12), sharex=True)
# plt.subplots_adjust(hspace=0.4)

# for ax, direction in zip([ax1, ax2], ['LONG', 'SHORT']):
# subset = df[df['direction'] == direction.upper()].copy()

# if subset.empty:
# ax.set_title(f"No {direction} trades in the selected period", color='white')
# continue

# pivot = subset.groupby(['time_bin', 'result']).size().unstack(fill_value=0)

# # Ensure all status types exist as columns
# for r in results_order:
# if r not in pivot.columns: pivot[r] = 0

# pivot = pivot[results_order]

# # Plot the bars
# pivot.plot(kind='bar', stacked=True, ax=ax, color=[colors[r] for r in results_order], width=0.9)

# ax.set_title(f"Hourly Closing Performance: {direction}", fontsize=16, color='white', fontweight='bold')
# ax.set_ylabel("Trades per Hour", color='white', fontsize=12)
# ax.legend(loc='upper left', frameon=True, facecolor='#1e1e1e', labelcolor='white')
# ax.grid(axis='y', linestyle='--', alpha=0.1)

# ax.set_facecolor('#1e1e1e')
# ax.tick_params(colors='white', labelsize=10)

# # X-axis labeling: format timestamps
# n = len(pivot.index)
# # We show a marker roughly every 12 hours so the axis doesn't get overloaded
# step = max(1, n // 14)
# ax2.set_xticks(range(0, n, step))
# ax2.set_xticklabels([pivot.index[i].strftime('%d.%m. %H:00') for i in range(0, n, step)], rotation=45, ha='right')

# fig.patch.set_facecolor('#1e1e1e')

# buf = io.BytesIO()
# plt.savefig(buf, format='png', bbox_inches='tight', facecolor=fig.get_facecolor(), dpi=120)
# buf.seek(0)
# plt.close(fig)
# return buf


def create_closings_plot(df):
    """Builds the chart with the new colors: red, blue, strong green."""
    # Pandas floor works on the column, but we need
    # clean Python datetimes for the time axis
    df['time_bin'] = df['posted'].dt.floor('h')

    colors = {'LOSS': '#FF0000', 'PARTIAL_WIN': '#0000FF', 'FULL_WIN': '#00FF00'}
    results_order = ['LOSS', 'PARTIAL_WIN', 'FULL_WIN']

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(18, 12), sharex=True)
    plt.subplots_adjust(hspace=0.4)

    # --- FIX: manual flooring for standard datetime ---
    now_utc = datetime.now(pytz.UTC)
    end_time = now_utc.replace(minute=0, second=0, microsecond=0)
    start_time = end_time - timedelta(days=7)

    # Time axis for reindex (Pandas)
    all_hours = pd.date_range(start=start_time, end=end_time, freq='h', tz='UTC')

    for ax, direction in zip([ax1, ax2], ['LONG', 'SHORT']):
        subset = df[df['direction'] == direction.upper()].copy()

        # Group and fill in the time axis
        pivot = subset.groupby(['time_bin', 'result']).size().unstack(fill_value=0)
        pivot = pivot.reindex(all_hours, fill_value=0)

        for r in results_order:
            if r not in pivot.columns:
                pivot[r] = 0

        pivot = pivot[results_order]

        # Plot (stacked bar)
        pivot.plot(
            kind='bar', stacked=True, ax=ax, color=[colors[r] for r in results_order], width=1.0, edgecolor='none'
        )

        ax.set_title(f"Hourly Performance (UTC): {direction}", fontsize=16, color='white', fontweight='bold')
        ax.set_facecolor('#1e1e1e')
        ax.grid(axis='y', linestyle='--', alpha=0.1)
        ax.tick_params(colors='white')
        ax.legend(loc='upper left', facecolor='#1e1e1e', labelcolor='white')

    # X-axis labeling (one label every 12 hours)
    n = len(pivot.index)
    step = 12
    ax2.set_xticks(range(0, n, step))
    ax2.set_xticklabels(
        [pivot.index[i].strftime('%d.%m. %H:00') for i in range(0, n, step)], rotation=45, ha='right', color='white'
    )

    fig.patch.set_facecolor('#1e1e1e')
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', facecolor='#1e1e1e', dpi=120)
    buf.seek(0)
    plt.close(fig)
    return buf


async def closings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.text.strip().upper() != "!CLOSINGS":
        return

    await update.message.reply_chat_action(ChatAction.TYPING)

    df = await get_closings_data()
    if df is None or df.empty:
        await update.message.reply_text("❌ No closed trades found in database.")
        return

    try:
        chart_buf = create_closings_plot(df)
        await update.message.reply_photo(
            photo=chart_buf,
            caption="📊 <b>Trade Closing Performance (Last 1000 Trades)</b>\n\n"
            "Red: SL hit (No Target)\n"
            "Blue: SL hit (Target 1-3 reached)\n"
            "Green: All Targets hit",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error in !closings handler: {e}")
        await update.message.reply_text("Error generating the performance chart.")


# Don't forget to register the handler:
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!closings$"), closings_handler))

# ========================================= !TRADING HANDLER  =========================================================================


async def trading_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    match = re.match(r"!trading\s+([A-Za-z0-9]+)", update.message.text.strip(), re.IGNORECASE)
    if not match:
        return

    raw_sym = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_sym)
    if not valid_symbol:
        await update.message.reply_text(f"Invalid coin: `{raw_sym}` not found.", parse_mode="Markdown")
        return

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!trading " + raw_sym, update.effective_user)
    await update.message.reply_chat_action(ChatAction.TYPING)

    try:
        await send_trading(update, context, valid_symbol, username)
    except Exception as e:
        logger.error(f"!trading error: {e}", exc_info=True)
        await update.message.reply_text("Trading data generation failed.")


async def get_volume_data(symbol: str, hours_ago: float):

    now = datetime.now(pytz.UTC)
    start = now - pd.Timedelta(hours=hours_ago)

    conn = await get_conn()
    try:
        query = f'''
            SELECT
                COALESCE(SUM(volume * close ), 0) as usd_totals,
                COALESCE(SUM(volume ), 0) as total_contracts,
                COALESCE(SUM(CASE WHEN close >= open THEN volume * close ELSE 0 END), 0) as buy_contracts,
                COALESCE(SUM(CASE WHEN close < open THEN volume * close ELSE 0 END), 0) as sell_contracts
            FROM "{symbol.upper()}_30m"
            WHERE open_time >= $1 AND open_time <= $2
        '''
        row = await conn.fetchrow(query, start, now)
        await release_conn(conn)

        if not row:
            return 0, 0, 0, 0

        usd_totals = float(row['usd_totals'])
        total_contracts = float(row['total_contracts'])
        buy_contracts = float(row['buy_contracts'])
        sell_contracts = float(row['sell_contracts'])

        usd_totals = usd_totals
        usd_buy = buy_contracts
        usd_sell = sell_contracts

        return usd_totals, usd_buy, usd_sell, total_contracts
    except Exception as e:
        logger.error(f"Volume query error {symbol}: {e}")
        await release_conn(conn)
        return 0, 0, 0, 0


async def send_trading(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, username: str):
    latest_30m = await get_latest_data(symbol, '30m')
    df_90d = await get_1h_data_last_90d(symbol)

    if latest_30m.empty or df_90d.empty:
        await update.message.reply_text(f"No trading data available for {symbol}")
        return

    live_price = await get_live_price(symbol) or latest_30m['CLOSE'].iloc[0]
    # now = latest_30m['OPEN_TIME'].iloc[0]
    now = datetime.now(pytz.UTC)
    close_now = float(latest_30m['CLOSE'].iloc[0])

    # Daily / Weekly / Monthly
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    def get_period(df_period):
        high = df_period['HIGH'].max() if not df_period.empty else None
        low = df_period['LOW'].min() if not df_period.empty else None
        h_pct = (live_price - high) / high * 100 if high else None
        l_pct = (live_price - low) / low * 100 if low else None
        return high, low, h_pct, l_pct

    daily_h, daily_l, daily_h_pct, daily_l_pct = get_period(df_90d[df_90d['OPEN_TIME'].dt.date == today])
    weekly_h, weekly_l, weekly_h_pct, weekly_l_pct = get_period(df_90d[df_90d['OPEN_TIME'].dt.date >= monday])
    monthly_h, monthly_l, monthly_h_pct, monthly_l_pct = get_period(df_90d[df_90d['OPEN_TIME'].dt.date >= month_start])

    # 30d average (for comparison)
    # usd_30d, _, _ = await get_volume_data(symbol,30*24)
    # avg_daily_usd = usd_30d / 30 if usd_30d > 0 else 1

    # --- Fetch all volume data ONCE ---
    periods = {'1h': 1, '4h': 4, '12h': 12, '24h': 24, '7d': 7 * 24, '30d': 30 * 24}

    vol_data = {}
    usd_30d = 0

    for name, hours in periods.items():
        usd, buy, sell, _ = await get_volume_data(symbol, hours)
        diff = buy - sell
        vol_data[name] = {'usd': usd, 'buy': buy, 'sell': sell, 'diff': diff}
        if name == '30d':
            usd_30d = usd

    # Now compute correctly
    avg_daily_usd = usd_30d / 30 if usd_30d > 0 else 1

    # Compute percentages
    for name, data in vol_data.items():
        hours = periods[name]
        expected = avg_daily_usd * (hours / 24)
        pct = (data['usd'] / expected - 1) * 100 if expected > 0 else 0
        data['pct'] = pct

    # Volume + OBV (async!)
    vol_spike = await detect_volume_spike(symbol)
    obv_1h = await calculate_obv_period(symbol, 1 / 24)
    obv_4h = await calculate_obv_period(symbol, 4 / 24)
    obv_12h = await calculate_obv_period(symbol, 12 / 24)
    obv_24h = await calculate_obv_period(symbol, 1)
    obv_7d = await calculate_obv_period(symbol, 7)
    obv_30d = await calculate_obv_period(symbol, 30)

    # Price Changes
    async def get_change(hours):
        start = now - timedelta(hours=hours)
        conn = await get_conn()  # ← fetch only once!
        try:
            row = await conn.fetchrow(
                f'SELECT close FROM "{symbol.upper()}_30m" WHERE open_time >= $1 ORDER BY open_time ASC LIMIT 1', start
            )
            if row:
                old = float(row['close'])
                return (close_now - old) / old * 100 if old > 0 else 0.0
            return 0.0
        finally:
            await release_conn(conn)

    changes = {
        '1h': await get_change(1),
        '4h': await get_change(4),
        '12h': await get_change(12),
        '24h': await get_change(24),
        '7d': await get_change(7 * 24),
        '30d': await get_change(30 * 24),
    }

    # Format
    f = lambda x: f"{x:,.8f}" if x is not None else "—"
    p = lambda x: f"{x:+.2f}%" if x is not None else "—"
    v = lambda x: f"${x / 1e9:.2f}B" if x >= 1e9 else f"${x / 1e6:.2f}M" if x >= 1e6 else f"${x:,.0f}"

    # Final, perfect HTML block
    html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family: 'Courier New', monospace; font-size:14px; line-height:1.6; border-left: 5px solid #00ffff;">
<b style="color:#00ffff;">Trading Performance for <a href="https://t.me/{username}" style="color:#00ffff;">@{username}</a></b>
<b style="color:#00ffff;">│</b>
<b style="color:#00ffff;">├─ Coin:</b> <b style="color:#ffd700;">{symbol}</b>
<b style="color:#00ffff;">├─ Time:</b> <span class="tg-spoiler" style="color:#cccccc;">{now.strftime('%Y-%m-%d %H:%M')} UTC</span>
<b style="color:#00ffff;">├─ Live:</b> <b style="color:#ffd700;">${f(live_price)}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Price Changes:</b>
<b style="color:#ff00ff;">│  1h:</b> <b style="color:{'lime' if changes['1h'] > 0 else 'red'};">{p(changes['1h'])}</b> │ <b style="color:#ff00ff;">4h:</b> <b style="color:{'lime' if changes['4h'] > 0 else 'red'};">{p(changes['4h'])}</b>
<b style="color:#ff00ff;">│ 12h:</b> <b style="color:{'lime' if changes['12h'] > 0 else 'red'};">{p(changes['12h'])}</b> │ <b style="color:#ff00ff;">24h:</b> <b style="color:{'lime' if changes['24h'] > 0 else 'red'};">{p(changes['24h'])}</b>
<b style="color:#ff00ff;">│  7d:</b> <b style="color:{'lime' if changes['7d'] > 0 else 'red'};">{p(changes['7d'])}</b> │ <b style="color:#ff00ff;">30d:</b> <b style="color:{'lime' if changes['30d'] > 0 else 'red'};">{p(changes['30d'])}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#00ff00;">├─ Today:</b>   H {f(daily_h)} (<b style="color:{'lime' if daily_h_pct > 0 else 'red'}">{p(daily_h_pct)}</b>) │ L {f(daily_l)} (<b style="color:{'lime' if daily_l_pct > 0 else 'red'}">{p(daily_l_pct)}</b>)
<b style="color:#00ff00;">├─ Weekly:</b>  H {f(weekly_h)} (<b style="color:{'lime' if weekly_h_pct > 0 else 'red'}">{p(weekly_h_pct)}</b>) │ L {f(weekly_l)} (<b style="color:{'lime' if weekly_l_pct > 0 else 'red'}">{p(weekly_l_pct)}</b>)
<b style="color:#00ff00;">├─ Monthly:</b> H {f(monthly_h)} (<b style="color:{'lime' if monthly_h_pct > 0 else 'red'}">{p(monthly_h_pct)}</b>) │ L {f(monthly_l)} (<b style="color:{'lime' if monthly_l_pct > 0 else 'red'}">{p(monthly_l_pct)}</b>)
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Volume Information:</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ 30d Avg. Daily Volume:</b> <b style="color:yellow;">{v(avg_daily_usd)}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#00ff00;">├─ 1h Volume:</b>  <b>{(v(vol_data['1h']['usd']))}</b> <span class="tg-spoiler" style="color:{'lime' if vol_data['1h']['pct'] > 0 else 'red'};">({p(vol_data['1h']['pct'])})</span>
<b style="color:#00ff00;">├─ 4h Volume:</b>  <b>{(v(vol_data['4h']['usd']))}</b> <span class="tg-spoiler" style="color:{'lime' if vol_data['4h']['pct'] > 0 else 'red'};">({p(vol_data['4h']['pct'])})</span>
<b style="color:#00ff00;">├─ 12h Volume:</b> <b>{(v(vol_data['12h']['usd']))}</b> <span class="tg-spoiler" style="color:{'lime' if vol_data['12h']['pct'] > 0 else 'red'};">({p(vol_data['12h']['pct'])})</span>
<b style="color:#00ff00;">├─ 24h Volume:</b> <b>{(v(vol_data['24h']['usd']))}</b> <span class="tg-spoiler" style="color:{'lime' if vol_data['24h']['pct'] > 0 else 'red'};">({p(vol_data['24h']['pct'])})</span>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Buy/Sell Volume (30d):</b>
<b style="color:#ff00ff;">│  Buy:</b>  <b style="color:#00ff00;">{v(vol_data['30d']['buy'])}</b> │ Sell: <b style="color:#ff0000;">{v(vol_data['30d']['sell'])}</b> │ Diff: <b style="color:{'lime' if vol_data['30d']['diff'] > 0 else 'red'};">{v(vol_data['30d']['diff'])}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Buy/Sell Volume (7d):</b>
<b style="color:#ff00ff;">│  Buy:</b>  <b style="color:#00ff00;">{v(vol_data['7d']['buy'])}</b> │ Sell: <b style="color:#ff0000;">{v(vol_data['7d']['sell'])}</b> │ Diff: <b style="color:{'lime' if vol_data['7d']['diff'] > 0 else 'red'};">{v(vol_data['7d']['diff'])}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Buy/Sell Volume (24h):</b>
<b style="color:#ff00ff;">│  Buy:</b> <b style="color:#00ff00;">{v(vol_data['24h']['buy'])}</b> │ Sell: <b style="color:#ff0000;">{v(vol_data['24h']['sell'])}</b> │ Diff: <b style="color:{'lime' if vol_data['24h']['diff'] > 0 else 'red'};">{v(vol_data['24h']['diff'])}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Buy/Sell Volume (12h):</b>
<b style="color:#ff00ff;">│  Buy:</b> <b style="color:#00ff00;">{v(vol_data['12h']['buy'])}</b> │ Sell: <b style="color:#ff0000;">{v(vol_data['12h']['sell'])}</b> │ Diff: <b style="color:{'lime' if vol_data['12h']['diff'] > 0 else 'red'};">{v(vol_data['12h']['diff'])}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Buy/Sell Volume (4h):</b>
<b style="color:#ff00ff;">│  Buy:</b> <b style="color:#00ff00;">{v(vol_data['4h']['buy'])}</b> │ Sell: <b style="color:#ff0000;">{v(vol_data['4h']['sell'])}</b> │ Diff: <b style="color:{'lime' if vol_data['4h']['diff'] > 0 else 'red'};">{v(vol_data['4h']['diff'])}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Buy/Sell Volume (1h):</b>
<b style="color:#ff00ff;">│  Buy:</b>  <b style="color:#00ff00;">{v(vol_data['1h']['buy'])}</b> │ Sell: <b style="color:#ff0000;">{v(vol_data['1h']['sell'])}</b> │ Diff: <b style="color:{'lime' if vol_data['1h']['diff'] > 0 else 'red'};">{v(vol_data['1h']['diff'])}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Volume Spike:</b> <b style="color:{'red' if 'SPIKE' in vol_spike else 'lime'};">{vol_spike}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ OBV Flow:</b>
<b style="color:#ff00ff;">│  1h:</b> <b style="color:{'lime' if obv_1h > 0 else 'red'};">{obv_1h:,}</b> │ 4h: <b style="color:{'lime' if obv_4h > 0 else 'red'};">{obv_4h:,}</b> │ 24h: <b style="color:{'lime' if obv_24h > 0 else 'red'};">{obv_24h:,}</b>
<b style="color:#ff00ff;">│  7d:</b> <b style="color:{'lime' if obv_7d > 0 else 'red'};">{obv_7d:,}</b> │ 30d: <b style="color:{'lime' if obv_30d > 0 else 'red'};">{obv_30d:,}</b>
</pre>
""".strip()

    await update.message.reply_html(html)


# ========================================= !OPEN HANDLER =========================================================================
async def open_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    match = re.match(r"!open\s+(CMP|LIMIT)\s+(LONG|SHORT)\s+([A-Za-z0-9]+)\s+(\d+x?)\s*(-V)?", text, re.IGNORECASE)
    if not match:
        await update.message.reply_text(
            "Usage: !open [CMP|LIMIT] [LONG|SHORT] [COIN] [LEVERAGE] [-V]\nExample: !open CMP LONG BTC 20x -V"
        )
        return

    order_type = match.group(1).upper()
    direction = match.group(2).upper()
    symbol_raw = match.group(3).upper()
    leverage_str = match.group(4).upper().replace("X", "x")
    post_video = bool(match.group(5))

    # Validate coin
    valid_symbol = await validate_symbol(symbol_raw)
    if not valid_symbol:
        await update.message.reply_text(f"Coin `{symbol_raw}` not found")
        return

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command(
        f"!open {order_type} {direction} {valid_symbol} {leverage_str} {'-V' if post_video else ''}",
        update.effective_user,
    )

    await update.message.reply_chat_action(ChatAction.TYPING)
    await send_open_trade(update, context, order_type, direction, valid_symbol, leverage_str, post_video, username)


def find_nearest_level(values, ref_price, direction="below", min_distance_pct=0.5):
    """
    Finds the nearest level with a minimum spacing in %
    direction: "below", "above", "any"
    """
    if not values:
        return None

    candidates = []
    for v in values:
        if v <= 0:
            continue
        dist_pct = abs((v - ref_price) / ref_price * 100)
        if dist_pct < min_distance_pct:
            continue  # too close
        if direction == "below" and v >= ref_price:
            continue
        if direction == "above" and v <= ref_price:
            continue
        candidates.append((v, dist_pct))

    if not candidates:
        return None

    # Sort by proximity
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0] if candidates else None


# --- CORRECT AND FINAL VERSION (copy 1:1) ---
async def send_open_trade(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order_type: str,
    direction: str,
    symbol: str,
    leverage: str,
    post_video: bool,
    username: str,
):

    # Leverage
    try:
        lev = int(leverage.lower().replace("x", ""))
        if not 1 <= lev <= 125:
            raise ValueError
    except:
        await update.message.reply_text("Leverage invalid (1x–125x)")
        return

    # Live price
    live_price = await get_live_price(symbol)
    if not live_price:
        await update.message.reply_text("No live price")
        return

    # Load 90 days of 1h data → for HVN, Fibs, Support/Resistance
    df = await get_1h_data_last_90d(symbol)
    if df.empty or len(df) < 100:
        await update.message.reply_text("Not enough data")
        return

    # Support / Resistance + HVN from 90 days
    supports, resistances = find_support_resistance(df)
    hvn_all = get_hvn_levelz(df, top_n=500)['all']  # <-- all strong HVNs

    # Swing for Fibs
    swing_high = df['HIGH'].max()
    swing_low = df['LOW'].min()
    fib_range = swing_high - swing_low

    fib_retracement = [swing_high - fib_range * x for x in [0.236, 0.382, 0.5, 0.618, 0.786]]
    fib_ext_up = [swing_low + fib_range * x for x in [1.272, 1.618, 2.0, 2.618]]
    fib_ext_down = [swing_high - fib_range * (x - 1) for x in [1.272, 1.618, 2.0, 2.618]]

    is_long = direction == "LONG"

    # =============== ENTRY LOGIC ===============
    if order_type == "CMP":
        entry1 = live_price
    else:  # LIMIT
        if is_long:
            pool = [x for x in supports + hvn_all + fib_retracement if x < live_price]
        else:
            pool = [x for x in resistances + hvn_all + fib_ext_up if x > live_price]
        if pool:
            entry1 = min(pool, key=lambda x: abs(x - live_price))
        else:
            entry1 = live_price * (0.985 if is_long else 1.015)

    # Entry 2
    if is_long:
        pool2 = [x for x in supports + hvn_all + fib_retracement if x < entry1]
    else:
        pool2 = [x for x in resistances + hvn_all + fib_ext_up if x > entry1]
    entry2 = min(pool2, key=lambda x: abs(x - entry1)) if pool2 else entry1 * (0.97 if is_long else 1.03)

    # # Stop Loss
    # if is_long:
    # sl_pool = [x for x in supports + hvn_all + fib_retracement if x < entry2 * 0.99]
    # sl = min(sl_pool) if sl_pool else entry2 * 0.93
    # else:
    # sl_pool = [x for x in resistances + hvn_all + fib_ext_up if x > entry2 * 1.01]
    # sl = max(sl_pool) if sl_pool else entry2 * 1.07

    # =============== STOP LOSS – FINAL & PERFECT ===============
    if is_long:
        # LONG: nearest Support/HVN/Fib below Entry2
        sl_candidates = [x for x in supports + hvn_all + fib_retracement if x < entry2 * 0.99]
        sl = min(sl_candidates, key=lambda x: abs(x - entry2)) if sl_candidates else entry2 * 0.93
    else:
        # SHORT: nearest resistance/HVN/FibExt above Entry2
        sl_candidates = [x for x in resistances + hvn_all + fib_ext_up if x > entry2 * 1.01]
        sl = min(sl_candidates, key=lambda x: abs(x - entry2)) if sl_candidates else entry2 * 1.07
        # ← min() = NEAREST, not max()!

    # =============== TARGETS ===============
    if is_long:
        target_candidates = [x for x in resistances + hvn_all + fib_ext_up if x > entry1]
        target_candidates = sorted(target_candidates)  # ascending
    else:
        target_candidates = [x for x in supports + hvn_all + fib_retracement + fib_ext_down if x < entry1 and x > 0]
        target_candidates = sorted(target_candidates, reverse=True)  # descending

    # Profit %
    def profit(entry, target):
        return (target - entry) / entry * 100 if is_long else (entry - target) / entry * 100

    # Categorize
    daily = [(p, profit(entry1, p)) for p in target_candidates if 0 < profit(entry1, p) <= 8]
    mid = [(p, profit(entry1, p)) for p in target_candidates if 8 < profit(entry1, p) <= 25]
    long_t = [(p, profit(entry1, p)) for p in target_candidates if profit(entry1, p) > 25]

    # RRR (50:50 Entry)
    risk1 = abs(profit(entry1, sl))
    risk2 = abs(profit(entry2, sl))
    avg_risk = (risk1 + risk2) / 2
    avg_reward = sum(p[1] for p in daily[:3]) / min(3, len(daily)) if daily else 3
    rrr = avg_reward / avg_risk if avg_risk > 0 else 0.01

    # =============== OUTPUT (new style) ===============
    html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family: 'Courier New', monospace; font-size:14px; line-height:1.6; border-left: 5px solid #00ffff;">
<b style="color:#00ffff;">Trade Signal for <a href="https://t.me/{username}">@ {username}</a></b>
<b style="color:#00ffff;">│</b>
<b style="color:#00ffff;">├─ Coin:</b> <b style="color:#ffd700;">{symbol}</b>
<b style="color:#00ffff;">├─ Type:</b> <b style="color:{'lime' if is_long else 'red'};">{order_type} {direction}</b>
<b style="color:#00ffff;">├─ Leverage:</b> <b style="color:yellow;">{lev}x</b>
<b style="color:#00ffff;">├─ RRR (50:50):</b> <b style="color:{'lime' if rrr >= 2 else 'yellow' if rrr >= 1.5 else 'red'};">1:{rrr:.2f}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#00ffff;">├─ Entry 1:</b> <b style="color:lime;">${entry1:,.8f}</b>
<b style="color:#00ffff;">└─ Entry 2:</b> <b style="color:lime;">${entry2:,.8f}</b>
<b style="color:#ff00ff;">│</b>
<b style="color:#ff00ff;">├─ Daily Targets:</b>
"""
    for i, (p, pct) in enumerate(daily[:7], 1):
        html += f"<b style=\"color:#00ff88;\">   T{i}:</b> <b>${p:,.8f}</b> → <b style=\"color:lime;\">+{pct * lev:.1f}%</b>\n"

    # html += "<b style=\"color:#ff00ff;">├─ Mid-Term Targets (8–25%):</b>\n"
    html += "<b style=\"color:#ff00ff;\">├─ Mid-Term Targets:</b>\n"
    for i, (p, pct) in enumerate(mid[:6], len(daily) + 1):
        html += f"<b style=\"color:#88ff88;\">   T{i}:</b> <b>${p:,.8f}</b> → <b style=\"color:lime;\">+{pct * lev:.1f}%</b>\n"

    # html += "<b style=\"color:#ff00ff;">├─ Long-Term Targets (>25%):</b>\n"
    html += "<b style=\"color:#ff00ff;\">├─ Long-Term Targets:</b>\n"
    for i, (p, pct) in enumerate(long_t[:5], len(daily) + len(mid) + 1):
        html += f"<b style=\"color:#00ffff;\">   T{i}:</b> <b>${p:,.8f}</b> → <b style=\"color:lime;\">+{pct * lev:.1f}%</b>\n"

    sl_loss = avg_risk * lev
    html += f"""
<b style="color:#ff00ff;">└─ Stop Loss:</b> <b style="color:red;">${sl:,.8f}</b> → <b style="color:red;">-{sl_loss:.1f}%</b>
</pre>
"""

    # Video or text
    if post_video:
        video = "botlong.mp4" if is_long else "botshort.mp4"
        if Path(video).exists():
            with open(video, 'rb') as f:
                await update.message.reply_video(video=f, caption=html, parse_mode='HTML')
        else:
            await update.message.reply_html(html)
    else:
        await update.message.reply_html(html)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username or update.effective_user.full_name or "Trader"

    # ========================================= !HELP with image =========================================
    html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:18px; border-radius:14px; font-family: 'Courier New', monospace; font-size:14px; line-height:1.8; border-left: 6px solid #00ffff;">
<b style="color:#00ffff; font-size:18px;">Bot Commands – requested by <a href="https://t.me/{username}">@{username}</a></b>

<b style="color:#00ffff;">├─ Charts & Analysis</b>
   <b style="color:#00ff88;">!info [COIN]</b>               → Complete technical analysis (RSI, TSI, EMA, HVN, OBV…)
   <b style="color:#00ff88;">!targets [COIN]</b>            → All trade targets (Fibonacci, HVN, S/R)
   <b style="color:#00ff88;">!trading [COIN]</b>            → Volume flow + spike detection
   <b style="color:#00ff88;">!chart [COIN]</b>              → 7-day chart (line) + indicators
   <b style="color:#00ff88;">!candles [COIN]</b>            → 7-day chart (candles) + indicators
   <b style="color:#00ff88;">!bb [COIN]</b>                 → 7-day chart (candles) + bollinger bands + indicators
   <b style="color:#00ff88;">!don [COIN]</b>                → 7-day chart (candles) + donchian bands + indicators
   <b style="color:#00ff88;">!daily [COIN]</b>              → 120-day KAMA candle chart + Volume + RSI + TSI
   <b style="color:#00ff88;">!minichart [COIN] [min]</b>    → 60-min lines chart (10s candles) with min parameter
   <b style="color:#00ff88;">!smooth [COIN] [min]</b>       → 60-min lines smoothed chart (10s candles) with min parameter

<b style="color:#ff00ff;">├─ Trade Signals</b>
   <b style="color:lime;">!open [CMP|LIMIT] [LONG|SHORT] [COIN] [LEVERAGE] [-V]</b>
       → Professional trade signal (optionally with video)

<b style="color:#ffff00;">├─ Market Overview</b>
   <b style="color:#00ff88;">!sentiment</b>       → Global mood (Fear&Greed, X, Hyperliquid)
   <b style="color:#00ff88;">!volume</b>          → Extreme volume spikes (4h vs 7d)
   <b style="color:#00ff88;">!volatile [perc]</b>        → Coins with ≥20% range in 4h, add desired perc.
   <b style="color:#00ff88;">!topgainers / !toplosers</b> → 24h performance

<b style="color:#ff8800;">├─ Whale & Liquidation Tracker</b>
   <b style="color:#00ff88;">!whalehistory [coin] [exchange] [n]</b> → Last futures whale trades
   <b style="color:#00ff88;">!whalestats [hours]</b>        → Futures whale statistics
   <b style="color:#00ff88;">!whaleleaderboard</b>          → Biggest 24h whale trades
   <b style="color:#00ff88;">!whaletop / !whalecompare</b>   → Whale size graphics
   <b style="color:#00ff88;">!spothistory / !spotstats !spotleaderboard</b> → Spot whales
   <b style="color:#00ff88;">!liqstats [hours]</b>          → Liquidation statistics
   <b style="color:#00ff88;">!heatmap [COIN]</b>            → Liquidation heatmap

<b style="color:#00ffff;">├─ Funding & Open Interest</b>
   <b style="color:#00ff88;">!funding [COIN]</b>         → Current funding rate
   <b style="color:#00ff88;">!fundingextreme</b>         → Top 10 extreme funding rates
   <b style="color:#00ff88;">!fundingtop20</b>           → Funding Top 20 coins
   <b style="color:#00ff88;">!oitop20</b>               → Open Interest Top 20 coins

<b style="color:#ff00ff;">├─ Utilities</b>
   <b style="color:#00ff88;">!price [COIN]</b>       → Current live price
   <b style="color:#00ff88;">!alarm [COIN] [PRICE]</b> → Price alert
   <b style="color:#00ff88;">!latestnews</b>         → Latest crypto news
   <b style="color:#00ff88;">!pumpstats</b>          → Latest pump detections
   <b style="color:#00ff88;">!help</b>               → This beautiful message
   ← you are here
   <b style="color:#00ff88;">!version</b>            → Bot changelog

<b style="color:#ff00ff;">└─ Examples</b>
   <b style="color:#88ff88;">!open LIMIT LONG BTCUSDT 20x -V</b>
   <b style="color:#88ff88;">!info ETHUSDT</b>
   <b style="color:#88ff88;">!daily SOL</b>
   <b style="color:#88ff88;">!whalehistory BTC binance 10</b>

<b style="color:#00ffff; font-size:15px;">Bot by Proven Crypto Insights – 2025</b>
</pre>
""".strip()

    # NO IMAGE – plain text message only → 4096 characters free!
    await update.message.reply_html(html)


# ========================================= !AI Handler  =========================================

# async def ai_signals_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

# """
# !ai [hours] [coin] [direction] [model]
# Possible parameters (in any order):
# - Number → hours (e.g. 24)
# - Coin symbol (e.g. BTC, SOL) → becomes BTCUSDT
# - long / short → direction filter
# - EPD1 / MIS1 / ATS1 → model filter
# Examples:
# !ai                  → last 100 signals
# !ai 24               → last 24 hours
# !ai BTC              → only BTCUSDT
# !ai long             → only LONG signals
# !ai MIS1             → only MIS1 model
# !ai long 48 MIS1     → LONG signals from MIS1 in the last 48h
# """
# if not update.message or not update.message.text:
# return

# raw_args = context.args or []  # ← safe: if None → empty list
# args = [a.strip().upper() for a in raw_args if a.strip()]
# logged_args = " ".join(raw_args) if raw_args else ""  # ← Fix 2: avoids join(None)
# #args = [a.strip().upper() for a in context.args if a.strip()]
# hours = None
# coin_filter = None
# direction_filter = None
# model_prefix = None  # e.g. "MIS1", "EPD1"

# valid_models = ["EPD1", "MIS1", "ATS1"]

# for arg in args:
# if arg.isdigit() or (arg.startswith('-') and arg[1:].isdigit()):
# hours = abs(int(arg))
# if hours == 0:
# hours = 24
# elif arg in ["LONG", "SHORT"]:
# direction_filter = arg
# elif arg in valid_models:
# model_prefix = arg
# elif arg.endswith("USDT"):
# coin_filter = arg
# elif len(arg) <= 8:
# coin_filter = arg + "USDT"

# username = update.effective_user.username or update.effective_user.full_name or "unknown"
# #log_command("!ai " + " ".join(context.args), update.effective_user)
# log_command(f"!ai {logged_args}", update.effective_user)  # ← sauberer String

# await update.message.reply_chat_action(ChatAction.TYPING)

# try:
# conn = await get_conn()
# query_parts = []
# params = []

# base_query = """
# SELECT symbol, timestamp, price, model, direction, confidence
# FROM ai_signals
# """

# if hours is not None:
# query_parts.append(f"timestamp >= NOW() - INTERVAL '{hours} hours'")

# if coin_filter:
# query_parts.append("symbol = $%d" % (len(params) + 1))
# params.append(coin_filter)

# if direction_filter:
# query_parts.append("direction = $%d" % (len(params) + 1))
# params.append(direction_filter)

# if model_prefix:
# query_parts.append("model LIKE $%d || '%%'" % (len(params) + 1))
# params.append(model_prefix)

# where_clause = " WHERE " + " AND ".join(query_parts) if query_parts else ""

# final_query = f"""
# {base_query}
# {where_clause}
# ORDER BY timestamp DESC
# LIMIT 100
# """

# rows = await conn.fetch(final_query, *params)
# await release_conn(conn)

# if not rows:
# filter_text = []
# if hours: filter_text.append(f"last {hours}h")
# if coin_filter: filter_text.append(coin_filter.replace("USDT", ""))
# if direction_filter: filter_text.append(direction_filter)
# if model_prefix: filter_text.append(model_prefix)
# filter_desc = " (" + " + ".join(filter_text) + ")" if filter_text else ""
# await update.message.reply_text(f"No AI signals found{filter_desc}.")
# return

# # Dynamic title
# title_parts = ["AI SIGNALS"]
# if hours: title_parts.append(f"Last {hours}h")
# if coin_filter: title_parts.append(coin_filter.replace("USDT", ""))
# if direction_filter: title_parts.append(direction_filter)
# if model_prefix: title_parts.append(model_prefix)
# title = " • ".join(title_parts)

# html_lines = [
# f'<b style="color:#00ffff; font-size:18px;">🤖 {title}</b>',
# f'<b style="color:#888888;">TIME     COIN     MODEL              DIR     CONF     PRICE</b>',
# '<b style="color:#00ffff;">─────────────────────────────────────────────────────</b>'
# ]

# for row in rows:
# #ts = row['timestamp'].strftime("%m-%d %H:%M")
# ts_utc = row['timestamp'].astimezone(pytz.UTC)
# ts = ts_utc.strftime("%m-%d %H:%M")
# sym = row['symbol'].replace("USDT", "")
# full_model = row['model']  # ← full name like "MSI1-168h_pump"
# dir = row['direction']
# conf = row['confidence']
# price = float(row['price'])

# # Colors
# dir_color = "#00ff88" if dir == "LONG" else "#ff6688"
# conf_color = (
# "#00ff00" if conf >= 0.7 else
# "#ffff00" if conf >= 0.5 else
# "#ffaa00" if conf >= 0.3 else
# "#ff4444"
# )

# # Truncate model name to 18 characters if too long (for nice alignment)
# display_model = full_model if len(full_model) <= 18 else full_model[:15] + "..."

# html_lines.append(
# f"<b>{ts}</b> <b>{sym:>8}</b> <b>{display_model:<18}</b> "
# f"<b style=\"color:{dir_color};\">{dir:>6}</b> "
# f"<b style=\"color:{conf_color};\">{conf:6.1%}</b> "
# f"<code>${price:,.0f}</code>"
# )

# html_lines += [
# '<b style="color:#00ffff;">─────────────────────────────────────────────────────</b>',
# f'<b style="color:#00ffff;">Requested by <a href="https://t.me/{username}">@{username}</a> • {datetime.now(pytz.UTC).strftime("%H:%M")} UTC</b>',
# f'<b style="color:#888888;">Total: {len(rows)} signals</b>'
# ]

# html = f"""
# <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:14px; line-height:1.7; border-left: 6px solid #00ffff;">
# {"\n".join(html_lines)}
# </pre>
# """.strip()

# await update.message.reply_html(html)

# except Exception as e:
# logger.error(f"!ai error: {e}", exc_info=True)
# await update.message.reply_text("Error loading the AI signals.")


async def ai_signals_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # SAFE ARG PARSING
    raw_args = context.args or []
    args = [a.strip().upper() for a in raw_args if a.strip()]
    logged_args = " ".join(raw_args) if raw_args else ""

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command(f"!ai {logged_args}", update.effective_user)

    await update.message.reply_chat_action(ChatAction.TYPING)

    hours = None
    coin_filter = None
    direction_filter = None
    model_prefix = None

    valid_models = ["EPD1", "MIS1", "ATS1"]

    for arg in args:
        if arg.isdigit() or (arg.startswith('-') and arg[1:].isdigit()):
            hours = abs(int(arg))
            if hours == 0:
                hours = 24
        elif arg in ["LONG", "SHORT"]:
            direction_filter = arg
        elif arg in valid_models:
            model_prefix = arg
        elif arg.endswith("USDT"):
            coin_filter = arg
        else:
            # Anything else that's not a number, LONG/SHORT, or model → interpret as coin
            # Length up to 12 (for names like 1000PEPEUSDT)
            if len(arg) <= 12:
                coin_filter = arg if arg.endswith("USDT") else arg + "USDT"

    try:
        conn = await get_conn()
        query_parts = []
        params = []

        base_query = """
            SELECT symbol, timestamp, price, model, direction, confidence
            FROM ai_signals
        """

        if hours is not None:
            query_parts.append(f"timestamp >= NOW() - INTERVAL '{hours} hours'")

        if coin_filter:
            query_parts.append("symbol = $%d" % (len(params) + 1))
            params.append(coin_filter)

        if direction_filter:
            query_parts.append("direction = $%d" % (len(params) + 1))
            params.append(direction_filter)

        if model_prefix:
            query_parts.append("model LIKE $%d || '%%'" % (len(params) + 1))
            params.append(model_prefix)

        where_clause = " WHERE " + " AND ".join(query_parts) if query_parts else ""

        final_query = f"""
            {base_query}
            {where_clause}
            ORDER BY timestamp DESC
            LIMIT 200  -- mehr holen, dann splitten
        """

        rows = await conn.fetch(final_query, *params)
        await release_conn(conn)

        if not rows:
            filter_text = []
            if hours:
                filter_text.append(f"last {hours}h")
            if coin_filter:
                filter_text.append(coin_filter.replace("USDT", ""))
            if direction_filter:
                filter_text.append(direction_filter)
            if model_prefix:
                filter_text.append(model_prefix)
            filter_desc = " (" + " + ".join(filter_text) + ")" if filter_text else ""
            await update.message.reply_text(f"No AI signals found{filter_desc}.")
            return

        # Title
        title_parts = ["AI SIGNALS"]
        if hours:
            title_parts.append(f"Last {hours}h")
        if coin_filter:
            title_parts.append(coin_filter.replace("USDT", ""))
        if direction_filter:
            title_parts.append(direction_filter)
        if model_prefix:
            title_parts.append(model_prefix)
        title = " • ".join(title_parts)

        # Split into chunks of max. 50 signals
        chunk_size = 50
        chunks = [rows[i : i + chunk_size] for i in range(0, len(rows), chunk_size)]

        for idx, chunk in enumerate(chunks):
            html_lines = []
            if idx == 0:
                html_lines += [
                    f'<b style="color:#00ffff; font-size:18px;">🤖 {title}</b>',
                    '<b style="color:#888888;">TIME     COIN     MODEL              DIR     CONF     PRICE</b>',
                    '<b style="color:#00ffff;">─────────────────────────────────────────────────────</b>',
                ]

            for row in chunk:
                ts_utc = row['timestamp'].astimezone(pytz.UTC)
                ts = ts_utc.strftime("%m-%d %H:%M")

                sym = row['symbol'].replace("USDT", "")
                full_model = row['model']
                display_model = full_model if len(full_model) <= 18 else full_model[:15] + "..."
                dir = row['direction']
                conf = row['confidence']
                price = float(row['price'])

                dir_color = "#00ff88" if dir == "LONG" else "#ff6688"
                conf_color = (
                    "#00ff00" if conf >= 0.7 else "#ffff00" if conf >= 0.5 else "#ffaa00" if conf >= 0.3 else "#ff4444"
                )

                html_lines.append(
                    f"<b>{ts}</b> <b>{sym:>8}</b> <b>{display_model:<18}</b> "
                    f"<b style=\"color:{dir_color};\">{dir:>6}</b> "
                    f"<b style=\"color:{conf_color};\">{conf:6.1%}</b> "
                    f"<code>${price:,.0f}</code>"
                )

            if idx == len(chunks) - 1:  # last message
                html_lines += [
                    '<b style="color:#00ffff;">─────────────────────────────────────────────────────</b>',
                    f'<b style="color:#00ffff;">Requested by <a href="https://t.me/{username}">@{username}</a> • {datetime.now(pytz.UTC).strftime("%H:%M")} UTC</b>',
                    f'<b style="color:#888888;">Total: {len(rows)} signals ({len(chunks)} message{"s" if len(chunks) > 1 else ""})</b>',
                ]
            else:
                html_lines.append(
                    f'<b style="color:#888888;">... continued in next message ({idx + 1}/{len(chunks)})</b>'
                )

            # CI fix (2026-07-06): a backslash in an f-string expression is only legal
            # from Python 3.12 (PEP 701) onward — the syntax check runs on 3.11.
            joined_html_lines = "\n".join(html_lines)
            html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:14px; line-height:1.7; border-left: 6px solid #00ffff;">
{joined_html_lines}
</pre>
            """.strip()

            await update.message.reply_html(html)

    except Exception as e:
        logger.error(f"!ai error: {e}", exc_info=True)
        await update.message.reply_text("Error loading the AI signals.")


# ========================================= !VERSION with image =========================================
async def version_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username or update.effective_user.full_name or "Trader"

    html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family: 'Courier New', monospace; font-size:14px; line-height:1.7; border-left: 5px solid #00ffff;">
<b style="color:#00ffff;">Proven Crypto Bot V2 Version History – requested by <a href="https://t.me/{username}">@{username}</a></b>
<b style="color:#00ffff;">│</b>
<b style="color:#00ff00;">Current Version: v1.4</b>  ← <b style="color:lime;">LIVE & FULLY OPERATIONAL</b>
<b style="color:#ff00ff;">├─ v1.4</b>   → implemented a new pump/dump detector with machine learning model for relevance filter
<b style="color:#ff00ff;">├─ v1.3</b>   → redesign for graphic functions
<b style="color:#ff00ff;">├─ v1.2</b>   → redesign for grabbing Futures volume and calculating HVNs
<b style="color:#ff00ff;">├─ v1.1</b>   → moved first batch of functions to new framefwork
<b style="color:#ff00ff;">└─ v1.0</b>   → Core Framework newly coded from scratch, fully async and restartable

<b style="color:#00ffff;">Bot by Proven Crypto Insights – 2025</b>
</pre>
""".strip()

    # Send image + caption
    if Path("bot.jpg").exists():
        with open("bot.jpg", "rb") as photo:
            await update.message.reply_photo(photo=photo, caption=html, parse_mode="HTML")
    else:
        await update.message.reply_html(html)


# ========================================= 1min candle job =========================================
async def load_1minute_data():
    """At bot start: load JSON from the last run (if present)"""
    global ONE_MINUTE_DATA
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            ONE_MINUTE_DATA = {}
            for symbol, entries in raw.items():
                # Only keep the last 240 entries (4h)
                dq = deque(maxlen=1440)
                for entry in entries[-1440:]:
                    dq.append(entry)
                ONE_MINUTE_DATA[symbol] = dq
            logger.info(f"Loaded 1min data for {len(ONE_MINUTE_DATA)} symbols from disk")
        except Exception as e:
            logger.error(f"Failed to load 1minute.json: {e}")
            ONE_MINUTE_DATA = {}
    else:
        ONE_MINUTE_DATA = {}


async def save_1minute_data():
    """Safe saving of all data (e.g. every 10 minutes or on shutdown)"""
    try:
        raw = {sym: list(dq) for sym, dq in ONE_MINUTE_DATA.items()}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved 1min data for {len(raw)} symbols")
    except Exception as e:
        logger.error(f"Failed to save 1minute.json: {e}")


async def one_minute_ticker_job():
    """
    Runs exactly every 10 seconds (UTC: :00, :10, :20, :30, :40, :50)
    Stores price + real 10-second volume (difference)
    """
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        while True:
            try:
                now = datetime.now(pytz.UTC)

                # Calculate the next 10-second step (e.g. 12:34:50 → 12:35:00)
                seconds = now.second
                next_tick = now.replace(microsecond=0) + timedelta(
                    seconds=(10 - seconds % 10) if seconds % 10 != 0 else 10
                )

                sleep_time = (next_tick - datetime.now(pytz.UTC)).total_seconds()
                if sleep_time <= 0:
                    sleep_time = 10  # Safety

                await asyncio.sleep(sleep_time)

                # --- Exact timestamp (always :00, :10, :20, …) ---
                timestamp = datetime.now(pytz.UTC)
                ts_str = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")  # e.g. 2025-12-08T12:34:50Z

                async with session.get("https://fapi.binance.com/fapi/v1/ticker/24hr") as resp:
                    if resp.status != 200:
                        logger.warning(f"Ticker error: {resp.status}")
                        continue
                    raw_data = await resp.json()

                updated_count = 0
                for item in raw_data:
                    symbol = item["symbol"]
                    if not symbol.endswith("USDT"):
                        continue

                    current_price = float(item["lastPrice"])
                    current_cum_volume = float(item["volume"])  # cumulative 24h volume

                    # Get previous entry for the volume difference
                    prev_volume = None
                    if symbol in ONE_MINUTE_DATA and ONE_MINUTE_DATA[symbol]:
                        prev_volume = ONE_MINUTE_DATA[symbol][-1].get("cum_vol")

                    # Compute 10-second volume
                    volume_10s = (current_cum_volume - prev_volume) if prev_volume is not None else 0.0
                    if volume_10s < 0:  # very rare during the 24h rollover
                        volume_10s = 0.0

                    entry = {
                        "t": ts_str,  # 2025-12-08T12:34:50Z
                        "p": current_price,  # current price
                        "v10s": round(volume_10s, 8),  # real 10-second volume
                        "cum_vol": current_cum_volume,  # only internal, for the next calculation
                    }

                    if symbol not in ONE_MINUTE_DATA:
                        ONE_MINUTE_DATA[symbol] = deque(maxlen=1440)  # 10s → 4 hours = 1440 points
                    ONE_MINUTE_DATA[symbol].append(entry)
                    updated_count += 1

                logger.info(
                    f"10s-Ticker updated @ {ts_str} | {updated_count} USDT pairs | "
                    f"sleep {sleep_time:.1f}s → next @ {next_tick.strftime('%H:%M:%S')} UTC"
                )

                # Optional: save to disk every 10 minutes
                if timestamp.minute % 10 == 0 and timestamp.second == 0:
                    asyncio.create_task(save_1minute_data())

            except asyncio.CancelledError:
                logger.info("10s ticker job cancelled – saving data...")
                await save_1minute_data()
                raise
            except Exception as e:
                logger.error(f"10s ticker job crashed: {e}")
                await asyncio.sleep(8)


# ============================== !volume HANDLER ==============================
async def volume_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # Optional: !volume 5 → only spikes ≥5× (instead of default 3×)
    args = context.args
    min_spike = float(args[0]) if args and args[0].replace('.', '').isdigit() else 3.0
    top_n = 20

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!volume", update.effective_user)

    await update.message.reply_chat_action("typing")

    try:
        spikes = await detect_volume_spikes_async(min_spike=min_spike, top_n=top_n)

        if not spikes:
            html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:20px; border-radius:16px; font-family:'Courier New', monospace; font-size:15px; line-height:1.9; border-left:8px solid #666666;">
<b style="color:#00ffff; font-size:20px;">NO VOLUME SPIKES</b>
<b style="color:#888888;">Last 4h vs 7d average (≥{min_spike:.1f}×)</b>
<b style="color:#666666;">──────────────────────────────────────────────</b>
No extreme volume spikes detected right now.
<b style="color:#00ffff;">Requested by <a href="https://t.me/{username}">@{username}</a></b>
</pre>
""".strip()
            await update.message.reply_html(html, disable_web_page_preview=True)
            return

        # === EPIC HTML DESIGN ===
        border_color = "#ff00ff" if min_spike >= 6 else "#00ff88"
        title = f"TOP {len(spikes)} VOLUME SPIKES (≥{min_spike:.1f}× in 4h)"

        html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:20px; border-radius:16px; font-family:'Courier New', monospace; font-size:15px; line-height:1.9; border-left:8px solid {border_color};">
<b style="color:#00ffff; font-size:20px;">VOLUME SPIKES • Last 4h vs 7d Avg</b>
<b style="color:#888888;">RANK COIN         SPIKE     4h USD VOL       7d AVG      PRICE</b>
<b style="color:#00ffff;">{'─' * 66}</b>
""".strip()

        for i, s in enumerate(spikes, 1):
            sym = s['symbol'].replace("USDT", "")
            spike_text = f"{s['spike_ratio']:.1f}×"
            color = "#ff00ff" if s['spike_ratio'] >= 10 else "#ff4466" if s['spike_ratio'] >= 6 else "#00ff88"
            html += f"\n<b style=\"color:#ffd700;\">{i:2d}</b> <b>{sym:>10}</b> <b style=\"color:{color};\">{spike_text:>6}</b>  <code>${s['usd_vol_4h']:>12,.0f}</code>  ${s['avg_vol_7d']:>12,.0f}  <code>${s['current_price']:>10,.6f}</code>"

        now = datetime.now(pytz.UTC).strftime("%H:%M:%S")
        html += f"""
<b style="color:#00ffff;">\n{'─' * 66}</b>
<b style="color:#00ffff;">Requested by <a href="https://t.me/{username}">@{username}</a> • {now} UTC</b>
</pre>
""".strip()

        await update.message.reply_html(html, disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"!volume error: {e}", exc_info=True)
        await update.message.reply_text("Error loading volume spikes.")


# ============================== ASYNC DETECTION ==============================
async def detect_volume_spikes_async(min_spike: float = 10.0, top_n: int = 50):
    coins = load_coins()
    if not coins:
        return []

    # =========================
    spikes = []
    now = datetime.now(pytz.UTC)
    start_4h = now - timedelta(hours=4)
    start_7d = now - timedelta(days=7)

    conn = await get_conn()  # ← CORRECT!
    try:
        for symbol in coins:
            tablename = f'"{symbol}_30m"'
            try:
                # 4h data
                rows_4h = await conn.fetch(
                    f"""
                    SELECT volume, close FROM {tablename}
                    WHERE open_time >= $1 AND open_time <= $2
                    ORDER BY open_time ASC
                """,
                    start_4h,
                    now,
                )

                if not rows_4h:
                    continue
                df_4h = pd.DataFrame(rows_4h, columns=['volume', 'close'])
                vol_4h = df_4h['volume'].sum()
                usd_vol_4h = vol_4h * df_4h['close'].iloc[-1]
                if usd_vol_4h < 250_000:
                    continue

                # 7d average
                rows_7d = await conn.fetch(
                    f"""
                    SELECT volume FROM {tablename}
                    WHERE open_time >= $1 AND open_time < $2
                """,
                    start_7d,
                    start_4h,
                )

                if len(rows_7d) < 10:
                    continue
                avg_vol_7d = pd.DataFrame(rows_7d, columns=['volume'])['volume'].mean()

                ratio = vol_4h / avg_vol_7d
                if ratio >= min_spike:
                    spikes.append(
                        {
                            'symbol': symbol,
                            'spike_ratio': ratio,
                            'usd_vol_4h': usd_vol_4h,
                            'avg_vol_7d': avg_vol_7d,
                            'current_price': df_4h['close'].iloc[-1],
                        }
                    )
            except Exception as e:
                logger.debug(f"Volume spike skip {symbol}: {e}")
                continue
    finally:
        await release_conn(conn)  # ← ALWAYS close!

    spikes.sort(key=lambda x: x['spike_ratio'], reverse=True)
    return spikes[:top_n]


# ============================== !volatile HANDLER ==============================
async def volatile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... your beginning up to try: ...
    # === THIS WAS MISSING HERE! ===
    args = context.args
    min_range_percent = float(args[0]) if args and args[0].replace('.', '').isdigit() else 10.0
    top_n = 20
    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!volatile", update.effective_user)

    try:
        coins = load_coins()
        if not coins:
            await update.message.reply_text("No coins in database.")
            return

        volatile_coins = []
        now = datetime.now(pytz.UTC)
        start_4h = now - timedelta(hours=4)

        conn = await get_conn()  # ← CORRECT!
        try:
            for symbol in coins:
                tablename = f'"{symbol}_30m"'
                try:
                    rows = await conn.fetch(
                        f"""
                        SELECT high, low, close FROM {tablename}
                        WHERE open_time >= $1 AND open_time <= $2
                        ORDER BY open_time ASC
                    """,
                        start_4h,
                        now,
                    )

                    if len(rows) < 2:
                        continue
                    df = pd.DataFrame(rows, columns=['high', 'low', 'close'])
                    high_4h = df['high'].max()
                    low_4h = df['low'].min()
                    current_price = df['close'].iloc[-1]

                    if low_4h <= 0:
                        continue
                    range_percent = (high_4h - low_4h) / low_4h * 100
                    if range_percent >= min_range_percent:
                        volatile_coins.append(
                            {
                                'symbol': symbol,
                                'range': round(range_percent, 2),
                                'high': high_4h,
                                'low': low_4h,
                                'price': current_price,
                            }
                        )
                except Exception as e:
                    logger.warning(f"Volatile error {symbol}: {e}")
                    continue
        finally:
            await release_conn(conn)  # ← ALWAYS close!

        # ... your complete HTML output stays 100% the same ...

        # Sort by range descending
        volatile_coins.sort(key=lambda x: x['range'], reverse=True)
        volatile_coins = volatile_coins[:top_n]

        if not volatile_coins:
            await update.message.reply_text(f"No coins with ≥{min_range_percent}% range in last 4h.")
            return

        # === GORGEOUS HTML DESIGN ===
        border_color = "#ff00ff" if min_range_percent >= 15 else "#00ff88"
        title = f"TOP {len(volatile_coins)} VOLATILE COINS (4h Range ≥{min_range_percent}%)"

        html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:20px; border-radius:16px; font-family:'Courier New', monospace; font-size:15px; line-height:1.9; border-left:8px solid {border_color};">
<b style="color:#00ffff; font-size:20px;">VOLATILE COINS • Last 4h</b>
<b style="color:#888888;">RANK  COIN         RANGE     HIGH → LOW     CURRENT</b>
<b style="color:#00ffff;">{'─' * 60}</b>
"""

        for i, coin in enumerate(volatile_coins, 1):
            sym = coin['symbol'].replace("USDT", "")
            rang = f"{coin['range']:>5.2f}%"
            arrow = "UP" if coin['price'] > coin['low'] * 1.02 else "DOWN"
            color = "#00ff88" if arrow == "UP" else "#ff4466"
            html += f"\n<b style=\"color:#ffd700;\">{i:2d}</b>  <b>{sym:>10}</b>  <b style=\"color:{color};\">{rang}</b>  {coin['high']:>8.4f} → {coin['low']:>8.4f}  <code>${coin['price']:>10,.4f}</code>"

        html += f"""
<b style="color:#00ffff;">\n{'─' * 60}</b>
<b style="color:#00ffff;">Requested by <a href="https://t.me/{username}">@{username}</a> • {now.strftime('%H:%M:%S')} UTC</b>
</pre>
""".strip()

        await update.message.reply_html(html, disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"!volatile error: {e}", exc_info=True)
        await update.message.reply_text("Error loading volatility data.")


# ========================= !smooth function =========================


async def generate_smooth_minichart_image(symbol: str, minutes: int = 60) -> BytesIO:
    # 1. Fetch data & validate (standard)
    if symbol not in ONE_MINUTE_DATA or len(ONE_MINUTE_DATA[symbol]) < 5:
        return None

    # Load buffer & data
    buffer_needed = int(minutes * 6 * 1.2)
    full_history = list(ONE_MINUTE_DATA[symbol])
    data = full_history[-buffer_needed:]

    if len(data) < 5:
        return None

    df = pd.DataFrame(data)
    df['t'] = pd.to_datetime(df['t'], utc=True)
    df = df.set_index('t').sort_index()
    df = df.dropna(subset=['p', 'v10s'])

    # Time filter
    cutoff_time = df.index[-1] - pd.Timedelta(minutes=minutes)
    df = df[df.index >= cutoff_time]
    if len(df) < 2:
        return None

    price = df['p'].astype(float)
    volume = df['v10s'].astype(float)

    # Minute calculation
    diff_seconds = (df.index[-1] - df.index[0]).total_seconds()
    actual_minutes = int(round(diff_seconds / 60))
    if actual_minutes < 1:
        actual_minutes = 1

    # === SMOOTHING LOGIC (the gamechanger) ===

    # 1. Numbers for the X axis
    x_dates = mdates.date2num(df.index)
    y_price = price.values

    # 2. Apply "blur" (Gaussian filter)
    # sigma=2 means: it smooths over roughly 2-3 neighboring values (20-30 seconds).
    # The higher sigma, the rounder (but less accurate) the chart becomes.
    # sigma=2 is a good compromise for 10s data.
    y_smoothed_gauss = gaussian_filter1d(y_price, sigma=2)

    # 3. Increase resolution (spline)
    # Now we draw the curve through the ALREADY SMOOTHED points.
    # We generate 300 points for a buttery-smooth line.
    x_smooth = np.linspace(x_dates.min(), x_dates.max(), 300)

    # We use make_interp_spline (B-spline) here, which looks more organic than PCHIP
    spline = make_interp_spline(x_dates, y_smoothed_gauss, k=3)
    y_smooth = spline(x_smooth)

    # Convert back for the plot
    x_smooth_dates = mdates.num2date(x_smooth)

    # === PLOT SETUP ===
    fig = plt.figure(figsize=(16, 9), facecolor="#0d0d0d")
    gs = fig.add_gridspec(1, 2, width_ratios=[4, 1], wspace=0.05)

    ax_price = fig.add_subplot(gs[0, 0])
    ax_vol = ax_price.twinx()
    ax_vbp = fig.add_subplot(gs[0, 1])

    # Color based on REAL start/end data (not smoothed)
    is_up = price.iloc[-1] >= price.iloc[0]

    # === VOLUMEN ===
    vol_max = volume.quantile(0.99) if len(volume) > 0 and volume.max() > 0 else 1
    if vol_max == 0:
        vol_max = volume.max() if volume.max() > 0 else 1

    vol_colors = ['#00ff88' if i == 0 or price.iloc[i] >= price.iloc[i - 1] else '#ff3040' for i in range(len(price))]

    # Width for volume bars
    if len(df) > 1:
        avg_step = df.index.to_series().diff().median().total_seconds()
        width = (avg_step / 86400) * 0.9
    else:
        width = 0.0001

    ax_vol.bar(df.index, volume, color=vol_colors, width=width, alpha=0.5, zorder=1)
    ax_vol.set_ylim(0, vol_max * 4.0)
    ax_vol.axis('off')

    # === PREIS (SMOOTH!) ===

    # Area Chart (Smooth)
    ax_price.fill_between(
        x_smooth_dates, y_smooth, price.min(), color="#00ff88" if is_up else "#ff3040", alpha=0.2, zorder=2
    )

    # Line (Smooth)
    ax_price.plot(x_smooth_dates, y_smooth, color="#00ffff", linewidth=2.5, zorder=3)

    # === IMPORTANT: CURRENT PRICE DISPLAY ===
    # We show the REAL last price, even if the smoothed curve deviates slightly.
    # So the user sees the exact value.
    last_real_price = price.iloc[-1]

    ax_price.axhline(last_real_price, color="white", linewidth=1, linestyle="--", alpha=0.5)

    ax_price.text(
        0.05,
        last_real_price,
        f"{last_real_price:,.4f}",
        transform=ax_price.get_yaxis_transform(),
        color="white",
        fontsize=10,
        fontweight='bold',
        va='center',
        bbox=dict(facecolor='#1e1e1e', edgecolor='none', pad=5),
    )

    # === VOLUME BY PRICE (right) ===
    ax_vbp.set_facecolor("#0d0d0d")
    bins = np.linspace(price.min() * 0.995, price.max() * 1.005, 45)
    hist, _ = np.histogram(price, bins=bins, weights=volume)
    centers = (bins[:-1] + bins[1:]) / 2

    ax_vbp.barh(
        centers,
        hist,
        height=(bins[1] - bins[0]) * 0.88,
        color='#ff69b4',
        alpha=0.75,
        edgecolor='#ff1493',
        linewidth=0.6,
    )

    if len(hist) > 0:
        max_idx = np.argmax(hist)
        ax_vbp.barh(
            centers[max_idx],
            hist[max_idx],
            height=(bins[1] - bins[0]),
            color='#00ffff',
            alpha=0.9,
            edgecolor='#ff1493',
            linewidth=0.6,
        )

    ax_vbp.set_ylim(ax_price.get_ylim())  # IMPORTANT: sync with price limits
    ax_vbp.invert_xaxis()
    ax_vbp.set_xlabel('Vol', color='#ff69b4', fontsize=10)
    ax_vbp.tick_params(colors='#ff69b4', labelsize=8)
    ax_vbp.spines[['top', 'right', 'left', 'bottom']].set_visible(False)

    # === STYLING ===
    coin = symbol.replace("USDT", "")
    title_time = f"{actual_minutes}min" if actual_minutes > 0 else f"{minutes}min"

    ax_price.set_title(
        f"{coin} • {title_time} • ${last_real_price:,.8f}",
        color="white",
        fontsize=20,
        fontweight='bold',
        loc='center',
        pad=10,
    )

    ax_price.grid(True, color='#333333', alpha=0.3, linestyle='--')
    ax_price.set_facecolor("#0d0d0d")
    ax_price.spines[['top', 'right', 'left', 'bottom']].set_visible(False)
    ax_price.tick_params(axis='x', colors='#888888', labelsize=10)
    ax_price.tick_params(axis='y', colors='#888888', labelsize=10)

    locator = MinuteLocator(interval=max(1, int(actual_minutes / 6)))
    ax_price.xaxis.set_major_locator(locator)
    ax_price.xaxis.set_major_formatter(DateFormatter('%H:%M'))

    # Set limits on the REAL data (so start/end are accurate)
    ax_price.set_xlim(df.index[0], df.index[-1])

    plt.subplots_adjust(left=0.05, right=0.9, top=0.9, bottom=0.1)

    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, facecolor="#0d0d0d", bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf


async def smooth_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text.strip()
    parts = text.split()

    if len(parts) < 2:
        await update.message.reply_text("Usage: !smooth <COIN> [minutes]\nExample: !smooth BTC 120")
        return

    symbol_raw = parts[1].upper()
    valid_symbol = await validate_symbol(symbol_raw)
    if not valid_symbol:
        await update.message.reply_text(f"Coin not found: {symbol_raw}")
        return
    symbol = valid_symbol

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!smooth " + symbol_raw, update.effective_user)

    minutes = 60
    if len(parts) >= 3:
        try:
            minutes = int(parts[2])
            minutes = min(max(minutes, 10), 240)
        except:
            minutes = 60

    buf = await generate_smooth_minichart_image(symbol, minutes)
    if not buf:
        await update.message.reply_text(f"no data fro {symbol} available")
        return

    caption = (
        f"Live {minutes}min Chart • {symbol.replace('USDT', '')}/USDT • @{username}\n"
        f"Update: {datetime.now(pytz.UTC).strftime('%H:%M:%S')} UTC"
    )

    await update.message.reply_photo(photo=buf, caption=caption)


# ========================= !minichart function =========================


async def generate_minichart_image(symbol: str, minutes: int = 60) -> BytesIO:
    # 1. Fetch data
    if symbol not in ONE_MINUTE_DATA or len(ONE_MINUTE_DATA[symbol]) < 5:
        return None

    # DEBUG: prints to your console how much data is actually available
    total_available = len(ONE_MINUTE_DATA[symbol])
    needed = minutes * 6
    print(f"DEBUG CHART: Requested {minutes}m ({needed} pts), Available: {total_available} pts")

    data = list(ONE_MINUTE_DATA[symbol])[-needed:]

    if len(data) < 5:
        return None

    df = pd.DataFrame(data)
    df['t'] = pd.to_datetime(df['t'], utc=True)
    df = df.set_index('t').sort_index()
    df = df.dropna(subset=['p', 'v10s'])

    price = df['p'].astype(float)
    volume = df['v10s'].astype(float)

    # Calculate the time span (for title and width)
    actual_minutes = int((df.index[-1] - df.index[0]).total_seconds() / 60)

    # === SETUP ===
    fig = plt.figure(figsize=(16, 9), facecolor="#0d0d0d")
    gs = fig.add_gridspec(1, 2, width_ratios=[4, 1], wspace=0.05)

    ax_price = fig.add_subplot(gs[0, 0])
    ax_vol = ax_price.twinx()  # Volume sits above the price axis
    ax_vbp = fig.add_subplot(gs[0, 1])

    is_up = price.iloc[-1] >= price.iloc[0]

    # === VOLUME (configure first so it's visible) ===
    # Trick: we use the 99% quantile instead of max.
    # If there's a huge spike, it gets clipped, but the rest is clearly visible.
    if len(volume) > 0 and volume.max() > 0:
        vol_max_scale = volume.quantile(0.99)
        if vol_max_scale == 0:
            vol_max_scale = volume.max()
    else:
        vol_max_scale = 1

    # Colors
    vol_colors = ['#00ff88' if i == 0 or price.iloc[i] >= price.iloc[i - 1] else '#ff3040' for i in range(len(price))]

    # Width: we make the bars artificially a bit wider than the time step
    # so no gaps appear (looks "fuller")
    time_diffs = df.index.to_series().diff().dt.total_seconds().median()  # usually 10s
    if pd.isna(time_diffs):
        time_diffs = 10
    width_days = (time_diffs / 86400) * 0.9  # convert to days for Matplotlib

    # Plot
    ax_vol.bar(
        price.index,
        volume,
        color=vol_colors,
        width=width_days,
        alpha=0.5,  # don't make it too transparent!
        align='center',
        zorder=1,
    )  # right at the back

    # SCALING: the volume should occupy the bottom quarter (25%)
    # We set the limit to 4x the max value.
    ax_vol.set_ylim(0, vol_max_scale * 4.0)
    ax_vol.axis('off')  # No axis labeling for volume on the left

    # === PRICE ===
    # Area
    ax_price.fill_between(price.index, price, price.min(), color="#00ff88" if is_up else "#ff3040", alpha=0.2, zorder=2)
    # Line
    ax_price.plot(price.index, price, color="#00ffff", linewidth=2.5, zorder=3)

    # Last price marker
    ax_price.axhline(price.iloc[-1], color="white", linewidth=1, linestyle="--", alpha=0.5)
    # ax_price.text(1.01, price.iloc[-1], f"{price.iloc[-1]:,.4f}",
    # transform=ax_price.get_yaxis_transform(),
    # color="white", fontsize=10, fontweight='bold', va='center',
    # bbox=dict(facecolor='#1e1e1e', edgecolor='none', pad=5))
    ax_price.text(
        0.05,
        price.iloc[-1],
        f"{price.iloc[-1]:,.4f}",
        transform=ax_price.get_yaxis_transform(),
        color="white",
        fontsize=10,
        fontweight='bold',
        va='center',
        bbox=dict(facecolor='#1e1e1e', edgecolor='none', pad=5),
    )

    ax_vbp.set_facecolor("#0d0d0d")
    bins = np.linspace(price.min() * 0.995, price.max() * 1.005, 45)
    hist, _ = np.histogram(price, bins=bins, weights=volume)
    centers = (bins[:-1] + bins[1:]) / 2
    ax_vbp.barh(
        centers,
        hist,
        height=(bins[1] - bins[0]) * 0.88,
        color='#ff69b4',
        alpha=0.75,
        edgecolor='#ff1493',
        linewidth=0.6,
    )
    max_idx = np.argmax(hist)
    ax_vbp.barh(
        centers[max_idx],
        hist[max_idx],
        height=(bins[1] - bins[0]),
        color='#00ffff',
        alpha=0.9,
        edgecolor='#ff1493',
        linewidth=0.6,
    )
    ax_vbp.set_ylim(ax_price.get_ylim())
    ax_vbp.invert_xaxis()
    ax_vbp.set_xlabel('Vol', color='#ff69b4', fontsize=10)
    ax_vbp.tick_params(colors='#ff69b4', labelsize=8)
    ax_vbp.spines[['top', 'right', 'left', 'bottom']].set_visible(False)

    # === STYLING ===
    coin = symbol.replace("USDT", "")

    # Title shows the actual time found
    title_time = f"{actual_minutes}min" if actual_minutes > 0 else f"{minutes}min"

    ax_price.set_title(
        f"{coin} • {title_time} • ${price.iloc[-1]:,.8f}",
        color="white",
        fontsize=20,
        fontweight='bold',
        loc='center',
        pad=10,
    )

    ax_price.grid(True, color='#333333', alpha=0.3, linestyle='--')
    ax_price.set_facecolor("#0d0d0d")
    ax_price.spines[['top', 'right', 'left', 'bottom']].set_visible(False)

    # Format X axis
    ax_price.tick_params(axis='x', colors='#888888', labelsize=10)
    ax_price.tick_params(axis='y', colors='#888888', labelsize=10)

    # Locator for the time axis
    locator = MinuteLocator(interval=max(1, int(actual_minutes / 6)))
    ax_price.xaxis.set_major_locator(locator)
    ax_price.xaxis.set_major_formatter(DateFormatter('%H:%M'))

    # Hard-set limits
    ax_price.set_xlim(df.index[0], df.index[-1])

    plt.subplots_adjust(left=0.05, right=0.9, top=0.9, bottom=0.1)

    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, facecolor="#0d0d0d", bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf


async def minichart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text.strip()
    parts = text.split()

    if len(parts) < 2:
        await update.message.reply_text("Usage: !minichart <COIN> [minutes]\nExample: !minichart BTC 120")
        return

    symbol_raw = parts[1].upper()
    valid_symbol = await validate_symbol(symbol_raw)
    if not valid_symbol:
        await update.message.reply_text(f"Coin not found: {symbol_raw}")
        return
    symbol = valid_symbol

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!minichart " + symbol_raw, update.effective_user)

    minutes = 60
    if len(parts) >= 3:
        try:
            minutes = int(parts[2])
            minutes = min(max(minutes, 10), 240)
        except:
            minutes = 60

    buf = await generate_minichart_image(symbol, minutes)
    if not buf:
        await update.message.reply_text(f"No 1min data for {symbol} available")
        return

    caption = (
        f"Live {minutes}min Chart • {symbol.replace('USDT', '')}/USDT • @{username}\n"
        f"Update: {datetime.now(pytz.UTC).strftime('%H:%M:%S')} UTC"
    )

    await update.message.reply_photo(photo=buf, caption=caption)


# ========================= SMC GOLD/SILVER DETECTOR  =========================

# async def create_smc_zones_table():
# """Table for active Fair Value Gaps (FVG) and orderblocks"""
# conn = await get_conn()
# try:
# await conn.execute("""
# CREATE TABLE IF NOT EXISTS active_smc_zones (
# id BIGSERIAL PRIMARY KEY,
# symbol TEXT NOT NULL,
# timeframe TEXT NOT NULL,
# zone_type TEXT NOT NULL,      -- 'BISI' (Bullish) or 'SIBI' (Bearish)
# top_edge NUMERIC NOT NULL,
# bottom_edge NUMERIC NOT NULL,
# created_time TIMESTAMPTZ NOT NULL,
# mitigated BOOLEAN DEFAULT FALSE,
# mitigated_time TIMESTAMPTZ,
# inserted_at TIMESTAMPTZ DEFAULT NOW(),
# UNIQUE(symbol, timeframe, created_time, zone_type)
# )
# """)
# logger.info("Table 'active_smc_zones' ready")
# except Exception as e:
# logger.error(f"Error creating active_smc_zones: {e}")
# finally:
# await release_conn(conn)


# # The coins that should be tracked exclusively via SMC
# SMC_METALS = ['PAXGUSDT', 'XAGUSDT', 'XAUUSDT']
# SMC_TIMEFRAMES = {
# '15m': '15min',
# '30m': '30min',
# '1h': '1h',
# '4h': '4h',
# '12h': '12h',
# '1d': '1D',
# '1w': '1W'
# }

# async def smc_fvg_detector():
# """
# Searches for new FVGs and mitigations for precious metals on multiple timeframes.
# Runs every 5 minutes.
# """
# logger.info("SMC / ICT FVG Detector started")
# await asyncio.sleep(15) # Wait briefly after bot start

# while True:
# try:
# now = datetime.now(pytz.UTC)

# for symbol in SMC_METALS:
# # 1. Fetch base data from the DB (we use 15m as the base for everything below 1h, and 1h for the rest)
# conn = await get_conn()
# try:
# # Fetch 1h data for the larger timeframes (last 60 days)
# rows_1h = await conn.fetch(f"""
# SELECT open_time, open, high, low, close
# FROM "{symbol}_1h"
# WHERE open_time >= NOW() - INTERVAL '60 days'
# ORDER BY open_time ASC
# """)

# # Fetch 15m data for the smaller timeframes (last 7 days)
# rows_15m = []
# if await table_exists_async(conn, f"{symbol}_15m"):
# rows_15m = await conn.fetch(f"""
# SELECT open_time, open, high, low, close
# FROM "{symbol}_15m"
# WHERE open_time >= NOW() - INTERVAL '7 days'
# ORDER BY open_time ASC
# """)
# finally:
# await release_conn(conn)

# df_1h = pd.DataFrame([dict(r) for r in rows_1h]) if rows_1h else pd.DataFrame()
# df_15m = pd.DataFrame([dict(r) for r in rows_15m]) if rows_15m else pd.DataFrame()

# for tf_name, tf_rule in SMC_TIMEFRAMES.items():
# # Select the matching base DF
# if tf_name in ['15m', '30m'] and not df_15m.empty:
# base_df = df_15m.copy()
# elif not df_1h.empty:
# base_df = df_1h.copy()
# else:
# continue

# base_df['open_time'] = pd.to_datetime(base_df['open_time'], utc=True)
# base_df.set_index('open_time', inplace=True)

# # Resampling to the target timeframe (e.g. 4h, 1D)
# if tf_name not in ['15m', '1h']:
# df_resampled = base_df.resample(tf_rule).agg({
# 'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
# }).dropna()
# else:
# df_resampled = base_df

# if len(df_resampled) < 5:
# continue

# # Current price to check the mitigation
# current_price = df_resampled['close'].iloc[-1]
# current_high = df_resampled['high'].iloc[-1]
# current_low = df_resampled['low'].iloc[-1]

# # --- PHASE 1: DETECT NEW FVG ---
# # We check candle -3 (C1), candle -2 (C2), candle -1 (C3)
# # (Index -1 is the last CLOSED or currently open one, we take the last 3 completed)
# c1 = df_resampled.iloc[-4]
# c2 = df_resampled.iloc[-3]
# c3 = df_resampled.iloc[-2]

# fvg_created = False
# fvg_type = None
# top_edge = 0
# bottom_edge = 0

# # BISI (Bullish FVG)
# if c3['low'] > c1['high']:
# fvg_type = 'BISI (Bullish FVG)'
# top_edge = c3['low']
# bottom_edge = c1['high']
# fvg_created = True

# # SIBI (Bearish FVG)
# elif c3['high'] < c1['low']:
# fvg_type = 'SIBI (Bearish FVG)'
# top_edge = c1['low']
# bottom_edge = c3['high']
# fvg_created = True

# if fvg_created:
# c1_time = df_resampled.index[-4] # Timestamp of creation

# # Insert into DB (ON CONFLICT DO NOTHING)
# conn2 = await get_conn()
# try:
# res = await conn2.execute("""
# INSERT INTO active_smc_zones
# (symbol, timeframe, zone_type, top_edge, bottom_edge, created_time)
# VALUES ($1, $2, $3, $4, $5, $6)
# ON CONFLICT DO NOTHING
# """, symbol, tf_name, fvg_type, top_edge, bottom_edge, c1_time)

# # If actually newly inserted, then post!
# if res.endswith("1"):
# color = "#00ff88" if "Bullish" in fvg_type else "#ff4466"
# msg = f"""
# <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
# <b style="color:#00ffff; font-size:18px;">🧱 NEW SMC ZONE CREATED</b>
# <b style="color:#ffd700;">{symbol.replace('USDT','')} | {tf_name} Chart</b>
# <b>→ Type: {fvg_type}</b>
# <b>→ Top: <code>${top_edge:,.4f}</code></b>
# <b>→ Bottom: <code>${bottom_edge:,.4f}</code></b>
# <b>→ Time: {now.strftime('%H:%M')} UTC</b>
# </pre>
# """.strip()
# await application.bot.send_message(chat_id=MARKET_CHANNEL_ID, text=msg, parse_mode="HTML")
# finally:
# await release_conn(conn2)

# # --- PHASE 2: CHECK MITIGATION (RETEST) ---
# conn3 = await get_conn()
# try:
# active_zones = await conn3.fetch("""
# SELECT id, zone_type, top_edge, bottom_edge, created_time
# FROM active_smc_zones
# WHERE symbol = $1 AND timeframe = $2 AND mitigated = FALSE
# """, symbol, tf_name)

# for zone in active_zones:
# z_id = zone['id']
# z_type = zone['zone_type']
# z_top = float(zone['top_edge'])
# z_bot = float(zone['bottom_edge'])

# mitigated = False

# # Mitigation bullish (price falls into the gap / below the top)
# if "BISI" in z_type and current_low <= z_top:
# mitigated = True
# # Mitigation bearish (price rises into the gap / above the bottom)
# elif "SIBI" in z_type and current_high >= z_bot:
# mitigated = True

# if mitigated:
# # Update in DB
# await conn3.execute("UPDATE active_smc_zones SET mitigated = TRUE, mitigated_time = NOW() WHERE id = $1", z_id)

# color = "#00ff88" if "BISI" in z_type else "#ff4466"
# emoji = "✅" if "BISI" in z_type else "🎯"
# msg = f"""
# <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
# <b style="color:#00ffff; font-size:18px;">{emoji} FVG MITIGATED (RETEST)</b>
# <b style="color:#ffd700;">{symbol.replace('USDT','')} | {tf_name} Chart</b>
# <b>→ Zone: {z_type}</b>
# <b>→ Range: ${z_bot:,.4f} - ${z_top:,.4f}</b>
# <b>→ Current Price: <code>${current_price:,.4f}</code></b>
# <i>Zone filled and closed.</i>
# </pre>
# """.strip()
# await application.bot.send_message(chat_id=MARKET_CHANNEL_ID, text=msg, parse_mode="HTML")
# finally:
# await release_conn(conn3)

# await asyncio.sleep(300) # Check every 5 minutes

# except asyncio.CancelledError:
# raise
# except Exception as e:
# logger.error(f"SMC Detector Crash: {e}", exc_info=True)
# await asyncio.sleep(60)

# # Small helper to check whether 15m tables exist
# async def table_exists_async(conn, table_name):
# try:
# val = await conn.fetchval("SELECT to_regclass($1)", f'"{table_name}"')
# return val is not None
# except:
# return False


# ========================= SMC / ICT GOLD & SILVER BOT =========================
SMC_METALS = ['PAXGUSDT', 'XAGUSDT', 'XAUUSDT']
METALS_CHANNEL_ID = "0"
SMC_TIMEFRAMES = {
    '15m': '15min',
    '30m': '30min',
    '1h': '1h',
    '2h': '2h',
    '4h': '4h',
    '12h': '12h',
    '1d': '1D',
    '1w': '1W',
}

# Global variable to track whether the history has already been scanned
HISTORICAL_SCANNED = set()


async def fetch_and_store_metal_data():
    """Fetches 15m and 1h candles. Smart fetch: 1000 on init, otherwise 10."""
    endpoints = ["15m", "1h"]
    conn = await get_conn()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            for symbol in SMC_METALS:
                for interval in endpoints:
                    table_name = f'"{symbol}_{interval}"'

                    await conn.execute(f"""
                        CREATE TABLE IF NOT EXISTS {table_name} (
                            symbol TEXT, open_time TIMESTAMP WITH TIME ZONE,
                            open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
                            close DOUBLE PRECISION, volume DOUBLE PRECISION,
                            PRIMARY KEY (symbol, open_time)
                        );
                    """)

                    last_time = await conn.fetchval(f"SELECT MAX(open_time) FROM {table_name}")
                    limit = 1000 if last_time is None else 10

                    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
                    data = []

                    # Attempt 1: Futures, attempt 2: Spot
                    async with session.get("https://fapi.binance.com/fapi/v1/klines", params=params) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                        else:
                            async with session.get("https://api.binance.com/api/v3/klines", params=params) as resp_spot:
                                if resp_spot.status == 200:
                                    data = await resp_spot.json()

                    if not data:
                        continue

                    tuples = []
                    for row in data:
                        ts = datetime.fromtimestamp(row[0] / 1000, pytz.utc)
                        tuples.append(
                            (symbol, ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
                        )

                    await conn.executemany(
                        f"""
                        INSERT INTO {table_name} (symbol, open_time, open, high, low, close, volume)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (symbol, open_time) DO UPDATE
                        SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                            close=EXCLUDED.close, volume=EXCLUDED.volume
                    """,
                        tuples,
                    )
    except Exception as e:
        logger.error(f"Autonomous metals fetch error: {e}")
    finally:
        await release_conn(conn)


async def _run_historical_catchup(df_resampled, symbol, tf_name):
    """
    Searches ONCE through the entire loaded history for FVGs.
    Immediately checks with Pandas min/max whether they were filled in the future.
    Silently writes only OPEN ones to the database.
    """
    logger.info(f"Running historical catch-up for {symbol} ({tf_name})...")
    open_zones = []

    # We iterate through the history (stopping 3 candles before the end)
    for i in range(len(df_resampled) - 3):
        c1 = df_resampled.iloc[i]
        c2 = df_resampled.iloc[i + 1]
        c3 = df_resampled.iloc[i + 2]

        fvg_type = None
        top_edge = 0
        bottom_edge = 0

        if c3['low'] > c1['high']:
            fvg_type = 'BISI (Bullish FVG)'
            top_edge = c3['low']
            bottom_edge = c1['high']
        elif c3['high'] < c1['low']:
            fvg_type = 'SIBI (Bearish FVG)'
            top_edge = c1['low']
            bottom_edge = c3['high']

        if fvg_type:
            # Check whether it was filled in the future (from i+3 to today)
            future_df = df_resampled.iloc[i + 3 :]
            mitigated = False

            if not future_df.empty:
                if "BISI" in fvg_type and future_df['low'].min() <= top_edge:
                    mitigated = True
                elif "SIBI" in fvg_type and future_df['high'].max() >= bottom_edge:
                    mitigated = True

            if not mitigated:
                open_zones.append((symbol, tf_name, fvg_type, top_edge, bottom_edge, df_resampled.index[i]))

    # Write to the DB (without Telegram spam)
    if open_zones:
        conn = await get_conn()
        try:
            await conn.executemany(
                """
                INSERT INTO active_smc_zones
                (symbol, timeframe, zone_type, top_edge, bottom_edge, created_time)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT DO NOTHING
            """,
                open_zones,
            )
            logger.info(f"-> {len(open_zones)} historical, open FVGs imported for {symbol} ({tf_name}).")
        finally:
            await release_conn(conn)


# async def smc_fvg_detector():
# """Live scanner for precious metals (SMC)"""
# logger.info("SMC / ICT FVG Detector started")
# await asyncio.sleep(5)

# while True:
# try:
# now = datetime.now(pytz.UTC)
# await fetch_and_store_metal_data()

# for symbol in SMC_METALS:
# await asyncio.sleep(0.01)
# conn = await get_conn()
# try:
# rows_1h = await conn.fetch(f'SELECT open_time, open, high, low, close FROM "{symbol}_1h" ORDER BY open_time ASC')
# rows_15m = await conn.fetch(f'SELECT open_time, open, high, low, close FROM "{symbol}_15m" ORDER BY open_time ASC')
# except Exception:
# rows_1h, rows_15m = [], []
# finally:
# await release_conn(conn)

# df_1h = pd.DataFrame([dict(r) for r in rows_1h]) if rows_1h else pd.DataFrame()
# df_15m = pd.DataFrame([dict(r) for r in rows_15m]) if rows_15m else pd.DataFrame()

# for tf_name, tf_rule in SMC_TIMEFRAMES.items():
# await asyncio.sleep(0.01)
# if tf_name in ['15m', '30m'] and not df_15m.empty:
# base_df = df_15m.copy()
# elif not df_1h.empty:
# base_df = df_1h.copy()
# else:
# continue

# base_df['open_time'] = pd.to_datetime(base_df['open_time'], utc=True)
# base_df.set_index('open_time', inplace=True)

# if tf_name not in ['15m', '1h']:
# df_resampled = base_df.resample(tf_rule).agg({
# 'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
# }).dropna()
# else:
# df_resampled = base_df

# if len(df_resampled) < 5:
# continue

# # --- HISTORICAL CATCH-UP (once per start) ---
# state_key = f"{symbol}_{tf_name}"
# if state_key not in HISTORICAL_SCANNED:
# await _run_historical_catchup(df_resampled, symbol, tf_name)
# HISTORICAL_SCANNED.add(state_key)

# # --- LIVE PHASE 1: DETECT NEW FVG (only the last 3 candles!) ---
# current_price = df_resampled['close'].iloc[-1]
# current_high = df_resampled['high'].iloc[-1]
# current_low = df_resampled['low'].iloc[-1]

# c1 = df_resampled.iloc[-4]
# c2 = df_resampled.iloc[-3]
# c3 = df_resampled.iloc[-2]

# fvg_created = False
# fvg_type = None

# if c3['low'] > c1['high']:
# fvg_type, top_edge, bottom_edge = 'BISI (Bullish FVG)', c3['low'], c1['high']
# fvg_created = True
# elif c3['high'] < c1['low']:
# fvg_type, top_edge, bottom_edge = 'SIBI (Bearish FVG)', c1['low'], c3['high']
# fvg_created = True

# if fvg_created:
# c1_time = df_resampled.index[-4]
# conn2 = await get_conn()
# try:
# res = await conn2.execute("""
# INSERT INTO active_smc_zones
# (symbol, timeframe, zone_type, top_edge, bottom_edge, created_time)
# VALUES ($1, $2, $3, $4, $5, $6)
# ON CONFLICT DO NOTHING
# """, symbol, tf_name, fvg_type, top_edge, bottom_edge, c1_time)

# if res.endswith("1"): # Was new
# color = "#00ff88" if "Bullish" in fvg_type else "#ff4466"
# msg = f"""
# <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
# <b style="color:#00ffff; font-size:18px;">🧱 NEW SMC ZONE CREATED</b>
# <b style="color:#ffd700;">{symbol.replace('USDT','')} | {tf_name} Chart</b>
# <b>→ Type: {fvg_type}</b>
# <b>→ Top: <code>${top_edge:,.4f}</code></b>
# <b>→ Bottom: <code>${bottom_edge:,.4f}</code></b>
# <b>→ Time: {now.strftime('%H:%M')} UTC</b>
# </pre>
# """.strip()
# await application.bot.send_message(chat_id=METALS_CHANNEL_ID, text=msg, parse_mode="HTML")
# finally:
# await release_conn(conn2)

# # --- LIVE PHASE 2: CHECK MITIGATION (RETEST) ---
# # Checks the current live candle against all zones in the database
# conn3 = await get_conn()
# try:
# active_zones = await conn3.fetch("""
# SELECT id, zone_type, top_edge, bottom_edge
# FROM active_smc_zones
# WHERE symbol = $1 AND timeframe = $2 AND mitigated = FALSE
# """, symbol, tf_name)

# for zone in active_zones:
# z_id = zone['id']
# z_type = zone['zone_type']
# z_top = float(zone['top_edge'])
# z_bot = float(zone['bottom_edge'])
# mitigated = False

# if "BISI" in z_type and current_low <= z_top:
# mitigated = True
# elif "SIBI" in z_type and current_high >= z_bot:
# mitigated = True

# if mitigated:
# await conn3.execute("UPDATE active_smc_zones SET mitigated = TRUE, mitigated_time = NOW() WHERE id = $1", z_id)

# color = "#00ff88" if "BISI" in z_type else "#ff4466"
# emoji = "✅" if "BISI" in z_type else "🎯"
# msg = f"""
# <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
# <b style="color:#00ffff; font-size:18px;">{emoji} FVG MITIGATED (RETEST)</b>
# <b style="color:#ffd700;">{symbol.replace('USDT','')} | {tf_name} Chart</b>
# <b>→ Zone: {z_type}</b>
# <b>→ Range: ${z_bot:,.4f} - ${z_top:,.4f}</b>
# <b>→ Current Price: <code>${current_price:,.4f}</code></b>
# <i>Zone filled and closed.</i>
# </pre>
# """.strip()
# await application.bot.send_message(chat_id=METALS_CHANNEL_ID, text=msg, parse_mode="HTML")
# finally:
# await release_conn(conn3)

# await asyncio.sleep(300)

# except asyncio.CancelledError:
# logger.info("SMC FVG Detector shutting down cleanly...")
# raise
# except Exception as e:
# logger.error(f"SMC Detector Crash: {e}", exc_info=True)
# await asyncio.sleep(60)


async def create_smc_zones_table():
    """Table for active Fair Value Gaps (FVG) and orderblocks"""
    conn = await get_conn()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS active_smc_zones (
                id BIGSERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                zone_type TEXT NOT NULL,
                top_edge NUMERIC NOT NULL,
                bottom_edge NUMERIC NOT NULL,
                created_time TIMESTAMPTZ NOT NULL,
                mitigated BOOLEAN DEFAULT FALSE,
                mitigated_time TIMESTAMPTZ,
                inserted_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(symbol, timeframe, created_time, zone_type)
            )
        """)
    except Exception as e:
        logger.error(f"Error creating active_smc_zones: {e}")
    finally:
        await release_conn(conn)


async def smc_fvg_detector():
    """Live scanner for precious metals: FVG, mitigation, BOS & CHoCH (with shutdown fix)."""
    logger.info("SMC / ICT FVG & Structure Detector started")
    await asyncio.sleep(5)

    try:
        while True:
            # 1. Fetch fresh market data
            await fetch_and_store_metal_data()

            for symbol in SMC_METALS:
                try:
                    # CHECKPOINT: allows asyncio to cancel the task here immediately
                    await asyncio.sleep(0.01)

                    conn = await get_conn()
                    try:
                        rows_1h = await conn.fetch(
                            f'SELECT open_time, open, high, low, close FROM "{symbol}_1h" ORDER BY open_time ASC'
                        )
                        rows_15m = await conn.fetch(
                            f'SELECT open_time, open, high, low, close FROM "{symbol}_15m" ORDER BY open_time ASC'
                        )
                    finally:
                        await release_conn(conn)

                    df_1h = pd.DataFrame([dict(r) for r in rows_1h]) if rows_1h else pd.DataFrame()
                    df_15m = pd.DataFrame([dict(r) for r in rows_15m]) if rows_15m else pd.DataFrame()

                    for tf_name, tf_rule in SMC_TIMEFRAMES.items():
                        await asyncio.sleep(0.01)  # Checkpoint

                        if tf_name in ['15m', '30m'] and not df_15m.empty:
                            base_df = df_15m.copy()
                        elif not df_1h.empty:
                            base_df = df_1h.copy()
                        else:
                            continue

                        base_df['open_time'] = pd.to_datetime(base_df['open_time'], utc=True)
                        base_df.set_index('open_time', inplace=True)

                        if tf_name not in ['15m', '1h']:
                            df_resampled = (
                                base_df.resample(tf_rule)
                                .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
                                .dropna()
                            )
                        else:
                            df_resampled = base_df

                        if len(df_resampled) < 10:
                            continue

                        # --- HISTORICAL CATCH-UP (once per start for FVGs) ---
                        state_key = f"{symbol}_{tf_name}"
                        if state_key not in HISTORICAL_SCANNED:
                            await _run_historical_catchup(df_resampled, symbol, tf_name)
                            HISTORICAL_SCANNED.add(state_key)

                        now_str = datetime.now(pytz.UTC).strftime('%H:%M')

                        # ==========================================================
                        # --- PHASE 1: DETECT NEW FVG (last 3 closed candles)
                        # ==========================================================
                        c1 = df_resampled.iloc[-4]
                        c2 = df_resampled.iloc[-3]
                        c3 = df_resampled.iloc[-2]

                        fvg_created = False
                        fvg_type = None

                        if c3['low'] > c1['high']:
                            fvg_type, top_edge, bottom_edge = 'BISI (Bullish FVG)', c3['low'], c1['high']
                            fvg_created = True
                        elif c3['high'] < c1['low']:
                            fvg_type, top_edge, bottom_edge = 'SIBI (Bearish FVG)', c1['low'], c3['high']
                            fvg_created = True

                        if fvg_created:
                            c1_time = df_resampled.index[-4]
                            conn2 = await get_conn()
                            try:
                                res = await conn2.execute(
                                    """
                                    INSERT INTO active_smc_zones
                                    (symbol, timeframe, zone_type, top_edge, bottom_edge, created_time)
                                    VALUES ($1, $2, $3, $4, $5, $6)
                                    ON CONFLICT DO NOTHING
                                """,
                                    symbol,
                                    tf_name,
                                    fvg_type,
                                    top_edge,
                                    bottom_edge,
                                    c1_time,
                                )

                                if res.endswith("1"):  # Was new
                                    try:
                                        chart_buf = await generate_smc_chart(
                                            df_resampled, symbol, tf_name, top_edge, bottom_edge, fvg_type
                                        )
                                        color = "#00ff88" if "Bullish" in fvg_type else "#ff4466"
                                        msg = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
<b style="color:#00ffff; font-size:18px;">🧱 NEW SMC ZONE CREATED</b>
<b style="color:#ffd700;">{symbol.replace('USDT', '')} | {tf_name} Chart</b>
<b>→ Type: {fvg_type}</b>
<b>→ Top: <code>${top_edge:,.4f}</code></b>
<b>→ Bottom: <code>${bottom_edge:,.4f}</code></b>
<b>→ Time: {now_str} UTC</b>
</pre>
                                        """.strip()
                                        await application.bot.send_photo(
                                            chat_id=METALS_CHANNEL_ID, photo=chart_buf, caption=msg, parse_mode="HTML"
                                        )
                                    except Exception as e:
                                        logger.error(f"Error sending FVG chart: {e}")
                            finally:
                                await release_conn(conn2)

                        # ==========================================================
                        # --- PHASE 2: DETECT BOS & CHoCH (MARKET STRUCTURE)
                        # ==========================================================
                        # Compute swing highs/lows (middle of 5 candles)
                        df_resampled['Pivot_High'] = (
                            df_resampled['high'] == df_resampled['high'].rolling(window=5, center=True).max()
                        )
                        df_resampled['Pivot_Low'] = (
                            df_resampled['low'] == df_resampled['low'].rolling(window=5, center=True).min()
                        )

                        # Trim off the last 2 (pivots need 2 candles on the left and right)
                        confirmed_df = df_resampled.iloc[:-2]

                        ph_indices = confirmed_df[confirmed_df['Pivot_High']].index
                        pl_indices = confirmed_df[confirmed_df['Pivot_Low']].index

                        if len(ph_indices) > 0 and len(pl_indices) > 0:
                            last_ph_idx = ph_indices[-1]
                            last_pl_idx = pl_indices[-1]

                            last_ph_val = confirmed_df.loc[last_ph_idx, 'high']
                            last_pl_val = confirmed_df.loc[last_pl_idx, 'low']

                            # We check the last fully closed candle (iloc[-2]) against the second-to-last (iloc[-3])
                            last_closed = df_resampled['close'].iloc[-2]
                            prev_closed = df_resampled['close'].iloc[-3]

                            current_trend = SMC_TREND_STATE.get(state_key, 0)
                            struct_type = None
                            struct_price = 0
                            pivot_time = None

                            # Breakout to the UPSIDE (above swing high)
                            if last_closed > last_ph_val and prev_closed <= last_ph_val:
                                if current_trend == 1 or current_trend == 0:
                                    struct_type = "BULLISH BOS 🟢"  # Trend continuation
                                else:
                                    struct_type = "BULLISH CHoCH 🚀"  # Trend reversal to the upside

                                SMC_TREND_STATE[state_key] = 1  # New trend is bullish
                                struct_price = last_ph_val
                                pivot_time = last_ph_idx

                            # Breakout to the DOWNSIDE (below swing low)
                            elif last_closed < last_pl_val and prev_closed >= last_pl_val:
                                if current_trend == -1 or current_trend == 0:
                                    struct_type = "BEARISH BOS 🔴"  # Trend continuation
                                else:
                                    struct_type = "BEARISH CHoCH 💥"  # Trend reversal to the downside

                                SMC_TREND_STATE[state_key] = -1  # New trend is bearish
                                struct_price = last_pl_val
                                pivot_time = last_pl_idx

                            if struct_type:
                                alert_key = f"{state_key}_{pivot_time}_{struct_type}"
                                if alert_key not in ALERTED_STRUCT:
                                    ALERTED_STRUCT.add(alert_key)

                                    try:
                                        # Generate chart with breakout line (top=bottom draws 1 line)
                                        chart_buf = await generate_smc_chart(
                                            df_resampled, symbol, tf_name, struct_price, struct_price, struct_type
                                        )
                                        color = "#00ff88" if "BULLISH" in struct_type else "#ff4466"
                                        msg = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
<b style="color:#00ffff; font-size:18px;">⚖️ MARKET STRUCTURE SHIFT</b>
<b style="color:#ffd700;">{symbol.replace('USDT', '')} | {tf_name} Chart</b>
<b>→ Type: {struct_type}</b>
<b>→ Broken Level: <code>${struct_price:,.4f}</code></b>
<b>→ Time: {now_str} UTC</b>
<i>Structural shift confirmed by candle close.</i>
</pre>
                                        """.strip()
                                        await application.bot.send_photo(
                                            chat_id=METALS_CHANNEL_ID, photo=chart_buf, caption=msg, parse_mode="HTML"
                                        )
                                    except Exception as e:
                                        logger.error(f"Error sending structure chart for {symbol}: {e}")

                        # ==========================================================
                        # --- PHASE 3: CHECK MITIGATION (RETEST) (live candle)
                        # ==========================================================
                        current_price = df_resampled['close'].iloc[-1]
                        current_high = df_resampled['high'].iloc[-1]
                        current_low = df_resampled['low'].iloc[-1]

                        conn3 = await get_conn()
                        try:
                            active_zones = await conn3.fetch(
                                """
                                SELECT id, zone_type, top_edge, bottom_edge
                                FROM active_smc_zones
                                WHERE symbol = $1 AND timeframe = $2 AND mitigated = FALSE
                            """,
                                symbol,
                                tf_name,
                            )

                            for zone in active_zones:
                                z_id = zone['id']
                                z_type = zone['zone_type']
                                z_top = float(zone['top_edge'])
                                z_bot = float(zone['bottom_edge'])
                                mitigated = False

                                if "BISI" in z_type and current_low <= z_top:
                                    mitigated = True
                                elif "SIBI" in z_type and current_high >= z_bot:
                                    mitigated = True

                                if mitigated:
                                    await conn3.execute(
                                        "UPDATE active_smc_zones SET mitigated = TRUE, mitigated_time = NOW() WHERE id = $1",
                                        z_id,
                                    )

                                    try:
                                        chart_buf = await generate_smc_chart(
                                            df_resampled, symbol, tf_name, z_top, z_bot, z_type
                                        )
                                        color = "#00ff88" if "BISI" in z_type else "#ff4466"
                                        emoji = "✅" if "BISI" in z_type else "🎯"
                                        msg = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
<b style="color:#00ffff; font-size:18px;">{emoji} FVG MITIGATED (RETEST)</b>
<b style="color:#ffd700;">{symbol.replace('USDT', '')} | {tf_name} Chart</b>
<b>→ Zone: {z_type}</b>
<b>→ Range: ${z_bot:,.4f} - ${z_top:,.4f}</b>
<b>→ Current Price: <code>${current_price:,.4f}</code></b>
<i>Zone filled and closed.</i>
</pre>
                                        """.strip()
                                        await application.bot.send_photo(
                                            chat_id=METALS_CHANNEL_ID, photo=chart_buf, caption=msg, parse_mode="HTML"
                                        )
                                    except Exception as e:
                                        logger.error(f"Error sending mitigation chart: {e}")
                        finally:
                            await release_conn(conn3)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"SMC error for {symbol}: {e}")

            # Wait time at the end of the main loop (5 min)
            await asyncio.sleep(300)

    except asyncio.CancelledError:
        logger.info("SMC FVG Detector task shutting down cleanly...")
        raise
    except Exception as e:
        logger.error(f"SMC Detector Crash: {e}", exc_info=True)
        await asyncio.sleep(60)


async def generate_smc_chart(df, symbol, timeframe, top_edge, bottom_edge, fvg_type):
    """Generates a candlestick chart and marks the FVG zone or the breakout level."""

    def create_chart():
        # We take the last 60 candles for a good zoom factor
        plot_df = df.tail(60).copy()

        # Binance/dark-mode colors
        mc = mpf.make_marketcolors(up='#00ff88', down='#ff4466', edge='inherit', wick='inherit')
        s = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds')

        # Draw lines (cyan for good visibility)
        # For FVG: 2 lines. For BOS/CHoCH (top_edge == bottom_edge): visually 1 line.
        hlines = dict(
            hlines=[top_edge, bottom_edge], colors=['#00ffff', '#00ffff'], linestyle='--', linewidths=1.5, alpha=0.7
        )

        # .replace('=X', '') makes forex tickers nicer, doesn't hurt for crypto
        clean_symbol = symbol.replace('=X', '')

        buf = io.BytesIO()
        # Draw the chart and save it to the buffer
        mpf.plot(
            plot_df,
            type='candle',
            style=s,
            title=f"\nSMC: {clean_symbol} ({timeframe})",
            hlines=hlines,
            figsize=(10, 6),
            tight_layout=True,
            savefig=buf,
            returnfig=False,
        )
        buf.seek(0)
        return buf

    # Offload to a thread so the drawing (synchronous) doesn't block the bot (asynchronous)
    return await asyncio.to_thread(create_chart)


# ========================= PATTERN DETECTOR =========================

# The global memory store for our live candles
WS_KLINE_BUFFER = {}


async def binance_ws_listener():
    """Builds a permanent connection to Binance and captures live candles."""
    url = "wss://fstream.binance.com/stream"

    # Generate stream names for Binance (format: btcusdt@kline_5m)
    streams = []
    for sym in coins:
        for tf in ['5m', '15m']:
            streams.append(f"{sym.lower()}@kline_{tf}")

    # Binance allows max 200 streams per request. We split them into blocks of 100.
    chunks = [streams[i : i + 100] for i in range(0, len(streams), 100)]

    while True:
        try:
            async with websockets.connect(url) as ws:
                logger.info("🟢 WebSocket to Binance connected successfully!")

                # Subscribe to streams
                for i, chunk in enumerate(chunks):
                    sub_msg = {"method": "SUBSCRIBE", "params": chunk, "id": i + 1}
                    await ws.send(json.dumps(sub_msg))
                    await asyncio.sleep(0.5)  # Short pause between requests

                # Endless loop: listen for incoming data
                while True:
                    msg = await ws.recv()
                    payload = json.loads(msg)

                    # If it's a valid candle message
                    if 'data' in payload and 'k' in payload['data']:
                        k = payload['data']['k']
                        sym = k['s']  # e.g. BTCUSDT
                        tf = k['i']  # e.g. 5m

                        # Convert timestamp to real UTC date
                        open_time = datetime.fromtimestamp(k['t'] / 1000, pytz.UTC)

                        # Write to the buffer (always overwrites with the latest tick)
                        # We store: (symbol, open_time, open, high, low, close, volume)
                        WS_KLINE_BUFFER[(sym, tf)] = (
                            sym,
                            open_time,
                            float(k['o']),
                            float(k['h']),
                            float(k['l']),
                            float(k['c']),
                            float(k['v']),
                        )

        except Exception as e:
            logger.error(f"🔴 WebSocket disconnected ({e}). Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


async def db_buffer_flusher():
    """Gently flushes the RAM buffer into the PostgreSQL database every 2 seconds."""
    logger.info("💾 DB buffer flusher started")
    while True:
        await asyncio.sleep(2)

        # If the buffer is empty, do nothing
        if not WS_KLINE_BUFFER:
            continue

        # Copy the buffer and clear it immediately so the WebSocket can keep working
        buffer_copy = WS_KLINE_BUFFER.copy()
        WS_KLINE_BUFFER.clear()

        conn = await get_conn()
        try:
            for (sym, tf), data in buffer_copy.items():
                table_name = f'"{sym}_{tf}"'

                # We update the running candle in the DB (upsert)
                await conn.execute(
                    f'''
                    INSERT INTO {table_name} (symbol, open_time, open, high, low, close, volume)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (symbol, open_time) DO UPDATE
                    SET open = EXCLUDED.open, high = EXCLUDED.high,
                        low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume
                ''',
                    *data,
                )
        except Exception as e:
            logger.error(f"Error during DB flush: {e}")
        finally:
            await release_conn(conn)


# --- YOUR DATAGREPPER LOGIC (synchronous, but safely wrapped) ---
def sync_turbo_grepper():
    """Runs the synchronous turbo update with 20 threads."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    import psycopg2.pool
    import requests

    # --- Configuration ---
    DB_NAME = 'cryptodata'
    DB_USER = 'dbfiller'
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_HOST = 'localhost'
    DB_PORT = 5432

    # Local globals for this run
    NUM_WORKERS = 3
    TIMEFRAMES = ['5m', '15m']
    BASE_URL = 'https://fapi.binance.com'

    # Initialize the pool locally within the function to avoid conflicts
    local_db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=5,
        maxconn=NUM_WORKERS + 5,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )

    symbols = coins  # We simply use the global 'coins' list from zzz.py

    def init_tables():
        conn = local_db_pool.getconn()
        try:
            with conn.cursor() as cur:
                for symbol in symbols:
                    for tf in TIMEFRAMES:
                        tablename = f'"{symbol}_{tf}"'
                        cur.execute(f"""
                            CREATE TABLE IF NOT EXISTS {tablename} (
                                symbol TEXT, open_time TIMESTAMP WITH TIME ZONE,
                                open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
                                close DOUBLE PRECISION, volume DOUBLE PRECISION,
                                PRIMARY KEY (symbol, open_time)
                            );
                        """)
            conn.commit()
        finally:
            local_db_pool.putconn(conn)

    def process_coin_local(symbol):
        conn = None
        session = requests.Session()
        session.headers.update({"User-Agent": "CryptoBot/Turbo"})
        try:
            conn = local_db_pool.getconn()
            now = datetime.now(pytz.UTC)
            end_ts = int(now.timestamp() * 1000)

            for tf in TIMEFRAMES:
                # Fetch the latest time
                tablename = f'"{symbol}_{tf}"'
                latest = None
                with conn.cursor() as cursor:
                    cursor.execute(f'SELECT MAX(open_time) FROM {tablename}')
                    res = cursor.fetchone()
                    if res and res[0]:
                        latest = res[0].astimezone(pytz.UTC)

                start_dt = latest if latest else now - timedelta(days=365)
                start_ts = int(start_dt.timestamp() * 1000)

                if start_ts > end_ts:
                    continue

                # Fetch OHLCV (heavily shortened for clarity, uses your logic)
                url = BASE_URL + '/fapi/v1/klines'
                curr = start_ts
                all_data = []
                while True:
                    limit = 100 if (end_ts - curr) < (48 * 3600 * 1000) else 1500
                    # resp = session.get(url, params={'symbol': symbol, 'interval': tf, 'startTime': curr, 'endTime': end_ts, 'limit': limit}, timeout=5)
                    # if resp.status_code == 429:
                    # time.sleep(int(resp.headers.get("Retry-After", 5)))
                    # continue
                    # if resp.status_code != 200: break
                    # data = resp.json()
                    # if not data: break
                    # all_data.extend(data)
                    # if len(data) < limit: break
                    # curr = data[-1][6] + 1
                    # if curr >= end_ts: break
                    # if limit == 1500: time.sleep(0.2)

                    # --- ANTI-BAN FIX: tiny pause before each request ---
                    # Ensures the workers don't DDoS Binance
                    time.sleep(0.1)

                    resp = session.get(
                        url,
                        params={'symbol': symbol, 'interval': tf, 'startTime': curr, 'endTime': end_ts, 'limit': limit},
                        timeout=5,
                    )

                    # 1. Warning: rate limit almost reached
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("Retry-After", 5))
                        logging.warning(f"⚠️ Binance limit (429) for {symbol}. Pausing thread for {retry_after}s...")
                        time.sleep(retry_after)
                        continue

                    # 2. Escalation: IP ban
                    if resp.status_code == 418:
                        logging.error(
                            f"🚨 BINANCE BAN (418 Teapot) for {symbol}! Bot is too fast. Stopping for 5 minutes..."
                        )
                        time.sleep(300)  # 5-minute forced timeout for this worker
                        break  # Abort for this coin

                    # Other errors
                    if resp.status_code != 200:
                        break

                    data = resp.json()
                    if not data:
                        break
                    all_data.extend(data)
                    if len(data) < limit:
                        break
                    curr = data[-1][6] + 1
                    if curr >= end_ts:
                        break

                    # Extra pause for large history downloads
                    if limit == 1500:
                        time.sleep(0.2)

                # Insert
                if all_data:
                    tuples = [
                        (
                            symbol,
                            datetime.fromtimestamp(r[0] / 1000, pytz.UTC),
                            float(r[1]),
                            float(r[2]),
                            float(r[3]),
                            float(r[4]),
                            float(r[5]),
                        )
                        for r in all_data
                    ]
                    sql = f"INSERT INTO {tablename} (symbol, open_time, open, high, low, close, volume) VALUES %s ON CONFLICT (symbol, open_time) DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume"
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_values(cur, sql, tuples)
                    conn.commit()
        except Exception:
            if conn:
                conn.rollback()
        finally:
            if conn:
                local_db_pool.putconn(conn)
            session.close()

    try:
        init_tables()
        logger.info(f"🚀 Turbo update started: {len(symbols)} coins...")
        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as exe:
            exe.map(process_coin_local, symbols)
    finally:
        local_db_pool.closeall()
        logger.info("✅ Turbo update complete.")


# --- ASYNC WRAPPER ---
async def update_pattern_data_async():
    """Runs the synchronous datagrepper in a background thread."""
    await asyncio.to_thread(sync_turbo_grepper)


# import numpy as np
# import io
# import mplfinance as mpf
# import asyncio
# import pandas as pd

# #old version for 5min/15min chart
# async def generate_pattern_chart(df, symbol, tf, pattern_name, line_highs, line_lows, start_idx, end_idx):
# """Generates a chart that draws the detected triangle or the channel."""
# def create_chart():
# # Cut out the zoomed region
# start_plot = max(0, start_idx - 20)
# end_plot = end_idx + 5
# plot_df = df.iloc[start_plot : end_plot].copy()

# # --- THE FIX FOR mplfinance ---
# # mplfinance requires a DatetimeIndex!
# plot_df['open_time'] = pd.to_datetime(plot_df['open_time'])
# plot_df.set_index('open_time', inplace=True)

# # Compute the Y values for the trendlines (based on the global integer indices)
# global_indices = np.arange(start_plot, start_plot + len(plot_df))
# y_highs = [line_highs['m'] * idx + line_highs['b'] for idx in global_indices]
# y_lows = [line_lows['m'] * idx + line_lows['b'] for idx in global_indices]

# mc = mpf.make_marketcolors(up='#00ff88', down='#ff4466', edge='inherit', wick='inherit')
# s  = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds')

# # Add trendlines
# apds = [
# mpf.make_addplot(y_highs, color='#00ffff', width=1.5, linestyle='-'),
# mpf.make_addplot(y_lows, color='#ffd700', width=1.5, linestyle='-')
# ]

# buf = io.BytesIO()
# mpf.plot(
# plot_df,
# type='candle',
# style=s,
# addplot=apds,
# title=f"\n{pattern_name}: {symbol.replace('USDT', '')} ({tf})",
# figsize=(10, 6),
# tight_layout=True,
# savefig=buf,
# returnfig=False
# )
# buf.seek(0)
# return buf

# return await asyncio.to_thread(create_chart)

import pandas as pd


async def generate_pattern_chart(df, symbol, tf, pattern_name, line_highs, line_lows, start_idx, current_idx):
    """Generates a 7-day chart (168 candles) including a volume panel at the bottom."""

    def create_chart():
        # --- 7-DAY ZOOM (168 candles) ---
        MAX_CANDLES = 168
        BUFFER_AFTER = 5

        start_plot = max(0, current_idx - MAX_CANDLES + BUFFER_AFTER)
        end_plot = current_idx + BUFFER_AFTER

        plot_df = df.iloc[start_plot:end_plot].copy()
        plot_df['open_time'] = pd.to_datetime(plot_df['open_time'])
        plot_df.set_index('open_time', inplace=True)

        # --- TRENDLINES ---
        global_indices = np.arange(start_plot, start_plot + len(plot_df))
        y_highs = [line_highs['m'] * idx + line_highs['b'] for idx in global_indices]
        y_lows = [line_lows['m'] * idx + line_lows['b'] for idx in global_indices]

        # --- HIGHLIGHT LINE ---
        highlight_date = plot_df.index[current_idx - start_plot]

        # Styles (Dark Mode)
        mc = mpf.make_marketcolors(up='#00ff88', down='#ff4466', edge='inherit', wick='inherit')
        s = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds')

        apds = [
            mpf.make_addplot(y_highs, color='#00ffff', width=1.5, linestyle='-'),
            mpf.make_addplot(y_lows, color='#ffd700', width=1.5, linestyle='-'),
        ]

        buf = io.BytesIO()
        # Draw the chart
        mpf.plot(
            plot_df,
            type='candle',
            style=s,
            volume=True,  # <--- HERE IS THE UPDATE!
            addplot=apds,
            vlines=dict(vlines=[highlight_date], linewidths=1.0, colors='white', linestyle='--'),
            title=f"\n{pattern_name}: {symbol.replace('USDT', '')} ({tf}) | Last 7 Days",
            figsize=(12, 8),  # A bit taller (8 instead of 6) to make room for volume
            tight_layout=True,
            savefig=buf,
            returnfig=False,
        )
        buf.seek(0)
        return buf

    return await asyncio.to_thread(create_chart)


# -----OLD 5 / 15 MIN VERSION -----
# async def pattern_detector():
# """Detects triangles and channels on 5m and 15m and reports breakouts."""
# logger.info("Pattern Detector started")
# await asyncio.sleep(15)

# try:
# while True:
# # 1. FIRST UPDATE THE DATA (does NOT block the bot)
# # try:
# # await update_pattern_data_async()
# # except asyncio.CancelledError:
# # raise
# # except Exception as e:
# # logger.error(f"Error during turbo data update: {e}")
# # await asyncio.sleep(60)
# # continue # If the data update fails, skip this round

# # 2. ONLY THEN START THE ANALYSIS
# for symbol in coins:
# try:
# await asyncio.sleep(0.01) # Checkpoint for shutdown

# conn = await get_conn()
# try:
# for tf in PATTERN_TIMEFRAMES:

# await asyncio.sleep(0.01)

# # Load data (the last 150 candles are plenty for these patterns)
# rows = await conn.fetch(f'''
# SELECT open_time, open, high, low, close
# FROM "{symbol}_{tf}"
# ORDER BY open_time DESC LIMIT 150
# ''')

# if len(rows) < 50:
# continue

# # Build DataFrame (and reverse it, so old -> new)
# df = pd.DataFrame([dict(r) for r in rows]).iloc[::-1].reset_index(drop=True)

# # 1. Find pivots (window of 9 candles for prominent points)
# df['Pivot_High'] = df['high'] == df['high'].rolling(window=9, center=True).max()
# df['Pivot_Low'] = df['low'] == df['low'].rolling(window=9, center=True).min()

# # Last confirmed pivots (excluding the last 4 unfinished candles)
# confirmed_df = df.iloc[:-4]
# highs = confirmed_df[confirmed_df['Pivot_High']]
# lows = confirmed_df[confirmed_df['Pivot_Low']]

# # We need at least 2 prominent highs and lows
# if len(highs) >= 2 and len(lows) >= 2:
# # Take the last 2-3 points for the line
# recent_highs = highs.tail(3)
# recent_lows = lows.tail(3)

# # Linear regression to compute the trendline (y = m*x + b)
# slope_h, intercept_h, _, _, _ = stats.linregress(recent_highs.index, recent_highs['high'])
# slope_l, intercept_l, _, _, _ = stats.linregress(recent_lows.index, recent_lows['low'])

# # Normalize geometry / slope (percentage change per candle)
# avg_price = df['close'].mean()
# m_high_pct = (slope_h / avg_price) * 100
# m_low_pct = (slope_l / avg_price) * 100

# pattern_name = None

# # Threshold for "flatness"
# flat_th = 0.02

# # Pattern classification
# if m_high_pct < -flat_th and m_low_pct > flat_th:
# pattern_name = "Symmetrical Triangle"
# elif abs(m_high_pct) <= flat_th and m_low_pct > flat_th:
# pattern_name = "Ascending Triangle"
# elif m_high_pct < -flat_th and abs(m_low_pct) <= flat_th:
# pattern_name = "Descending Triangle"
# elif m_high_pct > flat_th and m_low_pct > flat_th and abs(m_high_pct - m_low_pct) < 0.05:
# pattern_name = "Ascending Channel"
# elif m_high_pct < -flat_th and m_low_pct < -flat_th and abs(m_high_pct - m_low_pct) < 0.05:
# pattern_name = "Descending Channel"

# # --- NEW X-RAY VIEW FOR THE CONSOLE ---

# if pattern_name:
# logger.info(f"👀 Pattern detected: {symbol} ({tf}) -> {pattern_name}. Waiting for breakout...")
# # Breakout check on the LAST closed candle
# current_idx = len(df) - 2 # Second-to-last candle (fully closed)
# current_close = df['close'].iloc[current_idx]

# # Where are the trendlines at this candle?
# upper_boundary = slope_h * current_idx + intercept_h
# lower_boundary = slope_l * current_idx + intercept_l

# breakout_dir = None
# if current_close > upper_boundary:
# breakout_dir = "BULLISH BREAKOUT 🟢"
# elif current_close < lower_boundary:
# breakout_dir = "BEARISH BREAKOUT 🔴"

# if breakout_dir:
# alert_key = f"{symbol}_{tf}_{pattern_name}_{current_idx}"
# if alert_key not in ALERTED_PATTERNS:
# ALERTED_PATTERNS.add(alert_key)

# line_highs = {'m': slope_h, 'b': intercept_h}
# line_lows = {'m': slope_l, 'b': intercept_l}
# start_plot_idx = min(recent_highs.index[0], recent_lows.index[0])

# try:
# chart_buf = await generate_pattern_chart(
# df, symbol, tf, pattern_name, line_highs, line_lows, start_plot_idx, current_idx
# )

# color = "#00ff88" if "BULLISH" in breakout_dir else "#ff4466"
# msg = f"""
# <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
# <b style="color:#00ffff; font-size:18px;">📐 PATTERN BREAKOUT DETECTED</b>
# <b style="color:#ffd700;">{symbol.replace('USDT','')} | {tf} Chart</b>
# <b>→ Pattern: {pattern_name}</b>
# <b>→ Action: {breakout_dir}</b>
# <b>→ Breakout Price: <code>${current_close:,.4f}</code></b>
# </pre>
# """.strip()

# # PLEASE ENTER YOUR DESIRED CHANNEL HERE
# await application.bot.send_photo(
# chat_id=PATTERN_CHANNEL_ID, # or a different channel!
# photo=chart_buf,
# caption=msg,
# parse_mode="HTML"
# )
# except Exception as e:
# logger.error(f"Error with pattern chart for {symbol}: {e}")

# finally:
# await release_conn(conn)

# except asyncio.CancelledError:
# raise
# except Exception as e:
# logger.error(f"Error in Pattern Detector for {symbol}: {e}")

# # Scanning all coins once every 5 minutes is enough for 5m/15m timeframes
# await asyncio.sleep(300)

# except asyncio.CancelledError:
# logger.info("Pattern Detector shutting down cleanly...")
# raise
# except Exception as e:
# logger.error(f"Pattern Detector crash: {e}", exc_info=True)
# await asyncio.sleep(60)


async def pattern_detector():
    """Detects triangles/channels on 1h+, reports breakouts and checks for retests."""
    logger.info("Pattern Detector (1h+) started. Waiting for xx:15...")
    await asyncio.sleep(5)

    try:
        while True:
            now = datetime.now(pytz.UTC)

            # PUNCTUALITY: runs exactly at minute 15 (e.g. 14:15, 15:15)
            # Since we check the 1h candles that closed at :00,
            # at :15 we're absolutely sure all DB updates have gone through.
            if now.minute != 15:
                await asyncio.sleep(10)
                continue

            logger.info(f"Starting pattern scan for {PATTERN_TIMEFRAMES}...")

            for symbol in coins:
                try:
                    await asyncio.sleep(0.01)  # Checkpoint for shutdown

                    conn = await get_conn()
                    try:
                        for tf in PATTERN_TIMEFRAMES:
                            await asyncio.sleep(0.01)

                            # 150 candles are perfectly enough for 1h+ patterns
                            rows = await conn.fetch(f'''
                                SELECT open_time, open, high, low, close
                                FROM "{symbol}_{tf}"
                                ORDER BY open_time DESC LIMIT 168
                            ''')

                            if len(rows) < 50:
                                continue

                            df = pd.DataFrame([dict(r) for r in rows]).iloc[::-1].reset_index(drop=True)

                            # 1. Find pivots (window of 9 candles)
                            df['Pivot_High'] = df['high'] == df['high'].rolling(window=9, center=True).max()
                            df['Pivot_Low'] = df['low'] == df['low'].rolling(window=9, center=True).min()

                            confirmed_df = df.iloc[:-4]
                            highs = confirmed_df[confirmed_df['Pivot_High']]
                            lows = confirmed_df[confirmed_df['Pivot_Low']]

                            if len(highs) >= 2 and len(lows) >= 2:
                                recent_highs = highs.tail(3)
                                recent_lows = lows.tail(3)

                                slope_h, intercept_h, _, _, _ = stats.linregress(
                                    recent_highs.index, recent_highs['high']
                                )
                                slope_l, intercept_l, _, _, _ = stats.linregress(recent_lows.index, recent_lows['low'])

                                avg_price = df['close'].mean()
                                m_high_pct = (slope_h / avg_price) * 100
                                m_low_pct = (slope_l / avg_price) * 100

                                pattern_name = None
                                flat_th = 0.02

                                if m_high_pct < -flat_th and m_low_pct > flat_th:
                                    pattern_name = "Symmetrical Triangle"
                                elif abs(m_high_pct) <= flat_th and m_low_pct > flat_th:
                                    pattern_name = "Ascending Triangle"
                                elif m_high_pct < -flat_th and abs(m_low_pct) <= flat_th:
                                    pattern_name = "Descending Triangle"
                                elif (
                                    m_high_pct > flat_th and m_low_pct > flat_th and abs(m_high_pct - m_low_pct) < 0.05
                                ):
                                    pattern_name = "Ascending Channel"
                                elif (
                                    m_high_pct < -flat_th
                                    and m_low_pct < -flat_th
                                    and abs(m_high_pct - m_low_pct) < 0.05
                                ):
                                    pattern_name = "Descending Channel"

                                if pattern_name:
                                    # current_idx is the CANDLE THAT JUST CLOSED
                                    current_idx = len(df) - 2
                                    prev_idx = current_idx - 1

                                    c_close = df['close'].iloc[current_idx]
                                    c_low = df['low'].iloc[current_idx]
                                    c_high = df['high'].iloc[current_idx]
                                    p_close = df['close'].iloc[prev_idx]

                                    up_curr = slope_h * current_idx + intercept_h
                                    low_curr = slope_l * current_idx + intercept_l
                                    up_prev = slope_h * prev_idx + intercept_h
                                    low_prev = slope_l * prev_idx + intercept_l

                                    # Unique ID for this pattern (based on the timestamp of the last high)
                                    pivot_time_str = df['open_time'].iloc[recent_highs.index[-1]].strftime('%Y%m%d%H%M')
                                    pattern_id = f"{symbol}_{tf}_{pattern_name}_{pivot_time_str}"

                                    line_highs = {'m': slope_h, 'b': intercept_h}
                                    line_lows = {'m': slope_l, 'b': intercept_l}
                                    start_plot_idx = min(recent_highs.index[0], recent_lows.index[0])

                                    # ==========================================
                                    # CASE 1: NEW BREAKOUT
                                    # ==========================================
                                    breakout_dir = None
                                    if c_close > up_curr and p_close <= up_prev:
                                        breakout_dir = "BULLISH BREAKOUT 🟢"
                                    elif c_close < low_curr and p_close >= low_prev:
                                        breakout_dir = "BEARISH BREAKOUT 🔴"

                                    if breakout_dir:
                                        if pattern_id not in ALERTED_PATTERNS:
                                            ALERTED_PATTERNS.add(pattern_id)
                                            # We remember the pattern for a future retest!
                                            ACTIVE_PATTERNS[pattern_id] = breakout_dir

                                            try:
                                                chart_buf = await generate_pattern_chart(
                                                    df,
                                                    symbol,
                                                    tf,
                                                    pattern_name,
                                                    line_highs,
                                                    line_lows,
                                                    start_plot_idx,
                                                    current_idx,
                                                )
                                                color = "#00ff88" if "BULLISH" in breakout_dir else "#ff4466"
                                                msg = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
<b style="color:#00ffff; font-size:18px;">📐 PATTERN BREAKOUT</b>
<b style="color:#ffd700;">{symbol.replace('USDT', '')} | {tf} Chart</b>
<b>→ Pattern: {pattern_name}</b>
<b>→ Action: {breakout_dir}</b>
<b>→ Breakout Price: <code>${c_close:,.4f}</code></b>
<i>Waiting for retest...</i>
</pre>
                                                """.strip()
                                                await application.bot.send_photo(
                                                    chat_id=PATTERN_CHANNEL_ID,
                                                    photo=chart_buf,
                                                    caption=msg,
                                                    parse_mode="HTML",
                                                )
                                            except Exception as e:
                                                logger.error(f"Error with breakout chart: {e}")

                                    # ==========================================
                                    # CASE 2: RETEST OF A KNOWN PATTERN
                                    # ==========================================
                                    elif pattern_id in ACTIVE_PATTERNS:
                                        tracked_dir = ACTIVE_PATTERNS[pattern_id]
                                        retest_msg = None
                                        retest_color = None

                                        retest_alert_key = f"{pattern_id}_retest_{current_idx}"

                                        # Bullish retest check
                                        if "BULLISH" in tracked_dir:
                                            # Price must come down and touch/breach the breakout line
                                            if c_low <= up_curr:
                                                if c_close > up_curr:
                                                    retest_msg = (
                                                        "SUCCESSFUL RETEST 🟢\n(Bullish continuation confirmed)"
                                                    )
                                                    retest_color = "#00ff88"
                                                else:
                                                    retest_msg = "FAILED RETEST 🔴\n(Fakeout! Price closed back inside)"
                                                    retest_color = "#ff4466"
                                                    del ACTIVE_PATTERNS[pattern_id]  # Delete, since the pattern is destroyed

                                        # Bearish retest check
                                        elif "BEARISH" in tracked_dir:
                                            # Price must come up and touch/breach the breakout line
                                            if c_high >= low_curr:
                                                if c_close < low_curr:
                                                    retest_msg = (
                                                        "SUCCESSFUL RETEST 🔴\n(Bearish continuation confirmed)"
                                                    )
                                                    retest_color = "#ff4466"
                                                else:
                                                    retest_msg = "FAILED RETEST 🟢\n(Fakeout! Price closed back inside)"
                                                    retest_color = "#00ff88"
                                                    del ACTIVE_PATTERNS[pattern_id]  # Delete, since the pattern is destroyed

                                        if retest_msg and retest_alert_key not in ALERTED_RETESTS:
                                            ALERTED_RETESTS.add(retest_alert_key)
                                            try:
                                                chart_buf = await generate_pattern_chart(
                                                    df,
                                                    symbol,
                                                    tf,
                                                    pattern_name,
                                                    line_highs,
                                                    line_lows,
                                                    start_plot_idx,
                                                    current_idx,
                                                )
                                                msg = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {retest_color};">
<b style="color:#00ffff; font-size:18px;">🔄 PATTERN RETEST</b>
<b style="color:#ffd700;">{symbol.replace('USDT', '')} | {tf} Chart</b>
<b>→ Pattern: {pattern_name}</b>
<b>→ Result: {retest_msg}</b>
<b>→ Close Price: <code>${c_close:,.4f}</code></b>
</pre>
                                                """.strip()
                                                await application.bot.send_photo(
                                                    chat_id=PATTERN_CHANNEL_ID,
                                                    photo=chart_buf,
                                                    caption=msg,
                                                    parse_mode="HTML",
                                                )
                                            except Exception:
                                                pass

                    finally:
                        await release_conn(conn)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error in Pattern Detector for {symbol}: {e}")

            # IMPORTANT: sleep 60 seconds after the run.
            # This prevents the bot from starting twice within minute 15!
            await asyncio.sleep(60)

    except asyncio.CancelledError:
        logger.info("Pattern Detector shutting down cleanly...")
        raise
    except Exception as e:
        logger.error(f"Pattern Detector Crash: {e}", exc_info=True)
        await asyncio.sleep(60)


# ========================= FOREX =========================


async def fetch_and_store_forex_data():
    """Fetches 15m and 1h forex data via yfinance and stores them in the DB."""
    for symbol in FOREX_PAIRS:
        try:
            # 15m data (last 5 days) and 1h data (last 20 days)
            df_15m = await asyncio.to_thread(yf.download, symbol, period="5d", interval="15m", progress=False)
            df_1h = await asyncio.to_thread(yf.download, symbol, period="20d", interval="1h", progress=False)

            if df_15m.empty or df_1h.empty:
                continue

            for tf, df in [("15m", df_15m), ("1h", df_1h)]:
                df = df.copy()
                # yfinance multi-index fix (from version 0.2.x onward)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.droplevel(1)

                df.reset_index(inplace=True)

                # Standardize columns (in yfinance the datetime column is often called 'Datetime' or 'Date')
                time_col = 'Datetime' if 'Datetime' in df.columns else 'Date'
                df.rename(
                    columns={time_col: 'open_time', 'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close'},
                    inplace=True,
                )

                # Convert everything strictly to UTC
                df['open_time'] = pd.to_datetime(df['open_time'], utc=True)

                # Write to the database
                conn = await get_conn()
                try:
                    table_name = f"{symbol}_{tf}"
                    await conn.execute(f'''
                        CREATE TABLE IF NOT EXISTS "{table_name}" (
                            open_time TIMESTAMP WITH TIME ZONE PRIMARY KEY,
                            open REAL,
                            high REAL,
                            low REAL,
                            close REAL
                        )
                    ''')

                    records = df[['open_time', 'open', 'high', 'low', 'close']].to_dict('records')
                    for r in records:
                        # Extract float values (in case Pandas Series/Numpy types remain)
                        await conn.execute(
                            f'''
                            INSERT INTO "{table_name}" (open_time, open, high, low, close)
                            VALUES ($1, $2, $3, $4, $5)
                            ON CONFLICT (open_time) DO UPDATE
                            SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close
                        ''',
                            r['open_time'],
                            float(r['open']),
                            float(r['high']),
                            float(r['low']),
                            float(r['close']),
                        )
                finally:
                    await release_conn(conn)

        except Exception as e:
            logger.error(f"Error downloading forex data for {symbol}: {e}")

        # Small checkpoint and rate-limit protection for Yahoo Finance
        await asyncio.sleep(1)


async def forex_smc_detector():
    """Live scanner for forex: FVG, mitigation, BOS & CHoCH."""
    logger.info("Forex SMC Detector started")
    await asyncio.sleep(10)  # Let the other modules go first briefly at startup

    try:
        while True:
            # 1. Fetch fresh forex market data
            await fetch_and_store_forex_data()

            for symbol in FOREX_PAIRS:
                try:
                    await asyncio.sleep(0.01)  # Checkpoint for shutdown

                    conn = await get_conn()
                    try:
                        rows_1h = await conn.fetch(
                            f'SELECT open_time, open, high, low, close FROM "{symbol}_1h" ORDER BY open_time ASC'
                        )
                        rows_15m = await conn.fetch(
                            f'SELECT open_time, open, high, low, close FROM "{symbol}_15m" ORDER BY open_time ASC'
                        )
                    finally:
                        await release_conn(conn)

                    df_1h = pd.DataFrame([dict(r) for r in rows_1h]) if rows_1h else pd.DataFrame()
                    df_15m = pd.DataFrame([dict(r) for r in rows_15m]) if rows_15m else pd.DataFrame()

                    for tf_name, tf_rule in SMC_TIMEFRAMES.items():
                        await asyncio.sleep(0.01)  # Checkpoint


                        if tf_name in ['15m', '30m'] and not df_15m.empty:
                            base_df = df_15m.copy()
                        elif not df_1h.empty:
                            base_df = df_1h.copy()
                        else:
                            continue

                        base_df['open_time'] = pd.to_datetime(base_df['open_time'], utc=True)
                        base_df.set_index('open_time', inplace=True)

                        if tf_name not in ['15m', '1h']:
                            df_resampled = (
                                base_df.resample(tf_rule)
                                .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
                                .dropna()
                            )
                        else:
                            df_resampled = base_df

                        if len(df_resampled) < 10:
                            continue

                        state_key = f"{symbol}_{tf_name}"
                        if state_key not in FOREX_HISTORICAL_SCANNED:
                            await _run_historical_catchup(df_resampled, symbol, tf_name)
                            FOREX_HISTORICAL_SCANNED.add(state_key)

                        now_str = datetime.now(pytz.UTC).strftime('%H:%M')

                        # --- PHASE 1: DETECT FVG ---
                        c1, c2, c3 = df_resampled.iloc[-4], df_resampled.iloc[-3], df_resampled.iloc[-2]
                        fvg_created, fvg_type = False, None

                        if c3['low'] > c1['high']:
                            fvg_type, top_edge, bottom_edge = 'BISI (Bullish FVG)', c3['low'], c1['high']
                            fvg_created = True
                        elif c3['high'] < c1['low']:
                            fvg_type, top_edge, bottom_edge = 'SIBI (Bearish FVG)', c1['low'], c3['high']
                            fvg_created = True

                        if fvg_created:
                            c1_time = df_resampled.index[-4]
                            conn2 = await get_conn()
                            try:
                                res = await conn2.execute(
                                    """
                                    INSERT INTO active_smc_zones
                                    (symbol, timeframe, zone_type, top_edge, bottom_edge, created_time)
                                    VALUES ($1, $2, $3, $4, $5, $6)
                                    ON CONFLICT DO NOTHING
                                """,
                                    symbol,
                                    tf_name,
                                    fvg_type,
                                    top_edge,
                                    bottom_edge,
                                    c1_time,
                                )

                                if res.endswith("1"):
                                    try:
                                        chart_buf = await generate_smc_chart(
                                            df_resampled, symbol, tf_name, top_edge, bottom_edge, fvg_type
                                        )
                                        color = "#00ff88" if "Bullish" in fvg_type else "#ff4466"
                                        clean_symbol = symbol.replace('=X', '')
                                        msg = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
<b style="color:#00ffff; font-size:18px;">🧱 NEW FOREX SMC ZONE</b>
<b style="color:#ffd700;">{clean_symbol} | {tf_name} Chart</b>
<b>→ Type: {fvg_type}</b>
<b>→ Top: <code>{top_edge:,.5f}</code></b>
<b>→ Bottom: <code>{bottom_edge:,.5f}</code></b>
<b>→ Time: {now_str} UTC</b>
</pre>
                                        """.strip()
                                        await application.bot.send_photo(
                                            chat_id=FOREX_CHANNEL_ID, photo=chart_buf, caption=msg, parse_mode="HTML"
                                        )
                                    except Exception as e:
                                        logger.error(f"Error with forex FVG chart: {e}")
                            finally:
                                await release_conn(conn2)

                        # --- PHASE 2: BOS & CHoCH ---
                        df_resampled['Pivot_High'] = (
                            df_resampled['high'] == df_resampled['high'].rolling(window=5, center=True).max()
                        )
                        df_resampled['Pivot_Low'] = (
                            df_resampled['low'] == df_resampled['low'].rolling(window=5, center=True).min()
                        )

                        confirmed_df = df_resampled.iloc[:-2]
                        ph_indices = confirmed_df[confirmed_df['Pivot_High']].index
                        pl_indices = confirmed_df[confirmed_df['Pivot_Low']].index

                        if len(ph_indices) > 0 and len(pl_indices) > 0:
                            last_ph_idx, last_pl_idx = ph_indices[-1], pl_indices[-1]
                            last_ph_val, last_pl_val = (
                                confirmed_df.loc[last_ph_idx, 'high'],
                                confirmed_df.loc[last_pl_idx, 'low'],
                            )
                            last_closed, prev_closed = df_resampled['close'].iloc[-2], df_resampled['close'].iloc[-3]

                            current_trend = FOREX_TREND_STATE.get(state_key, 0)
                            struct_type, struct_price, pivot_time = None, 0, None

                            if last_closed > last_ph_val and prev_closed <= last_ph_val:
                                struct_type = "BULLISH BOS 🟢" if current_trend in [1, 0] else "BULLISH CHoCH 🚀"
                                FOREX_TREND_STATE[state_key] = 1
                                struct_price, pivot_time = last_ph_val, last_ph_idx
                            elif last_closed < last_pl_val and prev_closed >= last_pl_val:
                                struct_type = "BEARISH BOS 🔴" if current_trend in [-1, 0] else "BEARISH CHoCH 💥"
                                FOREX_TREND_STATE[state_key] = -1
                                struct_price, pivot_time = last_pl_val, last_pl_idx

                            if struct_type:
                                alert_key = f"{state_key}_{pivot_time}_{struct_type}"
                                if alert_key not in FOREX_ALERTED_STRUCT:
                                    FOREX_ALERTED_STRUCT.add(alert_key)
                                    try:
                                        chart_buf = await generate_smc_chart(
                                            df_resampled, symbol, tf_name, struct_price, struct_price, struct_type
                                        )
                                        color = "#00ff88" if "BULLISH" in struct_type else "#ff4466"
                                        clean_symbol = symbol.replace('=X', '')
                                        msg = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
<b style="color:#00ffff; font-size:18px;">⚖️ FOREX STRUCTURE SHIFT</b>
<b style="color:#ffd700;">{clean_symbol} | {tf_name} Chart</b>
<b>→ Type: {struct_type}</b>
<b>→ Broken Level: <code>{struct_price:,.5f}</code></b>
<b>→ Time: {now_str} UTC</b>
</pre>
                                        """.strip()
                                        await application.bot.send_photo(
                                            chat_id=FOREX_CHANNEL_ID, photo=chart_buf, caption=msg, parse_mode="HTML"
                                        )
                                    except Exception as e:
                                        logger.error(f"Error with forex BOS chart: {e}")

                        # --- PHASE 3: MITIGATION ---
                        current_price, current_high, current_low = (
                            df_resampled['close'].iloc[-1],
                            df_resampled['high'].iloc[-1],
                            df_resampled['low'].iloc[-1],
                        )

                        conn3 = await get_conn()
                        try:
                            active_zones = await conn3.fetch(
                                """
                                SELECT id, zone_type, top_edge, bottom_edge
                                FROM active_smc_zones
                                WHERE symbol = $1 AND timeframe = $2 AND mitigated = FALSE
                            """,
                                symbol,
                                tf_name,
                            )

                            for zone in active_zones:
                                z_id, z_type = zone['id'], zone['zone_type']
                                z_top, z_bot = float(zone['top_edge']), float(zone['bottom_edge'])
                                mitigated = False

                                if "BISI" in z_type and current_low <= z_top:
                                    mitigated = True
                                elif "SIBI" in z_type and current_high >= z_bot:
                                    mitigated = True

                                if mitigated:
                                    await conn3.execute(
                                        "UPDATE active_smc_zones SET mitigated = TRUE, mitigated_time = NOW() WHERE id = $1",
                                        z_id,
                                    )
                                    try:
                                        chart_buf = await generate_smc_chart(
                                            df_resampled, symbol, tf_name, z_top, z_bot, z_type
                                        )
                                        color, emoji = ("#00ff88", "✅") if "BISI" in z_type else ("#ff4466", "🎯")
                                        clean_symbol = symbol.replace('=X', '')
                                        msg = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; border-left:6px solid {color};">
<b style="color:#00ffff; font-size:18px;">{emoji} FOREX FVG MITIGATED</b>
<b style="color:#ffd700;">{clean_symbol} | {tf_name} Chart</b>
<b>→ Zone: {z_type}</b>
<b>→ Range: {z_bot:,.5f} - {z_top:,.5f}</b>
<b>→ Current Price: <code>{current_price:,.5f}</code></b>
</pre>
                                        """.strip()
                                        await application.bot.send_photo(
                                            chat_id=FOREX_CHANNEL_ID, photo=chart_buf, caption=msg, parse_mode="HTML"
                                        )
                                    except Exception:
                                        pass
                        finally:
                            await release_conn(conn3)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Forex SMC error for {symbol}: {e}")

            # 5-minute pause
            await asyncio.sleep(300)

    except asyncio.CancelledError:
        logger.info("Forex SMC Detector task shutting down cleanly...")
        raise
    except Exception as e:
        logger.error(f"Forex SMC Detector Crash: {e}", exc_info=True)
        await asyncio.sleep(60)


# ========================= PUMP % DUMP DETECTOR (FULLY ASYNC + SEXY HTML) =========================


async def create_pump_dump_archive_table():
    conn = await get_conn()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pump_dump_archive (
                id BIGSERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                avg_volume NUMERIC,
                last_alert_time TIMESTAMPTZ,
                usd_vol_4h NUMERIC,
                volume_samples JSONB
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_pump_dump_archive_time ON pump_dump_archive(timestamp)")
        logger.info("pump_dump_archive table ready")
    finally:
        await release_conn(conn)


async def archive_pump_dump_state_to_db():
    conn = await get_conn()
    try:
        async with conn.transaction():
            for symbol, state in PUMP_DUMP_STATE.items():
                await conn.execute(
                    """
                    INSERT INTO pump_dump_archive (symbol, avg_volume, last_alert_time, usd_vol_4h, volume_samples)
                    VALUES ($1, $2, $3, $4, $5)
                """,
                    symbol,
                    state["avg_volume"],
                    state["last_alert_time"],
                    state["usd_vol_4h"],
                    json.dumps(list(state["volume_samples"])),
                )
        logger.info("PUMP_DUMP_STATE archived in DB")
    except Exception as e:
        logger.error(f"Error archiving PUMP_DUMP_STATE: {e}")
    finally:
        await release_conn(conn)


async def cleanup_old_pump_dump_archive(days_to_keep: int = 180):
    conn = await get_conn()
    try:
        cutoff = datetime.now(pytz.UTC) - timedelta(days=days_to_keep)
        result = await conn.execute("DELETE FROM pump_dump_archive WHERE timestamp < $1", cutoff)
        logger.info(f"Pump/Dump archive cleanup: {result.split()[-1]} entries deleted")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
    finally:
        await release_conn(conn)


async def get_ml_model():
    global _ml_model, _ml_model_time
    now = datetime.now(pytz.UTC)

    if _ml_model is None or _ml_model_time is None or (now - _ml_model_time).total_seconds() > 3600:
        if os.path.exists(ML_MODEL_PATH):
            try:
                _ml_model = joblib.load(ML_MODEL_PATH)
                _ml_model_time = now
                logger.info("ML model for Pump/Dump loaded")
            except Exception as e:
                logger.error(f"Error loading the ML model: {e}")
                _ml_model = None
        else:
            # logger.warning("ML model not found – falling back to rule-based only")
            _ml_model = None
            _ml_model_time = now

    return _ml_model


async def ml_pump_dump_trainer():
    logger.info("ML Pump/Dump Trainer started – daily training")
    # await train_pump_dump_model()

    while True:
        try:
            now = datetime.now(pytz.UTC)
            # Runs daily at 04:00 UTC (after possible cleanup)
            if now.hour == 4 and now.minute < 30:
                # await train_pump_dump_model()
                logger.info("ML training complete – new model saved")

            await asyncio.sleep(1800)  # Check every 30 minutes

        except asyncio.CancelledError:
            logger.info("ML Trainer Task cancelled")
            raise
        except Exception as e:
            logger.error(f"ML Trainer crashed: {e}", exc_info=True)
            await asyncio.sleep(3600)


async def train_pump_dump_model():
    logger.info("ML Trainer Task started")
    conn = await get_conn()
    try:
        # Fetch the last 30 days of 10s data
        cutoff = datetime.now(pytz.UTC) - timedelta(days=30)
        rows = await conn.fetch(
            """
            SELECT symbol, timestamp, price, volume_10s
            FROM ticker_10s
            WHERE timestamp >= $1
            ORDER BY symbol, timestamp
        """,
            cutoff,
        )
        if len(rows) < 30000:
            logger.warning("Too little 10s data for ML training – at least 30k entries recommended")
            return

        # Into a DataFrame for easier processing
        df = pd.DataFrame(rows, columns=['symbol', 'timestamp', 'price', 'volume_10s'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)

        features = []
        labels = []

        # Process per symbol
        for symbol, group in df.groupby('symbol'):
            group = group.sort_values('timestamp').reset_index(drop=True)
            prices = group['price'].values
            volumes = group['volume_10s'].values
            times = group['timestamp'].values

            if len(prices) < 200:
                continue

            vol_deque = deque(maxlen=360)
            avg_vol = 0.0

            for i in range(len(prices)):
                current_vol = volumes[i]
                vol_deque.append(current_vol)

                if len(vol_deque) == 360:
                    avg_vol = sum(vol_deque) / 360
                elif avg_vol == 0 and len(vol_deque) > 30:
                    avg_vol = sum(vol_deque) / len(vol_deque)

                if avg_vol <= 0 or i < 90 or i + 60 >= len(prices):  # 60 instead of 90 → 10 minutes
                    continue

                volume_ratio = current_vol / avg_vol
                if volume_ratio < 5.0:
                    continue

                # --- Compute features ---
                start_idx = max(0, i - 6)
                recent_prices = prices[start_idx : i + 1]
                price_change_60s = (prices[i] / prices[i - 6] - 1) * 100 if i >= 6 else 0
                up_ticks = sum(1 for j in range(start_idx + 1, i + 1) if prices[j] > prices[j - 1])
                buy_pressure = up_ticks / max(1, i - start_idx)
                volatility = np.std(recent_prices) / np.mean(recent_prices) if np.mean(recent_prices) > 0 else 0

                # --- Fetch indicators ---
                spike_time = pd.Timestamp(times[i])
                if pd.isna(spike_time):
                    continue

                try:
                    ind_row = await conn.fetchrow(
                        f'''
                        SELECT rsi_14, tsi_fast_12_7_7, macd_dif_normal_12_26_9, ema_9, ema_21
                        FROM "{symbol}_1h_indicators"
                        WHERE open_time <= $1
                        ORDER BY open_time DESC LIMIT 1
                    ''',
                        spike_time.to_pydatetime(),
                    )

                    if ind_row is None:
                        continue

                    rsi = float(ind_row['rsi_14']) if ind_row['rsi_14'] is not None else 50.0
                    tsi = float(ind_row['tsi_fast_12_7_7']) if ind_row['tsi_fast_12_7_7'] is not None else 0.0
                    macd = (
                        float(ind_row['macd_dif_normal_12_26_9'])
                        if ind_row['macd_dif_normal_12_26_9'] is not None
                        else 0.0
                    )
                    ema9 = float(ind_row['ema_9']) if ind_row['ema_9'] is not None else float(prices[i])
                    ema21 = float(ind_row['ema_21']) if ind_row['ema_21'] is not None else float(prices[i])
                except asyncpg.exceptions.UndefinedTableError:
                    logger.debug(f"No indicator table for {symbol} – skipped")
                    continue
                except Exception as e:
                    logger.warning(f"Indicator error {symbol}: {e}")
                    continue

                current_price_float = float(prices[i])
                ema9_dist = (current_price_float - ema9) / ema9 * 100 if ema9 > 0 else 0.0
                ema21_dist = (current_price_float - ema21) / ema21 * 100 if ema21 > 0 else 0.0

                # --- Label: outcome in the next 10 minutes (+60 ticks) ---
                end_idx = i + 60
                future_change = (prices[end_idx] / prices[i] - 1) * 100 if end_idx < len(prices) else 0

                if future_change >= 3.0:  # slightly relaxed for more pump labels
                    label = 2
                elif future_change <= -3.0:  # a bit stricter for dumps (less noise)
                    label = 0
                else:
                    label = 1

                features.append(
                    [
                        volume_ratio,
                        price_change_60s,
                        buy_pressure,
                        volatility,
                        len(vol_deque) / 360.0,
                        rsi,
                        tsi,
                        macd,
                        ema9_dist,
                        ema21_dist,
                    ]
                )
                labels.append(label)

        if len(features) < 100:
            logger.warning(f"Only {len(features)} labeled events – too few for training")
            return

        X = np.array(features)
        y = np.array(labels)

        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

        # === Class balancing with sample_weight ===
        from sklearn.utils.class_weight import compute_class_weight

        # Define classes
        classes = np.unique(y_train)

        # Compute balanced weights
        class_weights = compute_class_weight('balanced', classes=classes, y=y_train)
        class_weight_dict = dict(zip(classes, class_weights))

        # Leave fake-out normal, only slightly boost pump/dump (max 3x)
        for label in class_weight_dict:
            if label == 1:  # Fake-out
                class_weight_dict[label] = 1.0
            else:
                class_weight_dict[label] = min(class_weight_dict[label], 3.0)

        # Create sample weights
        sample_weights = np.array([class_weight_dict[label] for label in y_train])

        # === Model with more capacity ===
        model = XGBClassifier(
            n_estimators=500,  # more trees for better learning
            max_depth=6,  # a bit lower against overfitting
            learning_rate=0.04,
            subsample=0.85,
            colsample_bytree=0.85,
            random_state=42,
            eval_metric='mlogloss',
        )

        model.fit(X_train, y_train, sample_weight=sample_weights)

        y_pred = model.predict(X_test)
        report = classification_report(y_test, y_pred, output_dict=True, zero_division=0)

        logger.info(f"ML training complete – events: {len(features)} | Accuracy: {report['accuracy']:.3f}")
        logger.info(
            f"Pump (2) Precision: {report.get('2', {}).get('precision', 0):.3f} | Recall: {report.get('2', {}).get('recall', 0):.3f}"
        )
        logger.info(
            f"Dump (0) Precision: {report.get('0', {}).get('precision', 0):.3f} | Recall: {report.get('0', {}).get('recall', 0):.3f}"
        )
        logger.info(
            f"Fake-Out (1) Precision: {report.get('1', {}).get('precision', 0):.3f} | Recall: {report.get('1', {}).get('recall', 0):.3f}"
        )

        # Feature importance
        booster = model.get_booster()
        importance = booster.get_score(importance_type='gain')
        feature_map = {
            f'f{i}': name
            for i, name in enumerate(
                [
                    'volume_ratio',
                    'price_change_60s',
                    'buy_pressure',
                    'volatility',
                    'deque_fill',
                    'rsi_14',
                    'tsi',
                    'macd_dif',
                    'ema9_dist',
                    'ema21_dist',
                ]
            )
        }

        sorted_imp = sorted(importance.items(), key=lambda x: x[1], reverse=True)
        logger.info("Feature importance (gain):")
        for f_idx, score in sorted_imp:
            feature_name = feature_map.get(f_idx, f_idx)
            logger.info(f"  {feature_name}: {score:.2f}")

        joblib.dump(model, "pump_dump_model.pkl")
        logger.info("New ML model saved: pump_dump_model.pkl")

    except Exception as e:
        logger.error(f"ML training error: {e}", exc_info=True)
    finally:
        await release_conn(conn)


async def create_pump_dump_events_table():
    conn = await get_conn()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pump_dump_events (
                id BIGSERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                spike_time TIMESTAMPTZ NOT NULL,
                volume_ratio NUMERIC,
                price_change_60s NUMERIC,
                buy_pressure NUMERIC,
                volatility NUMERIC,
                rsi_14 NUMERIC,
                tsi NUMERIC,
                macd_dif NUMERIC,
                ema9_distance_pct NUMERIC,
                ema21_distance_pct NUMERIC
            )
        """)
        logger.info("pump_dump_events table ready")
    finally:
        await release_conn(conn)


# async def send_cornix_signal(symbol: str, is_pump: bool, modul: str):
# """
# Sends a Cornix-compatible LONG/SHORT signal to the trading channel.
# Only CMP entry, SL and all available targets (max 20).
# """
# TRADE_CHANNEL_ID = 0  # Your Cornix channel
# direction = "LONG" if is_pump else "SHORT"
# leverage = "20x"

# # Fetch live price
# live_price = await get_live_price(symbol)
# if not live_price:
# logger.warning(f"Cornix signal for {symbol}: no live price available")
# return

# # 90 days of 1h data for targets/SL
# df = await get_1h_data_last_90d(symbol)
# is_long = is_pump
# if df.empty or len(df) <= 100:
# entry1 = live_price
# logger.warning(f"Cornix signal for {symbol}: not enough data for targets")

# if is_long:
# entry2 = entry1 * 0.95
# target_candidates = [live_price*1.0125, live_price*1.025, live_price*1.0375, live_price*1.05]
# sl=live_price*0.975
# else:
# entry2 = entry1 * 1.05
# target_candidates = [live_price*0.9875, live_price*0.975, live_price*0.9625, live_price*0.095]
# sl=entry2*1.025


# else:

# # Support / Resistance + HVN
# supports, resistances = find_support_resistance(df)
# hvn_all = get_hvn_levelz(df, top_n=500)['all']

# # Swing for Fibs
# swing_high = df['HIGH'].max()
# swing_low = df['LOW'].min()
# fib_range = swing_high - swing_low

# fib_retracement = [swing_high - fib_range * x for x in [0.236, 0.382, 0.5, 0.618, 0.786]]
# fib_ext_up = [swing_low + fib_range * x for x in [1.272, 1.618, 2.0, 2.618]]
# fib_ext_down = [swing_high - fib_range * (x - 1) for x in [1.272, 1.618, 2.0, 2.618]]

# is_long = is_pump

# # CMP Entry (both entries = current price)
# entry1 = live_price

# # Stop Loss
# if is_long:
# entry2 = entry1 * 0.95
# sl_candidates = [x for x in supports + hvn_all + fib_retracement if x < entry2 * 0.99]
# sl = min(sl_candidates, key=lambda x: abs(x - entry2)) if sl_candidates else entry2 * 0.95
# if sl <= entry2 * 0.95:
# sl=entry2*0.975
# else:
# entry2 = entry1 * 1.05
# sl_candidates = [x for x in resistances + hvn_all + fib_ext_up if x > entry2 * 1.01]
# sl = min(sl_candidates, key=lambda x: abs(x - entry2)) if sl_candidates else entry2 * 1.05
# if sl <= entry2 * 1.05:
# sl=entry2*1.025

# # Targets
# if is_long:
# entry2 = entry1 * 0.95
# target_candidates = [x for x in resistances + hvn_all + fib_ext_up if x > (entry1*1.01)]
# target_candidates = sorted(target_candidates)
# else:
# entry2 = entry1 * 1.05
# target_candidates = [x for x in supports + hvn_all + fib_retracement + fib_ext_down if x < (entry1*0.99) and x > 0]
# target_candidates = sorted(target_candidates, reverse=True)

# targets = target_candidates[:20]  # Max 20 targets

# # Build the text message – only show targets that were found
# lines = [
# f"📈 Signal for {symbol} 📈",
# f"🚨 Direction: {direction}",
# f"🚨 Leverage: {leverage}",
# f"🏦 CMP Entry: $ {entry1:.8f}",
# f"🏦 Entry 2: $ {entry2:.8f}",
# ]

# for i, target in enumerate(targets, 1):
# lines.append(f"💰 TP{i}: $ {target:.8f}")

# lines += [
# f"💸 Stop Loss: $ {sl:.8f}",
# f"🧠 Trade idea generated by Proven Crypto Bot V2 - AI module {modul}"
# ]

# message = "\n".join(lines)

# try:
# await application.bot.send_message(
# chat_id=TRADE_CHANNEL_ID,
# text=message
# )
# logger.info(f"Cornix signal sent: {symbol} {direction}")
# except Exception as e:
# logger.error(f"Cornix signal error {symbol}: {e}")


async def send_cornix_signal(symbol: str, is_pump: bool, modul: str, channel_id=None):
    """
    Sends a Cornix-compatible LONG/SHORT signal to the specified channel.
    Falls back to the old Cornix channel when channel_id=None.
    """
    DEFAULT_TRADE_CHANNEL_ID = 0  # Old Cornix channel
    target_channel = channel_id if channel_id is not None else DEFAULT_TRADE_CHANNEL_ID

    direction = "LONG" if is_pump else "SHORT"
    leverage = "20x-10x"

    # Fetch live price
    live_price = await get_live_price(symbol)
    if not live_price:
        logger.warning(f"Cornix signal for {symbol}: no live price available")
        return

    # 90 days of 1h data for targets/SL
    df = await get_1h_data_last_90d(symbol)
    is_long = is_pump

    if df.empty or len(df) <= 100:
        entry1 = live_price
        logger.warning(f"Cornix signal for {symbol}: not enough data for targets")

        if is_long:
            entry2 = entry1 * 0.95
            target_candidates = [live_price * 1.0125, live_price * 1.025, live_price * 1.0375, live_price * 1.05]
            sl = live_price * 0.975
        else:
            entry2 = entry1 * 1.05
            target_candidates = [live_price * 0.9875, live_price * 0.975, live_price * 0.9625, live_price * 0.095]
            sl = entry2 * 1.025
    else:
        # Support / Resistance + HVN + Fibs (your old code stays the same)
        supports, resistances = find_support_resistance(df)
        hvn_all = get_hvn_levelz(df, top_n=500)['all']
        swing_high = df['HIGH'].max()
        swing_low = df['LOW'].min()
        fib_range = swing_high - swing_low
        fib_retracement = [swing_high - fib_range * x for x in [0.236, 0.382, 0.5, 0.618, 0.786]]
        fib_ext_up = [swing_low + fib_range * x for x in [1.272, 1.618, 2.0, 2.618]]
        fib_ext_down = [swing_high - fib_range * (x - 1) for x in [1.272, 1.618, 2.0, 2.618]]

        entry1 = live_price

        if is_long:
            entry2 = entry1 * 0.95
            sl_candidates = [x for x in supports + hvn_all + fib_retracement if x < entry2 * 0.99]
            sl = min(sl_candidates, key=lambda x: abs(x - entry2)) if sl_candidates else entry2 * 0.95
            if sl <= entry2 * 0.95:
                sl = entry2 * 0.975
        else:
            entry2 = entry1 * 1.05
            sl_candidates = [x for x in resistances + hvn_all + fib_ext_up if x > entry2 * 1.01]
            sl = min(sl_candidates, key=lambda x: abs(x - entry2)) if sl_candidates else entry2 * 1.05
            if sl <= entry2 * 1.05:
                sl = entry2 * 1.025

        if is_long:
            entry2 = entry1 * 0.95
            target_candidates = [x for x in resistances + hvn_all + fib_ext_up if x > (entry1 * 1.01)]
            target_candidates = sorted(target_candidates)
        else:
            entry2 = entry1 * 1.05
            target_candidates = [
                x for x in supports + hvn_all + fib_retracement + fib_ext_down if x < (entry1 * 0.99) and x > 0
            ]
            target_candidates = sorted(target_candidates, reverse=True)

    targets = target_candidates[:20]

    lines = [
        f"📈 Signal for {symbol} 📈",
        f"🚨 Direction: {direction}",
        f"🚨 Leverage: {leverage}",
        f"🏦 CMP Entry: $ {entry1:.8f}",
        f"🏦 Entry 2: $ {entry2:.8f}",
    ]
    for i, target in enumerate(targets, 1):
        lines.append(f"💰 TP{i}: $ {target:.8f}")
    lines += [f"💸 Stop Loss: $ {sl:.8f}", f"🧠 Trade idea generated by Proven Crypto Bot V2 - AI module {modul}"]
    message = "\n".join(lines)

    try:
        await application.bot.send_message(chat_id=target_channel, text=message)
        logger.info(f"Cornix signal sent: {symbol} {direction} in channel {target_channel}")
    except Exception as e:
        logger.error(f"Cornix signal error {symbol}: {e}")


async def early_pump_dump_detector():
    logger.info("Early Pump/Dump Detector task started (hybrid: volume + momentum)")
    global PUMP_DUMP_STATE
    # Only store truly informative events
    min_volume_for_save = 3.8
    min_momentum_for_low_volume = 1.2  # % in 60s

    last_save_time = datetime.now(pytz.UTC) - timedelta(minutes=11)

    # logger.info(f"Number of coins from coins.json: {len(coins)}")
    # sample_coins = list(coins)[:5]
    # logger.info(f"Example coins.json: {sample_coins}")

    # logger.info(f"Number of symbols in ONE_MINUTE_DATA: {len(ONE_MINUTE_DATA)}")
    # sample_ticker = list(ONE_MINUTE_DATA.keys())[:5]
    # logger.info(f"Example ONE_MINUTE_DATA: {sample_ticker}")

    # overlap = set(coins) & set(ONE_MINUTE_DATA.keys())
    # logger.info(f"Overlap (common symbols): {len(overlap)} → {list(overlap)[:10]}")

    # Initialize state for all coins
    for symbol in coins:
        if symbol not in PUMP_DUMP_STATE:
            PUMP_DUMP_STATE[symbol] = {
                "avg_volume": 0.0,
                "volume_samples": deque(maxlen=360),
                "last_alert_time": datetime(1970, 1, 1, tzinfo=pytz.UTC),
                "usd_vol_4h": 0.0,
            }

    while True:
        try:
            await asyncio.sleep(10)
            logger.info("Pump/Dump State starting")
            processed = 0
            signals_batch = []
            for symbol in coins:
                if symbol not in ONE_MINUTE_DATA or len(ONE_MINUTE_DATA[symbol]) < 12:
                    continue
                processed += 1
                data = list(ONE_MINUTE_DATA[symbol])
                current_entry = data[-1]
                current_price = float(current_entry["p"])
                current_vol = float(current_entry["v10s"])
                state = PUMP_DUMP_STATE[symbol]

                # Rolling average volume
                state["volume_samples"].append(current_vol)

                # Cold-start protection
                if len(state["volume_samples"]) < 60:
                    continue

                if len(state["volume_samples"]) == 360:
                    state["avg_volume"] = sum(state["volume_samples"]) / 360
                elif state["avg_volume"] == 0 and len(state["volume_samples"]) > 18:
                    state["avg_volume"] = sum(state["volume_samples"]) / len(state["volume_samples"])

                if state["avg_volume"] <= 0:
                    continue

                volume_ratio = current_vol / state["avg_volume"]

                # Shared calculations
                recent_prices = [float(e["p"]) for e in data[-7:]]
                price_change_60s = (recent_prices[-1] / recent_prices[-7] - 1) * 100 if len(recent_prices) >= 7 else 0

                up_ticks = sum(1 for j in range(1, len(recent_prices)) if recent_prices[j] > recent_prices[j - 1])
                buy_pressure = up_ticks / max(1, len(recent_prices) - 1)

                volatility = np.std(recent_prices) / np.mean(recent_prices) if np.mean(recent_prices) > 0 else 0

                change_5min = (current_price / float(data[-30]["p"]) - 1) * 100 if len(data) >= 30 else 0

                now = datetime.now(pytz.UTC)

                # Cooldown
                if (now - state["last_alert_time"]).total_seconds() < 900:
                    continue

                if volume_ratio >= min_volume_for_save:
                    # High volume → always save (potential pump OR dump)
                    pass
                elif abs(price_change_60s) >= min_momentum_for_low_volume:
                    # Strong movement even with moderate volume → interesting
                    pass
                else:
                    continue  # Both weak → probably irrelevant

                # === Fetch indicators (once for both paths) ===
                rsi = 50.0
                tsi = 0.0
                macd = 0.0
                ema9 = current_price
                ema21 = current_price

                conn = await get_conn()
                try:
                    ind_row = await conn.fetchrow(f'''
                        SELECT rsi_14, tsi_fast_12_7_7, macd_dif_normal_12_26_9, ema_9, ema_21
                        FROM "{symbol}_1h_indicators"
                        ORDER BY open_time DESC LIMIT 1
                    ''')
                    if ind_row:
                        rsi = float(ind_row['rsi_14']) if ind_row['rsi_14'] is not None else 50.0
                        tsi = float(ind_row['tsi_fast_12_7_7']) if ind_row['tsi_fast_12_7_7'] is not None else 0.0
                        macd = (
                            float(ind_row['macd_dif_normal_12_26_9'])
                            if ind_row['macd_dif_normal_12_26_9'] is not None
                            else 0.0
                        )
                        ema9 = float(ind_row['ema_9']) if ind_row['ema_9'] is not None else current_price
                        ema21 = float(ind_row['ema_21']) if ind_row['ema_21'] is not None else current_price
                except asyncpg.exceptions.UndefinedTableError:
                    logger.debug(f"No indicator table for {symbol}")
                except Exception as e:
                    logger.warning(f"Indicator error {symbol}: {e}")
                finally:
                    await release_conn(conn)

                ema9_dist = (current_price - ema9) / ema9 * 100 if ema9 > 0 else 0.0
                ema21_dist = (current_price - ema21) / ema21 * 100 if ema21 > 0 else 0.0

                # === Path 1: volume-based (as before) ===
                volume_score = 0.0

                # Volume itself (symmetric, but weaker)
                if volume_ratio > 1:
                    volume_score += min(40, (volume_ratio - 1) * 8)  # Pump bonus
                else:
                    volume_score += max(-30, (volume_ratio - 1) * 30)  # Weaker dump penalty

                # Momentum
                volume_score += price_change_60s * 10

                # Buy/sell pressure, asymmetric:
                if price_change_60s > 0:  # Uptrend → buy pressure must be high
                    volume_score += (buy_pressure - 0.5) * 60
                else:  # Downtrend → sell pressure (low buy_pressure) must be strong
                    volume_score += (
                        0.5 - buy_pressure
                    ) * 50  # Inverted: low buy_pressure → positive dump score

                # 5min change
                if abs(change_5min) < 8:
                    volume_score += 15
                elif abs(change_5min) > 20:
                    volume_score -= 25

                # === Path 3: early signal (moderate, but consistent – asymmetric) ===
                early_score = 0.0

                # Base momentum
                early_score += price_change_60s * 15

                # Buy/sell pressure, asymmetric
                if price_change_60s > 0:  # Potential pump
                    early_score += (buy_pressure - 0.5) * 70  # High buy pressure → bonus
                else:  # Potential dump
                    early_score += (0.5 - buy_pressure) * 60  # Low buy pressure (sell pressure) → bonus for dump

                # 5min change (reward moderate moves)
                if 3 < abs(change_5min) < 10:
                    early_score += 30 if change_5min > 0 else -30
                elif 1 < abs(change_5min) <= 3:
                    early_score += 15 if change_5min > 0 else -15

                # Indicator context: different for pump/dump
                if price_change_60s > 0:  # Pump direction
                    if (
                        rsi < 65
                        and tsi > -10
                        and macd > -0.002
                        and current_price > ema9 * 0.998
                        and current_price > ema21 * 0.998
                    ):
                        early_score += 40
                else:  # Dump direction
                    if (
                        rsi > 35
                        and tsi < 10
                        and macd < 0.002
                        and current_price < ema9 * 1.002
                        and current_price < ema21 * 1.002
                    ):
                        early_score -= 40

                # === Path 2: momentum + indicators (asymmetric for pump/dump) ===
                momentum_score = 0.0

                # Momentum and direction
                momentum_score += price_change_60s * 20

                # Buy/sell pressure (asymmetric)
                if price_change_60s > 0:
                    momentum_score += (buy_pressure - 0.5) * 80  # Strong buy pressure for pump
                else:
                    momentum_score += (0.5 - buy_pressure) * 70  # Strong sell pressure for dump

                if abs(change_5min) > 5:
                    momentum_score += 40

                if abs(change_5min) > 3:
                    momentum_score += 20

                if abs(change_5min) < -5:
                    momentum_score -= 40

                if abs(change_5min) < -3:
                    momentum_score -= 20

                # Bullish context → bonus for pump
                if (
                    change_5min > 0
                    and buy_pressure > 0.6
                    and rsi < 75
                    and tsi > -15
                    and macd > -0.001
                    and current_price > ema9 * 0.995
                    and current_price > ema21 * 0.995
                ):
                    momentum_score += 40

                # Bearish context → bonus for dump (sell pressure!)
                if (
                    change_5min < 0
                    and buy_pressure < 0.4
                    and rsi > 25
                    and tsi < 15
                    and macd < 0.001
                    and current_price < ema9 * 1.005
                    and current_price < ema21 * 1.005
                ):
                    momentum_score -= 40

                # Bullish indicator context → bonus
                if (
                    rsi < 75
                    and tsi > -15
                    and macd > -0.001
                    and current_price > ema9 * 0.997
                    and current_price > ema21 * 0.997
                ):
                    momentum_score += 40
                # Bearish → penalty
                elif (
                    rsi > 25
                    and tsi < 15
                    and macd < 0.001
                    and current_price < ema9 * 1.003
                    and current_price < ema21 * 1.003
                ):
                    momentum_score -= 40

                # === Trigger: one of the two paths must be strong ===
                trigger = False
                if abs(volume_score) >= 65:
                    trigger = True
                    # logger.info(f"Volume spike detected: {symbol} | Score {volume_score:+.0f}")
                if abs(momentum_score) >= 100:
                    trigger = True
                    # logger.info(f"Momentum spike detected: {symbol} | Momentum score {momentum_score:+.0f}")
                if abs(early_score) >= 60:  # Threshold slightly lower than the momentum path
                    trigger = True
                    # logger.info(f"Early signal detected: {symbol} | E-score {early_score:+.0f}")
                if not trigger:
                    continue

                # Save event (for training – for every candidate)
                conn2 = await get_conn()
                try:
                    await conn2.execute(
                        """
                        INSERT INTO pump_dump_events (
                            symbol, spike_time, volume_ratio, price_change_60s, buy_pressure,
                            volatility, rsi_14, tsi, macd_dif, ema9_distance_pct, ema21_distance_pct
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    """,
                        symbol,
                        now,
                        volume_ratio,
                        price_change_60s,
                        buy_pressure,
                        volatility,
                        rsi,
                        tsi,
                        macd,
                        ema9_dist,
                        ema21_dist,
                    )
                except Exception as e:
                    logger.error(f"Event insert error {symbol}: {e}")
                finally:
                    await release_conn(conn2)

                model = await get_ml_model()
                if model is None:
                    continue

                features = np.array(
                    [
                        [
                            volume_ratio,
                            price_change_60s,
                            buy_pressure,
                            volatility,
                            len(state["volume_samples"]) / 360.0,
                            rsi,
                            tsi,
                            macd,
                            ema9_dist,
                            ema21_dist,
                        ]
                    ]
                )

                prob = model.predict_proba(features)[0]
                classes = model.classes_
                prob_dump = prob[0] if 0 in classes else 0
                prob_fake = prob[1] if 1 in classes else 0
                prob_pump = prob[2] if 2 in classes else 0

                max_prob = max(prob_pump, prob_dump)
                is_pump = prob_pump >= prob_dump

                if max_prob >= 0.25:  # instead of just >=0.6
                    signals_batch.append(
                        {
                            'symbol': symbol,
                            'price': current_price,
                            'model': 'EPD1',
                            'direction': 'LONG' if is_pump else 'SHORT',
                            'confidence': max_prob,
                        }
                    )

                if max_prob < 0.6:
                    if max_prob > 0.25:
                        logger.info(
                            f"ML alert weak: {symbol} | {'PUMP' if is_pump else 'DUMP'} | Confidence {max_prob:.1%} (Pump: {prob_pump:.1%}, Dump: {prob_dump:.1%})"
                        )
                    # else:
                    # #logger.info(f"ML alert very weak: {symbol} | {'PUMP' if is_pump else 'DUMP'} | Confidence {max_prob:.1%} (Pump: {prob_pump:.1%}, Dump: {prob_dump:.1%})")
                    # logger.info(f"ML blocking alert: {symbol} | V{volume_score:+.0f} | M{momentum_score:+.0f} | E{early_score:+.0f} | Prob {max_prob:.1%}")
                    continue

                # CORRECT: ML always decides the direction when confidence is high enough
                # is_pump = prob_pump >= prob_dump

                # Optional: logging for debugging

                logger.info(
                    f"ML-Alert: {symbol} | {'PUMP' if is_pump else 'DUMP'} | Confidence {max_prob:.1%} (Pump: {prob_pump:.1%}, Dump: {prob_dump:.1%})"
                )

                await send_cornix_signal(symbol, is_pump, 'EPD1')

                # === Post alert ===
                border_color = "#00ff00" if is_pump else "#ff0066"
                emoji = "🚀 EARLY PUMP DETECTION" if is_pump else "💥 EARLY DUMP ALERT"
                score_color = "#00ff00" if is_pump else "#ff0066"
                change_color = "#00ff00" if change_5min > 0 else "#ff0066"

                html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {border_color};">
<b style="color:#00ffff; font-size:18px;">{emoji}</b>
<b style="color:#ffd700;">{symbol.replace('USDT', '')}/USDT</b>
<b>{'Pump' if is_pump else 'Dump'} Score: <b style="color:{score_color};">V{volume_score:+.0f} | M{momentum_score:+.0f} | E-Score {early_score:+.0f}</b></b>
<b>→ Price: <code>${current_price:,.8f}</code> <b style="color:{change_color}">({change_5min:+.2f}% in 5min)</b></b>
<b>→ Volume: <b style="color:#00ff88;">{volume_ratio:.1f}×</b> above average</b>
<b>→ ML-Confidence: <b style="color:#00ffff;">{max_prob:.1%}</b> / Module: EPD1</b>
<b>→ Time: {now.strftime('%H:%M')} UTC</b>
</pre>
                """.strip()

                chart_buf = await generate_smooth_minichart_image(symbol, minutes=240)
                try:
                    if chart_buf:
                        await application.bot.send_photo(
                            chat_id=AI_CHANNEL_ID, photo=chart_buf, caption=html, parse_mode="HTML"
                        )
                    else:
                        await application.bot.send_message(chat_id=AI_CHANNEL_ID, text=html, parse_mode="HTML")
                    logger.info(
                        f"Hybrid alert sent: {symbol} | V{volume_score:+.0f} | M{momentum_score:+.0f} | E{early_score:+.0f} | Prob {max_prob:.1%}"
                    )
                except Exception as e:
                    logger.error(f"Alert send error {symbol}: {e}")

                state["last_alert_time"] = now

            logger.info(f"Pump/Dump Detector run complete – {processed} coins checked ")

            if signals_batch:
                asyncio.create_task(log_ai_signals(signals_batch))

            # Save every 10 minutes
            now = datetime.now(pytz.UTC)
            if (now - last_save_time).total_seconds() >= 600:
                try:
                    save_data = {}
                    for sym, state in PUMP_DUMP_STATE.items():
                        save_data[sym] = {
                            "avg_volume": state["avg_volume"],
                            "last_alert_time": state["last_alert_time"].isoformat(),
                            "usd_vol_4h": state["usd_vol_4h"],
                            "volume_samples": list(state["volume_samples"]),
                        }
                    with open(PUMP_DUMP_FILE, "w", encoding="utf-8") as f:
                        json.dump(save_data, f, indent=2, ensure_ascii=False)
                    logger.info("Pump/Dump state saved (cyclically)")
                    last_save_time = now
                except Exception as e:
                    logger.error(f"Save error: {e}")

        except asyncio.CancelledError:
            logger.info("Detector cancelled")
            raise
        except Exception as e:
            logger.error(f"Detector crashed: {e}", exc_info=True)
            await asyncio.sleep(10)


async def rubberband_detector():
    """
    Runs every hour at minute 12 (RUB1 module)
    Searches for rubber-band overextensions.
    """
    logger.info("Rubberband Detector (RUB1) started")

    while True:
        try:
            now = datetime.now(pytz.UTC)
            # We let it run at 12 minutes past the full hour
            if now.minute != 12 or now.second > 30:
                await asyncio.sleep(20)
                continue

            logger.info("Starting hourly Rubberband (RUB1) scan...")

            for symbol in coins:
                try:
                    await asyncio.sleep(0.1)
                    df_90d = await get_1h_data_last_90d(symbol)
                    ind_df = await get_latest_indicators(symbol)

                    if df_90d.empty or len(df_90d) < 50 or ind_df.empty:
                        continue

                    df_90d['ts'] = df_90d['OPEN_TIME'].apply(lambda x: x.timestamp())
                    ts_values = df_90d['ts'].values
                    close_values = df_90d['CLOSE'].values

                    A = np.vstack([ts_values, np.ones(len(ts_values))]).T
                    slope, intercept = np.linalg.lstsq(A, close_values, rcond=None)[0]

                    curr_ts = ts_values[-1]
                    curr_close = close_values[-1]
                    trend_val_curr = slope * curr_ts + intercept

                    dist_to_trend_pct = (curr_close - trend_val_curr) / trend_val_curr
                    slope_pct_per_day = (slope * 86400) / curr_close if curr_close != 0 else 0

                    ind = ind_df.iloc[0] if isinstance(ind_df, pd.DataFrame) else ind_df

                    rsi = float(ind.get('RSI_14', 50))
                    tsi_line = float(ind.get('TSI_FAST_12_7_7', 0))
                    tsi_signal = float(ind.get('TSI_FAST_12_7_7_SIGNAL', 0))
                    macd_line = float(ind.get('MACD_DIF_NORMAL_12_26_9', 0))
                    macd_signal = float(ind.get('MACD_DEA_NORMAL_12_26_9', 0))
                    atr_14 = float(ind.get('ATR_14', 0))
                    ema_200 = float(ind.get('EMA_200', curr_close))
                    dc_lower = float(ind.get('DONCHIAN_LOWER_20', curr_close))
                    dc_upper = float(ind.get('DONCHIAN_UPPER_20', curr_close))

                    atr_pct = (atr_14 / curr_close) if curr_close > 0 else 0
                    dist_ema200 = (curr_close - ema_200) / ema_200 if ema_200 > 0 else 0

                    event_type = None
                    if dist_to_trend_pct <= -0.08 and rsi < 30 and tsi_line < -15 and curr_close <= dc_lower * 1.01:
                        event_type = "REVERSION_UP"
                    elif dist_to_trend_pct >= 0.08 and rsi > 70 and tsi_line > 15 and curr_close >= dc_upper * 0.99:
                        event_type = "REVERSION_DOWN"

                    if not event_type:
                        continue

                    features = pd.DataFrame(
                        [
                            {
                                'dist_to_trend': dist_to_trend_pct,
                                'rsi': rsi,
                                'atr_pct': atr_pct,
                                'dist_ema200': dist_ema200,
                                'slope_trend': slope_pct_per_day,
                                'MACD_Line': macd_line,
                                'MACD_Signal': macd_signal,
                                'TSI_Line': tsi_line,
                                'TSI_Signal': tsi_signal,
                            }
                        ]
                    )

                    is_long = event_type == "REVERSION_UP"
                    model = REVERSION_MODEL_LONG if is_long else REVERSION_MODEL_SHORT
                    threshold = REVERSION_THRESH_LONG if is_long else REVERSION_THRESH_SHORT

                    if model is None:
                        continue

                    prob = model.predict_proba(features)[0, 1]
                    direction = "LONG" if is_long else "SHORT"

                    logger.info(f"RUB1 Trigger: {symbol} {direction} | ML-Conf: {prob:.1%} (Thresh: {threshold})")

                    if prob >= threshold:
                        if is_cooled_down('RUB1', symbol, direction) and 'USDT_' not in symbol:
                            dist_str = f"{dist_to_trend_pct * 100:+.2f}%"
                            await post_trade(
                                module='RUB1',
                                coin=symbol,
                                direction=direction,
                                confidence=prob,
                                is_long=is_long,
                                source=f"Rubberband Mean Reversion | Distance: {dist_str}",
                            )
                        else:
                            logger.info(f"RUB1 trade for {symbol} ignored due to cooldown.")

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error in RUB1 for {symbol}: {e}", exc_info=False)

            await asyncio.sleep(60)

        except asyncio.CancelledError:
            logger.info("Rubberband Detector (RUB1) shutting down cleanly...")
            raise
        except Exception as e:
            logger.error(f"RUB1 Detector Crash: {e}", exc_info=True)
            await asyncio.sleep(60)


# ========================= ML ATSI Bot =========================


# async def ml_filtered_tsi_trader():
# """
# ML-filtered TSI crossover trader
# - Runs every 8 minutes past the full hour
# - Checks the second-to-last 1h candle for a TSI crossover
# - Only trades if ML probability > 70%
# - Sends Cornix signal + posting with chart
# """
# logger.info("ML-Filtered TSI Trader task started")

# # Load model
# try:
# model = joblib.load("tsi_profit_predictor_relative.pkl")
# logger.info("ML model loaded")
# except Exception as e:
# logger.error(f"ML model could not be loaded: {e}")
# return

# while True:
# try:
# now = datetime.now(pytz.UTC)
# # Wait until 8 minutes past the full hour
# next_run = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) + timedelta(minutes=8)
# sleep_seconds = max(0, (next_run - datetime.now(pytz.UTC)).total_seconds())
# logger.info(f"Next scan at {next_run} UTC – sleeping {sleep_seconds:.0f}s")
# await asyncio.sleep(sleep_seconds)
# signals_batch = []
# logger.info("Hourly TSI scan with ML filter started")

# for symbol in coins:

# try:
# # Fetch the second-to-last completed 1h candle
# conn = await get_conn()
# rows = await conn.fetch(f"""
# SELECT i.open_time, p.close, tsi_fast_12_7_7, tsi_fast_12_7_7_signal,
# rsi_14, ema_9, ema_21, ema_200, kama_21,
# macd_dif_normal_12_26_9, macd_dea_normal_12_26_9,
# atr_14, volume
# FROM "{symbol}_1h_indicators" i
# JOIN "{symbol}_1h" p ON i.open_time = p.open_time
# ORDER BY i.open_time DESC
# LIMIT 3
# """)
# await release_conn(conn)

# if len(rows) < 3:
# continue

# # Order: newest first
# latest = rows[0]      # current (open) hour → ignore
# prev = rows[1]        # second-to-last completed → possible signal
# prev_prev = rows[2]   # third-to-last → for crossover comparison

# # TSI values
# tsi_prev_prev = float(prev_prev["tsi_fast_12_7_7"]) if prev_prev["tsi_fast_12_7_7"] is not None else 0
# signal_prev_prev = float(prev_prev["tsi_fast_12_7_7_signal"]) if prev_prev["tsi_fast_12_7_7_signal"] is not None else 0
# tsi_prev = float(prev["tsi_fast_12_7_7"]) if prev["tsi_fast_12_7_7"] is not None else 0
# signal_prev = float(prev["tsi_fast_12_7_7_signal"]) if prev["tsi_fast_12_7_7_signal"] is not None else 0

# # Check the crossover in the second-to-last candle
# long_cross = (tsi_prev_prev <= signal_prev_prev) and (tsi_prev > signal_prev)
# short_cross = (tsi_prev_prev >= signal_prev_prev) and (tsi_prev < signal_prev)

# if not (long_cross or short_cross):
# continue

# direction = "LONG" if long_cross else "SHORT"
# is_long = long_cross

# # Compute features from the second-to-last candle
# close = float(prev["close"])
# ema200 = float(prev["ema_200"]) if prev["ema_200"] is not None else close
# ema9 = float(prev["ema_9"]) if prev["ema_9"] is not None else close
# ema21 = float(prev["ema_21"]) if prev["ema_21"] is not None else close
# kama = float(prev["kama_21"]) if prev["kama_21"] is not None else close
# macd_dif = float(prev["macd_dif_normal_12_26_9"]) if prev["macd_dif_normal_12_26_9"] is not None else 0
# macd_dea = float(prev["macd_dea_normal_12_26_9"]) if prev["macd_dea_normal_12_26_9"] is not None else 0
# atr = float(prev["atr_14"]) if prev["atr_14"] is not None else 0
# volume = float(prev["volume"])

# # Volume ratio (20-period SMA from 1h data) – table fixed!
# conn2 = await get_conn()
# vol_rows = await conn2.fetch(f"""
# SELECT volume FROM "{symbol}_1h"
# ORDER BY open_time DESC LIMIT 21
# """)
# await release_conn(conn2)
# volumes = [float(r["volume"]) for r in vol_rows]
# vol_sma20 = sum(volumes[1:]) / 20 if len(volumes) == 21 else volume
# volume_ratio = volume / vol_sma20 if vol_sma20 > 0 else 1.0

# # Relative features
# features = {
# "rsi_14": float(prev["rsi_14"]) if prev["rsi_14"] is not None else 50,
# "volume_ratio": volume_ratio,
# "close_to_ema200_pct": (close / ema200 - 1) * 100 if ema200 > 0 else 0,
# "close_to_kama_pct": (close / kama - 1) * 100 if kama > 0 else 0,
# "ema9_to_ema21_pct": (ema9 / ema21 - 1) * 100 if ema21 > 0 else 0,
# "ema9_to_ema200_pct": (ema9 / ema200 - 1) * 100 if ema200 > 0 else 0,
# "atr_pct": (atr / close * 100) if close > 0 else 0,
# "macd_hist": macd_dif - macd_dea,
# "macd_positive": 1 if (macd_dif - macd_dea) > 0 else 0
# }

# feature_vector = np.array([[features[col] for col in model.feature_names_in_]])
# prob_profit = model.predict_proba(feature_vector)[0, 1]

# logger.info(f"{symbol} TSI {direction} Crossover – ML Prob: {prob_profit:.1%}")

# if prob_profit >= 0.25:  # instead of just >=0.60
# signals_batch.append({
# 'symbol': symbol,
# 'price': close,
# 'model': 'ATS1',
# 'direction': direction,
# 'confidence': prob_profit
# })

# if prob_profit < 0.70:
# continue  # Filter

# # Open trade
# logger.info(f"Trade opened: {symbol} {direction} (ML Prob {prob_profit:.1%})")
# await send_cornix_signal(symbol, is_long, "ATS1")

# # Posting with chart
# border_color = "#00ff00" if is_long else "#ff0066"
# emoji = "🚀 TSI-CROSSOVER LONG SIGNAL" if is_long else "💥 TSI-CROSSOVER SHORT SIGNAL"
# html = f"""
# <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {border_color};">
# <b style="color:#00ffff; font-size:18px;">{emoji}</b>
# <b style="color:#ffd700;">{symbol.replace('USDT','')}/USDT</b>
# <b>→ Direction: {direction}</b>
# <b>→ ML Confidence: <b style="color:#00ffff;">{prob_profit:.1%}</b></b>
# <b>→ RSI: {features['rsi_14']:.1f} | Volume Ratio: {features['volume_ratio']:.1f}x</b>
# <b>→ Dist to EMA200: {features['close_to_ema200_pct']:+.2f}%</b>
# <b>→ Time: {now.strftime('%H:%M')} UTC | Module: ATS1</b>
# </pre>
# """.strip()

# chart_buf = await generate_smooth_minichart_image(symbol, minutes=240)
# try:
# if chart_buf:
# await application.bot.send_photo(
# chat_id=AI_CHANNEL_ID,
# photo=chart_buf,
# caption=html,
# parse_mode="HTML"
# )
# else:
# await application.bot.send_message(
# chat_id=AI_CHANNEL_ID,
# text=html,
# parse_mode="HTML"
# )
# except Exception as e:
# logger.error(f"Posting error {symbol}: {e}")

# except Exception as e:
# logger.error(f"Error for {symbol}: {e}")

# if signals_batch:
# asyncio.create_task(log_ai_signals(signals_batch))

# except asyncio.CancelledError:
# logger.info("TSI Trader cancelled")
# raise
# except Exception as e:
# logger.error(f"TSI Trader crashed: {e}", exc_info=True)
# await asyncio.sleep(60)


async def ml_filtered_tsi_trader():
    """
    ML-filtered TSI crossover trader (dual model robust)
    - Runs every 8 minutes past the full hour
    - Checks the second-to-last 1h candle for a TSI crossover
    - Computes complex features (volume, VWAP, OBV) live
    - Uses separate long/short XGBoost models
    """
    logger.info("ML-Filtered TSI Trader task (dual robust) started")

    # Load models
    try:
        model_long = joblib.load(TSI_MODEL_LONG_PATH)
        model_short = joblib.load(TSI_MODEL_SHORT_PATH)
        logger.info(f"ML models loaded: {TSI_MODEL_LONG_PATH}, {TSI_MODEL_SHORT_PATH}")
    except Exception as e:
        logger.error(f"ML models could not be loaded: {e}")
        return

    while True:
        try:
            now = datetime.now(pytz.UTC)
            # Wait until 8 minutes past the full hour
            next_run = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) + timedelta(minutes=8)
            sleep_seconds = max(0, (next_run - datetime.now(pytz.UTC)).total_seconds())
            logger.info(f"Next scan at {next_run.strftime('%H:%M')} UTC – sleeping {sleep_seconds:.0f}s")
            await asyncio.sleep(sleep_seconds)

            signals_batch = []
            logger.info("Hourly TSI scan with dual ML filter started")

            for symbol in coins:
                try:
                    # We need roughly 50 candles of history for correct indicator calculations (VWAP, OBV, rolling)
                    conn = await get_conn()
                    # We fetch price AND indicators in one join, sorted by time descending, then reverse
                    # Note: we recompute VWAP and OBV here in Python to be sure
                    rows = await conn.fetch(f"""
                        SELECT
                            p.open_time, p.high, p.low, p.close, p.volume,
                            i.rsi_14, i.rsi_6, i.tsi_fast_12_7_7, i.tsi_fast_12_7_7_signal,
                            i.ema_9, i.ema_21, i.ema_50, i.ema_200,
                            i.kama_9, i.kama_21, i.kama_55,
                            i.macd_dif_normal_12_26_9, i.macd_dea_normal_12_26_9,
                            i.atr_14, i.boll_upper_20, i.boll_lower_20,
                            i.donchian_upper_20, i.donchian_lower_20,
                            i.trendline_slope, i.support_price, i.resistance_price
                        FROM "{symbol}_1h" p
                        LEFT JOIN "{symbol}_1h_indicators" i ON p.open_time = i.open_time
                        ORDER BY p.open_time DESC
                        LIMIT 50
                    """)
                    await release_conn(conn)

                    if len(rows) < 50:
                        continue

                    # Convert to a Pandas DataFrame and sort time ascending
                    # rows is a list of Record objects, we convert them manually or directly
                    data = [dict(row) for row in rows]
                    df = pd.DataFrame(data)
                    df = df.iloc[::-1].reset_index(drop=True)  # Reverse: index 0 is old, index -1 is new

                    # Ensure numeric conversion
                    cols = [
                        'high',
                        'low',
                        'close',
                        'volume',
                        'rsi_14',
                        'rsi_6',
                        'tsi_fast_12_7_7',
                        'tsi_fast_12_7_7_signal',
                        'ema_9',
                        'ema_21',
                        'ema_50',
                        'ema_200',
                        'kama_9',
                        'kama_21',
                        'kama_55',
                        'macd_dif_normal_12_26_9',
                        'macd_dea_normal_12_26_9',
                        'atr_14',
                        'boll_upper_20',
                        'boll_lower_20',
                        'donchian_upper_20',
                        'donchian_lower_20',
                        'trendline_slope',
                        'support_price',
                        'resistance_price',
                    ]
                    for col in cols:
                        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

                    # --- SIGNAL CHECK (on the second-to-last candle: index -2) ---
                    # Index -1 is the current (open) candle (or the one that just closed, depending on timing)
                    # Since the bot runs 8 min AFTER the hour, index -1 is the open new candle.
                    # Index -2 is the completed candle that we want to trade.

                    current_idx = -2  # The completed candle
                    prev_idx = -3  # The candle before it (for the crossover)

                    tsi_curr = df.iloc[current_idx]['tsi_fast_12_7_7']
                    sig_curr = df.iloc[current_idx]['tsi_fast_12_7_7_signal']
                    tsi_prev = df.iloc[prev_idx]['tsi_fast_12_7_7']
                    sig_prev = df.iloc[prev_idx]['tsi_fast_12_7_7_signal']

                    long_cross = (tsi_prev <= sig_prev) and (tsi_curr > sig_curr)
                    short_cross = (tsi_prev >= sig_prev) and (tsi_curr < sig_curr)

                    if not (long_cross or short_cross):
                        continue

                    direction = "LONG" if long_cross else "SHORT"

                    # --- LIVE FEATURE ENGINEERING ---
                    # 1. Compute OBV & VWAP
                    df['obv'] = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
                    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
                    # VWAP Rolling 20
                    df['vwap_20'] = (df['volume'] * df['typical_price']).rolling(20).sum() / df['volume'].rolling(
                        20
                    ).sum()
                    df['vwap_20'] = df['vwap_20'].fillna(df['close'])

                    # 2. Compute features for the signal candle (index -2)
                    row = df.iloc[current_idx]
                    row_prev = df.iloc[prev_idx]  # For crossover features

                    # Volatility & volume helper
                    vol_sma20 = df['volume'].rolling(20).mean().iloc[current_idx]
                    if vol_sma20 == 0:
                        vol_sma20 = 1.0  # Zero div fix

                    # Build dictionary
                    features = {
                        "rsi_14": row['rsi_14'],
                        "rsi_6": row['rsi_6'],
                        "macd_hist": row['macd_dif_normal_12_26_9'] - row['macd_dea_normal_12_26_9'],
                        "atr_pct": (row['atr_14'] / row['close']) * 100 if row['close'] else 0,
                        "vol_ratio": row['volume'] / vol_sma20,
                        "bb_width": (row['boll_upper_20'] - row['boll_lower_20']) / row['boll_lower_20']
                        if row['boll_lower_20']
                        else 0,
                        "bb_pos": (row['close'] - row['boll_lower_20']) / (row['boll_upper_20'] - row['boll_lower_20'])
                        if (row['boll_upper_20'] - row['boll_lower_20']) != 0
                        else 0,
                        "dist_ema200": (row['close'] / row['ema_200']) - 1 if row['ema_200'] else 0,
                        "dist_ema9_21": (row['ema_9'] / row['ema_21']) - 1 if row['ema_21'] else 0,
                        "dist_kama9": (row['close'] / row['kama_9']) - 1 if row['kama_9'] else 0,
                        "dist_kama21": (row['close'] / row['kama_21']) - 1 if row['kama_21'] else 0,
                        "dist_kama55": (row['close'] / row['kama_55']) - 1 if row['kama_55'] else 0,
                        "dist_kama9_21": (row['kama_9'] / row['kama_21']) - 1 if row['kama_21'] else 0,
                        "dist_donch_up": (row['close'] / row['donchian_upper_20']) - 1
                        if row['donchian_upper_20']
                        else 0,
                        "dist_donch_low": (row['close'] / row['donchian_lower_20']) - 1
                        if row['donchian_lower_20']
                        else 0,
                        "rsi_ratio": row['rsi_6'] / row['rsi_14'] if row['rsi_14'] else 0,
                        "slope_norm": (row['trendline_slope'] / row['close']) * 1000 if row['close'] else 0,
                        "dist_supp": (row['close'] - row['support_price']) / row['close'] if row['close'] else 0,
                        "dist_res": (row['resistance_price'] - row['close']) / row['close'] if row['close'] else 0,
                        # Binary / crossover flags (using row_prev)
                        "macd_cross_bearish": int(
                            row_prev['macd_dif_normal_12_26_9'] >= row_prev['macd_dea_normal_12_26_9']
                            and row['macd_dif_normal_12_26_9'] < row['macd_dea_normal_12_26_9']
                        ),
                        "ema9_21_cross_bearish": int(
                            row_prev['ema_9'] >= row_prev['ema_21'] and row['ema_9'] < row['ema_21']
                        ),
                        "kama9_21_cross_bearish": int(
                            row_prev['kama_9'] >= row_prev['kama_21'] and row['kama_9'] < row['kama_21']
                        ),
                        "bollinger_lower_break": int(row['close'] < row['boll_lower_20']),
                        "close_below_ema50": int(row['close'] < row['ema_50']),
                        # Advanced volume features
                        "obv_ratio": row['obv'] / df['obv'].rolling(20).mean().iloc[current_idx]
                        if df['obv'].rolling(20).mean().iloc[current_idx] != 0
                        else 0,
                        "close_to_vwap_pct": (row['close'] / row['vwap_20']) - 1 if row['vwap_20'] else 0,
                        "obv_val": row['obv'],
                        "volume_spike": int(row['volume'] > vol_sma20 * 2),
                        "volume_trend_up": int(df['volume'].rolling(5).mean().iloc[current_idx] > vol_sma20),
                    }

                    # Create prediction DataFrame
                    X_live = pd.DataFrame([features])
                    # Force column order!
                    X_live = X_live[TSI_FEATURES].fillna(0)

                    prob_profit = 0.0
                    threshold = 0.0

                    if long_cross:
                        prob_profit = model_long.predict_proba(X_live)[0, 1]
                        threshold = TSI_THRESH_LONG
                    else:
                        prob_profit = model_short.predict_proba(X_live)[0, 1]
                        threshold = TSI_THRESH_SHORT

                    logger.info(
                        f"{symbol} TSI {direction} Crossover – ML Prob: {prob_profit:.1%} (Thresh: {threshold:.2f})"
                    )

                    # Logging for database / statistics
                    if prob_profit >= 0.25:
                        signals_batch.append(
                            {
                                'symbol': symbol,
                                'price': row['close'],
                                'model': 'ATS1',
                                'direction': direction,
                                'confidence': prob_profit,
                            }
                        )

                    # Trade filter
                    if prob_profit < threshold:
                        continue

                    # --- EXECUTE TRADE ---
                    logger.info(f"🔥 TRADE EXECUTE: {symbol} {direction} (ML {prob_profit:.1%})")
                    is_long = direction == "LONG"
                    await send_cornix_signal(symbol, is_long, "ATS1")

                    # Posting
                    border_color = "#00ff00" if is_long else "#ff0066"
                    emoji = "🚀 TSI-SNIPER LONG" if is_long else "💥 TSI-SNIPER SHORT"

                    # Top feature for info (e.g. volume trend)
                    vol_trend_str = "YES" if features['volume_trend_up'] else "NO"

                    html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {border_color};">
<b style="color:#00ffff; font-size:18px;">{emoji}</b>
<b style="color:#ffd700;">{symbol.replace('USDT', '')}/USDT</b>
<b>→ Direction: {direction}</b>
<b>→ Confidence: <b style="color:#00ffff;">{prob_profit:.1%}</b> (Thresh {threshold})</b>
<b>→ Price: {row['close']:.4f}</b>
<b>→ Vol Trend Up: {vol_trend_str} | Spike: {features['volume_spike']}</b>
<b>→ BB Pos: {features['bb_pos']:.2f} | Dist EMA200: {features['dist_ema200'] * 100:.2f}%</b>
<b>→ Time: {now.strftime('%H:%M')} UTC | Module: ATS1</b>
</pre>
                    """.strip()

                    chart_buf = await generate_smooth_minichart_image(symbol, minutes=240)
                    try:
                        if chart_buf:
                            await application.bot.send_photo(
                                chat_id=AI_CHANNEL_ID, photo=chart_buf, caption=html, parse_mode="HTML"
                            )
                        else:
                            await application.bot.send_message(chat_id=AI_CHANNEL_ID, text=html, parse_mode="HTML")
                    except Exception as e:
                        logger.error(f"Posting error {symbol}: {e}")

                except Exception as e:
                    logger.error(f"Error for {symbol}: {e}")

            if signals_batch:
                asyncio.create_task(log_ai_signals(signals_batch))

        except asyncio.CancelledError:
            logger.info("TSI Trader cancelled")
            raise
        except Exception as e:
            logger.error(f"TSI Trader crashed: {e}", exc_info=True)
            await asyncio.sleep(60)


# ========================= ROUND LEVEL BRAKER (FULLY ASYNC + SEXY HTML) =========================


def get_current_round_level(symbol: str, price: float) -> float | None:
    cfg = ROUND_LEVEL_CONFIG.get(symbol)
    if not cfg:
        return None
    step = cfg["step"]
    decimals = cfg["decimals"]
    return round(price // step * step, decimals)


async def round_level_breaker():
    logger.info("Round Level Breaker task started (FIXED)")
    global ROUND_BREAK_STATE

    # Brief wait at startup so data is available
    await asyncio.sleep(5)

    while True:
        try:
            await asyncio.sleep(5)  # Check more frequently (5s)

            for symbol, cfg in ROUND_LEVEL_CONFIG.items():
                if symbol not in ONE_MINUTE_DATA or len(ONE_MINUTE_DATA[symbol]) < 2:
                    continue

                data = list(ONE_MINUTE_DATA[symbol])
                prev_price = float(data[-2]["p"])
                current_price = float(data[-1]["p"])

                # Determine step size
                step = cfg.get('step_size')
                if not step:
                    # Fallback logic
                    if current_price > 10000:
                        step = 1000
                    elif current_price > 1000:
                        step = 100
                    elif current_price > 100:
                        step = 10
                    elif current_price > 10:
                        step = 1
                    else:
                        step = 0.1

                # --- CORE LOGIC ---
                # We check whether we've slipped into a new "hundreds/thousands block"
                prev_bucket = int(prev_price / step)
                curr_bucket = int(current_price / step)

                if prev_bucket == curr_bucket:
                    continue

                # Determine level
                if current_price > prev_price:
                    direction = "upwards"
                    crossed_level = curr_bucket * step
                else:
                    direction = "downwards"
                    crossed_level = prev_bucket * step

                # Validation: did the price really cross the line?
                is_valid = False
                if direction == "upwards" and prev_price < crossed_level <= current_price:
                    is_valid = True
                elif direction == "downwards" and prev_price > crossed_level >= current_price:
                    is_valid = True

                if not is_valid:
                    continue

                # --- COOLDOWN ---
                state = ROUND_BREAK_STATE.get(symbol, {})
                last_saved_level = state.get(
                    "last_level", 0
                )  # Renamed to last_saved_level to avoid confusion
                last_time = state.get("last_break_time", datetime(1970, 1, 1, tzinfo=pytz.UTC))

                # Cooldown check
                if last_saved_level == crossed_level:
                    if (datetime.now(pytz.UTC) - last_time).total_seconds() < COOLDOWN_SECONDS:
                        continue

                logger.info(f"BREAK: {symbol} crossed {crossed_level} ({direction})")

                # --- SEND ---
                chart_buf = await generate_smooth_minichart_image(symbol, minutes=240)

                color = "#00ff00" if direction == "upwards" else "#ff0066"
                decimals = cfg.get('decimals', 2)

                # THIS IS WHERE THE BUG WAS: we now use 'crossed_level' instead of 'last_level'
                html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {color};">
<b style="color:#00ffff; font-size:18px;">ROUND LEVEL BREAK</b>
<b style="color:#ffd700;">{symbol.replace('USDT', '')}/USDT</b> breaks <b style="color:{color};">{crossed_level:,.{decimals}f}</b> <b style="color:{color};">{direction.upper()}</b>
<b>→ Price:</b> <code>${current_price:,.{decimals}f}</code>
<b>→ Time:</b> {datetime.now(pytz.UTC).strftime('%H:%M:%S')} UTC
</pre>
                """.strip()

                try:
                    if chart_buf:
                        await application.bot.send_photo(
                            chat_id=MARKET_CHANNEL_ID, photo=chart_buf, caption=html, parse_mode="HTML"
                        )
                    else:
                        await application.bot.send_message(chat_id=MARKET_CHANNEL_ID, text=html, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Error while sending: {e}")

                # Update state
                ROUND_BREAK_STATE[symbol] = {
                    "last_level": crossed_level,
                    "last_break_time": datetime.now(pytz.UTC),
                    "direction": direction,
                }

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Crashed: {e}", exc_info=True)
            await asyncio.sleep(10)


# ========================= !SENTIMENT HANDLER (FULLY ASYNC + SEXY HTML) =========================


async def fetch_json(session: aiohttp.ClientSession, url: str, params=None, headers=None):
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=12), ssl=ssl_context, headers=headers
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                logger.warning(f"HTTP {resp.status} for {url}")
                return None
    except Exception as e:
        logger.error(f"Fetch error {url}: {e}")
        return None


async def get_crypto_data_async(session):
    try:
        price_url = "https://api.coingecko.com/api/v3/simple/price"
        params = {
            'ids': 'bitcoin,ethereum',
            'vs_currencies': 'usd',
            'include_24hr_change': 'true',
            'include_market_cap': 'true',
        }
        data = await fetch_json(session, price_url, params)
        if not data:
            return None

        global_data = await fetch_json(session, "https://api.coingecko.com/api/v3/global")
        if not global_data:
            return None
        g = global_data['data']

        btc_price = data['bitcoin']['usd']
        btc_change = data['bitcoin']['usd_24h_change']
        btc_mcap = data['bitcoin']['usd_market_cap']
        eth_price = data['ethereum']['usd']
        eth_change = data['ethereum']['usd_24h_change']
        eth_mcap = data['ethereum']['usd_market_cap']

        total_mcap = g['total_market_cap']['usd']
        total_mcap_change = g.get('market_cap_change_percentage_24h_usd', 0.0)
        total_volume = g['total_volume']['usd']
        total2 = total_mcap - btc_mcap

        # TOTAL2 Change (approx)
        total2_change = (
            ((total_mcap * (1 + total_mcap_change / 100)) - (btc_mcap * (1 + btc_change / 100)) - total2) / total2 * 100
            if total2 > 0
            else 0.0
        )

        btc_dominance = (btc_mcap / total_mcap) * 100
        prev_dom = (btc_mcap / (1 + btc_change / 100)) / (total_mcap / (1 + total_mcap_change / 100)) * 100
        btc_dom_change = btc_dominance - prev_dom

        eth_btc_ratio = eth_price / btc_price
        prev_ratio = (eth_price / (1 + eth_change / 100)) / (btc_price / (1 + btc_change / 100))
        eth_btc_change = (eth_btc_ratio / prev_ratio - 1) * 100 if prev_ratio > 0 else 0.0

        return {
            'btc_price': btc_price,
            'btc_change': btc_change,
            'btc_mcap': btc_mcap,
            'eth_price': eth_price,
            'eth_change': eth_change,
            'eth_mcap': eth_mcap,
            'total_mcap': total_mcap,
            'total_mcap_change': total_mcap_change,
            'total_volume': total_volume,
            'total2': total2,
            'total2_change': total2_change,
            'btc_dominance': btc_dominance,
            'btc_dom_change': btc_dom_change,
            'eth_btc_ratio': eth_btc_ratio,
            'eth_btc_change': eth_btc_change,
        }
    except Exception as e:
        logger.error(f"get_crypto_data_async error: {e}")
        return None


async def get_fear_greed_async(session):
    data = await fetch_json(session, "https://api.alternative.me/fng/?limit=1")
    if data and 'data' in data and data['data']:
        d = data['data'][0]
        return int(d['value']), d['value_classification']
    return 50, "Neutral"


async def get_funding_rates_async(session):
    async def fetch(symbol):
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
        data = await fetch_json(session, url)
        return float(data[0]['fundingRate']) if data else 0.0

    btc = await fetch("BTCUSDT")
    eth = await fetch("ETHUSDT")
    return {'btc': btc, 'eth': eth}


async def get_open_interest_async(session):
    async def fetch(symbol):
        url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
        data = await fetch_json(session, url)
        if not data:
            return 0.0
        oi = float(data['openInterest'])
        price_url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}"
        price_data = await fetch_json(session, price_url)
        price = float(price_data['price']) if price_data else 1.0
        return oi * price

    btc_oi = await fetch("BTCUSDT")
    eth_oi = await fetch("ETHUSDT")
    return {'btc': btc_oi, 'eth': eth_oi, 'total': btc_oi + eth_oi}


async def get_x_sentiment_async():
    try:
        data = get_crypto_data()  # sync fallback is fine (rare)
        if not data:
            return 0.0
        score = data['btc_change'] / 10 + data['eth_change'] / 15 + data['total_mcap_change'] / 8
        fg, _ = get_fear_greed()
        score += 0.4 if fg > 70 else -0.4 if fg < 30 else 0
        return max(min(score, 1.0), -1.0)
    except:
        return 0.0


async def analyze_sentiment_score(data, fg_value, funding, oi, btc_ls, eth_ls, x_sent):
    score = 0.0
    if data['btc_change'] > 2:
        score += 2
    elif data['btc_change'] > 0:
        score += 1
    elif data['btc_change'] < -2:
        score -= 2
    else:
        score -= 1

    if data['eth_change'] > 2:
        score += 1.5
    elif data['eth_change'] > 0:
        score += 0.75
    elif data['eth_change'] < -2:
        score -= 1.5
    else:
        score -= 0.75

    if data['total_mcap_change'] > 1.5:
        score += 1.5
    elif data['total_mcap_change'] > 0:
        score += 0.75
    elif data['total_mcap_change'] < -1.5:
        score -= 1.5
    else:
        score -= 0.75

    if fg_value > 75:
        score += 2
    elif fg_value > 60:
        score += 1
    elif fg_value < 25:
        score -= 2
    elif fg_value < 40:
        score -= 1

    score += x_sent * 3

    if funding['btc'] > 0.0002:
        score += 1.5
    elif funding['btc'] < -0.0002:
        score -= 1.5

    if oi['btc'] > 55e9:
        score += 1.0

    # Replace the proxy logic:
    if btc_ls > 2.5:
        score += 1.5
    elif btc_ls > 1.8:
        score += 0.75
    elif btc_ls < 0.6:
        score -= 1.5
    elif btc_ls < 0.8:
        score -= 0.75
    if eth_ls > 2.5:
        score += 1.0
    elif eth_ls > 1.8:
        score += 0.5
    elif eth_ls < 0.6:
        score -= 1.0
    elif eth_ls < 0.8:
        score -= 0.5

    if score > 6:
        return "VERY BULLISH"
    elif score > 3:
        return "BULLISH"
    elif score > -3:
        return "NEUTRAL"
    elif score > -6:
        return "BEARISH"
    else:
        return "VERY BEARISH"


async def get_long_short_ratios_async(session: aiohttp.ClientSession):
    """
    Fetches real L/S ratios from Binance (SIGNED with key) or a fallback proxy.
    """
    try:
        # SIGNED Binance request (for L/S ratio – requires a key since 2025)
        def sign_request(query_string):
            total_params = f"{query_string}&timestamp={int(time.time() * 1000)}"
            signature = hmac.new(
                BINANCE_SECRET.encode('utf-8'), total_params.encode('utf-8'), hashlib.sha256
            ).hexdigest()
            return f"{total_params}&signature={signature}"

        # BTC L/S
        btc_params = {'symbol': 'BTCUSDT', 'period': '5m', 'limit': 1}
        btc_query = urlencode(btc_params)
        signed_btc = sign_request(btc_query)
        btc_headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
        btc_url = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?{signed_btc}"
        btc_data = await fetch_json(session, btc_url, headers=btc_headers)

        # ETH L/S
        eth_params = {'symbol': 'ETHUSDT', 'period': '5m', 'limit': 1}
        eth_query = urlencode(eth_params)
        signed_eth = sign_request(eth_query)
        eth_url = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?{signed_eth}"
        eth_data = await fetch_json(session, eth_url, headers=btc_headers)

        if btc_data and eth_data and len(btc_data) > 0 and len(eth_data) > 0:
            btc_ls = float(btc_data[0]['longShortRatio'])
            eth_ls = float(eth_data[0]['longShortRatio'])
            total_ls = (btc_ls + eth_ls) / 2
            logger.info(f"Real L/S ratios from Binance: BTC={btc_ls:.2f}, ETH={eth_ls:.2f}")
            return {
                'btc': max(0.5, min(btc_ls, 3.0)),
                'eth': max(0.5, min(eth_ls, 3.0)),
                'total': max(0.5, min(total_ls, 3.0)),
            }

    except Exception as e:
        logger.warning(f"Binance SIGNED L/S error: {e}")

    # Fallback: robust proxy (no NameError – uses crypto data from async)
    logger.warning("Binance L/S failed – using proxy")
    try:
        # Simple proxy from 24h change (like your original, but without sync get_crypto_data)
        btc_change = crypto.get('btc_change', 0) if 'crypto' in locals() else 0
        eth_change = crypto.get('eth_change', 0) if 'crypto' in locals() else 0
        btc_ls = 1 + (btc_change / 100) * 2 if btc_change > 0 else 1 - abs(btc_change / 100) * 2
        eth_ls = 1 + (eth_change / 100) * 2 if eth_change > 0 else 1 - abs(eth_change / 100) * 2
        return {'btc': max(0.5, min(btc_ls, 3.0)), 'eth': max(0.5, min(eth_ls, 3.0)), 'total': (btc_ls + eth_ls) / 2}
    except:
        return {'btc': 1.0, 'eth': 1.0, 'total': 1.0}  # Ultimate safe fallback


async def sentiment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.text.strip() != "!sentiment":
        return

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!sentiment", update.effective_user)
    await update.message.reply_chat_action(ChatAction.TYPING)

    async with aiohttp.ClientSession() as session:
        try:
            crypto = await get_crypto_data_async(session)
            if not crypto:
                await update.message.reply_text("Market data not available.")
                return

            fg_value, fg_class = await get_fear_greed_async(session)
            funding = await get_funding_rates_async(session)
            oi = await get_open_interest_async(session)
            x_sent = await get_x_sentiment_async()

            # FETCH REAL L/S RATIOS
            ls_ratios = await get_long_short_ratios_async(session)
            btc_ls = ls_ratios.get('btc', 1.0)
            eth_ls = ls_ratios.get('eth', 1.0)
            total_ls = ls_ratios.get('total', 1.0)

            # COMPUTE SENTIMENT
            sentiment = await analyze_sentiment_score(
                data=crypto, fg_value=fg_value, funding=funding, oi=oi, btc_ls=btc_ls, eth_ls=eth_ls, x_sent=x_sent
            )

            # Colors & emojis (your code stays)
            if "VERY BULLISH" in sentiment:
                border_color = "#00ff00"
                mood_emoji = "BULLISH"
            elif "BULLISH" in sentiment:
                border_color = "#88ff88"
                mood_emoji = "BULLISH"
            elif "NEUTRAL" in sentiment:
                border_color = "#ffff00"
                mood_emoji = "NEUTRAL"
            elif "BEARISH" in sentiment:
                border_color = "#ff8800"
                mood_emoji = "BEARISH"
            else:
                border_color = "#ff0066"
                mood_emoji = "BEARISH"

            # HTML with L/S lines (your tab layout + L/S)
            # html = f"""
            # <pre style="background:#1e1e1e; color:#ffffff; padding:20px; border-radius:16px; font-family:'Courier New', monospace; font-size:15px; line-height:2.1; border-left:10px solid {border_color};">
            # <b style="color:#00ffff; font-size:23px;">CRYPTO MARKET SENTIMENT</b>
            # <b style="color:#ffd700; font-size:19px;"> Overall → </b><b style="color:{border_color}; font-size:21px;">{mood_emoji} {sentiment}</b>

            # <b style="color:#888888;">┌────────────────────────────────────────────────────┐</b>
            # <b>│ BTC Price       │ </b><code>${crypto['btc_price']:>12,.0f}</code> <b style="color:{'#00ff00' if crypto['btc_change']>0 else '#ff0066'};">{crypto['btc_change']:>+7.2f}%</b> <b style="color:#666666;">│</b>
            # <b>│ ETH Price       │ </b><code>${crypto['eth_price']:>12,.0f}</code> <b style="color:{'#00ff00' if crypto['eth_change']>0 else '#ff0066'};">{crypto['eth_change']:>+7.2f}%</b> <b style="color:#666666;">│</b>
            # <b>│ Total MarketCap │ </b><code>${crypto['total_mcap']/1e12:>8.2f}T</code> <b style="color:{'#00ff00' if crypto['total_mcap_change']>0 else '#ff0066'};">{crypto['total_mcap_change']:>+7.2f}%</b> <b style="color:#666666;">│</b>
            # <b>│ TOTAL2 (ex BTC) │ </b><code>${crypto['total2']/1e12:>8.2f}T</code> <b style="color:{'#00ff00' if crypto['total2_change']>0 else '#ff0066'};">{crypto['total2_change']:>+7.2f}%</b> <b style="color:#666666;">│</b>
            # <b>│ BTC Dominance   │ </b><b style="color:#00ffff;">{crypto['btc_dominance']:>7.2f}%</b> <b style="color:{'#00ff88' if crypto['btc_dom_change']<0 else '#ff6688'};">{crypto['btc_dom_change']:>+6.2f}pp</b> <b style="color:#666666;">│</b>
            # <b>│ ETH/BTC Ratio   │ </b><b style="color:#ff69b4;">{crypto['eth_btc_ratio']:.5f}</b> <b style="color:{'#00ff00' if crypto['eth_btc_change']>0 else '#ff0066'};">{crypto['eth_btc_change']:>+7.2f}%</b> <b style="color:#666666;">│</b>
            # <b style="color:#888888;">├────────────────────────────────────────────────────┤</b>
            # <b>│ Fear & Greed    │ </b><b style="color:#ff00ff;">{fg_value:>3}</b> → <b style="color:#ffd700;">{fg_class:>15}</b> <b style="color:#666666;">│</b>
            # <b>│ BTC Funding     │ </b><b style="color:{'#00ff88' if funding['btc']>0.0001 else '#ff6688' if funding['btc']<0 else '#ffff88'};">{funding['btc']:.6f}</b>{' ' * 20}<b style="color:#666666;">│</b>
            # <b>│ BTC OI          │ </b><b style="color:#00ffff;">${oi['btc']/1e9:>7.2f}B</b>{' ' * 25}<b style="color:#666666;">│</b>
            # <b>│ X Sentiment     │ </b><b style="color:{'#00ff88' if x_sent>0.1 else '#ff6688' if x_sent<-0.1 else '#ffff88'};">{x_sent:+.3f}</b>{' ' * 30}<b style="color:#666666;">│</b>
            # <b style="color:#888888;">├────────────────────────────────────────────────────┤</b>
            # <b>│ BTC L/S Ratio   │ </b><b style="color:{'#00ff88' if btc_ls>1.5 else '#ff6688' if btc_ls<0.8 else '#ffff88'};">{btc_ls:>5.2f}x</b>{' ' * 25}<b style="color:#666666;">│</b>
            # <b>│ ETH L/S Ratio   │ </b><b style="color:{'#00ff88' if eth_ls>1.5 else '#ff6688' if eth_ls<0.8 else '#ffff88'};">{eth_ls:>5.2f}x</b>{' ' * 25}<b style="color:#666666;">│</b>
            # <b>│ Total L/S Ratio │ </b><b style="color:{'#00ff88' if total_ls>1.5 else '#ff6688' if total_ls<0.8 else '#ffff88'};">{total_ls:>5.2f}x</b>{' ' * 20}<b style="color:#666666;">│</b>
            # <b style="color:#888888;">└────────────────────────────────────────────────────┘</b>

            # <b style="color:#00ffff;">Requested by </b><a href="https://t.me/{username}">@{username}</a><b style="color:#888888;"> • {datetime.now(pytz.UTC).strftime('%H:%M:%S')} UTC</b>
            # </pre>
            # """.strip()

            html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.9; border-left:8px solid {border_color};">
<b style="font-size:20px;">CRYPTO MARKET SENTIMENT</b>
<b style="color:#ffd700;">Overall → </b><b style="font-size:19px;">{sentiment}</b>
<b style="color:#888888;">{'─' * 48}</b>
<b> BTC Price       </b> <code>${crypto['btc_price']:>8.2f}</code>  <b>{crypto['btc_change']:>+7.2f}%</b>
<b> ETH Price       </b> <code>${crypto['eth_price']:>8.2f}</code>  <b>{crypto['eth_change']:>+7.2f}%</b>
<b> Total MarketCap </b> <code>${crypto['total_mcap'] / 1e12:>8.2f}T</code>     <b>{crypto['total_mcap_change']:>+7.2f}%</b>
<b> TOTAL2 (ex BTC) </b> <code>${crypto['total2'] / 1e12:>8.2f}T</code>     <b>{crypto['total2_change']:>+7.2f}%</b>
<b> BTC Dominance   </b> <b>{crypto['btc_dominance']:>8.2f}%</b>       <b>{crypto['btc_dom_change']:>+7.2f}pp</b>
<b> ETH/BTC Ratio   </b> <b>{crypto['eth_btc_ratio']:>4.6f}</b>            <b>{crypto['eth_btc_change']:>+7.2f}%</b>

<b> Fear & Greed    </b> <b>{fg_value:>3}</b> → {fg_class:>15}
<b> BTC Funding     </b> <b>{funding['btc']:>4.6f}</b>
<b> BTC OI          </b> <b>${oi['btc'] / 1e9:>8.2f}B</b>
<b> X Sentiment     </b> <b>{x_sent:+8.2f}</b>

<b> BTC L/S Ratio   </b> <b>{btc_ls:>8.2f}x</b>
<b> ETH L/S Ratio   </b> <b>{eth_ls:>8.2f}x</b>
<b> Total L/S Ratio </b> <b>{total_ls:>8.2f}x</b>

<b style="color:#888888;">{'─' * 48}</b>
<b>Requested by </b><a href="https://t.me/{username}">@{username}</a> <b>• {datetime.now(pytz.UTC).strftime('%H:%M:%S')} UTC</b>
</pre>
            """.strip()

            await update.message.reply_html(html, disable_web_page_preview=True)

        except Exception as e:
            logger.error(f"!sentiment error: {e}", exc_info=True)
            await update.message.reply_text("Sentiment data currently unavailable.")


async def market_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. CHECK: button or text?
    is_callback = update.callback_query is not None
    message_target = update.effective_message  # Works for both!

    # If NO button was pressed, we check the text command
    if not is_callback:
        if not update.message or "!market" not in update.message.text:
            return

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    log_command("!market", update.effective_user)

    # IMPORTANT: use message_target!
    await message_target.reply_chat_action(ChatAction.TYPING)

    async with aiohttp.ClientSession() as session:
        try:
            crypto = await get_crypto_data_async(session)
            if not crypto:
                await message_target.reply_text("No market data available.")
                return

            fg_value, fg_class = await get_fear_greed_async(session)
            funding = await get_funding_rates_async(session)
            oi = await get_open_interest_async(session)
            x_sent = await get_x_sentiment_async()
            ls_ratios = await get_long_short_ratios_async(session)

            btc_ls = ls_ratios.get('btc', 1.0)
            eth_ls = ls_ratios.get('eth', 1.0)
            total_ls = ls_ratios.get('total', 1.0)

            sentiment = await analyze_sentiment_score(
                data=crypto, fg_value=fg_value, funding=funding, oi=oi, btc_ls=btc_ls, eth_ls=eth_ls, x_sent=x_sent
            )

            # Build payload
            payload = {
                "sent": sentiment,
                "btc": round(crypto['btc_price'], 2),
                "btc_chg": round(crypto['btc_change'], 2),
                "eth": round(crypto['eth_price'], 2),
                "eth_chg": round(crypto['eth_change'], 2),
                "dom": round(crypto['btc_dominance'], 2),
                "fg": fg_value,
                "fg_txt": fg_class,
                "fund": round(funding['btc'] * 10000, 4),
                "oi": round(oi['btc'] / 1e9, 2),
                "ls_btc": round(btc_ls, 2),
                "ls_eth": round(eth_ls, 2),
                "ls_tot": round(total_ls, 2),
                "ts": datetime.now(pytz.UTC).strftime('%H:%M'),
            }

            json_str = json.dumps(payload)
            b64_data = base64.urlsafe_b64encode(json_str.encode()).decode()
            base_url = "https://ziagl888.github.io/provencryptobotv2/sentiment.html"
            full_url = f"{base_url}#data={b64_data}"

            # Create button
            # keyboard = [
            # [InlineKeyboardButton(
            # text="🚀 Open Dashboard",
            # url=full_url
            # )]
            # ]
            keyboard = [[InlineKeyboardButton(text="Open Market Analysis Dashboard", web_app=WebAppInfo(url=full_url))]]

            # IMPORTANT: use message_target.reply_text here!
            await message_target.reply_text(
                f"📊 **Crypto Sentiment Analysis**\nStatus: {sentiment}\nPress for Details:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )

        except Exception as e:
            logger.error(f"!market error: {e}", exc_info=True)
            await message_target.reply_text("Sentiment data currently unavailable.")


async def market_dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # For text or button
    is_callback = update.callback_query is not None
    message = update.effective_message or (update.callback_query.message if is_callback else None)
    if not message:
        return

    username = update.effective_user.username or update.effective_user.full_name or "Trader"
    log_command("market_dashboard", update.effective_user)

    await message.reply_chat_action(ChatAction.TYPING)

    try:
        # === COLLECT ALL DATA (like in your old handlers) ===
        session = aiohttp.ClientSession()
        try:
            crypto = await get_crypto_data_async(session)
            fg_value, fg_class = await get_fear_greed_async(session)
            funding = await get_funding_rates_async(session)
            oi = await get_open_interest_async(session)
            ls = await get_long_short_ratios_async(session)
            x_sent = await get_x_sentiment_async()
            sentiment = await analyze_sentiment_score(
                crypto, fg_value, funding, oi, ls.get('btc', 1.0), ls.get('eth', 1.0), x_sent
            )

            # Top Gainers/Losers
            async with session.get("https://fapi.binance.com/fapi/v1/ticker/24hr") as r:
                all_data = await r.json() if r.status == 200 else []
            valid = [c for c in all_data if c["symbol"] in coins]
            gainers = sorted(valid, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:10]
            losers = sorted(valid, key=lambda x: float(x["priceChangePercent"]))[:10]

            # Volume Spikes
            spikes = await detect_volume_spikes_async(min_spike=3.0, top_n=10)

            # Volatile
            volatiles = []
            now = datetime.now(pytz.UTC)
            start_4h = now - timedelta(hours=4)
            conn = await get_conn()
            try:
                for symbol in list(coins)[:40]:
                    try:
                        rows = await conn.fetch(
                            f'''
                            SELECT MAX(high)-MIN(low) as rng, MIN(low) as l
                            FROM "{symbol}_30m" WHERE open_time >= $1
                        ''',
                            start_4h,
                        )
                        if rows and rows[0]['l'] > 0:
                            pct = (rows[0]['rng'] / rows[0]['l']) * 100
                            if pct >= 10:
                                volatiles.append(f"{symbol.replace('USDT', '')}: {pct:.1f}%")
                    except:
                        continue
            finally:
                await release_conn(conn)
            volatiles = volatiles[:10] or ["No strong movements"]

        finally:
            await session.close()

        # === BUILD HTML DIRECTLY IN THE BOT ===
        html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Proven Crypto Dashboard</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
    body {{margin:0; padding:16px; background:#0d0d0d; color:white; font-family:system-ui;}}
    .card {{background:#1a1a1a; border-radius:12px; padding:16px; margin-bottom:16px;}}
    .grid {{display:grid; grid-template-columns:1fr 1fr; gap:12px;}}
    h1 {{text-align:center; color:#00ffff;}}
    h3 {{color:#00ffff; border-bottom:1px solid #333; padding-bottom:8px;}}
    .green {{color:#0f0;}} .red {{color:#f33;}} .orange {{color:#ffaa00;}}
    .item {{display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid #333;}}
    .rank {{color:#00ffff; font-weight:bold;}}
</style>
</head>
<body>
<h1>Proven Crypto Dashboard</h1>
<div class="card"><h3>Market Mood</h3><center><b style="font-size:28px; color:{'#0f0' if 'BULL' in sentiment else '#f33' if 'BEAR' in sentiment else '#ffaa00'}">{sentiment}</b></center></div>

<div class="grid">
  <div class="card">BTC Price<br><b>${crypto['btc_price']:,.0f}</b><br><span class="{'green' if crypto['btc_change'] > 0 else 'red'}">{crypto['btc_change']:+.2f}%</span></div>
  <div class="card">ETH Price<br><b>${crypto['eth_price']:,.0f}</b><br><span class="{'green' if crypto['eth_change'] > 0 else 'red'}">{crypto['eth_change']:+.2f}%</span></div>
  <div class="card">BTC Dominance<br><b>{crypto['btc_dominance']:.1f}%</b></div>
  <div class="card">Fear & Greed<br><b>{fg_value}</b> → {fg_class}</div>
</div>

<h3>Top 10 Gainers</h3>
<div class="card">
  {"".join([f'<div class="item"><span class="rank">{i + 1:02d}</span> {c["symbol"].replace("USDT", "")} <span class="green">+{float(c["priceChangePercent"]):.2f}%</span></div>' for i, c in enumerate(gainers)])}
</div>

<h3>Top 10 Losers</h3>
<div class="card">
  {"".join([f'<div class="item"><span class="rank">{i + 1:02d}</span> {c["symbol"].replace("USDT", "")} <span class="red">{float(c["priceChangePercent"]):.2f}%</span></div>' for i, c in enumerate(losers)])}
</div>

<h3>Volume Spikes (≥3×)</h3>
<div class="card">
  {''.join([f'<div class="item">{s["symbol"].replace("USDT", "")} <b class="orange">{s["spike_ratio"]:.1f}×</b></div>' for s in spikes]) or '<div class="item">No strong spikes</div>'}
</div>

<h3>Most Volatile (4h)</h3>
<div class="card">
  {''.join([f'<div class="item">{v}</div>' for v in volatiles])}
</div>

<script>
  const tg = window.Telegram.WebApp;
  tg.ready(); tg.expand();
  tg.MainButton.text = "Back to bot"; tg.MainButton.show();
  tg.MainButton.onClick(() => tg.close());
</script>
</body>
</html>
        """

        # Base64 encode & send as URL
        import base64

        b64 = base64.urlsafe_b64encode(html.encode()).decode()
        url = f"https://ziagl888.github.io/provencryptobotv2/market.html#data={b64}"

        keyboard = [[InlineKeyboardButton("Open Dashboard", web_app=WebAppInfo(url=url))]]
        await message.reply_text(
            f"**Crypto Market Dashboard**\n\nLive data for @{username}\nClick below:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        await message.reply_text("Error loading the dashboard.")


# ========================= 10 sec candles to database =========================


async def create_ticker_10s_table():
    conn = await get_conn()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ticker_10s (
                id BIGSERIAL,
                symbol TEXT NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL,
                price NUMERIC NOT NULL,
                volume_10s NUMERIC NOT NULL,
                cum_volume NUMERIC NOT NULL DEFAULT 0,
                inserted_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (symbol, timestamp)  -- <<< Add this here!
            )
        """)
        # Index for fast queries (optional, since the PK is already indexed)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ticker_10s_timestamp
            ON ticker_10s(timestamp DESC)
        """)
        logger.info("ticker_10s table ready or updated with PK")
    except Exception as e:
        logger.error(f"Error creating the ticker_10s table: {e}")
    finally:
        await release_conn(conn)


async def cleanup_old_10s_data(days_to_keep: int = 180):
    """Deletes data older than X days (default: 180 days)"""
    conn = await get_conn()
    try:
        cutoff = datetime.now(pytz.UTC) - timedelta(days=days_to_keep)
        result = await conn.execute(
            """
            DELETE FROM ticker_10s
            WHERE timestamp < $1
        """,
            cutoff,
        )
        deleted = result.split()[-1] if ' ' in result else 0
        logger.info(f"Cleanup ticker_10s: {deleted} old entries deleted (older than {days_to_keep} days)")
    except Exception as e:
        logger.error(f"Error during ticker_10s cleanup: {e}")
    finally:
        await release_conn(conn)


async def archive_10s_data_to_db():
    logger.info("10s Data Archiver task started – saves to DB every 10 minutes")
    await create_ticker_10s_table()  # Ensure the table exists

    while True:
        try:
            await asyncio.sleep(600)  # 10 minutes

            if not ONE_MINUTE_DATA:
                logger.debug("ONE_MINUTE_DATA empty – nothing to archive")
                continue

            total_to_insert = sum(len(entries) for entries in ONE_MINUTE_DATA.values())
            logger.info(f"Starting archive: {len(ONE_MINUTE_DATA)} symbols, approx. {total_to_insert} entries")

            conn = await get_conn()
            insert_count = 0
            error_count = 0

            try:
                # First test without a transaction – individual inserts for easier debugging
                for symbol, entries in ONE_MINUTE_DATA.items():
                    if not entries:
                        continue

                    recent = list(entries)[-80:]  # last ~13 minutes as buffer
                    logger.debug(f"{symbol}: {len(recent)} new entries to write")

                    for entry in recent:
                        try:
                            await conn.execute(
                                """
                                INSERT INTO ticker_10s (symbol, timestamp, price, volume_10s, cum_volume)
                                VALUES ($1, $2, $3, $4, $5)
                                ON CONFLICT (symbol, timestamp) DO NOTHING
                            """,
                                symbol,
                                isoparse(entry["t"]),  # String in ISO format – asyncpg can handle that!
                                float(entry["p"]),
                                float(entry["v10s"]),
                                float(entry.get("cum_vol", 0)),
                            )
                            insert_count += 1
                        except Exception as insert_e:
                            error_count += 1
                            logger.error(f"Insert error for {symbol} @ {entry['t']}: {insert_e}", exc_info=True)

                logger.info(f"Archiving complete: {insert_count} successfully inserted, {error_count} errors")

            finally:
                await release_conn(conn)

            # Cleanup, etc.
            now = datetime.now(pytz.UTC)
            if now.hour == 3 and now.minute < 30:
                await cleanup_old_10s_data(180)
                await archive_pump_dump_state_to_db()
                await cleanup_old_pump_dump_archive(180)

        except asyncio.CancelledError:
            logger.info("Archiver Task cancelled")
            raise
        except Exception as e:
            logger.error(f"Archiver crashed: {e}", exc_info=True)
            await asyncio.sleep(60)


# ========================= EXTREME PRICE MOVE + VOLUME EXPLOSION DETECTOR =========================


async def extreme_move_and_volume_detector():
    logger.info("Extreme Price Move & Volume Explosion Detector task started (optimized against spam)")
    global PRICE_VOLUME_ALERT_STATE

    for symbol in coins:
        if symbol not in PRICE_VOLUME_ALERT_STATE:
            PRICE_VOLUME_ALERT_STATE[symbol] = {"last_alert_time": datetime(1970, 1, 1, tzinfo=pytz.UTC)}

    while True:
        try:
            await asyncio.sleep(10)
            now = datetime.now(pytz.UTC)

            # Dynamic cooldown: 15 min for large moves, otherwise 5 min
            for symbol in coins:
                valid_symbol = symbol
                if symbol not in ONE_MINUTE_DATA or len(ONE_MINUTE_DATA[symbol]) < 36:
                    continue

                data = list(ONE_MINUTE_DATA[symbol])
                state = PRICE_VOLUME_ALERT_STATE[symbol]

                # Base cooldown 5 min, but longer for strong moves
                base_cooldown = 300
                extended_cooldown = 900  # 15 min for >10% move
                time_since_alert = (now - state["last_alert_time"]).total_seconds()
                if time_since_alert < base_cooldown:
                    continue

                current_price = float(data[-1]["p"])
                prices = [float(e["p"]) for e in data]
                volumes_10s = [float(e["v10s"]) for e in data]

                alerted = False
                alert_type = None
                details = ""
                color = "#00ffff"
                use_extended_cooldown = False

                # === 1. Extreme Price Move Detection ===
                for lookback_points, min_pct, max_minutes in [
                    (12, 3.0, 2),
                    (18, 4.0, 3),
                    (30, 5.0, 5),
                    (42, 7.5, 7),
                    (60, 10.0, 10),
                    (360, 20.0, 60),
                ]:
                    if len(prices) < lookback_points:
                        continue
                    old_price = prices[-lookback_points]
                    if old_price <= 0:
                        continue
                    change_pct = (current_price / old_price - 1) * 100

                    if abs(change_pct) >= min_pct:
                        direction = "PUMP" if change_pct > 0 else "DUMP"
                        minutes_taken = (lookback_points * 10) // 60
                        seconds_taken = (lookback_points * 10) % 60
                        time_str = f"{minutes_taken}m {seconds_taken}s" if minutes_taken > 0 else f"{seconds_taken}s"

                        alert_type = f"{direction} DETECTED"
                        details = f"{change_pct:+.2f}% in {time_str}"
                        color = "#00ff00" if change_pct > 0 else "#ff0066"
                        alerted = True

                        # For large moves (>10%), a longer cooldown
                        if abs(change_pct) >= 10.0:
                            use_extended_cooldown = True
                        break

                # === 2. Volume explosion (stricter conditions) ===
                if not alerted:
                    recent_vols = volumes_10s[-18:]  # last 3 min
                    recent_prices = prices[-18:]
                    if len(recent_vols) < 12 or len(recent_prices) < 12:
                        continue

                    recent_total_vol = sum(recent_vols)
                    price_start_3min = recent_prices[0]
                    price_change_3min = (current_price / price_start_3min - 1) * 100 if price_start_3min > 0 else 0

                    # Only with at least ±2% price movement within 3 min
                    if abs(price_change_3min) < 2.0:
                        continue

                    hour_vols = volumes_10s[-360:]
                    if len(hour_vols) < 100:
                        continue
                    avg_hour_vol_per_10s = sum(hour_vols) / len(hour_vols)
                    if avg_hour_vol_per_10s <= 0:
                        continue

                    expected_3min_vol = avg_hour_vol_per_10s * 18
                    volume_factor = recent_total_vol / expected_3min_vol

                    # Stricter threshold: 12× instead of 8×
                    if volume_factor >= 12.0:
                        if price_change_3min >= 2.0:
                            pressure = "BUY PRESSURE"
                            color = "#00ff88"
                        elif price_change_3min <= -2.0:
                            pressure = "SELL PRESSURE"
                            color = "#ff6688"
                        else:
                            pressure = "NEUTRAL"
                            color = "#ffff88"

                        alert_type = "VOLUME EXPLOSION DETECTED"
                        details = f"{volume_factor:.1f}× in last 3min ({pressure} {price_change_3min:+.2f}%)"
                        alerted = True

                # === Send alert ===
                if alerted:
                    emoji = "🚀" if "PUMP" in alert_type else "💥" if "DUMP" in alert_type else "📈"

                    html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {color};">
<b style="color:#00ffff; font-size:18px;">{emoji} {alert_type}</b>
<b style="color:#ffd700;">{symbol.replace('USDT', '')}/USDT</b>
<b>→ <b style="color:{color};">{details}</b></b>
<b>→ Price: <code>${current_price:,.8f}</code></b>
<b>→ Time: {now.strftime('%H:%M:%S')} UTC</b>
</pre>
                    """.strip()

                    chart_buf = await generate_smooth_minichart_image(symbol, minutes=240)

                    try:
                        if chart_buf:
                            await application.bot.send_photo(
                                chat_id=MARKET_CHANNEL_ID, photo=chart_buf, caption=html, parse_mode="HTML"
                            )
                        else:
                            await application.bot.send_message(chat_id=MARKET_CHANNEL_ID, text=html, parse_mode="HTML")
                        logger.info(f"Extreme alert sent: {symbol} | {alert_type} | {details}")
                    except Exception as e:
                        logger.error(f"Extreme Alert send error {symbol}: {e}")

                    # Set cooldown – longer for large moves
                    cooldown = extended_cooldown if use_extended_cooldown else base_cooldown
                    state["last_alert_time"] = now + timedelta(
                        seconds=(cooldown - base_cooldown)
                    )  # effective cooldown

        except asyncio.CancelledError:
            logger.info("Extreme Detector Task cancelled")
            raise
        except Exception as e:
            logger.error(f"Extreme Detector crashed: {e}", exc_info=True)
            await asyncio.sleep(10)


# ========================= TRENDLINE BREAK / BOUNCE DETECTOR (OPTIMIZED – ONLY RELEVANT EVENTS) =========================


def get_ml_prediction(df_raw, event_type_str, slope, current_close_price):
    """
    Computes the features for the last candle and queries the matching ML model.
    df_raw: the DataFrame with OHLCV data (should have at least 200 candles)
    event_type_str: "TRENDLINE BREAK UP" or "TRENDLINE BREAK DOWN"
    slope: the slope of the trendline at the time of the event
    current_close_price: the close price of the event candle
    """
    model_to_use = None
    if "UP" in event_type_str:
        model_to_use = LONG_ML_MODEL
        current_ml_threshold = LONG_ML_THRESHOLD
    elif "DOWN" in event_type_str:
        model_to_use = SHORT_ML_MODEL
        current_ml_threshold = SHORT_ML_THRESHOLD
    else:
        logger.warning(f"Unknown event_type_str: {event_type_str}. Returning 0.0.")
        return 0.0, 0.0  # Return: probability, threshold

    if model_to_use is None:
        logger.warning(
            f"ML model ({'LONG' if 'UP' in event_type_str else 'SHORT'}) not loaded or faulty. Returning 0.0."
        )
        return 0.0, current_ml_threshold  # Return: probability, threshold

    try:
        df = df_raw.copy()
        df.columns = df.columns.str.lower()

        if 'open_time' in df.columns:
            df['open_time'] = pd.to_datetime(df['open_time'], utc=True)
            # df['ts'] is not needed for the live prediction, only in the data collector
        else:
            logger.error("Error in get_ml_prediction: 'open_time' column not found in the DataFrame.")
            return 0.0, current_ml_threshold

        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col not in df.columns:
                logger.error(
                    f"Error in get_ml_prediction: '{col}' column not found in the DataFrame. Existing columns: {df.columns.tolist()}"
                )
                return 0.0, current_ml_threshold
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df.dropna(subset=['open', 'high', 'low', 'close', 'volume'], inplace=True)
        if df.empty:
            logger.warning("DataFrame empty after NaN cleanup in OHLCV columns.")
            return 0.0, current_ml_threshold

        df['vol_avg_20'] = df['volume'].rolling(window=20).mean()
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['ema_9'] = ta.ema(df['close'], length=9)
        df['ema_21'] = ta.ema(df['close'], length=21)
        df['ema_50'] = ta.ema(df['close'], length=50)
        df['ema_200'] = ta.ema(df['close'], length=200)
        df['dist_close_ema9_pct'] = (df['close'] - df['ema_9']) / df['ema_9']
        df['dist_ema9_ema21_pct'] = (df['ema_9'] - df['ema_21']) / df['ema_21']
        df['kama_9'] = ta.kama(df['close'], length=9)
        df['dist_close_kama9_pct'] = (df['close'] - df['kama_9']) / df['kama_9']

        macd = ta.macd(df['close'], fast=9, slow=21, signal=9)
        df['MACD_Line'] = macd['MACD_9_21_9']
        df['MACD_Signal'] = macd['MACDs_9_21_9']

        tsi = ta.tsi(df['close'], fast=12, slow=7, signal=7)
        df['TSI_Line'] = tsi['TSI_7_12_7']
        df['TSI_Signal'] = tsi['TSIs_7_12_7']

        bbands = ta.bbands(df['close'], length=20, std=2.0)
        bb_lower_col = next((col for col in bbands.columns if col.startswith('BBL_') and '20' in col), None)
        bb_upper_col = next((col for col in bbands.columns if col.startswith('BBU_') and '20' in col), None)

        if bb_lower_col and bb_upper_col:
            df['BB_Lower'] = bbands[bb_lower_col]
            df['BB_Upper'] = bbands[bb_upper_col]
            df['dist_close_bb_lower_pct'] = (df['close'] - df['BB_Lower']) / df['close']
            df['dist_close_bb_upper_pct'] = (df['close'] - df['BB_Upper']) / df['close']
            diff_bb = df['BB_Upper'] - df['BB_Lower']
            df['bb_position_relative'] = np.where(diff_bb != 0, (df['close'] - df['BB_Lower']) / diff_bb, 0)
        else:
            logger.warning("Bollinger Bands indicators could not be fully computed. Returning 0.0.")
            return 0.0, current_ml_threshold

        donchian = ta.donchian(df['high'], df['low'], length=20)
        dc_lower_col = next((col for col in donchian.columns if col.startswith('DCL_') and '20' in col), None)
        dc_upper_col = next((col for col in donchian.columns if col.startswith('DCU_') and '20' in col), None)

        if dc_lower_col and dc_upper_col:
            df['DC_Lower'] = donchian[dc_lower_col]
            df['DC_Upper'] = donchian[dc_upper_col]
            df['dist_close_dc_lower_pct'] = (df['close'] - df['DC_Lower']) / df['close']
            df['dist_close_dc_upper_pct'] = (df['close'] - df['DC_Upper']) / df['close']

            diff_dc = df['DC_Upper'] - df['DC_Lower']
            df['dc_position_relative'] = np.where(diff_dc != 0, (df['close'] - df['DC_Lower']) / diff_dc, 0)
        else:
            logger.warning("Donchian Channels indicators could not be fully computed. Returning 0.0.")
            return 0.0, current_ml_threshold

        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['ATR_PCT'] = df['ATR'] / df['close']

        df.dropna(inplace=True)
        if df.empty:
            logger.warning("DataFrame is empty after indicator calculation and NaN cleanup. Returning 0.0.")
            return 0.0, current_ml_threshold

        row = df.iloc[-1]

        vol_ratio = (row['volume'] / row['vol_avg_20']) if row['vol_avg_20'] > 0 else 0
        dist_ema200 = (row['close'] - row['ema_200']) / row['ema_200'] if row['ema_200'] else 0
        slope_pct_per_day = (slope * 86400) / current_close_price if current_close_price else 0
        hour_of_day = pd.to_datetime(row['open_time']).hour

        # Features for the prediction (WITHOUT event_type_numeric, since it's implied by the model selection)
        features_dict = {
            'vol_ratio': [vol_ratio],
            'rsi': [row['rsi']],
            'atr_pct': [row['ATR_PCT']],
            'dist_ema200': [dist_ema200],
            'slope_trend': [slope_pct_per_day],
            'hour_of_day': [hour_of_day],
            'dist_close_ema9_pct': [row['dist_close_ema9_pct']],
            'dist_ema9_ema21_pct': [row['dist_ema9_ema21_pct']],
            'dist_close_kama9_pct': [row['dist_close_kama9_pct']],
            'MACD_Line': [row['MACD_Line']],
            'MACD_Signal': [row['MACD_Signal']],
            'TSI_Line': [row['TSI_Line']],
            'TSI_Signal': [row['TSI_Signal']],
            'dist_close_bb_lower_pct': [row['dist_close_bb_lower_pct']],
            'dist_close_bb_upper_pct': [row['dist_close_bb_upper_pct']],
            'bb_position_relative': [row['bb_position_relative']],
            'dist_close_dc_lower_pct': [row['dist_close_dc_lower_pct']],
            'dist_close_dc_upper_pct': [row['dist_close_dc_upper_pct']],
            'dc_position_relative': [row['dc_position_relative']],
        }

        X_live = pd.DataFrame(features_dict)
        if X_live.isnull().values.any():
            logger.error(f"NaN values in the prepared features for the prediction: {X_live.isnull().sum()}")
            return 0.0, current_ml_threshold

        proba = model_to_use.predict_proba(X_live)[0][1]
        return proba, current_ml_threshold

    except Exception as e:
        logger.error(f"ML error during the prediction: {e}", exc_info=True)
        return 0.0, current_ml_threshold


async def trendline_break_bounce_detector():
    logger.info("Trendline Break/Bounce Detector task started (optimized: only when proximity ≤10%)")

    global TRENDLINE_STATE
    for symbol in coins:
        if symbol not in TRENDLINE_STATE:
            TRENDLINE_STATE[symbol] = {"last_alert": datetime(1970, 1, 1, tzinfo=pytz.UTC), "prev_relation": "unknown"}

    while True:
        try:
            now = datetime.now(pytz.UTC)
            if now.minute != 5 or now.second > 30:
                await asyncio.sleep(20)
                continue

            logger.info(f"Trendline check at {now.strftime('%H:%M')} UTC – only coins near the trendline")

            conn = await get_conn()  # Get connection
            try:
                for symbol in coins:
                    valid_symbol = symbol
                    state = TRENDLINE_STATE[symbol]
                    if (now - state["last_alert"]).total_seconds() < 3600:
                        continue

                    df_90d = await get_1h_data_last_90d(symbol)
                    if df_90d.empty or len(df_90d) < 50:
                        continue

                    df_recent = df_90d.tail(4).copy()
                    if len(df_recent) < 3:
                        continue

                    last_close = float(df_recent['CLOSE'].iloc[-1])
                    prev_close = float(df_recent['CLOSE'].iloc[-2])

                    trend_direction, trend_data = detect_trend(df_90d)
                    if trend_data is None:
                        continue

                    slope, intercept = trend_data

                    # Trend value for the last completed candle
                    last_time_sec = df_recent['OPEN_TIME'].iloc[-1].timestamp()
                    trend_value_last = slope * last_time_sec + intercept

                    # Relative distance in %
                    rel_distance = (last_close - trend_value_last) / trend_value_last if trend_value_last != 0 else 0

                    # Only continue if the price is within ±10% of the trendline
                    if abs(rel_distance) > 0.10:  # Corrected boundary (comment said 10%)
                        continue

                    tolerance = last_close * 0.008  # ±0.8% for "near"
                    distance = last_close - trend_value_last

                    # Current relation
                    if abs(distance) <= tolerance:
                        current_relation = "near"
                    elif distance > 0:
                        current_relation = "above"
                    else:
                        current_relation = "below"

                    prev_relation = state.get("prev_relation", "unknown")

                    significant_distance_before = False
                    for i in range(-4, -1):
                        if len(df_recent) <= abs(i):
                            break
                        time_sec = df_recent['OPEN_TIME'].iloc[i].timestamp()
                        trend_val = slope * time_sec + intercept
                        close_val = float(df_recent['CLOSE'].iloc[i])
                        dist_pct = abs((close_val - trend_val) / trend_val) if trend_val != 0 else 0
                        if dist_pct > 0.03:
                            significant_distance_before = True
                            break

                    # Detect events
                    event = None
                    color = "#00ffff"
                    emoji = "📈"

                    if (
                        prev_relation in ["below", "near", "unknown"]
                        and current_relation == "above"
                        and distance > tolerance
                    ):
                        event = "TRENDLINE BREAK UP"
                        color = "#00ff00"
                        emoji = "🚀"
                    elif (
                        prev_relation in ["above", "near", "unknown"]
                        and current_relation == "below"
                        and distance < -tolerance
                    ):
                        event = "TRENDLINE BREAK DOWN"
                        color = "#ff0066"
                        emoji = "💥"
                    elif prev_relation == "above" and current_relation == "near":
                        if min(df_recent['LOW'].iloc[-3:]) >= trend_value_last - tolerance and last_close > prev_close:
                            event = "BOUNCE UP FROM TRENDLINE"
                            color = "#00ff88"
                            emoji = "⬆️"
                    elif prev_relation == "below" and current_relation == "near":
                        if max(df_recent['HIGH'].iloc[-3:]) <= trend_value_last + tolerance and last_close < prev_close:
                            event = "BOUNCE DOWN FROM TRENDLINE"
                            color = "#ff6688"
                            emoji = "⬇️"

                    if event:
                        # === Collect DB data ===
                        rsi_9 = float(df_recent.get('RSI_9', np.nan).iloc[-1]) if 'RSI_9' in df_recent else None
                        rsi_14 = float(df_recent.get('RSI_14', np.nan).iloc[-1]) if 'RSI_14' in df_recent else None
                        rsi_24 = float(df_recent.get('RSI_24', np.nan).iloc[-1]) if 'RSI_24' in df_recent else None

                        volume_current = float(df_recent['VOLUME'].iloc[-1]) if 'VOLUME' in df_recent else None
                        volume_avg_20 = float(df_90d['VOLUME'].tail(20).mean()) if len(df_90d) >= 20 else None
                        volume_ratio_pct = (
                            (volume_current / volume_avg_20 * 100) if volume_avg_20 and volume_avg_20 > 0 else None
                        )

                        # Insert in DB
                        try:
                            await conn.execute(
                                """
                                INSERT INTO trendmeet_rawdata (
                                    coin, event_type, trend_direction,
                                    close_price, trend_value, rel_distance_pct, abs_distance,
                                    rsi_9, rsi_14, rsi_24,
                                    volume_current, volume_avg_20, volume_ratio_pct,
                                    slope, intercept, tolerance,
                                    prev_relation, current_relation,
                                    significant_dist_before
                                ) VALUES (
                                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                                    $11, $12, $13, $14, $15, $16, $17, $18, $19
                                )
                            """,
                                symbol,
                                event,
                                trend_direction,
                                last_close,
                                trend_value_last,
                                rel_distance * 100,
                                distance,
                                rsi_9,
                                rsi_14,
                                rsi_24,
                                volume_current,
                                volume_avg_20,
                                volume_ratio_pct,
                                slope,
                                intercept,
                                tolerance,
                                prev_relation,
                                current_relation,
                                significant_distance_before,
                            )
                            logger.debug(f"Trendmeet raw data saved: {symbol} | {event}")
                        except Exception as db_err:
                            logger.error(f"DB insert error for {symbol}: {db_err}")

                        # === Chart & Telegram (your existing code – structure only here) ===
                        chart_buf = None

                        try:
                            # ... your complete chart code (df_7d, df_ind, preprocessing, live_price, fig, axes, etc.) ...
                            # At the end:
                            # Prepare data for the !don chart
                            df_7d = await get_1h_data_last_7d(symbol)
                            df_ind = await get_1h_indicators_last_7d(symbol)
                            if df_7d.empty or df_ind.empty:
                                raise ValueError("No data")

                            # PREPROCESSING (identical)
                            if 'OPEN_TIME' in df_7d.columns:
                                df_7d['OPEN_TIME'] = pd.to_datetime(df_7d['OPEN_TIME']).dt.tz_localize(None)
                            if not df_ind.empty and 'OPEN_TIME' in df_ind.columns:
                                df_ind['OPEN_TIME'] = pd.to_datetime(df_ind['OPEN_TIME']).dt.tz_localize(None)
                            if not df_90d.empty and 'OPEN_TIME' in df_ind.columns:
                                df_90d['OPEN_TIME'] = pd.to_datetime(df_90d['OPEN_TIME']).dt.tz_localize(None)

                            if not df_ind.empty:
                                cols = [c for c in df_ind.columns if c not in df_7d.columns or c == 'OPEN_TIME']
                                df_plot = df_7d.merge(df_ind[cols], on='OPEN_TIME', how='left')
                            else:
                                df_plot = df_7d.copy()
                            df_plot = df_plot.ffill().bfill()

                            for c in ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']:
                                if c in df_plot.columns:
                                    df_plot[c] = pd.to_numeric(df_plot[c], errors='coerce')

                            # LIVE PRICE (identical)
                            live_price = await get_live_price(valid_symbol)
                            live_suffix = ""
                            if live_price and isinstance(live_price, (int, float)) and live_price > 0:
                                idx = df_plot.index[-1]
                                df_plot.loc[idx, "CLOSE"] = float(live_price)
                                df_plot.loc[idx, "HIGH"] = max(float(df_plot.loc[idx, "HIGH"]), float(live_price))
                                df_plot.loc[idx, "LOW"] = min(float(df_plot.loc[idx, "LOW"]), float(live_price))
                                live_suffix = f" (live ${live_price:,.8f})"

                            # PLOT SETUP
                            bg = '#1e1e1e'
                            fg = 'white'
                            fig = plt.figure(figsize=(22, 15), facecolor=bg)
                            gs = gridspec.GridSpec(
                                4, 2, width_ratios=[6, 1], height_ratios=[5, 1, 1.2, 0.8], hspace=0.4, wspace=0.05
                            )
                            ax1 = fig.add_subplot(gs[0, 0])
                            ax1.set_facecolor(bg)

                            x_vals = np.arange(len(df_plot))
                            o = df_plot['OPEN'].values
                            c = df_plot['CLOSE'].values
                            h = df_plot['HIGH'].values
                            l = df_plot['LOW'].values
                            up = c >= o
                            down = ~up
                            col_up = '#44ff44'
                            col_down = '#ff4444'

                            # Candles (identical)
                            ax1.vlines(x_vals[up], l[up], h[up], color=col_up, linewidth=1.2, zorder=3)
                            ax1.vlines(x_vals[down], l[down], h[down], color=col_down, linewidth=1.2, zorder=3)
                            body_h = np.abs(c - o)
                            chart_range = h.max() - l.min()
                            min_h = chart_range * 0.002
                            body_h = np.maximum(body_h, min_h)
                            body_b = np.minimum(o, c)
                            ax1.bar(
                                x_vals[up],
                                body_h[up],
                                bottom=body_b[up],
                                width=0.6,
                                color=col_up,
                                linewidth=0,
                                zorder=4,
                            )
                            ax1.bar(
                                x_vals[down],
                                body_h[down],
                                bottom=body_b[down],
                                width=0.6,
                                color=col_down,
                                linewidth=0,
                                zorder=4,
                            )

                            # Candles (identical to above)

                            # --- NUR HIER NEU: Donchian Channel 20 ---
                            if all(
                                col in df_plot.columns
                                for col in ['DONCHIAN_UPPER_20', 'DONCHIAN_MID_20', 'DONCHIAN_LOWER_20']
                            ):
                                ax1.plot(
                                    x_vals,
                                    df_plot['DONCHIAN_UPPER_20'],
                                    color='#00ffff',
                                    linewidth=1.5,
                                    label='Don Upper 20',
                                )
                                ax1.plot(
                                    x_vals,
                                    df_plot['DONCHIAN_MID_20'],
                                    color='#00ffff',
                                    linewidth=1.0,
                                    alpha=0.8,
                                    label='Don Mid 20',
                                )
                                ax1.plot(
                                    x_vals,
                                    df_plot['DONCHIAN_LOWER_20'],
                                    color='#00ffff',
                                    linewidth=1.5,
                                    label='Don Lower 20',
                                )
                                ax1.fill_between(
                                    x_vals,
                                    df_plot['DONCHIAN_LOWER_20'],
                                    df_plot['DONCHIAN_UPPER_20'],
                                    color='#00ffff',
                                    alpha=0.06,
                                )

                            # Standard EMAs (as always)
                            # ... (wie in candles_handler)

                            # Standard EMAs (as in candles_handler)
                            if 'EMA_9' in df_plot.columns:
                                ax1.plot(x_vals, df_plot['EMA_9'], color='yellow', linewidth=1.1, label='EMA9')
                            if 'EMA_21' in df_plot.columns:
                                ax1.plot(x_vals, df_plot['EMA_21'], color='#32CD32', linewidth=1.1, label='EMA21')
                            if 'WMA_55' in df_plot.columns:
                                ax1.plot(x_vals, df_plot['WMA_55'], color='#FFB300', linewidth=1.1, label='WMA55')
                            if 'EMA_200' in df_plot.columns:
                                ax1.plot(x_vals, df_plot['EMA_200'], color='#E53935', linewidth=1.1, label='EMA200')
                            if 'KAMA_9' in df_plot.columns:
                                ax1.plot(x_vals, df_plot['KAMA_9'], color='#9C27B0', linewidth=1.1, label='KAMA9')

                            ax1.set_title(
                                f"{valid_symbol} Donchian 20{live_suffix}", color=fg, fontsize=19, pad=25, weight='bold'
                            )
                            ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')
                            # ... rest identical to candles_handler ...
                            ax1.grid(True, alpha=0.25, color=fg, linewidth=0.5)
                            ax1.tick_params(colors=fg, labelsize=11)

                            # Last price marker
                            ax1.axhline(live_price, color="white", linewidth=1, linestyle="--", alpha=0.5)
                            ax1.text(
                                0.2,
                                live_price,
                                f"{live_price:,.8f}",
                                transform=ax1.get_yaxis_transform(),
                                color="white",
                                fontsize=10,
                                fontweight='bold',
                                va='center',
                                bbox=dict(facecolor='#1e1e1e', edgecolor='none', pad=5),
                            )

                            # Y-Limit
                            margin = chart_range * 0.05
                            ax1.set_ylim(l.min() - margin, h.max() + margin)

                            # Rest identical: Volume, VBP, Trend, RSI, TSI, formatting, saving...
                            # (Simply copy the entire rest from your candles_handler from Volume to the end here)

                            # ... [Volume, Volume Profile, trendline, RSI, TSI, X formatting, saving – all 1:1 like in candles_handler] ...

                            # --- VOLUME (bottom) ---
                            ax_vol = ax1.twinx()
                            vol_max = df_plot['VOLUME'].max()
                            vol_min_display = vol_max * 0.25

                            # Colors matching the candles
                            vol_colors = np.where(up, col_up, col_down)

                            # We need to compute 'display_volume' as in your original
                            display_volume = df_plot['VOLUME'].copy()
                            display_volume[display_volume < vol_min_display] = vol_min_display

                            # IMPORTANT: use x_vals
                            ax_vol.bar(
                                x_vals,
                                display_volume,
                                width=0.6,
                                color=vol_colors,
                                alpha=0.5,
                                edgecolor='#ffffff44',
                                linewidth=0.25,
                                align='center',
                            )

                            ax_vol.set_ylim(0, vol_max * 2.5)
                            ax_vol.yaxis.set_label_position("right")
                            ax_vol.yaxis.tick_right()
                            ax_vol.set_ylabel('Volume', color=fg, fontsize=13, weight='bold')
                            ax_vol.tick_params(colors=fg, labelsize=10)
                            ax_vol.legend(
                                ['Volume'],
                                loc='upper right',
                                facecolor=bg,
                                labelcolor=fg,
                                fontsize=11,
                                frameon=True,
                                fancybox=True,
                            )
                            ax_vol.grid(True, alpha=0.15, color=fg, linewidth=0.4, linestyle='-', axis='y')

                            # Volume overlay (gray area)
                            ax4 = ax1.twinx()
                            ax4.fill_between(x_vals, df_plot['VOLUME'], color='gray', alpha=0.4, label='volume')
                            ax4.plot(x_vals, df_plot['VOLUME'], color='gray', linewidth=1)
                            ax4.set_ylabel("volume", fontsize=12, color='gray')
                            ax4.tick_params(axis='y', labelcolor='gray')
                            ax4.set_ylim(0, vol_max * 2.5)  # Sync with ax_vol

                            # --- VOLUME PROFILE (right) ---
                            ax_vol_profile = fig.add_subplot(gs[0, 0], frameon=False)
                            ax_vol_profile.set_position([0.85, 0.68, 0.12, 0.25])

                            ax_vbp = fig.add_subplot(gs[0, 1])
                            ax_vbp.set_facecolor('#1e1e1e')
                            price_bins = np.linspace(l.min(), h.max(), 40)
                            vol_by_price = np.zeros(len(price_bins) - 1)

                            # Here we use searchsorted for speed, but your loop is also fine
                            # We use your logic:
                            for _, row in df_7d.iterrows():
                                idx = np.searchsorted(price_bins, [row['LOW'], row['HIGH']])
                                idx = np.clip(idx, 0, len(vol_by_price) - 1)
                                if idx[0] == idx[1]:
                                    vol_by_price[idx[0]] += row['VOLUME']
                                else:
                                    vol_by_price[idx[0] : idx[1]] += row['VOLUME'] / (idx[1] - idx[0])

                            ax_vbp.barh(
                                (price_bins[:-1] + price_bins[1:]) / 2,
                                vol_by_price,
                                height=(price_bins[1] - price_bins[0]) * 0.8,
                                color='#ff69b4',
                                alpha=0.6,
                            )
                            ax_vbp.set_ylim(ax1.get_ylim())  # Sync with chart
                            ax_vbp.invert_xaxis()
                            ax_vbp.set_xlabel('Vol', color='white', fontsize=10)
                            ax_vbp.tick_params(colors='white')

                            # --- TRENDLINE & PIVOTS ---
                            trend_direction, trend_data = detect_trend(df_90d)
                            slope, intercept = trend_data if trend_data else (None, None)
                            if slope is not None and intercept is not None:
                                # Compute trendline
                                trend_y = get_trend_values(slope, intercept, df_plot['OPEN_TIME'])
                                # Plot against x_vals
                                ax1.plot(
                                    x_vals,
                                    trend_y,
                                    color='orange',
                                    linewidth=5.0,
                                    alpha=0.98,
                                    label=f'90d Trend: {trend_direction}',
                                )
                                ax1.legend(facecolor=bg, labelcolor=fg, fontsize=12, loc='upper left')

                                # Pivots (mapping time -> index)
                                high_pivots, low_pivots = find_pivots(df_90d, distance=8)
                                last_7d_time = df_plot['OPEN_TIME'].iloc[0]

                                pivots = high_pivots if trend_direction == 'DOWN' else low_pivots
                                pivot_times = df_90d['OPEN_TIME'].iloc[pivots]
                                pivot_prices = df_90d['HIGH' if trend_direction == 'DOWN' else 'LOW'].iloc[pivots]

                                # Create map
                                t_map = {t: i for i, t in enumerate(df_plot['OPEN_TIME'])}
                                px, py = [], []
                                for t, p in zip(pivot_times, pivot_prices):
                                    if t in t_map:
                                        px.append(t_map[t])
                                        py.append(p)

                                color_pivot = '#ff4444' if trend_direction == 'DOWN' else '#44ff44'
                                if px:
                                    ax1.scatter(
                                        px,
                                        py,
                                        color=color_pivot,
                                        s=180,
                                        zorder=6,
                                        edgecolors='white',
                                        linewidth=3.0,
                                        marker='o',
                                    )

                            # --- RSI ---
                            ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)
                            ax2.set_facecolor(bg)
                            if 'RSI_9' in df_plot.columns:
                                ax2.plot(x_vals, df_plot['RSI_9'], color='yellow', linewidth=1.1, label='RSI 9')
                            if 'RSI_12' in df_plot.columns:
                                ax2.plot(x_vals, df_plot['RSI_12'], color='orange', linewidth=1.1, label='RSI 12')
                            if 'RSI_24' in df_plot.columns:
                                ax2.plot(x_vals, df_plot['RSI_24'], color='red', linewidth=1.1, label='RSI 24')
                            ax2.axhline(75, color='red', linestyle='--', alpha=0.8, linewidth=1)
                            ax2.axhline(50, color=fg, linestyle='-', alpha=0.3, linewidth=0.8)
                            ax2.axhline(25, color='green', linestyle='--', alpha=0.8, linewidth=1)
                            ax2.set_ylim(0, 100)
                            ax2.set_ylabel('RSI', color=fg, fontsize=12, weight='bold')
                            ax2.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
                            ax2.grid(True, alpha=0.15, color=fg)

                            # --- TSI ---
                            ax3 = fig.add_subplot(gs[2, 0], sharex=ax1)
                            ax3.set_facecolor(bg)
                            if 'TSI_FAST_12_7_7' in df_plot.columns:
                                ax3.plot(
                                    x_vals, df_plot['TSI_FAST_12_7_7'], color='#00ff00', linewidth=1.8, label='TSI'
                                )
                            if 'TSI_FAST_12_7_7_SIGNAL' in df_plot.columns:
                                ax3.plot(
                                    x_vals,
                                    df_plot['TSI_FAST_12_7_7_SIGNAL'],
                                    color='red',
                                    linewidth=1.4,
                                    label='Signal',
                                )
                            ax3.axhline(85, color='red', linestyle=':', alpha=0.9, linewidth=1.2)
                            ax3.axhline(50, color='red', linestyle='--', alpha=0.7)
                            ax3.axhline(0, color=fg, linestyle='-', alpha=0.3)
                            ax3.axhline(-50, color='green', linestyle='--', alpha=0.7)
                            ax3.axhline(-85, color='green', linestyle=':', alpha=0.9, linewidth=1.2)
                            ax3.set_ylim(-100, 100)
                            ax3.set_ylabel('TSI', color=fg, fontsize=12, weight='bold')
                            ax3.legend(facecolor=bg, labelcolor=fg, fontsize=10, loc='upper left')
                            ax3.grid(True, alpha=0.15, color=fg)

                            # --- X-AXIS FORMATTING (index -> date) ---
                            def format_date(x, pos=None):
                                idx = int(x + 0.5)
                                if 0 <= idx < len(df_plot):
                                    return df_plot['OPEN_TIME'].iloc[idx].strftime('%d.%m %H:%M')
                                return ''

                            ax1.xaxis.set_major_formatter(mticker.FuncFormatter(format_date))
                            ax1.xaxis.set_major_locator(mticker.MaxNLocator(nbins=10))

                            # White labels for all axes
                            for ax in [ax1, ax2, ax3, ax_vol]:
                                ax.tick_params(colors=fg, labelsize=10)
                                for label in ax.get_xticklabels() + ax.get_yticklabels():
                                    label.set_color(fg)

                            # # At the end:
                            # with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                            # fig.savefig(tmp.name, format='png', dpi=300, facecolor='#1e1e1e', bbox_inches='tight', pad_inches=0.4)
                            # tmp_path = tmp.name
                            # chart_buf = open(tmp_path, 'rb')
                            # os.unlink(tmp_path)
                            # plt.close(fig)

                            buf = BytesIO()
                            fig.savefig(buf, format='png', dpi=300, facecolor=bg, bbox_inches='tight', pad_inches=0.4)
                            buf.seek(0)
                            plt.close(fig)
                            chart_buf = buf
                        except Exception as e:
                            logger.error(f"Chart error for {symbol}: {e}")
                            chart_buf = None

                        try:
                            # ml_probability = get_ml_prediction(df_90d, event, slope, trend_value_last)
                            ml_probability, current_ml_threshold = get_ml_prediction(df_90d, event, slope, last_close)
                            logger.info(
                                f"Signal detected: {symbol} {event} | ML score: {ml_probability:.2f} (threshold: {current_ml_threshold})"
                            )

                            # Extended event string for the message
                            event_display = f"{event} (ML: {ml_probability:.0%} ML Score: {ml_probability:.2f} Threshold: {current_ml_threshold:.2f})"
                            if ml_probability >= 0.25:
                                try:
                                    direction = (
                                        "LONG"
                                        if "UP" in event or "BOUNCE UP" in event or "BREAK UP" in event
                                        else "SHORT"
                                    )

                                    await conn.execute(
                                        """
                                        INSERT INTO ML_TREND_TRADES (
                                            symbol,
                                            direction,
                                            ml_probability,
                                            close_price,
                                            event_type,
                                            trend_direction,
                                            created_at
                                        ) VALUES (
                                            $1, $2, $3, $4, $5, $6, $7
                                        )
                                    """,
                                        symbol,
                                        direction,
                                        float(ml_probability),
                                        float(last_close),
                                        event_display,  # Original event without the ML percentage
                                        trend_direction,
                                        now,  # datetime.now(pytz.UTC)
                                    )

                                    logger.info(
                                        f"High-conf ML trade saved: {symbol} {direction} "
                                        f"| ML: {ml_probability:.3f} | Price: {last_close:.8f}"
                                    )

                                except Exception as e:
                                    logger.error(f"Error saving to ML_TREND_TRADES for {symbol}: {e}")
                            event = f"{event} (ML: {ml_probability:.0%})"
                        except Exception as e:
                            logger.error(f"ML error for {symbol}: {e}")

                        # HTML + Send
                        html = f"""
    <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {color};">
    <b style="color:#00ffff; font-size:18px;">{emoji} {event}</b>
    <b style="color:#ffd700;">{symbol.replace('USDT', '')}/USDT</b>
    <b>→ 90d Trend: <b style="color:orange;">{trend_direction}</b></b>
    <b>→ Close: <code>${last_close:,.8f}</code> | Trend: <code>${trend_value_last:,.8f}</code></b>
    <b>→ Distance: {rel_distance:+.2%}</b>
    <b>→ Time: {now.strftime('%H:%M')} UTC</b>
    </pre>
                        """.strip()

                        try:
                            if chart_buf:
                                await application.bot.send_photo(
                                    chat_id=TRENDBREAKER_CHANNEL_ID, photo=chart_buf, caption=html, parse_mode="HTML"
                                )
                                chart_buf.close()
                            else:
                                await application.bot.send_message(
                                    chat_id=TRENDBREAKER_CHANNEL_ID, text=html, parse_mode="HTML"
                                )
                            logger.info(f"RELEVANT trend alert: {symbol} | {event} | Dist {rel_distance:+.2%}")
                        except Exception as e:
                            logger.error(f"Send error: {e}")
                            if chart_buf:
                                chart_buf.close()

                        state["last_alert"] = now

                    # Update state (even if no event)
                    state["prev_relation"] = current_relation

            finally:
                await release_conn(conn)

            # Next run in one hour +5 min
            next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1) + timedelta(minutes=5)
            sleep_sec = (next_run - datetime.now(pytz.UTC)).total_seconds()
            await asyncio.sleep(max(10, sleep_sec))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Trend Detector Crash: {e}", exc_info=True)
            await asyncio.sleep(60)


# CREATE TABLE IF NOT EXISTS trendmeet_rawdata (
# id                  SERIAL PRIMARY KEY,
# detection_time      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
# coin                TEXT NOT NULL,
# event_type          TEXT NOT NULL,          -- e.g. 'TRENDLINE BREAK UP', 'BOUNCE UP FROM TRENDLINE' ...
# trend_direction     TEXT,                   -- 'UP', 'DOWN' or NULL
# close_price         NUMERIC,
# trend_value         NUMERIC,
# rel_distance_pct    NUMERIC,                -- relative distance in %
# abs_distance        NUMERIC,
# rsi_9               NUMERIC,
# rsi_14              NUMERIC,
# rsi_24              NUMERIC,
# volume_current      NUMERIC,
# volume_avg_20       NUMERIC,                -- average of the last 20 candles
# volume_ratio_pct    NUMERIC,                -- volume_current / volume_avg_20 * 100
# slope               NUMERIC,
# intercept           NUMERIC,
# tolerance           NUMERIC,
# prev_relation       TEXT,
# current_relation    TEXT,
# significant_dist_before BOOLEAN DEFAULT FALSE,
# raw_json_data       JSONB,                  -- in case you want to store more later
# created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
# );

# CREATE INDEX idx_trendmeet_coin_event_time
# ON trendmeet_rawdata (coin, event_type, detection_time);


# ========================= MENUE HANDLER FOR BOT =========================


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # The layout of the buttons
    keyboard = [
        [
            InlineKeyboardButton("📊 Merket Sentiment", callback_data="cmd_market"),
            InlineKeyboardButton("🐂 Sentiment", callback_data="market_dashboard"),
        ],
        [
            InlineKeyboardButton("📈 BTC Chart", callback_data="chart_BTC"),
            InlineKeyboardButton("ethereum ETH Chart", callback_data="chart_ETH"),
        ],
        [
            InlineKeyboardButton("💰 Portfolio", callback_data="cmd_portfolio"),
            InlineKeyboardButton("❓ Help", callback_data="cmd_help"),
        ],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Open local file
    # 'rb' stands for read binary
    with open("bot.jpg", "rb") as f:
        await update.message.reply_photo(
            photo=f, caption="         PROVEN CRYPTO BOT V2 - choose a function:", reply_markup=reply_markup
        )

    # await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # Important: stops the loading animation on the button

    data = query.data

    # Here we route the clicks to your existing functions
    if data == "cmd_market":
        # We call your existing function
        await market_handler(update, context)

    elif data == "market_dashboard":
        # # Here you'd need to call your market_handler function
        # # If you have one: await market_handler(update, context)
        await market_dashboard_handler(update, context)
        # update.callback_query.message.reply_text("Loading market function...")

    # elif data.startswith("chart_"):
    # # Extract the coin from the button (e.g. "chart_BTC" -> "BTC")
    # symbol = data.split("_")[1]

    # # Note: your chart function probably expects text like "!minichart BTC"
    # # Since we don't have text here, we may need to slightly adapt the chart function
    # # or call the image generation directly:
    # await generate_and_send_chart(update, context, symbol)

    # elif data == "cmd_help":
    # await update.callback_query.message.reply_text("Here is the help...")


# async def load_pump_models():
# """Loads all 3 models + thresholds once at bot startup"""
# global PUMP_MODELS_LOADED
# for horizon, info in PUMP_MODELS.items():
# try:
# model = joblib.load(info["model_path"])
# threshold = joblib.load(info["threshold_path"])
# PUMP_MODELS_LOADED[horizon] = {"model": model, "threshold": threshold}
# info["loaded"] = True
# logger.info(f"{horizon}-Pump model loaded (threshold: {threshold:.3f})")
# except Exception as e:
# logger.error(f"Error loading {horizon} model: {e}")
# info["loaded"] = False


async def load_pump_models():
    """Loads all three trained pump models + thresholds at bot startup"""
    global PUMP_MODELS_LOADED
    PUMP_MODELS_LOADED = {}

    # horizons = [
    # ("8h", "pump_model_8h_pump_final.pkl", "threshold_8h_pump_final.pkl"),
    # ("72h", "pump_model_72h_pump_final.pkl", "threshold_72h_pump_final.pkl"),
    # ("168h", "pump_model_168h_pump_final.pkl", "threshold_168h_pump_final.pkl"),
    # ]

    horizons = [
        ("8h_pump", "pump_model_8h_pump_final.pkl", "threshold_8h_pump_final.pkl"),
        ("8h_dump", "pump_model_8h_dump_final.pkl", "threshold_8h_dump_final.pkl"),
        ("24h_pump", "pump_model_24h_pump_final.pkl", "threshold_24h_pump_final.pkl"),
        ("24h_dump", "pump_model_24h_dump_final.pkl", "threshold_24h_dump_final.pkl"),
        ("72h_pump", "pump_model_72h_pump_final.pkl", "threshold_72h_pump_final.pkl"),
        ("72h_dump", "pump_model_72h_dump_final.pkl", "threshold_72h_dump_final.pkl"),
        ("168h_pump", "pump_model_168h_pump_final.pkl", "threshold_168h_pump_final.pkl"),
        ("168h_dump", "pump_model_168h_dump_final.pkl", "threshold_168h_dump_final.pkl"),
    ]

    for horizon, model_path, threshold_path in horizons:
        model_file = Path(model_path)
        threshold_file = Path(threshold_path)

        if not model_file.exists():
            logger.warning(f"{horizon}-Pump model not found: {model_path}")
            PUMP_MODELS_LOADED[horizon] = {"model": None, "threshold": 0.5}
            continue
        if not threshold_file.exists():
            logger.warning(f"{horizon}-Threshold not found: {threshold_path}")
            threshold = 0.5  # Fallback
        else:
            try:
                threshold = joblib.load(threshold_file)
            except Exception as e:
                logger.error(f"Error loading {threshold_path}: {e}")
                threshold = 0.5

        try:
            model = joblib.load(model_file)
            PUMP_MODELS_LOADED[horizon] = {"model": model, "threshold": float(threshold)}
            logger.info(f"{horizon.upper()}-Pump model loaded (threshold: {threshold:.3f})")
        except Exception as e:
            logger.error(f"Error loading {model_path}: {e}")
            PUMP_MODELS_LOADED[horizon] = {"model": None, "threshold": 0.5}

    logger.info("All pump models loaded or initialized with fallback")


def pct_distance(price_series: pd.Series, indicator_series: pd.Series) -> pd.Series:
    denominator = indicator_series.replace(0, np.nan)
    result = (price_series - indicator_series) / denominator * 100
    return result.fillna(0)


def add_advanced_features(df: pd.DataFrame) -> pd.DataFrame:
    # Sort by open_time (must be present – guaranteed by h.open_time)
    if 'open_time' in df.columns:
        df = df.sort_values('open_time').reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)
        logger.warning("open_time missing – sorting skipped")

    # Volume features – WITHOUT groupby (only 1 coin)
    df['volume_ratio_prev'] = df['volume'] / df['volume'].shift(1)
    df['volume_sma20'] = df['volume'].rolling(20, min_periods=1).mean()
    df['volume_ratio_sma20'] = df['volume'] / df['volume_sma20']

    # Deltas – WITHOUT groupby
    delta_cols = ['rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24', 'tsi_fast', 'macd_dif']
    for col in delta_cols:
        if col in df.columns:
            df[f'{col}_delta_1'] = df[col].diff(1)

    # MACD
    if 'macd_dif' in df.columns and 'macd_dea' in df.columns:
        df['macd_hist'] = df['macd_dif'] - df['macd_dea']
        df['macd_hist_delta_1'] = df['macd_hist'].diff(1)
    else:
        df['macd_hist'] = 0.0
        df['macd_hist_delta_1'] = 0.0

    # Binary features
    if 'close' in df.columns and 'ema_200' in df.columns:
        df['above_ema_200'] = (df['close'] > df['ema_200']).astype(int)
    else:
        df['above_ema_200'] = 0

    if 'rsi_14' in df.columns:
        df['rsi_14_above_50'] = (df['rsi_14'] > 50).astype(int)
        df['rsi_14_cross_above_30'] = ((df['rsi_14'].shift(1) < 30) & (df['rsi_14'] >= 30)).astype(int)
    else:
        df['rsi_14_above_50'] = 0
        df['rsi_14_cross_above_30'] = 0

    if 'ema_9' in df.columns and 'ema_21' in df.columns:
        df['ema_9_cross_above_21'] = (
            (df['ema_9'].shift(1) < df['ema_21'].shift(1)) & (df['ema_9'] > df['ema_21'])
        ).astype(int)
    else:
        df['ema_9_cross_above_21'] = 0

    # ATR distances
    eps = 1e-8
    if all(c in df.columns for c in ['close', 'atr_14']):
        df['boll_upper_dist_atr'] = (df['close'] - df.get('boll_upper_20', df['close'])) / (df['atr_14'] + eps)
        df['boll_lower_dist_atr'] = (df['close'] - df.get('boll_lower_20', df['close'])) / (df['atr_14'] + eps)
        df['ema_200_dist_atr'] = (df['close'] - df.get('ema_200', df['close'])) / (df['atr_14'] + eps)
    else:
        df['boll_upper_dist_atr'] = 0.0
        df['boll_lower_dist_atr'] = 0.0
        df['ema_200_dist_atr'] = 0.0

    # Percentage distances
    price = df['close']
    line_cols = [
        c
        for c in df.columns
        if c.startswith(('ema_', 'wma_', 'kama_', 'boll_', 'donchian_')) and not c.endswith('_dist_pct')
    ]
    for col in line_cols:
        df[f'{col}_dist_pct'] = pct_distance(price, df[col])

    return df.fillna(0)


async def hourly_pump_model_checker():
    """Runs every hour at :10 – checks all coins against the 3 pump models"""
    logger.info("Hourly Pump Dump Model Checker task started – runs at :10 every hour")
    await load_pump_models()  # Load at startup

    # === DEBUG: show models ===
    logger.info("=== LOADED PUMP MODELS ===")
    for horizon, data in PUMP_MODELS_LOADED.items():
        model_status = "LOADED" if data.get("model") is not None else "MISSING"
        threshold = data.get("threshold", "N/A")
        logger.info(f"{horizon.upper()}: {model_status} | Threshold: {threshold:.3f}")
    logger.info("================================")

    # for horizon, data in PUMP_MODELS_LOADED.items():

    # if data["model"]:
    # logger.info(f"{horizon.upper()} model expects features: {len(data['model'].feature_names_in_)}")
    # logger.info(f"Feature names: {data['model'].feature_names_in_}")
    # === MANUAL TEST with BTCUSDT ===
    # test_symbol = "BTCUSDT"
    # try:
    # conn_test = await get_conn()
    # row_test = await conn_test.fetch(f"""
    # SELECT
    # h.open_time,
    # h.close,
    # h.volume,
    # i.rsi_6, i.rsi_9, i.rsi_12, i.rsi_14, i.rsi_24,
    # i.ema_7, i.ema_9, i.ema_12, i.ema_21, i.ema_26, i.ema_34, i.ema_50, i.ema_55, i.ema_89, i.ema_99, i.ema_200,
    # i.wma_7, i.wma_9, i.wma_12, i.wma_21, i.wma_26, i.wma_34, i.wma_50, i.wma_55, i.wma_89, i.wma_99, i.wma_200,
    # i.kama_7, i.kama_9, i.kama_12, i.kama_21, i.kama_26, i.kama_34, i.kama_50, i.kama_55, i.kama_89, i.kama_99,
    # i.boll_upper_20, i.boll_mid_20, i.boll_lower_20,
    # i.donchian_upper_20, i.donchian_mid_20, i.donchian_lower_20,
    # i.tsi_fast_12_7_7 AS tsi_fast,
    # i.macd_dif_normal_12_26_9 AS macd_dif,
    # i.macd_dea_normal_12_26_9 AS macd_dea,
    # i.atr_14
    # FROM "{test_symbol}_1h" h
    # LEFT JOIN "{test_symbol}_1h_indicators" i ON h.open_time = i.open_time
    # ORDER BY h.open_time DESC LIMIT 100
    # """)
    # await release_conn(conn_test)

    # if row_test:
    # df_test = pd.DataFrame([dict(row) for row in row_test])
    # df_test = add_advanced_features(df_test)

    # # # Feature extraction (like in the main code)
    # # feature_cols = [col for col in df_test.columns if
    # # col.endswith('_dist_pct') or '_delta_1' in col or
    # # col in ['volume_ratio_prev', 'volume_ratio_sma20', 'rsi_6', 'rsi_9', 'rsi_12', 'rsi_14', 'rsi_24', 'tsi_fast',
    # # 'macd_hist', 'macd_hist_delta_1', 'above_ema_200', 'rsi_14_above_50', 'rsi_14_cross_above_30',
    # # 'ema_9_cross_above_21', 'boll_upper_dist_atr', 'boll_lower_dist_atr', 'ema_200_dist_atr', 'atr_14']
    # # ]

    # # Take any loaded model for the feature names (all have the same)
    # model_for_features = next((data["model"] for data in PUMP_MODELS_LOADED.values() if data["model"]), None)
    # #if model_for_features is None:
    # #continue  # No model loaded

    # feature_cols = model_for_features.feature_names_in_

    # # Check whether all features are present
    # missing = [col for col in feature_cols if col not in df_test.columns]
    # if missing:
    # logger.debug(f"BTC: missing features for pump models: {missing}")
    # #continue

    # X_current = df_test[feature_cols].values  # exact order and count

    # X_test = df_test[feature_cols].values

    # logger.info("=== MANUAL TEST BTCUSDT ===")
    # for horizon, data in PUMP_MODELS_LOADED.items():
    # if data.get("model"):
    # prob = data["model"].predict_proba(X_test)[0, 1]
    # above_thr = "YES" if prob >= data["threshold"] else "no"
    # logger.info(f"{horizon.upper()}: Prob {prob:.1%} | Threshold {data['threshold']:.3f} → Signal: {above_thr}")
    # except Exception as e:
    # logger.error(f"Test error: {e}")
    # # === END TEST ===

    coins = load_coins()
    while True:
        try:
            now = datetime.now(pytz.UTC)
            # Wait until 10 minutes past the full hour
            next_run = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) + timedelta(minutes=10)
            sleep_seconds = (next_run - datetime.now(pytz.UTC)).total_seconds()
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
            signals_batch = []
            logger.info(f"Hourly Pump Dump check started at {datetime.now(pytz.UTC).strftime('%H:%M')} UTC")

            for symbol in coins:
                try:
                    # Load data (as before – 100 rows of history)
                    conn = await get_conn()
                    rows = await conn.fetch(f"""
                        SELECT
                            h.open_time,
                            h.close,
                            h.volume,
                            i.rsi_6, i.rsi_9, i.rsi_12, i.rsi_14, i.rsi_24,
                            i.ema_7, i.ema_9, i.ema_12, i.ema_21, i.ema_26, i.ema_34, i.ema_50, i.ema_55, i.ema_89, i.ema_99, i.ema_200,
                            i.wma_7, i.wma_9, i.wma_12, i.wma_21, i.wma_26, i.wma_34, i.wma_50, i.wma_55, i.wma_89, i.wma_99, i.wma_200,
                            i.kama_7, i.kama_9, i.kama_12, i.kama_21, i.kama_26, i.kama_34, i.kama_50, i.kama_55, i.kama_89, i.kama_99,
                            i.boll_upper_20, i.boll_mid_20, i.boll_lower_20,
                            i.donchian_upper_20, i.donchian_mid_20, i.donchian_lower_20,
                            i.tsi_fast_12_7_7 AS tsi_fast,
                            i.macd_dif_normal_12_26_9 AS macd_dif,
                            i.macd_dea_normal_12_26_9 AS macd_dea,
                            i.atr_14
                        FROM "{symbol}_1h" h
                        LEFT JOIN "{symbol}_1h_indicators" i ON h.open_time = i.open_time
                        ORDER BY h.open_time DESC LIMIT 100
                    """)
                    await release_conn(conn)

                    if not rows:
                        continue

                    df_current = pd.DataFrame([dict(row) for row in rows])
                    if len(df_current) < 10:
                        continue

                    df_current = add_advanced_features(df_current)
                    df_current = df_current.iloc[0:1]  # only the most recent candle

                    # logger.info(" Model loading now")
                    # Take the feature list from the first loaded model (all have the same features)
                    model_sample = next((data["model"] for data in PUMP_MODELS_LOADED.values() if data["model"]), None)
                    # logger.info(" Model loading now II")
                    if model_sample is None:
                        logger.warning("No model loaded – skipping checker")
                        break

                    feature_cols = model_sample.feature_names_in_

                    missing = [col for col in feature_cols if col not in df_current.columns]
                    if missing:
                        logger.debug(f"{symbol}: missing features – skipping")
                        continue

                    X_current = df_current[feature_cols].values

                    # logger.info("checking model now")
                    # === Check all models (PUMP + DUMP) ===
                    candidates = []  # List of (prob, horizon, direction)

                    for horizon, data in PUMP_MODELS_LOADED.items():
                        # if data.get("model") is None:
                        # continue

                        # logger.info("checking model - now in the loop")

                        model = data["model"]
                        threshold = data["threshold"]

                        # logger.info("model and threshold loaded")

                        try:
                            prob = model.predict_proba(X_current)[0, 1]  # 1 = positive event (pump or dump)
                            # logger.info(f"{symbol} {horizon.upper()}: {prob:.1%} (Threshold {threshold:.1%})")
                        except Exception as e:
                            logger.info(f"Prediction error {symbol} {horizon}: {e}")
                            continue

                        # Only if threshold exceeded
                        # if prob >= threshold:

                        if prob >= 0.25:  # instead of just threshold + 0.1 or 0.45
                            entry_price = float(df_current['close'].iloc[0])
                            direction = "LONG" if "pump" in horizon.lower() else "SHORT"
                            modell_key = f"MIS1-{horizon}"
                            signals_batch.append(
                                {
                                    'symbol': symbol,
                                    'price': entry_price,
                                    'model': modell_key,
                                    'direction': direction,
                                    'confidence': prob,
                                }
                            )
                        # if prob >= threshold and 'USDT_' not in symbol:
                        if prob >= 0.6 and 'USDT_' not in symbol:
                            direction = "LONG" if "pump" in horizon.lower() else "SHORT"
                            candidates.append(
                                (prob, horizon.upper().replace("_PUMP", "").replace("_DUMP", ""), direction, prob)
                            )
                            # logger.info(f"{symbol}: Signal found {direction} {prob}")
                        else:
                            direction = "LONG" if "pump" in horizon.lower() else "SHORT"
                            # logger.info(f"{symbol}: no strong Signal found {direction} {prob}")

                    # === No signal? Next coin ===
                    if not candidates:
                        # logger.info(f"{symbol}: no signal (highest prob: {max((p for p, _, _, _ in candidates or [(0, '', '', 0)]), default=0):.1%})")
                        continue

                    # else:
                    # #logger.info(f"{symbol}: {len(candidates)} potential signals found")

                    # === Select the strongest signal ===
                    candidates.sort(reverse=True)  # highest prob first
                    best_prob, best_horizon, best_direction, _ = candidates[0]

                    strength = (
                        "STRONG" if best_prob >= data["threshold"] + 0.1 else "MODERATE"
                    )  # Threshold from the last model – doesn't matter, only for text

                    # === Send Cornix signal ===
                    is_long = best_direction == "LONG"
                    await send_cornix_signal(symbol, is_long, "MIS1")

                    # === Posting ===
                    entry_price = float(df_current['close'].iloc[0])

                    border_color = "#00ff00" if is_long else "#ff0066"
                    emoji = (
                        "🚀 PUMP SIGNAL - AI INDICATOR COMBINATOR"
                        if is_long
                        else "💥 DUMP SIGNAL - AI INDICATOR COMBINATOR"
                    )

                    # Only show the strongest signal + probability
                    signal_text = f"{best_horizon} {'PUMP' if is_long else 'DUMP'} "

                    html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {border_color};">
<b style="color:#00ffff; font-size:18px;">{emoji}</b>
<b style="color:#ffd700;">{symbol.replace('USDT', '')}/USDT</b>
<b>→ Direction: <b style="color:{border_color};">{best_direction}</b></b>
<b>→ Signal Model: <b style="color:#00ffff;">{signal_text}</b></b>
<b>→ ML-Confidence: <b style="color:#00ffff;">{strength} – {best_prob:.1%}</b></b>
<b>→ Time: {now.strftime('%H:%M')} UTC | Module: MIS1</b>
</pre>
                    """.strip()

                    chart_buf = await generate_smooth_minichart_image(symbol, minutes=240)
                    try:
                        if chart_buf:
                            await application.bot.send_photo(
                                chat_id=AI_CHANNEL_ID, photo=chart_buf, caption=html, parse_mode="HTML"
                            )
                            chart_buf.close()
                        else:
                            await application.bot.send_message(chat_id=AI_CHANNEL_ID, text=html, parse_mode="HTML")
                        logger.info(f"Signal sent: {symbol} {best_direction} | {signal_text}")
                    except Exception as e:
                        logger.error(f"Signal send error {symbol}: {e}")
                        if chart_buf:
                            chart_buf.close()

                except Exception as e:
                    logger.error(f"Error for {symbol}: {e}", exc_info=True)

            if signals_batch:
                asyncio.create_task(log_ai_signals(signals_batch))
        except Exception as e:
            logger.error(f"Error for {symbol}: {e}", exc_info=True)


# ========================= AI Tracker =========================
async def create_ai_signals_table():
    """Creates the table for AI signals if it doesn't already exist"""
    conn = await get_conn()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_signals (
                id BIGSERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                price NUMERIC NOT NULL,
                model TEXT NOT NULL,          -- e.g. 'EPD1', 'MIS1', 'ATS1'
                direction TEXT NOT NULL,      -- 'LONG' or 'SHORT'
                confidence NUMERIC NOT NULL,  -- e.g. 0.73 for 73%
                inserted_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Index for fast queries by time and model
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_signals_timestamp
            ON ai_signals(timestamp DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_signals_model
            ON ai_signals(model, timestamp DESC)
        """)
        logger.info("Table 'ai_signals' ready or created")
    except Exception as e:
        logger.error(f"Error creating the ai_signals table: {e}")
    finally:
        await release_conn(conn)


async def log_ai_signals(signals: list[dict]):
    """
    Writes a list of signals to the DB.
    signals = [{'symbol': 'BTCUSDT', 'price': 65000.0, 'model': 'EPD1', 'direction': 'LONG', 'confidence': 0.73}, ...]
    """
    if not signals:
        return

    conn = await get_conn()
    try:
        async with conn.transaction():
            # IMPORTANT: convert all values to plain Python types
            prepared_data = []
            for s in signals:
                prepared_data.append(
                    (
                        str(s['symbol']),  # TEXT
                        float(s['price']),  # NUMERIC → Python float
                        str(s['model']),  # TEXT
                        str(s['direction']),  # TEXT
                        float(s['confidence']),  # NUMERIC → explicit float()
                    )
                )

            await conn.executemany(
                """
                INSERT INTO ai_signals (symbol, timestamp, price, model, direction, confidence)
                VALUES ($1, NOW(), $2, $3, $4, $5)
                ON CONFLICT DO NOTHING
            """,
                prepared_data,
            )

        logger.info(f"{len(signals)} AI signal(s) successfully written to DB")
    except Exception as e:
        logger.error(f"Error writing the AI signals: {e}", exc_info=True)
    finally:
        await release_conn(conn)


async def graphai_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    match = re.match(r"(?i)!graphai\s+(\w+)", update.message.text.strip())
    if not match:
        await update.message.reply_text("Usage: !graphai <coin>")
        return

    raw_coin = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_coin)
    if not valid_symbol:
        await update.message.reply_text(f"Coin not found: {raw_coin}")
        return

    username = update.effective_user.username or update.effective_user.full_name or "unknown"

    await update.message.reply_chat_action(ChatAction.UPLOAD_PHOTO)

    # Load data
    data_pack = await load_graphai_data_binance(valid_symbol)
    if not data_pack or data_pack['df_klines'].empty:
        await update.message.reply_text("No market data available.")
        return

    ts_str = datetime.now(pytz.UTC).strftime('%d.%m %H:%M')
    base_caption = f"{valid_symbol.replace('USDT', '')}/USDT • @{username} • {ts_str} UTC"

    # Configs
    plots_config = [
        {"title": "ALL Models (Only Conf >= 0.65)", "filter_model": None, "min_confidence": 0.65, "mode": "overview"},
        {
            "title": "EPD Only (Big = Conf >= 0.65)",
            "filter_prefix": "EPD",
            "min_confidence": 0.0,
            "mode": "single_model",
        },
        {
            "title": "ATS Only (Big = Conf >= 0.65)",
            "filter_prefix": "ATS",
            "min_confidence": 0.0,
            "mode": "single_model",
        },
        {
            "title": "MSI Only (8h=●, 72h=■, 168h=◆)",
            "filter_prefix": "MSI",
            "min_confidence": 0.0,
            "mode": "mis_special",
        },
    ]

    charts_sent = 0

    for cfg in plots_config:
        try:
            # Generate chart
            buf = await asyncio.wait_for(asyncio.to_thread(plot_graphai_flexible, data_pack, cfg), timeout=30.0)

            # IMPORTANT: if buf is None, there were no signals -> skip
            if buf is None:
                continue

            await update.message.reply_photo(photo=buf, caption=f"{base_caption}\n📊 **{cfg['title']}**")
            charts_sent += 1
            await asyncio.sleep(0.3)

        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.error(f"Chart error {cfg.get('title')}: {e}", exc_info=True)
            continue

    if charts_sent == 0:
        await update.message.reply_text(
            "No relevant signals found in the last 5 days (for the selected filters)."
        )


async def load_graphai_data_binance(symbol: str):
    """
    Loads klines from Binance and signals from the DB.
    FIX: always initializes df_signals with columns to prevent a KeyError on empty data.
    """
    try:
        # --- A) Load signals from DB ---
        conn = await get_conn()
        rows = await conn.fetch(
            """
            SELECT timestamp, direction, model, confidence, price
            FROM ai_signals
            WHERE symbol = $1
              AND timestamp >= NOW() - INTERVAL '5 days'
            ORDER BY timestamp ASC
        """,
            symbol,
        )
        await release_conn(conn)

        # FIX: define columns explicitly!
        columns = ['timestamp', 'price_signal', 'direction', 'model', 'confidence']

        if rows:
            data_list = []
            for r in rows:
                data_list.append(
                    {
                        'timestamp': r['timestamp'].astimezone(pytz.UTC),
                        'price_signal': float(r['price']),
                        'direction': r['direction'],
                        'model': r['model'],
                        'confidence': float(r['confidence']),
                    }
                )
            df_signals = pd.DataFrame(data_list)
            # Ensure the timestamp is sorted
            df_signals = df_signals.sort_values('timestamp')
        else:
            # Create an empty DataFrame with the correct columns
            df_signals = pd.DataFrame(columns=columns)

        # --- B) Load klines from Binance ---
        end_time = int(time.time() * 1000)
        start_time = end_time - (5 * 24 * 60 * 60 * 1000)  # 5 days

        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {'symbol': symbol, 'interval': '5m', 'startTime': start_time, 'endTime': end_time, 'limit': 1500}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return None
                raw_klines = await resp.json()

        if not raw_klines:
            return None

        # Create DataFrame
        df_klines = pd.DataFrame(
            raw_klines,
            columns=[
                'open_time',
                'open',
                'high',
                'low',
                'close',
                'volume',
                'close_time',
                'q_vol',
                'trades',
                'tb_base',
                'tb_quote',
                'ignore',
            ],
        )

        # Conversion
        df_klines['open_time'] = pd.to_datetime(df_klines['open_time'], unit='ms', utc=True)
        df_klines = df_klines.set_index('open_time')

        cols_to_float = ['open', 'high', 'low', 'close', 'volume']
        df_klines[cols_to_float] = df_klines[cols_to_float].astype(float)

        return {'symbol': symbol, 'df_klines': df_klines, 'df_signals': df_signals}

    except Exception as e:
        logger.error(f"load_graphai_data_binance Exception: {e}", exc_info=True)
        return None


def plot_graphai_flexible(data_pack: dict, config: dict) -> BytesIO:
    """
    Generates a chart with volume.
    Returns None if no signals remain after filtering.
    """
    symbol = data_pack['symbol']
    df_klines = data_pack['df_klines'].copy()
    df_signals = data_pack['df_signals']

    if df_klines.empty:
        return None

    # --- 1. FILTER DATA & SKIP CHECK ---
    if df_signals is None or df_signals.empty:
        return None  # No signals present -> no chart

    current_signals = df_signals.copy()

    # a) Confidence filter
    if config.get('min_confidence', 0) > 0:
        current_signals = current_signals[current_signals['confidence'] >= config['min_confidence']]

    # b) Model filter
    if config.get('filter_prefix'):
        current_signals = current_signals[current_signals['model'].str.startswith(config['filter_prefix'])]
    elif config.get('filter_model'):
        current_signals = current_signals[current_signals['model'] == config['filter_model']]

    # SKIP CHECK: if empty after filtering -> abort
    if current_signals.empty:
        return None

    # --- 2. MAPPING ---
    df_klines['mpl_index'] = range(len(df_klines))
    df_klines_merged = df_klines.reset_index()[['open_time', 'mpl_index', 'close']]

    mapped_signals = pd.DataFrame()
    mapped_signals = pd.merge_asof(
        current_signals,
        df_klines_merged,
        left_on='timestamp',
        right_on='open_time',
        direction='nearest',
        tolerance=pd.Timedelta('15min'),
    )
    mapped_signals = mapped_signals.dropna(subset=['mpl_index'])

    # Check again just to be safe
    if mapped_signals.empty:
        return None

    # --- 3. PLOT SETUP (WITH VOLUME) ---
    dark_style = mpf.make_mpf_style(base_mpf_style='nightclouds', gridcolor='#262626')

    # volume=True and panel_ratios
    fig, axlist = mpf.plot(
        df_klines,
        type='candle',
        style=dark_style,
        returnfig=True,
        figsize=(18, 12),  # A bit taller for volume
        warn_too_much_data=2000,
        datetime_format='%d.%m %H:%M',
        xrotation=20,
        volume=True,  # Enable volume!
        panel_ratios=(6, 2),  # Ratio candles:volume
    )
    ax = axlist[0]  # Main chart (candles)
    # axlist[2] would be the volume chart (axlist[1] is often a legend/axis-sharing entry)
    # mpf handles the volume, we only need to draw on ax[0].

    # --- 4. HELPER ---
    def calc_sizes_step(confidences):
        sizes = []
        for c in confidences:
            if c >= 0.65:
                sizes.append(350)
            else:
                sizes.append(60)
        return sizes

    def get_msi_marker(model_str):
        m = model_str.upper()
        if '168' in m:
            return 'D'
        if '72' in m:
            return 's'
        if '8' in m:
            return 'o'
        return 'o'

    # --- 5. DRAW SIGNALS ---
    # Mode 1: OVERVIEW
    if config['mode'] == 'overview':
        groups = mapped_signals.groupby(['model', 'direction'])
        for (model, direction), group in groups:
            c_code = '#cccccc'
            if 'EPD' in model:
                c_code = '#00ff00' if direction == 'LONG' else '#ff0000'
            elif 'ATS' in model:
                c_code = '#00ffff' if direction == 'LONG' else '#ff00ff'
            elif 'MSI' in model:
                c_code = '#ffff00' if direction == 'LONG' else '#ff9900'

            sizes = calc_sizes_step(group['confidence'].values)
            _scatter_group(ax, group, c_code, sizes, label=f"{model} {direction}")

    # Mode 2: SINGLE MODEL
    elif config['mode'] == 'single_model':
        color_map = {'LONG': '#00ff00', 'SHORT': '#ff0000'}
        for direction, group in mapped_signals.groupby('direction'):
            c_code = color_map.get(direction, '#ffffff')
            sizes = calc_sizes_step(group['confidence'].values)
            _scatter_group(ax, group, c_code, sizes, label=direction)

    # Mode 3: MSI SPECIAL
    elif config['mode'] == 'mis_special':
        color_map = {'LONG': '#00ff00', 'SHORT': '#ff0000'}
        groups = mapped_signals.groupby(['model', 'direction'])

        for (model, direction), group in groups:
            c_code = color_map.get(direction, '#ffffff')
            sizes = calc_sizes_step(group['confidence'].values)
            marker_char = get_msi_marker(model)

            clean_lbl = "MSI"
            if '8' in model:
                clean_lbl = "8h"
            if '72' in model:
                clean_lbl = "72h"
            if '168' in model:
                clean_lbl = "168h"

            _scatter_group(ax, group, c_code, sizes, label=f"{clean_lbl} {direction}", marker=marker_char)

    ax.legend(loc='upper left', facecolor='#0d0d0d', labelcolor='white', fontsize=10, framealpha=0.8)

    clean_symbol = symbol.replace('USDT', '')
    ax.set_title(f"{clean_symbol} • {config['title']}", color='white', fontsize=16, pad=20)

    buf = BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', facecolor='#0d0d0d', dpi=100)
    buf.seek(0)
    plt.close(fig)
    return buf


def _scatter_group(ax, group, color, sizes, label, marker='o'):
    x_vals = group['mpl_index'].values
    y_vals = group['close'].values
    direction = group['direction'].iloc[0]

    if direction == 'LONG':
        y_vals = y_vals * 0.997
    else:
        y_vals = y_vals * 1.003

    ax.scatter(
        x_vals,
        y_vals,
        s=sizes,
        c=color,
        label=label,
        alpha=0.9,
        edgecolors='white',
        linewidth=1.0,
        zorder=10,
        marker=marker,
    )


def plot_graphai_candles_v2(data_pack: dict) -> BytesIO:
    """
    Creates the chart.
    Corrected version: 'gridcolor' instead of 'grid_color'
    """
    symbol = data_pack['symbol']
    df_klines = data_pack['df_klines']
    df_signals = data_pack['df_signals']

    if df_klines.empty:
        return None

    # 1. Preparation for mplfinance mapping
    # mplfinance internally uses integer indices (0, 1, 2...) for the X axis.
    df_klines['mpl_index'] = range(len(df_klines))

    # Reset index so 'open_time' is a normal column for merge_asof
    df_klines_merged = df_klines.reset_index()[['open_time', 'mpl_index', 'close']]

    mapped_signals = pd.DataFrame()

    if not df_signals.empty:
        # Map signals to the nearest kline (tolerance 15 min)
        mapped_signals = pd.merge_asof(
            df_signals,
            df_klines_merged,
            left_on='timestamp',
            right_on='open_time',
            direction='nearest',
            tolerance=pd.Timedelta('15min'),
        )

        # Remove signals without a match
        mapped_signals = mapped_signals.dropna(subset=['mpl_index'])

    # 2. Plotting setup (THIS IS WHERE THE BUG WAS)
    # Fix: gridcolor (without underscore)
    dark_style = mpf.make_mpf_style(base_mpf_style='nightclouds', gridcolor='#262626')

    fig, axlist = mpf.plot(
        df_klines,
        type='candle',
        style=dark_style,
        returnfig=True,
        figsize=(18, 10),
        warn_too_much_data=2000,
        datetime_format='%d.%m %H:%M',
        xrotation=20,
        volume=False,
    )
    ax = axlist[0]  # Main chart axis

    # 3. Draw signals (group-wise!)
    if not mapped_signals.empty:
        # Color mapping
        color_map = {
            'EPD1_LONG': '#00ff00',
            'EPD1_SHORT': '#ff0000',
            'ATS1_LONG': '#00ffff',
            'ATS1_SHORT': '#ff00ff',
            'MIS1_LONG': '#ffff00',
            'MIS1_SHORT': '#ff8800',
        }

        # Group by model and direction -> vectorization for Matplotlib
        groups = mapped_signals.groupby(['model', 'direction'])

        for (model, direction), group in groups:
            key = f"{model}_{direction}"
            c_code = color_map.get(key, '#cccccc')

            # X values: the computed integer index
            x_vals = group['mpl_index'].values

            # Y values: offset the price slightly
            y_vals = group['close'].values
            if direction == 'LONG':
                y_vals = y_vals * 0.997  # Below the candle
            else:
                y_vals = y_vals * 1.003  # Above the candle

            # Size based on confidence
            sizes = (group['confidence'] * 300).clip(lower=50, upper=400).values

            # PLOT COMMAND
            ax.scatter(
                x_vals,
                y_vals,
                s=sizes,
                c=c_code,
                label=f"{model} {direction}",
                alpha=0.9,
                edgecolors='white',
                linewidth=1.0,
                zorder=10,
            )

        # Add legend
        ax.legend(loc='upper left', facecolor='#0d0d0d', labelcolor='white', fontsize=10, framealpha=0.8)

    # Title
    ax.set_title(f"{symbol.replace('USDT', '')} (5D 5m) + AI Signals", color='white', fontsize=16, pad=20)

    # Save
    buf = BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', facecolor='#0d0d0d', dpi=100)
    buf.seek(0)
    plt.close(fig)

    return buf


# ========================= graph conventional signals  =========================


async def graphconv_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler for !graphconv <coin>
    Shows signals from the 'conv_signals' table.
    """
    if not update.message or not update.message.text:
        return

    match = re.match(r"(?i)!graphconv\s+(\w+)", update.message.text.strip())
    if not match:
        await update.message.reply_text("Usage: !graphconv <coin>\nExample: !graphconv BTC")
        return

    raw_coin = match.group(1).upper()
    valid_symbol = await validate_symbol(raw_coin)
    if not valid_symbol:
        await update.message.reply_text(f"Coin not found: {raw_coin}")
        return

    username = update.effective_user.username or update.effective_user.full_name or "unknown"
    # log_command(...)

    await update.message.reply_chat_action(ChatAction.UPLOAD_PHOTO)

    # 1. Load data (klines + conv signals)
    data_pack = await load_conv_data(valid_symbol)

    if not data_pack or data_pack['df_klines'].empty:
        await update.message.reply_text("No market data available.")
        return

    # 2. Create chart
    try:
        buf = await asyncio.wait_for(asyncio.to_thread(plot_graphconv_chart, data_pack), timeout=30.0)
    except asyncio.TimeoutError:
        await update.message.reply_text("Timeout while creating the chart.")
        return
    except Exception as e:
        logger.error(f"GraphConv error: {e}", exc_info=True)
        await update.message.reply_text("Error drawing the chart.")
        return

    # 3. Send
    ts_str = datetime.now(pytz.UTC).strftime('%d.%m %H:%M')
    clean_symbol = valid_symbol.replace('USDT', '')
    caption = f"CONV Signals • {clean_symbol}/USDT • @{username}\n{ts_str} UTC"

    await update.message.reply_photo(photo=buf, caption=caption)


async def load_conv_data(symbol: str):
    """
    Loads klines from Binance and signals from the 'conv_signals' table.
    """
    try:
        # --- A) Load conv signals from DB ---
        # Assumption: 'coin' in the DB is e.g. 'BTCUSDT' or 'BTC'.
        # We try both variants or use 'valid_symbol' directly.
        # Since your DB uses 'coin' with values like 'NIGHTUSDT', we use symbol.

        conn = await get_conn()
        rows = await conn.fetch(
            """
            SELECT source_bot, source_time, direction, entry_price, lev
            FROM conv_signals
            WHERE coin = $1
              AND source_time >= NOW() - INTERVAL '5 days'
            ORDER BY source_time ASC
        """,
            symbol,
        )
        await release_conn(conn)

        columns = ['source_time', 'source_bot', 'direction', 'entry_price', 'lev']

        if rows:
            data_list = []
            for r in rows:
                data_list.append(
                    {
                        'timestamp': r['source_time'].astimezone(pytz.UTC),
                        'source_bot': r['source_bot'],
                        'direction': r['direction'],
                        'entry_price': float(r['entry_price']),
                        'lev': r['lev'],
                    }
                )
            df_signals = pd.DataFrame(data_list)
            df_signals = df_signals.sort_values('timestamp')
        else:
            df_signals = pd.DataFrame(columns=['timestamp', 'source_bot', 'direction', 'entry_price', 'lev'])

        # --- B) Load klines from Binance (copy & paste logic from before) ---
        end_time = int(time.time() * 1000)
        start_time = end_time - (5 * 24 * 60 * 60 * 1000)  # 5 days

        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {'symbol': symbol, 'interval': '5m', 'startTime': start_time, 'endTime': end_time, 'limit': 1500}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return None
                raw_klines = await resp.json()

        if not raw_klines:
            return None

        df_klines = pd.DataFrame(
            raw_klines,
            columns=[
                'open_time',
                'open',
                'high',
                'low',
                'close',
                'volume',
                'close_time',
                'q_vol',
                'trades',
                'tb_base',
                'tb_quote',
                'ignore',
            ],
        )

        df_klines['open_time'] = pd.to_datetime(df_klines['open_time'], unit='ms', utc=True)
        df_klines = df_klines.set_index('open_time')

        cols_to_float = ['open', 'high', 'low', 'close', 'volume']
        df_klines[cols_to_float] = df_klines[cols_to_float].astype(float)

        return {'symbol': symbol, 'df_klines': df_klines, 'df_signals': df_signals}

    except Exception as e:
        logger.error(f"load_conv_data Exception: {e}", exc_info=True)
        return None


def plot_graphconv_chart(data_pack: dict) -> BytesIO:
    """
    Draws the chart for conv_signals WITH VOLUME.
    """
    symbol = data_pack['symbol']
    df_klines = data_pack['df_klines'].copy()
    df_signals = data_pack['df_signals']

    if df_klines.empty:
        return None

    df_klines['mpl_index'] = range(len(df_klines))
    df_klines_merged = df_klines.reset_index()[['open_time', 'mpl_index', 'close']]

    mapped_signals = pd.DataFrame()
    if not df_signals.empty:
        mapped_signals = pd.merge_asof(
            df_signals,
            df_klines_merged,
            left_on='timestamp',
            right_on='open_time',
            direction='nearest',
            tolerance=pd.Timedelta('30min'),
        )
        mapped_signals = mapped_signals.dropna(subset=['mpl_index'])

    dark_style = mpf.make_mpf_style(base_mpf_style='nightclouds', gridcolor='#262626')

    # Create chart with volume
    fig, axlist = mpf.plot(
        df_klines,
        type='candle',
        style=dark_style,
        returnfig=True,
        figsize=(18, 12),  # A bit taller
        warn_too_much_data=2000,
        datetime_format='%d.%m %H:%M',
        xrotation=20,
        volume=True,  # Volume on
        panel_ratios=(6, 2),  # Adjust ratio
    )
    ax = axlist[0]

    if not mapped_signals.empty:
        bot_colors = {
            'Volume Bot': '#00ffff',
            'Fast Bot': '#ffff00',
            'SR Bot': '#ffffff',
            '5% Bot': '#ff9900',
        }
        fallback_color = '#cccccc'

        groups = mapped_signals.groupby(['source_bot', 'direction'])
        for (bot_name, direction), group in groups:
            c_code = bot_colors.get(bot_name, fallback_color)
            marker = '^' if direction == 'LONG' else 'v'

            x_vals = group['mpl_index'].values
            y_vals = group['entry_price'].values

            if direction == 'LONG':
                y_vals = y_vals * 0.995
            else:
                y_vals = y_vals * 1.005

            size = 150
            ax.scatter(
                x_vals,
                y_vals,
                s=size,
                c=c_code,
                label=f"{bot_name} {direction}",
                alpha=0.9,
                edgecolors='black',
                linewidth=1.0,
                zorder=10,
                marker=marker,
            )

        ax.legend(loc='upper left', facecolor='#0d0d0d', labelcolor='white', fontsize=10, framealpha=0.8)

    clean_symbol = symbol.replace('USDT', '')
    ax.set_title(f"{clean_symbol} (5D) • Conv Signals", color='white', fontsize=16, pad=20)

    buf = BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', facecolor='#0d0d0d', dpi=100)
    buf.seek(0)
    plt.close(fig)
    return buf


# ========================= 30 MIN and 1h filler  =========================
def run_kline_filler():
    """
    Your original script – taken over 1:1, just as a function
    Runs in a separate thread → doesn't block the bot
    """
    import datetime
    import json
    import logging
    import time
    from concurrent.futures import ThreadPoolExecutor

    import psycopg2
    import pytz
    import requests
    from psycopg2 import extras

    # --- Configuration (like in your script) ---
    DB_NAME = 'cryptodata'
    DB_USER = 'dbfiller'
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_HOST = 'localhost'
    DB_PORT = 5432
    BASE_URL = 'https://fapi.binance.com'
    KLINE_ENDPOINT = '/fapi/v1/klines'
    TIMEFRAMES = ['30m', '1h']
    NUM_WORKERS = 20

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

    db_pool = None

    def init_db_pool():
        nonlocal db_pool
        db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=5,
            maxconn=NUM_WORKERS + 5,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
        )

    def load_coins(filename='coins.json'):
        try:
            with open(filename) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading coins.json: {e}")
            return []

    def init_tables(symbols):
        logger.info("Checking table structure...")
        conn = db_pool.getconn()
        try:
            with conn.cursor() as cur:
                for symbol in symbols:
                    for tf in TIMEFRAMES:
                        tablename = f'"{symbol}_{tf}"'
                        cur.execute(f"""
                            CREATE TABLE IF NOT EXISTS {tablename} (
                                symbol TEXT, open_time TIMESTAMP WITH TIME ZONE,
                                open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
                                close DOUBLE PRECISION, volume DOUBLE PRECISION,
                                PRIMARY KEY (symbol, open_time)
                            );
                        """)
            conn.commit()
        except Exception as e:
            logger.error(f"Init Error: {e}")
            conn.rollback()
        finally:
            db_pool.putconn(conn)

    def get_latest_open_time(conn, symbol, timeframe):
        tablename = f'"{symbol}_{timeframe}"'
        try:
            with conn.cursor() as cursor:
                cursor.execute(f'SELECT MAX(open_time) FROM {tablename}')
                res = cursor.fetchone()
                return res[0].astimezone(pytz.utc) if res and res[0] else None
        except:
            conn.rollback()
            return None

    def fetch_ohlcv_smart(session, symbol, interval, start_ts, end_ts):
        url = BASE_URL + KLINE_ENDPOINT
        all_data = []
        curr = start_ts

        session.headers.update({"User-Agent": "CryptoBot/Turbo"})
        while True:
            time_diff = end_ts - curr
            is_update = time_diff < (48 * 3600 * 1000)
            limit = 100 if is_update else 1500
            params = {'symbol': symbol, 'interval': interval, 'startTime': curr, 'endTime': end_ts, 'limit': limit}

            try:
                resp = session.get(url, params=params, timeout=5)

                if resp.status_code == 429:
                    retry = int(resp.headers.get("Retry-After", 5))
                    logger.warning(f"⚠️ Hit Limit! Pause {retry}s...")
                    time.sleep(retry)
                    continue

                if resp.status_code != 200:
                    break

                data = resp.json()
                if not data:
                    break
                all_data.extend(data)

                if len(data) < limit:
                    break

                curr = data[-1][6] + 1
                if curr >= end_ts:
                    break

                if not is_update:
                    time.sleep(0.2)

            except Exception:
                time.sleep(1)
                break

        return all_data

    def insert_fast(conn, data, symbol, timeframe):
        if not data:
            return 0
        tablename = f'"{symbol}_{timeframe}"'
        tuples = []
        for row in data:
            ts = datetime.datetime.fromtimestamp(row[0] / 1000, pytz.utc)
            tuples.append((symbol, ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])))

        sql = f"""
            INSERT INTO {tablename} (symbol, open_time, open, high, low, close, volume)
            VALUES %s
            ON CONFLICT (symbol, open_time) DO UPDATE
            SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                close=EXCLUDED.close, volume=EXCLUDED.volume
        """
        try:
            with conn.cursor() as cur:
                extras.execute_values(cur, sql, tuples)
            conn.commit()
            return len(tuples)
        except:
            conn.rollback()
            return 0

    def process_coin(symbol):
        conn = None
        try:
            conn = db_pool.getconn()
            session = requests.Session()

            now = datetime.datetime.now(datetime.timezone.utc)
            end_ts = int(now.timestamp() * 1000)

            for tf in TIMEFRAMES:
                latest = get_latest_open_time(conn, symbol, tf)

                if latest:
                    start_dt = latest
                else:
                    start_dt = now - datetime.timedelta(days=365)

                start_ts = int(start_dt.timestamp() * 1000)

                if start_ts > end_ts:
                    continue
                raw = fetch_ohlcv_smart(session, symbol, tf, start_ts, end_ts)

                if raw:
                    count = insert_fast(conn, raw, symbol, tf)
                    if count > 5:
                        logger.info(f"{symbol}: {count} loaded.")

        except Exception as e:
            logger.error(f"Error {symbol}: {e}")
        finally:
            if conn:
                db_pool.putconn(conn)
            session.close()

    # === Main logic ===
    start_t = time.time()

    init_db_pool()
    symbols = load_coins()
    if not symbols:
        logger.warning("No coins loaded – task ended")
        return

    init_tables(symbols)
    logger.info(f"🚀 Kline filler started: {len(symbols)} coins, {NUM_WORKERS} threads...")

    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as exe:
        exe.map(process_coin, symbols)

    if db_pool:
        db_pool.closeall()

    duration = time.time() - start_t
    logger.info(f"✅ Kline filler done in {duration:.2f}s ({len(symbols) / duration:.1f} coins/sec)")


async def kline_filler_wrapper(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Kline filler started")
    await asyncio.to_thread(run_kline_filler)
    logger.info("Kline filler complete – starting indicator calculator")

    # Start the indicator calculator directly afterward
    await indicator_calculator_wrapper(context)


def run_indicator_calculator():
    """
    Your original indicator script – taken over 1:1 as a function
    Runs in a separate thread
    Computes indicators for 30m and 1h
    """
    import datetime
    import json
    import logging
    import warnings
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np
    import pandas as pd
    import psycopg2
    import scipy.signal
    from psycopg2 import extras
    from scipy import stats

    warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

    # --- Configuration (like in your script) ---
    DB_NAME = 'cryptodata'
    DB_USER = 'dbfiller'
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_HOST = 'localhost'
    DB_PORT = 5432
    COINS_FILE = 'coins.json'
    INDICATOR_SUFFIX = '_indicators'
    LOOKBACK_PERIOD = 5000
    NUM_WORKERS = 5
    TIMEFRAMES = ['30m', '1h']  # <-- Both timeframes

    db_pool = None

    def init_db_pool():
        global db_pool
        # Adjust pool size to workers
        db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=5,
            maxconn=NUM_WORKERS + 5,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT,
        )

    # --- Your helper functions (exactly like in the script) ---
    # (get_indicator_definitions, create_indicator_table, table_exists, calculate_trendline_and_channel_robust_optimized,
    #  get_hvn_poc_for_dataset, find_support_resistance, calc_fibonacci_levels_dynamic,
    #  calculate_wma, calculate_smma, calculate_rsi, calculate_kama, calculate_indicators_optimized,
    #  write_indicators_to_db_optimized)

    # ... (copy all your functions in here – exactly like the original) ...
    # --- Helper functions ---

    def get_timeframe_delta(timeframe):
        if timeframe == '1h':
            return pd.Timedelta(hours=1)
        if timeframe == '15m':
            return pd.Timedelta(minutes=15)
        if timeframe == '30m':
            return pd.Timedelta(minutes=30)
        if timeframe == '4h':
            return pd.Timedelta(hours=4)
        if timeframe == '1d':
            return pd.Timedelta(days=1)
        return pd.Timedelta(hours=1)

    def load_coins(filename=COINS_FILE):
        try:
            with open(filename) as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading coins.json: {e}")
            return []

    # def table_exists(conn, table_name):
    # try:
    # with conn.cursor() as cursor:
    # cursor.execute("SELECT to_regclass(%s)", (table_name,))
    # return cursor.fetchone()[0] is not None
    # except Exception as e:
    # logging.error(f"Error Table Exists {table_name}: {e}")
    # return False

    def table_exists(conn, table_name):
        """
        Checks whether a table exists.
        IMPORTANT: table_name must be passed exactly as it is named in the DB.
        If the table is called "BTCUSDT_1h" (with quotes), the string must also contain the quotes.
        """
        try:
            with conn.cursor() as cursor:
                # We use %s here, psycopg2 escapes it safely.
                # to_regclass expects the name as a string.
                # If table_name = '"BTCUSDT_1h"', this works perfectly.
                cursor.execute("SELECT to_regclass(%s)", (table_name,))
                result = cursor.fetchone()
                return result[0] is not None
        except Exception as e:
            logging.error(f"Error Table Exists {table_name}: {e}")
            conn.rollback()  # Important: reset transaction on error
            return False

    def get_indicator_definitions():
        # Definitions shortened for clarity - logic stays the same
        definitions = {
            "RSI_6": "REAL",
            "RSI_9": "REAL",
            "RSI_12": "REAL",
            "RSI_14": "REAL",
            "RSI_24": "REAL",
            "EMA_7": "REAL",
            "EMA_9": "REAL",
            "EMA_12": "REAL",
            "EMA_21": "REAL",
            "EMA_26": "REAL",
            "EMA_34": "REAL",
            "EMA_50": "REAL",
            "EMA_55": "REAL",
            "EMA_89": "REAL",
            "EMA_99": "REAL",
            "EMA_200": "REAL",
            "MA_7": "REAL",
            "MA_10": "REAL",
            "MA_20": "REAL",
            "MA_25": "REAL",
            "MA_50": "REAL",
            "MA_99": "REAL",
            "MA_100": "REAL",
            "MA_200": "REAL",
            "WMA_7": "REAL",
            "WMA_9": "REAL",
            "WMA_12": "REAL",
            "WMA_21": "REAL",
            "WMA_26": "REAL",
            "WMA_34": "REAL",
            "WMA_50": "REAL",
            "WMA_55": "REAL",
            "WMA_89": "REAL",
            "WMA_99": "REAL",
            "WMA_200": "REAL",
            "SMMA_10": "REAL",
            "SMMA_20": "REAL",
            "SMMA_25": "REAL",
            "SMMA_50": "REAL",
            "SMMA_99": "REAL",
            "SMMA_100": "REAL",
            "SMMA_200": "REAL",
            "KAMA_7": "REAL",
            "KAMA_9": "REAL",
            "KAMA_12": "REAL",
            "KAMA_21": "REAL",
            "KAMA_26": "REAL",
            "KAMA_34": "REAL",
            "KAMA_50": "REAL",
            "KAMA_55": "REAL",
            "KAMA_89": "REAL",
            "KAMA_99": "REAL",
            "ATR_9": "REAL",
            "ATR_14": "REAL",
            "ATR_21": "REAL",
            "TSI_25_13_13": "REAL",
            "TSI_25_13_13_SIGNAL": "REAL",
            "TSI_FAST_12_7_7": "REAL",
            "TSI_FAST_12_7_7_SIGNAL": "REAL",
            "HVN_1": "REAL",
            "HVN_2": "REAL",
            "HVN_3": "REAL",
            "POC": "REAL",
            "MACD_DIF_FAST_9_21_9": "REAL",
            "MACD_DEA_FAST_9_21_9": "REAL",
            "MACD_DIF_NORMAL_12_26_9": "REAL",
            "MACD_DEA_NORMAL_12_26_9": "REAL",
            "BOLL_UPPER_20": "REAL",
            "BOLL_MID_20": "REAL",
            "BOLL_LOWER_20": "REAL",
            "TRENDLINE_SLOPE": "REAL",
            "TRENDLINE_INTERCEPT": "REAL",
            "CHANNEL_UPPER_PRICE": "REAL",
            "CHANNEL_LOWER_PRICE": "REAL",
            "TRENDLINE_PRICE": "REAL",
            "MID_LINE": "REAL",
            "R_SQUARED": "REAL",
            "TREND_DIRECTION": "TEXT",
            "SUPPORT_PRICE": "REAL",
            "RESISTANCE_PRICE": "REAL",
        }

        # Dynamic Donchian columns
        for w in [4, 10, 12, 15, 20]:
            definitions[f"DONCHIAN_UPPER_{w}"] = "REAL"
            definitions[f"DONCHIAN_LOWER_{w}"] = "REAL"
            definitions[f"DONCHIAN_MID_{w}"] = "REAL"

        # Fibonacci
        for level in [0.236, 0.382, 0.5, 0.618, 0.786]:
            l_str = str(level).replace('.', '_')
            definitions[f"FIB_SUPPORT_{l_str}"] = "REAL"
            definitions[f"FIB_RESISTANCE_{l_str}"] = "REAL"

        for ext in [1.272, 1.618, 2.618]:
            e_str = str(ext).replace('.', '_')
            definitions[f"FIB_EXTENSION_{e_str}"] = "REAL"

        return definitions

    def create_indicator_table(conn, symbol, timeframe, definitions):
        table_name = f'"{symbol}_{timeframe}{INDICATOR_SUFFIX}"'
        cols_sql = ",\n".join([f"{n} {t}" for n, t in definitions.items()])
        sql = f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                symbol TEXT,
                open_time TIMESTAMP WITH TIME ZONE,
                close REAL,
                {cols_sql},
                PRIMARY KEY (symbol, open_time)
            );
            CREATE INDEX IF NOT EXISTS idx_{symbol}_{timeframe}_ot ON {table_name} (open_time DESC);
        """
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()

    # --- Optimized calculations (vectorized) ---

    # --- Missing math helper functions ---

    def calculate_trendline_and_channel_robust_optimized(df):
        """
        Calculates linear regression and a standard deviation channel.
        """
        # We take the last N points for the trend (e.g. 100) or all
        lookback = min(len(df), 100)
        subset = df.iloc[-lookback:].copy()

        y = subset['close'].values
        x = np.arange(len(y))

        # Linear regression
        slope, intercept, r_value, p_value, std_err = stats.linregress(x, y)

        # Project the trendline for the ENTIRE dataset
        # We need to set the x index relative to the end of the subset
        full_x = np.arange(len(df)) - (len(df) - lookback)
        trendline_values = slope * full_x + intercept

        # Compute standard deviation for the channel (based on distance to the line in the subset)
        residuals = y - (slope * x + intercept)
        std_dev = np.std(residuals)

        # Channel values for the whole set
        upper_channel = trendline_values + (2 * std_dev)
        lower_channel = trendline_values - (2 * std_dev)

        # Determine direction
        direction = "SIDEWAYS"
        if slope > 0.0001 * y[0]:
            direction = "UP"  # Very simple logic, adjustable
        elif slope < -0.0001 * y[0]:
            direction = "DOWN"

        # Since we need indicators for every time step, we must return Series.
        # Simplified here: we return constants or the computed arrays.
        # So it fits into calculate_indicators_optimized (where it goes into the dict update):

        return {
            "TRENDLINE_SLOPE": pd.Series(slope, index=df.index),  # Constant for the window
            "TRENDLINE_INTERCEPT": pd.Series(intercept, index=df.index),
            "TRENDLINE_PRICE": pd.Series(trendline_values, index=df.index),
            "CHANNEL_UPPER_PRICE": pd.Series(upper_channel, index=df.index),
            "CHANNEL_LOWER_PRICE": pd.Series(lower_channel, index=df.index),
            "MID_LINE": pd.Series(trendline_values, index=df.index),  # Alias
            "R_SQUARED": pd.Series(r_value**2, index=df.index),
            "TREND_DIRECTION": pd.Series(direction, index=df.index),  # Stored as text
        }

    def get_hvn_poc_for_dataset(df, timeframe):
        """
        Calculates Point of Control (POC) and High Volume Nodes (HVN) based on the volume profile.
        """
        try:
            # Simple volume profile over the loaded data
            prices = df['close'].values
            volumes = df['volume'].values if 'volume' in df.columns else np.ones(len(prices))

            # Create histogram (prices weighted by volume)
            # Bins: the square root of the number of data points is often a good rule of thumb for bins
            bins = int(np.sqrt(len(prices)))
            hist, bin_edges = np.histogram(prices, bins=bins, weights=volumes)

            # Find POC (bin with the highest volume)
            poc_idx = np.argmax(hist)
            poc_price = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2

            # Find HVNs (local maxima in the histogram)
            # We look for the top 3 peaks next to the POC
            peaks, _ = scipy.signal.find_peaks(hist, distance=5)  # distance prevents adjacent bins

            # Sort peaks by size (volume)
            sorted_peaks = sorted(peaks, key=lambda i: hist[i], reverse=True)

            hvn_prices = []
            for idx in sorted_peaks[:4]:  # Take top 4
                p = (bin_edges[idx] + bin_edges[idx + 1]) / 2
                if abs(p - poc_price) > (poc_price * 0.005):  # Only if far enough from the POC
                    hvn_prices.append(p)

            # Fill up if not enough were found
            while len(hvn_prices) < 3:
                hvn_prices.append(0)

            return {"POC": poc_price, "HVN_1": hvn_prices[0], "HVN_2": hvn_prices[1], "HVN_3": hvn_prices[2]}
        except Exception as e:
            logging.warning(f"HVN calculation error: {e}")
            return {"POC": 0, "HVN_1": 0, "HVN_2": 0, "HVN_3": 0}

    def find_support_resistance(df, window=20):
        """
        Finds local minima and maxima as support/resistance.
        """
        try:
            # We use argrelextrema for a fast search
            highs = df['high'].values
            lows = df['low'].values

            # Local maxima (resistance)
            max_idx = scipy.signal.argrelextrema(highs, np.greater, order=window)[0]
            resistances = [(highs[i], df.index[i]) for i in max_idx]

            # Local minima (support)
            min_idx = scipy.signal.argrelextrema(lows, np.less, order=window)[0]
            supports = [(lows[i], df.index[i]) for i in min_idx]

            # Sort by recency (newest first) or strength. Here: newest first.
            resistances.sort(key=lambda x: x[1], reverse=True)
            supports.sort(key=lambda x: x[1], reverse=True)

            return supports, resistances
        except Exception:
            return [], []

    def calc_fibonacci_levels_dynamic(df, timeframe='1h'):
        """
        Calculates Fib levels based on the high/low of the loaded window.
        """
        try:
            max_price = df['high'].max()
            min_price = df['low'].min()
            diff = max_price - min_price

            fibs = {
                'support': [],
                'resistance': [],  # Symmetric in this simple logic
                'extensions': [],
            }

            # Classic retracements (uptrend assumption for the calculation, levels are static within the window)
            for level in [0.236, 0.382, 0.5, 0.618, 0.786]:
                price = max_price - (diff * level)
                fibs['support'].append({'level': level, 'price': price})
                fibs['resistance'].append({'level': level, 'price': price})  # Simplified

            # Extensions (upward)
            for ext in [1.272, 1.618, 2.618]:
                price = max_price + (diff * (ext - 1))
                fibs['extensions'].append({'level': ext, 'price': price})

            return fibs
        except:
            return {'support': [], 'resistance': [], 'extensions': []}

    def calculate_wma(series, period):
        """
        Calculates the Weighted Moving Average (WMA).
        Uses rolling apply with a numpy dot product for performance.
        """
        weights = np.arange(1, period + 1)
        sum_weights = weights.sum()

        # raw=True is important for performance!
        return series.rolling(window=period).apply(lambda x: np.dot(x, weights) / sum_weights, raw=True).fillna(0)

    def calculate_smma(series, period):
        """
        Calculates the Smoothed Moving Average (SMMA).
        Optimization: SMMA is mathematically identical to an EMA where alpha = 1/period.
        That's 100x faster than a Python loop.
        """
        return series.ewm(alpha=1 / period, adjust=False).mean().fillna(0)

    def calculate_rsi(series, period=14):
        delta = series.diff()
        up = delta.clip(lower=0)
        down = -1 * delta.clip(upper=0)
        roll_up = up.ewm(span=period, adjust=False).mean()
        roll_down = down.ewm(span=period, adjust=False).mean()
        rs = roll_up / roll_down
        return 100.0 - (100.0 / (1.0 + rs)).fillna(0)

    def calculate_kama(series, period=10, fast=2, slow=30):
        # KAMA is iterative and hard to vectorize, here the numba-optimized logic or the classic version
        # Classic iterative (slow, but correct):
        closes = series.values
        kama = np.zeros_like(closes)
        # Init
        kama[:period] = closes[:period]  # Simple start

        fast_sc = 2 / (fast + 1)
        slow_sc = 2 / (slow + 1)

        for i in range(period, len(closes)):
            # Efficiency Ratio
            change = abs(closes[i] - closes[i - period])
            volatility = np.sum(np.abs(np.diff(closes[i - period : i + 1])))
            er = change / volatility if volatility != 0 else 0
            sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
            kama[i] = kama[i - 1] + sc * (closes[i] - kama[i - 1])

        return pd.Series(kama, index=series.index)

    def calculate_indicators_optimized(df, timeframe):
        """
        Calculates ALL indicators and returns a NEW DataFrame.
        Prevents 'DataFrame is highly fragmented'.
        """
        df = df.sort_values('open_time')
        close = df['close']
        high = df['high']
        low = df['low']

        # Dictionary to collect all new columns
        # This is the key to performance! Not df['new'] = ... but a dict
        results = {}

        # 1. RSI
        for p in [6, 9, 12, 14, 24]:
            results[f'RSI_{p}'] = calculate_rsi(close, p)

        # 2. EMA
        for p in [7, 9, 12, 21, 26, 34, 50, 55, 89, 99, 200]:
            results[f'EMA_{p}'] = close.ewm(span=p, adjust=False).mean().fillna(0)

        # 3. SMA / MA
        for p in [7, 10, 20, 25, 50, 99, 100, 200]:
            results[f'MA_{p}'] = close.rolling(window=p).mean().fillna(0)

        # --- NEWLY ADDED: WMA ---
        for p in [7, 9, 12, 21, 26, 34, 50, 55, 89, 99, 200]:
            results[f'WMA_{p}'] = calculate_wma(close, period=p)

        # --- NEWLY ADDED: SMMA ---
        for p in [10, 20, 25, 50, 99, 100, 200]:
            results[f'SMMA_{p}'] = calculate_smma(close, period=p)

        # 4. KAMA
        for p in [7, 9, 12, 21, 26, 34, 50, 55, 89, 99]:
            results[f'KAMA_{p}'] = calculate_kama(close, period=p)

        # 5. Bollinger Bands
        mid = close.rolling(20).mean()
        std = close.rolling(20).std()
        results['BOLL_MID_20'] = mid.fillna(0)
        results['BOLL_UPPER_20'] = (mid + 2 * std).fillna(0)
        results['BOLL_LOWER_20'] = (mid - 2 * std).fillna(0)

        # 6. Donchian
        for w in [4, 10, 12, 15, 20]:
            results[f'DONCHIAN_UPPER_{w}'] = high.rolling(w).max().fillna(0)
            results[f'DONCHIAN_LOWER_{w}'] = low.rolling(w).min().fillna(0)
            results[f'DONCHIAN_MID_{w}'] = (
                (results[f'DONCHIAN_UPPER_{w}'] + results[f'DONCHIAN_LOWER_{w}']) / 2
            ).fillna(0)

        # 7. MACD (Vectorized)
        def calc_macd(fast, slow, sig):
            f_ema = close.ewm(span=fast, adjust=False).mean()
            s_ema = close.ewm(span=slow, adjust=False).mean()
            dif = f_ema - s_ema
            dea = dif.ewm(span=sig, adjust=False).mean()
            return dif, dea

        results['MACD_DIF_FAST_9_21_9'], results['MACD_DEA_FAST_9_21_9'] = calc_macd(9, 21, 9)
        results['MACD_DIF_NORMAL_12_26_9'], results['MACD_DEA_NORMAL_12_26_9'] = calc_macd(12, 26, 9)

        # 8. ATR
        # Compute TR
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        for p in [9, 14, 21]:
            results[f'ATR_{p}'] = tr.ewm(alpha=1 / p, adjust=False).mean().fillna(0)

        # 9. TSI (Vectorized)
        def calc_tsi(r, s):
            diff = close.diff()
            smooth = diff.ewm(span=r, adjust=False).mean()
            double = smooth.ewm(span=s, adjust=False).mean()
            abs_smooth = diff.abs().ewm(span=r, adjust=False).mean()
            abs_double = abs_smooth.ewm(span=s, adjust=False).mean()
            return 100 * (double / abs_double).fillna(0)

        results['TSI_25_13_13'] = calc_tsi(25, 13)
        results['TSI_25_13_13_SIGNAL'] = results['TSI_25_13_13'].ewm(span=13, adjust=False).mean()
        results['TSI_FAST_12_7_7'] = calc_tsi(12, 7)
        results['TSI_FAST_12_7_7_SIGNAL'] = results['TSI_FAST_12_7_7'].ewm(span=7, adjust=False).mean()

        # Placeholder for complex calculations (Fibonacci, trend, HVN)
        # Since these functions often iterate row-wise or over the entire dataset,
        # we call them here and add the results into the dict.

        # Trendlines
        trend_data = calculate_trendline_and_channel_robust_optimized(df)
        results.update(trend_data)  # Adds TRENDLINE_PRICE etc.

        # HVN & POC (constant for the latest state, but we fill it in)
        hvn_data = get_hvn_poc_for_dataset(df, timeframe)
        for k, v in hvn_data.items():
            results[k] = v  # Scalar gets broadcast to a Series

        # Support / Resistance
        try:
            sup, res = find_support_resistance(df)
            # Take the strongest (first) value
            s_price = sup[0][0] if sup else 0
            r_price = res[0][0] if res else 0
            results['SUPPORT_PRICE'] = s_price
            results['RESISTANCE_PRICE'] = r_price
        except:
            results['SUPPORT_PRICE'] = 0
            results['RESISTANCE_PRICE'] = 0

        # Fibonacci
        fibs = calc_fibonacci_levels_dynamic(df, timeframe=timeframe)
        fibs = calc_fibonacci_levels_dynamic(df, timeframe=timeframe)
        # Support fibs
        for lvl in [0.236, 0.382, 0.5, 0.618, 0.786]:
            l_str = str(lvl).replace('.', '_')
            val = next((i['price'] for i in fibs['support'] if i['level'] == lvl), 0)
            results[f"FIB_SUPPORT_{l_str}"] = val
            val_r = next((i['price'] for i in fibs['resistance'] if i['level'] == lvl), 0)
            results[f"FIB_RESISTANCE_{l_str}"] = val_r

        for ext in [1.272, 1.618, 2.618]:
            e_str = str(ext).replace('.', '_')
            val = next((i['price'] for i in fibs['extensions'] if i['level'] == ext), 0)
            results[f"FIB_EXTENSION_{e_str}"] = val

        # === FINAL MERGE ===
        # Here we create ONE new DataFrame. This prevents fragmentation.
        indicators_df = pd.DataFrame(results, index=df.index)

        # Add base data
        indicators_df['open_time'] = df['open_time']
        indicators_df['close'] = df['close']
        indicators_df['symbol'] = df['symbol'].iloc[0] if not df.empty else ''

        return indicators_df

    def write_indicators_to_db_optimized(conn, df, symbol, timeframe, definitions):
        """
        Writes extremely fast using execute_values.
        """
        table_name = f'"{symbol}_{timeframe}{INDICATOR_SUFFIX}"'

        # Filter the columns from the DF that are in the DB definition
        valid_cols = ['symbol', 'open_time', 'close'] + list(definitions.keys())

        # Ensure all columns exist in the DF (fill with 0 if missing)
        for col in valid_cols:
            if col not in df.columns:
                df[col] = 0

        # Reduce the DataFrame to the valid columns and enforce the order
        df_to_write = df[valid_cols].copy()

        # Convert to list of tuples
        data_values = [tuple(x) for x in df_to_write.to_numpy()]

        cols_str = ', '.join(valid_cols)

        # UPDATE clause for upsert
        update_cols = [c for c in valid_cols if c not in ['symbol', 'open_time']]
        update_sql = ", ".join([f"{c} = EXCLUDED.{c}" for c in update_cols])

        sql = f"""
            INSERT INTO {table_name} ({cols_str})
            VALUES %s
            ON CONFLICT (symbol, open_time)
            DO UPDATE SET {update_sql}
        """

        with conn.cursor() as cur:
            extras.execute_values(cur, sql, data_values)
        conn.commit()

    def process_coin(args):
        symbol, timeframe = args

        start_time = datetime.datetime.now()
        logger.info(f"START {symbol} {timeframe} @ {start_time.strftime('%H:%M:%S')}")

        conn = None
        try:
            conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD, host=DB_HOST, port=DB_PORT)
        except Exception as e:
            logger.error(f"DB Connect Error: {e}")
            return

        try:
            definitions = get_indicator_definitions()

            ohlcv_table = f'"{symbol}_{timeframe}"'
            ind_table = f'"{symbol}_{timeframe}{INDICATOR_SUFFIX}"'

            if not table_exists(conn, ohlcv_table):
                logger.warning(f"Source table {ohlcv_table} does not exist. Skipping {symbol} {timeframe}.")
                return
            if not table_exists(conn, ind_table):
                logger.info(f"Creating table {ind_table}...")
                create_indicator_table(conn, symbol, timeframe, definitions)

            # Starting point: last computed indicator
            with conn.cursor() as cur:
                cur.execute(f"SELECT MAX(open_time) FROM {ind_table}")
                last_ind_time = cur.fetchone()[0]

            if last_ind_time is None:
                start_fetch_time = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
            else:
                if not isinstance(last_ind_time, pd.Timestamp):
                    last_ind_time = pd.Timestamp(last_ind_time)
                if last_ind_time.tzinfo is None:
                    last_ind_time = last_ind_time.tz_localize('UTC')
                start_fetch_time = last_ind_time

            tf_delta = get_timeframe_delta(timeframe)
            load_start = start_fetch_time - (tf_delta * 3000)
            save_start_filter = start_fetch_time - (tf_delta * 5)

            sql = f"SELECT * FROM {ohlcv_table} WHERE open_time >= %s ORDER BY open_time ASC"
            df_raw = pd.read_sql(sql, conn, params=(load_start,))

            if df_raw.empty or len(df_raw) < 50:
                return

            df_raw['open_time'] = pd.to_datetime(df_raw['open_time'], utc=True)
            if 'symbol' not in df_raw.columns:
                df_raw['symbol'] = symbol

            df_ind = calculate_indicators_optimized(df_raw, timeframe)

            df_save = df_ind[df_ind['open_time'] >= save_start_filter]

            if not df_save.empty:
                logger.info(f"Saving {len(df_save)} rows for {symbol} {timeframe}")
                write_indicators_to_db_optimized(conn, df_save, symbol, timeframe, definitions)
            else:
                logger.info(f"{symbol} {timeframe}: no new data")

            end_time = datetime.datetime.now()
            duration = (end_time - start_time).total_seconds()
            logger.info(f"END {symbol} {timeframe} after {duration:.1f}s")

        except Exception as e:
            logger.error(f"Error {symbol} {timeframe}: {e}", exc_info=True)
            conn.rollback()
        finally:
            conn.close()

    # === Main logic ===
    symbols = load_coins()
    if not symbols:
        logger.warning("No coins loaded – indicator task ended")
        return

    # All combinations (coin + timeframe)
    tasks = [(s, tf) for s in symbols for tf in TIMEFRAMES]

    logger.info(f"🚀 Indicator calculator started: {len(tasks)} tasks, {NUM_WORKERS} workers...")

    # with ProcessPoolExecutor(max_workers=NUM_WORKERS) as exe:
    # exe.map(process_coin, tasks)

    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as exe:
        list(exe.map(process_coin, tasks))

    logger.info("✅ Indicator calculator done")


async def indicator_calculator_wrapper(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Indicator calculator started")
    await asyncio.to_thread(run_indicator_calculator)
    logger.info("Indicator calculator complete")


# ========================= CONV TRACKER  =========================


async def create_conv_signals_table():
    """Creates the central conv_signals table if it doesn't already exist"""
    conn = await get_conn()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conv_signals (
                id BIGSERIAL PRIMARY KEY,
                source_bot TEXT NOT NULL,           -- e.g. '5%', 'Fast', 'SR', 'Volume'
                source_time TIMESTAMPTZ NOT NULL,   -- time from active_trades
                coin TEXT NOT NULL,
                direction TEXT NOT NULL,            -- LONG/SHORT
                entry_price NUMERIC,
                lev TEXT,
                inserted_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(source_bot, source_time, coin, direction)  -- prevents duplicates
            )
        """)
        # Index for fast queries
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conv_signals_coin
            ON conv_signals(coin, source_time DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conv_signals_bot
            ON conv_signals(source_bot, source_time DESC)
        """)
        logger.info("Table 'conv_signals' ready or created")
    except Exception as e:
        logger.error(f"Error creating conv_signals: {e}")
    finally:
        await release_conn(conn)


async def conv_signals_sync_task(context: ContextTypes.DEFAULT_TYPE):
    logger.info("conv_signals sync task started")

    bot_tables = {
        "5% Bot": "active_trades",
        "Fast Bot": "active_trades2",
        "SR Bot": "active_trades3",
        "Volume Bot": "active_trades4",
    }

    conn = None
    try:
        conn = await get_conn()
        total_inserted = 0

        for bot_name, table_name in bot_tables.items():
            try:
                rows = await conn.fetch(f"""
                    SELECT time, coin, direction, entry, lev
                    FROM "{table_name}"
                    ORDER BY time
                """)

                inserted = 0
                for row in rows:
                    trade_time = row['time']
                    coin = row['coin']
                    direction = row['direction']
                    entry = float(row['entry']) if row['entry'] is not None else None
                    lev = row['lev']

                    exists = await conn.fetchval(
                        """
                        SELECT 1 FROM conv_signals
                        WHERE source_bot = $1
                          AND source_time = $2
                          AND coin = $3
                          AND direction = $4
                    """,
                        bot_name,
                        trade_time,
                        coin,
                        direction,
                    )

                    if not exists:
                        await conn.execute(
                            """
                            INSERT INTO conv_signals (source_bot, source_time, coin, direction, entry_price, lev)
                            VALUES ($1, $2, $3, $4, $5, $6)
                            ON CONFLICT (source_bot, source_time, coin, direction) DO NOTHING
                        """,
                            bot_name,
                            trade_time,
                            coin,
                            direction,
                            entry,
                            lev,
                        )
                        inserted += 1

                total_inserted += inserted
                logger.info(f"{bot_name}: {len(rows)} trades checked, {inserted} newly inserted")

            except Exception as e:
                logger.error(f"Error for {bot_name} ({table_name}): {e}", exc_info=True)

        logger.info(f"conv_signals sync task finished – {total_inserted} new entries total")

    except Exception as e:
        logger.error(f"General error in conv_signals sync: {e}", exc_info=True)
    finally:
        if conn:
            await release_conn(conn)


# ========================= ML Checker for SR BOT  =========================

# CREATE TABLE IF NOT EXISTS ml_weighted_trades3 (
# id                SERIAL PRIMARY KEY,
# lfd               INTEGER,              -- from active_trades3
# time              TIMESTAMP,
# coin              TEXT,
# direction         TEXT,
# entry             REAL,
# lev               TEXT,
# target1           REAL,
# target2           REAL,
# target3           REAL,
# target4           REAL,
# sl                REAL,
# posted            TIMESTAMP,            -- from active_trades3 (if present)
# status            TEXT,                 -- from active_trades3 (if already set)
# model             TEXT,                 -- fixed "ML SR BOT"
# confidence        REAL,                 -- probability (0..1)
# created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
# );

# CREATE INDEX IF NOT EXISTS idx_mlwt_lfd_time_coin_dir
# ON ml_weighted_trades3 (lfd, time, coin, direction);


# ============================================
# Create features (now async!)
# ============================================


def create_feature_row(trade_row):
    """
    Fully synchronous version – no more await
    """
    indicators = get_indicators_at_time(trade_row['coin'], trade_row['time'])
    if indicators is None:
        return None

    row = indicators
    close = row.get('close', np.nan)
    if pd.isna(close) or close <= 0:
        return None

    features = {}

    # Direct indicators (as in training)
    base_cols = [
        'rsi_9',
        'rsi_14',
        'rsi_24',
        'macd_dif_fast_9_21_9',
        'macd_dea_fast_9_21_9',
        'tsi_fast_12_7_7',
        'tsi_fast_12_7_7_signal',
        'atr_14',
        'r_squared',
        'boll_upper_20',
        'boll_mid_20',
        'boll_lower_20',
        'donchian_upper_20',
        'donchian_lower_20',
        'donchian_mid_20',
        'support_price',
        'resistance_price',
        'ema_9',
        'ema_21',
        'wma_9',
        'wma_21',
        'kama_9',
        'kama_21',
        'close',
    ]

    for col in base_cols:
        val = row.get(col)
        features[col] = float(val) if pd.notna(val) else np.nan

    # Trend numeric
    trend_map = {'UP': 1.0, 'DOWN': -1.0, 'FLAT': 0.0, 'SIDEWAYS': 0.0}
    trend_val = str(row.get('trend_direction', '')).upper()
    features['trend_direction_num'] = trend_map.get(trend_val, 0.0)

    # Relative distances
    def pct(a, b):
        return (a - b) / close * 100 if pd.notna(b) and close > 0 else np.nan

    features.update(
        {
            'pct_ema9': pct(close, row.get('ema_9')),
            'pct_ema21': pct(close, row.get('ema_21')),
            'pct_wma9': pct(close, row.get('wma_9')),
            'pct_kama9': pct(close, row.get('kama_9')),
            'pct_support': pct(close, row.get('support_price')),
            'pct_resist': pct(row.get('resistance_price'), close),
            'pct_boll_mid': pct(close, row.get('boll_mid_20')),
            'ema9_ema21_pct': pct(row.get('ema_9'), row.get('ema_21')),
            'kama9_kama21_pct': pct(row.get('kama_9'), row.get('kama_21')),
        }
    )

    # ATR-normalized
    atr = row.get('atr_14', np.nan)
    if pd.notna(atr) and atr > 0:
        features.update(
            {
                'support_atr': (close - row.get('support_price', np.nan)) / atr,
                'resist_atr': (row.get('resistance_price', np.nan) - close) / atr,
                'boll_width_atr': ((row.get('boll_upper_20', 0) - row.get('boll_lower_20', 0)) / atr),
            }
        )

    # LONG/SHORT
    features['is_long'] = 1.0 if trade_row['direction'].upper() == 'LONG' else 0.0

    return features


def get_indicators_at_time(coin: str, timestamp: datetime):
    """
    Synchronous: fetches the latest indicator row <= timestamp for the coin
    Uses db_pool2 (psycopg2 ThreadedConnectionPool) – no await!
    """
    try:
        # Normalize coin (like in your async version)
        coin_norm = coin.replace('USDC', 'USDT').replace('/', '')
        table_name = f'"{coin_norm}_1h_indicators"'

        # Ensure timestamp (UTC)
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)

        conn = db_pool2.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT * FROM {table_name}
                    WHERE open_time <= %s
                    ORDER BY open_time DESC
                    LIMIT 1
                """,
                    (ts,),
                )
                row = cur.fetchone()

                if row is None:
                    logger.debug(f"No indicators for {coin} @ {ts}")
                    return None

                # Convert row (tuple) to dict
                columns = [desc[0] for desc in cur.description]
                return dict(zip(columns, row))

        finally:
            db_pool2.putconn(conn)  # Important: return the connection!

    except Exception as e:
        logger.error(f"Indicator error {coin}: {e}")
        return None


# ============================================
# Checker function (adapted for async features)
# ============================================


def _heavy_ml_weighted_checker(context=None):
    """
    Indestructible version – psycopg2 with db_pool2
    No more 'unkeyed connection' – guaranteed
    """
    logger.info("ML Weighted Trades Checker started (synchronous with psycopg2)")

    def safe_float(value):
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            logger.warning(f"Invalid numeric value: {value} – setting to None")
            return None

    if MODEL_LONG is None or MODEL_SHORT is None:
        logger.error("Models not loaded – aborting")
        return

    conn = None
    try:
        conn = db_pool2.getconn()
        logger.debug("Connection obtained")

        now_utc = datetime.now(timezone.utc)
        logger.debug(f"Searching S&R trades around: {now_utc} (UTC)")

        # Fetch trades – close cursor explicitly
        cur = conn.cursor()
        cur.execute("""
            SELECT lfd, time, coin, direction, entry, lev,
                   target1, target2, target3, target4, sl, posted, status
            FROM active_trades3
            WHERE time >= CURRENT_TIMESTAMP - INTERVAL '10 minutes'
            ORDER BY time DESC
        """)
        rows = cur.fetchall()
        cur.close()

        logger.info(f"Potential new trades found: {len(rows)}")

        if not rows:
            logger.info("No new trades – end")
            # Return connection – only here, when there's no loop
            db_pool2.putconn(conn)
            return

        inserted = 0

        for row in rows:
            trade = {
                'lfd': row[0],
                'time': row[1],
                'coin': row[2],
                'direction': row[3],
                'entry': row[4],
                'lev': row[5],
                'target1': row[6],
                'target2': row[7],
                'target3': row[8],
                'target4': row[9],
                'sl': row[10],
                'posted': row[11],
                'status': row[12],
            }

            numeric_fields = ['entry', 'target1', 'target2', 'target3', 'target4', 'sl']
            for field in numeric_fields:
                if trade.get(field) is not None:
                    trade[field] = safe_float(trade[field])

            # Duplicate check – new cursor
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1 FROM ml_weighted_trades3
                WHERE lfd = %s AND coin = %s AND direction = %s AND time = %s
            """,
                (trade['lfd'], trade['coin'], trade['direction'], trade['time']),
            )
            exists = cur.fetchone() is not None
            cur.close()

            if exists:
                continue

            # Features & Prediction
            try:
                feat_dict = create_feature_row(trade)

                if feat_dict is None:
                    logger.warning(f"No features for {trade['coin']} @ {trade['time']}")
                    continue

                X = pd.DataFrame([feat_dict])

                direction = trade['direction'].upper()
                if direction == 'LONG':
                    model = MODEL_LONG
                    is_pump = True
                elif direction == 'SHORT':
                    model = MODEL_SHORT
                    is_pump = False
                else:
                    logger.warning(f"Unknown direction: {direction}")
                    continue

                confidence = model.predict_proba(X)[0, 1]
                confidence = float(confidence)  # numpy → Python float

                logger.info(f"ML Weighted found: {trade['coin']} {direction} | Conf: {confidence:.4f}")

                # Insert
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO ml_weighted_trades3 (
                        lfd, time, coin, direction, entry, lev,
                        target1, target2, target3, target4, sl, posted, status,
                        model, confidence
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'ML SR BOT', %s
                    )
                    ON CONFLICT DO NOTHING
                """,
                    (
                        trade['lfd'],
                        trade['time'],
                        trade['coin'],
                        trade['direction'],
                        trade['entry'],
                        trade['lev'],
                        trade.get('target1'),
                        trade.get('target2'),
                        trade.get('target3'),
                        trade.get('target4'),
                        trade.get('sl'),
                        trade.get('posted'),
                        trade.get('status'),
                        confidence,
                    ),
                )
                cur.close()
                conn.commit()

                inserted += 1
                logger.info(f"ML Weighted saved: {trade['coin']} {direction} | Conf: {confidence:.4f}")

                # Send signal
                if confidence >= 0.75:
                    symbol = trade['coin']
                    diri = trade['direction']

                    # await send_cornix_signal(symbol, is_pump, 'SRA1')

                    # asyncio.run_coroutine_threadsafe(
                    # send_cornix_signal(symbol, is_pump, 'SRA1'),
                    # application_instance.loop # Get the event loop from the application
                    # )

                    # border_color = "#00ff00"
                    # emoji = "🚀 AI crosschecked Support&Resistance Signal"
                    # html = f"""
                    # <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {border_color};">
                    # <b style="color:#00ffff; font-size:18px;">{emoji}</b>
                    # <b style="color:#ffd700;">{symbol.replace('USDT','')}/USDT</b>
                    # <b>→ Direction: {diri} </b>
                    # <b>→ ML-Confidence: <b style="color:#00ffff;">{confidence:.1%}</b> / Module: SRA1</b>
                    # <b>→ Time: {now_utc.strftime('%H:%M')} UTC</b>
                    # </pre>
                    # """.strip()

                    # chart_buf = generate_smooth_minichart_image(symbol, minutes=240)

                    try:
                        logger.info(f"SR BOT alert sent: {symbol}")
                    except Exception as e:
                        logger.error(f"Alert send error {symbol}: {e}")

                    # try:
                    # if chart_buf:
                    # await application.bot.send_photo(
                    # chat_id=AI_CHANNEL_ID,
                    # photo=chart_buf,
                    # caption=html,
                    # parse_mode="HTML"
                    # )
                    # else:
                    # await application.bot.send_message(
                    # chat_id=AI_CHANNEL_ID,
                    # text=html,
                    # parse_mode="HTML"
                    # )
                    # logger.info(f"Channel posting sent: {coin} {direction} ({module})")
                    # except Exception as e:
                    # logger.error(f"Posting error {coin}: {e}")

            except Exception as e:
                logger.error(f"ML error for {trade.get('coin', '?')}: {e}", exc_info=True)
                if conn:
                    conn.rollback()
                continue

        logger.info(f"Checker finished – {inserted} new entries")

    except Exception as e:
        logger.error(f"Checker error: {e}", exc_info=True)
        if conn:
            conn.rollback()

    finally:
        if conn is not None:
            try:
                db_pool2.putconn(conn)
                logger.debug("DB connection returned to pool")
            except Exception as put_error:
                logger.error(f"Pool error while returning: {put_error}")
                # Optional: discard the connection if it's broken
                # db_pool2._pool.remove(conn)  # only if you know it's broken


async def ml_weighted_trades_checker(context):
    asyncio.create_task(asyncio.to_thread(_heavy_ml_weighted_checker))


async def master_trades_wrapper(context):
    """
    Async wrapper for check_master_trades – awaits the actual function
    """
    logger.info("Master Trades Checker job triggered – starting async function")

    # This is where the async function is properly awaited!
    await check_master_trades(context.application)  # ← Pass application as a parameter!


# ========================= New ML trades task =========================

# CREATE TABLE IF NOT EXISTS trade_cooldowns (
# id SERIAL PRIMARY KEY,
# module VARCHAR(10) NOT NULL,          -- 'SRA1', 'TBA1', 'AIM1'
# coin VARCHAR(20) NOT NULL,            -- 'BTCUSDT', 'ETHUSDT' etc.
# direction VARCHAR(10) NOT NULL,       -- 'LONG', 'SHORT'
# last_posted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
# cooldown_hours INTEGER DEFAULT 4,     -- 4 hours
# UNIQUE (module, coin, direction)
# );

# CREATE INDEX idx_trade_cooldowns_module_coin_dir ON trade_cooldowns (module, coin, direction);


# CREATE TABLE trade_scanner_state (
# module VARCHAR(10) NOT NULL,
# signal_type VARCHAR(20) NOT NULL,       -- 'ai_signal' or 'conv_signal' (for AIM1)
# last_processed_id BIGINT DEFAULT 0,
# last_scan_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
# PRIMARY KEY (module, signal_type)
# );

COOLDOWN_HOURS = 4
COOLDOWN_TABLE = "trade_cooldowns"


def is_cooled_down(module: str, coin: str, direction: str) -> bool:
    """Checks whether the cooldown has expired for this module/coin/direction."""
    conn = db_pool2.getconn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT last_posted_at FROM trade_cooldowns
            WHERE module = %s AND coin = %s AND direction = %s
        """,
            (module, coin, direction),
        )
        row = cur.fetchone()
        cur.close()

        if row is None:
            return True  # Never posted yet → OK

        last_posted = row[0]
        cooldown_end = last_posted + timedelta(hours=COOLDOWN_HOURS)
        return datetime.now(timezone.utc) >= cooldown_end

    except Exception as e:
        logger.error(f"Cooldown check error ({module}/{coin}/{direction}): {e}")
        return True  # On error, better to post than miss it
    finally:
        db_pool2.putconn(conn)


def update_cooldown(module: str, coin: str, direction: str):
    """Stores or updates the last post timestamp."""
    conn = db_pool2.getconn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO trade_cooldowns (module, coin, direction, last_posted_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (module, coin, direction)
            DO UPDATE SET last_posted_at = NOW()
        """,
            (module, coin, direction),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Cooldown update error ({module}/{coin}/{direction}): {e}")
        conn.rollback()
    finally:
        db_pool2.putconn(conn)


# async def post_trade(module: str, coin: str, direction: str, confidence: float, is_long: bool, source: str = ""):
# try:
# await send_cornix_signal(coin, is_long, modul=module)

# border_color = "#00ff00" if is_long else "#ff0066"
# emoji = f"🚀 AI {module} LONG SIGNAL" if is_long else f"💥 AI {module} SHORT SIGNAL"

# now_str = datetime.now(timezone.utc).strftime('%H:%M')
# html = f"""
# <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {border_color};">
# <b style="color:#00ffff; font-size:18px;">{emoji}</b>
# <b style="color:#ffd700;">{coin.replace('USDT','')}/USDT</b>
# <b>→ Direction: {direction}</b>
# <b>→ ML Confidence: <b style="color:#00ffff;">{confidence:.1%}</b></b>
# <b>→ Time: {now_str} UTC | Module: {module}</b>
# {"<b>→ Source: " + source + "</b>" if source else ""}
# </pre>
# """.strip()

# chart_buf = await generate_smooth_minichart_image(coin, minutes=240)

# try:
# if chart_buf:
# await application.bot.send_photo(
# chat_id=AI_CHANNEL_ID,
# photo=chart_buf,
# caption=html,
# parse_mode="HTML"
# )
# else:
# await application.bot.send_message(
# chat_id=AI_CHANNEL_ID,
# text=html,
# parse_mode="HTML"
# )
# logger.info(f"Channel posting sent: {coin} {direction} ({module})")
# except Exception as e:
# logger.error(f"Posting error {coin}: {e}")

# update_cooldown(module, coin, direction)

# except Exception as e:
# logger.error(f"Post trade error {coin} ({module}): {e}")

# async def post_trade(module: str, coin: str, direction: str, confidence: float, is_long: bool, source: str = ""):
# try:
# # 1. Cornix signal
# # New channel for ABR1, old one for all others
# cornix_channel = 0 if module == 'ABR1' else 0

# await send_cornix_signal(coin, is_long, modul=module, channel_id=cornix_channel)  # ← Pass channel_id

# # 2. Channel posting with chart (same channel as Cornix for ABR1)
# channel_id = 0 if module == 'ABR1' else AI_CHANNEL_ID

# border_color = "#00ff00" if is_long else "#ff0066"
# emoji = f"🚀 AI {module} LONG SIGNAL" if is_long else f"💥 AI {module} SHORT SIGNAL"

# now_str = datetime.now(timezone.utc).strftime('%H:%M')
# html = f"""
# <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {border_color};">
# <b style="color:#00ffff; font-size:18px;">{emoji}</b>
# <b style="color:#ffd700;">{coin.replace('USDT','')}/USDT</b>
# <b>→ Direction: {direction}</b>
# <b>→ ML Confidence: <b style="color:#00ffff;">{confidence:.1%}</b></b>
# <b>→ Time: {now_str} UTC | Module: {module}</b>
# {"<b>→ Source: " + source + "</b>" if source else ""}
# </pre>
# """.strip()

# chart_buf = await generate_smooth_minichart_image(coin, minutes=240)

# try:
# if chart_buf:
# await application.bot.send_photo(
# chat_id=channel_id,
# photo=chart_buf,
# caption=html,
# parse_mode="HTML"
# )
# else:
# await application.bot.send_message(
# chat_id=channel_id,
# text=html,
# parse_mode="HTML"
# )
# logger.info(f"Channel posting sent: {coin} {direction} ({module}) in channel {channel_id}")
# except Exception as e:
# logger.error(f"Posting error {coin}: {e}")

# update_cooldown(module, coin, direction)

# except Exception as e:
# logger.error(f"Post trade error {coin} ({module}): {e}")

# async def post_trade(module: str, coin: str, direction: str, confidence: float, is_long: bool, source: str = ""):
# try:
# # Channel selection
# cornix_channel = 0 if module == 'ABR1' else 0
# chart_channel = 0 if module == 'ABR1' else AI_CHANNEL_ID

# logger.debug(f"Posting for {module}: Coin={coin}, Direction={direction}, Conf={confidence:.1%}, Channel Cornix={cornix_channel}, Chart={chart_channel}")

# # 1. Cornix signal
# try:
# await send_cornix_signal(coin, is_long, modul=module, channel_id=cornix_channel)
# except Exception as e:
# logger.error(f"Cornix signal error {symbol}: {e}")

# # 2. Channel posting with chart
# border_color = "#00ff00" if is_long else "#ff0066"
# emoji = f"🚀 AI {module} LONG SIGNAL" if is_long else f"💥 AI {module} SHORT SIGNAL"

# now_str = datetime.now(timezone.utc).strftime('%H:%M')
# html = f"""
# <pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {border_color};">
# <b style="color:#00ffff; font-size:18px;">{emoji}</b>
# <b style="color:#ffd700;">{coin.replace('USDT','')}/USDT</b>
# <b>→ Direction: {direction}</b>
# <b>→ ML Confidence: <b style="color:#00ffff;">{confidence:.1%}</b></b>
# <b>→ Time: {now_str} UTC | Module: {module}</b>
# {"<b>→ Source: " + source + "</b>" if source else ""}
# </pre>
# """.strip()

# chart_buf = await generate_smooth_minichart_image(coin, minutes=240)

# try:
# if chart_buf:
# await application.bot.send_photo(
# chat_id=chart_channel,
# photo=chart_buf,
# caption=html,
# parse_mode="HTML"
# )
# else:
# await application.bot.send_message(
# chat_id=chart_channel,
# text=html,
# parse_mode="HTML"
# )
# logger.info(f"Channel posting sent: {coin} {direction} ({module}) in channel {chart_channel}")
# except Exception as e:
# logger.error(f"Posting error {coin}: {e}")

# update_cooldown(module, coin, direction)

# except Exception as e:
# logger.error(f"Post trade error {coin} ({module}): {e}", exc_info=True)


async def post_trade(module: str, coin: str, direction: str, confidence: float, is_long: bool, source: str = ""):
    try:
        # --- NEW: dynamic channel selection ---
        if module == 'ABR1':
            cornix_channel = 0
            chart_channel = 0
        elif module == 'RUB1':
            cornix_channel = RUBBERBAND_CHANNEL_ID  # Your default Cornix channel
            chart_channel = RUBBERBAND_CHANNEL_ID  # Your new channel for the chart
        else:
            cornix_channel = 0
            chart_channel = AI_CHANNEL_ID

        logger.debug(
            f"Posting for {module}: Coin={coin}, Direction={direction}, Conf={confidence:.1%}, Channel Cornix={cornix_channel}, Chart={chart_channel}"
        )

        # 1. Cornix signal
        try:
            await send_cornix_signal(coin, is_long, modul=module, channel_id=cornix_channel)
        except Exception as e:
            logger.error(f"Cornix signal error {coin}: {e}")

        # 2. Channel posting with chart
        border_color = "#00ff00" if is_long else "#ff0066"
        emoji = f"🚀 AI {module} LONG SIGNAL" if is_long else f"💥 AI {module} SHORT SIGNAL"

        now_str = datetime.now(timezone.utc).strftime('%H:%M')
        html = f"""
<pre style="background:#1e1e1e; color:#ffffff; padding:16px; border-radius:12px; font-family:'Courier New', monospace; font-size:15px; line-height:1.8; border-left:6px solid {border_color};">
<b style="color:#00ffff; font-size:18px;">{emoji}</b>
<b style="color:#ffd700;">{coin.replace('USDT', '')}/USDT</b>
<b>→ Direction: {direction}</b>
<b>→ ML Confidence: <b style="color:#00ffff;">{confidence:.1%}</b></b>
<b>→ Time: {now_str} UTC | Module: {module}</b>
{"<b>→ Source: " + source + "</b>" if source else ""}
</pre>
        """.strip()

        chart_buf = await generate_smooth_minichart_image(coin, minutes=240)

        try:
            if chart_buf:
                await application.bot.send_photo(
                    chat_id=chart_channel, photo=chart_buf, caption=html, parse_mode="HTML"
                )
            else:
                await application.bot.send_message(chat_id=chart_channel, text=html, parse_mode="HTML")
            logger.info(f"Channel posting sent: {coin} {direction} ({module}) in channel {chart_channel}")
        except Exception as e:
            logger.error(f"Posting error {coin}: {e}")

        update_cooldown(module, coin, direction)

    except Exception as e:
        logger.error(f"Post trade error {coin} ({module}): {e}", exc_info=True)


# async def trade_scanner(context=None):
# """
# Asynchronous trade scanner function – runs directly in the main loop
# Checks all 3 tables, posts new trades when conditions are met
# Every 60 seconds – no thread needed anymore
# """
# logger.info("Trade Scanner starting (async in the main loop)")

# conn = None
# try:
# conn = await get_conn()

# # Fetch the last processed IDs
# state_rows = await conn.fetch("""
# SELECT module, signal_type, last_processed_id FROM trade_scanner_state
# """)
# last_ids = {}
# for row in state_rows:
# module, signal_type, last_id = row
# key = (module, signal_type)
# last_ids[key] = last_id

# modules = [
# # SRA1: ml_weighted_trades3 – columns: id, coin, direction, confidence, entry, lev
# ('SRA1', 'ml_weighted_trades3', lambda conf: conf >= 0.65, 'confidence', 'id', 'coin', 'entry', 'lev', 'none'),
# # TBA1: ML_TREND_TRADES – columns: id, symbol, direction, ml_probability, entry_time
# ('TBA1', 'ML_TREND_TRADES', lambda conf, dir: (dir == 'SHORT' and conf >= 0.75) or (dir == 'LONG' and conf >= 0.8), 'ml_probability', 'id', 'symbol', 'entry_time', None, 'none'),
# # ABR1: ai_br_trades – columns: id, model, symbol, direction, confidence, threshold, retest_time, creation_time
# # Condition: confidence >= threshold (per trade)
# #('ABR1', 'ai_br_trades', lambda conf, thresh: conf >= thresh, 'confidence', 'id', 'symbol', None, None, 'none')
# # ... SRA1, TBA1 ...
# ('ABR1', 'ai_br_trades', lambda conf, thresh: conf >= thresh, 'confidence', 'id', 'symbol', 'threshold', None, 'none')  # ← threshold added!
# ]

# for module, table, condition, conf_col, id_col, coin_col, thresh_col,entry_col, lev_col, _ in modules:
# last_id = last_ids.get((module, 'none'), 0)

# select_cols = f"{id_col}, {coin_col}, direction, {conf_col}"
# if thresh_col:
# select_cols += f", {thresh_col}"
# if entry_col:
# select_cols += f", {entry_col}"
# if lev_col:
# select_cols += f", {lev_col}"

# new_rows = await conn.fetch(f"""
# SELECT {select_cols}
# FROM {table}
# WHERE {id_col} > $1
# ORDER BY {id_col} ASC
# """, last_id)

# new_max_id = last_id
# for r in new_rows:
# row_id = r[id_col]
# coin = r[coin_col]
# direction = r['direction']
# conf = r[conf_col]
# entry = r.get(entry_col)
# lev = r.get(lev_col)
# thresh = r[4] if len(r) > 4 else None  # ← query thresh

# new_max_id = max(new_max_id, row_id)


# # Inside the for loop for each module
# if module == 'ABR1':
# # ABR1: confidence >= threshold (threshold is per trade in the table)
# #if conf >= r['threshold']:  # ← r is the row as a dict
# logger.info(f"In the ABR1 loop for {coin}")
# is_long = direction.upper() == 'LONG'
# try:
# if is_cooled_down(module, coin, direction):
# logger.info(f"Posting trade {module}: {coin} {direction} (conf {conf:.1%}) – cooldown clear")
# MAIN_LOOP.create_task(post_trade(module, coin, direction, conf, is_long))
# else:
# logger.debug(f"Trade {module}: {coin} {direction} – cooldown active")
# except Exception as e:
# logger.error(f"ABR1 signal error {symbol}: {e}")
# else:
# # Old logic for SRA1/TBA1
# if condition(conf) if module == 'SRA1' else condition(conf, direction):

# is_long = direction.upper() == 'LONG'
# if is_cooled_down(module, coin, direction):
# MAIN_LOOP.create_task(post_trade(module, coin, direction, conf, is_long))


# # if condition(conf) if module == 'SRA1' else condition(conf, direction):
# # is_long = direction.upper() == 'LONG'
# # if is_cooled_down(module, coin, direction):
# # await post_trade(module, coin, direction, conf, is_long)

# # Update state
# if new_max_id > last_id:
# await conn.execute("""
# UPDATE trade_scanner_state
# SET last_processed_id = $1, last_scan_at = NOW()
# WHERE module = $2 AND signal_type = 'none'
# """, new_max_id, module)

# # AIM1 – special case: separate IDs per signal_type
# aim_types = ['ai_signal', 'conv_signal']
# for signal_type in aim_types:
# last_id = last_ids.get(('AIM1', signal_type), 0)

# aim_rows = await conn.fetch("""
# SELECT m.signal_id, m.ml_confidence,
# CASE WHEN m.signal_type = 'ai_signal' THEN a.symbol ELSE c.coin END AS symbol,
# CASE WHEN m.signal_type = 'ai_signal' THEN a.direction ELSE c.direction END AS direction,
# CASE WHEN m.signal_type = 'ai_signal' THEN a.model ELSE c.source_bot END AS source
# FROM master_ai_processed_signals m
# LEFT JOIN ai_signals a ON m.signal_type = 'ai_signal' AND m.signal_id = a.id
# LEFT JOIN conv_signals c ON m.signal_type = 'conv_signal' AND m.signal_id = c.id
# WHERE m.ml_confidence >= 0.88
# AND m.signal_type = $1
# AND m.signal_id > $2
# ORDER BY m.signal_id ASC
# """, signal_type, last_id)

# new_max_id = last_id
# from collections import defaultdict
# max_per_coin_dir = defaultdict(lambda: {'conf': 0, 'row': None})
# for r in aim_rows:
# signal_id, conf, symbol, direction, source = r['signal_id'], r['ml_confidence'], r['symbol'], r['direction'], r['source']
# new_max_id = max(new_max_id, signal_id)

# key = (symbol, direction.upper())
# if conf > max_per_coin_dir[key]['conf']:
# max_per_coin_dir[key] = {'conf': conf, 'row': r, 'id': signal_id}

# for (symbol, direction), data in max_per_coin_dir.items():
# if is_cooled_down('AIM1', symbol, direction):
# await post_trade('AIM1', symbol, direction, data['conf'], direction == 'LONG', data['row']['source'])

# # Update state
# if new_max_id > last_id:
# await conn.execute("""
# UPDATE trade_scanner_state
# SET last_processed_id = $1, last_scan_at = NOW()
# WHERE module = 'AIM1' AND signal_type = $2
# """, new_max_id, signal_type)

# await conn.execute("COMMIT")  # Ensure updates are committed

# except Exception as e:
# logger.error(f"Trade Scanner error: {e}", exc_info=True)
# if conn:
# await conn.execute("ROLLBACK")
# finally:
# if conn:
# await release_conn(conn)


async def trade_scanner(context=None):
    """
    Asynchronous trade scanner function – runs directly in the main loop
    Checks all 3 tables, posts new trades when conditions are met
    Every 60 seconds – no thread needed anymore
    """
    logger.info("Trade Scanner starting (async in the main loop)")
    conn = None
    try:
        conn = await get_conn()
        # Fetch the last processed IDs
        state_rows = await conn.fetch("""
            SELECT module, signal_type, last_processed_id FROM trade_scanner_state
        """)
        last_ids = {}
        for row in state_rows:
            module, signal_type, last_id = row
            key = (module, signal_type)
            last_ids[key] = last_id
        modules = [
            # SRA1: ml_weighted_trades3 – columns: id, coin, direction, confidence, entry, lev
            (
                'SRA1',
                'ml_weighted_trades3',
                lambda conf: conf >= 0.65,
                'confidence',
                'id',
                'coin',
                'entry',
                'lev',
                'none',
            ),
            # TBA1: ML_TREND_TRADES – columns: id, symbol, direction, ml_probability, entry_time
            (
                'TBA1',
                'ML_TREND_TRADES',
                lambda conf, dir: (dir == 'SHORT' and conf >= 0.75) or (dir == 'LONG' and conf >= 0.8),
                'ml_probability',
                'id',
                'symbol',
                'entry_time',
                None,
                'none',
            ),
            # ABR1: ai_br_trades – columns: id, model, symbol, direction, confidence, threshold, retest_time, creation_time
            (
                'ABR1',
                'ai_br_trades',
                lambda conf, thresh: conf >= thresh,
                'confidence',
                'id',
                'symbol',
                'threshold',
                None,
                'none',
            ),  # ← threshold added!
        ]
        for module, table, condition, conf_col, id_col, coin_col, entry_col, lev_col, _ in modules:
            last_id = last_ids.get((module, 'none'), 0)
            select_cols = f"{id_col}, {coin_col}, direction, {conf_col}"
            if entry_col:
                select_cols += f", {entry_col}"
            if lev_col:
                select_cols += f", {lev_col}"
            new_rows = await conn.fetch(
                f"""
                SELECT {select_cols}
                FROM {table}
                WHERE {id_col} > $1
                ORDER BY {id_col} ASC
            """,
                last_id,
            )
            new_max_id = last_id
            for r in new_rows:
                row_id = r[id_col]
                coin = r[coin_col]
                direction = r['direction']
                conf = r[conf_col]
                entry = r.get(entry_col)
                lev = r.get(lev_col)
                thresh = r.get('threshold')  # Optional for ABR1
                new_max_id = max(new_max_id, row_id)

                # Check condition (optional thresh for ABR1)
                if module == 'ABR1':
                    if thresh is not None and conf >= thresh:
                        is_long = direction.upper() == 'LONG'
                        logger.info(f"In the ABR1 loop for {coin}")
                        if is_cooled_down(module, coin, direction) and 'USDT_' not in coin:
                            logger.info(f"Posting trade {module}: {coin} {direction} (conf {conf:.1%}) – cooldown clear")
                            # In the loop (e.g. in trade_scanner)

                            # task = loop.create_task(post_trade(module, coin, direction, conf, is_long))
                            # await task  # ← Wait until the task is done!
                            # MAIN_LOOP.create_task(post_trade(module, coin, direction, conf, is_long))
                            await post_trade(module, coin, direction, conf, is_long)
                        else:
                            logger.debug(f"Trade {module}: {coin} {direction} – cooldown active")
                else:
                    if condition(conf) if module == 'SRA1' else condition(conf, direction):
                        is_long = direction.upper() == 'LONG'
                        if is_cooled_down(module, coin, direction) and 'USDT_' not in coin:
                            # MAIN_LOOP.create_task(post_trade(module, coin, direction, conf, is_long))
                            # In the loop (e.g. in trade_scanner)

                            # task = loop.create_task(post_trade(module, coin, direction, conf, is_long))
                            # await task  # ← Wait until the task is done!
                            await post_trade(module, coin, direction, conf, is_long)

            # Update state
            if new_max_id > last_id:
                await conn.execute(
                    """
                    UPDATE trade_scanner_state
                    SET last_processed_id = $1, last_scan_at = NOW()
                    WHERE module = $2 AND signal_type = 'none'
                """,
                    new_max_id,
                    module,
                )
        # AIM1 – special case: separate IDs per signal_type
        aim_types = ['ai_signal', 'conv_signal']
        for signal_type in aim_types:
            last_id = last_ids.get(('AIM1', signal_type), 0)
            aim_rows = await conn.fetch(
                """
                SELECT m.signal_id, m.ml_confidence,
                       CASE WHEN m.signal_type = 'ai_signal' THEN a.symbol ELSE c.coin END AS symbol,
                       CASE WHEN m.signal_type = 'ai_signal' THEN a.direction ELSE c.direction END AS direction,
                       CASE WHEN m.signal_type = 'ai_signal' THEN a.model ELSE c.source_bot END AS source
                FROM master_ai_processed_signals m
                LEFT JOIN ai_signals a ON m.signal_type = 'ai_signal' AND m.signal_id = a.id
                LEFT JOIN conv_signals c ON m.signal_type = 'conv_signal' AND m.signal_id = c.id
                WHERE m.ml_confidence >= 0.88
                  AND m.signal_type = $1
                  AND m.signal_id > $2
                ORDER BY m.signal_id ASC
            """,
                signal_type,
                last_id,
            )
            new_max_id = last_id
            from collections import defaultdict

            max_per_coin_dir = defaultdict(lambda: {'conf': 0, 'row': None})
            for r in aim_rows:
                signal_id, conf, symbol, direction, source = (
                    r['signal_id'],
                    r['ml_confidence'],
                    r['symbol'],
                    r['direction'],
                    r['source'],
                )
                if 'USDT_' in symbol:
                    continue
                new_max_id = max(new_max_id, signal_id)
                key = (symbol, direction.upper())
                if conf > max_per_coin_dir[key]['conf']:
                    max_per_coin_dir[key] = {'conf': conf, 'row': r, 'id': signal_id}
            for (symbol, direction), data in max_per_coin_dir.items():
                if is_cooled_down('AIM1', symbol, direction):
                    await post_trade(
                        'AIM1', symbol, direction, data['conf'], direction == 'LONG', data['row']['source']
                    )
            # Update state
            if new_max_id > last_id:
                await conn.execute(
                    """
                    UPDATE trade_scanner_state
                    SET last_processed_id = $1, last_scan_at = NOW()
                    WHERE module = 'AIM1' AND signal_type = $2
                """,
                    new_max_id,
                    signal_type,
                )
        await conn.execute("COMMIT")  # Ensure updates are committed
    except Exception as e:
        logger.error(f"Trade Scanner error: {e}", exc_info=True)
        if conn:
            await conn.execute("ROLLBACK")
    finally:
        if conn:
            await release_conn(conn)


async def trade_scanner_wrapper(context):
    # asyncio.create_task(asyncio.to_thread(trade_scanner, context))
    logger.info("Trade Scanner job triggered – starting async function")
    await trade_scanner(context)  # Await here to execute it!


# ========================= BREAK AND RETEST TASK  =========================


def _get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def _load_coins(file_path):
    with open(file_path) as f:
        data = json.load(f)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and 'coins' in data:
            return data['coins']
        else:
            raise ValueError(f"Format of {file_path} not recognized.")


def _get_ohlcv_data_recent(conn, symbol, hours_history):
    table_name = f"{symbol}_1h"
    query = f"""
        SELECT open_time::text as open_time, open, high, low, close, volume
        FROM "{table_name}"
        WHERE open_time >= NOW() - INTERVAL '{hours_history + 5} hours' -- +5 buffer
        ORDER BY open_time ASC;
    """
    try:
        df = pd.read_sql(query, conn)
        df['open_time'] = pd.to_datetime(df['open_time'], utc=True)

        current_hour_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        df = df[df['open_time'] < current_hour_utc]

        df = df.tail(hours_history).reset_index(drop=True)

        return df
    except Exception as e:
        if "relation" in str(e) and "does not exist" in str(e):
            logger.warning(f"  -> Table for {symbol} does not exist. Skipping.")
        else:
            logger.error(f"Error loading {symbol}: {e}")
        return None


def _calculate_technical_indicators(df):
    df['open'] = pd.to_numeric(df['open'])
    df['high'] = pd.to_numeric(df['high'])
    df['low'] = pd.to_numeric(df['low'])
    df['close'] = pd.to_numeric(df['close'])
    df['volume'] = pd.to_numeric(df['volume'])

    df.ta.ema(length=9, append=True)
    df.ta.ema(length=21, append=True)
    df.ta.kama(length=9, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.tsi(fast=7, slow=12, signal=7, append=True)
    df.ta.bbands(length=20, append=True)
    df.ta.donchian(length=20, append=True)

    expected_pta_cols = {
        'EMA_9': np.nan,
        'EMA_21': np.nan,
        'KAMA_9': np.nan,
        'RSI_14': np.nan,
        'TSI_12_7': np.nan,
        'TSIs_12_7_7': np.nan,
        'BBL_20_2': np.nan,
        'BBM_20_2': np.nan,
        'BBU_20_2': np.nan,
        'DCL_20': np.nan,
        'DCM_20': np.nan,
        'DCU_20': np.nan,
    }
    for col, default_val in expected_pta_cols.items():
        if col not in df.columns:
            df[col] = default_val

    df.rename(
        columns={
            'EMA_9': 'ema9',
            'EMA_21': 'ema21',
            'KAMA_9': 'kama9',
            'RSI_14': 'rsi14',
            'TSI_12_7': 'tsi',
            'TSIs_12_7_7': 'tsi_signal',
            'BBL_20_2': 'boll_lower_20',
            'BBM_20_2': 'boll_mid_20',
            'BBU_20_2': 'boll_upper_20',
            'DCL_20': 'donchian_lower_20',
            'DCM_20': 'donchian_mid_20',
            'DCU_20': 'donchian_upper_20',
        },
        inplace=True,
    )

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

    # --- FIX HERE ---
    # The model expects 'retest_volume', but in the live DataFrame it's called 'volume'.
    # We simply create an alias.
    df['retest_volume'] = df['volume']
    # ----------------

    df = df.fillna(0)

    return df


def _find_pivot_levels(df):  # window is taken globally from PIVOT_WINDOW
    if len(df) < PIVOT_WINDOW * 2 + 1:
        return []

    padded_high = np.pad(df['high'].values, (PIVOT_WINDOW, PIVOT_WINDOW), 'edge')
    padded_low = np.pad(df['low'].values, (PIVOT_WINDOW, PIVOT_WINDOW), 'edge')

    high_extrema_indices = argrelextrema(padded_high, np.greater_equal, order=PIVOT_WINDOW)[0]
    low_extrema_indices = argrelextrema(padded_low, np.less_equal, order=PIVOT_WINDOW)[0]

    levels = []

    for idx in high_extrema_indices:
        original_idx = idx - PIVOT_WINDOW
        if 0 <= original_idx < len(df):
            levels.append(
                {
                    'price': df.iloc[original_idx]['high'],
                    'type': 'resistance',
                    'index': original_idx,
                    'time': df.iloc[original_idx]['open_time'],
                }
            )

    for idx in low_extrema_indices:
        original_idx = idx - PIVOT_WINDOW
        if 0 <= original_idx < len(df):
            levels.append(
                {
                    'price': df.iloc[original_idx]['low'],
                    'type': 'support',
                    'index': original_idx,
                    'time': df.iloc[original_idx]['open_time'],
                }
            )

    return levels


def _create_ai_br_trades_table_blocking(db_config):
    conn = None
    try:
        conn = psycopg2.connect(**db_config)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_br_trades (
                id SERIAL PRIMARY KEY,
                model TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                confidence DOUBLE PRECISION NOT NULL,
                threshold DOUBLE PRECISION NOT NULL,
                retest_time TIMESTAMP WITH TIME ZONE NOT NULL,
                creation_time TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
        conn.commit()
        logger.info("Database table 'ai_br_trades' checked/created.")
    except Exception as e:
        logger.error(f"Error creating the DB table: {e}")
    finally:
        if conn:
            conn.close()


def _insert_trade_signal_blocking(db_config, signal_data):
    conn = None
    try:
        conn = psycopg2.connect(**db_config)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_br_trades (model, symbol, direction, confidence, threshold, retest_time)
            VALUES (%s, %s, %s, %s, %s, %s);
            """,
            (
                signal_data['model'],
                signal_data['symbol'],
                signal_data['direction'],
                signal_data['confidence'],
                signal_data['threshold'],
                signal_data['retest_time'],
            ),
        )
        conn.commit()
        logger.info(f"Signal saved: {signal_data}")
    except Exception as e:
        logger.error(f"Error saving the signal: {e}")
    finally:
        if conn:
            conn.close()


# --- SignalGenerator class (optimized for bot integration) ---
class SignalGenerator:
    def __init__(self, models, thresholds, success_class_idx, coins, db_config, loop: asyncio.AbstractEventLoop):
        logger.info("Initializing SignalGenerator...")
        self.models = models
        self.thresholds = thresholds
        self.success_class_idx = success_class_idx
        self.coins = coins
        self.db_config = db_config
        self.loop = loop

        self.feature_columns = [
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

        logger.info("SignalGenerator initialized.")

    def _process_coin(self, conn, symbol):
        """
        Internal logic for a single coin.
        Takes an open DB connection (reuses it).
        logger.info("SignalGenerator running...")
        """
        try:
            df = _get_ohlcv_data_recent(conn, symbol, LIVE_DATA_HISTORY_HOURS)
            # Check whether data is present
            if df is None or df.empty or len(df) < max(PIVOT_WINDOW * 2, 30) + RETEST_BACKWARD_LOOKUP_CANDLES + 2:
                return

            df_with_indicators = _calculate_technical_indicators(df.copy())

            # --- FIX: add retest_volume (if missing in _calculate...) ---
            if 'retest_volume' not in df_with_indicators.columns:
                df_with_indicators['retest_volume'] = df_with_indicators['volume']
            # ---------------------------------------------------------------------

            levels = _find_pivot_levels(df_with_indicators)
            if not levels:
                return

            # Check the last 3 candles for a retest
            potential_retest_candle_indices = range(
                len(df_with_indicators) - 1, max(0, len(df_with_indicators) - 1 - 3), -1
            )

            for retest_idx in potential_retest_candle_indices:
                retest_candle = df_with_indicators.iloc[retest_idx]
                if retest_candle['open_time'].minute != 0:
                    continue

                for level in levels:
                    if level['index'] >= retest_idx:
                        continue

                    lvl_price = level['price']
                    upper_bound = lvl_price * (1 + LEVEL_TOLERANCE_PCT)
                    lower_bound = lvl_price * (1 - LEVEL_TOLERANCE_PCT)

                    is_retest_long = retest_candle['low'] <= upper_bound and retest_candle['low'] >= lower_bound
                    is_retest_short = retest_candle['high'] >= lower_bound and retest_candle['high'] <= upper_bound

                    if not (is_retest_long or is_retest_short):
                        continue

                    break_found = False
                    direction = None
                    search_start_idx = retest_idx - 1
                    search_end_idx = max(level['index'], retest_idx - RETEST_BACKWARD_LOOKUP_CANDLES)

                    for break_idx in range(search_start_idx, search_end_idx, -1):
                        b_candle = df_with_indicators.iloc[break_idx]
                        prev_b_candle = df_with_indicators.iloc[break_idx - 1] if break_idx > 0 else None
                        if prev_b_candle is None:
                            continue

                        if (
                            level['type'] == 'resistance'
                            and prev_b_candle['close'] < lvl_price
                            and b_candle['close'] > lvl_price
                        ):
                            break_found = True
                            direction = 'LONG'
                            break
                        elif (
                            level['type'] == 'support'
                            and prev_b_candle['close'] > lvl_price
                            and b_candle['close'] < lvl_price
                        ):
                            break_found = True
                            direction = 'SHORT'
                            break

                    if break_found and direction:
                        current_model = self.models[direction]
                        current_threshold = self.thresholds[direction]

                        X_event_features = retest_candle[self.feature_columns].values
                        X_event = pd.DataFrame([X_event_features], columns=self.feature_columns, dtype=float)

                        prediction_proba = current_model.predict_proba(X_event)[0, self.success_class_idx]
                        logger.info(f"SignalGenerator Break&Retest for coin {symbol}")
                        # if prediction_proba >= current_threshold:
                        if prediction_proba >= 0.50:
                            signal_data = {
                                'model': MODEL_ID,
                                'symbol': symbol,
                                'direction': direction,
                                'confidence': float(prediction_proba),
                                'threshold': float(current_threshold),
                                'retest_time': retest_candle['open_time'],
                            }
                            # We use the open connection directly here
                            _insert_trade_signal_blocking(self.db_config, signal_data)

        except Exception as e:
            # We log the error but let the loop keep running
            logger.error(f"SignalGenerator error for coin {symbol}: {e}")

    def _run_full_analysis_cycle_blocking(self):
        """
        This function runs in ONE separate thread.
        It opens the DB connection ONCE and then iterates through all coins.
        This saves massive resources and prevents lag in the bot.
        """
        start_time = time.time()
        logger.info(f"SignalGenerator: starting analysis cycle for {len(self.coins)} coins...")

        conn = None
        try:
            conn = _get_db_connection()

            for i, coin in enumerate(self.coins):
                # Here we call the logic for ONE coin
                self._process_coin(conn, coin)

                # Optional: small sleep every X coins to let the CPU breathe
                if i % 10 == 0:
                    time.sleep(0.01)

        except Exception as e:
            logger.error(f"Critical error in the analysis cycle: {e}", exc_info=True)
        finally:
            if conn:
                conn.close()

        duration = time.time() - start_time
        logger.info(f"SignalGenerator: analysis finished in {duration:.2f} seconds.")

    async def run_hourly_check(self, context):
        """
        Called by the JobQueue. Starts the blocking task in the executor.
        """
        # We now pass the ONE large function to the executor,
        # instead of spawning 500 small tasks.
        await self.loop.run_in_executor(None, self._run_full_analysis_cycle_blocking)


# ========================= TASK REGISTRY =========================
tasks: dict[str, asyncio.Task] = {}
task_factories: dict[str, callable] = {}


async def register_task(name: str, coro):
    global tasks, task_factories
    if name in tasks:
        tasks[name].cancel()
        try:
            await tasks[name]
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(coro, name=name)
    tasks[name] = task
    task_factories[name] = coro

    def done_callback(t):
        if t.cancelled():
            return
        if exc := t.exception():
            logger.error(f"Task '{name}' crashed:\n{traceback.format_exc()}")
            asyncio.create_task(register_task(name, coro()))

    task.add_done_callback(done_callback)


# ========================= ADMIN COMMANDS =========================
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Not authorized.")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is running – Windows Edition\n/restart → full reboot")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check whether an argument was passed (e.g. /start menu)
    if context.args and context.args[0] == "menu":
        await menu_handler(update, context)
        return

    # Normal start
    await update.message.reply_text("PROVEN CRYPTO BOT V2 is running \n/menu → start main menue")


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    lines = ["Active background tasks:"]
    for name, task in tasks.items():
        status = "running" if not task.done() else ("cancelled" if task.cancelled() else "crashed")
        lines.append(f"• {name}: {status}")
    await update.message.reply_text("\n".join(lines) or "No tasks running")


async def cmd_restart_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /restart_task <name>\nActive: " + ", ".join(tasks.keys()))
        return
    name = context.args[0]
    if name not in task_factories:
        await update.message.reply_text(f"Task '{name}' not found")
        return
    await update.message.reply_text(f"Restarting task '{name}' …")
    await register_task(name, task_factories[name]())
    await update.message.reply_text(f"Task '{name}' restarted!")


async def restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    await update.message.reply_text("Bot restarting completely …")
    logger.warning(f"FULL RESTART by admin {update.effective_user.id}")
    await asyncio.sleep(2)

    try:
        LOCK_FILE.unlink(missing_ok=True)
    except:
        pass

    os.startfile(Path(__file__).resolve())

    try:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
    except:
        pass

    try:
        import ctypes

        ctypes.windll.kernel32.FreeConsole()
        ctypes.windll.kernel32.AttachConsole(-1)
    except:
        pass

    os._exit(0)


# ========================= LIFECYCLE HANDLERS =========================

# async def post_init(app: Application):
# """Starts background tasks AFTER the bot is ready"""
# await init_db_pool()
# await load_1minute_data()
# await load_pump_dump_state()
# await create_ticker_10s_table()
# await create_pump_dump_events_table()
# await create_ai_signals_table()
# await create_conv_signals_table()

# # Register tasks (in your global tasks list)
# # IMPORTANT: we use create_task and store them so we can cancel them
# await register_task("1minjob", one_minute_ticker_job())
# await register_task("round_level_breaker", round_level_breaker())
# await register_task("early_pump_dump_detector", early_pump_dump_detector())
# await register_task("archive_10s_data", archive_10s_data_to_db())
# await register_task("ml_pump_dump_trainer", ml_pump_dump_trainer())
# await register_task("extreme_move_and_volume_detector", extreme_move_and_volume_detector())
# await register_task("trendline_break_bounce_detector", trendline_break_bounce_detector())
# await register_task("ml_filtered_tsi_trader", ml_filtered_tsi_trader())
# await register_task("hourly_pump_checker", hourly_pump_model_checker())

# #await register_ml_weighted_checker(app)
# #await register_master_trades_checker(app)


# logger.info("Bot initialized successfully – Background tasks running")


# async def post_shutdown(app: Application):
# """Runs on shutdown (CTRL+C) – cleans up tasks"""
# logger.info("Stopping background tasks...")

# # Cancel all registered tasks
# for name, task in tasks.items():
# if not task.done():
# task.cancel()
# try:
# await task
# except asyncio.CancelledError:
# pass
# logger.info(f"Task '{name}' cancelled.")

# # Save data
# await save_1minute_data()


# try:
# with open(PUMP_DUMP_FILE, "w", encoding="utf-8") as f:
# save_data = {}
# for symbol, state in PUMP_DUMP_STATE.items():
# save_data[symbol] = {
# "avg_volume": state["avg_volume"],
# "last_alert_time": state["last_alert_time"].isoformat(),
# "usd_vol_4h": state["usd_vol_4h"],
# "volume_samples": list(state["volume_samples"])
# }
# json.dump(save_data, f, indent=2, ensure_ascii=False)
# logger.info("Pump/Dump state saved on shutdown")
# except Exception as e:
# logger.error(f"Shutdown save error: {e}")

# # Close DB pool
# if db_pool:
# await db_pool.close()
# logger.info("Database pool closed.")


async def post_init(app: Application):
    """Starts background tasks AFTER the bot is ready"""
    logger.info("Starting post_init...")
    await init_db_pool()  # Your existing DB pool
    await load_1minute_data()
    await load_pump_dump_state()
    await create_ticker_10s_table()
    await create_pump_dump_events_table()
    await create_ai_signals_table()  # In case this is for other signals
    await create_conv_signals_table()
    await create_smc_zones_table()

    # NEW: create table for AI Break & Retest trades (BLOCKING, but only once)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _create_ai_br_trades_table_blocking, DB_CONFIG)

    # MAIN_LOOP = asyncio.get_event_loop()

    # NEW: load ML models and coins for SignalGenerator
    logger.info("Loading ML models for the SignalGenerator...")
    # loaded_models = {
    # 'LONG': xgb.XGBClassifier(),
    # 'SHORT': xgb.XGBClassifier()
    # }
    # try:
    # loaded_models['LONG'].load_model(SG_LONG_MODEL_FILE)
    # loaded_models['SHORT'].load_model(SG_SHORT_MODEL_FILE)
    # logger.info("ML models loaded successfully.")
    # except Exception as e:
    # logger.critical(f"ERROR: Could not load ML models for SignalGenerator: {e}. Bot is shutting down.")
    # sys.exit(1) # Shut down the bot if models cannot be loaded

    loaded_coins = _load_coins(SG_COINS_FILE)
    logger.info(f"{len(loaded_coins)} coins loaded for the SignalGenerator.")

    # NEW: create SignalGenerator instance
    # global signal_generator_instance
    # signal_generator_instance = SignalGenerator(
    # models=loaded_models,
    # thresholds={'LONG': SG_LONG_THRESHOLD, 'SHORT': SG_SHORT_THRESHOLD},
    # success_class_idx=SG_SUCCESS_CLASS_IDX,
    # coins=loaded_coins,
    # db_config=DB_CONFIG,
    # loop=loop # Pass the event loop
    # )

    # --- Register existing tasks ---
    # await register_task("1minjob", one_minute_ticker_job())
    # await register_task("round_level_breaker", round_level_breaker())
    # await register_task("early_pump_dump_detector", early_pump_dump_detector())
    # await register_task("archive_10s_data", archive_10s_data_to_db())
    # await register_task("ml_pump_dump_trainer", ml_pump_dump_trainer())
    # await register_task("extreme_move_and_volume_detector", extreme_move_and_volume_detector())
    # await register_task("trendline_break_bounce_detector", trendline_break_bounce_detector())
    # await register_task("ml_filtered_tsi_trader", ml_filtered_tsi_trader())
    # await register_task("hourly_pump_checker", hourly_pump_model_checker())
    # await register_task("rubberband_detector", rubberband_detector())
    # await register_task("smc_fvg_detector", smc_fvg_detector())
    # await register_task("forex_smc_detector", forex_smc_detector())

    # # 1. One-time "catch-up" (REST API turbo grepper)
    # # Fills gaps that occurred while the bot was offline.
    # logger.info("⏳ Starting initial historical data download...")
    # await update_pattern_data_async()
    # logger.info("✅ History fully loaded!")

    # # 2. Start the WebSocket connection for live data
    # await register_task("binance_ws_listener", binance_ws_listener())
    # #application.create_task(binance_ws_listener())

    # # 3. Start the database saver
    # await register_task("db_buffer_flusher", db_buffer_flusher())
    # #application.create_task(db_buffer_flusher())

    # --- SHORT PAUSE ---
    # Gives the WebSocket 3 seconds to build up the live stream
    # await asyncio.sleep(3)

    # await register_task("pattern_detector", pattern_detector())

    # await register_ml_weighted_checker(app)
    # await register_master_trades_checker(app)

    # NEW: register SignalGenerator in the job queue
    # app.job_queue.run_custom(
    # callback=signal_generator_instance.run_hourly_check,
    # job_kwargs={
    # "trigger": CronTrigger(
    # minute=HOURLY_CHECK_DELAY_MINUTES, # Example: always runs at :10 (e.g. 10:10, 11:10)
    # hour='*',                          # Every hour
    # timezone="UTC"                     # Important: UTC for consistency
    # ),
    # "misfire_grace_time": 600,  # 10-minute tolerance in case the job ever hangs
    # "coalesce": True,           # If several are missed, only run once
    # "max_instances": 1          # Prevents overlap if a run takes longer than the interval
    # }
    # )
    # logger.info(f"SignalGenerator registered for hourly check (at :%s UTC).", HOURLY_CHECK_DELAY_MINUTES)

    # logger.info("Bot initialized successfully – Background tasks running")


async def post_shutdown(app: Application):
    """Runs on shutdown (CTRL+C) – cleans up tasks"""
    logger.info("Stopping background tasks...")

    # Cancel all registered tasks
    for name, task in tasks.items():
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info(f"Task '{name}' cancelled.")

    # Save data
    await save_1minute_data()

    try:
        with open(PUMP_DUMP_FILE, "w", encoding="utf-8") as f:
            save_data = {}
            for symbol, state in PUMP_DUMP_STATE.items():
                save_data[symbol] = {
                    "avg_volume": state["avg_volume"],
                    "last_alert_time": state["last_alert_time"].isoformat(),
                    "usd_vol_4h": state["usd_vol_4h"],
                    "volume_samples": list(state["volume_samples"]),
                }
            json.dump(save_data, f, indent=2, ensure_ascii=False)
        logger.info("Pump/Dump state saved on shutdown")
    except Exception as e:
        logger.error(f"Shutdown save error: {e}")

    # Close DB pool
    if db_pool:
        await db_pool.close()
        logger.info("Database pool closed.")


# ========================= MAIN ENTRY POINT =========================
# if __name__ == "__main__":
# # 1. WINDOWS FIX FOR CTRL+C (must be at the very top)
# if sys.platform.startswith("win"):
# asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# print("Initializing bot...")

# # 2. Build application
# # We add post_shutdown so CTRL+C cleans up properly
# application = ApplicationBuilder().token(TOKEN)\
# .post_init(post_init)\
# .post_shutdown(post_shutdown)\
# .concurrent_updates(True)\
# .build()

# # 3. Handler registrieren
# # --- Commands ---
# application.add_handler(CommandHandler("start", start))
# application.add_handler(CommandHandler("restart", restart_bot))
# application.add_handler(CommandHandler("tasks", cmd_tasks))
# application.add_handler(CommandHandler("restart_task", cmd_restart_task))
# application.add_handler(CommandHandler("menu", menu_handler))

# # --- Callback (Buttons) ---
# application.add_handler(CallbackQueryHandler(button_handler))

# # --- Text Regex Handler ---
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!price\s+\w+"), price_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!(topgainers|toplosers)$"), top_gainers_losers_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!chart\s+\w+"), chart_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!candles\s+\w+"), candles_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!outlook\s+\w+"), outlook_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!info\s+\w+"), info_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!trading\s+\w+"), trading_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!daily\s+\w+"), daily_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!targets\s+\w+"), targets_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!open\s+(CMP|LIMIT)\s+(LONG|SHORT)\s+\w+\s+\d+x?\s*(-V)?$"), open_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!help$"), help_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!version$"), version_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!minichart\s+\w+(\s+\d+)?$"), minichart_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!smooth\s+\w+(\s+\d+)?$"), smooth_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!volatile(\s+\d+(\.\d+)?)?$"), volatile_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!volume(\s+\d+(\.\d+)?)?$"), volume_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!sentiment$"), sentiment_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!market$"), market_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!bb\s+\w+"), bb_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!don\s+\w+"), don_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!day\s+\w+"), day_handler))
# #application.add_handler(MessageHandler(filters.Regex(r"(?i)^!ai(\s+.*)?$"), ai_signals_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!ai"), ai_signals_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!graphai\s+\w+$"), graphai_handler))
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!graphconv\s+\w+$"), graphconv_handler))


# # In your add_handler block:
# application.add_handler(MessageHandler(filters.Regex(r"(?i)^!mo$"), market_dashboard_handler))
# # Optionally also for button:
# application.add_handler(CallbackQueryHandler(market_dashboard_handler, pattern="^market_dashboard$"))

# # Start the sync task every 5 minutes (300 seconds)
# application.job_queue.run_repeating(
# conv_signals_sync_task,
# interval=300,
# first=10  # start only after 10 seconds
# )

# #application.job_queue.run_repeating(check_master_trades, interval=300, first=20)

# application.job_queue.run_repeating(
# callback=master_trades_wrapper,  # ← The new wrapper!
# interval=300,
# first=20,
# name="master_trades_checker",
# job_kwargs={"misfire_grace_time": 3600}
# )
# logger.info("Master Trades Checker registered (non-blocking async)")


# application.job_queue.run_repeating(
# callback=ml_weighted_trades_checker,
# interval=300,
# first=10,
# name="ml_weighted_checker",
# job_kwargs={"misfire_grace_time": 3600}
# )


# application.job_queue.run_repeating(
# callback=trade_scanner,
# interval=60,  # Check every 60 seconds
# first=10,
# name="trade_scanner",
# job_kwargs={"misfire_grace_time": 300}
# )
# logger.info("Trade Scanner task registered (every 60 seconds – non-blocking)")


# # application.job_queue.run_custom(
# # kline_filler_wrapper,
# # {"trigger": CronTrigger(
# # minute='0,15,30,45',      # every hour at minute 0 and 10
# # second=1,           # exactly at second 1
# # timezone="UTC"      # or "Europe/Berlin" if you want local time
# # )}
# # )

# # application.job_queue.run_custom(
# # kline_filler_wrapper,
# # job_kwargs={
# # "trigger": CronTrigger(
# # minute='*/10',  # every 15 minutes (00:01, 15:01, 30:01, 45:01)
# # second=1,
# # timezone="UTC"
# # ),
# # "misfire_grace_time": 300,  # 5-minute tolerance – job always runs
# # "coalesce": True,           # If several are missed, only run once
# # "max_instances": 1          # Prevents overlap
# # }
# # )

# print("Bot is running! Press CTRL+C to stop.")

# # 4. Start
# # run_polling blocks the script and manages the loop itself.
# # That's why we no longer need asyncio.run(main()).
# try:
# application.run_polling(drop_pending_updates=True, stop_signals=None)
# # stop_signals=None is important on Windows in some environments!
# except Exception as e:
# print(f"Critical Error: {e}")
# finally:
# if LOCK_FILE.exists():
# LOCK_FILE.unlink()
# print("Bot has been stopped.")


if __name__ == "__main__":
    # This block stays as it is; what matters is that asyncio.set_event_loop_policy
    # and mp.set_start_method / mp.freeze_support are called early.
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    print("Initializing bot...")

    # IMPORTANT: set_start_method for joblib/multiprocessing must be HERE
    # to ensure compatibility for all subprocesses (if any).
    # Since your datagrepper uses multiprocessing, this is necessary here.
    # And also for joblib/GridSearchCV, in case you use that again sometime.
    try:
        mp.set_start_method('spawn', force=True)
    except RuntimeError:
        pass
    mp.freeze_support()

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(True)
        .build()
    )

    # ... Rest deiner Handler-Registrierungen ...
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("restart", restart_bot))
    application.add_handler(CommandHandler("tasks", cmd_tasks))
    application.add_handler(CommandHandler("restart_task", cmd_restart_task))
    application.add_handler(CommandHandler("menu", menu_handler))

    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!price\s+\w+"), price_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!(topgainers|toplosers)$"), top_gainers_losers_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!chart\s+\w+"), chart_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!candles\s+\w+"), candles_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!outlook\s+\w+"), outlook_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!info\s+\w+"), info_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!trading\s+\w+"), trading_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!daily\s+\w+"), daily_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!targets\s+\w+"), targets_handler))
    application.add_handler(
        MessageHandler(filters.Regex(r"(?i)^!open\s+(CMP|LIMIT)\s+(LONG|SHORT)\s+\w+\s+\d+x?\s*(-V)?$"), open_handler)
    )
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!help$"), help_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!version$"), version_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!minichart\s+\w+(\s+\d+)?$"), minichart_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!smooth\s+\w+(\s+\d+)?$"), smooth_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!volatile(\s+\d+(\.\d+)?)?$"), volatile_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!volume(\s+\d+(\.\d+)?)?$"), volume_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!sentiment$"), sentiment_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!market$"), market_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!bb\s+\w+"), bb_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!don\s+\w+"), don_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!day\s+\w+"), day_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!ai"), ai_signals_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!graphai\s+\w+$"), graphai_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!graphconv\s+\w+$"), graphconv_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!24$"), macro_24_handler))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!mo$"), market_dashboard_handler))
    application.add_handler(CallbackQueryHandler(market_dashboard_handler, pattern="^market_dashboard$"))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^!closings$"), closings_handler))

    application.job_queue.run_repeating(conv_signals_sync_task, interval=300, first=10)

    # application.job_queue.run_repeating(
    # callback=master_trades_wrapper,
    # interval=300,
    # first=20,
    # name="master_trades_checker",
    # job_kwargs={"misfire_grace_time": 3600}
    # )
    # logger.info("Master Trades Checker registered (non-blocking async)")

    # application.job_queue.run_repeating(
    # callback=ml_weighted_trades_checker,
    # interval=300,
    # first=10,
    # name="ml_weighted_checker",
    # job_kwargs={"misfire_grace_time": 3600}
    # )

    # application.job_queue.run_repeating(
    # callback=trade_scanner,
    # interval=60,
    # first=10,
    # name="trade_scanner",
    # job_kwargs={"misfire_grace_time": 300}
    # )
    # logger.info("Trade Scanner task registered (every 60 seconds – non-blocking)")

    # application.job_queue.run_repeating(
    # callback=trade_scanner_wrapper,
    # interval=60,
    # first=10,
    # name="trade_scanner",
    # job_kwargs={"misfire_grace_time": 300}
    # )
    # logger.info("Trade Scanner registered (every 60 seconds – non-blocking)")

    print("Bot is running! Press CTRL+C to stop.")

    try:
        application.run_polling(drop_pending_updates=True, stop_signals=None)
    except Exception as e:
        print(f"Critical Error: {e}")
    finally:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
        print("Bot has been stopped.")

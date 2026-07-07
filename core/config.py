# core/config.py
# Central configuration for the entire system
# All secrets are read from the .env file (python-dotenv) or environment variables.

import os

from dotenv import load_dotenv

# Load .env file (if present). In production, variables can also be set via
# Systemd/Docker EnvironmentFile or `export VAR=...`.
load_dotenv()


def _required(name: str) -> str:
    """Reads a required environment variable. Raises if missing."""
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Environment variable '{name}' is missing. Please set it in .env or the environment (see .env.example)."
        )
    return val


# --- DATABASE CONFIGURATION ---
DB_NAME = os.getenv("DB_NAME", "cryptodata")
DB_USER = os.getenv("DB_USER", "dbfiller")
DB_PASSWORD = _required("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))

# --- BINANCE API CONFIGURATION ---
BASE_URL = "https://fapi.binance.com"
TIMEFRAMES = ["5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"]
INDICATOR_TIMEFRAMES = ["30m", "1h", "2h", "4h", "1d", "1w"]
NUM_WORKERS = 3

# --- PUMP/DUMP EVENT RETENTION (P1.40) ---
# Single source for two coupled thresholds: the detector's insert gate
# (10_pump_dump_detector.py) and the housekeeping retention DELETE
# (6_housekeeping.py) must use the same values, otherwise the gate silently
# drifts from what the DB keeps.
PUMP_EVENT_MIN_VOL_RATIO = 3.0
PUMP_EVENT_MIN_ABS_PCHG_60S = 1.5

# Optional — only needed by 6_housekeeping.py (leverage update).
# Falls not set, überspringt Housekeeping den Leverage-Refresh.
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")

# --- TELEGRAM CONFIGURATION ---
TELEGRAM_BOT_TOKEN = _required("TELEGRAM_BOT_TOKEN")


# --- CHANNEL IDS (env-driven, D-... never hardcode private channel IDs) ---
def _ch(name: str) -> int:
    """Read a Telegram channel id from the environment (0 = unset/disabled)."""
    return int(os.getenv(name, "0"))


CH_FAST_IN_OUT = _ch("CH_FAST_IN_OUT")
CH_5_PERCENT = _ch("CH_5_PERCENT")
CH_MAIN = _ch("CH_MAIN")
CH_SUPPORT_RESISTANCE = _ch("CH_SUPPORT_RESISTANCE")
CH_VOLUME_INDICATOR = _ch("CH_VOLUME_INDICATOR")
CH_PATTERN_DETECTOR = _ch("CH_PATTERN_DETECTOR")
CH_REGIME_TRADING = _ch("CH_REGIME_TRADING")
CH_MARKET_DATA = _ch("CH_MARKET_DATA")
CH_UFI1 = _ch("CH_UFI1")
CH_PUMP_MARKET = _ch("CH_PUMP_MARKET")
CH_PUMP_AI = _ch("CH_PUMP_AI")
CH_PUMP_MAIN = _ch("CH_PUMP_MAIN")
CH_MIS_8H = _ch("CH_MIS_8H")
CH_MIS_24H = _ch("CH_MIS_24H")
CH_MIS_72H = _ch("CH_MIS_72H")
CH_MIS_168H = _ch("CH_MIS_168H")
CH_ATS = _ch("CH_ATS")
CH_RUBBERBAND = _ch("CH_RUBBERBAND")
CH_ATB_TARGET = _ch("CH_ATB_TARGET")
CH_ATB_INFO = _ch("CH_ATB_INFO")
CH_MASTER = _ch("CH_MASTER")
CH_SMC_METALS = _ch("CH_SMC_METALS")
CH_SMC_FOREX = _ch("CH_SMC_FOREX")
CH_MAYANK = _ch("CH_MAYANK")
CH_ABR1 = _ch("CH_ABR1")
CH_BTC_SMC = _ch("CH_BTC_SMC")
CH_INSTITUTIONAL = _ch("CH_INSTITUTIONAL")
CH_SNIPER_BB = _ch("CH_SNIPER_BB")
CH_SNIPER_TD = _ch("CH_SNIPER_TD")
CH_PATTERN_BR = _ch("CH_PATTERN_BR")
CH_AI_SR = _ch("CH_AI_SR")
CH_PAPER = _ch("CH_PAPER")
CH_DISABLED = _ch("CH_DISABLED")
# Gemeinsamer Channel der Research-Bots 30-33 (PEX1/FMR1/TRM1/FIF1 — Report 15
# S6/S8/S10/S11). Ein Channel für alle vier: die neuen Ideen werden als Kohorte
# beobachtet, Attribution läuft über den Modell-Tag in ai_signals.
CH_NEW_IDEAS = _ch("CH_NEW_IDEAS")


# Per-Bot-Override (Operator 2026-07-07): ungesetzt → Fallback auf den
# Kohorten-Channel. Damit kann ein einzelner Bot (z.B. FMR2 mit eigenem
# Close-Pfad — Cornix' "Close <SYMBOL>" trifft ALLE Trades des Symbols im
# Channel) per .env auf einen eigenen Channel wandern, ohne Code-Deploy.
def _ch_override(name: str, fallback: int) -> int:
    """Wie _ch(), aber mit Fallback NUR bei ungesetzter/leerer Variable.

    Ein explizites `CH_X=0` muss 0 bleiben (die Bots erzwingen bei
    TARGET_CHANNEL_ID == 0 Shadow-only) — `_ch(name) or fallback` würde die
    repo-weite 0=disabled-Semantik schlucken und den Bot still auf den
    Live-Kohorten-Channel zurückfallen lassen.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return fallback
    return int(raw)


CH_PEX1 = _ch_override("CH_PEX1", CH_NEW_IDEAS)
CH_FMR1 = _ch_override("CH_FMR1", CH_NEW_IDEAS)
CH_TRM1 = _ch_override("CH_TRM1", CH_NEW_IDEAS)
CH_FIF1 = _ch_override("CH_FIF1", CH_NEW_IDEAS)


TELEGRAM_CHANNELS = {
    "Fast In And Out": CH_FAST_IN_OUT,
    "5 Percent": CH_5_PERCENT,
    "Main Channel": CH_MAIN,
    "Support Resistance": CH_SUPPORT_RESISTANCE,
    "Volume Indicator": CH_VOLUME_INDICATOR,
    "Pattern Detector": CH_PATTERN_DETECTOR,
}

# --- SPECIAL COIN LISTS ---
# The Main Channel Bot only runs on these coins
MAIN_CHANNEL_COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "LINKUSDT",
    "ADAUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "AVAXUSDT",
    "AAVEUSDT",
    "HBARUSDT",
    "BTCDOMUSDT",
    "ENSUSDT",
    "GRTUSDT",
    "INJUSDT",
    "FETUSDT",
    "ETHBTC",
    "SUIUSDT",
    "PENDLEUSDT",
    "SEIUSDT",
    "ONDOUSDT",
    "TONUSDT",
    "ETHFIUSDT",
    "ENAUSDT",
    "TAOUSDT",
    "RENDERUSDT",
    "BRETTUSDT",
    "EIGENUSDT",
    "IPUSDT",
    "HYPEUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "APTUSDT",
    "CRVUSDT",
    "SOLUSDT",
    "UNIUSDT",
    "NEARUSDT",
    "JUPUSDT",
    "BERAUSDT",
]


# --- REGIME ORCHESTRATOR CHANNELS ---
# Trading channel: Cornix listens EXCLUSIVELY to this channel.
# All old bot channels must be removed from the Cornix config,
# otherwise trades will be triggered twice on Binance.
REGIME_TRADING_CHANNEL_ID = CH_REGIME_TRADING

# Status channel: regime-change alerts, hourly status posts, daily cross-tables.
# Informational only — Cornix does not listen here. Uses the Sentiment Tracker channel.
REGIME_STATUS_CHANNEL_ID = CH_MARKET_DATA

# --- UFI1 BOT CHANNEL ---
# Fibonacci Inversion SHORT bot (rule-based, 1D charts, ≥60% swings)
# Backtest: WR 54.2%, avg +0.83R, total +278R (535 coins, 1 year)
UFI1_CHANNEL_ID = CH_UFI1

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
# If not set, housekeeping skips the leverage refresh.
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")

# --- TELEGRAM CONFIGURATION ---
TELEGRAM_BOT_TOKEN = _required("TELEGRAM_BOT_TOKEN")


# --- CHANNEL IDS (env-driven, D-... never hardcode private channel IDs) ---
def _ch(name: str) -> int:
    """Read a Telegram channel id from the environment (0 = unset/disabled).

    Empty/whitespace value counts as unset — a templated `.env` line like
    `CH_MAIN=` must not crash every bot at import via `int("")` (Review PR #10;
    audit_reports/01_core_infra.md LOW).
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return 0
    return int(raw)


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
# Shared channel for research bots 30-33 (PEX1/FMR1/TRM1/FIF1 — report 15
# S6/S8/S10/S11). One channel for all four: new ideas are observed as a cohort,
# attribution runs via the model tag in ai_signals.
CH_NEW_IDEAS = _ch("CH_NEW_IDEAS")
# AIM2-TOPN high-conviction channel (T-2026-CU-9050-051): the "Top 1-3 des
# Tages" stream is deliberately SEPARATE from the base AIM2 channel (CH_MASTER)
# so it can be traded on its own risk profile. Plain _ch (0 = unset): an unset
# channel forces shadow-only in 15_ai_master_bot.py — never silently falls back
# onto the base AIM2 channel.
CH_AIM2_TOPN = _ch("CH_AIM2_TOPN")
# Shadow visibility channel (T-2026-CU-9050-150): OPTIONAL preview-only for
# shadow trades. Unset/0 = OFF (default, backward compatible — shadow remains
# DB-only). If set, core.signal_post.post_shadow_ai_signal echoes each shadow
# trade with ONE deliberately NOT-Cornix-parseable preview — visibility only.
# Cornix listens EXCLUSIVELY per REGIME_TRADING_CHANNEL_ID to the trading channel;
# this one is a non-trading channel (no Cornix) → zero trade risk. Never enter
# the trading channel here (rule 4).
CH_SHADOW_TEST = _ch("CH_SHADOW_TEST")
# Trailing-close bot (bot 40, T-2026-KYT-9050-042 phase C): OWN channel into which
# the bot mirrors the 33 selected legs and closes via trailing. Michi hooks Cornix
# directly — the channel is the trailing arm against the hold arm of the
# existing fleet. Unset/0 ⇒ the bot runs, tracks, and logs, but posts
# nothing (shadow) — together with TRAILING_BOT_LIVE_POSTING the double safety
# net against a live post nobody decided on. NEVER enter an existing
# trading channel here: the bot posts `Close <SYMBOL>`, and that affects
# symbol-wide ALL trades in the channel (see comment at _ch_override below).
CH_TRAILING = _ch("CH_TRAILING")


# Per-bot override (operator 2026-07-07): unset → fallback to the
# cohort channel. This lets a single bot (e.g. FMR2 with its own
# close path — Cornix' "Close <SYMBOL>" affects ALL trades of the symbol in
# the channel) move to its own channel via .env without code deploy.
def _ch_override(name: str, fallback: int) -> int:
    """Like _ch(), but with fallback ONLY when variable is unset/empty.

    An explicit `CH_X=0` must stay 0 (bots enforce shadow-only when
    TARGET_CHANNEL_ID == 0) — `_ch(name) or fallback` would swallow the
    repo-wide 0=disabled semantics and silently fall the bot back to the
    live cohort channel.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return fallback
    return int(raw)


CH_PEX1 = _ch_override("CH_PEX1", CH_NEW_IDEAS)
CH_FMR1 = _ch_override("CH_FMR1", CH_NEW_IDEAS)
CH_TRM1 = _ch_override("CH_TRM1", CH_NEW_IDEAS)
CH_FIF1 = _ch_override("CH_FIF1", CH_NEW_IDEAS)
# ODS1 OI-divergence short (T-2026-KYT-9050-106). Cohort channel by default —
# `CH_NEW_IDEAS` is the test environment (operator decision 2026-07-06), and the
# cohort's `NEW_IDEAS_LIVE_POSTING` defaults to 1, so this leg is live on deploy
# by design rather than by omission.
CH_ODS1 = _ch_override("CH_ODS1", CH_NEW_IDEAS)

# MAX1 high-conviction rubberband short (T-2026-CU-9050-067). Operator decision
# 2026-07-11: this one posts into the MAIN channel — a deliberate departure from
# the CH_NEW_IDEAS convention for new bots (OPUS-HANDOFF §5), because the whole
# point is 1-3 high-conviction trades/day in the channel Michi actually trades.
# The override exists so the stream can be re-routed without a code deploy; an
# unset CH_MAIN (0) forces shadow-only in 34_ai_max1_bot.py.
CH_MAX1 = _ch_override("CH_MAX1", CH_MAIN)


TELEGRAM_CHANNELS = {
    "Fast In And Out": CH_FAST_IN_OUT,
    "5 Percent": CH_5_PERCENT,
    # "Main Channel" retired (T-2026-KYT-9050-020) → replaced by MAX2 (SRA2-LONG
    # trade coin-filtered to CH_MAIN, 9_ai_sr_bot.py). No detector emits
    # the "Main Channel" strategy anymore; the map entry would be dead code. CH_MAIN
    # itself remains (CH_MAX1 fallback + MAX2 destination).
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

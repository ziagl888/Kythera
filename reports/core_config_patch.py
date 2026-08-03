# core/config.py — PATCH: addition for regime orchestrator
from core import config as _kcfg  # channel ids
# Insert these two lines at the end of TELEGRAM_CHANNELS / after the block

# --- REGIME ORCHESTRATOR CHANNELS ---
# Trading channel: the only channel Cornix listens to from now on.
# Cornix must be configured to use EXCLUSIVELY this channel
# as signal source — remove all old bot channels from Cornix config.
REGIME_TRADING_CHANNEL_ID = _kcfg.CH_REGIME_TRADING

# Status channel: regime-change alerts, hourly status posts, daily cross-tables.
# Informational only — no Cornix listens here.
# We use the existing sentiment-tracker channel (fits thematically).
REGIME_STATUS_CHANNEL_ID = _kcfg.CH_MARKET_DATA

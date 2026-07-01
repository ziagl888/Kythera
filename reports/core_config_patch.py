# core/config.py — PATCH: Ergänzung für Regime-Orchestrator
from core import config as _kcfg  # channel ids
# Diese zwei Zeilen ans Ende von TELEGRAM_CHANNELS / nach dem Block einfügen

# --- REGIME ORCHESTRATOR CHANNELS ---
# Trading-Channel: Der einzige Channel den Cornix ab sofort hört.
# Cornix muss so konfiguriert werden, dass es AUSSCHLIESSLICH diesen Channel
# als Signalquelle nutzt — alle alten Bot-Channels aus der Cornix-Config entfernen.
REGIME_TRADING_CHANNEL_ID = _kcfg.CH_REGIME_TRADING

# Status-Channel: Regime-Wechsel-Alerts, stündliche Status-Posts, tägliche Cross-Tables.
# Rein informativ — kein Cornix hört hier.
# Wir nutzen den bestehenden Sentiment-Tracker-Channel (passt thematisch).
REGIME_STATUS_CHANNEL_ID = _kcfg.CH_MARKET_DATA

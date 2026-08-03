# main_watchdog.py — PATCH for regime orchestrator
#
# Add to PROCESSES_TO_RUN at the end (after the TD & BB Bot entry):
#
#     # {\"name\": \"IP Pattern Bot\",  \"script\": \"22_ip_pattern_bot.py\", ...},
#     {"name": "Market Tracker",    "script": "23_market_tracker.py",       "restart_interval": None,  "start_delay": 135},
#     {"name": "Quasimodo Bot",     "script": "24_quasimodo_bot.py",        "restart_interval": None,  "start_delay": 143},
#     {"name": "TD & BB Bot",       "script": "25_smc_ml_sniper.py",        "restart_interval": None,  "start_delay": 151},
#     # ── NEW ENTRIES (regime orchestrator) ──────────────────────────────────
#     {"name": "Regime Detector",   "script": "26_regime_detector.py",      "restart_interval": None,  "start_delay": 160},
#     {"name": "Bot Regime Analyzer","script": "27_bot_regime_analyzer.py", "restart_interval": None,  "start_delay": 167},
#     {"name": "Signal Orchestrator","script": "28_signal_orchestrator.py", "restart_interval": None,  "start_delay": 175},

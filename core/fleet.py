"""core/fleet.py — Single-source definition of Kythera process fleet.

Before T-2026-CU-9050-091 the process list existed twice and drifted
(audit ledger R2(a) / P1.38 aspect):

* ``main_watchdog.py`` (``PROCESSES_TO_RUN``) — authoritative, with ``start_delay``,
  complete fleet incl. regime/research/MAX1 bots 26–34.
* ``dashboard.py`` (``PROCESSES``) — with ``group`` (display), but without bots
  26–34; the dashboard thus showed only part of the running fleet.

Both consumers now import this single list. The watchdog list was the
authoritative source; ``group`` was taken from the dashboard (bots 26–34
are assigned an existing display group, see below).

Field contract per entry
------------------------
``name``             Display name (watchdog log, dashboard badge).
``script``           Bot script filename (startup + basename for
                     orphan sweep in P0.2).
``group``            Dashboard display group: ``core`` | ``ai`` | ``strategy`` |
                     ``logger`` (dashboard CSS/filter knows exactly these four).
``start_delay``      Seconds of staggered delay on fleet startup so not
                     all bots hit the DB at once (watchdog).
``restart_interval`` Seconds until planned RAM-recycling restart
                     (``None`` = never).

The watchdog reads ``name``/``script``/``start_delay``/``restart_interval`` and
ignores ``group``; the dashboard reads ``name``/``script``/``group``/
``restart_interval`` and ignores ``start_delay``. A field irrelevant to one
consumer is thus a no-op for the other.

Order = startup order (ascending ``start_delay``). The dashboard
renders the fleet in exactly this order.

IMPORTANT: pure data definition, no behaviour logic. Process lifecycle
(single-instance mutex, orphan sweep, CREATE_NEW_PROCESS_GROUP/CTRL_BREAK from
P0.2/P2.48; supervision/backoff/heartbeat from P1.37/P2.47) stays entirely in the
watchdog. A change here changes ONLY which processes run and how they appear
in the dashboard — nothing in the startup/stop/restart mechanics.
"""

from __future__ import annotations

from typing import Any

# Not part of the fleet (deliberately omitted):
#   22_ip_pattern_bot.py — commented out in watchdog since day one; the dashboard
#   never knew the bot. Stays disabled until it is taken up again.

FLEET: list[dict[str, Any]] = [
    # ── Core Pipeline ─────────────────────────────────────────────────────────
    {
        "name": "Data Ingestion",
        "script": "1_data_ingestion.py",
        "group": "core",
        "start_delay": 0,
        "restart_interval": None,
    },
    {
        "name": "Chart Data Service",
        "script": "chart_data_service.py",
        "group": "core",
        "start_delay": 3,
        "restart_interval": None,
    },
    {
        "name": "Indicator Engine",
        "script": "2_indicator_engine.py",
        "group": "core",
        "start_delay": 5,
        "restart_interval": 21600,
    },
    {"name": "Detectors", "script": "3_detectors.py", "group": "core", "start_delay": 5, "restart_interval": 21600},
    {
        "name": "Telegram Bot",
        "script": "4_telegram_bot.py",
        "group": "core",
        "start_delay": 5,
        "restart_interval": None,
    },
    {
        "name": "Trade Monitor",
        "script": "5_trade_monitor.py",
        "group": "core",
        "start_delay": 5,
        "restart_interval": None,
    },
    {
        "name": "Housekeeping",
        "script": "6_housekeeping.py",
        "group": "core",
        "start_delay": 10,
        "restart_interval": None,
    },
    # ── Classic Strategies / Monitors ──────────────────────────────────────
    {
        "name": "Pattern detector",
        "script": "7_pattern_detector.py",
        "group": "strategy",
        "start_delay": 15,
        "restart_interval": None,
    },
    {
        "name": "AI Trade Monitor",
        "script": "8_ai_trade_monitor.py",
        "group": "strategy",
        "start_delay": 23,
        "restart_interval": None,
    },
    {"name": "AI SR Bot", "script": "9_ai_sr_bot.py", "group": "strategy", "start_delay": 31, "restart_interval": None},
    {
        "name": "Pump Dump Detector",
        "script": "10_pump_dump_detector.py",
        "group": "strategy",
        "start_delay": 39,
        "restart_interval": None,
    },
    # ── AI Bots ───────────────────────────────────────────────────────────────
    {
        "name": "AI MIS1 Detector",
        "script": "11_ai_mis_bot.py",
        "group": "ai",
        "start_delay": 47,
        "restart_interval": None,
    },
    {
        "name": "AI ATS1 Detector",
        "script": "12_ai_ats_bot.py",
        "group": "ai",
        "start_delay": 55,
        "restart_interval": None,
    },
    {
        "name": "AI RUB1 Detector",
        "script": "13_ai_rub_bot.py",
        "group": "ai",
        "start_delay": 63,
        "restart_interval": None,
    },
    {
        "name": "AI ATB1 Detector",
        "script": "14_ai_atb_bot.py",
        "group": "ai",
        "start_delay": 71,
        "restart_interval": None,
    },
    {
        "name": "AI AIM2 Detector",
        "script": "15_ai_master_bot.py",
        "group": "ai",
        "start_delay": 79,
        "restart_interval": None,
    },
    {
        "name": "SMC FOREX Detector",
        "script": "16_smc_forex_metals_bot.py",
        "group": "strategy",
        "start_delay": 87,
        "restart_interval": None,
    },
    {
        "name": "Mayank Bot",
        "script": "17_mayank_bot.py",
        "group": "strategy",
        "start_delay": 95,
        "restart_interval": None,
    },
    {
        "name": "AI ABR1 Detector",
        "script": "18_ai_abr1_bot.py",
        "group": "ai",
        "start_delay": 103,
        "restart_interval": None,
    },
    # ── Logger ────────────────────────────────────────────────────────────────
    {
        "name": "Whale logger Bot",
        "script": "19_whale_logger_bot.py",
        "group": "logger",
        "start_delay": 111,
        "restart_interval": None,
    },
    {
        "name": "Funding logger Bot",
        "script": "20_funding_logger_bot.py",
        "group": "logger",
        "start_delay": 119,
        "restart_interval": None,
    },
    # ── Further Strategies ────────────────────────────────────────────────────
    {
        "name": "BTC SMC Bot",
        "script": "21_btc_smc_strategy.py",
        "group": "strategy",
        "start_delay": 127,
        "restart_interval": None,
    },
    {
        "name": "Market Tracker",
        "script": "23_market_tracker.py",
        "group": "logger",
        "start_delay": 135,
        "restart_interval": None,
    },
    {
        "name": "Quasimodo Bot",
        "script": "24_quasimodo_bot.py",
        "group": "strategy",
        "start_delay": 143,
        "restart_interval": None,
    },
    {
        "name": "TD & BB Bot",
        "script": "25_smc_ml_sniper.py",
        "group": "ai",
        "start_delay": 151,
        "restart_interval": None,
    },
    # ── Regime Orchestrator (v5) — not listed in dashboard so far ─────────
    # group="strategy": new for the dashboard; the regime layer feeds the
    # strategy gates. Existing display group so no new filter/CSS
    # category appears in the dashboard (only the LIST gets centralised).
    {
        "name": "Regime Detector",
        "script": "26_regime_detector.py",
        "group": "strategy",
        "start_delay": 160,
        "restart_interval": None,
    },
    {
        "name": "Bot Regime Analyzer",
        "script": "27_bot_regime_analyzer.py",
        "group": "strategy",
        "start_delay": 167,
        "restart_interval": None,
    },
    {
        "name": "Signal Orchestrator",
        "script": "28_signal_orchestrator.py",
        "group": "strategy",
        "start_delay": 175,
        "restart_interval": None,
    },
    {
        "name": "UFI1 Fib Bot",
        "script": "29_ufi1_bot.py",
        "group": "strategy",
        "start_delay": 183,
        "restart_interval": None,
    },
    # ── Research Bots (Report 15: S6/S8/S10/S11 — channel CH_NEW_IDEAS) ───────
    {
        "name": "AI PEX1 Detector",
        "script": "30_ai_pex1_bot.py",
        "group": "ai",
        "start_delay": 191,
        "restart_interval": None,
    },
    {
        "name": "AI FMR1 Detector",
        "script": "31_ai_fmr1_bot.py",
        "group": "ai",
        "start_delay": 199,
        "restart_interval": None,
    },
    {
        "name": "AI TRM1 Detector",
        "script": "32_ai_trm1_bot.py",
        "group": "ai",
        "start_delay": 207,
        "restart_interval": None,
    },
    {
        "name": "AI FIF1 Detector",
        "script": "33_ai_fif1_bot.py",
        "group": "ai",
        "start_delay": 215,
        "restart_interval": None,
    },
    # ── High-conviction throttle via RUB2-SHORT (T-2026-CU-9050-067) ──────────
    {
        "name": "AI MAX1 Detector",
        "script": "34_ai_max1_bot.py",
        "group": "ai",
        "start_delay": 223,
        "restart_interval": None,
    },
    # ── Open-interest collector (K9/OIC, T-2026-CU-9050-103) ──────────────────
    # Own failure domain (no detector attachment); hypertable oi_5m. PG budget:
    # +2 idle connections over standard pool (P1.34 — cross-check max_connections on
    # VPS). New entry only supervised after watchdog restart (FLEET read at watchdog import) ⇒ operator gate.
    {
        "name": "OI Collector",
        "script": "35_oi_collector.py",
        "group": "logger",
        "start_delay": 231,
        "restart_interval": None,
    },
    # ── Rule-based shadow forwarder (studies K1/K2/K5/K7, T-2026-CU-9050-149) ─
    # Pure shadow bots (no live post): validate negative/weak study
    # signals live via monitored, never-posted trades. New entry only
    # supervised after watchdog restart (FLEET read at watchdog import) ⇒
    # operator gate; below 100% CPU first check capacity (restart incident 07-15).
    {
        "name": "AI LIS1 Detector",
        "script": "36_ai_lis1_bot.py",
        "group": "ai",
        "start_delay": 239,
        "restart_interval": None,
    },
    {
        "name": "AI TSM1 Detector",
        "script": "37_ai_tsm1_bot.py",
        "group": "ai",
        "start_delay": 247,
        "restart_interval": None,
    },
    {
        "name": "AI SKW1 Detector",
        "script": "38_ai_skw1_bot.py",
        "group": "ai",
        "start_delay": 255,
        "restart_interval": None,
    },
    {
        "name": "AI XSM1 Detector",
        "script": "39_ai_xsm1_bot.py",
        "group": "ai",
        "start_delay": 263,
        "restart_interval": None,
    },
    # ── Trailing-close arm in own channel (T-2026-KYT-9050-042 phase C) ───
    # No detector: mirrors the 33 roster legs (core/trailing_roster.py)
    # in CH_TRAILING and closes them via trailing close — the A/B arm against
    # the hold arm of the existing fleet. Never writes to ai_signals. Only posts
    # when TRAILING_BOT_LIVE_POSTING=1 AND CH_TRAILING is set (both default
    # off). New entry only supervised after watchdog restart (FLEET read
    # at watchdog import) ⇒ operator gate.
    {
        "name": "Trailing Close Bot",
        "script": "40_trailing_close_bot.py",
        "group": "ai",
        "start_delay": 271,
        "restart_interval": None,
    },
    # ── Liquidations collector (LQE1, T-2026-KYT-9050-077) ────────────────────
    # Websocket !forceOrder@arr → hypertable liq_events (ground truth for the
    # MPS1 heatmap calibration). Own failure domain like K9; DB connection
    # pulled from the pool per flush. At the end of the list with the highest
    # delay (monotonicity regression backtest/test_fleet_definition.py; 247
    # collided with TSM1). New entry only supervised after watchdog restart
    # (FLEET read at watchdog import) ⇒ operator gate.
    {
        "name": "Liq Collector",
        "script": "41_liq_collector.py",
        "group": "logger",
        "start_delay": 279,
        "restart_interval": None,
    },
    # ── OI divergence short (ODS1, T-2026-KYT-9050-106) ───────────────────────
    # Shorts a rally that open interest did not pay for — the only one of three
    # OI mechanics that survived the T-096 event study. Reads oi_5m only, no
    # candles and no model artifact. Posts to CH_ODS1, which falls back to the
    # CH_NEW_IDEAS cohort channel, so it is live on deploy by design (operator
    # decision 2026-08-05, no shadow phase). New entry only supervised after
    # watchdog restart (FLEET read at watchdog import) ⇒ operator gate.
    {
        "name": "AI ODS1 Detector",
        "script": "42_ai_ods1_bot.py",
        "group": "ai",
        "start_delay": 283,
        "restart_interval": None,
    },
    # ── FIF2 vol-gated ladder mirror (T-2026-KYT-9050-112) ────────────────────
    # Mirrors fresh ai_signals arrivals whose symbol clears the rolling q80 of
    # sym_vol_4h; posts the measured t104 ladder to CH_FIF2. With CH_FIF2 and
    # CH_FIF1 both unset the chain ends at CH_NEW_IDEAS — there is no distinct
    # FIF channel — and that channel is not Cornix-executed, so posting there
    # produces forward measurement rather than fills. That is NOT a containment:
    # FIF2 holds a trailing roster seat (core/trailing_roster.py), so bot 40
    # mirrors these signals into CH_TRAILING, which IS executed. The channel is
    # the quiet path; the seat is the money path (T-2026-KYT-9050-115/118).
    # delay 291 was chosen to leave 283 free for ODS1 (PR #276, then in flight on
    # a parallel branch) so the monotonicity guard holds regardless of merge
    # order — ODS1 landed first, and both entries now sit in that order. Last in
    # the list with the highest delay (monotonicity regression
    # backtest/test_fleet_definition.py). New entry only supervised after a
    # watchdog restart ⇒ operator gate.
    {
        "name": "AI FIF2 Mirror",
        "script": "43_ai_fif2_bot.py",
        "group": "ai",
        "start_delay": 291,
        "restart_interval": None,
    },
    # ── Unrestricted trailing twin (T-2026-KYT-9050-117) ──────────────────────
    # Bot 40's engine under TRAILING_BOT_PROFILE=free: no admission caps, ALL
    # roster trades spread evenly over CH_TRAILING_FREE_A/B (2 × Cornix-500).
    # Own table trailing_free_positions, own gate TRAILING_FREE_LIVE_POSTING
    # (default 0); channels fall back to CH_SHADOW_TEST (not Cornix-executed).
    # Last in the list with the highest delay (monotonicity regression
    # backtest/test_fleet_definition.py). New entry only supervised after a
    # watchdog restart (FLEET read at watchdog import) ⇒ operator gate.
    {
        "name": "Trailing Free Bot",
        "script": "44_trailing_free_bot.py",
        "group": "ai",
        "start_delay": 299,
        "restart_interval": None,
    },
    # ── Shared candle snapshot (T-2026-KYT-9050-132) ──────────────────────────
    # "Fetch once, serve many": reads each (symbol, tf) once per candle period
    # and serves the nine hourly scanners' overlapping windows from RAM
    # (core/candle_snapshot.py hooks into core/candles.py — no bot is touched).
    # Consumers only appear when KYTHERA_CANDLE_SNAPSHOT is turned on; with the
    # gate off (the default) the process runs idle and nothing queries it, so
    # listing it here is safe ahead of the rollout decision. group="core": it is
    # infrastructure like chart_data_service, not a strategy. Last in the list
    # with the highest delay (monotonicity regression
    # backtest/test_fleet_definition.py) — it does not need an early slot,
    # because a cold store simply falls back to the DB path. New entry only
    # supervised after a watchdog restart (FLEET read at watchdog import) ⇒
    # operator gate.
    {
        "name": "Candle Snapshot Service",
        "script": "candle_snapshot_service.py",
        "group": "core",
        "start_delay": 307,
        "restart_interval": None,
    },
]

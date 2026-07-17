"""core/fleet.py — Single-Source-Definition der Kythera-Prozess-Fleet.

Vor T-2026-CU-9050-091 existierte die Prozessliste doppelt und driftete
(Audit-Ledger R2(a) / P1.38-Teilaspekt):

* ``main_watchdog.py`` (``PROCESSES_TO_RUN``) — autoritativ, mit ``start_delay``,
  komplette Fleet inkl. der Regime-/Research-/MAX1-Bots 26–34.
* ``dashboard.py`` (``PROCESSES``) — mit ``group`` (Anzeige), aber ohne die Bots
  26–34; das Dashboard zeigte damit nur einen Teil der laufenden Fleet.

Beide Konsumenten importieren jetzt diese eine Liste. Die Watchdog-Liste war die
autoritative Quelle; ``group`` wurde aus dem Dashboard übernommen (Bots 26–34
bekommen eine bestehende Anzeigegruppe zugewiesen, siehe unten).

Feld-Vertrag pro Eintrag
------------------------
``name``             Anzeigename (Watchdog-Log, Dashboard-Badge).
``script``           Dateiname des Bot-Skripts (Start + Basename für den
                     Orphan-Sweep in P0.2).
``group``            Dashboard-Anzeigegruppe: ``core`` | ``ai`` | ``strategy`` |
                     ``logger`` (das Dashboard-CSS/-Filter kennt genau diese vier).
``start_delay``      Sekunden Staffel-Verzögerung beim Fleet-Start, damit nicht
                     alle Bots gleichzeitig die DB treffen (Watchdog).
``restart_interval`` Sekunden bis zum geplanten RAM-Recycling-Restart
                     (``None`` = nie).

Der Watchdog liest ``name``/``script``/``start_delay``/``restart_interval`` und
ignoriert ``group``; das Dashboard liest ``name``/``script``/``group``/
``restart_interval`` und ignoriert ``start_delay``. Ein für den einen Konsumenten
irrelevantes Feld ist für den anderen damit ein No-op.

Reihenfolge = Start-Reihenfolge (aufsteigender ``start_delay``). Das Dashboard
rendert die Fleet in genau dieser Reihenfolge.

WICHTIG: reine Datendefinition, keine Verhaltens-Logik. Der Prozess-Lifecycle
(Single-Instance-Mutex, Orphan-Sweep, CREATE_NEW_PROCESS_GROUP/CTRL_BREAK aus
P0.2/P2.48; Supervision/Backoff/Heartbeat aus P1.37/P2.47) bleibt vollständig im
Watchdog. Eine Änderung hier ändert NUR, welche Prozesse laufen und wie sie im
Dashboard erscheinen — nichts an der Start-/Stop-/Restart-Mechanik.
"""

from __future__ import annotations

from typing import Any

# Nicht Teil der Fleet (bewusst ausgelassen):
#   22_ip_pattern_bot.py — im Watchdog seit jeher auskommentiert; das Dashboard
#   kannte den Bot nie. Bleibt deaktiviert, bis er wieder aufgenommen wird.

FLEET: list[dict[str, Any]] = [
    # ── Kern-Pipeline ─────────────────────────────────────────────────────────
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
    # ── Klassische Strategien / Monitore ──────────────────────────────────────
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
    # ── AI-Bots ───────────────────────────────────────────────────────────────
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
    # ── Weitere Strategien ────────────────────────────────────────────────────
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
    # ── Regime-Orchestrator (v5) — im Dashboard bisher NICHT gelistet ─────────
    # group="strategy": neu für das Dashboard; das Regime-Layer speist die
    # Strategie-Gates. Bestehende Anzeigegruppe, damit keine neue Filter-/CSS-
    # Kategorie im Dashboard entsteht (nur die LISTE wird zentralisiert).
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
    # ── Research-Bots (Report 15: S6/S8/S10/S11 — Channel CH_NEW_IDEAS) ───────
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
    # ── High-Conviction-Drossel über RUB2-SHORT (T-2026-CU-9050-067) ──────────
    {
        "name": "AI MAX1 Detector",
        "script": "34_ai_max1_bot.py",
        "group": "ai",
        "start_delay": 223,
        "restart_interval": None,
    },
    # ── Open-Interest-Collector (K9/OIC, T-2026-CU-9050-103) ──────────────────
    # Eigene Failure-Domain (kein Detector-Anbau); Hypertable oi_5m. PG-Budget:
    # +2 Idle-Connections über den Standard-Pool (P1.34 — max_connections auf
    # dem VPS gegenprüfen). Neuer Eintrag wird erst nach Watchdog-Restart
    # supervised (FLEET wird beim Watchdog-Import gelesen) ⇒ Operator-Gate.
    {
        "name": "OI Collector",
        "script": "35_oi_collector.py",
        "group": "logger",
        "start_delay": 231,
        "restart_interval": None,
    },
    # ── Regelbasierte Shadow-Forwarder (Studien K1/K2/K5/K7, T-2026-CU-9050-149) ─
    # Reine Shadow-Bots (kein Live-Post): validieren negative/schwache Studien-
    # Signale live über überwachte, nie gepostete Trades. Neuer Eintrag wird erst
    # nach Watchdog-Restart supervised (FLEET beim Watchdog-Import gelesen) ⇒
    # Operator-Gate; unter 100 % CPU zuerst Kapazität prüfen (Restart-Incident 07-15).
    {
        "name": "AI LIS1 Detector",
        "script": "36_ai_lis1_bot.py",
        "group": "ai",
        "start_delay": 239,
        "restart_interval": None,
    },
]

# backtest/test_fleet_definition.py
"""
Single-Source-Regression für die Fleet-Prozessliste (T-2026-CU-9050-091, R2(a)).

Vor der Zentralisierung existierte die Prozessliste doppelt und driftete:
``main_watchdog.PROCESSES_TO_RUN`` (autoritativ, mit ``start_delay``, volle Fleet)
vs. ``dashboard.PROCESSES`` (mit ``group``, aber ohne die Bots 26–34). Beide
konsumieren jetzt ``core.fleet.FLEET``.

Diese Tests pinnen:
  * Watchdog-Liste == Dashboard-Liste == fleet.FLEET (auf dem Pre-Fix-Stand
    fielen sie auseinander → dieser Test wäre dort rot).
  * die watchdog-relevante Projektion (name/script/start_delay/restart_interval)
    ist Byte-für-Byte die alte autoritative Liste → keine Verhaltensänderung an
    Start-Reihenfolge oder Delays.
  * jede ``group`` liegt in der vom Dashboard-CSS/-Filter gerenderten Menge, damit
    die neu gelisteten Bots kein ungestyltes Badge / keine neue Filterkategorie
    erzeugen.

DB-frei: Watchdog und Dashboard werden über importlib mit gemockten Schwer-
Abhängigkeiten geladen (kein DB-, Flask- oder psutil-Kontakt).

Run with: pytest backtest/test_fleet_definition.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

# C-Extension-Pakete VOR jedem mock.patch.dict(sys.modules) importieren: sonst
# kann ein Import innerhalb des Patch-Blocks numpy/pandas erstmalig in sys.modules
# eintragen, und der patch.dict-Teardown reißt die halb-initialisierte C-Extension
# wieder heraus (numpy-Teardown-Bug, MEMORY: patch-dict-sys-modules-numpy-teardown).
# Hier importiert zwar keiner der Loader pandas direkt, aber der Pre-Seed hält den
# kombinierten Suite-Lauf robust.
for _c_ext in ("numpy", "pandas", "scipy"):
    try:  # pragma: no cover - reine Import-Vorsorge
        __import__(_c_ext)
    except ImportError:
        pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module(name: str, mocked: dict):
    """Load ``<root>/<name>.py`` mit gemockten Schwer-Abhängigkeiten.

    ``core.fleet`` wird bewusst NICHT gemockt — der Test prüft ja, dass die
    Konsumenten die echte zentrale Liste ziehen.
    """
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict("sys.modules", mocked):
        spec.loader.exec_module(module)
    return module


def _load_watchdog():
    return _load_module(
        "main_watchdog",
        {
            "core.health_monitor": mock.MagicMock(),
            "core.process_control": mock.MagicMock(),
            "psutil": mock.MagicMock(),
        },
    )


def _load_dashboard():
    # threading mit-mocken: dashboard.py startet beim Import einen Daemon-Thread
    # (_stats_poller) und legt Modul-Level-Locks an — im Test unerwünscht.
    return _load_module(
        "dashboard",
        {
            "core.process_control": mock.MagicMock(),
            "psutil": mock.MagicMock(),
            "flask": mock.MagicMock(),
            "threading": mock.MagicMock(),
        },
    )


import core.fleet as fleet

wd = _load_watchdog()
dash = _load_dashboard()


# Die autoritative Projektion, wie der Watchdog sie vor der Zentralisierung inline
# trug (Name/Script/Start-Delay/Restart-Interval). Das ist der Verhaltens-Anker:
# Start-Reihenfolge und Staffel-Delays der Fleet dürfen sich nicht ändern.
EXPECTED_WATCHDOG_VIEW = [
    ("Data Ingestion", "1_data_ingestion.py", 0, None),
    ("Chart Data Service", "chart_data_service.py", 3, None),
    ("Indicator Engine", "2_indicator_engine.py", 5, 21600),
    ("Detectors", "3_detectors.py", 5, 21600),
    ("Telegram Bot", "4_telegram_bot.py", 5, None),
    ("Trade Monitor", "5_trade_monitor.py", 5, None),
    ("Housekeeping", "6_housekeeping.py", 10, None),
    ("Pattern detector", "7_pattern_detector.py", 15, None),
    ("AI Trade Monitor", "8_ai_trade_monitor.py", 23, None),
    ("AI SR Bot", "9_ai_sr_bot.py", 31, None),
    ("Pump Dump Detector", "10_pump_dump_detector.py", 39, None),
    ("AI MIS1 Detector", "11_ai_mis_bot.py", 47, None),
    ("AI ATS1 Detector", "12_ai_ats_bot.py", 55, None),
    ("AI RUB1 Detector", "13_ai_rub_bot.py", 63, None),
    ("AI ATB1 Detector", "14_ai_atb_bot.py", 71, None),
    ("AI AIM2 Detector", "15_ai_master_bot.py", 79, None),
    ("SMC FOREX Detector", "16_smc_forex_metals_bot.py", 87, None),
    ("Mayank Bot", "17_mayank_bot.py", 95, None),
    ("AI ABR1 Detector", "18_ai_abr1_bot.py", 103, None),
    ("Whale logger Bot", "19_whale_logger_bot.py", 111, None),
    ("Funding logger Bot", "20_funding_logger_bot.py", 119, None),
    ("BTC SMC Bot", "21_btc_smc_strategy.py", 127, None),
    ("Market Tracker", "23_market_tracker.py", 135, None),
    ("Quasimodo Bot", "24_quasimodo_bot.py", 143, None),
    ("TD & BB Bot", "25_smc_ml_sniper.py", 151, None),
    ("Regime Detector", "26_regime_detector.py", 160, None),
    ("Bot Regime Analyzer", "27_bot_regime_analyzer.py", 167, None),
    ("Signal Orchestrator", "28_signal_orchestrator.py", 175, None),
    ("UFI1 Fib Bot", "29_ufi1_bot.py", 183, None),
    ("AI PEX1 Detector", "30_ai_pex1_bot.py", 191, None),
    ("AI FMR1 Detector", "31_ai_fmr1_bot.py", 199, None),
    ("AI TRM1 Detector", "32_ai_trm1_bot.py", 207, None),
    ("AI FIF1 Detector", "33_ai_fif1_bot.py", 215, None),
    ("AI MAX1 Detector", "34_ai_max1_bot.py", 223, None),
]

# Vom Dashboard-CSS (.group-core/.group-ai/.group-strategy/.group-logger) und dem
# Filter gerenderte Gruppen. Eine group außerhalb dieser Menge erzeugt ein
# ungestyltes Badge — das wäre eine unbeabsichtigte UI-Änderung.
KNOWN_GROUPS = {"core", "ai", "strategy", "logger"}


# ── Single Source ────────────────────────────────────────────────────────────


def test_watchdog_consumes_the_central_fleet():
    assert wd.PROCESSES_TO_RUN == fleet.FLEET


def test_dashboard_consumes_the_central_fleet():
    assert dash.PROCESSES == fleet.FLEET


def test_watchdog_and_dashboard_agree():
    # Der Kern der Drift-Regression: vor dem Fix fehlten dem Dashboard 26–34.
    assert wd.PROCESSES_TO_RUN == dash.PROCESSES


# ── Keine Verhaltensänderung am Watchdog ─────────────────────────────────────


def test_watchdog_view_is_unchanged():
    view = [(p["name"], p["script"], p["start_delay"], p["restart_interval"]) for p in wd.PROCESSES_TO_RUN]
    assert view == EXPECTED_WATCHDOG_VIEW


def test_start_delays_are_monotonic():
    # Der Watchdog startet nach aufsteigendem start_delay; Listen- == Start-
    # Reihenfolge. Nicht-monotone Delays würden die Staffelung stillschweigend
    # umsortieren.
    delays = [p["start_delay"] for p in fleet.FLEET]
    assert delays == sorted(delays)


# ── Feld-Vertrag ─────────────────────────────────────────────────────────────


def test_every_entry_has_the_full_field_contract():
    for p in fleet.FLEET:
        assert set(p) == {"name", "script", "group", "start_delay", "restart_interval"}, p


def test_groups_are_dashboard_renderable():
    for p in fleet.FLEET:
        assert p["group"] in KNOWN_GROUPS, p


def test_scripts_are_unique():
    scripts = [p["script"] for p in fleet.FLEET]
    assert len(scripts) == len(set(scripts))

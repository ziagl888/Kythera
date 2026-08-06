# backtest/test_fleet_definition.py
"""
Single-source regression for the fleet process list (T-2026-CU-9050-091, R2(a)).

Before the centralization the process list existed twice and drifted:
``main_watchdog.PROCESSES_TO_RUN`` (authoritative, with ``start_delay``, full fleet)
vs. ``dashboard.PROCESSES`` (with ``group``, but without bots 26-34). Both now
consume ``core.fleet.FLEET``.

These tests pin down:
  * watchdog list == dashboard list == fleet.FLEET (on the pre-fix state
    they diverged → this test would be red there).
  * the watchdog-relevant projection (name/script/start_delay/restart_interval)
    is byte-for-byte the old authoritative list → no behaviour change to
    start order or delays.
  * every ``group`` is in the set rendered by the dashboard CSS/filter, so
    the newly listed bots don't produce an unstyled badge / a new filter
    category.

DB-free: watchdog and dashboard are loaded via importlib with mocked heavy
dependencies (no DB, Flask or psutil contact).

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

# Import C-extension packages BEFORE every mock.patch.dict(sys.modules): otherwise
# an import inside the patch block could register numpy/pandas in sys.modules for
# the first time, and the patch.dict teardown then rips the half-initialized C
# extension back out (numpy teardown bug, MEMORY: patch-dict-sys-modules-numpy-teardown).
# None of the loaders here import pandas directly, but the pre-seed keeps the
# combined suite run robust.
for _c_ext in ("numpy", "pandas", "scipy"):
    try:  # pragma: no cover - pure import precaution
        __import__(_c_ext)
    except ImportError:
        pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module(name: str, mocked: dict):
    """Load ``<root>/<name>.py`` with mocked heavy dependencies.

    ``core.fleet`` is deliberately NOT mocked — the test is precisely checking
    that the consumers pull the real central list.
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
    # Mock threading too: dashboard.py starts a daemon thread on import
    # (_stats_poller) and creates module-level locks — undesired in the test.
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


# The authoritative projection, as the watchdog carried it inline before the
# centralization (name/script/start_delay/restart_interval). This is the
# behaviour anchor: the fleet's start order and staggered delays must not change.
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
    # T-2026-CU-9050-103 (K9/OIC): deliberate fleet extension — the anchor grows
    # along with it, the delays/order of the existing entries remain unchanged.
    ("OI Collector", "35_oi_collector.py", 231, None),
    # Re-pinned under T-2026-KYT-9050-112: the anchor had silently drifted six
    # bots behind FLEET (36-41 were added without growing it, so this test was
    # red the whole time and nobody noticed — backtest/ runs in no CI job).
    # Entries below restate what already ran in production, plus FIF2.
    ("AI LIS1 Detector", "36_ai_lis1_bot.py", 239, None),
    ("AI TSM1 Detector", "37_ai_tsm1_bot.py", 247, None),
    ("AI SKW1 Detector", "38_ai_skw1_bot.py", 255, None),
    ("AI XSM1 Detector", "39_ai_xsm1_bot.py", 263, None),
    ("Trailing Close Bot", "40_trailing_close_bot.py", 271, None),
    ("Liq Collector", "41_liq_collector.py", 279, None),
    # ODS1 (T-2026-KYT-9050-106, PR #276) landed on main first; FIF2 reserved 291
    # and left 283 free for exactly that, so monotonicity holds under either
    # merge order and both entries now sit in it.
    ("AI ODS1 Detector", "42_ai_ods1_bot.py", 283, None),
    ("AI FIF2 Mirror", "43_ai_fif2_bot.py", 291, None),
]

# Groups rendered by the dashboard CSS (.group-core/.group-ai/.group-strategy/
# .group-logger) and the filter. A group outside this set produces an
# unstyled badge — that would be an unintended UI change.
KNOWN_GROUPS = {"core", "ai", "strategy", "logger"}


# ── Single Source ────────────────────────────────────────────────────────────


def test_watchdog_consumes_the_central_fleet():
    assert wd.PROCESSES_TO_RUN == fleet.FLEET


def test_dashboard_consumes_the_central_fleet():
    assert dash.PROCESSES == fleet.FLEET


def test_watchdog_and_dashboard_agree():
    # The core of the drift regression: before the fix, the dashboard was missing 26-34.
    assert wd.PROCESSES_TO_RUN == dash.PROCESSES


# ── No behaviour change to the watchdog ──────────────────────────────────────


def test_watchdog_view_is_unchanged():
    view = [(p["name"], p["script"], p["start_delay"], p["restart_interval"]) for p in wd.PROCESSES_TO_RUN]
    assert view == EXPECTED_WATCHDOG_VIEW


def test_start_delays_are_monotonic():
    # The watchdog starts in ascending start_delay order; list order == start
    # order. Non-monotonic delays would silently reorder the staggering.
    delays = [p["start_delay"] for p in fleet.FLEET]
    assert delays == sorted(delays)


# ── Field contract ───────────────────────────────────────────────────────────


def test_every_entry_has_the_full_field_contract():
    for p in fleet.FLEET:
        assert set(p) == {"name", "script", "group", "start_delay", "restart_interval"}, p


def test_groups_are_dashboard_renderable():
    for p in fleet.FLEET:
        assert p["group"] in KNOWN_GROUPS, p


def test_scripts_are_unique():
    scripts = [p["script"] for p in fleet.FLEET]
    assert len(scripts) == len(set(scripts))

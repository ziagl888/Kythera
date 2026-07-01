import os
import subprocess
import time
import sys
import logging
import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - WATCHDOG - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("watchdog.log", encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Dashboard läuft auf diesem Port — wird beim Start automatisch started.
DASHBOARD_SCRIPT = "dashboard.py"
DASHBOARD_PORT   = 5000

# 'restart_interval': Sekunden bis zum geplanten RAM-Recycling-Restart (None = nie).
# 'start_delay':      seconds to wait before first start, staggered so that
#                     not all 538 coins × 24 bots hammer the DB simultaneously.
PROCESSES_TO_RUN = [
    {"name": "Data Ingestion",    "script": "1_data_ingestion.py",        "restart_interval": None,  "start_delay": 0},
    {"name": "Chart Data Service","script": "chart_data_service.py",      "restart_interval": None,  "start_delay": 3},
    {"name": "Indicator Engine",  "script": "2_indicator_engine.py",      "restart_interval": 21600, "start_delay": 5},
    {"name": "Detectors",         "script": "3_detectors.py",             "restart_interval": 21600, "start_delay": 5},
    {"name": "Telegram Bot",      "script": "4_telegram_bot.py",          "restart_interval": None,  "start_delay": 5},
    {"name": "Trade Monitor",     "script": "5_trade_monitor.py",         "restart_interval": None,  "start_delay": 5},
    {"name": "Housekeeping",      "script": "6_housekeeping.py",          "restart_interval": None,  "start_delay": 10},
    {"name": "Pattern detector",  "script": "7_pattern_detector.py",      "restart_interval": None,  "start_delay": 15},
    {"name": "AI Trade Monitor",  "script": "8_ai_trade_monitor.py",      "restart_interval": None,  "start_delay": 23},
    {"name": "AI SR Bot",         "script": "9_ai_sr_bot.py",             "restart_interval": None,  "start_delay": 31},
    {"name": "Pump Dump Detector","script": "10_pump_dump_detector.py",   "restart_interval": None,  "start_delay": 39},
    {"name": "AI MIS1 Detector",  "script": "11_ai_mis_bot.py",           "restart_interval": None,  "start_delay": 47},
    {"name": "AI ATS1 Detector",  "script": "12_ai_ats_bot.py",           "restart_interval": None,  "start_delay": 55},
    {"name": "AI RUB1 Detector",  "script": "13_ai_rub_bot.py",           "restart_interval": None,  "start_delay": 63},
    {"name": "AI ATB1 Detector",  "script": "14_ai_atb_bot.py",           "restart_interval": None,  "start_delay": 71},
    {"name": "AI AIM1 Detector",  "script": "15_ai_master_bot.py",        "restart_interval": None,  "start_delay": 79},
    {"name": "SMC FOREX Detector","script": "16_smc_forex_metals_bot.py", "restart_interval": None,  "start_delay": 87},
    {"name": "Mayank Bot",        "script": "17_mayank_bot.py",           "restart_interval": None,  "start_delay": 95},
    {"name": "AI ABR1 Detector",  "script": "18_ai_abr1_bot.py",          "restart_interval": None,  "start_delay": 103},
    {"name": "Whale logger Bot",  "script": "19_whale_logger_bot.py",     "restart_interval": None,  "start_delay": 111},
    {"name": "Funding logger Bot","script": "20_funding_logger_bot.py",   "restart_interval": None,  "start_delay": 119},
    {"name": "BTC SMC Bot",       "script": "21_btc_smc_strategy.py",     "restart_interval": None,  "start_delay": 127},
    # {"name": "IP Pattern Bot",  "script": "22_ip_pattern_bot.py",       "restart_interval": None,  "start_delay": 135},
    {"name": "Market Tracker",    "script": "23_market_tracker.py",       "restart_interval": None,  "start_delay": 135},
    {"name": "Quasimodo Bot",     "script": "24_quasimodo_bot.py",        "restart_interval": None,  "start_delay": 143},
    {"name": "TD & BB Bot",       "script": "25_smc_ml_sniper.py",        "restart_interval": None,  "start_delay": 151},
    # ── Regime-Orchestrator (v5) ──────────────────────────────────────────────
    {"name": "Regime Detector",   "script": "26_regime_detector.py",      "restart_interval": None,  "start_delay": 160},
    {"name": "Bot Regime Analyzer","script": "27_bot_regime_analyzer.py", "restart_interval": None,  "start_delay": 167},
    {"name": "Signal Orchestrator","script": "28_signal_orchestrator.py", "restart_interval": None,  "start_delay": 175},
    {"name": "UFI1 Fib Bot",        "script": "29_ufi1_bot.py",            "restart_interval": None,  "start_delay": 183},
]

running_processes: dict = {}
_dashboard_proc: subprocess.Popen | None = None


# ── Dashboard ────────────────────────────────────────────────────────────────

def start_dashboard() -> None:
    """Startet das Web-Dashboard als Hintergrundprozess."""
    global _dashboard_proc

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), DASHBOARD_SCRIPT)
    if not os.path.exists(script):
        logger.warning(f"⚠️  dashboard.py not found ({script}) — Dashboard wird nicht started.")
        return

    # Bereits laufend?
    if _dashboard_proc and _dashboard_proc.poll() is None:
        return

    # FIX (#70): Previously stdout/stderr → DEVNULL so if the dashboard crashed,
    # war der Grund komplett unsichtbar. Jetzt in logs/dashboard.log,
    # damit der User after einem Crash afterschauen kann was passiert ist.
    os.makedirs("logs", exist_ok=True)
    dashboard_log = open("logs/dashboard.log", "a")
    dashboard_log.write(
        f"\n=== Dashboard started {datetime.datetime.now(datetime.timezone.utc).isoformat()} ===\n"
    )
    dashboard_log.flush()

    _dashboard_proc = subprocess.Popen(
        [sys.executable, script],
        stdout=dashboard_log,
        stderr=subprocess.STDOUT,  # stderr auch in gleiche Log-Datei
    )
    logger.info(f"🌐 Dashboard started (PID {_dashboard_proc.pid}) → http://localhost:{DASHBOARD_PORT} | Log: logs/dashboard.log")


def stop_dashboard() -> None:
    """Stops the dashboard cleanly."""
    global _dashboard_proc
    if _dashboard_proc and _dashboard_proc.poll() is None:
        _dashboard_proc.terminate()
        try:
            _dashboard_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _dashboard_proc.kill()
        logger.info("🛑 Dashboard stopped.")
    _dashboard_proc = None


def check_dashboard() -> None:
    """Restarts the dashboard if it has crashed."""
    global _dashboard_proc
    if _dashboard_proc and _dashboard_proc.poll() is not None:
        logger.warning("💥 Dashboard abgestürzt — Restart...")
        start_dashboard()


# ── Bot Prozesse ─────────────────────────────────────────────────────────────

def start_process(process_info: dict) -> None:
    name = process_info["name"]
    script_path = process_info["script"]
    logger.info(f"🚀 Starting Prozess: {name} ({script_path})")
    p = subprocess.Popen([sys.executable, script_path])
    running_processes[name] = {
        "process": p,
        "info": process_info,
        "start_time": time.time(),
    }


# FIX: Crash tracking for exponential backoff — previously the watchdog
# restarted crashing bots immediately, causing an infinite loop with
# DB hammering on config/import errors.
_crash_history: dict = {}  # name -> list of crash timestamps


def _compute_restart_delay(name: str) -> float:
    """Returns backoff delay based on crash frequency in the last hour."""
    now_ts = time.time()
    history = _crash_history.setdefault(name, [])
    # Alte entries (>1h) verwerfen
    history[:] = [t for t in history if now_ts - t < 3600]
    history.append(now_ts)

    crashes_last_hour = len(history)
    # Schedule: 1st crash immediately, 2nd 15s, 3rd 60s, 4th 300s, 5th+ → 900s
    schedule = [0, 15, 60, 300, 900]
    idx = min(crashes_last_hour - 1, len(schedule) - 1)
    delay = schedule[idx]
    if crashes_last_hour >= 5:
        logger.error(f"⚠️  {name} has crashed {crashes_last_hour}times in the last hour! "
                     f"Waiting {delay}s before next restart.")
    return delay


def kill_process(name: str) -> None:
    if name not in running_processes:
        return
    p = running_processes[name]["process"]
    logger.warning(f"🛑 Stopping process: {name}...")
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.error(f"⚠️ {name} not responding. Forcing kill!")
        p.kill()
    del running_processes[name]
    logger.info(f"✅ {name} stopped successfully.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("🛡️ System Watchdog started.")

    # Dashboard zuerst starten — ist sofort erreichbar während die Bots hochfahren.
    start_dashboard()

    # Starting bots in staggered sequence.
    sorted_procs = sorted(PROCESSES_TO_RUN, key=lambda p: p.get("start_delay", 0))
    last_start = 0
    for p_info in sorted_procs:
        delay = p_info.get("start_delay", 0)
        wait = delay - last_start
        if wait > 0:
            time.sleep(wait)
        start_process(p_info)
        last_start = delay

    total_delay = sorted_procs[-1].get("start_delay", 0) if sorted_procs else 0
    logger.info(f"🟢 Alle Systeme started (gestaffelt über {total_delay}s). Starting monitoring...")

    try:
        while True:
            current_time = time.time()

            # Dashboard-Crash-Check
            check_dashboard()

            # Bot-Crash-Check
            for p_info in PROCESSES_TO_RUN:
                name = p_info["name"]

                if name not in running_processes:
                    logger.error(f"🚨 Prozess {name} fehlt! Starting neu...")
                    start_process(p_info)
                    continue

                tracker = running_processes[name]
                p = tracker["process"]

                return_code = p.poll()
                if return_code is not None:
                    logger.error(f"💥 CRASH: {name} (Code: {return_code}). Emergency restart!")
                    del running_processes[name]
                    # FIX: Backoff vor Restart, um to limit crash loops.
                    delay = _compute_restart_delay(name)
                    if delay > 0:
                        logger.info(f"⏳ Waiting {delay}s vor Restart von {name} (crash protection)...")
                        time.sleep(delay)
                    start_process(p_info)
                    continue

                restart_interval = p_info.get("restart_interval")
                if restart_interval:
                    uptime = current_time - tracker["start_time"]
                    if uptime >= restart_interval:
                        logger.info(f"♻️ Geplanter Restart: {name} (Uptime: {uptime / 3600:.1f}h)")
                        kill_process(name)
                        start_process(p_info)

            time.sleep(10)

    except KeyboardInterrupt:
        logger.info("🛑 Watchdog stopped (Strg+C). Shutting down all systems...")
        for name in list(running_processes.keys()):
            kill_process(name)
        stop_dashboard()
        logger.info("🏁 System fully offline.")


if __name__ == "__main__":
    main()

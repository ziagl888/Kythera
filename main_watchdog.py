import atexit
import ctypes
import datetime
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Any

import psutil

from core.health_monitor import run_health_checks
from core.process_control import consume_restart, is_parked

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - WATCHDOG - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("watchdog.log", encoding='utf-8')],
)
logger = logging.getLogger(__name__)

# Dashboard läuft auf diesem Port — wird beim Start automatisch started.
DASHBOARD_SCRIPT = "dashboard.py"
DASHBOARD_PORT = 5000

# 'restart_interval': Sekunden bis zum geplanten RAM-Recycling-Restart (None = nie).
# 'start_delay':      seconds to wait before first start, staggered so that
#                     not all 538 coins × 24 bots hammer the DB simultaneously.
PROCESSES_TO_RUN: list[dict[str, Any]] = [
    {"name": "Data Ingestion", "script": "1_data_ingestion.py", "restart_interval": None, "start_delay": 0},
    {"name": "Chart Data Service", "script": "chart_data_service.py", "restart_interval": None, "start_delay": 3},
    {"name": "Indicator Engine", "script": "2_indicator_engine.py", "restart_interval": 21600, "start_delay": 5},
    {"name": "Detectors", "script": "3_detectors.py", "restart_interval": 21600, "start_delay": 5},
    {"name": "Telegram Bot", "script": "4_telegram_bot.py", "restart_interval": None, "start_delay": 5},
    {"name": "Trade Monitor", "script": "5_trade_monitor.py", "restart_interval": None, "start_delay": 5},
    {"name": "Housekeeping", "script": "6_housekeeping.py", "restart_interval": None, "start_delay": 10},
    {"name": "Pattern detector", "script": "7_pattern_detector.py", "restart_interval": None, "start_delay": 15},
    {"name": "AI Trade Monitor", "script": "8_ai_trade_monitor.py", "restart_interval": None, "start_delay": 23},
    {"name": "AI SR Bot", "script": "9_ai_sr_bot.py", "restart_interval": None, "start_delay": 31},
    {"name": "Pump Dump Detector", "script": "10_pump_dump_detector.py", "restart_interval": None, "start_delay": 39},
    {"name": "AI MIS1 Detector", "script": "11_ai_mis_bot.py", "restart_interval": None, "start_delay": 47},
    {"name": "AI ATS1 Detector", "script": "12_ai_ats_bot.py", "restart_interval": None, "start_delay": 55},
    {"name": "AI RUB1 Detector", "script": "13_ai_rub_bot.py", "restart_interval": None, "start_delay": 63},
    {"name": "AI ATB1 Detector", "script": "14_ai_atb_bot.py", "restart_interval": None, "start_delay": 71},
    {"name": "AI AIM2 Detector", "script": "15_ai_master_bot.py", "restart_interval": None, "start_delay": 79},
    {"name": "SMC FOREX Detector", "script": "16_smc_forex_metals_bot.py", "restart_interval": None, "start_delay": 87},
    {"name": "Mayank Bot", "script": "17_mayank_bot.py", "restart_interval": None, "start_delay": 95},
    {"name": "AI ABR1 Detector", "script": "18_ai_abr1_bot.py", "restart_interval": None, "start_delay": 103},
    {"name": "Whale logger Bot", "script": "19_whale_logger_bot.py", "restart_interval": None, "start_delay": 111},
    {"name": "Funding logger Bot", "script": "20_funding_logger_bot.py", "restart_interval": None, "start_delay": 119},
    {"name": "BTC SMC Bot", "script": "21_btc_smc_strategy.py", "restart_interval": None, "start_delay": 127},
    # {"name": "IP Pattern Bot",  "script": "22_ip_pattern_bot.py",       "restart_interval": None,  "start_delay": 135},
    {"name": "Market Tracker", "script": "23_market_tracker.py", "restart_interval": None, "start_delay": 135},
    {"name": "Quasimodo Bot", "script": "24_quasimodo_bot.py", "restart_interval": None, "start_delay": 143},
    {"name": "TD & BB Bot", "script": "25_smc_ml_sniper.py", "restart_interval": None, "start_delay": 151},
    # ── Regime-Orchestrator (v5) ──────────────────────────────────────────────
    {"name": "Regime Detector", "script": "26_regime_detector.py", "restart_interval": None, "start_delay": 160},
    {
        "name": "Bot Regime Analyzer",
        "script": "27_bot_regime_analyzer.py",
        "restart_interval": None,
        "start_delay": 167,
    },
    {
        "name": "Signal Orchestrator",
        "script": "28_signal_orchestrator.py",
        "restart_interval": None,
        "start_delay": 175,
    },
    {"name": "UFI1 Fib Bot", "script": "29_ufi1_bot.py", "restart_interval": None, "start_delay": 183},
    # ── Research-Bots (Report 15: S6/S8/S10/S11 — Channel CH_NEW_IDEAS) ──────
    {"name": "AI PEX1 Detector", "script": "30_ai_pex1_bot.py", "restart_interval": None, "start_delay": 191},
    {"name": "AI FMR1 Detector", "script": "31_ai_fmr1_bot.py", "restart_interval": None, "start_delay": 199},
    {"name": "AI TRM1 Detector", "script": "32_ai_trm1_bot.py", "restart_interval": None, "start_delay": 207},
    {"name": "AI FIF1 Detector", "script": "33_ai_fif1_bot.py", "restart_interval": None, "start_delay": 215},
]

running_processes: dict = {}
_dashboard_proc: subprocess.Popen | None = None

# Basenames aller Fleet-Skripte (Bots + Dashboard) — für die Orphan-Detection
# (P0.2): ein von uns gespawnter python-Prozess trägt eines dieser Skripte in
# seiner Cmdline. Läuft so ein Prozess ohne uns als Parent, ist er verwaist.
FLEET_SCRIPTS: frozenset = frozenset([os.path.basename(p["script"]) for p in PROCESSES_TO_RUN] + [DASHBOARD_SCRIPT])

# Named Mutex Handle (Windows) — muss über die gesamte Prozess-Lebensdauer
# referenziert bleiben, sonst gibt der GC das Handle frei und die zweite
# Instanz käme durch. P0.2.
_instance_mutex = None

# Windows GetLastError-Code für "Mutex existiert bereits" → zweite Fleet-Instanz.
_ERROR_ALREADY_EXISTS = 183


# ── Single-Instance-Guard (P0.2) ─────────────────────────────────────────────


def _acquire_single_instance_lock() -> None:
    """Verhindert eine zweite Watchdog-Instanz (P0.2 — geld-kritisch).

    Ein zweiter Watchdog spawnt eine zweite komplette Fleet → jedes Cornix-Signal
    doppelt. Windows Named Mutex (Global\\-Namespace, session-übergreifend): existiert
    er schon, läuft bereits ein Watchdog → hart abbrechen. Nur ctypes/kernel32,
    kein pywin32.
    """
    global _instance_mutex
    if os.name != "nt":
        # Non-Windows (Tests/Dev): Mutex-Guard nicht verfügbar — Orphan-Detection
        # bleibt die zweite Verteidigungslinie.
        return
    try:
        _instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\KytheraWatchdog")
        last_error = ctypes.windll.kernel32.GetLastError()
    except Exception as e:  # noqa: BLE001 — Guard darf den Start nie mit einem Traceback killen.
        logger.warning(f"⚠️  Single-Instance-Mutex nicht setzbar ({e}) — Start ohne Mutex-Guard.")
        return

    # Review-Härtung P0.2: CreateMutexW==NULL mit ERROR_ACCESS_DENIED (5) ist der
    # kanonische Fall "Mutex existiert, gehört aber einem anderen User/Elevation-
    # Level" (z.B. Task-Scheduler vs. interaktive Session) — auch das ist eine
    # laufende zweite Instanz.
    _ERROR_ACCESS_DENIED = 5
    if last_error == _ERROR_ALREADY_EXISTS or (not _instance_mutex and last_error == _ERROR_ACCESS_DENIED):
        logger.error(
            "🚨 Ein zweiter Watchdog läuft bereits (Mutex 'Global\\KytheraWatchdog' existiert, "
            f"GetLastError={last_error}) — Abbruch, um doppelte Fleet/doppelte Cornix-Signale "
            "zu verhindern (P0.2)."
        )
        sys.exit(1)
    if not _instance_mutex:
        # NULL-Handle aus anderem Grund: kein Beweis für eine zweite Instanz, aber
        # auch kein Lock — weiterlaufen und warnen; Orphan-Detection bleibt die
        # zweite Verteidigungslinie.
        logger.warning(f"⚠️  CreateMutexW lieferte NULL (GetLastError={last_error}) — Start ohne Mutex-Guard.")


def _terminate_orphan_fleet() -> None:
    """Killt verwaiste Fleet-Prozesse eines abgestürzten Vor-Watchdogs (P0.2).

    Ein hartes ``taskkill /F`` auf den alten Watchdog läuft nicht durch dessen
    SIGTERM-Handler → die Kinder überleben verwaist weiter und produzieren
    weiter Signale. Beim Start suchen wir daher python-Prozesse, deren Cmdline
    ein Fleet-Skript enthält und die NICHT unsere eigenen Kinder sind, und
    beenden sie (5s Grace, dann kill).
    """
    self_pid = os.getpid()
    try:
        own_children = {c.pid for c in psutil.Process(self_pid).children(recursive=True)}
    except psutil.Error:
        own_children = set()

    orphans = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid == self_pid or proc.pid in own_children:
                continue
            name = (proc.info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmdline = proc.info.get("cmdline") or []
            if any(os.path.basename(tok) in FLEET_SCRIPTS for tok in cmdline):
                orphans.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not orphans:
        return

    logger.warning(
        f"🧟 {len(orphans)} verwaiste Fleet-Prozesse gefunden (PIDs: "
        f"{[p.pid for p in orphans]}) — beende sie vor dem Start (P0.2)."
    )
    for proc in orphans:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    _gone, alive = psutil.wait_procs(orphans, timeout=5)
    for proc in alive:
        try:
            logger.warning(f"⚠️  Orphan PID {proc.pid} reagiert nicht — force kill.")
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


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
    dashboard_log.write(f"\n=== Dashboard started {datetime.datetime.now(datetime.timezone.utc).isoformat()} ===\n")
    dashboard_log.flush()

    _dashboard_proc = subprocess.Popen(
        [sys.executable, script],
        stdout=dashboard_log,
        stderr=subprocess.STDOUT,  # stderr auch in gleiche Log-Datei
    )
    logger.info(
        f"🌐 Dashboard started (PID {_dashboard_proc.pid}) → http://localhost:{DASHBOARD_PORT} | Log: logs/dashboard.log"
    )


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

# P1.37: earliest wall-clock time a crashed process may be restarted.
# The backoff used to be a time.sleep() inside the per-process loop, which froze
# the ENTIRE monitor — for up to 900s no other bot was supervised, no park marker
# was honoured, no dashboard restart was consumed, no health check ran. The delay
# is now a per-process deadline: the loop keeps turning and simply skips this one
# process until its deadline passes.
_restart_not_before: dict[str, float] = {}  # name -> epoch seconds


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
        logger.error(
            f"⚠️  {name} has crashed {crashes_last_hour}times in the last hour! Waiting {delay}s before next restart."
        )
    return delay


def supervise_process(p_info: dict, current_time: float) -> None:
    """One supervision pass over a single bot. Never blocks.

    Extracted from main()'s monitor loop (P1.37) so the crash-backoff deadline
    is testable without driving the whole watchdog. Order of the branches is
    load-bearing:

      1. parked  — the operator's stop wins over everything, including a
         pending crash backoff.
      2. dashboard restart — an explicit operator action overrides the backoff.
      3. backoff deadline — skip this bot, but keep the loop turning for all
         the others.
      4. missing / crashed / scheduled restart.
    """
    name = p_info["name"]
    script = p_info["script"]

    # Parking: a dashboard-initiated stop that must STAY stopped.
    # The watchdog is the single actuator — it stops the bot here and
    # does NOT revive it, fixing the old "stop gets undone in 10s" bug.
    if is_parked(script):
        # P1.37: a park during the backoff window must win. The old code slept
        # through the parking and then started the bot anyway; now the deadline
        # is dropped and the bot stays down.
        _restart_not_before.pop(name, None)
        if name in running_processes:
            if running_processes[name]["process"].poll() is None:
                logger.info(f"⏸️  {name} ist geparkt — stoppe.")
                kill_process(name)
            else:
                del running_processes[name]
        return

    # One-shot restart requested from the dashboard. An explicit operator
    # action overrides the crash backoff (P1.37).
    if consume_restart(script):
        logger.info(f"♻️ {name} — Restart über Dashboard angefordert.")
        _restart_not_before.pop(name, None)
        if name in running_processes and running_processes[name]["process"].poll() is None:
            kill_process(name)
        start_process(p_info)
        return

    # P1.37: crash backoff as a deadline, not a sleep.
    not_before = _restart_not_before.get(name)
    if not_before is not None:
        if current_time < not_before:
            return
        _restart_not_before.pop(name, None)
        logger.info(f"⏱️  {name} — Backoff abgelaufen, starte neu.")

    if name not in running_processes:
        logger.error(f"🚨 Prozess {name} fehlt! Starting neu...")
        start_process(p_info)
        return

    tracker = running_processes[name]
    p = tracker["process"]

    return_code = p.poll()
    if return_code is not None:
        logger.error(f"💥 CRASH: {name} (Code: {return_code}). Emergency restart!")
        del running_processes[name]
        # FIX: Backoff vor Restart, um to limit crash loops.
        # P1.37: kein time.sleep() — das fror die gesamte Schleife ein (bis 900s
        # keine anderen Restarts, keine Park-Marker, kein Dashboard, keine
        # Health-Checks). Deadline setzen und weiterlaufen; der Restart passiert
        # in dem Zyklus, in dem die Deadline abgelaufen ist — und erst nachdem
        # der Park-Check oben erneut gelaufen ist.
        delay = _compute_restart_delay(name)
        if delay > 0:
            _restart_not_before[name] = current_time + delay
            logger.info(f"⏳ Waiting {delay}s vor Restart von {name} (crash protection)...")
            return
        start_process(p_info)
        return

    restart_interval = p_info.get("restart_interval")
    if restart_interval:
        uptime = current_time - tracker["start_time"]
        if uptime >= restart_interval:
            logger.info(f"♻️ Geplanter Restart: {name} (Uptime: {uptime / 3600:.1f}h)")
            kill_process(name)
            start_process(p_info)


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


# ── Graceful Shutdown ────────────────────────────────────────────────────────


def shutdown_all() -> None:
    """Terminate every supervised bot and the dashboard. Idempotent.

    P0.2-Härtung: pro Kind gekapselt — schlägt das Beenden eines Prozesses fehl,
    darf das die Terminierung der restlichen Kinder nicht abbrechen (sonst
    überleben verwaiste Prozesse den Watchdog-Tod).
    """
    for name in list(running_processes.keys()):
        try:
            kill_process(name)
        except Exception:  # noqa: BLE001 — Teardown muss alle Kinder erreichen.
            logger.exception(f"⚠️  Fehler beim Beenden von {name} — fahre mit den übrigen fort.")
            running_processes.pop(name, None)
    stop_dashboard()


_shutting_down = False


def _handle_shutdown_signal(signum, frame) -> None:
    """SIGTERM/SIGINT/SIGBREAK → graceful teardown of the whole fleet.

    Previously the watchdog only cleaned up on Ctrl+C (KeyboardInterrupt); a
    service stop / taskkill (SIGTERM) orphaned every child process.
    """
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    logger.info(f"🛑 Signal {signum} empfangen — fahre alle Systeme herunter...")
    shutdown_all()
    logger.info("🏁 System fully offline.")
    sys.exit(0)


def _install_signal_handlers() -> None:
    # SIGBREAK is Windows-only (Ctrl+Break); SIGTERM covers Linux/service stop.
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle_shutdown_signal)
        except (ValueError, OSError):
            # Not in the main thread — non-fatal, KeyboardInterrupt still works.
            pass


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    logger.info("🛡️ System Watchdog started.")

    # P0.2: Zweite Watchdog-Instanz hart verhindern (Mutex), dann verwaiste Kinder
    # eines abgestürzten Vor-Watchdogs aufräumen — beides VOR dem Spawn der Fleet.
    _acquire_single_instance_lock()
    _terminate_orphan_fleet()

    _install_signal_handlers()
    # Letzte Verteidigungslinie: bei jedem Exit-Pfad (auch unerwartet) die Kinder
    # terminieren, bevor der Watchdog stirbt (P0.2). shutdown_all ist idempotent.
    atexit.register(shutdown_all)

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
        if is_parked(p_info["script"]):
            logger.info(f"⏸️  {p_info['name']} ist geparkt — Start übersprungen.")
            last_start = delay
            continue
        start_process(p_info)
        last_start = delay

    total_delay = sorted_procs[-1].get("start_delay", 0) if sorted_procs else 0
    logger.info(f"🟢 Alle Systeme started (gestaffelt über {total_delay}s). Starting monitoring...")

    last_health_check = 0.0

    try:
        while True:
            current_time = time.time()

            # Health-Monitoring (1x/min): Daten-Staleness (P2.47, mit Auto-Restart
            # der Ingestion), CPU-Dauerlast (WS-Disconnect-Ursache), Outbox-Failures
            # (P2.11). Alerts via TELEGRAM_ALERT_CHAT_ID + watchdog.log.
            if current_time - last_health_check >= 60:
                last_health_check = current_time
                run_health_checks()

            # Dashboard-Crash-Check
            check_dashboard()

            # Bot-Crash-Check
            for p_info in PROCESSES_TO_RUN:
                supervise_process(p_info, current_time)

            time.sleep(10)

    except KeyboardInterrupt:
        # Fallback if signal handlers could not be installed (e.g. non-main thread).
        logger.info("🛑 Watchdog stopped (Strg+C). Shutting down all systems...")
        shutdown_all()
        logger.info("🏁 System fully offline.")


if __name__ == "__main__":
    main()

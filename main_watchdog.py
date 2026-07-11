import atexit
import ctypes
import datetime
import logging
import os
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any

import psutil

from core.fleet import FLEET
from core.health_monitor import run_health_checks
from core.process_control import consume_restart, is_parked

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - WATCHDOG - %(levelname)s - %(message)s',
    # P3.2: RotatingFileHandler at the same path so the dashboard viewer
    # (dashboard.py) and health_monitor keep reading watchdog.log.
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler("watchdog.log", maxBytes=10 * 1024 * 1024, backupCount=3, encoding='utf-8'),
    ],
)
logger = logging.getLogger(__name__)

# Dashboard läuft auf diesem Port — wird beim Start automatisch started.
DASHBOARD_SCRIPT = "dashboard.py"
DASHBOARD_PORT = 5000

# Die Fleet-Definition (Name/Script/Group/Delays) lebt zentral in core/fleet.py;
# Watchdog UND Dashboard konsumieren dieselbe Liste (T-2026-CU-9050-091, R2(a)).
# Der Watchdog nutzt name/script/start_delay/restart_interval — das zusätzliche
# group-Feld (nur für die Dashboard-Anzeige) ist hier ein No-op. Reihenfolge und
# Delays sind identisch zur früheren Inline-Liste; keine Verhaltensänderung.
PROCESSES_TO_RUN: list[dict[str, Any]] = FLEET

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


# ── Graceful-Shutdown-Konfiguration (P2.48) ──────────────────────────────────
# terminate() ist auf Windows ein harter TerminateProcess: der Bot bekommt keine
# Chance aufzuräumen und — kritisch — die ProcessPool-Worker der Indicator-Engine
# (2_indicator_engine.py, ProcessPoolExecutor) überleben den Parent-Kill als
# Waisen und rechnen weiter → Doppel-Compute-Fenster. Wir starten jeden Bot daher
# in EINER eigenen Prozessgruppe (CREATE_NEW_PROCESS_GROUP) und schicken beim Stop
# ein CTRL_BREAK_EVENT an die GANZE Gruppe — das erreicht den Bot UND seine
# Worker-Kinder, anders als terminate(), das nur den Bot selbst trifft. Ohne die
# eigene Gruppe ginge das Console-Signal an die ganze Konsole inkl. Watchdog.
_IS_WINDOWS = os.name == "nt"
# CREATE_NEW_PROCESS_GROUP existiert nur auf Windows; 0 ist der neutrale
# creationflags-Wert auf POSIX (der Parameter existiert dort, muss aber 0 sein).
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
# CTRL_BREAK_EVENT existiert nur auf Windows.
_CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", None)
# Sekunden, die ein Bot nach dem Graceful-Signal zum sauberen Beenden bekommt,
# bevor hart nachgetreten wird.
GRACEFUL_STOP_TIMEOUT_S = int(os.getenv("KYTHERA_GRACEFUL_STOP_S", "10"))


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
        creationflags=_CREATE_NEW_PROCESS_GROUP,  # eigene Gruppe, konsistent zu den Bots (P2.48)
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
    # Eigene Prozessgruppe (P2.48): so erreicht das Stop-CTRL_BREAK den Bot samt
    # seiner ProcessPool-Worker, ohne die Watchdog-Konsole mitzutreffen.
    p = subprocess.Popen([sys.executable, script_path], creationflags=_CREATE_NEW_PROCESS_GROUP)
    running_processes[name] = {
        "process": p,
        "info": process_info,
        "start_time": time.time(),
    }


# FIX: Crash tracking for exponential backoff — previously the watchdog
# restarted crashing bots immediately, causing an infinite loop with
# DB hammering on config/import errors.
_crash_history: dict = {}  # name -> list of crash timestamps

# ── Hang-/Heartbeat-Detection (P2.47) ────────────────────────────────────────
# The watchdog only ever checked process EXISTENCE (poll()). A bot that hangs —
# alive but wedged on a dead socket / deadlock, producing nothing — stayed
# "green" while the fleet traded on stale data (Step-2-proven: ingestion 6h dead
# at a green watchdog). Data-staleness itself is already covered DB-side by
# core.health_monitor (candle age → auto-restart of ingestion). This adds the
# GENERIC signal: a supervised process whose log file has not advanced for
# HANG_LIMIT_S is wedged.
#
# Safe by construction (money path):
#   - The heartbeat is the process's OWN open log file, resolved mapping-free
#     from its open handles (no fragile script→logname table). A process with no
#     observable log file is EXEMPT — it can never be false-restarted.
#   - Auto-restart is DEFAULT-OFF: by default a hang only WARNs (operator
#     decides). Opt in per deployment via KYTHERA_WATCHDOG_HANG_AUTORESTART=1;
#     the restart then rides the SAME crash backoff, so a re-hanging bot backs
#     off instead of looping.
#   - A freshly (re)started bot gets a full HANG_LIMIT_S grace window before it
#     can be flagged.
# HANG_LIMIT_S <= 0 disables the check entirely.
HANG_LIMIT_S = int(os.getenv("KYTHERA_WATCHDOG_HANG_LIMIT_S", str(20 * 60)))
HANG_AUTORESTART = os.getenv("KYTHERA_WATCHDOG_HANG_AUTORESTART", "0") == "1"
_HANG_ALERT_COOLDOWN_S = 30 * 60
_hang_alerted: dict[str, float] = {}  # name -> epoch of last hang warning


def _resolve_heartbeat_log(pid: int) -> str | None:
    """Best-effort: the process's own open ``.log`` file, mapping-free.

    Prefers a file under ``logs/`` (core.logging_setup convention) over a
    root-level ``*.log`` (a few bots use a plain FileHandler). Returns None when
    nothing usable is open — that process is then exempt from hang-detection, so
    a bot that only logs to stdout is never mistaken for wedged.
    """
    try:
        open_files = psutil.Process(pid).open_files()
    except Exception:  # noqa: BLE001 — a heartbeat probe must never crash the watchdog.
        return None
    logs: list[str] = [f.path for f in open_files if f.path.lower().endswith(".log")]
    if not logs:
        return None
    logs.sort(key=lambda p: (os.sep + "logs" + os.sep) not in p.lower())
    return logs[0]


def check_heartbeat(p_info: dict, current_time: float) -> None:
    """One heartbeat pass over a single bot. Never blocks, never raises.

    Extracted so the hang deadline is testable without driving the whole
    watchdog (mirrors supervise_process).
    """
    if HANG_LIMIT_S <= 0:
        return
    name = p_info["name"]
    tracker = running_processes.get(name)
    if tracker is None:
        return  # missing / crashed → supervise_process owns that path.
    if tracker["process"].poll() is not None:
        return  # exited — a crash, not a hang.
    # Grace: give a freshly (re)started bot a full window to produce output.
    if current_time - tracker["start_time"] < HANG_LIMIT_S:
        return
    # Resolve + cache the heartbeat log once per process lifetime (open_files is
    # relatively expensive on Windows — never call it on the hot path).
    if "heartbeat_log" not in tracker:
        tracker["heartbeat_log"] = _resolve_heartbeat_log(tracker["process"].pid)
    log_path = tracker["heartbeat_log"]
    if not log_path:
        return  # no observable log → exempt.
    try:
        age = current_time - os.path.getmtime(log_path)
    except OSError:
        return
    if age < HANG_LIMIT_S:
        return

    if current_time - _hang_alerted.get(name, 0.0) >= _HANG_ALERT_COOLDOWN_S:
        _hang_alerted[name] = current_time
        logger.error(
            f"🫀 {name} lebt, aber {os.path.basename(log_path)} ist {age / 60:.0f} min still "
            f"(Limit {HANG_LIMIT_S // 60} min) — Prozess vermutlich wedged."
        )

    if not HANG_AUTORESTART:
        return  # WARNING-only default — operator decides on the money path.

    logger.warning(f"♻️ {name} — Hang-Restart (Auto, KYTHERA_WATCHDOG_HANG_AUTORESTART=1).")
    _hang_alerted.pop(name, None)
    kill_process(name)
    # Ride the existing crash backoff so a bot that keeps re-hanging backs off.
    delay = _compute_restart_delay(name)
    if delay > 0:
        _restart_not_before[name] = current_time + delay
    else:
        start_process(p_info)


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


def _request_graceful_stop(p: subprocess.Popen, name: str) -> bool:
    """Bittet ``p`` — auf Windows dessen ganze Prozessgruppe inkl. der
    ProcessPool-Worker — um ein geordnetes Herunterfahren (P2.48).

    POSIX: ``terminate()`` = SIGTERM ist bereits das geordnete Stop-Signal.
    Windows: ``CTRL_BREAK_EVENT`` an die Gruppe. Scheitert das — keine Konsole
    angehängt (Scheduled-Task-Start), Prozess nicht in eigener Gruppe, oder
    bereits beendet — fallen wir auf ``terminate()`` zurück; damit nie
    schlechter als der bisherige harte Kill.

    Returns True, wenn ein Graceful-Signal zugestellt wurde, sonst False.
    """
    if not _IS_WINDOWS:
        p.terminate()  # SIGTERM — auf POSIX bereits graceful
        return True
    ctrl_break = _CTRL_BREAK_EVENT
    if ctrl_break is None:
        # Kann auf echtem Windows nicht auftreten (CTRL_BREAK_EVENT existiert dort
        # immer); hält nur die Typen ehrlich und fällt sicher zurück.
        p.terminate()
        return False
    try:
        p.send_signal(ctrl_break)  # erreicht die ganze Prozessgruppe
        return True
    except (OSError, ValueError) as e:
        logger.warning(f"⚠️ {name}: CTRL_BREAK nicht zustellbar ({e}) — harter terminate().")
        try:
            p.terminate()
        except OSError:
            pass
        return False


def kill_process(name: str) -> None:
    if name not in running_processes:
        return
    p = running_processes[name]["process"]
    logger.warning(f"🛑 Stopping process: {name}...")
    graceful = _request_graceful_stop(p, name)
    try:
        p.wait(timeout=GRACEFUL_STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        if _IS_WINDOWS:
            sent = "CTRL_BREAK" if graceful else "terminate()"
        else:
            sent = "SIGTERM"
        logger.error(f"⚠️ {name} reagiert {GRACEFUL_STOP_TIMEOUT_S}s nach {sent} nicht — harter Kill!")
        p.kill()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error(f"⚠️ {name} überlebt selbst den harten Kill — ProcessPool-Waisen möglich.")
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

            # Bot-Crash-Check (Prozess-Existenz) + Hang-Check (P2.47:
            # lebt-aber-wedged via Log-Heartbeat).
            for p_info in PROCESSES_TO_RUN:
                supervise_process(p_info, current_time)
                check_heartbeat(p_info, current_time)

            time.sleep(10)

    except KeyboardInterrupt:
        # Fallback if signal handlers could not be installed (e.g. non-main thread).
        logger.info("🛑 Watchdog stopped (Strg+C). Shutting down all systems...")
        shutdown_all()
        logger.info("🏁 System fully offline.")


if __name__ == "__main__":
    main()

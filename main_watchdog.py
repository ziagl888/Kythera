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
from core.shadow_scanners import HOSTED_SCRIPTS

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

# Dashboard runs on this port — started automatically at startup.
DASHBOARD_SCRIPT = "dashboard.py"
DASHBOARD_PORT = 5000

# The fleet definition (name/script/group/delays) lives centrally in core/fleet.py;
# watchdog AND dashboard consume the same list (T-2026-CU-9050-091, R2(a)).
# The watchdog uses name/script/start_delay/restart_interval — the additional
# group field (only for the dashboard display) is a no-op here. Order and
# delays are identical to the former inline list; no behaviour change.
PROCESSES_TO_RUN: list[dict[str, Any]] = FLEET

running_processes: dict = {}
_dashboard_proc: subprocess.Popen | None = None

# Basenames of all fleet scripts (bots + dashboard) — for orphan detection
# (P0.2): a python process spawned by us carries one of these scripts in
# its cmdline. If such a process runs without us as parent, it is orphaned.
FLEET_SCRIPTS: frozenset = frozenset(
    [os.path.basename(p["script"]) for p in PROCESSES_TO_RUN]
    + [DASHBOARD_SCRIPT]
    # Scripts we no longer START but must still be able to REAP
    # (T-2026-KYT-9050-133): bots 36-39 moved into 45_shadow_scanner_runner.py.
    # The switchover happens at a watchdog restart, and the four old processes
    # are alive right up to that moment. Any of them that survives the tree stop
    # as an orphan would keep scanning next to the runner — two schedulers on
    # the same cooldown rows, i.e. a race for a duplicate LIVE post. They are
    # deliberately NOT in PROCESSES_TO_RUN, so they are never started; this set
    # is only read by the startup orphan sweep.
    + [os.path.basename(s) for s in HOSTED_SCRIPTS]
)

# The watchdog itself — NOT in FLEET_SCRIPTS (no PROCESSES_TO_RUN entry).
# For the mutex deadlock recovery (T-2026-CU-9050-127): a predecessor watchdog that
# survives a tree stop as a WMI orphan holds the single-instance mutex and blocks
# every restart. The new watchdog must be able to reap this orphan in a targeted way.
_WATCHDOG_SCRIPT: str = os.path.basename(__file__)

# Named mutex handle (Windows) — must stay referenced for the entire process
# lifetime, otherwise the GC frees the handle and a second
# instance could get through. P0.2.
_instance_mutex = None

# Windows GetLastError code for "mutex already exists" → second fleet instance.
_ERROR_ALREADY_EXISTS = 183


# ── Graceful shutdown configuration (P2.48) ──────────────────────────────────
# terminate() is a hard TerminateProcess on Windows: the bot gets no
# chance to clean up and — critically — the indicator engine's ProcessPool
# workers (2_indicator_engine.py, ProcessPoolExecutor) survive the parent kill as
# orphans and keep computing → double-compute window. We therefore start every bot
# in ITS OWN process group (CREATE_NEW_PROCESS_GROUP) and, on stop, send
# a CTRL_BREAK_EVENT to the WHOLE group — that reaches the bot AND its
# worker children, unlike terminate(), which only hits the bot itself. Without its
# own group the console signal would go to the entire console including the watchdog.
_IS_WINDOWS = os.name == "nt"
# CREATE_NEW_PROCESS_GROUP only exists on Windows; 0 is the neutral
# creationflags value on POSIX (the parameter exists there but must be 0).
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
# CREATE_NO_WINDOW (Windows) — the heartbeat probe child process should not
# flash a console window; 0 is the neutral value on POSIX.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# CTRL_BREAK_EVENT only exists on Windows.
_CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", None)
# Seconds a bot gets after the graceful signal to shut down cleanly,
# before it gets a hard follow-up.
GRACEFUL_STOP_TIMEOUT_S = int(os.getenv("KYTHERA_GRACEFUL_STOP_S", "10"))


# ── Single-instance guard (P0.2) ─────────────────────────────────────────────


def _acquire_single_instance_lock(_allow_orphan_reap: bool = True) -> None:
    """Prevents a second watchdog instance (P0.2 — money-critical).

    A second watchdog spawns a second complete fleet → every Cornix signal
    doubled. Windows named mutex (Global\\ namespace, cross-session): if it
    already exists, a watchdog is already running → hard abort. Only ctypes/kernel32,
    no pywin32.

    Orphan recovery (T-2026-CU-9050-127): a predecessor watchdog that survives a
    ``Stop-ScheduledTask`` tree stop as a WMI orphan keeps holding the mutex
    and used to block EVERY restart (deadlock: the orphan reaper only ran
    AFTER this guard, and it only reaps bots anyway, not the watchdog).
    Now: on a mutex conflict, targetedly reap orphaned WATCHDOG processes; if
    the holder really died (return > 0), the OS releases the named mutex →
    exactly ONE retry. Only if the holder is NOT reapable (different elevation/
    user, AccessDenied) does it fall back to the hard abort as before — no regression.
    Mutex-first stays the norm for a regular start: reaping only happens in the
    real conflict case.
    """
    global _instance_mutex
    if os.name != "nt":
        # Non-Windows (tests/dev): mutex guard not available — orphan detection
        # remains the second line of defence.
        return
    try:
        _instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\KytheraWatchdog")
        last_error = ctypes.windll.kernel32.GetLastError()
    except Exception as e:  # noqa: BLE001 — the guard must never kill startup with a traceback.
        logger.warning(f"⚠️  Could not set single-instance mutex ({e}) — starting without mutex guard.")
        return

    # Review hardening P0.2: CreateMutexW==NULL with ERROR_ACCESS_DENIED (5) is the
    # canonical case "mutex exists but belongs to a different user/elevation
    # level" (e.g. Task Scheduler vs. interactive session) — that too is a
    # running second instance.
    _ERROR_ACCESS_DENIED = 5
    if last_error == _ERROR_ALREADY_EXISTS or (not _instance_mutex and last_error == _ERROR_ACCESS_DENIED):
        if _allow_orphan_reap and _reap_orphans(frozenset([_WATCHDOG_SCRIPT])) > 0:
            # IMPORTANT: on ALREADY_EXISTS, CreateMutexW returned a valid handle
            # to the EXISTING mutex — this handle of ours keeps the named
            # object alive even if the orphan just died. Without
            # CloseHandle the retry would see ALREADY_EXISTS again. Only after reaping
            # (orphan's handle closed) AND CloseHandle (our handle closed) is the
            # last handle gone and the object destroyed.
            if _instance_mutex:
                try:
                    ctypes.windll.kernel32.CloseHandle(_instance_mutex)
                except Exception:  # noqa: BLE001
                    pass
                _instance_mutex = None
            logger.warning(
                "🧟 An orphaned predecessor watchdog held the single-instance mutex — reaped, "
                "one-time retry of the mutex acquisition (P0.2, T-2026-CU-9050-127)."
            )
            time.sleep(1)  # give the OS time to release the named mutex
            _acquire_single_instance_lock(_allow_orphan_reap=False)
            return
        logger.error(
            "🚨 A second watchdog is already running (mutex 'Global\\KytheraWatchdog' exists, "
            f"GetLastError={last_error}) — aborting to prevent a duplicate fleet/duplicate Cornix "
            "signals (P0.2)."
        )
        sys.exit(1)
    if not _instance_mutex:
        # NULL handle for another reason: no proof of a second instance, but
        # also no lock — keep running and warn; orphan detection remains the
        # second line of defence.
        logger.warning(f"⚠️  CreateMutexW returned NULL (GetLastError={last_error}) — starting without mutex guard.")


def _reap_orphans(script_names: frozenset) -> int:
    """Terminates non-child python processes whose cmdline names one of
    ``script_names``, and returns the number of processes ACTUALLY terminated (P0.2).

    A hard ``taskkill /F`` on an old process does not run through its
    SIGTERM handler → it/its children keep surviving as orphans. We therefore look
    for python processes whose cmdline contains a target script and that are NOT
    our own children (and not ourselves), and terminate them (5s grace, then
    kill). Shared between fleet orphan cleanup AND the mutex deadlock recovery
    (T-2026-CU-9050-127): the return value tells the latter whether the mutex holder
    really died (only then is the retry worthwhile). Elevation boundary: what the reaper
    is not allowed to kill (AccessDenied) does NOT count as terminated → no false retry.
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
            if any(os.path.basename(tok) in script_names for tok in cmdline):
                orphans.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not orphans:
        return 0

    logger.warning(
        f"🧟 Found {len(orphans)} orphaned processes (PIDs: {[p.pid for p in orphans]}) — terminating them (P0.2)."
    )
    for proc in orphans:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    _gone, alive = psutil.wait_procs(orphans, timeout=5)
    for proc in alive:
        try:
            logger.warning(f"⚠️  Orphan PID {proc.pid} is not responding — force kill.")
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # Really dead? (AccessDenied survivors do not count as terminated.)
    _gone2, still_alive = psutil.wait_procs(alive, timeout=3)
    return len(orphans) - len(still_alive)


def _terminate_orphan_fleet() -> None:
    """Reaps orphaned fleet processes from a crashed predecessor watchdog (P0.2)."""
    _reap_orphans(FLEET_SCRIPTS)


# ── Dashboard ────────────────────────────────────────────────────────────────


def start_dashboard() -> None:
    """Starts the web dashboard as a background process."""
    global _dashboard_proc

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), DASHBOARD_SCRIPT)
    if not os.path.exists(script):
        logger.warning(f"⚠️  dashboard.py not found ({script}) — dashboard will not be started.")
        return

    # Already running?
    if _dashboard_proc and _dashboard_proc.poll() is None:
        return

    # FIX (#70): Previously stdout/stderr → DEVNULL so if the dashboard crashed,
    # the reason was completely invisible. Now in logs/dashboard.log,
    # so the user can look after a crash to see what happened.
    os.makedirs("logs", exist_ok=True)
    dashboard_log = open("logs/dashboard.log", "a")
    dashboard_log.write(f"\n=== Dashboard started {datetime.datetime.now(datetime.timezone.utc).isoformat()} ===\n")
    dashboard_log.flush()

    _dashboard_proc = subprocess.Popen(
        [sys.executable, script],
        stdout=dashboard_log,
        stderr=subprocess.STDOUT,  # stderr also into the same log file
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
        logger.warning("💥 Dashboard crashed — restarting...")
        start_dashboard()


# ── Bot processes ─────────────────────────────────────────────────────────────


def start_process(process_info: dict) -> None:
    name = process_info["name"]
    script_path = process_info["script"]
    logger.info(f"🚀 Starting process: {name} ({script_path})")
    # Own process group (P2.48): so the stop CTRL_BREAK reaches the bot including
    # its ProcessPool workers, without also hitting the watchdog console.
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


# The psutil ``open_files()`` call used to discover a process's own log handle is
# a NATIVE C call that has intermittently access-violated (0xC0000005) on this
# Windows/Python 3.13 box, taking the ENTIRE watchdog down with it — and with the
# watchdog the outer supervision net, leaving the fleet running unsupervised
# (T-2026-KYT-9050-025; faulthandler stacks 2026-07-19 20:08 + 2026-07-22 12:50 both
# name open_files → _resolve_heartbeat_log). A native segfault cannot be caught
# with try/except, so the only safe way to keep using open_files() from the
# supervisor is to run it in a THROWAWAY CHILD process: a crash there returns a
# non-zero exit code and a hang is bounded by a timeout, and either way the parent
# treats the process as unresolvable (exempt) — exactly like a bot with no
# observable log. The probe runs once per process lifetime (check_heartbeat caches
# the result in the tracker), so the subprocess cost is negligible.
#
# Runs in the CHILD only: print the target PID's open ``.log`` handles, one per
# line. Any psutil failure → exit 4 (parent maps a non-zero exit to "exempt").
_HEARTBEAT_PROBE_SRC = (
    "import sys\n"
    "try:\n"
    "    import psutil\n"
    "    of = psutil.Process(int(sys.argv[1])).open_files()\n"
    "except Exception:\n"
    "    sys.exit(4)\n"
    "for f in of:\n"
    "    p = getattr(f, 'path', '') or ''\n"
    "    if p.lower().endswith('.log'):\n"
    "        sys.stdout.write(p + '\\n')\n"
)
# open_files() can also HANG on Windows; bound the isolated probe so a wedged
# child can never stall the supervision loop (the old in-process call had no such
# bound — a hang there froze the whole watchdog).
_HEARTBEAT_PROBE_TIMEOUT_S = 10


def _pick_heartbeat_log(paths: list[str]) -> str | None:
    """Choose the best heartbeat ``.log`` from candidate open-file paths (pure).

    Prefers a file under ``logs/`` (core.logging_setup convention) over a
    root-level ``*.log`` (a few bots use a plain FileHandler). Returns None when
    nothing usable is present — that process is then exempt from hang-detection,
    so a bot that only logs to stdout is never mistaken for wedged.
    """
    logs: list[str] = [p for p in paths if p.lower().endswith(".log")]
    if not logs:
        return None
    logs.sort(key=lambda p: (os.sep + "logs" + os.sep) not in p.lower())
    return logs[0]


def _probe_open_log_files(pid: int) -> list[str] | None:
    """Enumerate ``pid``'s open ``.log`` files in an ISOLATED child process.

    Returns the list of open ``.log`` paths, or None when the probe could not
    produce a trustworthy answer (child crashed/hung/failed to spawn). Isolation
    contains the psutil ``open_files()`` access-violation that used to kill the
    watchdog (T-2026-KYT-9050-025): a native crash surfaces here only as a
    non-zero return code, never as a fault in the supervisor process.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _HEARTBEAT_PROBE_SRC, str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_HEARTBEAT_PROBE_TIMEOUT_S,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        # SubprocessError covers TimeoutExpired (subprocess.run kills the child);
        # OSError/ValueError cover a failed spawn. All → exempt.
        return None
    if proc.returncode != 0:
        # Non-zero includes the native crash code (-1073741819 / 0xC0000005) and
        # the probe's own exit 4 on a psutil error.
        return None
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _resolve_heartbeat_log(pid: int) -> str | None:
    """Best-effort: the process's own open ``.log`` file, mapping-free.

    The open-file enumeration is isolated in a child process because the psutil
    call it relies on has native-crashed the watchdog (see _probe_open_log_files).
    """
    paths = _probe_open_log_files(pid)
    if not paths:
        return None
    return _pick_heartbeat_log(paths)


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
            f"🫀 {name} is alive, but {os.path.basename(log_path)} has been silent for {age / 60:.0f} min "
            f"(limit {HANG_LIMIT_S // 60} min) — process presumably wedged."
        )

    if not HANG_AUTORESTART:
        return  # WARNING-only default — operator decides on the money path.

    logger.warning(f"♻️ {name} — hang restart (auto, KYTHERA_WATCHDOG_HANG_AUTORESTART=1).")
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
    # Discard old entries (>1h)
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
                logger.info(f"⏸️  {name} is parked — stopping.")
                kill_process(name)
            else:
                del running_processes[name]
        return

    # One-shot restart requested from the dashboard. An explicit operator
    # action overrides the crash backoff (P1.37).
    if consume_restart(script):
        logger.info(f"♻️ {name} — restart requested via dashboard.")
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
        logger.info(f"⏱️  {name} — backoff expired, restarting.")

    if name not in running_processes:
        logger.error(f"🚨 Process {name} missing! Starting fresh...")
        start_process(p_info)
        return

    tracker = running_processes[name]
    p = tracker["process"]

    return_code = p.poll()
    if return_code is not None:
        logger.error(f"💥 CRASH: {name} (Code: {return_code}). Emergency restart!")
        del running_processes[name]
        # FIX: Backoff before restart, to limit crash loops.
        # P1.37: no time.sleep() — that froze the entire loop (up to 900s
        # no other restarts, no park markers, no dashboard, no
        # health checks). Set a deadline and keep running; the restart happens
        # in the cycle in which the deadline has expired — and only after
        # the park check above has run again.
        delay = _compute_restart_delay(name)
        if delay > 0:
            _restart_not_before[name] = current_time + delay
            logger.info(f"⏳ Waiting {delay}s before restarting {name} (crash protection)...")
            return
        start_process(p_info)
        return

    restart_interval = p_info.get("restart_interval")
    if restart_interval:
        uptime = current_time - tracker["start_time"]
        if uptime >= restart_interval:
            logger.info(f"♻️ Scheduled restart: {name} (uptime: {uptime / 3600:.1f}h)")
            kill_process(name)
            start_process(p_info)


def _request_graceful_stop(p: subprocess.Popen, name: str) -> bool:
    """Asks ``p`` — on Windows its whole process group including the
    ProcessPool workers — for an orderly shutdown (P2.48).

    POSIX: ``terminate()`` = SIGTERM is already the orderly stop signal.
    Windows: ``CTRL_BREAK_EVENT`` to the group. If that fails — no console
    attached (scheduled-task start), process not in its own group, or
    already terminated — we fall back to ``terminate()``; so it's never
    worse than the previous hard kill.

    Returns True if a graceful signal was delivered, otherwise False.
    """
    if not _IS_WINDOWS:
        p.terminate()  # SIGTERM — already graceful on POSIX
        return True
    ctrl_break = _CTRL_BREAK_EVENT
    if ctrl_break is None:
        # Cannot occur on real Windows (CTRL_BREAK_EVENT always exists there);
        # this just keeps the types honest and falls back safely.
        p.terminate()
        return False
    try:
        p.send_signal(ctrl_break)  # reaches the whole process group
        return True
    except (OSError, ValueError) as e:
        logger.warning(f"⚠️ {name}: could not deliver CTRL_BREAK ({e}) — hard terminate().")
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
        logger.error(f"⚠️ {name} is not responding {GRACEFUL_STOP_TIMEOUT_S}s after {sent} — hard kill!")
        p.kill()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error(f"⚠️ {name} survives even the hard kill — ProcessPool orphans possible.")
    del running_processes[name]
    logger.info(f"✅ {name} stopped successfully.")


# ── Graceful shutdown ────────────────────────────────────────────────────────


def shutdown_all() -> None:
    """Terminate every supervised bot and the dashboard. Idempotent.

    P0.2 hardening: encapsulated per child — if terminating one process fails,
    that must not abort terminating the rest of the children (otherwise
    orphaned processes would survive the watchdog's death).
    """
    for name in list(running_processes.keys()):
        try:
            kill_process(name)
        except Exception:  # noqa: BLE001 — teardown must reach all children.
            logger.exception(f"⚠️  Error terminating {name} — continuing with the rest.")
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
    logger.info(f"🛑 Signal {signum} received — shutting down all systems...")
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

    # P0.2: hard-prevent a second watchdog instance (mutex), then clean up orphaned
    # children of a crashed predecessor watchdog — both BEFORE spawning the fleet.
    _acquire_single_instance_lock()
    _terminate_orphan_fleet()

    _install_signal_handlers()
    # Last line of defence: on every exit path (even unexpected) terminate the
    # children before the watchdog dies (P0.2). shutdown_all is idempotent.
    atexit.register(shutdown_all)

    # Start the dashboard first — it's reachable immediately while the bots boot up.
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
            logger.info(f"⏸️  {p_info['name']} is parked — start skipped.")
            last_start = delay
            continue
        start_process(p_info)
        last_start = delay

    total_delay = sorted_procs[-1].get("start_delay", 0) if sorted_procs else 0
    logger.info(f"🟢 All systems started (staggered over {total_delay}s). Starting monitoring...")

    last_health_check = 0.0

    try:
        while True:
            current_time = time.time()

            # Health monitoring (1x/min): data staleness (P2.47, with auto-restart
            # of ingestion), sustained CPU load (WS disconnect cause), outbox failures
            # (P2.11). Alerts via TELEGRAM_ALERT_CHAT_ID + watchdog.log.
            if current_time - last_health_check >= 60:
                last_health_check = current_time
                run_health_checks()

            # Dashboard crash check
            check_dashboard()

            # Bot crash check (process existence) + hang check (P2.47:
            # alive-but-wedged via log heartbeat).
            for p_info in PROCESSES_TO_RUN:
                supervise_process(p_info, current_time)
                check_heartbeat(p_info, current_time)

            time.sleep(10)

    except KeyboardInterrupt:
        # Fallback if signal handlers could not be installed (e.g. non-main thread).
        logger.info("🛑 Watchdog stopped (Ctrl+C). Shutting down all systems...")
        shutdown_all()
        logger.info("🏁 System fully offline.")


if __name__ == "__main__":
    main()

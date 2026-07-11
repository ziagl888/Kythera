"""
dashboard.py — Bot Control Dashboard
Run alongside main_watchdog.py:  python dashboard.py
Opens on http://localhost:5000
"""

from __future__ import annotations

import json
import queue as queue_module
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import psutil
from flask import Flask, Response, jsonify, request, stream_with_context

from core.fleet import FLEET
from core.process_control import is_parked, park, request_restart, unpark

# ── Config ─────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
PORT = 5000

# Prozessliste — zentral in core/fleet.py definiert (Single Source, geteilt mit
# main_watchdog.py; T-2026-CU-9050-091, R2(a)). Das Dashboard nutzt name/script/
# group/restart_interval; das zusätzliche start_delay-Feld (nur für den Watchdog-
# Start) ist hier ein No-op. Seit der Zentralisierung zeigt das Dashboard die
# volle Fleet inkl. der zuvor fehlenden Bots 26–34.
PROCESSES: list[dict[str, Any]] = FLEET

# script → process info lookup
SCRIPT_MAP = {p["script"]: p for p in PROCESSES}

# SSE event queue for live push to browser
_sse_queue: deque[str] = deque(maxlen=200)
_sse_listeners: list[queue_module.Queue] = []
_sse_lock = threading.Lock()


def _push_event(event_type: str, data: dict) -> None:
    """Push a server-sent event to all connected browsers."""
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        _sse_queue.append(payload)
        for q in _sse_listeners:
            try:
                q.put_nowait(payload)
            except queue_module.Full:
                pass


# ── Process Discovery ───────────────────────────────────────────────────────


def _script_to_log(script: str) -> Path:
    """Derive log file path from script name, using core/logging_setup convention."""
    name_map = {p["script"]: p["name"] for p in PROCESSES}
    name = name_map.get(script, script.replace(".py", ""))
    # logging_setup writes to logs/<name>.log
    candidates = [
        LOG_DIR / f"{name}.log",
        BASE_DIR / f"{script.replace('.py', '')}.log",
        BASE_DIR / "watchdog.log",
    ]
    for c in candidates:
        if c.exists():
            return c
    return LOG_DIR / f"{name}.log"  # expected path even if not yet created


def _find_pid_for_script(script: str) -> int | None:
    """
    Find the PID of a running Python process executing the given script.
    Works on Windows (full paths in cmdline) and Linux alike.
    """
    # Match just the filename part so both "1_data_ingestion.py" and
    # "C:\...\1_data_ingestion.py" are found.
    script_name = script.replace("\\", "/").split("/")[-1].lower()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
            if not cmd:
                continue
            # First arg must be a python interpreter
            exe = (cmd[0] or "").lower()
            if "python" not in exe and not exe.endswith(("py.exe", "python3")):
                continue
            # Any remaining arg must end with our script filename
            for arg in cmd[1:]:
                arg_name = arg.replace("\\", "/").split("/")[-1].lower()
                if arg_name == script_name:
                    return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def _get_process_status(p_def: dict) -> dict:
    """Return live status dict for one process definition."""
    script = p_def["script"]
    pid = _find_pid_for_script(script)
    status = {
        "name": p_def["name"],
        "script": script,
        "group": p_def.get("group", "other"),
        "running": False,
        "parked": is_parked(script),
        "pid": None,
        "cpu": 0.0,
        "mem_mb": 0.0,
        "uptime_s": 0,
        "restart_interval": p_def.get("restart_interval"),
        "log_file": str(_script_to_log(script)),
    }
    if pid:
        try:
            proc = psutil.Process(pid)
            with proc.oneshot():
                status["running"] = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
                status["pid"] = pid
                status["cpu"] = round(proc.cpu_percent(interval=0.1), 1)
                status["mem_mb"] = round(proc.memory_info().rss / 1024 / 1024, 1)
                status["uptime_s"] = int(time.time() - proc.create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return status


def get_all_statuses() -> list[dict]:
    return [_get_process_status(p) for p in PROCESSES]


def get_system_stats() -> dict:
    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_pct": round(cpu, 1),
        "mem_used_gb": round(mem.used / 1e9, 2),
        "mem_total_gb": round(mem.total / 1e9, 2),
        "mem_pct": round(mem.percent, 1),
        "disk_used_gb": round(disk.used / 1e9, 1),
        "disk_total_gb": round(disk.total / 1e9, 1),
        "disk_pct": round(disk.percent, 1),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


# ── Process Control ─────────────────────────────────────────────────────────


# Process control is INTENT-only. main_watchdog.py is the single actuator of
# process lifecycle; the dashboard records intent (park/unpark/restart) here and
# the watchdog acts on it within its next monitor cycle (<=10s). This is what
# fixes the old bug where a dashboard "stop" was revived by the watchdog in 10s.


def start_process(script: str) -> dict:
    if not (BASE_DIR / script).exists():
        return {"ok": False, "msg": f"Script not found: {script}"}
    unpark(script)
    _push_event("process_change", {"script": script, "action": "unparked"})
    return {"ok": True, "msg": "Start angefordert — Watchdog startet den Bot (<=10s)."}


def stop_process(script: str) -> dict:
    park(script)
    _push_event("process_change", {"script": script, "action": "parked"})
    return {"ok": True, "msg": "Stop angefordert — Watchdog parkt den Bot (<=10s)."}


def restart_process(script: str) -> dict:
    unpark(script)
    request_restart(script)
    _push_event("process_change", {"script": script, "action": "restart_requested"})
    return {"ok": True, "msg": "Restart angefordert — Watchdog recycelt den Bot (<=10s)."}


def restart_all() -> dict:
    results = {p["script"]: restart_process(p["script"]) for p in PROCESSES}
    _push_event("system", {"action": "restart_all"})
    return results


def stop_all() -> dict:
    results = {p["script"]: stop_process(p["script"]) for p in PROCESSES}
    _push_event("system", {"action": "stop_all"})
    return results


def start_all() -> dict:
    results = {p["script"]: start_process(p["script"]) for p in PROCESSES}
    _push_event("system", {"action": "start_all"})
    return results


# ── Log Streaming ───────────────────────────────────────────────────────────


def tail_log(log_path: Path, lines: int = 200) -> list[str]:
    """Return the last N lines from a log file."""
    if not log_path.exists():
        return [f"[Log file not found: {log_path}]"]
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-lines:]
    except Exception as e:
        return [f"[Error reading log: {e}]"]


def stream_log(log_path: Path) -> Iterator[str]:
    """Tail -f style SSE stream for a log file."""
    if not log_path.exists():
        yield f"data: [Waiting for {log_path.name}...]\n\n"

    with open(log_path, encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)  # seek to end
        while True:
            line = f.readline()
            if line:
                yield f"data: {json.dumps(line.rstrip())}\n\n"
            else:
                time.sleep(0.3)


# ── Background stats poller ─────────────────────────────────────────────────


def _stats_poller() -> None:
    """Push system stats every 5s to all SSE clients."""
    while True:
        time.sleep(5)
        try:
            _push_event("stats", get_system_stats())
        except Exception:
            pass


threading.Thread(target=_stats_poller, daemon=True).start()


# ── Flask App ───────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    return HTML_PAGE


@app.route("/api/status")
def api_status():
    return jsonify({"processes": get_all_statuses(), "system": get_system_stats()})


@app.route("/api/process/<script>/start", methods=["POST"])
def api_start(script: str):
    return jsonify(start_process(script))


@app.route("/api/process/<script>/stop", methods=["POST"])
def api_stop(script: str):
    return jsonify(stop_process(script))


@app.route("/api/process/<script>/restart", methods=["POST"])
def api_restart(script: str):
    return jsonify(restart_process(script))


@app.route("/api/system/restart_all", methods=["POST"])
def api_restart_all():
    return jsonify(restart_all())


@app.route("/api/system/stop_all", methods=["POST"])
def api_stop_all():
    return jsonify(stop_all())


@app.route("/api/system/start_all", methods=["POST"])
def api_start_all():
    return jsonify(start_all())


@app.route("/api/logs/<script>")
def api_logs(script: str):
    p = SCRIPT_MAP.get(script)
    if not p:
        return jsonify({"error": "Unknown script"}), 404
    log_path = _script_to_log(script)
    n = int(request.args.get("n", 300))
    lines = tail_log(log_path, n)
    return jsonify({"lines": lines, "path": str(log_path)})


@app.route("/api/logs/<script>/stream")
def api_log_stream(script: str):
    p = SCRIPT_MAP.get(script)
    if not p:
        return Response("data: Unknown script\n\n", mimetype="text/event-stream")
    log_path = _script_to_log(script)

    def generate():
        # Send last 50 lines first
        for line in tail_log(log_path, 50):
            yield f"data: {json.dumps(line.rstrip())}\n\n"
        yield from stream_log(log_path)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/events")
def api_events():
    """SSE endpoint for process/system events."""
    q: queue_module.Queue = queue_module.Queue(maxsize=50)
    with _sse_lock:
        _sse_listeners.append(q)
        # Replay recent events
        recent = list(_sse_queue)[-10:]

    def generate():
        yield from recent
        try:
            while True:
                try:
                    yield q.get(timeout=30)
                except queue_module.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with _sse_lock:
                try:
                    _sse_listeners.remove(q)
                except ValueError:
                    pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── HTML Page ───────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Control</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Space+Grotesk:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:        #0a0c10;
    --surface:   #111318;
    --surface2:  #181c24;
    --border:    #232838;
    --border2:   #2d3448;
    --text:      #d4daf0;
    --muted:     #5a6280;
    --green:     #00e57a;
    --green-dim: #00e57a22;
    --red:       #ff4560;
    --red-dim:   #ff456022;
    --yellow:    #ffc947;
    --yellow-dim:#ffc94722;
    --blue:      #4da6ff;
    --blue-dim:  #4da6ff18;
    --mono:      'JetBrains Mono', monospace;
    --sans:      'Space Grotesk', sans-serif;
    --radius:    8px;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    min-height: 100vh;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 24px;
    height: 56px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    position: sticky; top: 0; z-index: 100;
  }
  .logo {
    font-family: var(--mono);
    font-weight: 700;
    font-size: 15px;
    letter-spacing: -0.5px;
    color: #fff;
    display: flex; align-items: center; gap: 10px;
  }
  .logo-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 8px var(--green);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%,100% { opacity: 1; }
    50%      { opacity: 0.4; }
  }
  .header-stats {
    display: flex; gap: 24px; align-items: center;
  }
  .hstat {
    display: flex; flex-direction: column; align-items: flex-end;
  }
  .hstat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }
  .hstat-value { font-family: var(--mono); font-size: 15px; font-weight: 600; color: #fff; }
  .header-actions { display: flex; gap: 8px; }

  /* ── Buttons ── */
  .btn {
    font-family: var(--sans);
    font-size: 12px;
    font-weight: 600;
    padding: 6px 14px;
    border-radius: var(--radius);
    border: 1px solid var(--border2);
    cursor: pointer;
    transition: all 0.15s;
    background: var(--surface2);
    color: var(--text);
    letter-spacing: 0.3px;
  }
  .btn:hover { border-color: var(--blue); color: var(--blue); }
  .btn:active { transform: scale(0.97); }
  .btn-danger  { border-color: var(--red-dim); color: var(--red); background: var(--red-dim); }
  .btn-danger:hover  { background: #ff456033; }
  .btn-success { border-color: var(--green-dim); color: var(--green); background: var(--green-dim); }
  .btn-success:hover { background: #00e57a33; }
  .btn-small { padding: 3px 9px; font-size: 11px; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }

  /* ── Layout ── */
  .main { display: flex; height: calc(100vh - 56px); overflow: hidden; }

  /* ── Sidebar ── */
  .sidebar {
    width: 260px;
    flex-shrink: 0;
    border-right: 1px solid var(--border);
    background: var(--surface);
    overflow-y: auto;
    display: flex;
    flex-direction: column;
  }
  .sidebar-section { padding: 12px 0; border-bottom: 1px solid var(--border); }
  .sidebar-label {
    padding: 0 16px 8px;
    font-size: 10px;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 1.5px;
  }

  /* ── Process Row in Sidebar ── */
  .proc-row {
    display: flex;
    align-items: center;
    padding: 6px 16px;
    gap: 10px;
    cursor: pointer;
    border-left: 2px solid transparent;
    transition: all 0.1s;
  }
  .proc-row:hover  { background: var(--surface2); }
  .proc-row.active { background: var(--blue-dim); border-left-color: var(--blue); }
  .proc-dot {
    width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
    background: var(--muted);
  }
  .proc-dot.up   { background: var(--green); box-shadow: 0 0 5px var(--green); }
  .proc-dot.down { background: var(--red); }
  .proc-dot.parked { background: var(--yellow); }
  .proc-name { flex: 1; font-size: 13px; line-height: 1.3; }
  .proc-pid  { font-family: var(--mono); font-size: 10px; color: var(--muted); }

  /* ── Content ── */
  .content { flex: 1; overflow-y: auto; display: flex; flex-direction: column; }

  /* ── Tab bar ── */
  .tabs {
    display: flex;
    gap: 0;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    padding: 0 24px;
    flex-shrink: 0;
  }
  .tab {
    padding: 14px 18px;
    font-size: 13px;
    font-weight: 500;
    color: var(--muted);
    cursor: pointer;
    border-bottom: 2px solid transparent;
    transition: all 0.15s;
    user-select: none;
  }
  .tab:hover  { color: var(--text); }
  .tab.active { color: #fff; border-bottom-color: var(--blue); }

  .tab-content { display: none; flex: 1; padding: 24px; }
  .tab-content.active { display: block; }

  /* ── Module Cards Grid ── */
  .cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 12px;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 16px;
    transition: border-color 0.15s;
  }
  .card:hover { border-color: var(--border2); }
  .card-head {
    display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
  }
  .card-status {
    width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  }
  .card-status.up   { background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 2s infinite; }
  .card-status.down { background: var(--red); }
  .card-status.parked { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }
  .card-name { font-size: 13px; font-weight: 600; flex: 1; }
  .card-script { font-family: var(--mono); font-size: 10px; color: var(--muted); margin-top: 1px; }
  .card-metrics {
    display: flex; gap: 12px; margin-bottom: 12px;
  }
  .metric {
    display: flex; flex-direction: column;
  }
  .metric-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; }
  .metric-value { font-family: var(--mono); font-size: 14px; font-weight: 600; }
  .metric-value.up   { color: var(--green); }
  .metric-value.down { color: var(--red); }
  .metric-value.parked { color: var(--yellow); }
  .card-actions { display: flex; gap: 6px; }
  .group-badge {
    font-size: 10px;
    padding: 2px 7px;
    border-radius: 4px;
    border: 1px solid var(--border2);
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-left: auto;
  }
  .group-core     { border-color: #4da6ff44; color: var(--blue); }
  .group-ai       { border-color: #c77dff44; color: #c77dff; }
  .group-strategy { border-color: #ffc94744; color: var(--yellow); }
  .group-logger   { border-color: #00e57a44; color: var(--green); }

  /* ── Log Panel ── */
  .log-header {
    display: flex; align-items: center; gap: 12px;
    margin-bottom: 12px;
  }
  .log-title { font-size: 15px; font-weight: 600; }
  .log-path  { font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .log-box {
    background: #07090e;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px;
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.7;
    height: calc(100vh - 250px);
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .log-line.error   { color: var(--red); }
  .log-line.warning { color: var(--yellow); }
  .log-line.info    { color: var(--text); }
  .log-line.success { color: var(--green); }

  /* ── System Panel ── */
  .sys-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin-bottom: 24px;
  }
  .sys-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
  }
  .sys-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  .sys-value { font-family: var(--mono); font-size: 28px; font-weight: 700; }
  .sys-bar {
    height: 4px;
    background: var(--border);
    border-radius: 2px;
    margin-top: 10px;
    overflow: hidden;
  }
  .sys-bar-fill {
    height: 100%;
    border-radius: 2px;
    background: var(--blue);
    transition: width 0.5s;
  }
  .sys-bar-fill.warn  { background: var(--yellow); }
  .sys-bar-fill.crit  { background: var(--red); }

  .summary-row {
    display: flex; gap: 10px; align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
  }
  .summary-row:last-child { border-bottom: none; }

  /* ── Uptime bar ── */
  .uptime { font-family: var(--mono); font-size: 11px; color: var(--muted); }

  /* ── Toast ── */
  #toast {
    position: fixed; bottom: 24px; right: 24px;
    background: var(--surface2);
    border: 1px solid var(--border2);
    border-radius: var(--radius);
    padding: 12px 18px;
    font-size: 13px;
    z-index: 9999;
    opacity: 0;
    transform: translateY(8px);
    transition: all 0.2s;
    pointer-events: none;
    max-width: 320px;
  }
  #toast.show { opacity: 1; transform: translateY(0); }
  #toast.ok  { border-color: var(--green); color: var(--green); }
  #toast.err { border-color: var(--red);   color: var(--red); }

  /* ── Group filter ── */
  .filter-bar { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
  .filter-btn {
    padding: 4px 12px; border-radius: 20px;
    border: 1px solid var(--border2);
    background: transparent;
    color: var(--muted);
    font-size: 12px; font-family: var(--sans);
    cursor: pointer; transition: all 0.1s;
  }
  .filter-btn.active, .filter-btn:hover { border-color: var(--blue); color: var(--blue); background: var(--blue-dim); }

  /* ── Confirm overlay ── */
  .overlay {
    display: none; position: fixed; inset: 0;
    background: #000a; z-index: 500;
    align-items: center; justify-content: center;
  }
  .overlay.show { display: flex; }
  .dialog {
    background: var(--surface2);
    border: 1px solid var(--border2);
    border-radius: 12px;
    padding: 28px 32px;
    width: 360px;
    text-align: center;
  }
  .dialog h3 { font-size: 17px; margin-bottom: 10px; }
  .dialog p  { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
  .dialog-btns { display: flex; gap: 10px; justify-content: center; }

  ::-webkit-scrollbar { width: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-dot" id="systemDot"></div>
    BOT CONTROL
  </div>
  <div class="header-stats">
    <div class="hstat">
      <span class="hstat-label">CPU</span>
      <span class="hstat-value" id="h-cpu">—</span>
    </div>
    <div class="hstat">
      <span class="hstat-label">RAM</span>
      <span class="hstat-value" id="h-mem">—</span>
    </div>
    <div class="hstat">
      <span class="hstat-label">Running</span>
      <span class="hstat-value" id="h-running">—</span>
    </div>
  </div>
  <div class="header-actions">
    <button class="btn btn-success" onclick="confirmAction('start_all','Start All','Alle gestoppten Module starten?')">▶ Start All</button>
    <button class="btn" onclick="confirmAction('restart_all','Restart All','Alle Module neu starten? (gestaffelt)')">↺ Restart All</button>
    <button class="btn btn-danger" onclick="confirmAction('stop_all','Stop All','Alle laufenden Module stoppen?')">■ Stop All</button>
  </div>
</header>

<div class="main">

  <!-- Sidebar -->
  <div class="sidebar">
    <div class="sidebar-section">
      <div class="sidebar-label">Module</div>
      <div id="sidebarList"></div>
    </div>
  </div>

  <!-- Content -->
  <div class="content">
    <div class="tabs">
      <div class="tab active" onclick="switchTab('overview')">Overview</div>
      <div class="tab" onclick="switchTab('logs')">Logs</div>
      <div class="tab" onclick="switchTab('system')">System</div>
    </div>

    <!-- OVERVIEW TAB -->
    <div class="tab-content active" id="tab-overview">
      <div class="filter-bar">
        <button class="filter-btn active" onclick="setFilter('all',this)">All</button>
        <button class="filter-btn" onclick="setFilter('core',this)">Core</button>
        <button class="filter-btn" onclick="setFilter('ai',this)">AI</button>
        <button class="filter-btn" onclick="setFilter('strategy',this)">Strategy</button>
        <button class="filter-btn" onclick="setFilter('logger',this)">Logger</button>
        <button class="filter-btn" onclick="setFilter('down',this)">⚠ Stopped</button>
      </div>
      <div class="cards-grid" id="cardsGrid"></div>
    </div>

    <!-- LOGS TAB -->
    <div class="tab-content" id="tab-logs">
      <div class="log-header">
        <div>
          <div class="log-title" id="logTitle">Wähle ein Modul aus der Sidebar</div>
          <div class="log-path" id="logPath"></div>
        </div>
        <div style="margin-left:auto;display:flex;gap:8px">
          <button class="btn btn-small" onclick="toggleAutoScroll()">Auto-Scroll: <span id="autoScrollLabel">ON</span></button>
          <button class="btn btn-small" onclick="clearLog()">Clear</button>
          <button class="btn btn-small" onclick="loadLogs()">↺ Reload</button>
        </div>
      </div>
      <div class="log-box" id="logBox"></div>
    </div>

    <!-- SYSTEM TAB -->
    <div class="tab-content" id="tab-system">
      <div class="sys-grid">
        <div class="sys-card">
          <div class="sys-label">CPU</div>
          <div class="sys-value" id="s-cpu">—</div>
          <div class="sys-bar"><div class="sys-bar-fill" id="s-cpu-bar" style="width:0%"></div></div>
        </div>
        <div class="sys-card">
          <div class="sys-label">Memory</div>
          <div class="sys-value" id="s-mem">—</div>
          <div class="sys-bar"><div class="sys-bar-fill" id="s-mem-bar" style="width:0%"></div></div>
        </div>
        <div class="sys-card">
          <div class="sys-label">Disk</div>
          <div class="sys-value" id="s-disk">—</div>
          <div class="sys-bar"><div class="sys-bar-fill" id="s-disk-bar" style="width:0%"></div></div>
        </div>
      </div>
      <div id="processSummary"></div>
    </div>
  </div>
</div>

<!-- Confirm Overlay -->
<div class="overlay" id="overlay">
  <div class="dialog">
    <h3 id="dlgTitle"></h3>
    <p id="dlgBody"></p>
    <div class="dialog-btns">
      <button class="btn" onclick="closeOverlay()">Abbrechen</button>
      <button class="btn btn-danger" id="dlgConfirm">Bestätigen</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div id="toast"></div>

<script>
// ── State ──────────────────────────────────────────────────────────────
let allStatuses = [];
let selectedScript = null;
let activeFilter = 'all';
let logStream = null;
let autoScroll = true;

// ── Init ───────────────────────────────────────────────────────────────
async function init() {
  await refresh();
  setInterval(refresh, 6000);
  subscribeEvents();
}

// ── Data ───────────────────────────────────────────────────────────────
async function refresh() {
  const data = await fetch('/api/status').then(r => r.json());
  allStatuses = data.processes;
  updateHeader(data.system);
  renderSidebar();
  renderCards();
  renderSystem(data.system, data.processes);
}

function updateHeader(sys) {
  document.getElementById('h-cpu').textContent = sys.cpu_pct + '%';
  document.getElementById('h-mem').textContent = sys.mem_pct + '%';
  const running = allStatuses.filter(p => p.running).length;
  const total   = allStatuses.length;
  document.getElementById('h-running').textContent = running + '/' + total;
  document.getElementById('systemDot').style.background = running > 0 ? 'var(--green)' : 'var(--red)';
}

// ── Sidebar ────────────────────────────────────────────────────────────
function renderSidebar() {
  const el = document.getElementById('sidebarList');
  el.innerHTML = allStatuses.map(p => `
    <div class="proc-row ${selectedScript === p.script ? 'active' : ''}"
         onclick="selectProcess('${p.script}')">
      <div class="proc-dot ${p.running ? 'up' : (p.parked ? 'parked' : 'down')}"></div>
      <div>
        <div class="proc-name">${p.name}</div>
      </div>
      ${p.pid ? `<div class="proc-pid">${p.pid}</div>` : ''}
    </div>
  `).join('');
}

// ── Cards ──────────────────────────────────────────────────────────────
function setFilter(f, btn) {
  activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderCards();
}

function renderCards() {
  let procs = allStatuses;
  if (activeFilter === 'down') procs = procs.filter(p => !p.running);
  else if (activeFilter !== 'all') procs = procs.filter(p => p.group === activeFilter);

  document.getElementById('cardsGrid').innerHTML = procs.map(p => {
    const up = p.running;
    const state = up ? 'up' : (p.parked ? 'parked' : 'down');
    const stateLabel = up ? 'Running' : (p.parked ? 'Parked' : 'Stopped');
    const uptime = up ? fmtUptime(p.uptime_s) : '—';
    return `
    <div class="card ${selectedScript === p.script ? 'selected' : ''}"
         style="${selectedScript === p.script ? 'border-color:var(--blue)' : ''}">
      <div class="card-head">
        <div class="card-status ${state}"></div>
        <div>
          <div class="card-name">${p.name}</div>
          <div class="card-script">${p.script}</div>
        </div>
        <div class="group-badge group-${p.group}">${p.group}</div>
      </div>
      <div class="card-metrics">
        <div class="metric">
          <span class="metric-label">Status</span>
          <span class="metric-value ${state}">${stateLabel}</span>
        </div>
        ${up ? `
        <div class="metric">
          <span class="metric-label">CPU</span>
          <span class="metric-value">${p.cpu}%</span>
        </div>
        <div class="metric">
          <span class="metric-label">RAM</span>
          <span class="metric-value">${p.mem_mb}MB</span>
        </div>
        <div class="metric">
          <span class="metric-label">Uptime</span>
          <span class="metric-value uptime">${uptime}</span>
        </div>
        ` : ''}
        ${p.pid ? `<div class="metric"><span class="metric-label">PID</span><span class="metric-value" style="color:var(--muted)">${p.pid}</span></div>` : ''}
      </div>
      <div class="card-actions">
        ${up
          ? `<button class="btn btn-small" onclick="doAction('restart','${p.script}','${p.name}')">↺ Restart</button>
             <button class="btn btn-small btn-danger" onclick="doAction('stop','${p.script}','${p.name}')">■ Stop</button>`
          : `<button class="btn btn-small btn-success" onclick="doAction('start','${p.script}','${p.name}')">▶ Start</button>`
        }
        <button class="btn btn-small" onclick="openLogs('${p.script}','${p.name}')">📋 Logs</button>
      </div>
    </div>`;
  }).join('');
}

// ── Process actions ────────────────────────────────────────────────────
async function doAction(action, script, name) {
  const res = await fetch(`/api/process/${encodeURIComponent(script)}/${action}`, {method:'POST'});
  const data = await res.json();
  toast(data.ok ? data.msg : '❌ ' + data.msg, data.ok ? 'ok' : 'err');
  setTimeout(refresh, 800);
}

// ── Logs ───────────────────────────────────────────────────────────────
function selectProcess(script) {
  selectedScript = script;
  const p = allStatuses.find(x => x.script === script);
  if (p) openLogs(script, p.name);
  renderSidebar();
  renderCards();
}

function openLogs(script, name) {
  selectedScript = script;
  switchTab('logs');
  document.getElementById('logTitle').textContent = name + ' — Logs';
  const p = allStatuses.find(x => x.script === script);
  if (p) document.getElementById('logPath').textContent = p.log_file || '';

  // Close existing stream
  if (logStream) { logStream.close(); logStream = null; }

  const box = document.getElementById('logBox');
  box.innerHTML = '<span style="color:var(--muted)">Loading…</span>';

  logStream = new EventSource(`/api/logs/${encodeURIComponent(script)}/stream`);
  box.innerHTML = '';
  logStream.onmessage = e => {
    const line = JSON.parse(e.data);
    appendLog(line);
  };
  logStream.onerror = () => {
    appendLog('[Stream disconnected — click Reload to reconnect]');
  };
}

function appendLog(line) {
  const box = document.getElementById('logBox');
  const div = document.createElement('div');
  div.className = 'log-line ' + classifyLine(line);
  div.textContent = line;
  box.appendChild(div);
  if (autoScroll) box.scrollTop = box.scrollHeight;
}

function classifyLine(line) {
  const l = line.toLowerCase();
  if (l.includes('error') || l.includes('❌') || l.includes('crash') || l.includes('kritisch')) return 'error';
  if (l.includes('warning') || l.includes('warn') || l.includes('⚠')) return 'warning';
  if (l.includes('✅') || l.includes('success') || l.includes('started')) return 'success';
  return 'info';
}

function clearLog() { document.getElementById('logBox').innerHTML = ''; }

function loadLogs() {
  if (selectedScript) {
    const p = allStatuses.find(x => x.script === selectedScript);
    if (p) openLogs(selectedScript, p.name);
  }
}

function toggleAutoScroll() {
  autoScroll = !autoScroll;
  document.getElementById('autoScrollLabel').textContent = autoScroll ? 'ON' : 'OFF';
}

// ── System tab ─────────────────────────────────────────────────────────
function renderSystem(sys, procs) {
  setBar('s-cpu', sys.cpu_pct, sys.cpu_pct + '%');
  setBar('s-mem', sys.mem_pct, sys.mem_used_gb + ' / ' + sys.mem_total_gb + ' GB');
  setBar('s-disk', sys.disk_pct, sys.disk_used_gb + ' / ' + sys.disk_total_gb + ' GB');

  const summary = document.getElementById('processSummary');
  summary.innerHTML = procs.map(p => `
    <div class="summary-row">
      <div class="proc-dot ${p.running ? 'up' : (p.parked ? 'parked' : 'down')}" style="flex-shrink:0"></div>
      <div style="flex:1">${p.name}</div>
      <div style="font-family:var(--mono);color:var(--muted);font-size:11px;width:60px">${p.pid ? '#' + p.pid : '—'}</div>
      <div style="font-family:var(--mono);font-size:11px;width:60px">${p.running ? p.cpu + '%' : ''}</div>
      <div style="font-family:var(--mono);font-size:11px;width:70px">${p.running ? p.mem_mb + 'MB' : ''}</div>
      <div style="font-family:var(--mono);color:var(--muted);font-size:11px;width:80px">${p.running ? fmtUptime(p.uptime_s) : (p.parked ? 'parked' : 'stopped')}</div>
    </div>
  `).join('');
}

function setBar(id, pct, label) {
  document.getElementById(id).textContent = label;
  const bar = document.getElementById(id + '-bar');
  bar.style.width = pct + '%';
  bar.className = 'sys-bar-fill' + (pct > 90 ? ' crit' : pct > 70 ? ' warn' : '');
}

// ── SSE event subscription ─────────────────────────────────────────────
function subscribeEvents() {
  const es = new EventSource('/api/events');
  es.addEventListener('process_change', e => {
    const d = JSON.parse(e.data);
    toast(`${d.action}: ${d.script}`, 'ok');
    setTimeout(refresh, 500);
  });
  es.addEventListener('stats', e => {
    const s = JSON.parse(e.data);
    document.getElementById('h-cpu').textContent = s.cpu_pct + '%';
    document.getElementById('h-mem').textContent = s.mem_pct + '%';
  });
  es.onerror = () => setTimeout(subscribeEvents, 3000);
}

// ── Confirm dialog ─────────────────────────────────────────────────────
let _pendingAction = null;
function confirmAction(action, title, body) {
  _pendingAction = action;
  document.getElementById('dlgTitle').textContent = title;
  document.getElementById('dlgBody').textContent  = body;
  document.getElementById('overlay').classList.add('show');
  document.getElementById('dlgConfirm').onclick = executeConfirmed;
}
function closeOverlay() {
  document.getElementById('overlay').classList.remove('show');
  _pendingAction = null;
}
async function executeConfirmed() {
  closeOverlay();
  if (!_pendingAction) return;
  const res = await fetch(`/api/system/${_pendingAction}`, {method:'POST'});
  toast('Befehl ausgeführt', 'ok');
  setTimeout(refresh, 1500);
}

// ── Tab switching ──────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t, i) => {
    const names = ['overview','logs','system'];
    t.classList.toggle('active', names[i] === name);
  });
  document.querySelectorAll('.tab-content').forEach(c => {
    c.classList.toggle('active', c.id === 'tab-' + name);
  });
}

// ── Toast ──────────────────────────────────────────────────────────────
let _toastTimer;
function toast(msg, type = 'ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + type;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.className = '', 3000);
}

// ── Helpers ────────────────────────────────────────────────────────────
function fmtUptime(s) {
  if (s < 60)   return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm';
  if (s < 86400) return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
  return Math.floor(s/86400) + 'd ' + Math.floor((s%86400)/3600) + 'h';
}

// ── Start ──────────────────────────────────────────────────────────────
init();
</script>
</body>
</html>"""


# ── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # FIX: Windows-Consoles nutzen per Default cp1252 als stdout-Encoding.
    # Unicode characters like 🟢 or 📁 crash there with UnicodeEncodeError.
    # sys.stdout/stderr auf UTF-8 umstellen bevor wir irgendwas printen.
    try:
        sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]  # guarded by except below
        sys.stderr.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
    except (AttributeError, Exception):
        # Python <3.7 or non-standard stdout — fallback: replace emojis
        pass

    LOG_DIR.mkdir(exist_ok=True)
    try:
        print(f"🟢  Bot Dashboard läuft auf  http://localhost:{PORT}")
        print(f"📁  Basis-Verzeichnis: {BASE_DIR}")
        print(f"📋  Log-Verzeichnis:   {LOG_DIR}")
    except UnicodeEncodeError:
        # Absolute fallback if reconfigure() did not work
        print(f"[OK] Bot Dashboard laeuft auf  http://localhost:{PORT}")
        print(f"[DIR] Basis-Verzeichnis: {BASE_DIR}")
        print(f"[LOG] Log-Verzeichnis:   {LOG_DIR}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

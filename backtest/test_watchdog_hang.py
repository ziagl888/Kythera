# backtest/test_watchdog_hang.py
"""
Unit tests for the watchdog's hang/heartbeat detection (P2.47).

The watchdog only ever checked process EXISTENCE, so a bot that hung — alive but
wedged, producing nothing — stayed "green" while the fleet traded stale data.
check_heartbeat flags a supervised process whose own log file has not advanced
for HANG_LIMIT_S. It is safe by construction:

  - a process with no observable open log file is EXEMPT (never false-restarted),
  - a freshly (re)started bot gets a full grace window,
  - auto-restart is DEFAULT-OFF (warning-only) and, when enabled, rides the
    existing crash backoff — it must never block.

Run with: pytest backtest/test_watchdog_hang.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pytest


def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "main_watchdog",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main_watchdog.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "core.health_monitor": mock.MagicMock(),
            "core.process_control": mock.MagicMock(),
            "psutil": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


wd = _load_watchdog()

P_INFO = {"name": "bot_a", "script": "1_data_ingestion.py"}
NOW = 1_000_000.0
LIMIT = 1200  # 20 min


class _Proc:
    def __init__(self, rc=None, pid=4242):
        self._rc = rc
        self.pid = pid

    def poll(self):
        return self._rc


@pytest.fixture
def env(monkeypatch):
    started: list[str] = []
    killed: list[str] = []
    monkeypatch.setattr(wd, "running_processes", {})
    monkeypatch.setattr(wd, "_restart_not_before", {})
    monkeypatch.setattr(wd, "_hang_alerted", {})
    monkeypatch.setattr(wd, "HANG_LIMIT_S", LIMIT)
    monkeypatch.setattr(wd, "HANG_AUTORESTART", False)
    monkeypatch.setattr(wd, "start_process", lambda p: started.append(p["name"]))
    monkeypatch.setattr(wd, "kill_process", lambda n: (killed.append(n), wd.running_processes.pop(n, None)))
    monkeypatch.setattr(wd.time, "sleep", mock.Mock(side_effect=AssertionError("check_heartbeat must never block")))
    return {"started": started, "killed": killed}


def _register(name: str, *, rc=None, start_age: float = 5000.0, heartbeat_log="logs/DATA_INGESTION.log"):
    tracker = {"process": _Proc(rc=rc), "info": {}, "start_time": NOW - start_age}
    if heartbeat_log is not None:
        tracker["heartbeat_log"] = heartbeat_log
    wd.running_processes[name] = tracker
    return tracker


def _mtime(monkeypatch, age: float) -> None:
    """Pin os.path.getmtime so the log looks `age` seconds stale at NOW."""
    monkeypatch.setattr(wd.os.path, "getmtime", lambda _p: NOW - age)


# ── Fresh log → healthy ──────────────────────────────────────────────────────


def test_recent_log_is_not_flagged(env, monkeypatch):
    _register("bot_a")
    _mtime(monkeypatch, age=10)  # 10s ago
    wd.check_heartbeat(P_INFO, NOW)
    assert env["started"] == []
    assert "bot_a" not in wd._hang_alerted


# ── Stale log, default (warning-only) → warn, never restart ──────────────────


def test_stale_log_warns_but_does_not_restart_by_default(env, monkeypatch):
    _register("bot_a")
    _mtime(monkeypatch, age=LIMIT + 60)
    wd.check_heartbeat(P_INFO, NOW)
    assert env["started"] == [], "wedged bot restarted despite default warning-only mode"
    assert env["killed"] == []
    assert "bot_a" in wd._hang_alerted, "hang went unreported"


# ── Grace window: a freshly started bot is never flagged ─────────────────────


def test_freshly_started_bot_is_within_grace(env, monkeypatch):
    _register("bot_a", start_age=100)  # started 100s ago, well under LIMIT
    _mtime(monkeypatch, age=LIMIT + 999)
    wd.check_heartbeat(P_INFO, NOW)
    assert "bot_a" not in wd._hang_alerted


# ── No observable log → exempt ───────────────────────────────────────────────


def test_process_without_a_log_is_exempt(env, monkeypatch):
    _register("bot_a", heartbeat_log=None)
    # Resolution yields nothing (stdout-only bot) → must never be flagged.
    monkeypatch.setattr(wd, "_resolve_heartbeat_log", lambda pid: None)
    monkeypatch.setattr(wd.os.path, "getmtime", mock.Mock(side_effect=AssertionError("must not stat")))
    wd.check_heartbeat(P_INFO, NOW)
    assert env["started"] == []
    assert "bot_a" not in wd._hang_alerted


def test_heartbeat_log_is_resolved_once_and_cached(env, monkeypatch):
    _register("bot_a", heartbeat_log=None)
    calls = {"n": 0}

    def _resolve(pid):
        calls["n"] += 1
        return "logs/DATA_INGESTION.log"

    monkeypatch.setattr(wd, "_resolve_heartbeat_log", _resolve)
    _mtime(monkeypatch, age=10)
    wd.check_heartbeat(P_INFO, NOW)
    wd.check_heartbeat(P_INFO, NOW)
    assert calls["n"] == 1, "open_files resolution must be cached per process lifetime"


# ── Crashed / missing processes are the crash path's job, not ours ───────────


def test_crashed_process_is_skipped(env, monkeypatch):
    _register("bot_a", rc=1)  # exited
    monkeypatch.setattr(wd.os.path, "getmtime", mock.Mock(side_effect=AssertionError("must not stat a dead proc")))
    wd.check_heartbeat(P_INFO, NOW)
    assert "bot_a" not in wd._hang_alerted


def test_missing_process_is_skipped(env, monkeypatch):
    # nothing registered
    monkeypatch.setattr(wd.os.path, "getmtime", mock.Mock(side_effect=AssertionError("must not stat")))
    wd.check_heartbeat(P_INFO, NOW)
    assert env["started"] == []


# ── Disable switch ───────────────────────────────────────────────────────────


def test_hang_limit_zero_disables_the_check(env, monkeypatch):
    monkeypatch.setattr(wd, "HANG_LIMIT_S", 0)
    _register("bot_a")
    monkeypatch.setattr(wd.os.path, "getmtime", mock.Mock(side_effect=AssertionError("must not stat when disabled")))
    wd.check_heartbeat(P_INFO, NOW)
    assert "bot_a" not in wd._hang_alerted


# ── Opt-in auto-restart rides the existing backoff, never blocks ─────────────


def test_autorestart_sets_backoff_deadline_when_delay_positive(env, monkeypatch):
    monkeypatch.setattr(wd, "HANG_AUTORESTART", True)
    monkeypatch.setattr(wd, "_compute_restart_delay", lambda name: 60)
    _register("bot_a")
    _mtime(monkeypatch, age=LIMIT + 60)

    wd.check_heartbeat(P_INFO, NOW)

    assert env["killed"] == ["bot_a"]
    assert env["started"] == [], "must wait out the backoff, not restart immediately"
    assert wd._restart_not_before["bot_a"] == pytest.approx(NOW + 60)


def test_autorestart_starts_immediately_when_delay_zero(env, monkeypatch):
    monkeypatch.setattr(wd, "HANG_AUTORESTART", True)
    monkeypatch.setattr(wd, "_compute_restart_delay", lambda name: 0)
    _register("bot_a")
    _mtime(monkeypatch, age=LIMIT + 60)

    wd.check_heartbeat(P_INFO, NOW)

    assert env["killed"] == ["bot_a"]
    assert env["started"] == ["bot_a"]
    assert "bot_a" not in wd._restart_not_before


# ── Selection logic: prefers logs/ dir (pure, no psutil/subprocess) ──────────


def test_pick_prefers_logs_dir():
    got = wd._pick_heartbeat_log([r"C:\Kythera\indicator_calculation.log", r"C:\Kythera\logs\DATA.log"])
    assert got.replace("\\", "/").endswith("logs/DATA.log")


def test_pick_returns_none_without_a_log():
    assert wd._pick_heartbeat_log([]) is None
    assert wd._pick_heartbeat_log([r"C:\Kythera\notes.txt"]) is None


def test_resolve_uses_isolated_probe(monkeypatch):
    monkeypatch.setattr(wd, "_probe_open_log_files", lambda pid: [r"C:\Kythera\logs\DATA.log"])
    assert wd._resolve_heartbeat_log(1).replace("\\", "/").endswith("logs/DATA.log")


def test_resolve_exempts_when_probe_unresolvable(monkeypatch):
    monkeypatch.setattr(wd, "_probe_open_log_files", lambda pid: None)
    assert wd._resolve_heartbeat_log(1) is None


# ── Probe isolation: a native crash / hang / spawn failure → exempt, never fatal ──


def _completed(returncode, stdout=""):
    cp = mock.MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    return cp


def test_probe_returns_paths_on_clean_exit(monkeypatch):
    monkeypatch.setattr(
        wd.subprocess, "run", lambda *a, **k: _completed(0, "C:\\Kythera\\logs\\DATA.log\n\n")
    )
    assert wd._probe_open_log_files(1) == [r"C:\Kythera\logs\DATA.log"]


def test_probe_exempts_on_access_violation_exit(monkeypatch):
    # The crash that took the whole watchdog down is now just a child return code.
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: _completed(-1073741819))
    assert wd._probe_open_log_files(1) is None


def test_probe_exempts_on_psutil_error_exit(monkeypatch):
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: _completed(4))
    assert wd._probe_open_log_files(1) is None


def test_probe_exempts_on_timeout(monkeypatch):
    def _boom(*a, **k):
        raise wd.subprocess.TimeoutExpired(cmd="probe", timeout=10)

    monkeypatch.setattr(wd.subprocess, "run", _boom)
    assert wd._probe_open_log_files(1) is None


def test_probe_exempts_on_spawn_failure(monkeypatch):
    def _boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(wd.subprocess, "run", _boom)
    assert wd._probe_open_log_files(1) is None

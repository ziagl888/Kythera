# backtest/test_watchdog_shutdown.py
"""
Unit tests for the watchdog's graceful shutdown path (P2.48).

``kill_process`` used a bare ``terminate()`` — on Windows a hard TerminateProcess
that gives the bot no chance to clean up and, critically, orphans the Indicator
Engine's ProcessPoolExecutor workers (they survive the parent kill and keep
computing → double-compute window). The fix:

  - every bot is started in its OWN process group (CREATE_NEW_PROCESS_GROUP) so
  - a stop sends CTRL_BREAK_EVENT to the WHOLE group (bot + its pool workers),
    with a graceful timeout before escalating to a hard kill.

These tests pin the group flag, the platform-specific stop signal, the escalation
to a hard kill on timeout, and the fallback when CTRL_BREAK cannot be delivered.
The structure is a genuine limit here: process-group signal delivery and
ProcessPool-worker teardown are only observable against a live Windows console,
so what is unit-testable is that the RIGHT signal is issued in the RIGHT order.

DB-free. Run with: pytest backtest/test_watchdog_shutdown.py -v
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
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


class _FakeProc:
    """Records terminate/kill/send_signal and controls how many wait() calls it
    survives before 'exiting'. ``exit_after=1`` = exits on the first wait."""

    def __init__(self, exit_after: int | None = 1, send_signal_error: Exception | None = None):
        self.calls: list[str] = []
        self.signals: list = []
        self._waits = 0
        self._exit_after = exit_after
        self._send_signal_error = send_signal_error

    def send_signal(self, sig):
        if self._send_signal_error is not None:
            raise self._send_signal_error
        self.signals.append(sig)

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")

    def wait(self, timeout=None):
        self._waits += 1
        if self._exit_after is not None and self._waits >= self._exit_after:
            return 0
        raise subprocess.TimeoutExpired(cmd="bot", timeout=timeout)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(wd, "running_processes", {})
    return None


def _register(proc, name="bot_a"):
    wd.running_processes[name] = {"process": proc, "info": {}, "start_time": 0.0}


# ── Bots are started in their own process group (prerequisite for CTRL_BREAK) ─


def test_start_process_uses_create_new_process_group(env, monkeypatch):
    captured = {}

    def _fake_popen(args, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(wd.subprocess, "Popen", _fake_popen)
    wd.start_process({"name": "bot_a", "script": "1_data_ingestion.py"})

    assert captured["kwargs"].get("creationflags") == wd._CREATE_NEW_PROCESS_GROUP


# ── POSIX branch: SIGTERM (terminate), no hard kill when it exits gracefully ──


def test_posix_stop_uses_terminate_no_kill(env, monkeypatch):
    monkeypatch.setattr(wd, "_IS_WINDOWS", False)
    proc = _FakeProc(exit_after=1)
    _register(proc)

    wd.kill_process("bot_a")

    assert proc.calls == ["terminate"], "POSIX stop should SIGTERM and, on clean exit, not hard-kill"
    assert proc.signals == []
    assert "bot_a" not in wd.running_processes


# ── Windows branch: CTRL_BREAK to the group, no hard kill on clean exit ──────


def test_windows_stop_sends_ctrl_break(env, monkeypatch):
    monkeypatch.setattr(wd, "_IS_WINDOWS", True)
    proc = _FakeProc(exit_after=1)
    _register(proc)

    wd.kill_process("bot_a")

    assert proc.signals == [wd._CTRL_BREAK_EVENT], "Windows stop should CTRL_BREAK the whole group"
    assert proc.calls == [], "graceful CTRL_BREAK exit must not escalate to a hard kill"
    assert "bot_a" not in wd.running_processes


# ── Escalation: a bot that ignores the graceful signal gets hard-killed ──────


def test_stop_escalates_to_hard_kill_on_timeout(env, monkeypatch):
    monkeypatch.setattr(wd, "_IS_WINDOWS", True)
    # Never exits on the graceful wait; exits only after the kill's own wait.
    proc = _FakeProc(exit_after=2)
    _register(proc)

    wd.kill_process("bot_a")

    assert proc.signals == [wd._CTRL_BREAK_EVENT]
    assert "kill" in proc.calls, "a wedged bot must be force-killed after the grace window"
    assert "bot_a" not in wd.running_processes


# ── Fallback: CTRL_BREAK undeliverable (no console) → terminate() ────────────


def test_ctrl_break_failure_falls_back_to_terminate(env, monkeypatch):
    monkeypatch.setattr(wd, "_IS_WINDOWS", True)
    proc = _FakeProc(exit_after=1, send_signal_error=OSError("no console attached"))
    _register(proc)

    graceful = wd._request_graceful_stop(proc, "bot_a")

    assert graceful is False, "a failed CTRL_BREAK must report it fell back"
    assert proc.calls == ["terminate"], "fallback must hard-terminate rather than do nothing"


def test_kill_process_survives_ctrl_break_failure(env, monkeypatch):
    """The whole kill_process still completes (bot removed) even when the
    graceful signal could not be delivered."""
    monkeypatch.setattr(wd, "_IS_WINDOWS", True)
    proc = _FakeProc(exit_after=1, send_signal_error=OSError("no console"))
    _register(proc)

    wd.kill_process("bot_a")

    assert "bot_a" not in wd.running_processes


# ── Unknown process name is a no-op ──────────────────────────────────────────


def test_kill_unknown_process_is_noop(env):
    wd.kill_process("does_not_exist")  # must not raise

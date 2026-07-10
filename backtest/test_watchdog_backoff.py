# backtest/test_watchdog_backoff.py
"""
Unit tests for the watchdog's crash-restart backoff (P1.37).

The backoff used to be a `time.sleep(delay)` inside the per-process monitor
loop. For up to 900 seconds that froze the ENTIRE watchdog: no other bot was
supervised, no park marker was honoured, no dashboard restart was consumed, no
health check ran. And after the sleep the crashed bot was started unconditionally
— even if the operator had parked it in the meantime.

The delay is now a per-process deadline. These tests pin both halves.

Run with: pytest backtest/test_watchdog_backoff.py -v
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
OTHER = {"name": "bot_b", "script": "2_indicator_engine.py"}

NOW = 1_000_000.0


class _Proc:
    """poll() -> None means alive; an int means it exited with that code."""

    def __init__(self, rc=None):
        self._rc = rc

    def poll(self):
        return self._rc


@pytest.fixture
def env(monkeypatch):
    started: list[str] = []
    killed: list[str] = []

    monkeypatch.setattr(wd, "running_processes", {})
    monkeypatch.setattr(wd, "_crash_history", {})
    monkeypatch.setattr(wd, "_restart_not_before", {})
    monkeypatch.setattr(wd, "is_parked", lambda script: False)
    monkeypatch.setattr(wd, "consume_restart", lambda script: False)
    monkeypatch.setattr(wd, "start_process", lambda p: started.append(p["name"]))
    monkeypatch.setattr(wd, "kill_process", lambda n: (killed.append(n), wd.running_processes.pop(n, None)))
    # A sleep inside the supervision pass is the very bug under test.
    monkeypatch.setattr(wd.time, "sleep", mock.Mock(side_effect=AssertionError("supervise_process must never block")))
    return {"started": started, "killed": killed}


def _crash(name: str, at: float = NOW) -> None:
    """Put `name` in running_processes as an exited process."""
    wd.running_processes[name] = {"process": _Proc(rc=1), "info": {}, "start_time": at - 100}


# ── The crash backoff no longer blocks ───────────────────────────────────────


def test_crashed_process_sets_a_deadline_instead_of_sleeping(env):
    _crash("bot_a")
    _crash("bot_a")  # second crash within the hour -> 15s per the schedule

    # First pass: crash #1, delay 0 -> immediate restart.
    wd.supervise_process(P_INFO, NOW)
    assert env["started"] == ["bot_a"]

    # Second pass: crash #2, delay 15 -> deadline, no restart, no sleep.
    _crash("bot_a")
    wd.supervise_process(P_INFO, NOW)
    assert env["started"] == ["bot_a"], "restarted despite an active backoff"
    assert wd._restart_not_before["bot_a"] == pytest.approx(NOW + 15)


def test_process_stays_down_until_its_deadline_passes(env):
    wd._restart_not_before["bot_a"] = NOW + 100

    wd.supervise_process(P_INFO, NOW + 50)
    assert env["started"] == [], "restarted before the deadline"
    assert "bot_a" in wd._restart_not_before

    wd.supervise_process(P_INFO, NOW + 101)
    assert env["started"] == ["bot_a"], "never restarted after the deadline"
    assert "bot_a" not in wd._restart_not_before, "deadline not cleared"


def test_backoff_on_one_bot_does_not_stall_the_others(env):
    """The whole point: bot_b is supervised while bot_a waits out its backoff."""
    wd._restart_not_before["bot_a"] = NOW + 900

    wd.supervise_process(P_INFO, NOW)
    wd.supervise_process(OTHER, NOW)

    assert env["started"] == ["bot_b"], "bot_b was not supervised during bot_a's backoff"


# ── An operator action during the backoff window wins ────────────────────────


def test_park_during_backoff_keeps_the_bot_down(env, monkeypatch):
    wd._restart_not_before["bot_a"] = NOW + 900
    monkeypatch.setattr(wd, "is_parked", lambda script: True)

    wd.supervise_process(P_INFO, NOW + 1000)  # deadline long past

    assert env["started"] == [], "parked bot was revived after the backoff"
    assert "bot_a" not in wd._restart_not_before, "stale deadline survived the park"


def test_dashboard_restart_overrides_the_backoff(env, monkeypatch):
    wd._restart_not_before["bot_a"] = NOW + 900
    monkeypatch.setattr(wd, "consume_restart", lambda script: True)

    wd.supervise_process(P_INFO, NOW)

    assert env["started"] == ["bot_a"], "explicit operator restart was swallowed by the backoff"
    assert "bot_a" not in wd._restart_not_before


# ── The schedule itself is unchanged ─────────────────────────────────────────


def test_backoff_schedule_is_unchanged(env):
    delays = [wd._compute_restart_delay("bot_a") for _ in range(6)]
    assert delays == [0, 15, 60, 300, 900, 900]

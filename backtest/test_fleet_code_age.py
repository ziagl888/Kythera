# backtest/test_fleet_code_age.py
"""
The "merged is not live" canary (T-2026-KYT-9050-071).

On 2026-08-02 the live checkout sat 45 commits behind origin/main while the
fleet traded on the previous evening's code — including an unshipped money-path
fix. Nothing alarmed; it surfaced only because a bot had been throwing the same
error for 17 days. A reboot starts the fleet WITHOUT pulling, so it looks like a
restart and is not one.

Two things this file holds down, both learned the hard way on that day:

  * the process set must be the WATCHDOG'S CHILDREN, not "python whose parent is
    python". The looser set contains a trainer's or backfill's workers, and one
    long-lived foreign job drags the oldest-process verdict backwards — measured:
    a 02:29 funding-backfill worker made a fleet restarted at 19:30 read as 13 h
    stale.
  * the watchdog is found STRUCTURALLY (most python children), never by
    CommandLine — that field is unreadable for the elevated fleet from an
    unelevated session, so a name match silently finds nothing.

And one judgement call: "1 of 41 stale" and "41 of 41 stale" are different
situations. The one-of case is the normal one here, because main_watchdog starts
dashboard.py through start_dashboard() rather than core.fleet.FLEET, so a
marker-based restart never recycles it.

Run with: pytest backtest/test_fleet_code_age.py -v
"""

from __future__ import annotations

import os
import sys
import unittest.mock as mock
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.ops import fleet_code_age as fca  # noqa: E402

HEAD = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
HEAD_EPOCH = HEAD.timestamp()
HOUR = 3600.0


def _procs(*offsets_h):
    """Fleet processes at HEAD + offset hours (negative = older than HEAD)."""
    return [{"pid": 1000 + i, "create_time": HEAD_EPOCH + h * HOUR} for i, h in enumerate(offsets_h)]


# ── the verdict ──────────────────────────────────────────────────────────────


def test_fleet_newer_than_head_is_current():
    with (
        mock.patch.object(fca, "head_commit_time", return_value=HEAD),
        mock.patch.object(fca, "fleet_processes", return_value=_procs(1, 2, 3)),
    ):
        out = fca.assess()
    assert out["verdict"] == "current"
    assert out["exit_code"] == 0


def test_whole_fleet_older_than_head_is_stale_and_not_partial():
    with (
        mock.patch.object(fca, "head_commit_time", return_value=HEAD),
        mock.patch.object(fca, "fleet_processes", return_value=_procs(-13, -13, -12)),
    ):
        out = fca.assess()
    assert out["verdict"] == "stale"
    assert out["exit_code"] == 1
    assert out["n_stale"] == 3
    assert out["partial"] is False


def test_a_single_stale_process_is_reported_as_partial():
    """The dashboard case: watchdog child, but not in FLEET, so markers miss it."""
    with (
        mock.patch.object(fca, "head_commit_time", return_value=HEAD),
        mock.patch.object(fca, "fleet_processes", return_value=_procs(-10, 5, 5, 5)),
    ):
        out = fca.assess()
    assert out["verdict"] == "stale"
    assert out["n_stale"] == 1
    assert out["n_processes"] == 4
    assert out["partial"] is True, "1 of 4 must not read like the whole fleet is behind"


def test_no_fleet_is_not_an_alarm():
    """A stopped box or a build machine is a legitimate state — crying wolf there
    would train the operator to ignore the canary."""
    with (
        mock.patch.object(fca, "head_commit_time", return_value=HEAD),
        mock.patch.object(fca, "fleet_processes", return_value=[]),
    ):
        out = fca.assess()
    assert out["verdict"] == "no_fleet"
    assert out["exit_code"] == 0


def test_unreadable_head_is_unmeasurable_not_stale():
    """A failed measurement is not a negative finding."""
    with (
        mock.patch.object(fca, "head_commit_time", return_value=None),
        mock.patch.object(fca, "fleet_processes", return_value=_procs(-99)),
    ):
        out = fca.assess()
    assert out["verdict"] == "unmeasurable"
    assert out["exit_code"] == 2


def test_lag_is_measured_from_the_oldest_process():
    with (
        mock.patch.object(fca, "head_commit_time", return_value=HEAD),
        mock.patch.object(fca, "fleet_processes", return_value=_procs(-5, -2, 1)),
    ):
        out = fca.assess()
    assert out["lag_hours"] == 5.0


# ── the process set ──────────────────────────────────────────────────────────


class _P:
    def __init__(self, pid, ppid, name="python.exe", create_time=0.0):
        self.info = {"pid": pid, "ppid": ppid, "name": name, "create_time": create_time}


def _with_psutil(procs):
    fake = mock.MagicMock()
    fake.process_iter.return_value = procs
    return mock.patch.dict(sys.modules, {"psutil": fake})


def test_only_the_watchdogs_children_count_not_a_foreign_python_job():
    """The 2026-08-02 false positive, pinned.

    PID 10 is the watchdog with three bots; PID 50 is a funding-backfill parent
    with one much older worker. The looser 'python child of python' set would
    include PID 51 and report the fleet as 12 h stale although every bot is fresh.
    """
    procs = [
        _P(10, 1, create_time=HEAD_EPOCH + HOUR),  # watchdog
        _P(11, 10, create_time=HEAD_EPOCH + HOUR),
        _P(12, 10, create_time=HEAD_EPOCH + HOUR),
        _P(13, 10, create_time=HEAD_EPOCH + HOUR),
        _P(50, 1, create_time=HEAD_EPOCH - 12 * HOUR),  # foreign job parent
        _P(51, 50, create_time=HEAD_EPOCH - 12 * HOUR),  # its worker
    ]
    with _with_psutil(procs):
        out = fca.fleet_processes()
    pids = sorted(p["pid"] for p in out)
    assert pids == [11, 12, 13], "the backfill worker must not be counted as fleet"


def test_py_exe_launchers_are_recognised_as_python():
    procs = [
        _P(10, 1, name="py.exe", create_time=HEAD_EPOCH),
        _P(11, 10, name="python.exe", create_time=HEAD_EPOCH),
        _P(12, 10, name="python.exe", create_time=HEAD_EPOCH),
    ]
    with _with_psutil(procs):
        out = fca.fleet_processes()
    assert sorted(p["pid"] for p in out) == [11, 12]


def test_no_python_pairs_yields_an_empty_set_not_a_crash():
    with _with_psutil([_P(10, 1), _P(20, 1)]):
        assert fca.fleet_processes() == []


def test_processes_without_a_create_time_are_dropped():
    procs = [
        _P(10, 1, create_time=HEAD_EPOCH),
        _P(11, 10, create_time=HEAD_EPOCH),
        _P(12, 10, create_time=None),
    ]
    with _with_psutil(procs):
        out = fca.fleet_processes()
    assert [p["pid"] for p in out] == [11]

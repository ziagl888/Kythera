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
  * the process table must arrive in ONE call. psutil's per-process attribute
    machinery hung a fleet restart for ten minutes (T-2026-KYT-9050-079), and
    the expensive attribute was `name`, not `create_time` — measured on SRV02,
    process_iter(["pid"]) walked 293 processes in 9.6 s while
    process_iter(["pid","name"]) managed 46 in 45 s. A failed query must read
    as no_fleet, never as stale.

And one judgement call: "1 of 41 stale" and "41 of 41 stale" are different
situations. The one-of case is the normal one here, because main_watchdog starts
dashboard.py through start_dashboard() rather than core.fleet.FLEET, so a
marker-based restart never recycles it.

Run with: pytest backtest/test_fleet_code_age.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
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


def _rows(*specs):
    """Process-table rows as _query_python_processes returns them."""
    return [{"pid": pid, "ppid": ppid, "create_time": ct} for pid, ppid, ct in specs]


def _with_table(rows):
    return mock.patch.object(fca, "_query_python_processes", return_value=rows)


def test_the_process_table_is_fetched_in_exactly_one_call():
    """The regression that blocked a fleet restart for ten minutes.

    psutil's per-process attribute machinery is the trap, and the expensive
    attribute is `name`, not `create_time`: measured on SRV02 2026-08-03,
    process_iter(["pid"]) walked 293 processes in 9.6 s while
    process_iter(["pid","name"]) managed 46 in 45 s. Any design that pays a
    per-process cost is on restart_fleet.ps1's critical path and will hang it
    again — the whole table has to arrive in one call.
    """
    calls = []

    def _counted(*a, **kw):
        calls.append(1)
        return _rows((10, 1, HEAD_EPOCH), (11, 10, HEAD_EPOCH), (12, 10, HEAD_EPOCH))

    with mock.patch.object(fca, "_query_python_processes", side_effect=_counted):
        fca.fleet_processes()
    assert len(calls) == 1, f"the process table must be fetched once, not {len(calls)}x"


def test_an_unreadable_process_table_reads_as_no_fleet_never_as_stale():
    """A timed-out or failed query is a failed MEASUREMENT, not a finding.

    The canary runs in restart_fleet.ps1's preflight; a query that gives up
    under load must not manufacture a staleness alarm on the way out.
    """
    with (
        mock.patch.object(fca, "head_commit_time", return_value=HEAD),
        _with_table([]),
    ):
        out = fca.assess()
    assert out["verdict"] == "no_fleet"
    assert out["exit_code"] == 0


def test_the_query_carries_its_own_timeout_and_survives_hitting_it():
    """Without a timeout the canary inherits the hang it was fixed for — and
    when the timeout does fire it must return empty, not propagate."""
    timed_out = subprocess.TimeoutExpired(cmd="powershell", timeout=7)
    with mock.patch.object(fca.os, "name", "nt"):
        with mock.patch.object(fca.subprocess, "run", side_effect=timed_out) as run:
            assert fca._query_python_processes(timeout_sec=7) == []
    assert run.call_args.kwargs["timeout"] == 7


def test_the_name_filter_covers_python_and_the_py_launcher():
    """Name matching happens in WQL now, so the filter text IS the behaviour.

    'py.exe' is matched exactly rather than by LIKE 'py%', which would also
    swallow pythonw, pycharm and py7zr.
    """
    assert "python%" in fca._WQL_PYTHON
    assert "py.exe" in fca._WQL_PYTHON
    assert fca._WQL_PYTHON in fca._CIM_QUERY


def test_a_row_that_cannot_be_parsed_is_dropped_not_guessed_at():
    payload = json.dumps(
        [
            {"ProcessId": 11, "ParentProcessId": 10, "Created": 1785626975.0},
            {"ProcessId": 12, "ParentProcessId": 10},  # no Created
            {"ProcessId": "nonsense", "ParentProcessId": 10, "Created": 1.0},
        ]
    )
    completed = mock.Mock(returncode=0, stdout=payload)
    with mock.patch.object(fca.os, "name", "nt"):
        with mock.patch.object(fca.subprocess, "run", return_value=completed):
            out = fca._query_python_processes()
    assert [p["pid"] for p in out] == [11]


def test_a_single_row_is_unwrapped_from_convertto_json():
    """ConvertTo-Json emits an object, not a list, for exactly one result."""
    payload = json.dumps({"ProcessId": 11, "ParentProcessId": 10, "Created": 1785626975.0})
    completed = mock.Mock(returncode=0, stdout=payload)
    with mock.patch.object(fca.os, "name", "nt"):
        with mock.patch.object(fca.subprocess, "run", return_value=completed):
            out = fca._query_python_processes()
    assert [p["pid"] for p in out] == [11]


def test_only_the_watchdogs_children_count_not_a_foreign_python_job():
    """The 2026-08-02 false positive, pinned.

    PID 10 is the watchdog with three bots; PID 50 is a funding-backfill parent
    with one much older worker. The looser 'python child of python' set would
    include PID 51 and report the fleet as 12 h stale although every bot is fresh.
    """
    rows = _rows(
        (10, 1, HEAD_EPOCH + HOUR),  # watchdog
        (11, 10, HEAD_EPOCH + HOUR),
        (12, 10, HEAD_EPOCH + HOUR),
        (13, 10, HEAD_EPOCH + HOUR),
        (50, 1, HEAD_EPOCH - 12 * HOUR),  # foreign job parent
        (51, 50, HEAD_EPOCH - 12 * HOUR),  # its worker
    )
    with _with_table(rows):
        out = fca.fleet_processes()
    assert sorted(p["pid"] for p in out) == [11, 12, 13], "the backfill worker must not be counted as fleet"


def test_no_python_pairs_yields_an_empty_set_not_a_crash():
    with _with_table(_rows((10, 1, HEAD_EPOCH), (20, 1, HEAD_EPOCH))):
        assert fca.fleet_processes() == []

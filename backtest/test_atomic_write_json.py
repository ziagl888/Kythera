# backtest/test_atomic_write_json.py
"""
Unit tests for core.state_utils.atomic_write_json (P2.49).

Two bugs are pinned here:

  1. A FIXED ``.tmp`` name: two writers to the same target both wrote to
     ``<path>.tmp`` and corrupted each other. The write now uses a unique
     ``tempfile.mkstemp`` name in the target directory.
  2. ``os.replace`` fails with PermissionError on Windows while a reader holds
     the target file open — the old broad ``except`` swallowed it and the update
     was lost SILENTLY. It now retries a few times and, on final failure, LOGS
     and returns False (never a silent loss).

DB-free, filesystem-only. Run with: pytest backtest/test_atomic_write_json.py -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import state_utils
from core.state_utils import atomic_read_json, atomic_write_json


# ── Happy path ───────────────────────────────────────────────────────────────


def test_roundtrip(tmp_path):
    target = str(tmp_path / "state.json")
    assert atomic_write_json(target, {"a": 1, "b": [1, 2, 3]}) is True
    assert atomic_read_json(target) == {"a": 1, "b": [1, 2, 3]}


def test_leaves_no_tmp_behind(tmp_path):
    target = str(tmp_path / "state.json")
    assert atomic_write_json(target, {"x": 1}) is True
    # Only the target file — no leftover tmp artefacts in the directory.
    assert os.listdir(tmp_path) == ["state.json"]


def test_creates_missing_parent_dir(tmp_path):
    target = str(tmp_path / "nested" / "deep" / "state.json")
    assert atomic_write_json(target, {"ok": True}) is True
    assert atomic_read_json(target) == {"ok": True}


def test_empty_path_is_rejected():
    assert atomic_write_json("", {"a": 1}) is False


# ── Bug 1: unique tmp name (no fixed ``.tmp`` collision) ─────────────────────


def test_tmp_name_is_unique_not_fixed(tmp_path):
    """The old code always used ``<path>.tmp`` — the source of the parallel-writer
    corruption. Assert we no longer create that fixed name."""
    target = str(tmp_path / "state.json")
    captured: list[str] = []
    real_mkstemp = state_utils.tempfile.mkstemp

    def _spy(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        captured.append(path)
        return fd, path

    with mock.patch.object(state_utils.tempfile, "mkstemp", side_effect=_spy):
        assert atomic_write_json(target, {"a": 1}) is True

    assert captured, "mkstemp was not used — fixed-name tmp path may have returned"
    assert captured[0] != target + ".tmp", "still using the fixed .tmp name"
    # tmp lives in the same directory (keeps os.replace atomic on one filesystem)
    assert os.path.dirname(captured[0]) == os.path.dirname(os.path.abspath(target))


def test_parallel_writers_do_not_share_a_tmp(tmp_path):
    """Two mkstemp calls for the same target must yield distinct tmp paths."""
    target = str(tmp_path / "state.json")
    directory = os.path.dirname(os.path.abspath(target))
    basename = os.path.basename(target)
    fd1, p1 = state_utils.tempfile.mkstemp(dir=directory, prefix=f".{basename}.", suffix=".tmp")
    fd2, p2 = state_utils.tempfile.mkstemp(dir=directory, prefix=f".{basename}.", suffix=".tmp")
    try:
        assert p1 != p2
    finally:
        os.close(fd1)
        os.close(fd2)
        os.remove(p1)
        os.remove(p2)


# ── Bug 2: os.replace retry on PermissionError ───────────────────────────────


def test_replace_retries_then_succeeds(tmp_path):
    """A reader briefly holding the target open (PermissionError) must NOT lose
    the update: os.replace is retried and eventually lands."""
    target = str(tmp_path / "state.json")
    real_replace = state_utils.os.replace
    calls = {"n": 0}

    def _flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:  # fail twice, succeed on the third try
            raise PermissionError("target held open by a reader")
        return real_replace(src, dst)

    with (
        mock.patch.object(state_utils.os, "replace", side_effect=_flaky_replace),
        mock.patch.object(state_utils.time, "sleep"),  # no real waiting
    ):
        assert atomic_write_json(target, {"v": 42}) is True

    assert calls["n"] == 3
    assert atomic_read_json(target) == {"v": 42}
    # The tmp file was consumed by the successful replace — nothing left over.
    assert os.listdir(tmp_path) == ["state.json"]


def test_replace_permanent_failure_logs_and_cleans_up(tmp_path, caplog):
    """If the target stays locked, the write must FAIL LOUDLY (return False +
    ERROR log) and leave no tmp litter — not silently drop the update."""
    target = str(tmp_path / "state.json")

    with (
        mock.patch.object(state_utils.os, "replace", side_effect=PermissionError("locked")),
        mock.patch.object(state_utils.time, "sleep"),
        caplog.at_level("ERROR"),
    ):
        assert atomic_write_json(target, {"v": 1}) is False

    assert any("NICHT geschrieben" in r.message for r in caplog.records), "silent update loss — no ERROR logged"
    # No tmp artefacts left behind after the failed write.
    assert os.listdir(tmp_path) == []


def test_non_permission_error_is_caught_and_cleaned(tmp_path):
    """A non-PermissionError from os.replace is handled defensively (return
    False, tmp cleaned) rather than propagating."""
    target = str(tmp_path / "state.json")

    with mock.patch.object(state_utils.os, "replace", side_effect=OSError("cross-device")):
        assert atomic_write_json(target, {"v": 1}) is False

    assert os.listdir(tmp_path) == []


# ── The written file is valid JSON at all times (atomicity contract) ─────────


def test_written_content_is_complete_json(tmp_path):
    target = str(tmp_path / "state.json")
    payload = {"nested": {"list": list(range(50)), "s": "ä-ö-ü"}}
    assert atomic_write_json(target, payload) is True
    with open(target, encoding="utf-8") as f:
        assert json.load(f) == payload

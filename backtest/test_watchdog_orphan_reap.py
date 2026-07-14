# backtest/test_watchdog_orphan_reap.py
"""DB-freie Tests für die Watchdog-Waisen-/Mutex-Deadlock-Recovery
(T-2026-CU-9050-127).

Pinnt zwei Invarianten des geld-kritischen P0.2-Pfads:
  1. _reap_orphans zählt NUR tatsächlich beendete Prozesse (AccessDenied-
     Überlebende NICHT), schliesst sich selbst + eigene Kinder aus.
  2. _acquire_single_instance_lock löst den Waisen-Mutex-Deadlock: bei Konflikt
     wird der verwaiste Vor-Watchdog gereapt, der eigene Handle geschlossen und
     die Akquise EINMAL wiederholt; ist der Halter nicht reapbar -> harter Exit
     wie bisher (kein Regress).

Run: pytest backtest/test_watchdog_orphan_reap.py -v
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import psutil  # noqa: E402

import main_watchdog as mw  # noqa: E402

WD = frozenset([mw._WATCHDOG_SCRIPT])  # {"main_watchdog.py"}


class _FakeProc:
    def __init__(self, pid, cmdline, name="python.exe", access_denied=False):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "cmdline": cmdline}
        self._ad = access_denied
        self.terminated = False
        self.killed = False

    def terminate(self):
        if self._ad:
            raise psutil.AccessDenied(self.pid)
        self.terminated = True

    def kill(self):
        if self._ad:
            raise psutil.AccessDenied(self.pid)
        self.killed = True


def _patch_psutil(monkeypatch, procs, own_children=()):
    monkeypatch.setattr(mw.psutil, "process_iter", lambda attrs=None: list(procs))

    class _SelfProc:
        def children(self, recursive=False):
            return [types.SimpleNamespace(pid=p) for p in own_children]

    monkeypatch.setattr(mw.psutil, "Process", lambda pid: _SelfProc())

    def _fake_wait(plist, timeout=None):
        gone = [p for p in plist if not getattr(p, "_ad", False)]
        alive = [p for p in plist if getattr(p, "_ad", False)]  # AccessDenied survives
        return gone, alive

    monkeypatch.setattr(mw.psutil, "wait_procs", _fake_wait)


# ── 1. _reap_orphans ─────────────────────────────────────────────────────────
def test_reap_zero_when_no_match(monkeypatch):
    _patch_psutil(monkeypatch, [_FakeProc(111, ["python", "not_a_target.py"])])
    assert mw._reap_orphans(WD) == 0


def test_reap_kills_matching_and_excludes_self_and_children(monkeypatch):
    me = os.getpid()
    orphan = _FakeProc(222, ["python", "main_watchdog.py"])
    procs = [
        _FakeProc(me, ["python", "main_watchdog.py"]),  # self — excluded
        _FakeProc(333, ["python", "main_watchdog.py"]),  # own child — excluded
        _FakeProc(444, ["python", "not_a_target.py"]),  # non-match
        orphan,  # the one true orphan watchdog
    ]
    _patch_psutil(monkeypatch, procs, own_children=(333,))
    reaped = mw._reap_orphans(WD)
    assert reaped == 1
    assert orphan.terminated is True


def test_reap_does_not_count_access_denied_survivors(monkeypatch):
    # Elevated orphan the reaper may not kill -> must NOT count as reaped
    # (otherwise the mutex-retry would fire on a still-held mutex).
    _patch_psutil(monkeypatch, [_FakeProc(222, ["python", "main_watchdog.py"], access_denied=True)])
    assert mw._reap_orphans(WD) == 0


# ── 2. _acquire_single_instance_lock deadlock recovery ───────────────────────
def _fake_ctypes(err_seq, handle_seq):
    state = {"last": 0}
    errs, handles, closed = list(err_seq), list(handle_seq), []
    k32 = types.SimpleNamespace()

    def _create(_a, _b, _name):
        state["last"] = errs.pop(0)
        return handles.pop(0)

    k32.CreateMutexW = _create
    k32.GetLastError = lambda: state["last"]
    k32.CloseHandle = lambda h: (closed.append(h), 1)[1]
    return types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=k32)), closed


@pytest.mark.skipif(os.name != "nt", reason="mutex path is Windows-only")
def test_acquire_retries_after_reaping_orphan(monkeypatch):
    mw._instance_mutex = None
    fake_ct, closed = _fake_ctypes([mw._ERROR_ALREADY_EXISTS, 0], [111, 222])
    monkeypatch.setattr(mw, "ctypes", fake_ct)
    monkeypatch.setattr(mw, "_reap_orphans", lambda scripts: 1)  # orphan watchdog reaped
    monkeypatch.setattr(mw.time, "sleep", lambda s: None)

    mw._acquire_single_instance_lock()  # must NOT sys.exit

    assert mw._instance_mutex == 222, "should acquire a fresh mutex on the retry"
    assert 111 in closed, "the first (conflicting) handle must be closed before retry"


@pytest.mark.skipif(os.name != "nt", reason="mutex path is Windows-only")
def test_acquire_exits_when_holder_not_reapable(monkeypatch):
    mw._instance_mutex = None
    fake_ct, _closed = _fake_ctypes([mw._ERROR_ALREADY_EXISTS], [111])
    monkeypatch.setattr(mw, "ctypes", fake_ct)
    monkeypatch.setattr(mw, "_reap_orphans", lambda scripts: 0)  # cannot reap the holder
    monkeypatch.setattr(mw.time, "sleep", lambda s: None)

    with pytest.raises(SystemExit):
        mw._acquire_single_instance_lock()


@pytest.mark.skipif(os.name != "nt", reason="mutex path is Windows-only")
def test_acquire_clean_start_no_reap(monkeypatch):
    # Normal start: mutex free -> acquire, never touch the reaper (mutex-first).
    mw._instance_mutex = None
    fake_ct, _closed = _fake_ctypes([0], [999])
    monkeypatch.setattr(mw, "ctypes", fake_ct)
    called = {"n": 0}
    monkeypatch.setattr(mw, "_reap_orphans", lambda scripts: called.__setitem__("n", called["n"] + 1) or 0)

    mw._acquire_single_instance_lock()

    assert mw._instance_mutex == 999
    assert called["n"] == 0, "clean start must not invoke the orphan reaper"

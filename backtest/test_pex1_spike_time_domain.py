"""T-2026-KYT-9050-061 — bot 30 (PEX1) must not die on spike_time anymore.

The live defect: `pump_dump_events.spike_time` is on the live DB
`timestamp WITH time zone` (the repo DDL in `10_pump_dump_detector.py` says
`TIMESTAMP` — the table was aged at some point), so psycopg2 returns
AWARE datetimes. `detect_spike_time_offset_h` subtracted a naive
`now` from it and threw on EVERY scan cycle

    PEX1 scan error: can't subtract offset-naive and offset-aware datetimes

in try block BEFORE the event loop — 8166 failures in the four most recent
`logs/watchdog_debug_*`, not a single successful scan since at least
2026-07-19.

This test drives the scan path DB-free with a fake cursor, once with
aware and once with naive spike_time values. Before the fix the aware run threw.
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")

UTC = datetime.timezone.utc


def _load_bot():
    """Module name begins with a digit — regular import doesn't work."""
    path = os.path.join(REPO_ROOT, "30_ai_pex1_bot.py")
    spec = importlib.util.spec_from_file_location("pex1_bot_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


BOT = _load_bot()


class FakeCursor:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **kw):
        pass

    def fetchone(self):
        return (self._value,)


class FakeConn:
    def __init__(self, value):
        self._value = value

    def cursor(self):
        return FakeCursor(self._value)


# ── detect_spike_time_offset_h ────────────────────────────────────────────


def test_aware_max_does_not_raise_and_reports_zero_offset():
    """The live case. A timestamptz value IS an instant — there is no
    domain question, so 0, and above all: no exception."""
    aware = datetime.datetime.now(UTC) - datetime.timedelta(seconds=30)
    assert BOT.detect_spike_time_offset_h(FakeConn(aware)) == 0


def test_aware_max_with_a_non_utc_offset_is_still_zero():
    # PG returns timestamptz in the session offset; a +03:00 value is the same
    # instant and must not produce a spurious offset.
    tz = datetime.timezone(datetime.timedelta(hours=3))
    aware_local = (datetime.datetime.now(UTC) - datetime.timedelta(seconds=30)).astimezone(tz)
    assert BOT.detect_spike_time_offset_h(FakeConn(aware_local)) == 0


def test_naive_local_max_still_measures_the_legacy_offset():
    """Legacy path (naive column, local time): the measurement must be
    preserved, otherwise spike_age would be wrong after a rollback to old data."""
    naive_local = datetime.datetime.now(UTC).replace(tzinfo=None) + datetime.timedelta(hours=3)
    assert BOT.detect_spike_time_offset_h(FakeConn(naive_local)) == 3


def test_naive_utc_max_measures_zero():
    naive_utc = datetime.datetime.now(UTC).replace(tzinfo=None)
    assert BOT.detect_spike_time_offset_h(FakeConn(naive_utc)) == 0


def test_empty_table_is_zero():
    assert BOT.detect_spike_time_offset_h(FakeConn(None)) == 0


# ── spike_time_to_utc_naive: the same normalisation in process_event ──────


@pytest.mark.parametrize("offset_h", [0, 2, 3])
def test_aware_value_normalizes_to_naive_utc_regardless_of_offset(offset_h):
    aware = datetime.datetime(2026, 8, 1, 16, 10, tzinfo=UTC)
    assert BOT.spike_time_to_utc_naive(aware, offset_h) == datetime.datetime(2026, 8, 1, 16, 10)


def test_aware_non_utc_value_is_converted_not_stripped():
    tz = datetime.timezone(datetime.timedelta(hours=3))
    aware_local = datetime.datetime(2026, 8, 1, 19, 10, tzinfo=tz)
    assert BOT.spike_time_to_utc_naive(aware_local, 0) == datetime.datetime(2026, 8, 1, 16, 10)


def test_naive_value_keeps_the_offset_subtraction():
    naive_local = datetime.datetime(2026, 8, 1, 19, 10)
    assert BOT.spike_time_to_utc_naive(naive_local, 3) == datetime.datetime(2026, 8, 1, 16, 10)
    assert BOT.spike_time_to_utc_naive(naive_local, 0) == naive_local


def test_none_is_passed_through():
    assert BOT.spike_time_to_utc_naive(None, 0) is None


def test_spike_age_arithmetic_survives_both_domains():
    """The actual crash path: `now` (naive) minus normalised spike_time.
    Must run through for both domains without TypeError and yield the same age."""
    now = datetime.datetime(2026, 8, 1, 16, 40)
    aware = datetime.datetime(2026, 8, 1, 16, 10, tzinfo=UTC)
    naive_local = datetime.datetime(2026, 8, 1, 19, 10)
    for value, offset in ((aware, 0), (naive_local, 3)):
        age_min = (now - BOT.spike_time_to_utc_naive(value, offset)).total_seconds() / 60.0
        assert age_min == 30.0


# ── Watermark: the second place where the domains meet ────────────


def test_boot_sentinel_is_aware_like_the_live_column():
    src = open(os.path.join(REPO_ROOT, "30_ai_pex1_bot.py"), encoding="utf-8").read()
    assert "datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)" in src
    # no more max() over sentinel and column value (ASC + strictly > watermark)
    assert 'max(watermark, event["spike_time"])' not in src

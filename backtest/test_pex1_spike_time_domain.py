"""T-2026-KYT-9050-061 — Bot 30 (PEX1) darf an spike_time nicht mehr sterben.

Der Live-Defekt: `pump_dump_events.spike_time` ist auf der Live-DB
`timestamp WITH time zone` (die Repo-DDL in `10_pump_dump_detector.py` sagt
`TIMESTAMP` — die Tabelle wurde irgendwann gealtert), psycopg2 liefert also
AWARE datetimes. `detect_spike_time_offset_h` subtrahierte davon ein naives
`now` und warf in JEDEM Scan-Zyklus

    PEX1-Scan-Fehler: can't subtract offset-naive and offset-aware datetimes

im try-Block VOR der Event-Schleife — 8166 Fehlschläge in den vier jüngsten
`logs/watchdog_debug_*`, kein einziger erfolgreicher Scan seit mindestens
2026-07-19.

Dieser Test fährt den Scan-Pfad DB-frei mit einem Fake-Cursor durch, einmal mit
aware und einmal mit naiven spike_time-Werten. Vor dem Fix wirft der aware-Lauf.
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
    """Der Modulname beginnt mit einer Ziffer — regulärer Import geht nicht."""
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
    """Der Live-Fall. Ein timestamptz-Wert IST ein Instant — es gibt keine
    Domänen-Frage, also 0, und vor allem: keine Exception."""
    aware = datetime.datetime.now(UTC) - datetime.timedelta(seconds=30)
    assert BOT.detect_spike_time_offset_h(FakeConn(aware)) == 0


def test_aware_max_with_a_non_utc_offset_is_still_zero():
    # PG liefert timestamptz im Session-Offset; ein +03:00-Wert ist derselbe
    # Instant und darf keinen Scheinoffset erzeugen.
    tz = datetime.timezone(datetime.timedelta(hours=3))
    aware_local = (datetime.datetime.now(UTC) - datetime.timedelta(seconds=30)).astimezone(tz)
    assert BOT.detect_spike_time_offset_h(FakeConn(aware_local)) == 0


def test_naive_local_max_still_measures_the_legacy_offset():
    """Legacy-Pfad (naive Spalte, Lokalzeit): die Messung muss erhalten
    bleiben, sonst wäre spike_age nach einem Rollback auf alte Daten falsch."""
    naive_local = datetime.datetime.now(UTC).replace(tzinfo=None) + datetime.timedelta(hours=3)
    assert BOT.detect_spike_time_offset_h(FakeConn(naive_local)) == 3


def test_naive_utc_max_measures_zero():
    naive_utc = datetime.datetime.now(UTC).replace(tzinfo=None)
    assert BOT.detect_spike_time_offset_h(FakeConn(naive_utc)) == 0


def test_empty_table_is_zero():
    assert BOT.detect_spike_time_offset_h(FakeConn(None)) == 0


# ── spike_time_to_utc_naive: dieselbe Normalisierung in process_event ──────


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
    """Der eigentliche Absturzpfad: `now` (naiv) minus normalisiertes spike_time.
    Muss für beide Domänen ohne TypeError durchlaufen und dasselbe Alter geben."""
    now = datetime.datetime(2026, 8, 1, 16, 40)
    aware = datetime.datetime(2026, 8, 1, 16, 10, tzinfo=UTC)
    naive_local = datetime.datetime(2026, 8, 1, 19, 10)
    for value, offset in ((aware, 0), (naive_local, 3)):
        age_min = (now - BOT.spike_time_to_utc_naive(value, offset)).total_seconds() / 60.0
        assert age_min == 30.0


# ── Watermark: der zweite Ort, an dem sich die Domänen treffen ────────────


def test_boot_sentinel_is_aware_like_the_live_column():
    src = open(os.path.join(REPO_ROOT, "30_ai_pex1_bot.py"), encoding="utf-8").read()
    assert "datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)" in src
    # kein max() mehr über Sentinel und Spaltenwert (ASC + strikt > watermark)
    assert 'max(watermark, event["spike_time"])' not in src

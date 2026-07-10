"""Standalone (DB-free) guard tests for the central UTC time source.

Background (T-2026-CU-9050-032, root cause R3): the fleet had no single time
source. `core/time.py` is now the only sanctioned one, and ruff's DTZ rules
keep new code from reaching around it. Everything that touches a timestamp
will import from here, so the semantics need a guard that does not depend on
the machine's local timezone — the bug class this module exists to kill is
exactly "works on my box, drifts on the Bucharest VPS".

These tests pin the contract without a DB and without assuming a local TZ:
  1. utc_now() is aware/UTC, utc_now_naive() is the same instant without tzinfo.
  2. utc_now_naive() reproduces the deprecated datetime.utcnow() semantics.
  3. to_utc() treats a naive input as UTC and converts an aware one correctly.
  4. as_naive_utc() round-trips through to_utc().
  5. from_unix_ts() reads epoch seconds and milliseconds as UTC.
  6. The module never returns a naive LOCAL timestamp, whatever TZ is set.

Run: py -3.13 backtest/test_time.py
"""

import datetime
import os
import sys
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.time import UTC, as_naive_utc, from_unix_ts, to_utc, utc_now, utc_now_naive  # noqa: E402

BUCHAREST_SUMMER = datetime.timezone(datetime.timedelta(hours=3))  # EEST, the live VPS offset


def test_utc_now_is_aware_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None, "utc_now() must be timezone-aware"
    assert now.utcoffset() == datetime.timedelta(0), "utc_now() must carry a zero UTC offset"


def test_utc_now_naive_is_the_same_instant() -> None:
    aware, naive = utc_now(), utc_now_naive()
    assert naive.tzinfo is None, "utc_now_naive() must be naive"
    delta = abs((aware.replace(tzinfo=None) - naive).total_seconds())
    assert delta < 1.0, f"utc_now_naive() drifted {delta}s from utc_now()"


def test_utc_now_naive_matches_deprecated_utcnow_semantics() -> None:
    # The legacy naive columns were filled by datetime.utcnow(); the replacement
    # must be a drop-in, not a local-time regression.
    reference = datetime.datetime.now(UTC).replace(tzinfo=None)
    delta = abs((utc_now_naive() - reference).total_seconds())
    assert delta < 1.0, f"utc_now_naive() is not utcnow()-equivalent (off by {delta}s)"


def test_to_utc_assumes_naive_input_is_utc() -> None:
    naive = datetime.datetime(2026, 7, 9, 12, 0, 0)
    assert to_utc(naive) == datetime.datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


def test_to_utc_converts_an_aware_input() -> None:
    bucharest = datetime.datetime(2026, 7, 9, 15, 0, 0, tzinfo=BUCHAREST_SUMMER)
    assert to_utc(bucharest) == datetime.datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


def test_to_utc_is_idempotent() -> None:
    aware = datetime.datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
    assert to_utc(to_utc(aware)) == aware


def test_as_naive_utc_strips_after_converting() -> None:
    bucharest = datetime.datetime(2026, 7, 9, 15, 0, 0, tzinfo=BUCHAREST_SUMMER)
    stripped = as_naive_utc(bucharest)
    assert stripped.tzinfo is None
    assert stripped == datetime.datetime(2026, 7, 9, 12, 0, 0)


def test_from_unix_ts_seconds_and_milliseconds() -> None:
    assert from_unix_ts(0) == datetime.datetime(1970, 1, 1, tzinfo=UTC)
    # Binance delivers epoch milliseconds; the ms=True path must not be 1000x off.
    assert from_unix_ts(1_700_000_000_000, ms=True) == from_unix_ts(1_700_000_000)
    assert from_unix_ts(1_700_000_000).tzinfo is not None


def test_nothing_returns_naive_local_time_under_a_shifted_tz() -> None:
    """The whole point of R3: results must not depend on the process timezone.

    Skipped where time.tzset() is unavailable (Windows) — the assertions below
    still run against whatever TZ the machine has, so a naive-local regression
    would fail on the Linux CI runner.
    """
    tzset = getattr(_time, "tzset", None)
    if tzset is None:
        return
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Europe/Bucharest"
        tzset()
        reference = datetime.datetime.now(UTC).replace(tzinfo=None)
        assert abs((utc_now_naive() - reference).total_seconds()) < 1.0
        assert utc_now().utcoffset() == datetime.timedelta(0)
        naive = datetime.datetime(2026, 7, 9, 12, 0, 0)
        assert to_utc(naive).hour == 12, "to_utc() must not localise a naive input to the process TZ"
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        tzset()


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    if failures:
        print(f"\n{failures}/{len(tests)} time-source invariants BROKEN")
        return 1
    print(f"OK - all {len(tests)} UTC time-source invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())

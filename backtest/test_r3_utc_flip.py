"""T-2026-KYT-9050-005 — R3 flip: pool timezone, P2.3 writer, one history knob.

DB-free. The flip's whole risk is that the three halves drift apart again:
the session TZ (core/database.py), the naive-local writer (3_detectors.py) and
the readers that used to compensate for the offset. Each of them is pinned
here, plus the semantics of the single history knob that replaced the six
hand-rolled compensations.
"""

from __future__ import annotations

import datetime
import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# core.database imports core.config at module level (repo-wide test convention).
os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")

from core.time import (  # noqa: E402
    LEGACY_WRITER_TZ,
    R3_CUTOVER_ENV,
    legacy_naive_to_utc,
    r3_cutover,
    r3_history_mode,
    utc_to_legacy_naive,
)


def _src(name: str) -> str:
    with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _clean_env():
    """Every test states its own history mode — no leakage between them."""
    before = os.environ.pop(R3_CUTOVER_ENV, None)
    yield
    os.environ.pop(R3_CUTOVER_ENV, None)
    if before is not None:
        os.environ[R3_CUTOVER_ENV] = before


# ── the three halves of the flip ──────────────────────────────────────────


def test_pool_pins_session_timezone_utc():
    # The GUC has to sit in the libpq options string of the POOL, not in a
    # per-query SET: a pooled connection is reused across bots.
    src = _src(os.path.join("core", "database.py"))
    assert '_DEFAULT_SESSION_TZ = "UTC"' in src
    assert 'f"-c timezone={_DEFAULT_SESSION_TZ}"' in src

    from core.database import _connect_options

    assert "-c timezone=UTC" in _connect_options()


def test_detectors_writer_is_utc_naive_not_local():
    # P2.3. Without this, the flip would have moved NOW() to UTC while
    # active_trades_master.time stayed local -> 33_ai_fif1's DB-side 1h/24h
    # windows compare two domains.
    src = _src("3_detectors.py")
    assert "now = utc_now_naive()" in src
    assert "from core.time import utc_now_naive" in src
    assert "datetime.datetime.now()" not in src


def test_no_hand_rolled_bucharest_compensation_left():
    """The six compensations are gone from the fleet's read path.

    A hand-rolled ``tz_localize(<legacy tz>)`` in any of these files would
    double-correct post-flip rows — the exact failure this task exists for.
    """
    for name in (
        "15_ai_master_bot.py",
        os.path.join("tools", "research_dataset_common.py"),
        os.path.join("tools", "aim2_build_dataset.py"),
        os.path.join("tools", "fif1_build_dataset.py"),
        os.path.join("tools", "retrain_sra2.py"),
    ):
        src = _src(name)
        # candles_window_start is NOT a compensation: it turns a calendar date
        # into a deliberately conservative warmup floor days before the events,
        # and the Bucharest reading is the EARLIER of the two, so it stays valid
        # under either regime. Everything else must be gone.
        body = src.split("def candles_window_start")[0] + src.split("return (ts - pd.Timedelta")[-1]
        assert "tz_localize(LOCAL_TZ" not in body, name
        assert 'tz_localize("Europe/Bucharest"' not in body, name
        assert "tz_convert(LOCAL_TZ)" not in body, name


def test_pex1_builder_localizes_only_on_a_measured_offset():
    # The one sanctioned exception: the offset is READ OFF THE DATA
    # (detect_offset_h), so it cannot double-correct — it measures 0 post-flip.
    src = _src(os.path.join("tools", "pex1_build_dataset.py"))
    assert "assume_legacy=True" in src
    assert "if offset_h in (2, 3):" in src
    assert "tz_localize(LOCAL_TZ" not in src


# ── the single history knob ───────────────────────────────────────────────

NAIVE_LOCAL_SUMMER = datetime.datetime(2026, 7, 1, 12, 0, 0)  # Bucharest = UTC+3
NAIVE_LOCAL_WINTER = datetime.datetime(2026, 1, 15, 12, 0, 0)  # Bucharest = UTC+2


def test_default_is_uniform_utc_identity():
    assert r3_cutover() is None
    assert r3_history_mode() == "uniform-utc"
    assert legacy_naive_to_utc(NAIVE_LOCAL_SUMMER) == NAIVE_LOCAL_SUMMER
    assert utc_to_legacy_naive(NAIVE_LOCAL_SUMMER) == NAIVE_LOCAL_SUMMER
    s = pd.Series([NAIVE_LOCAL_SUMMER, NAIVE_LOCAL_WINTER])
    assert list(legacy_naive_to_utc(s)) == list(pd.to_datetime(s))


def test_cutover_splits_history_dst_aware():
    os.environ[R3_CUTOVER_ENV] = "2026-08-01T20:00:00"
    assert r3_history_mode() == "cutover@2026-08-01T20:00:00"

    # before the cutover -> legacy wall clock, and the offset is NOT a constant:
    # +3h in summer (EEST), +2h in winter (EET). A fixed shift is the bug this
    # recipe exists to avoid.
    assert legacy_naive_to_utc(NAIVE_LOCAL_SUMMER) == datetime.datetime(2026, 7, 1, 9, 0)
    assert legacy_naive_to_utc(NAIVE_LOCAL_WINTER) == datetime.datetime(2026, 1, 15, 10, 0)

    # after the cutover -> already UTC, untouched
    after = datetime.datetime(2026, 8, 2, 12, 0)
    assert legacy_naive_to_utc(after) == after

    # Series takes the same split in one pass
    out = list(legacy_naive_to_utc(pd.Series([NAIVE_LOCAL_SUMMER, after])))
    assert out == [pd.Timestamp(2026, 7, 1, 9, 0), pd.Timestamp(after)]


def test_cutover_bound_conversion_is_the_inverse():
    os.environ[R3_CUTOVER_ENV] = "2026-08-01T20:00:00"
    bound = datetime.datetime(2026, 7, 1, 9, 0)  # UTC
    assert utc_to_legacy_naive(bound) == NAIVE_LOCAL_SUMMER
    assert legacy_naive_to_utc(utc_to_legacy_naive(bound)) == bound
    # A bound past the cutover stays UTC — that is the whole point post-flip.
    late = datetime.datetime(2026, 8, 2, 9, 0)
    assert utc_to_legacy_naive(late) == late


def test_aware_input_is_never_localized_twice():
    """A timestamptz column (pump_dump_events, closed_ai_signals.close_time)
    hands back a true instant — it must pass through as UTC in BOTH modes."""
    aware = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.timezone.utc)
    for mode in ("", "2026-08-01T20:00:00"):
        if mode:
            os.environ[R3_CUTOVER_ENV] = mode
        else:
            os.environ.pop(R3_CUTOVER_ENV, None)
        assert legacy_naive_to_utc(aware) == datetime.datetime(2026, 7, 1, 9, 0)
        s = pd.Series(pd.to_datetime([aware, aware]))
        assert list(legacy_naive_to_utc(s)) == [pd.Timestamp(2026, 7, 1, 9, 0)] * 2


def test_ambiguous_autumn_hour_becomes_nat_not_a_silent_guess():
    # 2025-10-26 03:00-04:00 local happens twice. Live count 2026-08-01:
    # 113 row-values, all in closed_trades_master. They are unmappable, so a
    # Series drops them (NaT) instead of inventing an instant.
    os.environ[R3_CUTOVER_ENV] = "2026-08-01T20:00:00"
    s = pd.Series([datetime.datetime(2025, 10, 26, 3, 30)])
    assert pd.isna(legacy_naive_to_utc(s).iloc[0])


def test_assume_legacy_ignores_the_cutover():
    # measured-domain path: localizes regardless of the knob
    assert legacy_naive_to_utc(NAIVE_LOCAL_SUMMER, assume_legacy=True) == datetime.datetime(2026, 7, 1, 9, 0)
    s = pd.Series([NAIVE_LOCAL_SUMMER])
    assert legacy_naive_to_utc(s, assume_legacy=True).iloc[0] == pd.Timestamp(2026, 7, 1, 9, 0)


def test_malformed_cutover_is_loud():
    os.environ[R3_CUTOVER_ENV] = "yesterday"
    with pytest.raises(ValueError):
        r3_cutover()


def test_legacy_writer_tz_is_the_measured_vps_zone():
    assert LEGACY_WRITER_TZ == "Europe/Bucharest"

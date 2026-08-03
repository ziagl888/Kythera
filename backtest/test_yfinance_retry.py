# backtest/test_yfinance_retry.py
"""Standalone (DB-free, network-free) guard for T-2026-KYT-9050-084/-088.

`core.yfinance_fetch.download_with_retry` wraps `yf.download` for bots 16/17.
The bug it addresses is a SILENT one: yfinance catches its own errors, logs
"1 Failed download: ['EURUSD=X']: TypeError(...)" onto the calling bot's logger
and returns an EMPTY frame, so a failed pull is indistinguishable from a healthy
cycle and costs that (ticker, timeframe) the whole scan. Measured on the live
VPS 2026-08-03: ~5% of bot 16's 77 pulls per cycle.

The contract asserted here:
  * a transient failure is retried and the recovered frame is returned,
  * an exception is treated exactly like an empty frame (yfinance swallows its
    own, so both mean "no data"),
  * exhausting the attempts returns an EMPTY frame, never None and never a
    raise — the caller's skip path must stay byte-identical to before,
  * the exhaustion is logged at WARNING naming ticker AND timeframe,
  * the attempt count is BOUNDED (no unbounded hammering of Yahoo),
  * the backoff grows, is jittered, and is never slept after the final attempt,
  * the CIRCUIT BREAKER opens after CIRCUIT_TRIP_AFTER consecutive total
    failures and collapses the retry to a single attempt, so a broad outage is
    not amplified; a success or a cycle reset closes it again.

The download, sleep and jitter are injected, so the test neither touches the
network nor spends wall-clock time, and the backoff assertions stay
deterministic.

yfinance itself is stubbed if absent: `core/yfinance_fetch.py` imports it at
module level ON PURPOSE (a missing install must break a bot at START, not
mid-cycle), but this test never calls it — the whole point of `_download` is
that the retry logic is testable without the dependency. The repo runs two
python environments (fleet 3.13.12 has yfinance, the dev interpreter may not),
and a guard that only runs in one of them is the failure mode this very task is
about.

Run: py -3.13 backtest/test_yfinance_retry.py   (or: pytest -q)
"""

from __future__ import annotations

import logging
import os
import sys
import types

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "yfinance" not in sys.modules:
    try:
        import yfinance  # noqa: F401
    except ImportError:
        _stub = types.ModuleType("yfinance")
        _stub.download = lambda *a, **kw: (_ for _ in ()).throw(  # type: ignore[attr-defined]
            AssertionError("the real yf.download must never be reached from this test")
        )
        sys.modules["yfinance"] = _stub

from core.yfinance_fetch import (  # noqa: E402
    CIRCUIT_TRIP_AFTER,
    YF_JITTER_RANGE,
    YF_MAX_ATTEMPTS,
    circuit_is_open,
    download_with_retry,
    reset_retry_budget,
)

# Deterministic stand-in for random.uniform: the midpoint of the jitter band, so
# backoff assertions stay exact while the production path keeps its randomness.
NO_JITTER = 1.0


def _no_jitter(low: float, high: float) -> float:
    return NO_JITTER


@pytest.fixture(autouse=True)
def _clean_breaker():
    """The breaker is module-global state — leaking it between tests would make
    them order-dependent (the classic way a green suite hides a real failure)."""
    reset_retry_budget()
    yield
    reset_retry_budget()


def _frame(rows: int = 3) -> pd.DataFrame:
    return pd.DataFrame({"Close": range(rows)})


class _Recorder:
    """Stands in for yf.download; returns a scripted sequence of outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, ticker, interval=None, period=None, progress=None):
        self.calls.append({"ticker": ticker, "interval": interval, "period": period})
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Clock:
    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)


def _call(ticker="EURUSD=X", interval="1h", period="60d", **kw):
    """download_with_retry with the deterministic jitter injected by default."""
    kw.setdefault("_rand", _no_jitter)
    return download_with_retry(ticker, interval, period, **kw)


# ---------------------------------------------------------------------------
# Retry basics
# ---------------------------------------------------------------------------
def test_first_attempt_success_does_not_retry_or_sleep():
    rec, clock = _Recorder([_frame()]), _Clock()
    out = _call(tf="1h", _download=rec, _sleep=clock)
    assert not out.empty, "a successful download must be returned unchanged"
    assert len(rec.calls) == 1, f"expected exactly one call, got {len(rec.calls)}"
    assert clock.slept == [], "no backoff may be slept when the first attempt succeeds"


def test_transient_empty_frame_is_retried_and_recovers():
    """The exact live failure mode: yfinance returns empty instead of raising."""
    rec, clock = _Recorder([pd.DataFrame(), _frame()]), _Clock()
    out = _call("GC=F", tf="4h", _download=rec, _sleep=clock)
    assert not out.empty, "the recovered frame must be returned"
    assert len(rec.calls) == 2, "the empty first attempt must be retried"
    assert len(clock.slept) == 1, "exactly one backoff between the two attempts"


def test_raised_exception_is_treated_like_an_empty_frame():
    rec, clock = _Recorder([TypeError("'NoneType' object is not subscriptable"), _frame()]), _Clock()
    out = _call("SI=F", "15m", "30d", tf="15m", _download=rec, _sleep=clock)
    assert not out.empty, "a raise on the first attempt must not abort the retry"
    assert len(rec.calls) == 2


def test_exhausted_attempts_return_an_empty_frame_not_none():
    """The caller does `if df.empty: return df` — the skip path must not change."""
    rec, clock = _Recorder([pd.DataFrame()]), _Clock()
    out = _call("JPY=X", tf="1h", _download=rec, _sleep=clock)
    assert out is not None, "None would crash the caller's `df.empty` check"
    assert isinstance(out, pd.DataFrame) and out.empty
    assert len(rec.calls) == YF_MAX_ATTEMPTS, (
        f"expected {YF_MAX_ATTEMPTS} attempts, got {len(rec.calls)}"
    )


def test_none_return_is_treated_like_an_empty_frame():
    """yfinance has returned None on some failure paths — must not crash."""
    rec, clock = _Recorder([None, _frame()]), _Clock()
    out = _call("EURGBP=X", tf="1h", _download=rec, _sleep=clock)
    assert not out.empty
    assert len(rec.calls) == 2


def test_download_arguments_are_passed_through_unchanged():
    """The interval/period pairs are the bots' contract with Yahoo."""
    rec, clock = _Recorder([_frame()]), _Clock()
    _call("AUDUSD=X", "30m", "30d", _download=rec, _sleep=clock)
    assert rec.calls[0] == {"ticker": "AUDUSD=X", "interval": "30m", "period": "30d"}


# ---------------------------------------------------------------------------
# Bounds, clamp and backoff shape
# ---------------------------------------------------------------------------
def test_attempts_are_bounded():
    """Guard against an unbounded retry loop hammering Yahoo."""
    rec, clock = _Recorder([pd.DataFrame()]), _Clock()
    _call("GBPJPY=X", attempts=5, _download=rec, _sleep=clock)
    assert len(rec.calls) == 5, "the attempts argument must cap the loop"
    assert len(clock.slept) == 4, "no backoff after the final attempt"
    assert clock.slept == sorted(clock.slept), "backoff must not shrink between attempts"
    assert clock.slept[0] < clock.slept[-1], "backoff must actually grow"


@pytest.mark.parametrize("bad", [0, -3])
def test_non_positive_attempts_clamp_consistently(bad, caplog):
    """The clamp must cover the loop, the backoff guard AND the log line.

    T-2026-KYT-9050-088: `max(1, attempts)` applied only to the loop bound left
    the message reading "no data after -3 attempts". Unreachable from the bots
    (neither passes `attempts`), but it is the one line whose job is to state the
    bound truthfully.
    """
    rec, clock = _Recorder([pd.DataFrame()]), _Clock()
    log = logging.getLogger(f"test_yf_clamp_{bad}")
    with caplog.at_level(logging.WARNING, logger=log.name):
        _call("USDCAD=X", attempts=bad, logger=log, _download=rec, _sleep=clock)
    assert len(rec.calls) == 1, "a non-positive budget must still make exactly one attempt"
    assert clock.slept == [], "no backoff with a single attempt"
    msg = caplog.records[-1].getMessage()
    assert "1 attempt" in msg, f"log must report the clamped budget, got: {msg}"
    assert str(bad) not in msg.split("(")[0], f"raw un-clamped value leaked into the log: {msg}"


def test_backoff_is_jittered_within_the_configured_band():
    """A fixed sleep marches every retry of a broad outage into the same window."""
    seen = []
    rec = _Recorder([pd.DataFrame()])
    clock = _Clock()
    _call("NZDUSD=X", attempts=2, backoff_s=2.0, _download=rec, _sleep=clock,
          _rand=lambda lo, hi: seen.append((lo, hi)) or hi)
    assert seen == [YF_JITTER_RANGE], f"jitter band not applied as configured: {seen}"
    assert clock.slept == [2.0 * 1 * YF_JITTER_RANGE[1]], (
        f"jitter factor not multiplied into the backoff: {clock.slept}"
    )


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
def _exhaust(times, rec=None, clock=None, **kw):
    rec = rec or _Recorder([pd.DataFrame()])
    clock = clock or _Clock()
    for _ in range(times):
        _call("EURUSD=X", tf="1h", _download=rec, _sleep=clock, **kw)
    return rec, clock


def test_breaker_stays_closed_below_the_threshold():
    rec, clock = _exhaust(CIRCUIT_TRIP_AFTER - 1)
    assert not circuit_is_open(), "breaker opened too early"
    assert len(rec.calls) == (CIRCUIT_TRIP_AFTER - 1) * YF_MAX_ATTEMPTS, (
        "full retries must still run below the threshold"
    )


def test_breaker_opens_after_consecutive_total_failures_and_collapses_the_retry():
    """The load-bearing property: a broad outage must not be amplified."""
    rec, clock = _exhaust(CIRCUIT_TRIP_AFTER)
    assert circuit_is_open(), f"breaker must open after {CIRCUIT_TRIP_AFTER} total failures"
    calls_before, sleeps_before = len(rec.calls), len(clock.slept)

    _exhaust(1, rec, clock)
    assert len(rec.calls) - calls_before == 1, (
        "with the breaker open a pull must cost exactly ONE request, not the full retry"
    )
    assert len(clock.slept) == sleeps_before, "no backoff may be slept while the breaker is open"


def test_a_single_success_closes_the_breaker():
    _exhaust(CIRCUIT_TRIP_AFTER)
    assert circuit_is_open()
    rec, clock = _Recorder([_frame()]), _Clock()
    out = _call(tf="1h", _download=rec, _sleep=clock)
    assert not out.empty
    assert not circuit_is_open(), "a demonstrably answering endpoint must re-enable retries"


def test_reset_retry_budget_closes_the_breaker_for_the_next_cycle():
    """Each bot calls this at the top of its scan; without it a bad cycle would
    keep the breaker open into the next one and hide a recovery."""
    _exhaust(CIRCUIT_TRIP_AFTER)
    assert circuit_is_open()
    reset_retry_budget()
    assert not circuit_is_open()
    rec, clock = _exhaust(1)
    assert len(rec.calls) == YF_MAX_ATTEMPTS, "after a reset the full retry budget is back"


def test_an_intervening_success_prevents_the_breaker_from_opening():
    """CONSECUTIVE is the point — scattered transient failures must not trip it.

    The breaker is checked after EVERY failure, not just at the end: a success
    closes an already-open breaker, so an end-only assertion passes even when the
    failure count is never cleared and the breaker flaps open and shut on every
    other pull. That exact hole was found by mutating the success-path reset away
    and watching the whole suite stay green (T-2026-KYT-9050-088).
    """
    for i in range(CIRCUIT_TRIP_AFTER * 2):
        _call(tf="1h", _download=_Recorder([pd.DataFrame()]), _sleep=_Clock())
        assert not circuit_is_open(), (
            f"breaker tripped after {i + 1} SCATTERED failures — a success must clear the "
            "run, otherwise the observed ~5% transient regime opens it constantly"
        )
        _call(tf="1h", _download=_Recorder([_frame()]), _sleep=_Clock())
    assert not circuit_is_open()


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
def test_final_failure_is_logged_with_ticker_and_timeframe(caplog):
    """The whole point: a silent skip must stop looking like a healthy cycle."""
    rec, clock = _Recorder([pd.DataFrame()]), _Clock()
    log = logging.getLogger("test_yf_retry")
    with caplog.at_level(logging.WARNING, logger="test_yf_retry"):
        _call("USDCHF=X", tf="4h", logger=log, _download=rec, _sleep=clock)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "exhausting the attempts must emit a WARNING, not stay silent"
    msg = warnings[-1].getMessage()
    assert "USDCHF=X" in msg, f"ticker missing from the warning: {msg}"
    assert "4h" in msg, f"timeframe missing from the warning — yfinance's own line has none: {msg}"


def test_breaker_opening_is_announced(caplog):
    """An operator must be able to tell 'Yahoo is down' from 'one bad pull'."""
    log = logging.getLogger("test_yf_breaker")
    with caplog.at_level(logging.WARNING, logger="test_yf_breaker"):
        _exhaust(CIRCUIT_TRIP_AFTER, logger=log)
    joined = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "retries suspended" in joined, (
        f"opening the breaker must be announced, not silent: {joined[-300:]}"
    )


if __name__ == "__main__":
    # caplog / parametrize are pytest features; run those under pytest only.
    for name, fn in sorted(globals().items()):
        if (
            name.startswith("test_")
            and callable(fn)
            and "caplog" not in fn.__code__.co_varnames
            and not hasattr(fn, "pytestmark")
        ):
            reset_retry_budget()
            fn()
            print(f"  ok  {name}")
    reset_retry_budget()
    print("OK — yfinance retry + circuit-breaker contract holds (run pytest for the caplog tests)")

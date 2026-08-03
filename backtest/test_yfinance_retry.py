# backtest/test_yfinance_retry.py
"""Standalone (DB-free, network-free) guard for T-2026-KYT-9050-084.

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
  * the backoff grows and is never slept after the final attempt.

The download and sleep are injected, so the test neither touches the network nor
spends wall-clock time.

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

from core.yfinance_fetch import YF_MAX_ATTEMPTS, download_with_retry  # noqa: E402


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


def test_first_attempt_success_does_not_retry_or_sleep():
    rec, clock = _Recorder([_frame()]), _Clock()
    out = download_with_retry("EURUSD=X", "1h", "60d", tf="1h", _download=rec, _sleep=clock)
    assert not out.empty, "a successful download must be returned unchanged"
    assert len(rec.calls) == 1, f"expected exactly one call, got {len(rec.calls)}"
    assert clock.slept == [], "no backoff may be slept when the first attempt succeeds"


def test_transient_empty_frame_is_retried_and_recovers():
    """The exact live failure mode: yfinance returns empty instead of raising."""
    rec, clock = _Recorder([pd.DataFrame(), _frame()]), _Clock()
    out = download_with_retry("GC=F", "1h", "60d", tf="4h", _download=rec, _sleep=clock)
    assert not out.empty, "the recovered frame must be returned"
    assert len(rec.calls) == 2, "the empty first attempt must be retried"
    assert len(clock.slept) == 1, "exactly one backoff between the two attempts"


def test_raised_exception_is_treated_like_an_empty_frame():
    rec, clock = _Recorder([TypeError("'NoneType' object is not subscriptable"), _frame()]), _Clock()
    out = download_with_retry("SI=F", "15m", "30d", tf="15m", _download=rec, _sleep=clock)
    assert not out.empty, "a raise on the first attempt must not abort the retry"
    assert len(rec.calls) == 2


def test_exhausted_attempts_return_an_empty_frame_not_none():
    """The caller does `if df.empty: return df` — the skip path must not change."""
    rec, clock = _Recorder([pd.DataFrame()]), _Clock()
    out = download_with_retry("JPY=X", "1h", "60d", tf="1h", _download=rec, _sleep=clock)
    assert out is not None, "None would crash the caller's `df.empty` check"
    assert isinstance(out, pd.DataFrame) and out.empty
    assert len(rec.calls) == YF_MAX_ATTEMPTS, (
        f"expected {YF_MAX_ATTEMPTS} attempts, got {len(rec.calls)}"
    )


def test_none_return_is_treated_like_an_empty_frame():
    """yfinance has returned None on some failure paths — must not crash."""
    rec, clock = _Recorder([None, _frame()]), _Clock()
    out = download_with_retry("EURGBP=X", "1h", "60d", tf="1h", _download=rec, _sleep=clock)
    assert not out.empty
    assert len(rec.calls) == 2


def test_attempts_are_bounded():
    """Guard against an unbounded retry loop hammering Yahoo."""
    rec, clock = _Recorder([pd.DataFrame()]), _Clock()
    download_with_retry("GBPJPY=X", "1h", "60d", attempts=5, _download=rec, _sleep=clock)
    assert len(rec.calls) == 5, "the attempts argument must cap the loop"
    assert len(clock.slept) == 4, "no backoff after the final attempt"
    assert clock.slept == sorted(clock.slept), "backoff must not shrink between attempts"
    assert clock.slept[0] < clock.slept[-1], "backoff must actually grow"


def test_final_failure_is_logged_with_ticker_and_timeframe(caplog):
    """The whole point: a silent skip must stop looking like a healthy cycle."""
    rec, clock = _Recorder([pd.DataFrame()]), _Clock()
    log = logging.getLogger("test_yf_retry")
    with caplog.at_level(logging.WARNING, logger="test_yf_retry"):
        download_with_retry("USDCHF=X", "1h", "60d", tf="4h", logger=log, _download=rec, _sleep=clock)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "exhausting the attempts must emit a WARNING, not stay silent"
    msg = warnings[-1].getMessage()
    assert "USDCHF=X" in msg, f"ticker missing from the warning: {msg}"
    assert "4h" in msg, f"timeframe missing from the warning — yfinance's own line has none: {msg}"


def test_download_arguments_are_passed_through_unchanged():
    """The interval/period pairs are the bots' contract with Yahoo."""
    rec, clock = _Recorder([_frame()]), _Clock()
    download_with_retry("AUDUSD=X", "30m", "30d", _download=rec, _sleep=clock)
    assert rec.calls[0] == {"ticker": "AUDUSD=X", "interval": "30m", "period": "30d"}


if __name__ == "__main__":
    # caplog is a pytest fixture; run that one under pytest only.
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn) and "caplog" not in fn.__code__.co_varnames:
            fn()
            print(f"  ok  {name}")
    print("OK — yfinance retry contract holds (run pytest for the caplog test)")

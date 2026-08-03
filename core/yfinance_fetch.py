# core/yfinance_fetch.py — bounded retry around yf.download (T-2026-KYT-9050-084).
#
# WHY THIS EXISTS: 16_smc_forex_metals_bot.py and 17_mayank_bot.py pull their
# forex/metal candles from Yahoo. Measured on the live VPS on 2026-08-03, ~5% of
# those pulls fail transiently: yfinance catches the error INTERNALLY, logs
#
#   1 Failed download:
#   ['EURUSD=X']: TypeError("'NoneType' object is not subscriptable")
#
# onto the calling bot's logger and returns an EMPTY frame. The bot does not
# crash — it silently skips that (ticker, timeframe) for the whole cycle. Bot 16
# fires ~77 requests per scan (11 tickers x 7 timeframes) and lost 17 of them
# across 4 cycles; an older log window shows 246. Every one of those combinations
# returns data when requested on its own, so the failure is transient and
# rate-limit-shaped, not a broken ticker or a dead interval.
#
# Two silent failure modes are addressed here:
#   1. the pull is not retried at all — one unlucky moment costs a full cycle;
#   2. the skip is indistinguishable from a healthy cycle in the logs, because
#      the only trace is yfinance's own line, which names no timeframe and no
#      caller.
#
# THE CIRCUIT BREAKER (T-2026-KYT-9050-088, from both core reviews of PR #259):
# a plain retry has an unpleasant feedback property — it multiplies load exactly
# when the cause is overload. Bot 16 walks 77 combinations per cycle on a
# 15-minute trigger; under a BROAD Yahoo outage a 3-attempt retry would add
# ~346s of pure sleep and 154 extra requests per cycle against an endpoint that
# is already refusing. So consecutive total failures are counted, and after
# CIRCUIT_TRIP_AFTER of them the retry switches itself off for the rest of the
# cycle: one attempt per pull, no backoff, no amplification. A single success
# closes it again, and each bot calls reset_retry_budget() at the start of a
# cycle so a fresh cycle always gets a fair chance.
#
# DELIBERATELY NOT IN core/market_utils.py: that module is imported by most of
# the fleet, and a module-level `import yfinance` there would make a missing or
# broken yfinance install take down bots that have nothing to do with Yahoo.
# Only bots 16/17 import this module.
#
# Invariants:
#   * returns a DataFrame, never None and never a raise — the callers' skip path
#     (`if df.empty: return df`) must stay exactly as it was;
#   * the attempt count is bounded, and a tripped breaker only ever lowers it;
#   * the breaker state is process-local and single-threaded (each bot is its own
#     process and pulls sequentially) — it is NOT safe to share across threads.

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

import pandas as pd
import yfinance as yf

_logger = logging.getLogger(__name__)

# Three attempts total (two retries). Backoff is short on purpose: bot 16 walks
# 77 combinations per cycle, so a long sleep per failure would stretch the scan
# far more than the missing data costs. Worst case per failed pull: ~1.5s + ~3.0s
# before jitter, ~6.75s at the top of the jitter band.
YF_MAX_ATTEMPTS = 3
YF_RETRY_BACKOFF_S = 1.5
# Jitter band on each backoff. Not about a thundering herd — the two bots are a
# single sequential process each and run on offset schedules — but a fixed sleep
# marches every retry of a broad outage in lockstep into the same recovery
# window. The band keeps the same expected wait.
YF_JITTER_RANGE = (0.5, 1.5)
# Consecutive total failures before the breaker opens. Five is above the observed
# transient rate (~5% of 77 pulls, essentially never five in a row by chance) and
# far below a full cycle, so it distinguishes "unlucky" from "Yahoo is down".
CIRCUIT_TRIP_AFTER = 5

# Process-local breaker state. See the threading note in the Invariants above.
_consecutive_exhaustions = 0
_circuit_open = False


def reset_retry_budget(logger: logging.Logger | None = None) -> None:
    """Close the breaker and clear the failure count — call once per scan cycle.

    Without this a breaker opened by one bad cycle would stay open into the next
    one for as long as every pull keeps failing, which would hide a recovery. The
    bots call it at the top of their scan.
    """
    global _consecutive_exhaustions, _circuit_open
    if _circuit_open:
        (logger or _logger).info("YFinance retry budget reset — retries re-enabled for this cycle.")
    _consecutive_exhaustions = 0
    _circuit_open = False


def circuit_is_open() -> bool:
    """Whether the breaker currently suppresses retries (for tests and callers)."""
    return _circuit_open


def download_with_retry(
    ticker: str,
    interval: str,
    period: str,
    *,
    tf: str | None = None,
    attempts: int = YF_MAX_ATTEMPTS,
    backoff_s: float = YF_RETRY_BACKOFF_S,
    logger: logging.Logger | None = None,
    _download: Callable[..., Any] | None = None,
    _sleep: Callable[[float], Any] = time.sleep,
    _rand: Callable[[float, float], float] = random.uniform,
) -> pd.DataFrame:
    """``yf.download`` with a bounded, breaker-guarded retry. Never returns None.

    An empty result is treated as a failed attempt: the bots always request at
    least 30 days of history, so a genuinely empty frame is not a quiet weekend
    but a failed pull. yfinance swallows its own exceptions and returns an empty
    frame, so "empty" and "raised" are the same signal here and both are retried.

    Exhausting the attempts is NOT an error for the caller — an empty frame is
    returned and the bot skips the symbol exactly as it did before. What changes
    is that the skip is logged explicitly, with the timeframe the caller was
    working on (``tf``), which yfinance's own message does not carry.

    While the breaker is open only a single attempt is made and no backoff is
    slept, so a broad outage costs one request per pull instead of three.

    ``_download`` / ``_sleep`` / ``_rand`` are injection points for the DB-free
    tests.
    """
    global _consecutive_exhaustions, _circuit_open

    log = logger or _logger
    download = _download or yf.download
    label = f"{ticker} ({tf})" if tf else ticker
    # One clamp, used by the loop, the backoff guard AND both log lines — a
    # clamp that covers only the loop bound reports "no data after -3 attempts"
    # (T-2026-KYT-9050-088).
    budget = 1 if _circuit_open else max(1, attempts)
    last_error: Exception | None = None

    for attempt in range(1, budget + 1):
        try:
            df = download(ticker, interval=interval, period=period, progress=False)
        except Exception as e:  # yfinance normally swallows these itself
            last_error = e
            df = None

        if df is not None and not df.empty:
            if attempt > 1:
                log.info(f"YFinance {label}: recovered on attempt {attempt}/{budget}.")
            # A success is the only thing that clears the failure run: the
            # endpoint is demonstrably answering again.
            _consecutive_exhaustions = 0
            if _circuit_open:
                _circuit_open = False
                log.info(f"YFinance {label}: endpoint responding again — retries re-enabled.")
            return df

        if attempt < budget:
            # Linear-ish backoff (1.5s, 3.0s) with jitter — enough to clear a
            # rate-limit burst without stretching a 77-request scan cycle.
            _sleep(backoff_s * attempt * _rand(*YF_JITTER_RANGE))

    _consecutive_exhaustions += 1
    reason = f"{type(last_error).__name__}: {last_error}" if last_error else "empty frame"
    log.warning(
        f"YFinance {label}: no data after {budget} attempt(s) ({reason}) — "
        f"interval={interval}, period={period}. Timeframe skipped this cycle."
    )
    if not _circuit_open and _consecutive_exhaustions >= CIRCUIT_TRIP_AFTER:
        _circuit_open = True
        log.warning(
            f"YFinance: {_consecutive_exhaustions} consecutive total failures — retries "
            f"suspended for the rest of this cycle (one attempt per pull, no backoff). "
            f"Retrying into a broad outage multiplies load on an endpoint that is already refusing."
        )
    return pd.DataFrame()

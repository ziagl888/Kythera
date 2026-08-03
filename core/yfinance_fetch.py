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
# DELIBERATELY NOT IN core/market_utils.py: that module is imported by most of
# the fleet, and a module-level `import yfinance` there would make a missing or
# broken yfinance install take down bots that have nothing to do with Yahoo.
# Only bots 16/17 import this module.

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import pandas as pd
import yfinance as yf

_logger = logging.getLogger(__name__)

# Three attempts total (two retries). Backoff is short on purpose: bot 16 walks
# 77 combinations per cycle, so a long sleep per failure would stretch the scan
# far more than the missing data costs. Worst case per failed pull: 1.5s + 3.0s.
YF_MAX_ATTEMPTS = 3
YF_RETRY_BACKOFF_S = 1.5


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
) -> pd.DataFrame:
    """``yf.download`` with a bounded retry, returning a DataFrame (never None).

    An empty result is treated as a failed attempt: the bots always request at
    least 30 days of history, so a genuinely empty frame is not a quiet weekend
    but a failed pull. yfinance swallows its own exceptions and returns an empty
    frame, so "empty" and "raised" are the same signal here and both are retried.

    Exhausting the attempts is NOT an error for the caller — an empty frame is
    returned and the bot skips the symbol exactly as it did before. What changes
    is that the skip is logged explicitly, with the timeframe the caller was
    working on (``tf``), which yfinance's own message does not carry.

    ``_download`` / ``_sleep`` are injection points for the DB-free tests.
    """
    log = logger or _logger
    download = _download or yf.download
    label = f"{ticker} ({tf})" if tf else ticker
    last_error: Exception | None = None

    for attempt in range(1, max(1, attempts) + 1):
        try:
            df = download(ticker, interval=interval, period=period, progress=False)
        except Exception as e:  # yfinance normally swallows these itself
            last_error = e
            df = None

        if df is not None and not df.empty:
            if attempt > 1:
                log.info(f"YFinance {label}: recovered on attempt {attempt}/{attempts}.")
            return df

        if attempt < attempts:
            # Linear-ish backoff (1.5s, 3.0s) — enough to clear a rate-limit
            # burst without stretching a 77-request scan cycle.
            _sleep(backoff_s * attempt)

    reason = f"{type(last_error).__name__}: {last_error}" if last_error else "empty frame"
    log.warning(
        f"YFinance {label}: no data after {attempts} attempts ({reason}) — "
        f"interval={interval}, period={period}. Timeframe skipped this cycle."
    )
    return pd.DataFrame()

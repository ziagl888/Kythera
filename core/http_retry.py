# core/http_retry.py — budgeted retry/backoff policy for Binance REST.
#
# Addresses P2.14 (fetch_ohlcv_batch can loop forever; 418 handling hammers the
# ban) and P2.18 (housekeeping REST without 429/418 handling). Pattern after
# HKUDS/Vibe-Trading loaders/_http.py (HostThrottle) + loaders/base.py
# (retry_with_budget) — template, not drop-in (T-2026-CU-9050-027 D2).
#
# Semantics:
#   * 429 Too Many Requests: respect Retry-After (fallback exponential).
#   * 418 IP-ban (Binance escalates ignored 429 to 418): backoff NEVER below
#     BAN_MIN_BACKOFF_S (120s), exponential per additional 418; a Retry-After
#     header may only INCREASE wait time, never decrease. Further hammering
#     extends the ban — precisely the P2.14 failure mode.
#   * Network/other errors: short exponential backoff with cap.
#   * Budget = max_attempts AND deadline_s (whichever exhausted first) — a
#     stuck symbol must not block the 12h catch-up anymore.
#
# Pure policy without I/O: callers (1_data_ingestion, 6_housekeeping) sleep
# themselves — thus testable standalone without DB/network (backtest/test_http_retry.py).

from __future__ import annotations

import random
import time
from collections.abc import Callable

BAN_MIN_BACKOFF_S = 120.0  # 418: nie schneller wiederkommen (Binance-Ban-Eskalation)
RATE_LIMIT_FALLBACK_S = 10.0  # 429 ohne Retry-After-Header
ERROR_BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 900.0
JITTER_MAX_S = 0.5


class RetryBudget:
    """Counts attempts + wall-clock deadline for ONE logical REST operation.

    ``attempt()`` returns False = budget exhausted, stop and continue with
    what is there. The CALLER decides what an "attempt" is —
    both patterns are intentional (not "aligned"):
      (a) count only FAILED attempts (1_data_ingestion.fetch_ohlcv_batch:
          success pages paginate free, else budget would cap long,
          error-free backfills);
      (b) count every attempt incl. the first (6_housekeeping gap-filler:
          ``while budget.attempt():`` — a single-range call without pagination).
    """

    def __init__(
        self,
        max_attempts: int = 8,
        deadline_s: float = 300.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = int(max_attempts)
        self.deadline_s = float(deadline_s)
        self._now = now
        self._t0 = now()
        self.attempts = 0

    def attempt(self) -> bool:
        if self.attempts >= self.max_attempts:
            return False
        if self._now() - self._t0 >= self.deadline_s:
            return False
        self.attempts += 1
        return True

    def exhausted_reason(self) -> str:
        if self.attempts >= self.max_attempts:
            return f"max_attempts={self.max_attempts} reached"
        return f"deadline={self.deadline_s:.0f}s exceeded"


def backoff_seconds(
    status_code: int | None,
    consecutive: int,
    retry_after: str | None = None,
    rng: Callable[[], float] = random.random,
) -> float:
    """Wait time before next attempt.

    ``status_code``: HTTP status (None = network/parse error).
    ``consecutive``: how many consecutive failures of this kind (>=1).
    ``retry_after``: value of Retry-After header if present (seconds).
    """
    consecutive = max(int(consecutive), 1)
    jitter = rng() * JITTER_MAX_S

    header_s: float | None = None
    if retry_after is not None:
        try:
            header_s = max(float(retry_after), 0.0)
        except (TypeError, ValueError):
            header_s = None

    if status_code == 418:
        # Ban: respect header, but never below BAN_MIN_BACKOFF_S; double
        # per additional 418 (ban lengthens with hammering).
        base = BAN_MIN_BACKOFF_S * (2.0 ** (consecutive - 1))
        if header_s is not None:
            base = max(base, header_s)
        return min(base, BACKOFF_CAP_S) + jitter

    if status_code == 429:
        if header_s is not None:
            return min(header_s, BACKOFF_CAP_S) + jitter
        return min(RATE_LIMIT_FALLBACK_S * (2.0 ** (consecutive - 1)), BACKOFF_CAP_S) + jitter

    # Network/other errors
    return min(ERROR_BACKOFF_BASE_S * (2.0 ** (consecutive - 1)), BACKOFF_CAP_S) + jitter


class MinIntervalThrottle:
    """Process-wide minimum interval per bucket (HostThrottle pattern, simplified).

    ``wait()`` blocks until at least ``min_interval`` seconds (+ jitter) have
    passed since the last call to the same bucket. For the gap-filler
    (P2.18): many symbols in sequence desynchronise rather than burst.
    Single-thread use (housekeeping jobs run sequentially); for
    multi-thread callers would need a lock around bookkeeping.
    """

    def __init__(
        self,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._last: dict[str, float] = {}
        self._now, self._sleep, self._rng = now, sleep, rng

    def wait(self, bucket: str, min_interval: float) -> None:
        if min_interval <= 0:
            return
        now = self._now()
        last = self._last.get(bucket)
        if last is None or now >= last + min_interval:
            fire_at = now
        else:
            fire_at = last + min_interval + self._rng() * JITTER_MAX_S
        self._last[bucket] = fire_at
        delay = fire_at - self._now()
        if delay > 0:
            self._sleep(delay)

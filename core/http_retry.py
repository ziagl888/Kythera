# core/http_retry.py — gebudgetete Retry-/Backoff-Politik für Binance-REST.
#
# Adressiert P2.14 (fetch_ohlcv_batch kann ewig loopen; 418-Handling hämmert in
# den Ban) und P2.18 (Housekeeping-REST ohne 429/418-Handling). Muster nach
# HKUDS/Vibe-Trading loaders/_http.py (HostThrottle) + loaders/base.py
# (retry_with_budget) — Vorlage, kein Drop-in (T-2026-CU-9050-027 D2).
#
# Semantik:
#   * 429 Too Many Requests: Retry-After respektieren (Fallback exponentiell).
#   * 418 IP-Ban (Binance eskaliert ignorierte 429 zu 418): Backoff NIE unter
#     BAN_MIN_BACKOFF_S (120s), exponentiell je weiterem 418; ein Retry-After-
#     Header darf die Wartezeit nur ERHÖHEN, nie senken. Weiter hämmern
#     verlängert den Ban — genau der P2.14-Fehlermodus.
#   * Netzwerk-/Sonstige Fehler: kurzer exponentieller Backoff mit Cap.
#   * Budget = max_attempts UND deadline_s (was zuerst erschöpft ist) — ein
#     stuck Symbol darf den 12h-Catch-up nicht mehr blockieren.
#
# Reine Politik ohne I/O: die Caller (1_data_ingestion, 6_housekeeping) schlafen
# selbst — dadurch DB-/netzfrei standalone testbar (backtest/test_http_retry.py).

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
    """Zählt Versuche + Wanduhr-Deadline für EINE logische REST-Operation.

    ``attempt()`` vor jedem (Wieder-)Versuch aufrufen; False = Budget erschöpft,
    aufhören und mit dem weiterarbeiten, was da ist.
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
            return f"max_attempts={self.max_attempts} erreicht"
        return f"deadline={self.deadline_s:.0f}s überschritten"


def backoff_seconds(
    status_code: int | None,
    consecutive: int,
    retry_after: str | None = None,
    rng: Callable[[], float] = random.random,
) -> float:
    """Wartezeit vor dem nächsten Versuch.

    ``status_code``: HTTP-Status (None = Netzwerk-/Parse-Fehler).
    ``consecutive``: wie viele Fehlversuche dieser Art direkt hintereinander (>=1).
    ``retry_after``: Wert des Retry-After-Headers, falls vorhanden (Sekunden).
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
        # Ban: Header respektieren, aber nie unter BAN_MIN_BACKOFF_S; je
        # weiterem 418 verdoppeln (der Ban wird bei Hämmern länger).
        base = BAN_MIN_BACKOFF_S * (2.0 ** (consecutive - 1))
        if header_s is not None:
            base = max(base, header_s)
        return min(base, BACKOFF_CAP_S) + jitter

    if status_code == 429:
        if header_s is not None:
            return min(header_s, BACKOFF_CAP_S) + jitter
        return min(RATE_LIMIT_FALLBACK_S * (2.0 ** (consecutive - 1)), BACKOFF_CAP_S) + jitter

    # Netzwerk-/Sonstige Fehler
    return min(ERROR_BACKOFF_BASE_S * (2.0 ** (consecutive - 1)), BACKOFF_CAP_S) + jitter


class MinIntervalThrottle:
    """Prozessweiter Mindestabstand je Bucket (HostThrottle-Muster, vereinfacht).

    ``wait()`` blockt, bis seit dem letzten Call desselben Buckets mindestens
    ``min_interval`` Sekunden (+ Jitter) vergangen sind. Für den Gap-Filler
    (P2.18): viele Symbole hintereinander desynchronisieren statt bursten.
    Single-Thread-Nutzung (Housekeeping-Jobs laufen sequenziell); für
    Multi-Thread-Caller müsste ein Lock um die Buchhaltung.
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

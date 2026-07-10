# core/time.py — the single time source of the fleet (R3, T-2026-CU-9050-032)
"""Central UTC policy.

Kythera's target state is: every timestamp stored and compared in UTC. This
module is the Python half of that policy — the only sanctioned way to obtain
"now" or to convert an external timestamp. ``ruff``'s ``DTZ`` rules keep new
code from reaching around it: a bare ``datetime.now()`` / ``utcnow()`` /
``fromtimestamp(ts)`` fails the lint gate.

The DB half is NOT in place yet. The connection pool does not pin
``timezone=UTC``, so Postgres still casts between ``timestamptz`` and the
legacy naive ``timestamp`` columns using the VPS timezone
(``Europe/Bucharest``). Several writers and the dataset builders in ``tools/``
depend on that today. Flipping the session timezone is a fleet-restart change
that has to land together with those call sites — see ``docs/UTC_POLICY.md``,
which carries the inventory and the ordered rollout.

Naive vs aware: aware datetimes are the default. Several live tables are still
``TIMESTAMP WITHOUT TIME ZONE`` and read back naive — for those, and only for
those, use ``utc_now_naive()`` / ``as_naive_utc()``, which produce a naive
datetime whose wall clock *is* UTC.

Note on the module name: inside the ``core`` package ``import time`` still
resolves to the stdlib module (absolute imports); this one is ``core.time``.
"""

from __future__ import annotations

import datetime

UTC = datetime.timezone.utc

__all__ = ["UTC", "as_naive_utc", "from_unix_ts", "to_utc", "utc_now", "utc_now_naive"]


def utc_now() -> datetime.datetime:
    """Current time as a timezone-aware UTC datetime. The default."""
    return datetime.datetime.now(UTC)


def utc_now_naive() -> datetime.datetime:
    """Current UTC wall clock as a *naive* datetime.

    Only for writing to / comparing against the legacy ``TIMESTAMP WITHOUT TIME
    ZONE`` columns (``active_trades_master``, ``closed_trades_master``,
    ``regime_*``, …). Equivalent to the deprecated ``datetime.utcnow()``.
    """
    return datetime.datetime.now(UTC).replace(tzinfo=None)


def to_utc(dt: datetime.datetime) -> datetime.datetime:
    """Normalise to an aware UTC datetime. A naive input is assumed to be UTC.

    Careful: that assumption is the *target* storage contract. Columns written
    by a naive-local writer (``3_detectors.py``, P2.3) or by Postgres' ``NOW()``
    under the current non-UTC session do not satisfy it yet. Check
    ``docs/UTC_POLICY.md`` before pointing this at a legacy naive column.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def as_naive_utc(dt: datetime.datetime) -> datetime.datetime:
    """Strip the tzinfo after converting to UTC — for the naive legacy columns."""
    return to_utc(dt).replace(tzinfo=None)


def from_unix_ts(ts: float, *, ms: bool = False) -> datetime.datetime:
    """Aware UTC datetime from a Unix epoch (seconds, or milliseconds with ``ms=True``).

    Binance delivers epoch milliseconds; local caches (funding, whale trades)
    store epoch seconds.
    """
    return datetime.datetime.fromtimestamp(ts / 1000.0 if ms else ts, tz=UTC)

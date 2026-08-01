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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import cost off the bots
    import numpy as np

UTC = datetime.timezone.utc

# The session TZ under which Postgres' DB defaults (now()) and naive-local
# writers historically stamped the legacy TIMESTAMP columns. Pinned as a
# constant on purpose: rows written before a column's writer went UTC keep
# this wall clock forever, regardless of any later session/server TZ change
# (R3 flip). Readers that must interpret such legacy rows convert with
# ``AT TIME ZONE`` against THIS zone, never current_setting('TimeZone').
LEGACY_WRITER_TZ = "Europe/Bucharest"

__all__ = [
    "LEGACY_WRITER_TZ",
    "UTC",
    "as_naive_utc",
    "epoch_seconds",
    "from_unix_ts",
    "to_utc",
    "utc_now",
    "utc_now_naive",
]


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


def epoch_seconds(values: Any) -> np.ndarray:
    """Epoch SECONDS (float64) from a pandas datetime Series/Index/array.

    The idiom this replaces — ``series.astype("int64") / 1e9`` — is not a unit
    conversion, it is a bet on the datetime RESOLUTION of the column. ``astype``
    yields the integer count of the dtype's own unit, so the ``/ 1e9`` only lands
    on seconds while the dtype happens to be ``datetime64[ns]``:

        pandas 2.3.2 (fleet, Python 3.13)  → datetime64[ns] → /1e9 = seconds  ✔
        pandas 3.0.x  (Python 3.14)        → datetime64[us] → /1e9 = kiloseconds ✘

    The failure is silent and scale-shaped: under ``us`` the epoch axis shrinks
    1000x, so a regression slope over it comes out 1000x too LARGE while the
    fitted value at the window's end — and with it ``dist_to_trend`` — stays
    intact. In ``core/rub_features.rub_trend`` that is exactly one of the fifteen
    RUB2 model inputs (``slope_trend``) silently off by three orders of magnitude
    while its neighbour still matches: a train/serve skew that no feature-contract
    check can see, because the column is present and finite.

    Measured on this repo (T-2026-KYT-9050-008): the replay generated under the
    fleet interpreter reproduces ``slope_trend`` bit-exactly; the same code under
    pandas 3.0.3 returns exactly 1000x that value on all 229 probed events.

    Normalising to ``ns`` first makes the result resolution-independent, so the
    live bot (which goes through ``datetime.timestamp()`` and is therefore always
    in seconds) and the replay agree no matter which interpreter generated the
    replay.
    """
    import pandas as pd

    idx = pd.DatetimeIndex(values)
    if idx.tz is not None:
        idx = idx.tz_convert(UTC).tz_localize(None)
    return idx.to_numpy(dtype="datetime64[ns]").astype("int64") / 1e9

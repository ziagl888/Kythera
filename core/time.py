# core/time.py — the single time source of the fleet (R3, T-2026-CU-9050-032)
"""Central UTC policy.

Kythera's target state is: every timestamp stored and compared in UTC. This
module is the Python half of that policy — the only sanctioned way to obtain
"now" or to convert an external timestamp. ``ruff``'s ``DTZ`` rules keep new
code from reaching around it: a bare ``datetime.now()`` / ``utcnow()`` /
``fromtimestamp(ts)`` fails the lint gate.

The DB half landed with T-2026-KYT-9050-005: the connection pool pins
``timezone=UTC``, so Postgres casts between ``timestamptz`` and the legacy
naive ``timestamp`` columns in UTC, and every writer stamps UTC. That flip
takes effect for a process when it (re)starts — from that instant on, the
naive legacy columns carry TWO domains: rows written before it are
``Europe/Bucharest`` wall clock, rows after it are UTC. ``R3_CUTOVER_UTC``
below is the single knob that says which of the two worlds a reader is in;
see ``docs/UTC_POLICY.md`` §6 for the open operator decision.

Naive vs aware: aware datetimes are the default. Several live tables are still
``TIMESTAMP WITHOUT TIME ZONE`` and read back naive — for those, and only for
those, use ``utc_now_naive()`` / ``as_naive_utc()``, which produce a naive
datetime whose wall clock *is* UTC.

Note on the module name: inside the ``core`` package ``import time`` still
resolves to the stdlib module (absolute imports); this one is ``core.time``.
"""

from __future__ import annotations

import datetime
import os
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

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

# ── R3 history boundary (docs/UTC_POLICY.md §6) ───────────────────────────
# The pool flip + fleet restart splits every legacy naive column into two
# domains: LEGACY_WRITER_TZ wall clock before the restart, UTC after it.
# `R3_CUTOVER_UTC` is the ONE place that decides how a reader treats that:
#
#   None  — the column carries a single domain (UTC) over its whole history.
#           True after a history backfill, and the value this repo ships with.
#   set   — cutover mode: rows whose stored wall clock is before this instant
#           are read as LEGACY_WRITER_TZ, the rest as UTC.
#
# Overridable per process via KYTHERA_R3_CUTOVER_UTC (ISO-8601, UTC, e.g.
# "2026-08-01T20:00:00") so the decision can be made — and revised — without a
# code change. Both operator paths from UTC_POLICY §6 stay open through this
# constant; nothing else in the fleet localizes a legacy column any more.
R3_CUTOVER_ENV = "KYTHERA_R3_CUTOVER_UTC"
R3_CUTOVER_UTC: datetime.datetime | None = None

__all__ = [
    "LEGACY_WRITER_TZ",
    "R3_CUTOVER_ENV",
    "R3_CUTOVER_UTC",
    "UTC",
    "as_naive_utc",
    "epoch_seconds",
    "from_unix_ts",
    "legacy_naive_to_utc",
    "r3_cutover",
    "r3_history_mode",
    "to_utc",
    "utc_now",
    "utc_now_naive",
    "utc_to_legacy_naive",
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


def r3_cutover() -> datetime.datetime | None:
    """The configured R3 cutover as a NAIVE UTC datetime, or ``None``.

    Naive on purpose: it is compared against the stored wall clock of a naive
    legacy column, not against an aware instant.
    """
    raw = os.getenv(R3_CUTOVER_ENV, "").strip()
    if not raw:
        return R3_CUTOVER_UTC
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:  # loud: a typo'd cutover would silently skew history
        raise ValueError(f"{R3_CUTOVER_ENV}='{raw}' is not an ISO-8601 datetime") from e
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def r3_history_mode() -> str:
    """``"uniform-utc"`` or ``"cutover@<iso>"`` — for a one-line startup log."""
    cut = r3_cutover()
    return "uniform-utc" if cut is None else f"cutover@{cut.isoformat()}"


def legacy_naive_to_utc(values: Any, *, assume_legacy: bool = False) -> Any:
    """Read a legacy naive column (scalar or pandas Series) as naive UTC.

    ``assume_legacy=True`` skips the cutover question and localizes
    unconditionally. Exactly one caller has that right: a reader that has
    MEASURED the domain of its input (``tools/pex1_build_dataset.detect_offset_h``
    reads the offset off the data instead of assuming it). Everyone else must
    go through the cutover, so the fleet has one answer, not N.

    Without a cutover this is the identity — the post-flip contract is that a
    naive column IS UTC. With a cutover configured, values stamped before it
    are localized as ``LEGACY_WRITER_TZ`` and converted.

    The boundary is deliberately the stored wall clock against the cutover
    INSTANT, which misreads exactly one bounded set: rows written locally in
    the last 2–3 h before the restart (their local wall clock already sits
    past the cutover). Erring that way keeps every post-flip row — the ones a
    live bot's rolling window is made of — exactly right; the misread band is
    a fixed ≤3 h slice of history that ages out of every window.

    DST: the spring-forward gap is shifted forward; the ambiguous autumn hour
    is unmappable and becomes NaT for Series (scalars take the DST reading).
    Measured on the live DB 2026-08-01: 113 row-values fall into that hour,
    all of them in ``closed_trades_master`` (54 ``time`` + 59 ``posted``).
    """
    cut = r3_cutover()
    if hasattr(values, "dt") or hasattr(values, "dtype"):  # pandas Series
        import pandas as pd

        s = pd.to_datetime(values, errors="coerce")
        if getattr(s.dt, "tz", None) is not None:  # already aware → true instants
            return s.dt.tz_convert(UTC).dt.tz_localize(None)
        if cut is None and not assume_legacy:
            return s
        legacy = s.notna() if assume_legacy else (s < pd.Timestamp(cut))
        conv = (
            s[legacy]
            .dt.tz_localize(LEGACY_WRITER_TZ, nonexistent="shift_forward", ambiguous="NaT")
            .dt.tz_convert("UTC")
            .dt.tz_localize(None)
        )
        out = s.copy()
        out[legacy] = conv
        return out

    if values is None:
        return None
    dt = values if isinstance(values, datetime.datetime) else datetime.datetime.fromisoformat(str(values))
    if dt.tzinfo is not None:
        return as_naive_utc(dt)
    if not assume_legacy and (cut is None or dt >= cut):
        return dt
    return dt.replace(tzinfo=ZoneInfo(LEGACY_WRITER_TZ)).astimezone(UTC).replace(tzinfo=None)


def utc_to_legacy_naive(dt: datetime.datetime) -> datetime.datetime:
    """Inverse of :func:`legacy_naive_to_utc` for a single query BOUND.

    A window bound has to be expressed in the domain the column stores, so the
    comparison can stay server-side. Without a cutover that is UTC (identity);
    with one, a bound older than the cutover is converted to the legacy wall
    clock. A window that straddles the cutover is off by the offset at exactly
    one of its two ends for at most the offset's worth of rows — the same ≤3 h
    band documented above.
    """
    dt = as_naive_utc(dt) if dt.tzinfo is not None else dt
    cut = r3_cutover()
    if cut is None or dt >= cut:
        return dt
    return dt.replace(tzinfo=UTC).astimezone(ZoneInfo(LEGACY_WRITER_TZ)).replace(tzinfo=None)


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

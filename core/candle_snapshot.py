# core/candle_snapshot.py — protocol + client for the shared candle-snapshot service.
#
# WHY THIS MODULE EXISTS
# ----------------------
# Nine hourly scanners (bots 7, 11, 12, 13, 14, 18, 24, 25, 34) walk the same
# ~523 coins at staggered minutes and read overlapping 1h windows of the same
# candles/indicators rows. 60-75 % of the ~6-7k candle queries per minute are
# duplicates across bots, and the `candles JOIN indicators` read is the #1 entry
# in pg_stat_statements (3.0M calls @ 36.5 ms). The fix is "fetch once, serve
# many": one process reads each (symbol, tf) once per candle period with the
# maximum lookback any bot needs, keeps the frames in RAM, and answers the bots'
# slices over localhost TCP.
#
# The bots never learn about it. `core/candles.py` is the single access point
# (see its header: "swap the internals without touching a single bot"), so the
# client hook lives THERE and nowhere else — no bot script and no
# `core/*_features.py` builder is touched (hard rule 7).
#
# THE GATE
# --------
# KYTHERA_CANDLE_SNAPSHOT is OFF by default. With the gate off,
# :func:`snapshot_enabled` returns False on the first line of every entry point
# and the read behaves exactly as it did before — that is the merge condition.
# Turning it on is an operator decision (.env + fleet restart).
#
# WHAT "IDENTICAL" MEANS HERE (and how it is achieved)
# ----------------------------------------------------
# `core.candles._fetch_df` builds every frame as
# ``pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])``.
# The snapshot path reproduces exactly that construction: the service holds the
# frame a DB read produced, slices it with the same window/limit/order rules the
# SQL uses, ships the resulting ROW VALUES over the wire, and the client rebuilds
# ``pd.DataFrame(rows, columns=cols)``. Same values, same construction, therefore
# same dtypes — see backtest/test_candle_snapshot_protocol.py.
#
# The wire format is line-based JSON (chart_data_service.py precedent). JSON
# carries floats round-trip-exactly (repr) and a missing value travels as
# `null`. The one type JSON cannot carry at all is `datetime`, so datetime
# columns are tagged and encoded as ISO-8601; anything that is neither
# JSON-native nor a datetime (a `Decimal`, say) is REFUSED rather than coerced —
# the service then answers "not covered" and the caller silently takes the DB
# path. Losing a cache hit is cheap; serving a subtly different frame to a
# trading bot is not.
#
# The one place where `null` is not enough information is an ALL-MISSING float
# slice, and that one is refused too — see _all_null_is_ambiguous.
#
# WHAT IS NEVER SERVED (deliberate holes, each one a fallback, never a guess)
# --------------------------------------------------------------------------
#   * ``include_forming=True`` — the snapshot only ever holds CLOSED candles.
#     Price-shaped readers (monitors 5/8, get_live_price) must keep hitting the
#     DB; contract 2 of core/candles.py.
#   * a (symbol, tf) or kind the service does not cover.
#   * a window the frame does not span (see :func:`select_slice`).
#   * a frame older than the newest closed candle for the request
#     (:func:`frame_is_fresh`) — the T-2026-KYT-9050-068 lesson: a backend that
#     silently serves frozen candles to a trading fleet is a loaded weapon. A
#     stale frame is refused on the service side AND re-checked on the client
#     side against the client's own clock.
#   * a joined read whose candle rows do not all have an indicator row. The SQL
#     is a LEFT JOIN, so a missing indicator row becomes a row of NULLs; rather
#     than reproduce NULL-fill semantics in pandas we hand the request back to
#     the DB.
#   * a slice in which some float column is ALL missing
#     (:func:`_all_null_is_ambiguous`).
#   * any transport/protocol error at all.
from __future__ import annotations

import json
import logging
import math
import os
import socket
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from core.candles import last_closed_open_time, snapshot_enabled, validate_symbol, validate_timeframe

_logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

#: The gate itself lives in core.candles (one reading of the flag, and it has to
#: be checkable there without importing this module); re-exported for callers
#: that already hold this module.
GATE_ENV = "KYTHERA_CANDLE_SNAPSHOT"
HOST_ENV = "CANDLE_SNAPSHOT_HOST"
PORT_ENV = "CANDLE_SNAPSHOT_PORT"
TIMEOUT_ENV = "CANDLE_SNAPSHOT_TIMEOUT_SEC"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5556  # 5555 is chart_data_service
DEFAULT_TIMEOUT_SEC = 2.0

#: Longest response line the client will accept. The widest legitimate answer is
#: bot 12's joined read (500 rows x ~121 columns, ~1-2 MB), so this is ~25x
#: headroom — and it bounds a wedged or hostile peer that would otherwise grow
#: the receive buffer until the BOT process runs out of memory.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

#: Reads served from RAM. `joined` is read_candles_with_indicators.
KIND_CANDLES = "candles"
KIND_INDICATORS = "indicators"
KIND_JOINED = "joined"
KINDS = (KIND_CANDLES, KIND_INDICATORS, KIND_JOINED)

#: The indicator columns the joined read resolves away — they exist on both
#: sides and the candle table wins (read_candles_with_indicators docstring).
JOIN_DROPPED_INDICATOR_COLUMNS = ("symbol", "open_time", "close")

# Column type tags on the wire. Only datetimes need one; everything else is
# JSON-native and travels as-is.
_TYPE_DATETIME = "dt"
_TYPE_RAW = "raw"

# Reasons a request is not served. All of them mean "take the DB path".
REASON_NOT_COVERED = "not_covered"  # (symbol, tf, kind) is not in the store
REASON_FORMING = "include_forming"  # the snapshot only holds closed candles
REASON_WINDOW = "window_not_covered"  # the frame does not span the request
REASON_STALE = "stale_frame"  # frame older than the newest closed candle
REASON_EMPTY_FRAME = "empty_frame"
REASON_UNKNOWN_COLUMN = "unknown_column"
REASON_INDICATOR_GAP = "indicator_gap"  # joined read would need a LEFT-JOIN NULL fill
REASON_ENCODE = "unencodable_value"
REASON_BAD_REQUEST = "bad_request"
REASON_CONFIG = "config"  # an env var this module reads is unusable
#: An all-missing float slice: NaN-in-the-DB and NULL-in-the-DB decode to
#: DIFFERENT dtypes and the wire cannot tell them apart (see
#: :func:`_all_null_is_ambiguous`). An ordinary case — a flatlining coin, an
#: as-of read of a window-global column — so it stays at DEBUG.
REASON_AMBIGUOUS_NULL = "ambiguous_all_null"

#: Reasons that indicate a real problem (unreachable service, stale frames,
#: a typo in the endpoint env) and are therefore logged at WARNING; the rest are
#: ordinary "not my request" cases and stay at DEBUG so a cold service does not
#: drown the bot logs.
_WARN_REASONS = frozenset({REASON_STALE, REASON_ENCODE, REASON_CONFIG, "transport", "protocol"})

#: A scan touches ~523 coins, so one WARN per coin per scan would be 523 lines
#: for a single root cause. One line per reason per minute is enough to notice.
_WARN_COOLDOWN_SEC = 60.0
_last_warned: dict[str, float] = {}


class SnapshotEncodeError(RuntimeError):
    """A frame holds a value the wire format cannot carry losslessly."""


class SnapshotAmbiguousNullError(RuntimeError):
    """A slice holds a float column whose dtype the wire cannot pin down.

    Deliberately NOT a subclass of :class:`SnapshotEncodeError`: an unencodable
    value means something is wrong with the data, this one means the request is
    simply not answerable from RAM — different log level, different reason code.
    """


def endpoint() -> tuple[str, int]:
    """(host, port) of the snapshot service — localhost by default.

    An unparsable port falls back to the default instead of raising: this runs
    inside every bot read, and a typo in `.env` must degrade to the DB path
    (via a connection that finds nothing listening), never take a scan down.
    """
    host = os.getenv(HOST_ENV, DEFAULT_HOST)
    raw = os.getenv(PORT_ENV, "").strip()
    if not raw:
        return host, DEFAULT_PORT
    try:
        return host, int(raw)
    except ValueError:
        _note(REASON_CONFIG, f"{PORT_ENV}={raw!r} is not a port number — using {DEFAULT_PORT}")
        return host, DEFAULT_PORT


def client_timeout() -> float:
    """Socket timeout for one request. Deliberately short: a slow service must
    never become slower than the DB read it is replacing."""
    try:
        return float(os.getenv(TIMEOUT_ENV, "") or DEFAULT_TIMEOUT_SEC)
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


def _note(reason: str, detail: str) -> None:
    """Log a fallback. WARNING for the reasons that mean something is wrong,
    DEBUG for the ordinary ones — both throttled per reason."""
    level = logging.WARNING if reason in _WARN_REASONS else logging.DEBUG
    if level == logging.WARNING:
        now = time.monotonic()
        if now - _last_warned.get(reason, float("-inf")) < _WARN_COOLDOWN_SEC:
            level = logging.DEBUG
        else:
            _last_warned[reason] = now
    _logger.log(level, "candle snapshot: falling back to the DB path (%s) — %s", reason, detail)


# ── Timezone alignment ────────────────────────────────────────────────────────
#
# `open_time` is `timestamp with time zone` in both the per-coin tables and the
# hypertables, so psycopg2 hands back aware datetimes and every comparison below
# is a plain instant comparison. A naive value can still show up against an
# offline fixture DB; it is then read as UTC — the same reading
# core.candles.latest_open_time and _assert_legacy_not_stale already apply.


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _align(value: datetime, like: Any) -> datetime:
    """`value` expressed with the same awareness as the frame value `like`.

    Comparing an aware bound against a naive column raises TypeError, so the
    bound follows the data — reading a naive column as UTC, never the other way
    round (that would move an instant).
    """
    naive_frame = getattr(like, "tzinfo", None) is None
    if naive_frame:
        return _as_utc(value).astimezone(timezone.utc).replace(tzinfo=None)
    return _as_utc(value)


# ── Wire format ───────────────────────────────────────────────────────────────


def _column_type(series: pd.Series) -> str:
    """`dt` if the column carries datetimes, `raw` otherwise.

    Datetime columns are usually a real datetime64 dtype, but a frame built from
    rows in which every datetime was NULL lands in `object` — hence the value
    probe as well.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return _TYPE_DATETIME
    if series.dtype == object:
        for value in series:
            if value is None or value is pd.NaT:
                continue
            return _TYPE_DATETIME if isinstance(value, datetime) else _TYPE_RAW
    return _TYPE_RAW


def _encode_value(value: Any, coltype: str) -> Any:
    if isinstance(value, np.generic):  # np.float64 & friends -> their Python twin
        value = value.item()
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        # A NaN that reaches this point sits in a column that also holds a real
        # float (:func:`encode_frame` refused the all-missing case before
        # calling us), and in such a column BOTH readings — a NaN the DB stores
        # and a NULL pandas rendered as NaN — decode to float64/NaN. So `null`
        # and `NaN` are interchangeable here and `null` is the one that is valid
        # JSON for a strict parser.
        return None
    if coltype == _TYPE_DATETIME:
        if not isinstance(value, datetime):
            raise SnapshotEncodeError(f"datetime column carries {type(value).__name__}")
        return value.isoformat()
    if isinstance(value, (bool, int, float, str)):
        return value
    raise SnapshotEncodeError(f"value of type {type(value).__name__} cannot be carried losslessly")


def _decode_value(value: Any, coltype: str) -> Any:
    if value is None:
        return None
    if coltype == _TYPE_DATETIME:
        return datetime.fromisoformat(value)
    return value


def _all_null_is_ambiguous(series: pd.Series) -> bool:
    """True when this column's dtype cannot be reproduced from the wire.

    Every missing value travels as `null`, so the decoded frame is the frame
    `_fetch_df` would have built if the DB had sent NULLs. That is the right
    frame — except where the DB did NOT send a NULL. `float8` is the one type
    here that holds a second kind of missing: a stored NaN, which psycopg2 reads
    back as `float('nan')`, and which `2_indicator_engine.py` writes on purpose
    (``_as_of_now_window_globals``: numeric window-globals become NaN on every
    non-reference bar; only the TEXT column becomes a real NULL).

    pandas then decides the dtype per frame. With at least one real float in the
    column both missings land on float64/NaN, so the two are interchangeable and
    the decode is exact. An ALL-missing float column is where they part: NULLs
    give object/None, NaNs give float64/NaN — and the wide frame the store holds
    renders both as NaN, so nothing on this side can tell which one the SQL path
    would produce. Such a slice is REFUSED (a flatlining coin's RSI, an as-of
    read landing on a NULLed window-global bar). Guessing either way would hand
    half the callers a frame whose `is None` / `pd.notna` checks answer
    differently from the DB path's.

    Non-float columns have one kind of missing only — NaT is not a value
    PostgreSQL can store in a `timestamptz`, and a TEXT column's missing is
    always a NULL — so their all-missing slices reproduce exactly and are served
    (see the text-column test in backtest/test_candle_snapshot_protocol.py).
    """
    if not pd.api.types.is_float_dtype(series):
        return False
    return len(series) > 0 and bool(series.isna().all())


def encode_frame(df: pd.DataFrame) -> dict[str, Any]:
    """Serialise a frame into the wire payload (columns + type tags + rows).

    Raises :class:`SnapshotEncodeError` for any value the format cannot carry
    without changing it, and :class:`SnapshotAmbiguousNullError` for a column
    whose dtype the wire cannot pin down — the caller answers "not covered" and
    the client takes the DB path.
    """
    columns = [str(c) for c in df.columns]
    for i, name in enumerate(columns):
        if _all_null_is_ambiguous(df.iloc[:, i]):
            raise SnapshotAmbiguousNullError(f"{name}: all-missing float column — DB NaN and DB NULL differ here")
    coltypes = [_column_type(df.iloc[:, i]) for i in range(df.shape[1])]
    rows = [
        [_encode_value(value, coltype) for value, coltype in zip(row, coltypes, strict=True)]
        for row in df.itertuples(index=False, name=None)
    ]
    return {"columns": columns, "coltypes": coltypes, "rows": rows}


def decode_frame(payload: dict[str, Any]) -> pd.DataFrame:
    """Rebuild the frame from the wire payload.

    Uses the SAME construction as core.candles._fetch_df —
    ``pd.DataFrame(rows, columns=cols)`` over the original row values — so the
    dtypes are whatever the DB path would have produced.
    """
    columns = list(payload["columns"])
    coltypes = list(payload["coltypes"])
    # strict=True: a row whose width disagrees with the header is a corrupt
    # payload, and a silently truncated frame is exactly what must not reach a bot.
    rows = [
        tuple(_decode_value(value, coltype) for value, coltype in zip(row, coltypes, strict=True))
        for row in payload["rows"]
    ]
    return pd.DataFrame(rows, columns=columns)


# ── Slicing: the SQL semantics, reproduced on a frame ─────────────────────────


def select_slice(
    df: pd.DataFrame,
    *,
    limit: int | None,
    start: datetime | None,
    end: datetime | None,
) -> tuple[pd.DataFrame | None, str]:
    """The rows the DB read would return, or (None, reason) if the frame cannot
    prove it holds them.

    Reproduces ``_windowed_select``: filter to ``[start, end]`` on open_time,
    order DESC, take ``limit``, hand back ASC. The frame is already ASC and
    open_time is unique per (symbol, tf) (primary key), so "the newest `limit`
    rows" is a plain tail slice — no tie-breaking ambiguity.

    COVERAGE is the load-bearing part. The frame holds only the newest
    ``lookback`` rows, so it spans ``[frame_min, frame_max]`` while the table
    spans ``[db_min, frame_max]``. The frame can answer the request when either

      * ``limit`` rows already lie inside the window (the older rows the DB
        would also see can never enter the newest-`limit` answer), or
      * the window's own floor is at or above ``frame_min`` (then the frame
        holds every row the window selects).

    Otherwise the answer might be missing rows that exist below ``frame_min``,
    and the request goes to the DB. Freshness at the TOP of the window is a
    separate question — :func:`frame_is_fresh`.
    """
    if df is None or df.empty:
        return None, REASON_EMPTY_FRAME
    if "open_time" not in df.columns:
        return None, REASON_NOT_COVERED
    if limit is not None and limit < 0:
        # `LIMIT -1` is a server-side error; let the DB path raise it as before.
        return None, REASON_BAD_REQUEST

    open_time = df["open_time"]
    frame_min = open_time.iloc[0]

    window = df
    if start is not None:
        window = window[window["open_time"] >= _align(start, frame_min)]
    if end is not None:
        window = window[window["open_time"] <= _align(end, frame_min)]

    if limit == 0:
        # SQL `LIMIT 0` returns nothing; `iloc[-0:]` would return everything.
        return window.iloc[0:0].reset_index(drop=True), ""

    window_floor_is_covered = start is not None and _align(start, frame_min) >= frame_min
    if not window_floor_is_covered and (limit is None or len(window) < limit):
        return None, REASON_WINDOW

    sliced = window if limit is None else window.iloc[-limit:]
    return sliced.reset_index(drop=True), ""


def frame_is_fresh(frame_max_open_time: Any, tf: str, *, end: datetime | None, now: datetime) -> bool:
    """True when the frame reaches the newest candle the request could see.

    The anchor is ``now``, or ``end`` when the request asks as-of an earlier
    instant (a replay/as-of read does not need a fresh frame — and with
    ``end <= frame_max`` the check passes on its own, so no special case is
    needed). "Reaches" means the frame's newest open_time is at or beyond the
    newest CLOSED candle of ``tf`` at that anchor.
    """
    anchor = now if end is None else min(_as_utc(now), _as_utc(end))
    required = last_closed_open_time(tf, _as_utc(anchor))
    return bool(frame_max_open_time >= _align(required, frame_max_open_time))


# ── Store (service side) ──────────────────────────────────────────────────────


class SnapshotStore:
    """The service's in-RAM frames, keyed by (symbol, tf, kind).

    Only ``candles`` and ``indicators`` frames are stored; a joined request is
    composed from the two. Assignment and lookup are single dict operations, so
    a reader always sees a whole frame — never a half-written one, and never a
    mutated one: a refresh REPLACES the entry, and every read path builds new
    objects (slice, projection, concat) rather than writing into the stored
    frame. Iteration is the one thing that is not atomic — a sweep inserting
    while ``stats``/``memory_bytes`` walked the dict would raise "dictionary
    changed size during iteration" — so those two take a snapshot under the lock
    and walk that. ``get`` stays lock-free; it is the hot path.
    """

    def __init__(self) -> None:
        self._frames: dict[tuple[str, str, str], pd.DataFrame] = {}
        self._fetched_at: dict[tuple[str, str, str], float] = {}
        self._lock = threading.Lock()

    def put(self, symbol: str, tf: str, kind: str, df: pd.DataFrame, *, fetched_at: float | None = None) -> None:
        key = (symbol, tf, kind)
        stamp = time.time() if fetched_at is None else fetched_at
        with self._lock:
            self._frames[key] = df
            self._fetched_at[key] = stamp

    def get(self, symbol: str, tf: str, kind: str) -> pd.DataFrame | None:
        return self._frames.get((symbol, tf, kind))

    def fetched_at(self, symbol: str, tf: str, kind: str) -> float | None:
        return self._fetched_at.get((symbol, tf, kind))

    def keys(self) -> list[tuple[str, str, str]]:
        with self._lock:
            return list(self._frames)

    def max_open_time(self, symbol: str, tf: str, kind: str) -> Any | None:
        df = self.get(symbol, tf, kind)
        if df is None or df.empty or "open_time" not in df.columns:
            return None
        return df["open_time"].iloc[-1]

    def _entries(self) -> list[tuple[tuple[str, str, str], pd.DataFrame]]:
        """A stable view of the store to walk outside the lock."""
        with self._lock:
            return list(self._frames.items())

    def memory_bytes(self) -> int:
        return int(sum(int(df.memory_usage(deep=True).sum()) for _, df in self._entries()))

    def stats(self) -> dict[str, Any]:
        entries = self._entries()  # one snapshot for all four figures — they agree
        by_kind: dict[str, int] = {}
        for (_, _, kind), _df in entries:
            by_kind[kind] = by_kind.get(kind, 0) + 1
        return {
            "entries": len(entries),
            "entries_by_kind": by_kind,
            "rows": int(sum(len(df) for _, df in entries)),
            "memory_bytes": int(sum(int(df.memory_usage(deep=True).sum()) for _, df in entries)),
        }


# ── Request handling (service side) ───────────────────────────────────────────


def _resolve_columns(requested: Sequence[str] | None, available: list[str]) -> list[str] | None:
    """The projection to serve, or None when a requested column is absent.

    ``requested is None`` means the caller asked for the table's own shape
    (``SELECT *``, or CANDLE_COLUMNS on the hypertable path) — which is exactly
    the shape the stored frame was read with.
    """
    if requested is None:
        return list(available)
    cols = [str(c) for c in requested]
    return None if any(c not in available for c in cols) else cols


def _serve_simple(
    store: SnapshotStore, kind: str, symbol: str, tf: str, req: dict[str, Any], now: datetime
) -> tuple[pd.DataFrame | None, str]:
    df = store.get(symbol, tf, kind)
    if df is None:
        return None, REASON_NOT_COVERED
    # A frame with no rows (or no open_time) has no watermark to judge, so the
    # freshness check below cannot run — refuse before it, not after.
    if df.empty or "open_time" not in df.columns:
        return None, REASON_EMPTY_FRAME
    cols = _resolve_columns(req.get("columns"), list(df.columns))
    if cols is None:
        return None, REASON_UNKNOWN_COLUMN
    end = _parse_iso(req.get("end"))
    if not frame_is_fresh(df["open_time"].iloc[-1], tf, end=end, now=now):
        return None, REASON_STALE
    sliced, reason = select_slice(df, limit=req.get("limit"), start=_parse_iso(req.get("start")), end=end)
    if sliced is None:
        return None, reason
    return sliced[cols], ""


def _serve_joined(
    store: SnapshotStore, symbol: str, tf: str, req: dict[str, Any], now: datetime
) -> tuple[pd.DataFrame | None, str]:
    """read_candles_with_indicators, composed from the two stored frames.

    The SQL is ``candles h LEFT JOIN indicators i ON h.open_time = i.open_time``
    with the window, forming filter and LIMIT all applied to ``h`` — so the
    joined rowset IS the candle rowset, widened by the indicator columns
    ((symbol, tf, open_time) is the indicator primary key, so the join is 1:1).
    That makes the composition an alignment, not a join: slice the candle frame
    exactly as :func:`select_slice` would, then look every open_time up in the
    indicator frame. If ONE of them is missing, the SQL would emit a row of
    NULLs; rather than reproduce that in pandas the request goes to the DB.
    """
    candles = store.get(symbol, tf, KIND_CANDLES)
    indicators = store.get(symbol, tf, KIND_INDICATORS)
    if candles is None or indicators is None:
        return None, REASON_NOT_COVERED
    if candles.empty or "open_time" not in candles.columns:
        return None, REASON_EMPTY_FRAME
    if indicators.empty or "open_time" not in indicators.columns:
        return None, REASON_EMPTY_FRAME

    ccols = _resolve_columns(req.get("candle_columns"), list(candles.columns))
    requested_icols = req.get("indicator_columns")
    if requested_icols is None:
        # What read_candles_with_indicators would have built from the catalog:
        # every indicator column minus the three the candle side already owns.
        icols: list[str] | None = [c for c in indicators.columns if c not in JOIN_DROPPED_INDICATOR_COLUMNS]
    else:
        icols = _resolve_columns(requested_icols, list(indicators.columns))
    if ccols is None or icols is None:
        return None, REASON_UNKNOWN_COLUMN
    if not icols:
        return None, REASON_BAD_REQUEST

    end = _parse_iso(req.get("end"))
    if not frame_is_fresh(candles["open_time"].iloc[-1], tf, end=end, now=now):
        return None, REASON_STALE
    sliced, reason = select_slice(candles, limit=req.get("limit"), start=_parse_iso(req.get("start")), end=end)
    if sliced is None:
        return None, reason

    ind = indicators.set_index("open_time")
    if ind.index.has_duplicates or not sliced["open_time"].isin(ind.index).all():
        return None, REASON_INDICATOR_GAP

    right = ind.loc[sliced["open_time"], icols].reset_index(drop=True)
    # axis=1 concat keeps every column's own dtype — no upcasting across sides.
    return pd.concat([sliced[ccols], right], axis=1), ""


def serve_request(store: SnapshotStore, req: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Answer one `get` request against the store. Never raises.

    A refusal is a normal answer: ``{"ok": false, "reason": ...}``. The client
    turns any refusal — and any transport failure — into the DB path.
    """
    now = datetime.now(timezone.utc) if now is None else now
    try:
        kind = req.get("kind")
        symbol = str(req.get("symbol", ""))
        tf = str(req.get("tf", ""))
        if kind not in KINDS:
            return {"ok": False, "reason": REASON_BAD_REQUEST}
        validate_symbol(symbol)
        validate_timeframe(tf)
        if req.get("include_forming"):
            # The store holds closed candles only; the forming row is by
            # definition not in it (contract 2 of core/candles.py).
            return {"ok": False, "reason": REASON_FORMING}

        if kind == KIND_JOINED:
            df, reason = _serve_joined(store, symbol, tf, req, now)
        else:
            df, reason = _serve_simple(store, kind, symbol, tf, req, now)
        if df is None:
            return {"ok": False, "reason": reason}

        frame_kind = KIND_CANDLES if kind == KIND_JOINED else kind
        frame_max = store.max_open_time(symbol, tf, frame_kind)
        payload = encode_frame(df)
        payload["ok"] = True
        payload["frame_open_time"] = None if frame_max is None else _as_datetime(frame_max).isoformat()
        return payload
    except SnapshotAmbiguousNullError as exc:
        return {"ok": False, "reason": REASON_AMBIGUOUS_NULL, "detail": str(exc)}
    except SnapshotEncodeError as exc:
        return {"ok": False, "reason": REASON_ENCODE, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 — a refusal is always safe; a crash is not
        return {"ok": False, "reason": REASON_BAD_REQUEST, "detail": f"{type(exc).__name__}: {exc}"}


def _as_datetime(value: Any) -> datetime:
    """pandas hands back a Timestamp; the wire wants a plain datetime."""
    return value.to_pydatetime() if isinstance(value, pd.Timestamp) else value


def _parse_iso(value: Any) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


# ── Client ────────────────────────────────────────────────────────────────────


def request(payload: dict[str, Any]) -> dict[str, Any] | None:
    """One line-based JSON round trip. None on any transport/protocol failure."""
    host, port = endpoint()
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    try:
        with socket.create_connection((host, port), timeout=client_timeout()) as sock:
            sock.settimeout(client_timeout())
            sock.sendall(line.encode("utf-8"))
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_RESPONSE_BYTES:
                    _note("protocol", f"response exceeded {MAX_RESPONSE_BYTES} bytes without a newline")
                    return None
    except (TimeoutError, OSError) as exc:
        _note("transport", f"{host}:{port} — {type(exc).__name__}: {exc}")
        return None
    try:
        response = json.loads(bytes(buf).split(b"\n", 1)[0].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as exc:
        _note("protocol", f"unreadable response — {type(exc).__name__}: {exc}")
        return None
    if not isinstance(response, dict):
        _note("protocol", f"expected an object, got {type(response).__name__}")
        return None
    return response


def _fetch(
    kind: str,
    symbol: str,
    tf: str,
    *,
    limit: int | None,
    start: datetime | None,
    end: datetime | None,
    include_forming: bool,
    columns: Sequence[str] | None = None,
    candle_columns: Sequence[str] | None = None,
    indicator_columns: Sequence[str] | None = None,
) -> pd.DataFrame | None:
    """The frame from the snapshot service, or None to take the DB path."""
    if not snapshot_enabled():
        return None
    if include_forming:
        # Cheap local refusal — no socket for a request that can never be served.
        return None
    validate_symbol(symbol)  # same fail-fast as the DB path: a bad identifier
    validate_timeframe(tf)  # raises before anything is dialled

    payload: dict[str, Any] = {
        "cmd": "get",
        "kind": kind,
        "symbol": symbol,
        "tf": tf,
        "limit": None if limit is None else int(limit),
        "start": _iso(start),
        "end": _iso(end),
        "include_forming": False,
    }
    if kind == KIND_JOINED:
        payload["candle_columns"] = None if candle_columns is None else [str(c) for c in candle_columns]
        payload["indicator_columns"] = None if indicator_columns is None else [str(c) for c in indicator_columns]
    else:
        payload["columns"] = None if columns is None else [str(c) for c in columns]

    response = request(payload)
    if response is None:
        return None
    if not response.get("ok"):
        _note(str(response.get("reason", "refused")), f"{symbol} {tf} {kind}")
        return None

    # Re-check freshness against OUR clock, not the service's. The service
    # already refused stale frames; this is the second door, because "the
    # service said it was fresh" is exactly the assurance T-2026-KYT-9050-068
    # showed cannot be trusted on its own.
    frame_open_time = response.get("frame_open_time")
    if frame_open_time is None:
        _note(REASON_STALE, f"{symbol} {tf} {kind} — service reported no frame watermark")
        return None
    try:
        watermark = datetime.fromisoformat(frame_open_time)
    except (TypeError, ValueError) as exc:
        # A watermark this end cannot read is a peer problem, not a bot problem:
        # fall back like any other refusal instead of raising inside the read.
        _note("protocol", f"{symbol} {tf} {kind} — unreadable frame watermark {frame_open_time!r}: {exc}")
        return None
    if not frame_is_fresh(watermark, tf, end=end, now=datetime.now(timezone.utc)):
        _note(REASON_STALE, f"{symbol} {tf} {kind} — frame watermark {frame_open_time} is behind the last close")
        return None

    try:
        return decode_frame(response)
    except Exception as exc:  # noqa: BLE001 — a malformed payload must not break a read
        _note("protocol", f"{symbol} {tf} {kind} — undecodable frame: {type(exc).__name__}: {exc}")
        return None


def read_candles(
    symbol: str,
    tf: str,
    *,
    limit: int | None,
    start: datetime | None,
    end: datetime | None,
    include_forming: bool,
    columns: Sequence[str] | None,
) -> pd.DataFrame | None:
    """core.candles.read_candles from the snapshot, or None for the DB path."""
    return _fetch(
        KIND_CANDLES,
        symbol,
        tf,
        limit=limit,
        start=start,
        end=end,
        include_forming=include_forming,
        columns=columns,
    )


def read_indicators(
    symbol: str,
    tf: str,
    *,
    limit: int | None,
    start: datetime | None,
    end: datetime | None,
    include_forming: bool,
    columns: Sequence[str] | None,
) -> pd.DataFrame | None:
    """core.candles.read_indicators from the snapshot, or None for the DB path."""
    return _fetch(
        KIND_INDICATORS,
        symbol,
        tf,
        limit=limit,
        start=start,
        end=end,
        include_forming=include_forming,
        columns=columns,
    )


def read_candles_with_indicators(
    symbol: str,
    tf: str,
    *,
    limit: int | None,
    start: datetime | None,
    end: datetime | None,
    include_forming: bool,
    candle_columns: Sequence[str] | None,
    indicator_columns: Sequence[str] | None,
) -> pd.DataFrame | None:
    """core.candles.read_candles_with_indicators from the snapshot, or None.

    ``indicator_columns=None`` is forwarded as None on purpose: resolving it
    DB-side costs the information_schema catalog probe this path exists to
    avoid, and the service already knows the column list — it read the frame
    with exactly that projection.
    """
    return _fetch(
        KIND_JOINED,
        symbol,
        tf,
        limit=limit,
        start=start,
        end=end,
        include_forming=include_forming,
        candle_columns=candle_columns,
        indicator_columns=indicator_columns,
    )


__all__ = [
    "GATE_ENV",
    "KINDS",
    "KIND_CANDLES",
    "KIND_INDICATORS",
    "KIND_JOINED",
    "REASON_AMBIGUOUS_NULL",
    "SnapshotAmbiguousNullError",
    "SnapshotEncodeError",
    "SnapshotStore",
    "decode_frame",
    "encode_frame",
    "endpoint",
    "frame_is_fresh",
    "read_candles",
    "read_candles_with_indicators",
    "read_indicators",
    "request",
    "select_slice",
    "serve_request",
    "snapshot_enabled",
]

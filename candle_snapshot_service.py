"""
candle_snapshot_service.py — shared candle/indicator snapshot ("fetch once, serve many")
========================================================================================

Nine hourly scanners (bots 7, 11, 12, 13, 14, 18, 24, 25, 34) walk the same
~523 coins at staggered minutes and read overlapping 1h windows of the same
rows. 60-75 % of the ~6-7k candle queries per minute are duplicates across
bots, and ``candles JOIN indicators`` is the #1 statement in pg_stat_statements
(3.0M calls @ 36.5 ms). This process reads each (symbol, tf) ONCE per candle
period with the maximum lookback any bot needs, keeps the frames in RAM, and
answers the bots' slices over localhost TCP.

The bots do not know it exists. ``core/candles.py`` is the single access point
for candle data, so the client hook lives there (``core/candle_snapshot.py``)
and no bot script and no ``core/*_features.py`` builder is touched.

Gate
----
``KYTHERA_CANDLE_SNAPSHOT`` is **off by default**. With the gate off nothing can
ask this service anything, so the process makes itself DORMANT: no sweep, no
store, no DB connection, no listening socket — just a sleep loop with a
heartbeat line every 15 minutes (:func:`dormant_loop`). Sweeping for nobody
would cost ~1046 DB reads/h and ~320 MB of RAM, which is exactly the kind of
cost a default-off gate is supposed to prevent. Every read then behaves exactly
as it did before. Turning the gate on is an operator decision (.env + fleet
restart).

Architecture (modelled on chart_data_service.py)
------------------------------------------------
- One refresh loop. Every ``REFRESH_POLL_SEC`` it asks — in RAM, without
  touching the DB — which entries are behind the newest closed candle, and
  re-reads exactly those. When everything is fresh the poll costs nothing, so
  the DB load is one read per (symbol, tf, kind) per candle period. An entry
  that stays behind after a re-read (a thin or delisted coin whose candle
  simply has not been written) backs off for ``STALE_BACKOFF_SEC`` instead of
  being retried every poll.
- ``REFRESH_WORKERS`` threads, each with its own pooled connection (the default
  keeps well inside ``KYTHERA_DB_POOL_MAX``). A full sweep is ~2 x 523 reads;
  the scanners start at minute 2, so the sweep has to finish inside a minute.
- One TCP server on localhost, line-based JSON (see core/candle_snapshot.py for
  the wire format).
- No disk snapshot. Unlike the chart buffer this store is reconstructible from
  the DB in one sweep, and a snapshot from before a restart is by definition
  stale — the one thing this service must never serve.

Protocol (line-based JSON, newline-terminated)
----------------------------------------------
Request:  {"cmd": "get", "kind": "candles"|"indicators"|"joined", "symbol": ...,
           "tf": "1h", "limit": 100, "start": "<iso>|null", "end": "<iso>|null",
           "include_forming": false, "columns"|"candle_columns"+"indicator_columns": [...]|null}
Response: {"ok": true, "columns": [...], "coltypes": [...], "rows": [[...]],
           "frame_open_time": "<iso>"}
Refusal:  {"ok": false, "reason": "stale_frame"|"window_not_covered"|...}
Health:   {"cmd": "health"} -> {"status": "ok", "entries": ..., "memory_bytes": ..., ...}

A refusal is a normal answer: the client takes the DB path and logs it. Never
serving a frame that might differ from the DB is the whole design (the
T-2026-KYT-9050-068 lesson — a backend that silently serves frozen candles to a
trading fleet is a loaded weapon).

Memory budget
-------------
The dominant term is the indicator store: symbols x INDICATOR_LOOKBACK x ~120
double-precision columns. At the defaults (523 coins, 500 rows) that is roughly
250 MB, plus ~70 MB of candles. The actual figure is logged after every sweep
("store: ... MB") — check it against free RAM on the VPS BEFORE flipping the
gate, and turn the lookbacks down if it is tight.

Start
-----
    py candle_snapshot_service.py

Logs: logs/candle_snapshot_service.log
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from core.candle_snapshot import (
    GATE_ENV,
    KIND_CANDLES,
    KIND_INDICATORS,
    SnapshotStore,
    endpoint,
    frame_is_fresh,
    serve_request,
    snapshot_enabled,
)
from core.candles import TF_SECONDS, history_start, read_candles, read_indicators
from core.logging_setup import setup_logging
from core.market_utils import load_coins as _core_load_coins

logger = setup_logging("CANDLE_SNAPSHOT")

# ─── Configuration ────────────────────────────────────────────────────────────

COINS_FILE = "coins.json"

#: Timeframes to snapshot. Default 1h — the nine hourly scanners are the whole
#: point; 4h (bot 25) is opt-in because it doubles the store for one caller.
TIMEFRAMES = [tf.strip() for tf in os.getenv("KYTHERA_SNAPSHOT_TIMEFRAMES", "1h").split(",") if tf.strip()]

#: Rows kept per (symbol, tf). The candle default covers the widest hourly
#: reader (bot 14, limit=1700) and the 95-day windows of bots 13/34; the
#: indicator default covers bot 12's limit=500 joined read. Anything wider than
#: the store falls back to the DB — a miss, never a wrong answer.
CANDLE_LOOKBACK = int(os.getenv("KYTHERA_SNAPSHOT_CANDLE_LOOKBACK", "2400"))
INDICATOR_LOOKBACK = int(os.getenv("KYTHERA_SNAPSHOT_INDICATOR_LOOKBACK", "500"))

REFRESH_WORKERS = int(os.getenv("KYTHERA_SNAPSHOT_WORKERS", "4"))
#: How often staleness is evaluated. The check is pure RAM arithmetic, so a
#: tight poll is cheap; it only turns into DB work right after a candle closes.
REFRESH_POLL_SEC = int(os.getenv("KYTHERA_SNAPSHOT_POLL_SEC", "20"))
#: A (symbol, tf, kind) that is still behind after a re-read is not retried for
#: this long — thin and delisted coins would otherwise be re-read every poll.
STALE_BACKOFF_SEC = int(os.getenv("KYTHERA_SNAPSHOT_STALE_BACKOFF_SEC", "300"))
#: coins.json is rewritten daily by 6_housekeeping; pick new listings up
#: additively, exactly like chart_data_service does.
COIN_REFRESH_INTERVAL_SEC = int(os.getenv("KYTHERA_SNAPSHOT_COIN_REFRESH_SEC", "300"))

#: Dormant mode (gate off). How often the gate is re-read, and how often the
#: process says so in the log. Both cheap: the poll is one os.getenv.
DORMANT_POLL_SEC = float(os.getenv("KYTHERA_SNAPSHOT_DORMANT_POLL_SEC", "60"))
DORMANT_LOG_INTERVAL_SEC = float(os.getenv("KYTHERA_SNAPSHOT_DORMANT_LOG_SEC", "900"))

_START_TIME = time.time()

STORE = SnapshotStore()
_SYMBOLS: list[str] = []
_SYMBOL_LOCK = threading.Lock()
#: (symbol, tf, kind) -> monotonic deadline before which we do not retry.
_BACKOFF_UNTIL: dict[tuple[str, str, str], float] = {}
_LAST_SWEEP: dict[str, Any] = {"started_at": None, "duration_sec": None, "refreshed": 0, "failed": 0}


def load_coins() -> list[str]:
    """Sorted, de-duplicated USDT symbols from coins.json (chart_data_service parity)."""
    return sorted(set(_core_load_coins(COINS_FILE, usdt_only=True, uppercase=True)))


# ─── Refresh ──────────────────────────────────────────────────────────────────


def lookback_for(kind: str) -> int:
    return CANDLE_LOOKBACK if kind == KIND_CANDLES else INDICATOR_LOOKBACK


def due_keys(
    store: SnapshotStore, symbols: list[str], tfs: list[str], *, now: datetime, monotonic: float
) -> list[tuple[str, str, str]]:
    """The (symbol, tf, kind) entries that need a DB read right now.

    Missing entries always qualify; present ones only when their newest row is
    behind the newest closed candle of their timeframe AND their backoff has
    expired. Pure RAM — this is what keeps the poll free once the store is warm.
    """
    due: list[tuple[str, str, str]] = []
    for tf in tfs:
        for symbol in symbols:
            for kind in (KIND_CANDLES, KIND_INDICATORS):
                key = (symbol, tf, kind)
                if monotonic < _BACKOFF_UNTIL.get(key, float("-inf")):
                    continue
                watermark = store.max_open_time(symbol, tf, kind)
                if watermark is None or not frame_is_fresh(watermark, tf, end=None, now=now):
                    due.append(key)
    return due


def _read_frame(conn: Any, symbol: str, tf: str, kind: str) -> Any:
    """The one DB read this whole service exists to perform.

    ``columns=None`` deliberately: the store then holds the table's own shape,
    which is exactly what a caller passing ``columns=None`` expects and a
    superset of every projection a caller can ask for. ``start`` is the
    TimescaleDB chunk-exclusion hint (T-2026-CU-9050-180) — it bounds the plan
    without changing which of the newest ``limit`` rows come back.
    """
    lookback = lookback_for(kind)
    reader = read_candles if kind == KIND_CANDLES else read_indicators
    return reader(
        conn,
        symbol,
        tf,
        limit=lookback,
        start=history_start(tf, lookback),
        include_forming=False,
        columns=None,
    )


def _refresh_one(conn: Any, key: tuple[str, str, str], now: datetime, monotonic: float) -> bool:
    """Re-read one entry on an open connection. True when the frame is now fresh."""
    symbol, tf, kind = key
    try:
        df = _read_frame(conn, symbol, tf, kind)
    except Exception as exc:  # noqa: BLE001 — one bad coin must not stop the sweep
        logger.warning("refresh %s %s %s failed: %s: %s", symbol, tf, kind, type(exc).__name__, exc)
        try:
            # A failed statement aborts the transaction; the next key on this
            # shared connection would fail with "current transaction is aborted".
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        _BACKOFF_UNTIL[key] = monotonic + STALE_BACKOFF_SEC
        return False

    STORE.put(symbol, tf, kind, df)
    watermark = STORE.max_open_time(symbol, tf, kind)
    fresh = watermark is not None and frame_is_fresh(watermark, tf, end=None, now=now)
    if fresh:
        _BACKOFF_UNTIL.pop(key, None)
    else:
        # The DB itself has not caught up (thin coin, delisted, ingestion lag).
        # The frame is stored anyway — serve_request refuses it on staleness, so
        # it costs nothing and saves the re-read once the candle does land.
        _BACKOFF_UNTIL[key] = monotonic + STALE_BACKOFF_SEC
    return fresh


def _refresh_chunk(keys: list[tuple[str, str, str]], now: datetime, monotonic: float) -> int:
    """Refresh one worker's share of the sweep on ONE pooled connection.

    Per-key connections would mean ~1k pool get/put round trips per sweep; the
    indicator-engine CPU work (T-2026-CU-9050-174) settled that question already
    — hold the connection for the batch.
    """
    from core.database import db_connection

    try:
        with db_connection() as conn:
            return sum(1 for key in keys if _refresh_one(conn, key, now, monotonic))
    except Exception as exc:  # noqa: BLE001 — no connection: back the whole chunk off
        logger.warning("refresh chunk of %d entries failed: %s: %s", len(keys), type(exc).__name__, exc)
        for key in keys:
            _BACKOFF_UNTIL[key] = monotonic + STALE_BACKOFF_SEC
        return 0


def sweep() -> dict[str, Any]:
    """One refresh pass over everything that is due. Blocking; runs in a thread."""
    now = datetime.now(timezone.utc)
    monotonic = time.monotonic()
    with _SYMBOL_LOCK:
        symbols = list(_SYMBOLS)
    keys = due_keys(STORE, symbols, TIMEFRAMES, now=now, monotonic=monotonic)
    if not keys:
        return {"refreshed": 0, "failed": 0, "duration_sec": 0.0}

    started = time.monotonic()
    workers = max(1, REFRESH_WORKERS)
    chunks = [chunk for chunk in (keys[i::workers] for i in range(workers)) if chunk]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="snapshot") as pool:
        fresh = sum(pool.map(lambda chunk: _refresh_chunk(chunk, now, monotonic), chunks))
    duration = time.monotonic() - started
    result = {"refreshed": len(keys), "failed": len(keys) - fresh, "duration_sec": round(duration, 1)}
    logger.info(
        "sweep: %d entries in %.1fs (%d still behind the last close) — store: %d entries, %.0f MB",
        len(keys),
        duration,
        len(keys) - fresh,
        STORE.stats()["entries"],
        STORE.memory_bytes() / 1024 / 1024,
    )
    return result


async def refresh_loop() -> None:
    while True:
        try:
            if not snapshot_enabled():
                # No consumers ⇒ no DB work. main() parks the process in
                # dormant_loop before this loop ever starts, so reaching here
                # means the gate went off after the start — the second door.
                await asyncio.sleep(REFRESH_POLL_SEC)
                continue
            _LAST_SWEEP["started_at"] = datetime.now(timezone.utc).isoformat()
            _LAST_SWEEP.update(await asyncio.to_thread(sweep))
        except Exception as exc:  # noqa: BLE001 — the loop outlives any single sweep
            logger.error("refresh loop error: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(REFRESH_POLL_SEC)


async def coin_refresh_loop() -> None:
    """Additively pick up coins newly listed in coins.json.

    Conservative, exactly like chart_data_service.coin_refresh_loop: an empty or
    torn read changes nothing, and symbols are never removed — a delisted coin
    just stops going fresh and backs off.
    """
    while True:
        await asyncio.sleep(COIN_REFRESH_INTERVAL_SEC)
        try:
            current = set(await asyncio.to_thread(load_coins))
            if not current:
                continue
            with _SYMBOL_LOCK:
                new = sorted(current - set(_SYMBOLS))
                if new:
                    _SYMBOLS.extend(new)
            if new:
                logger.info("%d new coins in coins.json — snapshot coverage extended.", len(new))
        except Exception as exc:  # noqa: BLE001
            logger.error("coin refresh error: %s: %s", type(exc).__name__, exc)


# ─── TCP server ───────────────────────────────────────────────────────────────


def health_payload() -> dict[str, Any]:
    stats = STORE.stats()
    return {
        "status": "ok",
        "uptime_sec": int(time.time() - _START_TIME),
        "gate_enabled": snapshot_enabled(),
        "symbols": len(_SYMBOLS),
        "timeframes": list(TIMEFRAMES),
        "candle_lookback": CANDLE_LOOKBACK,
        "indicator_lookback": INDICATOR_LOOKBACK,
        "backoff_entries": len(_BACKOFF_UNTIL),
        "last_sweep": dict(_LAST_SWEEP),
        **stats,
    }


def handle_line(line: str) -> dict[str, Any]:
    """Parse and answer one request line. Never raises."""
    try:
        req = json.loads(line)
    except json.JSONDecodeError as exc:
        return {"ok": False, "reason": "bad_request", "detail": f"invalid json: {exc}"}
    if not isinstance(req, dict):
        return {"ok": False, "reason": "bad_request", "detail": "expected an object"}
    cmd = req.get("cmd")
    if cmd == "health":
        return health_payload()
    if cmd == "get":
        return serve_request(STORE, req)
    return {"ok": False, "reason": "bad_request", "detail": f"unknown cmd: {cmd}"}


def render_response(line: str) -> bytes:
    """Answer one request line and serialise it — the whole CPU cost in one
    call, so a single thread hop takes all of it off the event loop."""
    return (json.dumps(handle_line(line), separators=(",", ":")) + "\n").encode("utf-8")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """One request per connection (chart_data_service pattern).

    ``handle_line`` is CPU work — slicing and JSON-encoding a 500 x 121 joined
    response is ~500 ms — so it runs in a worker thread, not on the event loop.
    On the loop it would serialise the whole fleet: nine scanners burst through
    ~523 coins each, and the second caller of a pair would sit out the first
    one's encode and then trip the client's 2 s timeout (a fallback to the DB
    path, i.e. exactly the load this service exists to remove).

    Off-loading is safe because the handler only READS: ``serve_request`` looks
    the frame up with one atomic dict get and then builds new objects (mask,
    ``iloc`` slice, projection, ``concat``). A concurrent refresh REPLACES the
    dict entry rather than writing into the frame, so a reader mid-request keeps
    the whole, consistent frame it started with. ``stats``/``memory_bytes`` walk
    a snapshot taken under the store lock.
    """
    addr = writer.get_extra_info("peername")
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not raw:
            return
        writer.write(await asyncio.to_thread(render_response, raw.decode("utf-8").strip()))
        await writer.drain()
    except asyncio.TimeoutError:
        logger.debug("client %s timed out", addr)
    except Exception as exc:  # noqa: BLE001 — one bad client must not kill the server
        logger.debug("client %s error: %s: %s", addr, type(exc).__name__, exc)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


# ─── Orchestration ────────────────────────────────────────────────────────────


async def dormant_loop() -> None:
    """Idle until the gate turns on. Returns as soon as it is on.

    "Dormant" is literal: no DB connection, no store, no listening socket, no
    coins.json read. With ``KYTHERA_CANDLE_SNAPSHOT`` off nothing can query this
    service, so a sweeping process would burn ~1046 DB reads/h and hold ~320 MB
    for zero consumers — the cost the default-off gate exists to avoid.

    The gate is re-read every poll (``snapshot_enabled`` reads the env at call
    time). The ordinary flip is .env + fleet restart; re-reading just means a
    process that is already running picks it up instead of needing a second one.
    """
    last_log: float | None = None
    while not snapshot_enabled():
        now = time.monotonic()
        if last_log is None or now - last_log >= DORMANT_LOG_INTERVAL_SEC:
            logger.info(
                "dormant: %s is off — no sweeps, no store, no listener. Flip the gate to activate.",
                GATE_ENV,
            )
            last_log = now
        await asyncio.sleep(DORMANT_POLL_SEC)


async def main() -> None:
    unknown = [tf for tf in TIMEFRAMES if tf not in TF_SECONDS]
    if unknown:
        logger.error("unknown timeframes in KYTHERA_SNAPSHOT_TIMEFRAMES: %s — service cannot start.", unknown)
        return

    if not snapshot_enabled():
        logger.info("=== CANDLE SNAPSHOT SERVICE DORMANT (%s is off — nothing can query it) ===", GATE_ENV)
        await dormant_loop()
        logger.info("%s turned on — activating the snapshot service.", GATE_ENV)

    await run_service()


async def run_service() -> None:
    """The active service: warm store, refresh loops, TCP listener."""
    coins = load_coins()
    if not coins:
        logger.error("no coins loaded — service cannot start.")
        return
    _SYMBOLS.extend(coins)

    host, port = endpoint()
    logger.info(
        "=== CANDLE SNAPSHOT SERVICE START (%d coins, tf=%s, lookback %d/%d, gate ON) ===",
        len(coins),
        ",".join(TIMEFRAMES),
        CANDLE_LOOKBACK,
        INDICATOR_LOOKBACK,
    )

    server = await asyncio.start_server(handle_client, host, port)
    logger.info("TCP server listening on %s:%d", host, port)

    refresh_task = asyncio.create_task(refresh_loop())
    coin_task = asyncio.create_task(coin_refresh_loop())
    async with server:
        await asyncio.gather(server.serve_forever(), refresh_task, coin_task)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("candle snapshot service stopped.")

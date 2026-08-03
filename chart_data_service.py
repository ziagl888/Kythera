"""
chart_data_service.py — live 1min candle buffer for all coins
================================================================

This process connects to Binance Futures WebSockets and keeps an in-memory
ring buffer of the last 300 1-minute candles for every known coin
(~5h history).

The chart-generating bots (EPD, SR, MIS, ATS, RUB, ATB, Master, ABR1,
Quasimodo, SMC Sniper, IP Pattern) fetch the data here instead of directly
from fapi.binance.com — this makes them independent of Binance rate limits
and DNS problems.

Architecture
------------
- Multiple WebSocket workers (max. 200 streams each, Binance limit)
- One TCP server on localhost:5555 for chart client requests
- Periodic snapshots as JSON on disk (chart_buffer_snapshot.json),
  so the buffer has data again immediately after a restart

Protocol (line-based JSON, newline-terminated)
-----------------------------------------------
Request:   {"cmd": "get", "symbol": "BTCUSDT", "minutes": 240}\n
Response:  {"symbol": "BTCUSDT", "candles": [[open_time_ms, open, high, low, close, volume], ...]}\n
Error:     {"symbol": "BTCUSDT", "error": "not_available"}\n
Health:    {"cmd": "health"} → {"status": "ok", "symbols_tracked": 537, "uptime_sec": 12345}\n

WebSocket message (Binance kline 1m):
  {"stream": "btcusdt@kline_1m", "data": {"E": ..., "k": {"t": ..., "T": ...,
   "s": "BTCUSDT", "i": "1m", "o": "42000.5", "c": "42100.0", "h": ..., "l": ...,
   "v": ..., "x": true/false}}}

Only FINALISED candles (x=true) are taken into the buffer. The latest,
still-forming candle changes constantly and would make rendering unnecessarily unstable.

Start
-----
    py chart_data_service.py

Logs: logs/chart_data_service.log
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import deque
from typing import Any

# Dependencies
import websockets

# Own config (for coin list)
# Important: the service runs in the project root, so all imports should work
from core.logging_setup import setup_logging
from core.market_utils import load_coins as _core_load_coins
from core.ws_utils import apply_keepalive as _apply_keepalive

logger = setup_logging("CHART_SERVICE")

# ─── Configuration ────────────────────────────────────────────────────────────
# If the port is changed via .env, read it here
CHART_SERVICE_PORT = int(os.getenv("CHART_SERVICE_PORT", "5555"))
CHART_SERVICE_HOST = os.getenv("CHART_SERVICE_HOST", "127.0.0.1")

COINS_FILE = "coins.json"
SNAPSHOT_FILE = "chart_buffer_snapshot.json"
# P2.20: the ~12MB snapshot ran synchronously on the event loop; 60s was too tight.
# With the to_thread dump (see save_snapshot) and a 300s interval, the
# snapshot no longer blocks the WS consumers — a 5min loss window is uncritical, the
# buffer is only used as warm-start history at startup anyway.
SNAPSHOT_INTERVAL_SEC = 300  # snapshot every 5min

# P2.20: message watchdog. Binance can accept a connection and go silent
# (no error, just 0 messages). Without a timeout, `async for msg in ws` hangs forever,
# without ever reconnecting. >120s without a message despite an open connection = dead.
CHART_WS_MESSAGE_WATCHDOG_SEC = 120

# P2.15: coins.json is updated at runtime by 6_housekeeping (daily 03:00 UTC).
# Without a re-read, newly listed coins would get no chart data until the
# process restart. Re-read every 300s and add new symbols additively.
COIN_REFRESH_INTERVAL_SEC = 300

BUFFER_SIZE = 300  # 300 × 1min = 5h history per coin
STREAMS_PER_WS = 600  # Binance Futures limit: 1024 streams per connection
# (300 is the connect-attempt limit per 5 min per IP — different thing).
# 600 fits all ~537 USDT coins comfortably in a single connection.
# Binance migration 23.04.2026: kline = /market stream, routed URL mandatory.
BINANCE_WS_URL = "wss://fstream.binance.com/market/stream?streams="

# ─── In-memory buffer ─────────────────────────────────────────────────────────
# Structure: { "BTCUSDT": deque([[open_time_ms, open, high, low, close, volume], ...]) }
_BUFFERS: dict[str, deque] = {}
_BUFFER_LOCK = asyncio.Lock()  # protection for concurrent read/write
_START_TIME = time.time()

# P2.15: additive coin refresh. Symbols already assigned to a WS worker;
# running worker ID sequence across the initial fleet + refresh workers.
_TRACKED_SYMBOLS: set[str] = set()
_ws_worker_counter = 0


def _next_worker_id() -> int:
    global _ws_worker_counter
    _ws_worker_counter += 1
    return _ws_worker_counter


def compute_new_symbols(current: set[str], tracked: set[str]) -> list[str]:
    """New, not-yet-tracked symbols (additive, sorted).

    Conservative: an empty ``current`` (torn/empty coins.json read) results in no
    change — coins are never removed and we never react to a broken read.
    load_coins() returns [] on a broken read (all-or-nothing json.load).
    """
    if not current:
        return []
    return sorted(current - tracked)


# ─── Load coin list ────────────────────────────────────────────────────────────


def load_coins() -> list[str]:
    """Reads coins.json and returns the sorted, de-duplicated USDT symbols (upper-case).

    Delegates to core.market_utils.load_coins (P3.1 consolidation) for the
    read/dict-unwrap/USDT-filter/symbol-validation; the sort+dedup on top is
    this service's own contract.
    """
    return sorted(set(_core_load_coins(COINS_FILE, usdt_only=True, uppercase=True)))


# ─── Snapshot persistence ──────────────────────────────────────────────────────


async def save_snapshot() -> None:
    """Serialises all buffers as JSON to disk.

    Uses atomic write (tmp + rename) so a half-written snapshot does not
    lead to a broken load on the next start.

    P2.20: the ~12MB JSON dump + os.replace ran synchronously on the event loop and
    blocked the WS consumers + the TCP server every 60s. Now only the consistent
    buffer snapshot is briefly copied under the lock (fast, shallow
    reference copy); the serialisation + the disk write run in the thread. The
    candle lists are never mutated in-place (only appended), so the thread reads
    a stable snapshot while the loop keeps pushing candles into the deques.
    """
    async with _BUFFER_LOCK:
        snapshot = {sym: list(buf) for sym, buf in _BUFFERS.items() if len(buf) > 0}

    await asyncio.to_thread(_write_snapshot_to_disk, snapshot)


def _write_snapshot_to_disk(snapshot: dict[str, list]) -> None:
    """Blocking JSON dump + atomic rename — runs in the thread, never on the event loop."""
    tmp = SNAPSHOT_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, separators=(",", ":"))  # compact, ~12MB
        os.replace(tmp, SNAPSHOT_FILE)
        size_mb = os.path.getsize(SNAPSHOT_FILE) / 1024 / 1024
        logger.debug(f"Snapshot saved ({len(snapshot)} symbols, {size_mb:.1f}MB)")
    except Exception as e:
        logger.error(f"Snapshot save failed: {e}")


def load_snapshot() -> None:
    """Loads the last snapshot into the buffers. Robust against missing/broken files."""
    if not os.path.exists(SNAPSHOT_FILE):
        logger.info("No previous snapshot — buffers starting empty.")
        return

    try:
        with open(SNAPSHOT_FILE, encoding="utf-8") as f:
            snapshot = json.load(f)
    except Exception as e:
        logger.warning(f"Snapshot load failed ({e}), starting with empty buffers.")
        return

    loaded = 0
    for symbol, candles in snapshot.items():
        if not isinstance(candles, list):
            continue
        buf = deque(maxlen=BUFFER_SIZE)
        for c in candles:
            # Validate every candle (6 numbers)
            if isinstance(c, list) and len(c) == 6:
                buf.append(c)
        if len(buf) > 0:
            _BUFFERS[symbol] = buf
            loaded += 1

    logger.info(f"Snapshot loaded: {loaded} symbols with history restored.")


# ─── WebSocket worker ─────────────────────────────────────────────────────────


async def ws_worker(worker_id: int, symbols: list[str]) -> None:
    """A WebSocket worker listens on up to STREAMS_PER_WS streams.

    On disconnect, reconnects with exponential backoff.
    """
    # Binance-WS-URL: combined streams via "&streams="
    stream_names = [f"{s.lower()}@kline_1m" for s in symbols]
    url = BINANCE_WS_URL + "/".join(stream_names)

    backoff = 5.0
    while True:
        try:
            logger.info(f"WS-Worker {worker_id}: connecting ({len(symbols)} streams)...")
            async with websockets.connect(url, ping_interval=None, ping_timeout=None, open_timeout=30) as ws:
                _apply_keepalive(ws)
                logger.info(f"✅ WS worker {worker_id} connected.")
                backoff = 5.0  # reset after successful connect

                # Unsolicited pong every 120s — keepalive safety net
                async def _chart_pong_task():
                    while True:
                        await asyncio.sleep(120)
                        try:
                            await ws.pong()
                        except Exception:
                            break

                pong_task = asyncio.create_task(_chart_pong_task())
                try:
                    await _consume_with_watchdog(ws, worker_id)
                finally:
                    pong_task.cancel()
                    try:
                        await pong_task
                    except (asyncio.CancelledError, Exception):
                        pass

        except websockets.ConnectionClosed as e:
            logger.warning(f"🔴 WS worker {worker_id} disconnected ({e}). Reconnect in {backoff:.0f}s...")
        except Exception as e:
            logger.warning(f"🔴 WS worker {worker_id} error ({type(e).__name__}: {e}). Reconnect in {backoff:.0f}s...")

        spread = (worker_id - 1) * 2.0
        await asyncio.sleep(backoff + spread)
        backoff = min(backoff * 2.0, 300.0)  # exponential backoff, cap 300s


async def _consume_with_watchdog(ws, worker_id: int) -> None:
    """Reads messages until timeout or close and then returns (caller reconnects).

    P2.20: instead of ``async for msg in ws`` (blocks forever if Binance accepts
    the connection but goes silent) every message is fetched with
    ``asyncio.wait_for(ws.recv(), CHART_WS_MESSAGE_WATCHDOG_SEC)``. If every message
    stays absent longer than the watchdog window, the connection is considered dead
    and the function returns → the ws_worker leaves the ``async with`` and
    reconnects with backoff. A ConnectionClosed from ``recv()`` propagates as
    before to the ws_worker handler.
    """
    last_msg = time.time()
    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=CHART_WS_MESSAGE_WATCHDOG_SEC)
        except asyncio.TimeoutError:
            silence = time.time() - last_msg
            logger.warning(f"⏰ WS worker {worker_id}: {silence:.0f}s no messages — forcing reconnect.")
            return
        last_msg = time.time()
        try:
            await _process_ws_message(msg)
        except Exception as e:
            logger.debug(f"WS-Worker {worker_id} message-error: {e}")


async def _process_ws_message(raw_msg: str | bytes) -> None:
    """Processes a single WebSocket message."""
    try:
        msg = json.loads(raw_msg)
    except json.JSONDecodeError:
        return

    data = msg.get("data", {})
    k = data.get("k", {})
    if not k:
        return

    # Only take finalised candles
    if not k.get("x"):
        return

    symbol = k.get("s")
    if not symbol:
        return

    try:
        candle = [
            int(k["t"]),  # open_time (ms)
            float(k["o"]),  # open
            float(k["h"]),  # high
            float(k["l"]),  # low
            float(k["c"]),  # close
            float(k["v"]),  # volume
        ]
    except (KeyError, ValueError, TypeError):
        return

    async with _BUFFER_LOCK:
        if symbol not in _BUFFERS:
            _BUFFERS[symbol] = deque(maxlen=BUFFER_SIZE)

        # Anti-dup: if this open_time is already present (old candles could be
        # in there due to a snapshot reload), skip
        buf = _BUFFERS[symbol]
        if buf and buf[-1][0] >= candle[0]:
            return

        buf.append(candle)


# ─── TCP server ────────────────────────────────────────────────────────────────


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Handler per TCP client connection. Reads one line of request, responds, closes."""
    addr = writer.get_extra_info("peername")
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not line:
            return

        try:
            req = json.loads(line.decode("utf-8").strip())
        except json.JSONDecodeError as e:
            response: dict[str, Any] = {"error": f"invalid json: {e}"}
            writer.write((json.dumps(response) + "\n").encode("utf-8"))
            await writer.drain()
            return

        cmd = req.get("cmd")

        if cmd == "health":
            async with _BUFFER_LOCK:
                n_symbols = len(_BUFFERS)
                n_with_data = sum(1 for b in _BUFFERS.values() if len(b) > 0)
            response = {
                "status": "ok",
                "symbols_tracked": n_symbols,
                "symbols_with_data": n_with_data,
                "uptime_sec": int(time.time() - _START_TIME),
            }

        elif cmd == "get":
            symbol = req.get("symbol", "").upper()
            minutes = int(req.get("minutes", 240))

            async with _BUFFER_LOCK:
                buf = _BUFFERS.get(symbol)
                if not buf or len(buf) < 2:
                    response = {"symbol": symbol, "error": "not_available"}
                else:
                    # Last N candles, but at most as many as available
                    candles = list(buf)[-minutes:]
                    response = {"symbol": symbol, "candles": candles}

        else:
            response = {"error": f"unknown cmd: {cmd}"}

        writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))
        await writer.drain()

    except asyncio.TimeoutError:
        logger.debug(f"Client {addr} Timeout")
    except Exception as e:
        logger.debug(f"Client {addr} Error: {e}")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ─── Orchestration ─────────────────────────────────────────────────────────────


async def snapshot_loop() -> None:
    """Writes the current buffer state to disk every SNAPSHOT_INTERVAL_SEC."""
    while True:
        await asyncio.sleep(SNAPSHOT_INTERVAL_SEC)
        try:
            await save_snapshot()
        except Exception as e:
            logger.error(f"Snapshot-Loop-Error: {e}")


async def coin_refresh_loop() -> None:
    """P2.15: additively pulls in coins newly appeared in coins.json (own WS worker).

    Conservative: never tear down streams for removed coins (that is left to the
    restart) — a coin missing due to a false negative (torn/empty coins.json read)
    must not drop out of the chart buffer live. compute_new_symbols() holds this invariant.
    """
    while True:
        await asyncio.sleep(COIN_REFRESH_INTERVAL_SEC)
        try:
            new = compute_new_symbols(set(load_coins()), _TRACKED_SYMBOLS)
            if not new:
                continue
            logger.info(f"🆕 {len(new)} new coins in coins.json — chart streams are being added.")
            for i in range(0, len(new), STREAMS_PER_WS):
                chunk = new[i : i + STREAMS_PER_WS]
                wid = _next_worker_id()
                asyncio.create_task(ws_worker(wid, chunk))
                logger.info(f"🆕 Chart WS worker {wid} started for {len(chunk)} new coins.")
            _TRACKED_SYMBOLS.update(new)
        except Exception as e:
            logger.error(f"Coin refresh error: {e}")


async def main() -> None:
    # 1. Load snapshot (if present)
    load_snapshot()

    # 2. Coins from coins.json
    coins = load_coins()
    if not coins:
        logger.error("No coins loaded — service cannot start.")
        return
    logger.info(f"=== 📊 CHART DATA SERVICE START ({len(coins)} Coins) ===")
    _TRACKED_SYMBOLS.update(coins)

    # 3. Start WebSocket workers (chunked)
    ws_tasks = []
    for i in range(0, len(coins), STREAMS_PER_WS):
        chunk = coins[i : i + STREAMS_PER_WS]
        worker_id = _next_worker_id()
        ws_tasks.append(asyncio.create_task(ws_worker(worker_id, chunk)))
        if i > 0:
            await asyncio.sleep(3.0)  # stagger: avoid simultaneous connects
    logger.info(f"📡 {len(ws_tasks)} WebSocket-Worker started.")

    # 4. Start TCP server
    server = await asyncio.start_server(handle_client, CHART_SERVICE_HOST, CHART_SERVICE_PORT)
    addr = server.sockets[0].getsockname()
    logger.info(f"🔌 TCP server listening on {addr[0]}:{addr[1]}")

    # 5. Snapshot loop + coin refresh loop
    snapshot_task = asyncio.create_task(snapshot_loop())
    refresh_task = asyncio.create_task(coin_refresh_loop())

    async with server:
        # Server runs until KeyboardInterrupt
        await asyncio.gather(server.serve_forever(), *ws_tasks, snapshot_task, refresh_task)


if __name__ == "__main__":
    # Windows: SelectorEventLoopPolicy for asyncio stability
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Chart Data Service manually stopped. Final snapshot...")
        # One more final snapshot before exit
        try:
            asyncio.run(save_snapshot())
        except Exception:
            pass
        logger.info("✅ Clean shutdown.")

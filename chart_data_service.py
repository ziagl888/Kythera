"""
chart_data_service.py — Live 1min-Kerzen-Buffer für alle Coins
================================================================

Dieser Prozess verbindet sich mit Binance Futures-WebSockets und hält für jeden
bekannten Coin einen In-Memory-Ringbuffer der letzten 300 1-Minuten-Kerzen
(~5h Historie).

Die Chart-generierenden Bots (EPD, SR, MIS, ATS, RUB, ATB, Master, ABR1,
Quasimodo, SMC Sniper, IP Pattern) holen die Daten hier statt direkt von
fapi.binance.com — das macht sie unabhängig von Binance-Rate-Limits und
DNS-Problemen.

Architektur
-----------
- Mehrere WebSocket-Worker (je max. 200 Streams, Binance-Limit)
- Ein TCP-Server auf localhost:5555 für Chart-Client-Requests
- Periodische Snapshots als JSON auf Disk (chart_buffer_snapshot.json),
  damit der Buffer after Restart sofort wieder Daten hat

Protokoll (Line-based JSON, newline-terminiert)
-----------------------------------------------
Request:   {"cmd": "get", "symbol": "BTCUSDT", "minutes": 240}\n
Response:  {"symbol": "BTCUSDT", "candles": [[open_time_ms, open, high, low, close, volume], ...]}\n
Error:     {"symbol": "BTCUSDT", "error": "not_available"}\n
Health:    {"cmd": "health"} → {"status": "ok", "symbols_tracked": 537, "uptime_sec": 12345}\n

WebSocket-Message (Binance kline 1m):
  {"stream": "btcusdt@kline_1m", "data": {"E": ..., "k": {"t": ..., "T": ...,
   "s": "BTCUSDT", "i": "1m", "o": "42000.5", "c": "42100.0", "h": ..., "l": ...,
   "v": ..., "x": true/false}}}

Nur FINALISIERTE Kerzen (x=true) werden in den Buffer übernommen. Die letzte,
laufende Kerze ändert sich ständig und würde Rendering unnötig instabil machen.

Start
-----
    py chart_data_service.py

Logs: logs/chart_data_service.log
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from collections import deque
from typing import Any

# Dependencies
import websockets

# Eigene Config (für Coin-Liste)
# Wichtig: Der Service läuft im Projekt-Root, also sollten alle Imports funktionieren
from core.logging_setup import setup_logging

logger = setup_logging("CHART_SERVICE")

# ─── Konfiguration ──────────────────────────────────────────────────────────
# Falls der Port via .env geändert wird, hier auslesen
CHART_SERVICE_PORT = int(os.getenv("CHART_SERVICE_PORT", "5555"))
CHART_SERVICE_HOST = os.getenv("CHART_SERVICE_HOST", "127.0.0.1")

COINS_FILE = "coins.json"
SNAPSHOT_FILE = "chart_buffer_snapshot.json"
SNAPSHOT_INTERVAL_SEC = 60  # alle 60s Snapshot

BUFFER_SIZE = 300  # 300 × 1min = 5h Historie pro Coin
STREAMS_PER_WS = 600  # Binance Futures limit: 1024 streams per connection
# (300 is the connect-attempt limit per 5 min per IP — different thing).
# 600 fits all ~537 USDT coins comfortably in a single connection.
# Binance-Migration 23.04.2026: kline = /market-Stream, geroutete URL Pflicht.
BINANCE_WS_URL = "wss://fstream.binance.com/market/stream?streams="

# ─── In-Memory-Buffer ────────────────────────────────────────────────────────
# Struktur: { "BTCUSDT": deque([[open_time_ms, open, high, low, close, volume], ...]) }
_BUFFERS: dict[str, deque] = {}
_BUFFER_LOCK = asyncio.Lock()  # Schutz bei gleichzeitigem Read/Write
_START_TIME = time.time()

# ─── Coin-Liste laden ────────────────────────────────────────────────────────


def load_coins() -> list[str]:
    """Reads coins.json and returns a list of symbols (lowercase for WS)."""
    try:
        with open(COINS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        coins = data.get("coins", data) if isinstance(data, dict) else data
        # Nur USDT-Paare, lowercase für Binance-WS
        return sorted({c.upper() for c in coins if c.upper().endswith("USDT")})
    except Exception as e:
        logger.error(f"Could not load coins.json: {e}")
        return []


# ─── Snapshot-Persistenz ─────────────────────────────────────────────────────


async def save_snapshot() -> None:
    """Serialisiert alle Buffers als JSON auf Disk.

    Nutzt atomic write (tmp + rename), damit ein halb-geschriebener Snapshot
    beim nächsten Start nicht zu einem kaputten Load führt.
    """
    async with _BUFFER_LOCK:
        snapshot = {sym: list(buf) for sym, buf in _BUFFERS.items() if len(buf) > 0}

    tmp = SNAPSHOT_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, separators=(",", ":"))  # kompakt, ~12MB
        os.replace(tmp, SNAPSHOT_FILE)
        size_mb = os.path.getsize(SNAPSHOT_FILE) / 1024 / 1024
        logger.debug(f"Snapshot saved ({len(snapshot)} symbols, {size_mb:.1f}MB)")
    except Exception as e:
        logger.error(f"Snapshot save failed: {e}")


def load_snapshot() -> None:
    """Lädt den letzten Snapshot in die Buffers. Robust gegen fehlende/kaputte Files."""
    if not os.path.exists(SNAPSHOT_FILE):
        logger.info("No previous snapshot — buffers starting empty.")
        return

    try:
        with open(SNAPSHOT_FILE, encoding="utf-8") as f:
            snapshot = json.load(f)
    except Exception as e:
        logger.warning(f"Snapshot load failed ({e}), starting mit leeren Buffers.")
        return

    loaded = 0
    for symbol, candles in snapshot.items():
        if not isinstance(candles, list):
            continue
        buf = deque(maxlen=BUFFER_SIZE)
        for c in candles:
            # Validiere jede Kerze (6 Zahlen)
            if isinstance(c, list) and len(c) == 6:
                buf.append(c)
        if len(buf) > 0:
            _BUFFERS[symbol] = buf
            loaded += 1

    logger.info(f"Snapshot geladen: {loaded} Symbole mit Historie wiederhergestellt.")


# ─── WebSocket-Worker ────────────────────────────────────────────────────────


def _apply_keepalive(ws) -> None:
    """Applies TCP keepalive to an already-connected WebSocket.

    Called AFTER websockets.connect() succeeds. Gets the underlying socket
    from the transport and sets SO_KEEPALIVE + platform-specific intervals.
    This avoids the Windows WinError 10057 that occurs when passing an
    unconnected socket to websockets.connect(sock=...).

    Prevents NAT/firewall idle-timeout disconnects (~300-360s) by sending
    TCP-level ACK probes every 60s.
    """
    import sys

    try:
        sock = ws.transport.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if sys.platform == "win32":
            # SIO_KEEPALIVE_VALS: (onoff, keepalivetime_ms, keepaliveinterval_ms)
            # First probe after 60s idle, then every 10s
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60_000, 10_000))
        else:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6)
    except (AttributeError, OSError):
        pass  # Non-fatal — connection still works without keepalive


async def ws_worker(worker_id: int, symbols: list[str]) -> None:
    """Ein WebSocket-Worker hört auf bis zu STREAMS_PER_WS Streams.

    Bei Disconnect wird mit exponentiellem Backoff neu verbunden.
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
                logger.info(f"✅ WS-Worker {worker_id} verbunden.")
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
                    async for msg in ws:
                        try:
                            await _process_ws_message(msg)
                        except Exception as e:
                            logger.debug(f"WS-Worker {worker_id} message-error: {e}")
                finally:
                    pong_task.cancel()
                    try:
                        await pong_task
                    except (asyncio.CancelledError, Exception):
                        pass

        except websockets.ConnectionClosed as e:
            logger.warning(f"🔴 WS-Worker {worker_id} getrennt ({e}). Reconnect in {backoff:.0f}s...")
        except Exception as e:
            logger.warning(f"🔴 WS-Worker {worker_id} Fehler ({type(e).__name__}: {e}). Reconnect in {backoff:.0f}s...")

        spread = (worker_id - 1) * 2.0
        await asyncio.sleep(backoff + spread)
        backoff = min(backoff * 2.0, 300.0)  # exponential backoff, cap 300s


async def _process_ws_message(raw_msg: str | bytes) -> None:
    """Verarbeitet eine einzelne WebSocket-Nachricht."""
    try:
        msg = json.loads(raw_msg)
    except json.JSONDecodeError:
        return

    data = msg.get("data", {})
    k = data.get("k", {})
    if not k:
        return

    # Nur finalisierte Kerzen übernehmen
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

        # Anti-Dup: falls diese open_time schon da ist (durch Snapshot-Reload
        # könnten alte Kerzen drin sein), skippingn
        buf = _BUFFERS[symbol]
        if buf and buf[-1][0] >= candle[0]:
            return

        buf.append(candle)


# ─── TCP-Server ──────────────────────────────────────────────────────────────


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Handler pro TCP-Client-Verbindung. Liest eine Zeile Request, antwortet, schließt."""
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
                    # Letzte N Kerzen, aber maximal so viele wie vorhanden
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


# ─── Orchestrierung ──────────────────────────────────────────────────────────


async def snapshot_loop() -> None:
    """Schreibt alle SNAPSHOT_INTERVAL_SEC den aktuellen Buffer-Stand after Disk."""
    while True:
        await asyncio.sleep(SNAPSHOT_INTERVAL_SEC)
        try:
            await save_snapshot()
        except Exception as e:
            logger.error(f"Snapshot-Loop-Error: {e}")


async def main() -> None:
    # 1. Snapshot laden (wenn vorhanden)
    load_snapshot()

    # 2. Coins aus coins.json
    coins = load_coins()
    if not coins:
        logger.error("Keine Coins geladen — Service kann nicht starten.")
        return
    logger.info(f"=== 📊 CHART DATA SERVICE START ({len(coins)} Coins) ===")

    # 3. WebSocket-Worker starten (chunked)
    ws_tasks = []
    for i in range(0, len(coins), STREAMS_PER_WS):
        chunk = coins[i : i + STREAMS_PER_WS]
        worker_id = i // STREAMS_PER_WS + 1
        ws_tasks.append(asyncio.create_task(ws_worker(worker_id, chunk)))
        if i > 0:
            await asyncio.sleep(3.0)  # stagger: avoid simultaneous connects
    logger.info(f"📡 {len(ws_tasks)} WebSocket-Worker started.")

    # 4. TCP-Server starten
    server = await asyncio.start_server(handle_client, CHART_SERVICE_HOST, CHART_SERVICE_PORT)
    addr = server.sockets[0].getsockname()
    logger.info(f"🔌 TCP-Server hört auf {addr[0]}:{addr[1]}")

    # 5. Snapshot-Loop
    snapshot_task = asyncio.create_task(snapshot_loop())

    async with server:
        # Server läuft bis KeyboardInterrupt
        await asyncio.gather(server.serve_forever(), *ws_tasks, snapshot_task)


if __name__ == "__main__":
    # Windows: SelectorEventLoopPolicy für asyncio-Stabilität
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Chart Data Service manuell stopped. Letzter Snapshot...")
        # Noch einen letzten Snapshot vor dem Exit
        try:
            asyncio.run(save_snapshot())
        except Exception:
            pass
        logger.info("✅ Shutdown sauber.")

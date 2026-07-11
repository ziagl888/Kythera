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
import sys
import time
from collections import deque
from typing import Any

# Dependencies
import websockets

# Eigene Config (für Coin-Liste)
# Wichtig: Der Service läuft im Projekt-Root, also sollten alle Imports funktionieren
from core.logging_setup import setup_logging
from core.market_utils import load_coins as _core_load_coins
from core.ws_utils import apply_keepalive as _apply_keepalive

logger = setup_logging("CHART_SERVICE")

# ─── Konfiguration ──────────────────────────────────────────────────────────
# Falls der Port via .env geändert wird, hier auslesen
CHART_SERVICE_PORT = int(os.getenv("CHART_SERVICE_PORT", "5555"))
CHART_SERVICE_HOST = os.getenv("CHART_SERVICE_HOST", "127.0.0.1")

COINS_FILE = "coins.json"
SNAPSHOT_FILE = "chart_buffer_snapshot.json"
# P2.20: der ~12MB-Snapshot lief synchron auf dem Event-Loop; 60s war zu dicht.
# Mit dem to_thread-Dump (siehe save_snapshot) und 300s-Intervall blockiert der
# Snapshot die WS-Consumer nicht mehr — 5min Verlust-Fenster ist unkritisch, der
# Buffer wird beim Start ohnehin nur als Warmstart-Historie genutzt.
SNAPSHOT_INTERVAL_SEC = 300  # alle 5min Snapshot

# P2.20: Message-Watchdog. Binance kann eine Connection annehmen und stumm lassen
# (kein Fehler, nur 0 Messages). Ohne Timeout haengt `async for msg in ws` ewig,
# ohne je zu reconnecten. >120s ohne Message trotz offener Connection = tot.
CHART_WS_MESSAGE_WATCHDOG_SEC = 120

# P2.15: coins.json wird zur Laufzeit von 6_housekeeping (taeglich 03:00 UTC)
# aktualisiert. Ohne Re-Read bekaemen neu gelistete Coins bis zum Prozess-Restart
# keine Chart-Daten. Alle 300s neu lesen und neue Symbole additiv nachziehen.
COIN_REFRESH_INTERVAL_SEC = 300

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

# P2.15: additiver Coin-Refresh. Symbole, die bereits einem WS-Worker zugeteilt
# sind; laufende Worker-ID-Sequenz ueber initialen Fleet + Refresh-Worker hinweg.
_TRACKED_SYMBOLS: set[str] = set()
_ws_worker_counter = 0


def _next_worker_id() -> int:
    global _ws_worker_counter
    _ws_worker_counter += 1
    return _ws_worker_counter


def compute_new_symbols(current: set[str], tracked: set[str]) -> list[str]:
    """Neue, noch nicht getrackte Symbole (additiv, sortiert).

    Konservativ: ein leeres ``current`` (torn/leerer coins.json-Read) ergibt keine
    Aenderung — es werden nie Coins entfernt und nie auf einem kaputten Read
    reagiert. load_coins() liefert bei kaputtem Read [] (all-or-nothing json.load).
    """
    if not current:
        return []
    return sorted(current - tracked)


# ─── Coin-Liste laden ────────────────────────────────────────────────────────


def load_coins() -> list[str]:
    """Reads coins.json and returns the sorted, de-duplicated USDT symbols (upper-case).

    Delegates to core.market_utils.load_coins (P3.1 consolidation) for the
    read/dict-unwrap/USDT-filter/symbol-validation; the sort+dedup on top is
    this service's own contract.
    """
    return sorted(set(_core_load_coins(COINS_FILE, usdt_only=True, uppercase=True)))


# ─── Snapshot-Persistenz ─────────────────────────────────────────────────────


async def save_snapshot() -> None:
    """Serialisiert alle Buffers als JSON auf Disk.

    Nutzt atomic write (tmp + rename), damit ein halb-geschriebener Snapshot
    beim nächsten Start nicht zu einem kaputten Load führt.

    P2.20: der ~12MB-JSON-Dump + os.replace liefen synchron auf dem Event-Loop und
    blockierten alle 60s die WS-Consumer + den TCP-Server. Nur der konsistente
    Buffer-Snapshot wird jetzt kurz unter dem Lock kopiert (schnell, flache
    Referenz-Kopie); die Serialisierung + der Disk-Write laufen im Thread. Die
    Candle-Listen werden nie in-place mutiert (nur appended), der Thread liest also
    einen stabilen Snapshot, waehrend der Loop weiter Kerzen in die Deques schiebt.
    """
    async with _BUFFER_LOCK:
        snapshot = {sym: list(buf) for sym, buf in _BUFFERS.items() if len(buf) > 0}

    await asyncio.to_thread(_write_snapshot_to_disk, snapshot)


def _write_snapshot_to_disk(snapshot: dict[str, list]) -> None:
    """Blocking JSON-Dump + atomic rename — laeuft im Thread, nie auf dem Event-Loop."""
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
                    await _consume_with_watchdog(ws, worker_id)
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


async def _consume_with_watchdog(ws, worker_id: int) -> None:
    """Liest Messages bis Timeout oder Close und kehrt dann zurueck (Caller reconnectet).

    P2.20: statt ``async for msg in ws`` (blockiert ewig, wenn Binance die
    Connection annimmt aber stumm laesst) wird jede Message mit
    ``asyncio.wait_for(ws.recv(), CHART_WS_MESSAGE_WATCHDOG_SEC)`` geholt. Bleibt
    laenger als das Watchdog-Fenster jede Message aus, gilt die Connection als tot
    und die Funktion kehrt zurueck → der ws_worker verlaesst den ``async with`` und
    reconnectet mit Backoff. Ein ConnectionClosed aus ``recv()`` propagiert wie
    gehabt zum ws_worker-Handler.
    """
    last_msg = time.time()
    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=CHART_WS_MESSAGE_WATCHDOG_SEC)
        except asyncio.TimeoutError:
            silence = time.time() - last_msg
            logger.warning(f"⏰ WS-Worker {worker_id}: {silence:.0f}s keine Messages — erzwinge Reconnect.")
            return
        last_msg = time.time()
        try:
            await _process_ws_message(msg)
        except Exception as e:
            logger.debug(f"WS-Worker {worker_id} message-error: {e}")


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


async def coin_refresh_loop() -> None:
    """P2.15: zieht neu in coins.json aufgetauchte Coins additiv nach (eigener WS-Worker).

    Konservativ: nie Streams fuer entfernte Coins abbauen (das bleibt dem Restart)
    — ein faelschlich (torn/leerer coins.json-Read) fehlender Coin darf nicht live
    aus dem Chart-Buffer fallen. compute_new_symbols() haelt diese Invariante.
    """
    while True:
        await asyncio.sleep(COIN_REFRESH_INTERVAL_SEC)
        try:
            new = compute_new_symbols(set(load_coins()), _TRACKED_SYMBOLS)
            if not new:
                continue
            logger.info(f"🆕 {len(new)} neue Coins in coins.json — Chart-Streams werden nachgezogen.")
            for i in range(0, len(new), STREAMS_PER_WS):
                chunk = new[i : i + STREAMS_PER_WS]
                wid = _next_worker_id()
                asyncio.create_task(ws_worker(wid, chunk))
                logger.info(f"🆕 Chart-WS-Worker {wid} fuer {len(chunk)} neue Coins gestartet.")
            _TRACKED_SYMBOLS.update(new)
        except Exception as e:
            logger.error(f"Coin-Refresh-Fehler: {e}")


async def main() -> None:
    # 1. Snapshot laden (wenn vorhanden)
    load_snapshot()

    # 2. Coins aus coins.json
    coins = load_coins()
    if not coins:
        logger.error("Keine Coins geladen — Service kann nicht starten.")
        return
    logger.info(f"=== 📊 CHART DATA SERVICE START ({len(coins)} Coins) ===")
    _TRACKED_SYMBOLS.update(coins)

    # 3. WebSocket-Worker starten (chunked)
    ws_tasks = []
    for i in range(0, len(coins), STREAMS_PER_WS):
        chunk = coins[i : i + STREAMS_PER_WS]
        worker_id = _next_worker_id()
        ws_tasks.append(asyncio.create_task(ws_worker(worker_id, chunk)))
        if i > 0:
            await asyncio.sleep(3.0)  # stagger: avoid simultaneous connects
    logger.info(f"📡 {len(ws_tasks)} WebSocket-Worker started.")

    # 4. TCP-Server starten
    server = await asyncio.start_server(handle_client, CHART_SERVICE_HOST, CHART_SERVICE_PORT)
    addr = server.sockets[0].getsockname()
    logger.info(f"🔌 TCP-Server hört auf {addr[0]}:{addr[1]}")

    # 5. Snapshot-Loop + Coin-Refresh-Loop
    snapshot_task = asyncio.create_task(snapshot_loop())
    refresh_task = asyncio.create_task(coin_refresh_loop())

    async with server:
        # Server läuft bis KeyboardInterrupt
        await asyncio.gather(server.serve_forever(), *ws_tasks, snapshot_task, refresh_task)


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

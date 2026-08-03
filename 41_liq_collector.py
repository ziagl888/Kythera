# 41_liq_collector.py — Liquidations collector (LQE1, T-2026-KYT-9050-077)
#
# Own lightweight process (separate failure domain, 35_oi_collector pattern):
# holds the Binance futures websocket stream `!forceOrder@arr` (market-wide
# forced liquidations) and writes every event batched into the hypertable
# `liq_events` (core/liq_events.py). Purpose: ground truth for calibrating
# the estimated liquidation heatmap (tools/mps1_liq_heatmap.py, MPS1).
#
# TIME-CRITICAL as a collector: Binance offers NO REST endpoint for historical
# liquidations (allForceOrders was removed in 2021) — whatever the stream does
# not capture live is irretrievably lost (the same lesson as
# ticker_10s and K9/oi_5m).
#
# Data contract (must be documented): Binance throttles the stream to
# a maximum of ONE order per second PER SYMBOL — the table is a SAMPLE,
# not a full census (details in the header of core/liq_events.py). No rate-
# limit budget needed: a single stream, no polling.
#
# Connection lifecycle: Binance hard-terminates websocket streams after 24h —
# the outer reconnect loop with backoff is therefore normal operation, not
# an error case. The websockets library answers server pings automatically;
# a recv timeout only serves as the flush cadence (liquidations are sparse,
# quiet minutes without an event are normal).
#
# Persistence batching: events are buffered and written with ONE insert every
# FLUSH_INTERVAL_S (or once FLUSH_MAX_ROWS is reached) (WAL churn, P1.40).
# The DB connection is pulled from the pool PER FLUSH (checkout liveness
# P1.33 replaces dead connections after a DB restart — oi_collector pattern).
# If a flush fails, the buffer is kept and retried on the next
# cadence — capped at BUFFER_CAP rows (then the oldest are dropped with an
# ERROR log: bounded data loss instead of unbounded memory).
#
# Kill switch: KYTHERA_LIQ_PERSIST=0 (default on). Persistence is the ONLY
# job of this process — at 0 it keeps idling supervised (watchdog-quiet),
# instead of exiting (exiting would trigger the crash backoff loop).
#
# Registration: core/fleet.py (group=logger, start_delay=279). The watchdog
# reads FLEET at import — the NEW fleet entry is only supervised after a
# watchdog restart (= fleet intervention ⇒ operator/Michi, like K9).
#
# The price-check-only exception (R1) does not apply: forceOrder events are
# completed orders, not forming candles.

import json
import os
import time

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from core import config as _kcfg  # noqa: F401 — loads .env (DB access), fleet convention
from core import liq_events
from core.database import db_connection
from core.logging_setup import setup_logging

logger = setup_logging("LIQ_COLLECTOR")

LIQ_PERSIST = os.getenv("KYTHERA_LIQ_PERSIST", "1") == "1"

WS_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"

RECV_TIMEOUT_S = 30.0  # Flush-Takt in ruhigen Phasen; Keepalive macht die Library
FLUSH_INTERVAL_S = 10.0
FLUSH_MAX_ROWS = 500
BUFFER_CAP = 10_000  # harter Speicher-Deckel bei anhaltend toter DB
RECONNECT_BACKOFF_START_S = 2.0
RECONNECT_BACKOFF_CAP_S = 120.0


def _lower_process_priority() -> None:
    """VPS runs at the load limit — collector runs with BELOW_NORMAL (K9 pattern)."""
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        logger.info("Process priority: BELOW_NORMAL")
    except Exception as e:
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.kernel32.SetPriorityClass(handle, 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
            logger.info("Process priority: BELOW_NORMAL (ctypes)" if ok else f"⚠️ SetPriorityClass failed ({e})")
        except Exception:
            logger.warning(f"⚠️ Priority reduction failed ({e}) — continuing with normal priority.")


class _Flusher:
    """Buffer + time-/size-driven batched insert.

    Invariants:
      * flush() never raises — a failed insert leaves the buffer standing for
        the next cadence (the collector loop must never die).
      * The buffer never exceeds BUFFER_CAP: overflow drops the
        OLDEST rows with an ERROR log (bounded, visible data loss).
      * ensure_schema runs lazily on the first successful flush and is
        retried after failures (DB is still booting → next cadence).
    """

    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.last_flush = time.monotonic()
        self.schema_ok = False
        self.total_inserted = 0

    def add(self, row: tuple) -> None:
        self.rows.append(row)
        if len(self.rows) > BUFFER_CAP:
            dropped = len(self.rows) - BUFFER_CAP
            del self.rows[:dropped]
            logger.error(f"Buffer overflow: {dropped} oldest liquidation rows dropped (DB dead?)")

    def due(self) -> bool:
        if not self.rows:
            return False
        return len(self.rows) >= FLUSH_MAX_ROWS or time.monotonic() - self.last_flush >= FLUSH_INTERVAL_S

    def flush(self) -> None:
        self.last_flush = time.monotonic()
        if not self.rows:
            return
        batch = self.rows
        try:
            with db_connection() as conn:
                if not self.schema_ok:
                    liq_events.ensure_schema(conn)
                    self.schema_ok = True
                liq_events.insert_liq(conn, batch)
            self.rows = []
            self.total_inserted += len(batch)
        except Exception as e:
            # Keep the buffer (add() caps it), next cadence retries.
            logger.error(f"Flush failed ({len(batch)} rows, retry next cadence): {e}")


def _stream_once(flusher: _Flusher) -> None:
    """Hold one websocket connection until close (Binance: max. 24h).

    Raises ConnectionClosed/OSError to the reconnect loop; catch everything
    else per message — one malformed event must never cost the connection.
    """
    with connect(WS_URL, open_timeout=15) as ws:
        logger.info(f"Connected: {WS_URL}")
        while True:
            try:
                raw = ws.recv(timeout=RECV_TIMEOUT_S)
            except TimeoutError:
                # Quiet phase — only serve the flush cadence.
                if flusher.due():
                    flusher.flush()
                continue
            try:
                row = liq_events.row_from_force_order(json.loads(raw))
            except Exception as e:
                # Broad catch by design: a single frame — no matter how
                # broken — must never cost the connection (flush philosophy).
                logger.error(f"Unparseable websocket frame dropped: {e}")
                row = None
            if row is not None:
                flusher.add(row)
            if flusher.due():
                flusher.flush()


def main() -> None:
    logger.info("=== 💥 LIQ COLLECTOR START (LQE1) ===")
    _lower_process_priority()

    if not LIQ_PERSIST:
        # Kill switch: idle supervised instead of exiting (see header comment).
        logger.warning("KYTHERA_LIQ_PERSIST=0 — collector idling without persistence.")
        while True:
            time.sleep(300)
            logger.info("Idle (KYTHERA_LIQ_PERSIST=0).")

    flusher = _Flusher()
    backoff = RECONNECT_BACKOFF_START_S
    while True:
        connected_at = time.monotonic()
        try:
            _stream_once(flusher)
        except ConnectionClosed as e:
            lived_s = time.monotonic() - connected_at
            if lived_s >= 60.0:
                # 24h rotation or a later network hiccup — normal operation.
                logger.info(
                    f"Connection closed after {lived_s:.0f}s ({e.rcvd or e.sent or 'no close frame'}) — reconnecting."
                )
                backoff = RECONNECT_BACKOFF_START_S
            else:
                # Immediate close (reject/ban/network issue): treat like an error,
                # otherwise the loop hammers the endpoint once per second.
                logger.warning(f"Connection closed after only {lived_s:.0f}s — backoff {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP_S)
        except Exception as e:
            if time.monotonic() - connected_at >= 60.0:
                backoff = RECONNECT_BACKOFF_START_S  # long-lived connection → fresh backoff
            logger.error(f"Stream error: {e} — reconnecting in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP_S)
            continue
        finally:
            # Save whatever is in the buffer before/after every connection end —
            # flush() never raises (invariant), not here either.
            flusher.flush()
        time.sleep(1.0)  # gentle reconnect spacing in the normal case


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Liq Collector manually stopped (Ctrl+C).")

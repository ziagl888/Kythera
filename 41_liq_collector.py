# 41_liq_collector.py — Liquidations-Collector (LQE1, T-2026-KYT-9050-077)
#
# Eigener schlanker Prozess (getrennte Failure-Domain, 35_oi_collector-Muster):
# hält den Binance-Futures-Websocket-Stream `!forceOrder@arr` (marktweite
# Zwangsliquidationen) und schreibt jedes Event batched in die Hypertable
# `liq_events` (core/liq_events.py). Zweck: Ground-Truth für die Kalibrierung
# der geschätzten Liquidations-Heatmap (tools/mps1_liq_heatmap.py, MPS1).
#
# ZEITKRITISCH als Sammler: Binance bietet KEINEN REST-Endpoint für historische
# Liquidationen (allForceOrders wurde 2021 entfernt) — was der Stream nicht
# live einsammelt, ist unwiederbringlich verloren (dieselbe Lektion wie
# ticker_10s und K9/oi_5m).
#
# Daten-Contract (dokumentationspflichtig): Binance drosselt den Stream auf
# maximal EINE Order pro Sekunde PRO SYMBOL — die Tabelle ist ein SAMPLE,
# keine Vollerhebung (Details im Kopf von core/liq_events.py). Kein Rate-
# Limit-Budget nötig: ein einzelner Stream, kein Polling.
#
# Verbindungs-Lifecycle: Binance beendet Websocket-Streams hart nach 24h —
# der äußere Reconnect-Loop mit Backoff ist daher Normalbetrieb, kein
# Fehlerfall. Die websockets-Library beantwortet Server-Pings automatisch;
# ein recv-Timeout dient nur als Flush-Takt (Liquidationen sind sparse,
# ruhige Minuten ohne Event sind normal).
#
# Persistenz-Batching: Events werden gepuffert und alle FLUSH_INTERVAL_S
# (oder ab FLUSH_MAX_ROWS) mit EINEM Insert geschrieben (WAL-Churn, P1.40).
# Die DB-Connection wird PRO FLUSH aus dem Pool gezogen (Checkout-Liveness
# P1.33 ersetzt tote Connections nach DB-Restart — oi_collector-Muster).
# Schlägt ein Flush fehl, bleibt der Puffer erhalten und wird beim nächsten
# Takt erneut versucht — gedeckelt auf BUFFER_CAP Rows (dann älteste mit
# ERROR-Log verwerfen: begrenzter Datenverlust statt unbegrenztem Speicher).
#
# Kill-Switch: KYTHERA_LIQ_PERSIST=0 (Default an). Persistenz ist der EINZIGE
# Job dieses Prozesses — bei 0 idlet er supervised weiter (Watchdog-ruhig),
# statt sich zu beenden (Exit würde die Crash-Backoff-Schleife triggern).
#
# Registrierung: core/fleet.py (group=logger, start_delay=279). Der Watchdog
# liest FLEET beim Import — der NEUE Fleet-Eintrag wird erst nach einem
# Watchdog-Restart supervised (= Fleet-Eingriff ⇒ Operator/Michi, wie K9).
#
# Nur-Preis-Check-Ausnahme (R1) greift nicht: forceOrder-Events sind
# abgeschlossene Orders, keine forming Candles.

import json
import os
import time

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from core import config as _kcfg  # noqa: F401 — lädt .env (DB-Zugang), Konvention der Fleet
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
    """VPS läuft an der Lastgrenze — Collector läuft mit BELOW_NORMAL (K9-Muster)."""
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        logger.info("Prozess-Priorität: BELOW_NORMAL")
    except Exception as e:
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.kernel32.SetPriorityClass(handle, 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
            logger.info(
                "Prozess-Priorität: BELOW_NORMAL (ctypes)" if ok else f"⚠️ SetPriorityClass fehlgeschlagen ({e})"
            )
        except Exception:
            logger.warning(f"⚠️ Prioritäts-Absenkung fehlgeschlagen ({e}) — laufe mit Normal-Priorität weiter.")


class _Flusher:
    """Puffer + zeit-/größengesteuerter batched Insert.

    Invariants:
      * flush() wirft nie — ein fehlgeschlagener Insert lässt den Puffer für
        den nächsten Takt stehen (Collector-Loop darf nie sterben).
      * Der Puffer überschreitet BUFFER_CAP nie: Überlauf verwirft die
        ÄLTESTEN Rows mit ERROR-Log (begrenzter, sichtbarer Datenverlust).
      * ensure_schema läuft lazy beim ersten erfolgreichen Flush und wird
        nach Fehlern erneut versucht (DB bootet noch → nächster Takt).
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
            logger.error(f"Puffer-Überlauf: {dropped} älteste Liquidations-Rows verworfen (DB tot?)")

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
            # Puffer behalten (add() deckelt), nächster Takt versucht erneut.
            logger.error(f"Flush fehlgeschlagen ({len(batch)} Rows, Retry nächster Takt): {e}")


def _stream_once(flusher: _Flusher) -> None:
    """Eine Websocket-Verbindung bis zum Close halten (Binance: max. 24h).

    Wirft ConnectionClosed/OSError an den Reconnect-Loop; alles andere pro
    Message abfangen — ein malformtes Event darf die Verbindung nie kosten.
    """
    with connect(WS_URL, open_timeout=15) as ws:
        logger.info(f"Verbunden: {WS_URL}")
        while True:
            try:
                raw = ws.recv(timeout=RECV_TIMEOUT_S)
            except TimeoutError:
                # Ruhige Phase — nur den Flush-Takt bedienen.
                if flusher.due():
                    flusher.flush()
                continue
            try:
                row = liq_events.row_from_force_order(json.loads(raw))
            except Exception as e:
                # Breiter Fang mit Absicht: ein einzelnes Frame — egal wie
                # kaputt — darf die Verbindung nie kosten (Flush-Philosophie).
                logger.error(f"Unparsebares Websocket-Frame verworfen: {e}")
                row = None
            if row is not None:
                flusher.add(row)
            if flusher.due():
                flusher.flush()


def main() -> None:
    logger.info("=== 💥 LIQ COLLECTOR START (LQE1) ===")
    _lower_process_priority()

    if not LIQ_PERSIST:
        # Kill-Switch: supervised idlen statt beenden (s. Kopfkommentar).
        logger.warning("KYTHERA_LIQ_PERSIST=0 — Collector idlet ohne Persistenz.")
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
                # 24h-Rotation oder später Netz-Hickup — Normalbetrieb.
                logger.info(
                    f"Verbindung nach {lived_s:.0f}s geschlossen ({e.rcvd or e.sent or 'ohne Close-Frame'}) — Reconnect."
                )
                backoff = RECONNECT_BACKOFF_START_S
            else:
                # Sofort-Close (Reject/Ban/Netzproblem): wie Fehler behandeln,
                # sonst hämmert der Loop im Sekundentakt gegen den Endpoint.
                logger.warning(f"Verbindung nach nur {lived_s:.0f}s geschlossen — Backoff {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP_S)
        except Exception as e:
            if time.monotonic() - connected_at >= 60.0:
                backoff = RECONNECT_BACKOFF_START_S  # lange gelebte Verbindung → frischer Backoff
            logger.error(f"Stream-Fehler: {e} — Reconnect in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP_S)
            continue
        finally:
            # Was im Puffer liegt, vor/nach jedem Verbindungsende sichern —
            # flush() wirft nie (Invariant), auch hier nicht.
            flusher.flush()
        time.sleep(1.0)  # sanfter Reconnect-Abstand im Normalfall


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Liq Collector manuell gestoppt (Strg+C).")

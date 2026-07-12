# 35_oi_collector.py — Open-Interest-Collector (K9/OIC, T-2026-CU-9050-103)
#
# Eigener schlanker Prozess (getrennte Failure-Domain — bewusst KEIN Anbau an
# den Pump-Dump-Detector): alle 5 Minuten ein Sweep über die coins.json-Symbole
# via GET /futures/data/openInterestHist (period=5m, limit=1), EIN batched
# Insert in die Hypertable `oi_5m` (core/oi_5m.py). ZEITKRITISCH als Sammler:
# Binance-REST hält nur ~30 Tage OI-Historie — jeder Tag ohne Collector ist
# unwiederbringlich verlorene Historie (dieselbe Lektion wie ticker_10s).
#
# Endpoint-Wahl (Spec K9 lässt beide zu): openInterestHist statt
# /fapi/v1/openInterest, weil nur der Hist-Endpoint `sumOpenInterestValue`
# (USDT-Bewertung → Spalte oi_value_usdt) liefert und seine Timestamps auf das
# 5m-Raster gestempelt sind — damit dedupliziert ON CONFLICT (ts, symbol)
# gegen Doppelstart und Backfill-Überlappung wirklich (identische Keys statt
# now()-Jitter, das ticker_10s-Floor-Argument).
#
# Rate-Budget (Spec K9, dokumentationspflichtig): ~530 Symbole × 1 Request pro
# 5-min-Sweep = ~530 req/5min. Die /futures/data/*-Endpoints tragen ein
# IP-Limit von 1000 Requests/5min (getrennt vom 2400-weight/min-Budget der
# /fapi/*-Endpoints) — wir bleiben mit REQUEST_SPACING_S ≈ 0,3s deutlich
# darunter und verteilen die Requests über den Sweep statt zu bursten.
# 429/418 laufen über core/http_retry (Retry-After respektieren, 418 nie unter
# 120s, ein 418 bricht den ganzen Sweep ab statt weiterzuhämmern — P2.14).
#
# Kill-Switch: KYTHERA_OI_PERSIST=0 (Default an). Da Persistenz der EINZIGE
# Job dieses Prozesses ist, idlet er bei 0 supervised weiter (Watchdog-ruhig),
# statt sich zu beenden (Exit würde die Crash-Backoff-Schleife triggern).
#
# Registrierung: core/fleet.py (group=logger, start_delay=231). Der Watchdog
# liest FLEET beim Import — ein NEUER Fleet-Eintrag wird erst nach einem
# Watchdog-Restart supervised (= Fleet-Eingriff ⇒ Operator/Michi, Spec K9 §4).
#
# Nur-Preis-Check-Ausnahme (R1) greift hier nicht: openInterestHist liefert
# abgeschlossene 5m-Perioden-Snapshots, keine forming Candles.

import os
import time

import requests

from core import config as _kcfg  # noqa: F401 — lädt .env (DB-Zugang), Konvention der Fleet
from core import oi_5m
from core.database import db_connection
from core.http_retry import RetryBudget, backoff_seconds
from core.logging_setup import setup_logging
from core.market_utils import load_coins
from core.time import utc_now

logger = setup_logging("OI_COLLECTOR")

OI_PERSIST = os.getenv("KYTHERA_OI_PERSIST", "1") == "1"

BASE_URL = "https://fapi.binance.com"
HIST_ENDPOINT = "/futures/data/openInterestHist"

SWEEP_INTERVAL_S = 300  # 5m-Raster — Takt der openInterestHist-Punkte
# Nach der 5m-Marke warten, bis Binance den frisch geschlossenen Punkt
# publiziert hat — ein Sweep exakt AUF der Marke sähe noch die Vorperiode.
SWEEP_OFFSET_S = 20
REQUEST_SPACING_S = 0.3  # ~530 req über ~160s verteilt, kein Burst (s. Rate-Budget oben)
REQUEST_TIMEOUT_S = 10


class _SweepAborted(Exception):
    """418 (IP-Ban-Eskalation): Sweep sofort abbrechen, Backoff schlafen."""

    def __init__(self, wait_s: float) -> None:
        super().__init__(f"sweep aborted, backoff {wait_s:.0f}s")
        self.wait_s = wait_s


def _sleep_until_next_sweep() -> None:
    """Schläft bis zur nächsten 5m-Marke + SWEEP_OFFSET_S (UTC-Raster)."""
    now_epoch = utc_now().timestamp()
    next_mark = (int(now_epoch) // SWEEP_INTERVAL_S + 1) * SWEEP_INTERVAL_S
    time.sleep(max(next_mark + SWEEP_OFFSET_S - now_epoch, 1.0))


def _fetch_latest_point(session: requests.Session, symbol: str) -> list[tuple]:
    """Holt den jüngsten 5m-OI-Punkt eines Symbols (limit=1, Spec K9).

    Gebudgeteter Retry nach core/http_retry (Muster (b): jeder Versuch zählt).
    Liefert [] wenn das Budget erschöpft ist — ein fehlender Punkt ist ein
    akzeptierter Datenpunkt-Verlust, der Sweep läuft weiter. 418 eskaliert als
    _SweepAborted an den Sweep (weiterhämmern verlängert den Ban, P2.14).
    """
    budget = RetryBudget(max_attempts=2, deadline_s=30.0)
    consecutive = 0
    while budget.attempt():
        try:
            resp = session.get(
                BASE_URL + HIST_ENDPOINT,
                params={"symbol": symbol, "period": "5m", "limit": 1},
                timeout=REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as e:
            consecutive += 1
            logger.warning(f"{symbol}: Netzwerkfehler ({e}) — Versuch {budget.attempts}/{budget.max_attempts}")
            time.sleep(backoff_seconds(None, consecutive))
            continue
        if resp.status_code == 418:
            raise _SweepAborted(backoff_seconds(418, 1, resp.headers.get("Retry-After")))
        if resp.status_code == 429:
            consecutive += 1
            wait_s = backoff_seconds(429, consecutive, resp.headers.get("Retry-After"))
            logger.warning(f"{symbol}: 429 — {wait_s:.0f}s Backoff")
            time.sleep(wait_s)
            continue
        if resp.status_code != 200:
            consecutive += 1
            logger.warning(f"{symbol}: HTTP {resp.status_code}")
            time.sleep(backoff_seconds(None, consecutive))
            continue
        try:
            payload = resp.json()
        except ValueError as e:
            consecutive += 1
            logger.warning(f"{symbol}: JSON-Parse-Fehler ({e})")
            time.sleep(backoff_seconds(None, consecutive))
            continue
        return oi_5m.rows_from_hist_payload(symbol, payload)
    logger.warning(f"{symbol}: Punkt verworfen ({budget.exhausted_reason()})")
    return []


def _run_sweep(session: requests.Session, conn) -> tuple[int, int]:
    """Ein kompletter 5m-Sweep über coins.json. Liefert (rows, symbole)."""
    # Pro Sweep frisch laden: das Universum ändert sich (Listings/Delistings,
    # Housekeeping schreibt coins.json nächtlich neu) — billig bei 5m-Takt.
    coins = load_coins()
    if not coins:
        logger.error("Keine Coins aus coins.json — Sweep übersprungen.")
        return 0, 0
    rows: list[tuple] = []
    aborted: _SweepAborted | None = None
    try:
        for symbol in coins:
            rows.extend(_fetch_latest_point(session, symbol))
            time.sleep(REQUEST_SPACING_S)
    except _SweepAborted as e:
        # Bereits gefetchte Punkte VOR dem Ban-Backoff persistieren — der
        # einzige Job dieses Prozesses ist lückenlose Historie; die Rows der
        # restlichen Symbole sind ohnehin verloren, diese hier nicht.
        aborted = e
    # EIN batched Insert pro Sweep (alle Coins) — nie den Loop stoppen, ein
    # verlorener Sweep ist akzeptabel, ein toter Collector nicht.
    oi_5m.insert_oi(conn, rows)
    if aborted is not None:
        raise aborted
    return len(rows), len(coins)


def _lower_process_priority() -> None:
    """VPS läuft an der Lastgrenze — Collector läuft mit BELOW_NORMAL (Spec K9)."""
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        logger.info("Prozess-Priorität: BELOW_NORMAL")
    except Exception as e:
        # ctypes-Fallback direkt auf die WinAPI (walkforward_sim-Muster) —
        # falls psutil im Prozess-venv fehlt.
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.kernel32.SetPriorityClass(handle, 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
            logger.info(
                "Prozess-Priorität: BELOW_NORMAL (ctypes)" if ok else f"⚠️ SetPriorityClass fehlgeschlagen ({e})"
            )
        except Exception:
            logger.warning(f"⚠️ Prioritäts-Absenkung fehlgeschlagen ({e}) — laufe mit Normal-Priorität weiter.")


def main() -> None:
    logger.info("=== 📊 OI COLLECTOR START (K9/OIC) ===")
    _lower_process_priority()

    if not OI_PERSIST:
        # Kill-Switch: supervised idlen statt beenden (s. Kopfkommentar).
        logger.warning("KYTHERA_OI_PERSIST=0 — Collector idlet ohne Persistenz.")
        while True:
            time.sleep(SWEEP_INTERVAL_S)
            logger.info("Idle (KYTHERA_OI_PERSIST=0).")

    schema_ok = False
    session = requests.Session()

    while True:
        _sleep_until_next_sweep()
        try:
            t0 = time.monotonic()
            # Connection PRO SWEEP aus dem Pool ziehen statt einmal beim Start
            # und für immer halten: der Checkout-Liveness-Check (P1.33) ersetzt
            # nach einem DB-Restart tote Connections — eine gehaltene Connection
            # bliebe dauerhaft kaputt und der Collector wäre "alive but dead"
            # (loggt weiter, sammelt nie wieder — die P2.47-Fehlerklasse).
            with db_connection() as conn:
                # Schema lazy + retry: schlägt das Setup fehl (DB bootet noch,
                # Extension fehlt), beim nächsten Sweep erneut versuchen statt
                # in die Watchdog-Crash-Backoff-Schleife zu exiten.
                if not schema_ok:
                    oi_5m.ensure_schema(conn)
                    schema_ok = True
                n_rows, n_coins = _run_sweep(session, conn)
            logger.info(f"✅ Sweep: {n_rows}/{n_coins} OI-Punkte persistiert ({time.monotonic() - t0:.0f}s)")
        except _SweepAborted as e:
            logger.error(f"🚫 418 vom Binance-Endpoint — Sweep abgebrochen, {e.wait_s:.0f}s Backoff.")
            time.sleep(e.wait_s)
        except Exception as e:
            # Auch DB-Fehler (insert_oi rollt selbst zurück und re-raised)
            # landen hier: Sweep verloren, Loop lebt weiter.
            logger.error(f"Sweep fehlgeschlagen (Datenpunkte verworfen): {e}", exc_info=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 OI Collector manuell gestoppt (Strg+C).")

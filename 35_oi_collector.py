# 35_oi_collector.py — open-interest collector (K9/OIC, T-2026-CU-9050-103)
#
# Own slim process (separate failure domain — deliberately NO attachment to the
# pump-dump detector): every 5 minutes a sweep over the coins.json symbols via
# GET /futures/data/openInterestHist (period=5m, limit=1), ONE batched insert
# into the hypertable `oi_5m` (core/oi_5m.py). TIME-CRITICAL as collector:
# Binance REST holds only ~30 days of OI history — each day without collector is
# irrecoverably lost history (same lesson as ticker_10s).
#
# Endpoint choice (spec K9 allows both): openInterestHist rather than
# /fapi/v1/openInterest, because only the hist endpoint delivers
# `sumOpenInterestValue` (USDT valuation → oi_value_usdt column) and its
# timestamps are gridded to 5m — so ON CONFLICT (ts, symbol) truly deduplicates
# against double-start and backfill overlap (identical keys instead of now()-jitter,
# the ticker_10s-floor argument).
#
# Rate budget (spec K9, documentation required): ~530 symbols × 1 request per
# 5-min sweep = ~530 req/5min. The /futures/data/* endpoints carry an IP limit
# of 1000 requests/5min (separate from the 2400-weight/min budget of /fapi/*
# endpoints) — we stay well below with REQUEST_SPACING_S ≈ 0.3s and distribute
# requests over the sweep instead of bursting. 429/418 go through core/http_retry
# (respect retry-after, 418 never below 120s, one 418 aborts the whole sweep
# instead of continuing to hammer — P2.14).
#
# Kill-switch: KYTHERA_OI_PERSIST=0 (default on). Since persistence is the ONLY
# job of this process, it idles supervised when 0 (watchdog-quiet) instead of
# exiting (exit would trigger the crash-backoff loop).
#
# Registration: core/fleet.py (group=logger, start_delay=231). Watchdog reads
# FLEET at import — a NEW fleet entry is only supervised after a watchdog
# restart (= fleet intervention ⇒ operator/Michi, spec K9 §4).
#
# Price-only-check exception (R1) does not apply here: openInterestHist delivers
# completed 5m-period snapshots, no forming candles.

import os
import time

import requests

from core import config as _kcfg  # noqa: F401 — loads .env (DB access), fleet convention
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

SWEEP_INTERVAL_S = 300  # 5m grid — cadence of openInterestHist points
# Wait after the 5m mark until Binance publishes the newly closed point — a
# sweep exactly AT the mark would still see the prior period.
SWEEP_OFFSET_S = 20
REQUEST_SPACING_S = 0.3  # ~530 req spread over ~160s, no burst (see rate budget above)
REQUEST_TIMEOUT_S = 10


class _SweepAborted(Exception):
    """418 (IP ban escalation): abort sweep immediately, backoff sleep."""

    def __init__(self, wait_s: float) -> None:
        super().__init__(f"sweep aborted, backoff {wait_s:.0f}s")
        self.wait_s = wait_s


def _sleep_until_next_sweep() -> None:
    """Sleeps until next 5m mark + SWEEP_OFFSET_S (UTC grid)."""
    now_epoch = utc_now().timestamp()
    next_mark = (int(now_epoch) // SWEEP_INTERVAL_S + 1) * SWEEP_INTERVAL_S
    time.sleep(max(next_mark + SWEEP_OFFSET_S - now_epoch, 1.0))


def _fetch_latest_point(session: requests.Session, symbol: str) -> list[tuple]:
    """Fetches the most recent 5m-OI point for a symbol (limit=1, spec K9).

    Budgeted retry per core/http_retry (pattern (b): each attempt counts).
    Returns [] if budget is exhausted — a missing point is an accepted data loss,
    sweep continues. 418 escalates as _SweepAborted to sweep (continuing to
    hammer extends the ban, P2.14).
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
            logger.warning(f"{symbol}: Network error ({e}) — attempt {budget.attempts}/{budget.max_attempts}")
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
            logger.warning(f"{symbol}: JSON parse error ({e})")
            time.sleep(backoff_seconds(None, consecutive))
            continue
        return oi_5m.rows_from_hist_payload(symbol, payload)
    logger.warning(f"{symbol}: Point discarded ({budget.exhausted_reason()})")
    return []


def _run_sweep(session: requests.Session, conn) -> tuple[int, int]:
    """A complete 5m sweep over coins.json. Returns (rows, symbols)."""
    # Load fresh per sweep: the universe changes (listings/delistings,
    # housekeeping rewrites coins.json nightly) — cheap at 5m cadence.
    coins = load_coins()
    if not coins:
        logger.error("No coins from coins.json — sweep skipped.")
        return 0, 0
    rows: list[tuple] = []
    aborted: _SweepAborted | None = None
    try:
        for symbol in coins:
            rows.extend(_fetch_latest_point(session, symbol))
            time.sleep(REQUEST_SPACING_S)
    except _SweepAborted as e:
        # Persist points already fetched BEFORE the ban backoff — the only job
        # of this process is unbroken history; the rows of the remaining symbols
        # are lost anyway, not these.
        aborted = e
    # ONE batched insert per sweep (all coins) — never stop the loop, a lost
    # sweep is acceptable, a dead collector is not.
    oi_5m.insert_oi(conn, rows)
    if aborted is not None:
        raise aborted
    return len(rows), len(coins)


def _lower_process_priority() -> None:
    """VPS runs at the load limit — collector runs at BELOW_NORMAL (spec K9)."""
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        logger.info("Process priority: BELOW_NORMAL")
    except Exception as e:
        # ctypes fallback directly to WinAPI (walkforward_sim pattern) — in
        # case psutil is missing from the process venv.
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.kernel32.SetPriorityClass(handle, 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
            logger.info("Process priority: BELOW_NORMAL (ctypes)" if ok else f"⚠️ SetPriorityClass failed ({e})")
        except Exception:
            logger.warning(f"⚠️ Priority lowering failed ({e}) — running at normal priority.")


def main() -> None:
    logger.info("=== 📊 OI COLLECTOR START (K9/OIC) ===")
    _lower_process_priority()

    if not OI_PERSIST:
        # Kill-switch: supervised idle instead of exit (see header).
        logger.warning("KYTHERA_OI_PERSIST=0 — collector idles without persistence.")
        while True:
            time.sleep(SWEEP_INTERVAL_S)
            logger.info("Idle (KYTHERA_OI_PERSIST=0).")

    schema_ok = False
    session = requests.Session()

    while True:
        _sleep_until_next_sweep()
        try:
            t0 = time.monotonic()
            # Pull connection PER SWEEP from pool instead of once at start and
            # hold forever: the checkout-liveness check (P1.33) replaces dead
            # connections after a DB restart — a held connection would remain
            # permanently broken and the collector would be 'alive but dead'
            # (logs continue, never collects again — the P2.47 failure class).
            with db_connection() as conn:
                # Schema lazy + retry: if setup fails (DB still booting,
                # extension missing), retry at next sweep instead of exiting
                # into the watchdog-crash-backoff loop.
                if not schema_ok:
                    oi_5m.ensure_schema(conn)
                    schema_ok = True
                n_rows, n_coins = _run_sweep(session, conn)
            logger.info(f"✅ Sweep: {n_rows}/{n_coins} OI points persisted ({time.monotonic() - t0:.0f}s)")
        except _SweepAborted as e:
            logger.error(f"🚫 418 from Binance endpoint — sweep aborted, {e.wait_s:.0f}s backoff.")
            time.sleep(e.wait_s)
        except Exception as e:
            # Also DB errors (insert_oi rolls back itself and re-raises) land
            # here: sweep lost, loop lives on.
            logger.error(f"Sweep failed (data points discarded): {e}", exc_info=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 OI Collector manually stopped (Ctrl+C).")

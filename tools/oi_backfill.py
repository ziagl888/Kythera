# tools/oi_backfill.py — one-time 30d initial backfill for hypertable oi_5m
#
# K9/OIC from docs/MODEL_CANDIDATES_SPEC_2026-07.md (T-2026-CU-9050-103), point 3:
# read the available ~30 days of `/futures/data/openInterestHist` (period=5m,
# paginated) per coins.json symbol — Binance doesn't hold more, then the running
# collector takes over (35_oi_collector.py).
#
# Operating rules (Live VPS!):
#   * Run only in a VPS session (build machine has no DB).
#   * Process priority BELOW_NORMAL; writes EXCLUSIVELY to oi_5m
#     (new table, CREATE TABLE IF NOT EXISTS — no live table touched).
#   * Idempotent: ON CONFLICT (ts, symbol) DO NOTHING — retry run and overlap
#     with already running collector are no-ops. A retry >3 days after the first
#     run may hit compressed chunks (compression policy) — Timescale 2.26 can do
#     upserts into compressed chunks, but slowly; backfill is meant as ONE-TIME
#     run directly after schema setup.
#   * Rate budget: /futures/data/* endpoints have IP limit of
#     1000 req/5min. ~530 symbols × ~18 pages (30d × 288 points / 500-page-size)
#     ≈ 9.5k requests; --spacing 0.4s ⇒ ~750 req/5min, runtime ~65 min.
#     If collector runs in parallel (+530 req/5min), increase --spacing to 0.8
#     or run backfill BEFORE collector start (recommended).
#   * 429/418 backoff via core/http_retry (418 never under 120s, P2.14).
#
# Usage (VPS, one console, respect one-job rule — not a training job, but
# CPU/IO-light and OK alongside fleet):
#   python tools/oi_backfill.py                # full run, all coins, ~30d
#   python tools/oi_backfill.py --symbols BTCUSDT ETHUSDT
#   python tools/oi_backfill.py --dry-run      # fetch/count only, no DB contact

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.http_retry import RetryBudget, backoff_seconds  # noqa: E402
from core.market_utils import load_coins  # noqa: E402
from core.oi_5m import rows_from_hist_payload  # noqa: E402
from core.time import utc_now  # noqa: E402

BASE_URL = "https://fapi.binance.com"
HIST_ENDPOINT = "/futures/data/openInterestHist"
PAGE_LIMIT = 500  # Endpoint-Maximum
PERIOD_MS = 5 * 60 * 1000
# Safety cap against endless pagination: 30d × 288 points / 500 ≈ 18 pages.
MAX_PAGES_PER_SYMBOL = 25


def lower_priority() -> None:
    """The VPS runs at load limit — we run with BELOW_NORMAL."""
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        print("Process priority: BELOW_NORMAL")
    except Exception:
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.kernel32.SetPriorityClass(handle, 0x4000)
            print("Process priority: BELOW_NORMAL (ctypes)" if ok else "WARNING: SetPriorityClass failed")
        except Exception:
            print("WARNING: Priority lowering failed — running with normal priority.")


def fetch_page(session: requests.Session, symbol: str, end_time_ms: int | None, spacing_s: float) -> list[dict]:
    """One openInterestHist page (backward via endTime). [] = done/exhausted.

    Backward pagination is self-terminating: older than ~30d, endpoint
    returns empty list — no date guessing needed.
    """
    params: dict = {"symbol": symbol, "period": "5m", "limit": PAGE_LIMIT}
    if end_time_ms is not None:
        params["endTime"] = end_time_ms
    budget = RetryBudget(max_attempts=5, deadline_s=180.0)
    consecutive = 0
    while budget.attempt():
        time.sleep(spacing_s)
        try:
            resp = session.get(BASE_URL + HIST_ENDPOINT, params=params, timeout=15)
        except requests.RequestException as e:
            consecutive += 1
            print(f"  {symbol}: Network error ({e}), backoff…")
            time.sleep(backoff_seconds(None, consecutive))
            continue
        if resp.status_code in (418, 429):
            consecutive += 1
            wait_s = backoff_seconds(resp.status_code, consecutive, resp.headers.get("Retry-After"))
            print(f"  {symbol}: HTTP {resp.status_code} — {wait_s:.0f}s backoff")
            time.sleep(wait_s)
            continue
        if resp.status_code != 200:
            consecutive += 1
            print(f"  {symbol}: HTTP {resp.status_code}")
            time.sleep(backoff_seconds(None, consecutive))
            continue
        try:
            data = resp.json()
        except ValueError:
            consecutive += 1
            time.sleep(backoff_seconds(None, consecutive))
            continue
        return data if isinstance(data, list) else []
    print(f"  {symbol}: Page abandoned ({budget.exhausted_reason()}) — symbol may have gaps.")
    return []


def backfill_symbol(session: requests.Session, conn, symbol: str, spacing_s: float, dry_run: bool) -> int:
    """Paginate available history of ONE symbol backward and insert per page."""
    from core import oi_5m

    total = 0
    end_time_ms: int | None = None  # None = newest page
    for _page in range(MAX_PAGES_PER_SYMBOL):
        payload = fetch_page(session, symbol, end_time_ms, spacing_s)
        if not payload:
            break
        rows = rows_from_hist_payload(symbol, payload)
        if not rows:
            break
        if not dry_run:
            oi_5m.insert_oi(conn, rows)
        total += len(rows)
        oldest_ms = min(int(item["timestamp"]) for item in payload)
        if len(payload) < PAGE_LIMIT:
            break  # History start reached (Binance doesn't hold older points)
        end_time_ms = oldest_ms - PERIOD_MS
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time ~30d OI backfill to oi_5m (K9/OIC)")
    parser.add_argument("--symbols", nargs="*", default=None, help="Subset instead of coins.json (e.g. BTCUSDT ETHUSDT)")
    parser.add_argument("--spacing", type=float, default=0.4, help="Seconds between requests (default 0.4)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch/count only — no DB contact (build-machine smoke)")
    args = parser.parse_args()

    lower_priority()
    symbols = args.symbols or load_coins()
    if not symbols:
        print("No symbols (coins.json empty?) — aborting.")
        sys.exit(1)

    conn = None
    if not args.dry_run:
        from core import oi_5m
        from core.database import get_db_connection

        conn = get_db_connection()
        oi_5m.ensure_schema(conn)

    print(f"Backfill for {len(symbols)} symbols (spacing={args.spacing}s, dry_run={args.dry_run}) — start {utc_now():%Y-%m-%d %H:%M:%SZ}")
    session = requests.Session()
    grand_total = 0
    t0 = time.monotonic()
    for i, symbol in enumerate(symbols, 1):
        n = backfill_symbol(session, conn, symbol, args.spacing, args.dry_run)
        grand_total += n
        if i % 25 == 0 or i == len(symbols):
            elapsed = time.monotonic() - t0
            print(f"[{i}/{len(symbols)}] {symbol}: {n} points | total {grand_total} | {elapsed / 60:.1f} min")

    print(f"DONE: {grand_total} OI points {'counted' if args.dry_run else 'persisted'} in {(time.monotonic() - t0) / 60:.1f} min")
    if conn is not None:
        conn.close()


if __name__ == "__main__":
    main()

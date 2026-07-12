# tools/oi_backfill.py — einmaliger 30d-Initial-Backfill für die Hypertable oi_5m
#
# K9/OIC aus docs/MODEL_CANDIDATES_SPEC_2026-07.md (T-2026-CU-9050-103), Punkt 3:
# die verfügbaren ~30 Tage `/futures/data/openInterestHist` (period=5m,
# paginiert) je coins.json-Symbol einlesen — mehr hält Binance nicht vor,
# danach übernimmt der laufende Collector (35_oi_collector.py).
#
# Betriebsregeln (Live-VPS!):
#   * Nur in einer VPS-Session laufen lassen (Build-Maschine hat keine DB).
#   * Prozess-Priorität BELOW_NORMAL; schreibt AUSSCHLIESSLICH in oi_5m
#     (neue Tabelle, CREATE TABLE IF NOT EXISTS — keine Live-Tabelle berührt).
#   * Idempotent: ON CONFLICT (ts, symbol) DO NOTHING — Wiederholungslauf und
#     Überlappung mit dem bereits laufenden Collector sind No-ops. Ein
#     Wiederholungslauf >3 Tage nach dem Erstlauf trifft ggf. komprimierte
#     Chunks (Compression-Policy) — Timescale 2.26 kann Upserts in
#     komprimierte Chunks, aber langsam; der Backfill ist als EINMALIGER
#     Lauf direkt nach dem Schema-Setup gedacht.
#   * Rate-Budget: /futures/data/*-Endpoints tragen ein IP-Limit von
#     1000 req/5min. ~530 Symbole × ~18 Seiten (30d × 288 Punkte / 500er-Seiten)
#     ≈ 9.5k Requests; --spacing 0.4s ⇒ ~750 req/5min, Laufzeit ~65 min.
#     Läuft der Collector parallel (+530 req/5min), --spacing auf 0.8 erhöhen
#     oder den Backfill VOR dem Collector-Start fahren (empfohlen).
#   * 429/418-Backoff nach core/http_retry (418 nie unter 120s, P2.14).
#
# Aufruf (VPS, eine Konsole, Ein-Job-Regel beachten — das ist zwar kein
# Trainings-Job, aber CPU/IO-arm und darf neben der Fleet laufen):
#   python tools/oi_backfill.py                # voller Lauf, alle Coins, ~30d
#   python tools/oi_backfill.py --symbols BTCUSDT ETHUSDT
#   python tools/oi_backfill.py --dry-run      # nur fetchen/zählen, kein DB-Kontakt

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
# Sicherheitskappe gegen Endlos-Paginierung: 30d × 288 Punkte / 500 ≈ 18 Seiten.
MAX_PAGES_PER_SYMBOL = 25


def lower_priority() -> None:
    """Der VPS läuft an der Lastgrenze — wir laufen mit BELOW_NORMAL."""
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        print("Prozess-Priorität: BELOW_NORMAL")
    except Exception:
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.kernel32.SetPriorityClass(handle, 0x4000)
            print("Prozess-Priorität: BELOW_NORMAL (ctypes)" if ok else "WARNUNG: SetPriorityClass fehlgeschlagen")
        except Exception:
            print("WARNUNG: Prioritäts-Absenkung fehlgeschlagen — laufe mit Normal-Priorität.")


def fetch_page(session: requests.Session, symbol: str, end_time_ms: int | None, spacing_s: float) -> list[dict]:
    """Eine openInterestHist-Seite (rückwärts via endTime). [] = fertig/erschöpft.

    Rückwärts-Paginierung ist selbst-terminierend: älter als ~30d liefert der
    Endpoint eine leere Liste — kein Datums-Raten nötig.
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
            print(f"  {symbol}: Netzwerkfehler ({e}), Backoff …")
            time.sleep(backoff_seconds(None, consecutive))
            continue
        if resp.status_code in (418, 429):
            consecutive += 1
            wait_s = backoff_seconds(resp.status_code, consecutive, resp.headers.get("Retry-After"))
            print(f"  {symbol}: HTTP {resp.status_code} — {wait_s:.0f}s Backoff")
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
    print(f"  {symbol}: Seite aufgegeben ({budget.exhausted_reason()}) — Symbol bleibt ggf. lückig.")
    return []


def backfill_symbol(session: requests.Session, conn, symbol: str, spacing_s: float, dry_run: bool) -> int:
    """Paginiert die verfügbare Historie EINES Symbols rückwärts und insertet je Seite."""
    from core import oi_5m

    total = 0
    end_time_ms: int | None = None  # None = jüngste Seite
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
            break  # Historie-Anfang erreicht (ältere Punkte hält Binance nicht vor)
        end_time_ms = oldest_ms - PERIOD_MS
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Einmaliger ~30d-OI-Backfill nach oi_5m (K9/OIC)")
    parser.add_argument("--symbols", nargs="*", default=None, help="Teilmenge statt coins.json (z.B. BTCUSDT ETHUSDT)")
    parser.add_argument("--spacing", type=float, default=0.4, help="Sekunden zwischen Requests (Default 0.4)")
    parser.add_argument("--dry-run", action="store_true", help="Nur fetchen/zählen — kein DB-Kontakt (Build-Maschinen-Smoke)")
    args = parser.parse_args()

    lower_priority()
    symbols = args.symbols or load_coins()
    if not symbols:
        print("Keine Symbole (coins.json leer?) — Abbruch.")
        sys.exit(1)

    conn = None
    if not args.dry_run:
        from core import oi_5m
        from core.database import get_db_connection

        conn = get_db_connection()
        oi_5m.ensure_schema(conn)

    print(f"Backfill für {len(symbols)} Symbole (spacing={args.spacing}s, dry_run={args.dry_run}) — Start {utc_now():%Y-%m-%d %H:%M:%SZ}")
    session = requests.Session()
    grand_total = 0
    t0 = time.monotonic()
    for i, symbol in enumerate(symbols, 1):
        n = backfill_symbol(session, conn, symbol, args.spacing, args.dry_run)
        grand_total += n
        if i % 25 == 0 or i == len(symbols):
            elapsed = time.monotonic() - t0
            print(f"[{i}/{len(symbols)}] {symbol}: {n} Punkte | gesamt {grand_total} | {elapsed / 60:.1f} min")

    print(f"FERTIG: {grand_total} OI-Punkte {'gezählt' if args.dry_run else 'persistiert'} in {(time.monotonic() - t0) / 60:.1f} min")
    if conn is not None:
        conn.close()


if __name__ == "__main__":
    main()

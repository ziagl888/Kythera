# tools/backfill_funding_rates.py
"""Backfill der Binance-Funding-Rates in die Tabelle funding_rates.

Grundlage fuer den ABR1-LONG-Forschungs-Track (Report 21 §3: neue
Informationsquellen). Funding ist — anders als Whale-Daten (WS erst seit
2026-07-04 wieder live) — vollstaendig historisch abrufbar:
GET /fapi/v1/fundingRate, 1000 Eintraege/Request, alle 8h ein Wert.

Resumierbar: pro Symbol wird ab MAX(funding_time) weitergeladen; Re-Runs
sind idempotent (ON CONFLICT DO NOTHING).

Aufruf:  python tools/backfill_funding_rates.py [--days 430] [--limit N]
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)
from core.database import get_db_connection  # noqa: E402

import json  # noqa: E402

URL = "https://fapi.binance.com/fapi/v1/fundingRate"

DDL = """
CREATE TABLE IF NOT EXISTS funding_rates (
    symbol TEXT NOT NULL,
    funding_time TIMESTAMP WITH TIME ZONE NOT NULL,
    funding_rate DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (symbol, funding_time)
);
"""


def fetch(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    rows, cursor = [], start_ms
    while cursor < end_ms:
        for attempt in range(5):
            try:
                r = requests.get(URL, params={"symbol": symbol, "startTime": cursor,
                                              "endTime": end_ms, "limit": 1000}, timeout=15)
                if r.status_code == 429:
                    wait = 30 * (attempt + 1)
                    print(f"  429 — warte {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                batch = r.json()
                break
            except requests.RequestException as e:
                if attempt == 4:
                    raise
                time.sleep(5 * (attempt + 1))
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1]["fundingTime"] + 1
        if len(batch) < 1000:
            break
        time.sleep(0.15)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=430, help="Rueckblick, wenn Symbol noch leer")
    ap.add_argument("--limit", type=int, default=None, help="nur erste N Coins (Test)")
    args = ap.parse_args()

    with open("coins.json", encoding="utf-8") as f:
        coins = json.load(f)
    if args.limit:
        coins = coins[: args.limit]

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    default_start = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)

    total = 0
    t0 = time.time()
    for i, symbol in enumerate(coins, 1):
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT EXTRACT(EPOCH FROM MAX(funding_time)) * 1000 "
                            "FROM funding_rates WHERE symbol = %s", (symbol,))
                row = cur.fetchone()
            start_ms = int(row[0]) + 1 if row and row[0] else default_start

            rows = fetch(symbol, start_ms, now_ms)
            if rows:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO funding_rates (symbol, funding_time, funding_rate) "
                        "VALUES (%s, to_timestamp(%s / 1000.0), %s) ON CONFLICT DO NOTHING",
                        [(r["symbol"], r["fundingTime"], float(r["fundingRate"])) for r in rows],
                    )
                conn.commit()
                total += len(rows)
        except Exception as e:
            conn.rollback()
            print(f"  !! {symbol}: {e}", flush=True)
        if i % 25 == 0:
            print(f"[{i}/{len(coins)}] {total} Zeilen, {time.time()-t0:.0f}s", flush=True)
        time.sleep(0.15)

    print(f"FERTIG: {total} neue Funding-Zeilen ueber {len(coins)} Coins in {time.time()-t0:.0f}s")
    conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""
tools/candles_parity.py — old per-coin tables vs. new `candles` hypertable.

Phase-2/3 gate of docs/TIMESCALE_R1_MIGRATION.md: while ingestion dual-writes,
this compares, per (symbol, tf):

  * row count in the comparison window
  * max(open_time)
  * a checksum over the OHLCV tuples in the window

Exit code 0 = no drift, 1 = drift found, 2 = usage/connection error. Meant to
run as a nightly cron on the VPS; three consecutive clean days are the gate for
the read-cutover.

DESIGN NOTES
------------
* Only CLOSED candles are compared. The forming candle changes between the two
  SELECTs (ingestion is writing into it), so including it would produce drift
  reports that are noise. The cutoff is core.candles.period_start(), the same
  clock the reader API uses.
* The comparison itself (`compare_stats`) is pure and DB-free — `--self-check`
  exercises it on synthetic data and runs anywhere, including the build machine
  which has no DB credentials.
* Floats are canonicalised to 12 significant digits before hashing: the two
  tables may go through different type paths (REAL vs double precision, see the
  open operator question P3.12) and a bit-for-bit repr comparison would flag
  every row.
* Read-only. This tool never writes to either side.

USAGE
-----
    python tools/candles_parity.py --self-check                 # no DB needed
    python tools/candles_parity.py --tf 1h --days 7             # all coins
    python tools/candles_parity.py --symbols BTCUSDT,ETHUSDT --tf 5m --days 2
    python tools/candles_parity.py --tf 1h --days 7 --json report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.candles import (  # noqa: E402
    TF_SECONDS,
    candles_table,
    period_start,
    validate_symbol,
    validate_timeframe,
)

HYPERTABLE = "candles"
_FLOAT_FMT = "{:.12g}"


# ── Pure comparison core (DB-free) ────────────────────────────────────────────


@dataclass(frozen=True)
class SideStats:
    """What one side of the comparison reports for a (symbol, tf) window."""

    rows: int
    max_open_time: datetime | None
    checksum: str


@dataclass(frozen=True)
class Drift:
    symbol: str
    tf: str
    field: str
    old: Any
    new: Any

    def __str__(self) -> str:
        return f"{self.symbol} {self.tf}: {self.field} old={self.old!r} new={self.new!r}"


def canonical_row(row: Sequence[Any]) -> str:
    """(open_time, open, high, low, close, volume) → a stable, driver-independent string."""
    ts, *ohlcv = row
    if isinstance(ts, datetime):
        ts_key = str(int(ts.astimezone(timezone.utc).timestamp()))
    else:  # pragma: no cover — defensive, psycopg2 always hands back datetimes
        ts_key = str(ts)
    parts = [ts_key]
    for v in ohlcv:
        parts.append("" if v is None else _FLOAT_FMT.format(float(v)))
    return "|".join(parts)


def checksum_rows(rows: Iterable[Sequence[Any]]) -> str:
    """Order-sensitive checksum over canonicalised OHLCV rows (feed them ASC)."""
    h = hashlib.sha256()
    for row in rows:
        h.update(canonical_row(row).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def compare_stats(symbol: str, tf: str, old: SideStats, new: SideStats) -> list[Drift]:
    """Pure diff of two SideStats. Empty list = parity."""
    drifts: list[Drift] = []
    if old.rows != new.rows:
        drifts.append(Drift(symbol, tf, "rows", old.rows, new.rows))
    if old.max_open_time != new.max_open_time:
        drifts.append(Drift(symbol, tf, "max_open_time", old.max_open_time, new.max_open_time))
    if old.checksum != new.checksum:
        drifts.append(Drift(symbol, tf, "checksum", old.checksum, new.checksum))
    return drifts


# ── DB side (VPS only) ────────────────────────────────────────────────────────

_OHLCV = "open_time, open, high, low, close, volume"


def _fetch_side(conn: Any, query: Any, params: Sequence[Any]) -> SideStats:
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return SideStats(
        rows=len(rows),
        max_open_time=rows[-1][0] if rows else None,
        checksum=checksum_rows(rows),
    )


def fetch_legacy(conn: Any, symbol: str, tf: str, start: datetime, cutoff: datetime) -> SideStats:
    from psycopg2 import sql

    query = sql.SQL("SELECT " + _OHLCV + " FROM {tbl} WHERE open_time >= %s AND open_time < %s ORDER BY open_time ASC")
    return _fetch_side(conn, query.format(tbl=sql.Identifier(candles_table(symbol, tf))), (start, cutoff))


def fetch_hyper(conn: Any, symbol: str, tf: str, start: datetime, cutoff: datetime) -> SideStats:
    from psycopg2 import sql

    query = sql.SQL(
        "SELECT " + _OHLCV + " FROM {tbl} "
        "WHERE symbol = %s AND tf = %s AND open_time >= %s AND open_time < %s "
        "ORDER BY open_time ASC"
    ).format(tbl=sql.Identifier(HYPERTABLE))
    return _fetch_side(conn, query, (symbol, tf, start, cutoff))


def compare_symbol(conn: Any, symbol: str, tf: str, days: int, now: datetime) -> list[Drift]:
    cutoff = period_start(tf, now)
    start = cutoff - timedelta(days=days)
    old = fetch_legacy(conn, symbol, tf, start, cutoff)
    new = fetch_hyper(conn, symbol, tf, start, cutoff)
    return compare_stats(symbol, tf, old, new)


# ── CLI ───────────────────────────────────────────────────────────────────────


def _load_symbols(arg: str | None) -> list[str]:
    if arg:
        return [validate_symbol(s.strip()) for s in arg.split(",") if s.strip()]
    with open("coins.json", encoding="utf-8") as fh:
        return [validate_symbol(s) for s in json.load(fh)]


def _self_check() -> int:
    """Exercise the pure core without a database. Used on the build machine."""
    t0 = datetime(2026, 7, 9, 12, tzinfo=timezone.utc)
    rows = [(t0 + timedelta(hours=i), 1.0, 2.0, 0.5, 1.5, 100.0) for i in range(3)]
    same = SideStats(len(rows), rows[-1][0], checksum_rows(rows))
    assert compare_stats("BTCUSDT", "1h", same, same) == []

    # A difference below the 12-significant-digit canonicalisation is NOT drift:
    # that is exactly the REAL-vs-double-precision noise floor (P3.12).
    noisy = [(ts, o, h, low, cl + 1e-13, v) for ts, o, h, low, cl, v in rows]
    assert checksum_rows(rows) == checksum_rows(noisy)
    # A difference above it IS drift.
    real_change = [(ts, o, h, low, cl + 0.01, v) for ts, o, h, low, cl, v in rows]
    assert checksum_rows(rows) != checksum_rows(real_change)
    assert canonical_row(rows[0]).startswith(str(int(t0.timestamp())))

    missing_last = SideStats(len(rows) - 1, rows[-2][0], checksum_rows(rows[:-1]))
    drifts = compare_stats("BTCUSDT", "1h", same, missing_last)
    assert {d.field for d in drifts} == {"rows", "max_open_time", "checksum"}, drifts

    assert period_start("1h", datetime(2026, 7, 9, 14, 37, tzinfo=timezone.utc)) == datetime(
        2026, 7, 9, 14, tzinfo=timezone.utc
    )
    print("self-check OK — comparison core behaves, no DB touched")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-check", action="store_true", help="run the DB-free assertions and exit")
    ap.add_argument("--tf", default="1h", help=f"timeframe ({'/'.join(TF_SECONDS)})")
    ap.add_argument("--days", type=int, default=7, help="comparison window in days (default 7)")
    ap.add_argument("--symbols", help="comma-separated symbols; default: all of coins.json")
    ap.add_argument("--json", dest="json_out", help="write the full report to this path")
    ap.add_argument("--max-report", type=int, default=50, help="stop printing after N drift lines")
    args = ap.parse_args(argv)

    if args.self_check:
        return _self_check()

    try:
        tf = validate_timeframe(args.tf)
        symbols = _load_symbols(args.symbols)
    except (ValueError, OSError) as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return 2

    try:
        from core.database import db_connection
    except Exception as exc:  # missing DB_PASSWORD on a machine without credentials
        print(f"no database configuration: {exc}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    drifts: list[Drift] = []
    checked = 0
    errors: list[str] = []

    with db_connection() as conn:
        for symbol in symbols:
            try:
                drifts.extend(compare_symbol(conn, symbol, tf, args.days, now))
                checked += 1
            except Exception as exc:
                conn.rollback()
                errors.append(f"{symbol} {tf}: {exc}")

    for line in [str(d) for d in drifts[: args.max_report]]:
        print(f"DRIFT  {line}")
    if len(drifts) > args.max_report:
        print(f"… {len(drifts) - args.max_report} further drift lines suppressed (--max-report)")
    for err in errors:
        print(f"ERROR  {err}", file=sys.stderr)

    print(f"\n{checked}/{len(symbols)} symbols compared on {tf}, window {args.days}d, {len(drifts)} drift finding(s)")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "generated_at": now.isoformat(),
                    "tf": tf,
                    "days": args.days,
                    "symbols_checked": checked,
                    "symbols_total": len(symbols),
                    "errors": errors,
                    "drifts": [asdict(d) for d in drifts],
                },
                fh,
                indent=2,
                default=str,
            )

    if errors and not drifts:
        return 2
    return 1 if drifts else 0


if __name__ == "__main__":
    raise SystemExit(main())

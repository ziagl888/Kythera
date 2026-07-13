#!/usr/bin/env python3
# tools/candles_backfill.py — one-shot historical copy of the per-coin candle /
# indicator tables into the `candles` / `indicators` hypertables.
#
# C-Gate Phase 2 slice 2b (T-2026-CU-9050-119, umbrella T-2026-CU-9050-018,
# D-2026-CLD-109). Complements the forward-only dual-write in core.candles:
# dual-write handles NEW rows from activation onward, this backfill copies the
# pre-flag HISTORY once. Together they make the hypertables a complete mirror,
# which the Phase-3 parity report then watches before the Phase-4 read-cutover.
#
# Contract:
#   * Idempotent — ON CONFLICT (symbol, tf, open_time) DO NOTHING, so it never
#     overwrites a row the forward dual-write already wrote (forward wins on
#     overlap) and a re-run is safe.
#   * Resumable — a (symbol, tf, kind) is recorded in a progress file after its
#     table is committed; a re-run skips finished tables.
#   * Per-row is_closed = (open_time < period_start(tf, now)). The legacy tables
#     contain the forming candle (the R1 root cause), so the design-doc §3 sketch
#     `SELECT …, true` would mislabel it. The per-row cutoff is the honest R1
#     contract and is robust even while ingestion is stale (every stale row is
#     old → is_closed true, which is correct).
#   * Indicators are copied VERBATIM (copy/cast, NOT recompute — D-2026-CLD-109
#     #4): historical indicator values keep their forming-contamination; the
#     retrain program addresses that, not this copy.
#   * symbol/tf come from the table NAME (canonical, NOT NULL), OHLCV/open_time
#     from the legacy columns.
#
# Reads stay legacy (KYTHERA_CANDLES_SOURCE) until Phase 4 — this only populates
# the otherwise-empty hypertables. Run off-peak. Default is a dry-run plan;
# --execute writes. The hypertables must already exist (core.candles_schema).

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from psycopg2 import sql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import candles as c  # noqa: E402
from core.database import db_connection  # noqa: E402
from core.time import utc_now  # noqa: E402

logger = logging.getLogger("candles_backfill")

PROGRESS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "control", "candles_backfill_progress.json")


def _load_progress() -> set[tuple[str, str, str]]:
    if not os.path.exists(PROGRESS_FILE):
        return set()
    with open(PROGRESS_FILE, encoding="utf-8") as fh:
        return {tuple(x) for x in json.load(fh).get("done", [])}


def _save_progress(done: set[tuple[str, str, str]]) -> None:
    os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"done": sorted(done)}, fh)
    os.replace(tmp, PROGRESS_FILE)


def _candles_copy_sql(symbol: str, tf: str) -> sql.Composed:
    """INSERT … SELECT copying one legacy candle table into the `candles`
    hypertable. symbol/tf are literals (table identity); is_closed is computed
    from the per-tf cutoff passed as a parameter."""
    return sql.SQL(
        "INSERT INTO candles (symbol, tf, open_time, open, high, low, close, volume, is_closed) "
        "SELECT %(symbol)s, %(tf)s, open_time, open, high, low, close, volume, (open_time < %(cutoff)s) "
        "FROM {src} ON CONFLICT (symbol, tf, open_time) DO NOTHING"
    ).format(src=sql.Identifier(c.candles_table(symbol, tf)))


def _indicators_copy_sql(conn, symbol: str, tf: str) -> sql.Composed:
    """INSERT … SELECT copying one legacy indicator table into the `indicators`
    hypertable. Payload columns are read from the catalog (the ~120 columns are
    generated at runtime), symbol/open_time handled explicitly."""
    cols = c.indicator_column_names(conn, symbol, tf)
    payload = [col for col in cols if col not in ("symbol", "open_time")]
    if not payload:
        raise ValueError(f"{c.indicators_table(symbol, tf)} has no payload columns")
    payload_idents = sql.SQL(", ").join(sql.Identifier(col) for col in payload)
    return sql.SQL(
        "INSERT INTO indicators (symbol, tf, open_time, is_closed, {cols}) "
        "SELECT %(symbol)s, %(tf)s, open_time, (open_time < %(cutoff)s), {cols} "
        "FROM {src} ON CONFLICT (symbol, tf, open_time) DO NOTHING"
    ).format(cols=payload_idents, src=sql.Identifier(c.indicators_table(symbol, tf)))


def _copy_one(conn, symbol: str, tf: str, kind: str) -> int:
    cutoff = c.period_start(tf, utc_now())
    query = _candles_copy_sql(symbol, tf) if kind == "candles" else _indicators_copy_sql(conn, symbol, tf)
    with conn.cursor() as cur:
        cur.execute(query, {"symbol": symbol, "tf": tf, "cutoff": cutoff})
        return cur.rowcount


def run(kinds: tuple[str, ...], tf_filter: str | None, symbol_filter: str | None, limit: int | None, execute: bool) -> None:
    with db_connection() as conn:
        # Guard: the hypertables must exist before we copy into them.
        for tbl in ("candles", "indicators"):
            if not c.table_exists(conn, tbl):
                raise SystemExit(f"target hypertable {tbl!r} does not exist — run core.candles_schema first")

        targets: list[tuple[str, str, str]] = []
        for kind in kinds:
            for symbol, tf, k in c.list_coin_tables(conn, tf_filter, kind=kind):
                if symbol_filter and symbol != symbol_filter:
                    continue
                targets.append((symbol, tf, k))
        targets.sort()

        done = _load_progress()
        pending = [t for t in targets if t not in done]
        if limit is not None:
            pending = pending[:limit]

        logger.info(
            "backfill plan: %d target table(s), %d already done, %d pending%s",
            len(targets), len(targets) - len([t for t in targets if t not in done]),
            len(pending), "" if execute else "  [DRY RUN — nothing written]",
        )
        if not execute:
            for symbol, tf, kind in pending[:20]:
                logger.info("  would copy %s %s (%s)", symbol, tf, kind)
            if len(pending) > 20:
                logger.info("  … and %d more", len(pending) - 20)
            logger.info("re-run with --execute to write.")
            return

        total_rows = 0
        for i, (symbol, tf, kind) in enumerate(pending, 1):
            try:
                rows = _copy_one(conn, symbol, tf, kind)
                conn.commit()  # commit per table so a crash resumes cleanly
            except Exception:
                conn.rollback()
                logger.exception("failed on %s %s (%s) — leaving it un-done for the next run", symbol, tf, kind)
                continue
            done.add((symbol, tf, kind))
            _save_progress(done)
            total_rows += rows
            if i % 100 == 0 or i == len(pending):
                logger.info("  [%d/%d] copied %s %s (%s): %d rows", i, len(pending), symbol, tf, kind, rows)
        logger.info("backfill done: %d table(s), %d row(s) copied", len(pending), total_rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C-Gate Phase 2 slice 2b: copy per-coin history into the hypertables.")
    parser.add_argument("--execute", action="store_true", help="write (default: dry-run plan only)")
    parser.add_argument("--kind", choices=("candles", "indicators", "both"), default="both")
    parser.add_argument("--tf", default=None, help="restrict to one timeframe")
    parser.add_argument("--symbol", default=None, help="restrict to one symbol")
    parser.add_argument("--limit", type=int, default=None, help="cap the number of tables (testing)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    kinds = ("candles", "indicators") if args.kind == "both" else (args.kind,)
    run(kinds, args.tf, args.symbol, args.limit, args.execute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# core/oi_5m.py — 5m open interest persistence (TimescaleDB hypertable `oi_5m`)
#
# K9/OIC from docs/MODEL_CANDIDATES_SPEC_2026-07.md (T-2026-CU-9050-103).
# Blueprint: core/ticker_10s.py — same Timescale conventions, same
# caller contract (errors never stop the collector loop).
#
# Writer: 35_oi_collector.py (ONE batched insert per 5m sweep across all
# coins — no per-symbol insert, P1.40 lesson about WAL churn) and one-off
# tools/oi_backfill.py (30d initial backfill, Binance doesn't keep more than that).
# Reader: future OI model studies (OI-price divergence, OI spike fade,
# OI×funding — own tasks from ~Oct 2026, ≥60d history; spec K9).
#
# TZ contract: `ts` is TIMESTAMPTZ and is written UTC-aware (timestamps
# arrive as Binance epoch-ms and pass through core.time.from_unix_ts) —
# the same deliberate deviation from the naive legacy columns as ticker_10s,
# so the DST mixed-offset error class (fix f95f092) doesn't arise here.
#
# Volume budget: ~530 coins × 288 points/day ≈ 153k rows/day (~8 MB/day raw).
# Chunks 1 day, compression after 3 days (segmentby=symbol), retention 730
# days — native Timescale jobs, 6_housekeeping does NOT need to touch the table.
#
# Dedup: unlike ticker_10s it needs no UNIQUE index migration — the
# table is new and the PRIMARY KEY (ts, symbol) from the spec enforces
# uniqueness from the start. Both the collector and backfill write with
# ON CONFLICT DO NOTHING against it (double start/backfill overlap = no-op).

from __future__ import annotations

import datetime
import logging

from psycopg2.extras import execute_values

from core.time import from_unix_ts

logger = logging.getLogger(__name__)

TABLE = "oi_5m"
CHUNK_INTERVAL = "1 day"
COMPRESS_AFTER = "3 days"
RETAIN_FOR = "730 days"


def ensure_schema(conn) -> None:
    """Creates the hypertable + compression/retention policy idempotently.

    Call once at process start (not per sweep). Expects the
    timescaledb extension in the DB (installed on the live VPS, 2.26).
    """
    try:
        _ensure_schema_inner(conn)
    except Exception:
        # Never leave half-executed DDL on the shared connection —
        # the caller retries the schema on the next sweep and needs
        # a clean transaction for that (ticker_10s pattern).
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback after failed oi_5m schema setup failed")
        raise


def _ensure_schema_inner(conn) -> None:
    with conn.cursor() as cur:
        # DDL exactly per spec K9: PRIMARY KEY (ts, symbol) contains the
        # partitioning column, so create_hypertable accepts it.
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                    ts            TIMESTAMPTZ      NOT NULL,
                    symbol        TEXT             NOT NULL,
                    open_interest DOUBLE PRECISION,
                    oi_value_usdt DOUBLE PRECISION,
                    PRIMARY KEY (ts, symbol)
                )"""
        )
        cur.execute(
            f"SELECT create_hypertable(%s, 'ts', chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}', if_not_exists => TRUE)",
            (TABLE,),
        )
        cur.execute(
            f"""ALTER TABLE {TABLE} SET (
                    timescaledb.compress,
                    timescaledb.compress_segmentby = 'symbol',
                    timescaledb.compress_orderby = 'ts DESC'
                )"""
        )
        cur.execute(
            f"SELECT add_compression_policy(%s, INTERVAL '{COMPRESS_AFTER}', if_not_exists => TRUE)",
            (TABLE,),
        )
        cur.execute(
            f"SELECT add_retention_policy(%s, INTERVAL '{RETAIN_FOR}', if_not_exists => TRUE)",
            (TABLE,),
        )
    conn.commit()
    logger.info(
        f"✅ Hypertable {TABLE} ready (chunk={CHUNK_INTERVAL}, compress>{COMPRESS_AFTER}, retention={RETAIN_FOR})"
    )


def rows_from_hist_payload(symbol: str, payload: list[dict]) -> list[tuple]:
    """Builds insert rows from a `/futures/data/openInterestHist` response.

    One source for BOTH writers (collector sweep and backfill pagination),
    so parsing/TZ conversion cannot drift. Binance delivers per point
    ``sumOpenInterest`` (contracts), ``sumOpenInterestValue`` (USDT) and
    ``timestamp`` (epoch-ms, UTC). Malformed entries are dropped with an
    ERROR log — never substituted with 0 (feature contract discipline, P0.12).
    """
    rows: list[tuple[datetime.datetime, str, float, float]] = []
    for item in payload:
        try:
            rows.append(
                (
                    from_unix_ts(int(item["timestamp"]), ms=True),
                    symbol,
                    float(item["sumOpenInterest"]),
                    float(item["sumOpenInterestValue"]),
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"oi_5m: malformed openInterestHist point for {symbol} dropped: {e} — {item!r}")
    return rows


def insert_oi(conn, rows: list[tuple]) -> None:
    """Batched insert of a complete 5m sweep (or a backfill page).

    ``rows``: list of ``(ts_utc_aware, symbol, open_interest, oi_value_usdt)``.
    Errors must never stop the collector loop — the caller catches exceptions
    (a lost sweep is an accepted data point loss, a dead collector loses
    EVERYTHING from that point on — the same asymmetry as with the detector).
    """
    if not rows:
        return
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                f"INSERT INTO {TABLE} (ts, symbol, open_interest, oi_value_usdt) VALUES %s "
                f"ON CONFLICT (ts, symbol) DO NOTHING",
                rows,
                page_size=200,
            )
        conn.commit()
    except Exception:
        # Rollback is part of commit ownership: without it the shared
        # connection stays in InFailedSqlTransaction and all subsequent sweeps
        # fail — exactly the "dead collector" scenario.
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback after failed oi_5m insert failed")
        raise

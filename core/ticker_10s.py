# core/ticker_10s.py — 10s ticker persistence (TimescaleDB hypertable `ticker_10s`)
#
# Writer: 10_pump_dump_detector.py (ONE batched insert per 10s tick across all
# coins — no per-symbol insert, see P1.40 lesson on WAL churn).
# Reader: future microstructure builders (PEX1 V2: exhaustion features from
# order-flow decay AFTER the spike; report 15 S6).
#
# TZ contract: `ts` is TIMESTAMPTZ and written UTC-aware — deliberate
# deviation from naive legacy columns (session TZ Europe/Bucharest), so
# the DST mixed-offset error class (fix f95f092) doesn't arise here at all.
#
# Volume budget: ~108 coins × 8,640 ticks/day ≈ 0.9M rows/day (~45 MB/day raw).
# Chunks are compressed column-wise after COMPRESS_AFTER (segmentby=symbol),
# retention deletes chunks after RETAIN_FOR — both native timescale jobs;
# housekeeping (6_housekeeping.py) must NOT touch this table.

import logging

from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)

TABLE = "ticker_10s"
CHUNK_INTERVAL = "1 day"
COMPRESS_AFTER = "3 days"
RETAIN_FOR = "365 days"


def ensure_schema(conn) -> None:
    """Creates hypertable + compression/retention policy idempotently.

    Call once at process startup (not per tick). Expects the
    timescaledb extension in the DB (installed on live VPS, 2.26).
    """
    try:
        _ensure_schema_inner(conn)
    except Exception:
        # Never leave half-executed DDL on the shared connection —
        # after a schema error the caller deliberately continues without persistence
        # and needs a clean transaction for that.
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback after failed ticker_10s schema setup failed")
        raise


def _ensure_schema_inner(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                    ts        TIMESTAMPTZ      NOT NULL,
                    symbol    VARCHAR(20)      NOT NULL,
                    price     DOUBLE PRECISION NOT NULL,
                    vol_10s   DOUBLE PRECISION NOT NULL,
                    vol_valid BOOLEAN          NOT NULL
                )"""
        )
        cur.execute(
            f"SELECT create_hypertable(%s, 'ts', chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}', if_not_exists => TRUE)",
            (TABLE,),
        )
        # UNIQUE instead of plain index: a second writer (detector double-start)
        # must not silently create duplicate rows — known error class
        # (closed_ai_signals dups, coins.json double-writer).
        # Insert runs with ON CONFLICT DO NOTHING against this index.
        cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", (f"uq_{TABLE}_symbol_ts",))
        if cur.fetchone() is None:
            # One-time migration with dedup preface: if duplicates already exist
            # at the first start of the new code (exactly the double-writer class
            # the index protects against), CREATE UNIQUE INDEX would otherwise
            # fail on EVERY start and persistence would stay permanently disabled.
            # Same ts ⇒ same chunk, the ctid comparison is unique there.
            cur.execute(
                f"DELETE FROM {TABLE} a USING {TABLE} b WHERE a.symbol = b.symbol AND a.ts = b.ts AND a.ctid > b.ctid"
            )
            cur.execute(f"DROP INDEX IF EXISTS ix_{TABLE}_symbol_ts")
            cur.execute(f"CREATE UNIQUE INDEX uq_{TABLE}_symbol_ts ON {TABLE} (symbol, ts)")
            # Commit migration immediately (own transaction): if a later
            # policy statement fails, the rollback would otherwise drop dedup +
            # index and the full-table DELETE would re-run on EVERY start
            # — after COMPRESS_AFTER against compressed chunks,
            # where DELETE/CREATE UNIQUE INDEX are restricted.
            conn.commit()
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


def insert_ticks(conn, rows: list[tuple]) -> None:
    """Batched insert of a complete 10s tick.

    ``rows``: list of ``(ts_utc_aware, symbol, price, vol_10s, vol_valid)``.
    Errors must never stop the detector loop — the caller catches exceptions
    (a lost tick is an accepted data-point loss; a dead detector is not).
    """
    if not rows:
        return
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                f"INSERT INTO {TABLE} (ts, symbol, price, vol_10s, vol_valid) VALUES %s "
                f"ON CONFLICT (symbol, ts) DO NOTHING",
                rows,
                page_size=200,
            )
        conn.commit()
    except Exception:
        # Rollback is commit's responsibility: without it the shared connection
        # stays in InFailedSqlTransaction and ALL subsequent detector inserts
        # (pump_dump_events, outbox) fail — exactly the "dead detector" scenario
        # the caller contract excludes.
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback after failed ticker_10s insert failed")
        raise

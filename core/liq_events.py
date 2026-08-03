# core/liq_events.py — forceOrder liquidation persistence (TimescaleDB hypertable `liq_events`)
#
# LQE1 (T-2026-KYT-9050-077, follow-up of the MPS1 study T-2026-KYT-9050-073):
# real Binance liquidations as ground truth for calibrating the estimated
# liquidation heatmap (tools/mps1_liq_heatmap.py). Blueprint: core/oi_5m.py —
# same Timescale conventions, same caller contract (errors never stop the
# collector loop).
#
# Writer: 41_liq_collector.py (websocket !forceOrder@arr, batched inserts).
# Reader: future calibration studies (heatmap levels vs. real liquidation
# clusters; MPS family).
#
# IMPORTANT (data quality contract): Binance throttles the !forceOrder@arr stream
# to a maximum of ONE order per second PER SYMBOL — the table is thus a
# SAMPLE of the liquidations, not a full census. In cascades (many orders/s in
# a single symbol) the undersample is largest. For cluster calibration
# (WHERE liquidations sit) the sample is usable, for volume sums it is NOT.
#
# TZ contract: `ts` is TIMESTAMPTZ and is written UTC-aware (Binance
# epoch-ms → core.time.from_unix_ts) — the same deliberate departure from the
# naive legacy columns as oi_5m/ticker_10s (DST mixed-offset error class).
#
# Volume budget: market-wide liquidations are sparse (quiet <1/s, cascades
# capped by the 1/s/symbol throttle) — roughly ≤ ~100k rows/day worst case,
# typically far below that. Chunks 1 day, compression after 3 days
# (segmentby=symbol), retention 730 days — native Timescale jobs.
#
# Dedup: PRIMARY KEY (ts, symbol, side, price) + ON CONFLICT DO NOTHING.
# Two orders of the same symbol in the same ms with the same side and the same
# price are practically excluded by the 1/s/symbol throttle; duplicate
# delivery after a reconnect thus becomes a no-op (ticker_10s argument).

from __future__ import annotations

import logging

from psycopg2.extras import execute_values

from core.time import from_unix_ts

logger = logging.getLogger(__name__)

TABLE = "liq_events"
CHUNK_INTERVAL = "1 day"
COMPRESS_AFTER = "3 days"
RETAIN_FOR = "730 days"


def ensure_schema(conn) -> None:
    """Creates the hypertable + compression/retention policy idempotently.

    Call once at process start (not per flush). Expects the
    timescaledb extension in the DB (installed on the live VPS, 2.26).
    """
    try:
        _ensure_schema_inner(conn)
    except Exception:
        # Never leave half-executed DDL sitting on the shared connection —
        # the caller retries the schema on the next flush and needs a
        # clean transaction for that (oi_5m/ticker_10s pattern).
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback after failed liq_events schema setup failed")
        raise


def _ensure_schema_inner(conn) -> None:
    with conn.cursor() as cur:
        # side: 'SELL' = a long was liquidated, 'BUY' = a short was liquidated
        # (Binance convention: the order side of the forced liquidation).
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                    ts         TIMESTAMPTZ      NOT NULL,
                    symbol     TEXT             NOT NULL,
                    side       TEXT             NOT NULL,
                    price      DOUBLE PRECISION,
                    avg_price  DOUBLE PRECISION,
                    qty        DOUBLE PRECISION,
                    value_usdt DOUBLE PRECISION,
                    PRIMARY KEY (ts, symbol, side, price)
                )"""
        )
        cur.execute(
            f"SELECT create_hypertable(%s, 'ts', chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}', if_not_exists => TRUE)",
            (TABLE,),
        )
        # orderby together with segmentby covers the full PRIMARY KEY —
        # older Timescale versions (≤~2.17) otherwise hard-reject a
        # compression config and ensure_schema would kill EVERY flush
        # (alive-but-dead collector on a stream without backfill).
        cur.execute(
            f"""ALTER TABLE {TABLE} SET (
                    timescaledb.compress,
                    timescaledb.compress_segmentby = 'symbol',
                    timescaledb.compress_orderby = 'ts DESC, side, price'
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


def row_from_force_order(msg: dict) -> tuple | None:
    """Builds the insert row from ONE `forceOrder` websocket event.

    Expects the parsed JSON object `{"e": "forceOrder", "o": {...}}`.
    ``o.T`` (epoch-ms, UTC) → aware ts; ``o.z`` (cumulative filled quantity) ×
    ``o.ap`` (average price) → value_usdt: this is the actually
    executed notional of the forced liquidation, not the order size.
    Malformed events are dropped with an ERROR log — never substituted with 0
    (feature contract discipline, P0.12; oi_5m pattern).
    """
    try:
        if msg.get("e") != "forceOrder":
            return None
        o = msg["o"]
        filled = float(o["z"])
        avg_price = float(o["ap"])
        return (
            from_unix_ts(int(o["T"]), ms=True),
            str(o["s"]),
            str(o["S"]),
            float(o["p"]),
            avg_price,
            filled,
            filled * avg_price,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        # AttributeError covers non-dict payloads (e.g. a JSON array) —
        # a format change of the stream must never escalate to the connection.
        logger.error(f"liq_events: malformed forceOrder event dropped: {e} — {msg!r}")
        return None


def insert_liq(conn, rows: list[tuple]) -> None:
    """Batched insert of one flush batch.

    ``rows``: list of ``(ts_utc_aware, symbol, side, price, avg_price, qty,
    value_usdt)``. Errors must never stop the collector loop — the caller
    catches exceptions (a lost batch is an accepted data point
    loss, a dead collector loses EVERYTHING from that point on).
    """
    if not rows:
        return
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                f"INSERT INTO {TABLE} (ts, symbol, side, price, avg_price, qty, value_usdt) "
                f"VALUES %s ON CONFLICT (ts, symbol, side, price) DO NOTHING",
                rows,
                page_size=200,
            )
        conn.commit()
    except Exception:
        # Rollback belongs to commit ownership: without it the shared
        # connection stays in InFailedSqlTransaction and all following flushes
        # fail — exactly the "dead collector" scenario (oi_5m pattern).
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback after failed liq_events insert failed")
        raise

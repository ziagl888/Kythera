# core/ticker_10s.py — 10s-Ticker-Persistenz (TimescaleDB-Hypertable `ticker_10s`)
#
# Schreiber: 10_pump_dump_detector.py (EIN batched Insert pro 10s-Tick über alle
# Coins — kein Per-Symbol-Insert, siehe P1.40-Lehre zu WAL-Churn).
# Leser: künftige Microstructure-Builder (PEX1 V2: Exhaustion-Features aus dem
# Order-Flow-Abklingen NACH dem Spike; Report 15 S6).
#
# TZ-Vertrag: `ts` ist TIMESTAMPTZ und wird UTC-aware geschrieben — bewusste
# Abweichung von den naiven Legacy-Spalten (Session-TZ Europe/Bucharest), damit
# die DST-Mixed-Offset-Fehlerklasse (Fix f95f092) hier gar nicht erst entsteht.
#
# Volumen-Budget: ~108 Coins × 8.640 Ticks/Tag ≈ 0,9M Rows/Tag (~45 MB/Tag roh).
# Chunks werden nach COMPRESS_AFTER spaltenweise komprimiert (segmentby=symbol),
# Retention löscht Chunks nach RETAIN_FOR — beides native Timescale-Jobs, das
# Housekeeping (6_housekeeping.py) muss diese Tabelle NICHT anfassen.

import logging

from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)

TABLE = "ticker_10s"
CHUNK_INTERVAL = "1 day"
COMPRESS_AFTER = "3 days"
RETAIN_FOR = "365 days"


def ensure_schema(conn) -> None:
    """Legt Hypertable + Compression-/Retention-Policy idempotent an.

    Einmal beim Prozess-Start aufrufen (nicht pro Tick). Erwartet die
    timescaledb-Extension in der DB (auf dem Live-VPS installiert, 2.26).
    """
    try:
        _ensure_schema_inner(conn)
    except Exception:
        # Halb ausgeführte DDL nie auf der geteilten Connection liegen lassen —
        # der Caller läuft nach einem Schema-Fehler bewusst ohne Persistenz
        # weiter und braucht dafür eine saubere Transaktion.
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback nach fehlgeschlagenem ticker_10s-Schema-Setup fehlgeschlagen")
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
        cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_symbol_ts ON {TABLE} (symbol, ts DESC)")
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
        f"✅ Hypertable {TABLE} bereit (chunk={CHUNK_INTERVAL}, compress>{COMPRESS_AFTER}, retention={RETAIN_FOR})"
    )


def insert_ticks(conn, rows: list[tuple]) -> None:
    """Batched Insert eines kompletten 10s-Ticks.

    ``rows``: Liste von ``(ts_utc_aware, symbol, price, vol_10s, vol_valid)``.
    Fehler dürfen den Detector-Loop nie stoppen — der Caller fängt Exceptions
    (ein verlorener Tick ist ein akzeptierter Datenpunkt-Verlust, ein toter
    Detector nicht).
    """
    if not rows:
        return
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                f"INSERT INTO {TABLE} (ts, symbol, price, vol_10s, vol_valid) VALUES %s",
                rows,
                page_size=200,
            )
        conn.commit()
    except Exception:
        # Rollback gehört zum Commit-Besitz: ohne ihn bleibt die geteilte
        # Connection in InFailedSqlTransaction und ALLE folgenden Detector-
        # Inserts (pump_dump_events, Outbox) schlagen fehl — genau das
        # "toter Detector"-Szenario, das der Caller-Contract ausschließt.
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback nach fehlgeschlagenem ticker_10s-Insert fehlgeschlagen")
        raise

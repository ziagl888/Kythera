# core/liq_events.py — forceOrder-Liquidations-Persistenz (TimescaleDB-Hypertable `liq_events`)
#
# LQE1 (T-2026-KYT-9050-077, Follow-up aus der MPS1-Studie T-2026-KYT-9050-073):
# echte Binance-Liquidationen als Ground-Truth zur Kalibrierung der geschätzten
# Liquidations-Heatmap (tools/mps1_liq_heatmap.py). Blaupause: core/oi_5m.py —
# gleiche Timescale-Konventionen, gleicher Caller-Contract (Fehler stoppen den
# Collector-Loop nie).
#
# Schreiber: 41_liq_collector.py (Websocket !forceOrder@arr, batched Inserts).
# Leser: künftige Kalibrier-Studien (Heatmap-Level vs. echte Liquidations-
# Cluster; MPS-Familie).
#
# WICHTIG (Datenqualitäts-Contract): Binance drosselt den !forceOrder@arr-Stream
# auf maximal EINE Order pro Sekunde PRO SYMBOL — die Tabelle ist damit ein
# SAMPLE der Liquidationen, keine Vollerhebung. In Kaskaden (viele Orders/s in
# einem Symbol) ist das Untersample am größten. Für Cluster-Kalibrierung
# (WO liegen Liquidationen) ist das Sample brauchbar, für Volumens-Summen NICHT.
#
# TZ-Vertrag: `ts` ist TIMESTAMPTZ und wird UTC-aware geschrieben (Binance-
# Epoch-ms → core.time.from_unix_ts) — gleiche bewusste Abweichung von den
# naiven Legacy-Spalten wie oi_5m/ticker_10s (DST-Mixed-Offset-Fehlerklasse).
#
# Volumen-Budget: marktweite Liquidationen sind sparse (ruhig <1/s, Kaskaden
# durch die 1/s/Symbol-Drossel gedeckelt) — grob ≤ ~100k Rows/Tag worst case,
# typischerweise weit darunter. Chunks 1 Tag, Compression nach 3 Tagen
# (segmentby=symbol), Retention 730 Tage — native Timescale-Jobs.
#
# Dedup: PRIMARY KEY (ts, symbol, side, price) + ON CONFLICT DO NOTHING.
# Zwei Orders desselben Symbols im selben ms mit gleicher Seite und gleichem
# Preis sind durch die 1/s/Symbol-Drossel praktisch ausgeschlossen; Doppel-
# Delivery nach Reconnect wird damit zum No-op (ticker_10s-Argument).

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
    """Legt Hypertable + Compression-/Retention-Policy idempotent an.

    Einmal beim Prozess-Start aufrufen (nicht pro Flush). Erwartet die
    timescaledb-Extension in der DB (auf dem Live-VPS installiert, 2.26).
    """
    try:
        _ensure_schema_inner(conn)
    except Exception:
        # Halb ausgeführte DDL nie auf der geteilten Connection liegen lassen —
        # der Caller versucht das Schema beim nächsten Flush erneut und braucht
        # dafür eine saubere Transaktion (oi_5m/ticker_10s-Muster).
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback nach fehlgeschlagenem liq_events-Schema-Setup fehlgeschlagen")
        raise


def _ensure_schema_inner(conn) -> None:
    with conn.cursor() as cur:
        # side: 'SELL' = Long wurde liquidiert, 'BUY' = Short wurde liquidiert
        # (Binance-Konvention: die Order-Seite der Zwangsglattstellung).
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
        # orderby deckt zusammen mit segmentby den vollen PRIMARY KEY ab —
        # ältere Timescale-Versionen (≤~2.17) lehnen eine Compression-Config
        # sonst hart ab und ensure_schema würde JEDEN Flush killen
        # (alive-but-dead-Collector auf einem Stream ohne Backfill).
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
        f"✅ Hypertable {TABLE} bereit (chunk={CHUNK_INTERVAL}, compress>{COMPRESS_AFTER}, retention={RETAIN_FOR})"
    )


def row_from_force_order(msg: dict) -> tuple | None:
    """Baut die Insert-Row aus EINEM `forceOrder`-Websocket-Event.

    Erwartet das geparste JSON-Objekt `{"e": "forceOrder", "o": {...}}`.
    ``o.T`` (Epoch-ms, UTC) → aware ts; ``o.z`` (kumulativ gefüllte Menge) ×
    ``o.ap`` (Durchschnittspreis) → value_usdt: das ist das tatsächlich
    exekutierte Notional der Zwangsglattstellung, nicht die Order-Größe.
    Malformte Events werden mit ERROR-Log verworfen — nie mit 0 substituiert
    (Feature-Contract-Disziplin, P0.12; oi_5m-Muster).
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
        # AttributeError deckt Nicht-Dict-Payloads (z.B. ein JSON-Array) ab —
        # ein Format-Wechsel des Streams darf nie bis zur Verbindung eskalieren.
        logger.error(f"liq_events: malformtes forceOrder-Event verworfen: {e} — {msg!r}")
        return None


def insert_liq(conn, rows: list[tuple]) -> None:
    """Batched Insert eines Flush-Batches.

    ``rows``: Liste von ``(ts_utc_aware, symbol, side, price, avg_price, qty,
    value_usdt)``. Fehler dürfen den Collector-Loop nie stoppen — der Caller
    fängt Exceptions (ein verlorener Batch ist ein akzeptierter Datenpunkt-
    Verlust, ein toter Collector verliert ab da ALLES).
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
        # Rollback gehört zum Commit-Besitz: ohne ihn bleibt die geteilte
        # Connection in InFailedSqlTransaction und alle folgenden Flushes
        # schlagen fehl — genau das "toter Collector"-Szenario (oi_5m-Muster).
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback nach fehlgeschlagenem liq_events-Insert fehlgeschlagen")
        raise

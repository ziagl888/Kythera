# core/oi_5m.py — 5m-Open-Interest-Persistenz (TimescaleDB-Hypertable `oi_5m`)
#
# K9/OIC aus docs/MODEL_CANDIDATES_SPEC_2026-07.md (T-2026-CU-9050-103).
# Blaupause: core/ticker_10s.py — gleiche Timescale-Konventionen, gleicher
# Caller-Contract (Fehler stoppen den Collector-Loop nie).
#
# Schreiber: 35_oi_collector.py (EIN batched Insert pro 5m-Sweep über alle
# Coins — kein Per-Symbol-Insert, P1.40-Lehre zu WAL-Churn) und einmalig
# tools/oi_backfill.py (30d-Initial-Backfill, mehr hält Binance nicht vor).
# Leser: künftige OI-Modell-Studien (OI-Preis-Divergenz, OI-Spike-Fade,
# OI×Funding — eigene Tasks ab ~Okt 2026, ≥60d Historie; Spec K9).
#
# TZ-Vertrag: `ts` ist TIMESTAMPTZ und wird UTC-aware geschrieben (Timestamps
# kommen als Binance-Epoch-ms und laufen durch core.time.from_unix_ts) —
# gleiche bewusste Abweichung von den naiven Legacy-Spalten wie ticker_10s,
# damit die DST-Mixed-Offset-Fehlerklasse (Fix f95f092) hier nicht entsteht.
#
# Volumen-Budget: ~530 Coins × 288 Punkte/Tag ≈ 153k Rows/Tag (~8 MB/Tag roh).
# Chunks 1 Tag, Compression nach 3 Tagen (segmentby=symbol), Retention 730
# Tage — native Timescale-Jobs, 6_housekeeping muss die Tabelle NICHT anfassen.
#
# Dedup: anders als ticker_10s braucht es keine UNIQUE-Index-Migration — die
# Tabelle ist neu und der PRIMARY KEY (ts, symbol) aus der Spec erzwingt die
# Eindeutigkeit von Anfang an. Collector und Backfill schreiben beide mit
# ON CONFLICT DO NOTHING dagegen (Doppelstart/Backfill-Überlappung = No-op).

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
    """Legt Hypertable + Compression-/Retention-Policy idempotent an.

    Einmal beim Prozess-Start aufrufen (nicht pro Sweep). Erwartet die
    timescaledb-Extension in der DB (auf dem Live-VPS installiert, 2.26).
    """
    try:
        _ensure_schema_inner(conn)
    except Exception:
        # Halb ausgeführte DDL nie auf der geteilten Connection liegen lassen —
        # der Caller versucht das Schema beim nächsten Sweep erneut und braucht
        # dafür eine saubere Transaktion (ticker_10s-Muster).
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback nach fehlgeschlagenem oi_5m-Schema-Setup fehlgeschlagen")
        raise


def _ensure_schema_inner(conn) -> None:
    with conn.cursor() as cur:
        # DDL exakt nach Spec K9: PRIMARY KEY (ts, symbol) enthält die
        # Partitionierungs-Spalte, damit akzeptiert create_hypertable ihn.
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
        f"✅ Hypertable {TABLE} bereit (chunk={CHUNK_INTERVAL}, compress>{COMPRESS_AFTER}, retention={RETAIN_FOR})"
    )


def rows_from_hist_payload(symbol: str, payload: list[dict]) -> list[tuple]:
    """Baut Insert-Rows aus einer `/futures/data/openInterestHist`-Antwort.

    Eine Quelle für BEIDE Writer (Collector-Sweep und Backfill-Paginierung),
    damit Parsing/TZ-Konversion nicht driften kann. Binance liefert je Punkt
    ``sumOpenInterest`` (Kontrakte), ``sumOpenInterestValue`` (USDT) und
    ``timestamp`` (Epoch-ms, UTC). Malformte Einträge werden mit ERROR-Log
    verworfen — nie mit 0 substituiert (Feature-Contract-Disziplin, P0.12).
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
            logger.error(f"oi_5m: malformter openInterestHist-Punkt für {symbol} verworfen: {e} — {item!r}")
    return rows


def insert_oi(conn, rows: list[tuple]) -> None:
    """Batched Insert eines kompletten 5m-Sweeps (oder einer Backfill-Seite).

    ``rows``: Liste von ``(ts_utc_aware, symbol, open_interest, oi_value_usdt)``.
    Fehler dürfen den Collector-Loop nie stoppen — der Caller fängt Exceptions
    (ein verlorener Sweep ist ein akzeptierter Datenpunkt-Verlust, ein toter
    Collector verliert ab da ALLES — dieselbe Asymmetrie wie beim Detector).
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
        # Rollback gehört zum Commit-Besitz: ohne ihn bleibt die geteilte
        # Connection in InFailedSqlTransaction und alle folgenden Sweeps
        # schlagen fehl — genau das "toter Collector"-Szenario.
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback nach fehlgeschlagenem oi_5m-Insert fehlgeschlagen")
        raise

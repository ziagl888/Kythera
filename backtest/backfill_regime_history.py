# backtest/backfill_regime_history.py
"""
Backfills regime_history for the last 90 days in 5-minute steps.

Usage:
    cd <project-root>
    py backtest/backfill_regime_history.py

Idempotent — uses ON CONFLICT DO NOTHING so it can be run multiple times.
Runtime: ~10-20 minutes for 90 days (25,920 iterations).
Progress is logged every 1,000 iterations.
"""
from __future__ import annotations

import json
import logging
import sys
import os
from datetime import datetime, timedelta, timezone

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import get_db_connection
from core.logging_setup import setup_logging
from core.regime_logic import classify_regime, compute_features

logger = setup_logging("BACKFILL")

BACKFILL_DAYS = 90
STEP_MINUTES = 5


def run_backfill() -> None:
    logger.info(f"=== BACKFILL START: last {BACKFILL_DAYS} Tage ===")

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    start = now_utc - timedelta(days=BACKFILL_DAYS)

    # Build list of timestamps to process (5-min aligned)
    # Align start to next 5-minute boundary
    extra = start.minute % STEP_MINUTES
    if extra:
        start += timedelta(minutes=(STEP_MINUTES - extra))
    start = start.replace(second=0, microsecond=0)

    timestamps = []
    ts = start
    while ts <= now_utc:
        timestamps.append(ts)
        ts += timedelta(minutes=STEP_MINUTES)

    total = len(timestamps)
    logger.info(f"Timestamps to process: {total}")

    conn = None
    inserted = 0
    skipped = 0
    errors = 0

    try:
        conn = get_db_connection()

        # Ensure schema exists
        from backtest.backfill_regime_history import _ensure_minimal_schema
        _ensure_minimal_schema(conn)

        for i, ts in enumerate(timestamps):
            if i > 0 and i % 1000 == 0:
                pct = i / total * 100
                logger.info(
                    f"Progress: {i}/{total} ({pct:.1f}%) — "
                    f"inserted={inserted}, skipped={skipped}, errors={errors}"
                )

            try:
                # Compute features as-of this historical timestamp
                as_of = ts.replace(tzinfo=timezone.utc)
                features = compute_features(conn, as_of=as_of)
                if features is None:
                    skipped += 1
                    continue

                result = classify_regime(
                    features, features["vola_p75"], features["vola_p40"]
                )

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO regime_history
                            (ts, regime, alt_context,
                             btc_price, btc_return_1h, btc_return_4h,
                             btc_atr_1h_pct, btc_atr_4h_pct,
                             btcdom_value, btcdom_return_24h,
                             confidence, confidence_btc, confidence_alt,
                             raw_features)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (ts) DO NOTHING
                        """,
                        (
                            ts,
                            result["regime"],
                            result["alt_context"],
                            features.get("btc_price"),
                            features.get("btc_return_1h"),
                            features.get("btc_return_4h"),
                            features.get("btc_atr_1h_pct"),
                            features.get("btc_atr_4h_pct"),
                            features.get("btcdom_value"),
                            features.get("btcdom_return_24h"),
                            result["confidence"],
                            result["confidence_btc"],
                            result["confidence_alt"],
                            json.dumps({k: v for k, v in features.items()
                                        if isinstance(v, (int, float, type(None)))}),
                        ),
                    )
                conn.commit()
                inserted += 1

            except Exception as e:
                errors += 1
                if errors <= 10:
                    logger.error(f"Error for {ts}: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass

    finally:
        if conn:
            conn.close()

    logger.info(
        f"=== BACKFILL COMPLETE ===\n"
        f"  Gesamt:   {total}\n"
        f"  Inserted: {inserted}\n"
        f"  Skipped:  {skipped} (unvollständige Daten)\n"
        f"  Errors:   {errors}"
    )


def _ensure_minimal_schema(conn) -> None:
    """Ensures regime_history table exists (minimal version for backfill)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS regime_history (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMP WITHOUT TIME ZONE NOT NULL UNIQUE,
                regime TEXT NOT NULL,
                alt_context TEXT NOT NULL,
                btc_price REAL,
                btc_return_1h REAL,
                btc_return_4h REAL,
                btc_atr_1h_pct REAL,
                btc_atr_4h_pct REAL,
                btcdom_value REAL,
                btcdom_return_24h REAL,
                confidence REAL,
                confidence_btc REAL,
                confidence_alt REAL,
                raw_features JSON
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_regime_history_ts_desc
            ON regime_history (ts DESC)
        """)
    conn.commit()


if __name__ == "__main__":
    run_backfill()

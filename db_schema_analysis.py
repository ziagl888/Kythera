#!/usr/bin/env python3
"""
db_schema_analysis.py — Analyses the current DB status and quantifies
the overhead of the table-per-coin schema.

The script is READ-ONLY — it writes nothing, changes nothing, blocks nothing.
You can run it against the production DB without concern.

Usage:
    python db_schema_analysis.py

Output:
    1. Overall statistics (table count, size, pg_attribute entries)
    2. OHLCV tables aggregated by timeframe
    3. Indicator tables aggregated by timeframe
    4. Largest individual tables (Top 20)
    5. Bloat estimation
    6. System catalog size
    7. Autovacuum status
    8. Recommended consolidation potentials

All numbers in MB/GB (pg_size_pretty format), no raw bytes.
"""

from __future__ import annotations

import os
import sys

# Extend sys.path to import core.database
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from core.database import get_db_connection
except ImportError:
    print("Error: core.database not found.")
    print("Place the script in the project root and run it from there.")
    sys.exit(1)


# ────────────────────────────────────────────────────────────────────────────
# PRINTING HELPERS
# ────────────────────────────────────────────────────────────────────────────


def section(title: str) -> None:
    print()
    print("═" * 80)
    print(f"  {title}")
    print("═" * 80)


def subsection(title: str) -> None:
    print()
    print(f"── {title} ──")


def print_kv(key: str, value, width: int = 38) -> None:
    print(f"  {key:<{width}} {value}")


def print_table(rows: list[tuple], headers: list[str], widths: list[int] | None = None) -> None:
    """Simple table printer with aligned columns."""
    if not rows:
        print("  (no data)")
        return
    if widths is None:
        widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) + 2 for i, h in enumerate(headers)]
    header_line = "  " + "".join(f"{h:<{w}}" for h, w in zip(headers, widths, strict=False))
    print(header_line)
    print("  " + "─" * (sum(widths)))
    for r in rows:
        row_line = "  " + "".join(f"{str(v):<{w}}" for v, w in zip(r, widths, strict=False))
        print(row_line)


# ────────────────────────────────────────────────────────────────────────────
# QUERIES
# ────────────────────────────────────────────────────────────────────────────


def analyze_overall(cur) -> None:
    section("1. OVERALL STATISTICS")

    # Count of user tables
    cur.execute("""
        SELECT COUNT(*) FROM pg_class
        WHERE relkind = 'r'
          AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
    """)
    n_tables = cur.fetchone()[0]

    # Count of indexes
    cur.execute("""
        SELECT COUNT(*) FROM pg_class
        WHERE relkind = 'i'
          AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
    """)
    n_indexes = cur.fetchone()[0]

    # Total size of user data
    cur.execute("""
        SELECT pg_size_pretty(SUM(pg_total_relation_size(c.oid))::bigint)
        FROM pg_class c
        WHERE relkind = 'r'
          AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
    """)
    total_size = cur.fetchone()[0]

    # pg_attribute, pg_class total entries
    cur.execute("SELECT COUNT(*) FROM pg_attribute")
    n_attrs = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM pg_class")
    n_relations = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM pg_index")
    n_index_rows = cur.fetchone()[0]

    # Database total size
    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
    db_size = cur.fetchone()[0]

    print_kv("Database total size:", db_size)
    print_kv("User tables (public):", f"{n_tables:,}")
    print_kv("User indexes (public):", f"{n_indexes:,}")
    print_kv("User data size:", total_size)
    print()
    print_kv("System catalog pg_class entries:", f"{n_relations:,}")
    print_kv("System catalog pg_attribute entries:", f"{n_attrs:,}")
    print_kv("System catalog pg_index entries:", f"{n_index_rows:,}")

    if n_tables > 1000:
        print()
        print("  ⚠  User table count is very high (>1000). PostgreSQL is optimised for")
        print("     hundreds to a few thousand tables — beyond that, query planning slows,")
        print("     autovacuum hits timeouts more often, and pg_attribute grows")
        print("     disproportionately.")


def analyze_ohlcv_tables(cur) -> None:
    section("2. OHLCV TABLES BY TIMEFRAME")

    # Aggregation over all Coin_TF tables
    # Pattern: {COIN}USDT_{TF}  where TF is one of the known ones
    cur.execute("""
        WITH ohlcv_tables AS (
            SELECT
                c.relname,
                -- Extract timeframe at the end
                SUBSTRING(c.relname FROM '_([0-9]+[mhdw])$') AS timeframe,
                pg_total_relation_size(c.oid) AS total_size,
                pg_relation_size(c.oid) AS data_size,
                c.reltuples::bigint AS row_estimate
            FROM pg_class c
            WHERE c.relkind = 'r'
              AND c.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
              AND c.relname ~ '^[A-Z0-9]+USDT_[0-9]+[mhdw]$'
        )
        SELECT
            timeframe,
            COUNT(*) AS n_tables,
            pg_size_pretty(SUM(total_size)::bigint) AS total_size_pretty,
            SUM(total_size) AS total_size_bytes,
            SUM(row_estimate) AS total_rows,
            pg_size_pretty(AVG(total_size)::bigint) AS avg_per_table
        FROM ohlcv_tables
        GROUP BY timeframe
        ORDER BY
            CASE timeframe
                WHEN '5m' THEN 1 WHEN '15m' THEN 2 WHEN '30m' THEN 3
                WHEN '1h' THEN 4 WHEN '2h' THEN 5 WHEN '4h' THEN 6
                WHEN '1d' THEN 7 WHEN '1w' THEN 8 ELSE 99
            END
    """)
    rows = cur.fetchall()

    if not rows:
        print("  (no OHLCV tables found — regex expects NAMEUSDT_Xm/h/d/w)")
        return

    headers = ["Timeframe", "# Tables", "Total size", "Rows (≈)", "Ø per table"]
    widths = [12, 10, 15, 15, 18]
    display = [(r[0], f"{r[1]:,}", r[2], f"{r[4]:,}", r[5]) for r in rows]
    print_table(display, headers, widths)

    # Totals
    total_tables = sum(r[1] for r in rows)
    total_bytes = sum(r[3] for r in rows)
    total_rows = sum(r[4] for r in rows)

    print()
    print_kv("OHLCV tables total:", f"{total_tables:,}")
    print_kv("OHLCV storage total:", _bytes_to_pretty(total_bytes))
    print_kv("OHLCV rows total:", f"{total_rows:,}")


def analyze_indicator_tables(cur) -> None:
    section("3. INDICATOR TABLES BY TIMEFRAME")

    cur.execute("""
        WITH ind_tables AS (
            SELECT
                c.relname,
                SUBSTRING(c.relname FROM '_([0-9]+[mhdw])_indicators$') AS timeframe,
                pg_total_relation_size(c.oid) AS total_size,
                pg_relation_size(c.oid) AS data_size,
                c.reltuples::bigint AS row_estimate,
                (SELECT COUNT(*) FROM pg_attribute WHERE attrelid = c.oid AND attnum > 0) AS n_columns
            FROM pg_class c
            WHERE c.relkind = 'r'
              AND c.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
              AND c.relname ~ '^[A-Z0-9]+USDT_[0-9]+[mhdw]_indicators$'
        )
        SELECT
            timeframe,
            COUNT(*) AS n_tables,
            pg_size_pretty(SUM(total_size)::bigint) AS total_size_pretty,
            SUM(total_size) AS total_size_bytes,
            SUM(row_estimate) AS total_rows,
            pg_size_pretty(AVG(total_size)::bigint) AS avg_per_table,
            AVG(n_columns)::int AS avg_cols
        FROM ind_tables
        GROUP BY timeframe
        ORDER BY
            CASE timeframe
                WHEN '5m' THEN 1 WHEN '15m' THEN 2 WHEN '30m' THEN 3
                WHEN '1h' THEN 4 WHEN '2h' THEN 5 WHEN '4h' THEN 6
                WHEN '1d' THEN 7 WHEN '1w' THEN 8 ELSE 99
            END
    """)
    rows = cur.fetchall()

    if not rows:
        print("  (no indicator tables found)")
        return

    headers = ["Timeframe", "# Tables", "Total size", "Rows (≈)", "Ø per table", "Ø columns"]
    widths = [12, 10, 15, 15, 18, 12]
    display = [(r[0], f"{r[1]:,}", r[2], f"{r[4]:,}", r[5], r[6]) for r in rows]
    print_table(display, headers, widths)

    total_tables = sum(r[1] for r in rows)
    total_bytes = sum(r[3] for r in rows)
    total_rows = sum(r[4] for r in rows)
    total_cols = sum(r[1] * r[6] for r in rows)  # n_tables * avg_cols

    print()
    print_kv("Indicator tables total:", f"{total_tables:,}")
    print_kv("Indicator storage total:", _bytes_to_pretty(total_bytes))
    print_kv("Indicator rows total:", f"{total_rows:,}")
    print_kv("Indicator pg_attribute entries:", f"{total_cols:,}")


def analyze_top_tables(cur) -> None:
    section("4. TOP 20 LARGEST TABLES")

    cur.execute("""
        SELECT
            c.relname,
            pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
            pg_size_pretty(pg_relation_size(c.oid)) AS data_size,
            c.reltuples::bigint AS row_estimate
        FROM pg_class c
        WHERE c.relkind = 'r'
          AND c.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
        ORDER BY pg_total_relation_size(c.oid) DESC
        LIMIT 20
    """)
    rows = cur.fetchall()

    headers = ["Table", "Total size", "Data", "Rows (≈)"]
    widths = [42, 15, 15, 15]
    display = [(r[0][:40], r[1], r[2], f"{r[3]:,}") for r in rows]
    print_table(display, headers, widths)


def analyze_system_catalog(cur) -> None:
    section("5. SYSTEM CATALOG SIZE")

    cur.execute("""
        SELECT
            relname,
            pg_size_pretty(pg_total_relation_size(oid)) AS size,
            reltuples::bigint AS rows
        FROM pg_class
        WHERE relname IN ('pg_class', 'pg_attribute', 'pg_index',
                         'pg_statistic', 'pg_constraint', 'pg_attrdef',
                         'pg_depend', 'pg_type')
          AND relkind = 'r'
        ORDER BY pg_total_relation_size(oid) DESC
    """)
    rows = cur.fetchall()

    headers = ["Catalog table", "Size", "Rows"]
    widths = [22, 12, 15]
    display = [(r[0], r[1], f"{r[2]:,}") for r in rows]
    print_table(display, headers, widths)

    print()
    print("  Benchmark values for 'normal' usage with 50-200 user tables:")
    print("    pg_attribute:  < 50,000 rows, < 20 MB")
    print("    pg_class:      < 5,000 rows,  < 5 MB")
    print("    pg_index:      < 3,000 rows,  < 2 MB")


def analyze_autovacuum(cur) -> None:
    section("6. AUTOVACUUM STATUS")

    # Tables that have not been vacuumed the longest
    cur.execute("""
        SELECT
            schemaname || '.' || relname AS full_name,
            last_vacuum,
            last_autovacuum,
            n_dead_tup AS dead_tuples,
            n_live_tup AS live_tuples,
            CASE WHEN n_live_tup > 0
                 THEN ROUND(n_dead_tup::numeric / n_live_tup * 100, 1)
                 ELSE 0 END AS dead_pct
        FROM pg_stat_user_tables
        WHERE schemaname = 'public'
          AND n_live_tup > 100
        ORDER BY dead_pct DESC
        LIMIT 15
    """)
    rows = cur.fetchall()

    subsection("Top 15 tables with most dead tuples")
    if not rows:
        print("  (no data)")
    else:
        headers = ["Table", "Dead%", "Dead", "Live", "Last autovac"]
        widths = [40, 8, 12, 12, 25]
        display = [(r[0][:38], f"{r[5]}%", f"{r[3]:,}", f"{r[4]:,}", str(r[2])[:19] if r[2] else "NIE") for r in rows]
        print_table(display, headers, widths)

    # Tables that have never been analysed
    cur.execute("""
        SELECT COUNT(*)
        FROM pg_stat_user_tables
        WHERE schemaname = 'public'
          AND last_analyze IS NULL
          AND last_autoanalyze IS NULL
    """)
    n_never_analyzed = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*)
        FROM pg_stat_user_tables
        WHERE schemaname = 'public'
          AND last_autovacuum IS NULL
          AND last_vacuum IS NULL
    """)
    n_never_vacuumed = cur.fetchone()[0]

    subsection("Autovacuum run status")
    print_kv("Tables never analysed:", f"{n_never_analyzed:,}")
    print_kv("Tables never vacuumed:", f"{n_never_vacuumed:,}")

    if n_never_analyzed > 100 or n_never_vacuumed > 100:
        print()
        print("  ⚠  Autovacuum cannot keep up. With this many tables, a single run")
        print("     often takes hours, then triggers again at the next scheduled run.")
        print("     Consequence: bloat grows slowly over time.")


def analyze_unused_indexes(cur) -> None:
    section("7. UNUSED INDEXES")

    cur.execute("""
        SELECT
            schemaname || '.' || relname AS table_name,
            indexrelname AS index_name,
            pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size,
            idx_scan
        FROM pg_stat_user_indexes s
        WHERE schemaname = 'public'
          AND idx_scan = 0
          AND pg_relation_size(s.indexrelid) > 10 * 1024 * 1024  -- > 10 MB
        ORDER BY pg_relation_size(s.indexrelid) DESC
        LIMIT 15
    """)
    rows = cur.fetchall()

    if not rows:
        print("  No unused indexes > 10 MB found.")
    else:
        headers = ["Table", "Index", "Size", "Scans"]
        widths = [35, 40, 10, 8]
        display = [(r[0][:33], r[1][:38], r[2], r[3]) for r in rows]
        print_table(display, headers, widths)


def analyze_consolidation_potential(cur) -> None:
    section("8. CONSOLIDATION POTENTIAL")

    # OHLCV-Tabellen
    cur.execute("""
        SELECT
            COUNT(*),
            SUM(pg_total_relation_size(oid))
        FROM pg_class
        WHERE relkind = 'r'
          AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
          AND relname ~ '^[A-Z0-9]+USDT_[0-9]+[mhdw]$'
    """)
    n_ohlcv, size_ohlcv = cur.fetchone()
    size_ohlcv = size_ohlcv or 0

    # Indikator-Tabellen
    cur.execute("""
        SELECT
            COUNT(*),
            SUM(pg_total_relation_size(oid))
        FROM pg_class
        WHERE relkind = 'r'
          AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
          AND relname ~ '^[A-Z0-9]+USDT_[0-9]+[mhdw]_indicators$'
    """)
    n_ind, size_ind = cur.fetchone()
    size_ind = size_ind or 0

    # Distinct Timeframes identifizieren
    cur.execute("""
        SELECT DISTINCT SUBSTRING(relname FROM '_([0-9]+[mhdw])$') AS tf
        FROM pg_class
        WHERE relkind = 'r'
          AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
          AND relname ~ '^[A-Z0-9]+USDT_[0-9]+[mhdw]$'
        ORDER BY tf
    """)
    ohlcv_tfs = [r[0] for r in cur.fetchall() if r[0]]

    cur.execute("""
        SELECT DISTINCT SUBSTRING(relname FROM '_([0-9]+[mhdw])_indicators$') AS tf
        FROM pg_class
        WHERE relkind = 'r'
          AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
          AND relname ~ '^[A-Z0-9]+USDT_[0-9]+[mhdw]_indicators$'
        ORDER BY tf
    """)
    ind_tfs = [r[0] for r in cur.fetchall() if r[0]]

    subsection("Current vs. after consolidation")
    print_kv("OHLCV tables current:", f"{n_ohlcv:,}")
    print_kv("OHLCV tables after consolidation:", f"{len(ohlcv_tfs)} (one per timeframe)")
    print_kv(
        " → Reduction:",
        f"{n_ohlcv - len(ohlcv_tfs):,} fewer tables ({100 * (n_ohlcv - len(ohlcv_tfs)) / max(n_ohlcv, 1):.1f}%)",
    )
    print()
    print_kv("Indicator tables current:", f"{n_ind:,}")
    print_kv("Indicator tables after consolidation:", f"{len(ind_tfs)} (one per timeframe)")
    print_kv(
        " → Reduction:",
        f"{n_ind - len(ind_tfs):,} fewer tables ({100 * (n_ind - len(ind_tfs)) / max(n_ind, 1):.1f}%)",
    )

    total_old = n_ohlcv + n_ind
    total_new = len(ohlcv_tfs) + len(ind_tfs)

    print()
    print_kv("Total table count current:", f"{total_old:,}")
    print_kv("Total table count after fix:", f"{total_new}")
    print_kv(
        " → Reduction:",
        f"{total_old - total_new:,} fewer tables ({100 * (total_old - total_new) / max(total_old, 1):.1f}%)",
    )

    subsection("Storage estimation with TimescaleDB compression")
    combined = size_ohlcv + size_ind
    print_kv("Current OHLCV + indicator storage:", _bytes_to_pretty(combined))
    print_kv("After consolidation (uncompressed):", _bytes_to_pretty(combined))
    print_kv(" → TimescaleDB 90% compression (typical):", _bytes_to_pretty(int(combined * 0.10)))
    print_kv(" → TimescaleDB 75% compression (conservative):", _bytes_to_pretty(int(combined * 0.25)))
    print()
    print("  OHLCV data typically compresses very well (90%+) because")
    print("  open/high/low/close/volume are sequential, correlated values.")
    print("  Indicator data compresses somewhat worse (70-85%) due to")
    print("  more variation, but still yields significant savings.")


# ────────────────────────────────────────────────────────────────────────────
# UTILS
# ────────────────────────────────────────────────────────────────────────────


def _bytes_to_pretty(n: int) -> str:
    """Formats bytes as MB/GB/TB string."""
    if n is None:
        return "0 B"
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ────────────────────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────────────────────


def main() -> None:
    print("╔" + "═" * 78 + "╗")
    print("║  CRYPTO TRADING BOT — DB SCHEMA ANALYSIS" + " " * 37 + "║")
    print("║  Read-only analysis, no data modified." + " " * 39 + "║")
    print("╚" + "═" * 78 + "╝")

    try:
        conn = get_db_connection()
    except Exception as e:
        print(f"\nFehler beim DB-Connect: {e}")
        sys.exit(1)

    try:
        with conn.cursor() as cur:
            analyze_overall(cur)
            analyze_ohlcv_tables(cur)
            analyze_indicator_tables(cur)
            analyze_top_tables(cur)
            analyze_system_catalog(cur)
            analyze_autovacuum(cur)
            analyze_unused_indexes(cur)
            analyze_consolidation_potential(cur)
    finally:
        conn.close()

    print()
    print("═" * 80)
    print("  Analysis complete. No changes made.")
    print("═" * 80)
    print()


if __name__ == "__main__":
    main()

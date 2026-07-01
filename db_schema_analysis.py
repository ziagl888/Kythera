#!/usr/bin/env python3
"""
db_schema_analysis.py — Analysiert den aktuellen DB-Status und quantifiziert
den Overhead des Tabellen-pro-Coin Schemas.

Das Skript ist READ-ONLY — es schreibt nichts, ändert nichts, blockiert nichts.
Du kannst es bedenkenlos gegen die Produktions-DB laufen lassen.

Usage:
    python db_schema_analysis.py

Output:
    1. Gesamt-Statistik (Anzahl Tabellen, Größe, pg_attribute-Einträge)
    2. OHLCV-Tabellen aggregiert nach Timeframe
    3. Indikator-Tabellen aggregiert nach Timeframe
    4. Größte Einzeltabellen (Top 20)
    5. Bloat-Schätzung
    6. System-Catalog-Größe
    7. Autovacuum-Status
    8. Empfohlene Konsolidierungs-Potenziale

Alle Zahlen in MB/GB (pg_size_pretty-Format), keine Raw-Bytes.
"""

from __future__ import annotations

import os
import sys

# sys.path für Import von core.database erweitern
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from core.database import get_db_connection
except ImportError:
    print("Fehler: core.database nicht gefunden.")
    print("Lege das Skript im Projekt-Root ab und starte es von dort.")
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
        print("  (keine Daten)")
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
    section("1. GESAMT-STATISTIK")

    # Anzahl User-Tables
    cur.execute("""
        SELECT COUNT(*) FROM pg_class
        WHERE relkind = 'r'
          AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
    """)
    n_tables = cur.fetchone()[0]

    # Anzahl Indexes
    cur.execute("""
        SELECT COUNT(*) FROM pg_class
        WHERE relkind = 'i'
          AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
    """)
    n_indexes = cur.fetchone()[0]

    # Gesamt-Größe User-Data
    cur.execute("""
        SELECT pg_size_pretty(SUM(pg_total_relation_size(c.oid))::bigint)
        FROM pg_class c
        WHERE relkind = 'r'
          AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
    """)
    total_size = cur.fetchone()[0]

    # pg_attribute, pg_class Gesamt-Einträge
    cur.execute("SELECT COUNT(*) FROM pg_attribute")
    n_attrs = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM pg_class")
    n_relations = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM pg_index")
    n_index_rows = cur.fetchone()[0]

    # DB-Gesamt-Größe
    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
    db_size = cur.fetchone()[0]

    print_kv("Datenbank-Gesamtgröße:", db_size)
    print_kv("User-Tabellen (public):", f"{n_tables:,}")
    print_kv("User-Indexe (public):", f"{n_indexes:,}")
    print_kv("User-Daten-Größe:", total_size)
    print()
    print_kv("System-Catalog pg_class-Einträge:", f"{n_relations:,}")
    print_kv("System-Catalog pg_attribute-Einträge:", f"{n_attrs:,}")
    print_kv("System-Catalog pg_index-Einträge:", f"{n_index_rows:,}")

    if n_tables > 1000:
        print()
        print("  ⚠  Anzahl User-Tabellen ist sehr hoch (>1000). PostgreSQL ist für")
        print("     hunderte bis wenige tausend Tabellen optimiert — bei mehr wird")
        print("     Query-Planning langsamer, Autovacuum läuft häufiger ins Timeout,")
        print("     pg_attribute wird disproportional groß.")


def analyze_ohlcv_tables(cur) -> None:
    section("2. OHLCV-TABELLEN NACH TIMEFRAME")

    # Aggregation über alle Coin_TF Tabellen
    # Pattern: {COIN}USDT_{TF}  wobei TF eines der bekannten ist
    cur.execute("""
        WITH ohlcv_tables AS (
            SELECT
                c.relname,
                -- Timeframe am Ende extrahieren
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
        print("  (keine OHLCV-Tabellen gefunden — Regex erwartet NAMEUSDT_Xm/h/d/w)")
        return

    headers = ["Timeframe", "# Tables", "Gesamtgröße", "Rows (≈)", "Ø pro Tabelle"]
    widths = [12, 10, 15, 15, 18]
    display = [(r[0], f"{r[1]:,}", r[2], f"{r[4]:,}", r[5]) for r in rows]
    print_table(display, headers, widths)

    # Summen
    total_tables = sum(r[1] for r in rows)
    total_bytes = sum(r[3] for r in rows)
    total_rows = sum(r[4] for r in rows)

    print()
    print_kv("OHLCV-Tabellen gesamt:", f"{total_tables:,}")
    print_kv("OHLCV-Storage gesamt:", _bytes_to_pretty(total_bytes))
    print_kv("OHLCV-Rows gesamt:", f"{total_rows:,}")


def analyze_indicator_tables(cur) -> None:
    section("3. INDIKATOR-TABELLEN NACH TIMEFRAME")

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
        print("  (keine Indikator-Tabellen gefunden)")
        return

    headers = ["Timeframe", "# Tables", "Gesamtgröße", "Rows (≈)", "Ø pro Tabelle", "Ø Spalten"]
    widths = [12, 10, 15, 15, 18, 12]
    display = [(r[0], f"{r[1]:,}", r[2], f"{r[4]:,}", r[5], r[6]) for r in rows]
    print_table(display, headers, widths)

    total_tables = sum(r[1] for r in rows)
    total_bytes = sum(r[3] for r in rows)
    total_rows = sum(r[4] for r in rows)
    total_cols = sum(r[1] * r[6] for r in rows)  # n_tables * avg_cols

    print()
    print_kv("Indikator-Tabellen gesamt:", f"{total_tables:,}")
    print_kv("Indikator-Storage gesamt:", _bytes_to_pretty(total_bytes))
    print_kv("Indikator-Rows gesamt:", f"{total_rows:,}")
    print_kv("Indikator pg_attribute-Einträge:", f"{total_cols:,}")


def analyze_top_tables(cur) -> None:
    section("4. TOP 20 GRÖSSTE TABELLEN")

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

    headers = ["Tabelle", "Gesamtgröße", "Daten", "Rows (≈)"]
    widths = [42, 15, 15, 15]
    display = [(r[0][:40], r[1], r[2], f"{r[3]:,}") for r in rows]
    print_table(display, headers, widths)


def analyze_system_catalog(cur) -> None:
    section("5. SYSTEM-CATALOG-GRÖSSE")

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

    headers = ["Catalog-Tabelle", "Größe", "Rows"]
    widths = [22, 12, 15]
    display = [(r[0], r[1], f"{r[2]:,}") for r in rows]
    print_table(display, headers, widths)

    print()
    print("  Richtwerte für einen 'normalen' Einsatz mit 50-200 User-Tabellen:")
    print("    pg_attribute:  < 50.000 rows, < 20 MB")
    print("    pg_class:      < 5.000 rows,  < 5 MB")
    print("    pg_index:      < 3.000 rows,  < 2 MB")


def analyze_autovacuum(cur) -> None:
    section("6. AUTOVACUUM-STATUS")

    # Tabellen die am längsten nicht vacuum'd wurden
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

    subsection("Top 15 Tabellen mit meisten Dead Tuples")
    if not rows:
        print("  (keine Daten)")
    else:
        headers = ["Tabelle", "Dead%", "Dead", "Live", "Last Autovac"]
        widths = [40, 8, 12, 12, 25]
        display = [(r[0][:38], f"{r[5]}%", f"{r[3]:,}", f"{r[4]:,}", str(r[2])[:19] if r[2] else "NIE") for r in rows]
        print_table(display, headers, widths)

    # Tabellen die nie analysiert wurden
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

    subsection("Autovacuum-Durchlauf-Status")
    print_kv("Tabellen nie analysiert:", f"{n_never_analyzed:,}")
    print_kv("Tabellen nie vacuum'd:", f"{n_never_vacuumed:,}")

    if n_never_analyzed > 100 or n_never_vacuumed > 100:
        print()
        print("  ⚠  Autovacuum kommt nicht hinterher. Bei dieser Tabellenanzahl")
        print("     braucht er oft Stunden pro Durchlauf, und triggert beim nächsten")
        print("     Scheduled-Run wieder neu. Konsequenz: Bloat wächst langsam an.")


def analyze_unused_indexes(cur) -> None:
    section("7. UNGENUTZTE INDIZES")

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
        print("  Keine ungenutzten Indizes > 10 MB gefunden.")
    else:
        headers = ["Tabelle", "Index", "Größe", "Scans"]
        widths = [35, 40, 10, 8]
        display = [(r[0][:33], r[1][:38], r[2], r[3]) for r in rows]
        print_table(display, headers, widths)


def analyze_consolidation_potential(cur) -> None:
    section("8. KONSOLIDIERUNGS-POTENZIAL")

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

    subsection("Aktuell vs. nach Konsolidierung")
    print_kv("OHLCV-Tabellen aktuell:", f"{n_ohlcv:,}")
    print_kv("OHLCV-Tabellen nach Konsolidierung:", f"{len(ohlcv_tfs)} (eine pro Timeframe)")
    print_kv(
        " → Reduktion:",
        f"{n_ohlcv - len(ohlcv_tfs):,} Tabellen weniger ({100 * (n_ohlcv - len(ohlcv_tfs)) / max(n_ohlcv, 1):.1f}%)",
    )
    print()
    print_kv("Indikator-Tabellen aktuell:", f"{n_ind:,}")
    print_kv("Indikator-Tabellen nach Konsolidierung:", f"{len(ind_tfs)} (eine pro Timeframe)")
    print_kv(
        " → Reduktion:",
        f"{n_ind - len(ind_tfs):,} Tabellen weniger ({100 * (n_ind - len(ind_tfs)) / max(n_ind, 1):.1f}%)",
    )

    total_old = n_ohlcv + n_ind
    total_new = len(ohlcv_tfs) + len(ind_tfs)

    print()
    print_kv("Tabellenanzahl gesamt aktuell:", f"{total_old:,}")
    print_kv("Tabellenanzahl gesamt nach Fix:", f"{total_new}")
    print_kv(
        " → Reduktion:",
        f"{total_old - total_new:,} Tabellen weniger ({100 * (total_old - total_new) / max(total_old, 1):.1f}%)",
    )

    subsection("Storage-Schätzung mit TimescaleDB-Compression")
    combined = size_ohlcv + size_ind
    print_kv("Aktuelles OHLCV + Indicator Storage:", _bytes_to_pretty(combined))
    print_kv("Nach Konsolidierung (unkomprimiert):", _bytes_to_pretty(combined))
    print_kv(" → TimescaleDB 90% Compression (typisch):", _bytes_to_pretty(int(combined * 0.10)))
    print_kv(" → TimescaleDB 75% Compression (konservativ):", _bytes_to_pretty(int(combined * 0.25)))
    print()
    print("  OHLCV-Daten komprimieren typischerweise sehr gut (90%+) weil")
    print("  open/high/low/close/volume sequenzielle, korrelierte Werte sind.")
    print("  Indikator-Daten komprimieren etwas schlechter (70-85%) weil")
    print("  mehr Variation, aber immer noch signifikante Ersparnis.")


# ────────────────────────────────────────────────────────────────────────────
# UTILS
# ────────────────────────────────────────────────────────────────────────────


def _bytes_to_pretty(n: int) -> str:
    """Formatiert Bytes als MB/GB/TB-String."""
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
    print("║  CRYPTO TRADING BOT — DB SCHEMA ANALYSE" + " " * 38 + "║")
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
    print("  Analyse abgeschlossen. Keine Änderungen vorgenommen.")
    print("═" * 80)
    print()


if __name__ == "__main__":
    main()

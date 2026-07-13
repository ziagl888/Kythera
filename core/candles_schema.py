# core/candles_schema.py — TimescaleDB target-hypertable DDL for the R1 migration.
#
# C-Gate Phase 0 (T-2026-CU-9050-118, umbrella T-2026-CU-9050-018, D-2026-CLD-109):
# create the two EMPTY target hypertables `candles` and `indicators`. `core.candles`
# keeps reading the OLD per-coin tables (KYTHERA_CANDLES_SOURCE=legacy) until the
# Phase-4 read-cutover, so this DDL is invisible to the running fleet and trivially
# reversible (DROP TABLE — nothing reads these tables yet).
#
# Blueprint: core/oi_5m.ensure_schema — same TimescaleDB idioms, same caller
# contract (self-committing schema setup, rollback-on-failure so a half-applied DDL
# never lingers on the shared connection).
#
# Decisions baked in (D-2026-CLD-109 / docs/TIMESCALE_R1_MIGRATION.md §5):
#   1. Retention UNLIMITED — no add_retention_policy, compression only.
#   2. REAL -> double precision for ALL numeric indicator columns (P3.12): sub-cent
#      coins lose precision under float4; compression makes the size delta moot.
#   3./4. (1d/1w WS removal, retrain rollout) live in later phases, not here.
#   - Compression config is DEFERRED to Phase 5 (operator decision 2026-07-13): this
#     module creates tables + hypertable + index ONLY. On empty tables the ALTER is
#     harmless, but the migration-phase plan schedules "Compression-Policies aktiv"
#     for the Phase-5 cleanup, so we keep the phase gates clean.
#
# The indicators column list is derived at build time from the ONE canonical source,
# 2_indicator_engine.get_indicator_definitions(), so the hypertable can never drift
# from what the engine/writer actually produce — the whole point of Report #18
# ("Schema-Aenderung in einer Spalte statt 9.297 Rollouts").
#
# create_hypertable uses the classic `chunk_time_interval` form (matching the one
# proven hypertable-creation site in this repo, core/oi_5m.ensure_schema, on the
# live TimescaleDB 2.26.3) rather than the by_range() dimension builder shown in
# the design doc §1 — the two are semantically equivalent here; this picks the
# in-repo precedent.

from __future__ import annotations

import importlib.util
import logging
import os

logger = logging.getLogger(__name__)

CANDLES_TABLE = "candles"
INDICATORS_TABLE = "indicators"
CHUNK_INTERVAL = "7 days"

# Fixed columns of the candles hypertable (docs/TIMESCALE_R1_MIGRATION.md §1).
# `tf` is now a real column (it used to be implicit in the per-coin table name);
# `is_closed` is the R1 contract (Binance kline `k['x']`, DEFAULT false for the
# forming candle). PK includes the partition column `open_time` as TimescaleDB
# requires.
_CANDLES_DDL = f"""
    CREATE TABLE IF NOT EXISTS {CANDLES_TABLE} (
        symbol      text        NOT NULL,
        tf          text        NOT NULL,
        open_time   timestamptz NOT NULL,
        open        double precision,
        high        double precision,
        low         double precision,
        close       double precision,
        volume      double precision,
        is_closed   boolean     NOT NULL DEFAULT false,
        PRIMARY KEY (symbol, tf, open_time)
    )
"""


def _load_indicator_definitions() -> dict[str, str]:
    """Load get_indicator_definitions() from 2_indicator_engine.py — the single
    source of truth for the indicator column set. The engine's module name starts
    with a digit, so it is loaded via importlib (mirror of
    backtest/test_gap_continuity.py). Lazy: only imported when actually building
    DDL, so importing core.candles_schema stays cheap and side-effect-free.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "2_indicator_engine.py")
    spec = importlib.util.spec_from_file_location("kythera_indicator_engine", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"could not load indicator-engine spec from {path!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.get_indicator_definitions()


def _pg_type(engine_type: str) -> str:
    """Map the engine's stored type to the hypertable type: REAL -> double
    precision (P3.12), everything else (TEXT) stays as-is."""
    return "double precision" if engine_type.strip().upper() == "REAL" else "text"


def build_indicators_ddl(definitions: dict[str, str] | None = None) -> str:
    """CREATE TABLE for the indicators hypertable: fixed keys + is_closed + close,
    then every engine-defined indicator column (REAL->double, TEXT->text).

    Column names are lowercased to match the unquoted identifiers the writer emits
    (Postgres folds unquoted identifiers to lower case, so the legacy per-coin
    `{SYM}_{tf}_indicators` tables store e.g. `rsi_6`, not `RSI_6`).

    `definitions` is injectable for DB-free unit tests; None -> canonical engine
    definitions.
    """
    if definitions is None:
        definitions = _load_indicator_definitions()
    col_lines = ",\n".join(f"        {name.lower()} {_pg_type(t)}" for name, t in definitions.items())
    return f"""
    CREATE TABLE IF NOT EXISTS {INDICATORS_TABLE} (
        symbol      text        NOT NULL,
        tf          text        NOT NULL,
        open_time   timestamptz NOT NULL,
        is_closed   boolean     NOT NULL DEFAULT false,
        close       double precision,
{col_lines},
        PRIMARY KEY (symbol, tf, open_time)
    )
"""


def ensure_hypertables(conn) -> None:
    """Idempotently create the empty `candles` and `indicators` hypertables plus
    their (symbol, tf, open_time DESC) indexes. Self-committing; on failure rolls
    back the half-applied DDL so the shared connection stays clean (oi_5m pattern).

    Compression/retention are intentionally NOT configured here — deferred to
    Phase 5 (see module header). Retention stays unlimited by design.
    """
    try:
        _ensure_hypertables_inner(conn)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            logger.exception("Rollback after failed candles/indicators schema setup failed")
        raise


def _ensure_hypertables_inner(conn) -> None:
    indicators_ddl = build_indicators_ddl()
    with conn.cursor() as cur:
        # --- candles ---
        cur.execute(_CANDLES_DDL)
        cur.execute(
            f"SELECT create_hypertable(%s, 'open_time', "
            f"chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}', if_not_exists => TRUE)",
            (CANDLES_TABLE,),
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{CANDLES_TABLE}_sym_tf_ot ON {CANDLES_TABLE} (symbol, tf, open_time DESC)"
        )
        # --- indicators ---
        cur.execute(indicators_ddl)
        cur.execute(
            f"SELECT create_hypertable(%s, 'open_time', "
            f"chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}', if_not_exists => TRUE)",
            (INDICATORS_TABLE,),
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{INDICATORS_TABLE}_sym_tf_ot "
            f"ON {INDICATORS_TABLE} (symbol, tf, open_time DESC)"
        )
    conn.commit()
    logger.info(
        "✅ Hypertables %s + %s bereit (chunk=%s, compression DEFERRED to Phase 5, retention unlimited)",
        CANDLES_TABLE,
        INDICATORS_TABLE,
        CHUNK_INTERVAL,
    )


def _print_ddl() -> None:
    """Print the exact DDL this module would execute (dry run, no DB)."""
    print(_CANDLES_DDL.strip())
    print(
        f"SELECT create_hypertable('{CANDLES_TABLE}', 'open_time', "
        f"chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}', if_not_exists => TRUE);"
    )
    print(f"CREATE INDEX IF NOT EXISTS idx_{CANDLES_TABLE}_sym_tf_ot ON {CANDLES_TABLE} (symbol, tf, open_time DESC);")
    print()
    print(build_indicators_ddl().strip())
    print(
        f"SELECT create_hypertable('{INDICATORS_TABLE}', 'open_time', "
        f"chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}', if_not_exists => TRUE);"
    )
    print(
        f"CREATE INDEX IF NOT EXISTS idx_{INDICATORS_TABLE}_sym_tf_ot "
        f"ON {INDICATORS_TABLE} (symbol, tf, open_time DESC);"
    )


def main(argv: list[str] | None = None) -> int:
    """One-shot Phase-0 runner. Defaults to a DB-free dry run; --execute is the
    explicit, operator-gated switch that actually applies the DDL to the live DB."""
    import argparse

    parser = argparse.ArgumentParser(
        description="C-Gate Phase 0: create the empty candles/indicators hypertables (idempotent).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="apply the DDL to the live DB (default: dry-run print only)",
    )
    args = parser.parse_args(argv)

    if not args.execute:
        _print_ddl()
        print("\n-- dry run only. Re-run with --execute to apply to the live DB.")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from core.database import db_connection

    with db_connection() as conn:
        ensure_hypertables(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""tools/recompute_indicators.py — null the P1.13 warmup head rows in the
*_indicators tables, WITHOUT recomputing the mid-band (T-2026-CU-9050-061).

WHY NOT A FULL RECOMPUTE (measured, T-061)
    The obvious approach — recompute each table and upsert — is NOT
    position-stable. The live DB was written incrementally (a 1000-candle window
    per run, over months, partly by older engine builds), so today's engine does
    not reproduce the stored mid-band values. Measured on MLNUSDT_1h, a full
    recompute of just the four P1.13 columns differs from the DB on hundreds of
    MID-BAND rows per column — up to 48% on rsi_14 — not only the warmup heads.
    A recompute would therefore shift the live serving distribution and decouple
    training from serving. See the dry-run: UNEXPECTED (mid-band) drift is large.

WHAT THIS DOES INSTEAD
    P1.13 (git 3c5d133) only changes the warmup HEAD rows of four rolling
    families (WMA_*, RSI_*, BOLL_*_20, DONCHIAN_*): 0/50 -> NULL. The retrain
    only needs those heads to be NULL so the replay's dropna() drops them. So we
    let the engine tell us WHERE the warmup ends (the rows it now returns as
    NaN) and write ONLY NULL there — never a recomputed value. The mid-band is
    never touched, so the operation is position-stable by construction.

SAFETY MODEL
    --dry-run (default) writes NOTHING. Per table it recomputes the P1.13
    columns, and for each such column reports:
      - HEADS   : rows where recompute is NaN and the DB is finite -> to be
                  nulled. This is the intended change.
      - MIDBAND : rows where BOTH are finite but differ (rel > 1e-4). This is
                  what a recompute WOULD wrongly change; head-nulling leaves it
                  untouched. Reported so the gap is visible, never written.
    --execute issues, per table and per P1.13 column, a single
      UPDATE <tbl> SET <col>=NULL WHERE open_time = ANY(<head_times>)
    Only NULLs, only head rows, only the four families. Idempotent and
    resumable via --state.

USAGE
    python tools/recompute_indicators.py --dry-run --sample 30   # read-only proof
    python tools/recompute_indicators.py --execute               # operator-gated

RSI REWRITE MODE (--rsi-rewrite, T-2026-CU-9050-099 / P2.12 follow-up)
    Since T-2026-CU-9050-095 the engine computes true Wilder RSI
    (alpha=1/period); rows written before the 2026-07-12 fleet restart hold the
    old ewm(span) values (~4.8 points off on rsi_14). Until the history is
    rewritten, every consumer of rsi_* reads a TWO-DOMAIN column. This mode
    rewrites the rsi_* columns over the FULL history with the engine's Wilder
    recompute. That is deliberately NOT position-stable — it is the
    operator-approved domain migration from P2.12, the opposite trade-off of the
    head-nulling above (which must never touch the mid-band). Same safety rails:
    --dry-run (default, read-only) measures how many cells would change and by
    how much; --execute writes batched per-column UPDATEs, tail-guarded against
    the bot-2 race, idempotent and resumable via its own state file. Refuses to
    run if the loaded engine is not the Wilder build (parity self-check), so an
    old checkout can never rewrite the history with span-RSI.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.config import INDICATOR_TIMEFRAMES  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from tools.research_dataset_common import set_low_priority  # noqa: E402

# P1.13 changed exactly these four rolling families (git 3c5d133): the warmup
# head rows now flow as NaN instead of fabricated 0 (WMA/BOLL/DONCHIAN) or 50
# (RSI). MA_* was deliberately left untouched (no active consumer).
P113_PREFIXES = ("WMA_", "RSI_", "BOLL_", "DONCHIAN_")
TAIL_ROWS = 3  # newest rows bot 2 may have touched mid-read


def _load_engine():
    spec = importlib.util.spec_from_file_location("indicator_engine", os.path.join(_ROOT, "2_indicator_engine.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ENG = _load_engine()


def list_indicator_tables(conn) -> list[tuple[str, str]]:
    """Every (symbol, timeframe) that has both an OHLCV and an _indicators table."""
    df = pd.read_sql_query(
        """
        SELECT tablename FROM pg_tables
        WHERE schemaname='public' AND tablename LIKE %s
        ORDER BY tablename
        """,
        conn,
        params=("%\\_indicators",),
    )
    out = []
    tfs = set(INDICATOR_TIMEFRAMES)
    for t in df["tablename"]:
        base = t[: -len("_indicators")]
        # split trailing timeframe token
        for tf in tfs:
            if base.endswith("_" + tf):
                out.append((base[: -(len(tf) + 1)], tf))
                break
    return out


def recompute_frame(conn, symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Full-history recompute for one table — mirrors process_coin_task's
    last_ind_time-is-None path (lookback to 2020), minus the write."""
    ohlcv = f'"{symbol}_{timeframe}"'
    if not ENG.table_exists(conn, ohlcv):
        return None
    df_raw = pd.read_sql_query(f"SELECT * FROM {ohlcv} ORDER BY open_time ASC", conn)
    if df_raw.empty or len(df_raw) < 50:
        return None
    df_raw["open_time"] = pd.to_datetime(df_raw["open_time"], utc=True)
    if "symbol" not in df_raw.columns:
        df_raw["symbol"] = symbol
    df_ind = ENG.calculate_indicators_optimized(df_raw, timeframe)
    return df_ind


REL_TOL = 1e-4  # a mid-band value counts as "would change" above this rel diff


def head_null_plan(db: pd.DataFrame, rc: pd.DataFrame, p113_cols: list[str]) -> dict:
    """For each P1.13 column, find the warmup head rows to NULL and measure the
    mid-band gap we deliberately leave alone.

    Returns per-column {head_times: [Timestamp...], midband: int, midband_max: float}
    plus aggregate counts. Only rows where recompute is NaN AND db is finite are
    head rows; the newest TAIL_ROWS are excluded (bot-2 race).
    """
    db = db.copy()
    rc = rc.copy()
    db.columns = [c.upper() for c in db.columns]
    rc.columns = [c.upper() for c in rc.columns]
    db["OPEN_TIME"] = pd.to_datetime(db["OPEN_TIME"], utc=True)
    rc["OPEN_TIME"] = pd.to_datetime(rc["OPEN_TIME"], utc=True)
    merged = db.merge(rc, on="OPEN_TIME", how="inner", suffixes=("_db", "_rc"))
    out = {"rows": len(merged), "heads": 0, "midband": 0, "midband_max": 0.0, "cols": {}}
    if merged.empty:
        return out
    tail_cut = merged["OPEN_TIME"].nlargest(TAIL_ROWS).min()

    for col in p113_cols:
        col = col.upper()
        cdb, crc = f"{col}_db", f"{col}_rc"
        if cdb not in merged or crc not in merged:
            continue
        a = pd.to_numeric(merged[cdb], errors="coerce").to_numpy(dtype=np.float64)
        b = pd.to_numeric(merged[crc], errors="coerce").to_numpy(dtype=np.float64)
        a_nan, b_nan = np.isnan(a), np.isnan(b)
        not_tail = (merged["OPEN_TIME"] < tail_cut).to_numpy()
        # head rows to null: recompute NaN, db finite (excluding the tail)
        head_mask = b_nan & ~a_nan & not_tail
        # mid-band gap we leave alone: both finite, differ beyond rel tol
        both = ~a_nan & ~b_nan & not_tail
        rel = np.zeros(len(a))
        rel[both] = np.abs(a[both] - b[both]) / (np.abs(a[both]) + 1e-9)
        mid_mask = both & (rel > REL_TOL)
        head_times = list(merged.loc[head_mask, "OPEN_TIME"])
        out["cols"][col] = {
            "head_times": head_times,
            "midband": int(mid_mask.sum()),
            "midband_max": float(rel[mid_mask].max()) if mid_mask.any() else 0.0,
        }
        out["heads"] += len(head_times)
        out["midband"] += int(mid_mask.sum())
        out["midband_max"] = max(out["midband_max"], out["cols"][col]["midband_max"])
    return out


# ── RSI rewrite mode (P2.12 follow-up, T-2026-CU-9050-099) ───────────────────

RSI_PREFIXES = ("RSI_",)
# RSI lives on a 0-100 scale and the columns are REAL (float4, ~7 significant
# digits). 1e-3 RSI points sits far below any trading significance and far
# above float4 round-trip noise (~4e-6 at RSI 50) — so an already-rewritten
# cell never re-registers as a change (idempotent dry-run after execute).
RSI_ABS_TOL = 1e-3

# Engine parity witness: the classic 20-close Wilder demo series, run through
# up/down = diff().clip(); ewm(alpha=1/14, adjust=False) — i.e. the exact
# T-2026-CU-9050-095 semantics (ewm-seeded Wilder, NOT the SMA-seeded textbook
# variant, which would read 43.99 here). Recomputed independently with the
# fleet interpreter; if the loaded engine returns anything else, it is not the
# Wilder build and must never rewrite the history.
_WILDER_WITNESS_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]
_WILDER_WITNESS_RSI14 = 43.19652040095396


def assert_wilder_engine(eng=None) -> None:
    """Abort unless the loaded engine computes T-095 Wilder RSI.

    The tool imports the engine from the checkout it runs in. An old checkout
    (pre-T-095) would happily "rewrite" the history back to ewm(span) values —
    the exact opposite of the migration. One fixed witness series pins the
    semantics; a span-based build reads ~46.7 here and fails the check.
    """
    eng = eng or ENG
    got = float(eng.calculate_rsi(pd.Series(_WILDER_WITNESS_CLOSES), period=14).iloc[-1])
    if abs(got - _WILDER_WITNESS_RSI14) > 1e-6:
        sys.exit(
            f"engine parity check FAILED: calculate_rsi witness = {got!r}, expected "
            f"{_WILDER_WITNESS_RSI14!r} (T-2026-CU-9050-095 Wilder). Refusing to rewrite "
            "RSI history from a non-Wilder engine build."
        )


def rsi_rewrite_plan(db: pd.DataFrame, rc: pd.DataFrame, rsi_cols: list[str]) -> dict:
    """For each rsi_* column, plan the full-history rewrite to the Wilder domain.

    A cell is written when the recomputed value differs from the stored one:
      - both finite and |delta| > RSI_ABS_TOL  -> new Wilder value
      - stored finite, recompute NaN           -> NULL (warmup head / flat series)
      - stored NULL,   recompute finite        -> new Wilder value (rare backfill)
    Unchanged cells are skipped so a second run is a no-op. The newest TAIL_ROWS
    are excluded (bot-2 race), mirroring head_null_plan.

    Returns per-column {times: [...], values: [float|None ...]} plus aggregate
    counts and the finite->finite delta stats.
    """
    db = db.copy()
    rc = rc.copy()
    db.columns = [c.upper() for c in db.columns]
    rc.columns = [c.upper() for c in rc.columns]
    db["OPEN_TIME"] = pd.to_datetime(db["OPEN_TIME"], utc=True)
    rc["OPEN_TIME"] = pd.to_datetime(rc["OPEN_TIME"], utc=True)
    merged = db.merge(rc, on="OPEN_TIME", how="inner", suffixes=("_db", "_rc"))
    out = {
        "rows": len(merged),
        "cells": 0,
        "to_null": 0,
        "null_fills": 0,
        "delta_sum": 0.0,
        "delta_max": 0.0,
        "cols": {},
    }
    if merged.empty:
        return out
    tail_cut = merged["OPEN_TIME"].nlargest(TAIL_ROWS).min()
    not_tail = (merged["OPEN_TIME"] < tail_cut).to_numpy()

    for col in rsi_cols:
        col = col.upper()
        cdb, crc = f"{col}_db", f"{col}_rc"
        if cdb not in merged or crc not in merged:
            continue
        a = pd.to_numeric(merged[cdb], errors="coerce").to_numpy(dtype=np.float64)
        b = pd.to_numeric(merged[crc], errors="coerce").to_numpy(dtype=np.float64)
        a_nan, b_nan = np.isnan(a), np.isnan(b)
        both = ~a_nan & ~b_nan
        delta = np.zeros(len(a))
        delta[both] = np.abs(a[both] - b[both])
        write_mask = not_tail & (
            (both & (delta > RSI_ABS_TOL))  # domain shift on a finite cell
            | (~a_nan & b_nan)  # stored value where Wilder says NaN -> NULL
            | (a_nan & ~b_nan)  # stored NULL where Wilder is defined -> fill
        )
        times = list(merged.loc[write_mask, "OPEN_TIME"])
        values = [None if np.isnan(v) else float(v) for v in b[write_mask]]
        changed_finite = write_mask & both
        out["cols"][col] = {"times": times, "values": values}
        out["cells"] += len(times)
        out["to_null"] += int((write_mask & ~a_nan & b_nan).sum())
        out["null_fills"] += int((write_mask & a_nan & ~b_nan).sum())
        out["delta_sum"] += float(delta[changed_finite].sum())
        out["delta_max"] = max(out["delta_max"], float(delta[changed_finite].max()) if changed_finite.any() else 0.0)
    return out


def rewrite_rsi_rows(cur, ind_tbl: str, cols_plan: dict) -> int:
    """Issue one batched UPDATE per rsi_* column with pending writes.

    Times and values travel as parallel arrays through unnest — parameterised,
    never string-interpolated; None entries become SQL NULL. Returns the number
    of columns updated. A column with no pending cells is skipped.
    """
    n = 0
    for col, info in cols_plan.items():
        if not info["times"]:
            continue
        cur.execute(
            f'UPDATE {ind_tbl} AS t SET "{col.lower()}" = u.val '
            f"FROM (SELECT unnest(%s::timestamptz[]) AS ot, unnest(%s::float8[]) AS val) AS u "
            f"WHERE t.open_time = u.ot",
            (info["times"], info["values"]),
        )
        n += 1
    return n


def null_head_rows(cur, ind_tbl: str, cols_plan: dict) -> int:
    """Issue one UPDATE ... SET col=NULL per P1.13 column with head rows.

    Extracted from main() so the SQL construction and the head_times handoff are
    testable without a live DB. Returns the number of columns updated. Writes
    ONLY NULL, ONLY at the given head times; a column with no head rows is
    skipped (no empty UPDATE).
    """
    n = 0
    for col, info in cols_plan.items():
        if not info["head_times"]:
            continue
        cur.execute(
            f'UPDATE {ind_tbl} SET "{col.lower()}" = NULL WHERE open_time = ANY(%s)',
            (info["head_times"],),
        )
        n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--execute", action="store_true")
    ap.add_argument(
        "--sample",
        type=int,
        default=0,
        help="limit to N tables (spread across timeframes) — works in BOTH modes, e.g. a bounded --execute test",
    )
    ap.add_argument("--timeframes", default="", help="comma list to restrict, e.g. 1h,4h")
    ap.add_argument(
        "--rsi-rewrite",
        action="store_true",
        help="P2.12 mode: rewrite the FULL rsi_* history to the Wilder domain (T-099) instead of head-nulling",
    )
    default_state = os.path.join(_ROOT, "control", "recompute_progress.json")
    ap.add_argument("--state", default=default_state)
    args = ap.parse_args()
    execute = args.execute
    if args.rsi_rewrite and args.state == default_state:
        # own resume ledger — the two modes must never share progress
        args.state = os.path.join(_ROOT, "control", "rsi_rewrite_progress.json")

    set_low_priority()  # be a good neighbour to the live fleet (CPU-throttled)

    if args.rsi_rewrite:
        assert_wilder_engine()  # never rewrite history from a pre-T-095 engine

    conn = get_db_connection()
    if not execute:
        conn.set_session(readonly=True)
    prefixes = RSI_PREFIXES if args.rsi_rewrite else P113_PREFIXES
    p113_cols = [c for c in ENG.get_indicator_definitions().keys() if c.upper().startswith(prefixes)]

    tables = list_indicator_tables(conn)
    if args.timeframes:
        keep = set(args.timeframes.split(","))
        tables = [t for t in tables if t[1] in keep]

    if args.sample:
        # Spread the sample across timeframes AND coin ages for a fair estimate.
        # Works in BOTH modes: `--execute --sample N` is the bounded test run
        # (write only N tables) before committing to the full ~4197.
        by_tf: dict[str, list] = {}
        for sym, tf in tables:
            by_tf.setdefault(tf, []).append((sym, tf))
        per = max(1, args.sample // max(1, len(by_tf)))
        sampled = []
        for lst in by_tf.values():
            step = max(1, len(lst) // per)
            sampled.extend(lst[::step][:per])
        tables = sampled[: args.sample]

    mode_name = ("RSI-REWRITE " if args.rsi_rewrite else "") + ("EXECUTE" if execute else "DRY-RUN")
    print(f"mode={mode_name} | tables={len(tables)} | cols={len(p113_cols)}")

    done = set()
    if execute and os.path.exists(args.state):
        with open(args.state, encoding="utf-8") as fh:
            done = set(tuple(x) for x in json.load(fh).get("done", []))
        print(f"resume: {len(done)} tables already done")

    t0 = time.time()
    total_heads = total_midband = 0
    midband_max = 0.0
    total_cells = total_to_null = total_null_fills = 0
    delta_sum = 0.0
    delta_max = 0.0
    timings = []
    written = 0

    for i, (sym, tf) in enumerate(tables, 1):
        if execute and (sym, tf) in done:
            continue
        ts = time.time()
        try:
            rc = recompute_frame(conn, sym, tf)
        except Exception as e:
            conn.rollback()
            print(f"  ! {sym}_{tf}: recompute failed: {e}")
            continue
        if rc is None:
            continue

        ind_tbl = f'"{sym}_{tf}_indicators"'
        try:
            db = pd.read_sql_query(f"SELECT * FROM {ind_tbl} ORDER BY open_time ASC", conn)
        except Exception:
            conn.rollback()
            continue
        if args.rsi_rewrite:
            plan = rsi_rewrite_plan(db, rc, p113_cols)
            total_cells += plan["cells"]
            total_to_null += plan["to_null"]
            total_null_fills += plan["null_fills"]
            delta_sum += plan["delta_sum"]
            delta_max = max(delta_max, plan["delta_max"])
        else:
            plan = head_null_plan(db, rc, p113_cols)
            total_heads += plan["heads"]
            total_midband += plan["midband"]
            midband_max = max(midband_max, plan["midband_max"])

        if execute:
            # NULL only the warmup head rows (P1.13 mode) or write the Wilder
            # values (RSI mode), per column. Per-table try/except like the
            # phases above: a single table's failure (lock timeout against
            # bot 2, degenerate column) must roll back that table and skip on,
            # not abort the whole run. Resume is idempotent, so a skipped table
            # is retried next run.
            try:
                with conn.cursor() as cur:
                    if args.rsi_rewrite:
                        rewrite_rsi_rows(cur, ind_tbl, plan["cols"])
                    else:
                        null_head_rows(cur, ind_tbl, plan["cols"])
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"  ! {sym}_{tf}: update failed, skipping: {e}")
                timings.append(time.time() - ts)
                continue
            written += 1
            done.add((sym, tf))
            if written % 50 == 0:
                os.makedirs(os.path.dirname(args.state), exist_ok=True)
                with open(args.state, "w", encoding="utf-8") as fh:
                    json.dump({"done": sorted(done)}, fh)
        timings.append(time.time() - ts)

        if i % 10 == 0 or i == len(tables):
            print(f"  {i}/{len(tables)} | {time.time() - t0:.0f}s | last {timings[-1]:.2f}s/table")

    if execute:
        os.makedirs(os.path.dirname(args.state), exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump({"done": sorted(done)}, fh)
        if args.rsi_rewrite:
            print(
                f"\nRSI-REWRITE done: {written} tables, {total_cells} cells written "
                f"({total_to_null} -> NULL, {total_null_fills} NULL fills) in {time.time() - t0:.0f}s"
            )
        else:
            print(f"\nEXECUTE done: {written} tables, {total_heads} head cells nulled in {time.time() - t0:.0f}s")
    else:
        n = max(1, len(timings))
        avg = sum(timings) / n
        # full population for the extrapolation — reuse the open conn, don't leak a new one
        total_tables = len(tables) if not args.sample else len(list_indicator_tables(conn))
        if args.rsi_rewrite:
            changed_finite = total_cells - total_to_null - total_null_fills
            print("\n=== DRY-RUN SUMMARY (rsi Wilder rewrite) ===")
            print(f"sampled tables        : {len(timings)}")
            print(f"cells to write        : {total_cells}  (on {','.join(RSI_PREFIXES)}, tail-guarded)")
            print(f"  finite domain shifts: {changed_finite}")
            print(f"  finite -> NULL      : {total_to_null}")
            print(f"  NULL fills          : {total_null_fills}")
            if changed_finite:
                print(f"avg |delta| RSI points: {delta_sum / changed_finite:.3f}  (max {delta_max:.3f})")
        else:
            print("\n=== DRY-RUN SUMMARY (head-nulling) ===")
            print(f"sampled tables        : {len(timings)}")
            print(f"HEAD cells to NULL    : {total_heads}  (recompute NaN & db finite, on {','.join(P113_PREFIXES)})")
            print(f"MID-BAND left untouched: {total_midband}  (recompute would change these; we DON'T)")
            print(f"  worst mid-band rel  : {midband_max:.2e}")
        print(f"avg time/table        : {avg:.2f}s")
        print(
            f"full-run estimate     : {total_tables} tables -> "
            f"{avg * total_tables / 60:.0f} min single-thread, ~{avg * total_tables / 60 / 3:.0f} min at 3 workers"
        )

    conn.close()


if __name__ == "__main__":
    main()

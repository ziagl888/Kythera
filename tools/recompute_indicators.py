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


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--execute", action="store_true")
    ap.add_argument("--sample", type=int, default=0, help="dry-run: recompute only N tables (spread across timeframes)")
    ap.add_argument("--timeframes", default="", help="comma list to restrict, e.g. 1h,4h")
    ap.add_argument("--state", default=os.path.join(_ROOT, "control", "recompute_progress.json"))
    args = ap.parse_args()
    execute = args.execute

    set_low_priority()  # be a good neighbour to the live fleet (CPU-throttled)

    conn = get_db_connection()
    if not execute:
        conn.set_session(readonly=True)
    p113_cols = [c for c in ENG.get_indicator_definitions().keys() if c.upper().startswith(P113_PREFIXES)]

    tables = list_indicator_tables(conn)
    if args.timeframes:
        keep = set(args.timeframes.split(","))
        tables = [t for t in tables if t[1] in keep]

    if not execute and args.sample:
        # spread the sample across timeframes AND coin ages for a fair estimate
        by_tf: dict[str, list] = {}
        for sym, tf in tables:
            by_tf.setdefault(tf, []).append((sym, tf))
        per = max(1, args.sample // max(1, len(by_tf)))
        sampled = []
        for lst in by_tf.values():
            step = max(1, len(lst) // per)
            sampled.extend(lst[::step][:per])
        tables = sampled[: args.sample]

    print(f"mode={'EXECUTE' if execute else 'DRY-RUN'} | tables={len(tables)} | p113_cols={len(p113_cols)}")

    done = set()
    if execute and os.path.exists(args.state):
        with open(args.state, encoding="utf-8") as fh:
            done = set(tuple(x) for x in json.load(fh).get("done", []))
        print(f"resume: {len(done)} tables already done")

    t0 = time.time()
    total_heads = total_midband = 0
    midband_max = 0.0
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
        plan = head_null_plan(db, rc, p113_cols)
        total_heads += plan["heads"]
        total_midband += plan["midband"]
        midband_max = max(midband_max, plan["midband_max"])

        if execute:
            # NULL only the warmup head rows, per P1.13 column. Never a value.
            with conn.cursor() as cur:
                for col, info in plan["cols"].items():
                    if not info["head_times"]:
                        continue
                    cur.execute(
                        f'UPDATE {ind_tbl} SET "{col.lower()}" = NULL WHERE open_time = ANY(%s)',
                        (info["head_times"],),
                    )
            conn.commit()
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
        print(f"\nEXECUTE done: {written} tables, {total_heads} head cells nulled in {time.time() - t0:.0f}s")
    else:
        n = max(1, len(timings))
        avg = sum(timings) / n
        total_tables = len(list_indicator_tables(get_db_connection()))
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

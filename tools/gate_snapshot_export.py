# tools/gate_snapshot_export.py — one-shot read-only export for the funding×liq gate pilot
"""Pulls the four study tables (tools/gate_snapshot.TABLES) from live Postgres
into one DuckDB snapshot file. T-2026-KYT-9050-120.

READ-ONLY: only the SELECTs in gate_snapshot.TABLES are executed. Runs in a
VPS session (the build machine has no DB credentials, Hard Rule 1). Output is
gitignored (.local/).

Invocation (VPS session):
  python tools/gate_snapshot_export.py                # → .local/gate_snapshots/gate_snapshot_<utc-date>.duckdb
  python tools/gate_snapshot_export.py --out path.duckdb

The whole pull is a handful of full-table SELECTs — deliberately NOT an
incremental watermark job (rationale in tools/gate_snapshot.py). Re-running
overwrites the day's file; the study reads the snapshot, never Postgres.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

from core.database import get_db_connection  # noqa: E402
from tools.gate_snapshot import TABLES, write_snapshot  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    now = datetime.now(timezone.utc)
    default_out = os.path.join(".local", "gate_snapshots", f"gate_snapshot_{now:%Y%m%d}.duckdb")
    ap.add_argument("--out", default=default_out, help="snapshot file (default: %(default)s)")
    args = ap.parse_args()

    conn = get_db_connection()
    try:
        dfs: dict[str, pd.DataFrame] = {}
        for name, sql in TABLES.items():
            df = pd.read_sql(sql, conn)
            dfs[name] = df
            ts_cols = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c].dtype)]
            span = ""
            if len(df) and ts_cols:
                c = ts_cols[0]
                span = f"  [{c}: {df[c].min()} .. {df[c].max()}]"
            print(f"{name:10s} {len(df):>9,d} rows{span}", flush=True)

        # T-123: market-squeeze episodes + targeted ticker_10s slices around
        # them, so the flatten replay can price counterfactual closes. Episodes
        # are minutes-per-days rare, so the slices stay small — this is what
        # makes a price table affordable inside the snapshot at all.
        from tools.funding_liq_gate_study import market_breadth_minutes, squeeze_episodes

        eps = squeeze_episodes(market_breadth_minutes(dfs["liq"]))
        dfs["episodes"] = eps
        slices = []
        for ep in eps.itertuples(index=False):
            t0 = ep.start - pd.Timedelta(minutes=10)
            t1 = ep.end + pd.Timedelta(minutes=5)
            slices.append(
                pd.read_sql(
                    "SELECT ts, symbol, price FROM ticker_10s WHERE ts BETWEEN %(t0)s AND %(t1)s",
                    conn,
                    params={"t0": t0, "t1": t1},
                )
            )
        ticker = (
            pd.concat(slices, ignore_index=True).drop_duplicates(["symbol", "ts"])
            if slices
            else pd.DataFrame(columns=["ts", "symbol", "price"])
        )
        dfs["ticker_slices"] = ticker
        print(f"episodes   {len(eps):>9,d} rows  ticker_slices {len(ticker):,d} rows", flush=True)
    finally:
        conn.close()

    write_snapshot(dfs, args.out, created_at_utc=now.isoformat())
    size_mb = os.path.getsize(args.out) / 1e6
    print(f"\nsnapshot written: {args.out} ({size_mb:.1f} MB)")
    print("next: python tools/funding_liq_gate_study.py --snapshot " + args.out)


if __name__ == "__main__":
    main()

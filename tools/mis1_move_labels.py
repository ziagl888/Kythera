"""
tools/mis1_move_labels.py — Move labels for the MIS1 replay samples (operator concept).

Purpose
-------
The replay label (TP1-before-SL of the smart-targets geometry) answers "does
the posted trade earn money?". But the original MIS concept asks "does a
pump/dump of ±X% happen within T?" — with horizon-dependent X:

    8h → ±5%      24h → ±10%      72h → ±15%      168h → ±25%

For every (symbol, signal_time) point of the existing replay this script
computes the maximum up/down move per horizon AFTER — purely from the
1h price series, without recomputing features or geometry. What is stored
are the CONTINUOUS extremes (close and wick basis), so the label thresholds
can be varied in the trainer without a re-run.

Window convention as in the replay (walkforward_sim.run_mis1): decision candle t
(signal_time = open_time[t] + 1h), entry = close[t], move window =
candles t+1 .. t+H. `full_Hh=false` means: data end before horizon end — a
positive threshold can still be scored as 1, but a 0 there is not a reliable
label (the trainer discards it).

Operating rules (live VPS!): BELOW_NORMAL, DB strictly read-only, output as JSONL
to Documents\\_X\\staging_models\\replay\\.

Example
-------
  python tools/mis1_move_labels.py --replay ...\\replay\\mis1_replay_400d.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from datetime import timedelta  # noqa: E402

from core.candles import read_candles  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.time import utc_now  # noqa: E402
from tools.walkforward_sim import set_low_priority  # noqa: E402

HORIZONS = (8, 24, 72, 168)


def collect_sample_times(replay_path: str) -> dict[str, list[str]]:
    """One pass over the replay JSONL: per symbol the unique
    signal_times (LONG/SHORT share the timestamp)."""
    per_symbol: dict[str, set] = {}
    with open(replay_path, encoding="utf-8") as fh:
        for line in fh:
            t = json.loads(line)
            per_symbol.setdefault(t["symbol"], set()).add(t["signal_time"])
    return {s: sorted(v) for s, v in per_symbol.items()}


def load_prices(conn, symbol: str, days: int) -> pd.DataFrame | None:
    try:
        # Via core.candles: CLOSED 1h candles, ASC. The cutoff there is
        # epoch arithmetic on the DB clock and replaces the earlier
        # date_trunc('hour', NOW()) — TZ-independent instead of session-dependent.
        df = read_candles(
            conn,
            symbol,
            "1h",
            start=utc_now() - timedelta(days=int(days)),
            include_forming=False,
            columns=("open_time", "high", "low", "close"),
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ("high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)


def forward_extremes(series: pd.Series, horizon: int, mode: str) -> np.ndarray:
    """Extremum over candles t+1 .. t+horizon (partial window at data end).

    rolling(h, min_periods=1) at index i covers [i-h+1 .. i]; shifted forward
    by h it covers exactly [t+1 .. t+h] at index t."""
    roll = series.rolling(horizon, min_periods=1)
    agg = roll.max() if mode == "max" else roll.min()
    return agg.shift(-horizon).values


def label_symbol(df: pd.DataFrame, sample_times: list[str]) -> list[dict]:
    n = len(df)
    close = df["close"]
    entry = close.values

    ext = {}
    for h in HORIZONS:
        ext[(h, "up_close")] = forward_extremes(close, h, "max")
        ext[(h, "dn_close")] = forward_extremes(close, h, "min")
        ext[(h, "up_wick")] = forward_extremes(df["high"], h, "max")
        ext[(h, "dn_wick")] = forward_extremes(df["low"], h, "min")

    # signal_time = open_time + 1h → index of the decision candle.
    # Replay signal_times are tz-naive (UTC wall-clock time), DB open_time is tz-aware —
    # normalise both sides to naive UTC, otherwise NOTHING matches.
    naive_ot = df["open_time"].dt.tz_localize(None)
    idx_by_time = {ts + pd.Timedelta(hours=1): i for i, ts in enumerate(naive_ot)}

    out = []
    for st in sample_times:
        t = idx_by_time.get(pd.to_datetime(st, utc=True).tz_localize(None))
        if t is None or entry[t] <= 0:
            continue
        e = entry[t]
        rec: dict = {"symbol": None, "signal_time": st}  # symbol is set by the caller
        for h in HORIZONS:
            up_c, dn_c = ext[(h, "up_close")][t], ext[(h, "dn_close")][t]
            up_w, dn_w = ext[(h, "up_wick")][t], ext[(h, "dn_wick")][t]
            if np.isnan(up_c):  # not a single candle after t
                rec[f"runup_close_pct_{h}h"] = None
                rec[f"drawdown_close_pct_{h}h"] = None
                rec[f"runup_wick_pct_{h}h"] = None
                rec[f"drawdown_wick_pct_{h}h"] = None
            else:
                rec[f"runup_close_pct_{h}h"] = round((up_c / e - 1) * 100, 4)
                rec[f"drawdown_close_pct_{h}h"] = round((dn_c / e - 1) * 100, 4)
                rec[f"runup_wick_pct_{h}h"] = round((up_w / e - 1) * 100, 4)
                rec[f"drawdown_wick_pct_{h}h"] = round((dn_w / e - 1) * 100, 4)
            rec[f"full_{h}h"] = bool(t + h <= n - 1)
        out.append(rec)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Move labels for MIS1 replay samples")
    ap.add_argument("--replay", required=True, help="mis1_replay_*.jsonl from walkforward_sim")
    ap.add_argument("--days", type=int, default=410,
                    help="DB load window; must cover the replay window")
    ap.add_argument("--out", default=None,
                    help="Default: <replay-directory>/mis1_move_labels.jsonl")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    set_low_priority()

    out_path = args.out or os.path.join(os.path.dirname(args.replay), "mis1_move_labels.jsonl")

    print("Collecting sample timestamps from the replay ...")
    per_symbol = collect_sample_times(args.replay)
    n_samples = sum(len(v) for v in per_symbol.values())
    print(f"{len(per_symbol)} symbols, {n_samples} unique (symbol, signal_time) points")

    conn = get_db_connection()
    t0 = time.time()
    n_written = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        for i, (symbol, times) in enumerate(sorted(per_symbol.items()), 1):
            df = load_prices(conn, symbol, args.days)
            if df is None:
                print(f"  !! {symbol}: no price data — skipped")
                continue
            recs = label_symbol(df, times)
            if not recs and times:
                print(f"  !! {symbol}: 0/{len(times)} timestamps matched (data gap?)")
            for rec in recs:
                rec["symbol"] = symbol
                fh.write(json.dumps(rec) + "\n")
                n_written += 1
            fh.flush()
            if i % 50 == 0:
                print(f"[{i}/{len(per_symbol)}] {symbol}: total {n_written} labels "
                      f"({time.time() - t0:.0f}s)", flush=True)
    conn.close()

    print(f"\nDone: {n_written}/{n_samples} labels → {out_path}")
    if n_written < n_samples * 0.5:
        print(f"ERROR: only {n_written}/{n_samples} labelled — result unusable")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""
tools/fif1_build_dataset.py — Trainings-Events + Replay-Labels für FIF1
"FIFO-Filter" (Report 15, S11). Läuft auf dem VPS (Step 2).

Events = ALLE Fast-In-And-Out-Signale aus active_trades_master +
closed_trades_master (der 111k-Trades-Datensatz, E6). Zeiten sind PG-Lokalzeit
(Europe/Bucharest) → UTC-Konvertierung wie in aim2_build_dataset.

Label: simulate_exit über die AUFGEZEICHNETE FIFO-Geometrie (entry/target1/sl,
n_published=1) ab der Kerze nach dem floor-1-Join — die Selektion ist die
einzige Frage, deshalb wird exakt die Original-Geometrie replayed (nicht der
status aus closed_trades_master, der ist unzuverlässig — Report 14).

Features: core.research_features.build_fif1_row (geteilter Builder) —
Markt-Kontext floor-1, Regime, FIFO-Burst-Dichte, Tageszeit.

Beispiel:
  python tools/fif1_build_dataset.py
  python tools/fif1_build_dataset.py --sample-pct 25 --limit-symbols 20   # Smoke
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time

import numpy as np
import pandas as pd

from tools.research_dataset_common import (
    MIN_WINDOW,
    REPLAY_DIR,
    df_query,
    floor_idx,
    join_is_stale,
    load_candles_ctx,
    load_regime,
    log,
    regime_at,
    set_low_priority,
    to_utc_naive,
)

from core.database import get_db_connection  # noqa: E402
from core.research_features import build_fif1_row  # noqa: E402
from tools.walkforward_sim import simulate_exit  # noqa: E402

SINCE_DEFAULT = "2026-02-25"
SOURCE_STRATEGY = "Fast In And Out"
HORIZON_CANDLES = 7 * 24


def load_events(conn, since: str) -> pd.DataFrame:
    ev = df_query(
        conn,
        """
        SELECT id, time, coin, direction, entry, target1, sl
        FROM active_trades_master WHERE strategy = %(s)s AND time > %(since)s
        UNION ALL
        SELECT id, time, coin, direction, entry, target1, sl
        FROM closed_trades_master WHERE strategy = %(s)s AND time > %(since)s
        """,
        {"s": SOURCE_STRATEGY, "since": since},
    )
    ev["ts"] = to_utc_naive(ev["time"])
    ev["symbol"] = ev["coin"].astype(str).str.upper()
    ev = ev[ev["symbol"].str.endswith("USDT")].dropna(subset=["ts"])
    ev["direction"] = ev["direction"].astype(str).str.upper()
    ev = ev[ev["direction"].isin(["LONG", "SHORT"])]
    for c in ("entry", "target1", "sl"):
        ev[c] = pd.to_numeric(ev[c], errors="coerce")
    ev = ev.dropna(subset=["entry", "target1", "sl"])
    ev = ev[(ev["entry"] > 0) & (ev["target1"] > 0) & (ev["sl"] > 0)]
    return ev.sort_values("ts").reset_index(drop=True)


def keep_sampled(event_id, pct: int) -> bool:
    if pct >= 100:
        return True
    h = int(hashlib.md5(f"fif1|{event_id}".encode()).hexdigest(), 16)
    return (h % 100) < pct


def build_burst_index(ev: pd.DataFrame):
    """Burst-Dichte auf dem VOLLEN Event-Strom (Sampling verändert die Welt
    nicht, nur die Trainingsauswahl — aim2-Regel)."""
    fleet_ts = ev["ts"].values.astype("datetime64[ns]")
    per_key: dict[tuple[str, str], np.ndarray] = {
        (sym, d): g["ts"].values.astype("datetime64[ns]")
        for (sym, d), g in ev.groupby(["symbol", "direction"], sort=False)
    }

    def counts(symbol: str, direction: str, ts64) -> tuple[int, int]:
        arr = per_key.get((symbol, direction))
        same = 0
        if arr is not None:
            hi = int(np.searchsorted(arr, ts64, side="left"))  # strikt < ts → Event selbst raus
            lo = int(np.searchsorted(arr, ts64 - np.timedelta64(24, "h"), side="left"))
            same = hi - lo
        hi_f = int(np.searchsorted(fleet_ts, ts64, side="left"))
        lo_f = int(np.searchsorted(fleet_ts, ts64 - np.timedelta64(1, "h"), side="left"))
        return same, hi_f - lo_f

    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE_DEFAULT)
    ap.add_argument("--out", default=os.path.join(REPLAY_DIR, "fif1_events.jsonl"))
    ap.add_argument("--sample-pct", type=int, default=100,
                    help="deterministisches Event-Sampling (md5) — Burst-Index bleibt voll")
    ap.add_argument("--limit-symbols", type=int, default=0)
    ap.add_argument("--skip-entry-hour", action="store_true",
                    help="Konservative Labels: Signalstunden-Kerze nicht replayen (Lookahead-Probe)")
    args = ap.parse_args()

    set_low_priority()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    t0 = time.time()

    conn = get_db_connection()
    ev = load_events(conn, args.since)
    log(f"FIFO-Events gesamt: {len(ev)} über {ev['symbol'].nunique()} Symbole")
    burst_counts = build_burst_index(ev)
    r_ts, r_rows = load_regime(conn)
    log(f"Regime-Zeilen: {len(r_rows)}")

    ev["sampled"] = [keep_sampled(i, args.sample_pct) for i in ev["id"]]
    weight = 100.0 / max(args.sample_pct, 1)
    train_ev = ev[ev["sampled"]]
    log(f"Gesampelt fürs Training: {len(train_ev)} ({args.sample_pct}%)")

    symbols = list(train_ev["symbol"].drop_duplicates())
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]

    start_off = 2 if args.skip_entry_hour else 1
    stats = {k: 0 for k in ("written", "wins", "open_end", "no_candles", "no_window",
                            "stale_join", "feature_fail")}
    with open(args.out, "w", encoding="utf-8") as fh:
        for i, sym in enumerate(symbols, 1):
            df = load_candles_ctx(conn, sym, args.since)
            sym_ev = train_ev[train_ev["symbol"] == sym]
            if df is None or len(df) < MIN_WINDOW:
                stats["no_candles"] += len(sym_ev)
                continue
            times = df["open_time"].values.astype("datetime64[ns]")
            highs = df["high"].to_numpy(dtype=np.float64)
            lows = df["low"].to_numpy(dtype=np.float64)
            closes = df["close"].to_numpy(dtype=np.float64)

            for row in sym_ev.itertuples():
                ts64 = np.datetime64(row.ts)
                idx = floor_idx(times, row.ts)
                if idx < MIN_WINDOW:
                    stats["no_window"] += 1
                    continue
                if join_is_stale(times, idx, row.ts):
                    stats["stale_join"] += 1
                    continue
                try:
                    regime_row, regime_age = regime_at(r_ts, r_rows, ts64)
                    same_dir, fleet = burst_counts(sym, row.direction, ts64)
                    feats = build_fif1_row(
                        row.direction, df, idx, regime_row, regime_age,
                        fifo_same_dir_24h=same_dir, fifo_fleet_1h=fleet, ts=row.ts,
                    )
                except Exception:
                    stats["feature_fail"] += 1
                    continue

                end = min(idx + start_off + HORIZON_CANDLES, len(times))
                res = simulate_exit(
                    times[:end], highs[:end], lows[:end], closes[:end],
                    start_idx=idx + start_off, direction=row.direction,
                    entry=float(row.entry), sl=float(row.sl),
                    targets=[float(row.target1)], n_published=1,
                )
                label = res.get("outcome_tp1")
                if res.get("exit_reason") == "open_at_end":
                    label = None
                fh.write(json.dumps({
                    "event_id": int(row.id), "symbol": sym,
                    "ts": pd.Timestamp(row.ts).isoformat(),
                    "direction": row.direction, "weight": weight,
                    "entry": float(row.entry), "sl": float(row.sl),
                    "targets": [float(row.target1)],
                    "label": label, "net_pnl_pct": res.get("net_pnl_pct"),
                    "exit_reason": res.get("exit_reason"), "risk_pct": res.get("risk_pct"),
                    "features": feats,
                }) + "\n")
                stats["written"] += 1
                stats["wins"] += 1 if label == 1 else 0
                stats["open_end"] += 1 if label is None else 0

            if i % 25 == 0 or i == len(symbols):
                closed = stats["written"] - stats["open_end"]
                wr = stats["wins"] / closed * 100 if closed else 0.0
                log(f"{i}/{len(symbols)} Symbole | geschrieben {stats['written']} "
                    f"(WR geschlossen: {wr:.1f}%) | {time.time() - t0:.0f}s")
    conn.close()
    log(f"FERTIG -> {args.out}")
    log(json.dumps(stats))


if __name__ == "__main__":
    main()

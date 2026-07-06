"""
tools/mis1_move_labels.py — Move-Labels für die MIS1-Replay-Samples (Operator-Konzept).

Zweck
-----
Das Replay-Label (TP1-vor-SL der Smart-Targets-Geometrie) beantwortet "verdient
der gepostete Trade Geld?". Das ursprüngliche MIS-Konzept fragt aber "kommt ein
Pump/Dump von ±X% innerhalb T?" — mit horizontabhängigem X:

    8h → ±5%      24h → ±10%      72h → ±15%      168h → ±25%

Dieses Skript berechnet für jeden (symbol, signal_time)-Punkt des vorhandenen
Replays die maximale Auf-/Abwärtsbewegung je Horizont NACH — nur aus den
1h-Preisreihen, ohne Feature- oder Geometrie-Neuberechnung. Gespeichert werden
die KONTINUIERLICHEN Extreme (Close- und Wick-Basis), damit die Label-Schwellen
im Trainer ohne Neu-Lauf variiert werden können.

Fensterkonvention wie im Replay (walkforward_sim.run_mis1): Entscheidungskerze t
(signal_time = open_time[t] + 1h), Entry = close[t], Bewegungsfenster =
Kerzen t+1 .. t+H. `full_Hh=false` heißt: Datenende vor Horizontende — eine
positive Schwelle kann trotzdem als 1 gewertet werden, eine 0 ist dort aber
kein verlässliches Label (Trainer verwirft sie).

Betriebsregeln (Live-VPS!): BELOW_NORMAL, DB strikt read-only, Output als JSONL
nach Documents\\_X\\staging_models\\replay\\.

Beispiel
--------
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

from core.database import get_db_connection  # noqa: E402
from tools.walkforward_sim import set_low_priority  # noqa: E402

HORIZONS = (8, 24, 72, 168)


def collect_sample_times(replay_path: str) -> dict[str, list[str]]:
    """Ein Durchlauf über das Replay-JSONL: je Symbol die eindeutigen
    signal_times (LONG/SHORT teilen sich den Zeitpunkt)."""
    per_symbol: dict[str, set] = {}
    with open(replay_path, encoding="utf-8") as fh:
        for line in fh:
            t = json.loads(line)
            per_symbol.setdefault(t["symbol"], set()).add(t["signal_time"])
    return {s: sorted(v) for s, v in per_symbol.items()}


def load_prices(conn, symbol: str, days: int) -> pd.DataFrame | None:
    try:
        df = pd.read_sql_query(
            f"""SELECT open_time, high, low, close
                FROM "{symbol}_1h"
                WHERE open_time >= NOW() - INTERVAL '{int(days)} days'
                  AND open_time < date_trunc('hour', NOW())
                ORDER BY open_time ASC""",
            conn,
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
    """Extremum über die Kerzen t+1 .. t+horizon (Teilfenster am Datenende).

    rolling(h, min_periods=1) bei Index i deckt [i-h+1 .. i]; um h nach vorn
    geschoben deckt es bei Index t exakt [t+1 .. t+h]."""
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

    # signal_time = open_time + 1h → Index der Entscheidungskerze.
    # Replay-signal_times sind tz-naiv (UTC-Wandzeit), DB-open_time tz-aware —
    # beide Seiten auf naive UTC normalisieren, sonst matcht NICHTS.
    naive_ot = df["open_time"].dt.tz_localize(None)
    idx_by_time = {ts + pd.Timedelta(hours=1): i for i, ts in enumerate(naive_ot)}

    out = []
    for st in sample_times:
        t = idx_by_time.get(pd.to_datetime(st, utc=True).tz_localize(None))
        if t is None or entry[t] <= 0:
            continue
        e = entry[t]
        rec: dict = {"symbol": None, "signal_time": st}  # symbol setzt der Aufrufer
        for h in HORIZONS:
            up_c, dn_c = ext[(h, "up_close")][t], ext[(h, "dn_close")][t]
            up_w, dn_w = ext[(h, "up_wick")][t], ext[(h, "dn_wick")][t]
            if np.isnan(up_c):  # keine einzige Kerze nach t
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
    ap = argparse.ArgumentParser(description="Move-Labels für MIS1-Replay-Samples")
    ap.add_argument("--replay", required=True, help="mis1_replay_*.jsonl aus walkforward_sim")
    ap.add_argument("--days", type=int, default=410,
                    help="DB-Ladefenster; muss das Replay-Fenster abdecken")
    ap.add_argument("--out", default=None,
                    help="Default: <replay-Verzeichnis>/mis1_move_labels.jsonl")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    set_low_priority()

    out_path = args.out or os.path.join(os.path.dirname(args.replay), "mis1_move_labels.jsonl")

    print("Sammle Sample-Zeitpunkte aus dem Replay ...")
    per_symbol = collect_sample_times(args.replay)
    n_samples = sum(len(v) for v in per_symbol.values())
    print(f"{len(per_symbol)} Symbole, {n_samples} eindeutige (symbol, signal_time)-Punkte")

    conn = get_db_connection()
    t0 = time.time()
    n_written = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        for i, (symbol, times) in enumerate(sorted(per_symbol.items()), 1):
            df = load_prices(conn, symbol, args.days)
            if df is None:
                print(f"  !! {symbol}: keine Preisdaten — übersprungen")
                continue
            recs = label_symbol(df, times)
            if not recs and times:
                print(f"  !! {symbol}: 0/{len(times)} Zeitstempel gematcht (Datenlücke?)")
            for rec in recs:
                rec["symbol"] = symbol
                fh.write(json.dumps(rec) + "\n")
                n_written += 1
            fh.flush()
            if i % 50 == 0:
                print(f"[{i}/{len(per_symbol)}] {symbol}: total {n_written} Labels "
                      f"({time.time() - t0:.0f}s)", flush=True)
    conn.close()

    print(f"\nFertig: {n_written}/{n_samples} Labels → {out_path}")
    if n_written < n_samples * 0.5:
        print(f"FEHLER: nur {n_written}/{n_samples} gelabelt — Ergebnis unbrauchbar")
        sys.exit(1)


if __name__ == "__main__":
    main()

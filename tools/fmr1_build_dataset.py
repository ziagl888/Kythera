"""
tools/fmr1_build_dataset.py — Trainings-Events + Replay-Labels für FMR1
"Funding-Extreme Mean-Reversion" (Report 15, S8). Läuft auf dem VPS (Step 2).

Voraussetzung: funding_rates ist befüllt (python tools/backfill_funding_rates.py).

Pipeline je Settlement (8h-Raster, funding_time = timestamptz/UTC):
  1. Cross-Section über alle Coins des Settlements → Perzentil-Rang.
  2. Events: Perzentil >= 0.95 → SHORT-Kandidat, <= 0.05 → LONG-Kandidat
     (Gates aus core.research_features, live gespiegelt in Bot 31).
  3. Dedup je Symbol+Seite: 24h-Mindestabstand (Spiegel des Live-Cooldowns).
  4. Statistik-Features aus der eigenen Settlement-Historie (funding_stats —
     geteilter Builder), Markt-Kontext floor-1 (kein Lookahead).
  5. Geometrie: calculate_smart_targets; Label: simulate_exit, Horizont 7 Tage.

Bekannter, bewusster Rest-Skew: live gated Bot 31 die LAUFENDE Rate
(premiumIndex), hier die GESETTELTE — gleiche Quelle, ein Settlement Versatz.

Beispiel:
  python tools/fmr1_build_dataset.py
  python tools/fmr1_build_dataset.py --limit-symbols 15
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

# REPO_ROOT MUSS vor dem ersten tools-/core-Import auf sys.path liegen
# (Henne-Ei; Spec-Review-Fix 2026-07-06, Muster tools/aim2_build_dataset.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.research_dataset_common import (  # noqa: E402
    MIN_WINDOW,
    REPLAY_DIR,
    WINDOW_CANDLES,
    df_query,
    floor_idx,
    join_is_stale,
    load_candles_ctx,
    log,
    set_low_priority,
)

from core.database import get_db_connection  # noqa: E402
from core.research_features import (  # noqa: E402
    FMR1_LONG_PCTL,
    FMR1_SHORT_PCTL,
    build_fmr1_row,
    funding_stats,
)
from core.trade_utils import calculate_smart_targets  # noqa: E402
from tools.walkforward_sim import simulate_exit  # noqa: E402

SINCE_DEFAULT = "2026-02-25"
HORIZON_CANDLES = 7 * 24
DEDUP_HOURS = 24              # Spiegel des Live-Cooldowns (Bot 31)
N_PUBLISHED = 3
MIN_CROSS_SECTION = 50        # Settlements mit weniger Coins verwerfen
MIN_HISTORY = 10              # funding_stats-Mindesthistorie


def load_funding(conn, since: str) -> pd.DataFrame:
    """Volle Settlement-Historie (inkl. Vorlauf für die Statistik-Features)."""
    df = df_query(
        conn,
        """
        SELECT symbol, funding_time, funding_rate
        FROM funding_rates
        WHERE funding_time >= %s::timestamptz - INTERVAL '45 days'
        ORDER BY funding_time ASC
        """,
        (since,),
    )
    if df.empty:
        raise SystemExit("funding_rates ist leer — erst tools/backfill_funding_rates.py laufen lassen.")
    df["funding_time"] = pd.to_datetime(df["funding_time"], utc=True).dt.tz_localize(None)
    df["symbol"] = df["symbol"].astype(str).str.upper()
    df = df[df["symbol"].str.endswith("USDT")]
    df["funding_rate"] = pd.to_numeric(df["funding_rate"], errors="coerce")
    return df.dropna(subset=["funding_rate"]).reset_index(drop=True)


def build_events(fund: pd.DataFrame, since: str) -> pd.DataFrame:
    """Cross-Section je Settlement → Extrem-Events mit Perzentil."""
    since_ts = pd.Timestamp(since)
    events = []
    for ft, g in fund.groupby("funding_time", sort=True):
        if ft < since_ts or len(g) < MIN_CROSS_SECTION:
            continue
        pctl = g["funding_rate"].rank(pct=True)
        for side, mask in (("SHORT", pctl >= FMR1_SHORT_PCTL), ("LONG", pctl <= FMR1_LONG_PCTL)):
            for i in g.index[mask]:
                events.append({
                    "symbol": g.at[i, "symbol"], "ts": ft, "direction": side,
                    "rate": float(g.at[i, "funding_rate"]), "pctl": float(pctl.at[i]),
                })
    ev = pd.DataFrame(events).sort_values("ts").reset_index(drop=True)

    keep, last_ts = [], {}
    for row in ev.itertuples():
        key = (row.symbol, row.direction)
        prev = last_ts.get(key)
        ok = prev is None or (row.ts - prev).total_seconds() >= DEDUP_HOURS * 3600
        keep.append(ok)
        if ok:
            last_ts[key] = row.ts
    return ev[pd.Series(keep, index=ev.index)].reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE_DEFAULT)
    ap.add_argument("--out", default=os.path.join(REPLAY_DIR, "fmr1_events.jsonl"))
    ap.add_argument("--limit-symbols", type=int, default=0)
    args = ap.parse_args()

    set_low_priority()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    t0 = time.time()

    conn = get_db_connection()
    fund = load_funding(conn, args.since)
    log(f"Funding-Zeilen: {len(fund)} über {fund['symbol'].nunique()} Symbole")
    ev = build_events(fund, args.since)
    log(f"Extrem-Events nach Dedup: {len(ev)} "
        f"(SHORT {int((ev['direction'] == 'SHORT').sum())} / LONG {int((ev['direction'] == 'LONG').sum())})")

    # Settlement-Historie je Symbol als Arrays (für funding_stats bis Event-Zeit).
    hist: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for sym, g in fund.groupby("symbol", sort=False):
        hist[sym] = (
            g["funding_time"].values.astype("datetime64[ns]"),
            g["funding_rate"].to_numpy(dtype=np.float64),
        )

    symbols = list(ev["symbol"].drop_duplicates())
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]

    stats = {k: 0 for k in ("written", "wins", "open_end", "no_candles", "no_window",
                            "stale_join", "geometry_fail", "short_history")}
    with open(args.out, "w", encoding="utf-8") as fh:
        for i, sym in enumerate(symbols, 1):
            df = load_candles_ctx(conn, sym, args.since)
            sym_ev = ev[ev["symbol"] == sym]
            if df is None or len(df) < MIN_WINDOW:
                stats["no_candles"] += len(sym_ev)
                continue
            times = df["open_time"].values.astype("datetime64[ns]")
            highs = df["high"].to_numpy(dtype=np.float64)
            lows = df["low"].to_numpy(dtype=np.float64)
            closes = df["close"].to_numpy(dtype=np.float64)
            f_ts, f_rates = hist[sym]

            for row in sym_ev.itertuples():
                ts64 = np.datetime64(row.ts)
                idx = floor_idx(times, row.ts)
                if idx < MIN_WINDOW:
                    stats["no_window"] += 1
                    continue
                if join_is_stale(times, idx, row.ts):
                    stats["stale_join"] += 1
                    continue
                # Historie bis EINSCHLIESSLICH des Event-Settlements (kein Lookahead).
                hi = int(np.searchsorted(f_ts, ts64, side="right"))
                if hi < MIN_HISTORY:
                    stats["short_history"] += 1
                    continue
                try:
                    f_stats = funding_stats(list(f_rates[:hi]))
                    entry_close = float(closes[idx])
                    win = df.iloc[max(0, idx - WINDOW_CANDLES + 1): idx + 1]
                    setup = calculate_smart_targets(None, sym, row.direction, entry_close, df=win)
                    entry1 = float(setup["entry1"])
                    sl = float(setup["sl"])
                    targets = [float(t) for t in setup["targets"][:N_PUBLISHED]]
                    if not targets or sl <= 0 or entry1 <= 0:
                        raise ValueError("degenerate geometry")
                    end = min(idx + 1 + HORIZON_CANDLES, len(times))
                    res = simulate_exit(
                        times[:end], highs[:end], lows[:end], closes[:end],
                        start_idx=idx + 1, direction=row.direction, entry=entry1, sl=sl,
                        targets=targets, n_published=len(targets),
                    )
                    feats = build_fmr1_row(f_stats, row.pctl, row.direction, df, idx)
                except ValueError:
                    stats["geometry_fail"] += 1
                    continue
                except Exception:
                    stats["geometry_fail"] += 1
                    continue

                label = res.get("outcome_tp1")
                if res.get("exit_reason") == "open_at_end":
                    label = None
                fh.write(json.dumps({
                    "symbol": sym, "ts": pd.Timestamp(row.ts).isoformat(),
                    "direction": row.direction, "weight": 1.0,
                    "entry": entry1, "sl": sl, "targets": targets,
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

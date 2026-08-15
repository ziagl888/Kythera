# tools/pump_long_sl_study.py — SL grid for the pump-continuation LONG (T-2026-KYT-9050-145).
#
# T-144 found the LONG side of extreme pumpers the stronger lean (>=75%: +8.3
# net @24h, t=1.6) — but these coins are violently volatile, so the operator
# question is: which stop distance survives the drawdown, and does the edge
# survive the stop (and the funding bill)?
#
# Pre-registration (OWN commit before this tool, incl. the candidate rule):
#   staging_models/replay/pump_long_sl_study_t145_prereg.md
#
# READ-ONLY: SELECTs against oi_5m, candles (tf='5m') and funding_rates. Runs
# from the repo root or a worktree inside it. Verdict:
#   staging_models/replay/pump_long_sl_study_t145.md
#
# Event construction is imported from tools/pump_oi_study.py (T-144) so the
# two studies share one grid definition; execution switches to REAL candle
# paths: entry = open of the first 5m candle strictly after the signal hour,
# SL touched on candle low, gap-through fills at the touching candle's open.

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("pump_oi_study", _HERE / "pump_oi_study.py")
pos = importlib.util.module_from_spec(_spec)
sys.modules["pump_oi_study"] = pos
_spec.loader.exec_module(pos)

FEE_RT = pos.FEE_RT  # 0.10 %/RT — one convention across the study family

# Pre-registered matrix — no post-hoc search.
PUMP_PCTS = [25.0, 50.0, 75.0]
SL_PCTS = [10.0, 15.0, 20.0, 25.0, 30.0, 40.0]  # below entry; None = hold baseline
HORIZONS_H = [24, 48, 72]
ENTRY_MAX_DELAY_MIN = 15  # no 5m candle within this after the signal => void
MAE_QUANTILES = [0.25, 0.50, 0.75, 0.90]


def load_events(conn) -> pd.DataFrame:
    """T-144 grid, M0 pump-only, LONG side, deduped per pump threshold."""
    g = pos.add_features(pos.hourly_grid(pos.load(conn)))
    frames = []
    for pump_pct in PUMP_PCTS:
        ev = pos.dedupe(g[g["dpx_24h"] >= pump_pct].copy())
        ev = ev[["ts", "symbol"]].copy()
        ev["pump_pct"] = pump_pct
        frames.append(ev)
    return pd.concat(frames, ignore_index=True)


def load_candles(conn, symbol: str, t0: pd.Timestamp, t1: pd.Timestamp) -> pd.DataFrame:
    c = pd.read_sql(
        "SELECT open_time, open, high, low, close FROM candles "
        "WHERE symbol = %(s)s AND tf = '5m' AND open_time > %(t0)s AND open_time <= %(t1)s "
        "ORDER BY open_time",
        conn,
        params={"s": symbol, "t0": t0, "t1": t1},
    )
    c["open_time"] = pd.to_datetime(c["open_time"], utc=True)
    return c


def load_funding(conn, symbols: list[str], t0, t1) -> pd.DataFrame:
    fr = pd.read_sql(
        "SELECT symbol, funding_time, funding_rate FROM funding_rates "
        "WHERE funding_time >= %(t0)s AND funding_time <= %(t1)s AND symbol = ANY(%(syms)s) "
        "ORDER BY funding_time",
        conn,
        params={"t0": t0, "t1": t1, "syms": list(symbols)},
    )
    fr["funding_time"] = pd.to_datetime(fr["funding_time"], utc=True)
    return fr


def simulate_event(candles: pd.DataFrame, ts: pd.Timestamp) -> dict | None:
    """One event: entry + per-(SL,H) exits + MAE per horizon.

    Returns None (void) when no candle exists within ENTRY_MAX_DELAY_MIN after
    the signal — a missing feed must not be filled (P0.12).
    """
    after = candles[candles["open_time"] > ts]
    if after.empty:
        return None
    first = after.iloc[0]
    if (first["open_time"] - ts) > pd.Timedelta(minutes=ENTRY_MAX_DELAY_MIN):
        return None
    entry_t = first["open_time"]
    entry = float(first["open"])
    if not entry > 0:
        return None
    out: dict = {"entry_t": entry_t, "entry": entry}
    path = after  # entry candle included: its low can already hit the SL

    for h in HORIZONS_H:
        w = path[path["open_time"] <= ts + pd.Timedelta(hours=h)]
        if w.empty:
            continue
        out[f"mae_{h}h"] = (float(w["low"].min()) / entry - 1) * 100
        out[f"hold_{h}h"] = (float(w.iloc[-1]["close"]) / entry - 1) * 100
        out[f"hold_exit_t_{h}h"] = w.iloc[-1]["open_time"]
        for sl in SL_PCTS:
            sl_px = entry * (1 - sl / 100)
            hit = w[w["low"] <= sl_px]
            if hit.empty:
                out[f"ret_{h}h_sl{sl:.0f}"] = out[f"hold_{h}h"]
                out[f"exit_t_{h}h_sl{sl:.0f}"] = out[f"hold_exit_t_{h}h"]
            else:
                cndl = hit.iloc[0]
                # Gap-through: a candle OPENING below the stop fills at its
                # open (worse), never better than the stop price itself.
                fill = min(sl_px, float(cndl["open"]))
                out[f"ret_{h}h_sl{sl:.0f}"] = (fill / entry - 1) * 100
                out[f"exit_t_{h}h_sl{sl:.0f}"] = cndl["open_time"]
    return out


def funding_cost_pct(fr_sym: pd.DataFrame, t_in, t_out) -> float:
    """Realized funding a LONG pays between entry and exit (%-points; positive
    rates cost the long, negative rates pay it)."""
    if fr_sym is None or fr_sym.empty:
        return 0.0
    w = fr_sym[(fr_sym["funding_time"] > t_in) & (fr_sym["funding_time"] <= t_out)]
    return float(w["funding_rate"].sum()) * 100


def cell_stats(rets: pd.Series) -> dict:
    r = rets.dropna()
    if len(r) < 2:
        return {}
    t = r.mean() / (r.std() / np.sqrt(len(r))) if r.std() > 0 else np.nan
    return {
        "n": len(r),
        "net": round(r.mean() - FEE_RT, 3),
        "t": round(t, 2),
        "median": round(r.median(), 3),
        "wr": round((r > 0).mean(), 2),
        "worst": round(r.min(), 1),
    }


def main() -> None:
    sys.path.insert(0, ".")
    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        events = load_events(conn)
        print(f"events (all thresholds, pre-path): {len(events)}")
        t0, t1 = events["ts"].min(), events["ts"].max() + pd.Timedelta(hours=max(HORIZONS_H))
        fr = load_funding(conn, events["symbol"].unique().tolist(), t0, t1)
        fr_by_sym = {s: d for s, d in fr.groupby("symbol")}

        rows = []
        for (symbol, ts), grp in events.groupby(["symbol", "ts"]):
            candles = load_candles(conn, symbol, ts, ts + pd.Timedelta(hours=max(HORIZONS_H)))
            sim = simulate_event(candles, ts)
            if sim is None:
                continue
            for pump_pct in grp["pump_pct"]:
                row = {"symbol": symbol, "ts": ts, "pump_pct": pump_pct, **sim}
                rows.append(row)
    finally:
        conn.close()

    df = pd.DataFrame(rows)
    print(f"simulated event-rows: {len(df)} (voided: no timely entry candle for the rest)")

    for pump_pct in PUMP_PCTS:
        sub = df[df["pump_pct"] == pump_pct]
        print(f"\n######## LONG on pump >= {pump_pct:.0f}% — n={len(sub)} ########")
        for h in HORIZONS_H:
            mae = sub[f"mae_{h}h"].dropna()
            if len(mae) >= 5:
                qs = ", ".join(f"p{int(q * 100)}={mae.quantile(q):+.1f}%" for q in MAE_QUANTILES)
                print(f"  MAE @{h}h (n={len(mae)}): {qs}")
        for h in HORIZONS_H:
            hold = cell_stats(sub[f"hold_{h}h"])
            if not hold:
                continue
            print(f"  H={h}h  HOLD (no SL): n={hold['n']} net {hold['net']:+.3f}% (t={hold['t']}, "
                  f"med {hold['median']:+.2f}, WR {hold['wr']:.0%}, worst {hold['worst']})")
            for sl in SL_PCTS:
                col = f"ret_{h}h_sl{sl:.0f}"
                st = cell_stats(sub[col])
                if not st:
                    continue
                # SL-hit = the exit happened before the hold exit (works also
                # when the stop SAVED the trade vs a deeper hold loss).
                hitrate = (sub[f"exit_t_{h}h_sl{sl:.0f}"] != sub[f"hold_exit_t_{h}h"]).mean()
                # Funding-adjusted net: realized settlements to each event's exit.
                fadj = []
                for row in sub.itertuples():
                    ret = getattr(row, col.replace("-", "_"))
                    if pd.isna(ret):
                        continue
                    t_out = getattr(row, f"exit_t_{h}h_sl{int(sl)}")
                    cost = funding_cost_pct(fr_by_sym.get(row.symbol), row.entry_t, t_out)
                    fadj.append(ret - cost)
                fnet = (np.mean(fadj) - FEE_RT) if fadj else np.nan
                # Weekly stability (rule 5): mean per calendar week on this cell.
                wk = sub.set_index("ts")[col].groupby(pd.Grouper(freq="W")).mean().dropna()
                wkpos = (wk > 0).mean() if len(wk) else np.nan
                print(f"    SL {sl:4.0f}%: n={st['n']} net {st['net']:+.3f}% (t={st['t']}) "
                      f"fund-adj {fnet:+.3f}% | SL-hit {hitrate:.0%} | med {st['median']:+.2f} "
                      f"WR {st['wr']:.0%} worst {st['worst']} | wk+ {wkpos:.0%}")


if __name__ == "__main__":
    main()

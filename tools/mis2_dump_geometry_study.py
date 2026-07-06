"""
tools/mis2_dump_geometry_study.py — Dump-Seite-Überarbeitung, Schritt 1 (Studie).

Befund (Move-Retrains 2026-07-06): Die MIS2-Dump-Modelle ERKENNEN Dumps gut
(Move-Trefferquoten 64–73 % bei ~10 % Basisrate), aber die generische
Short-Smart-Targets-Geometrie verdient daran nichts. Diese Studie beantwortet
datengetrieben, WORAN das liegt und WELCHE Short-Geometrie stattdessen trägt:

  1. Diagnose: Exit-Gründe + PnL des Status quo (Replay-Geometrie) auf den
     Events, die das jeweilige Dump-Modell selektiert (Operating Point =
     Top-2%-Quantil der Validation-Probabilities — die Safe-Picker-Thresholds
     existieren für Dumps bewusst nicht).
  2. Geometrie-Grid: label-angepasste Brackets auf denselben Events, wick-aware
     First-Touch auf 1h-Kerzen, SL-first bei Ambiguität, Fees 0,10 % RT,
     hartes Timeout am Horizontende (Exit zum Close):
        TP ∈ {X/2, 2X/3, X} der Move-Schwelle X des Horizonts
        SL ∈ {2, 3, 5, 8} %
  3. Vergleich je (Horizont × Label-Basis close/wick × Variante):
     n, WR, Ø-/Median-/Summen-Netto-PnL — gegen den Status quo.

Kein Deploy, keine Bot-Änderung — reine Studie; Ergebnis-JSON + Konsole.
Betriebsregeln: BELOW_NORMAL, DB read-only, EIN Job (Sequenz-Regel).

Beispiel:
  python tools/mis2_dump_geometry_study.py --replay ...\\mis1_replay_400d.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import joblib
import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.database import get_db_connection  # noqa: E402
from tools.mis1_move_labels import load_prices  # noqa: E402
from tools.retrain_from_replay import (  # noqa: E402
    MIS1_FEATURES,
    MIS1_HORIZONS,
    MOVE_THRESH_PCT,
    STAGING_DIR,
    chrono_split,
    load_mis1_move_labels,
    load_mis1_replay,
)
from tools.walkforward_sim import set_low_priority  # noqa: E402

FEE_RT_PCT = 0.10  # 0,05 % je Seite
# V2 (Operator-Feedback 2026-07-06 abends): SL-Raster horizontabhängig — bei
# −15 %/−25 %-Zielen war 8 % max. unangemessen eng ("mehr Abstand beim SL").
SL_GRID_BY_HORIZON = {
    8: (3.0, 5.0, 8.0, 12.0),
    24: (5.0, 8.0, 12.0, 16.0),
    72: (5.0, 8.0, 12.0, 20.0),
    168: (8.0, 12.0, 16.0, 20.0),
}
TP_FRACTIONS = (0.5, 2.0 / 3.0, 1.0)
# V2: Entry-Varianten — die funktionierenden Shorts der Flotte (EPD1/RUB1/SR)
# verkaufen an Struktur bzw. in die Gegenbewegung; stumpfer Close-Entry war
# die Schwäche des V1-Grids. 0.0 = Market am Signal-Close; sonst Limit-Sell
# X % ÜBER dem Signal-Close (füllt nur, wenn der Bounce kommt).
ENTRY_BOUNCE_PCT = (0.0, 2.5, 5.0)
OPERATING_QUANTILE = 0.98  # Top-2 % der Val-Probabilities als Gate


def simulate_short_bracket(candles: pd.DataFrame, t: int, horizon: int,
                           signal_close: float, tp_pct: float, sl_pct: float,
                           bounce_pct: float = 0.0):
    """Wick-aware First-Touch für einen SHORT auf Kerzen t+1..t+horizon.

    bounce_pct > 0: Limit-Sell bei signal_close*(1+bounce) — Entry erst, wenn
    eine Kerze das Level per High erreicht (konservativ: Fill zum Limit).
    TP liegt IMMER relativ zum Signal-Close (die Move-Prognose zählt ab dem
    Signalzeitpunkt), SL relativ zum tatsächlichen Entry.
    SL-first bei Ambiguität. Timeout → Close-Exit. Rückgabe:
    (outcome, net_pnl_pct) — 1=TP, 0=SL, 2=Timeout, 3=nie gefüllt, None=Datenende."""
    n = len(candles)
    end = t + horizon
    highs, lows, closes = candles["high"].values, candles["low"].values, candles["close"].values
    tp_price = signal_close * (1 - tp_pct / 100.0)

    if bounce_pct <= 0:
        entry, entry_i = signal_close, t
    else:
        limit_price = signal_close * (1 + bounce_pct / 100.0)
        entry, entry_i = None, None
        for i in range(t + 1, min(end, n - 1) + 1):
            if highs[i] >= limit_price:
                entry, entry_i = limit_price, i
                break
        if entry is None:
            return (3, None) if end <= n - 1 else (None, None)

    sl_price = entry * (1 + sl_pct / 100.0)
    # Fill-Kerze selbst: SL-Check ab Fill (konservativ — High könnte nach dem
    # Fill weiterlaufen), TP-Check erst ab der Folgekerze.
    if entry_i > t and highs[entry_i] >= sl_price:
        return 0, -sl_pct - FEE_RT_PCT
    for i in range(entry_i + 1, min(end, n - 1) + 1):
        if highs[i] >= sl_price:  # SL-first bei Ambiguität
            return 0, -sl_pct - FEE_RT_PCT
        if lows[i] <= tp_price:
            return 1, (entry - tp_price) / entry * 100.0 - FEE_RT_PCT
    if end <= n - 1:
        return 2, (entry - closes[end]) / entry * 100.0 - FEE_RT_PCT
    return None, None


def main() -> None:
    ap = argparse.ArgumentParser(description="MIS2-Dump-Geometrie-Studie")
    ap.add_argument("--replay", required=True)
    ap.add_argument("--move-labels", default=None)
    ap.add_argument("--days", type=int, default=410)
    ap.add_argument("--out", default=os.path.join(STAGING_DIR, "mis2_dump_geometry_study.json"))
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    set_low_priority()

    print("Lade Replay (SHORT-Samples) ...")
    df_all = load_mis1_replay(args.replay)
    move_path = args.move_labels or os.path.join(os.path.dirname(args.replay), "mis1_move_labels.jsonl")
    mv = load_mis1_move_labels(move_path)
    df_all = df_all.merge(mv, on=["symbol", "signal_time"], how="left")
    df_all = df_all[df_all["direction"] == "SHORT"].reset_index(drop=True)
    print(f"{len(df_all)} SHORT-Samples, {df_all['symbol'].nunique()} Coins")

    results: dict = {"operating_quantile": OPERATING_QUANTILE, "fee_rt_pct": FEE_RT_PCT,
                     "models": {}}
    conn = get_db_connection()
    price_cache: dict[str, pd.DataFrame | None] = {}
    idx_cache: dict[str, dict] = {}

    t0 = time.time()
    for basis, prefix in (("close", "mis1_move_model"), ("wick", "mis1_move_wick_model")):
        for horizon in MIS1_HORIZONS:
            key = f"{horizon}h_dump"
            art_path = os.path.join(STAGING_DIR, f"{prefix}_{key}.pkl")
            if not os.path.exists(art_path):
                print(f"!! Artefakt fehlt: {art_path} — übersprungen")
                continue
            art = joblib.load(art_path)
            model = art["model"]

            thr_move = MOVE_THRESH_PCT[horizon]
            ext_col = f"drawdown_{basis}_pct_{horizon}h"
            full_col = f"full_{horizon}h"
            d = df_all.copy()
            ext = pd.to_numeric(d[ext_col], errors="coerce")
            hit = ext <= -thr_move
            full = d[full_col].fillna(False).astype(bool)
            d["outcome"] = np.where(hit, 1.0, np.where(full, 0.0, np.nan))
            d.loc[ext.isna(), "outcome"] = np.nan
            d["net_pnl_pct"] = pd.to_numeric(d[f"net_pnl_{horizon}h"], errors="coerce")
            d = d[d["outcome"].notna()].reset_index(drop=True)

            train, val, test = chrono_split(d, horizon + 24)
            p_val = model.predict_proba(val[MIS1_FEATURES].fillna(0))[:, 1]
            p_test = model.predict_proba(test[MIS1_FEATURES].fillna(0))[:, 1]
            gate = float(np.quantile(p_val, OPERATING_QUANTILE))
            sel = test[p_test >= gate].copy()

            rec = {
                "basis": basis, "horizon": horizon, "gate_prob": round(gate, 4),
                "n_test_total": int(len(test)), "n_selected": int(len(sel)),
                "move_wr_selected": round(float(sel["outcome"].mean()) * 100, 1) if len(sel) else None,
                "move_wr_base": round(float(test["outcome"].mean()) * 100, 1),
                "status_quo": {}, "exit_reasons": {}, "variants": {},
            }

            # 1) Status quo (Replay-Smart-Targets) auf den selektierten Events
            sq = sel["net_pnl_pct"].dropna()
            if len(sq):
                rec["status_quo"] = {
                    "n": int(len(sq)), "avg": round(float(sq.mean()), 3),
                    "median": round(float(sq.median()), 3), "sum": round(float(sq.sum()), 1),
                }
            er_col = f"exit_reason_{horizon}h"
            if er_col in sel.columns:
                rec["exit_reasons"] = sel[er_col].fillna("none").value_counts().to_dict()

            # 2) Geometrie-Grid auf 1h-Kerzen (V2: SL je Horizont + Entry-Bounces)
            sl_grid = SL_GRID_BY_HORIZON[horizon]
            grids = {(tp_f, sl, b): [] for tp_f in TP_FRACTIONS for sl in sl_grid
                     for b in ENTRY_BOUNCE_PCT}
            for _, row in sel.iterrows():
                sym = row["symbol"]
                if sym not in price_cache:
                    price_cache[sym] = load_prices(conn, sym, args.days)
                    dfp = price_cache[sym]
                    idx_cache[sym] = (
                        {ts + pd.Timedelta(hours=1): i
                         for i, ts in enumerate(dfp["open_time"].dt.tz_localize(None))}
                        if dfp is not None else {}
                    )
                dfp = price_cache[sym]
                if dfp is None:
                    continue
                t = idx_cache[sym].get(pd.Timestamp(row["signal_time"]))
                if t is None:
                    continue
                entry = float(dfp["close"].iloc[t])
                if entry <= 0:
                    continue
                for (tp_f, sl, b), acc in grids.items():
                    out, pnl = simulate_short_bracket(dfp, t, horizon, entry,
                                                      thr_move * tp_f, sl, bounce_pct=b)
                    if out is not None:
                        acc.append((out, pnl))

            for (tp_f, sl, b), acc in grids.items():
                filled = [a for a in acc if a[0] != 3]
                if not filled:
                    continue
                outs = np.array([a[0] for a in filled], dtype=float)
                pnls = np.array([a[1] for a in filled], dtype=float)
                rec["variants"][f"tp{thr_move * tp_f:.1f}_sl{sl:.0f}_e{b:.1f}"] = {
                    "n_signals": int(len(acc)),
                    "n_filled": int(len(filled)),
                    "fill_rate": round(float(len(filled) / len(acc)) * 100, 1),
                    "tp_rate": round(float((outs == 1).mean()) * 100, 1),
                    "sl_rate": round(float((outs == 0).mean()) * 100, 1),
                    "timeout_rate": round(float((outs == 2).mean()) * 100, 1),
                    "avg": round(float(pnls.mean()), 3),
                    "median": round(float(np.median(pnls)), 3),
                    "sum": round(float(pnls.sum()), 1),
                }

            results["models"][f"{basis}_{key}"] = rec
            best = max(rec["variants"].items(), key=lambda kv: kv[1]["avg"]) if rec["variants"] else None
            print(f"[{time.time()-t0:.0f}s] {basis}_{key}: gate={gate:.3f}, n_sel={len(sel)}, "
                  f"Move-WR {rec['move_wr_selected']}% (Basis {rec['move_wr_base']}%), "
                  f"StatusQuo Ø {rec['status_quo'].get('avg')}% | "
                  f"beste Variante: {best[0] if best else '—'} "
                  f"Ø {best[1]['avg'] if best else '—'}%", flush=True)

    conn.close()
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)
    print(f"\nFertig → {args.out}")


if __name__ == "__main__":
    main()

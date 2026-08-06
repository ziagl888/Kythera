# tools/fif2_single_tp_backtest.py — is the vol-gated single-TP bot worth building?
"""FIF2 candidate decision backtest (T-2026-KYT-9050-111).

Michi's proposal after T-110: replace FIF1 with a new bot that mirrors fleet
signals passing a volatility gate and posts them with a SINGLE take-profit —
100 % out at TP1, no ladder, no runner. Per hard rule 6 the rework would post
under a NEW tag (FIF2); this study is the go/no-go gate for building it.

Pre-registered design (written before the first run)
----------------------------------------------------
* **Exit:** 100 % at TP1, stop at SL, t104 geometry primary (LONG 4/5, SHORT
  3/2), ``symmetric_tight`` as robustness. Same-candle tie books as SL. Neither
  touched inside 72 h -> horizon mark. Fees 0.09 on notional, T-105 value.
* **Gate:** ``sym_vol_4h`` (T-110's one strong predictor) above a threshold
  fixed on the TRAIN quantiles — q80 primary (T-110's deciles 9-10 are where
  TP1-first overtakes SL-first), q90 secondary. NaN vol never passes the gate:
  a bot cannot act on a feature it does not have.
* **Comparison:** {ungated, q80, q90} x {single-TP, T-105 ladder} so the gate
  effect and the exit effect are separable. The ladder cells reuse
  ``tools.portfolio_backtest.precompute`` verbatim — one implementation, no
  drift between the study and the simulator.
* **Validation:** chronological 70/30 split (thresholds from train, verdict
  from test), per-ISO-week mean-pp sign consistency on test+train.
* **Economics:** mean pp/trade net of fees, win rate, median hold, PnL per
  slot-hour (the T-105 scarce resource), posts/day and implied average
  concurrency against the Cornix 500-slot cap.

The go/no-go bar, stated before the numbers: the gated single-TP cell must be
positive on the TEST split AND win the slot-hour comparison against its ladder
twin — otherwise FIF1 is not replaced by this idea.

DB-free: consumes the T-104/T-105 ``.npz`` exports. Nothing here touches the
live database or the fleet.

Usage
-----
    python tools/fif2_single_tp_backtest.py --in reports/leg_composition_raw.npz \\
        --out reports/fif2_single_tp_backtest.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.leg_composition_replay as lcr  # noqa: E402
import tools.portfolio_backtest as pb  # noqa: E402
from tools.leg_composition_replay import replay_signal  # noqa: E402
from tools.tp1_speed_study import series_features  # noqa: E402

UTC = timezone.utc

GEOMETRIES = {
    "t104": {"LONG": (4.0, 5.0), "SHORT": (3.0, 2.0)},
    "symmetric_tight": {"LONG": (3.0, 2.0), "SHORT": (3.0, 2.0)},
}
FEE_PCT = pb.FEE_PCT
CANDLE_S = 300
TRAIN_SHARE = 0.7
GATE_QUANTILES = (0.8, 0.9)  # fixed on train — q80 primary, q90 secondary
SLOT_CAP = pb.SLOT_CAP


def single_tp_records(z, geometry: dict[str, tuple[float, float]], vol: np.ndarray) -> list[dict]:
    """One record per covered signal: single-TP outcome, exit instant, gate feature.

    Mirrors the T-105 ``precompute`` walk (same window bounds, same tie -> SL
    convention, same exit_ts grid) but with the one-shot exit: the whole
    position leaves at TP1, SL, or the horizon mark.
    """
    meta = json.loads(str(z["meta"][0]))
    horizon_s = int(meta["horizon_h"] * 3600)
    symbols, models = list(z["symbols"]), list(z["models"])
    c_sym, c_ts = z["c_sym"], z["c_ts"]
    c_high, c_low, c_close = z["c_high"], z["c_low"], z["c_close"]
    s_sym, s_mod, s_long, s_ts, s_entry = z["s_sym"], z["s_mod"], z["s_long"], z["s_ts"], z["s_entry"]

    starts = np.searchsorted(c_sym, np.arange(len(symbols)), side="left")
    ends = np.searchsorted(c_sym, np.arange(len(symbols)), side="right")

    recs = []
    for k in range(len(s_ts)):
        a, b = starts[s_sym[k]], ends[s_sym[k]]
        if a == b:
            continue
        ts = c_ts[a:b]
        lo = a + int(np.searchsorted(ts, s_ts[k], side="right"))
        hi = a + int(np.searchsorted(ts, s_ts[k] + horizon_s, side="right"))
        if hi <= lo:
            continue
        direction = "LONG" if s_long[k] else "SHORT"
        tp, sl = geometry[direction]
        i_tp, i_sl, mark, _, _ = replay_signal(
            c_high[lo:hi], c_low[lo:hi], c_close[lo:hi], float(s_entry[k]), bool(s_long[k])
        )
        it, isl = i_tp[str(tp)], i_sl[str(sl)]
        if it is not None and (isl is None or it < isl):  # tie inside one candle -> SL
            pnl, step = tp, it
        elif isl is not None:
            pnl, step = -sl, isl
        else:
            pnl, step = mark, (hi - lo) - 1
        exit_ts = int(c_ts[min(hi - 1, lo + step)])
        recs.append(
            {
                "model": models[s_mod[k]],
                "direction": direction,
                "open_ts": int(s_ts[k]),
                "exit_ts": exit_ts,
                "pnl_pct": float(pnl) - FEE_PCT,
                "symbol": symbols[s_sym[k]],
                "oi": float("nan"),
                "vol": float(vol[k]),
                "hold_h": max(CANDLE_S, exit_ts + CANDLE_S - int(s_ts[k])) / 3600.0,
            }
        )
    recs.sort(key=lambda r: r["open_ts"])
    return recs


def stats(recs: list[dict]) -> dict:
    """Per-trade and per-slot-hour economics of a record set."""
    if not recs:
        return {"n": 0}
    pnl = np.array([r["pnl_pct"] for r in recs])
    hold = np.array([r["hold_h"] for r in recs])
    span_d = max(1e-9, (recs[-1]["open_ts"] - recs[0]["open_ts"]) / 86400.0)
    weeks: dict[str, list[float]] = defaultdict(list)
    for r in recs:
        iso = datetime.fromtimestamp(r["open_ts"], UTC).isocalendar()
        weeks[f"{iso.year}-W{iso.week:02d}"].append(r["pnl_pct"])
    weekly = {wk: round(float(np.mean(v)), 4) for wk, v in sorted(weeks.items())}
    return {
        "n": len(recs),
        "per_day": round(len(recs) / span_d, 1),
        "mean_pp": round(float(pnl.mean()), 4),
        "t_stat": round(float(pnl.mean() / (pnl.std(ddof=1) / np.sqrt(len(pnl)))), 2) if len(pnl) > 2 else None,
        "win_rate": round(float((pnl > 0).mean()), 4),
        "median_hold_h": round(float(np.median(hold)), 2),
        "pp_per_slot_hour": round(float(pnl.sum() / hold.sum()), 5),
        "avg_concurrent": round(len(recs) / span_d * float(hold.mean()) / 24.0, 1),
        "weekly_mean_pp": weekly,
        "weeks_positive": sum(1 for v in weekly.values() if v > 0),
        "weeks_total": len(weekly),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="FIF2 single-TP decision backtest (T-2026-KYT-9050-111)")
    ap.add_argument("--in", dest="infile", default="reports/leg_composition_raw.npz")
    ap.add_argument("--out", default="reports/fif2_single_tp_backtest.json")
    args = ap.parse_args()

    z = np.load(args.infile, allow_pickle=True)
    src_meta = json.loads(str(z["meta"][0]))
    print(f"Loaded {len(z['s_ts']):,} signals from {args.infile} (export since {src_meta.get('since')})")

    fit = lcr._timestamp_domain_fit(
        c_sym=z["c_sym"],
        c_ts=z["c_ts"],
        c_high=z["c_high"],
        c_low=z["c_low"],
        s_sym=z["s_sym"],
        s_ts=z["s_ts"],
        s_entry=z["s_entry"],
    )
    print(f"Input timestamp-domain fit: {fit['rate']:.1%}")
    if fit["rate"] < lcr.DOMAIN_FIT_MIN:
        raise SystemExit("Input export FAILS the timestamp-domain check — re-export first.")

    # sym_vol_4h per signal, exactly as in T-110 (closed candles only)
    symbols = list(z["symbols"])
    s_ts, s_sym = z["s_ts"], z["s_sym"]
    c_sym, c_ts, c_close = z["c_sym"], z["c_ts"], z["c_close"]
    starts = np.searchsorted(c_sym, np.arange(len(symbols)), side="left")
    ends = np.searchsorted(c_sym, np.arange(len(symbols)), side="right")
    vol = np.full(len(s_ts), np.nan)
    for si in range(len(symbols)):
        sel = np.flatnonzero(s_sym == si)
        if sel.size == 0:
            continue
        ts, close = c_ts[starts[si] : ends[si]], c_close[starts[si] : ends[si]]
        if len(ts) == 0:
            continue
        vol[sel] = series_features(ts, close, s_ts[sel], {}, vol_key="v")["v"]

    results: dict[str, dict] = {}
    for geom_name, geometry in GEOMETRIES.items():
        print(f"\n=== geometry {geom_name} ===")
        singles = single_tp_records(z, geometry, vol)
        ladder = pb.precompute(z, geometry, None, None)
        # the ladder walk covers the same signals in the same order; carry the
        # gate feature over positionally and refuse to guess if that ever drifts
        if len(ladder) != len(singles):
            raise SystemExit(f"record alignment broke: {len(singles)} single-TP vs {len(ladder)} ladder")
        for r_l, r_s in zip(ladder, singles, strict=True):
            if (r_l["model"], r_l["open_ts"], r_l["symbol"]) != (r_s["model"], r_s["open_ts"], r_s["symbol"]):
                raise SystemExit("record alignment broke: order mismatch between ladder and single-TP walks")
            r_l["vol"] = r_s["vol"]
            r_l["hold_h"] = max(CANDLE_S, r_l["exit_ts"] + CANDLE_S - r_l["open_ts"]) / 3600.0

        cut_ts = np.quantile([r["open_ts"] for r in singles], TRAIN_SHARE)
        train = [r for r in singles if r["open_ts"] <= cut_ts]
        thresholds = {f"q{int(q * 100)}": float(np.nanquantile([r["vol"] for r in train], q)) for q in GATE_QUANTILES}
        print("  train vol thresholds: " + ", ".join(f"{k}={v:.4f}" for k, v in thresholds.items()))

        for exit_name, recs in (("single_tp", singles), ("ladder", ladder)):
            for gate_name, thr in [("ungated", None)] + list(thresholds.items()):
                kept = recs if thr is None else [r for r in recs if r["vol"] == r["vol"] and r["vol"] >= thr]
                te = [r for r in kept if r["open_ts"] > cut_ts]
                tr = [r for r in kept if r["open_ts"] <= cut_ts]
                s_te, s_tr = stats(te), stats(tr)
                key = f"{geom_name}|{exit_name}|{gate_name}"
                results[key] = {"train": s_tr, "test": s_te, "threshold": thr}
                if s_te.get("n"):
                    print(
                        f"  {exit_name:9s} {gate_name:8s} TEST n={s_te['n']:>5} ({s_te['per_day']:>5}/d) "
                        f"pp={s_te['mean_pp']:+.3f} (t={s_te['t_stat']}) wr={s_te['win_rate']:.1%} "
                        f"hold={s_te['median_hold_h']:>5.1f}h pp/slot-h={s_te['pp_per_slot_hour']:+.5f} "
                        f"conc~{s_te['avg_concurrent']} wk+ {s_te['weeks_positive']}/{s_te['weeks_total']}"
                        f"   (train pp={s_tr['mean_pp']:+.3f})"
                    )

    # portfolio sanity run of the primary candidate cell (defensive variant:
    # pb.simulate keeps its weekly leg-selection — the bot Michi described
    # mirrors everything past the gate, so this UNDERSTATES its trade count but
    # uses the identical, tested admission machinery)
    prim_geom = "t104"
    thr = results[f"{prim_geom}|single_tp|q80"]["threshold"]
    singles = single_tp_records(z, GEOMETRIES[prim_geom], vol)
    gated = [r for r in singles if r["vol"] == r["vol"] and r["vol"] >= thr]
    port = pb.simulate(gated, capital=1000.0, fixed_usd=5.0, leverage=5.0)
    print(
        f"\nportfolio run (t104 single-TP q80, 1000 EUR, 5 EUR, 5x, T-105 admission): "
        f"taken={port['trades_taken']} ret={port['return_pct']:+.2f}% maxDD={port['max_drawdown_pct']:.2f}% "
        f"bind={port['binding_constraint']}"
    )

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "task": "T-2026-KYT-9050-111",
                "input": os.path.basename(args.infile),
                "input_since": src_meta.get("since"),
                "input_domain_fit": round(fit["rate"], 4),
                "geometries": GEOMETRIES,
                "fee_pct": FEE_PCT,
                "gate_quantiles": GATE_QUANTILES,
                "slot_cap": SLOT_CAP,
                "results": results,
                "portfolio_t104_single_tp_q80": port,
            },
            fh,
            indent=1,
        )
    print(f"\nWritten: {args.out}")


if __name__ == "__main__":
    main()

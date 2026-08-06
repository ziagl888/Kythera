# tools/oi_gate_eval.py — does open interest separate good SHORT signals from bad ones?
"""OI as a GATE on existing signals — the question T-094 and T-096 both left open.

T-2026-KYT-9050-104. Two prior studies touched OI and answered different questions:

* ``oi_event_study_t096.md`` used OI as a **generator**: a rally >=3% whose 4h OI
  *fell* mean-reverts (DIVERGENCE-SHORT, net +0.41/event @1h, t=3.2, n=580,
  8 of 9 weeks positive). That is a proposal for a new leg, not a filter.
* ``oi_liq_gate_verdict_t094.md`` used OI as a **gate** and found no edge
  (AUC ~0.5) — but it measured on the Bot-40 mirror population, which was **83%
  LONG** before the 2026-07-28 cutoff. A filter that fails to separate a
  long-heavy book says close to nothing about separating shorts.

So the gate question has never actually been asked of the SHORT side. It matters
because the short legs are regime-unstable (the same legs run +1.22 pp per trade
over 11.07.-28.07. and -0.37 pp over 28.07.-02.08.), and the channel needs a
short side for direction balance regardless of what it earns. A working gate
would buy that balance at a lower premium; a failing one leaves ``EXPOSURE_CAP``
as the only control, which is a useful answer too.

What this measures
------------------
For every replayed signal, the OI state **as of the signal instant**, then the
discrimination of that state against the already-replayed outcome:

* ``oi_chg_4h`` / ``oi_chg_24h`` — percentage change of open interest, the
  T-096 divergence feature.
* ``oi_pct_30d`` — where current OI sits in the symbol's own trailing 30d range,
  the T-096 squeeze feature.

Reported per direction x cohort: AUC of the feature against a binary win, plus
expectancy per feature bucket. AUC ~0.5 means no gate exists — that is a result,
not a failure, and it is reported as such rather than being tuned away.

Data-quality constraints (measured 2026-08-05, not assumed)
-----------------------------------------------------------
* ``oi_5m`` is **not** a 5-minute table. Effective cadence is a median of 10 min,
  p90 20 min, mean 13.2 — the collector degraded (T-2026-KYT-9050-097). Every
  lookup is therefore **as-of with a staleness cap**, never forward-filled.
* There is a **45h outage 2026-07-12 -> 14** inside the replay window. Signals
  whose as-of lookup lands in it are voided (NaN), not interpolated — a filled
  gap would invent exactly the feature being tested.
* ``liq_events`` only starts 2026-08-03 (2 days). No liquidation feature here;
  that stays with T-095 from ~24.08.

Read-only (hard rule 1). ``features`` touches the DB, ``gate`` does not.

Usage
-----
    python tools/oi_gate_eval.py features --in reports/leg_composition_raw.npz \
        --out reports/oi_features.npz --force-on-busy
    python tools/oi_gate_eval.py gate --in reports/leg_composition_raw.npz \
        --oi reports/oi_features.npz --out reports/oi_gate.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.leg_composition_replay import (  # noqa: E402
    REGIME_CUTOFF_EPOCH,
    outcome,
    replay_signal,
)

STALENESS_CAP_S = 45 * 60  # T-096's cap; ~2x the measured p90 cadence of 20 min
LOOKBACKS = {"4h": 4 * 3600, "24h": 24 * 3600}
PCT_WINDOW_S = 30 * 24 * 3600


# ─────────────────────────────────────────────────────────────────────────────
# FEATURES (the only half that touches the DB)
# ─────────────────────────────────────────────────────────────────────────────


def cmd_features(args: argparse.Namespace) -> None:
    from core.database import get_db_connection
    from tools.walkforward_sim import set_low_priority
    from tools.whitelist_v2_realized_eval import wait_for_cpu_headroom

    set_low_priority()
    cpu = wait_for_cpu_headroom(args.cpu_wait_min, args.force_on_busy)

    z = np.load(args.infile, allow_pickle=True)
    symbols = list(z["symbols"])
    s_sym, s_ts = z["s_sym"], z["s_ts"]
    n = len(s_ts)
    print(f"{n:,} signals over {len(symbols)} symbols — pulling OI (as-of, cap {STALENESS_CAP_S // 60} min) ...")

    lo_ts = int(s_ts.min()) - PCT_WINDOW_S
    hi_ts = int(s_ts.max())

    feats = {k: np.full(n, np.nan) for k in ("oi_chg_4h", "oi_chg_24h", "oi_pct_30d")}
    by_symbol: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        by_symbol[int(s_sym[i])].append(i)

    conn = get_db_connection()
    voided = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            for done, (sym_idx, idxs) in enumerate(sorted(by_symbol.items()), 1):
                cur.execute(
                    """SELECT extract(epoch FROM ts)::bigint, open_interest
                         FROM oi_5m
                        WHERE symbol = %s AND ts >= to_timestamp(%s) AND ts <= to_timestamp(%s)
                          AND open_interest > 0
                        ORDER BY ts""",
                    (symbols[sym_idx], lo_ts, hi_ts),
                )
                block = cur.fetchall()
                if len(block) < 2:
                    voided += len(idxs)
                    continue
                ts = np.fromiter((r[0] for r in block), dtype=np.int64, count=len(block))
                oi = np.fromiter((r[1] for r in block), dtype=np.float64, count=len(block))

                for i in idxs:
                    t = int(s_ts[i])
                    now = _as_of(ts, oi, t)
                    if now is None:
                        voided += 1
                        continue
                    for name, back in LOOKBACKS.items():
                        then = _as_of(ts, oi, t - back)
                        if then is not None and then > 0:
                            feats[f"oi_chg_{name}"][i] = (now / then - 1.0) * 100.0
                    lo = int(np.searchsorted(ts, t - PCT_WINDOW_S, side="left"))
                    hi = int(np.searchsorted(ts, t, side="right"))
                    if hi - lo >= 50:  # enough history for a percentile to mean anything
                        window = oi[lo:hi]
                        feats["oi_pct_30d"][i] = float((window < now).mean() * 100.0)
                if done % 50 == 0 or done == len(by_symbol):
                    print(f"  {done}/{len(by_symbol)} symbols", flush=True)
    finally:
        conn.close()

    have = int(np.isfinite(feats["oi_chg_4h"]).sum())
    print(f"OI resolved for {have:,}/{n:,} signals ({have / n:.1%}); {voided:,} voided (stale/outage/no history).")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    np.savez_compressed(
        args.out,
        cpu_at_export=np.array([cpu if cpu is not None else -1.0]),
        staleness_cap_s=np.array([STALENESS_CAP_S]),
        **feats,
    )
    print(f"Written: {args.out}")


def _as_of(ts: np.ndarray, oi: np.ndarray, t: int) -> float | None:
    """Last OI point at or before ``t``, or None if it is staler than the cap.

    Voiding beats filling: an interpolated point would manufacture the very
    change this study measures (P0.12 — stale rows are voided, not filled).
    """
    j = int(np.searchsorted(ts, t, side="right")) - 1
    if j < 0 or t - int(ts[j]) > STALENESS_CAP_S:
        return None
    return float(oi[j])


# ─────────────────────────────────────────────────────────────────────────────
# GATE (numpy only — no DB)
# ─────────────────────────────────────────────────────────────────────────────


def auc(scores: np.ndarray, wins: np.ndarray) -> float:
    """Rank AUC — ties averaged. 0.5 means the feature carries no information."""
    if wins.sum() == 0 or (~wins).sum() == 0:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=np.float64)
    ranks[order] = np.arange(1, len(scores) + 1, dtype=np.float64)
    # average ranks within ties so a constant feature scores exactly 0.5
    _, inv, counts = np.unique(scores, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=ranks)
    ranks = (sums / counts)[inv]
    n_pos = int(wins.sum())
    n_neg = len(wins) - n_pos
    return float((ranks[wins].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def cmd_gate(args: argparse.Namespace) -> None:
    z = np.load(args.infile, allow_pickle=True)
    zo = np.load(args.oi)
    meta = json.loads(str(z["meta"][0]))
    horizon_s = int(meta["horizon_h"] * 3600)
    symbols, models = list(z["symbols"]), list(z["models"])
    c_sym, c_ts, c_high, c_low, c_close = z["c_sym"], z["c_ts"], z["c_high"], z["c_low"], z["c_close"]
    s_sym, s_mod, s_long, s_ts, s_entry = z["s_sym"], z["s_mod"], z["s_long"], z["s_ts"], z["s_entry"]

    starts = np.searchsorted(c_sym, np.arange(len(symbols)), side="left")
    ends = np.searchsorted(c_sym, np.arange(len(symbols)), side="right")

    tp, sl = args.tp, args.sl
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
        i_tp, i_sl, mark, _, _ = replay_signal(
            c_high[lo:hi], c_low[lo:hi], c_close[lo:hi], float(s_entry[k]), bool(s_long[k])
        )
        res = outcome(i_tp[str(tp)], i_sl[str(sl)])
        pnl = tp if res == "TP" else (-sl if res == "SL" else mark)
        recs.append(
            {
                "model": models[s_mod[k]],
                "direction": "LONG" if s_long[k] else "SHORT",
                "cohort": "post" if s_ts[k] >= REGIME_CUTOFF_EPOCH else "pre",
                "pnl": pnl,
                "win": pnl > 0,
                **{f: float(zo[f][k]) for f in ("oi_chg_4h", "oi_chg_24h", "oi_pct_30d")},
            }
        )
    print(f"Scored {len(recs):,} signals at TP {tp}% / SL {sl}%.")

    report: dict = {"task": "T-2026-KYT-9050-104", "tp": tp, "sl": sl, "cells": {}}
    for feature in ("oi_chg_4h", "oi_chg_24h", "oi_pct_30d"):
        for direction in ("SHORT", "LONG"):
            for cohort in ("pre", "post"):
                sub = [
                    r for r in recs if r["direction"] == direction and r["cohort"] == cohort and np.isfinite(r[feature])
                ]
                if len(sub) < 100:
                    continue
                x = np.array([r[feature] for r in sub])
                w = np.array([r["win"] for r in sub])
                p = np.array([r["pnl"] for r in sub])
                qs = np.quantile(x, [0.2, 0.4, 0.6, 0.8])
                buckets = []
                for lo_q, hi_q in zip([-np.inf, *qs], [*qs, np.inf], strict=True):
                    m = (x >= lo_q) & (x < hi_q)
                    if m.sum() == 0:
                        continue
                    buckets.append(
                        {
                            "range": [
                                None if np.isinf(lo_q) else round(float(lo_q), 3),
                                None if np.isinf(hi_q) else round(float(hi_q), 3),
                            ],
                            "n": int(m.sum()),
                            "win_rate": round(float(w[m].mean()) * 100, 1),
                            "exp_pp": round(float(p[m].mean()), 3),
                        }
                    )
                report["cells"][f"{feature}|{direction}|{cohort}"] = {
                    "n": len(sub),
                    "auc": round(auc(x, w), 4),
                    "buckets": buckets,
                }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    print_gate(report)
    print(f"\nWritten: {args.out}")


def print_gate(report: dict) -> None:
    print(f"\n=== OI gate discrimination at TP {report['tp']}% / SL {report['sl']}% ===")
    print("  AUC 0.50 = the feature carries no information. Read it before reading the buckets.\n")
    for key, cell in sorted(report["cells"].items()):
        feature, direction, cohort = key.split("|")
        print(f"  {feature:<12}{direction:<7}{cohort:<6}n={cell['n']:<7}AUC {cell['auc']:.3f}")
        for b in cell["buckets"]:
            lo = "-inf" if b["range"][0] is None else f"{b['range'][0]:+.2f}"
            hi = "+inf" if b["range"][1] is None else f"{b['range'][1]:+.2f}"
            print(f"       [{lo:>8} .. {hi:>8})  n={b['n']:<6} WR {b['win_rate']:>5.1f}%  exp {b['exp_pp']:+.3f}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description="OI-as-a-gate evaluation on existing signals (T-2026-KYT-9050-104)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    fe = sub.add_parser("features", help="read-only OI feature export, aligned to the replay signals")
    fe.add_argument("--in", dest="infile", default="reports/leg_composition_raw.npz")
    fe.add_argument("--out", default="reports/oi_features.npz")
    fe.add_argument("--cpu-wait-min", type=int, default=0)
    fe.add_argument("--force-on-busy", action="store_true")
    fe.set_defaults(func=cmd_features)

    ga = sub.add_parser("gate", help="AUC + bucket expectancy — numpy only, no DB")
    ga.add_argument("--in", dest="infile", default="reports/leg_composition_raw.npz")
    ga.add_argument("--oi", default="reports/oi_features.npz")
    ga.add_argument("--out", default="reports/oi_gate.json")
    ga.add_argument("--tp", type=float, default=4.0)
    ga.add_argument("--sl", type=float, default=3.0)
    ga.set_defaults(func=cmd_gate)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

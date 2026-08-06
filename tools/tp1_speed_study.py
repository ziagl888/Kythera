# tools/tp1_speed_study.py — which signals pay their slot back fast?
"""TP1-speed pattern study over the exported fleet signals (T-2026-KYT-9050-110).

Michi's question after T-108/T-109: can market context at signal time (BTC,
BTCDOM, open interest, ...) predict which trades reach TP1 *fast*? The economic
motivation comes straight from T-105: the bot-40 book is margin/slot-bound, so
capital turnover per slot — not per-trade expectancy — is the scarce resource.
A trade that takes TP1 in an hour returns its margin 70x faster than one that
limps to the horizon.

Pre-registered design (written before the first run)
----------------------------------------------------
* **Label:** under the t104 geometry (LONG TP1 4 % / SL 5 %, SHORT TP1 3 % /
  SL 2 %), ``fast = TP1 touched strictly before SL`` (a same-candle tie books
  as SL, exactly like ``outcome()``) ``and the touching candle closes within
  4 h of the signal``. 12 h is the pre-registered secondary window, reported as
  robustness, not cherry-picked afterwards.
* **Features:** as-of the last candle that CLOSED before the signal instant
  (open_time <= s_ts - 300 — the forming candle is never touched, hard rule 5),
  with a 15-minute staleness tolerance and NaN where history is missing:
  BTC returns 1 h/4 h/24 h + 4 h realised vol, BTCDOM returns 4 h/24 h, the
  signal symbol's own returns 1 h/4 h/24 h + 4 h vol, and the three aligned OI
  features from the T-104 export (oi_chg_4h, oi_chg_24h, oi_pct_30d).
* **Validation:** chronological 70/30 split on signal time. Per-feature AUC
  (fast vs rest) on train, confirmed on test; per-ISO-week AUC sign consistency
  for the top features. With 13 features x 2 windows the Bonferroni line for
  alpha 0.05 would sit at p < 0.0019 — but see the caveat below: this run does
  NOT compute p-values, so that line is never actually applied.
* **Not here, stated openly:** funding and forced-liquidation features need the
  VPS DB or an API backfill (``liq_events`` only exists since 2026-08-03 — a
  three-day history is not a feature), and signals cluster in time, so the
  effective sample is far below 43k — the weekly consistency table is the
  honest lens, not the raw N.
* **Two design promises this run does not keep** (named rather than buried,
  found in the T-112 pre-merge review): the task brief also listed a
  *time-of-day* feature — it is not built, so nothing here says whether the
  signal hour matters. And no significance test is computed at all: the
  verdicts below rest on AUC magnitude plus per-week sign consistency, not on
  the Bonferroni line quoted above. For the top feature (AUC 0.79 at n ~ 30k)
  that distinction is academic; for the 0.54-0.56 band it is exactly the
  distinction between "signal" and "suggestive", so treat that band as
  suggestive only. Both gaps are follow-up work, not corrections to the
  headline.

This is an association study producing a verdict, not a deployable gate.

DB-free: consumes the T-104/T-105 ``.npz`` exports. Nothing here touches the
live database or the fleet.

Usage
-----
    python tools/tp1_speed_study.py --in reports/leg_composition_raw.npz \\
        --oi reports/oi_features.npz --out reports/tp1_speed_study.json
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
from core.vol_features import VOL_WINDOW_5M, rolling_std_pct  # noqa: E402
from tools.leg_composition_replay import replay_signal  # noqa: E402

UTC = timezone.utc

GEOMETRY = {"LONG": (4.0, 5.0), "SHORT": (3.0, 2.0)}  # t104 — the live-relevant one
CANDLE_S = 300  # 5m export grid
STALE_TOLERANCE_S = 900  # a lookback anchor may be at most 15 min older than asked
FAST_WINDOW_H = 4.0  # primary, pre-registered
SLOW_WINDOW_H = 12.0  # secondary, robustness
VOL_WINDOW = VOL_WINDOW_5M  # 4h of 5m candles
MIN_MODEL_N = 200  # descriptive per-model table only above this
TRAIN_SHARE = 0.7

# The rolling-std implementation moved to core/vol_features.py (T-112) so the
# FIF2 bot serves the exact number this study validated — one source, no drift.
rolling_std = rolling_std_pct


def asof_index(ts: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Last candle index with open_time <= target; -1 where none exists."""
    return np.searchsorted(ts, targets, side="right") - 1


def series_features(
    ts: np.ndarray, close: np.ndarray, s_ts: np.ndarray, lookbacks_s: dict[str, int], vol_key: str | None = None
) -> dict[str, np.ndarray]:
    """As-of returns (pct) per lookback + optional 4h vol, NaN where stale/missing."""
    anchor = s_ts - CANDLE_S  # last instant a candle may OPEN at and still be closed
    i = asof_index(ts, anchor)
    i_ok = (i >= 0) & (ts[np.clip(i, 0, None)] >= anchor - STALE_TOLERANCE_S)
    out: dict[str, np.ndarray] = {}
    for name, lb in lookbacks_s.items():
        j = asof_index(ts, anchor - lb)
        j_ok = (j >= 0) & (ts[np.clip(j, 0, None)] >= anchor - lb - STALE_TOLERANCE_S)
        ok = i_ok & j_ok
        vals = np.full(len(s_ts), np.nan)
        ii, jj = np.clip(i, 0, None), np.clip(j, 0, None)
        with np.errstate(invalid="ignore", divide="ignore"):
            vals[ok] = (close[ii[ok]] / close[jj[ok]] - 1.0) * 100.0
        out[name] = vals
    if vol_key is not None:
        rs = rolling_std(close, VOL_WINDOW)
        vals = np.full(len(s_ts), np.nan)
        vals[i_ok] = rs[np.clip(i, 0, None)[i_ok]]
        out[vol_key] = vals
    return out


def auc_fast_vs_rest(x: np.ndarray, y: np.ndarray) -> tuple[float, int]:
    """Mann-Whitney AUC of `x` for y==1 vs y==0, NaN rows dropped, ties averaged."""
    m = ~np.isnan(x)
    x, y = x[m], y[m]
    n1, n0 = int(y.sum()), int((~y.astype(bool)).sum())
    if n1 == 0 or n0 == 0:
        return float("nan"), n1 + n0
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x))
    ranks[order] = np.arange(1, len(x) + 1)
    # average ties so a discretised feature cannot fake separation
    xs = x[order]
    k = 0
    while k < len(xs):
        j = k
        while j + 1 < len(xs) and xs[j + 1] == xs[k]:
            j += 1
        if j > k:
            ranks[order[k : j + 1]] = ranks[order[k : j + 1]].mean()
        k = j + 1
    u = ranks[y.astype(bool)].sum() - n1 * (n1 + 1) / 2
    return float(u / (n1 * n0)), n1 + n0


def compute_labels(z) -> dict[str, np.ndarray]:
    """Per signal: TP1-before-SL flag and the hit instant, under t104 geometry."""
    meta = json.loads(str(z["meta"][0]))
    horizon_s = int(meta["horizon_h"] * 3600)
    symbols = list(z["symbols"])
    c_sym, c_ts = z["c_sym"], z["c_ts"]
    c_high, c_low, c_close = z["c_high"], z["c_low"], z["c_close"]
    s_sym, s_long, s_ts, s_entry = z["s_sym"], z["s_long"], z["s_ts"], z["s_entry"]

    starts = np.searchsorted(c_sym, np.arange(len(symbols)), side="left")
    ends = np.searchsorted(c_sym, np.arange(len(symbols)), side="right")

    n = len(s_ts)
    tp_first = np.zeros(n, dtype=bool)
    sl_first = np.zeros(n, dtype=bool)
    hit_s = np.full(n, np.nan)  # seconds from signal to the close of the touching candle
    covered = np.zeros(n, dtype=bool)
    for k in range(n):
        a, b = starts[s_sym[k]], ends[s_sym[k]]
        if a == b:
            continue
        ts = c_ts[a:b]
        lo = a + int(np.searchsorted(ts, s_ts[k], side="right"))
        hi = a + int(np.searchsorted(ts, s_ts[k] + horizon_s, side="right"))
        if hi <= lo:
            continue
        covered[k] = True
        direction = "LONG" if s_long[k] else "SHORT"
        tp, sl = GEOMETRY[direction]
        i_tp, i_sl, _, _, _ = replay_signal(
            c_high[lo:hi], c_low[lo:hi], c_close[lo:hi], float(s_entry[k]), bool(s_long[k])
        )
        it, isl = i_tp[str(tp)], i_sl[str(sl)]
        if it is not None and (isl is None or it < isl):  # tie inside one candle -> SL
            tp_first[k] = True
            hit_s[k] = float(c_ts[lo + it] + CANDLE_S - s_ts[k])
        elif isl is not None:  # SL first, or the tie
            sl_first[k] = True
    return {"tp_first": tp_first, "sl_first": sl_first, "hit_s": hit_s, "covered": covered}


def weekly_auc(x: np.ndarray, y: np.ndarray, s_ts: np.ndarray) -> list[tuple[str, float, int]]:
    weeks = defaultdict(list)
    for idx in range(len(s_ts)):
        iso = datetime.fromtimestamp(int(s_ts[idx]), UTC).isocalendar()
        weeks[f"{iso.year}-W{iso.week:02d}"].append(idx)
    rows = []
    for wk in sorted(weeks):
        sel = np.array(weeks[wk])
        a, n = auc_fast_vs_rest(x[sel], y[sel])
        rows.append((wk, a, n))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="TP1-speed pattern study (T-2026-KYT-9050-110)")
    ap.add_argument("--in", dest="infile", default="reports/leg_composition_raw.npz")
    ap.add_argument("--oi", default="reports/oi_features.npz")
    ap.add_argument("--out", default="reports/tp1_speed_study.json")
    args = ap.parse_args()

    z = np.load(args.infile, allow_pickle=True)
    src_meta = json.loads(str(z["meta"][0]))
    print(f"Loaded {len(z['s_ts']):,} signals from {args.infile} (export since {src_meta.get('since')})")

    # Same input gate as the T-105/T-108 tools: refuse the defective export.
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

    symbols = list(z["symbols"])
    s_ts, s_long, s_sym = z["s_ts"], z["s_long"], z["s_sym"]

    print("Computing labels (t104 geometry, first-touch replay) ...")
    lab = compute_labels(z)
    covered = lab["covered"]
    fast4 = lab["tp_first"] & (lab["hit_s"] <= FAST_WINDOW_H * 3600)
    fast12 = lab["tp_first"] & (lab["hit_s"] <= SLOW_WINDOW_H * 3600)
    hit_h = lab["hit_s"][lab["tp_first"]] / 3600.0
    print(
        f"  covered {covered.sum():,}/{len(covered):,} · TP1-first {lab['tp_first'].mean():.1%} · "
        f"fast4h {fast4.mean():.1%} · fast12h {fast12.mean():.1%} · "
        f"median hit {np.median(hit_h):.1f}h (of TP1-first)"
    )

    # ── features ────────────────────────────────────────────────────────────
    feats: dict[str, np.ndarray] = {}
    c_sym, c_ts, c_close = z["c_sym"], z["c_ts"], z["c_close"]
    starts = np.searchsorted(c_sym, np.arange(len(symbols)), side="left")
    ends = np.searchsorted(c_sym, np.arange(len(symbols)), side="right")

    def ref_series(sym: str) -> tuple[np.ndarray, np.ndarray] | None:
        if sym not in symbols:
            return None
        i = symbols.index(sym)
        return c_ts[starts[i] : ends[i]], c_close[starts[i] : ends[i]]

    btc = ref_series("BTCUSDT")
    if btc is not None:
        feats.update(
            series_features(
                btc[0],
                btc[1],
                s_ts,
                {"btc_ret_1h": 3600, "btc_ret_4h": 14400, "btc_ret_24h": 86400},
                vol_key="btc_vol_4h",
            )
        )
    dom = ref_series("BTCDOMUSDT")
    if dom is not None:
        feats.update(series_features(dom[0], dom[1], s_ts, {"btcdom_ret_4h": 14400, "btcdom_ret_24h": 86400}))

    print("Computing own-symbol context ...")
    for name in ("sym_ret_1h", "sym_ret_4h", "sym_ret_24h", "sym_vol_4h"):
        feats[name] = np.full(len(s_ts), np.nan)
    for si in range(len(symbols)):
        sel = np.flatnonzero(s_sym == si)
        if sel.size == 0:
            continue
        ts, close = c_ts[starts[si] : ends[si]], c_close[starts[si] : ends[si]]
        if len(ts) == 0:
            continue
        f = series_features(
            ts, close, s_ts[sel], {"sym_ret_1h": 3600, "sym_ret_4h": 14400, "sym_ret_24h": 86400}, vol_key="sym_vol_4h"
        )
        for name, vals in f.items():
            feats[name][sel] = vals

    if args.oi and os.path.exists(args.oi):
        oi = np.load(args.oi)
        for k in ("oi_chg_4h", "oi_chg_24h", "oi_pct_30d"):
            feats[k] = np.asarray(oi[k], dtype=float)
    else:
        print("  (no OI file — OI features skipped)")

    # ── evaluation: chronological split, per-feature AUC, weekly consistency ─
    mask = covered
    order = np.argsort(s_ts[mask], kind="mergesort")
    idx_all = np.flatnonzero(mask)[order]
    cut = int(len(idx_all) * TRAIN_SHARE)
    tr, te = idx_all[:cut], idx_all[cut:]
    split_ts = int(s_ts[tr[-1]])
    print(f"Split: train {len(tr):,} (to {datetime.fromtimestamp(split_ts, UTC):%m-%d %H:%M}Z) · test {len(te):,}")

    results: dict[str, dict] = {}
    for label_name, y in (("fast_4h", fast4), ("fast_12h", fast12)):
        rows = []
        for name, x in feats.items():
            auc_tr, n_tr = auc_fast_vs_rest(x[tr], y[tr])
            auc_te, n_te = auc_fast_vs_rest(x[te], y[te])
            rows.append(
                {
                    "feature": name,
                    "auc_train": round(auc_tr, 4),
                    "auc_test": round(auc_te, 4),
                    "n_train": n_tr,
                    "n_test": n_te,
                    "nan_share": round(float(np.mean(np.isnan(x[idx_all]))), 4),
                }
            )
        rows.sort(key=lambda r: abs(r["auc_train"] - 0.5), reverse=True)
        results[label_name] = {"base_rate": round(float(y[idx_all].mean()), 4), "features": rows}
        print(f"\n=== label {label_name} (base rate {y[idx_all].mean():.1%}) ===")
        print(f"  {'feature':14s} {'AUC train':>10s} {'AUC test':>10s} {'NaN':>6s}")
        for r in rows:
            print(f"  {r['feature']:14s} {r['auc_train']:>10.4f} {r['auc_test']:>10.4f} {r['nan_share']:>6.1%}")

    # weekly consistency for the top-3 train features of the primary label
    top3 = [r["feature"] for r in results["fast_4h"]["features"][:3]]
    weekly: dict[str, list] = {}
    print("\n=== weekly AUC consistency (fast_4h, top-3 train features) ===")
    for name in top3:
        rows_w = weekly_auc(feats[name][idx_all], fast4[idx_all], s_ts[idx_all])
        weekly[name] = [[wk, round(a, 4) if a == a else None, n] for wk, a, n in rows_w]
        direction = ">" if results["fast_4h"]["features"][top3.index(name)]["auc_train"] > 0.5 else "<"
        agree = sum(1 for _, a, _ in rows_w if a == a and ((a > 0.5) == (direction == ">")))
        total = sum(1 for _, a, _ in rows_w if a == a)
        cells = "  ".join(f"{wk[-3:]}:{a:.3f}" if a == a else f"{wk[-3:]}:-" for wk, a, _ in rows_w)
        print(f"  {name:14s} {agree}/{total} weeks agree with train direction  [{cells}]")

    # Decile table for the top train feature — the tautology check. A feature
    # can "predict fast TP1" by simply speeding up EVERYTHING, stop-loss
    # included; whether the SL-first rate rises alongside the fast rate across
    # deciles is what separates "selects fast winners" from "selects fast
    # trades". Edges are fit on train only, applied everywhere.
    top1 = results["fast_4h"]["features"][0]["feature"]
    xv = feats[top1]
    edges = np.nanquantile(xv[tr], np.linspace(0.1, 0.9, 9))
    bins = np.digitize(xv[idx_all], edges)
    bins[np.isnan(xv[idx_all])] = -1
    decile_rows = []
    print(f"\n=== {top1} deciles (train-fit edges) · fast4h vs TP1-first vs SL-first ===")
    for d in range(10):
        sel = idx_all[bins == d]
        if len(sel) == 0:
            continue
        row = {
            "decile": d + 1,
            "n": int(len(sel)),
            "fast_4h": round(float(fast4[sel].mean()), 4),
            "tp1_first": round(float(lab["tp_first"][sel].mean()), 4),
            "sl_first": round(float(lab["sl_first"][sel].mean()), 4),
        }
        decile_rows.append(row)
        print(
            f"  D{row['decile']:>2d} n={row['n']:>5d}  fast4h {row['fast_4h']:>6.1%}  "
            f"TP1-first {row['tp1_first']:>6.1%}  SL-first {row['sl_first']:>6.1%}"
        )

    # per-direction and per-model base rates — descriptive, no test
    per_dir = {
        "LONG": round(float(fast4[idx_all][s_long[idx_all]].mean()), 4),
        "SHORT": round(float(fast4[idx_all][~s_long[idx_all]].mean()), 4),
    }
    models = list(z["models"])
    per_model = {}
    for mi, m in enumerate(models):
        sel = idx_all[z["s_mod"][idx_all] == mi]
        if len(sel) >= MIN_MODEL_N:
            per_model[m] = {"n": int(len(sel)), "fast_4h": round(float(fast4[sel].mean()), 4)}
    print(f"\nfast_4h by direction: {per_dir}")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "task": "T-2026-KYT-9050-110",
                "input": os.path.basename(args.infile),
                "input_since": src_meta.get("since"),
                "input_domain_fit": round(fit["rate"], 4),
                "geometry": GEOMETRY,
                "fast_window_h": FAST_WINDOW_H,
                "secondary_window_h": SLOW_WINDOW_H,
                "covered": int(covered.sum()),
                "tp1_first_rate": round(float(lab["tp_first"].mean()), 4),
                "median_hit_h_tp1_first": round(float(np.median(hit_h)), 2),
                "split_ts": split_ts,
                "results": results,
                "top_feature_deciles": {"feature": top1, "rows": decile_rows},
                "weekly_auc_fast4h": weekly,
                "fast4h_by_direction": per_dir,
                "fast4h_by_model": per_model,
                "gaps": "funding + forced-liq features need VPS DB / API backfill; liq_events only since 2026-08-03",
            },
            fh,
            indent=1,
        )
    print(f"\nWritten: {args.out}")


if __name__ == "__main__":
    main()

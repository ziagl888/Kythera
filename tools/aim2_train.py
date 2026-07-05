"""
tools/aim2_train.py — Training des AIM2-Master-Meta-Modells (docs/AIM2_DESIGN.md).

Konsumiert <staging>/replay/aim2_events.jsonl (tools/aim2_build_dataset.py) und
trainiert das binäre Gate „TP1 vor SL?" über alle Quellsignale.

Methodik (Batch-E-Gerüst, X-R2/R4-Fixes):
  * chronologischer 70/15/15-Split mit 7-Tage-Purge-Gap (P1.29)
  * XGBoost binär, Early Stopping auf Validation
  * Isotonic-Kalibrierung auf Validation
  * Threshold-Wahl per gewichtetem Replay-Netto-PnL auf Validation
  * Test bleibt bis zum Abschlussreport unberührt
  * Artefakt NUR nach staging_models (P1.35): model, features, threshold,
    calibrator, meta — Bot 15 liest model/features/threshold/calibrator.

Beispiel:
  python tools/aim2_train.py
  python tools/aim2_train.py --events <pfad> --min-val-trades 50
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

STAGING_DIR = os.getenv("KYTHERA_STAGING_DIR", r"C:\Users\Michael\Documents\_X\staging_models")
REPLAY_DIR = os.getenv("KYTHERA_REPLAY_DIR", os.path.join(STAGING_DIR, "replay"))
PURGE_DAYS = 7


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_events(path: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    metas, feats = [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            f = rec.pop("features")
            metas.append(rec)
            feats.append(f)
    meta = pd.DataFrame(metas)
    X = pd.DataFrame(feats).fillna(0.0)  # src_*-One-Hots sind sparse → 0
    meta["ts"] = pd.to_datetime(meta["ts"])
    return meta, X


def chrono_split(meta: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, pd.Timestamp, pd.Timestamp]:
    ts = meta["ts"]
    t1 = ts.quantile(0.70)
    t2 = ts.quantile(0.85)
    purge = pd.Timedelta(days=PURGE_DAYS)
    train = (ts <= t1 - purge).to_numpy()
    val = ((ts > t1) & (ts <= t2 - purge)).to_numpy()
    test = (ts > t2).to_numpy()
    return train, val, test, t1, t2


def bucket_report(prob: np.ndarray, y: np.ndarray, pnl: np.ndarray, w: np.ndarray, label: str) -> list[dict]:
    rows = []
    for lo in np.arange(0.0, 1.0, 0.1):
        hi = lo + 0.1
        m = (prob >= lo) & (prob < hi) if hi < 1.0 else (prob >= lo)
        if m.sum() == 0:
            continue
        rows.append({
            "bucket": f"{lo:.1f}-{hi:.1f}", "n": int(m.sum()),
            "mean_prob": round(float(prob[m].mean()), 3),
            "wr": round(float(np.average(y[m], weights=w[m])), 3),
            "avg_pnl": round(float(np.average(pnl[m], weights=w[m])), 3),
        })
    log(f"Reliability [{label}]:")
    for r in rows:
        log(f"  {r['bucket']}: n={r['n']:6d}  p̄={r['mean_prob']:.3f}  WR={r['wr']:.3f}  øPnL={r['avg_pnl']:+.3f}%")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=os.path.join(REPLAY_DIR, "aim2_events.jsonl"))
    ap.add_argument("--out", default=os.path.join(STAGING_DIR, "master_meta_model_aim2.pkl"))
    ap.add_argument("--min-val-trades", type=int, default=50)
    args = ap.parse_args()

    meta, X = load_events(args.events)
    log(f"Events geladen: {len(meta)} | Features: {X.shape[1]}")

    labeled = meta["label"].isin([0, 1]).to_numpy()
    meta, X = meta[labeled].reset_index(drop=True), X[labeled].reset_index(drop=True)
    y = meta["label"].astype(int).to_numpy()
    pnl = pd.to_numeric(meta["net_pnl_pct"], errors="coerce").fillna(0.0).to_numpy()
    w = pd.to_numeric(meta["weight"], errors="coerce").fillna(1.0).to_numpy()
    log(f"Gelabelt: {len(meta)} | Basis-WR (gewichtet): {np.average(y, weights=w):.3f} "
        f"| ø Replay-PnL: {np.average(pnl, weights=w):+.3f}%")

    tr, va, te, t1, t2 = chrono_split(meta)
    log(f"Split: train={tr.sum()} (bis {t1.date()} − {PURGE_DAYS}d) | "
        f"val={va.sum()} (bis {t2.date()} − {PURGE_DAYS}d) | test={te.sum()} (ab {t2.date()})")

    feature_names = list(X.columns)
    model = xgb.XGBClassifier(
        n_estimators=600, learning_rate=0.05, max_depth=5, min_child_weight=5,
        subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0,
        tree_method="hist", eval_metric="auc", early_stopping_rounds=50,
        n_jobs=4, random_state=42,
    )
    model.fit(
        X[tr], y[tr], sample_weight=w[tr],
        eval_set=[(X[va], y[va])], sample_weight_eval_set=[w[va]], verbose=False,
    )
    log(f"Trainiert: best_iteration={model.best_iteration}")

    raw_va = model.predict_proba(X[va])[:, 1]
    raw_te = model.predict_proba(X[te])[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(raw_va, y[va], sample_weight=w[va])
    cal_va = iso.predict(raw_va)
    cal_te = iso.predict(raw_te)

    auc_va = roc_auc_score(y[va], raw_va, sample_weight=w[va])
    auc_te = roc_auc_score(y[te], raw_te, sample_weight=w[te])
    brier_te = brier_score_loss(y[te], cal_te, sample_weight=w[te])
    log(f"AUC val={auc_va:.4f} | AUC test={auc_te:.4f} | Brier test (kalibriert)={brier_te:.4f}")

    # Threshold per Replay-PnL auf VAL (nie Test)
    pnl_va, w_va = pnl[va], w[va]
    best = None
    for thr in np.arange(0.30, 0.81, 0.01):
        sel = cal_va >= thr
        if sel.sum() < args.min_val_trades:
            continue
        total = float((pnl_va[sel] * w_va[sel]).sum())
        avg = float(np.average(pnl_va[sel], weights=w_va[sel]))
        if best is None or total > best["total_pnl"]:
            best = {"threshold": round(float(thr), 2), "total_pnl": round(total, 1),
                    "avg_pnl": round(avg, 3), "n": int(sel.sum())}
    if best is None:
        log("⚠️ Kein Threshold mit genug Val-Trades — Abbruch ohne Artefakt.")
        sys.exit(2)
    log(f"Val-Operating-Point: thr={best['threshold']} | n={best['n']} | "
        f"øPnL={best['avg_pnl']:+.3f}% | ΣPnL={best['total_pnl']:+.1f}%")

    # ── Abschlussreport auf TEST (einmalig) ──
    y_te, pnl_te, w_te = y[te], pnl[te], w[te]
    buckets = bucket_report(cal_te, y_te, pnl_te, w_te, "test, kalibriert")
    sel = cal_te >= best["threshold"]
    base_avg = float(np.average(pnl_te, weights=w_te))
    gate_avg = float(np.average(pnl_te[sel], weights=w_te[sel])) if sel.sum() else float("nan")
    gate_wr = float(np.average(y_te[sel], weights=w_te[sel])) if sel.sum() else float("nan")
    log(f"GATE-UPLIFT test: ohne Gate ø {base_avg:+.3f}%/Trade | "
        f"mit Gate (thr={best['threshold']}) ø {gate_avg:+.3f}%/Trade, WR {gate_wr:.3f}, "
        f"n={int(sel.sum())}/{len(y_te)} ({sel.sum() / max(len(y_te), 1):.1%} Pass-Rate)")

    te_meta = meta[te].copy()
    te_meta["cal"] = cal_te
    te_meta["pass"] = sel
    per_src = []
    for src, g in te_meta.groupby("source"):
        gw = w_te[te_meta.index.get_indexer(g.index)]
        per_src.append({
            "source": src, "n": len(g),
            "pass_rate": round(float(g["pass"].mean()), 3),
            "wr_all": round(float(np.average(g["label"].astype(int), weights=gw)), 3),
            "avg_pnl_all": round(float(np.average(
                pd.to_numeric(g["net_pnl_pct"]).fillna(0.0), weights=gw)), 3),
        })
    per_src.sort(key=lambda r: -r["n"])
    log("Per-Quelle (test): " + json.dumps(per_src[:12], ensure_ascii=False))

    # Monats-Robustheit der Gate-Auswahl auf Test
    monthly = []
    if sel.sum():
        g = te_meta[te_meta["pass"]]
        for m, mg in g.groupby(g["ts"].dt.to_period("M")):
            monthly.append({"month": str(m), "n": len(mg),
                            "wr": round(float(mg["label"].astype(int).mean()), 3),
                            "avg_pnl": round(float(pd.to_numeric(mg["net_pnl_pct"]).mean()), 3)})
        log("Monatlich (test, gated): " + json.dumps(monthly))

    vocab = sorted(c[4:] for c in feature_names if c.startswith("src_") and c != "src_is_ai")
    artifact = {
        "model": model, "features": feature_names, "threshold": best["threshold"],
        "calibrator": iso, "vocab_sources": vocab,
        "meta": {
            "name": "AIM2", "trained_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "events_file": args.events, "n_train": int(tr.sum()), "n_val": int(va.sum()),
            "n_test": int(te.sum()), "split_t1": str(t1), "split_t2": str(t2),
            "purge_days": PURGE_DAYS, "auc_val": round(float(auc_va), 4),
            "auc_test": round(float(auc_te), 4), "brier_test": round(float(brier_te), 4),
            "operating_point": best,
            "gate_uplift_test": {"base_avg_pnl": round(base_avg, 3),
                                 "gate_avg_pnl": round(gate_avg, 3) if sel.sum() else None,
                                 "gate_wr": round(gate_wr, 3) if sel.sum() else None,
                                 "n_pass": int(sel.sum()), "n_test": int(len(y_te))},
            "reliability_test": buckets, "per_source_test": per_src, "monthly_test": monthly,
        },
    }
    os.makedirs(STAGING_DIR, exist_ok=True)
    joblib.dump(artifact, args.out)
    with open(args.out.replace(".pkl", "_report.json"), "w", encoding="utf-8") as fh:
        json.dump(artifact["meta"], fh, ensure_ascii=False, indent=2)
    log(f"Artefakt → {args.out} (NUR staging — Deploy ist eine Operator-Entscheidung, P1.35)")


if __name__ == "__main__":
    main()

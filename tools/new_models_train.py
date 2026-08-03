"""
tools/new_models_train.py — Training of research models PEX1/FMR1/TRM1/FIF1
(Report 15: S6/S8/S10/S11). Runs on the VPS (step 2), AFTER the respective
dataset builder (tools/<strat>_build_dataset.py).

Methodology (batch-E framework, identical to tools/aim2_train.py):
  * chronological 70/15/15 split with 7-day purge gap (P1.29)
  * XGBoost (binary; TRM1: multi:softprob with 3 classes), early stopping on val
  * Isotonic calibration on validation (display only — gate runs raw)
  * Threshold selection per replay net PnL on validation; test untouched until
    final report
  * Artefact ONLY to staging_models (P1.35). Deploy to repo root (filename
    <strat>_model.pkl, which bots 30–33 load) is an operator decision.

Artefact contract (core/model_artifacts.load_artifact):
  dict(model, features, optimal_threshold, calibrator_isotonic, meta)

Examples:
  python tools/new_models_train.py --strategy pex1
  python tools/new_models_train.py --strategy trm1 --min-val-trades 20
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
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.research_features import (  # noqa: E402
    FIF1_FEATURES,
    FMR1_FEATURES,
    FMR2_FEATURES,
    PEX1_FEATURES,
    TRM1_CLASS_DOWN,
    TRM1_CLASS_UP,
    TRM1_FEATURES,
)

STAGING_DIR = os.getenv("KYTHERA_STAGING_DIR", r"C:\Users\Michael\Documents\_X\staging_models")
REPLAY_DIR = os.getenv("KYTHERA_REPLAY_DIR", os.path.join(STAGING_DIR, "replay"))

# purge_days >= replay horizon of the respective builder, else PnL
# windows of threshold selection overlap the split boundary (review fix 2026-07-06):
# pex1/fmr1/fif1 simulate 7 days, trm1 (BTC-smart-targets) 14 days, fmr2
# (normalisation/timeout exit) at most 9 settlements = 3 days.
STRATEGIES = {
    "pex1": {"model_id": "PEX1", "features": PEX1_FEATURES, "kind": "binary", "purge_days": 7},
    "fmr1": {"model_id": "FMR1", "features": FMR1_FEATURES, "kind": "binary", "purge_days": 7},
    "fmr2": {"model_id": "FMR2", "features": FMR2_FEATURES, "kind": "binary", "purge_days": 3},
    "trm1": {"model_id": "TRM1", "features": TRM1_FEATURES, "kind": "multiclass", "purge_days": 14},
    "fif1": {"model_id": "FIF1", "features": FIF1_FEATURES, "kind": "binary", "purge_days": 7},
}


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_events(path: str, feature_cols: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    metas, feats = [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            feats.append(rec.pop("features"))
            metas.append(rec)
    meta = pd.DataFrame(metas)
    X = pd.DataFrame(feats)
    missing = [c for c in feature_cols if c not in X.columns]
    if missing:
        raise SystemExit(f"Events don't carry all contract features — missing: {missing}")
    X = X[feature_cols].astype(float).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    # ISO8601 instead of format inference: builders write mixed precision
    # (FMR1 settlements second-precise without .%f, spike events with microseconds) —
    # pandas else guesses format from first row and breaks on second.
    meta["ts"] = pd.to_datetime(meta["ts"], format="ISO8601")
    return meta, X


def chrono_split(meta: pd.DataFrame, purge_days: int):
    ts = meta["ts"]
    t1 = ts.quantile(0.70)
    t2 = ts.quantile(0.85)
    purge = pd.Timedelta(days=purge_days)
    train = (ts <= t1 - purge).to_numpy()
    val = ((ts > t1) & (ts <= t2 - purge)).to_numpy()
    test = (ts > t2).to_numpy()
    return train, val, test, t1, t2


def make_model(kind: str) -> xgb.XGBClassifier:
    common = dict(
        n_estimators=600, learning_rate=0.05, max_depth=5, min_child_weight=5,
        subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0,
        tree_method="hist", early_stopping_rounds=50, n_jobs=4, random_state=42,
    )
    if kind == "multiclass":
        return xgb.XGBClassifier(objective="multi:softprob", num_class=3,
                                 eval_metric="mlogloss", **common)
    return xgb.XGBClassifier(eval_metric="auc", **common)


def pick_threshold(cal_or_raw: np.ndarray, pnl: np.ndarray, w: np.ndarray,
                   min_trades: int) -> dict | None:
    best = None
    for thr in np.arange(0.30, 0.81, 0.01):
        sel = cal_or_raw >= thr
        if sel.sum() < min_trades:
            continue
        total = float((pnl[sel] * w[sel]).sum())
        avg = float(np.average(pnl[sel], weights=w[sel]))
        if best is None or total > best["total_pnl"]:
            best = {"threshold": round(float(thr), 2), "total_pnl": round(total, 1),
                    "avg_pnl": round(avg, 3), "n": int(sel.sum())}
    return best


def train_binary(cfg: dict, meta: pd.DataFrame, X: pd.DataFrame, args) -> dict:
    labeled = meta["label"].isin([0, 1]).to_numpy()
    meta, X = meta[labeled].reset_index(drop=True), X[labeled].reset_index(drop=True)
    y = meta["label"].astype(int).to_numpy()
    pnl = pd.to_numeric(meta["net_pnl_pct"], errors="coerce").fillna(0.0).to_numpy()
    w = pd.to_numeric(meta["weight"], errors="coerce").fillna(1.0).to_numpy()
    log(f"Labelled: {len(meta)} | WR (weighted): {np.average(y, weights=w):.3f} "
        f"| ø replay-PnL: {np.average(pnl, weights=w):+.3f}%")

    tr, va, te, t1, t2 = chrono_split(meta, cfg["purge_days"])
    log(f"Split: train={tr.sum()} | val={va.sum()} | test={te.sum()} "
        f"(t1={t1.date()}, t2={t2.date()}, purge={cfg['purge_days']}d)")
    if min(tr.sum(), va.sum(), te.sum()) < args.min_val_trades:
        raise SystemExit("Too few events for a reliable split — abort.")

    model = make_model("binary")
    model.fit(X[tr], y[tr], sample_weight=w[tr],
              eval_set=[(X[va], y[va])], sample_weight_eval_set=[w[va]], verbose=False)
    log(f"Trained: best_iteration={model.best_iteration}")

    raw_va = model.predict_proba(X[va])[:, 1]
    raw_te = model.predict_proba(X[te])[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(raw_va, y[va], sample_weight=w[va])
    auc_va = roc_auc_score(y[va], raw_va, sample_weight=w[va])
    auc_te = roc_auc_score(y[te], raw_te, sample_weight=w[te])
    brier_te = brier_score_loss(y[te], iso.predict(raw_te), sample_weight=w[te])
    log(f"AUC val={auc_va:.4f} | AUC test={auc_te:.4f} | Brier test (calibrated)={brier_te:.4f}")

    # Threshold on RAW val probs (bots gate raw — convention MIS2/ABR1).
    best = pick_threshold(raw_va, pnl[va], w[va], args.min_val_trades)
    if best is None:
        log("⚠️ No threshold with enough val trades — abort without artefact.")
        sys.exit(2)
    log(f"Val operating point: thr={best['threshold']} | n={best['n']} | "
        f"øPnL={best['avg_pnl']:+.3f}% | ΣPnL={best['total_pnl']:+.1f}%")

    sel = raw_te >= best["threshold"]
    base_avg = float(np.average(pnl[te], weights=w[te]))
    gate_avg = float(np.average(pnl[te][sel], weights=w[te][sel])) if sel.sum() else float("nan")
    gate_wr = float(np.average(y[te][sel], weights=w[te][sel])) if sel.sum() else float("nan")
    log(f"GATE-UPLIFT test: without gate ø {base_avg:+.3f}%/trade | with gate "
        f"ø {gate_avg:+.3f}%/trade, WR {gate_wr:.3f}, n={int(sel.sum())}/{int(te.sum())}")

    return {
        "model": model, "calibrator": iso, "threshold": best["threshold"],
        "meta_extra": {
            "auc_val": round(float(auc_va), 4), "auc_test": round(float(auc_te), 4),
            "brier_test": round(float(brier_te), 4), "operating_point": best,
            "gate_uplift_test": {
                "base_avg_pnl": round(base_avg, 3),
                "gate_avg_pnl": round(gate_avg, 3) if sel.sum() else None,
                "gate_wr": round(gate_wr, 3) if sel.sum() else None,
                "n_pass": int(sel.sum()), "n_test": int(te.sum()),
            },
            "n_train": int(tr.sum()), "n_val": int(va.sum()), "n_test_split": int(te.sum()),
            "split_t1": str(t1), "split_t2": str(t2),
        },
    }


def train_trm1(cfg: dict, meta: pd.DataFrame, X: pd.DataFrame, args) -> dict:
    """TRM1: 3-class model; gate = max(P(up), P(down)); threshold chosen
    via replay PnL of BTC trade in predicted direction."""
    # Events with open replay (win_* = None → mark-to-market PnL) are
    # completely filtered out — consistent with label filter of binary paths
    # (review fix 2026-07-06; before, open trades noised the
    # threshold selection).
    closed = (meta["win_long"].notna() & meta["win_short"].notna()).to_numpy()
    meta, X = meta[closed].reset_index(drop=True), X[closed].reset_index(drop=True)
    y = meta["label_class"].astype(int).to_numpy()
    pnl_long = pd.to_numeric(meta["pnl_long"], errors="coerce").fillna(0.0).to_numpy()
    pnl_short = pd.to_numeric(meta["pnl_short"], errors="coerce").fillna(0.0).to_numpy()
    w = pd.to_numeric(meta["weight"], errors="coerce").fillna(1.0).to_numpy()
    log(f"Events (replay closed): {len(meta)} | classes: other={int((y == 0).sum())} "
        f"up={int((y == TRM1_CLASS_UP).sum())} down={int((y == TRM1_CLASS_DOWN).sum())}")

    tr, va, te, t1, t2 = chrono_split(meta, cfg["purge_days"])
    log(f"Split: train={tr.sum()} | val={va.sum()} | test={te.sum()} "
        f"(t1={t1.date()}, t2={t2.date()}, purge={cfg['purge_days']}d)")
    if min(tr.sum(), va.sum(), te.sum()) < args.min_val_trades:
        raise SystemExit("Too few events for a reliable split — abort.")

    model = make_model("multiclass")
    model.fit(X[tr], y[tr], sample_weight=w[tr],
              eval_set=[(X[va], y[va])], sample_weight_eval_set=[w[va]], verbose=False)
    log(f"Trained: best_iteration={model.best_iteration}")

    def gate_and_pnl(proba: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        p_up, p_down = proba[:, TRM1_CLASS_UP], proba[:, TRM1_CLASS_DOWN]
        gate = np.maximum(p_up, p_down)
        chosen = np.where(p_up >= p_down, pnl_long[mask], pnl_short[mask])
        chosen_win = (chosen > 0).astype(int)
        return gate, chosen, chosen_win

    proba_va = model.predict_proba(X[va])
    proba_te = model.predict_proba(X[te])
    gate_va, pnl_va, win_va = gate_and_pnl(proba_va, va)
    gate_te, pnl_te, win_te = gate_and_pnl(proba_te, te)

    # Calibration: gate-prob → P(chosen trade wins) — display only.
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(gate_va, win_va, sample_weight=w[va])

    best = pick_threshold(gate_va, pnl_va, w[va], args.min_val_trades)
    if best is None:
        log("⚠️ No threshold with enough val trades — abort without artefact.")
        sys.exit(2)
    log(f"Val operating point: thr={best['threshold']} | n={best['n']} | "
        f"øPnL={best['avg_pnl']:+.3f}% | ΣPnL={best['total_pnl']:+.1f}%")

    sel = gate_te >= best["threshold"]
    base_avg = float(np.average(pnl_te, weights=w[te]))
    gate_avg = float(np.average(pnl_te[sel], weights=w[te][sel])) if sel.sum() else float("nan")
    acc = float(np.average((win_te[sel] == 1), weights=w[te][sel])) if sel.sum() else float("nan")
    log(f"GATE-UPLIFT test: all transitions ø {base_avg:+.3f}% | gated "
        f"ø {gate_avg:+.3f}%, trade-WR {acc:.3f}, n={int(sel.sum())}/{int(te.sum())}")

    return {
        "model": model, "calibrator": iso, "threshold": best["threshold"],
        "meta_extra": {
            "operating_point": best,
            "gate_uplift_test": {
                "base_avg_pnl": round(base_avg, 3),
                "gate_avg_pnl": round(gate_avg, 3) if sel.sum() else None,
                "gate_trade_wr": round(acc, 3) if sel.sum() else None,
                "n_pass": int(sel.sum()), "n_test": int(te.sum()),
            },
            "class_contract": {"other": 0, "up": TRM1_CLASS_UP, "down": TRM1_CLASS_DOWN},
            "n_train": int(tr.sum()), "n_val": int(va.sum()), "n_test_split": int(te.sum()),
            "split_t1": str(t1), "split_t2": str(t2),
        },
    }


def main() -> None:
    # cp1252 console: ø/Σ in output must not break the run
    # (same fix as tools/retrain_from_replay.py, 13ce748).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True, choices=sorted(STRATEGIES))
    ap.add_argument("--events", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--min-val-trades", type=int, default=50)
    args = ap.parse_args()

    cfg = STRATEGIES[args.strategy]
    events_path = args.events or os.path.join(REPLAY_DIR, f"{args.strategy}_events.jsonl")
    out_path = args.out or os.path.join(STAGING_DIR, f"{args.strategy}_model.pkl")

    meta, X = load_events(events_path, cfg["features"])
    log(f"Events geladen: {len(meta)} | Features: {X.shape[1]} ({cfg['model_id']})")

    if cfg["kind"] == "multiclass":
        result = train_trm1(cfg, meta, X, args)
    else:
        result = train_binary(cfg, meta, X, args)

    artifact = {
        "model": result["model"],
        "features": list(X.columns),
        "optimal_threshold": float(result["threshold"]),
        "calibrator_isotonic": result["calibrator"],
        "meta": {
            "model_id": cfg["model_id"],
            "kind": cfg["kind"],
            "trained_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "events_file": events_path,
            "purge_days": cfg["purge_days"],
            **result["meta_extra"],
        },
    }
    os.makedirs(STAGING_DIR, exist_ok=True)
    joblib.dump(artifact, out_path)
    with open(out_path.replace(".pkl", "_report.json"), "w", encoding="utf-8") as fh:
        json.dump(artifact["meta"], fh, ensure_ascii=False, indent=2)
    log(f"Artefact → {out_path} (staging ONLY — deploy to repo root as "
        f"{args.strategy}_model.pkl is an operator decision, P1.35)")


if __name__ == "__main__":
    main()

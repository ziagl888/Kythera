"""
tools/retrain_from_replay.py — Retraining auf Walk-Forward-Replay-Labels (Batch E3).

Konsumiert die JSONL-Trades aus tools/walkforward_sim.py (Label = First-Touch-
Outcome der TATSÄCHLICH geposteten Order-Geometrie — X-R1-Fix) und trainiert
die Nachfolger-Modelle:

  td / bb   — binäres XGB wie smc_ml_trainer (20 Features), ein Modell je TF
  abr1      — binäres XGB je Richtung (18 Features wie 18_ai_abr1_bot)

Methodik (Report-13-Gerüst):
  * chronologischer 70/15/15-Split mit Purge-Gap (P1.29)
  * Threshold auf dem Validation-Slice per realem Replay-PnL (net_pnl_pct
    aus dem Simulator, nicht 2R-Formel)
  * Isotonic-Kalibrierung auf Validation (im Artefakt als Zusatz-Key —
    die Live-Bots lesen weiterhin model/features/threshold)
  * Kalibrierungs-Report alt vs. neu (Confidence-Buckets vs. Replay-Outcome)
  * Artefakte NUR nach staging_models (P1.35-Regel), mit meta

Beispiele:
  python tools/retrain_from_replay.py --strategy td --tf 4h
  python tools/retrain_from_replay.py --strategy abr1
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.isotonic import IsotonicRegression

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

STAGING_DIR = os.getenv("KYTHERA_STAGING_DIR", r"C:\Users\Michael\Documents\_X\staging_models")
REPLAY_DIR = os.getenv("KYTHERA_REPLAY_DIR", os.path.join(STAGING_DIR, "replay"))
LIVE_DIR = r"C:\Users\Michael\PycharmProjects\crypto_trading_bot_v2"

SNIPER_FEATURES = [
    "dir_num", "atr_14_pct",
    "rsi_14", "tsi_25_13_13", "macd_dif_normal_12_26_9", "macd_dea_normal_12_26_9",
    "ema_9_dist_pct", "ema_21_dist_pct", "ema_50_dist_pct", "ema_200_dist_pct",
    "kama_21_dist_pct", "wma_21_dist_pct",
    "donchian_upper_20_dist_pct", "donchian_lower_20_dist_pct", "donchian_mid_20_dist_pct",
    "boll_upper_20_dist_pct", "boll_lower_20_dist_pct",
    "trend_UP", "trend_DOWN", "trend_SIDEWAYS",
]

ABR1_FEATURES = [
    "dist_close_ema9_pct", "dist_ema9_ema21_pct", "dist_close_kama9_pct",
    "rsi14", "rsi_below_30", "rsi_above_70",
    "tsi", "tsi_signal", "tsi_above_0", "tsi_below_0",
    "dist_close_boll_upper_pct", "dist_close_boll_mid_pct", "dist_close_boll_lower_pct",
    "dist_close_donchian_upper_pct", "dist_close_donchian_mid_pct", "dist_close_donchian_lower_pct",
    "retest_volume", "retest_volume_ratio_avg",
]


def load_replay(path: str) -> pd.DataFrame:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            t = json.loads(line)
            if t.get("outcome_tp1") is None:
                continue  # bei Datenende noch offene Trades: kein Label
            row = dict(t.pop("features", {}))
            row.update({
                "symbol": t["symbol"], "direction": t["direction"],
                "signal_time": pd.Timestamp(t["signal_time"]),
                "outcome": int(t["outcome_tp1"]),
                "net_pnl_pct": float(t.get("net_pnl_pct", 0.0)),
                "r_multiple": t.get("r_multiple"),
            })
            rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["signal_time"] = pd.to_datetime(df["signal_time"], utc=True).dt.tz_localize(None)
        df = df.sort_values("signal_time").reset_index(drop=True)
    return df


def chrono_split(df: pd.DataFrame, gap_hours: int):
    t_train = df["signal_time"].quantile(0.70)
    t_val = df["signal_time"].quantile(0.85)
    gap = pd.Timedelta(hours=gap_hours)
    train = df[df["signal_time"] <= t_train]
    val = df[(df["signal_time"] > t_train + gap) & (df["signal_time"] <= t_val)]
    test = df[df["signal_time"] > t_val + gap]
    return train, val, test


def bucket_calibration(probs: np.ndarray, outcomes: np.ndarray, pnl: np.ndarray) -> list[dict]:
    edges = [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.01]
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (probs >= lo) & (probs < hi)
        n = int(m.sum())
        out.append({
            "bucket": f"{lo:.1f}-{hi:.1f}".replace("1.0", "1.0"),
            "n": n,
            "tp1_rate": round(float(outcomes[m].mean()) * 100, 1) if n else None,
            "avg_net_pnl_pct": round(float(pnl[m].mean()), 2) if n else None,
        })
    return out


def pick_threshold(val_df: pd.DataFrame, probs: np.ndarray) -> tuple[float, dict]:
    """Threshold per realem Replay-PnL auf Validation (P1.29 + X-R2-Fix)."""
    best_thresh, best_pnl, best = 0.5, -np.inf, {}
    for thresh in np.arange(0.30, 0.85, 0.05):
        m = probs >= thresh
        if m.sum() < 10:
            continue
        pnl = float(val_df.loc[m, "net_pnl_pct"].sum())
        if pnl > best_pnl:
            best_pnl, best_thresh = pnl, float(thresh)
            best = {"n": int(m.sum()), "sum_net_pnl_pct": round(pnl, 2),
                    "wr": round(float(val_df.loc[m, "outcome"].mean()) * 100, 1)}
    return best_thresh, best


def train_binary(train, val, test, feature_cols, hyper=None):
    hyper = hyper or dict(n_estimators=300, max_depth=5, learning_rate=0.03,
                          subsample=0.8, colsample_bytree=0.8, random_state=42,
                          eval_metric="logloss")
    model = xgb.XGBClassifier(**hyper)
    model.fit(train[feature_cols].fillna(0), train["outcome"].astype(int))

    p_val = model.predict_proba(val[feature_cols].fillna(0))[:, 1]
    p_test = model.predict_proba(test[feature_cols].fillna(0))[:, 1]

    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(p_val, val["outcome"].astype(int))

    thresh, val_stats = pick_threshold(val, p_val)

    m = p_test >= thresh
    test_stats = {
        "n_taken": int(m.sum()),
        "wr": round(float(test.loc[m, "outcome"].mean()) * 100, 1) if m.sum() else None,
        "sum_net_pnl_pct": round(float(test.loc[m, "net_pnl_pct"].sum()), 2) if m.sum() else None,
        "base_rate_test": round(float(test["outcome"].mean()) * 100, 1),
        "n_test_total": int(len(test)),
    }
    calib_new = bucket_calibration(p_test, test["outcome"].values.astype(float),
                                   test["net_pnl_pct"].values.astype(float))
    return model, iso, thresh, val_stats, test_stats, calib_new


def old_model_calibration(strategy, tf, df, direction=None):
    """Kalibrierung des PRODUKTIONS-Modells auf denselben Replay-Events."""
    try:
        if strategy in ("td", "bb"):
            data = joblib.load(os.path.join(LIVE_DIR, f"{strategy}_xgboost_model_{tf}.pkl"))
            model, feats = data["model"], data["features"]
            X = df.reindex(columns=feats, fill_value=0).fillna(0)
            probs = model.predict_proba(X)[:, 1]
        else:  # abr1: natives 3-Klassen-JSON, success = Klasse 0
            model = xgb.XGBClassifier()
            model.load_model(os.path.join(LIVE_DIR, f"bt2_model_{direction}.json"))
            X = df.reindex(columns=ABR1_FEATURES, fill_value=0).fillna(0)
            probs = model.predict_proba(X)[:, 0]
        return probs, bucket_calibration(np.asarray(probs), df["outcome"].values.astype(float),
                                         df["net_pnl_pct"].values.astype(float))
    except Exception as e:
        print(f"  (Alt-Modell-Kalibrierung fehlgeschlagen: {e})")
        return None, None


def save_artifact(path, model, feature_cols, thresh, iso, meta):
    os.makedirs(STAGING_DIR, exist_ok=True)
    if os.path.abspath(os.path.dirname(path)) != os.path.abspath(STAGING_DIR):
        raise SystemExit(f"Refuse: Artefakt-Ziel liegt nicht in STAGING_DIR: {path}")
    joblib.dump({
        "model": model, "features": feature_cols, "optimal_threshold": thresh,
        "calibrator_isotonic": iso, "meta": meta,
    }, path)
    print(f"  💾 {path}")


def run_td_bb(strategy: str, tf: str, replay_path: str) -> dict:
    df = load_replay(replay_path)
    if df.empty or len(df) < 300:
        raise SystemExit(f"Zu wenig Replay-Trades ({len(df)}) in {replay_path}")
    gap_hours = 100 * (1 if tf == "1h" else 4)
    train, val, test = chrono_split(df, gap_hours)
    print(f"{strategy}_{tf}: {len(df)} gelabelte Events | split {len(train)}/{len(val)}/{len(test)} | "
          f"Basisrate TP1 {df['outcome'].mean()*100:.1f}%")

    model, iso, thresh, val_stats, test_stats, calib_new = train_binary(train, val, test, SNIPER_FEATURES)
    _, calib_old = old_model_calibration(strategy, tf, test)

    meta = {
        "trainer": "tools/retrain_from_replay.py", "strategy": strategy, "tf": tf,
        "label_source": os.path.basename(replay_path),
        "label": "first-touch TP1-vor-SL der geposteten smart-targets-Geometrie, Fees inkl.",
        "split": "chronological 70/15/15 + purge gap", "threshold_selected_on": "validation",
        "xgboost_version": xgb.__version__,
        "n_train": len(train), "n_val": len(val), "n_test": len(test),
        "val_stats": val_stats, "test_stats": test_stats,
    }
    save_artifact(os.path.join(STAGING_DIR, f"{strategy}_xgboost_model_{tf}.pkl"),
                  model, SNIPER_FEATURES, thresh, iso, meta)
    return {"strategy": strategy, "tf": tf, "n_events": len(df),
            "base_rate": round(df["outcome"].mean() * 100, 1),
            "threshold": thresh, "val_stats": val_stats, "test_stats": test_stats,
            "calibration_new_test": calib_new, "calibration_old_same_events": calib_old,
            "feature_importance_top": top_importance(model, SNIPER_FEATURES)}


def run_abr1(replay_path: str) -> dict:
    df = load_replay(replay_path)
    if df.empty or len(df) < 300:
        raise SystemExit(f"Zu wenig Replay-Trades ({len(df)}) in {replay_path}")
    results = {}
    for direction in ("LONG", "SHORT"):
        d = df[df["direction"] == direction].reset_index(drop=True)
        if len(d) < 200:
            print(f"ABR1 {direction}: nur {len(d)} Events — übersprungen")
            continue
        train, val, test = chrono_split(d, 100)
        print(f"abr1 {direction}: {len(d)} Events | split {len(train)}/{len(val)}/{len(test)} | "
              f"Basisrate {d['outcome'].mean()*100:.1f}%")
        model, iso, thresh, val_stats, test_stats, calib_new = train_binary(train, val, test, ABR1_FEATURES)
        _, calib_old = old_model_calibration("abr1", None, test, direction=direction)

        # natives XGB-JSON wie das Produktions-Format + meta-Sidecar
        os.makedirs(STAGING_DIR, exist_ok=True)
        out_json = os.path.join(STAGING_DIR, f"bt2_model_{direction}.json")
        model.save_model(out_json)
        meta = {
            "trainer": "tools/retrain_from_replay.py", "strategy": "abr1", "direction": direction,
            "label_source": os.path.basename(replay_path),
            "label": "first-touch TP1-vor-SL der geposteten smart-targets-Geometrie, Fees inkl.",
            "model_type": "binary (1=TP1-first-touch) — ANDERS als das alte 3-Klassen-Modell!",
            "success_proba": "predict_proba[:, 1]",
            "optimal_threshold": thresh, "split": "chronological 70/15/15 + purge gap",
            "xgboost_version": xgb.__version__,
            "n_train": len(train), "n_val": len(val), "n_test": len(test),
            "val_stats": val_stats, "test_stats": test_stats,
        }
        with open(out_json.replace(".json", "_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        print(f"  💾 {out_json}")
        results[direction] = {"n_events": len(d), "base_rate": round(d["outcome"].mean() * 100, 1),
                              "threshold": thresh, "val_stats": val_stats, "test_stats": test_stats,
                              "calibration_new_test": calib_new, "calibration_old_same_events": calib_old,
                              "feature_importance_top": top_importance(model, ABR1_FEATURES)}
    return {"strategy": "abr1", **results}


def top_importance(model, feature_cols, k=8):
    imp = model.feature_importances_
    order = np.argsort(imp)[::-1][:k]
    return [{"feature": feature_cols[i], "importance": round(float(imp[i]), 4)} for i in order]


def main():
    # cp1252-Konsole: Emojis in Ausgaben dürfen den Lauf nicht abbrechen.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True, choices=["td", "bb", "abr1"])
    ap.add_argument("--tf", default="4h", choices=["1h", "4h"])
    ap.add_argument("--replay", default=None)
    ap.add_argument("--days", type=int, default=540)
    args = ap.parse_args()

    if args.replay is None:
        tag = f"{args.strategy}_{args.tf}" if args.strategy in ("td", "bb") else args.strategy
        days = args.days if args.strategy in ("td", "bb") else 365
        args.replay = os.path.join(REPLAY_DIR, f"{tag}_replay_{days}d.jsonl")

    if args.strategy in ("td", "bb"):
        result = run_td_bb(args.strategy, args.tf, args.replay)
        name = f"{args.strategy}_{args.tf}"
    else:
        result = run_abr1(args.replay)
        name = "abr1"

    out = os.path.join(STAGING_DIR, f"retrain_{name}_stats.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    print(f"\nStats: {out}")


if __name__ == "__main__":
    main()

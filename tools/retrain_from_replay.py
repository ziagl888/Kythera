"""
tools/retrain_from_replay.py — Retraining auf Walk-Forward-Replay-Labels (Batch E3).

Konsumiert die JSONL-Trades aus tools/walkforward_sim.py (Label = First-Touch-
Outcome der TATSÄCHLICH geposteten Order-Geometrie — X-R1-Fix) und trainiert
die Nachfolger-Modelle:

  td / bb   — binäres XGB wie smc_ml_trainer (20 Features), ein Modell je TF
  abr1      — binäres XGB je Richtung (18 Features wie 18_ai_abr1_bot)
  mis1      — 8 binäre XGB ({8,24,72,168}h × {pump,dump}) auf den 63 bereinigten
              Features aus core.mis_features (Leakage-Fix, Report 13); Label =
              TP1-vor-SL INNERHALB des Horizonts (horizontgekappter Replay)

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

from core.funding_features import FUNDING_FEATURES  # noqa: E402
from core.mis_features import FEATURE_COLS as MIS1_FEATURES  # noqa: E402
from core.mis_features import assert_features_alive  # noqa: E402
from core.rub_features import RUB_FEATURES  # noqa: E402

MIS1_HORIZONS = (8, 24, 72, 168)  # muss zu tools/walkforward_sim.MIS1_HORIZONS passen

# RUB2-Vertrag (MODEL_INTENT §8): die 9 geteilten Bot-Features (core/rub_features,
# MACD fix auf normal_12_26_9 = Live-Parität, Semantikbruch behoben) + die 6
# Funding-Features (core/funding_features) aus dem Replay-Adapter.
RUB2_FEATURES = list(RUB_FEATURES) + list(FUNDING_FEATURES)

# EPD2 (MODEL_INTENT §7): die 10 Live-Features von Bot 10 (Schlüsselnamen wie
# im Builder tools/epd2_build_dataset.py geschrieben) + die 6 Funding-Features.
EPD2_FEATURES = [
    "vol_ratio",
    "p_chg_60s",
    "buy_pres",
    "volat",
    "sample_fill",
    "rsi",
    "tsi",
    "macd",
    "e9_dist",
    "e21_dist",
] + list(FUNDING_FEATURES)

# Operator-Konzept (2026-07-06): Move-Label = "±X% Bewegung INNERHALB des
# Horizonts" (Close-Basis), Schwelle wächst mit dem Horizont. Quelle:
# tools/mis1_move_labels.py über den Preisreihen der Replay-Samples.
MOVE_THRESH_PCT = {8: 5.0, 24: 10.0, 72: 15.0, 168: 25.0}

STAGING_DIR = os.getenv("KYTHERA_STAGING_DIR", r"C:\Users\Michael\Documents\_X\staging_models")
REPLAY_DIR = os.getenv("KYTHERA_REPLAY_DIR", os.path.join(STAGING_DIR, "replay"))
LIVE_DIR = r"C:\Users\Michael\PycharmProjects\crypto_trading_bot_v2"

SNIPER_FEATURES = [
    "dir_num",
    "atr_14_pct",
    "rsi_14",
    "tsi_25_13_13",
    "macd_dif_normal_12_26_9",
    "macd_dea_normal_12_26_9",
    "ema_9_dist_pct",
    "ema_21_dist_pct",
    "ema_50_dist_pct",
    "ema_200_dist_pct",
    "kama_21_dist_pct",
    "wma_21_dist_pct",
    "donchian_upper_20_dist_pct",
    "donchian_lower_20_dist_pct",
    "donchian_mid_20_dist_pct",
    "boll_upper_20_dist_pct",
    "boll_lower_20_dist_pct",
    "trend_UP",
    "trend_DOWN",
    "trend_SIDEWAYS",
]

# Feature-Vertrag des ALTEN 3-Klassen-Produktionsmodells (nur noch für den
# Alt-vs-Neu-Kalibrierungsvergleich — das alte Modell kennt exakt diese 18).
ABR1_FEATURES_LEGACY = [
    "dist_close_ema9_pct",
    "dist_ema9_ema21_pct",
    "dist_close_kama9_pct",
    "rsi14",
    "rsi_below_30",
    "rsi_above_70",
    "tsi",
    "tsi_signal",
    "tsi_above_0",
    "tsi_below_0",
    "dist_close_boll_upper_pct",
    "dist_close_boll_mid_pct",
    "dist_close_boll_lower_pct",
    "dist_close_donchian_upper_pct",
    "dist_close_donchian_mid_pct",
    "dist_close_donchian_lower_pct",
    "retest_volume",
    "retest_volume_ratio_avg",
]

# Neuer Vertrag: 18 Indikator-Features + Setup-Geometrie aus dem Detektor-
# Rework (find_break_retest_setups in 18_ai_abr1_bot — der Simulator schreibt
# sie ins Replay-Feature-Dict). Vorher war das Break&Retest-Setup selbst für
# das Modell unsichtbar.
ABR1_FEATURES = ABR1_FEATURES_LEGACY + [
    "setup_dist_close_level_pct",
    "setup_break_strength_pct",
    "setup_candles_since_break",
    "setup_level_age_candles",
    "setup_retest_wick_pct",
]


def load_replay(path: str, ts_key: str = "signal_time", label_key: str = "outcome_tp1") -> pd.DataFrame:
    """JSONL-Event-Loader. ts_key/label_key parametrisieren die Builder-Dialekte
    (Kerzen-Replays: signal_time/outcome_tp1; EPD2-Detektor-Events: ts/label) —
    EIN Loader, damit Fixes wie die utc=True-Mixed-Offset-Lehre (f95f092) nicht
    je Kopie nachgezogen werden müssen."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            t = json.loads(line)
            if t.get(label_key) is None:
                continue  # bei Datenende noch offene Trades: kein Label
            row = dict(t.pop("features", None) or {})
            row.update(
                {
                    "symbol": t["symbol"],
                    "direction": t["direction"],
                    # Roh-String lassen — das vektorisierte to_datetime unten parst
                    # die ganze Spalte einmal (statt pd.Timestamp je Zeile doppelt).
                    "signal_time": t[ts_key],
                    "outcome": int(t[label_key]),
                    "net_pnl_pct": float(t.get("net_pnl_pct") or 0.0),
                    "r_multiple": t.get("r_multiple"),
                }
            )
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
        out.append(
            {
                "bucket": f"{lo:.1f}-{hi:.1f}".replace("1.0", "1.0"),
                "n": n,
                "tp1_rate": round(float(outcomes[m].mean()) * 100, 1) if n else None,
                "avg_net_pnl_pct": round(float(pnl[m].mean()), 2) if n else None,
            }
        )
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
            best = {
                "n": int(m.sum()),
                "sum_net_pnl_pct": round(pnl, 2),
                "wr": round(float(val_df.loc[m, "outcome"].mean()) * 100, 1),
            }
    return best_thresh, best


def pick_threshold_safe(val_df: pd.DataFrame, probs: np.ndarray, min_n: int = 200):
    """Operator-Kriterium (2026-07-06): möglichst wenige, dafür sichere Trades.

    Statt Summen-PnL (belohnt Volumen → degeneriert in bullischen Val-Slices
    zum Take-almost-all) wird der Ø-Netto-PnL PRO Trade maximiert. Kandidaten
    sind Prob-Quantile (funktioniert bei jeder Basisrate), Mindest-Stichprobe
    min_n auf Validation, bei Gleichstand gewinnt der höhere Threshold.
    Liefert threshold=None, wenn kein Kandidat min_n erreicht ODER der beste
    Ø-PnL <= 0 ist — das Modell gilt dann als NICHT deploybar."""
    quantiles = (0.50, 0.70, 0.80, 0.85, 0.90, 0.925, 0.95, 0.97, 0.98, 0.99)
    cands = sorted({round(float(np.quantile(probs, q)), 4) for q in quantiles})
    curve, best = [], None
    for thresh in cands:
        m = probs >= thresh
        n = int(m.sum())
        if n < min_n:
            continue
        point = {
            "threshold": thresh,
            "n": n,
            "avg_net_pnl_pct": round(float(val_df.loc[m, "net_pnl_pct"].mean()), 3),
            "sum_net_pnl_pct": round(float(val_df.loc[m, "net_pnl_pct"].sum()), 2),
            "wr": round(float(val_df.loc[m, "outcome"].mean()) * 100, 1),
        }
        curve.append(point)
        if best is None or point["avg_net_pnl_pct"] >= best["avg_net_pnl_pct"]:
            best = point
    if best is None or best["avg_net_pnl_pct"] <= 0:
        return None, {"deployable": False, "best": best, "curve": curve}
    return best["threshold"], {"deployable": True, **best, "curve": curve}


def train_binary(train, val, test, feature_cols, hyper=None, picker=pick_threshold):
    hyper = hyper or dict(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        eval_metric="logloss",
    )
    model = xgb.XGBClassifier(**hyper)
    model.fit(train[feature_cols].fillna(0), train["outcome"].astype(int))

    p_val = model.predict_proba(val[feature_cols].fillna(0))[:, 1]
    p_test = model.predict_proba(test[feature_cols].fillna(0))[:, 1]

    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(p_val, val["outcome"].astype(int))

    thresh, val_stats = picker(val, p_val)

    m = p_test >= thresh if thresh is not None else np.zeros(len(p_test), dtype=bool)
    test_stats = {
        "n_taken": int(m.sum()),
        "wr": round(float(test.loc[m, "outcome"].mean()) * 100, 1) if m.sum() else None,
        "sum_net_pnl_pct": round(float(test.loc[m, "net_pnl_pct"].sum()), 2) if m.sum() else None,
        "base_rate_test": round(float(test["outcome"].mean()) * 100, 1),
        "n_test_total": int(len(test)),
    }
    calib_new = bucket_calibration(
        p_test, test["outcome"].values.astype(float), test["net_pnl_pct"].values.astype(float)
    )
    return model, iso, thresh, val_stats, test_stats, calib_new


def old_model_calibration(strategy, tf, df, direction=None, horizon=None):
    """Kalibrierung des PRODUKTIONS-Modells auf denselben Replay-Events."""
    try:
        if strategy == "mis1":
            # Legacy-67-Feature-pkl (inkl. der Unfall-Features — die stehen im
            # Replay als legacy_features-Spalten bereit, s. core.mis_features).
            key = f"{horizon}h_{'pump' if direction == 'LONG' else 'dump'}"
            path = os.path.join(LIVE_DIR, f"pump_model_{key}_final.pkl")
            if not os.path.exists(path):
                path = os.path.join(REPO_ROOT, f"pump_model_{key}_final.pkl")
            model = joblib.load(path)
            feats = list(model.feature_names_in_)
            X = df.reindex(columns=feats, fill_value=0).fillna(0)
            probs = model.predict_proba(X)[:, 1]
        elif strategy in ("td", "bb"):
            data = joblib.load(os.path.join(LIVE_DIR, f"{strategy}_xgboost_model_{tf}.pkl"))
            model, feats = data["model"], data["features"]
            X = df.reindex(columns=feats, fill_value=0).fillna(0)
            probs = model.predict_proba(X)[:, 1]
        else:  # abr1: natives 3-Klassen-JSON, success = Klasse 0
            model = xgb.XGBClassifier()
            model.load_model(os.path.join(LIVE_DIR, f"bt2_model_{direction}.json"))
            X = df.reindex(columns=ABR1_FEATURES_LEGACY, fill_value=0).fillna(0)
            probs = model.predict_proba(X)[:, 0]
        return probs, bucket_calibration(
            np.asarray(probs), df["outcome"].values.astype(float), df["net_pnl_pct"].values.astype(float)
        )
    except Exception as e:
        print(f"  (Alt-Modell-Kalibrierung fehlgeschlagen: {e})")
        return None, None


def save_artifact(path, model, feature_cols, thresh, iso, meta):
    os.makedirs(STAGING_DIR, exist_ok=True)
    if os.path.abspath(os.path.dirname(path)) != os.path.abspath(STAGING_DIR):
        raise SystemExit(f"Refuse: Artefakt-Ziel liegt nicht in STAGING_DIR: {path}")
    joblib.dump(
        {
            "model": model,
            "features": feature_cols,
            "optimal_threshold": thresh,
            "calibrator_isotonic": iso,
            "meta": meta,
        },
        path,
    )
    print(f"  💾 {path}")


def run_td_bb(strategy: str, tf: str, replay_path: str) -> dict:
    df = load_replay(replay_path)
    if df.empty or len(df) < 300:
        raise SystemExit(f"Zu wenig Replay-Trades ({len(df)}) in {replay_path}")
    gap_hours = 100 * (1 if tf == "1h" else 4)
    train, val, test = chrono_split(df, gap_hours)
    print(
        f"{strategy}_{tf}: {len(df)} gelabelte Events | split {len(train)}/{len(val)}/{len(test)} | "
        f"Basisrate TP1 {df['outcome'].mean() * 100:.1f}%"
    )

    model, iso, thresh, val_stats, test_stats, calib_new = train_binary(train, val, test, SNIPER_FEATURES)
    _, calib_old = old_model_calibration(strategy, tf, test)

    meta = {
        "trainer": "tools/retrain_from_replay.py",
        "strategy": strategy,
        "tf": tf,
        # Versionierungs-Regel (Operator 2026-07-06): Retrain-Generation postet
        # unter neuem Modell-Tag, damit Alt/Neu in den Trackern getrennt sind.
        "model_id": f"{strategy.upper()}2_{tf.upper()}",
        "label_source": os.path.basename(replay_path),
        "label": "first-touch TP1-vor-SL der geposteten smart-targets-Geometrie, Fees inkl.",
        "split": "chronological 70/15/15 + purge gap",
        "threshold_selected_on": "validation",
        "xgboost_version": xgb.__version__,
        "n_train": len(train),
        "n_val": len(val),
        "n_test": len(test),
        "val_stats": val_stats,
        "test_stats": test_stats,
    }
    save_artifact(
        os.path.join(STAGING_DIR, f"{strategy}_xgboost_model_{tf}.pkl"), model, SNIPER_FEATURES, thresh, iso, meta
    )
    return {
        "strategy": strategy,
        "tf": tf,
        "n_events": len(df),
        "base_rate": round(df["outcome"].mean() * 100, 1),
        "threshold": thresh,
        "val_stats": val_stats,
        "test_stats": test_stats,
        "calibration_new_test": calib_new,
        "calibration_old_same_events": calib_old,
        "feature_importance_top": top_importance(model, SNIPER_FEATURES),
    }


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
        print(
            f"abr1 {direction}: {len(d)} Events | split {len(train)}/{len(val)}/{len(test)} | "
            f"Basisrate {d['outcome'].mean() * 100:.1f}%"
        )
        model, iso, thresh, val_stats, test_stats, calib_new = train_binary(train, val, test, ABR1_FEATURES)
        _, calib_old = old_model_calibration("abr1", None, test, direction=direction)

        # natives XGB-JSON wie das Produktions-Format + meta-Sidecar.
        # "features" gehört IN die meta (Artefakt-Governance, Report 13) — der
        # Bot lädt den Vertrag von dort statt ihn zu hardcoden (R13-ABR1-5).
        os.makedirs(STAGING_DIR, exist_ok=True)
        out_json = os.path.join(STAGING_DIR, f"bt2_model_{direction}.json")
        model.save_model(out_json)
        meta = {
            "trainer": "tools/retrain_from_replay.py",
            "strategy": "abr1",
            "direction": direction,
            "model_id": "ABR2",  # Versionierungs-Regel Operator 2026-07-06
            "label_source": os.path.basename(replay_path),
            "label": "first-touch TP1-vor-SL der geposteten smart-targets-Geometrie, Fees inkl.",
            "model_type": "binary (1=TP1-first-touch) — ANDERS als das alte 3-Klassen-Modell!",
            "success_proba": "predict_proba[:, 1]",
            "features": ABR1_FEATURES,
            "optimal_threshold": thresh,
            "split": "chronological 70/15/15 + purge gap",
            "xgboost_version": xgb.__version__,
            "n_train": len(train),
            "n_val": len(val),
            "n_test": len(test),
            "val_stats": val_stats,
            "test_stats": test_stats,
        }
        with open(out_json.replace(".json", "_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        # Isotonic-Kalibrator persistieren (war vorher NUR bei td/bb im pkl —
        # für abr1 ging er verloren). Der Bot nutzt ihn für die angezeigte
        # Confidence; das Gate läuft weiter auf der Roh-Probability.
        joblib.dump(iso, out_json.replace(".json", "_calib.pkl"))
        print(f"  💾 {out_json}")
        results[direction] = {
            "n_events": len(d),
            "base_rate": round(d["outcome"].mean() * 100, 1),
            "threshold": thresh,
            "val_stats": val_stats,
            "test_stats": test_stats,
            "calibration_new_test": calib_new,
            "calibration_old_same_events": calib_old,
            "feature_importance_top": top_importance(model, ABR1_FEATURES),
        }
    return {"strategy": "abr1", **results}


def load_mis1_replay(path: str) -> pd.DataFrame:
    """MIS1-JSONL: Features + legacy_features flach, beide Horizont-Labels.
    Zeilen ohne Label für einen Horizont (Datenende) werden erst je Horizont
    verworfen — deshalb hier KEIN globaler outcome_tp1-Filter."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            t = json.loads(line)
            row = dict(t.pop("features", {}))
            row.update(t.pop("legacy_features", {}))
            row.update(
                {
                    "symbol": t["symbol"],
                    "direction": t["direction"],
                    "signal_time": pd.Timestamp(t["signal_time"]),
                }
            )
            for h in MIS1_HORIZONS:
                row[f"outcome_{h}h"] = t.get(f"outcome_{h}h")
                row[f"net_pnl_{h}h"] = t.get(f"net_pnl_{h}h")
            rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["signal_time"] = pd.to_datetime(df["signal_time"], utc=True).dt.tz_localize(None)
        df = df.sort_values("signal_time").reset_index(drop=True)
    return df


def load_mis1_move_labels(path: str) -> pd.DataFrame:
    """JSONL aus tools/mis1_move_labels.py: kontinuierliche Move-Extreme je
    (symbol, signal_time) — Label-Schwellen werden hier im Trainer angelegt."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rows.append(json.loads(line))
    df = pd.DataFrame(rows)
    df["signal_time"] = pd.to_datetime(df["signal_time"], utc=True).dt.tz_localize(None)
    return df


def run_mis1(
    replay_path: str,
    stride_hours: int = 24,
    label_mode: str = "geometry",
    move_path: str | None = None,
    move_basis: str = "close",
) -> dict:
    df_all = load_mis1_replay(replay_path)
    if df_all.empty or len(df_all) < 2000:
        raise SystemExit(f"Zu wenig Replay-Samples ({len(df_all)}) in {replay_path}")
    print(
        f"mis1: {len(df_all)} Samples, {df_all['symbol'].nunique()} Coins, "
        f"{df_all['signal_time'].min()} → {df_all['signal_time'].max()}"
    )

    if label_mode == "move":
        move_path = move_path or os.path.join(os.path.dirname(replay_path), "mis1_move_labels.jsonl")
        mv = load_mis1_move_labels(move_path)
        df_all = df_all.merge(mv, on=["symbol", "signal_time"], how="left")
        n_matched = df_all[f"full_{MIS1_HORIZONS[0]}h"].notna().sum()
        print(f"mis1 move-labels: {len(mv)} Punkte geladen, {n_matched}/{len(df_all)} Samples gematcht")

    # P0.12-Assertion auf dem Trainingsmaterial: kein kontinuierliches Feature konstant.
    assert_features_alive(df_all, context=" (mis1-Retrain)")

    results: dict = {"strategy": "mis1"}
    for horizon in MIS1_HORIZONS:
        for direction in ("LONG", "SHORT"):
            key = f"{horizon}h_{'pump' if direction == 'LONG' else 'dump'}"
            if label_mode == "move":
                thr_move = MOVE_THRESH_PCT[horizon]
                col = (
                    f"runup_{move_basis}_pct_{horizon}h"
                    if direction == "LONG"
                    else f"drawdown_{move_basis}_pct_{horizon}h"
                )
                sub = df_all[df_all["direction"] == direction].copy()
                ext = pd.to_numeric(sub[col], errors="coerce")
                hit = (ext >= thr_move) if direction == "LONG" else (ext <= -thr_move)
                full = sub[f"full_{horizon}h"].fillna(False).astype(bool)
                # Ein Treffer zählt immer; eine 0 nur bei vollem Horizontfenster
                # (Datenende vor Horizontende ist kein verlässliches "kein Move").
                sub["outcome"] = np.where(hit, 1.0, np.where(full, 0.0, np.nan))
                sub.loc[ext.isna(), "outcome"] = np.nan
                d = sub[sub["outcome"].notna()].copy()
                d["outcome"] = d["outcome"].astype(int)
            else:
                d = df_all[(df_all["direction"] == direction) & df_all[f"outcome_{horizon}h"].notna()].copy()
                d["outcome"] = d[f"outcome_{horizon}h"].astype(int)
            # Ökonomische Bewertung bleibt in beiden Modi die gepostete
            # Trade-Geometrie (das verdient/verliert ein Follower real).
            d["net_pnl_pct"] = pd.to_numeric(d[f"net_pnl_{horizon}h"], errors="coerce").fillna(0.0)
            d = d.reset_index(drop=True)
            if len(d) < 2000:
                print(f"mis1 {key}: nur {len(d)} Events — übersprungen")
                continue

            # Purge-Gap = Horizont + Stride: kein Label-Fenster aus dem Train-
            # Slice ragt in Val/Test hinein (Zwillings-Leakage, 13-Addendum-P0).
            train, val, test = chrono_split(d, horizon + stride_hours)
            print(
                f"mis1 {key}: {len(d)} Events | split {len(train)}/{len(val)}/{len(test)} | "
                f"Basisrate TP1@{horizon}h {d['outcome'].mean() * 100:.1f}%"
            )

            model, iso, thresh, val_stats, test_stats, calib_new = train_binary(
                train, val, test, MIS1_FEATURES, picker=pick_threshold_safe
            )
            _, calib_old = old_model_calibration("mis1", None, test, direction=direction, horizon=horizon)

            if label_mode == "move":
                label_txt = (
                    f"{move_basis.capitalize()}-Move {'+' if direction == 'LONG' else '-'}"
                    f"{MOVE_THRESH_PCT[horizon]}% INNERHALB {horizon}h "
                    f"(Operator-Konzept; Quelle tools/mis1_move_labels.py)"
                )
            else:
                label_txt = (
                    f"first-touch TP1-vor-SL der geposteten smart-targets-Geometrie "
                    f"INNERHALB {horizon}h, Fees inkl. (Timeout=0)"
                )
            if label_mode == "move":
                prefix = "mis1_move_model" if move_basis == "close" else "mis1_move_wick_model"
            else:
                prefix = "mis1_model"
            meta = {
                "trainer": "tools/retrain_from_replay.py",
                "strategy": "mis1",
                "model_id": "MIS2",  # Bot hängt den Horizont an: MIS2-8H etc.
                "label_mode": label_mode,
                "horizon_hours": horizon,
                "direction": direction,
                "label_source": os.path.basename(replay_path),
                "label": label_txt,
                "features": "core.mis_features.FEATURE_COLS (63, skalenfrei — Leakage-Fix Report 13)",
                "split": f"chronological 70/15/15 + purge gap {horizon + stride_hours}h",
                "threshold_selected_on": "validation (Ø-Netto-PnL/Trade, min_n=200 — pick_threshold_safe)",
                "xgboost_version": xgb.__version__,
                "n_train": len(train),
                "n_val": len(val),
                "n_test": len(test),
                "val_stats": val_stats,
                "test_stats": test_stats,
            }
            save_artifact(os.path.join(STAGING_DIR, f"{prefix}_{key}.pkl"), model, MIS1_FEATURES, thresh, iso, meta)
            with open(os.path.join(STAGING_DIR, f"{prefix}_{key}_meta.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2, default=str)
            results[key] = {
                "n_events": len(d),
                "base_rate": round(d["outcome"].mean() * 100, 1),
                "threshold": thresh,
                "val_stats": val_stats,
                "test_stats": test_stats,
                "calibration_new_test": calib_new,
                "calibration_old_same_events": calib_old,
                "feature_importance_top": top_importance(model, MIS1_FEATURES),
            }
    return results


def run_rub(replay_path: str) -> dict:
    """RUB2-Retrain (Task #2): Binärmodell je Richtung auf den Replay-Events des
    geteilten Vorfilters (core/rub_features), Label = First-Touch der eigenen
    HVN/S-R-Geometrie inkl. SL-Pfad (behebt das Max-Favorable-Label des alten
    BT3-Trainers), chronologischer Split (behebt die Episoden-Memorization des
    Random-Splits), Threshold via pick_threshold_safe."""
    df = load_replay(replay_path)
    if df.empty or len(df) < 600:
        raise SystemExit(f"Zu wenig Replay-Events ({len(df)}) in {replay_path}")
    print(
        f"rub: {len(df)} Events, {df['symbol'].nunique()} Coins, {df['signal_time'].min()} → {df['signal_time'].max()}"
    )

    results: dict = {"strategy": "rub2", "features": RUB2_FEATURES}
    for direction in ("LONG", "SHORT"):
        d = df[df["direction"] == direction].reset_index(drop=True)
        if len(d) < 300:
            print(f"rub2 {direction}: nur {len(d)} Events — übersprungen")
            continue
        # Purge-Gap 7 Tage: Reversion-Trades können lange laufen, und die
        # Extrem-Episoden clustern — großzügig gegen Zwillings-Leakage.
        train, val, test = chrono_split(d, gap_hours=7 * 24)
        print(
            f"rub2 {direction}: {len(d)} Events | split {len(train)}/{len(val)}/{len(test)} | "
            f"Basisrate TP1 {d['outcome'].mean() * 100:.1f}%"
        )

        model, iso, thresh, val_stats, test_stats, calib_new = train_binary(
            train, val, test, RUB2_FEATURES, picker=pick_threshold_safe
        )

        meta = {
            "trainer": "tools/retrain_from_replay.py",
            "strategy": "rub2",
            "model_id": "RUB2",
            "direction": direction,
            "model_type": "binary (1=TP1-first-touch)",
            "success_proba": "predict_proba[:, 1]",
            "features": RUB2_FEATURES,
            "optimal_threshold": thresh,
            "label_source": os.path.basename(replay_path),
            "label": "first-touch TP1-vor-SL der HVN/S-R-Geometrie (Bot-13-Parität), Fees inkl.",
            "changes_vs_rub1": "MACD auf normal_12_26_9 fixiert (Live-Parität), Label mit "
            "SL-Pfad statt Max-Favorable-72h, chronologischer Split mit "
            "7d-Purge statt Random-Split, +6 Funding-Features",
            "split": "chronological 70/15/15 + 7d purge gap",
            "xgboost_version": xgb.__version__,
            "n_train": len(train),
            "n_val": len(val),
            "n_test": len(test),
            "val_stats": val_stats,
            "test_stats": test_stats,
        }
        save_artifact(os.path.join(STAGING_DIR, f"rub2_model_{direction}.pkl"), model, RUB2_FEATURES, thresh, iso, meta)
        with open(os.path.join(STAGING_DIR, f"rub2_model_{direction}_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=str)
        results[direction] = {
            "n_events": len(d),
            "base_rate": round(d["outcome"].mean() * 100, 1),
            "threshold": thresh,
            "val_stats": val_stats,
            "test_stats": test_stats,
            "calibration_new_test": calib_new,
            "feature_importance_top": top_importance(model, RUB2_FEATURES),
        }
    return results


def run_epd(events_path: str) -> dict:
    """EPD2-Retrain (MODEL_INTENT §7): Binärmodell je Richtung auf den
    Detektor-Events aus tools/epd2_build_dataset.py (nur vol_ratio≥5 wie live,
    Label = First-Touch TP1-vor-SL der Bot-10-HVN/SR-Geometrie via
    simulate_exit, 7d-Horizont; offene Trades ungelabelt). Der Builder
    schreibt ts/label/features statt signal_time/outcome_tp1 → Key-Mapping
    im geteilten Loader."""
    df = load_replay(events_path, ts_key="ts", label_key="label")
    if df.empty or len(df) < 600:
        raise SystemExit(f"Zu wenig gelabelte EPD2-Events ({len(df)}) in {events_path}")
    print(
        f"epd: {len(df)} Events, {df['symbol'].nunique()} Coins, {df['signal_time'].min()} → {df['signal_time'].max()}"
    )

    results: dict = {"strategy": "epd2", "features": EPD2_FEATURES}
    for direction in ("LONG", "SHORT"):
        d = df[df["direction"] == direction].reset_index(drop=True)
        if len(d) < 300:
            print(f"epd2 {direction}: nur {len(d)} Events — übersprungen")
            continue
        # Purge-Gap 7 Tage = Label-Horizont des Builders (HORIZON_CANDLES).
        train, val, test = chrono_split(d, gap_hours=7 * 24)
        print(
            f"epd2 {direction}: {len(d)} Events | split {len(train)}/{len(val)}/{len(test)} | "
            f"Basisrate TP1 {d['outcome'].mean() * 100:.1f}%"
        )
        if min(len(train), len(val), len(test)) < 50:
            # Zeitraum zu kurz für den Purge-Gap (z. B. abgeschnittener Builder-
            # Lauf): iso.fit/Picker würden auf leeren Slices crashen.
            print(f"epd2 {direction}: degenerierter Split — übersprungen")
            continue

        model, iso, thresh, val_stats, test_stats, calib_new = train_binary(
            train, val, test, EPD2_FEATURES, picker=pick_threshold_safe
        )

        meta = {
            "trainer": "tools/retrain_from_replay.py",
            "strategy": "epd2",
            "model_id": "EPD2",
            "direction": direction,
            "model_type": "binary (1=TP1-first-touch)",
            "success_proba": "predict_proba[:, 1]",
            "features": EPD2_FEATURES,
            "optimal_threshold": thresh,
            "label_source": os.path.basename(events_path),
            "label": "first-touch TP1-vor-SL der Bot-10-HVN/SR-Geometrie (simulate_exit, 7d), Fees inkl.",
            "changes_vs_epd1": "nur vol_ratio>=5-Events (Training==Serving statt OOD), Label = "
            "gepostete Geometrie statt Fix-Bracket, chronologischer Split mit "
            "7d-Purge statt Random-Split, +6 Funding-Features (Operator 2026-07-06)",
            "split": "chronological 70/15/15 + 7d purge gap",
            "xgboost_version": xgb.__version__,
            "n_train": len(train),
            "n_val": len(val),
            "n_test": len(test),
            "val_stats": val_stats,
            "test_stats": test_stats,
        }
        save_artifact(os.path.join(STAGING_DIR, f"epd2_model_{direction}.pkl"), model, EPD2_FEATURES, thresh, iso, meta)
        with open(os.path.join(STAGING_DIR, f"epd2_model_{direction}_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=str)
        results[direction] = {
            "n_events": len(d),
            "base_rate": round(d["outcome"].mean() * 100, 1),
            "threshold": thresh,
            "val_stats": val_stats,
            "test_stats": test_stats,
            "calibration_new_test": calib_new,
            "feature_importance_top": top_importance(model, EPD2_FEATURES),
        }
    return results


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
    ap.add_argument("--strategy", required=True, choices=["td", "bb", "abr1", "mis1", "rub", "epd"])
    ap.add_argument("--tf", default="4h", choices=["1h", "4h"])
    ap.add_argument("--replay", default=None)
    ap.add_argument("--days", type=int, default=540)
    ap.add_argument(
        "--stride", type=int, default=24, help="mis1: Sampling-Stride des Replays (geht in den Purge-Gap ein)"
    )
    ap.add_argument(
        "--label-mode",
        default="geometry",
        choices=["geometry", "move"],
        help="mis1: geometry = TP1-vor-SL der Smart-Targets; "
        "move = ±X%%-Bewegung innerhalb des Horizonts (Operator-Konzept)",
    )
    ap.add_argument(
        "--move-labels",
        default=None,
        help="mis1 move: JSONL aus tools/mis1_move_labels.py (Default: mis1_move_labels.jsonl neben dem Replay)",
    )
    ap.add_argument(
        "--move-basis",
        default="close",
        choices=["close", "wick"],
        help="mis1 move: Schlusskurs- oder Docht-Extreme als Label-Basis "
        "(Operator 2026-07-06: beide Varianten trainieren und vergleichen)",
    )
    args = ap.parse_args()

    if args.replay is None:
        if args.strategy == "epd":
            # EPD2 nutzt die Detektor-Events des Builders, kein Kerzen-Replay.
            args.replay = os.path.join(REPLAY_DIR, "epd2_events.jsonl")
        else:
            tag = f"{args.strategy}_{args.tf}" if args.strategy in ("td", "bb") else args.strategy
            days = args.days if args.strategy in ("td", "bb", "mis1") else 365
            args.replay = os.path.join(REPLAY_DIR, f"{tag}_replay_{days}d.jsonl")

    if args.strategy in ("rub", "epd"):
        # Ein Dispatch statt Zwillings-Ternaries — die nächste Event-Strategie
        # ergänzt genau einen Eintrag (Runner + Artefakt-Name zusammen).
        runner, name = {"rub": (run_rub, "rub2"), "epd": (run_epd, "epd2")}[args.strategy]
        result = runner(args.replay)
        out = os.path.join(STAGING_DIR, f"retrain_{name}_stats.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)
        print(f"\nStats: {out}")
        return

    if args.strategy in ("td", "bb"):
        result = run_td_bb(args.strategy, args.tf, args.replay)
        name = f"{args.strategy}_{args.tf}"
    elif args.strategy == "mis1":
        result = run_mis1(
            args.replay,
            stride_hours=args.stride,
            label_mode=args.label_mode,
            move_path=args.move_labels,
            move_basis=args.move_basis,
        )
        if args.label_mode == "move":
            name = "mis1_move" if args.move_basis == "close" else "mis1_move_wick"
        else:
            name = "mis1"
    else:
        result = run_abr1(args.replay)
        name = "abr1"

    out = os.path.join(STAGING_DIR, f"retrain_{name}_stats.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    print(f"\nStats: {out}")


if __name__ == "__main__":
    main()

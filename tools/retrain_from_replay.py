"""
tools/retrain_from_replay.py — Retraining on walk-forward replay labels (Batch E3).

Consumes JSONL trades from tools/walkforward_sim.py (label = first-touch
outcome of ACTUALLY posted order geometry — X-R1 fix) and trains the
successor models:

  td / bb   — binary XGB like smc_ml_trainer (20 features), one model per TF
  abr1      — binary XGB per direction (18 features like 18_ai_abr1_bot)
  mis1      — 8 binary XGB ({8,24,72,168}h × {pump,dump}) on the 63 cleaned
              features from core.mis_features (leakage fix, Report 13); label =
              TP1-before-SL WITHIN the horizon (horizon-capped replay)

Methodology (Report-13 structure):
  * chronological 70/15/15 split with purge gap (P1.29)
  * threshold on validation slice per actual replay PnL (net_pnl_pct
    from simulator, not 2R formula)
  * isotonic calibration on validation (in artifact as extra key —
    live bots still read model/features/threshold)
  * calibration report old vs. new (confidence buckets vs. replay outcome)
  * artifacts ONLY to staging_models (P1.35 rule), with meta

Examples:
  python tools/retrain_from_replay.py --strategy td --tf 4h
  python tools/retrain_from_replay.py --strategy abr1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from functools import partial

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.isotonic import IsotonicRegression

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.ats_features import ATS_FEATURES  # noqa: E402
from core.funding_features import FUNDING_FEATURES  # noqa: E402
from core.moment_features import MOMENT_FEATURES  # noqa: E402
from core.mis_features import FEATURE_COLS as MIS1_FEATURES  # noqa: E402
from core.mis_features import assert_features_alive  # noqa: E402
from core.rub_features import RUB_FEATURES  # noqa: E402
from core.staging_guard import assert_no_foreign_overwrite  # noqa: E402
from core.atb2_features import ATB2_FEATURES  # noqa: E402
from core.atb2_features import assert_features_alive as assert_atb2_alive  # noqa: E402

MIS1_HORIZONS = (8, 24, 72, 168)  # must match tools/walkforward_sim.MIS1_HORIZONS

# RUB2 contract (MODEL_INTENT §8): the 9 shared bot features (core/rub_features,
# MACD fixed to normal_12_26_9 = live parity, semantic break fixed) + the 6
# funding features (core/funding_features) from the replay adapter.
RUB2_FEATURES = list(RUB_FEATURES) + list(FUNDING_FEATURES)

# EPD2 (MODEL_INTENT §7): the 10 live features from bot 10 (key names as
# written in builder tools/epd2_build_dataset.py) + the 6 funding features.
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

# ATS2 (Bot 12 TSI Sniper): the 29-feature contract of core.ats_features (ONE
# source with the bot + the walkforward_sim adapter). NO funding features —
# bot 12 does not read any (unlike RUB2/EPD2).
ATS2_FEATURES = list(ATS_FEATURES)

# Operator concept (2026-07-06): move label = "±X% movement WITHIN the
# horizon" (close basis), threshold grows with the horizon. Source:
# tools/mis1_move_labels.py over the price series of replay samples.
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

# Feature contract of the OLD 3-class production model (only for
# old-vs-new calibration comparison — the old model knows exactly these 18).
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

# New contract: 18 indicator features + setup geometry from the detector
# rework (find_break_retest_setups in 18_ai_abr1_bot — the simulator writes
# them to the replay feature dict). Previously the break & retest setup itself was
# invisible to the model.
ABR1_FEATURES = ABR1_FEATURES_LEGACY + [
    "setup_dist_close_level_pct",
    "setup_break_strength_pct",
    "setup_candles_since_break",
    "setup_level_age_candles",
    "setup_retest_wick_pct",
]


# --- Optional additive feature block attachment (§K7 MOM, T-2026-CU-9050-141) ---
#
# DEFAULT-OFF. Model is the built-in funding block (RUB2_FEATURES =
# RUB_FEATURES + FUNDING_FEATURES above): there, shared feature names are
# simply appended to a strategy's contract. The moment block does the same,
# but BEHIND the --features flag rather than hard-wired — only when "moments"
# is passed does ``with_extra_features`` append the core.moment_features block.
#
# Strictly additive: without --features moments, ``extra_features`` is empty, and
# ``with_extra_features(BASE, [])`` yields an element-identical copy of BASE.
# The retrain behaviour (selected columns, artifact, meta) is then byte-identical
# to before — the attachment is a pure no-op as long as the flag is missing.
#
# Appending the names triggers NO retrain and fills no values: the replay writer
# (tools/walkforward_sim) must deliver the moment columns first before a --features
# moments run makes sense. Building this writer + the retrain run itself are
# reserved for the queue (§K7, one-job rule) — here ONLY the attachment is built.
FEATURE_HOOKS: dict[str, list[str]] = {"moments": list(MOMENT_FEATURES)}


def resolve_extra_features(names) -> list[str]:
    """Resolves --features selection to concrete extra feature names.

    ``None``/empty → ``[]`` (default-OFF path, no behaviour change)."""
    extra: list[str] = []
    for n in names or ():
        extra.extend(FEATURE_HOOKS[n])
    return extra


def with_extra_features(base, extra_features) -> list[str]:
    """``list(base)`` plus optional extra features. With empty ``extra_features``
    element-identical to ``base`` (byte-identical retrain behaviour)."""
    return list(base) + list(extra_features)


def load_replay(path: str, ts_key: str = "signal_time", label_key: str = "outcome_tp1") -> pd.DataFrame:
    """JSONL event loader. ts_key/label_key parametrise the builder dialects
    (candle replays: signal_time/outcome_tp1; EPD2 detector events: ts/label) —
    ONE loader so that fixes like the utc=True mixed-offset lesson (f95f092) don't
    have to be chased across copies."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            t = json.loads(line)
            if t.get(label_key) is None:
                continue  # at data end still open trades: no label
            feats = t.pop("features", None)
            pnl = t.get("net_pnl_pct")
            if feats is None or pnl is None:
                # FAIL loudly rather than train silently with 0 (Review PR #10):
                # null features/null PnL are writer bugs — as 0.0 rows they would
                # dilute the validation economics on which
                # pick_threshold_safe selects the LIVE gate threshold.
                missing = "features" if feats is None else "net_pnl_pct"
                raise ValueError(
                    f"Replay row without {missing} in {path} "
                    f"({t.get('symbol')}, {t.get(ts_key)}) — check replay writer."
                )
            row = dict(feats)
            row.update(
                {
                    "symbol": t["symbol"],
                    "direction": t["direction"],
                    # Keep as raw string — the vectorised to_datetime below parses
                    # the entire column once (rather than pd.Timestamp per row twice).
                    "signal_time": t[ts_key],
                    "outcome": int(t[label_key]),
                    "net_pnl_pct": float(pnl),
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


def split_shortfall(df: pd.DataFrame, gap_days: int, min_rows: int = 50, band: float = 0.15) -> dict:
    """Why ``chrono_split`` yields empty slices — and how much calendar is missing.

    ``chrono_split`` returns val and test as each the ``band`` quantile band of
    signal times; the purge gap cuts ``gap_days`` days from the front. If
    the band is shorter than the gap, both slices are EMPTY — regardless of how
    many rows the dataset has. The remedy is therefore never "more coins",
    but always "more calendar".

    Calculation (assuming uniform density ``rows_per_day`` — with strongly
    varying event rate, ``required_span_days`` is only an estimate):
        (band · span − gap_days) · rows_per_day ≥ min_rows
    """
    span_days = (df["signal_time"].max() - df["signal_time"].min()).total_seconds() / 86400
    rows_per_day = len(df) / span_days if span_days > 0 else 0.0
    required = (gap_days + (min_rows / rows_per_day if rows_per_day > 0 else float("inf"))) / band
    return {
        "span_days": round(span_days, 1),
        "band_days": round(band * span_days, 1),
        "gap_days": gap_days,
        "min_rows": min_rows,
        "rows_per_day": round(rows_per_day, 1),
        "required_span_days": round(required, 1),
        "missing_days": round(max(0.0, required - span_days), 1),
    }


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
    """Threshold per actual replay PnL on validation (P1.29 + X-R2 fix)."""
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
    """Operator criterion (2026-07-06): few but safe trades.

    Instead of sum PnL (rewards volume → degenerates to take-almost-all in bullish val
    slices), average net PnL per trade is maximised. Candidates are
    probability quantiles (work at any base rate), minimum sample size
    min_n on validation, with ties broken by higher threshold.
    Returns threshold=None if no candidate reaches min_n OR the best
    average PnL <= 0 — the model is then considered NOT deployable."""
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
    """Calibration of the PRODUCTION model on the same replay events."""
    try:
        if strategy == "mis1":
            # Legacy 67-feature pickle (incl. the accident features — they stand
            # in replay as legacy_features columns, see core.mis_features).
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
        else:  # abr1: native 3-class JSON, success = class 0
            model = xgb.XGBClassifier()
            model.load_model(os.path.join(LIVE_DIR, f"bt2_model_{direction}.json"))
            X = df.reindex(columns=ABR1_FEATURES_LEGACY, fill_value=0).fillna(0)
            probs = model.predict_proba(X)[:, 0]
        return probs, bucket_calibration(
            np.asarray(probs), df["outcome"].values.astype(float), df["net_pnl_pct"].values.astype(float)
        )
    except Exception as e:
        print(f"  (Old model calibration failed: {e})")
        return None, None


def save_artifact(path, model, feature_cols, thresh, iso, meta):
    os.makedirs(STAGING_DIR, exist_ok=True)
    if os.path.abspath(os.path.dirname(path)) != os.path.abspath(STAGING_DIR):
        raise SystemExit(f"Refuse: artifact destination not in STAGING_DIR: {path}")
    # td/bb share the filename with smc_ml_trainer.py — a legacy run on
    # 2026-07-14 silently overwrote four ready replay artifacts (T-2026-KYT-9050-006).
    assert_no_foreign_overwrite(path, meta.get("trainer", "tools/retrain_from_replay.py"))
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


def run_td_bb(strategy: str, tf: str, replay_path: str, extra_features=()) -> dict:
    df = load_replay(replay_path)
    if df.empty or len(df) < 300:
        raise SystemExit(f"Too few replay trades ({len(df)}) in {replay_path}")
    feats = with_extra_features(SNIPER_FEATURES, extra_features)
    gap_hours = 100 * (1 if tf == "1h" else 4)
    train, val, test = chrono_split(df, gap_hours)
    print(
        f"{strategy}_{tf}: {len(df)} labelled events | split {len(train)}/{len(val)}/{len(test)} | "
        f"base rate TP1 {df['outcome'].mean() * 100:.1f}%"
    )

    model, iso, thresh, val_stats, test_stats, calib_new = train_binary(train, val, test, feats)
    _, calib_old = old_model_calibration(strategy, tf, test)

    meta = {
        "trainer": "tools/retrain_from_replay.py",
        "strategy": strategy,
        "tf": tf,
        # Versioning rule (operator 2026-07-06): retrain generation posts
        # under new model tag so old/new are separated in trackers.
        "model_id": f"{strategy.upper()}2_{tf.upper()}",
        "label_source": os.path.basename(replay_path),
        "label": "first-touch TP1-before-SL of posted smart-targets geometry, fees incl.",
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
        os.path.join(STAGING_DIR, f"{strategy}_xgboost_model_{tf}.pkl"), model, feats, thresh, iso, meta
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
        "feature_importance_top": top_importance(model, feats),
    }


def run_abr1(replay_path: str, extra_features=()) -> dict:
    df = load_replay(replay_path)
    if df.empty or len(df) < 300:
        raise SystemExit(f"Too few replay trades ({len(df)}) in {replay_path}")
    feats = with_extra_features(ABR1_FEATURES, extra_features)
    results = {}
    for direction in ("LONG", "SHORT"):
        d = df[df["direction"] == direction].reset_index(drop=True)
        if len(d) < 200:
            print(f"ABR1 {direction}: only {len(d)} events — skipped")
            continue
        train, val, test = chrono_split(d, 100)
        print(
            f"abr1 {direction}: {len(d)} events | split {len(train)}/{len(val)}/{len(test)} | "
            f"base rate {d['outcome'].mean() * 100:.1f}%"
        )
        model, iso, thresh, val_stats, test_stats, calib_new = train_binary(train, val, test, feats)
        _, calib_old = old_model_calibration("abr1", None, test, direction=direction)

        # native XGB JSON like the production format + meta sidecar.
        # "features" belongs IN the meta (artifact governance, Report 13) — the
        # bot loads the contract from there rather than hard-coding it (R13-ABR1-5).
        os.makedirs(STAGING_DIR, exist_ok=True)
        out_json = os.path.join(STAGING_DIR, f"bt2_model_{direction}.json")
        model.save_model(out_json)
        meta = {
            "trainer": "tools/retrain_from_replay.py",
            "strategy": "abr1",
            "direction": direction,
            "model_id": "ABR2",  # versioning rule operator 2026-07-06
            "label_source": os.path.basename(replay_path),
            "label": "first-touch TP1-before-SL of posted smart-targets geometry, fees incl.",
            "model_type": "binary (1=TP1-first-touch) — DIFFERENT from the old 3-class model!",
            "success_proba": "predict_proba[:, 1]",
            "features": feats,
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
        # Persist isotonic calibrator (was previously ONLY in pkl for td/bb —
        # for abr1 it was lost). The bot uses it for displayed
        # confidence; the gate runs on raw probability.
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
            "feature_importance_top": top_importance(model, feats),
        }
    return {"strategy": "abr1", **results}


def load_mis1_replay(path: str) -> pd.DataFrame:
    """MIS1 JSONL: features + legacy_features flat, both horizon labels.
    Rows without label for a horizon (data end) are discarded per-horizon first
    — therefore NO global outcome_tp1 filter here."""
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
    """JSONL from tools/mis1_move_labels.py: continuous move extremes per
    (symbol, signal_time) — label thresholds are set here in the trainer."""
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
    extra_features=(),
) -> dict:
    df_all = load_mis1_replay(replay_path)
    if df_all.empty or len(df_all) < 2000:
        raise SystemExit(f"Too few replay samples ({len(df_all)}) in {replay_path}")
    print(
        f"mis1: {len(df_all)} samples, {df_all['symbol'].nunique()} coins, "
        f"{df_all['signal_time'].min()} → {df_all['signal_time'].max()}"
    )

    if label_mode == "move":
        move_path = move_path or os.path.join(os.path.dirname(replay_path), "mis1_move_labels.jsonl")
        mv = load_mis1_move_labels(move_path)
        df_all = df_all.merge(mv, on=["symbol", "signal_time"], how="left")
        n_matched = df_all[f"full_{MIS1_HORIZONS[0]}h"].notna().sum()
        print(f"mis1 move-labels: {len(mv)} points loaded, {n_matched}/{len(df_all)} samples matched")

    # P0.12 assertion on training material: no continuous feature is constant.
    assert_features_alive(df_all, context=" (mis1 retrain)")

    feats = with_extra_features(MIS1_FEATURES, extra_features)
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
                # A hit always counts; a 0 only with full horizon window
                # (data end before horizon end is not reliable "no move").
                sub["outcome"] = np.where(hit, 1.0, np.where(full, 0.0, np.nan))
                sub.loc[ext.isna(), "outcome"] = np.nan
                d = sub[sub["outcome"].notna()].copy()
                d["outcome"] = d["outcome"].astype(int)
            else:
                d = df_all[(df_all["direction"] == direction) & df_all[f"outcome_{horizon}h"].notna()].copy()
                d["outcome"] = d[f"outcome_{horizon}h"].astype(int)
            # Economic valuation stays the same in both modes: the posted
            # trade geometry (that's what a follower earns/loses in reality).
            d["net_pnl_pct"] = pd.to_numeric(d[f"net_pnl_{horizon}h"], errors="coerce").fillna(0.0)
            d = d.reset_index(drop=True)
            if len(d) < 2000:
                print(f"mis1 {key}: only {len(d)} events — skipped")
                continue

            # Purge gap = horizon + stride: no label window from train
            # slice extends into val/test (twin leakage, 13-addendum-P0).
            train, val, test = chrono_split(d, horizon + stride_hours)
            print(
                f"mis1 {key}: {len(d)} events | split {len(train)}/{len(val)}/{len(test)} | "
                f"base rate TP1@{horizon}h {d['outcome'].mean() * 100:.1f}%"
            )

            model, iso, thresh, val_stats, test_stats, calib_new = train_binary(
                train, val, test, feats, picker=pick_threshold_safe
            )
            _, calib_old = old_model_calibration("mis1", None, test, direction=direction, horizon=horizon)

            if label_mode == "move":
                label_txt = (
                    f"{move_basis.capitalize()} move {'+' if direction == 'LONG' else '-'}"
                    f"{MOVE_THRESH_PCT[horizon]}% WITHIN {horizon}h "
                    f"(operator concept; source tools/mis1_move_labels.py)"
                )
            else:
                label_txt = (
                    f"first-touch TP1-before-SL of posted smart-targets geometry "
                    f"WITHIN {horizon}h, fees incl. (timeout=0)"
                )
            if label_mode == "move":
                prefix = "mis1_move_model" if move_basis == "close" else "mis1_move_wick_model"
            else:
                prefix = "mis1_model"
            meta = {
                "trainer": "tools/retrain_from_replay.py",
                "strategy": "mis1",
                "model_id": "MIS2",  # bot appends the horizon: MIS2-8H etc.
                "label_mode": label_mode,
                "horizon_hours": horizon,
                "direction": direction,
                "label_source": os.path.basename(replay_path),
                "label": label_txt,
                "features": "core.mis_features.FEATURE_COLS (63, scale-free — leakage fix Report 13)",
                "split": f"chronological 70/15/15 + purge gap {horizon + stride_hours}h",
                "threshold_selected_on": "validation (avg net PnL/trade, min_n=200 — pick_threshold_safe)",
                "xgboost_version": xgb.__version__,
                "n_train": len(train),
                "n_val": len(val),
                "n_test": len(test),
                "val_stats": val_stats,
                "test_stats": test_stats,
            }
            save_artifact(os.path.join(STAGING_DIR, f"{prefix}_{key}.pkl"), model, feats, thresh, iso, meta)
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
                "feature_importance_top": top_importance(model, feats),
            }
    return results


def run_rub(replay_path: str, extra_features=()) -> dict:
    """RUB2 retrain (task #2): binary model per direction on replay events of the
    shared pre-filter (core/rub_features), label = first-touch of own
    HVN/S-R geometry incl. SL path (fixes max-favourable label of old
    BT3 trainer), chronological split (fixes episode memorization of
    random split), threshold via pick_threshold_safe."""
    df = load_replay(replay_path)
    if df.empty or len(df) < 600:
        raise SystemExit(f"Too few replay events ({len(df)}) in {replay_path}")
    print(
        f"rub: {len(df)} events, {df['symbol'].nunique()} coins, {df['signal_time'].min()} → {df['signal_time'].max()}"
    )

    feats = with_extra_features(RUB2_FEATURES, extra_features)
    results: dict = {"strategy": "rub2", "features": feats}
    for direction in ("LONG", "SHORT"):
        d = df[df["direction"] == direction].reset_index(drop=True)
        if len(d) < 300:
            print(f"rub2 {direction}: only {len(d)} events — skipped")
            continue
        # purge gap 7 days: reversion trades can run long, and extreme
        # episodes cluster — generous against twin leakage.
        train, val, test = chrono_split(d, gap_hours=7 * 24)
        print(
            f"rub2 {direction}: {len(d)} events | split {len(train)}/{len(val)}/{len(test)} | "
            f"base rate TP1 {d['outcome'].mean() * 100:.1f}%"
        )

        model, iso, thresh, val_stats, test_stats, calib_new = train_binary(
            train, val, test, feats, picker=pick_threshold_safe
        )

        meta = {
            "trainer": "tools/retrain_from_replay.py",
            "strategy": "rub2",
            "model_id": "RUB2",
            "direction": direction,
            "model_type": "binary (1=TP1-first-touch)",
            "success_proba": "predict_proba[:, 1]",
            "features": feats,
            "optimal_threshold": thresh,
            "label_source": os.path.basename(replay_path),
            "label": "first-touch TP1-before-SL of HVN/S-R geometry (bot-13 parity), fees incl.",
            "changes_vs_rub1": "MACD fixed to normal_12_26_9 (live parity), label with "
            "SL path instead of max-favourable-72h, chronological split with "
            "7d purge instead of random split, +6 funding features",
            "split": "chronological 70/15/15 + 7d purge gap",
            "xgboost_version": xgb.__version__,
            "n_train": len(train),
            "n_val": len(val),
            "n_test": len(test),
            "val_stats": val_stats,
            "test_stats": test_stats,
        }
        save_artifact(os.path.join(STAGING_DIR, f"rub2_model_{direction}.pkl"), model, feats, thresh, iso, meta)
        with open(os.path.join(STAGING_DIR, f"rub2_model_{direction}_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=str)
        results[direction] = {
            "n_events": len(d),
            "base_rate": round(d["outcome"].mean() * 100, 1),
            "threshold": thresh,
            "val_stats": val_stats,
            "test_stats": test_stats,
            "calibration_new_test": calib_new,
            "feature_importance_top": top_importance(model, feats),
        }
    return results


def run_ats(replay_path: str, extra_features=()) -> dict:
    """ATS2 retrain (bot 12 TSI sniper, T-2026-CU-9050-121): binary model per
    direction on replay events of the shared TSI crossover pre-filter
    (core/ats_features), label = first-touch TP1-before-SL of own HVN/S-R
    geometry incl. SL path + fees (fixes max-favourable-proxy label of old
    X8 TSI trainer), chronological split with purge gap (fixes episode
    memorization of random split), threshold via pick_threshold_safe.

    29-feature parity trainer==serving: the bot builds the feature vector with
    the same core.ats_features.build_ats_features (parity test
    backtest/test_ats_features)."""
    df = load_replay(replay_path)
    if df.empty or len(df) < 600:
        raise SystemExit(f"Too few replay events ({len(df)}) in {replay_path}")
    print(
        f"ats: {len(df)} events, {df['symbol'].nunique()} coins, {df['signal_time'].min()} → {df['signal_time'].max()}"
    )

    feats = with_extra_features(ATS2_FEATURES, extra_features)
    results: dict = {"strategy": "ats2", "features": feats}
    for direction in ("LONG", "SHORT"):
        d = df[df["direction"] == direction].reset_index(drop=True)
        if len(d) < 300:
            print(f"ats2 {direction}: only {len(d)} events — skipped")
            continue
        # purge gap 7 days: TSI trades can run over several days; generous
        # against twin leakage between train/val/test.
        train, val, test = chrono_split(d, gap_hours=7 * 24)
        print(
            f"ats2 {direction}: {len(d)} events | split {len(train)}/{len(val)}/{len(test)} | "
            f"base rate TP1 {d['outcome'].mean() * 100:.1f}%"
        )

        model, iso, thresh, val_stats, test_stats, calib_new = train_binary(
            train, val, test, feats, picker=pick_threshold_safe
        )

        meta = {
            "trainer": "tools/retrain_from_replay.py",
            "strategy": "ats2",
            "model_id": "ATS2",
            "direction": direction,
            "model_type": "binary (1=TP1-first-touch)",
            "success_proba": "predict_proba[:, 1]",
            "features": feats,
            "optimal_threshold": thresh,
            "label_source": os.path.basename(replay_path),
            "label": "first-touch TP1-before-SL of HVN/S-R geometry (bot-12 parity), fees incl.",
            "changes_vs_ats1": "DB-based over core.candles (R1-clean, no CSV intermediate), "
            "label with SL path instead of max-favourable-move proxy, chronological split with "
            "7d purge instead of random split, shared feature builder core.ats_features",
            "split": "chronological 70/15/15 + 7d purge gap",
            "xgboost_version": xgb.__version__,
            "n_train": len(train),
            "n_val": len(val),
            "n_test": len(test),
            "val_stats": val_stats,
            "test_stats": test_stats,
        }
        save_artifact(os.path.join(STAGING_DIR, f"ats2_model_{direction}.pkl"), model, feats, thresh, iso, meta)
        with open(os.path.join(STAGING_DIR, f"ats2_model_{direction}_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=str)
        results[direction] = {
            "n_events": len(d),
            "base_rate": round(d["outcome"].mean() * 100, 1),
            "threshold": thresh,
            "val_stats": val_stats,
            "test_stats": test_stats,
            "calibration_new_test": calib_new,
            "feature_importance_top": top_importance(model, feats),
        }
    return results


def artifact_slot(model_id: str) -> str:
    """Filename prefix that belongs to a generation tag (hard rule 6).

    ``EPD4`` → ``epd4``, ``MIS2-8H`` → ``mis28h``. The prefix MUST come from the tag:
    a challenger placed under the filename of a FOREIGN generation hijacks
    that loader slot during promotion and posts the same model under two
    tags (the EPD3-SHORT case from 2026-07-21, T-2026-KYT-9050-057).
    Identical to ``tools.promotion_guard.tag_prefix`` — kept local here so
    the trainer doesn't have to import core.shadow_gate + variant registry;
    equality is pinned by backtest/test_retrain_model_id.py."""
    return model_id.strip().upper().replace("-", "").lower()


def run_epd(events_path: str, extra_features=(), model_id: str = "EPD2") -> dict:
    """EPD2 retrain (MODEL_INTENT §7): binary model per direction on
    detector events from tools/epd2_build_dataset.py (only vol_ratio≥5 like live,
    label = first-touch TP1-before-SL of bot-10 HVN/SR geometry via
    simulate_exit, 7d horizon; open trades unlabelled). The builder
    writes ts/label/features instead of signal_time/outcome_tp1 → key mapping
    in the shared loader.

    ``model_id`` is the generation tag of the CREATED artifacts (hard rule 6).
    It sets ``meta.model_id`` AND the filename prefix together — letting them
    diverge is exactly the slot-hijack error above. Default ``EPD2`` keeps the
    previous run byte-identical; a retrain on a new feature definition runs
    under a free tag (EPD1/2/3 are taken)."""
    df = load_replay(events_path, ts_key="ts", label_key="label")
    if df.empty or len(df) < 600:
        raise SystemExit(f"Too few labelled EPD2 events ({len(df)}) in {events_path}")
    print(
        f"epd: {len(df)} events, {df['symbol'].nunique()} coins, {df['signal_time'].min()} → {df['signal_time'].max()}"
    )

    feats = with_extra_features(EPD2_FEATURES, extra_features)
    # Register keys are UPPER (core/shadow_gate._norm) — normalise here so
    # a `--model-id epd4` doesn't end up as a lowercase tag in meta
    # and miss the lifecycle lookup there.
    model_id = model_id.strip().upper()
    slot = artifact_slot(model_id)
    results: dict = {"strategy": "epd2", "model_id": model_id, "features": feats}
    for direction in ("LONG", "SHORT"):
        d = df[df["direction"] == direction].reset_index(drop=True)
        if len(d) < 300:
            print(f"epd2 {direction}: only {len(d)} events — skipped")
            continue
        # purge gap 7 days = label horizon of the builder (HORIZON_CANDLES).
        gap_days = 7
        train, val, test = chrono_split(d, gap_hours=gap_days * 24)
        print(
            f"epd2 {direction}: {len(d)} events | split {len(train)}/{len(val)}/{len(test)} | "
            f"base rate TP1 {d['outcome'].mean() * 100:.1f}%"
        )
        if min(len(train), len(val), len(test)) < 50:
            # Time period too short for purge gap (e.g. truncated builder
            # run): iso.fit/picker would crash on empty slices.
            #
            # This is the standard case for EVERY post-P1.39 cut, as long as
            # the history is young (T-2026-KYT-9050-004). The bare message
            # "skipped" looked like a data error there, though it's just a
            # calendar statement — hence the calculation:
            # val and test each get the 15% quantile band, and the
            # purge gap eats the first `gap_days` days of it. A band must
            # EXCEED the gap before any row lands in val/test at all.
            diag = split_shortfall(d, gap_days, min_rows=50)
            print(
                f"epd2 {direction}: degenerate split — skipped. "
                f"span {diag['span_days']:.1f}d, 15% band {diag['band_days']:.1f}d < purge gap {gap_days}d "
                f"(density {diag['rows_per_day']:.0f} rows/day) ⇒ val/test empty. "
                f"For ≥{diag['min_rows']} rows per slice need ~{diag['required_span_days']:.0f}d span "
                f"(~{diag['missing_days']:.0f}d more data collection)."
            )
            results[direction] = {"n_events": len(d), "skipped": "degenerate_split", **diag}
            continue

        model, iso, thresh, val_stats, test_stats, calib_new = train_binary(
            train, val, test, feats, picker=pick_threshold_safe
        )

        meta = {
            "trainer": "tools/retrain_from_replay.py",
            "strategy": "epd2",
            "model_id": model_id,
            "direction": direction,
            "model_type": "binary (1=TP1-first-touch)",
            "success_proba": "predict_proba[:, 1]",
            "features": feats,
            "optimal_threshold": thresh,
            "label_source": os.path.basename(events_path),
            "label": "first-touch TP1-before-SL of bot-10 HVN/SR geometry (simulate_exit, 7d), fees incl.",
            "changes_vs_epd1": "only vol_ratio>=5 events (training==serving instead of OOD), label = "
            "posted geometry instead of fix bracket, chronological split with "
            "7d purge instead of random split, +6 funding features (operator 2026-07-06)",
            "split": "chronological 70/15/15 + 7d purge gap",
            "xgboost_version": xgb.__version__,
            "n_train": len(train),
            "n_val": len(val),
            "n_test": len(test),
            "val_stats": val_stats,
            "test_stats": test_stats,
        }
        save_artifact(os.path.join(STAGING_DIR, f"{slot}_model_{direction}.pkl"), model, feats, thresh, iso, meta)
        with open(os.path.join(STAGING_DIR, f"{slot}_model_{direction}_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=str)
        results[direction] = {
            "n_events": len(d),
            "base_rate": round(d["outcome"].mean() * 100, 1),
            "threshold": thresh,
            "val_stats": val_stats,
            "test_stats": test_stats,
            "calibration_new_test": calib_new,
            "feature_importance_top": top_importance(model, feats),
        }
    return results


def run_atb(replay_path: str, extra_features=()) -> dict:
    """ATB2 retrain (MODEL_INTENT §11, task-104): binary model per direction on
    converging-channel breakout events of the shared detector
    (core/atb2_features). Label = first-touch TP1-before-SL of measured-move
    geometry incl. SL path and fees (fixes the +10%-touch-WITHOUT-SL label of
    dead BT1 trainer, X-R1/X-R5), chronological 3-way split with embargo
    (fixes twin leakage over 72h-overlapping windows, X-R3),
    threshold via pick_threshold_safe on validation (fixes test-set
    threshold, X-R2). Replaces ATB1 completely — the old close-regression
    detector is discarded."""
    df = load_replay(replay_path)
    if df.empty or len(df) < 400:
        raise SystemExit(f"Too few ATB2 replay events ({len(df)}) in {replay_path}")
    print(
        f"atb2: {len(df)} events, {df['symbol'].nunique()} coins, "
        f"{df['signal_time'].min()} → {df['signal_time'].max()}"
    )

    feats = with_extra_features(ATB2_FEATURES, extra_features)
    results: dict = {"strategy": "atb2", "features": feats}
    for direction in ("LONG", "SHORT"):
        d = df[df["direction"] == direction].reset_index(drop=True)
        if len(d) < 200:
            print(f"atb2 {direction}: only {len(d)} events — skipped")
            continue
        # silent feature death self-test on training replay (P0.12 pattern).
        assert_atb2_alive(d, context=f" ({direction} replay)")
        # purge gap 3 days: measured-move trades can run over several days;
        # generous embargo against overlapping episodes (X-R3 fix).
        train, val, test = chrono_split(d, gap_hours=3 * 24)
        print(
            f"atb2 {direction}: {len(d)} events | split {len(train)}/{len(val)}/{len(test)} | "
            f"base rate TP1 {d['outcome'].mean() * 100:.1f}%"
        )

        model, iso, thresh, val_stats, test_stats, calib_new = train_binary(
            train, val, test, feats, picker=pick_threshold_safe
        )

        meta = {
            "trainer": "tools/retrain_from_replay.py",
            "strategy": "atb2",
            "model_id": "ATB2",
            "direction": direction,
            "model_type": "binary (1=TP1-first-touch)",
            "success_proba": "predict_proba[:, 1]",
            "features": feats,
            "optimal_threshold": thresh,
            "label_source": os.path.basename(replay_path),
            "label": "first-touch TP1-before-SL of measured-move geometry "
            "(⅓/⅔/1× channel width), fees incl.",
            "changes_vs_atb1": "converging-channel detector (confirmed pivots, "
            "closed breakout, §11) instead of 90d close regression line; label WITH "
            "SL path instead of +10% touch/72h; chronological split with 3d purge instead of "
            "random split over overlapping windows; threshold on validation instead of "
            "test-set; 5 WillyAlgoTrader setup features + channel geometry as XGB features",
            "split": "chronological 70/15/15 + 3d purge gap",
            "threshold_selected_on": "validation",
            "xgboost_version": xgb.__version__,
            "n_train": len(train),
            "n_val": len(val),
            "n_test": len(test),
            "val_stats": val_stats,
            "test_stats": test_stats,
        }
        save_artifact(
            os.path.join(STAGING_DIR, f"atb2_model_{direction}.pkl"),
            model, feats, thresh, iso, meta,
        )
        with open(os.path.join(STAGING_DIR, f"atb2_model_{direction}_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, default=str)
        results[direction] = {
            "n_events": len(d),
            "base_rate": round(d["outcome"].mean() * 100, 1),
            "threshold": thresh,
            "val_stats": val_stats,
            "test_stats": test_stats,
            "calibration_new_test": calib_new,
            "feature_importance_top": top_importance(model, feats),
        }
    return results


def top_importance(model, feature_cols, k=8):
    imp = model.feature_importances_
    order = np.argsort(imp)[::-1][:k]
    return [{"feature": feature_cols[i], "importance": round(float(imp[i]), 4)} for i in order]


def main():
    # cp1252 console: emojis in output must not abort the run.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True,
                    choices=["td", "bb", "abr1", "mis1", "rub", "epd", "atb2", "ats"])
    ap.add_argument("--tf", default="4h", choices=["1h", "4h"])
    ap.add_argument("--replay", default=None)
    ap.add_argument("--days", type=int, default=540)
    ap.add_argument(
        "--stride", type=int, default=24, help="mis1: sampling stride of the replay (enters the purge gap)"
    )
    ap.add_argument(
        "--label-mode",
        default="geometry",
        choices=["geometry", "move"],
        help="mis1: geometry = TP1-before-SL of smart targets; "
        "move = ±X%% movement within the horizon (operator concept)",
    )
    ap.add_argument(
        "--move-labels",
        default=None,
        help="mis1 move: JSONL from tools/mis1_move_labels.py (default: mis1_move_labels.jsonl next to replay)",
    )
    ap.add_argument(
        "--move-basis",
        default="close",
        choices=["close", "wick"],
        help="mis1 move: close price or wick extremes as label basis "
        "(operator 2026-07-06: train and compare both variants)",
    )
    ap.add_argument(
        "--features",
        action="append",
        choices=sorted(FEATURE_HOOKS),
        default=None,
        help="optional additive feature block (DEFAULT-OFF, §K7 MOM). 'moments' "
        "appends core.moment_features.MOMENT_FEATURES to the feature contract of the "
        "strategy (can be specified multiple times). WITHOUT this flag retrain is "
        "byte-identical to before (no-op attachment). Appending the names triggers "
        "NO retrain — the replay writer must deliver the moment columns first (queue).",
    )
    ap.add_argument(
        "--model-id",
        default="EPD2",
        help="epd: generation tag of created artifacts (hard rule 6). Sets meta.model_id "
        "AND the filename prefix (EPD4 -> staging_models/epd4_model_{LONG,SHORT}.pkl, "
        "retrain_epd4_stats.json). default EPD2 = unchanged run. only for --strategy epd.",
    )
    args = ap.parse_args()
    extra_features = resolve_extra_features(args.features)
    args.model_id = args.model_id.strip().upper()
    if args.model_id != "EPD2" and args.strategy != "epd":
        # Better to fail than silently swallow the flag: the caller would otherwise
        # think a new tag is set and promote an artifact under the old one.
        raise SystemExit("--model-id is today only wired for --strategy epd.")
    if not re.fullmatch(r"[A-Z][A-Z0-9]*(-[A-Z0-9]+)*", args.model_id):
        raise SystemExit(f"--model-id {args.model_id!r} is not a valid model tag (e.g. EPD4, MIS2-8H).")

    if args.replay is None:
        if args.strategy == "epd":
            # EPD2 uses detector events from the builder, not candle replay.
            args.replay = os.path.join(REPLAY_DIR, "epd2_events.jsonl")
        else:
            tag = f"{args.strategy}_{args.tf}" if args.strategy in ("td", "bb") else args.strategy
            days = args.days if args.strategy in ("td", "bb", "mis1") else 365
            args.replay = os.path.join(REPLAY_DIR, f"{tag}_replay_{days}d.jsonl")

    if args.strategy in ("rub", "epd", "atb2", "ats"):
        # a dispatch instead of twin ternaries — the next event strategy
        # adds exactly one entry (runner + artifact name together).
        runner, name = {
            "rub": (run_rub, "rub2"),
            # artifact name AND stats name follow the tag (rule 6, see artifact_slot).
            "epd": (partial(run_epd, model_id=args.model_id), artifact_slot(args.model_id)),
            "atb2": (run_atb, "atb2"),
            "ats": (run_ats, "ats2"),
        }[args.strategy]
        result = runner(args.replay, extra_features)
        out = os.path.join(STAGING_DIR, f"retrain_{name}_stats.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)
        print(f"\nStats: {out}")
        return

    if args.strategy in ("td", "bb"):
        result = run_td_bb(args.strategy, args.tf, args.replay, extra_features)
        name = f"{args.strategy}_{args.tf}"
    elif args.strategy == "mis1":
        result = run_mis1(
            args.replay,
            stride_hours=args.stride,
            label_mode=args.label_mode,
            move_path=args.move_labels,
            move_basis=args.move_basis,
            extra_features=extra_features,
        )
        if args.label_mode == "move":
            name = "mis1_move" if args.move_basis == "close" else "mis1_move_wick"
        else:
            name = "mis1"
    else:
        result = run_abr1(args.replay, extra_features)
        name = "abr1"

    out = os.path.join(STAGING_DIR, f"retrain_{name}_stats.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    print(f"\nStats: {out}")


if __name__ == "__main__":
    main()

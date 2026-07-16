#!/usr/bin/env py -3.13
# tools/breadth_study.py — K6 · BRD (market breadth/dispersion) study (T-2026-CU-9050-140)
"""Read-only validation study for the shared breadth builder (core.breadth_features).

Question (docs/MODEL_CANDIDATES_SPEC_2026-07.md §K6): do market-breadth /
dispersion features over the ~530-coin USDT-perp universe (share of coins > EMA200/
EMA50, median-7d return, advance/decline, return-dispersion vs BTC, TOTAL3-proxy
level/regression/breakout) ADD information over the BTC-only regime — and do they
separate RUB-LONG outcomes out-of-sample? The stop-criterion (§K6): if NO breadth
feature separates RUB-LONG months out-of-sample better than the existing BTC-only
regime ⇒ document it; the builder stays as infra regardless (a no-op is a valid
win). This script produces that verdict; it deploys nothing (any RUB-LONG gate /
whitelist / HMM follow-up is an operator decision, Michi).

Three parts:
  (a) Breadth features as-of vs the forward outcome of the RUB-**LONG** events in
      _X/staging_models/replay/rub_replay_365d.jsonl (EXISTS, 36 MB — streamed line
      by line, no new sim run). ``net_pnl_pct`` / ``outcome_tp1`` are the already
      simulated per-trade outcomes. Per-feature Spearman (as-of feature vs
      net_pnl_pct) with a chrono val/test split, top/bottom terciles, month-split.
      DECISIVE head-to-head: an incremental logit predicting the RUB-LONG WIN from
      the BTC-only regime features (joined as-of from regime_history) vs the SAME
      regime features PLUS breadth — chrono val/test — the ΔAUC on the test half is
      the "does breadth beat BTC-only regime OOS" number.
  (b) Breadth features vs ``regime_history`` classes: a logit diagnostic — does
      adding breadth to a BTC-only feature set improve TREND_UP-vs-rest AUC on a
      chronological holdout? Per-feature single-variable AUC alongside (supporting).
  (c) Month-split throughout.

RESUME / CHECKPOINT (the live VPS watchdog reaps stray python.exe reproducibly,
~every few minutes around ~75 coins, so a naive full run never finishes — same
lesson as the K1/tsmom_study.py full run). Mirrors that proven pattern:
  * The kill-prone phase is LOADING the ~530 per-coin daily panels (one DB query
    per coin). The cross-section build + analysis is seconds. So the CHECKPOINT
    UNIT is the loaded per-coin panel: each coin is loaded via the shared builder's
    load_universe_panels (unchanged), folded into a compact per-coin panel store,
    and every --checkpoint-every coins the store + processed-set are atomically
    written to a transient JSON state file in the OS TEMP dir (NEVER the repo).
  * --resume reloads that state, SKIPS already-processed coins, and folds the rest.
    A kill between checkpoints loses only the last <N coins' loads, which are simply
    re-loaded on resume (idempotent, keyed by symbol → never double-counted).
  * Once every universe coin is processed the build/analyze phase runs from the
    persisted store; if a kill hits there, --resume re-enters it directly (loading
    already complete). On clean exit the state file is removed. RAM guard aborts
    below MIN_AVAIL_MB rather than risk the live fleet; memory is bounded
    (~530×~880×5 floats ≈ 18 MB store).

READ-ONLY: SELECTs only, BELOW_NORMAL (walkforward_sim.set_low_priority). The VPS
is CPU-saturated; walkforward_sim.check_cpu_headroom would abort, so a study-local
--skip-cpu-check flag (default OFF) bypasses it deliberately. Artifacts →
staging_models/ ONLY (repo Rule 2), never the repo root.

Contracts reused (no reinvention): core.breadth_features (the shared X-R1 builder,
UNCHANGED); core.time.LEGACY_WRITER_TZ = "Europe/Bucharest" — regime_history.ts is
TIMESTAMP WITHOUT TIME ZONE = naive local Bucharest, localized DST-aware before the
as-of join (a constant ±offset is wrong across the DST flip); RUB signal_time is
naive UTC (a candle open_time instant), used as-is. Known bias: survivorship —
coins.json / the per-coin tables cover ACTIVE USDT-perps; the TOTAL3 index is a
PRICE proxy (no market-cap weights). Both documented in the builder.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.breadth_features import (  # noqa: E402
    BREADTH_FEATURES,
    BreadthFeatureError,
    breadth_features_asof,
    build_breadth_panel,
    load_universe_panels,
)
from core.database import db_connection  # noqa: E402
from core.time import LEGACY_WRITER_TZ  # noqa: E402
from tools.walkforward_sim import check_cpu_headroom, set_low_priority  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_ROOT, "staging_models")
# The replay lives outside the repo in Documents\_X (same convention as
# tools/scratch_exit_study.py). Absolute path, overridable via --replay-path or the
# KYTHERA_RUB_REPLAY env var — a worktree's parent is .claude/worktrees, not Documents.
DEFAULT_REPLAY = os.environ.get(
    "KYTHERA_RUB_REPLAY", r"C:\Users\Michael\Documents\_X\staging_models\replay\rub_replay_365d.jsonl"
)
# Transient resume-state (per-coin panel store + processed set) in the OS temp dir,
# NEVER the repo. Removed on clean completion.
DEFAULT_STATE_PATH = os.path.join(tempfile.gettempdir(), "breadth_study_state.json")

TREND_UP = "TREND_UP"
BTC_REGIME_FEATURES = ["btc_return_1h", "btc_return_4h", "btc_atr_1h_pct", "btc_atr_4h_pct"]
MIN_ROWS_FOR_LOGIT = 200  # below this the AUC diagnostic is skipped
MIN_ABS_SPEARMAN = 0.03  # OOS magnitude floor for a feature to "separate" (mirrors funding_risk_study)
MIN_AUC_DELTA = 0.02  # test-half ΔAUC (breadth over BTC-only) to count as "adds information"
MIN_STABLE_FEATURES = 2  # clean-positive needs at least this many OOS-stable single features
REGIME_HURT_GUARD = -0.02  # if part-(b) breadth ΔAUC is below this, breadth is NOT robustly additive
CHECKPOINT_EVERY = 25  # atomic-write panel store + processed set every N coins
MIN_AVAIL_MB = 500  # abort below this free RAM (protect the live fleet)


# ─────────────────────────────────────────────────────────────────────────────
# Small stats (numpy-only; sklearn only for the logit)
# ─────────────────────────────────────────────────────────────────────────────
def load_coins(path: str = "coins.json") -> list[str]:
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as fh:
        coins = json.load(fh)
    if not isinstance(coins, list) or not coins:
        raise ValueError(f"{path} is not a non-empty list")
    return coins


def _r(v: float | None, nd: int = 4) -> float | None:
    return None if v is None else round(v, nd)


def spearman(x: np.ndarray, y: np.ndarray) -> float | None:
    """Spearman rank correlation (numpy-only). None if degenerate / too few points."""
    mask = ~(np.isnan(x) | np.isnan(y))
    x, y = x[mask], y[mask]
    if len(x) < 30:
        return None
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if np.std(rx) == 0 or np.std(ry) == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def rank_auc(scores: np.ndarray, labels: np.ndarray) -> float | None:
    """AUC of a single continuous score vs a binary label (Mann-Whitney, numpy-only)."""
    mask = ~np.isnan(scores)
    scores, labels = scores[mask], labels[mask]
    pos = labels == 1
    n_pos, n_neg = int(pos.sum()), int((~pos).sum())
    if n_pos == 0 or n_neg == 0:
        return None
    order = pd.Series(scores).rank().to_numpy()
    auc = (order[pos].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return float(auc)


def _logit_auc(x_train, y_train, x_test, y_test) -> float | None:
    """Standardized logistic-regression holdout AUC. sklearn is in the live venv;
    None on any failure (degenerate class, singular fit)."""
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        from sklearn.preprocessing import StandardScaler
    except Exception:
        return None
    if len(np.unique(y_train)) < 2 or len(np.unique(y_test)) < 2:
        return None
    try:
        scaler = StandardScaler().fit(x_train)
        clf = LogisticRegression(max_iter=1000, C=1.0)
        clf.fit(scaler.transform(x_train), y_train)
        proba = clf.predict_proba(scaler.transform(x_test))[:, 1]
        return float(roc_auc_score(y_test, proba))
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Resume state — compact per-coin panel store in OS temp
# ─────────────────────────────────────────────────────────────────────────────
def panel_to_compact(df: pd.DataFrame) -> dict:
    """A loaded per-coin panel → JSON-safe compact arrays (epoch-seconds + floats)."""
    return {
        "t": (df["open_time"].astype("int64") // 10**9).tolist(),
        "c": df["close"].tolist(),
        "v": df["volume"].tolist(),
        "e50": df["ema_50"].tolist(),
        "e200": df["ema_200"].tolist(),
    }


def panels_from_store(store: dict) -> dict[str, pd.DataFrame]:
    """Rebuild the {symbol -> DataFrame} panels the builder expects from the store."""
    panels: dict[str, pd.DataFrame] = {}
    for sym, d in store.items():
        panels[sym] = pd.DataFrame(
            {
                "open_time": pd.to_datetime(np.asarray(d["t"], dtype="int64"), unit="s", utc=True),
                "close": np.asarray(d["c"], dtype=float),
                "volume": np.asarray(d["v"], dtype=float),
                "ema_50": np.asarray(d["e50"], dtype=float),
                "ema_200": np.asarray(d["e200"], dtype=float),
            }
        )
    return panels


def save_state(state_path: str, state: dict) -> None:
    """Atomic-write (temp + os.replace) so a mid-write kill never truncates state."""
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, state_path)


def load_state(state_path: str) -> dict | None:
    if not os.path.exists(state_path):
        return None
    try:
        with open(state_path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _avail_mb() -> float | None:
    try:
        import psutil

        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:
        return None


def _rss_mb() -> float | None:
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def load_one_panel(conn, sym: str) -> pd.DataFrame | None:
    """Load ONE coin's daily panel via the shared builder (unchanged). Returns the
    frame, or None if the coin has no usable data (delisted/missing table). A
    genuine X-R1 column-contract violation is re-raised (schema is uniform, so this
    should never fire per-coin — but it must NOT be swallowed)."""
    try:
        p = load_universe_panels(conn, [sym], tf="1d")
    except BreadthFeatureError as e:
        if "fehlende Pflicht-Spalten" in str(e):
            raise  # real column-contract break — surface it, do not treat as no-data
        return None  # "keine verwertbaren Panels" → this coin simply has no data
    return p.get(sym)


# ─────────────────────────────────────────────────────────────────────────────
# (a) RUB-LONG events vs breadth as-of
# ─────────────────────────────────────────────────────────────────────────────
def stream_rub_long_events(path: str, max_events: int | None) -> pd.DataFrame:
    """Stream the replay jsonl, keep only RUB LONG events (signal_time, symbol,
    net_pnl_pct, outcome_tp1). The file is 36 MB — never fully materialized."""
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if '"LONG"' not in line:  # cheap pre-filter before json.loads
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("direction") != "LONG":
                continue
            rows.append(
                {
                    "signal_time": rec.get("signal_time"),
                    "symbol": rec.get("symbol"),
                    "net_pnl_pct": rec.get("net_pnl_pct"),
                    "outcome_tp1": rec.get("outcome_tp1"),
                }
            )
            if max_events is not None and len(rows) >= max_events:
                break
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["signal_time_utc"] = pd.to_datetime(df["signal_time"], utc=True)  # naive UTC instant
    df["net_pnl_pct"] = pd.to_numeric(df["net_pnl_pct"], errors="coerce")
    df["outcome_tp1"] = pd.to_numeric(df["outcome_tp1"], errors="coerce")
    return df.dropna(subset=["signal_time_utc", "net_pnl_pct"]).reset_index(drop=True)


def attach_breadth(df: pd.DataFrame, panel: pd.DataFrame, ts_col: str) -> pd.DataFrame:
    """As-of breadth features for every row (O(log n) lookup into the daily panel)."""
    feat_rows = [breadth_features_asof(panel, ts) for ts in df[ts_col]]
    feats = pd.DataFrame(feat_rows, index=df.index)
    return pd.concat([df, feats], axis=1)


def attach_regime_asof(df: pd.DataFrame, regime_df: pd.DataFrame, ts_col: str) -> pd.DataFrame:
    """Join the BTC-only regime features as-of each event (backward merge_asof).

    Events before regime_history begins (2026-01-18) get NaN regime features and
    drop out of the head-to-head logit — reported as reduced n, not hidden.
    """
    if regime_df.empty:
        for c in BTC_REGIME_FEATURES:
            df[c] = np.nan
        return df
    left = df.sort_values(ts_col).reset_index(drop=True)
    right = regime_df[["ts_utc", *BTC_REGIME_FEATURES]].sort_values("ts_utc").reset_index(drop=True)
    return pd.merge_asof(left, right, left_on=ts_col, right_on="ts_utc", direction="backward")


def month_split_pnl(df: pd.DataFrame, min_n: int = 20) -> dict:
    out = {}
    for m, gm in df.groupby(df["signal_time_utc"].dt.strftime("%Y-%m")):
        if len(gm) >= min_n:
            out[m] = {
                "n": int(len(gm)),
                "avg_net_pnl_pct": round(float(gm["net_pnl_pct"].mean()), 4),
                "wr": round(float((gm["net_pnl_pct"] > 0).mean()), 4),
            }
    return out


def tercile_expectancy(df: pd.DataFrame, feature: str) -> dict:
    """Bottom vs top tercile net-PnL expectancy for a breadth feature, plus the
    chrono val/test read so out-of-sample separation is visible."""
    sub = df[df[feature].notna()]
    if len(sub) < 60:
        return {"n": int(len(sub)), "note": "too few points"}
    lo, hi = float(np.quantile(sub[feature], 1 / 3)), float(np.quantile(sub[feature], 2 / 3))
    median_ts = sub["signal_time_utc"].median()

    def blk(g: pd.DataFrame) -> dict:
        h1 = g[g["signal_time_utc"] < median_ts]
        h2 = g[g["signal_time_utc"] >= median_ts]
        return {
            "n": int(len(g)),
            "avg_net_pnl_pct": round(float(g["net_pnl_pct"].mean()), 4) if len(g) else None,
            "wr": round(float((g["net_pnl_pct"] > 0).mean()), 4) if len(g) else None,
            "val_avg_net_pnl_pct": round(float(h1["net_pnl_pct"].mean()), 4) if len(h1) else None,
            "test_avg_net_pnl_pct": round(float(h2["net_pnl_pct"].mean()), 4) if len(h2) else None,
        }

    return {
        "lo_edge": round(lo, 6),
        "hi_edge": round(hi, 6),
        "bottom_tercile": blk(sub[sub[feature] <= lo]),
        "top_tercile": blk(sub[sub[feature] >= hi]),
    }


def incremental_logit_vs_regime(df: pd.DataFrame) -> dict | None:
    """DECISIVE head-to-head: predict the RUB-LONG WIN (net_pnl_pct>0) from the
    BTC-only regime features vs the SAME regime features PLUS breadth, chrono
    val/test (70/30). The test-half ΔAUC is the "breadth beats BTC-only regime OOS"
    number. None if too few events carry both regime and breadth."""
    present = [f for f in BREADTH_FEATURES if f in df.columns]
    need = BTC_REGIME_FEATURES + present
    sub = df.dropna(subset=need).sort_values("signal_time_utc").reset_index(drop=True)
    if len(sub) < MIN_ROWS_FOR_LOGIT:
        return {"n": int(len(sub)), "note": f"< {MIN_ROWS_FOR_LOGIT} events with regime+breadth overlap"}
    y = (sub["net_pnl_pct"] > 0).astype(int).to_numpy()
    cut = int(len(sub) * 0.7)
    y_tr, y_te = y[:cut], y[cut:]
    auc_reg = _logit_auc(
        sub[BTC_REGIME_FEATURES].to_numpy(float)[:cut], y_tr,
        sub[BTC_REGIME_FEATURES].to_numpy(float)[cut:], y_te,
    )
    auc_both = _logit_auc(
        sub[need].to_numpy(float)[:cut], y_tr, sub[need].to_numpy(float)[cut:], y_te
    )
    return {
        "target": "RUB-LONG win (net_pnl_pct>0)",
        "split": "chrono 70/30",
        "n_events_overlap": int(len(sub)),
        "n_test": int(len(y_te)),
        "auc_regime_only": _r(auc_reg),
        "auc_regime_plus_breadth": _r(auc_both),
        "auc_delta_test": _r(auc_both - auc_reg) if (auc_reg is not None and auc_both is not None) else None,
    }


def analyze_rub(df: pd.DataFrame) -> dict:
    result: dict = {
        "n_events": int(len(df)),
        "n_with_breadth": int(df["brd_pct_above_ema200"].notna().sum()) if "brd_pct_above_ema200" in df else 0,
        "overall": {
            "avg_net_pnl_pct": round(float(df["net_pnl_pct"].mean()), 4),
            "wr": round(float((df["net_pnl_pct"] > 0).mean()), 4),
        },
        "months": month_split_pnl(df),
        "feature_gradient": {},
        "feature_terciles": {},
        "incremental_logit_vs_regime": incremental_logit_vs_regime(df),
    }
    median_ts = df["signal_time_utc"].median()
    h1 = df[df["signal_time_utc"] < median_ts]
    h2 = df[df["signal_time_utc"] >= median_ts]
    y = df["net_pnl_pct"].to_numpy(dtype=float)
    for feat in BREADTH_FEATURES:
        if feat not in df.columns:
            continue
        x = df[feat].to_numpy(dtype=float)
        result["feature_gradient"][feat] = {
            "spearman_all": _r(spearman(x, y)),
            "spearman_val": _r(spearman(h1[feat].to_numpy(dtype=float), h1["net_pnl_pct"].to_numpy(dtype=float))),
            "spearman_test": _r(spearman(h2[feat].to_numpy(dtype=float), h2["net_pnl_pct"].to_numpy(dtype=float))),
        }
        result["feature_terciles"][feat] = tercile_expectancy(df, feat)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# (b) regime_history diagnostic
# ─────────────────────────────────────────────────────────────────────────────
def load_regime_history(conn, max_rows: int | None) -> pd.DataFrame:
    limit = f"LIMIT {int(max_rows)}" if max_rows else ""
    q = f"""
        SELECT ts, regime, btc_return_1h, btc_return_4h, btc_atr_1h_pct, btc_atr_4h_pct
        FROM regime_history
        ORDER BY ts
        {limit}
    """
    df = pd.read_sql_query(q, conn)
    if df.empty:
        return df
    # ts is naive local Bucharest → DST-aware UTC (same recipe as funding_risk_study).
    localized = pd.to_datetime(df["ts"]).dt.tz_localize(
        LEGACY_WRITER_TZ, nonexistent="shift_forward", ambiguous="NaT"
    )
    df["ts_utc"] = localized.dt.tz_convert("UTC")
    for c in BTC_REGIME_FEATURES:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["ts_utc"]).reset_index(drop=True)


def analyze_regime(df: pd.DataFrame) -> dict:
    present = [f for f in BREADTH_FEATURES if f in df.columns]
    labelled = df.dropna(subset=BTC_REGIME_FEATURES + present).copy()
    result: dict = {
        "n_rows": int(len(df)),
        "n_usable": int(len(labelled)),
        "regime_counts": {k: int(v) for k, v in df["regime"].value_counts().items()},
        "per_feature_auc_trend_up": {},
        "incremental_logit": None,
        "note": "",
    }
    if len(labelled) < MIN_ROWS_FOR_LOGIT:
        result["note"] = f"only {len(labelled)} usable rows (< {MIN_ROWS_FOR_LOGIT}); logit diagnostic skipped."
        return result

    y = (labelled["regime"] == TREND_UP).astype(int).to_numpy()
    if int(y.sum()) == 0:
        result["note"] = "no TREND_UP rows in this regime_history window — AUC undefined (single class)."
    for feat in present:
        result["per_feature_auc_trend_up"][feat] = _r(rank_auc(labelled[feat].to_numpy(dtype=float), y))

    labelled = labelled.sort_values("ts_utc").reset_index(drop=True)
    cut = int(len(labelled) * 0.7)
    tr, te = labelled.iloc[:cut], labelled.iloc[cut:]
    y_tr = (tr["regime"] == TREND_UP).astype(int).to_numpy()
    y_te = (te["regime"] == TREND_UP).astype(int).to_numpy()
    auc_btc = _logit_auc(
        tr[BTC_REGIME_FEATURES].to_numpy(float), y_tr, te[BTC_REGIME_FEATURES].to_numpy(float), y_te
    )
    auc_both = _logit_auc(
        tr[BTC_REGIME_FEATURES + present].to_numpy(float), y_tr,
        te[BTC_REGIME_FEATURES + present].to_numpy(float), y_te,
    )
    result["incremental_logit"] = {
        "target": "TREND_UP vs rest",
        "split": "chrono 70/30",
        "n_train": int(len(tr)),
        "n_test": int(len(te)),
        "auc_btc_only": _r(auc_btc),
        "auc_btc_plus_breadth": _r(auc_both),
        "auc_delta": _r(auc_both - auc_btc) if (auc_btc is not None and auc_both is not None) else None,
    }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Verdict (§K6 stop-criterion)
# ─────────────────────────────────────────────────────────────────────────────
def derive_verdict(rub: dict, regime: dict) -> dict:
    """§K6: breadth must separate RUB-LONG OUT-OF-SAMPLE better than the existing
    BTC-only regime. Three-way, deliberately conservative (two independent OOS
    tests must AGREE for a clean positive):

    Signals:
      * head-to-head: test-half ΔAUC (RUB-LONG win, regime+breadth vs regime-only).
        auc_ok = ΔAUC ≥ MIN_AUC_DELTA.
      * per-feature corroboration: breadth features whose Spearman-vs-net_pnl is
        SIGN-stable across the chrono val/test halves with |ρ_test| ≥ MIN_ABS_SPEARMAN.
      * part-(b) guard: the regime_history TREND_UP incremental ΔAUC. If breadth
        HURTS the cleaner, far-larger-n regime classification below REGIME_HURT_GUARD,
        the RUB-LONG lift is not corroborated by an independent OOS test.

    Verdicts:
      * "breadth-adds-over-btc-regime"      — auc_ok AND ≥MIN_STABLE_FEATURES stable
        single features AND part-(b) does NOT strongly hurt (a clean, corroborated edge).
      * "weak/mixed-breadth-signal (not deployable)" — auc_ok but corroboration weak
        (few OOS-stable features and/or most features sign-flip) OR part-(b) contradicts.
        A modest multivariate lift that the independent tests do not back up.
      * "no-op/breadth-no-better-than-btc-regime" — no head-to-head lift at all.

    In every non-clean case the shared builder still stands as infrastructure (HMM
    T-020, whitelist §23); any RUB-LONG breadth gate is an operator decision (Michi),
    never licensed here."""
    stable_features = []
    n_sign_flip = 0
    for feat, g in rub.get("feature_gradient", {}).items():
        sv, st = g.get("spearman_val"), g.get("spearman_test")
        if sv is None or st is None:
            continue
        sign_stable = (sv > 0) == (st > 0)
        if not sign_stable:
            n_sign_flip += 1
        if sign_stable and abs(st) >= MIN_ABS_SPEARMAN:
            stable_features.append({"feature": feat, "spearman_val": sv, "spearman_test": st})

    inc = rub.get("incremental_logit_vs_regime") or {}
    auc_delta = inc.get("auc_delta_test")
    auc_ok = auc_delta is not None and auc_delta >= MIN_AUC_DELTA

    reg_inc = regime.get("incremental_logit") or {}
    reg_delta = reg_inc.get("auc_delta")
    regime_hurts = reg_delta is not None and reg_delta < REGIME_HURT_GUARD

    feat_ok = len(stable_features) >= MIN_STABLE_FEATURES
    clean_positive = bool(auc_ok and feat_ok and not regime_hurts)
    weak_positive = bool(auc_ok and not clean_positive)

    if clean_positive:
        verdict = "breadth-adds-over-btc-regime"
        note = (
            "Breadth separates RUB-LONG outcomes OOS, lifts the win-logit over BTC-only regime, "
            "and the independent regime_history test does not contradict it — a corroborated edge. "
            "Deployment (RUB-LONG breadth gate) remains an operator decision (Michi)."
        )
    elif weak_positive:
        verdict = "weak/mixed-breadth-signal (not deployable)"
        note = (
            f"The multivariate head-to-head lift is modest (+{auc_delta} test AUC) but NOT "
            f"corroborated: only {len(stable_features)} of the 11 features are OOS sign+magnitude "
            f"stable ({n_sign_flip} sign-flip val→test), and the independent regime_history TREND_UP "
            f"test shows breadth HURTING OOS (Δ={reg_delta}). Two OOS tests disagree ⇒ no clean, "
            "robust edge — §K6 near-no-op. The shared builder stays as infrastructure (HMM T-020, "
            "whitelist §23); no RUB-LONG breadth gate is licensed."
        )
    else:
        verdict = "no-op/breadth-no-better-than-btc-regime"
        note = (
            "No head-to-head lift over the BTC-only regime — §K6 no-op. The shared builder stays as "
            "infrastructure (HMM T-020, whitelist §23); no RUB-LONG breadth gate is licensed."
        )

    return {
        "verdict": verdict,
        "min_auc_delta": MIN_AUC_DELTA,
        "min_abs_spearman": MIN_ABS_SPEARMAN,
        "min_stable_features": MIN_STABLE_FEATURES,
        "regime_hurt_guard": REGIME_HURT_GUARD,
        "rub_headtohead_auc_delta_test": auc_delta,
        "rub_headtohead_auc_ok": bool(auc_ok),
        "n_oos_stable_features": len(stable_features),
        "n_features_sign_flip": n_sign_flip,
        "oos_stable_features": stable_features,
        "regime_trend_up_auc_delta": reg_delta,
        "regime_test_contradicts": bool(regime_hurts),
        "note": note,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────
def build_markdown(meta: dict, rub: dict, regime: dict, verdict: dict) -> str:
    L: list[str] = []
    L.append("# K6 · BRD — market breadth/dispersion study (T-2026-CU-9050-140)\n")
    L.append(
        f"_Generated {meta['generated_at']} · read-only · status={meta['status']} · "
        f"coins_loaded={meta['n_symbols_loaded']}/{meta['n_universe']} · RUB-LONG events={rub['n_events']} · "
        f"peak RSS {meta.get('peak_rss_mb')} MB_\n"
    )
    L.append(f"**VERDICT: {verdict['verdict']}**\n")
    L.append(
        f"- RUB-LONG head-to-head (win-logit, chrono test): ΔAUC breadth-over-BTC-regime = "
        f"**{verdict['rub_headtohead_auc_delta_test']}** (≥{verdict['min_auc_delta']} required: "
        f"{verdict['rub_headtohead_auc_ok']})"
    )
    L.append(
        f"- OOS sign+magnitude-stable breadth features (|ρ_test|≥{verdict['min_abs_spearman']}, "
        f"≥{verdict['min_stable_features']} needed for a clean edge): **{verdict['n_oos_stable_features']}**"
        + (
            f" — {', '.join(f['feature'] for f in verdict['oos_stable_features'])}"
            if verdict["oos_stable_features"]
            else ""
        )
        + f"; features that sign-FLIP val→test: {verdict['n_features_sign_flip']}/11"
    )
    L.append(
        f"- regime_history TREND_UP incremental ΔAUC (independent OOS check): "
        f"{verdict['regime_trend_up_auc_delta']} — contradicts breadth: {verdict['regime_test_contradicts']}\n"
    )
    L.append(f"> {verdict['note']}\n")
    L.append(
        "Breadth is a PRICE proxy over active USDT-perps (survivorship-biased); TOTAL3 has no real "
        "market-cap weights — see core/breadth_features docstring.\n"
    )

    L.append("## Builder output — daily breadth panel\n")
    L.append(f"- panel rows (days): {meta['panel_rows']} · span (UTC): {meta['panel_span']}")
    L.append(f"- features emitted: {', '.join(BREADTH_FEATURES)}\n")

    L.append("## (a) RUB-LONG events vs breadth as-of\n")
    L.append(f"- RUB LONG events: {rub['n_events']} (with as-of breadth: {rub['n_with_breadth']})")
    L.append(f"- overall: avg net PnL {rub['overall']['avg_net_pnl_pct']}% · WR {rub['overall']['wr']}\n")
    inc = rub.get("incremental_logit_vs_regime") or {}
    if inc.get("auc_regime_only") is not None:
        L.append(
            f"### Head-to-head win-logit (RUB-LONG win; {inc['split']}, n_test={inc.get('n_test')}, "
            f"overlap n={inc.get('n_events_overlap')})\n"
        )
        L.append(
            f"- AUC BTC-regime only = {inc['auc_regime_only']} → BTC-regime + breadth = "
            f"{inc['auc_regime_plus_breadth']} (Δ={inc['auc_delta_test']})\n"
        )
    elif inc.get("note"):
        L.append(f"### Head-to-head win-logit: skipped — {inc['note']}\n")
    L.append("### Per-feature gradient (Spearman vs net_pnl_pct; sign must survive the chrono split)\n")
    L.append("| feature | Spearman all | val | test |")
    L.append("|---|--:|--:|--:|")
    for feat, g in rub["feature_gradient"].items():
        L.append(f"| {feat} | {g['spearman_all']} | {g['spearman_val']} | {g['spearman_test']} |")
    L.append("")
    L.append("### Top vs bottom tercile net-PnL expectancy (with chrono val/test)\n")
    L.append("| feature | bottom n | bottom PnL% | bottom test% | top n | top PnL% | top test% |")
    L.append("|---|--:|--:|--:|--:|--:|--:|")
    for feat, t in rub["feature_terciles"].items():
        if "bottom_tercile" not in t:
            continue
        b, tp = t["bottom_tercile"], t["top_tercile"]
        L.append(
            f"| {feat} | {b['n']} | {b['avg_net_pnl_pct']} | {b['test_avg_net_pnl_pct']} "
            f"| {tp['n']} | {tp['avg_net_pnl_pct']} | {tp['test_avg_net_pnl_pct']} |"
        )
    L.append("")
    L.append("### RUB-LONG month-split (overall)\n")
    L.append("| month | n | avg net PnL% | WR |")
    L.append("|---|--:|--:|--:|")
    for m, s in sorted(rub["months"].items()):
        L.append(f"| {m} | {s['n']} | {s['avg_net_pnl_pct']} | {s['wr']} |")
    L.append("")

    L.append("## (b) regime_history diagnostic — does breadth add over BTC-only?\n")
    L.append(f"- regime rows: {regime['n_rows']} · usable (breadth+BTC non-NaN): {regime['n_usable']}")
    L.append(f"- regime class counts: {regime['regime_counts']}")
    if regime.get("note"):
        L.append(f"- NOTE: {regime['note']}")
    rinc = regime.get("incremental_logit")
    if rinc:
        L.append(
            f"- incremental logit ({rinc['target']}, {rinc['split']}, n_test={rinc['n_test']}): "
            f"AUC BTC-only={rinc['auc_btc_only']} → BTC+breadth={rinc['auc_btc_plus_breadth']} "
            f"(Δ={rinc['auc_delta']})"
        )
    L.append("\n### Single-feature AUC (TREND_UP vs rest)\n")
    L.append("| feature | AUC |")
    L.append("|---|--:|")
    for feat, a in regime["per_feature_auc_trend_up"].items():
        L.append(f"| {feat} | {a} |")
    L.append("")

    L.append("## Caveats\n")
    L.append(
        "- **Verdict basis**: §K6 stop-criterion — breadth must separate RUB-LONG OOS better than the "
        "existing BTC-only regime. A no-op is a valid, documented result; the builder stays as infra."
    )
    L.append("- **Survivorship**: breadth computed over active USDT-perps only; delisted coins missing.")
    L.append("- **TOTAL3 is a price proxy** (equal- and volume-weighted over perps ex BTC/ETH), not a")
    L.append("  market-cap index — prefer the scale-free dist_reg90d / breakout over the raw level.")
    L.append(
        "- RUB signal_time is naive UTC; regime_history.ts is naive Bucharest → localized DST-aware. "
        "regime_history starts 2026-01-18, so RUB-LONG events before then carry no as-of regime "
        "baseline and drop from the head-to-head logit (reported as overlap n)."
    )
    L.append(f"- CPU-check override: --skip-cpu-check={meta['skip_cpu_check']} (read-only, BELOW_NORMAL).")
    L.append(
        f"- Resume machinery: per-coin panel checkpoint every {CHECKPOINT_EVERY} coins to OS-temp state "
        "(survives watchdog kills); memory bounded, state removed on clean exit."
    )
    if meta.get("limit_symbols"):
        L.append(f"- ⚠ SAMPLING CAP: --limit-symbols={meta['limit_symbols']} (NOT a full run).")
    return "\n".join(L)


def write_outputs(out: dict, md: str, json_path: str, md_path: str) -> None:
    """Atomic write (temp + os.replace) so a mid-run kill leaves a valid file."""
    tmp = json_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    os.replace(tmp, json_path)
    tmp_md = md_path + ".tmp"
    with open(tmp_md, "w", encoding="utf-8") as fh:
        fh.write(md)
    os.replace(tmp_md, md_path)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="K6 · BRD breadth/dispersion study (read-only, resumable).")
    ap.add_argument("--limit-symbols", type=int, default=None, help="Cap the universe to the first N coins (smoke).")
    ap.add_argument("--max-events", type=int, default=None, help="Cap RUB-LONG events streamed (smoke).")
    ap.add_argument("--max-regime-rows", type=int, default=None, help="Cap regime_history rows (smoke).")
    ap.add_argument("--replay-path", default=DEFAULT_REPLAY, help="Path to rub_replay_365d.jsonl.")
    ap.add_argument("--checkpoint-every", type=int, default=CHECKPOINT_EVERY, help="Checkpoint every N coins.")
    ap.add_argument("--progress-every", type=int, default=25, help="Print progress every N coins.")
    ap.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Resume from the saved per-coin panel state (survives watchdog kills); exit 0 = complete.",
    )
    ap.add_argument("--state-path", default=DEFAULT_STATE_PATH, help="Transient resume-state JSON (OS temp, not repo).")
    ap.add_argument(
        "--skip-cpu-check",
        action="store_true",
        default=False,
        help="Bypass walkforward_sim.check_cpu_headroom (default OFF). Needed on the CPU-saturated VPS.",
    )
    args = ap.parse_args()

    set_low_priority()
    if not args.skip_cpu_check:
        check_cpu_headroom()
    else:
        print("CPU-check SKIPPED (--skip-cpu-check): read-only BELOW_NORMAL job on saturated VPS.", flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    avail0 = _avail_mb()
    if avail0 is not None:
        print(f"RAM available at start: {avail0:.0f} MB", flush=True)
        if avail0 < MIN_AVAIL_MB:
            print(f"ABORT: only {avail0:.0f} MB free (< {MIN_AVAIL_MB} MB) — refusing to risk the live fleet.")
            return 2

    universe = load_coins()
    coins = universe[: args.limit_symbols] if args.limit_symbols else universe
    state_path = args.state_path

    store: dict = {}
    processed: set[str] = set()
    peak_rss = 0.0
    if args.resume:
        st = load_state(state_path)
        if st is not None and st.get("universe_len") == len(universe) and st.get("limit_symbols") == args.limit_symbols:
            store = st.get("store", {})
            processed = set(st.get("processed", []))
            peak_rss = st.get("peak_rss", 0.0)
            print(f"RESUMED: {len(processed)} coins processed, {len(store)} panels in store", flush=True)
        else:
            print("RESUME requested but no compatible state — starting fresh.", flush=True)

    json_path = os.path.join(OUT_DIR, "breadth_study.json")
    md_path = os.path.join(OUT_DIR, "breadth_study.md")

    def persist() -> None:
        save_state(
            state_path,
            {
                "universe_len": len(universe),
                "limit_symbols": args.limit_symbols,
                "processed": sorted(processed),
                "store": store,
                "peak_rss": peak_rss,
            },
        )

    # ── Phase 1: load per-coin panels (kill-prone; checkpointed) ──────────────
    todo = [c for c in coins if c not in processed]
    if todo:
        with db_connection() as conn:
            for sym in todo:
                try:
                    df = load_one_panel(conn, sym)
                except BreadthFeatureError:
                    raise  # X-R1 column-contract break — must NOT be swallowed (flag prominently)
                except Exception as e:
                    conn.rollback()
                    print(f"  WARN {sym}: {type(e).__name__} {e}", flush=True)
                    df = None
                if df is not None and not df.empty:
                    store[sym] = panel_to_compact(df)
                processed.add(sym)
                rss = _rss_mb()
                if rss is not None:
                    peak_rss = max(peak_rss, rss)
                if len(processed) % args.progress_every == 0:
                    av = _avail_mb()
                    msg = f"  ...{len(processed)}/{len(coins)} coins, {len(store)} panels, peak_rss={peak_rss:.0f}MB"
                    if av is not None:
                        msg += f" avail={av:.0f}MB"
                    print(msg, flush=True)
                    if av is not None and av < MIN_AVAIL_MB:
                        persist()
                        print(f"ABORT: {av:.0f} MB free (< {MIN_AVAIL_MB}) — state saved, resume later.")
                        return 2
                if len(processed) % args.checkpoint_every == 0:
                    persist()
                    print(f"  checkpoint written at {len(processed)} coins ({len(store)} panels)", flush=True)
        persist()
    print(f"panel load complete: {len(store)} panels of {len(coins)} coins", flush=True)

    # ── Phase 2: build cross-section + analyze (fast; re-entrant on resume) ────
    panels = panels_from_store(store)
    panel = build_breadth_panel(panels)
    print(f"breadth panel: {len(panel)} daily rows", flush=True)

    with db_connection() as conn:
        regime_raw = load_regime_history(conn, args.max_regime_rows)
    print(f"regime_history rows: {len(regime_raw)}", flush=True)

    # (a) RUB-LONG
    if not os.path.exists(args.replay_path):
        print(f"WARNING: replay not found at {args.replay_path} — part (a) skipped", flush=True)
        events = pd.DataFrame()
    else:
        events = stream_rub_long_events(args.replay_path, args.max_events)
        print(f"streamed {len(events)} RUB-LONG events", flush=True)
    if not events.empty:
        events = attach_breadth(events, panel, "signal_time_utc")
        events = attach_regime_asof(events, regime_raw, "signal_time_utc")
        rub = analyze_rub(events)
    else:
        rub = {
            "n_events": 0,
            "n_with_breadth": 0,
            "overall": {"avg_net_pnl_pct": None, "wr": None},
            "months": {},
            "feature_gradient": {},
            "feature_terciles": {},
            "incremental_logit_vs_regime": None,
        }

    # (b) regime_history
    if not regime_raw.empty:
        regime_raw = attach_breadth(regime_raw, panel, "ts_utc")
        regime = analyze_regime(regime_raw)
    else:
        regime = {
            "n_rows": 0,
            "n_usable": 0,
            "regime_counts": {},
            "per_feature_auc_trend_up": {},
            "incremental_logit": None,
            "note": "regime_history empty",
        }

    verdict = derive_verdict(rub, regime)

    meta = {
        "study": "K6 · BRD (market breadth/dispersion)",
        "task": "T-2026-CU-9050-140",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "complete" if not args.limit_symbols else "partial (sampling cap)",
        "limit_symbols": args.limit_symbols,
        "max_events": args.max_events,
        "max_regime_rows": args.max_regime_rows,
        "n_universe": len(universe),
        "n_symbols_loaded": len(panels),
        "panel_rows": int(len(panel)),
        "panel_span": f"{panel.index.min()} .. {panel.index.max()}" if len(panel) else "n/a",
        "peak_rss_mb": round(peak_rss, 1),
        "skip_cpu_check": args.skip_cpu_check,
        "breadth_features": BREADTH_FEATURES,
    }

    out = {"meta": meta, "verdict": verdict, "rub_long": rub, "regime_history": regime}
    write_outputs(out, build_markdown(meta, rub, regime, verdict), json_path, md_path)

    # Clean completion → drop the transient resume-state so a later run starts fresh.
    try:
        if os.path.exists(state_path):
            os.remove(state_path)
    except OSError:
        pass

    print(f"\nVERDICT: {verdict['verdict']}")
    print(
        f"coins_loaded={len(panels)}/{len(universe)} rub_events={rub['n_events']} "
        f"headtohead_auc_delta={verdict['rub_headtohead_auc_delta_test']} "
        f"oos_stable_features={verdict['n_oos_stable_features']} peak_rss={peak_rss:.0f}MB"
    )
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

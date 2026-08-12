"""T-2026-KYT-9050-140 — stage-2 per-position hazard gate for the trailing books.

Pre-registration: docs/T-2026-KYT-9050-140-trailing-hazard-gate-study.md (committed
before any outcome). Reuses the T-139 replay conventions from
tools/trailing_exit_gate_study.py and the T-110-validated vol builder from
core/vol_features.py — no reimplementation of either.

Subcommands:

    python -m tools.trailing_hazard_gate_study pull5m --data <dir>
        Adds 5m closes for the book symbols to an existing T-139 snapshot directory
        (chunked hypertable pulls with lower time bounds). Read-only.

    python -m tools.trailing_hazard_gate_study replay --data <dir> [--json <file>]
        Offline: build per-instant features, train the FIT-only logistic hazard model,
        select (scope, theta) on FIT, evaluate the winner ONCE on the holdout.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import timedelta, timezone

import numpy as np
import pandas as pd

from core.trailing_state import mark_pct
from core.vol_features import VOL_WINDOW_5M, rolling_std_pct
from tools.trailing_exit_gate_study import (
    FIT_CUTOFF_UTC,
    PULL_START_UTC,
    _mark_lookup,
    exit_mix,
    paired_stats,
    tape_down_series,
    tape_state_at,
)

# ── Pre-registered constants (mirror the doc; never tune here) ────────────────

INSTANT_CAP = 72  # first 72 hourly instants per trade
THETA_GRID = (0.5, 0.6, 0.7, 0.8, 0.9)
SCOPES = ("ALL", "LONG-only")
L2_LAMBDA = 1e-3
GD_LR = 0.1
GD_ITERS = 500
FEATURE_NAMES = ("mark_pct", "drawdown_from_peak", "hours_in_trade", "vol_4h", "is_long", "btc_td1")


# ── Pure logic (DB-free — covered by backtest/test_trailing_hazard_gate_study.py) ──


def build_instant_features(
    filled_at: pd.Timestamp,
    closed_at: pd.Timestamp,
    entry: float,
    is_long: bool,
    tape: pd.DataFrame,
    marks_at_hours: pd.Series,
    vol_at: VolSeries,
) -> pd.DataFrame:
    """Per-instant feature rows for one trade. Only information ≤ instant is used.

    Instants: the fill plus the first INSTANT_CAP hourly BTC candle closes strictly
    inside (filled_at, closed_at). An hourly instant without a mark is skipped (the
    gate cannot evaluate there); a NaN vol keeps the row but the caller must never
    let it fire (a bot must not act on a missing feature).
    """
    rows = []
    peak = 0.0
    vol_fill = vol_at.value_at(filled_at)
    rows.append(
        {
            "instant": filled_at,
            "at_fill": True,
            "mark_price": entry,
            "mark_pct": 0.0,
            "drawdown_from_peak": 0.0,
            "hours_in_trade": 0.0,
            "vol_4h": vol_fill,
            "is_long": 1.0 if is_long else 0.0,
            "btc_td1": 1.0 if tape_state_at(tape, filled_at, "TD1") else 0.0,
        }
    )
    lo = tape["close_time"].searchsorted(filled_at, side="right")
    hourly_used = 0
    for i in range(lo, len(tape)):
        if hourly_used >= INSTANT_CAP:
            break
        instant = tape["close_time"].iloc[i]
        if instant >= closed_at:
            break
        mark = marks_at_hours.get(instant)
        if mark is None or (isinstance(mark, float) and math.isnan(mark)):
            continue
        m_pct = mark_pct(entry, float(mark), is_long)
        peak = max(peak, m_pct)
        rows.append(
            {
                "instant": instant,
                "at_fill": False,
                "mark_price": float(mark),
                "mark_pct": m_pct,
                "drawdown_from_peak": peak - m_pct,
                "hours_in_trade": (instant - filled_at) / pd.Timedelta(hours=1),
                "vol_4h": vol_at.value_at(instant),
                "is_long": 1.0 if is_long else 0.0,
                "btc_td1": 1.0 if tape.iloc[i]["TD1"] else 0.0,
            }
        )
        hourly_used += 1
    return pd.DataFrame(rows)


class VolSeries:
    """As-of lookup into the shared rolling vol of one symbol's 5m closes.

    ``values[i]`` is core.vol_features.rolling_std_pct at candle i; a candle is
    usable at instant t only once CLOSED (open_time + 5m ≤ t) — hard rule 5.
    """

    def __init__(self, open_times: pd.DatetimeIndex, closes: np.ndarray):
        self.close_times = open_times + pd.Timedelta(minutes=5)
        self.values = rolling_std_pct(closes.astype(float), VOL_WINDOW_5M)

    def value_at(self, instant: pd.Timestamp) -> float:
        idx = int(self.close_times.searchsorted(instant, side="right")) - 1
        if idx < 0:
            return float("nan")
        return float(self.values[idx])


class EmptyVolSeries(VolSeries):
    def __init__(self) -> None:  # noqa: D107 — sentinel for symbols without 5m data
        self.close_times = pd.DatetimeIndex([], tz=timezone.utc)
        self.values = np.array([])


def train_logistic(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Deterministic full-batch logistic regression on standardized features.

    Returns (weights incl. intercept at index 0, feature means, feature stds).
    L2 on the non-intercept weights only.
    """
    mu = x.mean(axis=0)
    sd = x.std(axis=0)
    sd[sd == 0] = 1.0
    xs = (x - mu) / sd
    xb = np.hstack([np.ones((len(xs), 1)), xs])
    w = np.zeros(xb.shape[1])
    for _ in range(GD_ITERS):
        p = 1.0 / (1.0 + np.exp(-xb @ w))
        grad = xb.T @ (p - y) / len(y)
        grad[1:] += L2_LAMBDA * w[1:]
        w -= GD_LR * grad
    return w, mu, sd


def predict_proba(w: np.ndarray, mu: np.ndarray, sd: np.ndarray, x: np.ndarray) -> np.ndarray:
    xs = (x - mu) / sd
    xb = np.hstack([np.ones((len(xs), 1)), xs])
    return 1.0 / (1.0 + np.exp(-xb @ w))


def auc_score(labels: np.ndarray, scores: np.ndarray) -> float:
    """Rank-based AUC (Mann-Whitney), NaN-free inputs expected."""
    pos = scores[labels == 1]
    neg = scores[labels == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    ranks = np.empty(len(order))
    ranks[order] = np.arange(1, len(order) + 1)
    r_pos = ranks[: len(pos)].sum()
    return (r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def first_fire_index(probs: np.ndarray, vols: np.ndarray, theta: float) -> int | None:
    """Index of the first instant with P > theta; NaN-vol instants can never fire."""
    for i in range(len(probs)):
        if math.isnan(vols[i]):
            continue
        if probs[i] > theta:
            return i
    return None


# ── Snapshot extension (live PG, read-only) ───────────────────────────────────


def cmd_pull5m(data_dir: str) -> None:
    from datetime import datetime

    from core.database import get_db_connection

    positions = pd.read_pickle(os.path.join(data_dir, "positions.pkl"))
    symbols = sorted(positions["symbol"].unique().tolist())
    hi = datetime.now(timezone.utc)
    conn = get_db_connection()
    try:
        frames = []
        lo = PULL_START_UTC - timedelta(minutes=5 * (VOL_WINDOW_5M + 2))
        while lo < hi:
            chunk_hi = min(lo + timedelta(days=2), hi)
            frames.append(
                pd.read_sql(
                    """SELECT symbol, open_time, close FROM candles
                       WHERE tf='5m' AND is_closed AND symbol = ANY(%(syms)s)
                         AND open_time >= %(lo)s AND open_time < %(hi)s
                       ORDER BY symbol, open_time""",
                    conn,
                    params={"syms": symbols, "lo": lo, "hi": chunk_hi},
                )
            )
            lo = chunk_hi
        closes_5m = pd.concat(frames, ignore_index=True)
        closes_5m.to_pickle(os.path.join(data_dir, "symbol_5m_closes.pkl"))
        print(f"pulled: {len(closes_5m)} 5m closes for {len(symbols)} symbols -> {data_dir}")
    finally:
        conn.close()


# ── Replay (offline) ──────────────────────────────────────────────────────────


def cmd_replay(data_dir: str, json_out: str | None) -> None:
    positions = pd.read_pickle(os.path.join(data_dir, "positions.pkl"))
    btc = pd.read_pickle(os.path.join(data_dir, "btc_1h.pkl"))
    closes = pd.read_pickle(os.path.join(data_dir, "symbol_1h_closes.pkl"))
    ticks = pd.read_pickle(os.path.join(data_dir, "hourly_first_ticks.pkl"))
    closes_5m = pd.read_pickle(os.path.join(data_dir, "symbol_5m_closes.pkl"))

    for col in ("filled_at", "closed_at"):
        positions[col] = pd.to_datetime(positions[col], utc=True)
    btc["open_time"] = pd.to_datetime(btc["open_time"], utc=True)
    closes["open_time"] = pd.to_datetime(closes["open_time"], utc=True)
    ticks["hour"] = pd.to_datetime(ticks["hour"], utc=True)
    closes_5m["open_time"] = pd.to_datetime(closes_5m["open_time"], utc=True)

    tape = tape_down_series(btc)
    mark_lookups = {sym: _mark_lookup(sym, ticks, closes) for sym in positions["symbol"].unique()}
    vol_lookups: dict[str, VolSeries] = {}
    for sym, grp in closes_5m.groupby("symbol"):
        grp = grp.sort_values("open_time")
        vol_lookups[sym] = VolSeries(pd.DatetimeIndex(grp["open_time"]), grp["close"].to_numpy())
    empty_vol = EmptyVolSeries()

    fit_mask = (positions["book"] == "bot40") & (positions["filled_at"] < FIT_CUTOFF_UTC)

    # Per-trade feature frames, built once (variant loops only re-threshold them).
    feats: list[pd.DataFrame] = []
    for pos in positions.itertuples():
        f = build_instant_features(
            pos.filled_at,
            pos.closed_at,
            float(pos.entry),
            pos.direction == "LONG",
            tape,
            mark_lookups.get(pos.symbol, pd.Series(dtype=float)),
            vol_lookups.get(pos.symbol, empty_vol),
        )
        feats.append(f)

    n_no_vol = sum(int(f["vol_4h"].isna().sum()) for f in feats)
    n_rows = sum(len(f) for f in feats)
    print(f"positions {len(positions)} · instant rows {n_rows} · NaN-vol rows {n_no_vol}")

    # FIT training set: instant rows of FIT trades, NaN-vol dropped, terminal label.
    x_parts, y_parts = [], []
    for f, pos, in_fit in zip(feats, positions.itertuples(), fit_mask, strict=True):
        if not in_fit:
            continue
        ok = f.dropna(subset=["vol_4h"])
        if ok.empty:
            continue
        x_parts.append(ok[list(FEATURE_NAMES)].to_numpy(dtype=float))
        y_parts.append(np.full(len(ok), 1.0 if float(pos.close_mark_pct) < 0 else 0.0))
    x_fit = np.vstack(x_parts)
    y_fit = np.concatenate(y_parts)
    w, mu, sd = train_logistic(x_fit, y_fit)
    print(
        f"trained on {len(x_fit)} FIT rows (bad share {y_fit.mean():.3f}); "
        f"weights: " + ", ".join(f"{n}={v:+.3f}" for n, v in zip(("bias",) + FEATURE_NAMES, w, strict=True))
    )

    # Score every trade's instants once with the frozen model.
    probs: list[np.ndarray] = []
    for f in feats:
        x = f[list(FEATURE_NAMES)].to_numpy(dtype=float)
        x_safe = np.nan_to_num(x, nan=0.0)
        p = predict_proba(w, mu, sd, x_safe)
        probs.append(p)

    # Variant replay: gate fires at the first NaN-free instant with P > theta.
    per_cell: dict[str, pd.DataFrame] = {}
    for scope in SCOPES:
        for theta in THETA_GRID:
            rows = []
            for f, p, pos in zip(feats, probs, positions.itertuples(), strict=True):
                delta = 0.0
                gate_pct = None
                cf_reason = pos.close_reason
                in_scope = scope == "ALL" or pos.direction == "LONG"
                if in_scope and len(f):
                    idx = first_fire_index(p, f["vol_4h"].to_numpy(dtype=float), theta)
                    if idx is not None:
                        row = f.iloc[idx]
                        gate_pct = 0.0 if bool(row["at_fill"]) else float(row["mark_pct"])
                        delta = gate_pct - float(pos.close_mark_pct)
                        cf_reason = "GATE"
                rows.append(
                    {
                        "id": pos.id,
                        "book": pos.book,
                        "direction": pos.direction,
                        "close_reason": pos.close_reason,
                        "actual_pct": float(pos.close_mark_pct),
                        "gate_pct": gate_pct,
                        "cf_reason": cf_reason,
                        "delta": delta,
                    }
                )
            per_cell[f"{scope}/theta={theta}"] = pd.DataFrame(rows)

    results: dict[str, dict] = {"fit": {}, "holdout": None}
    for key, df in per_cell.items():
        stats = paired_stats(df.loc[fit_mask.to_numpy(), "delta"].to_numpy())
        results["fit"][key] = stats
        print(f"FIT {key}: n={stats['n']} sum={stats['sum']:+.1f} mean={stats['mean']:+.4f} t={stats['t']:+.2f}")

    best_sum = max(s["sum"] for s in results["fit"].values())
    winner = max(
        results["fit"],
        key=lambda k: (results["fit"][k]["sum"], float(k.split("theta=")[1])),
    )
    print(f"\nFIT winner: {winner} (best sum {best_sum:+.1f}) — evaluated ONCE on holdout:")

    dfw = per_cell[winner]
    hold = dfw.loc[~fit_mask.to_numpy()]
    all_stats = paired_stats(hold["delta"].to_numpy())
    by_book = {book: paired_stats(hold.loc[hold["book"] == book, "delta"].to_numpy()) for book in ("bot40", "bot44")}
    results["holdout"] = {"variant": winner, "all": all_stats, "by_book": by_book}
    print(
        f"HOLDOUT all: n={all_stats['n']} sum={all_stats['sum']:+.1f} mean={all_stats['mean']:+.4f} t={all_stats['t']:+.2f}"
    )
    for book, s in by_book.items():
        print(f"  {book}: n={s['n']} sum={s['sum']:+.1f} mean={s['mean']:+.4f} t={s['t']:+.2f}")

    # Reported, not verdict-bearing: holdout instant-level AUC + killed TRAIL winners.
    auc_labels, auc_scores = [], []
    for f, p, pos, in_fit in zip(feats, probs, positions.itertuples(), fit_mask, strict=True):
        if in_fit:
            continue
        ok = ~f["vol_4h"].isna().to_numpy()
        if ok.any():
            auc_labels.append(np.full(int(ok.sum()), 1.0 if float(pos.close_mark_pct) < 0 else 0.0))
            auc_scores.append(p[ok])
    auc = auc_score(np.concatenate(auc_labels), np.concatenate(auc_scores))
    killed_trail = int(((hold["cf_reason"] == "GATE") & (hold["close_reason"] == "TRAIL")).sum())
    results["holdout"]["auc_instant_level"] = auc
    results["holdout"]["killed_trail_winners"] = killed_trail
    print(f"holdout AUC (instant level): {auc:.3f} · TRAIL winners killed: {killed_trail} (stage 1: 745)")

    print("\nHOLDOUT exit-mix ACTUAL (booked):")
    print(exit_mix(hold, "actual_pct", "close_reason").to_string())
    cf = hold.copy()
    cf["cf_pct"] = np.where(cf["cf_reason"] == "GATE", cf["gate_pct"], cf["actual_pct"])
    print("\nHOLDOUT exit-mix COUNTERFACTUAL:")
    print(exit_mix(cf, "cf_pct", "cf_reason").to_string())

    if json_out:
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"results written to {json_out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_pull = sub.add_parser("pull5m")
    p_pull.add_argument("--data", required=True)
    p_rep = sub.add_parser("replay")
    p_rep.add_argument("--data", required=True)
    p_rep.add_argument("--json")
    args = ap.parse_args()
    if args.cmd == "pull5m":
        cmd_pull5m(args.data)
    else:
        cmd_replay(args.data, args.json)


if __name__ == "__main__":
    main()

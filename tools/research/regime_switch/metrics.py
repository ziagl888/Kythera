"""metrics.py — whipsaw / dwell / TREND-hold / separation, vocabulary-agnostic.

All metrics run on the COMMON window (the intersection where every variant is
non-None, i.e. past the slowest warmup) so A/B/C/D are strictly apples-to-apples.

Separation timing follows the Stoic/GARCH convention: a state observed at close of
candle t is scored against the return of candle t+1 (no same-bar fill). A good
regime filter makes the per-state forward-return distributions genuinely
different — quantified by eta² (between-state share of total variance).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_CANDLES_PER_HOUR_15M = 4
_CANDLES_PER_30D = 30 * 96
_CANDLES_PER_YEAR = 365 * 96

# Which labels count as "trending / directional" per vocabulary.
TREND_STATES = {"TREND_UP", "TREND_DOWN", "BULL", "BEAR"}


def common_index(series: dict[str, pd.Series]) -> pd.Index:
    """Index where every variant has a non-None label (past all warmups)."""
    idx = None
    for s in series.values():
        valid = s[s.notna()].index
        idx = valid if idx is None else idx.intersection(valid)
    return idx if idx is not None else pd.Index([])


def _runs(labels: list) -> list[tuple[object, int]]:
    """Run-length encode a label list → [(label, length), ...]."""
    out: list[tuple[object, int]] = []
    for lab in labels:
        if out and out[-1][0] == lab:
            out[-1] = (lab, out[-1][1] + 1)
        else:
            out.append((lab, 1))
    return out


def whipsaw_metrics(labels: pd.Series) -> dict:
    """Switch frequency + dwell-time distribution for one label series."""
    vals = list(labels.values)
    n = len(vals)
    runs = _runs(vals)
    switches = len(runs) - 1 if runs else 0
    dwell = np.array([ln for _, ln in runs], dtype=float)
    return {
        "n_candles": n,
        "n_switches": switches,
        "switches_per_30d": round(switches / (n / _CANDLES_PER_30D), 2) if n else None,
        "mean_dwell_hours": round(float(dwell.mean()) / _CANDLES_PER_HOUR_15M, 2) if len(dwell) else None,
        "median_dwell_hours": round(float(np.median(dwell)) / _CANDLES_PER_HOUR_15M, 2) if len(dwell) else None,
        "n_episodes": len(runs),
        "pct_episodes_under_1h": round(100 * float((dwell < _CANDLES_PER_HOUR_15M).mean()), 1) if len(dwell) else None,
    }


def trend_hold_metrics(labels: pd.Series, trend_states: set[str] = TREND_STATES) -> dict:
    """TREND-hold defect: time in directional states, episode length, <1h share —
    the exact defect the regime_logic comment cites ('34% der TREND-Episoden <1h')."""
    vals = list(labels.values)
    n = len(vals)
    runs = _runs(vals)
    trend_runs = [ln for lab, ln in runs if lab in trend_states]
    tr = np.array(trend_runs, dtype=float)
    n_trend_candles = int(tr.sum()) if len(tr) else 0
    return {
        "pct_time_trend": round(100 * n_trend_candles / n, 1) if n else None,
        "n_trend_episodes": len(trend_runs),
        "mean_trend_episode_hours": round(float(tr.mean()) / _CANDLES_PER_HOUR_15M, 2) if len(tr) else None,
        "median_trend_episode_hours": round(float(np.median(tr)) / _CANDLES_PER_HOUR_15M, 2) if len(tr) else None,
        "n_trend_episodes_under_1h": int((tr < _CANDLES_PER_HOUR_15M).sum()) if len(tr) else 0,
        "pct_trend_episodes_under_1h": round(100 * float((tr < _CANDLES_PER_HOUR_15M).mean()), 1) if len(tr) else None,
    }


# The regime is a 4h-return construct; its separation must be judged over a
# horizon that matches that timescale, not the next 15m candle (near-pure noise).
# Primary = 4h; 1h/24h reported alongside. Forward returns over H>1 overlap per
# candle → eta² stays a valid descriptive variance-share (all variants share the
# scheme), but the per-state Sharpe is optimistic on overlap (documented).
PRIMARY_HORIZON_CANDLES = 16  # 4h
HORIZONS = {"1h": 4, "4h": 16, "24h": 96}


def _separation_at(labels: pd.Series, price: pd.Series, horizon: int) -> dict:
    px = price.reindex(labels.index).astype(float)
    fwd = (px.shift(-horizon) / px - 1.0) * 100.0
    df = pd.DataFrame({"label": labels.values, "fwd": fwd.values}).dropna()
    if df.empty:
        return {"eta_squared": None, "per_state": {}}
    periods_per_year = _CANDLES_PER_YEAR / horizon
    grand = df["fwd"].mean()
    ss_total = float(((df["fwd"] - grand) ** 2).sum())
    ss_between = 0.0
    per_state: dict[str, dict] = {}
    for lab, g in df.groupby("label"):
        r = g["fwd"].to_numpy()
        ss_between += len(r) * (r.mean() - grand) ** 2
        vol = float(r.std())
        per_state[str(lab)] = {
            "n": int(len(r)),
            "pct_of_time": round(100 * len(r) / len(df), 1),
            "mean_fwd_pct": round(float(r.mean()), 4),
            "ann_return_pct": round(float(r.mean()) * periods_per_year, 1),
            "ann_vol_pct": round(vol * np.sqrt(periods_per_year), 1),
            "sharpe": round(float(r.mean()) / vol * np.sqrt(periods_per_year), 2) if vol > 0 else None,
        }
    eta2 = ss_between / ss_total if ss_total > 0 else 0.0
    return {"eta_squared": round(float(eta2), 5), "per_state": per_state}


def separation_metrics(labels: pd.Series, price: pd.Series) -> dict:
    """Per-state forward-return separation + eta² at the primary 4h horizon, with
    1h/24h eta² alongside. eta² = between-state share of total forward-return
    variance — higher = the regime label explains more of forward return."""
    primary = _separation_at(labels, price, PRIMARY_HORIZON_CANDLES)
    primary["horizon"] = "4h"
    primary["eta_squared_by_horizon"] = {
        name: _separation_at(labels, price, h)["eta_squared"] for name, h in HORIZONS.items()
    }
    return primary


def variant_report(name: str, labels: pd.Series, price: pd.Series) -> dict:
    return {
        "variant": name,
        "whipsaw": whipsaw_metrics(labels),
        "trend_hold": trend_hold_metrics(labels),
        "separation": separation_metrics(labels, price),
    }

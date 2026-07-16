#!/usr/bin/env py -3.13
# tools/breadth_study.py — K6 · BRD (market breadth/dispersion) study (T-2026-CU-9050-140)
"""Read-only validation study for the shared breadth builder (core.breadth_features).

Question (docs/MODEL_CANDIDATES_SPEC_2026-07.md §K6): do market-breadth /
dispersion features over the ~530-coin USDT-perp universe (share of coins > EMA200/
EMA50, median-7d return, advance/decline, return-dispersion vs BTC, TOTAL3-proxy
level/regression/breakout) ADD information over the BTC-only regime — and do they
separate RUB-LONG outcomes? This script does not decide deployment; it produces the
evidence so the later FULL run (Ein-Job-deferred) can.

Three parts:
  (a) Breadth features as-of vs the forward outcome of the RUB-**LONG** events in
      _X/staging_models/replay/rub_replay_365d.jsonl (EXISTS, 36 MB — streamed line
      by line, no new sim run). ``net_pnl_pct`` / ``outcome_tp1`` are the already
      simulated per-trade outcomes; we correlate each breadth feature as-of
      ``signal_time`` with them (per-trade Spearman), month-split, top/bottom
      terciles.
  (b) Breadth features vs ``regime_history`` classes: a simple logit diagnostic —
      does adding breadth to a BTC-only feature set improve TREND_UP-vs-rest AUC on
      a chronological holdout? Per-feature single-variable AUC alongside.
  (c) Month-split throughout.

This run is a **SMOKE** (--limit-symbols / --max-events caps): it proves the code
imports, the builder emits features and the study runs end to end. The full-universe
report is deferred to the queue (Ein-Job-Regel — a second heavy study must not run
while another is live). The header of both artifacts says so.

READ-ONLY. SELECTs only, BELOW_NORMAL (tools.walkforward_sim.set_low_priority).
Artifacts to staging_models/ ONLY (repo Rule 2), never the repo root.

Contracts reused (no reinvention):
  * core.breadth_features — the shared X-R1 as-of builder (load_universe_panels →
    build_breadth_panel → breadth_features_asof). ONE query per coin.
  * core.time.LEGACY_WRITER_TZ = "Europe/Bucharest" — regime_history.ts is
    TIMESTAMP WITHOUT TIME ZONE = naive local Bucharest; localized DST-aware before
    the as-of join (a constant ±offset is wrong across the DST flip). RUB
    signal_time is naive UTC (it comes from candle open_time, a UTC instant) and is
    used as-is.
  * tools.walkforward_sim.set_low_priority / check_cpu_headroom — the fleet-safe
    priority + CPU-headroom guards used by the sibling studies.

Known bias (documented, not corrected): survivorship — coins.json / the per-coin
tables cover ACTIVE USDT-perps; delisted coins are partly missing, so every breadth
row is computed over a survivorship-skewed universe. The TOTAL3 index is a PRICE
proxy (no real market-cap weights) — see the builder docstring.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.breadth_features import (  # noqa: E402
    BREADTH_FEATURES,
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

TREND_UP = "TREND_UP"
BTC_REGIME_FEATURES = ["btc_return_1h", "btc_return_4h", "btc_atr_1h_pct", "btc_atr_4h_pct"]
MIN_ROWS_FOR_LOGIT = 200  # below this the AUC diagnostic is skipped (smoke-safe)


def load_coins(path: str = "coins.json") -> list[str]:
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as fh:
        coins = json.load(fh)
    if not isinstance(coins, list) or not coins:
        raise ValueError(f"{path} is not a non-empty list")
    return coins


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


# ─────────────────────────────────────────────────────────────────────────────
# (a) RUB-LONG events vs breadth as-of
# ─────────────────────────────────────────────────────────────────────────────
def stream_rub_long_events(path: str, max_events: int | None) -> pd.DataFrame:
    """Stream the replay jsonl, keep only RUB LONG events (signal_time, net_pnl_pct,
    outcome_tp1). The file is 36 MB — never fully materialized."""
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
    return df.dropna(subset=["signal_time_utc"]).reset_index(drop=True)


def attach_breadth(df: pd.DataFrame, panel: pd.DataFrame, ts_col: str) -> pd.DataFrame:
    """As-of breadth features for every row (O(log n) lookup into the daily panel)."""
    feat_rows = [breadth_features_asof(panel, ts) for ts in df[ts_col]]
    feats = pd.DataFrame(feat_rows, index=df.index)
    return pd.concat([df, feats], axis=1)


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
    """Bottom vs top tercile net-PnL expectancy for a breadth feature."""
    sub = df[df[feature].notna()]
    if len(sub) < 30:
        return {"n": int(len(sub)), "note": "too few points"}
    lo, hi = float(np.quantile(sub[feature], 1 / 3)), float(np.quantile(sub[feature], 2 / 3))
    bottom = sub[sub[feature] <= lo]
    top = sub[sub[feature] >= hi]

    def blk(g: pd.DataFrame) -> dict:
        return {
            "n": int(len(g)),
            "avg_net_pnl_pct": round(float(g["net_pnl_pct"].mean()), 4) if len(g) else None,
            "wr": round(float((g["net_pnl_pct"] > 0).mean()), 4) if len(g) else None,
        }

    return {"lo_edge": round(lo, 6), "hi_edge": round(hi, 6), "bottom_tercile": blk(bottom), "top_tercile": blk(top)}


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
# (b) regime_history diagnostic — does breadth add over BTC-only?
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


def _logit_auc(x_train, y_train, x_test, y_test) -> float | None:
    """Standardized logistic-regression holdout AUC. sklearn is available in the
    live venv; None on any failure (degenerate class, singular fit)."""
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
        result["note"] = (
            f"only {len(labelled)} usable rows (< {MIN_ROWS_FOR_LOGIT}); logit diagnostic skipped "
            "(SMOKE — full run will have the whole regime_history)."
        )
        # Still report single-feature AUC where possible (cheap, numpy-only).
        y = (labelled["regime"] == TREND_UP).astype(int).to_numpy() if len(labelled) else np.array([])
        for feat in present:
            if len(labelled):
                result["per_feature_auc_trend_up"][feat] = _r(rank_auc(labelled[feat].to_numpy(dtype=float), y))
        return result

    y = (labelled["regime"] == TREND_UP).astype(int).to_numpy()
    if int(y.sum()) == 0:
        result["note"] = (
            "no TREND_UP rows in the (smoke-capped) regime_history window — AUC undefined "
            "(single class); the full run over all regime_history includes TREND_UP."
        )
    for feat in present:
        result["per_feature_auc_trend_up"][feat] = _r(rank_auc(labelled[feat].to_numpy(dtype=float), y))

    # Chronological holdout: first 70 % train, last 30 % test.
    labelled = labelled.sort_values("ts_utc").reset_index(drop=True)
    cut = int(len(labelled) * 0.7)
    tr, te = labelled.iloc[:cut], labelled.iloc[cut:]
    y_tr = (tr["regime"] == TREND_UP).astype(int).to_numpy()
    y_te = (te["regime"] == TREND_UP).astype(int).to_numpy()
    auc_btc = _logit_auc(
        tr[BTC_REGIME_FEATURES].to_numpy(float), y_tr, te[BTC_REGIME_FEATURES].to_numpy(float), y_te
    )
    auc_both = _logit_auc(
        tr[BTC_REGIME_FEATURES + present].to_numpy(float),
        y_tr,
        te[BTC_REGIME_FEATURES + present].to_numpy(float),
        y_te,
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


def _r(v: float | None, nd: int = 4) -> float | None:
    return None if v is None else round(v, nd)


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────
def build_markdown(meta: dict, rub: dict, regime: dict) -> str:
    L: list[str] = []
    L.append("# K6 · BRD — market breadth/dispersion study (T-2026-CU-9050-140)\n")
    L.append("> **SMOKE — full run pending.** This artifact was produced with symbol/event caps to")
    L.append("> prove the builder + study run end to end. The full-universe report is deferred to the")
    L.append("> queue (Ein-Job-Regel: a second heavy study must not run while another is live).\n")
    L.append(f"_Generated {meta['generated_at']} · read-only · limit_symbols={meta['limit_symbols']} · ")
    L.append(f"max_events={meta['max_events']} · universe_loaded={meta['n_symbols_loaded']}_\n")
    L.append("Breadth is a PRICE proxy over active USDT-perps (survivorship-biased); TOTAL3 has no real")
    L.append("market-cap weights — see core/breadth_features docstring.\n")

    L.append("## Builder output — daily breadth panel\n")
    L.append(f"- panel rows (days): {meta['panel_rows']}")
    L.append(f"- panel span (UTC): {meta['panel_span']}")
    L.append(f"- features emitted: {', '.join(BREADTH_FEATURES)}\n")

    L.append("## (a) RUB-LONG events vs breadth as-of\n")
    L.append(f"- RUB LONG events streamed: {rub['n_events']} (with as-of breadth: {rub['n_with_breadth']})")
    L.append(f"- overall: avg net PnL {rub['overall']['avg_net_pnl_pct']}% · WR {rub['overall']['wr']}\n")
    L.append("### Per-feature gradient (Spearman vs net_pnl_pct; sign must survive the chrono split)\n")
    L.append("| feature | Spearman all | val | test |")
    L.append("|---|--:|--:|--:|")
    for feat, g in rub["feature_gradient"].items():
        L.append(f"| {feat} | {g['spearman_all']} | {g['spearman_val']} | {g['spearman_test']} |")
    L.append("")
    L.append("### Top vs bottom tercile net-PnL expectancy\n")
    L.append("| feature | bottom n | bottom PnL% | bottom WR | top n | top PnL% | top WR |")
    L.append("|---|--:|--:|--:|--:|--:|--:|")
    for feat, t in rub["feature_terciles"].items():
        if "bottom_tercile" not in t:
            continue
        b, tp = t["bottom_tercile"], t["top_tercile"]
        L.append(
            f"| {feat} | {b['n']} | {b['avg_net_pnl_pct']} | {b['wr']} "
            f"| {tp['n']} | {tp['avg_net_pnl_pct']} | {tp['wr']} |"
        )
    L.append("")

    L.append("## (b) regime_history diagnostic — does breadth add over BTC-only?\n")
    L.append(f"- regime rows: {regime['n_rows']} · usable (breadth+BTC non-NaN): {regime['n_usable']}")
    if regime.get("note"):
        L.append(f"- NOTE: {regime['note']}")
    inc = regime.get("incremental_logit")
    if inc:
        L.append(
            f"- incremental logit ({inc['target']}, {inc['split']}, n_test={inc['n_test']}): "
            f"AUC BTC-only={inc['auc_btc_only']} → BTC+breadth={inc['auc_btc_plus_breadth']} "
            f"(Δ={inc['auc_delta']})"
        )
    L.append("\n### Single-feature AUC (TREND_UP vs rest)\n")
    L.append("| feature | AUC |")
    L.append("|---|--:|")
    for feat, a in regime["per_feature_auc_trend_up"].items():
        L.append(f"| {feat} | {a} |")
    L.append("")

    L.append("## Caveats\n")
    L.append("- **SMOKE run**: caps make the numbers non-decisive; the stop-criterion verdict (§K6) is")
    L.append("  the FULL run's job. The builder stays as infra regardless of the study outcome.")
    L.append("- **Survivorship**: breadth computed over active USDT-perps only; delisted coins missing.")
    L.append("- **TOTAL3 is a price proxy** (equal- and volume-weighted over perps ex BTC/ETH), not a")
    L.append("  market-cap index — the level/regression/breakout are proxy-relative.")
    L.append("- RUB signal_time is naive UTC; regime_history.ts is naive Bucharest → localized DST-aware.")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="K6 · BRD breadth/dispersion study (read-only, SMOKE-capable).")
    ap.add_argument("--limit-symbols", type=int, default=None, help="Smoke: cap the universe to the first N coins.")
    ap.add_argument("--max-events", type=int, default=None, help="Smoke: cap RUB-LONG events streamed.")
    ap.add_argument("--max-regime-rows", type=int, default=None, help="Smoke: cap regime_history rows.")
    ap.add_argument("--replay-path", default=DEFAULT_REPLAY, help="Path to rub_replay_365d.jsonl.")
    ap.add_argument(
        "--skip-cpu-check",
        action="store_true",
        help="Skip the fleet CPU-headroom guard (ONLY for a deliberate tiny operator smoke under known load).",
    )
    args = ap.parse_args()

    set_low_priority()
    if not args.skip_cpu_check:
        check_cpu_headroom()
    os.makedirs(OUT_DIR, exist_ok=True)

    coins = load_coins()
    if args.limit_symbols:
        coins = coins[: args.limit_symbols]
    print(f"universe: {len(coins)} coins (limit_symbols={args.limit_symbols})", flush=True)

    with db_connection() as conn:
        panels = load_universe_panels(conn, coins, tf="1d")
        print(f"loaded {len(panels)} panels (one query per coin)", flush=True)
        panel = build_breadth_panel(panels)
        regime_raw = load_regime_history(conn, args.max_regime_rows)

    print(f"breadth panel: {len(panel)} daily rows", flush=True)

    # (a) RUB-LONG
    if not os.path.exists(args.replay_path):
        print(f"WARNING: replay not found at {args.replay_path} — part (a) skipped", flush=True)
        events = pd.DataFrame()
    else:
        events = stream_rub_long_events(args.replay_path, args.max_events)
        print(f"streamed {len(events)} RUB-LONG events", flush=True)
    if not events.empty:
        events = attach_breadth(events, panel, "signal_time_utc")
        rub = analyze_rub(events)
    else:
        rub = {
            "n_events": 0,
            "n_with_breadth": 0,
            "overall": {"avg_net_pnl_pct": None, "wr": None},
            "months": {},
            "feature_gradient": {},
            "feature_terciles": {},
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

    meta = {
        "study": "K6 · BRD (market breadth/dispersion)",
        "task": "T-2026-CU-9050-140",
        "mode": "SMOKE — full run pending (Ein-Job deferred)",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "limit_symbols": args.limit_symbols,
        "max_events": args.max_events,
        "max_regime_rows": args.max_regime_rows,
        "n_symbols_loaded": len(panels),
        "panel_rows": int(len(panel)),
        "panel_span": f"{panel.index.min()} .. {panel.index.max()}" if len(panel) else "n/a",
        "breadth_features": BREADTH_FEATURES,
    }

    out = {"meta": meta, "rub_long": rub, "regime_history": regime}
    json_path = os.path.join(OUT_DIR, "breadth_study.json")
    md_path = os.path.join(OUT_DIR, "breadth_study.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(build_markdown(meta, rub, regime))

    print("SMOKE — full run pending")
    print(f"panel_rows={meta['panel_rows']} n_symbols={meta['n_symbols_loaded']} rub_events={rub['n_events']}")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

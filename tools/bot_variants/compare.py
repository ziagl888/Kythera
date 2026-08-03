#!/usr/bin/env python3
# tools/bot_variants/compare.py — Generation A/B sim harness (T-2026-KYT-9050-039, D3).
#
# PURPOSE: Compare two bot generations head-to-head over the EXISTING, DB-free
# replay infra (Generation A vs Generation B on the SAME events).
# Does NOT rebuild the sim: the labels/PnL come from an already-generated
# `*_replay_*.jsonl` (tools/retrain_from_replay.load_replay — the same loader
# that also feeds the retrains), the generation only supplies the scoring model.
#
# Scope boundary: tools/walkforward_sim.py GENERATES the replays from the live DB
# (DB-bound, VPS-only). compare.py CONSUMES them (DB-free) — it only loads a
# replay JSONL + the artifact pkls and computes comparative metrics.
#
# Metrics per generation (on the replay, at the respective operating threshold):
#   n · avg_net_pnl_pct · sum_net_pnl_pct · win_rate · max_drawdown_pct.
#
# Invariants:
#   * READ-ONLY, DB-free, no network. No writes (only --out JSON optional).
#   * Scoring contract identical to the live/shadow path: raw predict_proba[:,1] on
#     the artifact's feature order (core.shadow_gate.score_artifact semantics).
#   * Threshold semantics like live: prob >= optimal_threshold; threshold=None ⇒
#     emission on EVERY event (detector is the gate) — a faithful preview.

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.bot_variants import index as ix  # noqa: E402
from tools.retrain_from_replay import load_replay  # noqa: E402

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Load artifact contract (generic: dict artifact, xgb-json, bare classifier)
# ─────────────────────────────────────────────────────────────────────────────
def _features_from_bare_model(model: Any) -> list[str] | None:
    """Feature names from a bare estimator (booster / sklearn)."""
    booster = getattr(model, "get_booster", None)
    if callable(booster):
        try:
            names = booster().feature_names
            if names:
                return list(names)
        except Exception:  # pragma: no cover - defensive
            pass
    names_in = getattr(model, "feature_names_in_", None)
    if names_in is not None:
        return list(names_in)
    return None


def load_contract(path: str) -> dict[str, Any]:
    """Loads an artifact into a lean scoring contract {model, features, threshold}.

    Covers the three fleet formats: .json (native XGB + *_meta.json sidecar),
    .pkl/.joblib as dict (retrain_from_replay: model/features/optimal_threshold)
    and .pkl as a bare estimator (legacy — features from the booster). Raises
    ValueError if no feature contract can be derived (no fair scoring without
    features)."""
    if not os.path.isfile(path):
        raise ValueError(f"Artifact not found: {path}")
    if path.endswith(".json"):
        import xgboost as xgb

        model = xgb.XGBClassifier()
        model.load_model(path)
        meta_path = os.path.splitext(path)[0] + "_meta.json"
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        features = list(meta["features"])
        return {"model": model, "features": features, "threshold": meta.get("optimal_threshold")}

    import joblib

    art = joblib.load(path)
    if isinstance(art, dict) and "model" in art:
        features = list(art.get("features") or [])
        if not features:
            raise ValueError(f"Artifact {path} without feature list — no scoring contract.")
        return {"model": art["model"], "features": features, "threshold": art.get("optimal_threshold")}

    features = _features_from_bare_model(art)
    if not features:
        raise ValueError(f"Bare estimator {path} without feature names — feature contract not derivable.")
    return {"model": art, "features": features, "threshold": None}


def resolve_artifact_path(tag: str, direction: str, index: dict[str, Any] | None = None) -> str:
    """Location path of the artifact for a (tag, direction) from the index.

    ``index`` lets the caller build the (expensive) FS scan ONCE and
    reuse it for both generations."""
    if index is None:
        index = ix.build_index(load_embedded=False)
    norm = tag.strip().upper()
    for gen in index["generations"]:
        if gen["tag"].upper() != norm:
            continue
        for art in gen["artifacts"]:
            if art["direction"] == direction.upper() and art["exists"] and art["path"]:
                return os.path.join(ix.REPO_ROOT, art["path"])
        raise ValueError(f"{tag}/{direction}: no artifact present in the index.")
    raise ValueError(f"Unknown generation '{tag}'.")


# ─────────────────────────────────────────────────────────────────────────────
# Scoring + metrics (DB-free, on a loaded replay DataFrame)
# ─────────────────────────────────────────────────────────────────────────────
def score(contract: dict[str, Any], replay: pd.DataFrame) -> np.ndarray:
    """Raw predict_proba[:,1] on the artifact's feature order."""
    features = contract["features"]
    X = replay.reindex(columns=features).fillna(0)
    return contract["model"].predict_proba(X)[:, 1].astype(float)


def _max_drawdown_pct(pnl_series: np.ndarray) -> float:
    """Max drawdown of the cumulative net PnL curve (in PnL-%-points, <= 0)."""
    if pnl_series.size == 0:
        return 0.0
    cum = np.cumsum(pnl_series)
    running_max = np.maximum.accumulate(cum)
    drawdown = cum - running_max  # <= 0
    return round(float(drawdown.min()), 4)


def evaluate(contract: dict[str, Any], replay: pd.DataFrame, threshold: float | None = None) -> dict[str, Any]:
    """Metrics for one generation on the replay at the operating threshold.

    threshold override > contract['threshold'] > None. None ⇒ every event counts
    (detector gate, as a non-deployable generation would emit live).
    replay must be sorted chronologically (load_replay provides that)."""
    probs = score(contract, replay)
    thr = threshold if threshold is not None else contract.get("threshold")
    mask = probs >= thr if thr is not None else np.ones(len(replay), dtype=bool)
    sel = replay.loc[mask]
    n = int(mask.sum())
    if n == 0:
        return {
            "threshold": thr,
            "n": 0,
            "avg_net_pnl_pct": None,
            "sum_net_pnl_pct": 0.0,
            "win_rate": None,
            "max_drawdown_pct": 0.0,
        }
    pnl = sel["net_pnl_pct"].to_numpy(dtype=float)
    return {
        "threshold": thr,
        "n": n,
        "avg_net_pnl_pct": round(float(pnl.mean()), 4),
        "sum_net_pnl_pct": round(float(pnl.sum()), 2),
        "win_rate": round(float(sel["outcome"].mean()) * 100, 1),
        "max_drawdown_pct": _max_drawdown_pct(pnl),
    }


def compare(
    tag_a: str,
    tag_b: str,
    direction: str,
    replay_path: str,
    threshold_a: float | None = None,
    threshold_b: float | None = None,
    ts_key: str = "signal_time",
    label_key: str = "outcome_tp1",
) -> dict[str, Any]:
    """Head-to-head of two generations on the same replay (DB-free)."""
    replay = load_replay(replay_path, ts_key=ts_key, label_key=label_key)
    if replay.empty:
        raise ValueError(f"Replay {replay_path} contains no labelled events.")

    index = ix.build_index(load_embedded=False)  # build once, use for both
    contract_a = load_contract(resolve_artifact_path(tag_a, direction, index))
    contract_b = load_contract(resolve_artifact_path(tag_b, direction, index))
    eval_a = evaluate(contract_a, replay, threshold_a)
    eval_b = evaluate(contract_b, replay, threshold_b)

    winner = None
    if eval_a["avg_net_pnl_pct"] is not None and eval_b["avg_net_pnl_pct"] is not None:
        if eval_a["avg_net_pnl_pct"] != eval_b["avg_net_pnl_pct"]:
            winner = tag_a if eval_a["avg_net_pnl_pct"] > eval_b["avg_net_pnl_pct"] else tag_b

    return {
        "schema": "bot_variants_compare/v1",
        "direction": direction.upper(),
        "replay": ix._rel(replay_path) if replay_path.startswith(ix.REPO_ROOT) else replay_path,
        "replay_events": int(len(replay)),
        "a": {"tag": tag_a.upper(), **eval_a},
        "b": {"tag": tag_b.upper(), **eval_b},
        "winner_by_avg_net_pnl": winner,
    }


def render_compare(result: dict[str, Any]) -> str:
    a, b = result["a"], result["b"]
    lines = [
        f"# Generation A/B — {a['tag']} vs {b['tag']} ({result['direction']})",
        "",
        f"Replay: {result['replay']} ({result['replay_events']} events)",
        "",
        "| Metric | " + a["tag"] + " | " + b["tag"] + " |",
        "|---|---|---|",
        f"| threshold | {a['threshold']} | {b['threshold']} |",
        f"| n (emitted) | {a['n']} | {b['n']} |",
        f"| Ø net_pnl_pct | {a['avg_net_pnl_pct']} | {b['avg_net_pnl_pct']} |",
        f"| Σ net_pnl_pct | {a['sum_net_pnl_pct']} | {b['sum_net_pnl_pct']} |",
        f"| win_rate % | {a['win_rate']} | {b['win_rate']} |",
        f"| max_drawdown_pct | {a['max_drawdown_pct']} | {b['max_drawdown_pct']} |",
        "",
        f"**Winner (Ø net_pnl):** {result['winner_by_avg_net_pnl'] or 'tie / n=0'}",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generation A/B sim (DB-free, T-2026-KYT-9050-039).")
    parser.add_argument("tag_a", help="Generation A, e.g. RUB2")
    parser.add_argument("tag_b", help="Generation B, e.g. RUB3")
    parser.add_argument("--direction", required=True, choices=["LONG", "SHORT"])
    parser.add_argument("--replay", required=True, help="Path to a *_replay_*.jsonl")
    parser.add_argument("--threshold-a", type=float, default=None, help="Threshold override A")
    parser.add_argument("--threshold-b", type=float, default=None, help="Threshold override B")
    parser.add_argument("--ts-key", default="signal_time")
    parser.add_argument("--label-key", default="outcome_tp1")
    parser.add_argument("--out", default=None, help="Write result JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    try:
        result = compare(
            args.tag_a,
            args.tag_b,
            args.direction,
            args.replay,
            args.threshold_a,
            args.threshold_b,
            args.ts_key,
            args.label_key,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}")
        return 2

    print(render_compare(result))
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

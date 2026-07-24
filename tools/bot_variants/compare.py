#!/usr/bin/env python3
# tools/bot_variants/compare.py — Generation-A/B-Sim-Harness (T-2026-KYT-9050-039, D3).
#
# ZWECK: Zwei Bot-Generationen head-to-head über die BESTEHENDE, DB-freie
# Replay-Infra vergleichen (Generation-A vs Generation-B auf DENSELBEN Events).
# Baut die Sim NICHT neu: die Labels/PnL kommen aus einem bereits erzeugten
# `*_replay_*.jsonl` (tools/retrain_from_replay.load_replay — derselbe Loader,
# der auch die Retrains speist), die Generation liefert nur das Scoring-Modell.
#
# Abgrenzung: tools/walkforward_sim.py ERZEUGT die Replays aus der Live-DB
# (DB-gebunden, VPS-only). compare.py KONSUMIERT sie (DB-frei) — es lädt nur ein
# Replay-JSONL + die Artefakt-pkls und rechnet vergleichende Metriken.
#
# Metriken je Generation (auf dem Replay, am jeweiligen Operating-Threshold):
#   n · avg_net_pnl_pct · sum_net_pnl_pct · win_rate · max_drawdown_pct.
#
# Invariants:
#   * READ-ONLY, DB-frei, kein Netzwerk. Kein Schreiben (nur --out JSON optional).
#   * Scoring-Vertrag identisch zum Live-/Shadow-Pfad: rohe predict_proba[:,1] auf
#     der Feature-Reihenfolge des Artefakts (core.shadow_gate.score_artifact-Semantik).
#   * Threshold-Semantik wie Live: prob >= optimal_threshold; threshold=None ⇒
#     Emission auf JEDEM Event (Detektor ist das Gate) — getreue Vorschau.

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
# Artefakt-Contract laden (generisch: dict-Artefakt, xgb-json, bare classifier)
# ─────────────────────────────────────────────────────────────────────────────
def _features_from_bare_model(model: Any) -> list[str] | None:
    """Feature-Namen aus einem nackten Estimator (booster / sklearn)."""
    booster = getattr(model, "get_booster", None)
    if callable(booster):
        try:
            names = booster().feature_names
            if names:
                return list(names)
        except Exception:  # pragma: no cover - defensiv
            pass
    names_in = getattr(model, "feature_names_in_", None)
    if names_in is not None:
        return list(names_in)
    return None


def load_contract(path: str) -> dict[str, Any]:
    """Lädt ein Artefakt zu einem schlanken Scoring-Contract {model, features, threshold}.

    Deckt die drei Fleet-Formate: .json (nativer XGB + *_meta.json-Sidecar),
    .pkl/.joblib als dict (retrain_from_replay: model/features/optimal_threshold)
    und .pkl als nackter Estimator (Legacy — Features aus dem Booster). Wirft
    ValueError, wenn kein Feature-Vertrag ableitbar ist (ohne Features kein
    faires Scoring)."""
    if not os.path.isfile(path):
        raise ValueError(f"Artefakt nicht gefunden: {path}")
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
            raise ValueError(f"Artefakt {path} ohne Feature-Liste — kein Scoring-Vertrag.")
        return {"model": art["model"], "features": features, "threshold": art.get("optimal_threshold")}

    features = _features_from_bare_model(art)
    if not features:
        raise ValueError(f"Bare-Estimator {path} ohne Feature-Namen — Feature-Vertrag nicht ableitbar.")
    return {"model": art, "features": features, "threshold": None}


def resolve_artifact_path(tag: str, direction: str) -> str:
    """Fundort-Pfad des Artefakts einer (tag, direction) aus dem Index."""
    index = ix.build_index(load_embedded=False)
    norm = tag.strip().upper()
    for gen in index["generations"]:
        if gen["tag"].upper() != norm:
            continue
        for art in gen["artifacts"]:
            if art["direction"] == direction.upper() and art["exists"] and art["path"]:
                return os.path.join(ix.REPO_ROOT, art["path"])
        raise ValueError(f"{tag}/{direction}: kein vorhandenes Artefakt im Index.")
    raise ValueError(f"Unbekannte Generation '{tag}'.")


# ─────────────────────────────────────────────────────────────────────────────
# Scoring + Metriken (DB-frei, auf einem geladenen Replay-DataFrame)
# ─────────────────────────────────────────────────────────────────────────────
def score(contract: dict[str, Any], replay: pd.DataFrame) -> np.ndarray:
    """Rohe predict_proba[:,1] auf der Feature-Reihenfolge des Artefakts."""
    features = contract["features"]
    X = replay.reindex(columns=features).fillna(0)
    return contract["model"].predict_proba(X)[:, 1].astype(float)


def _max_drawdown_pct(pnl_series: np.ndarray) -> float:
    """Max Drawdown der kumulierten Netto-PnL-Kurve (in PnL-%-Punkten, <= 0)."""
    if pnl_series.size == 0:
        return 0.0
    cum = np.cumsum(pnl_series)
    running_max = np.maximum.accumulate(cum)
    drawdown = cum - running_max  # <= 0
    return round(float(drawdown.min()), 4)


def evaluate(contract: dict[str, Any], replay: pd.DataFrame, threshold: float | None = None) -> dict[str, Any]:
    """Metriken einer Generation auf dem Replay am Operating-Threshold.

    threshold-Override > contract['threshold'] > None. None ⇒ jedes Event zählt
    (Detektor-Gate, wie eine nicht-deploybare Generation live emittieren würde).
    replay muss chronologisch sortiert sein (load_replay liefert das)."""
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
    """Head-to-head zweier Generationen auf demselben Replay (DB-frei)."""
    replay = load_replay(replay_path, ts_key=ts_key, label_key=label_key)
    if replay.empty:
        raise ValueError(f"Replay {replay_path} enthält keine gelabelten Events.")

    contract_a = load_contract(resolve_artifact_path(tag_a, direction))
    contract_b = load_contract(resolve_artifact_path(tag_b, direction))
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
        f"# Generation-A/B — {a['tag']} vs {b['tag']} ({result['direction']})",
        "",
        f"Replay: {result['replay']} ({result['replay_events']} Events)",
        "",
        "| Metrik | " + a["tag"] + " | " + b["tag"] + " |",
        "|---|---|---|",
        f"| threshold | {a['threshold']} | {b['threshold']} |",
        f"| n (emittiert) | {a['n']} | {b['n']} |",
        f"| Ø net_pnl_pct | {a['avg_net_pnl_pct']} | {b['avg_net_pnl_pct']} |",
        f"| Σ net_pnl_pct | {a['sum_net_pnl_pct']} | {b['sum_net_pnl_pct']} |",
        f"| win_rate % | {a['win_rate']} | {b['win_rate']} |",
        f"| max_drawdown_pct | {a['max_drawdown_pct']} | {b['max_drawdown_pct']} |",
        "",
        f"**Sieger (Ø net_pnl):** {result['winner_by_avg_net_pnl'] or 'unentschieden / n=0'}",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generation-A/B-Sim (DB-frei, T-2026-KYT-9050-039).")
    parser.add_argument("tag_a", help="Generation A, z.B. RUB2")
    parser.add_argument("tag_b", help="Generation B, z.B. RUB3")
    parser.add_argument("--direction", required=True, choices=["LONG", "SHORT"])
    parser.add_argument("--replay", required=True, help="Pfad zu einem *_replay_*.jsonl")
    parser.add_argument("--threshold-a", type=float, default=None, help="Threshold-Override A")
    parser.add_argument("--threshold-b", type=float, default=None, help="Threshold-Override B")
    parser.add_argument("--ts-key", default="signal_time")
    parser.add_argument("--label-key", default="outcome_tp1")
    parser.add_argument("--out", default=None, help="Ergebnis-JSON schreiben")
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
        print(f"FEHLER: {exc}")
        return 2

    print(render_compare(result))
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        print(f"geschrieben: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

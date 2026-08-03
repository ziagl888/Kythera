# core/model_artifacts.py — unified artifact loader for research bots 30–33
# and (since T-2026-CU-9050-042) the retrain paths of EPD and SRA.
#
# TWO artifact formats live in the repo. Both deliver the same contract dict here:
#
#   A) dict-pkl  — tools/retrain_from_replay.save_artifact / tools/new_models_train.py:
#      joblib-dict(model, features, optimal_threshold, calibrator_isotonic, meta)
#      Users: MIS2, RUB2-SHORT, EPD2, research bots 30–33.  → load_artifact()
#
#   B) XGB-JSON + sidecars — tools/retrain_from_replay (abr1) / tools/retrain_sra2.py:
#      model.save_model(x.json) + x_meta.json + x_calib.pkl. Native XGBoost JSON
#      is the production format for XGB bots; a pickled booster object coupled
#      trainer and bot xgboost versions together.
#      Users: ABR2 (18_ai_abr1_bot._load_model_contract), SRA2.  → load_artifact_json()
#
# meta carries e.g. model_id (posting tag), trained_at and the validation operating point.
# The tag ALWAYS comes from meta.model_id, never from a source code constant
# (hard rule 6); default_tag only kicks in for legacy artifacts without meta.
#
# Missing artifact is NOT a startup abort: bots then run in idle mode
# (code can be deployed before VPS training, avoid watchdog restart loop)
# — the caller decides this via loaded=False.

from __future__ import annotations

import json
import logging
import os
import time

import joblib

logger = logging.getLogger(__name__)

RELOAD_SECONDS = 24 * 3600  # R07-AIM1-b pattern: reload artifact daily


def empty_contract(path: str, default_tag: str, default_threshold: float = 1.0) -> dict:
    """The not-loaded state of the contract (idle mode, trap 3)."""
    return {
        "loaded": False,
        "model": None,
        "features": None,
        "threshold": float(default_threshold),
        "calibrator": None,
        "tag": default_tag,
        "meta": {},
        "loaded_at": time.time(),
        "path": path,
        "default_tag": default_tag,
        "default_threshold": float(default_threshold),
    }


def check_feature_contract(features: list[str], expected_features: list[str]) -> None:
    """P0.12: if the artifact requires features the current builder doesn't
    deliver, it will NOT be loaded — no silent fillna(0) over missing columns.

    A feature the builder knows and whose value is missing at runtime (e.g.
    funding without history) is different: that's a NaN/0 value and trainer
    parity, not a contract breach.
    """
    missing = [c for c in features if c not in expected_features]
    if missing:
        raise ValueError(f"Artifact requires features the builder doesn't deliver: {missing[:6]}…")


def build_contract(art: dict, expected_features: list[str], default_tag: str, path: str = "") -> dict:
    """Builds the contract from an already-loaded dict artifact (format A).

    Separate from load_artifact because bots with legacy format (EPD) load
    the file themselves and only then decide whether it's an artifact or
    a raw model.
    """
    features = list(art["features"])
    check_feature_contract(features, expected_features)
    out = empty_contract(path, default_tag)
    meta = dict(art.get("meta", {}))
    out.update(
        loaded=True,
        model=art["model"],
        features=features,
        threshold=float(art["optimal_threshold"]),
        calibrator=art.get("calibrator_isotonic"),
        meta=meta,
        tag=str(meta.get("model_id", default_tag)),
    )
    return out


def _log_loaded(path: str, contract: dict) -> None:
    n = len(contract["features"]) if contract["features"] else 0
    logger.info(
        f"✅ Artifact loaded: {path} — {n} features, "
        f"threshold {contract['threshold']:.2f}, tag {contract['tag']}, "
        f"calibrator: {'yes' if contract['calibrator'] is not None else 'no'}"
    )


def load_artifact(path: str, expected_features: list[str], default_tag: str) -> dict:
    """Loads a dict-pkl artifact (format A) and validates the feature contract.

    Return (always same keys):
      {loaded, model, features, threshold, calibrator, tag, meta, loaded_at, path,
       default_tag, default_threshold}
    """
    out = empty_contract(path, default_tag)
    if not os.path.exists(path):
        logger.warning(f"Artifact missing: {path} — bot runs in idle mode until deploy.")
        return out
    try:
        out = build_contract(joblib.load(path), expected_features, default_tag, path)
        _log_loaded(path, out)
    except Exception as e:
        logger.error(f"❌ Artifact {path} not loadable: {e}")
        return empty_contract(path, default_tag)
    return out


def load_artifact_json(
    path: str,
    expected_features: list[str],
    default_tag: str,
    default_threshold: float = 1.0,
) -> dict:
    """Loads a native XGB-JSON artifact with meta/calib sidecars (format B).

    Pattern: 18_ai_abr1_bot._load_model_contract (T-2026-CU-9050-042 lifts it into
    the shared loader). Without ``<name>_meta.json`` there's a LEGACY model:
    it still loads, but keeps ``default_tag``/``default_threshold`` and delivers
    ``features=None`` — the bot then builds its feature frame as before.
    Only the meta of a retrain generation carries tag, threshold and feature
    contract; so an SRA3 posts as SRA3 instead of silently as SRA2.
    """
    import xgboost as xgb  # local: pkl-bots don't pull xgboost via this import

    out = empty_contract(path, default_tag, default_threshold)
    if not os.path.exists(path):
        logger.warning(f"Artifact missing: {path} — bot runs in idle mode until deploy.")
        return out
    try:
        model = xgb.XGBClassifier()
        model.load_model(path)

        calib_path = path.replace(".json", "_calib.pkl")
        calibrator = joblib.load(calib_path) if os.path.exists(calib_path) else None

        meta_path = path.replace(".json", "_meta.json")
        if not os.path.exists(meta_path):
            logger.warning(
                f"⚠️ {meta_path} missing — legacy contract: tag {default_tag}, "
                f"threshold {default_threshold:.2f}, features from bot builder."
            )
            out.update(loaded=True, model=model, calibrator=calibrator)
            return out

        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        # The 3-class predecessor and binary retrain model share the filename slot.
        # success = predict_proba[:, 1] only holds binary — an undetected format
        # change would silently read the wrong column.
        if not str(meta.get("model_type", "")).startswith("binary"):
            raise ValueError(f"{meta_path}: unexpected model_type {meta.get('model_type')!r}")
        features = meta.get("features")
        if not features:
            raise ValueError(f"{meta_path}: features list missing — regenerate artifact with current trainer")
        check_feature_contract(list(features), expected_features)

        out.update(
            loaded=True,
            model=model,
            features=list(features),
            threshold=float(meta["optimal_threshold"]),
            calibrator=calibrator,
            meta=dict(meta),
            tag=str(meta.get("model_id", default_tag)),
        )
        _log_loaded(path, out)
    except Exception as e:
        logger.error(f"❌ Artifact {path} not loadable: {e}")
        return empty_contract(path, default_tag, default_threshold)
    return out


def _reload(artifact: dict, expected_features: list[str]) -> dict:
    """Reloads the same artifact in the same format (format from extension)."""
    path = artifact["path"]
    default_tag = artifact.get("default_tag", artifact["tag"])
    if path.endswith(".json"):
        return load_artifact_json(path, expected_features, default_tag, artifact.get("default_threshold", 1.0))
    return load_artifact(path, expected_features, default_tag)


def maybe_reload(artifact: dict, expected_features: list[str]) -> dict:
    """Daily reload (silently picks up new deploys, R07-AIM1-b pattern).

    A failed reload must not discard a LOADED artifact (review PR #10): a
    transient error (file lock during operator copy, AV scan, half-written
    deploy) would otherwise silently shut down a live path until the next 24h
    window. Only if the file is GONE (operator undeploy) is the not-loaded
    state adopted.

    Reload goes via ``default_tag``, not the CURRENTLY loaded tag: otherwise
    a legacy artifact without meta would inherit the tag of the generation
    it's replacing.
    """
    if time.time() - artifact.get("loaded_at", 0) < RELOAD_SECONDS:
        return artifact
    fresh = _reload(artifact, expected_features)
    if fresh["loaded"] or not artifact.get("loaded"):
        return fresh
    if not os.path.exists(artifact["path"]):
        return fresh
    logger.warning(
        f"⚠️ Reload of {artifact['path']} failed — keeping loaded "
        f"artifact {artifact['tag']} (next try in {RELOAD_SECONDS // 3600}h)."
    )
    return {**artifact, "loaded_at": fresh["loaded_at"]}


def calibrated_confidence(artifact: dict, raw_prob: float) -> float:
    """Calibrated confidence ONLY for display/logging — the gate runs on the
    raw probability that the threshold was also chosen for (convention from
    11_ai_mis_bot / 18_ai_abr1_bot)."""
    cal = artifact.get("calibrator")
    if cal is None:
        return float(raw_prob)
    try:
        return float(min(max(cal.predict([raw_prob])[0], 0.0), 1.0))
    except Exception:
        return float(raw_prob)

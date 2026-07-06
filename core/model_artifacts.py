# core/model_artifacts.py — einheitlicher Artefakt-Loader der Research-Bots 30–33.
#
# Artefakt-Vertrag (tools/new_models_train.py, gleiche Keys wie die MIS2-
# Artefakte aus tools/retrain_from_replay.py):
#   dict(model, features, optimal_threshold, calibrator_isotonic, meta)
# meta trägt u.a. model_id (Posting-Tag), trained_at und den Val-Operating-Point.
#
# Fehlendes Artefakt ist KEIN Startabbruch: die Bots laufen dann im Idle-Modus
# (Code kann vor dem VPS-Training deployt werden, Watchdog-Restart-Loop
# vermeiden) — der Aufrufer entscheidet das über loaded=False.

from __future__ import annotations

import logging
import os
import time

import joblib

logger = logging.getLogger(__name__)

RELOAD_SECONDS = 24 * 3600  # R07-AIM1-b-Muster: Artefakt täglich neu laden


def load_artifact(path: str, expected_features: list[str], default_tag: str) -> dict:
    """Lädt ein Research-Modell-Artefakt und validiert den Feature-Vertrag.

    Rückgabe (immer dieselben Keys):
      {loaded, model, features, threshold, calibrator, tag, meta, loaded_at, path}

    Der Feature-Vertrag ist hart: verlangt das Artefakt Features, die der
    aktuelle Builder nicht liefert, wird es NICHT geladen (P0.12 — kein
    stilles fillna(0) über fehlende Spalten).
    """
    out = {
        "loaded": False,
        "model": None,
        "features": None,
        "threshold": 1.0,
        "calibrator": None,
        "tag": default_tag,
        "meta": {},
        "loaded_at": time.time(),
        "path": path,
    }
    if not os.path.exists(path):
        logger.warning(f"Artefakt fehlt: {path} — Bot läuft im Idle-Modus bis zum Deploy.")
        return out
    try:
        art = joblib.load(path)
        features = list(art["features"])
        missing = [c for c in features if c not in expected_features]
        if missing:
            raise ValueError(f"Artefakt verlangt Features, die der Builder nicht liefert: {missing[:6]}…")
        out.update(
            loaded=True,
            model=art["model"],
            features=features,
            threshold=float(art["optimal_threshold"]),
            calibrator=art.get("calibrator_isotonic"),
            meta=dict(art.get("meta", {})),
        )
        out["tag"] = str(out["meta"].get("model_id", default_tag))
        logger.info(
            f"✅ Artefakt geladen: {path} — {len(features)} Features, "
            f"Threshold {out['threshold']:.2f}, Tag {out['tag']}, "
            f"Kalibrator: {'ja' if out['calibrator'] is not None else 'nein'}"
        )
    except Exception as e:
        logger.error(f"❌ Artefakt {path} nicht ladbar: {e}")
    return out


def maybe_reload(artifact: dict, expected_features: list[str]) -> dict:
    """Tägliches Reload (nimmt still neue Deploys auf, R07-AIM1-b-Muster)."""
    if time.time() - artifact.get("loaded_at", 0) < RELOAD_SECONDS:
        return artifact
    return load_artifact(artifact["path"], expected_features, artifact["tag"])


def calibrated_confidence(artifact: dict, raw_prob: float) -> float:
    """Kalibrierte Confidence NUR für Anzeige/Logging — das Gate läuft auf der
    rohen Probability, auf der auch der Threshold gewählt wurde (Konvention
    aus 11_ai_mis_bot / 18_ai_abr1_bot)."""
    cal = artifact.get("calibrator")
    if cal is None:
        return float(raw_prob)
    try:
        return float(min(max(cal.predict([raw_prob])[0], 0.0), 1.0))
    except Exception:
        return float(raw_prob)

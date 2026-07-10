# core/model_artifacts.py — einheitlicher Artefakt-Loader der Research-Bots 30–33
# und (seit T-2026-CU-9050-042) der Retrain-Pfade von EPD und SRA.
#
# Im Repo leben ZWEI Artefakt-Formate. Beide liefern hier denselben Contract-Dict:
#
#   A) dict-pkl  — tools/retrain_from_replay.save_artifact / tools/new_models_train.py:
#      joblib-dict(model, features, optimal_threshold, calibrator_isotonic, meta)
#      Nutzer: MIS2, RUB2-SHORT, EPD2, Research-Bots 30–33.  → load_artifact()
#
#   B) XGB-JSON + Sidecars — tools/retrain_from_replay (abr1) / tools/retrain_sra2.py:
#      model.save_model(x.json) + x_meta.json + x_calib.pkl. Natives XGBoost-JSON
#      ist das Produktions-Format der XGB-Bots; ein gepickeltes Booster-Objekt
#      koppelte Trainer- und Bot-xgboost-Version aneinander.
#      Nutzer: ABR2 (18_ai_abr1_bot._load_model_contract), SRA2.  → load_artifact_json()
#
# meta trägt u.a. model_id (Posting-Tag), trained_at und den Val-Operating-Point.
# Der Tag kommt IMMER aus meta.model_id, nie aus einer Quellcode-Konstante
# (harte Regel 6); default_tag greift nur für Legacy-Artefakte ohne Meta.
#
# Fehlendes Artefakt ist KEIN Startabbruch: die Bots laufen dann im Idle-Modus
# (Code kann vor dem VPS-Training deployt werden, Watchdog-Restart-Loop
# vermeiden) — der Aufrufer entscheidet das über loaded=False.

from __future__ import annotations

import json
import logging
import os
import time

import joblib

logger = logging.getLogger(__name__)

RELOAD_SECONDS = 24 * 3600  # R07-AIM1-b-Muster: Artefakt täglich neu laden


def empty_contract(path: str, default_tag: str, default_threshold: float = 1.0) -> dict:
    """Der Nicht-geladen-Zustand des Contracts (Idle-Modus, Falle 3)."""
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
    """P0.12: verlangt das Artefakt Features, die der aktuelle Builder nicht
    liefert, wird es NICHT geladen — kein stilles fillna(0) über fehlende Spalten.

    Ein Feature, das der Builder kennt und dessen Wert zur Laufzeit fehlt (z.B.
    Funding ohne Historie), ist etwas anderes: das ist ein NaN/0-Wert und
    Trainer-Parität, kein Vertragsbruch.
    """
    missing = [c for c in features if c not in expected_features]
    if missing:
        raise ValueError(f"Artefakt verlangt Features, die der Builder nicht liefert: {missing[:6]}…")


def build_contract(art: dict, expected_features: list[str], default_tag: str, path: str = "") -> dict:
    """Baut den Contract aus einem bereits geladenen dict-Artefakt (Format A).

    Getrennt von load_artifact, weil Bots mit Legacy-Format (EPD) die Datei
    selbst laden und erst am Objekt entscheiden, ob es ein Artefakt oder ein
    rohes Modell ist.
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
        f"✅ Artefakt geladen: {path} — {n} Features, "
        f"Threshold {contract['threshold']:.2f}, Tag {contract['tag']}, "
        f"Kalibrator: {'ja' if contract['calibrator'] is not None else 'nein'}"
    )


def load_artifact(path: str, expected_features: list[str], default_tag: str) -> dict:
    """Lädt ein dict-pkl-Artefakt (Format A) und validiert den Feature-Vertrag.

    Rückgabe (immer dieselben Keys):
      {loaded, model, features, threshold, calibrator, tag, meta, loaded_at, path,
       default_tag, default_threshold}
    """
    out = empty_contract(path, default_tag)
    if not os.path.exists(path):
        logger.warning(f"Artefakt fehlt: {path} — Bot läuft im Idle-Modus bis zum Deploy.")
        return out
    try:
        out = build_contract(joblib.load(path), expected_features, default_tag, path)
        _log_loaded(path, out)
    except Exception as e:
        logger.error(f"❌ Artefakt {path} nicht ladbar: {e}")
        return empty_contract(path, default_tag)
    return out


def load_artifact_json(
    path: str,
    expected_features: list[str],
    default_tag: str,
    default_threshold: float = 1.0,
) -> dict:
    """Lädt ein natives XGB-JSON-Artefakt mit Meta-/Calib-Sidecars (Format B).

    Muster: 18_ai_abr1_bot._load_model_contract (T-2026-CU-9050-042 hebt es in
    den geteilten Loader). Ohne ``<name>_meta.json`` liegt ein LEGACY-Modell vor:
    es lädt weiterhin, behält aber ``default_tag``/``default_threshold`` und
    liefert ``features=None`` — der Bot baut seinen Feature-Frame dann wie
    bisher. Erst die Meta einer Retrain-Generation trägt Tag, Threshold und
    Feature-Vertrag; damit postet ein SRA3 als SRA3 statt still als SRA2.
    """
    import xgboost as xgb  # lokal: die pkl-Bots ziehen xgboost nicht über diesen Import

    out = empty_contract(path, default_tag, default_threshold)
    if not os.path.exists(path):
        logger.warning(f"Artefakt fehlt: {path} — Bot läuft im Idle-Modus bis zum Deploy.")
        return out
    try:
        model = xgb.XGBClassifier()
        model.load_model(path)

        calib_path = path.replace(".json", "_calib.pkl")
        calibrator = joblib.load(calib_path) if os.path.exists(calib_path) else None

        meta_path = path.replace(".json", "_meta.json")
        if not os.path.exists(meta_path):
            logger.warning(
                f"⚠️ {meta_path} fehlt — Legacy-Vertrag: Tag {default_tag}, "
                f"Threshold {default_threshold:.2f}, Features aus dem Bot-Builder."
            )
            out.update(loaded=True, model=model, calibrator=calibrator)
            return out

        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        # Der 3-Klassen-Vorgänger und das binäre Retrain-Modell teilen sich den
        # Dateinamen-Slot. success = predict_proba[:, 1] gilt nur binär — ein
        # unerkannter Formatwechsel läse still die falsche Spalte.
        if not str(meta.get("model_type", "")).startswith("binary"):
            raise ValueError(f"{meta_path}: unerwarteter model_type {meta.get('model_type')!r}")
        features = meta.get("features")
        if not features:
            raise ValueError(f"{meta_path}: Feature-Liste fehlt — Artefakt mit aktuellem Trainer neu erzeugen")
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
        logger.error(f"❌ Artefakt {path} nicht ladbar: {e}")
        return empty_contract(path, default_tag, default_threshold)
    return out


def _reload(artifact: dict, expected_features: list[str]) -> dict:
    """Lädt dasselbe Artefakt im selben Format neu (Format aus der Endung)."""
    path = artifact["path"]
    default_tag = artifact.get("default_tag", artifact["tag"])
    if path.endswith(".json"):
        return load_artifact_json(path, expected_features, default_tag, artifact.get("default_threshold", 1.0))
    return load_artifact(path, expected_features, default_tag)


def maybe_reload(artifact: dict, expected_features: list[str]) -> dict:
    """Tägliches Reload (nimmt still neue Deploys auf, R07-AIM1-b-Muster).

    Ein fehlgeschlagener Reload darf ein GELADENES Artefakt nicht verwerfen
    (Review PR #10): ein transienter Fehler (File-Lock während Operator-Copy,
    AV-Scan, halb geschriebener Deploy) würde sonst eine live Seite bis zum
    nächsten 24h-Fenster stumm schalten. Nur wenn die Datei WEG ist
    (Operator-Undeploy), wird der Nicht-geladen-Zustand übernommen.

    Der Reload geht über ``default_tag``, nicht über den AKTUELL geladenen Tag:
    sonst erbte ein Legacy-Artefakt ohne Meta den Tag der Generation, die es
    gerade ersetzt hat.
    """
    if time.time() - artifact.get("loaded_at", 0) < RELOAD_SECONDS:
        return artifact
    fresh = _reload(artifact, expected_features)
    if fresh["loaded"] or not artifact.get("loaded"):
        return fresh
    if not os.path.exists(artifact["path"]):
        return fresh
    logger.warning(
        f"⚠️ Reload von {artifact['path']} fehlgeschlagen — behalte das geladene "
        f"Artefakt {artifact['tag']} (nächster Versuch in {RELOAD_SECONDS // 3600}h)."
    )
    return {**artifact, "loaded_at": fresh["loaded_at"]}


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

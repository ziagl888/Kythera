"""Standalone (DB-free) behaviour tests for the shared artifact loader.

T-2026-CU-9050-042 lifts the ABR-style "native XGB-JSON + meta/calib sidecar"
loader into core/model_artifacts.py so EPD and SRA stop hardcoding their posting
tag. What these tests pin down:

  * the tag comes from meta.model_id, never from the source constant (rule 6);
  * a legacy artifact WITHOUT a meta sidecar still loads, under default_tag and
    default_threshold, with features=None (the bot keeps its own builder);
  * the feature contract is hard (P0.12): an artifact asking for a column the
    builder cannot produce does NOT load — it must never be silently fillna(0)'d;
  * a missing artifact is idle-mode, not a crash (Falle 3);
  * a 3-class model wearing a binary slot's filename is refused, because
    success = predict_proba[:, 1] only holds for the binary generation.

Run: python backtest/test_model_artifacts.py
"""

import json
import sys
import tempfile
from pathlib import Path

import joblib
import numpy as np
import xgboost as xgb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.model_artifacts import (  # noqa: E402
    build_contract,
    load_artifact,
    load_artifact_json,
    maybe_reload,
)

FEATURES = ["f0", "f1", "f2"]
BUILDER_CAN_PRODUCE = FEATURES + ["f3_unused"]


def _fit(n_classes=2):
    rng = np.random.default_rng(42)
    x = rng.normal(size=(60, len(FEATURES)))
    y = rng.integers(0, n_classes, size=60)
    y[:n_classes] = np.arange(n_classes)  # jede Klasse mindestens einmal
    m = xgb.XGBClassifier(n_estimators=3, max_depth=2, verbosity=0)
    m.fit(x, y)
    return m


def _write_json_artifact(d: Path, name="m.json", meta=None, calib=True):
    model = _fit(2 if (meta is None or str(meta.get("model_type", "")).startswith("binary")) else 3)
    p = d / name
    model.save_model(str(p))
    if meta is not None:
        (d / name.replace(".json", "_meta.json")).write_text(json.dumps(meta), encoding="utf-8")
    if calib:
        joblib.dump(None, d / name.replace(".json", "_calib.pkl"))
    return str(p)


def _binary_meta(model_id="SRA2", thresh=0.71, features=None):
    return {
        "model_type": "binary (1 = TP1 erreicht)",
        "model_id": model_id,
        "optimal_threshold": thresh,
        "features": features if features is not None else FEATURES,
    }


def test_json_tag_and_threshold_come_from_meta():
    with tempfile.TemporaryDirectory() as d:
        p = _write_json_artifact(Path(d), meta=_binary_meta(model_id="SRA3", thresh=0.42))
        c = load_artifact_json(p, BUILDER_CAN_PRODUCE, "SRA1", 0.65)
    assert c["loaded"]
    assert c["tag"] == "SRA3", f"tag must track meta.model_id, got {c['tag']!r}"
    assert abs(c["threshold"] - 0.42) < 1e-9, "threshold must come from the artifact, not the source constant"
    assert c["features"] == FEATURES


def test_json_legacy_without_meta_keeps_defaults():
    """The live SRA files have no meta sidecar. They must keep loading, under the
    named default tag, with features=None so the bot builds its own frame."""
    with tempfile.TemporaryDirectory() as d:
        p = _write_json_artifact(Path(d), meta=None)
        c = load_artifact_json(p, BUILDER_CAN_PRODUCE, "SRA1", 0.65)
    assert c["loaded"], "a legacy XGB-JSON model without meta must still load"
    assert c["tag"] == "SRA1"
    assert abs(c["threshold"] - 0.65) < 1e-9
    assert c["features"] is None


def test_json_feature_contract_is_hard():
    """P0.12: an artifact demanding a column the builder cannot produce must NOT
    load. Silently zero-filling it would serve the model a feature it never saw."""
    with tempfile.TemporaryDirectory() as d:
        meta = _binary_meta(features=[*FEATURES, "funding_that_the_bot_cannot_build"])
        p = _write_json_artifact(Path(d), meta=meta)
        c = load_artifact_json(p, BUILDER_CAN_PRODUCE, "SRA1", 0.65)
    assert not c["loaded"], "artifact with an unbuildable feature must be refused, not fillna(0)'d"
    assert c["tag"] == "SRA1" and c["model"] is None


def test_json_non_binary_model_type_is_refused():
    """success = predict_proba[:, 1] only holds for the binary generation."""
    with tempfile.TemporaryDirectory() as d:
        meta = _binary_meta()
        meta["model_type"] = "multi:softprob (3 Klassen)"
        p = _write_json_artifact(Path(d), meta=meta)
        c = load_artifact_json(p, BUILDER_CAN_PRODUCE, "SRA1", 0.65)
    assert not c["loaded"], "a non-binary model_type must be refused — it would read the wrong proba column"


def test_missing_artifact_is_idle_not_crash():
    c = load_artifact_json("does_not_exist.json", BUILDER_CAN_PRODUCE, "SRA1", 0.65)
    assert not c["loaded"] and c["model"] is None and c["tag"] == "SRA1"
    c = load_artifact("does_not_exist.pkl", BUILDER_CAN_PRODUCE, "EPD2")
    assert not c["loaded"] and c["model"] is None and c["tag"] == "EPD2"


def test_pkl_dict_artifact_tag_from_meta():
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "a.pkl")
        joblib.dump(
            {
                "model": _fit(),
                "features": FEATURES,
                "optimal_threshold": 0.33,
                "calibrator_isotonic": None,
                "meta": {"model_id": "EPD3"},
            },
            p,
        )
        c = load_artifact(p, BUILDER_CAN_PRODUCE, "EPD2")
    assert c["loaded"] and c["tag"] == "EPD3" and abs(c["threshold"] - 0.33) < 1e-9


def test_build_contract_defaults_to_tag_when_meta_has_no_model_id():
    c = build_contract(
        {"model": object(), "features": FEATURES, "optimal_threshold": 0.5, "meta": {}},
        BUILDER_CAN_PRODUCE,
        "EPD2",
    )
    assert c["loaded"] and c["tag"] == "EPD2"


def test_reload_of_a_json_artifact_uses_the_json_loader():
    """maybe_reload dispatches on the path suffix. Routed through the pkl loader a
    .json artifact would never re-read, and the daily reload would silently keep
    serving the old generation after an operator deployed the new one.

    Probed by deploying a NEW generation under the same path: only a real json
    reload picks the new tag up. (Asserting `loaded` alone proves nothing — a
    failed reload deliberately keeps the previously loaded artifact.)"""
    with tempfile.TemporaryDirectory() as d:
        p = _write_json_artifact(Path(d), meta=_binary_meta(model_id="SRA2"))
        c = load_artifact_json(p, BUILDER_CAN_PRODUCE, "SRA1", 0.65)
        assert c["tag"] == "SRA2"
        _write_json_artifact(Path(d), meta=_binary_meta(model_id="SRA3"))  # Operator deployt SRA3
        c["loaded_at"] = 0.0  # Reload-Fenster erzwingen
        fresh = maybe_reload(c, BUILDER_CAN_PRODUCE)
    assert fresh["loaded"], "the redeployed artifact must load"
    assert fresh["tag"] == "SRA3", f"reload did not re-read the json artifact — still serving {fresh['tag']}"


def test_reload_of_a_hand_built_contract_without_default_tag_keeps_the_old_behaviour():
    """13_ai_rub_bot builds RUB2_SHORT by hand and has no `default_tag` key. The old
    maybe_reload passed `artifact["tag"]` as the fallback; the .get() must land on
    exactly that, or an untouched live bot changes behaviour on its daily reload."""
    with tempfile.TemporaryDirectory() as d:
        p = str(Path(d) / "rub2_model_SHORT.pkl")
        joblib.dump(
            {"model": _fit(), "features": FEATURES, "optimal_threshold": 0.5, "meta": {}},  # keine model_id
            p,
        )
        hand_built = {"loaded": False, "model": None, "features": None, "threshold": 1.0, "tag": "RUB2", "path": p}
        fresh = maybe_reload(hand_built, BUILDER_CAN_PRODUCE)  # loaded_at fehlt → .get(...,0) → Reload
    assert fresh["loaded"], "the hand-built contract must still reload"
    assert fresh["tag"] == "RUB2", f"fallback tag drifted for a contract without default_tag: {fresh['tag']}"


def test_reload_keeps_a_loaded_artifact_when_the_file_turns_unreadable():
    with tempfile.TemporaryDirectory() as d:
        p = _write_json_artifact(Path(d), meta=_binary_meta(model_id="SRA2"))
        c = load_artifact_json(p, BUILDER_CAN_PRODUCE, "SRA1", 0.65)
        c["loaded_at"] = 0.0
        Path(p).write_text("{ this is not a model", encoding="utf-8")
        fresh = maybe_reload(c, BUILDER_CAN_PRODUCE)
    assert fresh["loaded"] and fresh["tag"] == "SRA2", "a transient bad read must not silence a live model"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK — shared artifact-loader contract holds")

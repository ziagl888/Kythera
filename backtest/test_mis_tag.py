"""Standalone (DB-free) guard for the MIS model-tag contract.

Background (T-2026-CU-9050-030, finding P1.45): the bot loaded its artifacts but
never read art["meta"]["model_id"], tagging every trade with the source constant
MODEL_GENERATION ("MIS2"). The artifact filenames mis2_model_*.pkl are slot names
(operator decision 2026-07-09) — a MIS3 retrain overwrites the same slot, so
meta.model_id is the ONLY generation marker. Dropping it would post MIS3 trades as
MIS2-8H/-24H/-72H/-168H, merging both generations in ai_signals, ml_predictions_master
and the per-bot stats the orchestrator gates on (versioning rule 6).

The static check is the load-bearing net: a runtime assertion would be swallowed by
the fleet-wide broad except blocks (lesson from T-2026-CU-9050-024).

Run: py -3.13 backtest/test_mis_tag.py
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "11_ai_mis_bot.py").read_text(encoding="utf-8")


def _load_body() -> str:
    body = re.search(r"def load_pump_models\(\):\n(.*?)\ndef ", SRC, re.DOTALL)
    assert body, "load_pump_models not found"
    return body.group(1)


def test_loader_reads_model_id_from_meta():
    body = _load_body()
    assert re.search(r"art\.get\(\"meta\"\)", body), (
        "load_pump_models no longer reads the artifact meta — the posting tag would "
        "fall back to the MODEL_GENERATION constant for every generation"
    )
    assert "model_id" in body, "load_pump_models no longer reads meta.model_id"
    assert re.search(r"cfg\[\"generation\"\]\s*=\s*model_id", body), (
        "the artifact model_id is read but not stored as the model's generation"
    )


def test_missing_model_id_fails_loudly():
    """A retrain artifact without model_id must not silently inherit the old tag."""
    body = _load_body()
    assert re.search(r"logger\.error\(", body), (
        "the meta.model_id fallback no longer logs loudly — a missing tag would be silent"
    )


def test_tag_is_built_from_artifact_generation():
    assert re.search(r"module_tag\s*=\s*f\"\{best_generation\}-\{best_horizon\}\"", SRC), (
        "module_tag is not built from the winning artifact's generation"
    )
    assert not re.search(r"module_tag\s*=\s*f\"\{MODEL_GENERATION\}", SRC), (
        "module_tag is derived from the MODEL_GENERATION constant again — "
        "a MIS3 retrain would post under the MIS2 tag"
    )


def test_generation_travels_with_the_candidate():
    """Each horizon carries ITS OWN generation, so a partial rollout (72H already
    MIS3, rest MIS2) tags every signal with the generation of the model that fired."""
    assert re.search(r"candidates\.append\(\(.*cfg\[\"generation\"\]\)\)", SRC), (
        "candidates no longer carry their model's generation"
    )
    assert re.search(r"best_conf,\s*best_generation\s*=\s*candidates\[0\]", SRC), (
        "the winning candidate's generation is not unpacked"
    )


if __name__ == "__main__":
    test_loader_reads_model_id_from_meta()
    test_missing_model_id_fails_loudly()
    test_tag_is_built_from_artifact_generation()
    test_generation_travels_with_the_candidate()
    print("OK — MIS model-tag contract holds")

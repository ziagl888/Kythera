"""Standalone (DB-free) guard for the Quasimodo model-tag contract.

Background (T-2026-CU-9050-030, finding P1.45): the bot derived its posting tag as
f"QM_{tf.upper()}" in two places — the scan loop and, sniper-style, again inside
send_cornix_signal — and never looked at the artifact. This one is PREVENTIVE: today
qm_ml_trainer.py writes no model_id, so the derived QM_1H is the correct tag and
nothing is mistagged. But the orchestrator was already made QM2-aware (QM\\d*_ in
BOT_IDENTIFICATION_PATTERNS, ff8e01e), and a QM2 retrain following the established
convention f"{strategy.upper()}2_{tf.upper()}" (retrain_from_replay.py) would silently
post as QM_1H, merging QM2 with the QM1 history that the orchestrator gates on
(versioning rule 6). Closed before QM2 exists.

The static check is the load-bearing net: a runtime assertion would be swallowed by
the fleet-wide broad except blocks (lesson from T-2026-CU-9050-024).

Run: py -3.13 backtest/test_quasimodo_tag.py
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "24_quasimodo_bot.py").read_text(encoding="utf-8")


def test_loader_prefers_artifact_model_id():
    assert re.search(r"ml_data\.get\('meta'\)", SRC), (
        "the loader no longer reads the artifact meta — a QM2 artifact would post as QM_1H"
    )
    assert re.search(r"'tag':\s*model_id or f\"QM_\{tf\.upper\(\)\}\"", SRC), (
        "the loader no longer falls back to the derived tag when model_id is absent "
        "(today's qm_ml_trainer writes none)"
    )


def test_scan_uses_the_artifact_tag():
    assert re.search(r"module_tag\s*=\s*ML_MODELS\[tf\]\['tag'\]", SRC), (
        "scan_market derives the tag from tf again instead of taking the artifact tag"
    )


def test_send_requires_module_tag():
    sig = re.search(r"def send_cornix_signal\((.*?)\):", SRC, re.DOTALL)
    assert sig, "send_cornix_signal not found"
    params = sig.group(1)
    assert "*, module_tag" in params, (
        "module_tag must be a REQUIRED keyword parameter — a call site that forgets it "
        "should raise TypeError, not silently reintroduce the derived tag"
    )
    assert not re.search(r"module_tag\s*=", params), "module_tag must not have a default"


def test_send_does_not_recompute_the_tag():
    body = re.search(r"def send_cornix_signal\(.*?\):\n(.*?)\ndef ", SRC, re.DOTALL)
    assert body, "send_cornix_signal body not found"
    assert not re.search(r"module_tag\s*=\s*f\"QM_", body.group(1)), (
        "send_cornix_signal derives the tag from tf again — a QM2 generation would be "
        "written to ai_signals under the QM1 tag"
    )


def test_trade_call_passes_module_tag():
    call = re.search(r"send_cornix_signal\((.*?)\)\n\s*update_cooldown", SRC, re.DOTALL)
    assert call, "trade call site not found"
    assert "module_tag=module_tag" in call.group(1), (
        "scan_market no longer passes the artifact tag to send_cornix_signal"
    )


if __name__ == "__main__":
    test_loader_prefers_artifact_model_id()
    test_scan_uses_the_artifact_tag()
    test_send_requires_module_tag()
    test_send_does_not_recompute_the_tag()
    test_trade_call_passes_module_tag()
    print("OK — Quasimodo model-tag contract holds")

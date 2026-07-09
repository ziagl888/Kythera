"""Standalone (DB-free) guard for the RUB model-tag contract.

Background (T-2026-CU-9050-030, finding P1.45): load_artifact already computed the
correct tag from the SHORT artifact's meta.model_id, but the bot dropped it and
posted both directions under the source constant "RUB2". A RUB3 retrain into the
same slot would have merged with RUB2 in ai_signals and in the per-bot win rate the
orchestrator gates on (versioning rule 6).

The fix is direction-dependent, and that asymmetry is the thing this guard protects:

  * SHORT fires rub2_model_SHORT.pkl → tag MUST come from RUB2_SHORT["tag"].
  * LONG  fires the legacy long_reversion_model.joblib, which has no artifact meta.
    It posts under the constant RUB_LONG_TAG by operator decision (2026-07-06).
    Wiring RUB2_SHORT["tag"] into the LONG branch would tag a signal with the
    generation of a model that never ran.

The static check is the load-bearing net: a runtime assertion would be swallowed by
the fleet-wide broad except blocks (lesson from T-2026-CU-9050-024).

Run: py -3.13 backtest/test_rub_tag.py
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "13_ai_rub_bot.py").read_text(encoding="utf-8")


def test_tag_is_direction_dependent():
    assert re.search(
        r"module_tag\s*=\s*RUB_LONG_TAG\s+if\s+is_long\s+else\s+RUB2_SHORT\[\"tag\"\]", SRC
    ), (
        "module_tag is no longer direction-dependent — SHORT must take the artifact tag "
        "(meta.model_id), LONG the legacy constant"
    )
    assert not re.search(r"module_tag\s*=\s*[\"']RUB2[\"']", SRC), (
        "module_tag is a hardcoded literal again — a RUB3 artifact would post as RUB2"
    )


def test_long_tag_is_a_named_constant():
    """LONG's constant must stay a named constant with its rationale, not drift into a
    literal that a later reader mistakes for a forgotten artifact lookup."""
    assert re.search(r"^RUB_LONG_TAG\s*=\s*[\"']RUB2[\"']", SRC, re.MULTILINE), (
        "RUB_LONG_TAG constant missing"
    )


def test_short_tag_comes_from_the_artifact():
    """load_artifact resolves tag = meta.model_id (core/model_artifacts.py); the bot must
    load the SHORT model through it rather than hand-building a partial contract."""
    assert re.search(r"RUB2_SHORT\s*=\s*load_artifact\(RUB2_SHORT_ARTIFACT_PATH", SRC), (
        "the SHORT artifact no longer goes through load_artifact — its tag would not "
        "track meta.model_id"
    )


if __name__ == "__main__":
    test_tag_is_direction_dependent()
    test_long_tag_is_a_named_constant()
    test_short_tag_comes_from_the_artifact()
    print("OK — RUB model-tag contract holds")

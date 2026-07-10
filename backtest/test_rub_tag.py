"""Standalone (DB-free) guard for the RUB model-tag and re-fire contract.

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
    assert re.search(r"module_tag\s*=\s*RUB_LONG_TAG\s+if\s+is_long\s+else\s+RUB2_SHORT\[\"tag\"\]", SRC), (
        "module_tag is no longer direction-dependent — SHORT must take the artifact tag "
        "(meta.model_id), LONG the legacy constant"
    )
    assert not re.search(r"module_tag\s*=\s*[\"']RUB2[\"']", SRC), (
        "module_tag is a hardcoded literal again — a RUB3 artifact would post as RUB2"
    )


def test_long_tag_is_a_named_constant():
    """LONG's constant must stay a named constant with its rationale, not drift into a
    literal that a later reader mistakes for a forgotten artifact lookup."""
    assert re.search(r"^RUB_LONG_TAG\s*=\s*[\"']RUB2[\"']", SRC, re.MULTILINE), "RUB_LONG_TAG constant missing"


def test_short_tag_comes_from_the_artifact():
    """load_artifact resolves tag = meta.model_id (core/model_artifacts.py); the bot must
    load the SHORT model through it rather than hand-building a partial contract."""
    assert re.search(r"RUB2_SHORT\s*=\s*load_artifact\(RUB2_SHORT_ARTIFACT_PATH", SRC), (
        "the SHORT artifact no longer goes through load_artifact — its tag would not track meta.model_id"
    )


def test_active_trade_check_blocks_a_second_position():
    """T-2026-CU-9050-043: the cooldown is a FREQUENCY guard (4h), not a position guard.
    A mean-reversion trade routinely outlives its cooldown, so without a check against the
    open trades in ai_signals the next signal opens a SECOND live position on the same
    coin. The bot must probe ai_signals by symbol/direction/model and skip on a hit —
    same shape as 11_ai_mis_bot.py and 25_smc_ml_sniper.py."""
    assert re.search(
        r"SELECT 1 FROM ai_signals\s*\n\s*WHERE symbol = %s AND direction = %s AND model IN \(%s, %s\)", SRC
    ), (
        "the active-trade check against ai_signals is gone — a trade outliving its 4h "
        "cooldown would let the same coin/direction fire a second live position"
    )
    assert re.search(r"if trade_exists:\s*\n\s*continue", SRC), (
        "the active-trade check no longer skips the signal when an open trade exists"
    )


def test_active_trade_check_uses_the_posting_tag_and_the_legacy_tag():
    """The check keys on the tag, so it must bind the SAME direction-dependent tag the
    post path writes (module_tag) plus the pre-fix tag. On a RUB3 rollout an open RUB2
    position would otherwise stop blocking, and the guard would reopen exactly the hole
    it was built to close."""
    assert re.search(r"\(symbol,\s*direction,\s*module_tag,\s*RUB_LEGACY_TAG\)", SRC), (
        "the active-trade check no longer binds (module_tag, RUB_LEGACY_TAG) — either it "
        "stopped tracking the posting tag or it lost the transitional legacy tag"
    )


def test_cooldown_covers_the_legacy_tag():
    """The cooldown stays alongside the active-trade check as the frequency guard (as in
    MIS: both run). Its key is the tag too. When RUB3 rolls out the SHORT tag flips, so a
    fresh RUB2 cooldown row would stop blocking a RUB3 signal on the same coin. The
    cooldown therefore also probes the pre-fix tag; while the tags agree the second query
    is skipped."""
    assert re.search(r"^RUB_LEGACY_TAG\s*=\s*[\"']RUB2[\"']", SRC, re.MULTILINE), "RUB_LEGACY_TAG constant missing"
    assert re.search(
        r"cooldown_tags\s*=\s*\[module_tag\]\s*if\s*module_tag\s*==\s*RUB_LEGACY_TAG\s*else\s*\[module_tag,\s*RUB_LEGACY_TAG\]",
        SRC,
    ), "the cooldown no longer probes the legacy tag on a generation switch"
    assert re.search(
        r"any\(check_cooldown\(conn,\s*t,\s*symbol,\s*direction,\s*4\)\s*for\s*t\s*in\s*cooldown_tags\)", SRC
    ), "the cooldown check no longer blocks when EITHER tag is still cooling down"


if __name__ == "__main__":
    test_tag_is_direction_dependent()
    test_active_trade_check_blocks_a_second_position()
    test_active_trade_check_uses_the_posting_tag_and_the_legacy_tag()
    test_cooldown_covers_the_legacy_tag()
    test_long_tag_is_a_named_constant()
    test_short_tag_comes_from_the_artifact()
    print("OK — RUB model-tag + re-fire contract holds")

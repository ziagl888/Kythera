"""Standalone (DB-free) guard for the RUB1-revive model-tag and re-fire contract.

Background (T-2026-KYT-9050-037, Michi bot_results.xlsx): Bot 13 is reverted to the
ORIGINAL RUB1 behaviour — BOTH directions run the original legacy reversion models
(long_reversion_model.joblib / short_reversion_model.joblib) and post live under the
original tag "RUB1". This reverts (a) the T-030 LONG tag rename (→ "RUB2") and (b) the
PR-#9 removal of the legacy-SHORT branch (the rub2_model_SHORT retrain). The RUB2
retrain generation is benched (stays SHADOW in core.shadow_gate); the RUB3/RUB4-LONG
challenger keeps running as a shadow (unchanged).

This guard pins the revived contract:
  * BOTH directions post under the single named constant RUB_TAG == "RUB1".
  * BOTH directions load the legacy joblib models (no load_artifact / meta.model_id).
  * SHORT is a first-class live leg again, at its ORIGINAL threshold (parity with the
    pre-PR-#9 RUB1 logic, git 07c8874^): legacy model, 9 rub features, no funding.
  * The transitional dedup (active-trade check + cooldown) still probes RUB_LEGACY_TAG
    == "RUB2" so an open RUB2 position across the tag switch cannot double-post (Regel 4).
  * Exactly ONE Cornix-parseable message per signal (Regel 4).
  * The two legacy model files are byte-unchanged (md5 assert, present-or-skip).

The static check is the load-bearing net: a runtime assertion would be swallowed by
the fleet-wide broad except blocks (lesson from T-2026-CU-9050-024).

Run: py -3.13 backtest/test_rub_tag.py   (oder pytest backtest/test_rub_tag.py)
"""

import hashlib
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "13_ai_rub_bot.py").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT))

# Known-good md5 of the two ORIGINAL RUB1 legacy models (Spec Anhang A, == v2-Checkout).
# This PR must NOT touch them (Regel 7). The files live in the repo root; on a lean CI
# checkout without them the assert skips rather than failing.
LEGACY_MD5 = {
    "long_reversion_model.joblib": "0227bb4a793444e2dddb5bf4f70ad69b",
    "short_reversion_model.joblib": "16ca37114f1239ac12fa4a503aebce72",
}


def test_tag_is_the_single_rub1_constant():
    """Both directions post under RUB_TAG; no direction-dependent meta lookup remains."""
    assert re.search(r"^RUB_TAG\s*=\s*[\"']RUB1[\"']", SRC, re.MULTILINE), "RUB_TAG must be the constant 'RUB1'"
    assert re.search(r"module_tag\s*=\s*RUB_TAG\b", SRC), "module_tag must resolve to the single RUB_TAG constant"
    # The reverted retrain path must be gone: no RUB2_SHORT artifact/contract, no
    # load_artifact/maybe_reload, no meta.model_id tag lookup.
    assert "RUB2_SHORT" not in SRC, "the RUB2-SHORT retrain contract must be removed (benched, not live-routed)"
    assert "load_artifact" not in SRC and "maybe_reload" not in SRC, (
        "the shared artifact loader is gone — RUB1 fires the raw legacy joblib models"
    )


def test_both_directions_load_the_legacy_joblib_models():
    """LONG and SHORT each load their original legacy joblib model by path."""
    assert re.search(r"^MODEL_LONG_PATH\s*=\s*['\"]long_reversion_model\.joblib['\"]", SRC, re.MULTILINE)
    assert re.search(r"^MODEL_SHORT_PATH\s*=\s*['\"]short_reversion_model\.joblib['\"]", SRC, re.MULTILINE)
    assert re.search(r"MODEL_LONG\s*=\s*joblib\.load\(MODEL_LONG_PATH\)", SRC), "LONG legacy model not loaded"
    assert re.search(r"MODEL_SHORT\s*=\s*joblib\.load\(MODEL_SHORT_PATH\)", SRC), (
        "the legacy-SHORT branch was not reactivated — SHORT must load short_reversion_model.joblib again"
    )


def test_short_fires_the_legacy_model_at_its_original_threshold():
    """SHORT parity with the pre-PR-#9 RUB1 logic: legacy model, original threshold,
    raw 9-feature predict_proba (NO funding features)."""
    assert re.search(r"^REVERSION_THRESH_LONG\s*=\s*0\.75", SRC, re.MULTILINE)
    assert re.search(r"^REVERSION_THRESH_SHORT\s*=\s*0\.85", SRC, re.MULTILINE), (
        "SHORT must use the original RUB1 threshold 0.85 (parity, not a new invented threshold)"
    )
    assert re.search(
        r"threshold\s*=\s*REVERSION_THRESH_SHORT\s*\n\s*prob\s*=\s*MODEL_SHORT\.predict_proba\("
        r"pd\.DataFrame\(\[base_features\]\)\)\[0,\s*1\]",
        SRC,
    ), "SHORT must score the legacy model on the raw base_features at REVERSION_THRESH_SHORT"
    # No funding features get mixed into the SHORT prediction path any more.
    assert "funding_features_asof" in SRC, "RUB3/RUB4 LONG shadow still needs funding — import must remain"
    # ... but only inside the RUB3 shadow emitter, never on the SHORT live path.
    short_branch = SRC.split("if is_long:", 1)[-1]
    assert "base_features.update(funding_features_asof" not in short_branch.split("_emit_rub3_shadow", 1)[0], (
        "the SHORT live path must not fold funding features into base_features (legacy = 9 rub features only)"
    )


def test_active_trade_check_binds_posting_and_legacy_tag():
    """T-2026-CU-9050-043: the cooldown is a FREQUENCY guard (4h), not a position guard.
    A mean-reversion trade routinely outlives its cooldown, so the bot must probe
    ai_signals by symbol/direction/model and skip on a hit. Across the RUB2 → RUB1 tag
    switch it must also bind the legacy tag, or an open RUB2 position stops blocking."""
    assert re.search(
        r"SELECT 1 FROM ai_signals\s*\n\s*WHERE symbol = %s AND direction = %s AND model IN \(%s, %s\)", SRC
    ), "the active-trade check against ai_signals is gone"
    assert re.search(r"\(symbol,\s*direction,\s*module_tag,\s*RUB_LEGACY_TAG\)", SRC), (
        "the active-trade check no longer binds (module_tag, RUB_LEGACY_TAG)"
    )
    assert re.search(r"if trade_exists:\s*\n\s*continue", SRC), "no skip when an open trade exists"


def test_cooldown_covers_the_legacy_tag():
    """The cooldown key is the tag too; across RUB2 → RUB1 it must probe the legacy tag."""
    assert re.search(r"^RUB_LEGACY_TAG\s*=\s*[\"']RUB2[\"']", SRC, re.MULTILINE), "RUB_LEGACY_TAG must be 'RUB2'"
    assert re.search(
        r"cooldown_tags\s*=\s*\[module_tag\]\s*if\s*module_tag\s*==\s*RUB_LEGACY_TAG\s*else\s*\[module_tag,\s*RUB_LEGACY_TAG\]",
        SRC,
    ), "the cooldown no longer probes the legacy tag on a generation switch"
    assert re.search(
        r"any\(check_cooldown\(conn,\s*t,\s*symbol,\s*direction,\s*4\)\s*for\s*t\s*in\s*cooldown_tags\)", SRC
    ), "the cooldown check no longer blocks when EITHER tag is still cooling down"


def test_exactly_one_cornix_message_per_signal():
    """Regel 4: exactly ONE Cornix-parseable message per signal. The Cornix block
    (cornix_msg) is written to the outbox exactly once; the HTML chart caption is a
    separate, non-repeating message (the fleet-wide double-trade bug, fixed 2026-07-06)."""
    assert SRC.count("(RUBBERBAND_CHANNEL_ID, cornix_msg)") == 1, (
        "the Cornix block must be posted to the outbox exactly once"
    )
    assert "cornix_msg" not in SRC.split("html_caption =", 1)[1].split("chart_buf", 1)[0], (
        "the HTML caption must not re-embed the Cornix block (double-post hazard)"
    )


def test_legacy_models_are_byte_unchanged():
    """Regel 7: this PR must not modify the legacy RUB1 model artifacts. Assert md5 when
    the files are present; skip on a lean checkout without them."""
    checked = 0
    for fname, expected in LEGACY_MD5.items():
        p = ROOT / fname
        if not p.exists():
            continue
        got = hashlib.md5(p.read_bytes()).hexdigest()
        assert got == expected, f"{fname} md5 changed ({got} != {expected}) — legacy RUB1 model was modified"
        checked += 1
    if checked == 0:
        print("  (skip) legacy model files not present on this checkout")


def test_register_says_live_live_shadow_shadow():
    """Spec §5 register assert: leg_status(RUB1 LONG / RUB1 SHORT / RUB2 LONG / RUB3 SHORT)
    == live live shadow shadow. Imports core.shadow_gate (dependency-light: stdlib only)."""
    os.environ.setdefault("DB_PASSWORD", "test")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
    from core import shadow_gate as sg

    assert sg.leg_status("RUB1", "LONG") == sg.LIVE
    assert sg.leg_status("RUB1", "SHORT") == sg.LIVE
    assert sg.leg_status("RUB2", "LONG") == sg.SHADOW
    assert sg.leg_status("RUB3", "SHORT") == sg.SHADOW


if __name__ == "__main__":
    test_tag_is_the_single_rub1_constant()
    test_both_directions_load_the_legacy_joblib_models()
    test_short_fires_the_legacy_model_at_its_original_threshold()
    test_active_trade_check_binds_posting_and_legacy_tag()
    test_cooldown_covers_the_legacy_tag()
    test_exactly_one_cornix_message_per_signal()
    test_legacy_models_are_byte_unchanged()
    test_register_says_live_live_shadow_shadow()
    print("OK — RUB1-revive model-tag + re-fire contract holds")

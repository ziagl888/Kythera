"""Standalone (DB-free) guard for the SMC-sniper model-tag contract.

Background (T-2026-CU-9050-026): send_cornix_signal used to recompute the
module tag as f"{strategy_code}_{tf}", so retrain-generation trades (artifact
model_id BB2_4H/TD2_4H) were written to ai_signals under the OLD tags
BB_4H/TD_4H — merging generations in every downstream stat and violating the
versioning rule (new generations post under a new tag).

Run: py -3.13 backtest/test_sniper_tag.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "25_smc_ml_sniper.py").read_text(encoding="utf-8")


def test_send_cornix_signal_requires_module_tag():
    sig = re.search(r"def send_cornix_signal\((.*?)\):", SRC, re.DOTALL)
    assert sig, "send_cornix_signal not found"
    params = sig.group(1)
    assert "module_tag" in params, "send_cornix_signal lost its module_tag parameter"
    assert not re.search(r"module_tag\s*=", params), (
        "module_tag must be a REQUIRED parameter — a default reintroduces the silent old-tag bug"
    )


def test_no_tag_recompute_in_send():
    body_match = re.search(r"def send_cornix_signal\(.*?\):\n(.*?)\ndef ", SRC, re.DOTALL)
    assert body_match, "send_cornix_signal body not found"
    body = body_match.group(1)
    assert not re.search(r"module_tag\s*=\s*f[\"']\{strategy_code", body), (
        "send_cornix_signal derives the tag from strategy_code/tf again — "
        "retrain generations would post under the old tag"
    )


def test_trade_call_passes_module_tag():
    call = re.search(r"send_cornix_signal\((.*?)\)\n\s*update_cooldown", SRC, re.DOTALL)
    assert call, "trade call site not found"
    assert "module_tag=module_tag" in call.group(1), (
        "evaluate_and_trade no longer passes the artifact tag to send_cornix_signal"
    )


def test_active_check_covers_legacy_tag():
    """Transitional dedup: open positions written under the pre-fix static tag
    (BB_4H/TD_4H) must still block a re-fire under the new tag — otherwise the
    same symbol/direction opens a second live position."""
    body_match = re.search(r"def evaluate_and_trade\(.*?\):\n(.*?)\ndef ", SRC, re.DOTALL)
    assert body_match, "evaluate_and_trade body not found"
    body = body_match.group(1)
    assert "model IN (%s, %s)" in body, "active-trade check no longer covers the legacy tag"
    assert "legacy_tag" in body, "legacy_tag derivation missing in evaluate_and_trade"


if __name__ == "__main__":
    test_send_cornix_signal_requires_module_tag()
    test_no_tag_recompute_in_send()
    test_trade_call_passes_module_tag()
    test_active_check_covers_legacy_tag()
    print("OK — sniper model-tag contract holds")

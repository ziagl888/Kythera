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


def test_send_cornix_signal_accepts_module_tag():
    sig = re.search(r"def send_cornix_signal\((.*?)\):", SRC, re.DOTALL)
    assert sig, "send_cornix_signal not found"
    assert "module_tag" in sig.group(1), "send_cornix_signal lost its module_tag parameter"


def test_recompute_only_as_fallback():
    body_match = re.search(r"def send_cornix_signal\(.*?\n(.*?)\ndef ", SRC, re.DOTALL)
    assert body_match, "send_cornix_signal body not found"
    body = body_match.group(1)
    # The static tag may only be derived when no tag was passed in.
    for m in re.finditer(r"^(\s*)module_tag\s*=\s*f\"\{strategy_code", body, re.MULTILINE):
        indent = len(m.group(1))
        assert indent > 4, "send_cornix_signal recomputes module_tag unconditionally again"
    assert "if not module_tag" in body, "fallback guard for missing module_tag is gone"


def test_trade_call_passes_module_tag():
    call = re.search(r"send_cornix_signal\((.*?)\)\n\s*update_cooldown", SRC, re.DOTALL)
    assert call, "trade call site not found"
    assert "module_tag=module_tag" in call.group(1), (
        "evaluate_and_trade no longer passes the artifact tag to send_cornix_signal"
    )


if __name__ == "__main__":
    test_send_cornix_signal_accepts_module_tag()
    test_recompute_only_as_fallback()
    test_trade_call_passes_module_tag()
    print("OK — sniper model-tag contract holds")

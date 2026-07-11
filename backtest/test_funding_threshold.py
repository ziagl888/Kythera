# backtest/test_funding_threshold.py
"""Standalone (DB-free) tests for the funding "EXTREME" alert threshold (P2.40).

Background: 20_funding_logger_bot posts a TOP20 "FUNDING EXTREME ALERT" whenever
a share of the top-20 coins are lopsidedly positive/negative. The old floor was
75%. But the funding baseline is mildly positive (~+0.01%), so ~75%+ of the
top-20 are routinely positive in the normal state — the 75 trigger fired the
"extreme" alert almost permanently. Operator decision (Michi 2026-07-11): raise
the floor to 95/85.

These tests pin the boundary behaviour of the extracted classifier so a revert
to the 75 floor (or an off-by-one on the >= comparison) fails loudly. No DB, no
network — classify_funding_extreme is pure.

Run: py -3.13 backtest/test_funding_threshold.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

# Pre-seed pandas/numpy before any patch.dict(sys.modules) block so a patched
# teardown can never rip numpy out from under a later import (known trap,
# memory patch-dict-sys-modules-numpy-teardown). The funding bot itself does
# not import pandas, but the loader convention stays uniform across the suite.
import pandas  # noqa: F401

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def _load_funding_bot():
    """Import 20_funding_logger_bot.py under a stable alias (digit prefix)."""
    spec = importlib.util.spec_from_file_location(
        "funding_logger_bot",
        os.path.join(REPO_ROOT, "20_funding_logger_bot.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "aiohttp": mock.MagicMock(),
            "core.config": mock.MagicMock(CH_MARKET_DATA=-1),
            "core.market_utils": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


funding = _load_funding_bot()


def test_75_no_longer_fires():
    """The core P2.40 regression: 75% positive is the normal state, not extreme."""
    triggered, _, _ = funding.classify_funding_extreme(75.0)
    assert not triggered, "75% positive must not trigger an EXTREME alert any more"
    # 75% negative (= 25% positive) must be equally silent.
    triggered_neg, _, _ = funding.classify_funding_extreme(25.0)
    assert not triggered_neg, "75% negative must not trigger either"


def test_positive_boundaries():
    # Just below the 85 floor: silent.
    assert funding.classify_funding_extreme(84.9) == (False, "", 0)
    # Exactly on the 85 floor: POSITIVE.
    triggered, direction, pct = funding.classify_funding_extreme(85.0)
    assert (triggered, direction) == (True, "POSITIVE") and pct == 85.0
    # 95 floor: still POSITIVE, reports the actual share.
    triggered, direction, pct = funding.classify_funding_extreme(95.0)
    assert (triggered, direction) == (True, "POSITIVE") and pct == 95.0
    # 100% positive.
    assert funding.classify_funding_extreme(100.0) == (True, "POSITIVE", 100.0)


def test_negative_boundaries():
    # 16% positive -> 84% negative: just below the floor, silent.
    assert funding.classify_funding_extreme(16.0) == (False, "", 0)
    # 15% positive -> 85% negative: NEGATIVE at the floor, pct is the negative share.
    triggered, direction, pct = funding.classify_funding_extreme(15.0)
    assert (triggered, direction) == (True, "NEGATIVE") and pct == 85.0
    # 5% positive -> 95% negative.
    triggered, direction, pct = funding.classify_funding_extreme(5.0)
    assert (triggered, direction) == (True, "NEGATIVE") and pct == 95.0
    # 0% positive -> 100% negative.
    assert funding.classify_funding_extreme(0.0) == (True, "NEGATIVE", 100.0)


def test_neutral_band_is_silent():
    for pos_pct in (50.0, 60.0, 70.0, 80.0, 84.9):
        assert funding.classify_funding_extreme(pos_pct) == (False, "", 0), (
            f"{pos_pct}% positive should be in the neutral band"
        )


def test_threshold_constant_is_9585():
    """The source of truth for the floors — a revert to [..., 75] fails here."""
    assert funding.FUNDING_EXTREME_THRESHOLDS == [95, 85]


if __name__ == "__main__":
    test_75_no_longer_fires()
    test_positive_boundaries()
    test_negative_boundaries()
    test_neutral_band_is_silent()
    test_threshold_constant_is_9585()
    print("OK — funding EXTREME threshold (95/85) boundaries hold")

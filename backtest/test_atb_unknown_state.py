# backtest/test_atb_unknown_state.py
"""
Unit tests for ATB1's unknown-state observe-only rule (P2.36).

After a state loss (``trendline_state.json`` missing or corrupt) TRENDLINE_STATE
resets to {}, so every coin falls back to prev_relation="unknown". The old inline
logic listed "unknown" in every break/bounce condition, so on the first cycle
after a state loss EVERY coin currently above/below its trendline emitted a fresh
BREAK event — a mass signal flood with real money (the old inline comment
admitted the bug outright).

classify_trendline_event now treats unknown as observe-only: it emits nothing
while the prior relation is unknown, and the scan loop still records the observed
relation so genuine transitions fire from the next cycle on. These tests pin that
invariant and differentially prove it against the pre-fix predicate.

Run with: pytest backtest/test_atb_unknown_state.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.config fails hard on missing secrets; the build machine ships an empty .env.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")


def _load_atb1():
    spec = importlib.util.spec_from_file_location(
        "atb1_bot",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "14_ai_atb_bot.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    # 14_ai_atb_bot imports heavy plotting / DB deps at module load; stub them so
    # the pure helper can be imported standalone and DB-free.
    with mock.patch.dict(
        "sys.modules",
        {
            "core.database": mock.MagicMock(),
            "core.charting": mock.MagicMock(),
            "core.market_utils": mock.MagicMock(),
            "core.trade_utils": mock.MagicMock(),
            "matplotlib": mock.MagicMock(),
            "matplotlib.pyplot": mock.MagicMock(),
            "matplotlib.gridspec": mock.MagicMock(),
            "matplotlib.ticker": mock.MagicMock(),
            "pandas_ta": mock.MagicMock(),
            "scipy.signal": mock.MagicMock(),
            "scipy.stats": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


atb1 = _load_atb1()


def _old_classify(prev, curr, distance, tolerance):
    """The pre-P2.36 break predicate, verbatim — "unknown" still in every list.

    This is the exact logic that produced the flood. The tests below assert that
    the shipped helper diverges from it precisely on the unknown case, so they
    fail against the pre-fix behaviour rather than merely against a missing name.
    """
    if prev in ["below", "near", "unknown"] and curr == "above" and distance > tolerance:
        return "TRENDLINE BREAK UP"
    if prev in ["above", "near", "unknown"] and curr == "below" and distance < -tolerance:
        return "TRENDLINE BREAK DOWN"
    return None


# ── unknown = observe-only, no event ─────────────────────────────────────────


def test_unknown_above_emits_no_break_up():
    # Fresh start, coin sits clearly above its line: the old code fired BREAK UP.
    assert atb1.classify_trendline_event("unknown", "above", 5.0, 1.0, [], [], 100.0, 105.0, 104.0) is None


def test_unknown_below_emits_no_break_down():
    assert atb1.classify_trendline_event("unknown", "below", -5.0, 1.0, [], [], 100.0, 95.0, 96.0) is None


def test_unknown_near_emits_no_bounce():
    # Even a picture-perfect bounce geometry stays silent while state is unknown.
    assert atb1.classify_trendline_event("unknown", "near", 0.0, 1.0, [99.5], [], 100.0, 100.2, 99.9) is None


def test_unknown_regresses_against_old_flood_logic():
    """Differential proof: same inputs, old predicate floods, new helper is silent."""
    assert _old_classify("unknown", "above", 5.0, 1.0) == "TRENDLINE BREAK UP"
    assert atb1.classify_trendline_event("unknown", "above", 5.0, 1.0, [], [], 100.0, 105.0, 104.0) is None

    assert _old_classify("unknown", "below", -5.0, 1.0) == "TRENDLINE BREAK DOWN"
    assert atb1.classify_trendline_event("unknown", "below", -5.0, 1.0, [], [], 100.0, 95.0, 96.0) is None


# ── genuine transitions still fire (no over-suppression) ─────────────────────


def test_known_below_to_above_still_breaks_up():
    assert atb1.classify_trendline_event("below", "above", 5.0, 1.0, [], [], 100.0, 105.0, 104.0) == "TRENDLINE BREAK UP"


def test_known_above_to_below_still_breaks_down():
    assert (
        atb1.classify_trendline_event("above", "below", -5.0, 1.0, [], [], 100.0, 95.0, 96.0) == "TRENDLINE BREAK DOWN"
    )


def test_known_above_to_near_still_bounces_up():
    # lows held above (line - tolerance) and price ticked up => bounce up
    assert (
        atb1.classify_trendline_event("above", "near", 0.0, 1.0, [99.5, 99.8], [], 100.0, 100.2, 99.9)
        == "BOUNCE UP FROM TRENDLINE"
    )


def test_known_below_to_near_still_bounces_down():
    # highs stayed below (line + tolerance) and price ticked down => bounce down
    assert (
        atb1.classify_trendline_event("below", "near", 0.0, 1.0, [], [100.5, 100.2], 100.0, 99.8, 100.1)
        == "BOUNCE DOWN FROM TRENDLINE"
    )


def test_break_up_needs_distance_beyond_tolerance():
    # above but within tolerance is not a break (distance !> tolerance)
    assert atb1.classify_trendline_event("below", "above", 0.5, 1.0, [], [], 100.0, 100.1, 100.0) is None


def test_no_event_when_relation_unchanged_above():
    # above -> above is neither a break nor a bounce
    assert atb1.classify_trendline_event("above", "above", 5.0, 1.0, [], [], 100.0, 105.0, 104.0) is None

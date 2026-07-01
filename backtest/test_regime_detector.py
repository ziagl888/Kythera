# backtest/test_regime_detector.py
"""
Unit tests for regime detector (BTC + Alt-Context classifiers, debounce).
Run with: pytest backtest/test_regime_detector.py -v
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from core.regime_logic import (
    classify_btc_regime,
    classify_alt_context,
    classify_regime,
    apply_debounce,
    ALT_CONTEXT_THRESHOLD_PCT,
    TREND_RETURN_THRESHOLD_4H_PCT,
    CHOP_RETURN_THRESHOLD_4H_PCT,
)


# ── BTC-Regime-Classifier ─────────────────────────────────────────────────────

def test_classify_btc_high_vola():
    features = {"btc_return_4h": 0.5, "btc_atr_4h_pct": 3.0}
    regime, conf = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert regime == "HIGH_VOLA"
    assert conf > 0.5


def test_classify_btc_trend_up():
    features = {"btc_return_4h": 2.5, "btc_atr_4h_pct": 0.5}
    regime, conf = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert regime == "TREND_UP"
    assert conf > 0


def test_classify_btc_trend_down():
    features = {"btc_return_4h": -2.5, "btc_atr_4h_pct": 0.5}
    regime, conf = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert regime == "TREND_DOWN"
    assert conf > 0


def test_classify_btc_chop():
    features = {"btc_return_4h": 0.2, "btc_atr_4h_pct": 0.5}
    regime, conf = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert regime == "CHOP"
    assert conf == pytest.approx(0.8)


def test_classify_btc_transition_mid_zone():
    # Mid-vola, moderate return (not clearly trend or chop)
    features = {"btc_return_4h": 1.0, "btc_atr_4h_pct": 1.2}
    regime, conf = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert regime == "TRANSITION"


def test_classify_btc_insufficient_data_returns_transition():
    features = {"btc_return_4h": None, "btc_atr_4h_pct": None}
    regime, conf = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert regime == "TRANSITION"
    assert conf == 0.0


# ── Alt-Context-Classifier ────────────────────────────────────────────────────

def test_classify_alt_strong_on_btcdom_fall():
    features = {"btcdom_return_24h": -2.5}
    context, conf = classify_alt_context(features)
    assert context == "ALT_STRONG"
    assert conf > 0.5


def test_classify_alt_weak_on_btcdom_rise():
    features = {"btcdom_return_24h": 2.5}
    context, conf = classify_alt_context(features)
    assert context == "ALT_WEAK"
    assert conf > 0.5


def test_classify_alt_neutral_in_dead_zone():
    features = {"btcdom_return_24h": 0.3}
    context, conf = classify_alt_context(features)
    assert context == "ALT_NEUTRAL"
    assert conf >= 0.5


def test_classify_alt_neutral_when_btcdom_missing():
    features = {"btcdom_return_24h": None}
    context, conf = classify_alt_context(features)
    assert context == "ALT_NEUTRAL"
    assert conf == pytest.approx(0.3)


def test_alt_classifier_independent_from_btc_regime():
    """Alt-Context only depends on btcdom, not btc features."""
    features_1 = {"btcdom_return_24h": -2.0, "btc_return_4h": 3.0, "btc_atr_4h_pct": 0.5}
    features_2 = {"btcdom_return_24h": -2.0, "btc_return_4h": -2.0, "btc_atr_4h_pct": 2.5}
    c1, _ = classify_alt_context(features_1)
    c2, _ = classify_alt_context(features_2)
    assert c1 == c2 == "ALT_STRONG"


def test_alt_confidence_scales_with_btcdom_magnitude():
    f_strong = {"btcdom_return_24h": -4.0}
    f_weak = {"btcdom_return_24h": -1.6}
    _, conf_strong = classify_alt_context(f_strong)
    _, conf_weak = classify_alt_context(f_weak)
    assert conf_strong > conf_weak


# ── Combined Classifier ───────────────────────────────────────────────────────

def test_classify_regime_returns_both_axes():
    features = {
        "btc_return_4h": 2.0, "btc_atr_4h_pct": 0.5,
        "btcdom_return_24h": -2.0,
    }
    result = classify_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert "regime" in result
    assert "alt_context" in result
    assert result["regime"] == "TREND_UP"
    assert result["alt_context"] == "ALT_STRONG"


def test_combined_confidence_is_min_of_both():
    features = {
        "btc_return_4h": 2.0, "btc_atr_4h_pct": 0.5,
        "btcdom_return_24h": -1.6,
    }
    result = classify_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert result["confidence"] == min(result["confidence_btc"], result["confidence_alt"])


# ── Debounce ──────────────────────────────────────────────────────────────────

def _make_mock_conn(row=None):
    """Helper to create a mock DB connection."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=row)
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()
    return conn, cur


def test_debounce_btc_axis_no_change_stays_stable():
    """Same regime as current → no change."""
    # Simulates current=TREND_UP, raw=TREND_UP
    row = ("TREND_UP", "ALT_NEUTRAL",
           datetime(2026, 1, 1), datetime(2026, 1, 1),
           None, 0, None, 0)
    conn, cur = _make_mock_conn(row)

    result = apply_debounce(conn, "TREND_UP", "ALT_NEUTRAL", 0.8,
                            datetime.now(timezone.utc))
    assert result["btc_regime_changed"] is False
    assert result["effective_regime"] == "TREND_UP"


def test_debounce_btc_axis_confirmed_change_after_2_checks():
    """TREND_UP → CHOP: first check sets pending, second confirms change."""
    # First check: pend_count=0, no pending
    row1 = ("TREND_UP", "ALT_NEUTRAL",
            datetime(2026, 1, 1), datetime(2026, 1, 1),
            None, 0, None, 0)
    conn1, cur1 = _make_mock_conn(row1)
    result1 = apply_debounce(conn1, "CHOP", "ALT_NEUTRAL", 0.8,
                             datetime.now(timezone.utc))
    assert result1["btc_regime_changed"] is False  # Only first check

    # Second check: pending=CHOP, pend_count=1 → should confirm
    row2 = ("TREND_UP", "ALT_NEUTRAL",
            datetime(2026, 1, 1), datetime(2026, 1, 1),
            "CHOP", 1, None, 0)
    conn2, cur2 = _make_mock_conn(row2)
    result2 = apply_debounce(conn2, "CHOP", "ALT_NEUTRAL", 0.8,
                             datetime.now(timezone.utc))
    assert result2["btc_regime_changed"] is True
    assert result2["effective_regime"] == "CHOP"


def test_debounce_alt_axis_confirmed_change_after_2_checks():
    row = ("TREND_UP", "ALT_NEUTRAL",
           datetime(2026, 1, 1), datetime(2026, 1, 1),
           None, 0, "ALT_STRONG", 1)
    conn, cur = _make_mock_conn(row)
    result = apply_debounce(conn, "TREND_UP", "ALT_STRONG", 0.8,
                            datetime.now(timezone.utc))
    assert result["alt_context_changed"] is True
    assert result["effective_alt_context"] == "ALT_STRONG"


def test_debounce_axes_independent():
    """Only alt_context changes, BTC stays the same."""
    row = ("TREND_UP", "ALT_NEUTRAL",
           datetime(2026, 1, 1), datetime(2026, 1, 1),
           None, 0, "ALT_STRONG", 1)
    conn, cur = _make_mock_conn(row)
    result = apply_debounce(conn, "TREND_UP", "ALT_STRONG", 0.8,
                            datetime.now(timezone.utc))
    assert result["btc_regime_changed"] is False
    assert result["alt_context_changed"] is True


def test_debounce_both_axes_change_simultaneously():
    row = ("TREND_UP", "ALT_NEUTRAL",
           datetime(2026, 1, 1), datetime(2026, 1, 1),
           "CHOP", 1, "ALT_STRONG", 1)
    conn, cur = _make_mock_conn(row)
    result = apply_debounce(conn, "CHOP", "ALT_STRONG", 0.8,
                            datetime.now(timezone.utc))
    assert result["btc_regime_changed"] is True
    assert result["alt_context_changed"] is True


def test_debounce_reset_on_raw_flicker():
    """If raw flickers back to current, pending is reset."""
    row = ("TREND_UP", "ALT_NEUTRAL",
           datetime(2026, 1, 1), datetime(2026, 1, 1),
           "CHOP", 1, None, 0)
    conn, cur = _make_mock_conn(row)
    # Raw goes back to TREND_UP
    result = apply_debounce(conn, "TREND_UP", "ALT_NEUTRAL", 0.8,
                            datetime.now(timezone.utc))
    assert result["btc_regime_changed"] is False
    # Pending should be cleared
    update_call = conn.cursor().__enter__().execute.call_args_list
    # Just verify commit was called (state saved)
    conn.commit.assert_called()

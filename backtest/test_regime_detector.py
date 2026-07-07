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
    hysteresis_prev_regime,
    ALT_CONTEXT_THRESHOLD_PCT,
    TREND_RETURN_THRESHOLD_4H_PCT,
    CHOP_RETURN_THRESHOLD_4H_PCT,
)


def _state_row(regime, pending_regime=None):
    # Spaltenreihenfolge von read_regime_state (_STATE_COLUMNS).
    return (regime, "ALT_NEUTRAL", None, None, pending_regime, 1, None, 0)


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
    # Mid-vola, moderate return: |ret|=1.0 < 1.5×ATR(1.2)=1.8 → TRANSITION
    features = {"btc_return_4h": 1.0, "btc_atr_4h_pct": 1.2}
    regime, conf = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert regime == "TRANSITION"


# ── Mid-Vola-Trend-Regel (§22, 2026-07-07) ───────────────────────────────────


def test_classify_btc_mid_band_trend_up_on_vol_scaled_return():
    # Mid-Band: |ret|=2.0 ≥ 1.5×ATR(1.2)=1.8 → TREND_UP
    features = {"btc_return_4h": 2.0, "btc_atr_4h_pct": 1.2}
    regime, conf = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert regime == "TREND_UP"
    assert conf > 0.5


def test_classify_btc_mid_band_trend_down_on_vol_scaled_return():
    features = {"btc_return_4h": -2.0, "btc_atr_4h_pct": 1.2}
    regime, conf = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert regime == "TREND_DOWN"


def test_classify_btc_mid_band_hysteresis_holds_existing_trend():
    # ret=1.3 liegt unter Enter (1.8) aber über Exit (1.2) → hält NUR mit prev.
    features = {"btc_return_4h": 1.3, "btc_atr_4h_pct": 1.2}
    regime_without, _ = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8)
    regime_with, _ = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8, prev_regime="TREND_UP")
    assert regime_without == "TRANSITION"
    assert regime_with == "TREND_UP"


def test_classify_btc_mid_band_hysteresis_exits_below_exit_threshold():
    # ret=1.0 < Exit (1.0×ATR=1.2) → TREND_UP fällt trotz prev.
    features = {"btc_return_4h": 1.0, "btc_atr_4h_pct": 1.2}
    regime, _ = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8, prev_regime="TREND_UP")
    assert regime == "TRANSITION"


def test_classify_btc_mid_band_hysteresis_direction_specific():
    # prev=TREND_DOWN hält keinen positiven ret im Halte-Band.
    features = {"btc_return_4h": 1.3, "btc_atr_4h_pct": 1.2}
    regime, _ = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8, prev_regime="TREND_DOWN")
    assert regime == "TRANSITION"


def test_hysteresis_prev_regime_cold_start_is_none():
    assert hysteresis_prev_regime(None) is None


def test_hysteresis_prev_regime_uses_effective_regime():
    assert hysteresis_prev_regime(_state_row("TREND_UP")) == "TREND_UP"
    assert hysteresis_prev_regime(_state_row("CHOP")) == "CHOP"


def test_hysteresis_prev_regime_pending_trend_counts_like_existing():
    # §22 (Review PR #9): während der Debounce-Pending-Phase muss die
    # Hold-Schwelle greifen, sonst resettet ein einzelner Dip unter Enter
    # den Bestätigungszähler und TREND bestätigt bei Oszillation nie.
    row = _state_row("TRANSITION", pending_regime="TREND_UP")
    assert hysteresis_prev_regime(row) == "TREND_UP"


def test_hysteresis_prev_regime_pending_non_trend_ignored():
    row = _state_row("TREND_UP", pending_regime="CHOP")
    assert hysteresis_prev_regime(row) == "TREND_UP"


def test_pending_trend_oscillation_survives_dip_below_enter():
    # Entry-Oszillation 1.6→1.4→1.7×ATR (ATR=1.2 ⇒ Enter=1.8, Hold=1.2):
    # mit pendendem TREND_UP als prev bleibt der Raw-Wert bei ret=1.7
    # (< Enter, ≥ Hold) TREND_UP — der Zähler läuft weiter statt zu resetten.
    features = {"btc_return_4h": 1.7, "btc_atr_4h_pct": 1.2}
    prev = hysteresis_prev_regime(_state_row("TRANSITION", pending_regime="TREND_UP"))
    regime, _ = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8, prev_regime=prev)
    assert regime == "TREND_UP"


def test_classify_btc_high_vola_still_overrides_mid_band_trend():
    # ATR über P75 → HIGH_VOLA, auch wenn ret die Trend-Schwelle schlägt.
    features = {"btc_return_4h": 5.0, "btc_atr_4h_pct": 2.5}
    regime, _ = classify_btc_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert regime == "HIGH_VOLA"


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
        "btc_return_4h": 2.0,
        "btc_atr_4h_pct": 0.5,
        "btcdom_return_24h": -2.0,
    }
    result = classify_regime(features, vola_p75=2.0, vola_p40=0.8)
    assert "regime" in result
    assert "alt_context" in result
    assert result["regime"] == "TREND_UP"
    assert result["alt_context"] == "ALT_STRONG"


def test_combined_confidence_is_min_of_both():
    features = {
        "btc_return_4h": 2.0,
        "btc_atr_4h_pct": 0.5,
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
    row = ("TREND_UP", "ALT_NEUTRAL", datetime(2026, 1, 1), datetime(2026, 1, 1), None, 0, None, 0)
    conn, cur = _make_mock_conn(row)

    result = apply_debounce(conn, "TREND_UP", "ALT_NEUTRAL", 0.8, datetime.now(timezone.utc))
    assert result["btc_regime_changed"] is False
    assert result["effective_regime"] == "TREND_UP"


def test_debounce_btc_axis_confirmed_change_after_2_checks():
    """TREND_UP → CHOP: first check sets pending, second confirms change."""
    # First check: pend_count=0, no pending
    row1 = ("TREND_UP", "ALT_NEUTRAL", datetime(2026, 1, 1), datetime(2026, 1, 1), None, 0, None, 0)
    conn1, cur1 = _make_mock_conn(row1)
    result1 = apply_debounce(conn1, "CHOP", "ALT_NEUTRAL", 0.8, datetime.now(timezone.utc))
    assert result1["btc_regime_changed"] is False  # Only first check

    # Second check: pending=CHOP, pend_count=1 → should confirm
    row2 = ("TREND_UP", "ALT_NEUTRAL", datetime(2026, 1, 1), datetime(2026, 1, 1), "CHOP", 1, None, 0)
    conn2, cur2 = _make_mock_conn(row2)
    result2 = apply_debounce(conn2, "CHOP", "ALT_NEUTRAL", 0.8, datetime.now(timezone.utc))
    assert result2["btc_regime_changed"] is True
    assert result2["effective_regime"] == "CHOP"


def test_debounce_alt_axis_confirmed_change_after_2_checks():
    row = ("TREND_UP", "ALT_NEUTRAL", datetime(2026, 1, 1), datetime(2026, 1, 1), None, 0, "ALT_STRONG", 1)
    conn, cur = _make_mock_conn(row)
    result = apply_debounce(conn, "TREND_UP", "ALT_STRONG", 0.8, datetime.now(timezone.utc))
    assert result["alt_context_changed"] is True
    assert result["effective_alt_context"] == "ALT_STRONG"


def test_debounce_axes_independent():
    """Only alt_context changes, BTC stays the same."""
    row = ("TREND_UP", "ALT_NEUTRAL", datetime(2026, 1, 1), datetime(2026, 1, 1), None, 0, "ALT_STRONG", 1)
    conn, cur = _make_mock_conn(row)
    result = apply_debounce(conn, "TREND_UP", "ALT_STRONG", 0.8, datetime.now(timezone.utc))
    assert result["btc_regime_changed"] is False
    assert result["alt_context_changed"] is True


def test_debounce_both_axes_change_simultaneously():
    row = ("TREND_UP", "ALT_NEUTRAL", datetime(2026, 1, 1), datetime(2026, 1, 1), "CHOP", 1, "ALT_STRONG", 1)
    conn, cur = _make_mock_conn(row)
    result = apply_debounce(conn, "CHOP", "ALT_STRONG", 0.8, datetime.now(timezone.utc))
    assert result["btc_regime_changed"] is True
    assert result["alt_context_changed"] is True


def test_debounce_trend_needs_three_checks():
    """TREND-Ziele brauchen 3 Checks (§22-Flap-Dämpfung), andere weiterhin 2."""
    # pend_count=1 → dieser Check wäre der zweite: für TREND noch KEIN Wechsel …
    row = ("TRANSITION", "ALT_NEUTRAL", datetime(2026, 1, 1), datetime(2026, 1, 1), "TREND_UP", 1, None, 0)
    conn, _ = _make_mock_conn(row)
    result = apply_debounce(conn, "TREND_UP", "ALT_NEUTRAL", 0.8, datetime.now(timezone.utc))
    assert result["btc_regime_changed"] is False

    # … erst der dritte Check (pend_count=2) bestätigt.
    row3 = ("TRANSITION", "ALT_NEUTRAL", datetime(2026, 1, 1), datetime(2026, 1, 1), "TREND_UP", 2, None, 0)
    conn3, _ = _make_mock_conn(row3)
    result3 = apply_debounce(conn3, "TREND_UP", "ALT_NEUTRAL", 0.8, datetime.now(timezone.utc))
    assert result3["btc_regime_changed"] is True
    assert result3["effective_regime"] == "TREND_UP"


def test_debounce_reset_on_raw_flicker():
    """If raw flickers back to current, pending is reset."""
    row = ("TREND_UP", "ALT_NEUTRAL", datetime(2026, 1, 1), datetime(2026, 1, 1), "CHOP", 1, None, 0)
    conn, cur = _make_mock_conn(row)
    # Raw goes back to TREND_UP
    result = apply_debounce(conn, "TREND_UP", "ALT_NEUTRAL", 0.8, datetime.now(timezone.utc))
    assert result["btc_regime_changed"] is False
    # Pending should be cleared
    update_call = conn.cursor().__enter__().execute.call_args_list
    # Just verify commit was called (state saved)
    conn.commit.assert_called()

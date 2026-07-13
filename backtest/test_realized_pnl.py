# backtest/test_realized_pnl.py
"""
Unit tests for core/realized_pnl.py (T-2026-CU-9050-115) — the target-weighted,
leveraged realized-PnL math behind the Sentiment-Tracker report.

Position model under test: stake split equally across N targets; hitting
target i realises 1/N at the target price; the unrealised rest closes at
close_price. Result × leverage, clamped at -100%.

Run with: pytest backtest/test_realized_pnl.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.realized_pnl import (  # noqa: E402
    MAX_ABS_MOVE_PCT,
    parse_leverage,
    realized_pnl_pct,
    weighted_move_pct,
)

# ── parse_leverage ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20x", 20.0),
        ("20X", 20.0),
        (" 25x ", 25.0),
        ("20", 20.0),
        (20, 20.0),
        (12.5, 12.5),
        ("1x", 1.0),
    ],
)
def test_parse_leverage_valid(raw, expected):
    assert parse_leverage(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "x", "abc", "0x", 0, -5, "-3x"])
def test_parse_leverage_invalid(raw):
    assert parse_leverage(raw) is None


# ── weighted_move_pct: the operator example ──────────────────────────────────
# "ein Trade hat 4 Targets, beim Erreichen von Target 1 werden 25% realisiert;
#  20x Hebel: 5% Preisänderung wären 100% Gewinn."


def test_single_target_full_move_is_5_pct():
    # LONG entry 100, one target at 105, hit → close at 105: move = +5%
    move = weighted_move_pct("LONG", 100.0, 105.0, [105.0], 1)
    assert move == pytest.approx(5.0)
    # ... and at 20x that is +100% realized
    assert realized_pnl_pct("LONG", 100.0, 105.0, [105.0], 1, "20x") == pytest.approx(100.0)


def test_four_targets_tp1_then_sl_at_entry():
    # LONG entry 100, targets 105/110/115/120. TP1 hit (25% at +5%), SL pulled
    # to entry, rest (75%) closes at 100 (0%): weighted move = 5/4 = 1.25%.
    move = weighted_move_pct("LONG", 100.0, 100.0, [105.0, 110.0, 115.0, 120.0], 1)
    assert move == pytest.approx(1.25)
    assert realized_pnl_pct("LONG", 100.0, 100.0, [105.0, 110.0, 115.0, 120.0], 1, "20x") == pytest.approx(25.0)


def test_all_targets_hit():
    # k=N, close_price = last target (monitor sets exactly that on
    # ALL TARGETS HIT): weighted = mean of the four target moves.
    targets = [105.0, 110.0, 115.0, 120.0]
    move = weighted_move_pct("LONG", 100.0, 120.0, targets, 4)
    assert move == pytest.approx((5.0 + 10.0 + 15.0 + 20.0) / 4)


def test_sl_without_any_target():
    # k=0: full position closes at SL. LONG entry 100 → SL 95 = -5%.
    move = weighted_move_pct("LONG", 100.0, 95.0, [105.0, 110.0], 0)
    assert move == pytest.approx(-5.0)
    # At 20x that is exactly the liquidation floor.
    assert realized_pnl_pct("LONG", 100.0, 95.0, [105.0, 110.0], 0, "20x") == pytest.approx(-100.0)


def test_short_direction_inverts():
    # SHORT entry 100, targets below, TP1 95 hit, rest closes at entry (SL@entry).
    move = weighted_move_pct("SHORT", 100.0, 100.0, [95.0, 90.0], 1)
    assert move == pytest.approx(2.5)
    # SHORT losing: price rises to 105 with no TP → -5%.
    move_loss = weighted_move_pct("SHORT", 100.0, 105.0, [95.0], 0)
    assert move_loss == pytest.approx(-5.0)


def test_sl_after_tp2_uses_target_prices_for_hit_parts():
    # LONG entry 100, targets 105/110/115/120, TP2 hit, then SL at target1
    # (trailing SL = previous target): 25% at +5, 25% at +10, 50% at +5.
    move = weighted_move_pct("LONG", 100.0, 105.0, [105.0, 110.0, 115.0, 120.0], 2)
    assert move == pytest.approx((5.0 + 10.0 + 5.0 + 5.0) / 4)


def test_horizon_timeout_closes_rest_at_market():
    # HORIZON_TIMEOUT (monitor closes at the candle close, k targets already
    # hit): 25% at +5%, 75% at the market close of +2%.
    move = weighted_move_pct("LONG", 100.0, 102.0, [105.0, 110.0, 115.0, 120.0], 1)
    assert move == pytest.approx((5.0 + 3 * 2.0) / 4)


def test_direction_is_normalised():
    assert weighted_move_pct(" long ", 100.0, 105.0, [105.0], 1) == pytest.approx(5.0)
    assert weighted_move_pct("Short", 100.0, 95.0, [90.0], 0) == pytest.approx(5.0)


# ── clamps and invalid input ─────────────────────────────────────────────────


def test_targets_hit_is_clamped_to_target_count():
    # Legacy rows can carry targets_hit beyond len(targets) (e.g. classic
    # status "4" on a 3-target trade) — clamp, never IndexError.
    move_over = weighted_move_pct("LONG", 100.0, 115.0, [105.0, 110.0, 115.0], 7)
    assert move_over == pytest.approx((5.0 + 10.0 + 15.0) / 3)
    move_neg = weighted_move_pct("LONG", 100.0, 95.0, [105.0], -3)
    assert move_neg == pytest.approx(-5.0)


def test_loss_is_clamped_at_minus_100():
    # -10% move × 20x = -200% raw → clamped to -100% (liquidation floor).
    assert realized_pnl_pct("LONG", 100.0, 90.0, [105.0], 0, "20x") == pytest.approx(-100.0)


def test_outlier_move_is_rejected():
    # |move| > MAX_ABS_MOVE_PCT pre-leverage = data bug → None, not a number.
    assert MAX_ABS_MOVE_PCT == pytest.approx(100.0)
    assert realized_pnl_pct("LONG", 1.0, 2.5, [3.0], 0, "20x") is None


def test_outlier_close_leg_is_rejected_even_when_diluted_by_hit_targets():
    # Review finding 2026-07-13: with k=3 of 4 targets hit, a data-bugged
    # +150% close leg dilutes to (5+10+15+150)/4 ≈ 45% and would pass a gate
    # on the weighted move only. The RAW close leg must be gated too.
    targets = [105.0, 110.0, 115.0, 120.0]
    assert weighted_move_pct("LONG", 100.0, 250.0, targets, 3) is None
    assert realized_pnl_pct("LONG", 100.0, 250.0, targets, 3, "20x") is None
    # ... while a legit close leg at the boundary still computes.
    assert weighted_move_pct("LONG", 100.0, 200.0, targets, 3) is not None


@pytest.mark.parametrize(
    ("direction", "entry", "close", "targets", "hits"),
    [
        ("LONG", 0.0, 105.0, [105.0], 0),  # entry <= 0
        ("LONG", 100.0, 0.0, [105.0], 0),  # close <= 0
        ("LONG", 100.0, 105.0, [], 0),  # no targets
        ("LONG", 100.0, 105.0, [0.0, 105.0], 1),  # zero target price
        ("SIDEWAYS", 100.0, 105.0, [105.0], 1),  # unknown direction
        ("LONG", None, 105.0, [105.0], 1),  # entry not numeric
        ("LONG", 100.0, 105.0, ["abc"], 1),  # target not numeric
    ],
)
def test_invalid_inputs_return_none(direction, entry, close, targets, hits):
    assert weighted_move_pct(direction, entry, close, targets, hits) is None


def test_missing_leverage_returns_none():
    assert realized_pnl_pct("LONG", 100.0, 105.0, [105.0], 1, None) is None
    assert realized_pnl_pct("LONG", 100.0, 105.0, [105.0], 1, "kaputt") is None

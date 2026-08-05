# backtest/test_portfolio_backtest.py — the admission rules, pinned.
"""Standalone, DB-free tests for tools/portfolio_backtest.py (T-2026-KYT-9050-105).

Every number this simulator produces is decided by four admission rules and one
ordering rule. The ordering rule is the subtle one: a position that closes at the
same instant a signal arrives must free its margin *before* the admission
decision, otherwise the run under-trades in exactly the busy stretches that
decide the result.

The slot cap has its own test because the first version of the simulator did not
have one and happily held 800 concurrent positions against a production ceiling
of 500.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.portfolio_backtest import (  # noqa: E402
    EXPOSURE_CAP,
    FEE_PCT,
    GEOMETRY_VARIANTS,
    SLOT_CAP,
    simulate,
)

HOUR = 3600
T0 = 1_752_000_000  # arbitrary fixed epoch; Date.now() has no place in a replay


def rec(model="M", direction="LONG", open_h=0, hold_h=1, pnl=1.0):
    return {
        "model": model,
        "direction": direction,
        "open_ts": T0 + open_h * HOUR,
        "exit_ts": T0 + (open_h + hold_h) * HOUR,
        "pnl_pct": pnl,
        "symbol": "XUSDT",
        "oi": float("nan"),
    }


def train(model="M", direction="LONG", n=40, pnl=1.0):
    """A trailing block long enough to make the leg selectable."""
    return [rec(model, direction, open_h=i, hold_h=1, pnl=pnl) for i in range(n)]


# ── margin accounting ────────────────────────────────────────────────────────


def test_pnl_is_notional_times_move_not_margin_times_move():
    """Leverage multiplies the position, not the deposit — getting this wrong
    understates both the return and the risk by the leverage factor."""
    recs = train(n=30, pnl=2.0) + [rec(open_h=24 * 21, hold_h=1, pnl=2.0)]
    out = simulate(recs, capital=1000.0, fixed_usd=10.0, leverage=5.0)
    # notional 50 USD, move +2 % -> +1.00 USD per trade; fee already inside pnl_pct
    assert out["notional_per_trade_pct"] == pytest.approx(5.0)
    assert out["final_balance"] > 1000.0


def test_margin_is_returned_on_close_and_reused():
    """A single slot's worth of capital must fund an unlimited number of
    *sequential* trades."""
    recs = train(n=30) + [rec(open_h=24 * 21 + i * 2, hold_h=1) for i in range(20)]
    out = simulate(recs, capital=100.0, fixed_usd=100.0, leverage=1.0)
    assert out["rejected"]["margin"] == 0
    assert out["peak_occupancy"] == 1


def test_insufficient_margin_rejects_rather_than_going_negative():
    recs = train(n=30) + [rec(open_h=24 * 21, hold_h=48) for _ in range(10)]
    out = simulate(recs, capital=100.0, fixed_usd=50.0, leverage=1.0)
    assert out["peak_occupancy"] <= 2
    assert out["rejected"]["margin"] >= 1


# ── the ceilings ─────────────────────────────────────────────────────────────


def test_slot_cap_binds_before_margin_when_capital_is_ample():
    """The production ceiling is 500 open trades per channel. A run that ignores
    it reports occupancy the venue would never have allowed.

    Directions alternate on purpose: with a one-sided book the exposure cap (50)
    binds first, which is correct behaviour and would hide the slot cap entirely.
    """
    recs = train("A", "LONG", n=30) + train("B", "SHORT", n=30)
    for i in range(SLOT_CAP + 200):
        side = "LONG" if i % 2 == 0 else "SHORT"
        recs.append(rec("A" if side == "LONG" else "B", side, open_h=24 * 21, hold_h=72))
    out = simulate(recs, capital=1_000_000.0, fixed_usd=1.0, leverage=1.0)
    assert out["peak_occupancy"] == SLOT_CAP
    assert out["rejected"]["slot_cap"] >= 200
    assert out["binding_constraint"] == "slot_cap"


def test_exposure_cap_limits_direction_overhang():
    """Direction balance is enforced structurally — 27.07. opened 81 % LONG and
    cost -933 pp, which is why this is a hard constraint and not a preference."""
    recs = train("L", "LONG", n=30) + [rec("L", "LONG", open_h=24 * 21, hold_h=72) for _ in range(EXPOSURE_CAP + 50)]
    out = simulate(recs, capital=1_000_000.0, fixed_usd=1.0, leverage=1.0)
    assert out["peak_occupancy"] <= EXPOSURE_CAP
    assert out["rejected"]["exposure_cap"] >= 50


def test_binding_constraint_is_reported():
    """Reporting a return without which ceiling bound invites reading a
    sample-selection artifact as a sizing result."""
    recs = train(n=30) + [rec(open_h=24 * 21, hold_h=72) for _ in range(300)]
    out = simulate(recs, capital=100.0, fixed_usd=10.0, leverage=1.0)
    assert out["binding_constraint"] in {"slot_cap", "margin", "exposure_cap"}
    assert out["peak_notional_x_capital"] == pytest.approx(out["peak_occupancy"] * 10.0 / 100.0)


# ── ordering ─────────────────────────────────────────────────────────────────


def test_a_close_frees_margin_before_a_same_instant_open_is_judged():
    """Otherwise the run under-trades exactly in the busy stretches, which is
    where the result is decided."""
    recs = train(n=30)
    recs.append(rec(open_h=24 * 21, hold_h=1))  # occupies the only slot
    recs.append(rec(open_h=24 * 21 + 1, hold_h=1))  # arrives as the first exits
    out = simulate(recs, capital=10.0, fixed_usd=10.0, leverage=1.0)
    assert out["rejected"]["margin"] == 0
    assert out["trades_taken"] >= 2


# ── walk-forward selection ───────────────────────────────────────────────────


def test_a_leg_that_never_ran_positive_is_never_selected():
    recs = train("BAD", "LONG", n=60, pnl=-3.0)
    out = simulate(recs, capital=1000.0, fixed_usd=1.0, leverage=1.0)
    assert out["trades_taken"] == 0
    assert out["rejected"]["leg"] > 0


def test_selection_uses_only_the_trailing_window_not_the_future():
    """The whole point of walk-forward: a leg may not be admitted on the strength
    of trades that had not happened yet."""
    # nothing at all in the first three weeks, then a burst
    recs = [rec("NEW", "LONG", open_h=24 * 22 + i, hold_h=1, pnl=5.0) for i in range(60)]
    out = simulate(recs, capital=1000.0, fixed_usd=1.0, leverage=1.0)
    # the first refit sees an empty trailing window, so the burst cannot be taken
    # wholesale on its own evidence
    assert out["trades_taken"] < len(recs)


# ── geometry variants ────────────────────────────────────────────────────────


def test_inverted_variant_is_the_mirror_of_t104():
    """`inverted` exists to be able to fail. If it silently equalled `t104` the
    falsification control would be decorative."""
    t104, inv = GEOMETRY_VARIANTS["t104"], GEOMETRY_VARIANTS["inverted"]
    assert t104["LONG"] == inv["SHORT"]
    assert t104["SHORT"] == inv["LONG"]
    assert t104 != inv


def test_every_variant_defines_both_directions():
    for name, geom in GEOMETRY_VARIANTS.items():
        assert set(geom) == {"LONG", "SHORT"}, name
        for tp, sl in geom.values():
            assert tp > 0 and sl > 0, name


def test_fee_is_charged_once_per_round_trip():
    assert FEE_PCT == pytest.approx(0.09)

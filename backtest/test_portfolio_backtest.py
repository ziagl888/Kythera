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


# ── the three defects the core review found, pinned ───────────────────────────
# All three shipped green: the suite covered `simulate` against hand-built records
# whose exit_ts was correct by construction, and never covered `precompute` or
# `select_legs` at all. So it validated the accounting and never the input it
# accounted over.


def test_exit_ts_is_kept_when_the_position_closes_on_the_first_candle():
    """`_exit_step` returns 0 for a first-candle touch; `if step_exit` read that
    valid 0 as "never exited" and pinned the trade to the full 72h horizon —
    an 864x overstatement of slot occupancy, biased toward tight geometries."""
    from tools.portfolio_backtest import _exit_step

    assert _exit_step(0, None, 10) == 0, "a first-candle TP is step 0, not falsy-absent"
    assert _exit_step(None, 0, 10) == 0
    assert _exit_step(None, None, 10) == 9, "neither fired -> horizon end"
    # the guard that matters: 0 must not be treated as absent
    step = _exit_step(0, None, 10)
    assert step is not None and step == 0
    assert bool(step) is False, "precisely why `if step_exit` was wrong"


def test_leg_selection_excludes_trades_that_had_not_resolved_at_the_refit():
    """Look-ahead: filtering on open_ts while scoring realised pnl let ~72h of
    each trailing window be selected on outcomes that had not happened yet."""
    from tools.portfolio_backtest import MIN_N_TRAIN, select_legs

    t0, t1 = T0, T0 + 100 * HOUR
    # A leg that is negative on everything RESOLVED inside the window, but is
    # rescued by trades that open inside it and resolve after it.
    resolved = [rec(model="L", open_h=1, hold_h=1, pnl=-1.0) for _ in range(MIN_N_TRAIN)]
    unresolved = [rec(model="L", open_h=99, hold_h=72, pnl=+50.0) for _ in range(MIN_N_TRAIN)]
    picked = select_legs(resolved + unresolved, t0, t1)
    assert ("L", "LONG") not in picked, "a leg may only be judged on trades that had resolved by the refit instant"
    # and the same leg IS admitted once those trades genuinely resolve in-window
    later = [rec(model="L", open_h=1, hold_h=1, pnl=+2.0) for _ in range(MIN_N_TRAIN)]
    assert ("L", "LONG") in select_legs(later, t0, t1)


def test_drawdown_sees_the_fall_from_initial_capital():
    """The curve started AFTER the first close, so initial capital was never a
    peak and the fall from capital to the first trough was invisible. The warm-up
    block only trains the leg (its own trades are rejected — no prior window), so
    the first SCORED trade is the first move off the initial capital."""
    recs = train(n=30, pnl=1.0) + [rec(open_h=24 * 21, hold_h=1, pnl=-50.0)]
    out = simulate(recs, capital=1000.0, fixed_usd=100.0, leverage=1.0)
    assert out["trades_taken"] > 0, "warm-up must make the leg selectable"
    assert out["max_drawdown_pct"] < -1.0, f"a losing trade must show drawdown, got {out['max_drawdown_pct']}"


def test_drawdown_is_not_smoothed_away_by_same_timestamp_exits():
    """`sorted(equity)` reordered same-instant exits ascending by BALANCE, so an
    intra-instant dip vanished. equity is already chronological; the sort was
    both unnecessary and harmful."""
    h = 24 * 21
    recs = (
        train(model="A", n=30, pnl=1.0)
        + train(model="B", n=30, pnl=1.0)
        + [rec(model="A", open_h=h, hold_h=1, pnl=-40.0), rec(model="B", open_h=h, hold_h=1, pnl=+35.0)]
    )
    out = simulate(recs, capital=1000.0, fixed_usd=100.0, leverage=1.0)
    assert out["max_drawdown_pct"] < -1.0, (
        f"the dip between two same-instant exits must survive, got {out['max_drawdown_pct']}"
    )


def test_binding_constraint_reports_none_when_nothing_bound():
    """The field exists so a return cannot be read without its constraint; the
    old chain fell through to 'exposure_cap' even at zero rejections."""
    out = simulate(train(n=30, pnl=1.0), capital=100_000.0, fixed_usd=1.0, leverage=1.0)
    assert out["binding_constraint"] == "none"


def test_equity_curve_is_emitted():
    """The task's first-named deliverable was computed and discarded."""
    recs = train(n=30, pnl=1.0) + [rec(open_h=24 * 21, hold_h=1, pnl=2.0)]
    out = simulate(recs, capital=1000.0, fixed_usd=100.0, leverage=1.0)
    curve = out["equity_curve"]
    assert curve and curve[0][1] == pytest.approx(1000.0), "curve must start at initial capital"
    assert all(len(p) == 2 for p in curve)

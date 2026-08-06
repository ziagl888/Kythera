# backtest/test_capital_split_backtest.py — the transfer rules, pinned.
"""Standalone, DB-free tests for tools/capital_split_backtest.py (T-2026-KYT-9050-108).

The scheme under test has one analytic property worth pinning before any real
data touches it: with equal skim and refill fractions every closed trade moves
half its PnL into each bucket, so available and reserve stay identical and the
whole construction collapses to a single bucket at half the size fraction. The
tests pin that equivalence, the two places it legitimately breaks (an exhausted
reserve, a halved margin pool), and the one variant that is not a no-op (the
one-way ratchet).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.capital_split_backtest import simulate_split  # noqa: E402

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


AFTER_WARMUP = 24 * 21  # first admissible hour: TRAIN_WEEKS of warm-up


# ── the symmetric scheme is a disguised single bucket ────────────────────────


def test_equal_skim_and_refill_keep_the_buckets_identical():
    """+0.5*pnl into each bucket per close: available == reserve at all times."""
    evals = [rec(open_h=AFTER_WARMUP + i * 2, hold_h=1, pnl=(2.0 if i % 2 else -1.0)) for i in range(10)]
    out = simulate_split(train(n=30) + evals, 1000.0, leverage=5.0)
    assert out["refill_capped_events"] == 0
    assert out["final_available"] == pytest.approx(out["final_reserve"], abs=0.01)
    cum = out["final_total"] - 1000.0
    assert out["final_available"] == pytest.approx(500.0 + 0.5 * cum, abs=0.01)


def test_split_equals_single_bucket_at_half_size_fraction():
    """The no-op equivalence: same trades, same total, while margin never binds."""
    evals = [rec(open_h=AFTER_WARMUP + i * 2, hold_h=1, pnl=(1.5 if i % 3 else -2.0)) for i in range(12)]
    recs = train(n=30) + evals
    split = simulate_split(recs, 1000.0, leverage=5.0)
    single = simulate_split(recs, 1000.0, leverage=5.0, split_frac=1.0, size_frac=0.005, skim_frac=0.0, refill_frac=0.0)
    assert split["rejected"]["margin"] == single["rejected"]["margin"] == 0
    assert split["trades_taken"] == single["trades_taken"]
    assert split["final_total"] == pytest.approx(single["final_total"], abs=0.01)
    assert split["size_mean"] == pytest.approx(single["size_mean"], abs=1e-6)


def test_reserve_is_dead_margin_and_halves_the_admission_pool():
    """Where the equivalence DOES break: concurrent positions can only draw on
    the available half, so the split rejects on margin where the single bucket
    still admits."""
    evals = [rec(open_h=AFTER_WARMUP, hold_h=48) for _ in range(30)]
    recs = train(n=30) + evals
    split = simulate_split(recs, 1000.0, leverage=1.0, fixed_size=50.0)
    single = simulate_split(recs, 1000.0, leverage=1.0, split_frac=1.0, fixed_size=50.0, skim_frac=0.0, refill_frac=0.0)
    assert split["peak_occupancy"] == 10  # 500 available / 50
    assert single["peak_occupancy"] == 20  # 1000 available / 50
    assert split["rejected"]["margin"] > single["rejected"]["margin"]


# ── the two one-sided behaviours ─────────────────────────────────────────────


def test_refill_stops_at_an_empty_reserve_and_never_goes_negative():
    """A 100-EUR reserve owes 75 EUR per loss: the second loss can only be
    refilled with what is left, and the ledger must floor at zero."""
    evals = [rec(open_h=AFTER_WARMUP + i * 2, hold_h=1, pnl=-30.0) for i in range(5)]
    out = simulate_split(train(n=30) + evals, 1000.0, leverage=5.0, split_frac=0.9, fixed_size=100.0)
    assert out["refill_capped_events"] >= 1
    assert out["final_reserve"] == pytest.approx(0.0, abs=0.01)
    assert out["final_available"] >= 0.0


def test_ratchet_reserve_only_grows():
    """refill_frac=0: wins feed the reserve, losses never drain it."""
    evals = [rec(open_h=AFTER_WARMUP + i * 2, hold_h=1, pnl=(3.0 if i % 2 else -3.0)) for i in range(10)]
    out = simulate_split(train(n=30) + evals, 1000.0, leverage=5.0, refill_frac=0.0)
    assert out["final_reserve"] >= 500.0
    # every skim that entered the reserve came out of available's compounding base
    assert out["final_available"] <= 500.0 + (out["final_total"] - 1000.0)

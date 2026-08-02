# backtest/test_mps1_event_study.py
"""Unit tests for the PURE decision logic of tools/mps1_event_study.py — the
edge gate whose EDGE/NO-EDGE verdict decides whether MPS1 gets a follow-up
backtest (T-2026-KYT-9050-073, z-code-reviewer MEDIUM finding).

DB-free: only the accumulator/verdict/spread helpers are exercised, never the
DB-reading replay. Pinned contracts:

  * _stat_row — n=0 (None mean), n=1 (no t-stat), zero-variance (no t-stat);
  * _density_bucket / _oi_tier — boundary assignment;
  * derive_verdict — the pre-registered gate: net mean > 0 AND event gross >
    control gross on BOTH halves at n >= MIN_CELL_N; each failure mode flips
    the verdict to NO-EDGE;
  * _fold_spread — first-touch win/loss/timeout and the conservative SL-first
    tie-break when TP and SL fall in the same bar.

Run with: pytest backtest/test_mps1_event_study.py -v
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mps1_event_study import (  # noqa: E402
    MIN_CELL_N,
    ROUND_TRIP_FEE,
    SL_TOLERANCES,
    VERDICT_HORIZON,
    _density_bucket,
    _fold_spread,
    _fold_stat,
    _new_stat,
    _oi_tier,
    _stat_row,
    derive_verdict,
    new_acc,
)

# ── _stat_row ────────────────────────────────────────────────────────────────


def test_stat_row_empty():
    row = _stat_row(_new_stat())
    assert row == {"n": 0, "mean": None, "tstat": None}


def test_stat_row_single_observation_has_no_tstat():
    st = _new_stat()
    _fold_stat(st, 0.01)
    row = _stat_row(st)
    assert row["n"] == 1
    assert row["mean"] == pytest.approx(0.01)
    assert row["tstat"] is None


def test_stat_row_zero_variance_has_no_tstat():
    st = _new_stat()
    # 0.5 is exactly representable — sum2/n − mean² is EXACTLY 0 (no fp noise).
    for _ in range(10):
        _fold_stat(st, 0.5)
    row = _stat_row(st)
    assert row["mean"] == pytest.approx(0.5)
    assert row["tstat"] is None  # var == 0 → no t-stat, not a division crash


def test_stat_row_mean_and_tstat():
    st = _new_stat()
    for x in (0.01, 0.03):
        _fold_stat(st, x)
    row = _stat_row(st)
    assert row["mean"] == pytest.approx(0.02)
    assert row["tstat"] == pytest.approx(0.02 / np.sqrt(0.0001 / 2))


# ── bucket helpers ───────────────────────────────────────────────────────────


def test_density_bucket_boundaries():
    assert _density_bucket(0.0) == "d<10%"
    assert _density_bucket(0.10) == "d10-25%"
    assert _density_bucket(0.25) == "d>=25%"
    assert _density_bucket(1.0) == "d>=25%"


def test_oi_tier_boundaries():
    assert _oi_tier(2e9) == "mega>=1B"
    assert _oi_tier(1e8) == "major>=100M"
    assert _oi_tier(5e7) == "mid>=10M"
    assert _oi_tier(1e6) == "tail"


# ── derive_verdict (the pre-registered gate) ─────────────────────────────────


def _fill_cell(acc, pop: str, side: str, half: str, value: float, n: int) -> None:
    """Fold `n` identical 4h returns into a cell (gross; events also net)."""
    cell = acc[pop][side][half]
    for _ in range(n):
        _fold_stat(cell["gross"][VERDICT_HORIZON], value)
        if pop == "event":
            _fold_stat(cell["net"][VERDICT_HORIZON], value - ROUND_TRIP_FEE)


def _passing_acc(side: str = "up"):
    """Events clearly beat fees and controls on both halves."""
    acc = new_acc()
    for half in ("val", "test"):
        _fill_cell(acc, "event", side, half, 0.01, MIN_CELL_N)
        _fill_cell(acc, "control", side, half, 0.002, MIN_CELL_N)
    return acc


def test_verdict_edge_when_both_halves_pass():
    v = derive_verdict(_passing_acc("up"))
    assert v["verdict"] == "EDGE"
    assert v["passing_sides"] == ["up"]
    assert v["sides"]["up"]["checks"]["val"]["passed"] is True
    assert v["sides"]["up"]["checks"]["test"]["passed"] is True
    assert v["sides"]["down"]["passed"] is False  # untouched side stays failed


def test_verdict_no_edge_when_one_half_fails_sign():
    acc = _passing_acc("up")
    # Overwrite test half with net-negative events (value below the fee).
    acc["event"]["up"]["test"] = new_acc()["event"]["up"]["test"]
    _fill_cell(acc, "event", "up", "test", 0.0005, MIN_CELL_N)  # < 0.10% fee
    v = derive_verdict(acc)
    assert v["verdict"] == "NO-EDGE"
    assert v["sides"]["up"]["checks"]["val"]["passed"] is True
    assert v["sides"]["up"]["checks"]["test"]["passed"] is False


def test_verdict_no_edge_below_n_floor():
    acc = new_acc()
    for half in ("val", "test"):
        _fill_cell(acc, "event", "up", half, 0.01, MIN_CELL_N - 1)
        _fill_cell(acc, "control", "up", half, 0.002, MIN_CELL_N)
    assert derive_verdict(acc)["verdict"] == "NO-EDGE"


def test_verdict_no_edge_when_control_reverts_more():
    acc = new_acc()
    for half in ("val", "test"):
        _fill_cell(acc, "event", "up", half, 0.01, MIN_CELL_N)
        _fill_cell(acc, "control", "up", half, 0.02, MIN_CELL_N)  # control beats events
    assert derive_verdict(acc)["verdict"] == "NO-EDGE"


def test_verdict_empty_control_is_no_edge_not_crash():
    acc = new_acc()
    for half in ("val", "test"):
        _fill_cell(acc, "event", "up", half, 0.01, MIN_CELL_N)
    assert derive_verdict(acc)["verdict"] == "NO-EDGE"  # ct mean None → guarded


# ── _fold_spread (first-touch scan) ──────────────────────────────────────────


def _spread_cell():
    return {f"{tol:.3f}": {"win": 0, "loss": 0, "timeout": 0, "sum_pnl": 0.0} for tol in SL_TOLERANCES}


def _flat(n: int, price: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    p = np.full(n, price)
    return p.copy(), p.copy(), p.copy()  # close, high, low


def test_fold_spread_win_tp_first():
    close, high, low = _flat(10, 100.0)
    low[3] = 89.0  # reaches the opposite band at 90 (side=up → TP below)
    cell = _spread_cell()
    _fold_spread(cell, "up", 0, close, high, low, band=110.0, opp=90.0, n=10)
    for tol in SL_TOLERANCES:
        st = cell[f"{tol:.3f}"]
        assert (st["win"], st["loss"], st["timeout"]) == (1, 0, 0)
        assert st["sum_pnl"] == pytest.approx(abs(90.0 / 100.0 - 1.0) - ROUND_TRIP_FEE)


def test_fold_spread_loss_sl_first():
    close, high, low = _flat(10, 100.0)
    high[2] = 113.0  # breaches 110·(1+tol) for every tolerance
    low[5] = 89.0  # TP later — must not count, SL already hit
    cell = _spread_cell()
    _fold_spread(cell, "up", 0, close, high, low, band=110.0, opp=90.0, n=10)
    for tol in SL_TOLERANCES:
        st = cell[f"{tol:.3f}"]
        assert (st["win"], st["loss"], st["timeout"]) == (0, 1, 0)
        sl = 110.0 * (1.0 + tol)
        assert st["sum_pnl"] == pytest.approx(-abs(sl / 100.0 - 1.0) - ROUND_TRIP_FEE)


def test_fold_spread_same_bar_ambiguity_is_loss():
    close, high, low = _flat(10, 100.0)
    high[4] = 113.0
    low[4] = 89.0  # TP and SL in the SAME bar → conservative loss
    cell = _spread_cell()
    _fold_spread(cell, "up", 0, close, high, low, band=110.0, opp=90.0, n=10)
    for tol in SL_TOLERANCES:
        assert cell[f"{tol:.3f}"]["loss"] == 1


def test_fold_spread_timeout_marks_to_market():
    close, high, low = _flat(10, 100.0)
    close[-1] = 98.0  # neither band reached; short from 100 ends at 98
    cell = _spread_cell()
    _fold_spread(cell, "up", 0, close, high, low, band=110.0, opp=90.0, n=10)
    for tol in SL_TOLERANCES:
        st = cell[f"{tol:.3f}"]
        assert (st["win"], st["loss"], st["timeout"]) == (0, 0, 1)
        assert st["sum_pnl"] == pytest.approx(-(98.0 / 100.0 - 1.0) - ROUND_TRIP_FEE)


def test_fold_spread_down_side_mirror():
    close, high, low = _flat(10, 100.0)
    high[3] = 111.0  # opposite band above (long TP at 110)
    cell = _spread_cell()
    _fold_spread(cell, "down", 0, close, high, low, band=90.0, opp=110.0, n=10)
    st = cell[f"{SL_TOLERANCES[0]:.3f}"]
    assert (st["win"], st["loss"], st["timeout"]) == (1, 0, 0)
    assert st["sum_pnl"] == pytest.approx(abs(110.0 / 100.0 - 1.0) - ROUND_TRIP_FEE)

# backtest/test_mps2_short_backtest.py
"""Unit tests for the pure decision logic of tools/mps2_short_backtest.py
(T-2026-KYT-9050-078). DB-free: event detection runs on synthetic heatmap
frames, fold/verdict on synthetic simulate_exit results.

Pinned contracts:
  * detect_short_events — gate-study semantics on the SHORT side: prior-bar
    band (as-of), min band distance, touch by high, warm-up excluded, last
    bar excluded (exit scan needs a next bar);
  * fold_trade / cell_stats — outcome bucketing (TP1/SL/open), net folding,
    R aggregation, empty-cell shape;
  * derive_verdict — the pre-registered gate: val AND test avg net > 0 at
    n ≥ MIN_HALF_N; each failure mode flips to NO-DEPLOY.

Run with: pytest backtest/test_mps2_short_backtest.py -v
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.mps1_event_study import MIN_EVENT_BAND_DIST, WARMUP_BARS  # noqa: E402
from tools.mps2_short_backtest import (  # noqa: E402
    MIN_HALF_N,
    cell_stats,
    derive_verdict,
    detect_short_events,
    fold_trade,
    new_acc,
)

# ── detect_short_events ──────────────────────────────────────────────────────


def _mk(n: int, band: float | None, touch_at: list[int], close_val: float = 100.0):
    """Synthetic heatmap frame + high/close arrays. Band constant, touches at
    the given indices (high spikes to the band)."""
    ub = np.full(n, np.nan if band is None else band)
    hm = pd.DataFrame({"upper_band": ub})
    high = np.full(n, close_val)
    close = np.full(n, close_val)
    for i in touch_at:
        high[i] = band if band is not None else close_val
    return hm, high, close


def test_detect_requires_prior_band_touch_and_distance():
    n = WARMUP_BARS + 10
    t = WARMUP_BARS + 5
    hm, high, close = _mk(n, 101.0, [t])  # band 1% above close (> 0.3% min)
    events = detect_short_events(hm, high, close)
    assert t in events
    # No touch → no event.
    hm2, high2, close2 = _mk(n, 101.0, [])
    assert len(detect_short_events(hm2, high2, close2)) == 0
    # Band too close to spot (< MIN_EVENT_BAND_DIST) → filtered.
    tight = 100.0 * (1.0 + MIN_EVENT_BAND_DIST / 2)
    hm3, high3, close3 = _mk(n, tight, [t])
    assert len(detect_short_events(hm3, high3, close3)) == 0
    # NaN band → no event.
    hm4, high4, close4 = _mk(n, None, [])
    high4[t] = 200.0
    assert len(detect_short_events(hm4, high4, close4)) == 0


def test_detect_excludes_warmup_and_last_bar():
    n = WARMUP_BARS + 10
    hm, high, close = _mk(n, 101.0, [WARMUP_BARS - 2, n - 1])
    events = detect_short_events(hm, high, close)
    assert len(events) == 0  # warm-up bar and final bar both excluded


def test_detect_uses_prior_bar_band_not_current():
    # Band exists ONLY on bar t-1; bar t's own band is NaN — touch still valid
    # (as-of semantics), while a band appearing first ON the touch bar is not.
    n = WARMUP_BARS + 10
    t = WARMUP_BARS + 5
    ub = np.full(n, np.nan)
    ub[t - 1] = 101.0
    hm = pd.DataFrame({"upper_band": ub})
    high = np.full(n, 100.0)
    close = np.full(n, 100.0)
    high[t] = 101.5
    assert t in detect_short_events(hm, high, close)
    # Shift the band to bar t itself (NaN at t-1) → not eligible at t.
    ub2 = np.full(n, np.nan)
    ub2[t] = 101.0
    hm2 = pd.DataFrame({"upper_band": ub2})
    assert t not in detect_short_events(hm2, high, close)


# ── fold / stats / verdict ───────────────────────────────────────────────────


def _res(net_pct: float, outcome, r=None) -> dict:
    return {"net_pnl_pct": net_pct, "outcome_tp1": outcome, "r_multiple": r}


def test_fold_and_cell_stats():
    acc = new_acc()
    fold_trade(acc, "val", _res(2.0, 1, r=1.5))
    fold_trade(acc, "val", _res(-1.0, 0, r=-1.0))
    fold_trade(acc, "val", _res(0.5, None))  # open at scan cap
    st = cell_stats(acc["val"])
    assert st["n"] == 3
    assert st["avg_net_pct"] == pytest.approx(0.5)
    assert st["tp1_wr"] == pytest.approx(0.5)  # 1 win / 2 closed
    assert st["open_end"] == 1
    assert st["avg_r"] == pytest.approx(0.25)


def test_cell_stats_empty_shape():
    st = cell_stats(new_acc()["val"])
    assert st == {
        "n": 0,
        "avg_net_pct": None,
        "tstat": None,
        "tp1_wr": None,
        "avg_r": None,
        "open_end": 0,
        "no_targets_skipped": 0,
    }


def _filled_acc(val_net: float, test_net: float, n: int = MIN_HALF_N) -> dict:
    acc = new_acc()
    for _ in range(n):
        fold_trade(acc, "val", _res(val_net, 1))
        fold_trade(acc, "test", _res(test_net, 1))
    return acc


def test_verdict_pass_needs_both_halves_positive():
    assert derive_verdict(_filled_acc(0.3, 0.2))["verdict"] == "PASS"
    assert derive_verdict(_filled_acc(0.3, -0.1))["verdict"] == "NO-DEPLOY"
    assert derive_verdict(_filled_acc(-0.3, 0.2))["verdict"] == "NO-DEPLOY"


def test_verdict_no_deploy_below_n_floor():
    acc = _filled_acc(0.3, 0.2, n=MIN_HALF_N - 1)
    assert derive_verdict(acc)["verdict"] == "NO-DEPLOY"


def test_verdict_empty_is_no_deploy_not_crash():
    assert derive_verdict(new_acc())["verdict"] == "NO-DEPLOY"

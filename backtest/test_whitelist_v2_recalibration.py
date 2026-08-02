# backtest/test_whitelist_v2_recalibration.py
"""
Unit tests for the v2 whitelist parameter sweep (T-2026-KYT-9050-007).

The tool's job is to be HARD to read optimistically. Two things carry that and
are pinned here:

  * `verdict_of` must call a configuration that removes profitable traffic
    "entfernt GEWINNER" even when its retained side looks excellent — that is
    exactly the shape the in-sample run produced and the out-of-sample run
    exposed as a selection artifact.
  * `score_config` must never treat a missing leg as a zero, and must never
    score the suppressed side (which has no ROM1 leg by construction).

Also pinned: the sweep restores the analyzer's module-level parameters, since it
mutates a LIVE module's globals to re-decide cells.

Run with: pytest backtest/test_whitelist_v2_recalibration.py -v
"""

from __future__ import annotations

import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from tools import whitelist_v2_recalibration as recal  # noqa: E402


class _FakeAnalyzer:
    """Stand-in for 27_bot_regime_analyzer with the three tunables."""

    V2_LOWER_BOUND_Z = 1.64
    V2_SHRINKAGE_PSEUDO_COUNT = 25.0
    V2_BREAK_EVEN_PNL_PCT = 0.1
    REFERENCE_WINDOW_DAYS = 30

    def _v2_whitelist_decision(self, cell, parent_regime, parent_overall):
        # Toy gate: pass when the cell's avg beats the break-even, scaled by z.
        avg = (cell or {}).get("avg_pnl") or 0.0
        return (avg - self.V2_LOWER_BOUND_Z * 0.1 > self.V2_BREAK_EVEN_PNL_PCT, "stub")


def _event(bot="ROM1", regime="TREND_UP", alt="ALT_NEUTRAL", direction="LONG", leg=None):
    e = {"bot_name": bot, "regime": regime, "alt_context": alt, "direction": direction, "side": "forwarded"}
    if leg is not None:
        e["rom1"] = leg
    return e


# ── verdict_of: the reading that must not flatter ────────────────────────────


def test_removing_profitable_traffic_is_called_out_even_with_a_great_kept_side():
    row = {
        "n_blocked": 500,
        "kept": {"mean_move_pct": 4.37},  # spectacular
        "blocked": {"mean_move_pct": 0.554},  # but what it dropped was winning
    }
    assert recal.verdict_of(row, {"mean_move_pct": 0.689}) == "entfernt GEWINNER"


def test_removing_losers_requires_the_kept_side_to_actually_improve():
    good = {"n_blocked": 100, "kept": {"mean_move_pct": 0.56}, "blocked": {"mean_move_pct": -0.08}}
    assert recal.verdict_of(good, {"mean_move_pct": 0.033}) == "entfernt Verlierer"
    # Same negative blocked side, but the kept side is WORSE than the baseline:
    # dropping losers while the remainder degrades is not an improvement.
    ambiguous = {"n_blocked": 100, "kept": {"mean_move_pct": 0.01}, "blocked": {"mean_move_pct": -0.08}}
    assert recal.verdict_of(ambiguous, {"mean_move_pct": 0.033}) != "entfernt Verlierer"


def test_a_gate_that_blocks_nothing_is_a_no_op_not_a_success():
    row = {"n_blocked": 0, "kept": {"mean_move_pct": 0.7}, "blocked": {"mean_move_pct": None}}
    assert recal.verdict_of(row, {"mean_move_pct": 0.689}) == "no-op (blockt nichts)"


def test_missing_leg_stats_are_not_bewertbar():
    row = {"n_blocked": 10, "kept": {"mean_move_pct": None}, "blocked": {"mean_move_pct": None}}
    assert "nicht bewertbar" in recal.verdict_of(row, {"mean_move_pct": 0.1})


# ── score_config ─────────────────────────────────────────────────────────────


def test_config_splits_traffic_and_counts_undecidable_separately():
    analyzer = _FakeAnalyzer()
    perf = {
        ("ROM1", "TREND_UP", "ALT_NEUTRAL", "LONG"): {"avg_pnl": 5.0},
        ("ROM1", "CHOP", "ALT_NEUTRAL", "LONG"): {"avg_pnl": -5.0},
    }
    events = [
        _event(regime="TREND_UP", leg={"move_pct": 1.0}),
        _event(regime="CHOP", leg={"move_pct": -1.0}),
        _event(regime=None, leg={"move_pct": 9.0}),  # unattributable cell
    ]
    with mock.patch.object(recal, "summarize_legs", side_effect=lambda evs, key: {"n": len(evs)}):
        row = recal.score_config(analyzer, perf, events, 1.64, 25.0, 0.1)
    assert row["n_kept"] == 1
    assert row["n_blocked"] == 1
    assert row["n_undecidable"] == 1, "an unattributable cell must not be silently counted as blocked"
    assert row["n_forwarded"] == 3


def test_keep_rate_is_none_on_an_empty_population_not_zero():
    analyzer = _FakeAnalyzer()
    with mock.patch.object(recal, "summarize_legs", side_effect=lambda evs, key: {"n": 0}):
        row = recal.score_config(analyzer, {}, [], 1.64, 25.0, 0.1)
    assert row["keep_rate_pct"] is None, "0 % would claim a measurement that was never made"


def test_config_is_actually_applied_to_the_analyzer():
    analyzer = _FakeAnalyzer()
    with mock.patch.object(recal, "summarize_legs", side_effect=lambda evs, key: {}):
        recal.score_config(analyzer, {}, [], 0.67, 5.0, -0.1)
    assert analyzer.V2_LOWER_BOUND_Z == 0.67
    assert analyzer.V2_SHRINKAGE_PSEUDO_COUNT == 5.0
    assert analyzer.V2_BREAK_EVEN_PNL_PCT == -0.1


def test_lowering_z_opens_the_gate():
    """The finding the whole report rests on: z is the lever, not break-even."""
    analyzer = _FakeAnalyzer()
    perf = {("ROM1", "TREND_UP", "ALT_NEUTRAL", "LONG"): {"avg_pnl": 0.25}}
    events = [_event(leg={"move_pct": 1.0})]
    with mock.patch.object(recal, "summarize_legs", side_effect=lambda evs, key: {"n": len(evs)}):
        strict = recal.score_config(analyzer, perf, events, 1.64, 25.0, 0.1)
        loose = recal.score_config(analyzer, perf, events, 0.0, 25.0, 0.1)
    assert strict["n_kept"] == 0
    assert loose["n_kept"] == 1


# ── the snapshot claim the caveat rests on ───────────────────────────────────


class _Cur:
    def __init__(self, results):
        self._results = list(results)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *a, **kw):
        pass

    def fetchone(self):
        return self._results.pop(0)


class _Conn:
    def __init__(self, results):
        self._cur = _Cur(results)

    def cursor(self):
        return self._cur


def test_snapshot_check_reports_history_when_duplicates_exist():
    """The 'not a backtest' caveat is measured per run — if the table ever gains
    history the report must say so instead of repeating a stale disclaimer."""
    conn = _Conn([(0,), ("2026-04-18", "2026-08-02")])
    assert recal.snapshot_is_history(conn)["is_snapshot"] is True
    conn = _Conn([(17,), ("2026-04-18", "2026-08-02")])
    out = recal.snapshot_is_history(conn)
    assert out["is_snapshot"] is False
    assert out["duplicate_cells"] == 17

# backtest/test_ods1_entry.py — ODS1's entry rule and its OI staleness contract.
"""Standalone, DB-free tests for 42_ai_ods1_bot.py (T-2026-KYT-9050-106).

Three things decide whether this bot makes or loses money, and none of them are
visible from a green import: the entry rule must match the T-096 operating point
it claims to implement, a stale OI point must be voided rather than filled, and
the roster seat must not break the register's eviction order.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load_bot():
    """Digit-prefixed filename — not importable by name."""
    spec = importlib.util.spec_from_file_location("ods1", os.path.join(REPO, "42_ai_ods1_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ods1"] = mod
    spec.loader.exec_module(mod)
    return mod


ods1 = _load_bot()
NOW = 1_760_000_000


def series(points):
    """[(seconds_before_now, open_interest, price)] -> the bot's row format."""
    return [(NOW - dt, oi, px) for dt, oi, px in sorted(points, key=lambda p: -p[0])]


# ── the T-096 operating point ────────────────────────────────────────────────


def test_thresholds_match_the_study_they_cite():
    """px >= +3 %, 4h OI <= -2 %. Drifting from these silently turns the bot into
    something the study never measured."""
    assert ods1.PX_RALLY_PCT == pytest.approx(3.0)
    assert ods1.OI_DROP_PCT == pytest.approx(-2.0)
    assert ods1.LOOKBACK_S == 4 * 3600
    assert ods1.COOLDOWN_H == 24


def test_rally_on_draining_oi_fires():
    rows = {"XUSDT": series([(4 * 3600, 1_000_000.0, 100.0), (0, 950_000.0, 104.0)])}
    out = ods1.find_candidates(rows, NOW)
    assert [c["symbol"] for c in out] == ["XUSDT"]
    assert out[0]["px_chg"] == pytest.approx(4.0)
    assert out[0]["oi_chg"] == pytest.approx(-5.0)


def test_rally_on_rising_oi_does_not_fire():
    """New money behind the move is a trend, not a squeeze — this is the case the
    study refuted (spike-fade, -2.56 @24h)."""
    rows = {"XUSDT": series([(4 * 3600, 1_000_000.0, 100.0), (0, 1_100_000.0, 104.0)])}
    assert ods1.find_candidates(rows, NOW) == []


def test_oi_drain_without_a_rally_does_not_fire():
    rows = {"XUSDT": series([(4 * 3600, 1_000_000.0, 100.0), (0, 900_000.0, 100.5)])}
    assert ods1.find_candidates(rows, NOW) == []


def test_thin_books_are_skipped():
    """Study universe was median OI >= $3M; a 40k book is not the same asset."""
    rows = {"XUSDT": series([(4 * 3600, 400.0, 100.0), (0, 380.0, 104.0)])}
    assert ods1.find_candidates(rows, NOW) == []


def test_strictest_divergence_ranks_first():
    """The threshold matrix was monotone in strictness, so under a slot cap the
    most extreme event is the one worth the seat."""
    rows = {
        "MILD": series([(4 * 3600, 1_000_000.0, 100.0), (0, 970_000.0, 103.5)]),
        "HARSH": series([(4 * 3600, 1_000_000.0, 100.0), (0, 880_000.0, 108.0)]),
    }
    assert [c["symbol"] for c in ods1.find_candidates(rows, NOW)] == ["HARSH", "MILD"]


# ── the staleness contract ───────────────────────────────────────────────────


def test_stale_now_point_voids_the_symbol():
    """The collector degraded to a 10-min median cadence (T-097). A point older
    than the cap is voided, never carried forward."""
    stale = ods1.STALENESS_CAP_S + 600
    rows = {"XUSDT": series([(4 * 3600 + stale, 1_000_000.0, 100.0), (stale, 900_000.0, 108.0)])}
    assert ods1.find_candidates(rows, NOW) == []


def test_as_of_never_looks_forward():
    rows = series([(0, 900_000.0, 108.0)])
    assert ods1._as_of(rows, NOW - 4 * 3600) is None


def test_as_of_takes_the_last_point_at_or_before_t():
    rows = series([(3600, 1_000_000.0, 100.0), (600, 950_000.0, 104.0)])
    oi, px = ods1._as_of(rows, NOW)
    assert (oi, px) == (950_000.0, 104.0)


def test_a_gap_inside_the_cap_is_still_usable():
    """Voiding is for staleness, not for every irregular cadence — the table is
    effectively 10-minutely and would otherwise never produce a signal."""
    rows = {"XUSDT": series([(4 * 3600 + 1200, 1_000_000.0, 100.0), (1200, 900_000.0, 108.0)])}
    assert len(ods1.find_candidates(rows, NOW)) == 1


# ── geometry and the roster seat ─────────────────────────────────────────────


def test_bracket_is_sized_to_the_measured_drift_not_the_fleet_default():
    """T-096 measured +0.41 % @1h and +0.73 % @4h. A fleet-default bracket (TP1
    ~4-5 %) sits far outside that and the edge leaks away before TP1."""
    assert max(ods1.TP_PCTS) <= 2.0
    assert ods1.SL_PCT <= 2.5
    assert ods1.TP_PCTS == tuple(sorted(ods1.TP_PCTS))


def test_short_targets_are_below_entry_and_stop_is_above():
    """P0.7's failure class: a ladder built on the wrong side of the entry scores
    losses as TP hits."""
    price = 100.0
    targets = [price * (1 - p / 100.0) for p in ods1.TP_PCTS]
    sl = price * (1 + ods1.SL_PCT / 100.0)
    assert all(t < price for t in targets)
    assert sl > price
    assert targets == sorted(targets, reverse=True)


def test_roster_seat_exists_and_does_not_break_the_eviction_order():
    from core.trailing_roster import ROSTER, density, is_rostered

    assert is_rostered("ODS1", "SHORT")
    values = list(ROSTER.values())
    pairs = zip(values, values[1:], strict=False)  # values[1:] is one shorter by construction
    assert all(a > b for a, b in pairs), "density must stay strictly descending"
    assert density("ODS1", "SHORT") == min(values), "an unmeasured leg yields its seat first, not last"

# backtest/test_trailing_live_vs_study.py — T-2026-KYT-9050-047 pins.
#
# The tool answers one operator question — does Bot 40 turn its book over faster than
# the study that picked act = 2 % — and the first version of the analysis got the
# answer BACKWARDS twice, in two different ways. Both mistakes are pinned here:
#
#   1. Measuring the live median over CLOSED rows only. Six live days against a
#      147-day simulation means the long holders are still open; dropping them reports
#      the arm as faster than it is, which is exactly the hypothesis under test.
#      `censored_median_bounds` turns that into an interval instead of a point.
#   2. Replaying the study's rule only up to the live exit. The 15m trigger almost
#      always sits in the very bar the live exit falls into, and a flush window
#      excludes exactly that bar — which produced "88 % of the arm's exits would not
#      have been trailed by the study" out of what is, measured properly, a median
#      difference of one bar. `resolution_bucket` keeps `same-bar` a named outcome.
#
# Plus the two rule-level invariants the whole comparison rests on: a 15m trail may
# not fire on a peak established in its OWN bar (the study's `prior_peak`), and its
# exit instant is the bar CLOSE, because that is the first moment the trigger is
# knowable (T-052: a deadline read off future tape turned 59k into 7k).
#
# Runs without a DB:  python backtest/test_trailing_live_vs_study.py

import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.trailing_live_vs_study import (  # noqa: E402
    BAR,
    GRID_H,
    bars_in_window,
    censored_median_bounds,
    fee_share_of_gross,
    hold_hours,
    hourly_occupancy,
    replay_study_rule,
    resolution_bucket,
    slot_interval,
    turnover_per_slot_day,
    weighted_median,
)

T0 = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=100)


def _row(**kw) -> dict:
    base = {
        "posted": True,
        "close_reason": "TRAIL",
        "opened_at": T0,
        "filled_at": T0 + timedelta(seconds=30),
        "closed_at": T0 + timedelta(hours=4),
        "entry": 100.0,
        "direction": "LONG",
    }
    base.update(kw)
    return base


# ------------------------------------------------------------------ slots --- #
def test_a_slot_starts_at_the_fill_not_at_the_mirror_row():
    """Between insert and fill sits an unfilled limit order, which holds no seat."""
    start, end = slot_interval(_row(), NOW)
    assert start == T0 + timedelta(seconds=30)
    assert end == T0 + timedelta(hours=4)


def test_a_row_without_filled_at_falls_back_to_opened_at():
    """The first 124 posted rows predate T-050 and carry NULL — seconds apart, and the
    substitution errs toward MORE occupancy, never less."""
    assert slot_interval(_row(filled_at=None), NOW)[0] == T0


def test_never_filled_and_never_posted_rows_occupy_nothing():
    """ENTRY_NOT_FILLED is a posted row with no position behind it: no slot, no fee.
    Counting it inflates the slot draw and the fee load at once."""
    assert slot_interval(_row(close_reason="ENTRY_NOT_FILLED"), NOW) is None
    assert slot_interval(_row(close_reason="PREEXISTING"), NOW) is None
    assert slot_interval(_row(close_reason="SHADOW_CARRYOVER"), NOW) is None
    # The shadow book never reached Cornix at all.
    assert slot_interval(_row(posted=False), NOW) is None


def test_an_open_position_is_occupied_up_to_now():
    assert hold_hours(_row(closed_at=None), NOW) == (NOW - (T0 + timedelta(seconds=30))).total_seconds() / 3600


def test_a_sub_hour_position_still_draws_its_hour():
    """A book of ten-minute trades holds real Cornix seats; reporting zero draw for it
    would make the slot cap look free exactly where turnover is highest."""
    occ = hourly_occupancy([(T0, T0 + timedelta(minutes=10))])
    assert list(occ) == [1]


def test_occupancy_counts_simultaneity_not_trades():
    a = (T0, T0 + timedelta(hours=3))
    b = (T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    assert list(hourly_occupancy([a, b])) == [1, 2, 1]


# ---------------------------------------------------------- censoring (1) --- #
def test_the_still_open_positions_widen_the_median_into_an_interval():
    """Lower bound = every open row closes now; upper = every open row outlives the rest."""
    lo, hi = censored_median_bounds([1.0, 2.0, 3.0], [4.0])
    assert (lo, hi) == (2.5, 2.5)  # the censored row sits above the middle either way
    lo, hi = censored_median_bounds([1.0, 2.0, 9.0], [3.0])
    assert lo == 2.5 and hi == 5.5  # here it does not, and the bounds separate


def test_the_median_is_refused_when_most_of_the_book_is_still_open():
    """Better no number than a number built on the half that happened to close first."""
    assert censored_median_bounds([1.0], [2.0, 3.0]) is None
    assert censored_median_bounds([], []) is None


def test_weighted_median_lets_the_live_mix_decide():
    """MIS2-168h SHORT (3 live mirrors) must not count as much as MIS1-72h LONG (403)."""
    assert weighted_median([(0.4, 3.0), (6.6, 403.0)]) == 6.6
    assert weighted_median([(0.4, 0.0), (6.6, 0.0)]) is None


# ------------------------------------------------------------- fee / rate --- #
def test_a_fee_share_of_a_losing_book_is_refused_not_rounded_to_something_small():
    """"Fees ate 12 % of a negative gross" has no meaning, and printing a small positive
    number for it makes a loss-making book read as cheap."""
    assert fee_share_of_gross(-806.2, 999) is None
    assert fee_share_of_gross(0.0, 10) is None
    assert abs(fee_share_of_gross(100.0, 100) - 0.10) < 1e-9


def test_turnover_is_per_occupied_slot_day():
    """Neither trade count nor holding time alone survives a mix change; this does."""
    assert abs(turnover_per_slot_day(999, 711.0) - 1.4051) < 1e-3
    assert turnover_per_slot_day(10, 0.0) is None


# ------------------------------------------------------- the study's rule --- #
def _bars(*hl):
    return [(T0 + i * BAR, h, low) for i, (h, low) in enumerate(hl)]


def test_a_peak_and_its_giveback_inside_one_bar_do_not_trigger():
    """The study's core restriction: within one candle the order of high and low is
    unknowable, so the peak that arms a trail must be on an EARLIER bar. This is the
    whole reason the 15m rule can fire later than a 10 s poll — remove it and the
    resolution comparison measures nothing."""
    # bar 1 alone would arm (+3 %) and breach (−3 %); with a strictly prior peak of
    # +0.5 % it cannot.
    assert replay_study_rule(_bars((100.5, 99.5), (103.0, 97.0)), 100.0, True) is None


def test_the_trail_fires_on_a_later_bar_and_times_out_at_that_bar_s_close():
    """Timestamping the trigger at the bar's OPEN would credit the rule with an exit up
    to 15 min before the information existed."""
    bars = _bars((100.5, 99.5), (103.0, 97.0), (103.5, 102.0))
    exit_at, mark = replay_study_rule(bars, 100.0, True)
    assert exit_at == T0 + 2 * BAR + BAR
    assert abs(mark - 2.7) < 1e-9  # the +3 % peak, given back by 10 %


def test_the_activation_floor_still_binds_in_the_replay():
    """Without it a scale-free trail is a micro-scalper — the finding that put act on
    the map in the first place."""
    bars = _bars((100.5, 99.5), (103.0, 97.0), (103.5, 102.0))
    assert replay_study_rule(bars, 100.0, True, activation=5.0) is None


def test_short_side_reads_the_low_as_favourable():
    """A SHORT profits when the tape falls; reading `high` as its peak inverts the sign
    and would make every SHORT look as if it never armed."""
    bars = _bars((100.5, 99.5), (103.0, 97.0), (98.0, 97.5))
    exit_at, mark = replay_study_rule(bars, 100.0, False)
    assert exit_at == T0 + 3 * BAR
    assert abs(mark - 2.7) < 1e-9


def test_the_window_is_flush_on_both_sides():
    """The study's own mask selects on open_time alone, so its last bar can reach past
    the close and arm a trail on tape the trade never saw (its documented limit). Flush
    withholds trigger opportunities from the study side — the conservative direction."""
    bars = _bars((1, 1), (2, 2), (3, 3), (4, 4))
    got = bars_in_window(bars, T0, T0 + 3 * BAR)
    assert [b[1] for b in got] == [1, 2, 3]  # the 4th bar would close past the window


# --------------------------------------------------------- buckets (2) ----- #
def test_one_bar_of_difference_is_the_grid_not_a_different_operating_point():
    """The mistake this pin exists for: with `same-bar` folded into "the study would
    have held", 67 % of the arm's exits read as a rule difference when the median gap
    is +0.33 h — one bar."""
    assert resolution_bucket(0.20) == "same-bar"
    assert resolution_bucket(GRID_H) == "same-bar"
    assert resolution_bucket(GRID_H + 1e-6) == "study-later"
    assert resolution_bucket(0.0) == "study-earlier"
    assert resolution_bucket(-2.35) == "study-earlier"
    assert resolution_bucket(None) == "study-never-fires"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("  ok   %s" % name)
            except AssertionError as exc:
                fails += 1
                print("  FAIL %s: %s" % (name, exc))
    print("\n%s" % ("all pins green" if not fails else "%d FAILED" % fails))
    sys.exit(1 if fails else 0)

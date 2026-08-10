# backtest/test_trailing_roster_rerank.py — T-2026-KYT-9050-134 pins.
#
# The re-rank tool exists to correct replay numbers against the live book, so the ways
# it can be silently wrong are all in the join and the units:
#
#   1. Activation mismatch. A slot-budget report scores its `legs` block at whatever
#      `chosen_act` its own selector picked. The live-window re-run picked 0.0 where the
#      original picked 2.0, and at 0.0 the trail is the micro-scalper pinned in
#      test_trailing_slot_budget (median hold 0.42h vs 5.58h). Calibrating across that
#      compares two different strategies and reads as a clean measurement.
#   2. Fee side. The replay reports NET per trade; the live book stores a GROSS unlevered
#      mark. Comparing them directly flatters the replay by exactly one round-trip.
#   3. Key case. Replay keys spell the tag as the trainer does (`MIS1-72h`), the live book
#      as the bot wrote it (`MIS1-72H`). An unnormalised join drops legs silently, which
#      shrinks the calibration set without any error.
#
# Runs without a DB:  python backtest/test_trailing_roster_rerank.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.trailing_roster_rerank import (  # noqa: E402
    ActivationMismatch,
    calibrate,
    check_activation,
    normalise_leg,
    score_legs,
)

FEE = 0.10


def test_activation_mismatch_is_refused():
    """A report scored at 0.0 must not be silently compared against a 2.0 arm."""
    try:
        check_activation({"chosen_act": 0.0}, 2.0, "live-window run")
    except ActivationMismatch as exc:
        assert "--activations 2.0" in str(exc), f"error must name the fix, got: {exc}"
    else:
        raise AssertionError("activation mismatch was not refused")


def test_activation_match_passes():
    check_activation({"chosen_act": 2.0}, 2.0, "ok")


def test_missing_activation_is_refused():
    try:
        check_activation({}, 2.0, "no field")
    except ActivationMismatch:
        return
    raise AssertionError("a report without chosen_act must not pass")


def test_normalise_leg_matches_trainer_and_bot_spelling():
    assert normalise_leg("MIS1-72h LONG") == normalise_leg("MIS1-72H LONG")
    assert normalise_leg("  aim2 short ") == "AIM2 SHORT"


def test_calibration_recovers_a_known_line():
    """live = -0.5 + 0.5*replay, planted exactly, must come back out."""
    replay = {f"L{i} LONG": {"per_trade_trail_net": float(i)} for i in range(1, 6)}
    # live stores GROSS, so add the fee back on top of the intended net value
    live = {f"L{i} LONG": (100, (-0.5 + 0.5 * i) + FEE) for i in range(1, 6)}
    cal = calibrate(replay, live, FEE)
    assert cal is not None
    assert abs(cal.a - (-0.5)) < 1e-9, f"intercept {cal.a}"
    assert abs(cal.b - 0.5) < 1e-9, f"slope {cal.b}"
    assert cal.r2 > 0.999
    assert abs(cal.predict(10.0) - 4.5) < 1e-9


def test_fee_is_subtracted_from_the_live_side():
    """A leg whose gross mark equals the replay's net must show a -FEE error, not zero."""
    replay = {f"L{i} LONG": {"per_trade_trail_net": float(i)} for i in range(1, 5)}
    live = {f"L{i} LONG": (100, float(i)) for i in range(1, 5)}
    cal = calibrate(replay, live, FEE)
    assert cal is not None
    assert abs(cal.median_error - (-FEE)) < 1e-9, f"expected -{FEE} median error, got {cal.median_error}"
    assert cal.overstated == 4


def test_thin_live_coverage_is_excluded_from_the_fit():
    """Legs under the live-n floor must not steer the correction."""
    replay = {f"L{i} LONG": {"per_trade_trail_net": float(i)} for i in range(1, 5)}
    live = {f"L{i} LONG": (100, float(i) * 0.5 + FEE) for i in range(1, 4)}
    live["L4 LONG"] = (2, 99.0)  # tiny sample, wild value
    cal = calibrate(replay, live, FEE, min_live_n=30)
    assert cal.n_legs == 3, f"thin leg leaked into the fit: n_legs={cal.n_legs}"
    assert abs(cal.b - 0.5) < 1e-9, f"slope corrupted by the thin leg: {cal.b}"


def test_calibration_needs_three_points():
    replay = {"A LONG": {"per_trade_trail_net": 1.0}, "B LONG": {"per_trade_trail_net": 2.0}}
    live = {"A LONG": (100, 1.0), "B LONG": (100, 2.0)}
    assert calibrate(replay, live, FEE) is None


def test_scores_rank_by_corrected_total_not_per_trade():
    """Absolute contribution is the primary measure — many small trades can outrank few large."""
    replay = {
        "SMALL LONG": {"per_trade_trail_net": 5.0, "n": 10, "density_trail": 99.0},
        "BIG LONG": {"per_trade_trail_net": 1.0, "n": 1000, "density_trail": 0.5},
    }
    scores = score_legs(replay, None, set(), {}, FEE)
    assert scores[0].key == "BIG LONG", "ranking must follow total contribution"
    assert abs(scores[0].corrected_total - 1000.0) < 1e-9
    assert abs(scores[1].corrected_total - 50.0) < 1e-9


def test_live_evidence_beats_the_fitted_correction():
    """A leg with real live trades must be scored on them, never on the regression line.

    This is the AIM2 SHORT case: 480 live trades at -0.511, but the fit predicts +0.149
    because a regression pulls everything toward the mean. Ranking on the fitted value
    would have recommended a seat for a leg T-129 retired for losing money.
    """
    replay = {"LOSER SHORT": {"per_trade_trail_net": 1.295, "n": 738}}
    live = {"LOSER SHORT": (480, -0.411)}  # gross; net = -0.511 after FEE
    # a fit that would otherwise rehabilitate it
    fitted = {f"C{i} LONG": {"per_trade_trail_net": float(i)} for i in range(1, 6)}
    fitted_live = {f"C{i} LONG": (100, 0.2 * i + FEE) for i in range(1, 6)}
    cal = calibrate(fitted, fitted_live, FEE)
    assert cal is not None and cal.predict(1.295) > 0, "precondition: fit must be optimistic here"

    scores = score_legs(replay, cal, set(), live, FEE)
    s = scores[0]
    assert s.basis == "live", f"expected live basis, got {s.basis}"
    assert abs(s.effective_per_trade - (-0.511)) < 1e-9, s.effective_per_trade
    assert s.corrected_total < 0, "a live-losing leg must not rank positive"


def test_thin_live_coverage_falls_back_to_the_fit():
    """Below the floor the live mean is noise, so the fitted value is the better estimate."""
    replay = {"X LONG": {"per_trade_trail_net": 2.0, "n": 50}}
    live = {"X LONG": (3, 40.0)}  # 3 trades, absurd mean
    fitted = {f"C{i} LONG": {"per_trade_trail_net": float(i)} for i in range(1, 6)}
    fitted_live = {f"C{i} LONG": (100, 0.5 * i + FEE) for i in range(1, 6)}
    cal = calibrate(fitted, fitted_live, FEE)
    scores = score_legs(replay, cal, set(), live, FEE)
    assert scores[0].basis == "fitted", "3 live trades must not outweigh the fit"
    assert abs(scores[0].effective_per_trade - cal.predict(2.0)) < 1e-9


def test_uncalibrated_scores_fall_back_to_raw():
    replay = {"A LONG": {"per_trade_trail_net": 1.25, "n": 10}}
    scores = score_legs(replay, None, set(), {}, FEE)
    assert abs(scores[0].corrected_per_trade - 1.25) < 1e-9


def test_rostered_flag_uses_normalised_keys():
    replay = {"MIS1-72h LONG": {"per_trade_trail_net": 1.0, "n": 5}}
    scores = score_legs(replay, None, {"MIS1-72H LONG"}, {}, FEE)
    assert scores[0].rostered is True, "roster membership lost to tag casing"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

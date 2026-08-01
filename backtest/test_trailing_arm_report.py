# backtest/test_trailing_arm_report.py — T-2026-KYT-9050-054 pins.
#
# The report exists to stop three specific misreadings, each of which had already
# produced a confidently wrong verdict on the live arm. The pins hold exactly those:
#
#   1. SL reconstruction. A NULL close_mark_pct on an SL hit is a REPORTING gap, not
#      a zero and not an unknown — the fill is on the stop level and the level is in
#      the row. But a row without `sl` stays unknown; inventing a level there is the
#      original sin the NULL-booking was avoiding.
#   2. The source-trade match must be verified against the entry price. Without that
#      guard a lateral join happily pairs a mirror entered at 5.8e-05 with a trade
#      entered at 12.987 and reports "hold returned +2 600 837 %".
#   3. Market attribution is direction-signed. A falling tape hurts a LONG and pays
#      a SHORT; getting the sign wrong doubles the error instead of removing it.
#
# Plus the index construction: a median so one fresh listing cannot BE the market,
# and no return booked on a symbol's first observation.
#
# Runs without a DB:  python backtest/test_trailing_arm_report.py

import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.trailing_arm_report import (  # noqa: E402
    ENTRY_TOL,
    FEE_RT,
    build_index,
    classify_arm_exit,
    entry_scale_ok,
    index_move_pct,
    market_implied,
    net_of_fees,
    realized_mark,
    summarize,
)

T0 = datetime(2026, 7, 26, tzinfo=timezone.utc)


def _row(**kw) -> dict:
    base = {
        "close_mark_pct": None,
        "close_reason": "SL_HIT",
        "direction": "LONG",
        "entry": 100.0,
        "sl": 95.0,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------- (1) --
def test_sl_hit_with_null_mark_is_reconstructed_from_the_stop_level():
    """The exact defect: 67 live rows booked NULL although the outcome is known."""
    assert realized_mark(_row()) == -5.0
    # SHORT: the stop sits ABOVE entry and is likewise a 5 % loss, not a gain.
    assert realized_mark(_row(direction="SHORT", entry=100.0, sl=105.0)) == -5.0


def test_reconstruction_never_overrides_a_booked_mark():
    """Once the bot books a mark it is the measured truth; the stop level is not."""
    assert realized_mark(_row(close_mark_pct=-2.5)) == -2.5


def test_sl_row_without_a_stop_level_stays_unknown():
    """Pre-T-049 rows carry no `sl`. Guessing one is worse than reporting unknown."""
    assert realized_mark(_row(sl=None)) is None


def test_non_sl_null_mark_is_never_invented():
    """A missing MARKET price is genuinely unknown — only the SL case is recoverable."""
    assert realized_mark(_row(close_reason="SOURCE_CLOSED", sl=95.0)) is None


def test_zero_or_negative_entry_cannot_produce_a_mark():
    assert realized_mark(_row(entry=0.0)) is None


# --------------------------------------------------------------------- (2) --
def test_entry_guard_rejects_the_cross_trade_match():
    """The live failure: NEIROUSDT mirror at 5.836e-05 paired with a source at 12.987."""
    assert not entry_scale_ok(5.836e-05, 12.987)


def test_entry_guard_admits_ordinary_market_entry_drift():
    """The mirror enters at market up to ~240 s after the signal — small drift is normal."""
    assert entry_scale_ok(100.0, 100.5)
    # Just inside / clearly outside — pinning the exact boundary would only be
    # pinning float representation (100 * 1.03 == 103.00000000000001).
    assert entry_scale_ok(100.0, 100.0 * (1 + ENTRY_TOL * 0.99))
    assert not entry_scale_ok(100.0, 100.0 * (1 + ENTRY_TOL * 2))


def test_entry_guard_is_symmetric_and_rejects_nonsense_inputs():
    assert entry_scale_ok(100.0, 99.5) and entry_scale_ok(99.5, 100.0)
    assert not entry_scale_ok(None, 100.0)
    assert not entry_scale_ok(100.0, 0.0)


# --------------------------------------------------------------------- (3) --
def test_market_attribution_is_direction_signed():
    """A −5 % tape costs a LONG 5 points and hands a SHORT 5."""
    assert market_implied(-5.0, is_long=True) == -5.0
    assert market_implied(-5.0, is_long=False) == 5.0


def test_residual_isolates_the_leg_from_the_tape():
    """A LONG that lost 4 % while the market lost 5 % OUTPERFORMED by a point."""
    realised, tape = -4.0, -5.0
    assert realised - market_implied(tape, is_long=True) == 1.0


# ------------------------------------------------------------------ index --
def _candles(sym: str, start_h: int, closes: list[float]):
    return [(T0 + timedelta(hours=start_h + i), sym, c) for i, c in enumerate(closes)]


def test_index_uses_the_median_so_one_listing_cannot_be_the_market():
    """Three coins flat, one printing +300 %: the mean would say +75 %, the index 0 %."""
    rows = []
    for sym in ("A", "B", "C"):
        rows += _candles(sym, 0, [100.0, 100.0])
    rows += _candles("MOON", 0, [1.0, 4.0])
    idx = build_index(rows)
    assert len(idx) == 1
    assert abs(idx[0][1] - 1.0) < 1e-12, idx


def test_index_books_no_return_on_a_symbols_first_observation():
    """A coin that starts mid-window must not enter as a return from nothing."""
    rows = _candles("A", 0, [100.0, 110.0]) + _candles("LATE", 1, [50.0])
    idx = build_index(rows)
    assert len(idx) == 1
    assert abs(idx[0][1] - 1.10) < 1e-12, idx


def test_index_compounds_across_hours():
    rows = _candles("A", 0, [100.0, 110.0, 121.0]) + _candles("B", 0, [10.0, 11.0, 12.1])
    idx = build_index(rows)
    assert len(idx) == 2
    assert abs(idx[-1][1] - 1.21) < 1e-9, idx


def test_index_move_uses_last_level_at_or_before_each_end():
    rows = _candles("A", 0, [100.0, 110.0, 121.0]) + _candles("B", 0, [10.0, 11.0, 12.1])
    idx = build_index(rows)
    # Between the two index points, sampled 30 min past each — carries the earlier level.
    mv = index_move_pct(idx, T0 + timedelta(hours=1, minutes=30), T0 + timedelta(hours=2, minutes=30))
    assert abs(mv - 10.0) < 1e-9, mv


def test_index_move_is_none_when_the_window_predates_coverage():
    idx = build_index(_candles("A", 5, [100.0, 110.0]) + _candles("B", 5, [10.0, 11.0]))
    assert index_move_pct(idx, T0, T0 + timedelta(hours=1)) is None
    assert index_move_pct([], T0, T0 + timedelta(hours=1)) is None


def test_index_move_is_none_well_past_the_right_edge_rather_than_clamped():
    """Far past coverage the last level is a stale tape, and clamping to it would bias
    exactly the newest trades in one direction."""
    idx = build_index(_candles("A", 0, [100.0, 110.0, 121.0]) + _candles("B", 0, [10.0, 11.0, 12.1]))
    assert index_move_pct(idx, T0 + timedelta(hours=1), T0 + timedelta(hours=99)) is None


def test_index_move_tolerates_a_close_inside_the_current_bar():
    """The ordinary case: the index is hourly, trades are not. A sub-bar gap is
    granularity, not missing data — rejecting it would drop every recent trade."""
    idx = build_index(_candles("A", 0, [100.0, 110.0, 121.0]) + _candles("B", 0, [10.0, 11.0, 12.1]))
    mv = index_move_pct(idx, T0 + timedelta(hours=1), T0 + timedelta(hours=2, minutes=30))
    assert mv is not None and abs(mv - 10.0) < 1e-9, mv


# ------------------------------------------------------ counterfactual gate --
def _mirror(**kw) -> dict:
    m = {"src_still_open": False, "src_close_price": 90.0, "src_entry": 100.0,
         "entry": 100.0, "direction": "LONG"}
    m.update(kw)
    return m


def test_a_live_source_is_never_resolved_even_when_the_join_found_a_match():
    """The quiet twin of the id-join trap: the lateral match is keyed on
    (symbol, model, direction) + time, so while the source is still open it can match
    an EARLIER trade of the same leg. Booking that as 'what holding returned' compares
    against a different trade."""
    state, hold = classify_arm_exit(_mirror(src_still_open=True))
    assert state == "unresolved-source-open" and hold is None, (state, hold)


def test_a_closed_source_with_a_plausible_entry_resolves():
    state, hold = classify_arm_exit(_mirror())
    assert state == "resolved" and hold == -10.0, (state, hold)


def test_entry_mismatch_is_reported_as_its_own_unresolved_reason():
    state, hold = classify_arm_exit(_mirror(src_entry=12.987, entry=5.836e-05))
    assert state == "unresolved-entry-mismatch" and hold is None, (state, hold)


def test_no_match_at_all_is_unresolved():
    assert classify_arm_exit(_mirror(src_close_price=None))[0] == "unresolved-no-match"


# ----------------------------------------------------------------- sums ----
def test_fee_is_charged_once_per_trade():
    assert abs(net_of_fees([1.0, 1.0, 1.0]) - (3.0 - 3 * FEE_RT)) < 1e-12
    assert net_of_fees([]) == 0.0


def test_summarize_counts_a_flat_trade_as_a_loss_not_a_win():
    """Flat after fees is not a win; treating 0 as a win inflates the hit rate."""
    s = summarize([2.0, 0.0, -2.0])
    assert s["n"] == 3
    assert abs(s["win_rate"] - 100.0 / 3) < 1e-9, s
    assert abs(s["mean_loss"] - (-1.0)) < 1e-12, s


def test_summarize_handles_an_empty_population():
    assert summarize([])["n"] == 0


if __name__ == "__main__":
    # Catches Exception, not just AssertionError — a pin that fails by CRASHING is a
    # failing pin, and swallowing that reads exactly like "all green" (T-053).
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

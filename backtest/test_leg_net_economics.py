# backtest/test_leg_net_economics.py — T-2026-KYT-9050-065 pins.
#
# These two corrections changed several verdicts of the same day, so they are pinned
# tightly — each is a place where a plausible-looking slip flips a leg's sign:
#
#   1. The fee is NOT a flat 0.10 %. That is taker/taker, the worst case. Cornix fills a
#      posted entry zone as a limit (maker); a take-profit exit is limit, an SL / regime
#      close / timeout is market. On EPD3 SHORT the difference is -0.0005 vs +0.0365 per
#      trade — the leg did not change, the cost model did.
#   2. Funding is direction-signed and must be summed over each trade's OWN window on its
#      OWN symbol. Across the universe 82 % of rates are positive, which suggests shorts
#      collect; on the coins these legs actually short it is negative. Substituting the
#      universe median for the per-trade sum gets the SIGN wrong.
#
# Runs without a DB:  python backtest/test_leg_net_economics.py

import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.leg_net_economics import (  # noqa: E402
    MAKER,
    MAX_ABS_MOVE_PCT,
    TAKER,
    exit_is_maker,
    funding_pct,
    round_trip_fee,
    signed_move,
)

T0 = datetime(2026, 6, 1)


# ------------------------------------------------------------------- fees --
def test_a_take_profit_exit_is_maker_on_both_sides():
    assert exit_is_maker("ALL TARGETS HIT")
    assert abs(round_trip_fee("ALL TARGETS HIT") - (MAKER + MAKER)) < 1e-12


def test_every_market_close_pays_taker_on_the_way_out():
    """SL, regime close, timeout and delisting all close at market."""
    for status in ("SL Hit (SL: 0.5)", "CLOSED_REGIME_CHANGE", "HORIZON_TIMEOUT",
                   "DELISTED / CLEANUP"):
        assert not exit_is_maker(status), status
        assert abs(round_trip_fee(status) - (MAKER + TAKER)) < 1e-12, status


def test_an_unknown_status_is_charged_the_HIGHER_fee():
    """Assuming 'maker' for something unrecognised would flatter the leg. The unknown
    case must cost more, not less."""
    assert round_trip_fee("SOMETHING NEW") == MAKER + TAKER
    assert round_trip_fee(None) == MAKER + TAKER


def test_the_flat_worst_case_is_never_what_we_charge():
    """0.10 % was the old flat assumption; no real path costs that much."""
    for status in ("ALL TARGETS HIT", "SL Hit (SL: 1)", None):
        assert round_trip_fee(status) < 0.10, status


# ---------------------------------------------------------------- moves ----
def test_move_is_direction_signed():
    assert abs(signed_move(100.0, 110.0, is_long=True) - 10.0) < 1e-12
    assert abs(signed_move(100.0, 110.0, is_long=False) - (-10.0)) < 1e-12


def test_an_impossible_move_is_dropped_not_averaged_in():
    """Raw closed_ai_signals reports a MEDIAN of +21 %/trade on some legs because of
    duplicate/LEGACY rows. Values past the bound are data bugs, not trades."""
    assert signed_move(100.0, 300.0, is_long=True) is None
    # A SHORT can only breach the bound on the LOSS side: its profit asymptotes at
    # +100 % as the price approaches zero, so 1.0 → 3.0 (-200 %) is the case to pin.
    assert signed_move(1.0, 3.0, is_long=False) is None
    assert signed_move(1.0, 0.0001, is_long=False) is not None
    assert signed_move(100.0, 100.0 * (1 + MAX_ABS_MOVE_PCT / 100.0 * 0.99), is_long=True) is not None


def test_nonpositive_prices_are_dropped():
    assert signed_move(0.0, 10.0, is_long=True) is None
    assert signed_move(10.0, 0.0, is_long=True) is None
    assert signed_move(None, 10.0, is_long=True) is None


# -------------------------------------------------------------- funding ----
def _stamps(*hours):
    return [T0 + timedelta(hours=h) for h in hours]


def test_a_short_collects_a_positive_rate_and_a_long_pays_it():
    """Positive rate = longs pay shorts. A sign slip here inverts the correction."""
    times, rates = _stamps(8), [0.0001]
    assert abs(funding_pct(times, rates, T0, T0 + timedelta(hours=9), False) - 0.01) < 1e-12
    assert abs(funding_pct(times, rates, T0, T0 + timedelta(hours=9), True) - (-0.01)) < 1e-12


def test_only_stamps_inside_the_holding_window_count():
    """A position opened after a stamp did not hold through it."""
    times, rates = _stamps(0, 8, 16), [0.0001, 0.0001, 0.0001]
    # Opened at hour 1, closed at hour 9 → only the hour-8 stamp applies.
    v = funding_pct(times, rates, T0 + timedelta(hours=1), T0 + timedelta(hours=9), False)
    assert abs(v - 0.01) < 1e-12, v


def test_the_closing_stamp_counts_and_the_opening_one_does_not():
    times, rates = _stamps(0, 8), [0.0001, 0.0001]
    v = funding_pct(times, rates, T0, T0 + timedelta(hours=8), False)
    assert abs(v - 0.01) < 1e-12, v


def test_a_negative_rate_costs_a_short():
    """The empirically important case: these legs short after pumps, where the rate
    turns against them. Reading the universe median instead gets this backwards."""
    times, rates = _stamps(8), [-0.0002]
    assert funding_pct(times, rates, T0, T0 + timedelta(hours=9), False) < 0


def test_a_symbol_without_funding_rows_contributes_zero_not_a_crash():
    assert funding_pct([], [], T0, T0 + timedelta(hours=9), False) == 0.0


if __name__ == "__main__":
    # Catches Exception, not just AssertionError — a crashing pin is a failing pin.
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

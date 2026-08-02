"""P0.7 second door: zone targets must lie on the profit side of the entry.

T-2026-KYT-9050-009. `find_support_resistance_zones` filters its zones against
the last CLOSED candle's close, while the strategies build their ladder against
`entry = live_price`. Between those two reference prices the market moves, so a
resistance zone can end up BELOW a LONG entry — the old nearest-by-|distance|
pick then put TP1 on the losing side and the interpolation dragged the rest of
the ladder after it. Measured on the live DB (2026-07-01..2026-08-01):
342/3463 Support-Resistance and 12/188 Main-Channel trades were emitted that way.

Standalone and DB-free: run with  python backtest/test_zone_target_side.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.market_utils import select_zone_targets  # noqa: E402

# Zones as find_support_resistance_zones returns them: (price, hit_count),
# ordered by count — NOT by price. The helper must not rely on that order.
LONG_ZONES = [(105.0, 9), (95.0, 7), (110.0, 4), (90.0, 3), (102.0, 2)]
SHORT_ZONES = [(95.0, 9), (105.0, 7), (90.0, 4), (110.0, 3), (98.0, 2)]


def test_long_keeps_only_zones_above_entry():
    got = select_zone_targets(LONG_ZONES, entry=100.0, direction="LONG")
    assert got == [102.0, 105.0, 110.0], got


def test_short_keeps_only_zones_below_entry():
    got = select_zone_targets(SHORT_ZONES, entry=100.0, direction="SHORT")
    assert got == [98.0, 95.0, 90.0], got


def test_ladder_is_monotone_in_trade_direction():
    """Nearest-first on ONE side is monotone by construction — the property the
    old |distance| sort lost as soon as zones straddled the entry."""
    long_targets = select_zone_targets(LONG_ZONES, entry=100.0, direction="LONG")
    assert long_targets == sorted(long_targets), long_targets
    short_targets = select_zone_targets(SHORT_ZONES, entry=100.0, direction="SHORT")
    assert short_targets == sorted(short_targets, reverse=True), short_targets


def test_no_zone_on_the_profit_side_yields_nothing():
    """Feeds the callers' `if t1 == 0: return None` guard — no signal beats a
    signal whose whole ladder runs away from the trade."""
    assert select_zone_targets([(90.0, 5), (95.0, 3)], entry=100.0, direction="LONG") == []
    assert select_zone_targets([(105.0, 5), (110.0, 3)], entry=100.0, direction="SHORT") == []


def test_count_is_capped_and_nearest_wins():
    zones = [(101.0, 1), (102.0, 1), (103.0, 1), (104.0, 1), (105.0, 1)]
    assert select_zone_targets(zones, entry=100.0, direction="LONG") == [101.0, 102.0, 103.0, 104.0]
    assert select_zone_targets(zones, entry=100.0, direction="LONG", count=2) == [101.0, 102.0]


def test_entry_at_or_below_zero_is_refused():
    assert select_zone_targets(LONG_ZONES, entry=0.0, direction="LONG") == []
    assert select_zone_targets(LONG_ZONES, entry=-1.0, direction="LONG") == []


def test_zero_priced_zone_never_becomes_a_short_target():
    """A 0.0 price must not survive as a SHORT target — it would be "below
    entry" numerically and then read as the empty-ladder sentinel."""
    assert select_zone_targets([(0.0, 9), (95.0, 2)], entry=100.0, direction="SHORT") == [95.0]


def test_live_regression_labusdt_short():
    """active_trades_master id=211171 (2026-08-01 08:27 UTC): a SHORT on
    LABUSDT went out with TP1 0.15965 and TP2 0.16020 ABOVE the entry 0.1591 —
    both on the losing side — and the trade monitor scored it status=3."""
    zones = [(0.15965, 6), (0.16020, 4), (0.13635, 3)]
    got = select_zone_targets(zones, entry=0.1591, direction="SHORT")
    assert got == [0.13635], got
    assert all(price < 0.1591 for price in got)


def _main():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'} — {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())

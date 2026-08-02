# backtest/test_roster_proposal.py — T-2026-KYT-9050-065 pins.
#
# The verdict column is what an operator would act on, so its edges are pinned rather
# than trusted:
#
#   1. A rostered leg that emitted NOTHING is DEAD, not KEEP. A seat producing zero is
#      not "fine" — it hides the roster's real size, and five such seats were found.
#   2. A thin leg is never promoted on a handful of trades: below the minimum the sign
#      is not yet a measurement.
#   3. ADD and PROMOTE? must stay distinct. ADD is a roster line; PROMOTE? additionally
#      un-parks a leg, which makes it post to its own Cornix channel and is on the
#      escalation list. Collapsing the two would let a gate flip ride in as bookkeeping.
#
# Runs without a DB:  python backtest/test_roster_proposal.py

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.roster_proposal import ADD, DEAD, DROP, KEEP, PROMOTE, SKIP, verdict  # noqa: E402

MIN = 30


def test_a_rostered_leg_with_no_trades_is_dead_not_kept():
    """A seat that produces nothing is a finding, not a pass."""
    assert verdict(rostered=True, live=True, n=0, net=0.0, min_trades=MIN) == DEAD


def test_a_rostered_profitable_leg_is_kept():
    assert verdict(rostered=True, live=True, n=100, net=0.5, min_trades=MIN) == KEEP


def test_a_rostered_losing_leg_is_dropped():
    assert verdict(rostered=True, live=True, n=1278, net=-0.903, min_trades=MIN) == DROP


def test_a_live_unrostered_profitable_leg_is_add():
    assert verdict(rostered=False, live=True, n=1359, net=0.08, min_trades=MIN) == ADD


def test_a_shadow_profitable_leg_is_promote_not_add():
    """PROMOTE? also un-parks the leg into its own Cornix channel — an escalation item.
    Reporting it as ADD would smuggle a gate flip in as a roster line."""
    assert verdict(rostered=False, live=False, n=122, net=3.41, min_trades=MIN) == PROMOTE


def test_a_thin_leg_is_never_promoted_on_a_handful_of_trades():
    """Below the minimum the sign is not yet a measurement."""
    assert verdict(rostered=False, live=False, n=5, net=9.0, min_trades=MIN) == SKIP
    assert verdict(rostered=False, live=True, n=5, net=9.0, min_trades=MIN) == SKIP


def test_a_thin_but_rostered_leg_is_not_dropped_on_thin_evidence():
    """Dropping a seat needs evidence too — a losing handful is not enough to act on,
    but a thin PROFITABLE rostered leg is left in place."""
    assert verdict(rostered=True, live=True, n=5, net=0.5, min_trades=MIN) == KEEP
    assert verdict(rostered=True, live=True, n=5, net=-0.5, min_trades=MIN) == SKIP


def test_an_unrostered_losing_leg_is_skipped_whatever_its_gate():
    assert verdict(rostered=False, live=True, n=900, net=-0.6, min_trades=MIN) == SKIP
    assert verdict(rostered=False, live=False, n=900, net=-0.6, min_trades=MIN) == SKIP


def test_exactly_zero_net_does_not_count_as_profitable():
    """Break-even earns no seat: the fee model already gave it every benefit."""
    assert verdict(rostered=False, live=True, n=900, net=0.0, min_trades=MIN) == SKIP
    assert verdict(rostered=True, live=True, n=900, net=0.0, min_trades=MIN) == DROP


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

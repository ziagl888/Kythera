# backtest/test_trailing_intake_audit.py — T-2026-KYT-9050-060 pins.
#
# The audit exists because reading ONE gate produces a confident wrong answer: the
# freshness window looks like the throttle (rejects pile up at 241–256 s against a
# 240 s limit) while the gate that actually binds is the exposure cap, which leaves
# no DB row at all. These pins hold the two pieces of reasoning that carry that
# conclusion:
#
#   1. The tally parse. It is the ONLY evidence for the log-only gates, and a silent
#      mis-parse would hand back plausible numbers for the wrong gate.
#   2. The cap arithmetic. The cap constrains the DIFFERENCE, which is what turns
#      short-side supply into the ceiling on total volume. Getting the direction of
#      that identity wrong inverts the recommendation.
#
# Runs without a DB and without a log:  python backtest/test_trailing_intake_audit.py

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.trailing_intake_audit import headroom, parse_tally, summarize_tallies  # noqa: E402

LINE = ("2026-08-01 14:42:47,802 - TRAILING_BOT - \\u26d4 5 nicht aufgenommen "
        "(EXPOSURE_CAP 4, SYMBOL_HELD 1)")


# ------------------------------------------------------------------ parsing --
def test_tally_line_yields_day_and_per_gate_counts():
    day, gates = parse_tally(LINE)
    assert day == "2026-08-01"
    assert gates == {"EXPOSURE_CAP": 4, "SYMBOL_HELD": 1}, gates


def test_single_gate_line_parses():
    day, gates = parse_tally(
        "2026-07-30 07:47:46,788 - TRAILING_BOT - \\u26d4 1 nicht aufgenommen (SYMBOL_HELD 1)")
    assert gates == {"SYMBOL_HELD": 1}, gates


def test_three_gate_line_parses():
    _, gates = parse_tally(
        "2026-07-31 09:00:00,000 - TRAILING_BOT - x 9 nicht aufgenommen "
        "(EXPOSURE_CAP 4, SYMBOL_COOLING 2, SYMBOL_HELD 3)")
    assert gates == {"EXPOSURE_CAP": 4, "SYMBOL_COOLING": 2, "SYMBOL_HELD": 3}, gates


def test_lines_from_other_bots_are_ignored():
    """The tally sits in the SHARED fleet log — every other bot writes there too."""
    assert parse_tally("2026-08-01 10:00:00,000 - AI_MONITOR - 5 nicht aufgenommen (X 1)") is None
    assert parse_tally("2026-08-01 10:00:00,000 - TRAILING_BOT - Mirror: BTCUSDT AIM2 SHORT @ 1.0") is None
    assert parse_tally("") is None


def test_gate_name_is_split_off_the_count_not_the_other_way_round():
    """Gate names carry underscores; a rpartition on the wrong side eats the name."""
    _, gates = parse_tally("2026-08-01 10:00:00,000 - TRAILING_BOT - x 7 nicht aufgenommen (EXPOSURE_CAP 7)")
    assert list(gates) == ["EXPOSURE_CAP"], gates
    assert gates["EXPOSURE_CAP"] == 7


def test_summary_counts_cycles_separately_from_blocked_candidates():
    """A gate blocking 4 candidates in one cycle is not the same as 4 cycles.

    Rejections REPEAT every cycle while the source trade stays open, so these are a
    standing pressure, never a distinct-signal count — conflating the two overstates
    every gate by however many cycles it persisted.
    """
    lines = [
        "2026-08-01 10:00:00,000 - TRAILING_BOT - x 4 nicht aufgenommen (EXPOSURE_CAP 4)",
        "2026-08-01 10:00:10,000 - TRAILING_BOT - x 6 nicht aufgenommen (EXPOSURE_CAP 6)",
        "2026-08-02 10:00:00,000 - TRAILING_BOT - x 1 nicht aufgenommen (SYMBOL_HELD 1)",
    ]
    s = summarize_tallies(lines)
    assert s["2026-08-01"]["_cycles"]["n"] == 2
    assert s["2026-08-01"]["EXPOSURE_CAP"] == {"cycles": 2, "mean": 5.0, "max": 6}
    assert "EXPOSURE_CAP" not in s["2026-08-02"]


def test_a_gate_that_never_fires_is_absent_rather_than_zero():
    """SLOT_CAP never appearing is the finding 'the channel is not full' — it must
    not be manufactured as a 0-row that reads like a measured value."""
    s = summarize_tallies(["2026-08-01 10:00:00,000 - TRAILING_BOT - x 1 nicht aufgenommen (SYMBOL_HELD 1)"])
    assert "SLOT_CAP" not in s["2026-08-01"]


# --------------------------------------------------------------- cap maths --
def test_cap_blocks_the_leading_direction_only():
    h = headroom(open_long=80, open_short=28, cap=50)
    assert h["imbalance"] == 52
    assert h["long_blocked"] and not h["short_blocked"]
    assert h["long_headroom"] == 0
    assert h["short_headroom"] == 102


def test_cap_is_symmetric():
    h = headroom(open_long=10, open_short=70, cap=50)
    assert h["short_blocked"] and not h["long_blocked"]
    assert h["short_headroom"] == 0


def test_a_balanced_book_blocks_neither_side():
    h = headroom(open_long=40, open_short=40, cap=50)
    assert not h["long_blocked"] and not h["short_blocked"]
    assert h["long_headroom"] == 50 and h["short_headroom"] == 50


def test_the_cap_binds_at_exactly_the_limit_not_one_past_it():
    """admit() uses >=, so a book exactly AT the cap already refuses."""
    assert headroom(50, 0, 50)["long_blocked"]
    assert not headroom(49, 0, 50)["long_blocked"]


def test_total_capacity_grows_by_two_per_extra_short():
    """The identity behind the whole recommendation: with the book at the ceiling,
    one more SHORT position unlocks one more LONG slot, so total capacity moves by 2.
    Inverting this sends the operator at the long side, which is already blocked."""
    before = headroom(80, 30, 50)["total_capacity_at_cap"]
    after = headroom(80, 31, 50)["total_capacity_at_cap"]
    assert after - before == 2, (before, after)


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

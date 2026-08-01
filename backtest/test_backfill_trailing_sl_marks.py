# backtest/test_backfill_trailing_sl_marks.py — T-2026-KYT-9050-054 pins.
#
# This script WRITES to the live DB, so its planning step is pinned tighter than a
# read-only tool's would be. What must hold:
#
#   1. The reconstructed value is direction-correct. A SHORT stopped out ABOVE its
#      entry lost money; a sign slip here books 67 losses as profits and would make
#      the book look better than doing nothing at all.
#   2. A stop that reconstructs to a GAIN is refused, never written. That can only
#      mean the row's sl sits on the wrong side of entry — a data fault, and the one
#      shape of bad input that a plausible-looking number would hide.
#   3. Rows the script has no business touching are not in the plan at all.
#   4. The UPDATE re-checks its own guard, so a mark the live bot booked between
#      preview and write is never overwritten.
#
# Runs without a DB:  python backtest/test_backfill_trailing_sl_marks.py

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.backfill_trailing_sl_marks import SQL_CANDIDATES, SQL_UPDATE, plan  # noqa: E402


def _row(row_id=1, direction="LONG", entry=100.0, sl=95.0, **kw):
    r = {"id": row_id, "symbol": "XUSDT", "model": "MIS1-72H",
         "direction": direction, "entry": entry, "sl": sl, "closed_at": None}
    r.update(kw)
    return r


# --------------------------------------------------------------------- (1) --
def test_long_stop_below_entry_is_a_loss():
    writes, refused = plan([_row(entry=100.0, sl=95.0)])
    assert refused == []
    assert writes == [(1, -5.0)], writes


def test_short_stop_above_entry_is_also_a_loss():
    """The sign trap: a SHORT stopped out ABOVE entry lost 5 %, it did not gain 5 %."""
    writes, refused = plan([_row(direction="SHORT", entry=100.0, sl=105.0)])
    assert refused == []
    assert writes == [(1, -5.0)], writes


def test_reconstruction_scales_with_distance_to_the_stop():
    writes, _ = plan([_row(entry=0.02, sl=0.0175)])
    assert abs(writes[0][1] - (-12.5)) < 1e-9, writes


# --------------------------------------------------------------------- (2) --
def test_stop_on_the_wrong_side_is_refused_not_written():
    """A LONG whose sl is ABOVE entry reconstructs to a gain — that is a data fault."""
    writes, refused = plan([_row(entry=100.0, sl=105.0)])
    assert writes == []
    assert len(refused) == 1 and "gain" in refused[0]["why"], refused


def test_short_with_stop_below_entry_is_refused():
    writes, refused = plan([_row(direction="SHORT", entry=100.0, sl=95.0)])
    assert writes == [] and len(refused) == 1, (writes, refused)


def test_a_stop_exactly_at_entry_is_refused():
    """Zero is not a loss, so it is not a stop-out this script can vouch for."""
    writes, refused = plan([_row(entry=100.0, sl=100.0)])
    assert writes == [] and len(refused) == 1, (writes, refused)


# --------------------------------------------------------------------- (3) --
def test_missing_or_impossible_inputs_are_refused():
    writes, refused = plan([_row(row_id=1, sl=None), _row(row_id=2, entry=0.0),
                            _row(row_id=3, entry=None)])
    assert writes == []
    assert len(refused) == 3, refused


def test_good_and_bad_rows_are_separated_not_all_or_nothing():
    writes, refused = plan([_row(row_id=1), _row(row_id=2, sl=105.0), _row(row_id=3, sl=90.0)])
    assert [w[0] for w in writes] == [1, 3], writes
    assert [r["id"] for r in refused] == [2], refused


# --------------------------------------------------------------------- (4) --
def test_candidate_query_selects_only_repairable_sl_rows():
    sql = " ".join(SQL_CANDIDATES.split())
    assert "close_reason = 'SL_HIT'" in sql
    assert "close_mark_pct IS NULL" in sql
    assert "sl IS NOT NULL" in sql
    assert "entry > 0" in sql


def test_update_rechecks_the_guard_so_a_live_booked_mark_is_never_overwritten():
    """Between preview and write the bot may book its own mark; it must win."""
    sql = " ".join(SQL_UPDATE.split())
    assert "close_mark_pct IS NULL" in sql, sql
    assert "close_reason = 'SL_HIT'" in sql, sql
    assert "WHERE id = %s" in sql, sql


def test_update_touches_exactly_one_column():
    sql = " ".join(SQL_UPDATE.split())
    assert sql.count("SET") == 1
    assert "SET close_mark_pct = %s" in sql, sql
    # close_reason / closed_at / posted must not be rewritten by a reporting repair.
    for col in ("closed_at", "posted", "peak_pct"):
        assert f"SET {col}" not in sql and f", {col} =" not in sql, col


def test_plan_is_idempotent_on_an_empty_candidate_set():
    assert plan([]) == ([], [])


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

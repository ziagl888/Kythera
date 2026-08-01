"""T-2026-KYT-9050-054 — backfill close_mark_pct on the pre-fix SL_HIT rows.

What is being repaired
----------------------
Until the T-053 fix went live (2026-07-30 07:50) every SL hit was booked with
``close_mark_pct = NULL``. The reasoning then — "the book should not claim a value
nobody measured" — holds for a missing MARKET price. It does not hold for a stop:
the fill sits on the stop level, and the level is in the row. The result was a
reporting defect that hid precisely the worst exits, so a sum over
``close_mark_pct`` read −188 % instead of −575 % gross: a factor of 3, optimistic
exactly where it hurts.

The live path stopped producing these rows on 2026-07-30. This script repairs the
ones already written.

What it computes
----------------
``core.trailing_state.mark_pct(entry, sl, is_long)`` — the same function the live
bot and the report use (Regel 7), so the backfilled value cannot drift from either.
Assumption, deliberate and identical to the study's treatment of hard stops:
**fill at the stop level, no slippage.** A gap through the stop realises worse, so
each written value is the optimistic edge, not the expectation.

What it refuses to touch
------------------------
  * anything that is not ``close_reason = 'SL_HIT'``;
  * rows that already carry a mark (the bot's measured value always wins);
  * rows without ``sl`` — the level is unknown there and guessing it is the exact
    error the original NULL-booking was avoiding;
  * rows whose reconstructed mark is not a LOSS. A stop is adverse by definition,
    so ``mark >= 0`` means the row's sl sits on the wrong side of entry — a data
    fault to be reported, never silently written.

Live-DB safety (harte Regel 1)
------------------------------
Dry-run is the DEFAULT and prints every row it would change. Writing requires
``--apply`` AND the explicit operator token ``--yes-write-live-db``; both together
are the documented go-ahead. The write runs in ONE transaction, re-checks the guard
inside the UPDATE itself (so a row that changed underneath us is skipped rather
than overwritten), and aborts if the affected row count differs from the preview.
Re-running is idempotent: the second run finds nothing.

Usage:
    python tools/backfill_trailing_sl_marks.py                      # preview
    python tools/backfill_trailing_sl_marks.py --apply --yes-write-live-db
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.trailing_state import mark_pct  # noqa: E402

#: Only ever these rows. Spelled out so the guard is readable next to the UPDATE.
SQL_CANDIDATES = """
    SELECT id, symbol, model, direction, entry, sl, closed_at
    FROM trailing_positions
    WHERE close_reason = 'SL_HIT'
      AND close_mark_pct IS NULL
      AND sl IS NOT NULL
      AND entry > 0
    ORDER BY closed_at
"""

#: The guard is repeated in the WHERE clause on purpose: between preview and write
#: the live bot may have booked a mark itself, and it must win.
SQL_UPDATE = """
    UPDATE trailing_positions
    SET close_mark_pct = %s
    WHERE id = %s
      AND close_reason = 'SL_HIT'
      AND close_mark_pct IS NULL
      AND sl IS NOT NULL
"""


def plan(rows: list[dict]) -> tuple[list[tuple[int, float]], list[dict]]:
    """Split candidate rows into (id, mark) writes and rows to refuse.

    A refusal is a finding, not a nuisance: a stop that reconstructs to a GAIN means
    the stored ``sl`` is on the wrong side of ``entry`` for that direction, and
    writing it would book a loss as profit.
    """
    writes: list[tuple[int, float]] = []
    refused: list[dict] = []
    for r in rows:
        entry, sl = r.get("entry"), r.get("sl")
        if entry is None or sl is None or float(entry) <= 0:
            refused.append({**r, "why": "missing entry/sl"})
            continue
        mark = mark_pct(float(entry), float(sl), r["direction"] == "LONG")
        if mark >= 0:
            refused.append({**r, "why": f"stop reconstructs to a gain ({mark:+.2f} %)"})
            continue
        writes.append((int(r["id"]), mark))
    return writes, refused


def load_candidates(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(SQL_CANDIDATES)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def apply_writes(conn, writes: list[tuple[int, float]]) -> int:
    """One transaction. Returns rows actually updated; caller compares to the plan."""
    updated = 0
    with conn.cursor() as cur:
        for row_id, mark in writes:
            cur.execute(SQL_UPDATE, (mark, row_id))
            updated += cur.rowcount
    return updated


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    ap.add_argument("--yes-write-live-db", action="store_true",
                    help="operator confirmation, required together with --apply")
    args = ap.parse_args()

    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        rows = load_candidates(conn)
        writes, refused = plan(rows)

        print("candidates=%d  to write=%d  refused=%d" % (len(rows), len(writes), len(refused)))
        if not rows:
            print("nothing to do — already backfilled (this script is idempotent)")
            return 0

        marks = [m for _, m in writes]
        by_id = {int(r["id"]): r for r in rows}
        print()
        print("  %-8s %-16s %-11s %-6s %12s %12s %9s" %
              ("id", "symbol", "model", "dir", "entry", "sl", "mark %"))
        for row_id, mark in writes:
            r = by_id[row_id]
            print("  %-8d %-16s %-11s %-6s %12.8g %12.8g %+9.2f"
                  % (row_id, r["symbol"], r["model"], r["direction"],
                     float(r["entry"]), float(r["sl"]), mark))
        if refused:
            print("\n  REFUSED (not written — inspect these):")
            for r in refused:
                print("    id=%s %s %s: %s" % (r["id"], r["symbol"], r["direction"], r["why"]))

        if marks:
            print("\n  Σ %.1f %%   mean %.2f %%   median %.2f %%   worst %.2f %%"
                  % (sum(marks), sum(marks) / len(marks),
                     sorted(marks)[len(marks) // 2], min(marks)))
            print("  effect: a SUM(close_mark_pct) over the book moves by %.1f %%-points" % sum(marks))

        if not args.apply:
            print("\nDRY-RUN — nothing written. To write:"
                  "\n  python tools/backfill_trailing_sl_marks.py --apply --yes-write-live-db")
            return 0

        if not args.yes_write_live_db:
            print("\nREFUSED: --apply needs --yes-write-live-db (live DB, harte Regel 1).")
            return 2

        updated = apply_writes(conn, writes)
        if updated != len(writes):
            conn.rollback()
            print("\nROLLED BACK: planned %d writes but the UPDATE matched %d rows."
                  " Something changed underneath — re-run the preview." % (len(writes), updated))
            return 3
        conn.commit()
        print("\nCOMMITTED %d rows." % updated)

        remaining = len(load_candidates(conn))
        print("remaining candidates after the write: %d (expected 0 unless some were refused)"
              % remaining)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

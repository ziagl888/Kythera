#!/usr/bin/env python3
"""Read-only shadow-performance tracker for a model tag in closed_ai_signals.

Motivation: EPD2 carries a formal "not deployable" walk-forward verdict, but a
tiny LONG shadow window (n=8, 2 days) looked promising (2026-07-08). Instead of
overriding the verdict on a micro-sample, we watch the shadow edge accumulate
until n is large enough over a real month to decide.

Signal-level PnL only (per-signal, unleveraged, dedup on the natural key). This
is NOT realized account PnL — closed_ai_signals mixes shadow and posted signals.
Strictly SELECT-only; never writes.

Usage:
    py -3.13 tools/track_shadow_model.py EPD2            # since first close
    py -3.13 tools/track_shadow_model.py EPD2 2026-07-05 # since a date
"""

from __future__ import annotations

import sys

import psycopg2


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "EPD2"
    since = sys.argv[2] if len(sys.argv) > 2 else "2000-01-01"

    conn = psycopg2.connect(
        dbname="cryptodata",
        user="postgres",
        host="localhost",
        port=5432,
        connect_timeout=5,
    )
    conn.set_session(readonly=True)
    cur = conn.cursor()

    # Deduplicate on the natural key; compute direction-signed signal PnL%.
    base = """
    WITH d AS (
      SELECT DISTINCT symbol, model, direction, entry, close_price, open_time, close_time
      FROM closed_ai_signals
      WHERE model LIKE %s AND close_time >= %s AND entry > 0 AND close_price > 0
    ),
    p AS (
      SELECT upper(direction) AS dir,
             CASE WHEN direction ILIKE 'SHORT' THEN (entry - close_price) / entry * 100
                  ELSE (close_price - entry) / entry * 100 END AS pnl,
             close_time
      FROM d
    )
    """
    like = tag + "%"

    print(f"=== {tag} shadow tracking (since {since}) — signal PnL%, deduped ===\n")

    cur.execute(
        base
        + """
        SELECT dir, count(*) n,
               round(100.0 * avg((pnl > 0)::int), 1) wr,
               round(avg(pnl)::numeric, 3) avg_pnl,
               round(sum(pnl)::numeric, 2) sum_pnl,
               round(min(pnl)::numeric, 2) worst,
               round(max(pnl)::numeric, 2) best,
               min(close_time)::date first_c, max(close_time)::date last_c
        FROM p GROUP BY dir ORDER BY dir
    """,
        (like, since),
    )
    rows = cur.fetchall()
    hdr = f"{'dir':6}{'n':>5}{'WR%':>7}{'avgP%':>9}{'sumP%':>10}{'worst':>9}{'best':>9}  span"
    print(hdr)
    print("-" * len(hdr))
    for dr, n, wr, ap, sp, wo, be, fc, lc in rows:
        print(f"{dr:6}{n:>5}{str(wr):>7}{str(ap):>9}{str(sp):>10}{str(wo):>9}{str(be):>9}  {fc}..{lc}")

    print("\n--- daily cumulative (all directions) ---")
    cur.execute(
        base
        + """
        SELECT close_time::date d, dir, count(*) n, round(sum(pnl)::numeric, 2) sum_pnl
        FROM p GROUP BY d, dir ORDER BY d, dir
    """,
        (like, since),
    )
    for d, dr, n, sp in cur.fetchall():
        print(f"   {d}  {dr:6} n={n:>3}  sumPnL%={sp}")

    print("\nNote: signal-level, unleveraged, shadow+posted mixed — NOT account PnL.")
    print("Decision bar: judge only once n per side is large over a full month.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

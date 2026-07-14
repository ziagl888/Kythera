#!/usr/bin/env python3
"""Read-only: find sources that are GLOBALLY NEGATIVE but REGIME-conditioned
positive, and check which of those cells survive the v2 EB-shrinkage gate
(T-2026-CU-9050-125, Teil 3 / evidence for the T-2026-CU-9050-069 whitelist-v2 flip).

Question (Michi): ROM (Bot 28) and AIM (Bot 15) gate whole sources off. But a
source that is negative over the WHOLE period can be positive in the RIGHT market
regime. Are there such sources — and does a regime-conditioned gate beat a blanket
off?

Data (already materialized by 27_bot_regime_analyzer, hourly):
  * bot_regime_performance — avg_pnl_pct / win_rate / n_trades per
    (bot_name, regime, alt_context, direction, window_days). The (regime='ALL',
    alt_context='ALL') row is the GLOBAL expectancy of that (bot, direction).
  * bot_regime_whitelist — per (bot, regime, alt_context, direction): v1 gate
    (whitelisted/reason) and the shadow v2 gate (whitelisted_v2/reason_v2), where
    reason_v2 carries the EB-shrinkage lower bound (lb), point estimate (est),
    source and effective n (neff).

Strictly SELECT-only; never writes. Run on the VPS (DB is local).

Usage:
    py -3.13 tools/regime_conditioned_gating_scan.py            # window 90, text
    py -3.13 tools/regime_conditioned_gating_scan.py --window 30
    py -3.13 tools/regime_conditioned_gating_scan.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg2

REGIMES = ("TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION")


def _connect():
    """Read-only connection. Env overrides, else the local-VPS defaults that
    tools/track_shadow_model.py uses."""
    conn = psycopg2.connect(
        dbname=os.environ.get("DB_NAME", "cryptodata"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD") or None,
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        connect_timeout=8,
    )
    conn.set_session(readonly=True)
    return conn


def scan(conn, window: int, min_cell_n: int, min_global_n: int) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT max(last_computed) FROM bot_regime_performance")
    computed = cur.fetchone()[0]

    # (1) globally-negative (bot, direction) with a per-regime cell that flips positive.
    cur.execute(
        """
        WITH g AS (
          SELECT bot_name, direction, avg_pnl_pct AS gpnl, n_trades AS gn
          FROM bot_regime_performance
          WHERE regime='ALL' AND alt_context='ALL' AND window_days=%s
        ),
        r AS (
          SELECT bot_name, direction, regime, avg_pnl_pct AS rpnl,
                 win_rate AS rwr, n_trades AS rn
          FROM bot_regime_performance
          WHERE regime<>'ALL' AND alt_context='ALL' AND window_days=%s
        )
        SELECT g.bot_name, g.direction, g.gpnl, g.gn, r.regime, r.rpnl, r.rwr, r.rn
        FROM g JOIN r USING (bot_name, direction)
        WHERE g.gpnl < 0 AND r.rpnl > 0 AND r.rn >= %s AND g.gn >= %s
        ORDER BY g.bot_name, g.direction, r.rpnl DESC
        """,
        (window, window, min_cell_n, min_global_n),
    )
    flips: list[dict] = []
    for bot, d, gp, gn, reg, rp, rwr, rn in cur.fetchall():
        flips.append(
            {
                "bot": bot,
                "direction": d,
                "global_avg_pnl": round(float(gp), 3),
                "global_n": int(gn),
                "regime": reg,
                "regime_avg_pnl": round(float(rp), 3),
                "regime_wr": round(float(rwr), 2),
                "regime_n": int(rn),
            }
        )

    # (2) globally-negative (bot, direction) legs whose regime cell SURVIVES the v2
    #     EB-shrinkage gate (whitelisted_v2 = true → lower bound > break-even).
    cur.execute(
        """
        SELECT bot_name, direction, avg_pnl_pct
        FROM bot_regime_performance
        WHERE regime='ALL' AND alt_context='ALL' AND window_days=%s AND avg_pnl_pct < 0
        """,
        (window,),
    )
    neg = {(b, d): float(p) for b, d, p in cur.fetchall()}

    cur.execute(
        """
        SELECT bot_name, regime, alt_context, direction, reason_v2
        FROM bot_regime_whitelist
        WHERE whitelisted_v2 = true
        ORDER BY bot_name, direction, regime, alt_context
        """
    )
    robust: list[dict] = []
    for b, reg, alt, d, rv in cur.fetchall():
        g = neg.get((b, d))
        if g is None:
            g = neg.get((b, "BOTH"))
        if g is not None:
            robust.append(
                {
                    "bot": b,
                    "direction": d,
                    "global_avg_pnl": round(g, 3),
                    "regime": reg,
                    "alt_context": alt,
                    "reason_v2": rv,
                }
            )

    return {
        "window_days": window,
        "regime_perf_last_computed": str(computed),
        "min_cell_n": min_cell_n,
        "min_global_n": min_global_n,
        "point_estimate_flips": flips,
        "v2_robust_under_negative_bot": robust,
    }


def _print_report(res: dict) -> None:
    print(f"# Regime-conditioned gating scan — window {res['window_days']}d")
    print(f"# bot_regime_performance last computed: {res['regime_perf_last_computed']}")
    print(f"# thresholds: cell n>={res['min_cell_n']}, global n>={res['min_global_n']}\n")

    print("## (1) POINT-ESTIMATE flips (global<0, a regime cell>0) — TEMPTING, not yet vetted")
    cur = None
    for f in res["point_estimate_flips"]:
        key = (f["bot"], f["direction"])
        if key != cur:
            print(f"\n  {f['bot']:22s} {f['direction']:5s}  global={f['global_avg_pnl']:+6.2f}% (n={f['global_n']})")
            cur = key
        print(f"       {f['regime']:12s} avg={f['regime_avg_pnl']:+6.2f}%  wr={f['regime_wr']:.2f}  n={f['regime_n']}")
    if not res["point_estimate_flips"]:
        print("  (none)")

    print("\n## (2) v2-ROBUST cells under a globally-negative bot+direction (lb>0 after EB-shrinkage)")
    print("     These are the DEFENSIBLE regime-conditioned-enable candidates.")
    for r in res["v2_robust_under_negative_bot"]:
        print(
            f"  {r['bot']:10s} {r['direction']:5s} global={r['global_avg_pnl']:+5.2f}%  ::  "
            f"{r['regime']}/{r['alt_context']}  {r['reason_v2']}"
        )
    if not res["v2_robust_under_negative_bot"]:
        print("  (none)")
    print(f"\n=> {len(res['v2_robust_under_negative_bot'])} regime-robust cells sit under a globally-negative leg.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=90, choices=(7, 30, 90))
    ap.add_argument("--min-cell-n", type=int, default=25)
    ap.add_argument("--min-global-n", type=int, default=40)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = _connect()
    try:
        res = scan(conn, args.window, args.min_cell_n, args.min_global_n)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        _print_report(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())

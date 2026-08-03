"""tools/aim2_topn_calibrate.py — read-only threshold calibration for AIM2-TOPN.

The AIM2-TOPN channel (T-2026-CU-9050-051) approximates "top 1-3 of the day" via
high minimum probability plus hard 24h cap. This tool estimates from
historical AIM2 scores which threshold live leads to ~1-3 posts/day — so
Michi doesn't have to guess AIM2_TOPN_MIN_PROB.

Source: master_ai_processed_signals (ml_confidence = calibrated AIM2 prob per
scored candidate, processed_at = time). READ-ONLY — no insert/update, no
artefact writing, no live intervention. Runs only on VPS (build machine
has no DB).

Call (VPS):  python tools/aim2_topn_calibrate.py [--days 30]

Output: per candidate threshold the resulting rate posts/day and the
smallest threshold that hits the target band (default 1-3/day). The cap
N is ONLY a backstop — the threshold should carry the rate, not the cap.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database import get_db_connection  # noqa: E402

# Kandidaten-Schwellen: dicht im interessanten oberen Band.
THRESHOLDS = [0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99]


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only AIM2-TOPN threshold calibration")
    ap.add_argument("--days", type=int, default=30, help="Analyse-Fenster in Tagen (Default 30)")
    ap.add_argument("--target-lo", type=float, default=1.0, help="Ziel-Rate untere Grenze (Posts/Tag)")
    ap.add_argument("--target-hi", type=float, default=3.0, help="Ziel-Rate obere Grenze (Posts/Tag)")
    args = ap.parse_args()

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ml_confidence
                FROM master_ai_processed_signals
                WHERE processed_at > NOW() - %s::interval
                  AND ml_confidence IS NOT NULL
                """,
                (f"{int(args.days)} days",),
            )
            probs = [float(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()

    if not probs:
        print(f"No scored candidates in the last {args.days} days — nothing to calibrate.")
        return

    n = len(probs)
    days = max(1.0, float(args.days))
    print(f"AIM2-TOPN calibration — {n} scored candidates over {args.days} days ({n / days:.0f}/day total)\n")
    print(f"  {'Threshold':>9} | {'Posts/Day':>9} | {'Share':>7}")
    print(f"  {'-' * 9}-+-{'-' * 9}-+-{'-' * 7}")

    recommendation = None
    for thr in THRESHOLDS:
        passed = sum(1 for p in probs if p >= thr)
        rate = passed / days
        share = passed / n
        flag = ""
        if args.target_lo <= rate <= args.target_hi:
            flag = "  ← target band"
            if recommendation is None:
                recommendation = thr
        print(f"  {thr:>9.2f} | {rate:>9.2f} | {share:>6.1%}{flag}")

    print()
    if recommendation is not None:
        print(
            f"Recommendation: AIM2_TOPN_MIN_PROB={recommendation:.2f} "
            f"(~{args.target_lo:.0f}-{args.target_hi:.0f}/day). "
            f"Set AIM2_TOPN_N as backstop independently (e.g. 3)."
        )
    else:
        print(
            "No threshold exactly hits the target band — adjust bounds (--target-lo/--target-hi) "
            "or extend window (--days)."
        )
    print("Note: read-only — this tool doesn't enable anything (gate flip = Michi's decision).")


if __name__ == "__main__":
    main()

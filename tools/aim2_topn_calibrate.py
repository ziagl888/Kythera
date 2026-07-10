"""tools/aim2_topn_calibrate.py — read-only Schwellen-Kalibrierung für AIM2-TOPN.

Der AIM2-TOPN-Kanal (T-2026-CU-9050-051) approximiert "Top 1-3 des Tages" über
eine hohe Mindest-Probability plus harte 24h-Kappe. Dieses Tool schätzt aus den
historischen AIM2-Scores, welche Schwelle live zu ~1-3 Posts/Tag führt — damit
Michi AIM2_TOPN_MIN_PROB nicht raten muss.

Quelle: master_ai_processed_signals (ml_confidence = kalibrierte AIM2-Prob je
gescortem Kandidat, processed_at = Zeit). Rein LESEND — kein Insert/Update, kein
Artefakt-Schreiben, kein Live-Eingriff. Läuft nur auf dem VPS (Build-Maschine
hat keine DB).

Aufruf (VPS):  python tools/aim2_topn_calibrate.py [--days 30]

Ausgabe: je Kandidat-Schwelle die resultierende Rate Posts/Tag und der
kleinste Threshold, der die Ziel-Bandbreite (Default 1-3/Tag) trifft. Die Kappe
N ist NUR ein Backstop — die Schwelle soll die Rate tragen, nicht die Kappe.
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
        print(f"Keine gescorten Kandidaten in den letzten {args.days} Tagen — nichts zu kalibrieren.")
        return

    n = len(probs)
    days = max(1.0, float(args.days))
    print(f"AIM2-TOPN-Kalibrierung — {n} gescorte Kandidaten über {args.days} Tage ({n / days:.0f}/Tag gesamt)\n")
    print(f"  {'Threshold':>9} | {'Posts/Tag':>9} | {'Anteil':>7}")
    print(f"  {'-' * 9}-+-{'-' * 9}-+-{'-' * 7}")

    recommendation = None
    for thr in THRESHOLDS:
        passed = sum(1 for p in probs if p >= thr)
        rate = passed / days
        share = passed / n
        flag = ""
        if args.target_lo <= rate <= args.target_hi:
            flag = "  ← Ziel-Band"
            if recommendation is None:
                recommendation = thr
        print(f"  {thr:>9.2f} | {rate:>9.2f} | {share:>6.1%}{flag}")

    print()
    if recommendation is not None:
        print(
            f"Empfehlung: AIM2_TOPN_MIN_PROB={recommendation:.2f} "
            f"(~{args.target_lo:.0f}-{args.target_hi:.0f}/Tag). "
            f"AIM2_TOPN_N als Backstop unabhängig setzen (z.B. 3)."
        )
    else:
        print(
            "Keine Schwelle trifft das Ziel-Band exakt — Grenzen anpassen (--target-lo/--target-hi) "
            "oder das Fenster (--days) verlängern."
        )
    print("Hinweis: rein lesend — dieses Tool schaltet nichts scharf (Gate-Flip = Michis Entscheid).")


if __name__ == "__main__":
    main()

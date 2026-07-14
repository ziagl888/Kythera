"""
tools/retrain_ats.py — ATS2-Retrain (Bot 12 TSI-Sniper) in EINEM Aufruf.

DB → Features → Replay-Label → Train → Staging, jederzeit wiederholbar. Kein
CSV-Zwischenschritt (die alten X8-TSI-EXPORT/-ML-Skripte in Documents\\_X sind
damit abgelöst), R1-clean über core.candles (include_forming=False), Feature-
Vektor bit-gleich zum Serving (core.ats_features, harte Regel 7). Artefakte NUR
nach staging_models mit model_id=ATS2 (harte Regel 2/6) — KEIN Rollout.

Ist ein dünner Orchestrator über die getesteten Fleet-Tools (Low-Priority,
CPU-Headroom-Check, Reconnect-Logik, chronologischer Split, pick_threshold_safe,
Isotonic-Kalibrierung, Staging-Guard leben dort):

  Stufe 1: tools/walkforward_sim.py   --strategy ats --days N   → ats_replay_Nd.jsonl
  Stufe 2: tools/retrain_from_replay.py --strategy ats --replay …   → ats2_model_{LONG,SHORT}.pkl

Beispiele:
  python tools/retrain_ats.py                      # letzte 540 Tage
  python tools/retrain_ats.py --days 365
  python tools/retrain_ats.py --since 2025-01-01   # ab Datum bis heute
  python tools/retrain_ats.py --skip-replay        # Replay-JSONL existiert schon, nur Stufe 2
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPLAY_DIR = os.getenv("KYTHERA_REPLAY_DIR", r"C:\Users\Michael\Documents\_X\staging_models\replay")


def _resolve_days(args: argparse.Namespace) -> int:
    if args.since:
        try:
            since = datetime.datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        except ValueError as e:
            raise SystemExit(f"--since erwartet YYYY-MM-DD, bekam {args.since!r}") from e
        days = (datetime.datetime.now(datetime.timezone.utc) - since).days
        if days < 1:
            raise SystemExit(f"--since {args.since} liegt in der Zukunft / heute — nichts zu tun.")
        return days
    return args.days


def _run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(cmd)}\n", flush=True)
    res = subprocess.run(cmd, cwd=REPO_ROOT)
    if res.returncode != 0:
        raise SystemExit(f"Abbruch: '{' '.join(cmd[:3])} …' endete mit Code {res.returncode}")


def main() -> None:
    ap = argparse.ArgumentParser(description="ATS2-Retrain (Bot 12) in einem Aufruf")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--days", type=int, default=540, help="Trainingsfenster in Tagen (Default 540)")
    grp.add_argument("--since", default=None, help="Startdatum YYYY-MM-DD (bis heute); alternativ zu --days")
    ap.add_argument("--coins", default=None, help="nur diese Coins (Komma-Liste) für den Replay")
    ap.add_argument("--limit", type=int, default=None, help="nur die ersten N Coins (Smoke-Test)")
    ap.add_argument("--skip-replay", action="store_true", help="Stufe 1 überspringen (JSONL existiert bereits)")
    ap.add_argument("--resume", action="store_true", help="Stufe-1-Replay an bestehendes JSONL anhängen")
    args = ap.parse_args()

    days = _resolve_days(args)
    replay_path = os.path.join(REPLAY_DIR, f"ats_replay_{days}d.jsonl")
    py = sys.executable

    if not args.skip_replay:
        cmd = [py, os.path.join("tools", "walkforward_sim.py"), "--strategy", "ats", "--days", str(days)]
        if args.coins:
            cmd += ["--coins", args.coins]
        if args.limit is not None:
            cmd += ["--limit", str(args.limit)]
        if args.resume:
            cmd += ["--resume"]
        _run(cmd)
    elif not os.path.exists(replay_path):
        raise SystemExit(f"--skip-replay, aber {replay_path} fehlt — erst Stufe 1 laufen lassen.")

    _run([py, os.path.join("tools", "retrain_from_replay.py"), "--strategy", "ats", "--replay", replay_path])
    print("\n✅ ATS2-Retrain fertig — Artefakte in staging_models (ats2_model_{LONG,SHORT}.pkl). KEIN Rollout.")


if __name__ == "__main__":
    main()

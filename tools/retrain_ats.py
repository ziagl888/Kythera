"""
tools/retrain_ats.py — ATS2 retrain (bot 12 TSI sniper) in ONE call.

DB → features → replay label → train → staging, repeatable at any time. No
CSV intermediate step (the old X8-TSI-EXPORT/-ML scripts in Documents\\_X are
superseded by this), R1-clean via core.candles (include_forming=False), feature
vector bit-identical to serving (core.ats_features, hard rule 7). Artifacts ONLY
into staging_models with model_id=ATS2 (hard rule 2/6) — NO rollout.

Is a thin orchestrator over the tested fleet tools (low priority,
CPU headroom check, reconnect logic, chronological split, pick_threshold_safe,
isotonic calibration, staging guard live there):

  Stage 1: tools/walkforward_sim.py   --strategy ats --days N   → ats_replay_Nd.jsonl
  Stage 2: tools/retrain_from_replay.py --strategy ats --replay …   → ats2_model_{LONG,SHORT}.pkl

Examples:
  python tools/retrain_ats.py                      # last 540 days
  python tools/retrain_ats.py --days 365
  python tools/retrain_ats.py --since 2025-01-01   # from date to today
  python tools/retrain_ats.py --skip-replay        # replay JSONL already exists, stage 2 only
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
            raise SystemExit(f"--since expects YYYY-MM-DD, got {args.since!r}") from e
        days = (datetime.datetime.now(datetime.timezone.utc) - since).days
        if days < 1:
            raise SystemExit(f"--since {args.since} is in the future / today — nothing to do.")
        return days
    return args.days


def _run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(cmd)}\n", flush=True)
    res = subprocess.run(cmd, cwd=REPO_ROOT)
    if res.returncode != 0:
        raise SystemExit(f"Aborted: '{' '.join(cmd[:3])} …' ended with code {res.returncode}")


def main() -> None:
    ap = argparse.ArgumentParser(description="ATS2 retrain (bot 12) in one call")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--days", type=int, default=540, help="training window in days (default 540)")
    grp.add_argument("--since", default=None, help="start date YYYY-MM-DD (until today); alternative to --days")
    ap.add_argument("--coins", default=None, help="only these coins (comma list) for the replay")
    ap.add_argument("--limit", type=int, default=None, help="only the first N coins (smoke test)")
    ap.add_argument("--skip-replay", action="store_true", help="skip stage 1 (JSONL already exists)")
    ap.add_argument("--resume", action="store_true", help="append stage-1 replay to existing JSONL")
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
        raise SystemExit(f"--skip-replay, but {replay_path} is missing — run stage 1 first.")

    _run([py, os.path.join("tools", "retrain_from_replay.py"), "--strategy", "ats", "--replay", replay_path])
    print("\n✅ ATS2 retrain done — artifacts in staging_models (ats2_model_{LONG,SHORT}.pkl). NO rollout.")


if __name__ == "__main__":
    main()

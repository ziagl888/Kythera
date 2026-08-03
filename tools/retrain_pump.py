"""
tools/retrain_pump.py — EPD2 retrain (bot 10 pump/dump) in ONE call.

Bot 10 detects on 10s ticks — bar-by-bar via core.candles is NOT
playable (live features vol_ratio/p_chg_60s/buy_pres/volat come from
the ticker buffer, not from 1h OHLCV). The DB-based retrain path thus
exists already via detector events: tools/epd2_build_dataset.py reads
``pump_dump_events`` (written by bot 10 with live gates) + ``ticker_10s``
(entry) + core.candles (R1-clean, include_forming=False, for geometry/indicators)
and writes JSONL (no CSV); tools/retrain_from_replay.py --strategy epd
trains from it → staging_models/epd2_model_{LONG,SHORT}.pkl (model_id=EPD2).

This orchestrator chains both stages into one call (symmetry to
tools/retrain_ats.py). NO rollout (hard rule 2). For provenance analysis
(why no candle-based pump trainer) see docs/MODEL_INTENT.md §7 and
audit_reports/13_x_ml_trainers.md.

  Stage 1: tools/epd2_build_dataset.py   --since DATE   → epd2_events.jsonl
  Stage 2: tools/retrain_from_replay.py  --strategy epd → <slot>_model_{LONG,SHORT}.pkl

``--model-id`` names the generated generation (hard rule 6) and thus determines
both tag AND filename prefix. A retrain on a changed feature definition
belongs under a FREE tag: EPD1/EPD2/EPD3 are taken (core/shadow_gate.py
and closed_ai_signals history), the next free is EPD4.

Examples:
  python tools/retrain_pump.py                     # from start of event history (2026-02-25)
  python tools/retrain_pump.py --since 2026-03-01
  python tools/retrain_pump.py --days 90           # last 90 days
  python tools/retrain_pump.py --skip-build        # JSONL already exists, stage 2 only
  python tools/retrain_pump.py --since 2026-07-11 --model-id EPD4   # post-P1.39 cut
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Must match tools/epd2_build_dataset.SINCE_DEFAULT (start of reliable
# pump_dump_events history).
SINCE_DEFAULT = "2026-02-25"


def _resolve_since(args: argparse.Namespace) -> str:
    if args.days is not None:
        if args.days < 1:
            raise SystemExit("--days must be >= 1.")
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
        return start.strftime("%Y-%m-%d")
    if args.since:
        try:
            datetime.datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError as e:
            raise SystemExit(f"--since expects YYYY-MM-DD, got {args.since!r}") from e
        return args.since
    return SINCE_DEFAULT


def _run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(cmd)}\n", flush=True)
    res = subprocess.run(cmd, cwd=REPO_ROOT)
    if res.returncode != 0:
        raise SystemExit(f"Abort: '{' '.join(cmd[:3])} …' ended with code {res.returncode}")


def main() -> None:
    ap = argparse.ArgumentParser(description="EPD2 retrain (bot 10) in one call")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--days", type=int, default=None, help="window in days (until today); alternative to --since")
    grp.add_argument("--since", default=None, help=f"start date YYYY-MM-DD (default {SINCE_DEFAULT})")
    ap.add_argument("--limit-symbols", type=int, default=0, help="only first N coins (smoke test)")
    ap.add_argument("--allow-pre-ticker", action="store_true", help="allow events before first ticker_10s tick")
    ap.add_argument("--skip-build", action="store_true", help="skip stage 1 (epd2_events.jsonl already exists)")
    ap.add_argument(
        "--model-id",
        default="EPD2",
        help="generation tag of produced artifacts (rule 6); sets tag + filename prefix. "
        "Default EPD2 = unchanged run; EPD1/2/3 are taken, free is EPD4.",
    )
    args = ap.parse_args()

    since = _resolve_since(args)
    slot = args.model_id.strip().upper().replace("-", "").lower()
    py = sys.executable

    if not args.skip_build:
        cmd = [py, os.path.join("tools", "epd2_build_dataset.py"), "--since", since]
        if args.limit_symbols:
            cmd += ["--limit-symbols", str(args.limit_symbols)]
        if args.allow_pre_ticker:
            cmd += ["--allow-pre-ticker"]
        _run(cmd)

    _run([py, os.path.join("tools", "retrain_from_replay.py"), "--strategy", "epd", "--model-id", args.model_id])
    print(
        f"\n✅ EPD retrain complete — artifacts in staging_models "
        f"({slot}_model_{{LONG,SHORT}}.pkl, tag {args.model_id.strip().upper()}). NO rollout."
    )


if __name__ == "__main__":
    main()

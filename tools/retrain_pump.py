"""
tools/retrain_pump.py — EPD2-Retrain (Bot 10 Pump/Dump) in EINEM Aufruf.

Bot 10 detektiert auf 10s-Ticks — bar-für-bar über core.candles ist das NICHT
nachspielbar (die Live-Features vol_ratio/p_chg_60s/buy_pres/volat kommen aus
dem Ticker-Puffer, nicht aus 1h-OHLCV). Der DB-basierte Retrain-Pfad existiert
darum bereits über die Detektor-Events: tools/epd2_build_dataset.py liest
``pump_dump_events`` (von Bot 10 mit den Live-Gates geschrieben) + ``ticker_10s``
(Entry) + core.candles (R1-clean, include_forming=False, für Geometrie/Indikatoren)
und schreibt JSONL (kein CSV); tools/retrain_from_replay.py --strategy epd
trainiert daraus → staging_models/epd2_model_{LONG,SHORT}.pkl (model_id=EPD2).

Dieser Orchestrator kettet beide Stufen zu einem Aufruf (Symmetrie zu
tools/retrain_ats.py). KEIN Rollout (harte Regel 2). Zur Provenienz-Analyse
(warum kein candle-basierter Pump-Trainer) siehe docs/MODEL_INTENT.md §7 und
audit_reports/13_x_ml_trainers.md.

  Stufe 1: tools/epd2_build_dataset.py   --since DATE   → epd2_events.jsonl
  Stufe 2: tools/retrain_from_replay.py  --strategy epd → epd2_model_{LONG,SHORT}.pkl

Beispiele:
  python tools/retrain_pump.py                     # ab Beginn der Event-Historie (2026-02-25)
  python tools/retrain_pump.py --since 2026-03-01
  python tools/retrain_pump.py --days 90           # letzte 90 Tage
  python tools/retrain_pump.py --skip-build        # JSONL existiert schon, nur Stufe 2
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Muss zu tools/epd2_build_dataset.SINCE_DEFAULT passen (Beginn belastbarer
# pump_dump_events-Historie).
SINCE_DEFAULT = "2026-02-25"


def _resolve_since(args: argparse.Namespace) -> str:
    if args.days is not None:
        if args.days < 1:
            raise SystemExit("--days muss >= 1 sein.")
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
        return start.strftime("%Y-%m-%d")
    if args.since:
        try:
            datetime.datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError as e:
            raise SystemExit(f"--since erwartet YYYY-MM-DD, bekam {args.since!r}") from e
        return args.since
    return SINCE_DEFAULT


def _run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(cmd)}\n", flush=True)
    res = subprocess.run(cmd, cwd=REPO_ROOT)
    if res.returncode != 0:
        raise SystemExit(f"Abbruch: '{' '.join(cmd[:3])} …' endete mit Code {res.returncode}")


def main() -> None:
    ap = argparse.ArgumentParser(description="EPD2-Retrain (Bot 10) in einem Aufruf")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--days", type=int, default=None, help="Fenster in Tagen (bis heute); alternativ zu --since")
    grp.add_argument("--since", default=None, help=f"Startdatum YYYY-MM-DD (Default {SINCE_DEFAULT})")
    ap.add_argument("--limit-symbols", type=int, default=0, help="nur die ersten N Coins (Smoke-Test)")
    ap.add_argument("--allow-pre-ticker", action="store_true", help="Events vor dem ersten ticker_10s-Tick zulassen")
    ap.add_argument("--skip-build", action="store_true", help="Stufe 1 überspringen (epd2_events.jsonl existiert)")
    args = ap.parse_args()

    since = _resolve_since(args)
    py = sys.executable

    if not args.skip_build:
        cmd = [py, os.path.join("tools", "epd2_build_dataset.py"), "--since", since]
        if args.limit_symbols:
            cmd += ["--limit-symbols", str(args.limit_symbols)]
        if args.allow_pre_ticker:
            cmd += ["--allow-pre-ticker"]
        _run(cmd)

    _run([py, os.path.join("tools", "retrain_from_replay.py"), "--strategy", "epd"])
    print("\n✅ EPD2-Retrain fertig — Artefakte in staging_models (epd2_model_{LONG,SHORT}.pkl). KEIN Rollout.")


if __name__ == "__main__":
    main()

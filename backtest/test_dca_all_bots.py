# backtest/test_dca_all_bots.py — DB-free pins for the fleet-wide DCA aggregator's
# pure ranking/verdict logic (T-2026-KYT-9050-045). No DB, no network. Run:
#   python backtest/test_dca_all_bots.py
#
# tools.dca_all_bots imports get_db_connection INSIDE main(), so render_md/_arm are
# importable without a DB. Pins: DCA-drag = B_sharpe - A_sharpe, the per-bot
# verdict thresholds (DCA-HURTS / neutral / THIN), and the fleet-count line.

from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.dca_all_bots import _arm, render_md  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(f"{name}: {detail}")


def arm(sh, dd):
    return {"n": 0, "sharpe_lev": sh, "maxdd_2pct": dd, "unlev_mean": 0.0, "lev_sum": 0.0}


print("dca_all_bots render/verdict")

meta = {
    "generated_at": "2026-07-26T00:00:00+00:00",
    "end": "2026-07-27 00:00:00",
    "bots": [
        # DCA hurts clearly (drag +0.10), solid n
        {
            "model": "AIM2",
            "start": "2026-07-11",
            "n": 600,
            "A": arm(0.18, 16.4),
            "B": arm(0.28, 15.8),
            "C": arm(0.23, 10.7),
        },
        # neutral (drag 0.00)
        {
            "model": "MAX1",
            "start": "2026-07-16",
            "n": 129,
            "A": arm(0.30, 5.0),
            "B": arm(0.30, 5.1),
            "C": arm(0.27, 4.7),
        },
        # thin (n<30) — no verdict trusted
        {
            "model": "BR1D",
            "start": "2026-07-20",
            "n": 12,
            "A": arm(0.10, 8.0),
            "B": arm(0.40, 7.0),
            "C": arm(0.35, 6.0),
        },
        # C beats B (bot-type dependent momentum win, drag +0.10, C-B +0.06)
        {
            "model": "MIS1-168H",
            "start": "2026-05-14",
            "n": 342,
            "A": arm(0.06, 24.4),
            "B": arm(0.16, 20.2),
            "C": arm(0.22, 12.9),
        },
    ],
}
md = render_md(meta)

check("AIM2 row: DCA-Drag +0.1", "**+0.1**" in md or "+0.1 " in md, "drag not rendered")
check("AIM2 verdict DCA-HURTS", "AIM2" in md and "DCA-HURTS" in md)
check("MAX1 neutral", "| MAX1 |" in md and "neutral" in md.split("| MAX1 |")[1].split("\n")[0])
check("BR1D THIN (n<30)", "| BR1D |" in md and "THIN" in md.split("| BR1D |")[1].split("\n")[0])
check("MIS1 C beats B (C-B +0.06)", "+0.06" in md)
# fleet verdict: 3 solid bots (AIM2, MAX1, MIS1); DCA hurts on AIM2+MIS1 = 2/3
check("fleet: DCA hurts on 2/3", "2/3" in md, md.split("Fleet verdict")[-1][:200])
# C beats B on 1/3 solid (MIS1 only)
check("fleet: C schlaegt B 1/3", "1/3" in md)

# _arm None-safety
check("_arm None -> {}", _arm({}, "X") == {} and _arm(None, "Y") == {})

print()
if FAILS:
    print(f"FAILED {len(FAILS)}:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all dca_all_bots pins passed")

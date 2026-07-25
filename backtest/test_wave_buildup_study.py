# backtest/test_wave_buildup_study.py — DB-free pins for the wave-buildup study
# pure helpers (T-2026-KYT-9050-041, Phase A). No DB, no network. Run:
#   python backtest/test_wave_buildup_study.py
#
# Pins the causal trailing-close capture, the signed-move guard, the per-trade
# Sharpe, and the fixed-fraction compounding + MaxDD that carry the Phase-A
# conclusion (risk-adjusted trailing beats hold). The DB reconstruction around
# these is exercised only by the read-only tool run itself.

from __future__ import annotations

import math
import os
import sys

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.wave_buildup_study import (  # noqa: E402
    compound_equity,
    sharpe,
    signed_move_pct,
    trail_capture,
)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(f"{name}: {detail}")


def approx(a, b, tol=1e-6) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol


# ── signed_move_pct ──────────────────────────────────────────────────────────
print("signed_move_pct")
check("LONG +10%", approx(signed_move_pct("LONG", 100, 110), 10.0))
check("SHORT +10% (price down)", approx(signed_move_pct("SHORT", 100, 90), 10.0))
check("SHORT -10% (price up)", approx(signed_move_pct("SHORT", 100, 110), -10.0))
check("bad direction -> None", signed_move_pct("FLAT", 100, 110) is None)
check("nonpos price -> None", signed_move_pct("LONG", 0, 110) is None)
check("data-bug >100% -> None", signed_move_pct("LONG", 100, 300) is None)

# ── trail_capture ────────────────────────────────────────────────────────────
print("trail_capture")
# LONG peaks at +30 then the adverse extreme retraces to 15 == 30*(1-0.5) -> trigger
fav = np.array([10.0, 30.0, 25.0])
adv = np.array([8.0, 20.0, 15.0])
ru, rl = trail_capture(fav, adv, 0.5, 20.0, hold_ru=-3.0, hold_rl=-60.0)
check("trigger closes at peak*(1-x)", approx(ru, 15.0) and approx(rl, 300.0), f"got {ru},{rl}")
# never retraces enough -> hold to recorded close
fav2 = np.array([10.0, 20.0])
adv2 = np.array([8.0, 18.0])
ru2, rl2 = trail_capture(fav2, adv2, 0.5, 20.0, hold_ru=1.7, hold_rl=34.0)
check("no trigger -> hold", approx(ru2, 1.7) and approx(rl2, 34.0), f"got {ru2},{rl2}")
# never in profit -> peak stays <=0 -> never arms -> hold
fav3 = np.array([-5.0, -10.0])
adv3 = np.array([-8.0, -15.0])
ru3, rl3 = trail_capture(fav3, adv3, 0.2, 20.0, hold_ru=-9.0, hold_rl=-100.0)
check("underwater never arms -> hold", approx(ru3, -9.0) and approx(rl3, -100.0), f"got {ru3},{rl3}")
# leveraged capture is -100% clamped
ru4, rl4 = trail_capture(np.array([2.0, 3.0]), np.array([1.0, -9.0]), 0.5, 20.0, hold_ru=0.0, hold_rl=0.0)
check("lev capture clamped at -100", rl4 >= -100.0)

# ── sharpe ───────────────────────────────────────────────────────────────────
print("sharpe")
check("zero variance -> nan", math.isnan(sharpe(np.array([1.0, 1.0, 1.0]))))
check("mean/std", approx(sharpe(np.array([1.0, 2.0, 3.0])), 2.0, tol=1e-9))

# ── compound_equity ──────────────────────────────────────────────────────────
print("compound_equity")
fm, md = compound_equity(np.array([1.0, -0.5]), f=1.0)  # x2 then x0.5 -> 1.0, peak 2 -> 50% dd
check("final multiple", approx(fm, 1.0), f"got {fm}")
check("maxdd 50%", approx(md, 50.0, tol=1e-9), f"got {md}")
fm2, md2 = compound_equity(np.array([-1.0]), f=1.0)  # full wipe
check("wipeout -> 0 / 100% dd", approx(fm2, 0.0) and approx(md2, 100.0))
fm3, md3 = compound_equity(np.array([0.1, 0.1]), f=0.5)  # monotone up -> no dd
check("monotone up -> 0 dd", approx(md3, 0.0))

print()
if FAILS:
    print(f"FAILED {len(FAILS)}:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all wave-buildup pure-helper pins passed")

# backtest/test_trailing_risk.py — DB-free pins for the trailing-close finalise
# report logic (T-2026-KYT-9050-046). No DB, no network. Run:
#   python backtest/test_trailing_risk.py
#
# tools.wave_exit_overlay imports get_db_connection at module load but never
# connects on import, so render_trailing_md is importable + testable without a DB.
# Pins the best-X / beats-hold verdict and the MaxDD-ratio line; the underlying
# per-trade Sharpe + compounding are pinned in test_wave_buildup_study.py.

from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.wave_exit_overlay import X_SWEEP, render_trailing_md  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(f"{name}: {detail}")


def var(sh, dd, n=200):
    return {"n": n, "mean_lev": 10.0, "sharpe_lev": sh, "maxdd_2pct": dd}


def summary(hold_sh, hold_dd, trail):
    """trail = dict {x_int: (sharpe, maxdd)} for each X_SWEEP entry."""
    variants = {"hold": var(hold_sh, hold_dd)}
    for x in X_SWEEP:
        sh, dd = trail[int(x * 100)]
        variants[f"{int(x * 100)}"] = var(sh, dd)
    return {
        "model": "AIM2",
        "generated_at": "2026-07-26T00:00:00+00:00",
        "window": ["2026-07-11 00:00:00", "2026-07-27 00:00:00"],
        "trailing": {"n_arts": 200, "variants": variants},
    }


print("trailing_risk render/verdict")

# Case 1: trailing 10% clearly wins (Sharpe up, MaxDD down) — like the real AIM2 result
s1 = summary(
    0.192,
    9.7,
    {10: (0.438, 2.0), 15: (0.43, 2.0), 20: (0.40, 2.0), 25: (0.398, 2.0), 30: (0.379, 2.0), 40: (0.363, 2.0)},
)
md1 = render_trailing_md(s1)
check("hold row rendered", "| **hold** |" in md1)
check("trailing 10% row", "| Trailing 10% |" in md1)
check("best-X = 10", "Best trailing-X = 10%" in md1)
check("beats hold -> confirmed", "confirmed" in md1 and "raises the risk-adjusted" in md1)
check("MaxDD ratio ~4.8x", "4.8× smaller" in md1, md1)

# Case 2: trailing never beats hold (hold Sharpe highest) — not confirmed
s2 = summary(
    0.60, 5.0, {10: (0.30, 6.0), 15: (0.28, 6.0), 20: (0.25, 6.0), 25: (0.22, 6.0), 30: (0.20, 6.0), 40: (0.18, 6.0)}
)
md2 = render_trailing_md(s2)
check("no Sharpe gain", "no Sharpe gain" in md2)
check("not confirmed", "weakened/not confirmed" in md2)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("all trailing_risk pins passed")

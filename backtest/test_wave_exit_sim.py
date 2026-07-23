# backtest/test_wave_exit_sim.py — DB-free pins for the wave-exit replay engine
# (T-2026-KYT-9050-035). Synthetic wick-aware candles; no DB, no network. Run:
#   python backtest/test_wave_exit_sim.py
#
# Pins the fidelity contract of core.wave_exit_sim against 8_ai_trade_monitor:
# entry fill (market + DCA limit on wick touch), SL-first, laddered TPs with
# trailing SL, the injected 10s order-resolver for within-candle ambiguity,
# all-targets / open-at-end close, and mark-to-market consistency. The %-math is
# reconciled through core.realized_pnl so engine and shared builder never drift.

from __future__ import annotations

import os
import sys

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.realized_pnl import weighted_move_pct  # noqa: E402
from core.wave_exit_sim import (  # noqa: E402
    FIRST_TP,
    mark_to_market_series,
    simulate_signal,
    trailing_tp_trigger,
)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(f"{name}: {detail}")


def approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(float(a) - float(b)) <= tol


def hlc(rows):
    """rows = [(high, low, close), ...] → (highs, lows, closes) arrays."""
    a = np.array(rows, dtype=float)
    return a[:, 0], a[:, 1], a[:, 2]


# ─────────────────────────────────────────────────────────────────────────────
# 1. LONG ladder through all targets
# ─────────────────────────────────────────────────────────────────────────────
def test_long_all_targets() -> None:
    print("test_long_all_targets")
    entry, sl = 100.0, 98.0
    targets = [101.0, 102.0, 103.0]
    highs, lows, closes = hlc(
        [
            (100.2, 99.8, 100.0),
            (101.2, 100.0, 101.1),
            (102.2, 101.0, 102.1),
            (103.3, 102.5, 103.2),
        ]
    )
    r = simulate_signal(highs, lows, closes, "LONG", [(entry, 1.0)], sl, targets)
    leg = r["legs"][0]
    check("all 3 targets", leg["targets_hit"] == 3, str(leg["targets_hit"]))
    check("all_targets", leg["exit_reason"] == "all_targets", leg["exit_reason"])
    check("close at last target", approx(leg["close_price"], 103.0))
    mv = weighted_move_pct("LONG", entry, leg["close_price"], targets, leg["targets_hit"])
    check("weighted move 2.0%", approx(mv, 2.0, 1e-6), str(mv))


# ─────────────────────────────────────────────────────────────────────────────
# 2. LONG SL-first (wick dips to SL before any target)
# ─────────────────────────────────────────────────────────────────────────────
def test_long_sl_first() -> None:
    print("test_long_sl_first")
    entry, sl = 100.0, 98.0
    targets = [101.0, 102.0, 103.0]
    highs, lows, closes = hlc([(100.1, 99.5, 99.8), (100.5, 97.9, 98.5), (101.6, 100.0, 101.5)])
    r = simulate_signal(highs, lows, closes, "LONG", [(entry, 1.0)], sl, targets)
    leg = r["legs"][0]
    check("0 targets", leg["targets_hit"] == 0, str(leg["targets_hit"]))
    check("sl_after_tp0", leg["exit_reason"] == "sl_after_tp0", leg["exit_reason"])
    check("close at SL", approx(leg["close_price"], 98.0))


# ─────────────────────────────────────────────────────────────────────────────
# 3. LONG trailing SL: TP1+TP2, then fall to the trailed SL = targets[0]
# ─────────────────────────────────────────────────────────────────────────────
def test_long_trailing_stop() -> None:
    print("test_long_trailing_stop")
    entry, sl = 100.0, 98.0
    targets = [101.0, 102.0, 103.0]
    highs, lows, closes = hlc(
        [
            (100.2, 99.9, 100.0),
            (102.1, 100.5, 101.8),
            (101.9, 100.9, 101.2),  # TP1+TP2 then dip to 101 → trailed SL
        ]
    )
    r = simulate_signal(highs, lows, closes, "LONG", [(entry, 1.0)], sl, targets)
    leg = r["legs"][0]
    check("2 targets", leg["targets_hit"] == 2, str(leg["targets_hit"]))
    check("stopped after tp2", leg["exit_reason"] == "sl_after_tp2", leg["exit_reason"])
    check("trailed SL 101", approx(leg["trailed_sl"], 101.0))
    mv = weighted_move_pct("LONG", entry, leg["close_price"], targets, leg["targets_hit"])
    check("weighted move 1.333%", approx(mv, 4.0 / 3.0, 1e-6), str(mv))


# ─────────────────────────────────────────────────────────────────────────────
# 4. SHORT ladder mirror
# ─────────────────────────────────────────────────────────────────────────────
def test_short_all_targets() -> None:
    print("test_short_all_targets")
    entry, sl = 100.0, 102.0
    targets = [99.0, 98.0, 97.0]
    highs, lows, closes = hlc([(100.1, 99.9, 100.0), (100.0, 98.9, 99.0), (99.0, 96.9, 97.0)])
    r = simulate_signal(highs, lows, closes, "SHORT", [(entry, 1.0)], sl, targets)
    leg = r["legs"][0]
    check("3 targets", leg["targets_hit"] == 3, str(leg["targets_hit"]))
    check("all_targets", leg["exit_reason"] == "all_targets", leg["exit_reason"])


# ─────────────────────────────────────────────────────────────────────────────
# 5. Within-candle ambiguity resolved by the injected 10s order-resolver
#    Candle 1 touches BOTH SL (98) and TP1 (101). Default → SL-first (loss).
#    Resolver saying TP-first → the ladder proceeds instead.
# ─────────────────────────────────────────────────────────────────────────────
def test_order_resolver() -> None:
    print("test_order_resolver")
    entry, sl = 100.0, 98.0
    targets = [101.0, 102.0, 103.0]
    highs, lows, closes = hlc([(100.1, 99.9, 100.0), (101.5, 97.8, 100.5), (103.2, 102.0, 103.1)])
    # default (SL-first)
    r0 = simulate_signal(highs, lows, closes, "LONG", [(entry, 1.0)], sl, targets)
    check(
        "default SL-first → 0 tgts loss",
        r0["legs"][0]["targets_hit"] == 0 and r0["legs"][0]["exit_reason"] == "sl_after_tp0",
        r0["legs"][0]["exit_reason"],
    )

    # resolver forcing TP-first on the ambiguous candle
    def resolver(idx, is_long, sl_level, tp_level):
        return FIRST_TP

    r1 = simulate_signal(highs, lows, closes, "LONG", [(entry, 1.0)], sl, targets, order_resolver=resolver)
    check("resolver TP-first → ladder proceeds", r1["legs"][0]["targets_hit"] == 3, str(r1["legs"][0]["targets_hit"]))


# ─────────────────────────────────────────────────────────────────────────────
# 6. DCA: entry2 (below CMP for LONG) fills on wick touch; both legs ladder
# ─────────────────────────────────────────────────────────────────────────────
def test_dca_entry2_fills() -> None:
    print("test_dca_entry2_fills")
    e1, e2, sl = 100.0, 99.0, 97.0
    targets = [101.0, 102.0, 103.0]
    highs, lows, closes = hlc(
        [
            (100.1, 99.9, 100.0),
            (100.2, 98.9, 99.2),
            (101.2, 99.5, 101.1),
            (103.3, 102.0, 103.2),
        ]
    )
    r = simulate_signal(highs, lows, closes, "LONG", [(e1, 0.5), (e2, 0.5)], sl, targets)
    l1, l2 = r["legs"]
    check("leg1 market @0", l1["filled"] and l1["fill_idx"] == 0)
    check("leg2 fills on touch @1", l2["filled"] and l2["fill_idx"] == 1, str(l2["fill_idx"]))
    check("both complete ladder", l1["targets_hit"] == 3 and l2["targets_hit"] == 3)
    mv1 = weighted_move_pct("LONG", e1, l1["close_price"], targets, l1["targets_hit"])
    mv2 = weighted_move_pct("LONG", e2, l2["close_price"], targets, l2["targets_hit"])
    check("entry2 leg richer (better entry)", mv2 > mv1, f"{mv2:.3f} vs {mv1:.3f}")


# ─────────────────────────────────────────────────────────────────────────────
# 7. DCA: entry2 never touched → unfilled, zero weight
# ─────────────────────────────────────────────────────────────────────────────
def test_dca_entry2_unfilled() -> None:
    print("test_dca_entry2_unfilled")
    e1, e2, sl = 100.0, 99.0, 97.0
    targets = [101.0, 102.0, 103.0]
    highs, lows, closes = hlc([(100.2, 99.8, 100.0), (101.2, 100.1, 101.1), (103.3, 102.0, 103.2)])
    r = simulate_signal(highs, lows, closes, "LONG", [(e1, 0.5), (e2, 0.5)], sl, targets)
    l1, l2 = r["legs"]
    check("leg1 filled", l1["filled"])
    check("leg2 unfilled", not l2["filled"] and l2["exit_reason"] == "entry_not_filled")


# ─────────────────────────────────────────────────────────────────────────────
# 8. Open at end → mark-to-market at last close
# ─────────────────────────────────────────────────────────────────────────────
def test_open_at_end() -> None:
    print("test_open_at_end")
    entry, sl = 100.0, 98.0
    targets = [101.0, 102.0, 103.0]
    highs, lows, closes = hlc([(100.2, 99.9, 100.0), (101.2, 100.0, 100.9), (100.9, 100.2, 100.8)])
    r = simulate_signal(highs, lows, closes, "LONG", [(entry, 1.0)], sl, targets)
    leg = r["legs"][0]
    check("1 target", leg["targets_hit"] == 1, str(leg["targets_hit"]))
    check("open_at_end", leg["exit_reason"] == "open_at_end", leg["exit_reason"])
    check("close = last close", approx(leg["close_price"], 100.8))


# ─────────────────────────────────────────────────────────────────────────────
# 9. Mark-to-market consistency: last mark == realised leg value
# ─────────────────────────────────────────────────────────────────────────────
def test_mtm_matches_realised() -> None:
    print("test_mtm_matches_realised")
    entry, sl = 100.0, 98.0
    targets = [101.0, 102.0, 103.0]
    highs, lows, closes = hlc(
        [
            (100.2, 99.9, 100.0),
            (102.1, 100.5, 101.8),
            (101.9, 100.9, 101.2),
        ]
    )
    r = simulate_signal(highs, lows, closes, "LONG", [(entry, 1.0)], sl, targets)
    leg = r["legs"][0]
    mtm = mark_to_market_series(closes, "LONG", [(entry, 1.0)], sl, targets, highs=highs, lows=lows)
    realised_frac = weighted_move_pct("LONG", entry, leg["close_price"], targets, leg["targets_hit"]) / 100.0
    check("MTM last == realised", approx(mtm[-1], realised_frac, 1e-9), f"{mtm[-1]:.6f} vs {realised_frac:.6f}")
    check("MTM peak >= final", mtm.max() >= mtm[-1] - 1e-12, f"peak {mtm.max():.6f} final {mtm[-1]:.6f}")


# ─────────────────────────────────────────────────────────────────────────────
# 10. Overlay (a) trailing-TP trigger: fires on X% retrace from an in-profit peak
# ─────────────────────────────────────────────────────────────────────────────
def test_trailing_tp_trigger() -> None:
    print("test_trailing_tp_trigger")
    # wave climbs to +0.10 then gives back; X=30% → threshold 0.10*0.7=0.07
    mtm = np.array([0.0, 0.04, 0.10, 0.08, 0.065, 0.05])
    check("fires at first step <= 0.07", trailing_tp_trigger(mtm, 0.30) == 4, str(trailing_tp_trigger(mtm, 0.30)))
    # underwater → never fires (activation guard keeps a loser from early close)
    check("underwater never fires", trailing_tp_trigger(np.array([0.0, -0.02, -0.05, -0.03]), 0.30) is None)
    # monotonic up → never fires
    check("monotonic up never fires", trailing_tp_trigger(np.array([0.0, 0.02, 0.05, 0.09]), 0.30) is None)


def main() -> int:
    for t in [
        test_long_all_targets,
        test_long_sl_first,
        test_long_trailing_stop,
        test_short_all_targets,
        test_order_resolver,
        test_dca_entry2_fills,
        test_dca_entry2_unfilled,
        test_open_at_end,
        test_mtm_matches_realised,
        test_trailing_tp_trigger,
    ]:
        t()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}:")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

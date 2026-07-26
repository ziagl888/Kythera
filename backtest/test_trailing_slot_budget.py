# backtest/test_trailing_slot_budget.py — T-2026-KYT-9050-042 (Phase C) pins.
#
# The slot-budget tool re-implements the T-041 trailing capture because it needs the
# trigger INDEX (the exit time — that is what frees a Cornix seat), which
# `wave_buildup_study.trail_capture` discards. Two measurement traps cost a full run
# each before the numbers meant anything, and both are pinned here:
#
#   1. Same-candle trigger — the inherited rule arms and breaches the trail on one
#      candle, so trades exited at hour 0 and every leg looked free (median trailing
#      hold 0h across ALL 43 legs, densities of 961 %/slot-day).
#   2. A scale-free trail is a micro-scalper — "give back 10 % of the peak" fires on a
#      +0.5 % peak, so 15m noise closes almost everything inside one candle. The
#      activation floor is the fix, and it is the parameter the report sweeps.
#
# Runs without a DB:  python backtest/test_trailing_slot_budget.py

import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import unittest.mock as mock  # noqa: E402

from tools import trailing_slot_budget as tsb  # noqa: E402
from tools.trailing_slot_budget import (  # noqa: E402
    capture_at,
    fill_channel,
    leg_stats,
    occupancy,
    simulate,
    slot_index,
    trail_exit,
)

LEV = 20.0
BASE = datetime(2026, 3, 1, tzinfo=timezone.utc)
GRID0 = np.datetime64(BASE.replace(tzinfo=None), "h")


def _original_trigger_index(fav, adv, x):
    """Reference: where wave_buildup_study.trail_capture would fire — it updates the
    peak and tests the retrace on the SAME candle."""
    peak = -1e9
    for k in range(len(fav)):
        if fav[k] > peak:
            peak = fav[k]
        if peak > 0 and adv[k] <= peak * (1.0 - x):
            return k
    return None


def test_never_exits_on_the_trades_own_first_candle():
    """Trap 1. A candle straddling the entry arms AND breaches the trail under the
    inherited rule, so the trade exits at hour 0 — which made every leg look like it
    costs no Cornix seat. Inside one candle the order of high and low is unknowable."""
    fav = np.array([10.0, 10.0])
    adv = np.array([-5.0, 0.0])
    assert _original_trigger_index(fav, adv, 0.10) == 0  # the inherited rule exits immediately
    assert trail_exit(fav, adv, 0.10) != 0  # ours cannot
    assert trail_exit(np.array([10.0]), np.array([-5.0]), 0.10) is None  # single-candle trade


def test_strict_peak_never_fires_before_the_original_would():
    """The conservative rule may only ever delay an exit, never pull it earlier — and
    when it fires, the fill is the armed peak's level."""
    rng = np.random.default_rng(4242)
    for i in range(300):
        n = int(rng.integers(1, 60))
        fav = rng.normal(loc=rng.uniform(-4, 8), scale=4, size=n)
        adv = fav - np.abs(rng.normal(scale=3, size=n))  # adverse extreme is never above fav
        for x in (0.10, 0.25, 0.50):
            k = trail_exit(fav, adv, x)
            k_orig = _original_trigger_index(fav, adv, x)
            if k is not None:
                assert k_orig is not None and k >= k_orig, f"case {i} x={x}: {k} < {k_orig}"
                armed = np.maximum.accumulate(fav)[k - 1]
                assert np.isclose(capture_at(fav, x, k, LEV, 0.0, 0.0)[0], armed * (1 - x))


def test_activation_floor_suppresses_micro_exits():
    """Trap 2. A trade that peaked at +0.5 % and gave back 10 % of that (0.05 %) is a
    micro-scalp, not a trade — below the 0.10 % round-trip fee it is a net loser. The
    activation floor must keep the trail disarmed until the peak is worth trailing."""
    fav = np.array([0.0, 0.5, 0.5, 6.0, 6.0])
    adv = np.array([0.0, 0.4, 0.44, 1.0, 5.0])
    assert trail_exit(fav, adv, 0.10, activation=0.0) == 2  # fires on the 0.5 % peak
    late = trail_exit(fav, adv, 0.10, activation=2.0)
    assert late == 4, late  # waits for the 6 % peak
    assert capture_at(fav, 0.10, late, LEV, 0.0, 0.0)[0] == 6.0 * 0.9


def test_underwater_trade_never_trails_out():
    """A trade that is never in profit must be held to its recorded close, otherwise
    trailing would invent exits on losers."""
    fav = np.array([-1.0, -3.0, -2.0, -8.0])
    adv = np.array([-2.0, -5.0, -4.0, -12.0])
    assert trail_exit(fav, adv, 0.10) is None
    assert capture_at(fav, 0.10, None, LEV, hold_ru=-8.0, hold_rl=-160.0) == (-8.0, -160.0)


def test_wider_give_back_exits_later():
    fav = np.array([0.0, 10.0, 10.0, 10.0])
    adv = np.array([0.0, 9.5, 8.9, 5.0])
    assert trail_exit(fav, adv, 0.10) == 2  # 9.5 > 9.0 holds, 8.9 <= 9.0 fires
    assert trail_exit(fav, adv, 0.50) == 3  # 5.0 <= 10 * 0.5 only on the last candle


def _trade(ot_h: int, ct_h: int, trail_h: int, ru: float = 1.0, act: float = 0.0):
    return {
        "ot": BASE + timedelta(hours=ot_h),
        "ct": BASE + timedelta(hours=ct_h),
        "real_unlev": ru,
        "trail": {act: (ru, ru * LEV, (BASE + timedelta(hours=trail_h)).replace(tzinfo=None))},
    }


def test_occupancy_counts_simultaneous_positions():
    trades = [_trade(0, 10, 10), _trade(2, 4, 4), _trade(2, 3, 3)]
    s, e = slot_index(trades, GRID0, 12, None)
    occ = occupancy(s, e, 12)
    assert occ[0] == 1  # only the long one
    assert occ[2] == 3  # all three overlap
    assert occ[5] == 1  # the short ones are gone
    assert occ[11] == 0


def test_trailing_shortens_occupancy():
    """The whole point of the exercise: an earlier exit frees seats."""
    trades = [_trade(0, 40, 5)]
    s_h, e_h = slot_index(trades, GRID0, 48, None)
    s_t, e_t = slot_index(trades, GRID0, 48, 0.0)
    assert occupancy(s_t, e_t, 48).sum() < occupancy(s_h, e_h, 48).sum()


def test_sub_hour_trade_still_occupies_a_seat():
    """A trade opened and closed inside one hour still held a Cornix slot."""
    s, e = slot_index([_trade(3, 3, 3)], GRID0, 6, 0.0)
    assert occupancy(s, e, 6)[3] == 1


def _leg(trades, density, thin=False, act=0.0, glen=100):
    s, e = slot_index(trades, GRID0, glen, act)
    return {
        "density_trail": density,
        "thin": thin,
        "_idx": (s, e),
        "_ru": np.array([t["real_unlev"] for t in trades]),
        "_n": len(trades),
    }


def test_fill_channel_respects_the_cap_and_prefers_density():
    """Greedy fill: the dense leg gets the seat, the bloated one is rejected, and the
    accepted set's own joint occupancy stays under the budget."""
    dense = [_trade(i, i + 1, i + 1, ru=5.0) for i in range(40)]
    bloated = [_trade(i, i + 90, i + 90, ru=0.1) for i in range(40)]
    stats = {"DENSE LONG": _leg(dense, 5.0), "BLOATED LONG": _leg(bloated, 0.01)}
    res = fill_channel(stats, 100, budget=5, measure="mean", fee=0.10)
    assert res["accepted"] == ["DENSE LONG"], res["accepted"]
    assert [r["leg"] for r in res["rejected"]] == ["BLOATED LONG"]
    assert res["joint_mean"] <= 5


def test_fill_channel_reports_value_net_of_fees():
    """The seat is only worth taking if it survives the round-trip fee."""
    trades = [_trade(i, i + 1, i + 1, ru=1.0) for i in range(10)]
    res = fill_channel({"L LONG": _leg(trades, 1.0)}, 100, budget=500, measure="mean", fee=0.10)
    assert np.isclose(res["value_trail_net"], 10 * 1.0 - 10 * 0.10)


def test_fill_channel_skips_thin_and_negative_legs():
    stats = {
        "THIN LONG": _leg([_trade(0, 1, 1)], 9.0, thin=True),
        "LOSER SHORT": _leg([_trade(0, 1, 1, ru=-1.0)], -0.5),
    }
    res = fill_channel(stats, 10, budget=500, measure="mean", fee=0.10)
    assert res["accepted"] == []


# ─────────────────────────────────────────────────────────────────────────────
# leg_stats + simulate — the two composing functions (added after the PR #198
# core review found them unpinned; they carry the grid-boundary and the candle-
# window logic, i.e. exactly where an off-by-one would be invisible).
# ─────────────────────────────────────────────────────────────────────────────


def _stat_trade(tag, direction, ot_h, ct_h, ru, act=0.0):
    ot = BASE + timedelta(hours=ot_h)
    ct = BASE + timedelta(hours=ct_h)
    return {
        "sym": "BTCUSDT",
        "tag": tag,
        "dir": direction,
        "ot": ot,
        "ct": ct,
        "real_unlev": ru,
        "real_lev": ru * LEV,
        "trail": {act: (ru, ru * LEV, ct.replace(tzinfo=None))},
    }


def test_leg_stats_scores_live_legs_on_a_grid_spanning_all_trades():
    """Grid bounds come from ALL trades, evaluation only from LIVE legs.

    Both halves matter: scoring a shadow leg would put a leg that never reaches
    Cornix into a Cornix budget, while cutting the GRID to the live trades would
    move every leg's occupancy denominator as soon as the register changes."""
    live = _stat_trade("MIS1-72h", "LONG", 0, 10, 5.0)
    shadow = _stat_trade("SHADOWTAG", "LONG", 0, 400, 99.0)  # much later close

    # Patch the real register's function, not sys.modules: leg_stats does
    # `from core import shadow_gate`, which reads the PACKAGE attribute — a
    # sys.modules patch only lands if nothing imported it earlier in the run.
    from core import shadow_gate as sg

    def fake_status(tag, direction):
        return sg.SHADOW if tag == "SHADOWTAG" else sg.LIVE

    with mock.patch.object(sg, "leg_status", fake_status):
        out, _grid0, glen = leg_stats([live, shadow], live_only=True, act=0.0, fee=0.10)
        assert set(out) == {"MIS1-72h LONG"}, out  # shadow leg not scored
        assert glen > 400, glen  # …but the grid still spans its 400h close
        assert out["MIS1-72h LONG"]["n"] == 1
        assert abs(out["MIS1-72h LONG"]["value_trail_net"] - (5.0 - 0.10)) < 1e-9

        out_all, _, _ = leg_stats([live, shadow], live_only=False, act=0.0, fee=0.10)
    assert set(out_all) == {"MIS1-72h LONG", "SHADOWTAG LONG"}


def test_simulate_uses_only_candles_inside_the_trade():
    """The candle window is `open_time` between entry and recorded close.

    Pins BOTH edges, because they are asymmetric on purpose (documented in the
    report's honest limits): the candle straddling the ENTRY is excluded, so no
    pre-entry peak can arm the trail — while the last candle may open at the close
    and therefore still reach past it."""
    seen = {}

    def fake_wick(conn, sym, lo, hi, tf):
        t = np.array(
            [np.datetime64(BASE.replace(tzinfo=None) + timedelta(hours=h)) for h in (-1, 0, 1, 2, 3, 4)],
            dtype="datetime64[ns]",
        )
        return {"t": t, "h": np.full(6, 101.0), "l": np.full(6, 99.0), "c": np.full(6, 100.0)}

    trade = {
        "sym": "BTCUSDT",
        "tag": "X",
        "dir": "LONG",
        "entry": 100.0,
        "ot": BASE + timedelta(hours=1),
        "ct": BASE + timedelta(hours=3),
        "real_unlev": 0.0,
        "real_lev": 0.0,
    }

    def spy_trail_exit(fav, adv, x, activation=0.0):
        seen["n"] = len(fav)
        return None

    with mock.patch.object(tsb, "read_coin_wick", fake_wick), mock.patch.object(tsb, "trail_exit", spy_trail_exit):
        simulate(None, [trade], LEV, "1h", 0.10, [0.0])

    # Candles at h=1,2,3 — the h=-1 and h=0 candles (before the entry) are out,
    # and h=4 (after the recorded close) is out.
    assert seen["n"] == 3, seen
    # No trigger → the trade keeps its recorded close (stored naive, like all times here).
    assert trade["trail"][0.0][2] == trade["ct"].replace(tzinfo=None)


def test_trailing_never_outlives_the_recorded_close():
    """Invariant from the module docstring: trailing can only pull an exit EARLIER."""

    def fake_wick(conn, sym, lo, hi, tf):
        t = np.array(
            [np.datetime64(BASE.replace(tzinfo=None) + timedelta(hours=h)) for h in (0, 1, 2)],
            dtype="datetime64[ns]",
        )
        return {
            "t": t,
            "h": np.array([100.0, 120.0, 120.0]),
            "l": np.array([100.0, 119.0, 100.0]),
            "c": t.astype(float),
        }

    trade = {
        "sym": "BTCUSDT",
        "tag": "X",
        "dir": "LONG",
        "entry": 100.0,
        "ot": BASE,
        "ct": BASE + timedelta(hours=2),
        "real_unlev": 0.0,
        "real_lev": 0.0,
    }
    with mock.patch.object(tsb, "read_coin_wick", fake_wick):
        simulate(None, [trade], LEV, "1h", 0.10, [0.0])
    assert trade["trail"][0.0][2] <= trade["ct"].replace(tzinfo=None)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

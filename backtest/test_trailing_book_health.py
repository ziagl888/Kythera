# backtest/test_trailing_book_health.py — T-2026-KYT-9050-052 pins.
#
# The book-health tool exists because realised sums hide a rotting open book
# (Bot 40's live lesson: the trail closes only winners, losers stay). These pins
# hold the rule semantics and the aggregation honest:
#
#   1. The time-stop may only ever touch trades the trail structurally cannot
#      see (never armed) — an armed trade belongs to the trail.
#   2. The equity curve must close the loop: final equity == net realised sum
#      (every open mark eventually converts into a realised value).
#   3. The partial rule charges exactly ONE round-trip fee per trade, split
#      across its two half-exits.
#   4. The exposure cap is an admission rule: it must refuse the entry that
#      would stretch the imbalance past the cap, and admit the other side.
#
# Runs without a DB:  python backtest/test_trailing_book_health.py

import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.trailing_book_health import (  # noqa: E402
    ACT_LIVE,
    FEE_RT,
    act_tp1,
    exit_breakeven,
    exit_trail,
    exit_trail_hardstop,
    exit_trail_timestop,
    impute_tp1,
    run_direction_gate,
    run_exposure_cap,
    run_feedback_gate,
    run_partial,
    run_rule,
)

BASE = datetime(2026, 3, 1, tzinfo=timezone.utc)
GRID0 = np.datetime64(BASE.replace(tzinfo=None), "h")


def _trade(
    ot_h: float,
    ct_h: float,
    fav: list[float],
    adv: list[float],
    cm: list[float] | None = None,
    direction: str = "LONG",
    real: float = 0.0,
    candle_h: float = 1.0,
) -> dict:
    """Synthetic trade with hourly candles starting at ot_h."""
    n = len(fav)
    tt = np.array(
        [np.datetime64(BASE.replace(tzinfo=None) + timedelta(hours=ot_h + i * candle_h)) for i in range(n)],
        dtype="datetime64[ns]",
    )
    cm_arr = np.asarray(cm if cm is not None else adv, dtype=float)
    gi0 = int(ot_h)
    gie = max(int(ct_h), gi0 + 1)
    hours = np.arange(gi0, gie, dtype=np.int64)
    hour_ts = (GRID0 + hours * np.timedelta64(1, "h")).astype("datetime64[ns]")
    pos = np.clip(np.searchsorted(tt, hour_ts, side="right") - 1, 0, n - 1)
    return {
        "sym": "BTCUSDT",
        "tag": "X",
        "dir": direction,
        "entry": 100.0,
        "ot": BASE + timedelta(hours=ot_h),
        "ct": BASE + timedelta(hours=ct_h),
        "real_unlev": real,
        "gi0": gi0,
        "gie": gie,
        "series": True,
        "fav": np.asarray(fav, dtype=float),
        "adv": np.asarray(adv, dtype=float),
        "cm": cm_arr,
        "tt": tt,
        "hm": cm_arr[pos],
    }


# ─────────────────────────────────────────────────────────────────────────────
# rule semantics
# ─────────────────────────────────────────────────────────────────────────────


def test_timestop_only_touches_never_armed_trades():
    """An armed trade is the trail's business; the time-stop must not preempt it."""
    # Never armed: peak stays below 2 % → time-stop closes at the deadline's candle close.
    loser = _trade(0, 100, fav=[0.5] * 100, adv=[-1.0] * 100, cm=[-0.8] * 100, real=-8.0)
    gi, val = exit_trail_timestop(loser, GRID0, 2.0, 24.0)
    assert gi == 24, gi
    assert val == -0.8, val
    # Armed within the window: identical to the plain trail outcome.
    winner = _trade(0, 100, fav=[0.0, 3.0] + [3.0] * 98, adv=[0.0, 0.0] + [2.0] * 98, real=5.0)
    assert exit_trail_timestop(winner, GRID0, 2.0, 24.0) == exit_trail(winner, GRID0, 2.0)


def test_timestop_after_natural_close_is_a_noop():
    """A trade that closed before the deadline keeps its recorded outcome."""
    t = _trade(0, 5, fav=[0.5] * 5, adv=[-1.0] * 5, real=-3.0)
    gi, val = exit_trail_timestop(t, GRID0, 2.0, 24.0)
    assert (gi, val) == (t["gie"], -3.0)


def test_hardstop_fires_only_if_before_the_trail():
    """Whichever level the tape touches first decides — candle order is ground truth."""
    # Dips to −2 % on candle 1, would have armed on candle 3 → hard stop wins.
    dip_first = _trade(0, 50, fav=[0.0, 1.0, 1.0, 5.0, 5.0], adv=[0.0, -2.5, -1.0, 1.0, 4.0], real=3.0)
    gi, val = exit_trail_hardstop(dip_first, GRID0, 2.0, 2.0)
    assert val == -2.0
    assert gi == 1
    # Arms and trails out on candle 2, dips later → trail wins, stop never fires.
    trail_first = _trade(0, 50, fav=[0.0, 5.0, 5.0, 5.0], adv=[0.0, 3.0, 4.0, -2.5], real=3.0)
    gi2, val2 = exit_trail_hardstop(trail_first, GRID0, 2.0, 2.0)
    assert np.isclose(val2, 4.5), val2  # 5.0 * (1 - 0.10)
    assert gi2 == 2


def test_trail_uses_strictly_prior_peak():
    """Inherited from the slot-budget pins: index 0 can never trigger."""
    t = _trade(0, 50, fav=[10.0, 10.0], adv=[-5.0, 0.0], real=1.0)
    gi, val = exit_trail(t, GRID0, 2.0)
    assert gi == 1  # candle 1, not the trade's own first candle
    assert np.isclose(val, 9.0)


# ─────────────────────────────────────────────────────────────────────────────
# aggregation
# ─────────────────────────────────────────────────────────────────────────────


def test_equity_final_equals_net_realised():
    """The ledger must close: once every trade is closed the open MTM is zero and
    the equity curve ends exactly at the net realised sum."""
    trades = [
        _trade(0, 10, fav=[1.0] * 10, adv=[-1.0] * 10, cm=[-0.5] * 10, real=-2.0),
        _trade(2, 6, fav=[0.0, 4.0, 4.0, 4.0], adv=[0.0, 2.0, 3.5, 1.0], real=1.5),
    ]
    book = run_rule(trades, 20, {i: exit_trail(t, GRID0, 2.0) for i, t in enumerate(trades)})
    s = book.stats()
    assert np.isclose(s["equity_final"], s["net"], atol=0.11), (s["equity_final"], s["net"])


def test_book_counts_positions_until_their_exit():
    t = _trade(0, 10, fav=[1.0] * 10, adv=[-1.0] * 10, cm=[-0.5] * 10, real=-2.0)
    book = run_rule([t], 20, {})
    s = book.stats()
    long_cnt, short_cnt = s["_cnt"]
    assert long_cnt[0] == 1 and long_cnt[9] == 1 and long_cnt[10] == 0
    assert short_cnt.sum() == 0


def test_partial_charges_one_round_trip_fee_per_trade():
    """Two half-exits must not double- or half-charge the fee."""
    t = _trade(0, 10, fav=[0.0, 4.0] + [4.0] * 8, adv=[0.0, 2.0] + [1.0] * 8, cm=[0.0] * 10, real=2.0)
    book = run_partial([t], 20, GRID0, 2.0)
    s = book.stats()
    # halves: 0.5 * 3.6 (trail fill at 4 * 0.9) + 0.5 * 2.0 (hold) − 1 × fee
    assert np.isclose(s["net"], 0.5 * 3.6 + 0.5 * 2.0 - FEE_RT), s["net"]
    assert s["n"] == 1


def test_exposure_cap_refuses_the_stretching_side_only():
    """Cap ±1: the second LONG (imbalance would reach 2) is refused, a SHORT is not."""
    trades = [
        _trade(0, 30, fav=[0.1] * 30, adv=[-0.1] * 30, real=0.0, direction="LONG"),
        _trade(1, 30, fav=[0.1] * 29, adv=[-0.1] * 29, real=5.0, direction="LONG"),
        _trade(1, 30, fav=[0.1] * 29, adv=[-0.1] * 29, real=1.0, direction="SHORT"),
    ]
    book = run_exposure_cap(trades, 40, GRID0, 2.0, cap=1)
    s = book.stats()
    assert s["n"] == 2, s["n"]  # the second LONG was never admitted
    assert s["cnt_short_mean"] > 0  # the SHORT was
    # net = admitted trades only: 0.0 + 1.0 − 2 fees
    assert np.isclose(s["net"], 1.0 - 2 * FEE_RT), s["net"]


def test_breakeven_ratchet_bounds_giveback_at_zero():
    """Armed at candle 1 (prior peak 5 > 2), touches entry again at candle 3 → exit 0.0.
    The ratchet may not exit before arming, and never at a negative value."""
    t = _trade(0, 50, fav=[0.0, 5.0, 5.0, 5.0], adv=[-1.0, 1.0, 2.0, -0.5], real=3.0)
    gi, val = exit_breakeven(t, GRID0, 2.0)
    assert val == 0.0
    assert gi == 3  # candle 0's entry-touch is pre-arming and must not fire
    # Armed but never back to entry → rides to its natural close.
    rider = _trade(0, 10, fav=[0.0, 5.0] + [6.0] * 8, adv=[0.0, 1.0] + [3.0] * 8, real=4.0)
    assert exit_breakeven(rider, GRID0, 2.0) == (rider["gie"], 4.0)


def test_breakeven_with_timestop_bounds_the_never_armed():
    """Without ts_hours a never-armed loser rides to its close; with ts_hours it
    time-stops — the ratchet alone has no stop to ratchet on an unarmed trade."""
    loser = _trade(0, 100, fav=[0.5] * 100, adv=[-1.0] * 100, cm=[-0.9] * 100, real=-7.0)
    assert exit_breakeven(loser, GRID0, 2.0) == (loser["gie"], -7.0)
    gi, val = exit_breakeven(loser, GRID0, 2.0, ts_hours=24.0)
    assert (gi, val) == (24, -0.9)


def test_breakeven_timestop_is_causal_for_late_armers():
    """A trade that first clears the activation AFTER the deadline must be
    time-stopped at the deadline — the live bot cannot see the future. (The
    original sim checked arming over the whole series; every late winner
    escaped the stop, inflating the be+ts results.)"""
    fav = [1.0] * 30 + [8.0] * 70  # arms at hour ~30, deadline is 24
    late = _trade(0, 100, fav=fav, adv=[-0.5] * 100, cm=[-0.4] * 100, real=6.0)
    gi, val = exit_breakeven(late, GRID0, 2.0, ts_hours=24.0)
    assert (gi, val) == (24, -0.4), (gi, val)  # stopped at the deadline mark
    # Armed BEFORE the deadline → the ratchet governs, no time-stop.
    early = _trade(0, 100, fav=[6.0] * 100, adv=[3.0] * 100, real=6.0)
    assert exit_breakeven(early, GRID0, 2.0, ts_hours=24.0) == (early["gie"], 6.0)


def test_exposure_cap_composes_with_custom_exits():
    """The cap must apply the exits it is given, not silently fall back to the trail."""
    t = _trade(0, 100, fav=[0.5] * 100, adv=[-1.0] * 100, cm=[-0.8] * 100, real=-8.0)
    exits = {0: exit_trail_timestop(t, GRID0, 2.0, 24.0)}
    book = run_exposure_cap([t], 120, GRID0, 2.0, cap=5, exits=exits)
    s = book.stats()
    assert np.isclose(s["net"], -0.8 - FEE_RT), s["net"]  # time-stop value, not hold's -8


def test_wider_giveback_exits_later_and_captures_less():
    """x=30 % must never fire before x=10 % and fills lower on the same peak."""
    t = _trade(0, 50, fav=[0.0, 10.0, 10.0, 10.0], adv=[0.0, 9.5, 8.5, 6.5], real=1.0)
    gi10, val10 = exit_trail(t, GRID0, 2.0, x=0.10)
    gi30, val30 = exit_trail(t, GRID0, 2.0, x=0.30)
    assert gi10 == 2 and np.isclose(val10, 9.0)
    assert gi30 == 3 and np.isclose(val30, 7.0)
    assert gi30 >= gi10 and val30 < val10


def test_feedback_gate_stops_feeding_a_bleeding_side():
    """Once >= min_n open LONGs sit below thresh, the next LONG is refused;
    a SHORT is still admitted (the gate reads each side separately)."""
    bleeders = [
        _trade(0, 100, fav=[0.5] * 100, adv=[-2.0] * 100, cm=[-2.0] * 100, real=-5.0, direction="LONG")
        for _ in range(3)
    ]
    late_long = _trade(5, 100, fav=[0.5] * 95, adv=[-1.0] * 95, cm=[-1.0] * 95, real=-4.0, direction="LONG")
    late_short = _trade(5, 100, fav=[0.5] * 95, adv=[-1.0] * 95, cm=[-1.0] * 95, real=2.0, direction="SHORT")
    book = run_feedback_gate(bleeders + [late_long, late_short], 120, GRID0, 2.0, thresh=-1.0, min_n=3)
    s = book.stats()
    assert s["n"] == 4, s["n"]  # the late LONG was refused, everything else admitted
    # the admitted set realises: 3 * -5.0 + 2.0 - 4 fees
    assert np.isclose(s["net"], 3 * -5.0 + 2.0 - 4 * FEE_RT), s["net"]


def test_direction_gate_filters_by_hourly_allowance():
    long_t = _trade(2, 30, fav=[0.5] * 28, adv=[-0.5] * 28, real=1.0, direction="LONG")
    short_t = _trade(2, 30, fav=[0.5] * 28, adv=[-0.5] * 28, real=1.5, direction="SHORT")
    allow_long = np.zeros(40, dtype=bool)  # BTC falling the whole time
    allow_short = np.ones(40, dtype=bool)
    book = run_direction_gate([long_t, short_t], 40, GRID0, 2.0, allow_long, allow_short)
    s = book.stats()
    assert s["n"] == 1
    assert np.isclose(s["net"], 1.5 - FEE_RT)


def test_total_cap_refuses_entries_while_full():
    """With cap=1 the second, overlapping trade is refused; after the first
    exits, a later arrival gets the freed seat."""
    from tools.trailing_book_health import run_total_cap

    first = _trade(0, 10, fav=[0.1] * 10, adv=[-0.1] * 10, real=1.0)
    overlapping = _trade(5, 20, fav=[0.1] * 15, adv=[-0.1] * 15, real=99.0)
    later = _trade(12, 20, fav=[0.1] * 8, adv=[-0.1] * 8, real=2.0)
    book = run_total_cap([first, overlapping, later], 30, {}, cap=1)
    s = book.stats()
    assert s["n"] == 2, s["n"]  # the overlapping trade never got a seat
    assert np.isclose(s["net"], 1.0 + 2.0 - 2 * FEE_RT), s["net"]


def test_chase_gate_spares_counter_move_entries():
    """A SHORT into a +60% pump is counter-move (the MIS edge) and must pass; a
    LONG into the same pump chases and is gated; unknown history never vetoes."""
    from tools.trailing_book_health import chases_the_move

    assert chases_the_move("LONG", 60.0, 50.0) is True
    assert chases_the_move("SHORT", 60.0, 50.0) is False  # shorting the pump = fine
    assert chases_the_move("SHORT", -60.0, 50.0) is True
    assert chases_the_move("LONG", -60.0, 50.0) is False  # longing the dump = fine
    assert chases_the_move("LONG", None, 50.0) is False


def test_move_gate_filters_admission_only():
    from tools.trailing_book_health import run_move_gate

    hot = _trade(0, 10, fav=[1.0] * 10, adv=[-1.0] * 10, cm=[0.0] * 10, real=2.0)
    hot["mv24"] = 80.0
    calm = _trade(0, 10, fav=[1.0] * 10, adv=[-1.0] * 10, cm=[0.0] * 10, real=1.0)
    calm["mv24"] = 5.0
    book = run_move_gate([hot, calm], 20, {}, abs_thresh=50.0)
    s = book.stats()
    assert s["n"] == 1 and np.isclose(s["net"], 1.0 - FEE_RT), (s["n"], s["net"])


def test_hardstop_tie_is_sl_first():
    """Same-candle touch of trail level AND stop → the stop fills (monitor
    convention; the earlier trail-first tie-break flattered the stop rule)."""
    t = _trade(0, 5, fav=[0.0, 5.0, 5.0], adv=[0.0, 0.0, -6.0], real=1.0)
    gi, val = exit_trail_hardstop(t, GRID0, 2.0, 5.0)
    assert (gi, val) == (2, -5.0), (gi, val)


def test_deployed_slcap_takes_the_earliest_event():
    """Composite rule: stop, trail and causal time-stop compete on candle index;
    the earliest fires, the time-stop only when unarmed at its deadline."""
    from tools.trailing_book_health import exit_deployed_slcap

    # Dips through −5 at hour 3, would have armed at hour 30 → stop wins.
    dip = _trade(0, 100, fav=[1.0] * 30 + [8.0] * 70, adv=[-1.0] * 3 + [-6.0] + [-1.0] * 96, cm=[-1.0] * 100, real=4.0)
    assert exit_deployed_slcap(dip, GRID0, 2.0, 24.0, 5.0) == (3, -5.0)
    # Never armed, never below −5 → causal time-stop at 24h.
    limbo = _trade(0, 100, fav=[0.5] * 100, adv=[-2.0] * 100, cm=[-1.5] * 100, real=-4.0)
    assert exit_deployed_slcap(limbo, GRID0, 2.0, 24.0, 5.0) == (24, -1.5)
    # Arms early and trails out before anything else.
    winner = _trade(0, 100, fav=[0.0, 4.0] + [4.0] * 98, adv=[0.0, 2.0] + [1.0] * 98, real=3.0)
    gi, val = exit_deployed_slcap(winner, GRID0, 2.0, 24.0, 5.0)
    assert gi == 2 and np.isclose(val, 3.6), (gi, val)


def test_no_series_trade_keeps_recorded_outcome_under_every_rule():
    """A trade without candle coverage must fall back to its recorded close."""
    t = {
        "sym": "X",
        "tag": "X",
        "dir": "LONG",
        "entry": 100.0,
        "ot": BASE,
        "ct": BASE + timedelta(hours=5),
        "real_unlev": -1.5,
        "gi0": 0,
        "gie": 5,
        "series": False,
    }
    for fn in (
        lambda: exit_trail(t, GRID0, 2.0),
        lambda: exit_trail_timestop(t, GRID0, 2.0, 24.0),
        lambda: exit_trail_hardstop(t, GRID0, 2.0, 2.0),
    ):
        gi, val = fn()
        assert (gi, val) == (5, -1.5)


# ─────────────────────────────────────────────────────────────────────────────
# TP1 activation (T-2026-KYT-9050-093)
# ─────────────────────────────────────────────────────────────────────────────


def test_act_tp1_uses_the_trades_own_target():
    """The activation becomes per-trade: two trades, two arming thresholds."""
    near = _trade(0, 10, fav=[1.0], adv=[0.0])
    far = _trade(0, 10, fav=[1.0], adv=[0.0])
    near["tp1_pct"], far["tp1_pct"] = 1.7, 9.0
    assert act_tp1(near) == 1.7
    assert act_tp1(far) == 9.0


def test_act_tp1_floor_only_ever_raises():
    """`f2` keeps the 2 % micro-scalper floor — 24 % of live TP1s sit below it."""
    low = _trade(0, 10, fav=[1.0], adv=[0.0])
    low["tp1_pct"] = 1.2
    assert act_tp1(low) == 1.2  # bare TP1 would arm EARLIER than the live bot
    assert act_tp1(low, 2.0) == 2.0
    high = _trade(0, 10, fav=[1.0], adv=[0.0])
    high["tp1_pct"] = 6.0
    assert act_tp1(high, 2.0) == 6.0  # the floor never caps a wide target


def test_act_tp1_without_coverage_falls_back_to_the_live_activation():
    """An uncovered trade must behave exactly like today's bot, not like a hold.

    This is why `--tp1-only` exists: on an uncovered period every TP1 rule silently
    degenerates into trail-a2, which would read as 'TP1 changes nothing'.
    """
    t = _trade(0, 10, fav=[1.0], adv=[0.0])
    t["tp1_pct"] = None
    assert act_tp1(t) == ACT_LIVE
    assert act_tp1(t, 2.0) == ACT_LIVE


def test_tp1_activation_delays_the_exit_versus_act2():
    """A trade whose TP1 sits at 6 % must not be trailed out on a 2,5 % blip."""
    # Peak 2,5 % in candle 1, gives back to 2,0 % in candle 2, then runs to 8 %.
    fav = [0.0, 2.5, 2.5, 8.0, 8.0]
    adv = [0.0, 2.5, 2.0, 2.0, 7.0]
    t = _trade(0, 10, fav=fav, adv=adv, cm=[0.0, 2.5, 2.0, 8.0, 7.0], real=7.0)
    t["tp1_pct"] = 6.0
    gi_a2, val_a2 = exit_trail(t, GRID0, 2.0)
    gi_tp1, val_tp1 = exit_trail(t, GRID0, act_tp1(t))
    assert gi_a2 < gi_tp1, (gi_a2, gi_tp1)
    assert val_tp1 > val_a2, (val_a2, val_tp1)
    assert np.isclose(val_tp1, 7.2), val_tp1  # peak 8 % × (1 − 0,10)


def test_impute_tp1_fills_only_uncovered_trades_of_a_covered_leg():
    """Imputation is a per-leg constant and must never overwrite a real target."""
    covered_a = _trade(0, 10, fav=[1.0], adv=[0.0])
    covered_b = _trade(0, 10, fav=[1.0], adv=[0.0])
    gap = _trade(0, 10, fav=[1.0], adv=[0.0])
    orphan = _trade(0, 10, fav=[1.0], adv=[0.0])
    orphan["tag"] = "OTHER"
    covered_a["tp1_pct"], covered_b["tp1_pct"] = 3.0, 5.0
    gap["tp1_pct"] = orphan["tp1_pct"] = None
    n = impute_tp1([covered_a, covered_b, gap, orphan])
    assert n == 1
    assert gap["tp1_pct"] == 4.0 and gap["tp1_imputed"] is True  # median of 3 and 5
    assert covered_a["tp1_pct"] == 3.0 and covered_a["tp1_imputed"] is False
    # A leg with no coverage at all keeps its gap rather than borrowing a foreign
    # leg's geometry — it then rides the documented act=2 fallback.
    assert orphan["tp1_pct"] is None


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

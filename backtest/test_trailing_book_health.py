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
    FEE_RT,
    exit_trail,
    exit_trail_hardstop,
    exit_trail_timestop,
    run_exposure_cap,
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

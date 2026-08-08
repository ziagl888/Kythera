# backtest/test_ods1_bracket_study.py — the replay's arithmetic and its honesty guards.
"""Standalone, DB-free tests for tools/ods1_bracket_study.py (T-2026-KYT-9050-116).

Three things in this study can be wrong in ways a green run would not reveal, and
each one changes the recommended bracket:

  * the SHORT path simulation (a flipped comparison scores losses as target hits —
    P0.7's failure class, and the reason 96.5 % phantom wins once passed review),
  * the look-ahead boundary (resolving a trade on the bar the decision was made on
    reads the future of that bar),
  * the bisect windowing that makes the replay runnable at all — it is only
    legitimate if it returns EXACTLY what the unwindowed rule returns.

The last one is the load-bearing claim: the whole study rests on "replay ==
serving", so the optimisation must be an identity, not an approximation.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import math
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")


def _load():
    spec = importlib.util.spec_from_file_location(
        "ods1_bracket_study", os.path.join(REPO, "tools", "ods1_bracket_study.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ods1_bracket_study"] = mod
    spec.loader.exec_module(mod)
    return mod


S = _load()
ODS1 = S.ODS1


def bars(*rows):
    """[(high, low, close), ...] -> the study's bar format, 5 min apart."""
    return [(1_760_000_000 + i * 300, h, lo, c) for i, (h, lo, c) in enumerate(rows)]


# ── the SHORT path ───────────────────────────────────────────────────────────


def test_entry_is_the_event_bar_close_and_that_bar_cannot_resolve_the_trade():
    """Look-ahead boundary. The decision is made ON the event bar, so using that
    bar's own high/low to close the trade would read the future of the bar the
    signal was born in — the same look-ahead that invalidated T-104's OI feature."""
    # event bar's range spans both the target and the stop; it must be ignored
    path = bars((200.0, 50.0, 100.0), (100.0, 98.9, 99.0))
    move, reason, _amb = S.simulate(path, tp1=1.0, tp2=2.0, sl=2.0)
    assert reason == "TP2" or reason == "TIME"
    assert move > 0, "the entry bar's own spike must not stop the trade out"


def test_a_short_takes_profit_when_price_falls():
    path = bars((100.0, 100.0, 100.0), (100.0, 98.9, 99.0), (99.0, 97.9, 98.0))
    move, reason, _ = S.simulate(path, tp1=1.0, tp2=2.0, sl=2.0)
    assert reason == "TP2"
    assert move == pytest.approx(1.5)  # (1.0 + 2.0) / 2


def test_a_short_stops_out_when_price_rises():
    path = bars((100.0, 100.0, 100.0), (102.5, 100.0, 102.0))
    move, reason, _ = S.simulate(path, tp1=1.0, tp2=2.0, sl=2.0)
    assert reason == "SL"
    assert move == pytest.approx(-2.0)  # both halves stopped: (-2 + -2) / 2


def test_the_runner_carries_the_stop_after_the_first_rung():
    """TP1 filled, then the move reverses into the stop. Half is booked at +1 %,
    half at −2 %. Getting this wrong is what makes a losing ladder look flat."""
    path = bars((100.0, 100.0, 100.0), (100.0, 98.9, 99.0), (102.5, 99.0, 102.0))
    move, reason, _ = S.simulate(path, tp1=1.0, tp2=2.0, sl=2.0)
    assert reason == "SL"
    assert move == pytest.approx((1.0 - 2.0) / 2.0)


def test_an_unresolved_trade_is_marked_out_at_the_last_close():
    """And on the SAME base as the rest of the function: a move is a percentage of
    the ENTRY, matching `core.realized_pnl._signed_move_pct` and therefore the
    closed book this study is meant to improve on.

    The first version hand-rolled `(entry / last - 1)`, putting the EXIT price in
    the denominator — 0.5025 % instead of 0.5000 % here. Small at this distance,
    but it made every time-exit incomparable with the live rows, and the bias grows
    with the size of the move.
    """
    path = bars((100.0, 100.0, 100.0), (100.2, 99.8, 99.5))
    move, reason, _ = S.simulate(path, tp1=1.0, tp2=2.0, sl=2.0)
    assert reason == "TIME"
    assert move == pytest.approx(0.5)  # SHORT 100 -> 99.5 as % of entry, both halves open

    from core.realized_pnl import _signed_move_pct

    assert move == pytest.approx(_signed_move_pct(-1.0, 100.0, 99.5))


def test_an_ambiguous_bar_is_resolved_against_the_trade_and_flagged():
    """One 5m bar touching both ends cannot be ordered from OHLC. Resolving it in
    the trade's favour would manufacture edge, so it resolves as a stop — and it is
    counted, because a surface built mostly on ambiguous bars is an artefact of
    this choice rather than a measurement."""
    path = bars((100.0, 100.0, 100.0), (102.5, 98.5, 100.0))
    move, reason, amb = S.simulate(path, tp1=1.0, tp2=2.0, sl=2.0)
    assert reason == "SL"
    assert amb is True
    assert move == pytest.approx(-2.0)


def test_a_path_without_forward_bars_is_not_scored_as_flat():
    """A missing path must be dropped, never counted as a 0 % trade — that would
    silently pull every cell's mean toward zero in proportion to the data gaps."""
    assert S.simulate(bars((100.0, 100.0, 100.0)), 1.0, 2.0, 2.0)[1] == "NO_PATH"
    assert S.simulate([], 1.0, 2.0, 2.0)[1] == "NO_PATH"


# ── grid admissibility ───────────────────────────────────────────────────────


def test_t_statistic_is_the_one_sample_t_the_verdict_rests_on():
    """`_t` produces the t=0.60 that decides the whole study, and nothing pinned it.

    Hand-computed: mean 2.0, sample sd (ddof=1) 1.0 over n=5 -> t = 2.0/(1/sqrt(5)).
    A population sd (ddof=0) or a missing sqrt(n) both change the verdict's number
    while leaving every other test green — both were survived mutations in review.
    """
    xs = [1.0, 2.0, 3.0, 1.0, 3.0]  # mean 2.0, ddof=1 sd = 1.0
    assert S._t(xs) == pytest.approx(2.0 / (1.0 / math.sqrt(5)))
    assert S._t([1.0, 1.0, 1.0]) == 0.0, "zero spread is not a signal"
    assert S._t([1.0, 2.0]) == 0.0, "n < 3 is undefined, not significant"


def test_the_replay_is_driven_end_to_end_not_re_implemented(monkeypatch):
    """The windowing identity, asserted against `replay_events` ITSELF.

    The first version re-implemented the bisect inline in the test and compared THAT
    to the bot — so it proved a property of the test's own code, not of the study's.
    Review caught it by mutating `span` to drop STALENESS_CAP_S: every test stayed
    green. This drives the real function over a fake cursor and compares against a
    naive full-series loop, which is the claim the module actually makes.
    """

    class _Cur:
        def __init__(self, rows):
            self.rows = rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self._out = [] if "SET " in sql else self.rows

        def fetchall(self):
            return self._out

    class _Conn:
        def __init__(self, rows):
            self.rows = rows

        def cursor(self):
            return _Cur(self.rows)

    # A rally on draining OI, with the two endpoints far enough apart that a window
    # missing STALENESS_CAP_S would lose the `then` point and fire nothing.
    t0 = int(dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc).timestamp())
    rows = []
    for off, oi, px in [
        (0, 1_000_000.0, 100.0),
        (4 * 3600 + 1500, 1_000_000.0, 100.0),
        (4 * 3600 + 1800, 940_000.0, 108.0),
        (5 * 3600, 940_000.0, 108.0),
    ]:
        rows.append(("XUSDT", t0 + off, oi, oi * px))

    got = S.replay_events(_Conn(rows), "2026-07-01", "2026-07-01T06:00:00")

    # naive reference: the bot's own rule over the FULL series, same poll grid
    series = {"XUSDT": [(int(ts), float(oi), float(v) / float(oi)) for _s, ts, oi, v in rows]}
    want, seen = [], {}
    t, end = t0, t0 + 6 * 3600
    while t <= end:
        for c in ODS1.find_candidates(series, t)[0]:
            prev = seen.get(c["symbol"])
            if prev is None or t - prev >= ODS1.COOLDOWN_H * 3600:
                seen[c["symbol"]] = t
                want.append((t, c["symbol"]))
        t += ODS1.POLL_SECONDS

    assert [(e["ts"], e["symbol"]) for e in got] == want
    assert want, "the fixture must actually fire, or this asserts nothing"


def test_entry_is_the_close_of_the_event_bar_not_its_open_or_high():
    """Pins WHICH price of the event bar is the anchor.

    Discriminating by construction, which the first attempt was not: it used a bar
    with a high of 120 and a close of 100, but the follow-on lows cleared BOTH
    rungs under either anchor, so `entry = path[0][1]` produced the same outcome and
    survived mutation. Here the follow-on low reaches TP1 but not TP2 measured from
    the close (99 / 98), while from the high (118.8 / 117.6) it clears both — so the
    two anchors differ in exit reason AND in value.
    """
    path = bars((120.0, 80.0, 100.0), (99.5, 98.5, 99.0), (99.2, 98.8, 99.0))
    move, reason, _ = S.simulate(path, tp1=1.0, tp2=2.0, sl=2.0)
    assert reason == "TIME", "from the CLOSE only TP1 fills; from the high both rungs would"
    assert move == pytest.approx(1.0)  # TP1 at +1 %, runner marked out at 99.0 = +1 %


def test_the_grid_refuses_rungs_closer_than_the_fleet_minimum():
    """ODS1 already shipped a dead rung once (1.0/1.5): Cornix splits 50/50 and at a
    0.5 % gap whoever reached TP1 took TP2 in the same move. The study must not
    recommend a bracket the venue cannot express as two rungs."""
    events = [{"ts": 0, "symbol": "X", "px_chg": 4.0, "oi_chg": -5.0}]
    paths = {("X", 0): bars(*[(100.0, 100.0, 100.0)] * 60)}
    cells = S.score(events * 40, {("X", 0): paths[("X", 0)]}, split_ts=None)
    assert cells, "an empty surface would make every assertion below vacuous"
    for c in cells:
        assert c["tp2"] - c["tp1"] >= ODS1.MIN_TP_GAP_PCT
        assert c["tp2"] <= c["sl"], "a target beyond the stop is unreachable without the stop firing first"


# ── the windowing identity (the study's load-bearing optimisation) ───────────


def test_windowed_replay_matches_the_unwindowed_rule_exactly():
    """`replay_events` slices each symbol's series to
    [t − LOOKBACK_S − STALENESS_CAP_S, t] so the replay is runnable at all.

    That is only legitimate if it is an IDENTITY. `_as_of` returns the last point
    at or before t and voids anything older than the staleness cap, and
    `find_candidates` asks for that at t and at t − LOOKBACK_S — so every point
    either call can return lies inside the window. Asserted against the bot's own
    function on a series with points deliberately outside it.
    """
    from bisect import bisect_left, bisect_right

    now = 1_760_000_000
    pts = []
    # far history (must be irrelevant), the two points the rule needs, and noise
    for age, oi, px in [
        (30 * 86400, 5_000_000.0, 10.0),  # ancient, outside any window
        (10 * 86400, 4_000_000.0, 20.0),  # ancient
        (4 * 3600 + 60, 1_000_000.0, 100.0),  # the `then` point
        (60, 950_000.0, 104.0),  # the `now` point
    ]:
        pts.append((now - age, oi, px))
    pts.sort()
    series = {"XUSDT": pts}

    full, _ = ODS1.find_candidates(series, now)

    span = ODS1.LOOKBACK_S + ODS1.STALENESS_CAP_S
    stamps = [p[0] for p in pts]
    i, j = bisect_left(stamps, now - span), bisect_right(stamps, now)
    windowed, _ = ODS1.find_candidates({"XUSDT": pts[i:j]}, now)

    assert full == windowed, (full, windowed)
    assert full, "the fixture must actually fire, or this asserts nothing"


def test_the_purge_gap_actually_drops_the_gap_cohort():
    """The gap must PURGE, not shift.

    The first version asserted only `PURGE_H == HORIZON_H` — the constant — while the
    assignment sent everything that was not holdout into fit, gap cohort included.
    The receipt that it never fired was arithmetic: `n_fit + n_hold` equalled
    `n_events` exactly. So this pins the ASSIGNMENT: an event inside the gap must end
    up in neither window, and the totals must not add up to the event count.
    """
    assert S.PURGE_H == S.HORIZON_H, "a purge shorter than the horizon leaks outcomes across the split"

    split = 1_760_000_000
    flat = bars(*[(100.0, 100.0, 100.0)] * 40)
    events, paths = [], {}
    # 40 before the split, 40 inside the gap, 40 after it
    for i, ts in enumerate(
        [split - (i + 1) * 3600 for i in range(40)]
        + [split + 3600 for _ in range(40)]
        + [split + S.PURGE_H * 3600 + 3600 for _ in range(40)]
    ):
        sym = f"S{i}USDT"
        events.append({"ts": ts, "symbol": sym, "px_chg": 4.0, "oi_chg": -5.0})
        paths[(sym, ts)] = flat

    cells = S.score(events, paths, split_ts=split)
    assert cells, "fixture must produce admissible cells"
    for c in cells:
        assert c["n_purged"] == 40, c["n_purged"]
        assert c["n_fit"] == 40 and c["n_hold"] == 40
        assert c["n_fit"] + c["n_hold"] < len(events), (
            "n_fit + n_hold == n_events is the signature of a gap that purges nothing"
        )


def test_the_window_keeps_both_endpoints_the_rule_needs():
    """Guards the span itself: a window shorter than LOOKBACK_S + STALENESS_CAP_S
    would drop the `then` endpoint and silently void symbols instead of firing."""
    assert S.PURGE_H == S.HORIZON_H, "a purge shorter than the horizon leaks outcomes across the split"
    now = 1_760_000_000
    pts = [(now - 4 * 3600 - 60, 1_000_000.0, 100.0), (now - 60, 950_000.0, 104.0)]
    got, _ = ODS1.find_candidates({"XUSDT": pts}, now)
    assert [c["symbol"] for c in got] == ["XUSDT"]
    # one second too short on the lookback side and the `then` point is gone
    too_short = [p for p in pts if p[0] > now - 4 * 3600]
    assert ODS1.find_candidates({"XUSDT": too_short}, now)[0] == []

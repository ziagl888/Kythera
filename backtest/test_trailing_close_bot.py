# backtest/test_trailing_close_bot.py — T-2026-KYT-9050-042 Phase C pins.
#
# Pins the twelve acceptance criteria of docs/T-2026-KYT-9050-042-trailing-bot-spec.md.
# Every one of them is DB-free: the bot's decisions (who gets mirrored, who gets a
# seat, when the trail fires) are pure functions of a roster, a register and a price.
#
# Three of these pins exist because the alternative is a money bug, not a wrong number:
#
#   * AK3/AK9 — Cornix' `Close <SYMBOL>` is symbol-wide (core/config.py:123). Two
#     positions on one symbol in the channel means one trailing exit flattens the
#     other. And a second Cornix-parsable message per signal is a second position
#     (hard rule 4, the fleet-wide double-trade bug of 2026-07-06).
#   * AK10 — the Cornix block has exactly ONE builder. PR #197 removed the entry2
#     line fleet-wide; a copy of that format in the trailing bot would have kept
#     publishing it.
#   * AK11 — the bot must not post anything on the strength of a deploy alone.
#
# Runs without a DB:  python backtest/test_trailing_close_bot.py

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# core.config raises at import when its _required() vars are unset; seed dummies
# before the loader execs the module (the build machine ships an empty .env stub).
os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")

import numpy as np  # noqa: E402

from core.signal_post import build_cornix_block  # noqa: E402
from core.trailing_roster import ROSTER, density, is_rostered, leg_key  # noqa: E402
from core.trailing_state import NO_PEAK, TrailingState, mark_pct  # noqa: E402
from core.wave_exit_sim import trailing_tp_trigger  # noqa: E402

CHANNEL = -1002222222222  # test-local, never a real channel id


def _recent(minutes_ago: float = 0.5):
    """opened_at as the DB hands it back: timezone-aware."""
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes_ago)


def _load_bot(live_posting: bool = True):
    """Load the numerically named bot module with DB + config stubbed out."""
    env = {"TRAILING_BOT_LIVE_POSTING": "1"} if live_posting else {}
    spec = importlib.util.spec_from_file_location("trailing_close_bot", os.path.join(ROOT, "40_trailing_close_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, env, clear=False):
        if not live_posting:
            os.environ.pop("TRAILING_BOT_LIVE_POSTING", None)
        # A free-profile load elsewhere in this file must not leak into the
        # default (trail) loads — the profile is read at module exec.
        os.environ.pop("TRAILING_BOT_PROFILE", None)
        with mock.patch.dict(
            "sys.modules",
            {
                "core.database": MagicMock(),
                "core.config": MagicMock(CH_TRAILING=CHANNEL if live_posting else 0),
            },
        ):
            spec.loader.exec_module(mod)
    return mod


bot = _load_bot()


# ─────────────────────────────────────────────────────────────────────────────
# Fake DB — records what the bot writes, so "never writes ai_signals" is testable
# ─────────────────────────────────────────────────────────────────────────────


class FakeCursor:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # Matchers say "FROM trailing" rather than naming the table: the module is
    # BOTH bots since T-117, and the free profile reads/writes
    # trailing_free_positions through the very same code path.
    def execute(self, sql, params=None):
        self.store["sql"].append((" ".join(sql.split()), params))
        if "FROM ai_signals" in sql:
            self._rows = self.store["ai_signals"]
        elif "DISTINCT symbol FROM trailing" in sql and "close_reason = ANY" in sql:
            # Checked BEFORE the cooling branch: both read `DISTINCT symbol FROM
            # trailing_*` and would otherwise answer from the same list,
            # which would make the two locks indistinguishable in every test here.
            self._rows = [(x,) for x in self.store["locked"]]
        elif "DISTINCT symbol FROM trailing" in sql:
            self._rows = [(x,) for x in self.store["cooling"]]
        elif "FROM trailing" in sql:
            self._rows = self.store["mirrors"]
        else:
            self._rows = []
            if "INSERT INTO telegram_outbox" in sql:
                self.store["outbox"].append(params)
            elif "INSERT INTO trailing" in sql and "RETURNING id" in sql:
                # A real insert returns its new id; the LosingCursor below overrides
                # fetchone to model ON CONFLICT DO NOTHING swallowing the row.
                self.store["inserted"].append(params)
                self._rows = [(len(self.store["inserted"]),)]

    rowcount = 0

    def executemany(self, sql, seq):
        for params in seq:
            self.execute(sql, params)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, ai_signals=(), mirrors=()):
        self.store = {
            "sql": [],
            "outbox": [],
            "inserted": [],
            "cooling": [],
            "locked": [],
            "ai_signals": list(ai_signals),
            "mirrors": list(mirrors),
        }

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass


def _src_row(sid, symbol, model, direction, entry=100.0, age_sec=5.0):
    """One ai_signals row in the shape read_source_signals SELECTs.

    `age_min` is what the DB computes as `NOW() - open_time`; default is a fresh
    signal so the existing pins keep exercising the mirroring path."""
    return (sid, symbol, model, direction, entry, entry, entry * 0.95, "[110.0, 120.0]", "20x", age_sec)


def _cand(sid, symbol, tag, direction):
    return sid, {"symbol": symbol, "tag": tag, "direction": direction, "density": density(tag, direction)}


# ─────────────────────────────────────────────────────────────────────────────
# AK1 / AK2 — who is mirrored at all
# ─────────────────────────────────────────────────────────────────────────────


#: Seats that did NOT come from the PR-#198 p95 fill and therefore carry a
#: placeholder density instead of a measured one. Listed explicitly so an
#: unmeasured leg can never be added by bumping a count — every entry here has to
#: be typed out, and the assertions below hold the whole set below the measured
#: floor rather than naming one leg.
UNMEASURED_SEATS = {("ODS1", "SHORT"), ("FIF2", "LONG"), ("FIF2", "SHORT")}


def test_roster_admits_only_selected_legs():
    """AK1. The roster is the PR-#198 selection minus ROM1 — not 'all live bots'.

    29 measured seats = the 33 of the p95 fill, minus ROM1 LONG/SHORT, minus
    MIS1-72h LONG and AIM2 SHORT. Bot 28 re-forwards trades the original legs
    already post (double-counting, T-052), and its rows carry the ORIGINAL
    open_time, so no freshness window can admit it honestly — documented in
    core.trailing_roster.EXCLUDED_AS_DUPLICATE.

    The other two went the opposite way: they were struck AFTER the fact on their
    own realised trailing book (-185.72 and -89.02 USD, T-2026-KYT-9050-126/-129),
    not on a verdict of the simulation. That register is
    core.trailing_roster.RETIRED_FOR_LIVE_PNL, and it is a per-DIRECTION decision —
    AIM2 LONG keeps its seat.

    On top sit the seats with no measured density: ODS1 SHORT
    (T-2026-KYT-9050-106) and FIF2 LONG/SHORT (T-2026-KYT-9050-115). They are
    pinned below the measured floor on purpose. That distinction is asserted
    structurally rather than folded into the count, so a future unmeasured leg
    cannot be added silently by bumping a number.

    FIF2 is a re-forwarder like ROM1 but is NOT excluded for it: ROM1 re-posted the
    same trades the original legs already fed into this channel, while FIF2's
    overlap is resolved by `admit` (density sort + SYMBOL_HELD) and its unique
    contribution is the legs that never earned a seat. What its seat did require is
    the symbol-scoped re-entry lock — a re-forwarded row carries a new
    src_signal_id, which the src-keyed lock cannot recognise."""
    from core.trailing_roster import EXCLUDED_AS_DUPLICATE, RETIRED_FOR_LIVE_PNL

    measured = {leg: d for leg, d in ROSTER.items() if leg not in UNMEASURED_SEATS}
    assert len(measured) == 29
    assert len(ROSTER) == len(measured) + len(UNMEASURED_SEATS)
    assert UNMEASURED_SEATS <= set(ROSTER), "every declared placeholder seat must actually be seated"
    # Every unmeasured leg must sit strictly below every measured one — the density
    # column doubles as eviction order when the 500-slot cap binds, so an unmeasured
    # leg must be the first to yield a seat, never the last.
    assert max(ROSTER[leg] for leg in UNMEASURED_SEATS) < min(measured.values())
    assert is_rostered("ODS1", "SHORT")
    assert not is_rostered("ODS1", "LONG")  # SHORT-only by construction
    assert is_rostered("FIF2", "LONG") and is_rostered("FIF2", "SHORT")  # both legs measured in T-111

    assert set(EXCLUDED_AS_DUPLICATE) == {("ROM1", "LONG"), ("ROM1", "SHORT")}
    assert not is_rostered("ROM1", "LONG") and not is_rostered("ROM1", "SHORT")
    assert is_rostered("BR4H", "LONG")  # accepted, density 0.549
    assert not is_rostered("BR4H", "SHORT")  # other direction never made the cut
    assert not is_rostered("EPD3", "LONG")  # rejected for cap (would_be p95 581)
    assert not is_rostered("TSM1", "SHORT")  # rejected for cap
    # The struck register is pinned like EXCLUDED_AS_DUPLICATE above, and the two
    # sets must stay DISJOINT: a leg back in ROSTER while still listed as retired
    # would make is_rostered() say yes where the register says no, and nothing else
    # in the codebase reads RETIRED_FOR_LIVE_PNL to catch it.
    assert set(RETIRED_FOR_LIVE_PNL) == {("MIS1-72h", "LONG"), ("AIM2", "SHORT")}
    assert not (set(ROSTER) & set(RETIRED_FOR_LIVE_PNL))
    # Struck after the fact on their own realised book (T-2026-KYT-9050-129).
    assert not is_rostered("MIS1-72h", "LONG")
    assert not is_rostered("AIM2", "SHORT")
    assert is_rostered("AIM2", "LONG")  # direction, not model — the LONG leg earns
    # Historic model spellings must land on the leg that earned the seat, not miss it.
    assert leg_key("MSI1-168H_pump", "long") == ("MIS1-168h", "LONG")
    assert is_rostered("MSI1-168H_pump", "long")

    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG"), _src_row(2, "ETHUSDT", "EPD3", "LONG")])
    got, all_open = bot.read_source_signals(conn)
    assert set(got) == {1}, got
    assert all_open == {1, 2}  # the filtered-out leg is still a KNOWN open source


def test_non_live_leg_is_never_mirrored():
    """AK2. shadow_gate beats the roster — the roster is a still from 2026-07-26."""
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG")])
    with mock.patch.object(bot.shadow_gate, "is_live", return_value=False):
        assert bot.read_source_signals(conn)[0] == {}
    assert set(bot.read_source_signals(conn)[0]) == {1}  # control: live again


def test_signal_without_geometry_is_not_mirrored():
    """A half order-geometry in a Cornix channel is worse than no mirror."""
    no_sl = (1, "BTCUSDT", "BR4H", "LONG", 100.0, 100.0, None, "[110.0]", "20x", 1.0)
    no_tgt = (2, "ETHUSDT", "BR4H", "LONG", 100.0, 100.0, 95.0, "[]", "20x", 1.0)
    assert bot.read_source_signals(FakeConn(ai_signals=[no_sl, no_tgt]))[0] == {}


# ─────────────────────────────────────────────────────────────────────────────
# AK3 / AK4 — admission
# ─────────────────────────────────────────────────────────────────────────────


def test_second_signal_on_same_symbol_is_rejected():
    """AK3. `Close <SYMBOL>` is symbol-wide: two positions on one symbol means the
    trailing exit of one flattens the other."""
    cands = [_cand(1, "BTCUSDT", "BR4H", "LONG"), _cand(2, "BTCUSDT", "RUB1", "LONG")]
    admitted, rejected = bot.admit(cands, held_symbols=set(), free_slots=500)
    assert [sid for sid, _ in admitted] == [2]  # RUB1 is denser (2.346 > 0.959)
    assert [(sid, why) for sid, _, why in rejected] == [(1, "SYMBOL_HELD")]

    # A symbol the channel already holds blocks a fresh signal too.
    admitted, rejected = bot.admit([_cand(3, "BTCUSDT", "AIM2", "LONG")], {"BTCUSDT"}, 500)
    assert admitted == [] and rejected[0][2] == "SYMBOL_HELD"


def test_slot_cap_rejects_by_density():
    """AK4. The chosen selection peaks at 2001 concurrent = 4x the Cornix cap. Who
    gets refused has to be our decision (by net per slot-day), not Cornix' by luck."""
    cands = [
        _cand(1, "AAAUSDT", "ATS2", "LONG"),  # density 0.054 — thinnest measured seat
        _cand(2, "BBBUSDT", "RUB1", "SHORT"),  # density 3.943 — dense
        _cand(3, "CCCUSDT", "EPD1", "SHORT"),  # density 2.238
    ]
    admitted, rejected = bot.admit(cands, set(), free_slots=2)
    assert [sid for sid, _ in admitted] == [2, 3]
    assert [(sid, why) for sid, _, why in rejected] == [(1, "SLOT_CAP")]
    # Nothing is dropped silently — every candidate is accounted for.
    assert len(admitted) + len(rejected) == len(cands)


def test_slot_cap_is_the_cornix_channel_cap():
    assert bot.SLOT_CAP == 500


# ─────────────────────────────────────────────────────────────────────────────
# AK5 / AK6 / AK7 — the trailing rule
# ─────────────────────────────────────────────────────────────────────────────


def test_activation_floor_gates_the_trail():
    """AK5. Without the floor a 10 %-give-back trail closes on a +0.5 % peak and the
    bot is a micro-scalper — that is why T-041/T-046 looked so good."""
    st = TrailingState(entry=100.0, is_long=True, retrace_frac=0.10, activation=2.0)
    assert st.update(100.5)[0] is False  # peak +0.5 %, floor not cleared
    assert st.update(100.4)[0] is False  # gave back 10 % of that peak — still disarmed
    assert st.armed is False and st.stop_pct() is None

    assert st.update(103.0)[0] is False  # peak +3 % clears the 2 % floor
    assert st.armed is True
    assert abs(st.stop_pct() - 2.7) < 1e-9  # 3 % x (1 - 10 %)
    assert st.update(102.8)[0] is False  # +2.8 % > stop — holds
    # The boundary itself, built FROM the stop so no float noise decides the pin:
    # a hair above holds, a hair below closes.
    stop = st.stop_pct()
    assert st.update(100.0 * (1.0 + (stop + 1e-6) / 100.0))[0] is False
    assert st.update(100.0 * (1.0 + (stop - 1e-6) / 100.0))[0] is True

    # SHORT is the mirror image: the mark rises as the price falls.
    sh = TrailingState(entry=100.0, is_long=False, retrace_frac=0.10, activation=2.0)
    assert sh.update(99.5)[0] is False  # peak +0.5 %, disarmed
    assert sh.update(97.0)[0] is False  # peak +3 %, armed, stop at +2.7 %
    assert abs(sh.stop_pct() - 2.7) < 1e-9
    assert sh.update(97.25)[0] is False  # +2.75 % — holds
    assert sh.update(97.35)[0] is True  # +2.65 % — closes


def test_underwater_trade_is_never_trailed_out():
    """A trade that was never in profit must ride to its source exit."""
    st = TrailingState(entry=100.0, is_long=True, retrace_frac=0.10, activation=2.0)
    for p in (99.0, 97.0, 98.0, 90.0):
        assert st.update(p)[0] is False
    assert st.armed is False


def test_live_state_matches_wave_exit_sim():
    """AK6 (Regel 7). The live state machine and the study's batch function are two
    SHAPES of one rule. Same mark series in, same trigger index out."""
    rng = np.random.default_rng(9050)
    for _ in range(300):
        entry = 100.0
        is_long = bool(rng.integers(0, 2))
        n = int(rng.integers(1, 80))
        prices = entry * (1.0 + rng.normal(scale=0.03, size=n))
        marks = np.array([mark_pct(entry, float(p), is_long) for p in prices])
        for x in (0.10, 0.25):
            for act in (0.0, 2.0, 5.0):
                st = TrailingState(entry, is_long, x, act)
                live = None
                for i, p in enumerate(prices):
                    if st.update(float(p))[0]:
                        live = i
                        break
                batch = trailing_tp_trigger(marks, x, act)
                assert live == batch, f"x={x} act={act}: live {live} != batch {batch}"


def test_peak_survives_restart():
    """AK7. Rebuilding the peak from the current mark would re-arm the trail below a
    peak the trade already gave back — the evaporated winner would never close."""
    st = TrailingState(100.0, True, 0.10, 2.0)
    st.update(110.0)  # peak +10 %, stop at +9 %
    saved = st.peak_pct
    assert saved == 10.0

    revived = TrailingState(100.0, True, 0.10, 2.0, peak_pct=saved)
    assert revived.update(109.0)[0] is True  # remembers the peak → closes

    amnesiac = TrailingState(100.0, True, 0.10, 2.0, peak_pct=NO_PEAK)
    assert amnesiac.update(109.0)[0] is False  # forgot it → never closes. The bug.


def test_only_new_highs_are_worth_persisting():
    """The peak is monotone, so the write rate is a handful per position, not one
    per poll per position (500 positions x 6 polls/min would be the alternative)."""
    st = TrailingState(100.0, True, 0.10, 2.0)
    advances = [st.update(p)[2] for p in (101.0, 100.5, 100.8, 102.0, 101.9)]
    assert advances == [True, False, False, True, False]


# ─────────────────────────────────────────────────────────────────────────────
# AK8 — mirroring the source close
# ─────────────────────────────────────────────────────────────────────────────


def test_source_close_mirrors_into_a_close():
    """AK8. When the fleet closes the trade (SL/TP/timeout), the mirror must close
    too — otherwise the A/B arm stops measuring the same trades."""
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, 5.0, True, None, None, None, None, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    with mock.patch.object(bot, "get_live_prices_batch", return_value={"BTCUSDT": 104.0}):
        bot.poll_open_mirrors(conn, sources={}, mirrors=mirrors)  # source row gone
    assert conn.store["outbox"][0] == (CHANNEL, "Close BTCUSDT")
    assert any("close_reason" in s.lower() or "closed_at" in s.lower() for s, _ in conn.store["sql"])
    assert any(p and bot.REASON_SOURCE_CLOSED in p for _, p in conn.store["sql"] if p)


def test_trailing_trigger_closes_the_mirror():
    """The bot's one own decision: peak +10 %, give back to +9 % → close."""
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, 10.0, True, None, None, None, None, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    src = {42: {"symbol": "BTCUSDT"}}
    with mock.patch.object(bot, "get_live_prices_batch", return_value={"BTCUSDT": 108.9}):
        bot.poll_open_mirrors(conn, sources=src, mirrors=mirrors)
    assert conn.store["outbox"][0] == (CHANNEL, "Close BTCUSDT")
    assert any(p and bot.REASON_TRAIL in p for _, p in conn.store["sql"] if p)


def test_time_stop_closes_only_stale_unarmed_mirrors():
    """T-052 loss bound. A mirror that never cleared the activation and is older
    than TIME_STOP_H closes at the market; an ARMED mirror of the same age belongs
    to the trail, a YOUNG unarmed one keeps its chance."""
    stale = (1, 41, "AAAUSDT", "BR4H", "LONG", 100.0, 1.0, True, None, None, _recent(60 * 25), 90.0, None)
    armed = (2, 42, "BBBUSDT", "BR4H", "LONG", 100.0, 3.0, True, None, None, _recent(60 * 25), 90.0, None)
    young = (3, 43, "CCCUSDT", "BR4H", "LONG", 100.0, 1.0, True, None, None, _recent(60), 90.0, None)
    conn = FakeConn(mirrors=[stale, armed, young])
    mirrors = bot.read_open_mirrors(conn)
    src = {41: {"symbol": "AAAUSDT"}, 42: {"symbol": "BBBUSDT"}, 43: {"symbol": "CCCUSDT"}}
    prices = {"AAAUSDT": 99.0, "BBBUSDT": 102.9, "CCCUSDT": 99.0}
    with (
        mock.patch.object(bot, "TIME_STOP_SINCE", _ts_since(1000)),
        mock.patch.object(bot, "get_live_prices_batch", return_value=prices),
    ):
        bot.poll_open_mirrors(conn, sources=src, mirrors=mirrors)
    assert conn.store["outbox"][0] == (CHANNEL, "Close AAAUSDT")
    closes = [p for sql, p in conn.store["sql"] if sql.startswith("UPDATE trailing_positions SET closed_at")]
    assert len(closes) == 1 and closes[0][0] == bot.REASON_TIME_STOP, closes
    # The armed one stays with the trail (mark +2.9 > stop 2.7), the young one lives.
    assert not any("BBBUSDT" in str(m) or "CCCUSDT" in str(m) for m in conn.store["outbox"])


def test_arming_at_the_deadline_poll_beats_the_time_stop():
    """Boundary semantics, deliberately: a stale mirror whose CURRENT price arms it
    in the same poll is a winner now — it goes to the trail, not the time-stop
    ('der Zeit-Stop trifft nie einen scharfen Spiegel'). Causality is preserved:
    the decision uses only the peak as of THIS price, never a future one — the
    look-ahead the study retracted was arming from prices hours ahead (Nachtrag 4),
    not one poll of granularity."""
    row = (1, 41, "AAAUSDT", "BR4H", "LONG", 100.0, 1.5, True, None, None, _recent(60 * 25), 90.0, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    with (
        mock.patch.object(bot, "TIME_STOP_SINCE", _ts_since(1000)),
        mock.patch.object(bot, "get_live_prices_batch", return_value={"AAAUSDT": 103.0}),
    ):
        bot.poll_open_mirrors(conn, sources={41: {"symbol": "AAAUSDT"}}, mirrors=mirrors)
    closes = [p for sql, p in conn.store["sql"] if sql.startswith("UPDATE trailing_positions SET closed_at")]
    assert closes == [], closes  # no close of any kind — the trail owns it now
    peaks = [s for s, _ in conn.store["sql"] if s.startswith("UPDATE trailing_positions SET peak_pct")]
    assert peaks, "the new peak must be persisted (restart safety)"


def test_time_stop_wave_is_rate_limited():
    """After a restart over an old book, hundreds of stale mirrors qualify at once.
    At most TIME_STOP_MAX_PER_CYCLE close per cycle — the Telegram outbox is FIFO
    for the whole fleet, a 150-message burst would stall everyone else's posts."""
    rows = [
        (i, 100 + i, f"C{i}USDT", "BR4H", "LONG", 100.0, 1.0, True, None, None, _recent(60 * 30), 90.0, None)
        for i in range(bot.TIME_STOP_MAX_PER_CYCLE + 10)
    ]
    conn = FakeConn(mirrors=rows)
    mirrors = bot.read_open_mirrors(conn)
    src = {100 + i: {"symbol": f"C{i}USDT"} for i in range(len(rows))}
    prices = {f"C{i}USDT": 99.0 for i in range(len(rows))}
    with (
        mock.patch.object(bot, "TIME_STOP_SINCE", _ts_since(1000)),
        mock.patch.object(bot, "get_live_prices_batch", return_value=prices),
    ):
        bot.poll_open_mirrors(conn, sources=src, mirrors=mirrors)
    closes = [p for sql, p in conn.store["sql"] if sql.startswith("UPDATE trailing_positions SET closed_at")]
    assert len(closes) == bot.TIME_STOP_MAX_PER_CYCLE, len(closes)


def _ts_since(hours_ago: float):
    """Patch target for TIME_STOP_SINCE — the real cutoff is 'deploy day', so any
    >24h-old test mirror would be grandfathered by accident; the time-stop pins pin
    the RULE, not the calendar."""
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours_ago)


def test_grandfathered_legacy_book_rides_past_the_time_stop():
    """Operator decision (Michi, 2026-07-28): mirrors opened BEFORE the cutoff ride
    to their natural SL/TP at the operator's explicit risk — the time-stop governs
    only the new regime. The cutoff is a fixed timestamp, not 'process start', so a
    later restart cannot silently exempt a fresh cohort."""
    legacy = (1, 41, "AAAUSDT", "BR4H", "LONG", 100.0, 1.0, True, None, None, _recent(60 * 72), 90.0, None)
    fresh_stale = (2, 42, "BBBUSDT", "BR4H", "LONG", 100.0, 1.0, True, None, None, _recent(60 * 25), 90.0, None)
    conn = FakeConn(mirrors=[legacy, fresh_stale])
    mirrors = bot.read_open_mirrors(conn)
    src = {41: {"symbol": "AAAUSDT"}, 42: {"symbol": "BBBUSDT"}}
    with (
        mock.patch.object(bot, "TIME_STOP_SINCE", _ts_since(48)),  # cutoff between the two
        mock.patch.object(bot, "get_live_prices_batch", return_value={"AAAUSDT": 99.0, "BBBUSDT": 99.0}),
    ):
        bot.poll_open_mirrors(conn, sources=src, mirrors=mirrors)
    closes = [p for sql, p in conn.store["sql"] if sql.startswith("UPDATE trailing_positions SET closed_at")]
    assert len(closes) == 1 and closes[0][2] == 2, closes  # only the post-cutoff mirror
    assert conn.store["outbox"][0] == (CHANNEL, "Close BBBUSDT")


def test_exposure_cap_refuses_only_the_stretching_side():
    """T-052 structural bound: with the book already EXPOSURE_CAP longs ahead, a
    new LONG is refused with its own reason while a SHORT is still admitted."""
    full = {"LONG": bot.EXPOSURE_CAP, "SHORT": 0}
    admitted, rejected = bot.admit([_cand(1, "AAAUSDT", "BR4H", "LONG")], set(), 500, open_by_dir=dict(full))
    assert admitted == [] and rejected[0][2] == "EXPOSURE_CAP"
    admitted, rejected = bot.admit([_cand(2, "BBBUSDT", "MIS1-8h", "SHORT")], set(), 500, open_by_dir=dict(full))
    assert [sid for sid, _ in admitted] == [2] and rejected == []
    # The cap is a NET bound: an admitted SHORT reduces the imbalance, so a LONG
    # arriving in the same cycle fits again — that is intended, not a leak.
    both = [_cand(1, "AAAUSDT", "BR4H", "LONG"), _cand(2, "BBBUSDT", "MIS1-8h", "SHORT")]
    admitted, _ = bot.admit(both, set(), 500, open_by_dir=dict(full))
    assert {sid for sid, _ in admitted} == {1, 2}


def test_exposure_cap_counts_admissions_within_the_cycle():
    """A single cycle full of one-sided candidates must not overrun the cap: the
    counter advances with every admission, not only with the DB state."""
    cands = [_cand(i, f"C{i}USDT", "BR4H", "LONG") for i in range(bot.EXPOSURE_CAP + 10)]
    admitted, rejected = bot.admit(cands, held_symbols=set(), free_slots=500)
    assert len(admitted) == bot.EXPOSURE_CAP
    assert {why for _sid, _sig, why in rejected} == {"EXPOSURE_CAP"}


def test_no_price_means_no_decision():
    """A coin without a tick keeps its position — closing it would be a statement
    about a market we cannot see."""
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, 10.0, True, None, None, None, None, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    with mock.patch.object(bot, "get_live_prices_batch", return_value={}):
        bot.poll_open_mirrors(conn, sources={42: {"symbol": "BTCUSDT"}}, mirrors=mirrors)
    assert conn.store["outbox"] == []


def test_a_failed_batch_never_falls_back_to_per_coin_calls():
    """Ban guard (operator, Michi 2026-07-26). core.live_price.get_live_price makes
    ONE HTTP call per symbol; at the ~285 concurrent positions this operating point
    expects, a 10s poll would be ~28 req/s against fapi.binance.com. A ban costs the
    whole fleet; a trailing exit delayed by one poll costs almost nothing."""
    assert not hasattr(bot, "get_live_price"), "per-coin fallback must not be reachable from the bot"

    src = open(os.path.join(ROOT, "40_trailing_close_bot.py"), encoding="utf-8").read()
    code = chr(10).join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "get_live_price(" not in code, "no per-symbol price call may remain in the bot"

    # And behaviourally: a dead batch produces no calls and no decisions at all.
    rows = [
        (i, 40 + i, f"C{i}USDT", "BR4H", "LONG", 100.0, 10.0, True, None, None, None, None, None) for i in range(30)
    ]
    conn = FakeConn(mirrors=rows)
    mirrors = bot.read_open_mirrors(conn)
    with mock.patch.object(bot, "get_live_prices_batch", return_value={}):
        bot.poll_open_mirrors(conn, sources={40 + i: {"symbol": f"C{i}USDT"} for i in range(30)}, mirrors=mirrors)
    assert conn.store["outbox"] == []
    assert not [s for s, _ in conn.store["sql"] if s.startswith("UPDATE trailing_positions SET peak_pct")]


def test_source_gone_without_a_price_closes_with_an_unknown_mark():
    """The source no longer holds it, so holding is wrong — but the ledger must not
    claim a mark nobody measured."""
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, 5.0, True, None, None, None, None, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    with mock.patch.object(bot, "get_live_prices_batch", return_value={}):
        bot.poll_open_mirrors(conn, sources={}, mirrors=mirrors, all_open=set())
    assert conn.store["outbox"][0] == (CHANNEL, "Close BTCUSDT")
    closes = [p for sql, p in conn.store["sql"] if sql.startswith("UPDATE trailing_positions SET closed_at")]
    assert closes and closes[0][1] is None, closes  # close_mark_pct stays NULL


# ─────────────────────────────────────────────────────────────────────────────
# AK9 / AK10 — the Cornix contract
# ─────────────────────────────────────────────────────────────────────────────


CORNIX_MARKERS = ("🚨 Direction:", "🏦 CMP Entry:", "💸 Stop Loss:")


def test_exactly_one_parsable_message_per_entry():
    """AK9 (hard rule 4). A second parsable message is a second position."""
    sig = {
        "symbol": "BTCUSDT",
        "tag": "BR4H",
        "direction": "LONG",
        "entry": 100.0,
        "sl": 95.0,
        "targets": [110.0, 120.0],
        "lev": "20x",
        "density": 0.959,
    }
    cornix, info = bot.entry_messages(sig)
    assert all(m in cornix for m in CORNIX_MARKERS)
    assert not any(m in info for m in CORNIX_MARKERS), info
    assert "Signal for" not in info


def test_close_command_is_not_parsable_as_an_entry():
    row = {"symbol": "BTCUSDT", "model": "BR4H", "direction": "LONG", "id": 1, "posted": True}
    cmd, info = bot.close_messages(row, bot.REASON_TRAIL, -1.5)
    assert cmd == "Close BTCUSDT"
    for msg in (cmd, info):
        assert not any(m in msg for m in CORNIX_MARKERS), msg


def test_cornix_block_is_shared():
    """AK10 (Regel 7). ONE builder. The literal below is the pre-extraction output of
    core.signal_post.post_ai_signal — byte-identical, entry2 line already absent
    (PR #197). A private copy in the bot is how that removal reaches one publisher
    and not the other."""
    got = build_cornix_block(
        model_tag="BR4H-TRAIL",
        symbol="BTCUSDT",
        direction="LONG",
        lev="20x",
        entry1=61234.5,
        sl=58000.0,
        targets=[62000.0, 63000.0, 64000.0, 65000.0],
    )
    assert got == (
        "📈 Signal for BTCUSDT 📈\n"
        "🚨 Direction: LONG\n"
        "🚨 Leverage: 20x\n"
        "🚨 Margin: Cross\n"
        "🏦 CMP Entry: $ 61234.5000\n"
        "💰 TP1: $ 62000.0000\n"
        "💰 TP2: $ 63000.0000\n"
        "💰 TP3: $ 64000.0000\n"
        "💸 Stop Loss: $ 58000.0000\n"
        "🧠 Trade idea generated by AI module BR4H-TRAIL"
    ), got
    assert "Entry 2" not in got  # PR #197 — single-entry, arm B
    assert got.count("CMP Entry") == 1


def test_post_ai_signal_still_publishes_exactly_the_shared_block():
    """The extraction must not have moved the fleet's own money path by one byte."""
    from core import signal_post

    conn = FakeConn()
    with (
        mock.patch.object(signal_post, "get_max_leverage", return_value="20x"),
        mock.patch.object(signal_post, "generate_minichart_image", return_value=None),
    ):
        signal_post.post_ai_signal(
            conn,
            CHANNEL,
            "BR4H",
            "BTCUSDT",
            "LONG",
            0.8,
            entry1=61234.5,
            entry2=60000.0,
            sl=58000.0,
            targets=[62000.0, 63000.0, 64000.0, 65000.0],
            source_desc="pin",
        )
    published = conn.store["outbox"][0][1]
    assert published == build_cornix_block(
        "BR4H",
        "BTCUSDT",
        "LONG",
        "20x",
        61234.5,
        58000.0,
        [62000.0, 63000.0, 64000.0, 65000.0],
    )


# ─────────────────────────────────────────────────────────────────────────────
# AK11 / AK12 — safety nets
# ─────────────────────────────────────────────────────────────────────────────


def test_default_is_shadow_only():
    """AK11. A deploy alone must not post. BOTH the gate and the channel are needed."""
    shadow = _load_bot(live_posting=False)
    assert shadow.LIVE_POSTING is False
    assert shadow.TARGET_CHANNELS == (0,)  # the "no real channel" sentinel
    assert shadow.POSTING_ENABLED is False

    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG")])
    sources, _ = shadow.read_source_signals(conn)
    assert shadow.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 101.0}) == 1  # tracked …
    assert conn.store["outbox"] == []  # … but nothing published
    assert any("INSERT INTO trailing_positions" in s for s, _ in conn.store["sql"])


def test_live_gate_open_publishes_two_messages():
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG")])
    bot.open_mirrors(conn, bot.read_source_signals(conn)[0], {}, set(), prices={"BTCUSDT": 101.0})
    assert len(conn.store["outbox"]) == 2  # cornix + info, nothing more
    assert all(row[0] == CHANNEL for row in conn.store["outbox"])


def test_bot_never_writes_ai_signals():
    """AK12. The bot reads the AI monitor's table and never writes it — a second
    writer there would fight Bot 8 over real positions."""
    src = open(os.path.join(ROOT, "40_trailing_close_bot.py"), encoding="utf-8").read()
    for verb in ("INSERT INTO ai_signals", "UPDATE ai_signals", "DELETE FROM ai_signals"):
        assert verb not in src, verb
    # And at runtime: a full open+poll cycle touches only its own tables + outbox.
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG")])
    bot.open_mirrors(conn, bot.read_source_signals(conn)[0], {}, set(), prices={"BTCUSDT": 101.0})
    writes = [s for s, _ in conn.store["sql"] if s.split()[0] in ("INSERT", "UPDATE", "DELETE")]
    assert writes and all(("trailing_positions" in w or "telegram_outbox" in w) for w in writes), writes


# ─────────────────────────────────────────────────────────────────────────────
# Re-entry — the bug the first cut of this bot had, and the reason the pins for it
# exist: the trailing exit normally fires while the SOURCE trade is still open
# (that is the entire point). Checked against only the OPEN mirrors, that same
# ai_signals row looks new again on the very next poll.
# ─────────────────────────────────────────────────────────────────────────────


def test_a_trailed_out_trade_is_never_re_entered():
    """The 10-second re-entry carousel. With Cornix attached this is real money:
    the bot would re-open the position on every poll until the fleet closes the
    source trade, hours or days later."""
    src = _src_row(42, "BTCUSDT", "BR4H", "LONG")
    conn = FakeConn(ai_signals=[src])
    sources, _ = bot.read_source_signals(conn)

    # The mirror was opened and has already been trailed out: no OPEN mirror row
    # remains, but trailing_positions still knows the source id.
    already = {42}
    assert bot.open_mirrors(conn, sources, {}, already, prices={"BTCUSDT": 101.0}) == 0
    assert conn.store["outbox"] == [], conn.store["outbox"]


def test_insert_losing_the_race_publishes_nothing():
    """Write first, publish only on a real insert (the P2.8 pattern). A post whose
    row lost the unique index is a position nobody will ever close."""
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG")])
    sources, _ = bot.read_source_signals(conn)

    real_cursor = conn.cursor

    class LosingCursor(FakeCursor):
        def fetchone(self):
            return None  # ON CONFLICT DO NOTHING RETURNING id -> no row

    conn.cursor = lambda: LosingCursor(conn.store)
    try:
        assert bot.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 101.0}) == 0
    finally:
        conn.cursor = real_cursor
    assert conn.store["outbox"] == []


def test_retired_leg_closes_with_its_own_reason():
    """A leg that leaves the register while its trade runs still ends the mirror —
    but it is not the same event as the fleet closing the trade, and the ledger
    must be able to tell them apart."""
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, 5.0, True, None, None, None, None, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    with mock.patch.object(bot, "get_live_prices_batch", return_value={"BTCUSDT": 104.0}):
        # filtered out of `sources`, but still an OPEN ai_signals row
        bot.poll_open_mirrors(conn, sources={}, mirrors=mirrors, all_open={42})
    assert any(p and bot.REASON_LEG_RETIRED in p for _, p in conn.store["sql"] if p)


# ─────────────────────────────────────────────────────────────────────────────
# Pre-existing source trades — found in the FIRST live shadow run (2026-07-26):
# the bot mirrored all 465 already-open trades on start, some of them days old.
# In shadow that was free; with the gate open it would have been 465 entries
# published at once, whose geometry belongs to a price the market left long ago.
# Same class as P2.7 in the AI monitor ("no retroactive scoring of old trades
# after a process restart").
# ─────────────────────────────────────────────────────────────────────────────


def test_already_running_trades_are_recorded_not_mirrored():
    """An old trade is remembered so it is never mirrored — and never re-considered."""
    conn = FakeConn(
        ai_signals=[
            _src_row(1, "BTCUSDT", "BR4H", "LONG", age_sec=240000.0),  # ~3 days old
            _src_row(2, "ETHUSDT", "RUB1", "LONG", age_sec=5.0),  # opened just now
        ]
    )
    sources, _ = bot.read_source_signals(conn)
    assert (
        bot.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 101.0, "ETHUSDT": 101.0}) == 1
    )  # only the fresh one

    published = [m for _ch, m in conn.store["outbox"]]
    assert not any("BTCUSDT" in m for m in published), published
    assert any("ETHUSDT" in m for m in published)

    # The old one got a closed PREEXISTING row — the same lock that stops a
    # trailed-out trade from being re-entered.
    marks = [p for _sql, p in conn.store["sql"] if p and bot.REASON_PREEXISTING in str(p)]
    assert marks, conn.store["sql"]


def test_the_age_cutoff_keeps_the_decision_current():
    """The window protects the relevance of the decision, sized to REALITY: the
    fleet's signal→ai_signals insert latency is 30–120 s (median 95 s, measured
    2026-07-29), so a 95-second-old row is a FRESH signal, not a stale one — the
    30 s window silently discarded ~85 % of real roster signals; the candle-cycle
    legs (the LONG side) sit deterministically at ~185–195 s, a wall just past
    180. 240 s covers both leg families; a signal beyond that is a genuinely old
    trade and stays out (operator decision Michi, supersedes the T-051 30 s)."""
    assert bot.MAX_MIRROR_AGE_SEC <= 240.0, bot.MAX_MIRROR_AGE_SEC
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG", age_sec=10.0)])
    sources, _ = bot.read_source_signals(conn)
    assert bot.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 101.0}) == 1

    # 95 s = tick-leg median insert latency; 190 s = the candle-cycle wall that
    # rejected 139 longs in 6 h under the 180 s window. Both must admit.
    for sid, sym, age in ((2, "ETHUSDT", 95.0), (3, "ADAUSDT", 190.0)):
        lagged = FakeConn(ai_signals=[_src_row(sid, sym, "BR4H", "LONG", age_sec=age)])
        src2, _ = bot.read_source_signals(lagged)
        assert bot.open_mirrors(lagged, src2, {}, set(), prices={sym: 101.0}) == 1, age

    stale = FakeConn(ai_signals=[_src_row(3, "SOLUSDT", "BR4H", "LONG", age_sec=600.0)])
    src3, _ = bot.read_source_signals(stale)
    assert bot.open_mirrors(stale, src3, {}, set(), prices={"SOLUSDT": 101.0}) == 0


def test_missing_open_time_counts_as_old():
    """A NULL open_time is unknowable age — in doubt, do not mirror."""
    row = (1, "BTCUSDT", "BR4H", "LONG", 100.0, 100.0, 95.0, "[110.0]", "20x", None)
    conn = FakeConn(ai_signals=[row])
    sources, _ = bot.read_source_signals(conn)
    assert sources[1]["age_sec"] == float("inf")
    assert bot.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 101.0}) == 0
    assert conn.store["outbox"] == []


def test_rejections_are_summarised_not_logged_per_item():
    """~870 rejections per 10s cycle would be ~1.5M log lines a day into the SHARED
    watchdog log (measured in the first shadow run). The counts stay visible; the
    per-item detail drops to DEBUG."""
    import logging as _logging

    records = []

    class Grab(_logging.Handler):
        def emit(self, r):
            records.append(r)

    h = Grab()
    bot.logger.addHandler(h)
    bot.logger.setLevel(_logging.INFO)
    try:
        conn = FakeConn()
        sources = {
            i: {
                "symbol": "BTCUSDT",
                "model": "BR4H",
                "tag": "BR4H",
                "direction": "LONG",
                "entry": 100.0,
                "sl": 95.0,
                "targets": [110.0],
                "lev": "20x",
                "density": 0.959,
                "age_sec": 5.0,
            }
            for i in range(50)
        }
        bot.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 101.0})
    finally:
        bot.logger.removeHandler(h)
    infos = [r for r in records if r.levelno >= _logging.INFO and "nicht aufgenommen" in r.getMessage()]
    assert len(infos) == 1, [r.getMessage() for r in infos]
    assert "49" in infos[0].getMessage(), infos[0].getMessage()


# ─────────────────────────────────────────────────────────────────────────────
# Shadow -> live carryover. On 2026-07-26 the switch would have started with 460
# open mirror rows that were never published: 460 symbols and 460 slots blocked
# for positions the channel does not contain.
# ─────────────────────────────────────────────────────────────────────────────


def test_unposted_rows_are_cleared_when_going_live():
    """An open row that was never published cannot be a channel position."""
    conn = FakeConn()
    n = bot.clear_unposted_carryover(conn)
    updates = [(sql, p) for sql, p in conn.store["sql"] if sql.startswith("UPDATE trailing_positions")]
    assert updates, conn.store["sql"]
    sql, params = updates[0]
    assert "posted = FALSE" in sql and "closed_at IS NULL" in sql, sql
    assert params == (bot.REASON_SHADOW_CARRYOVER,)
    assert n == 0  # FakeCursor reports rowcount 0; the SQL shape is what matters here


def test_shadow_mode_keeps_its_book():
    """In shadow the unposted open rows ARE the book — clearing them would wipe it
    on every restart."""
    shadow = _load_bot(live_posting=False)
    conn = FakeConn()
    assert shadow.clear_unposted_carryover(conn) == 0
    assert not [s for s, _ in conn.store["sql"] if s.startswith("UPDATE trailing_positions")]


# ─────────────────────────────────────────────────────────────────────────────
# Symbol cooldown after a close. Measured live on 2026-07-27: XTZUSDT `Close` and
# a fresh entry in the SAME SECOND (SRA2 SHORT -> BR4H LONG), ENAUSDT 3s apart
# (ATS2 LONG -> MAX1 SHORT). The outbox delivers in order, but Cornix then fires
# two opposite market orders at Binance almost simultaneously.
# ─────────────────────────────────────────────────────────────────────────────


def test_a_symbol_with_a_close_in_flight_is_not_re_admitted():
    """The race we refuse to take: overtaking our own Close with a counter-order."""
    cands = [_cand(1, "XTZUSDT", "BR4H", "LONG")]
    admitted, rejected = bot.admit(cands, held_symbols=set(), free_slots=500, cooling={"XTZUSDT"})
    assert admitted == []
    assert [(sid, why) for sid, _, why in rejected] == [(1, "SYMBOL_COOLING")]

    # Once the window has passed the same symbol is admitted normally.
    admitted, rejected = bot.admit(cands, set(), 500, cooling=set())
    assert [sid for sid, _ in admitted] == [1] and rejected == []


def test_open_mirrors_actually_applies_the_cooldown():
    """Wiring pin. `admit` and `read_cooling_symbols` are both correct in isolation;
    this is the one that fails if open_mirrors forgets to pass the cooling set —
    the exact defect a mutation test exposed when the pins tested only the parts."""
    conn = FakeConn(ai_signals=[_src_row(1, "XTZUSDT", "BR4H", "LONG")])
    conn.store["cooling"] = ["XTZUSDT"]
    sources, _ = bot.read_source_signals(conn)
    assert bot.open_mirrors(conn, sources, {}, set(), prices={"XTZUSDT": 101.0}) == 0
    assert conn.store["outbox"] == [], conn.store["outbox"]

    conn.store["cooling"] = []
    assert bot.open_mirrors(conn, sources, {}, set(), prices={"XTZUSDT": 101.0}) == 1


def test_cooling_only_counts_posted_closes():
    """A shadow close published no command, so it cannot race with anything."""
    conn = FakeConn()
    bot.read_cooling_symbols(conn)
    sql = [s for s, _ in conn.store["sql"] if "trailing_positions" in s]
    assert sql, conn.store["sql"]
    assert "posted" in sql[0] and "closed_at IS NOT NULL" in sql[0], sql[0]
    # The window is computed by the DB against its own NOW(), not in Python (R3).
    assert "NOW()" in sql[0] and "make_interval" in sql[0], sql[0]


def test_cooldown_is_long_enough_to_outlive_a_poll():
    """A window shorter than the poll interval could not block anything: the next
    cycle would arrive after it had already expired."""
    assert bot.SYMBOL_COOLDOWN_SEC >= bot.POLL_SECONDS, (bot.SYMBOL_COOLDOWN_SEC, bot.POLL_SECONDS)


def test_symbol_held_still_wins_over_cooling():
    """An already-open position is the stronger reason — the operator reading the
    log should see the real cause, not the milder one."""
    admitted, rejected = bot.admit([_cand(1, "BTCUSDT", "BR4H", "LONG")], {"BTCUSDT"}, 500, cooling={"BTCUSDT"})
    assert admitted == [] and rejected[0][2] == "SYMBOL_HELD"


# ─────────────────────────────────────────────────────────────────────────────
# Re-entry lock (T-2026-KYT-9050-115). The src_signal_id lock recognises only the
# SAME ai_signals row; a re-forwarding leg writes the same underlying trade under a
# new id and walks past it. The symbol is the only identity both rows share.
# ─────────────────────────────────────────────────────────────────────────────


def test_a_symbol_the_trail_just_exited_is_not_re_entered():
    cands = [_cand(1, "XTZUSDT", "BR4H", "LONG")]
    admitted, rejected = bot.admit(cands, held_symbols=set(), free_slots=500, locked={"XTZUSDT"})
    assert admitted == []
    assert [(sid, why) for sid, _, why in rejected] == [(1, "SYMBOL_REENTRY_LOCK")]

    admitted, rejected = bot.admit(cands, set(), 500, locked=set())
    assert [sid for sid, _ in admitted] == [1] and rejected == []


def test_re_entry_lock_reports_itself_rather_than_the_milder_cooldown():
    """A just-trailed symbol trips both windows. The log has to name the one that
    will still be blocking in an hour, not the one that expires in a minute."""
    admitted, rejected = bot.admit(
        [_cand(1, "XTZUSDT", "BR4H", "LONG")], set(), 500, cooling={"XTZUSDT"}, locked={"XTZUSDT"}
    )
    assert admitted == [] and rejected[0][2] == "SYMBOL_REENTRY_LOCK"


def test_open_mirrors_actually_applies_the_re_entry_lock():
    """Wiring pin, same shape as the cooldown one: `admit` and
    `read_reentry_locked_symbols` are each correct alone, and this is the assertion
    that fails if open_mirrors forgets to pass the locked set through."""
    conn = FakeConn(ai_signals=[_src_row(1, "XTZUSDT", "BR4H", "LONG")])
    conn.store["locked"] = ["XTZUSDT"]
    sources, _ = bot.read_source_signals(conn)
    assert bot.open_mirrors(conn, sources, {}, set(), prices={"XTZUSDT": 101.0}) == 0
    assert conn.store["outbox"] == [], conn.store["outbox"]

    conn.store["locked"] = []
    assert bot.open_mirrors(conn, sources, {}, set(), prices={"XTZUSDT": 101.0}) == 1


def test_only_the_trails_own_exits_arm_the_lock():
    """SL_HIT and SOURCE_CLOSED mean the underlying trade is over — there is
    nothing left to re-enter, and locking on them would cost entries without
    closing a hole. ENTRY_NOT_FILLED and SHADOW_CARRYOVER never held a position.

    Pinned as a set rather than by reading the query, so widening it stays a
    deliberate test edit."""
    assert set(bot.REENTRY_LOCKING_REASONS) == {bot.REASON_TRAIL, bot.REASON_TIME_STOP}
    for reason in (bot.REASON_SL_HIT, bot.REASON_SOURCE_CLOSED, bot.REASON_NOT_FILLED, bot.REASON_PREEXISTING):
        assert reason not in bot.REENTRY_LOCKING_REASONS


def test_re_entry_lock_query_is_reason_scoped_and_ignores_posted():
    """Two properties the SQL must have, and one it must NOT.

    Reason-scoped: an unscoped query would lock every closed symbol for an hour.
    Shadow rows count: `posted` answers "could a Cornix order collide", which is the
    COOLDOWN's question. This lock asks whether our book already traded and left the
    position, and a shadow mirror did exactly that.
    """
    conn = FakeConn()
    bot.read_reentry_locked_symbols(conn)
    sql = [s for s, _ in conn.store["sql"] if "trailing_positions" in s]
    assert sql, conn.store["sql"]
    assert "close_reason = ANY" in sql[0], sql[0]
    assert "posted" not in sql[0], sql[0]
    # Window computed by the DB against its own NOW() (TZ contract R3), in seconds
    # so a fractional REENTRY_LOCK_H survives — make_interval(hours => …) floors.
    assert "NOW()" in sql[0] and "make_interval(secs =>" in sql[0], sql[0]


def test_re_entry_lock_outlives_the_window_a_re_forward_can_arrive_in():
    """The lock only closes the hole if it is still standing when the re-forwarded
    row shows up. That row can be admitted up to MAX_MIRROR_AGE_SEC after its own
    open_time, so anything shorter leaves the gap open."""
    assert bot.REENTRY_LOCK_H * 3600 > bot.MAX_MIRROR_AGE_SEC, (bot.REENTRY_LOCK_H, bot.MAX_MIRROR_AGE_SEC)
    assert bot.REENTRY_LOCK_H * 3600 > bot.SYMBOL_COOLDOWN_SEC, "the cooldown alone was the hole"


# ─────────────────────────────────────────────────────────────────────────────
# Fill tracking. Operator finding (Michi, 2026-07-27): Cornix had not opened some
# trades because the entry was never reached — and the bot had already sent the
# Close. ENAUSDT MAX1 SHORT was posted at 0.08867 with the market at 0.09000, so
# Cornix waited for a fall that never came while our book trailed it out.
# ─────────────────────────────────────────────────────────────────────────────


def test_fill_needs_the_market_to_reach_the_entry():
    """Direction-agnostic on purpose: the pin asserts only that the price must touch
    the entry from the side it was on when we mirrored — it makes no claim about how
    Cornix treats LONG vs SHORT, which we cannot verify from here."""
    # Market ABOVE the entry when mirrored -> must come down.
    assert bot.has_filled(entry=100.0, mirror_price=101.5, price=101.0) is False
    assert bot.has_filled(entry=100.0, mirror_price=101.5, price=100.0) is True
    # Market BELOW the entry when mirrored -> must come up.
    assert bot.has_filled(entry=100.0, mirror_price=98.5, price=99.0) is False
    assert bot.has_filled(entry=100.0, mirror_price=98.5, price=100.0) is True
    # Mirrored exactly at the entry -> already there.
    assert bot.has_filled(entry=100.0, mirror_price=100.0, price=100.0) is True

    # The ENAUSDT case, with its real numbers.
    assert bot.has_filled(entry=0.08867, mirror_price=0.09000, price=0.09000) is False


def test_an_unfilled_mirror_is_never_trailed():
    """The phantom exit this task removes: no fill means Cornix holds nothing, so
    there is nothing to trail and nothing to close."""
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, 10.0, True, None, 101.5, _recent(), None, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    assert mirrors[42]["filled"] is False
    # A price that would trigger the trail on a FILLED position …
    bot.poll_open_mirrors(conn, {42: {"symbol": "BTCUSDT"}}, mirrors, prices={"BTCUSDT": 108.9})
    assert conn.store["outbox"] == [], conn.store["outbox"]  # … publishes nothing
    assert not [s for s, _ in conn.store["sql"] if "closed_at = NOW()" in s]


def test_reaching_the_entry_marks_the_fill_and_then_trailing_starts():
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, None, True, None, 101.5, _recent(), None, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    bot.poll_open_mirrors(conn, {42: {"symbol": "BTCUSDT"}}, mirrors, prices={"BTCUSDT": 100.0})
    assert any("filled_at = NOW()" in s for s, _ in conn.store["sql"]), conn.store["sql"]
    assert mirrors[42]["filled"] is True


def test_an_entry_never_reached_expires_instead_of_hanging():
    """A pending order must not sit in Cornix for days and fill unattended."""
    old = _recent(minutes_ago=bot.FILL_TIMEOUT_MIN + 5)
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, None, True, None, 101.5, old, None, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    bot.poll_open_mirrors(conn, {42: {"symbol": "BTCUSDT"}}, mirrors, prices={"BTCUSDT": 101.4})
    assert conn.store["outbox"][0] == (CHANNEL, "Close BTCUSDT")
    assert any(p and bot.REASON_NOT_FILLED in str(p) for _s, p in conn.store["sql"] if p)


def test_legacy_rows_without_a_mirror_price_stay_filled():
    """Rows written before this change carry no mirror_price. Declaring ~100 live
    positions unfilled on a suspicion would silence the whole channel."""
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, 10.0, True, None, None, _recent(), None, None)
    conn = FakeConn(mirrors=[row])
    assert bot.read_open_mirrors(conn)[42]["filled"] is True


def test_a_new_mirror_records_the_market_price_it_was_posted_at():
    """Wiring pin — without mirror_price the fill check cannot know which side the
    entry has to be reached from, and every new row would silently count as filled."""
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG")])
    sources, _ = bot.read_source_signals(conn)
    assert bot.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 101.5}) == 1
    ins = [p for s, p in conn.store["sql"] if s.startswith("INSERT INTO trailing_positions")][0]
    assert 101.5 in ins, ins


def test_without_a_market_price_nothing_is_mirrored():
    """No mirror_price would mean no fill check — better to wait one poll."""
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG")])
    sources, _ = bot.read_source_signals(conn)
    assert bot.open_mirrors(conn, sources, {}, set(), prices={}) == 0
    assert conn.store["outbox"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Market entry (operator decision, Michi 2026-07-27). Measured first: of 24 mirrors
# only 5 filled (21 %); for 15 of the 18 cancellations the market had never touched
# the source entry (checked against 5m candles). The arm was trading a selection it
# created itself — trades whose move retraced. Entering at market fills essentially
# always and lets both arms trade the same signals.
# ─────────────────────────────────────────────────────────────────────────────


def test_the_mirror_enters_at_the_market_not_at_the_source_entry():
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG", entry=100.0)])
    sources, _ = bot.read_source_signals(conn)
    assert sources[1]["entry"] == 100.0  # source geometry unchanged …
    assert bot.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 101.0}) == 1

    ins = [p for sql, p in conn.store["sql"] if sql.startswith("INSERT INTO trailing_positions")][0]
    assert 101.0 in ins, ins  # … but WE enter at the market
    assert 100.0 not in ins, ins
    cornix = [m for _ch, m in conn.store["outbox"] if m.startswith("📈")][0]
    assert "CMP Entry: $ 101.0000" in cornix, cornix
    # SL and targets keep their absolute prices - they are S/R levels, and the SL must
    # stay the same disaster stop the hold arm carries.
    assert "Stop Loss: $ 95.0000" in cornix and "TP1: $ 110.0000" in cornix, cornix


def test_a_market_entry_counts_as_filled_immediately():
    """No fill wait for a market entry - the old wait is what produced the false
    ENTRY_NOT_FILLED cancellations (3 of 18 were wrong: the market HAD touched the
    entry, our 10s sampling just missed it while Cornix fills on any tick)."""
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG")])
    sources, _ = bot.read_source_signals(conn)
    bot.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 101.0})
    sql = [s for s, _ in conn.store["sql"] if s.startswith("INSERT INTO trailing_positions")][0]
    assert "filled_at" in sql and "NOW()" in sql, sql


def test_a_market_past_tp1_or_past_the_sl_is_not_mirrored():
    """Entering there is not a trade, it is a fee: instantly at target, or instantly
    stopped out."""
    assert bot.mirrorable_at("LONG", market=105.0, sl=95.0, targets=[110.0]) is True
    assert bot.mirrorable_at("LONG", market=111.0, sl=95.0, targets=[110.0]) is False  # past TP1
    assert bot.mirrorable_at("LONG", market=94.0, sl=95.0, targets=[110.0]) is False  # past SL
    assert bot.mirrorable_at("SHORT", market=95.0, sl=105.0, targets=[90.0]) is True
    assert bot.mirrorable_at("SHORT", market=89.0, sl=105.0, targets=[90.0]) is False  # past TP1
    assert bot.mirrorable_at("SHORT", market=106.0, sl=105.0, targets=[90.0]) is False  # past SL
    assert bot.mirrorable_at("LONG", market=100.0, sl=95.0, targets=[]) is False


def test_the_guard_is_wired_into_the_mirror_path():
    """Wiring pin. mirrorable_at can be perfect on its own and still never be called —
    that failure mode has bitten this bot twice in one session."""
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG")])  # sl 95, tp1 110
    sources, _ = bot.read_source_signals(conn)
    assert bot.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 115.0}) == 0  # past TP1
    assert conn.store["outbox"] == []
    assert bot.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 94.0}) == 0  # past SL
    assert conn.store["outbox"] == []


# ─────────────────────────────────────────────────────────────────────────────
# SL handling (operator, Michi 2026-07-27): Cornix holds the stop as an order on
# the exchange and closes by itself. We only book the exit — and deliberately post
# nothing, because a Close of ours would claim an exit we did not cause.
# ─────────────────────────────────────────────────────────────────────────────


def test_the_stop_is_recognised_in_both_directions():
    assert bot.sl_reached("LONG", 95.0, 94.9) is True
    assert bot.sl_reached("LONG", 95.0, 95.1) is False
    assert bot.sl_reached("SHORT", 105.0, 105.1) is True
    assert bot.sl_reached("SHORT", 105.0, 104.9) is False
    assert bot.sl_reached("LONG", None, 1.0) is False  # legacy row without an SL


def test_a_stopped_out_mirror_books_the_exit_without_posting():
    """Cornix already closed it — a Close of ours is at best redundant."""
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, 5.0, True, _recent(), 100.0, _recent(), 95.0, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    bot.poll_open_mirrors(conn, {42: {"symbol": "BTCUSDT"}}, mirrors, prices={"BTCUSDT": 94.0})
    assert conn.store["outbox"] == [], conn.store["outbox"]  # nothing published
    assert any(p and bot.REASON_SL_HIT in str(p) for _s, p in conn.store["sql"] if p)


def test_the_trail_still_posts_its_own_close():
    """Contrast pin: an exit WE cause must still be published, or Cornix keeps the
    position open forever."""
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, 10.0, True, _recent(), 100.0, _recent(), 95.0, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    bot.poll_open_mirrors(conn, {42: {"symbol": "BTCUSDT"}}, mirrors, prices={"BTCUSDT": 108.9})
    assert conn.store["outbox"][0] == (CHANNEL, "Close BTCUSDT")
    assert any(p and bot.REASON_TRAIL in str(p) for _s, p in conn.store["sql"] if p)


def test_the_window_is_tight_enough_that_the_entry_is_the_market():
    """<= 240 s: covers the measured insert latency of BOTH leg families (tick
    legs ~95 s median, candle-cycle legs ~185–195 s) without admitting genuinely
    old trades. The market-entry + SL/TP1 plausibility gate carry the rest of the
    original T-051 protection."""
    assert bot.MAX_MIRROR_AGE_SEC <= 240.0, bot.MAX_MIRROR_AGE_SEC


# ─────────────────────────────────────────────────────────────────────────────
# SL mark (T-2026-KYT-9050-053). Booking the stop-out with a NULL mark hid the
# worst exits from every sum: over the clean series 66 hits at avg -5.78 %
# (sum -381 %) were missing, so a query over close_mark_pct showed net -186 %
# instead of -575 % - a factor of 3 optimistic, exactly on the losses.
# ─────────────────────────────────────────────────────────────────────────────


def test_the_stop_mark_is_the_stop_level():
    """The fill is the level the order sat at — known, so booked."""
    long_row = {"entry": 100.0, "sl": 95.0, "direction": "LONG"}
    assert abs(bot.sl_exit_mark(long_row) - (-5.0)) < 1e-9
    short_row = {"entry": 100.0, "sl": 105.0, "direction": "SHORT"}
    assert abs(bot.sl_exit_mark(short_row) - (-5.0)) < 1e-9
    # A stop far behind the entry realises far worse — the CHR case was -12.2 %.
    assert abs(bot.sl_exit_mark({"entry": 100.0, "sl": 87.8, "direction": "LONG"}) - (-12.2)) < 1e-9


def test_a_row_without_a_stop_level_stays_null():
    """Legacy rows (pre-T-049) carry no sl. Guessing one would be the very error the
    old NULL was meant to avoid."""
    assert bot.sl_exit_mark({"entry": 100.0, "sl": None, "direction": "LONG"}) is None
    assert bot.sl_exit_mark({"entry": 100.0, "direction": "LONG"}) is None


def test_the_stop_out_books_that_mark_and_still_posts_nothing():
    """Wiring pin: the mark reaches the row, and the exit stays unpublished — Cornix
    closed it, so a Close of ours would claim an exit we did not cause."""
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, 5.0, True, _recent(), 100.0, _recent(), 95.0, None)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    bot.poll_open_mirrors(conn, {42: {"symbol": "BTCUSDT"}}, mirrors, prices={"BTCUSDT": 94.0})
    assert conn.store["outbox"] == [], conn.store["outbox"]
    closes = [p for sql, p in conn.store["sql"] if sql.startswith("UPDATE trailing_positions SET closed_at")]
    assert closes, conn.store["sql"]
    reason, mark, _rid = closes[0]
    assert reason == bot.REASON_SL_HIT
    assert mark is not None and abs(mark - (-5.0)) < 1e-9, mark


# ─────────────────────────────────────────────────────────────────────────────
# Free profile (T-2026-KYT-9050-117). The module is BOTH trailing bots:
# TRAILING_BOT_PROFILE=free is the unfiltered arm — no density scarcity, no
# exposure cap, ALL roster trades spread evenly over TWO channels (Cornix caps
# 500 per channel). The pins here hold the three things that make the twin safe:
# the caps really are off (and ONLY there), the books can never mix (own table,
# own tag), and entry+close of one position always land in the SAME channel.
# ─────────────────────────────────────────────────────────────────────────────

CHANNEL_A = -1002333333333  # test-local, never real channel ids
CHANNEL_B = -1002444444444


def _load_free_bot(live_posting: bool = True, ch_a: int = CHANNEL_A, ch_b: int = CHANNEL_B):
    """Load the engine as bot 44 does: profile env set before module exec."""
    env = {"TRAILING_BOT_PROFILE": "free"}
    if live_posting:
        env["TRAILING_FREE_LIVE_POSTING"] = "1"
    spec = importlib.util.spec_from_file_location("trailing_free_bot", os.path.join(ROOT, "40_trailing_close_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, env, clear=False):
        if not live_posting:
            os.environ.pop("TRAILING_FREE_LIVE_POSTING", None)
        with mock.patch.dict(
            "sys.modules",
            {
                "core.database": MagicMock(),
                "core.config": MagicMock(CH_TRAILING_FREE_A=ch_a, CH_TRAILING_FREE_B=ch_b),
            },
        ):
            spec.loader.exec_module(mod)
    return mod


freebot = _load_free_bot()


def test_free_profile_lifts_the_admission_caps_and_nothing_else():
    """Operator decision Michi 2026-08-08: the twin measures ALL roster trades.
    The caps are off in the free profile ONLY — bot 40 keeps every bound, or
    the A/B comparison ('filtered vs unfiltered') loses its filtered arm."""
    assert freebot.PROFILE == "free"
    assert freebot.EXPOSURE_CAP == 0  # T-052's ±50 deliberately dropped
    assert freebot.TABLE == "trailing_free_positions"  # own book — the twins must never mix
    assert freebot.TAG_SUFFIX == "-TRAILF"  # distinguishable in every channel grep
    assert freebot.TARGET_CHANNELS == (CHANNEL_A, CHANNEL_B)
    # Shared exit rules: the time-stop is loss limitation, not admission.
    assert freebot.TIME_STOP_H == bot.TIME_STOP_H
    assert freebot.SLOT_CAP == bot.SLOT_CAP  # the 500 stays PER CHANNEL (Cornix physics)
    # Control: the trail profile still carries its caps.
    assert bot.PROFILE == "trail" and bot.EXPOSURE_CAP == 50 and bot.TABLE == "trailing_positions"


def test_exposure_cap_off_admits_a_one_sided_book():
    """The very case bot 40 refuses (T-052 structural bound) must pass here —
    that difference IS the experiment."""
    lopsided = {"LONG": 200, "SHORT": 0}
    admitted, rejected = freebot.admit([_cand(1, "AAAUSDT", "BR4H", "LONG")], set(), 1000, open_by_dir=lopsided)
    assert [sid for sid, _ in admitted] == [1] and rejected == []


def test_one_position_per_symbol_holds_across_both_channels():
    """Operator decision 2026-08-08: 1× per symbol GLOBALLY. The second signal on
    a held symbol is refused even though the other channel could take it —
    double exposure on one coin is not what the unfiltered arm measures."""
    admitted, rejected = freebot.admit([_cand(1, "BTCUSDT", "BR4H", "LONG")], {"BTCUSDT"}, 1000)
    assert admitted == [] and rejected[0][2] == "SYMBOL_HELD"


def test_free_channels_dedupe_when_both_fall_back_to_the_shadow_channel():
    """Interim wiring: until the real channels exist, A and B both fall back to
    CH_SHADOW_TEST. Two logical channels on ONE physical channel would allow two
    positions per symbol there — dedupe keeps the symbol-wide-close invariant
    true on the physical channel."""
    mod = _load_free_bot(ch_a=CHANNEL, ch_b=CHANNEL)
    assert mod.TARGET_CHANNELS == (CHANNEL,)


def test_free_default_is_shadow_only():
    """AK11 for the twin: a deploy alone posts nothing — gate AND channels."""
    no_gate = _load_free_bot(live_posting=False)
    assert no_gate.LIVE_POSTING is False and no_gate.POSTING_ENABLED is False
    no_channels = _load_free_bot(live_posting=True, ch_a=0, ch_b=0)
    assert no_channels.TARGET_CHANNELS == (0,) and no_channels.POSTING_ENABLED is False


def test_an_unknown_profile_refuses_to_start():
    """A typo in TRAILING_BOT_PROFILE must be a loud crash at import — not a
    silent trail-profile bot posting into channels nobody intended."""
    spec = importlib.util.spec_from_file_location("trailing_bogus", os.path.join(ROOT, "40_trailing_close_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"TRAILING_BOT_PROFILE": "fre"}):
        with mock.patch.dict("sys.modules", {"core.database": MagicMock(), "core.config": MagicMock()}):
            try:
                spec.loader.exec_module(mod)
            except RuntimeError as exc:
                assert "TRAILING_BOT_PROFILE" in str(exc)
            else:
                raise AssertionError("unknown profile must not load")


def test_assign_channels_balances_toward_the_emptier_channel():
    """The balancer: each entry goes to the channel with the fewest open
    positions, counts advancing per assignment — an even book alternates, a
    skewed book levels out first."""
    admitted = [_cand(i, f"C{i}USDT", "BR4H", "LONG") for i in range(4)]
    placed = freebot.assign_channels(admitted, {CHANNEL_A: 0, CHANNEL_B: 0})
    assert [c for _sid, _sig, c in placed] == [CHANNEL_A, CHANNEL_B, CHANNEL_A, CHANNEL_B]
    placed = freebot.assign_channels(admitted, {CHANNEL_A: 3, CHANNEL_B: 0})
    assert [c for _sid, _sig, c in placed] == [CHANNEL_B, CHANNEL_B, CHANNEL_B, CHANNEL_A]


def test_assign_channels_single_channel_degenerates_to_bot_40():
    """With one channel the balancer must be a no-op — bot 40 unchanged."""
    admitted = [_cand(i, f"C{i}USDT", "BR4H", "LONG") for i in range(3)]
    placed = bot.assign_channels(admitted, {CHANNEL: 7})
    assert [c for _sid, _sig, c in placed] == [CHANNEL, CHANNEL, CHANNEL]


def test_assign_channels_never_overfills_a_channel():
    """`admit` caps the COUNT at the summed free seats; min-count placement is
    what keeps every SINGLE channel under SLOT_CAP. Pinned together: fill the
    exact remaining budget and no channel may exceed the Cornix bound."""
    open_by_channel = {CHANNEL_A: freebot.SLOT_CAP - 1, CHANNEL_B: freebot.SLOT_CAP - 3}
    free = sum(freebot.SLOT_CAP - n for n in open_by_channel.values())
    admitted = [_cand(i, f"C{i}USDT", "BR4H", "LONG") for i in range(free)]
    counts = dict(open_by_channel)
    for _sid, _sig, c in freebot.assign_channels(admitted, open_by_channel):
        counts[c] += 1
    assert all(n <= freebot.SLOT_CAP for n in counts.values()), counts


def test_the_twin_outgrows_the_single_channel_cap():
    """600 open mirrors — past bot 40's 500 — and a fresh signal is still
    admitted: the budget is the SUM over both channels (~1000 seats)."""
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG")])
    mirrors = {
        10_000 + i: {"symbol": f"S{i}USDT", "direction": "LONG", "channel_id": (CHANNEL_A, CHANNEL_B)[i % 2]}
        for i in range(600)
    }
    sources, _ = freebot.read_source_signals(conn)
    assert freebot.open_mirrors(conn, sources, mirrors, set(), prices={"BTCUSDT": 101.0}) == 1


def test_free_entry_posts_the_trailf_tag_and_records_its_channel():
    """One entry: the Cornix block carries the twin's own tag, both messages land
    in ONE channel (hard rule 4 per physical channel), and the row remembers
    that channel for the close."""
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "BR4H", "LONG")])
    sources, _ = freebot.read_source_signals(conn)
    assert freebot.open_mirrors(conn, sources, {}, set(), prices={"BTCUSDT": 101.0}) == 1
    assert len(conn.store["outbox"]) == 2  # cornix + info, nothing more
    assert {ch for ch, _m in conn.store["outbox"]} == {CHANNEL_A}  # empty book: tie → first
    cornix = [m for _ch, m in conn.store["outbox"] if m.startswith("📈")][0]
    assert "BR4H-TRAILF" in cornix, cornix
    assert "BR4H-TRAIL\n" not in cornix  # never the other arm's tag
    ins = [p for s, p in conn.store["sql"] if s.startswith("INSERT INTO trailing_free_positions")][0]
    assert CHANNEL_A in ins, ins


def test_the_close_goes_to_the_channel_the_entry_was_posted_in():
    """Cornix' `Close` acts symbol-wide PER CHANNEL — routed anywhere else it
    closes a different trade, or none."""
    row = (7, 42, "BTCUSDT", "BR4H", "LONG", 100.0, 10.0, True, None, None, None, None, CHANNEL_B)
    conn = FakeConn(mirrors=[row])
    mirrors = freebot.read_open_mirrors(conn)
    assert mirrors[42]["channel_id"] == CHANNEL_B
    with mock.patch.object(freebot, "get_live_prices_batch", return_value={"BTCUSDT": 108.9}):
        freebot.poll_open_mirrors(conn, sources={42: {"symbol": "BTCUSDT"}}, mirrors=mirrors)
    assert conn.store["outbox"][0] == (CHANNEL_B, "Close BTCUSDT")


def test_the_wrapper_is_thin():
    """Bot 44 must stay profile selection + engine exec. Any SQL, admission or
    posting code of its own would fork the twins — the one thing the operator
    requirement ('changes are made once, in bot 40') forbids."""
    src = open(os.path.join(ROOT, "44_trailing_free_bot.py"), encoding="utf-8").read()
    assert 'os.environ["TRAILING_BOT_PROFILE"] = "free"' in src
    assert "40_trailing_close_bot.py" in src
    for marker in ("INSERT INTO", "SELECT", "def admit", "telegram_outbox", "build_cornix_block"):
        assert marker not in src, marker


if __name__ == "__main__":
    # Catches Exception, not just AssertionError. A pin that fails by CRASHING (a
    # TypeError on a None, a missing key) used to abort the whole run at that point:
    # every later pin went unreported, and a grep for "^FAIL" came back empty — which
    # reads exactly like "all green". That cost three false "the pin has no teeth"
    # readings during T-2026-KYT-9050-049/053 mutation testing, each time sending me
    # after a pin that was in fact correct.
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001 — a crashing pin is a failing pin
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

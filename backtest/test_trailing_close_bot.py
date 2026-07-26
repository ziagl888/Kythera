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


def _load_bot(live_posting: bool = True):
    """Load the numerically named bot module with DB + config stubbed out."""
    env = {"TRAILING_BOT_LIVE_POSTING": "1"} if live_posting else {}
    spec = importlib.util.spec_from_file_location("trailing_close_bot", os.path.join(ROOT, "40_trailing_close_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, env, clear=False):
        if not live_posting:
            os.environ.pop("TRAILING_BOT_LIVE_POSTING", None)
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

    def execute(self, sql, params=None):
        self.store["sql"].append((" ".join(sql.split()), params))
        if "FROM ai_signals" in sql:
            self._rows = self.store["ai_signals"]
        elif "FROM trailing_positions" in sql:
            self._rows = self.store["mirrors"]
        else:
            self._rows = []
            if "INSERT INTO telegram_outbox" in sql:
                self.store["outbox"].append(params)
            elif "INSERT INTO trailing_positions" in sql and "RETURNING id" in sql:
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
            "ai_signals": list(ai_signals),
            "mirrors": list(mirrors),
        }

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass


def _src_row(sid, symbol, model, direction, entry=100.0, age_min=1.0):
    """One ai_signals row in the shape read_source_signals SELECTs.

    `age_min` is what the DB computes as `NOW() - open_time`; default is a fresh
    signal so the existing pins keep exercising the mirroring path."""
    return (sid, symbol, model, direction, entry, entry, entry * 0.95, "[110.0, 120.0]", "20x", age_min)


def _cand(sid, symbol, tag, direction):
    return sid, {"symbol": symbol, "tag": tag, "direction": direction, "density": density(tag, direction)}


# ─────────────────────────────────────────────────────────────────────────────
# AK1 / AK2 — who is mirrored at all
# ─────────────────────────────────────────────────────────────────────────────


def test_roster_admits_only_selected_legs():
    """AK1. The roster is the 33 legs that earned a Cornix seat — not 'all live bots'."""
    assert len(ROSTER) == 33
    assert is_rostered("MIS1-72h", "LONG")  # accepted, density 0.959
    assert not is_rostered("MIS1-72h", "SHORT")  # other direction never made the cut
    assert not is_rostered("EPD3", "LONG")  # rejected for cap (would_be p95 581)
    assert not is_rostered("TSM1", "SHORT")  # rejected for cap
    # Historic model spellings must land on the leg that earned the seat, not miss it.
    assert leg_key("MSI1-72H_pump", "long") == ("MIS1-72h", "LONG")
    assert is_rostered("MSI1-72H_pump", "long")

    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "MIS1-72h", "LONG"), _src_row(2, "ETHUSDT", "EPD3", "LONG")])
    got, all_open = bot.read_source_signals(conn)
    assert set(got) == {1}, got
    assert all_open == {1, 2}  # the filtered-out leg is still a KNOWN open source


def test_non_live_leg_is_never_mirrored():
    """AK2. shadow_gate beats the roster — the roster is a still from 2026-07-26."""
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "MIS1-72h", "LONG")])
    with mock.patch.object(bot.shadow_gate, "is_live", return_value=False):
        assert bot.read_source_signals(conn)[0] == {}
    assert set(bot.read_source_signals(conn)[0]) == {1}  # control: live again


def test_signal_without_geometry_is_not_mirrored():
    """A half order-geometry in a Cornix channel is worse than no mirror."""
    no_sl = (1, "BTCUSDT", "MIS1-72h", "LONG", 100.0, 100.0, None, "[110.0]", "20x", 1.0)
    no_tgt = (2, "ETHUSDT", "MIS1-72h", "LONG", 100.0, 100.0, 95.0, "[]", "20x", 1.0)
    assert bot.read_source_signals(FakeConn(ai_signals=[no_sl, no_tgt]))[0] == {}


# ─────────────────────────────────────────────────────────────────────────────
# AK3 / AK4 — admission
# ─────────────────────────────────────────────────────────────────────────────


def test_second_signal_on_same_symbol_is_rejected():
    """AK3. `Close <SYMBOL>` is symbol-wide: two positions on one symbol means the
    trailing exit of one flattens the other."""
    cands = [_cand(1, "BTCUSDT", "MIS1-72h", "LONG"), _cand(2, "BTCUSDT", "RUB1", "LONG")]
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
        _cand(1, "AAAUSDT", "ATS2", "LONG"),  # density 0.054 — thinnest of the 33
        _cand(2, "BBBUSDT", "AIM2", "SHORT"),  # density 4.294 — dense
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
    row = (7, 42, "BTCUSDT", "MIS1-72h", "LONG", 100.0, 5.0, True)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    with mock.patch.object(bot, "get_live_prices_batch", return_value={"BTCUSDT": 104.0}):
        bot.poll_open_mirrors(conn, sources={}, mirrors=mirrors)  # source row gone
    assert conn.store["outbox"][0] == (CHANNEL, "Close BTCUSDT")
    assert any("close_reason" in s.lower() or "closed_at" in s.lower() for s, _ in conn.store["sql"])
    assert any(p and bot.REASON_SOURCE_CLOSED in p for _, p in conn.store["sql"] if p)


def test_trailing_trigger_closes_the_mirror():
    """The bot's one own decision: peak +10 %, give back to +9 % → close."""
    row = (7, 42, "BTCUSDT", "MIS1-72h", "LONG", 100.0, 10.0, True)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    src = {42: {"symbol": "BTCUSDT"}}
    with mock.patch.object(bot, "get_live_prices_batch", return_value={"BTCUSDT": 108.9}):
        bot.poll_open_mirrors(conn, sources=src, mirrors=mirrors)
    assert conn.store["outbox"][0] == (CHANNEL, "Close BTCUSDT")
    assert any(p and bot.REASON_TRAIL in p for _, p in conn.store["sql"] if p)


def test_no_price_means_no_decision():
    """A coin without a tick keeps its position — closing it would be a statement
    about a market we cannot see."""
    row = (7, 42, "BTCUSDT", "MIS1-72h", "LONG", 100.0, 10.0, True)
    conn = FakeConn(mirrors=[row])
    mirrors = bot.read_open_mirrors(conn)
    with (
        mock.patch.object(bot, "get_live_prices_batch", return_value={}),
        mock.patch.object(bot, "get_live_price", return_value=None),
    ):
        bot.poll_open_mirrors(conn, sources={42: {"symbol": "BTCUSDT"}}, mirrors=mirrors)
    assert conn.store["outbox"] == []


# ─────────────────────────────────────────────────────────────────────────────
# AK9 / AK10 — the Cornix contract
# ─────────────────────────────────────────────────────────────────────────────


CORNIX_MARKERS = ("🚨 Direction:", "🏦 CMP Entry:", "💸 Stop Loss:")


def test_exactly_one_parsable_message_per_entry():
    """AK9 (hard rule 4). A second parsable message is a second position."""
    sig = {
        "symbol": "BTCUSDT",
        "tag": "MIS1-72h",
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
    row = {"symbol": "BTCUSDT", "model": "MIS1-72h", "direction": "LONG", "id": 1, "posted": True}
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
        model_tag="MIS1-72h-TRAIL",
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
        "🧠 Trade idea generated by AI module MIS1-72h-TRAIL"
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
            "MIS1-72h",
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
        "MIS1-72h",
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
    assert shadow.TARGET_CHANNEL_ID == 0

    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "MIS1-72h", "LONG")])
    sources, _ = shadow.read_source_signals(conn)
    assert shadow.open_mirrors(conn, sources, {}, set()) == 1  # tracked …
    assert conn.store["outbox"] == []  # … but nothing published
    assert any("INSERT INTO trailing_positions" in s for s, _ in conn.store["sql"])


def test_live_gate_open_publishes_two_messages():
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "MIS1-72h", "LONG")])
    bot.open_mirrors(conn, bot.read_source_signals(conn)[0], {}, set())
    assert len(conn.store["outbox"]) == 2  # cornix + info, nothing more
    assert all(row[0] == CHANNEL for row in conn.store["outbox"])


def test_bot_never_writes_ai_signals():
    """AK12. The bot reads the AI monitor's table and never writes it — a second
    writer there would fight Bot 8 over real positions."""
    src = open(os.path.join(ROOT, "40_trailing_close_bot.py"), encoding="utf-8").read()
    for verb in ("INSERT INTO ai_signals", "UPDATE ai_signals", "DELETE FROM ai_signals"):
        assert verb not in src, verb
    # And at runtime: a full open+poll cycle touches only its own tables + outbox.
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "MIS1-72h", "LONG")])
    bot.open_mirrors(conn, bot.read_source_signals(conn)[0], {}, set())
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
    src = _src_row(42, "BTCUSDT", "MIS1-72h", "LONG")
    conn = FakeConn(ai_signals=[src])
    sources, _ = bot.read_source_signals(conn)

    # The mirror was opened and has already been trailed out: no OPEN mirror row
    # remains, but trailing_positions still knows the source id.
    already = {42}
    assert bot.open_mirrors(conn, sources, {}, already) == 0
    assert conn.store["outbox"] == [], conn.store["outbox"]


def test_insert_losing_the_race_publishes_nothing():
    """Write first, publish only on a real insert (the P2.8 pattern). A post whose
    row lost the unique index is a position nobody will ever close."""
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "MIS1-72h", "LONG")])
    sources, _ = bot.read_source_signals(conn)

    real_cursor = conn.cursor

    class LosingCursor(FakeCursor):
        def fetchone(self):
            return None  # ON CONFLICT DO NOTHING RETURNING id -> no row

    conn.cursor = lambda: LosingCursor(conn.store)
    try:
        assert bot.open_mirrors(conn, sources, {}, set()) == 0
    finally:
        conn.cursor = real_cursor
    assert conn.store["outbox"] == []


def test_retired_leg_closes_with_its_own_reason():
    """A leg that leaves the register while its trade runs still ends the mirror —
    but it is not the same event as the fleet closing the trade, and the ledger
    must be able to tell them apart."""
    row = (7, 42, "BTCUSDT", "MIS1-72h", "LONG", 100.0, 5.0, True)
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
            _src_row(1, "BTCUSDT", "MIS1-72h", "LONG", age_min=4000.0),  # ~3 days old
            _src_row(2, "ETHUSDT", "RUB1", "LONG", age_min=2.0),  # opened just now
        ]
    )
    sources, _ = bot.read_source_signals(conn)
    assert bot.open_mirrors(conn, sources, {}, set()) == 1  # only the fresh one

    published = [m for _ch, m in conn.store["outbox"]]
    assert not any("BTCUSDT" in m for m in published), published
    assert any("ETHUSDT" in m for m in published)

    # The old one got a closed PREEXISTING row — the same lock that stops a
    # trailed-out trade from being re-entered.
    marks = [p for _sql, p in conn.store["sql"] if p and bot.REASON_PREEXISTING in str(p)]
    assert marks, conn.store["sql"]


def test_the_age_cutoff_covers_a_restart_window():
    """A trade that opened DURING a fleet restart (~5 min) is still the same trade
    and must be mirrored; the default cutoff has to leave room for that."""
    assert bot.MAX_MIRROR_AGE_MIN >= 10.0, bot.MAX_MIRROR_AGE_MIN
    conn = FakeConn(ai_signals=[_src_row(1, "BTCUSDT", "MIS1-72h", "LONG", age_min=6.0)])
    sources, _ = bot.read_source_signals(conn)
    assert bot.open_mirrors(conn, sources, {}, set()) == 1


def test_missing_open_time_counts_as_old():
    """A NULL open_time is unknowable age — in doubt, do not mirror."""
    row = (1, "BTCUSDT", "MIS1-72h", "LONG", 100.0, 100.0, 95.0, "[110.0]", "20x", None)
    conn = FakeConn(ai_signals=[row])
    sources, _ = bot.read_source_signals(conn)
    assert sources[1]["age_min"] == float("inf")
    assert bot.open_mirrors(conn, sources, {}, set()) == 0
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
                "model": "MIS1-72h",
                "tag": "MIS1-72h",
                "direction": "LONG",
                "entry": 100.0,
                "sl": 95.0,
                "targets": [110.0],
                "lev": "20x",
                "density": 0.959,
                "age_min": 1.0,
            }
            for i in range(50)
        }
        bot.open_mirrors(conn, sources, {}, set())
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

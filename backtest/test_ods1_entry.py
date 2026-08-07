# backtest/test_ods1_entry.py — ODS1's entry rule and its OI staleness contract.
"""Standalone, DB-free tests for 42_ai_ods1_bot.py (T-2026-KYT-9050-106).

Three things decide whether this bot makes or loses money, and none of them are
visible from a green import: the entry rule must match the T-096 operating point
it claims to implement, a stale OI point must be voided rather than filled, and
the roster seat must not break the register's eviction order.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# core.config raises at import when its _required() vars are unset; seed dummies
# before the loader execs the module (the build machine ships an empty .env stub).
# Without this the file errors at COLLECTION on any credential-less host — it only
# looked green here because SRV02 carries the live .env, which is the opposite of
# the "standalone and DB-free" the task asked for.
os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")


def _load_bot():
    """Digit-prefixed filename — not importable by name."""
    spec = importlib.util.spec_from_file_location("ods1", os.path.join(REPO, "42_ai_ods1_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ods1"] = mod
    spec.loader.exec_module(mod)
    return mod


ods1 = _load_bot()
NOW = 1_760_000_000


def series(points):
    """[(seconds_before_now, open_interest, price)] -> the bot's row format."""
    return [(NOW - dt, oi, px) for dt, oi, px in sorted(points, key=lambda p: -p[0])]


# ── the T-096 operating point ────────────────────────────────────────────────


def test_thresholds_match_the_study_they_cite():
    """px >= +3 %, 4h OI <= -2 %. Drifting from these silently turns the bot into
    something the study never measured."""
    assert ods1.PX_RALLY_PCT == pytest.approx(3.0)
    assert ods1.OI_DROP_PCT == pytest.approx(-2.0)
    assert ods1.LOOKBACK_S == 4 * 3600
    assert ods1.COOLDOWN_H == 24


def test_rally_on_draining_oi_fires():
    rows = {"XUSDT": series([(4 * 3600, 1_000_000.0, 100.0), (0, 950_000.0, 104.0)])}
    out, _usable = ods1.find_candidates(rows, NOW)
    assert [c["symbol"] for c in out] == ["XUSDT"]
    assert out[0]["px_chg"] == pytest.approx(4.0)
    assert out[0]["oi_chg"] == pytest.approx(-5.0)


def test_rally_on_rising_oi_does_not_fire():
    """New money behind the move is a trend, not a squeeze — this is the case the
    study refuted (spike-fade, -2.56 @24h)."""
    rows = {"XUSDT": series([(4 * 3600, 1_000_000.0, 100.0), (0, 1_100_000.0, 104.0)])}
    assert ods1.find_candidates(rows, NOW)[0] == []


def test_oi_drain_without_a_rally_does_not_fire():
    rows = {"XUSDT": series([(4 * 3600, 1_000_000.0, 100.0), (0, 900_000.0, 100.5)])}
    assert ods1.find_candidates(rows, NOW)[0] == []


def test_thin_books_are_skipped():
    """Study universe was median OI >= $3M; a 40k book is not the same asset."""
    rows = {"XUSDT": series([(4 * 3600, 400.0, 100.0), (0, 380.0, 104.0)])}
    assert ods1.find_candidates(rows, NOW)[0] == []


def test_strictest_divergence_ranks_first():
    """The threshold matrix was monotone in strictness, so under a slot cap the
    most extreme event is the one worth the seat."""
    rows = {
        "MILD": series([(4 * 3600, 1_000_000.0, 100.0), (0, 970_000.0, 103.5)]),
        "HARSH": series([(4 * 3600, 1_000_000.0, 100.0), (0, 880_000.0, 108.0)]),
    }
    assert [c["symbol"] for c in ods1.find_candidates(rows, NOW)[0]] == ["HARSH", "MILD"]


# ── the staleness contract ───────────────────────────────────────────────────


def test_stale_now_point_voids_the_symbol():
    """The collector degraded to a 10-min median cadence (T-097). A point older
    than the cap is voided, never carried forward."""
    stale = ods1.STALENESS_CAP_S + 600
    rows = {"XUSDT": series([(4 * 3600 + stale, 1_000_000.0, 100.0), (stale, 900_000.0, 108.0)])}
    assert ods1.find_candidates(rows, NOW)[0] == []


def test_as_of_never_looks_forward():
    rows = series([(0, 900_000.0, 108.0)])
    assert ods1._as_of(rows, NOW - 4 * 3600) is None


def test_as_of_takes_the_last_point_at_or_before_t():
    rows = series([(3600, 1_000_000.0, 100.0), (600, 950_000.0, 104.0)])
    oi, px = ods1._as_of(rows, NOW)
    assert (oi, px) == (950_000.0, 104.0)


def test_a_gap_inside_the_cap_is_still_usable():
    """Voiding is for staleness, not for every irregular cadence — the table is
    effectively 10-minutely and would otherwise never produce a signal."""
    rows = {"XUSDT": series([(4 * 3600 + 1200, 1_000_000.0, 100.0), (1200, 900_000.0, 108.0)])}
    assert len(ods1.find_candidates(rows, NOW)[0]) == 1


# ── geometry and the roster seat ─────────────────────────────────────────────


def test_bracket_is_sized_to_the_measured_drift_not_the_fleet_default():
    """T-096 measured +0.41 % @1h and +0.73 % @4h. A fleet-default bracket (TP1
    ~4-5 %) sits far outside that and the edge leaks away before TP1."""
    assert max(ods1.TP_PCTS) <= 2.0
    assert ods1.SL_PCT <= 2.5
    assert ods1.TP_PCTS == tuple(sorted(ods1.TP_PCTS))


def test_ladder_rungs_are_far_enough_apart_to_be_rungs():
    """Cornix splits the position 50/50 across the ladder, so two targets closer
    than the fleet's 1 % minimum are not a staged exit — whoever reaches TP1 takes
    TP2 in the same move, and the book records a full ladder success for what was
    really a single target.

    ODS1 shipped at (1.0, 1.5) — a 0.5 % gap — and two of its first four live
    trades closed "ALL TARGETS HIT" within minutes. Measured across the fleet on
    signals since 2026-08-04 (TP1 distance / min gap), ODS1 was the only violator:

        EPD3 2.63 %/1.82 %   ROM1 3.09 %/2.01 %   ATS2 2.02 %/1.38 %
        TSM1 2.00 %/1.24 %   AIM2 7.21 %/3.67 %   ODS1 1.00 %/0.50 %  <-

    The previous guards pinned the ceiling and the ordering and never the spacing,
    which is why a faithful translation of the study still produced a dead rung.
    """
    # Pin the constant too. Without this, the guard can be defanged by lowering
    # MIN_TP_GAP_PCT instead of widening the ladder — verified by mutation, and the
    # identical hole was found in DOMAIN_FIT_MIN on PR #274 one commit earlier.
    # Changing the fleet minimum must show up as a test edit, with the fleet
    # re-measured.
    assert ods1.MIN_TP_GAP_PCT == pytest.approx(1.0)

    gaps = [b - a for a, b in zip(ods1.TP_PCTS, ods1.TP_PCTS[1:], strict=False)]
    assert gaps, "a single-target ladder has no spacing to check — state that deliberately"
    assert min(gaps) >= ods1.MIN_TP_GAP_PCT, (
        f"TP rungs {ods1.TP_PCTS} are {min(gaps):.2f} % apart, below the "
        f"{ods1.MIN_TP_GAP_PCT} % fleet minimum — the second rung would not fire separately"
    )


def test_the_last_rung_is_not_beyond_the_stop():
    """A target further from the entry than the stop can only be reached by a move
    that already had every chance to stop the trade out first."""
    assert max(ods1.TP_PCTS) <= ods1.SL_PCT


def _capture_emit(monkeypatch):
    """Run ods1.emit with the posting gate stubbed; return the captured kwargs."""
    seen: list[dict] = []

    def fake_post(conn, **kw):
        seen.append(kw)
        return 1

    monkeypatch.setattr(ods1, "post_ai_signal_gated", fake_post)
    monkeypatch.setattr(ods1, "get_max_leverage", lambda *a, **k: 20)
    return seen


def test_short_targets_are_below_entry_and_stop_is_above(monkeypatch):
    """P0.7's failure class: a ladder built on the wrong side of the entry scores
    losses as TP hits.

    Exercises ``emit`` rather than re-deriving the formula in the test body — the
    earlier version recomputed ``price * (1 - p/100)`` itself, so flipping the sign
    inside ``emit`` left it green. It guarded its own arithmetic, not the bot's.
    """
    seen = _capture_emit(monkeypatch)
    cand = {"symbol": "XUSDT", "price": 100.0, "px_chg": 4.0, "oi_chg": -5.0}
    assert ods1.emit(object(), cand, 100.0) is True

    kw = seen[0]
    assert kw["direction"] == "SHORT"
    assert all(t < kw["entry1"] for t in kw["targets"]), "SHORT targets must sit BELOW the entry"
    assert kw["sl"] > kw["entry1"], "a SHORT stop must sit ABOVE the entry"
    assert kw["targets"] == sorted(kw["targets"], reverse=True)


def test_emit_reports_whether_the_gate_actually_posted(monkeypatch):
    """A SILENT/RETIRED leg returns falsy; counting it would commit an empty tx."""
    monkeypatch.setattr(ods1, "post_ai_signal_gated", lambda conn, **kw: None)
    monkeypatch.setattr(ods1, "get_max_leverage", lambda *a, **k: 20)
    cand = {"symbol": "XUSDT", "price": 100.0, "px_chg": 4.0, "oi_chg": -5.0}
    assert ods1.emit(object(), cand, 100.0) is False


# ── the entry anchor (T-2026-KYT-9050-115) ───────────────────────────────────


def test_the_whole_bracket_hangs_off_the_posting_price_not_the_oi_point(monkeypatch):
    """The defect this replaced: the bracket was derived from the OI-implied price,
    which `_as_of` accepts up to STALENESS_CAP_S old on a collector running a 10-min
    median cadence. TP1 is 1.0 %, so a 10-minute-old anchor on this bot's own tape
    (+3 % over 4 h) could sit half a TP1 away — the posted risk/reward was not the
    one the geometry priced.

    Asserted against the MARKET price, and with decision != market so an
    implementation that silently kept using `cand["price"]` cannot pass.
    """
    seen = _capture_emit(monkeypatch)
    cand = {"symbol": "XUSDT", "price": 100.0, "px_chg": 4.0, "oi_chg": -5.0}
    assert ods1.emit(object(), cand, 110.0) is True

    kw = seen[0]
    assert kw["entry1"] == pytest.approx(110.0)
    assert kw["entry2"] == pytest.approx(110.0)
    assert kw["targets"] == pytest.approx([110.0 * 0.99, 110.0 * 0.98])
    assert kw["sl"] == pytest.approx(110.0 * 1.02)


def test_drift_is_signed_so_a_fallen_price_reads_as_consumed():
    """SHORT: the market moving DOWN since the OI point is the effect being spent."""
    assert ods1.drift_consumed_pct(100.0, 99.0) > 0
    assert ods1.drift_consumed_pct(100.0, 101.0) < 0
    assert ods1.drift_consumed_pct(100.0, 100.0) == pytest.approx(0.0)


def test_the_drift_bound_is_tied_to_tp1_not_a_loose_constant():
    """It has to move with the geometry it protects: re-pricing TP1 without
    re-pricing the bound would silently change what fraction of the measured effect
    may already be gone."""
    assert ods1.max_drift_pct() == pytest.approx(ods1.DRIFT_CONSUMED_FRAC_OF_TP1 * ods1.TP_PCTS[0])
    assert 0.0 < ods1.max_drift_pct() < ods1.TP_PCTS[0], "a bound at or above TP1 bounds nothing"


def test_entry_anchor_never_hands_the_connection_to_the_price_fallback(monkeypatch):
    """`get_live_price`'s DB fallback calls conn.rollback() on a query error. That is
    connection-wide, and ODS1 commits once at the END of the cycle — so a fallback
    that took the connection could discard every signal already written this cycle.
    HTTP-only is the cheaper failure."""
    got: list[tuple] = []
    monkeypatch.setattr(ods1, "get_live_price", lambda *a, **k: got.append((a, k)) or 42.0)
    sentinel = object()
    assert ods1.entry_anchor(sentinel, "XUSDT") == pytest.approx(42.0)
    args, kwargs = got[0]
    assert sentinel not in args and sentinel not in kwargs.values(), (args, kwargs)


def test_entry_anchor_voids_rather_than_inventing_a_price(monkeypatch):
    monkeypatch.setattr(ods1, "get_live_price", lambda *a, **k: None)
    assert ods1.entry_anchor(object(), "XUSDT") is None
    monkeypatch.setattr(ods1, "get_live_price", lambda *a, **k: 0.0)
    assert ods1.entry_anchor(object(), "XUSDT") is None


def test_emissions_are_bounded_per_cycle():
    """The entry rule is a MARKET-WIDE mechanic: one BTC-led short squeeze puts
    dozens of correlated alts over the threshold in the same poll. Unbounded, a
    single cycle could post the whole qualifying universe into a Cornix-executed
    channel — and the roster seat mirrors each one into CH_TRAILING too, so the
    burst hits two channels against a per-channel cap of 500. EPD3-SHORT was
    estimated low once and delivered ~484/day."""
    assert ods1.MAX_EMITS_PER_CYCLE > 0
    assert ods1.MAX_EMITS_PER_CYCLE <= 25, "a burst must not be able to fill a Cornix channel"


class _FakeCursor:
    """Enough of a cursor to model SAVEPOINT / RELEASE / ROLLBACK TO."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        op = sql.strip().upper()
        if op.startswith("SAVEPOINT"):
            self.conn.savepoint = list(self.conn.pending)
        elif op.startswith("ROLLBACK TO SAVEPOINT"):
            # exactly what Postgres does: undo back to the mark, keep the rest
            self.conn.pending = list(self.conn.savepoint)
        elif op.startswith("RELEASE SAVEPOINT"):
            self.conn.savepoint = None


class _FakeConn:
    """Models pending-vs-durable so a rollback that eats earlier work is visible.

    The previous version had `rollback()` as a bare `pass`, which made the
    isolation test structurally unable to observe the property it asserted.
    """

    def __init__(self):
        self.commits = 0
        self.pending: list[str] = []
        self.durable: list[str] = []
        self.savepoint: list[str] | None = None

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.durable.extend(self.pending)
        self.pending = []

    def rollback(self):
        self.pending = []


def test_run_cycle_stops_at_the_cap_and_keeps_the_strictest(monkeypatch):
    """Behavioural guard for the cap: 20 simultaneous qualifying symbols must not
    become 20 posts, and what survives must be the strictest divergences, since
    those are the events T-096 measured as the strongest."""
    burst = [{"symbol": f"S{i}USDT", "price": 100.0, "px_chg": 3.0 + i, "oi_chg": -2.0 - i} for i in range(20)]
    burst.sort(key=lambda c: c["oi_chg"] - c["px_chg"])  # as find_candidates does

    posted: list[str] = []
    monkeypatch.setattr(ods1, "load_oi_window", lambda conn, since: {})
    monkeypatch.setattr(ods1, "find_candidates", lambda series, now: (burst, len(burst)))
    monkeypatch.setattr(ods1, "has_open_ai_signal", lambda *a, **k: False)
    monkeypatch.setattr(ods1, "on_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(ods1, "get_live_prices_batch", lambda: {c["symbol"]: 100.0 for c in burst})
    monkeypatch.setattr(ods1, "emit", lambda conn, cand, market: posted.append(cand["symbol"]) or True)

    ods1.run_cycle(_FakeConn())

    assert len(posted) == ods1.MAX_EMITS_PER_CYCLE, "the cap must bound a market-wide burst"
    assert posted == [c["symbol"] for c in burst[: ods1.MAX_EMITS_PER_CYCLE]], (
        "truncation must keep the strictest divergences, not an arbitrary subset"
    )


def test_one_failing_symbol_does_not_void_the_rest_of_the_batch(monkeypatch):
    """A raise mid-batch must cost exactly that symbol, not the whole cycle.

    The SAVEPOINT is what makes this true. A bare conn.rollback() would be
    connection-wide and discard S0 as well, while `emitted` still counted it —
    the log would then claim a signal that never reached the outbox.
    """
    cands = [{"symbol": f"S{i}USDT", "price": 100.0, "px_chg": 4.0, "oi_chg": -5.0} for i in range(3)]

    def flaky(conn, cand, market):
        conn.pending.append(cand["symbol"])  # the signal row this emit would write
        if cand["symbol"] == "S1USDT":
            raise RuntimeError("emit failed")
        return True

    monkeypatch.setattr(ods1, "load_oi_window", lambda conn, since: {})
    monkeypatch.setattr(ods1, "find_candidates", lambda series, now: (cands, 3))
    monkeypatch.setattr(ods1, "has_open_ai_signal", lambda *a, **k: False)
    monkeypatch.setattr(ods1, "on_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(ods1, "get_live_prices_batch", lambda: {c["symbol"]: 100.0 for c in cands})
    monkeypatch.setattr(ods1, "emit", flaky)

    conn = _FakeConn()
    ods1.run_cycle(conn)  # must not raise
    assert conn.commits == 1
    assert conn.durable == ["S0USDT", "S2USDT"], (
        "the signal written before the failure must survive it, and the failed one must not"
    )


def test_a_candidate_whose_drift_already_ran_is_not_posted(monkeypatch):
    """T-096 measured a horizon return FROM the event instant. If the price has
    already mean-reverted between the OI point and this poll, the trade being
    entered is the tail of the effect, not the effect."""
    cands = [
        {"symbol": "SPENTUSDT", "price": 100.0, "px_chg": 4.0, "oi_chg": -5.0},
        {"symbol": "FRESHUSDT", "price": 100.0, "px_chg": 4.0, "oi_chg": -5.0},
    ]
    beyond = 100.0 * (1.0 - 2.0 * ods1.max_drift_pct() / 100.0)  # well past the bound
    posted: list[str] = []
    monkeypatch.setattr(ods1, "load_oi_window", lambda conn, since: {})
    monkeypatch.setattr(ods1, "find_candidates", lambda series, now: (cands, 2))
    monkeypatch.setattr(ods1, "has_open_ai_signal", lambda *a, **k: False)
    monkeypatch.setattr(ods1, "on_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(ods1, "get_live_prices_batch", lambda: {"SPENTUSDT": beyond, "FRESHUSDT": 100.0})
    monkeypatch.setattr(ods1, "emit", lambda conn, cand, market: posted.append(cand["symbol"]) or True)

    ods1.run_cycle(_FakeConn())
    assert posted == ["FRESHUSDT"], posted


def test_a_candidate_without_a_live_anchor_is_voided_not_posted_at_the_stale_price(monkeypatch):
    """The error path must not fall back to the OI-implied price — that is exactly
    the stale anchor this change removed, and reintroducing it there would make the
    defect intermittent instead of gone."""
    cands = [{"symbol": "XUSDT", "price": 100.0, "px_chg": 4.0, "oi_chg": -5.0}]
    posted: list[str] = []
    monkeypatch.setattr(ods1, "load_oi_window", lambda conn, since: {})
    monkeypatch.setattr(ods1, "find_candidates", lambda series, now: (cands, 1))
    monkeypatch.setattr(ods1, "has_open_ai_signal", lambda *a, **k: False)
    monkeypatch.setattr(ods1, "on_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(ods1, "get_live_prices_batch", lambda: {})
    monkeypatch.setattr(ods1, "get_live_price", lambda *a, **k: None)  # HTTP fallback also down
    monkeypatch.setattr(ods1, "emit", lambda conn, cand, market: posted.append(cand["symbol"]) or True)

    conn = _FakeConn()
    ods1.run_cycle(conn)
    assert posted == []
    assert conn.commits == 0, "nothing was emitted, so nothing should be committed"


def test_roster_seat_exists_and_does_not_break_the_eviction_order():
    from core.trailing_roster import ROSTER, density, is_rostered

    assert is_rostered("ODS1", "SHORT")
    values = list(ROSTER.values())
    pairs = zip(values, values[1:], strict=False)  # values[1:] is one shorter by construction
    assert all(a > b for a, b in pairs), "density must stay strictly descending"
    assert density("ODS1", "SHORT") == min(values), "an unmeasured leg yields its seat first, not last"

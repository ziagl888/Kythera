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
    assert ods1.emit(object(), cand) is True

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
    assert ods1.emit(object(), cand) is False


def test_emissions_are_bounded_per_cycle():
    """The entry rule is a MARKET-WIDE mechanic: one BTC-led short squeeze puts
    dozens of correlated alts over the threshold in the same poll. Unbounded, a
    single cycle could post the whole qualifying universe into a Cornix-executed
    channel — and the roster seat mirrors each one into CH_TRAILING too, so the
    burst hits two channels against a per-channel cap of 500. EPD3-SHORT was
    estimated low once and delivered ~484/day."""
    assert ods1.MAX_EMITS_PER_CYCLE > 0
    assert ods1.MAX_EMITS_PER_CYCLE <= 25, "a burst must not be able to fill a Cornix channel"


class _FakeConn:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


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
    monkeypatch.setattr(ods1, "emit", lambda conn, cand: posted.append(cand["symbol"]) or True)

    ods1.run_cycle(_FakeConn())

    assert len(posted) == ods1.MAX_EMITS_PER_CYCLE, "the cap must bound a market-wide burst"
    assert posted == [c["symbol"] for c in burst[: ods1.MAX_EMITS_PER_CYCLE]], (
        "truncation must keep the strictest divergences, not an arbitrary subset"
    )


def test_one_failing_symbol_does_not_void_the_rest_of_the_batch(monkeypatch):
    """post_ai_signal does a live chart fetch per signal. Without per-candidate
    isolation one raising symbol rolls back every signal already written."""
    cands = [{"symbol": f"S{i}USDT", "price": 100.0, "px_chg": 4.0, "oi_chg": -5.0} for i in range(3)]

    def flaky(conn, cand):
        if cand["symbol"] == "S1USDT":
            raise RuntimeError("chart fetch failed")
        return True

    monkeypatch.setattr(ods1, "load_oi_window", lambda conn, since: {})
    monkeypatch.setattr(ods1, "find_candidates", lambda series, now: (cands, 3))
    monkeypatch.setattr(ods1, "has_open_ai_signal", lambda *a, **k: False)
    monkeypatch.setattr(ods1, "on_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(ods1, "emit", flaky)

    conn = _FakeConn()
    ods1.run_cycle(conn)  # must not raise
    assert conn.commits == 1, "the two good signals must still be committed"


def test_roster_seat_exists_and_does_not_break_the_eviction_order():
    from core.trailing_roster import ROSTER, density, is_rostered

    assert is_rostered("ODS1", "SHORT")
    values = list(ROSTER.values())
    pairs = zip(values, values[1:], strict=False)  # values[1:] is one shorter by construction
    assert all(a > b for a, b in pairs), "density must stay strictly descending"
    assert density("ODS1", "SHORT") == min(values), "an unmeasured leg yields its seat first, not last"

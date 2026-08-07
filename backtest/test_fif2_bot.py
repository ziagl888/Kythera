# backtest/test_fif2_bot.py — the gate, the ladder, and the cap, pinned.
"""Standalone, DB-free tests for 43_ai_fif2_bot.py (T-2026-KYT-9050-112).

What must never drift: the serve-time vol is the study's vol (shared builder,
tested as an identity), the ladder is the measured t104 bracket in Cornix
order, the rolling gate refuses to exist before it has a distribution, the
bootstrap cycle samples but never posts, and the per-cycle cap trims the
weakest of a burst — counted, not silent.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# core.config raises at import when its _required() vars are unset; seed dummies
# before the loader execs the module (the build machine ships an empty .env stub).
os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")

from core.vol_features import VOL_WINDOW_5M, rolling_std_pct, vol_now_pct  # noqa: E402

CHANNEL = -1002222222222  # test-local, never a real channel id


def _load_bot():
    """Load the numerically named bot module with DB + config stubbed out.

    The two stub keys are swapped in and out by hand rather than through
    ``mock.patch.dict("sys.modules", ...)``: patch.dict restores the WHOLE
    snapshot it took on entry, which also drops every module imported while the
    block was open — including the numpy C submodules the bot pulls in on first
    touch. Re-importing those later raises "cannot load module more than once
    per process" (Python 3.14), so a suite that happened to run this file first
    took down the next test that needed numpy. Touch only what we stubbed.
    """
    spec = importlib.util.spec_from_file_location("fif2_bot", os.path.join(ROOT, "43_ai_fif2_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    stubs = {"core.database": MagicMock(), "core.config": MagicMock(CH_FIF2=CHANNEL)}
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        spec.loader.exec_module(mod)
    finally:
        for key, previous in saved.items():
            if previous is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = previous
    return mod


BOT = _load_bot()


# ── shared builder parity ────────────────────────────────────────────────────


def test_study_and_bot_share_one_vol_implementation():
    """The T-110 study's rolling_std IS the core builder — an identity, not a
    lookalike. If someone re-implements one side, this fails."""
    from tools.tp1_speed_study import rolling_std

    assert rolling_std is rolling_std_pct


def test_vol_now_matches_the_rolling_series_tail():
    rng = np.random.default_rng(7)
    closes = 100.0 * np.cumprod(1 + rng.normal(0, 0.002, VOL_WINDOW_5M + 1))
    assert vol_now_pct(closes) == pytest.approx(rolling_std_pct(closes, VOL_WINDOW_5M)[-1])


def test_vol_now_refuses_short_history():
    assert vol_now_pct(np.full(VOL_WINDOW_5M, 100.0)) is None  # one candle short


# ── ladder geometry ──────────────────────────────────────────────────────────


def test_ladder_is_the_measured_t104_bracket_in_cornix_order():
    targets, sl = BOT.ladder("LONG", 100.0)
    assert targets == pytest.approx([104.0, 105.0])  # ascending — nearest rung first
    assert sl == pytest.approx(95.0)
    targets, sl = BOT.ladder("SHORT", 100.0)
    assert targets == pytest.approx([97.0, 96.0])  # descending — nearest rung first
    assert sl == pytest.approx(102.0)


# ── the rolling gate ─────────────────────────────────────────────────────────


def test_gate_refuses_to_exist_during_warmup():
    assert BOT.gate_threshold([1.0] * (BOT.MIN_REFIT_N - 1)) is None


def test_gate_is_the_q80_of_the_trailing_distribution():
    vols = list(np.linspace(0.0, 1.0, BOT.MIN_REFIT_N))
    assert BOT.gate_threshold(vols) == pytest.approx(0.8, abs=0.01)


def test_percentile_confidence_is_monotone_and_capped():
    vols = list(np.linspace(0.0, 1.0, 1000))
    lo, hi = BOT.vol_percentile(vols, 0.2), BOT.vol_percentile(vols, 0.9)
    assert lo < hi
    assert BOT.vol_percentile(vols, 99.0) == pytest.approx(0.99)  # never 1.0


def test_trim_drops_samples_older_than_the_refit_window():
    now = 1_760_000_000.0
    samples = [[now - BOT.REFIT_WINDOW_S - 1, 0.1], [now - 10, 0.2]]
    assert BOT.trim_samples(samples, now) == [[now - 10, 0.2]]


# ── candidate ingestion ──────────────────────────────────────────────────────


def test_fetch_excludes_self_and_rows_without_a_usable_entry():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        (1, "AUSDT", "EPD3", "LONG", 100.0, 100.5, 30.0),
        (2, "BUSDT", "RUB3", "SHORT", None, None, 30.0),  # no entry, no price -> dropped
        (3, "CUSDT", "SRA2", "SIDEWAYS", 50.0, None, 30.0),  # unknown direction -> dropped
    ]
    rows = BOT.fetch_fresh_signals(conn)
    assert [r["id"] for r in rows] == [1]
    sql, params = cur.execute.call_args[0]
    assert "model <> %s" in sql  # the self-echo guard lives in the query itself
    assert params[0] == BOT.MODEL_ID


# ── cycle behaviour ──────────────────────────────────────────────────────────


def _cycle_fixture(monkeypatch, n_candidates: int, bootstrap: bool = False, prices=None):
    """Run one cycle against stubs: every candidate has vol i+1 (strongest last
    in arrival order), the trailing distribution is warm, nothing is open.

    ``prices`` defaults to every symbol trading exactly at its source entry, so the
    drift bound is inert and the cap/gate pins below measure what they name.
    """
    conn = MagicMock()
    cands = [
        {"id": i, "symbol": f"S{i}USDT", "model": "EPD3", "direction": "LONG", "entry": 100.0, "age_sec": 30.0}
        for i in range(n_candidates)
    ]
    emitted: list[tuple[str, float]] = []
    logged: list[bool] = []
    monkeypatch.setattr(BOT, "fetch_fresh_signals", lambda _c: cands)
    monkeypatch.setattr(BOT, "sym_vol_4h", lambda _c, s: float(s[1 : s.index("USDT")]) + 1.0)
    monkeypatch.setattr(BOT, "has_open_ai_signal", lambda *_a: False)
    monkeypatch.setattr(
        BOT,
        "get_live_prices_batch",
        lambda: {c["symbol"]: 100.0 for c in cands} if prices is None else dict(prices),
    )
    monkeypatch.setattr(BOT, "posting_anchor", lambda *_a, **_k: None)
    monkeypatch.setattr(BOT, "emit", lambda _c, cand, vol, conf, market: emitted.append((cand["symbol"], vol)) or True)
    monkeypatch.setattr(BOT, "log_prediction", lambda *_a, **kw: logged.append(kw.get("posted", _a[-1])))
    # warm distribution with a threshold every candidate clears (vols >= 1.0)
    now = 1_760_000_000.0
    samples = [[now - 10, 0.001 * i] for i in range(BOT.MIN_REFIT_N)]
    monkeypatch.setattr(BOT.time, "time", lambda: now)
    BOT.run_cycle(conn, samples, seen={}, bootstrap=bootstrap)
    return emitted, logged, conn


def test_bootstrap_cycle_samples_but_never_posts(monkeypatch):
    emitted, logged, _ = _cycle_fixture(monkeypatch, n_candidates=3, bootstrap=True)
    assert emitted == []
    assert logged == []  # bootstrap leaves no prediction rows either


def test_cap_keeps_the_strongest_of_a_burst_and_counts_the_rest(monkeypatch):
    n = BOT.MAX_EMITS_PER_CYCLE + 3
    emitted, logged, conn = _cycle_fixture(monkeypatch, n_candidates=n)
    assert len(emitted) == BOT.MAX_EMITS_PER_CYCLE
    # sorted strongest-first: the highest-vol symbols are the ones that posted
    assert [s for s, _ in emitted] == [f"S{i}USDT" for i in range(n - 1, n - 1 - BOT.MAX_EMITS_PER_CYCLE, -1)]
    # every evaluated candidate leaves a prediction row, posted or not
    assert len(logged) == n
    conn.commit.assert_called_once()  # the caller commits (hard rule 8)


def test_cap_reports_the_full_suppressed_count_not_the_tail(monkeypatch, caplog):
    """The count in the cycle log must be the whole dropped burst.

    The first version assigned `suppressed` inside the candidate loop, so every
    later candidate overwrote it and the line ended at the remaining tail — a
    15-candidate drop reported as 1. That defeats the very "no silent caps" rule
    the cap was given, and it is invisible to a test that only counts emissions,
    so the number itself is asserted here.
    """
    n = BOT.MAX_EMITS_PER_CYCLE + 15
    with caplog.at_level(logging.INFO, logger=BOT.logger.name):
        emitted, _, _ = _cycle_fixture(monkeypatch, n_candidates=n)
    assert len(emitted) == BOT.MAX_EMITS_PER_CYCLE
    cycle_lines = [r.message for r in caplog.records if r.message.startswith("FIF2 cycle:")]
    assert len(cycle_lines) == 1
    assert f"{n - BOT.MAX_EMITS_PER_CYCLE} over the per-cycle cap" in cycle_lines[0]


# ── the entry anchor (T-2026-KYT-9050-115) ───────────────────────────────────


def test_the_ladder_hangs_off_the_posting_price_not_the_source_entry(monkeypatch):
    """The defect this replaced: the ladder was built on the SOURCE signal's entry1
    — the price the originating bot saw when IT fired, up to MAX_MIRROR_AGE_S plus
    that leg's insert latency earlier. The t104 bracket is a percentage geometry, so
    anchoring it on a price this mirror is not opened at posts a risk/reward T-111
    never priced.

    Source entry and market are deliberately different, so an implementation that
    kept using ``cand["entry"]`` cannot pass.
    """
    seen: list[dict] = []
    monkeypatch.setattr(BOT, "post_ai_signal_gated", lambda conn, **kw: seen.append(kw) or 1)
    monkeypatch.setattr(BOT, "get_max_leverage", lambda *_a, **_k: 20)
    cand = {"id": 7, "symbol": "XUSDT", "model": "EPD3", "direction": "LONG", "entry": 100.0, "age_sec": 30.0}
    assert BOT.emit(MagicMock(), cand, vol=1.2, confidence=0.85, market=110.0) is True

    kw = seen[0]
    assert kw["entry1"] == pytest.approx(110.0)
    assert kw["entry2"] == pytest.approx(110.0)
    assert kw["targets"] == pytest.approx([110.0 * 1.04, 110.0 * 1.05])
    assert kw["sl"] == pytest.approx(110.0 * 0.95)


def test_drift_is_direction_aware():
    """Chasing means the opposite thing per side: for a LONG the market has run UP
    since the source signalled, for a SHORT it has run DOWN."""
    assert BOT.drift_consumed_pct("LONG", 100.0, 101.0) == pytest.approx(1.0)
    assert BOT.drift_consumed_pct("LONG", 100.0, 99.0) == pytest.approx(-1.0)
    assert BOT.drift_consumed_pct("SHORT", 100.0, 99.0) == pytest.approx(1.0)
    assert BOT.drift_consumed_pct("SHORT", 100.0, 101.0) == pytest.approx(-1.0)


def test_the_drift_bound_scales_with_each_direction_s_own_tp1():
    """LONG TP1 is 4 %, SHORT TP1 is 3 % — one shared absolute number would be a
    different fraction of the geometry on each side."""
    for direction in ("LONG", "SHORT"):
        assert BOT.max_drift_pct(direction) == pytest.approx(
            BOT.DRIFT_CONSUMED_FRAC_OF_TP1 * BOT.TP_PCTS[direction][0]
        )
        assert 0.0 < BOT.max_drift_pct(direction) < BOT.TP_PCTS[direction][0]
    assert BOT.max_drift_pct("LONG") != BOT.max_drift_pct("SHORT")


def test_a_mirror_that_would_chase_the_move_is_not_posted(monkeypatch):
    """Above the bound the mirror is buying the tail of a move the source already
    signalled — not the trade T-111 filled at signal price."""
    beyond = 100.0 * (1.0 + 2.0 * BOT.max_drift_pct("LONG") / 100.0)
    prices = {"S0USDT": beyond, "S1USDT": 100.0}
    emitted, logged, _ = _cycle_fixture(monkeypatch, n_candidates=2, prices=prices)
    assert [s for s, _ in emitted] == ["S1USDT"], emitted
    # The chased candidate still leaves a prediction row — that is the shadow record
    # the gate gets re-calibrated from.
    assert len(logged) == 2


def test_a_candidate_without_a_live_anchor_leaves_no_sample_and_no_row(monkeypatch):
    """Same doctrine as a voided vol: a candidate we cannot price is not one we
    could have traded, so it belongs in neither the gate's candidate population nor
    the prediction book. Falling back to the source entry would reintroduce exactly
    the stale anchor this change removed."""
    emitted, logged, _ = _cycle_fixture(monkeypatch, n_candidates=2, prices={"S1USDT": 100.0})
    assert [s for s, _ in emitted] == ["S1USDT"], emitted
    assert len(logged) == 1, "the unpriced candidate must not leave a prediction row"


def test_an_empty_batch_evaluates_nothing_and_marks_nothing_seen(monkeypatch):
    """An empty batch is a transport failure, not a verdict. Marking the burst seen
    would drop it permanently — those candidates are still inside MAX_MIRROR_AGE_S
    next cycle. And the fallback must NOT be per-symbol HTTP for the whole burst:
    that is the P2.44 regression, one call per coin, at a 60 s poll."""
    conn = MagicMock()
    cands = [
        {"id": i, "symbol": f"S{i}USDT", "model": "EPD3", "direction": "LONG", "entry": 100.0, "age_sec": 30.0}
        for i in range(3)
    ]
    per_symbol_calls: list[str] = []
    monkeypatch.setattr(BOT, "fetch_fresh_signals", lambda _c: cands)
    monkeypatch.setattr(BOT, "get_live_prices_batch", lambda: {})
    monkeypatch.setattr(BOT, "posting_anchor", lambda s, *_a, **_k: per_symbol_calls.append(s))
    monkeypatch.setattr(BOT, "sym_vol_4h", lambda _c, _s: 1.0)
    monkeypatch.setattr(BOT, "emit", lambda *_a, **_k: pytest.fail("nothing may post without an anchor"))

    seen: dict[int, float] = {}
    samples: list[list[float]] = []
    BOT.run_cycle(conn, samples, seen, bootstrap=False)

    assert seen == {}, "an unevaluated candidate must stay eligible for the next cycle"
    assert samples == [], "the gate distribution must not absorb candidates that were never priced"
    assert per_symbol_calls == [], "an empty batch must not degrade into one HTTP call per coin"

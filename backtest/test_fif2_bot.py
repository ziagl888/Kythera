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
import os
import sys
import unittest.mock as mock
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
    """Load the numerically named bot module with DB + config stubbed out."""
    spec = importlib.util.spec_from_file_location("fif2_bot", os.path.join(ROOT, "43_ai_fif2_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "core.database": MagicMock(),
            "core.config": MagicMock(CH_FIF2=CHANNEL),
        },
    ):
        spec.loader.exec_module(mod)
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


def _cycle_fixture(monkeypatch, n_candidates: int, bootstrap: bool = False):
    """Run one cycle against stubs: every candidate has vol i+1 (strongest last
    in arrival order), the trailing distribution is warm, nothing is open."""
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
    monkeypatch.setattr(BOT, "emit", lambda _c, cand, vol, conf: emitted.append((cand["symbol"], vol)) or True)
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

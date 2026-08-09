# backtest/test_liq_exit_replay_study.py — DB-free tests for the liq-cascade exit replay
"""Synthetic-fixture tests for tools/liq_exit_replay_study.py
(T-2026-KYT-9050-121). No DB, no snapshot file.

Run standalone (repo convention — backtest suites per file):
  python -m pytest backtest/test_liq_exit_replay_study.py -q
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.liq_exit_replay_study import (  # noqa: E402
    cascade_trigger_indices,
    first_qualifying_exit,
    mark_pct,
    per_slot_day_baseline,
    prepare_book,
    replay_variant,
    run_replay,
    summarize_variant,
)

_MIN = 60 * 1_000_000_000  # ns per minute


def ns(minutes: float) -> int:
    """Test clock: minutes since an arbitrary epoch, as int64 ns."""
    return int(minutes * _MIN)


# ── mechanics ─────────────────────────────────────────────────────────────────


def test_mark_pct_signs():
    assert mark_pct("LONG", 100.0, 110.0) == pytest.approx(10.0)
    assert mark_pct("LONG", 100.0, 90.0) == pytest.approx(-10.0)
    assert mark_pct("SHORT", 100.0, 90.0) == pytest.approx(10.0)
    assert mark_pct("SHORT", 100.0, 110.0) == pytest.approx(-10.0)


def test_cascade_trigger_indices():
    # 3 events within 60m starting at the 3rd; a 4th far later starts nothing
    ts = np.array([ns(0), ns(10), ns(50), ns(500)], dtype=np.int64)
    idx = cascade_trigger_indices(ts, k=3, w_ns=ns(60))
    assert list(idx) == [2]
    # k=2 within 15m: (0,10) triggers at 1; (10,50) and (50,500) are too far apart
    idx2 = cascade_trigger_indices(ts, k=2, w_ns=ns(15))
    assert list(idx2) == [1]
    # fewer events than k → nothing
    assert len(cascade_trigger_indices(ts[:2], k=3, w_ns=ns(60))) == 0


def test_first_qualifying_exit_life_window_and_condition():
    # LONG @100. Life = (t=20 .. t=100). Events (SELL side, against LONG):
    ev_ts = np.array([ns(0), ns(5), ns(10), ns(30), ns(35), ns(40), ns(70), ns(72), ns(100)], dtype=np.int64)
    ev_px = np.array([99.0, 98.0, 97.0, 99.0, 98.5, 98.0, 94.0, 93.0, 90.0])
    # The pre-entry cascade (0,5,10) must NOT trigger — first in-life cascade
    # completes at t=40 (30,35,40), price 98 → mark −2%.
    hit = first_qualifying_exit("LONG", 100.0, ns(20), ns(100), ev_ts, ev_px, k=3, w_ns=ns(60), min_loss_pct=None)
    assert hit is not None
    trig_ns, trig_px, cf = hit
    assert trig_ns == ns(40) and trig_px == 98.0 and cf == pytest.approx(-2.0)
    # Condition −5%: the t=40 cascade (mark −2%) does not qualify; the rule
    # re-arms and the cascade completing at t=70 (35,40,70 span 35m ≤ 60m,
    # price 94 → −6%) qualifies.
    hit2 = first_qualifying_exit("LONG", 100.0, ns(20), ns(100), ev_ts, ev_px, k=3, w_ns=ns(60), min_loss_pct=-5.0)
    assert hit2 is not None and hit2[0] == ns(70) and hit2[2] == pytest.approx(-6.0)
    # Truncated life (ends t=69): only the −2% cascade at t=40 exists → the
    # −5% condition never qualifies.
    assert (
        first_qualifying_exit("LONG", 100.0, ns(20), ns(69), ev_ts, ev_px, k=3, w_ns=ns(60), min_loss_pct=-5.0) is None
    )


# ── replay + accounting ───────────────────────────────────────────────────────


def _book(rows):
    df = pd.DataFrame(rows)
    for c in ("opened_at", "closed_at"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return prepare_book(df)


def _liq(rows):
    df = pd.DataFrame(rows, columns=["ts", "symbol", "side", "avg_price", "value_usdt"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).astype("datetime64[ns, UTC]")
    return df


def test_replay_variant_and_slot_accounting():
    # One LONG that rides to a deep SL (−8) with a cascade at −2 on the way,
    # one LONG with no events at all (untouched).
    book = _book(
        [
            {
                "id": 1,
                "symbol": "AAAUSDT",
                "model": "M",
                "direction": "LONG",
                "entry": 100.0,
                "sl": 90.0,
                "opened_at": "2026-08-05 10:00",
                "closed_at": "2026-08-05 20:00",
                "close_reason": "SL_HIT",
                "close_mark_pct": -8.0,
            },
            {
                "id": 2,
                "symbol": "BBBUSDT",
                "model": "M",
                "direction": "LONG",
                "entry": 50.0,
                "sl": 45.0,
                "opened_at": "2026-08-05 10:00",
                "closed_at": "2026-08-05 12:00",
                "close_reason": "TRAIL",
                "close_mark_pct": 2.0,
            },
        ]
    )
    liq = _liq(
        [
            ("2026-08-05 11:00", "AAAUSDT", "SELL", 99.0, 1.0),
            ("2026-08-05 11:10", "AAAUSDT", "SELL", 98.5, 1.0),
            ("2026-08-05 11:20", "AAAUSDT", "SELL", 98.0, 1.0),  # cascade completes → mark −2%
        ]
    )
    from tools.liq_exit_replay_study import against_events

    trig = replay_variant(book, against_events(liq), k=3, w_min=60, min_loss_pct=None)
    assert list(trig["id"]) == [1]
    row = trig.iloc[0]
    assert row["counterfactual"] == pytest.approx(-2.0)
    assert row["delta"] == pytest.approx(6.0)  # −2 instead of −8
    # freed slot time: closed 20:00 − trigger 11:20 = 8h40m ≈ 0.3611 days
    assert row["freed_days"] == pytest.approx(8.6667 / 24, abs=1e-3)
    # conditional −5% never fires (cascade mark −2%)
    assert len(replay_variant(book, against_events(liq), k=3, w_min=60, min_loss_pct=-5.0)) == 0

    # accounting: baseline per slot-day over the 2-position book
    psd = per_slot_day_baseline(book)
    days_total = (10 + 2) / 24  # 10h + 2h
    assert psd == pytest.approx((-8.0 + 2.0) / days_total)
    summary = summarize_variant(trig, book, psd)
    assert summary["n_triggered"] == 1 and summary["n_book"] == 2
    assert summary["delta_sum"] == pytest.approx(6.0)
    assert summary["slot_credit"] == pytest.approx(row["freed_days"] * psd, abs=0.005)  # report rounds to 2dp
    assert summary["delta_incl_slot_credit"] == pytest.approx(6.0 + row["freed_days"] * psd, abs=0.005)
    assert summary["by_close_reason"]["SL_HIT"]["n"] == 1


def test_run_replay_restricts_to_liq_coverage():
    # Position opened before liq coverage begins → excluded (a cascade could
    # have happened where we have no data).
    book_rows = [
        {
            "id": 1,
            "symbol": "AAAUSDT",
            "model": "M",
            "direction": "SHORT",
            "entry": 100.0,
            "sl": 110.0,
            "opened_at": "2026-08-01 10:00",
            "closed_at": "2026-08-05 20:00",
            "close_reason": "TRAIL",
            "close_mark_pct": 1.0,
        },
        {
            "id": 2,
            "symbol": "AAAUSDT",
            "model": "M",
            "direction": "SHORT",
            "entry": 100.0,
            "sl": 110.0,
            "opened_at": "2026-08-05 10:00",
            "closed_at": "2026-08-05 20:00",
            "close_reason": "TRAIL",
            "close_mark_pct": 1.0,
        },
    ]
    trailing = pd.DataFrame(book_rows)
    liq = _liq(
        [
            ("2026-08-05 09:00", "AAAUSDT", "BUY", 101.0, 1.0),
            ("2026-08-06 09:00", "AAAUSDT", "BUY", 102.0, 1.0),
        ]
    )
    res = run_replay(trailing, liq)
    assert res["book"]["n"] == 1  # only id=2 lies fully inside coverage
    # SHORT with a single BUY event inside life: no k>=2 cascade → nothing triggered
    assert all(v["n_triggered"] == 0 for v in res["variants"].values())

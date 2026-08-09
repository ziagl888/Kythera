# backtest/test_squeeze_flatten_replay.py — DB-free tests for the squeeze-flatten replay
"""Synthetic-fixture tests for tools/squeeze_flatten_replay.py and the shared
episode detector in tools/funding_liq_gate_study.py (T-2026-KYT-9050-123).

Run standalone (repo convention — backtest suites per file):
  python -m pytest backtest/test_squeeze_flatten_replay.py -q
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.funding_liq_gate_study import (  # noqa: E402
    MKT_SQUEEZE_BUY_SYMS,
    market_breadth_minutes,
    squeeze_episodes,
)
from tools.liq_exit_replay_study import prepare_book  # noqa: E402
from tools.squeeze_flatten_replay import asof_price, flatten_replay, ticker_arrays  # noqa: E402


def make_liq(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "symbol", "side"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).astype("datetime64[ns, UTC]")
    return df


def burst(minute: str, n_syms: int, side: str) -> list[tuple[str, str, str]]:
    return [(minute, f"S{i}USDT", side) for i in range(n_syms)]


# ── episode detection (shared detector) ───────────────────────────────────────


def test_breadth_and_episode_detection():
    # One-minute BUY burst across 120 distinct symbols (> threshold 110) with
    # only 3 SELL prints → SHORT_SQUEEZE. The rolling 15-min window keeps the
    # breadth elevated for the following 14 minutes.
    rows = burst("2026-08-05 12:00:00", 120, "BUY")
    rows += [("2026-08-05 12:00:30", "XAUSDT", "SELL"), ("2026-08-05 12:00:31", "XBUSDT", "SELL")]
    # quiet tail so the frame extends past the window
    rows += [("2026-08-05 12:30:00", "S0USDT", "SELL")]
    liq = make_liq(rows)
    b = market_breadth_minutes(liq)
    assert b.loc[pd.Timestamp("2026-08-05 12:00", tz="UTC"), "buy"] == 120
    eps = squeeze_episodes(b)
    assert list(eps["side"]) == ["SHORT_SQUEEZE"]
    assert eps.iloc[0]["start"] == pd.Timestamp("2026-08-05 12:00", tz="UTC")
    # window effect: episode persists exactly while the burst is inside the 15m window
    assert eps.iloc[0]["end"] == pd.Timestamp("2026-08-05 12:14", tz="UTC")
    assert MKT_SQUEEZE_BUY_SYMS <= 120


def test_no_episode_when_symmetric():
    rows = burst("2026-08-05 12:00:00", 120, "BUY") + burst("2026-08-05 12:00:00", 120, "SELL")
    eps = squeeze_episodes(market_breadth_minutes(make_liq(rows)))
    assert len(eps) == 0  # broad but symmetric → no directional episode


# ── pricing ───────────────────────────────────────────────────────────────────


def _ns(s: str) -> int:
    return int(pd.Timestamp(s, tz="UTC").value)


def test_asof_price_tolerance():
    ticker = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-08-05 11:59:00", "2026-08-05 12:00:30"], utc=True),
            "symbol": ["AAAUSDT", "AAAUSDT"],
            "price": [100.0, 101.0],
        }
    )
    packs = ticker_arrays(ticker)
    tol = 180 * 1_000_000_000
    # at 12:01 the 12:00:30 print (30s old) is the mark
    assert asof_price(packs["AAAUSDT"], _ns("2026-08-05 12:01:00"), tol) == 101.0
    # at 12:06 the last print is 5.5 min old → outside 180s tolerance
    assert asof_price(packs["AAAUSDT"], _ns("2026-08-05 12:06:00"), tol) is None
    assert asof_price(packs.get("MISSING"), _ns("2026-08-05 12:01:00"), tol) is None


# ── flatten replay ────────────────────────────────────────────────────────────


def _book(rows):
    df = pd.DataFrame(rows)
    for c in ("opened_at", "closed_at"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return prepare_book(df)


def test_flatten_selects_side_open_at_onset_first_episode_wins():
    # SHORT_SQUEEZE episodes at 12:00-12:14 and 13:00-13:14 → onsets 12:01 / 13:01.
    episodes = pd.DataFrame(
        {
            "side": ["SHORT_SQUEEZE", "SHORT_SQUEEZE"],
            "start": pd.to_datetime(["2026-08-05 12:00", "2026-08-05 13:00"], utc=True),
            "end": pd.to_datetime(["2026-08-05 12:14", "2026-08-05 13:14"], utc=True),
        }
    )
    book = _book(
        [
            # SHORT open across both onsets, realized deep loss → flattened at the FIRST onset
            {
                "id": 1,
                "symbol": "AAAUSDT",
                "direction": "SHORT",
                "entry": 100.0,
                "sl": 110.0,
                "opened_at": "2026-08-05 10:00",
                "closed_at": "2026-08-05 20:00",
                "close_reason": "SL_HIT",
                "close_mark_pct": -8.0,
            },
            # LONG rides the squeeze → never flattened by SHORT_SQUEEZE
            {
                "id": 2,
                "symbol": "AAAUSDT",
                "direction": "LONG",
                "entry": 100.0,
                "sl": 90.0,
                "opened_at": "2026-08-05 10:00",
                "closed_at": "2026-08-05 20:00",
                "close_reason": "TRAIL",
                "close_mark_pct": 2.0,
            },
            # SHORT already closed before the onset → untouched
            {
                "id": 3,
                "symbol": "AAAUSDT",
                "direction": "SHORT",
                "entry": 100.0,
                "sl": 110.0,
                "opened_at": "2026-08-05 09:00",
                "closed_at": "2026-08-05 11:00",
                "close_reason": "TRAIL",
                "close_mark_pct": 1.0,
            },
            # SHORT open but symbol has no ticker print → excluded, counted
            {
                "id": 4,
                "symbol": "NOPRICEUSDT",
                "direction": "SHORT",
                "entry": 50.0,
                "sl": 55.0,
                "opened_at": "2026-08-05 10:00",
                "closed_at": "2026-08-05 20:00",
                "close_reason": "TIME_STOP",
                "close_mark_pct": -1.0,
            },
        ]
    )
    ticker = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-08-05 12:00:40", "2026-08-05 13:00:40"], utc=True),
            "symbol": ["AAAUSDT", "AAAUSDT"],
            "price": [103.0, 106.0],
        }
    )
    trig, stats = flatten_replay(book, episodes, ticker)
    assert list(trig["id"]) == [1]
    row = trig.iloc[0]
    # SHORT @100 closed at 103 → mark −3 (not −6 from the later episode)
    assert row["counterfactual"] == pytest.approx(-3.0)
    assert row["delta"] == pytest.approx(5.0)
    # freed time from the FIRST onset 12:01 to close 20:00
    assert row["freed_days"] == pytest.approx((7 + 59 / 60) / 24, abs=1e-3)
    assert stats["no_price_total"] == 2  # id=4 at both episodes (never flattened)
    assert stats["episodes"][0]["flattened"] == 1
    assert stats["episodes"][1]["flattened"] == 0  # id=1 already flattened


def test_flatten_replay_empty_episodes():
    book = _book(
        [
            {
                "id": 1,
                "symbol": "AAAUSDT",
                "direction": "SHORT",
                "entry": 100.0,
                "sl": 110.0,
                "opened_at": "2026-08-05 10:00",
                "closed_at": "2026-08-05 20:00",
                "close_reason": "TRAIL",
                "close_mark_pct": 1.0,
            },
        ]
    )
    trig, stats = flatten_replay(
        book, pd.DataFrame(columns=["side", "start", "end"]), pd.DataFrame(columns=["ts", "symbol", "price"])
    )
    assert len(trig) == 0 and stats["no_price_total"] == 0


def test_ticker_arrays_drops_nan_prices():
    ticker = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-08-05 12:00:00", "2026-08-05 12:00:10"], utc=True),
            "symbol": ["AAAUSDT", "AAAUSDT"],
            "price": [np.nan, 100.0],
        }
    )
    ts, px = ticker_arrays(ticker)["AAAUSDT"]
    assert len(ts) == 1 and px[0] == 100.0

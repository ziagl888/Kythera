# backtest/test_candle_window_start.py — T-2026-CU-9050-181, DB-free.
"""
Byte-parity guard for the TimescaleDB chunk-exclusion fix.

The six hot serving reads (bots 11/12/15/24/25 + core/research_features) used to
call read_candles_with_indicators with `limit`/`end` but NO lower open_time bound,
so the planner scanned all 126 chunks of the candles/indicators hypertables
("Chunks excluded: 0"). The fix adds `start = window_start(tf, limit[, end])` to
each. The delivered rows MUST NOT change — only fewer chunks get scanned.

This module proves that DB-free, on two layers (same split as the T-172 detector
scan-optimization test):

  * `window_start` arithmetic is pure and unit-testable: it returns
    `anchor − limit·tf − 30d`, tz-aware, tf-aware, validated.

  * A faithful reader (`_deliver`) reproduces the documented read contract of
    core.candles — inclusive [start, end] window, forming rows dropped, the
    NEWEST `limit` rows, ASC. It mirrors _windowed_select / _read_joined_hyper;
    the SQL-level equivalence of that contract to hand-written SQL is already the
    subject of backtest/test_candles_db_parity.py (VPS). On top of it we assert:
    for a continuously-ingested coin the bounded read (`start=window_start(...)`)
    returns the SAME rows as the un-bounded read — with and without gaps — and
    that the only frames that differ (a coin whose whole history predates the
    window) were going to be rejected downstream anyway, so the decision is equal.

Run with: pytest backtest/test_candle_window_start.py -v
      or: python backtest/test_candle_window_start.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.candles import (  # noqa: E402
    CANDLE_READ_GAP_BUFFER,
    period_start,
    timeframe_delta,
    window_start,
)

# A fixed "now" so every derivation is deterministic (Date.now is banned in the
# harness anyway). Hour-aligned so the 1h forming cutoff lands on a candle edge.
NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── window_start arithmetic (pure) ────────────────────────────────────────────


def test_window_start_formula_no_end():
    for tf, limit in [("1h", 100), ("1h", 500), ("4h", 150), ("1h", 1), ("1h", 1500)]:
        got = window_start(tf, limit, now=NOW)
        expect = NOW - limit * timeframe_delta(tf) - CANDLE_READ_GAP_BUFFER
        assert got == expect, (tf, limit)
    print("OK  window_start = now − limit·tf − 30d (no end)")


def test_window_start_formula_with_end():
    end = NOW - timedelta(hours=1)
    got = window_start("1h", 1, end=end)
    assert got == end - 1 * timeframe_delta("1h") - CANDLE_READ_GAP_BUFFER
    # `end` wins over `now` when both are given (as-of reads anchor on end).
    got2 = window_start("1h", 1, end=end, now=NOW + timedelta(days=999))
    assert got2 == got
    print("OK  window_start anchors on `end` when provided")


def test_window_start_reproduces_brief_worked_example():
    """The brief's example: the widest serving read (ATB2 limit=1500 on 1h) must
    yield ≈90d — 62.5d of history + the 30d gap buffer."""
    span = NOW - window_start("1h", 1500, now=NOW)
    assert timedelta(days=89) <= span <= timedelta(days=93), span
    print(f"OK  1h × 1500 ≈ {span.days}d (brief's ~90d)")


def test_window_start_validation():
    for bad in (0, -5, 1.5, None):
        try:
            window_start("1h", bad, now=NOW)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f"limit={bad!r} should have raised")
    # unknown tf
    try:
        window_start("3h", 100, now=NOW)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown tf should raise")
    # naive anchor
    try:
        window_start("1h", 100, now=NOW.replace(tzinfo=None))
    except ValueError:
        pass
    else:
        raise AssertionError("naive anchor should raise")
    print("OK  window_start rejects bad limit / tf / naive anchor")


# ── faithful reader: the documented core.candles read contract ────────────────


def _deliver(
    frame: pd.DataFrame,
    tf: str,
    *,
    limit: int | None,
    start: datetime | None,
    end: datetime | None,
    now: datetime,
    include_forming: bool = False,
) -> pd.DataFrame:
    """Mirror of read_candles*'s window semantics (DB-free).

    Inclusive [start, end] on open_time, forming rows (open_time ≥ period_start)
    dropped unless include_forming, then the NEWEST `limit` rows, returned ASC —
    exactly what _windowed_select builds (DESC + LIMIT wrapped in an ASC select).
    """
    out = frame.sort_values("open_time").reset_index(drop=True)
    if start is not None:
        out = out[out["open_time"] >= start]
    if end is not None:
        out = out[out["open_time"] <= end]
    if not include_forming:
        cutoff = period_start(tf, now)
        out = out[out["open_time"] < cutoff]
    if limit is not None:
        out = out.tail(limit)
    return out.reset_index(drop=True)


def _continuous_1h(n_hours: int, *, gaps: tuple[tuple[int, int], ...] = ()) -> pd.DataFrame:
    """A continuously-ingested 1h candle frame ending at the last closed hour
    before NOW. `gaps` drops [from, to) hour-indices to simulate ingestion outages.
    Values encode the open_time so a dropped/re-ordered row is visible."""
    last_closed = period_start("1h", NOW) - timeframe_delta("1h")
    rows = []
    dropped = {i for a, b in gaps for i in range(a, b)}
    for i in range(n_hours):
        if i in dropped:
            continue
        ot = last_closed - i * timeframe_delta("1h")
        rows.append((ot, 100.0 + i, 10.0 + i, i))  # open_time, close, volume, rsi_14
    df = pd.DataFrame(rows, columns=["open_time", "close", "volume", "rsi_14"])
    return df.sort_values("open_time").reset_index(drop=True)


def _continuous_4h(n_bars: int) -> pd.DataFrame:
    last_closed = period_start("4h", NOW) - timeframe_delta("4h")
    rows = [(last_closed - i * timeframe_delta("4h"), 100.0 + i, 10.0 + i, i) for i in range(n_bars)]
    df = pd.DataFrame(rows, columns=["open_time", "close", "volume", "rsi_14"])
    return df.sort_values("open_time").reset_index(drop=True)


def _assert_bounded_equals_unbounded(frame, tf, *, limit, start, end):
    unbounded = _deliver(frame, tf, limit=limit, start=None, end=end, now=NOW)
    bounded = _deliver(frame, tf, limit=limit, start=start, end=end, now=NOW)
    assert len(bounded) == len(unbounded) > 0, (tf, limit)
    assert bounded.equals(unbounded), (tf, limit)
    # the parity precondition, made explicit: start sits at/below the oldest row
    # the un-bounded read delivered → it cannot drop any of them.
    assert start <= unbounded["open_time"].min()


# ── byte-parity at every fleet call-site (tf, limit) ──────────────────────────

# (tf, limit, has_end) for the six serving reads.
FLEET_READS = [
    ("1h", 100, False),  # 11_ai_mis
    ("1h", 500, False),  # 12_ai_ats
    ("1h", 1, True),  # 15_ai_master (end = floor-1h)
    ("1h", 100, False),  # 24_quasimodo (1h)
    ("1h", 150, False),  # 25_smc (1h)
    ("4h", 150, False),  # 25_smc (4h)
    ("1h", 60, False),  # core/research_features (default lookback)
]


def test_fleet_reads_byte_parity_continuous():
    """For a continuously-ingested coin the bounded read returns byte-identical
    rows to the un-bounded read, at every fleet (tf, limit)."""
    for tf, limit, has_end in FLEET_READS:
        # generous history (far more than the window) so `tail(limit)` is real
        frame = _continuous_4h(3000) if tf == "4h" else _continuous_1h(6000)
        end = period_start(tf, NOW) - timeframe_delta(tf) if has_end else None
        start = window_start(tf, limit, end=end, now=NOW)
        _assert_bounded_equals_unbounded(frame, tf, limit=limit, start=start, end=end)
    print("OK  byte-parity: bounded == un-bounded read at all 6 fleet call-sites")


def test_byte_parity_survives_ingestion_gaps():
    """A 14h outage (the largest observed) inside the window must not shorten the
    served history: the 30d buffer swallows it, rows stay identical."""
    frame = _continuous_1h(6000, gaps=((5, 19), (200, 214)))  # two ~14h holes
    for limit in (100, 500):
        start = window_start("1h", limit, now=NOW)
        _assert_bounded_equals_unbounded(frame, "1h", limit=limit, start=start, end=None)
    print("OK  byte-parity: two 14h ingestion gaps do not shorten the served history")


def test_short_history_coin_unchanged():
    """A freshly-listed coin with FEWER than `limit` candles (all recent) returns
    all of them either way — the bound never clips a young coin."""
    frame = _continuous_1h(40)  # < any fleet limit
    start = window_start("1h", 100, now=NOW)
    unbounded = _deliver(frame, "1h", limit=100, start=None, end=None, now=NOW)
    bounded = _deliver(frame, "1h", limit=100, start=start, end=None, now=NOW)
    assert bounded.equals(unbounded)
    assert 0 < len(bounded) < 100
    print("OK  byte-parity: young coin (<limit candles) unchanged by the bound")


def test_stale_coin_reaches_the_same_decision():
    """The ONLY frames that differ: a coin whose whole history predates the window.
    The un-bounded read returns its (stale) tail; the bounded read returns empty.
    Both hit a downstream reject — a `len(df) < floor` gate or the staleness guard
    — so the trading decision is identical. Modelled here with the MIS `len < 10`
    floor and the master 3h staleness guard."""
    # candles that stopped ~60 days ago — older than the ~34d window (limit=100)
    last = period_start("1h", NOW) - timedelta(days=60)
    rows = [(last - i * timeframe_delta("1h"), 100.0 + i, 10.0, i) for i in range(300)]
    frame = (
        pd.DataFrame(rows, columns=["open_time", "close", "volume", "rsi_14"])
        .sort_values("open_time")
        .reset_index(drop=True)
    )

    start = window_start("1h", 100, now=NOW)
    unbounded = _deliver(frame, "1h", limit=100, start=None, end=None, now=NOW)
    bounded = _deliver(frame, "1h", limit=100, start=start, end=None, now=NOW)

    # MIS floor `if len(df) < 10: return None` — unbounded has stale rows, bounded
    # is empty, but the >3h-stale rows would never have produced a live signal.
    assert len(unbounded) >= 10 and len(bounded) == 0
    # master-style staleness: newest delivered row is >3h before NOW on BOTH paths
    # (unbounded delivers a stale row that the guard rejects; bounded delivers none).
    newest_unbounded = unbounded["open_time"].max()
    assert (period_start("1h", NOW) - newest_unbounded) > timedelta(hours=3)
    print("OK  stale coin: bounded-empty vs unbounded-stale reach the same reject")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    fns = [
        test_window_start_formula_no_end,
        test_window_start_formula_with_end,
        test_window_start_reproduces_brief_worked_example,
        test_window_start_validation,
        test_fleet_reads_byte_parity_continuous,
        test_byte_parity_survives_ingestion_gaps,
        test_short_history_coin_unchanged,
        test_stale_coin_reaches_the_same_decision,
    ]
    for fn in fns:
        fn()
    print(f"\nAlle {len(fns)} window_start-Chunk-Exclusion-Tests bestanden (T-2026-CU-9050-181).")

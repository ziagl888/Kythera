"""Guard for the bounded-first lookup in `latest_open_time` (T-2026-KYT-9050-131).

A `MAX(open_time)` without a time predicate cannot exclude chunks, so the planner
plans all ~129 of them: measured 802 ms planning against 5 ms execution. Asking
with a lower bound first drops that to 21 ms.

The bound is a PERFORMANCE hint and must never become a semantic one. That is the
whole risk of this change: a (symbol, tf) whose newest row is older than the
window must still yield that row, not None — otherwise the indicator engine reads
a cold coin as empty and recomputes or skips it. These tests pin the two-stage
shape directly, against a fake cursor, so they hold without a live DB.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DB_PASSWORD", "unit-test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unit-test")
os.environ["KYTHERA_CANDLES_SOURCE"] = "hyper"  # the path under test

from core import candles as C  # noqa: E402

STALE = datetime(2025, 1, 5, 12, tzinfo=timezone.utc)
FRESH = datetime.now(timezone.utc) - timedelta(hours=2)


class _Cur:
    """Cursor that answers each execute() from a queued list and records the SQL."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.queries: list[str] = []
        self.params: list[list] = []

    def execute(self, query, params=None):
        # repr() of a psycopg2 Composed shows its SQL(...) fragments verbatim;
        # as_string() would need a live connection, which is the whole point of
        # not having one here.
        self.queries.append(repr(query))
        self.params.append(list(params) if params else [])

    def fetchone(self):
        return self._answers.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


def test_fresh_data_answers_from_the_bounded_query_alone():
    cur = _Cur([(FRESH,)])
    got = C.latest_open_time(_Conn(cur), "BTCUSDT", "1h", kind="indicators")
    assert got == FRESH
    assert len(cur.queries) == 1, "a hit in the window must not trigger the unbounded scan"
    assert "open_time > " in cur.queries[0]


def test_stale_data_still_returned_via_the_unbounded_fallback():
    """The risk this change had to avoid: a coin whose newest row predates the
    window must NOT read as empty."""
    cur = _Cur([(None,), (STALE,)])
    got = C.latest_open_time(_Conn(cur), "BTCUSDT", "1h", kind="indicators")
    assert got == STALE, "fallback lost the watermark of a stale (symbol, tf)"
    assert len(cur.queries) == 2
    assert "open_time > " in cur.queries[0]
    assert "open_time > " not in cur.queries[1], "the fallback must drop the bound"


def test_genuinely_empty_still_returns_none():
    cur = _Cur([(None,), (None,)])
    assert C.latest_open_time(_Conn(cur), "BTCUSDT", "1h", kind="indicators") is None
    assert len(cur.queries) == 2


def test_bound_is_passed_as_a_parameter_not_inlined():
    """Keeps the plan cacheable and the value out of the SQL text."""
    cur = _Cur([(FRESH,)])
    C.latest_open_time(_Conn(cur), "BTCUSDT", "1h", kind="indicators")
    assert isinstance(cur.params[0][-1], datetime)
    assert cur.params[0][:2] == ["BTCUSDT", "1h"], "scope params must stay first"


def test_lookback_clears_the_longest_timeframe_by_a_margin():
    """1w candles advance once a week — a window that tight would send every
    weekly lookup down the slow path."""
    assert C._LATEST_LOOKBACK >= 4 * C.timeframe_delta("1w")


def test_candles_kind_uses_the_same_bounded_shape():
    cur = _Cur([(FRESH,)])
    C.latest_open_time(_Conn(cur), "BTCUSDT", "1h", kind="candles")
    assert "open_time > " in cur.queries[0]

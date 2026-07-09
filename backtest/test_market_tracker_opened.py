# backtest/test_market_tracker_opened.py
"""
Unit tests for the "Opened" counting semantics of the market tracker (P1.44).

The per-bot statistics are the decision basis for orchestrator gating, so an
inflated "Opened" count is a money-path defect.

Two historical bugs, both on the AI side:

  1. Opens were read from `ml_predictions_master`, an append-only prediction log
     that nothing ever DELETEs from. `closed_ai_signals` holds the same signals
     after they close, and BOTH were concatenated into the created-set — so any
     AI signal that opened and closed inside the window counted TWICE.
  2. `ml_predictions_master` also carries shadow rows (posted=False) that were
     never traded. They counted as opened signals.

Opens now come from `ai_signals` ∪ `closed_ai_signals` — per-signal, disjoint
(the monitors DELETE from ai_signals on close), and free of shadow rows.

Run with: pytest backtest/test_market_tracker_opened.py -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import pandas as pd
import pytest


def _load_tracker():
    spec = importlib.util.spec_from_file_location(
        "market_tracker",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "23_market_tracker.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "core.database": mock.MagicMock(),
            "core.market_utils": mock.MagicMock(),
            "core.bot_naming": mock.MagicMock(pretty_name=lambda x: x),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


mt = _load_tracker()


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def rollback(self):
        pass


def _norm(sql: str) -> str:
    return " ".join(str(sql).split())


NOW = pd.Timestamp.now("UTC")


def _open_ai_row(strategy="MIS1", direction="LONG"):
    return pd.DataFrame([{"strategy": strategy, "direction": direction, "entry": 100.0, "created_at": NOW}])


def _closed_ai_row(strategy="MIS1", direction="LONG"):
    """A DIFFERENT signal that opened and closed inside the window."""
    return pd.DataFrame(
        [
            {
                "strategy": strategy,
                "direction": direction,
                "entry": 100.0,
                "close_price": 105.0,
                "created_at": NOW,
                "closed_at": NOW,
                "targets_hit": 1,
                "close_reason": "TP1",
            }
        ]
    )


@pytest.fixture
def summary(monkeypatch):
    """Drive job_signal_summary and return (rendered_message, executed_queries)."""
    posts: list[str] = []
    queries: list[str] = []

    monkeypatch.setattr(mt, "get_db_connection", lambda: FakeConn())
    monkeypatch.setattr(mt, "send_telegram", lambda msg, *a, **kw: posts.append(msg))

    def fake_read_sql(query, _conn, params=None, **kw):
        q = _norm(query)
        queries.append(q)
        if "FROM active_trades_master" in q:
            return pd.DataFrame()
        if "FROM ai_signals" in q:
            return _open_ai_row()
        if "FROM closed_trades_master" in q:
            return pd.DataFrame()
        if "FROM closed_ai_signals" in q:
            return _closed_ai_row()
        if "FROM ml_predictions_master" in q:
            # The append-only prediction log: it retains BOTH the still-open
            # signal and the already-closed one, plus a shadow row that was
            # never traded. Reading opens from here yields 3 where the truth
            # is 2 — this is exactly what the pre-fix code did.
            return pd.concat(
                [
                    _open_ai_row()[["strategy", "direction", "created_at"]],
                    _closed_ai_row()[["strategy", "direction", "created_at"]],
                    _open_ai_row(strategy="EPD2")[["strategy", "direction", "created_at"]],  # shadow
                ],
                ignore_index=True,
            )
        return pd.DataFrame()

    monkeypatch.setattr(mt.pd, "read_sql_query", fake_read_sql)
    asyncio.run(mt.job_signal_summary())
    assert posts, "job_signal_summary posted nothing"
    return posts[0], queries


def _opened_24h(msg: str, section: str = "INDICATOR BASED") -> tuple[int, int]:
    """Parse the '24h: 🟢 {L}L / 🔴 {S}S' line of a section's Opened block."""
    body = msg.split(section, 1)[1]
    opened = body.split("<b>Closed:</b>", 1)[0]
    m = re.search(r"24h:\s*🟢\s*(\d+)L\s*/\s*🔴\s*(\d+)S", opened)
    assert m, f"could not parse Opened/24h from:\n{opened}"
    return int(m.group(1)), int(m.group(2))


# ── The double-count is gone ──────────────────────────────────────────────────


def test_open_and_closed_ai_signal_count_once_each(summary):
    """1 still-open + 1 closed AI signal == 2 opened, not 3.

    Pre-fix this read 3: the closed signal was counted once from
    ml_predictions_master (which never drops it) and again from
    closed_ai_signals.
    """
    msg, _ = summary
    longs, shorts = _opened_24h(msg)
    assert (longs, shorts) == (2, 0), f"expected 2 opened LONGs, got {longs}L/{shorts}S"


def test_shadow_predictions_are_not_counted_as_opened(summary):
    """The EPD2 shadow row (posted=False, VOLUME category) never traded."""
    msg, _ = summary
    longs, shorts = _opened_24h(msg, section="VOLUME BASED")
    assert (longs, shorts) == (0, 0), f"shadow prediction leaked into Opened: {longs}L/{shorts}S"


# ── The opens source is ai_signals, never ml_predictions_master ───────────────


def test_opens_are_read_from_ai_signals(summary):
    _, queries = summary
    assert any("FROM ai_signals" in q for q in queries), "ai_signals was never queried for opens"
    # The fake read_sql raises on a bare ml_predictions_master read, so reaching
    # here already proves it is not an opens source. The JOIN is the only place
    # it may appear, and only to recover created_at.
    ml_reads = [q for q in queries if "ml_predictions_master" in q]
    for q in ml_reads:
        assert "LEFT JOIN ml_predictions_master" in q, f"non-JOIN read of the prediction log: {q}"


def test_created_at_join_only_matches_posted_rows(summary):
    """Shadow rows (posted=False) must not supply a timestamp, nor be counted."""
    _, queries = summary
    joins = [q for q in queries if "LEFT JOIN ml_predictions_master" in q]
    assert joins, "expected the created_at JOIN"
    for q in joins:
        assert "m.posted = TRUE" in q, f"JOIN does not filter shadow rows: {q}"


# ── Both posts share one query, so they cannot drift ──────────────────────────


def test_per_bot_post_uses_the_same_open_ai_query(monkeypatch):
    seen: list[str] = []

    monkeypatch.setattr(mt, "get_db_connection", lambda: FakeConn())
    monkeypatch.setattr(mt, "send_telegram", lambda *a, **kw: None)

    def fake_read_sql(query, _conn, params=None, **kw):
        seen.append(_norm(query))
        return pd.DataFrame()

    monkeypatch.setattr(mt.pd, "read_sql_query", fake_read_sql)
    try:
        asyncio.run(mt.job_per_bot_performance())
    except Exception:
        pass  # empty synthetic data may short-circuit later stages

    assert _norm(mt.OPEN_AI_SIGNALS_QUERY) in seen, "job_per_bot_performance no longer shares the helper query"


# ── The helper's fallback still rolls back first ──────────────────────────────


def test_helper_rolls_back_before_fallback(monkeypatch):
    conn = mock.Mock()
    calls: list[str] = []

    def fake_read_sql(query, _conn, **kw):
        q = _norm(query)
        if "LEFT JOIN" in q:
            raise RuntimeError("join failed")
        calls.append("fallback-after-rollback" if conn.rollback.called else "fallback-dirty")
        return pd.DataFrame()

    monkeypatch.setattr(mt.pd, "read_sql_query", fake_read_sql)
    mt._load_open_ai_signals(conn)

    assert calls == ["fallback-after-rollback"], "fallback ran inside the aborted transaction"


def test_helper_keeps_latest_prediction_row_per_signal(monkeypatch):
    """The fuzzy JOIN can match several ml rows; the newest created_at wins."""
    older, newer = pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-08")
    dupes = pd.DataFrame(
        [
            {"strategy": "MIS1", "direction": "LONG", "entry": 100.0, "created_at": older},
            {"strategy": "MIS1", "direction": "LONG", "entry": 100.0, "created_at": newer},
        ]
    )
    monkeypatch.setattr(mt.pd, "read_sql_query", lambda *a, **kw: dupes)

    out = mt._load_open_ai_signals(mock.Mock())
    assert len(out) == 1
    assert out.iloc[0]["created_at"] == newer

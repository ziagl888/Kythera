# backtest/test_market_tracker_realized.py
"""
Unit tests for the realized-PnL report helpers in 23_market_tracker
(T-2026-CU-9050-115): window bucketing on close-age and the per-bot block
formatting. Both live at module scope precisely so they can be driven
DB-free (same pattern as the chunker tests).

Run with: pytest backtest/test_market_tracker_realized.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")


def _load_tracker():
    spec = importlib.util.spec_from_file_location(
        "market_tracker_realized",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "23_market_tracker.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    # Pre-seed pandas before patch.dict — see test_market_tracker_chunker.py
    # for why (numpy C-extensions do not survive a torn-out re-import).
    import pandas  # noqa: F401

    with mock.patch.dict(
        "sys.modules",
        {
            "core.database": mock.MagicMock(),
            "core.market_utils": mock.MagicMock(),
            "core.bot_naming": mock.MagicMock(pretty_name=lambda x: x),
            "core.bot_catalog": mock.MagicMock(),
            "core.realized_pnl": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


mt = _load_tracker()


# ── _aggregate_realized_pnl: window bucketing ────────────────────────────────


def test_windows_are_cumulative_on_close_age():
    rows = [
        ("RUB2", 2.0, 10.0),  # inside every window
        ("RUB2", 12.0, 20.0),  # outside 8h, inside 24h..30d
        ("RUB2", 100.0, -30.0),  # outside 3d (72h), inside 7d/30d
        ("RUB2", 500.0, 40.0),  # only inside 30d
    ]
    stats = mt._aggregate_realized_pnl(rows)["RUB2"]
    assert stats["8h"] == {"sum": 10.0, "n": 1, "avg": 10.0}
    assert stats["24h"]["sum"] == pytest.approx(30.0)
    assert stats["24h"]["n"] == 2
    assert stats["3d"]["n"] == 2
    assert stats["7d"]["sum"] == pytest.approx(0.0)
    assert stats["7d"]["n"] == 3
    assert stats["30d"]["sum"] == pytest.approx(40.0)
    assert stats["30d"]["n"] == 4
    assert stats["30d"]["avg"] == pytest.approx(10.0)


def test_window_boundary_is_inclusive():
    stats = mt._aggregate_realized_pnl([("BOT", 8.0, 5.0)])["BOT"]
    assert stats["8h"]["n"] == 1


def test_future_closes_are_dropped():
    assert mt._aggregate_realized_pnl([("BOT", -0.5, 5.0)]) == {}


def test_bots_are_separated():
    stats = mt._aggregate_realized_pnl([("A", 1.0, 5.0), ("B", 1.0, -5.0)])
    assert stats["A"]["8h"]["sum"] == pytest.approx(5.0)
    assert stats["B"]["8h"]["sum"] == pytest.approx(-5.0)


# ── _format_realized_pnl_blocks ──────────────────────────────────────────────


def test_blocks_sorted_by_30d_sum_desc():
    stats = mt._aggregate_realized_pnl(
        [
            ("LOSER", 2.0, -50.0),
            ("WINNER", 2.0, 80.0),
            ("MID", 2.0, 10.0),
        ]
    )
    blocks = mt._format_realized_pnl_blocks(stats)
    order = [b.splitlines()[0] for b in blocks]
    assert order == ["<b>WINNER</b>", "<b>MID</b>", "<b>LOSER</b>"]


def test_block_contains_all_windows_and_placeholder():
    stats = mt._aggregate_realized_pnl([("BOT", 2.0, 12.5)])
    block = mt._format_realized_pnl_blocks(stats)[0]
    lines = block.splitlines()
    assert len(lines) == 1 + len(mt.REALIZED_WINDOWS)
    assert "Σ    +12.5%" in lines[1]
    assert "n=1" in lines[1]
    # every window listed, none silently missing
    for (name, _h), line in zip(mt.REALIZED_WINDOWS, lines[1:]):
        assert line.lstrip().startswith(name)


def test_bot_without_trades_in_window_shows_dash():
    # age 100h: outside 8h/24h/3d, inside 7d/30d
    stats = mt._aggregate_realized_pnl([("BOT", 100.0, 5.0)])
    block = mt._format_realized_pnl_blocks(stats)[0]
    lines = block.splitlines()
    assert lines[1].endswith("—")  # 8h
    assert lines[2].endswith("—")  # 24h
    assert lines[3].endswith("—")  # 3d
    assert "n=1" in lines[4]  # 7d
    assert "n=1" in lines[5]  # 30d


def test_empty_stats_give_no_blocks():
    assert mt._format_realized_pnl_blocks({}) == []


# ── report constants sanity ──────────────────────────────────────────────────


def test_windows_match_operator_spec():
    assert mt.REALIZED_WINDOWS == (
        ("8h", 8.0),
        ("24h", 24.0),
        ("3d", 72.0),
        ("7d", 168.0),
        ("30d", 720.0),
    )

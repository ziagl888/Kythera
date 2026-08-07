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


# ── _is_neutral_close: housekeeping filter for BOTH sources ─────────────────


@pytest.mark.parametrize(
    "reason",
    ["DELISTED", "DELISTED / CLEANUP", "delisted", "ORPHAN sweep", "Cleanup"],
)
def test_housekeeping_closes_are_neutral(reason):
    # Review finding 2026-07-13: 6_housekeeping writes DELISTED markers into
    # closed_trades_master.status too — the classic loop must filter them,
    # or a delisting close scores as a full leveraged move.
    assert mt._is_neutral_close(reason) is True


@pytest.mark.parametrize("reason", ["0", "1", "4", "SL Hit (SL: 1.23)", "ALL TARGETS HIT", "", None])
def test_regular_closes_are_not_neutral(reason):
    assert mt._is_neutral_close(reason) is False


# ── row parsing helpers (N-derivation from DB shapes) ───────────────────────


def test_parse_targets_accepts_list_and_json_string():
    assert mt._parse_targets([105.0, 110.0]) == [105.0, 110.0]
    assert mt._parse_targets("[105.0, 110.0]") == [105.0, 110.0]


@pytest.mark.parametrize("value", [None, "kaputt", "{}", 42, {"a": 1}])
def test_parse_targets_rejects_non_lists(value):
    assert mt._parse_targets(value) is None


def test_classic_targets_derive_n_from_non_null_columns():
    # 3_detectors writes 0 for absent targets; REAL NULLs arrive as NaN.
    assert mt._classic_targets(105.0, 110.0, 0.0, 0.0) == [105.0, 110.0]
    assert mt._classic_targets(105.0, None, float("nan"), -1.0) == [105.0]
    assert mt._classic_targets(0.0, 0.0, 0.0, 0.0) == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [("0", 0), ("2", 2), ("4", 4), (3, 3), ("4.0", 4), (None, 0), ("DELISTED", 0)],
)
def test_parse_hits(status, expected):
    assert mt._parse_hits(status) == expected


# ── report constants sanity ──────────────────────────────────────────────────


def test_windows_match_operator_spec():
    assert mt.REALIZED_WINDOWS == (
        ("8h", 8.0),
        ("24h", 24.0),
        ("3d", 72.0),
        ("7d", 168.0),
        ("30d", 720.0),
    )


# ── open book: _aggregate_open_book (T-2026-KYT-9050-114) ───────────────────


def test_open_book_folds_sum_n_avg_per_bot():
    stats = mt._aggregate_open_book([("BR4H", 100.0), ("BR4H", -40.0), ("SRA1", -10.0)])
    assert stats["BR4H"] == {"sum": 60.0, "n": 2, "avg": 30.0}
    assert stats["SRA1"] == {"sum": -10.0, "n": 1, "avg": -10.0}


def test_open_book_has_no_window_dimension():
    # An open position has no close time — there is nothing to bucket on, and
    # a position older than 30d counts in FULL (operator decision, AK6).
    stats = mt._aggregate_open_book([("BOT", 5.0)])
    assert set(stats["BOT"]) == {"sum", "n", "avg"}


def test_open_book_empty_input_gives_empty_stats():
    assert mt._aggregate_open_book([]) == {}


# ── open book: rendering into the per-bot blocks ────────────────────────────


def _closed(bot="BOT", age_h=2.0, pnl=100.0):
    return mt._aggregate_realized_pnl([(bot, age_h, pnl)])


def test_open_stats_none_reproduces_pre_114_block():
    # None = "no open book this run" (ticker down) → byte-identical to the
    # pre-114 output, so a ticker outage cannot change the closed report.
    block = mt._format_realized_pnl_blocks(_closed(), None)[0]
    assert len(block.splitlines()) == 1 + len(mt.REALIZED_WINDOWS)
    assert "open" not in block
    assert "Σall" not in block


def test_open_and_combined_lines_are_appended():
    blocks = mt._format_realized_pnl_blocks(_closed(pnl=100.0), {"BOT": {"sum": -30.0, "n": 2.0, "avg": -15.0}})
    lines = blocks[0].splitlines()
    assert len(lines) == 1 + len(mt.REALIZED_WINDOWS) + 2
    assert lines[-2].lstrip().startswith("open")
    assert "unrealized" in lines[-2]
    assert lines[-1].lstrip().startswith("Σall")


def test_combined_line_is_30d_closed_plus_open():
    # BR4H, measured 2026-08-07: -1995.6 closed (n=317) + 1907.7 open (n=53)
    # nets to -87.9 over 370 — the number the close-time windows cannot show.
    stats = {"BR4H": {"30d": {"sum": -1995.6, "n": 317.0, "avg": -6.30}}}
    block = mt._format_realized_pnl_blocks(stats, {"BR4H": {"sum": 1907.7, "n": 53.0, "avg": 35.99}})[0]
    combined = block.splitlines()[-1]
    assert "-87.9%" in combined
    assert "n=370" in combined
    assert "-0.24%" in combined  # Ø over the whole book, not over the closes


def test_empty_open_stats_still_renders_placeholder():
    # {} means "ticker fine, nothing open" and must NOT look like None.
    # A silent 0 here would read as "nothing running" — the exact misreading
    # this feature removes.
    lines = mt._format_realized_pnl_blocks(_closed(), {})[0].splitlines()
    assert lines[-2].rstrip().endswith("—")
    assert lines[-2].lstrip().startswith("open")
    assert "n=1" in lines[-1]  # Σall falls back to the closed part alone


def test_bot_with_only_an_open_book_still_appears():
    # A leg whose trades all closed longer than 30d ago but which still has
    # positions running has no entry in `stats` — it must not vanish.
    blocks = mt._format_realized_pnl_blocks({}, {"GHOST": {"sum": 50.0, "n": 1.0, "avg": 50.0}})
    assert len(blocks) == 1
    assert blocks[0].splitlines()[0] == "<b>GHOST</b>"
    assert blocks[0].splitlines()[1].rstrip().endswith("—")  # 8h window empty


def test_blocks_sorted_by_combined_sum_desc():
    # A bot that is deep red on closes but green on its open book must sort
    # above one that is red on both.
    stats = mt._aggregate_realized_pnl([("RECOVER", 2.0, -100.0), ("DEAD", 2.0, -50.0)])
    order = [
        b.splitlines()[0]
        for b in mt._format_realized_pnl_blocks(stats, {"RECOVER": {"sum": 400.0, "n": 1.0, "avg": 400.0}})
    ]
    assert order == ["<b>RECOVER</b>", "<b>DEAD</b>"]


def test_label_column_is_padded_uniformly():
    lines = mt._format_realized_pnl_blocks(_closed(), {"BOT": {"sum": 1.0, "n": 1.0, "avg": 1.0}})[0].splitlines()[1:]
    # every label sits in the same column, else the monospace block staggers
    assert {line.index(":") for line in lines} == {2 + mt._REALIZED_LABEL_W}


# ── open book: per-block total ──────────────────────────────────────────────


def test_block_total_sums_closed_and_open_over_all_bots():
    stats = mt._aggregate_realized_pnl([("A", 2.0, 100.0), ("B", 2.0, -40.0)])
    total = mt._format_block_total(
        "SHADOW", stats, {"A": {"sum": 10.0, "n": 1.0, "avg": 10.0}, "B": {"sum": 5.0, "n": 2.0, "avg": 2.5}}
    )
    lines = total.splitlines()
    assert lines[0] == "<b>── SHADOW TOTAL ──</b>"
    assert "+60.0%" in lines[1] and "n=2" in lines[1]  # closed 30d
    assert "+15.0%" in lines[2] and "n=3" in lines[2]  # open
    assert "+75.0%" in lines[3] and "n=5" in lines[3]  # Σall


def test_block_total_is_omitted_without_open_book():
    # Half a total is worse than no total: without the open book the footer
    # would claim to aggregate a book it only half covers.
    assert mt._format_block_total("ACTIVE", _closed(), None) is None


def test_block_total_is_omitted_when_block_is_empty():
    assert mt._format_block_total("RETIRED", {}, {}) is None


# ── open book: why the SQL entry-guard is load-bearing (review PR #283) ─────


def test_nan_entry_is_not_caught_by_the_python_guards():
    """The open-book queries MUST filter `entry > 0` in SQL, like the closed ones.

    A NULL entry arrives from pandas as NaN, and every downstream guard is
    NaN-permeable: `nan <= 0` is False, `abs(nan) > MAX_ABS_MOVE_PCT` is False,
    and `max(nan, -100.0)` returns nan. realized_pnl_pct therefore yields nan —
    NOT None — so add_open_row would accept the row and a single one would
    poison the bot's open/Σall line and the whole block total. This test pins
    the behaviour that makes the SQL filter necessary; delete the filter and
    the report silently prints nan.
    """
    from core.realized_pnl import realized_pnl_pct

    out = realized_pnl_pct("LONG", float("nan"), 100.0, [110.0], 0, 20)
    assert out is not None
    assert out != out  # nan


def test_open_book_queries_filter_non_positive_entries():
    import re

    src = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "23_market_tracker.py"),
        encoding="utf-8",
    ).read()
    assert re.search(r"FROM ai_signals\s*\n\s*WHERE entry1 > 0", src)
    assert re.search(r"FROM active_trades_master\s*\n\s*WHERE entry > 0", src)


def test_nan_poisons_an_aggregate_if_it_ever_gets_through():
    # Documents the blast radius the SQL guard prevents: sum() has no NaN
    # tolerance, so one bad row takes the bot line and the block total with it.
    stats = mt._aggregate_open_book([("BOT", 10.0), ("BOT", float("nan"))])
    assert stats["BOT"]["sum"] != stats["BOT"]["sum"]  # nan

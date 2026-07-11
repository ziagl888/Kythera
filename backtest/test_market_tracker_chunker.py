# backtest/test_market_tracker_chunker.py
"""
Unit tests for the market tracker's Telegram message chunker (P2.41).

Telegram rejects any message over 4096 chars; send_telegram only queues into
telegram_outbox and the dispatcher drops an over-limit message silently. The
per-bot performance post is split into several messages on ENTRY boundaries so
a bot/table entry is never torn across two messages.

The focus finding here (P2.41): a single block that alone exceeds the per-chunk
budget used to be emitted as one over-4096 chunk and then silently dropped. The
hard-split fallback must keep EVERY emitted chunk under the limit while never
losing content.

Run with: pytest backtest/test_market_tracker_chunker.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# core.config fails hard on missing secrets; the build machine ships an empty
# .env. Placeholders keep this test standalone — nothing here opens a socket.
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")


def _load_tracker():
    spec = importlib.util.spec_from_file_location(
        "market_tracker",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "23_market_tracker.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    # Pre-seed sys.modules with pandas (and, transitively, numpy) BEFORE the
    # patch.dict below. patch.dict snapshots sys.modules on entry and restores
    # it on exit, DELETING any key first imported inside the block. 23_market_tracker
    # imports pandas during exec_module; without this pre-import that first import
    # happens inside the patch and pandas/numpy get torn out of sys.modules on exit.
    # numpy's C-extensions do not survive a fresh re-import in the same process, so
    # every later-collected test that imports pandas (test_market_tracker_conn/opened)
    # would die with ImportError in numpy._core. Importing here puts them in the
    # snapshot, so they persist past the patch exit.
    import pandas  # noqa: F401
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

LIMIT = mt.TELEGRAM_TEXT_LIMIT
HDR_FIRST = "<pre>📊 HEAD 📊\n\nBot | 1h\n---\n"
HDR_CONT = "<pre>📊 HEAD (continued) 📊\n\nBot | 1h\n---\n"
FOOTER = "\n\n<b>Legend:</b> stuff</pre>"


def _all_under_limit(chunks: list[str]) -> bool:
    return all(len(c) <= LIMIT for c in chunks)


# ── Normal case: entries fit, nothing is torn ─────────────────────────────────


def test_small_input_single_chunk():
    blocks = ["BOT1\n  win 60%", "BOT2\n  win 55%"]
    chunks = mt._build_chunks(blocks, HDR_FIRST, HDR_CONT, FOOTER)
    assert len(chunks) == 1
    assert _all_under_limit(chunks)
    # Both entries present, header + footer present.
    assert "BOT1" in chunks[0] and "BOT2" in chunks[0]
    assert chunks[0].startswith(HDR_FIRST) and chunks[0].endswith(FOOTER)


def test_many_blocks_split_across_chunks_without_loss():
    # ~60 blocks of ~200 chars each → well over one 4096 message.
    blocks = [f"BOT{i:02d}\n  " + ("x" * 190) for i in range(60)]
    chunks = mt._build_chunks(blocks, HDR_FIRST, HDR_CONT, FOOTER)
    assert len(chunks) > 1, "should have spilled into multiple messages"
    assert _all_under_limit(chunks), "a chunk exceeded the Telegram limit"
    # Every block lands in exactly one chunk (no entry torn / dropped).
    joined = "\n".join(chunks)
    for i in range(60):
        assert f"BOT{i:02d}" in joined
    # Continued chunks carry the continued header, the first carries the full one.
    assert chunks[0].startswith(HDR_FIRST)
    assert all(c.startswith(HDR_CONT) for c in chunks[1:])


# ── The P2.41 finding: a single over-budget block must not blow the limit ─────


def test_single_oversized_single_line_block_is_hard_split():
    # One pathological entry whose single line dwarfs the budget. Pre-fix this
    # became one >4096 chunk that Telegram drops silently.
    giant = "X" * (LIMIT + 3000)
    chunks = mt._build_chunks([giant], HDR_FIRST, HDR_CONT, FOOTER)
    assert len(chunks) > 1
    assert _all_under_limit(chunks), "oversized block was not split under the limit"
    # No content lost: all the X's survive across chunks.
    total_x = sum(c.count("X") for c in chunks)
    assert total_x == LIMIT + 3000


def test_single_oversized_multiline_block_splits_on_lines():
    # A block far over budget, built from many short lines → should split on line
    # boundaries and keep each individual line intact.
    lines = [f"detail-line-{i:04d}" for i in range(400)]
    giant = "\n".join(lines)
    assert len(giant) > LIMIT
    chunks = mt._build_chunks([giant], HDR_FIRST, HDR_CONT, FOOTER)
    assert _all_under_limit(chunks)
    joined = "\n".join(chunks)
    for ln in lines:
        assert ln in joined, "a detail line was lost during the hard split"


def test_oversized_block_mixed_with_normal_blocks():
    blocks = [
        "BOT1\n  ok",
        "X" * (LIMIT + 500),  # pathological middle entry
        "BOT2\n  ok",
    ]
    chunks = mt._build_chunks(blocks, HDR_FIRST, HDR_CONT, FOOTER)
    assert _all_under_limit(chunks), "limit breached with an oversized block in the mix"
    joined = "\n".join(chunks)
    assert "BOT1" in joined and "BOT2" in joined
    assert sum(c.count("X") for c in chunks) == LIMIT + 500


# ── _hard_split_block invariants ──────────────────────────────────────────────


def test_hard_split_noop_when_under_budget():
    assert mt._hard_split_block("short", 100) == ["short"]
    assert mt._hard_split_block("", 100) == [""]


def test_hard_split_guards_nonpositive_budget():
    # Degenerate budget must not loop forever or crash — return the block as-is.
    assert mt._hard_split_block("anything", 0) == ["anything"]
    assert mt._hard_split_block("anything", -5) == ["anything"]


def test_hard_split_pieces_within_budget_and_lossless_on_lines():
    lines = [f"row{i:03d}" for i in range(200)]
    block = "\n".join(lines)
    budget = 100
    pieces = mt._hard_split_block(block, budget)
    assert all(len(p) <= budget for p in pieces), "a piece exceeded the budget"
    # Line-boundary split → rejoining with \n reconstructs the block exactly.
    assert "\n".join(pieces) == block


def test_hard_split_char_splits_a_single_overlong_line():
    line = "y" * 250
    budget = 100
    pieces = mt._hard_split_block(line, budget)
    assert all(len(p) <= budget for p in pieces)
    assert "".join(pieces) == line  # char split is contiguous


# ── Grouping helpers ──────────────────────────────────────────────────────────


def test_group_bot_entries_splits_on_blank_lines():
    src = ["BOT1", "  a", "", "BOT2", "  b", ""]
    assert mt._group_bot_entries(src) == ["BOT1\n  a", "BOT2\n  b"]


def test_group_table_entries_skips_header_and_separator():
    src = ["HEADER", "-------", "rowA", "", "rowB", ""]
    assert mt._group_table_entries(src) == ["rowA", "rowB"]


def test_group_table_entries_needs_two_leading_lines():
    assert mt._group_table_entries(["only-one"]) == []
    assert mt._group_table_entries([]) == []


def test_build_chunks_empty_input():
    assert mt._build_chunks([], HDR_FIRST, HDR_CONT, FOOTER) == []

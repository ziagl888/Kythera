# backtest/test_traded_targets.py
"""
Persisted targets != traded targets (T-2026-KYT-9050-012).

The realized position model splits the stake into `n` equal legs with
n = len(targets) as stored in the DB. That is only correct while an emitter
persists exactly what it posts to Cornix. Two do not:

    ROM1  28_signal_orchestrator:525/574  persists t_cands[:20], posts 3
    AIM2  15_ai_master_bot:544/589        persists the full list, posts 3

Cornix never saw the rest, so the stake rode on 3 legs, not 20. The model
diluted the TP profit by (n-k)/n and understated both bots — measured over
7 769 closed ROM1 trades (30 days): factor 1.43 on the sum, median 1.51 %
instead of 5.18 %.

What these tests hold down:
  * the trim happens BEFORE the leg count is taken (n IS the position model);
  * `targets_hit` cannot exceed the traded legs, so a trade never gets credit
    for a TP Cornix never had (139 of those 7 769 trades had targets_hit > 3);
  * every OTHER emitter is untouched — this is a two-name carve-out, not a
    fleet-wide behaviour change;
  * omitting `model` reproduces the old numbers byte-for-byte, so no existing
    caller changes silently.

Run with: pytest backtest/test_traded_targets.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.realized_pnl import (  # noqa: E402
    PUBLISHED_TARGET_COUNT,
    realized_pnl_pct,
    traded_targets,
    weighted_move_pct,
)

# A LONG from 100 with four ladder targets; close back at entry.
ENTRY = 100.0
TARGETS = [110.0, 120.0, 130.0, 140.0]


# ── traded_targets ───────────────────────────────────────────────────────────


def test_rom1_and_aim2_are_trimmed_to_what_cornix_received():
    assert traded_targets("ROM1", TARGETS) == [110.0, 120.0, 130.0]
    assert traded_targets("AIM2", TARGETS) == [110.0, 120.0, 130.0]


def test_every_other_emitter_is_untouched():
    for tag in ("MIS1-72H", "BR2H", "BB_4H", "RUB2", "EPD3", "AIM2-TOPN", "SRA2"):
        assert traded_targets(tag, TARGETS) == TARGETS, f"{tag} must not be trimmed"


def test_lookup_is_case_and_whitespace_insensitive():
    assert traded_targets(" rom1 ", TARGETS) == TARGETS[:3]
    assert traded_targets("Aim2", TARGETS) == TARGETS[:3]


def test_none_and_empty_model_are_no_ops():
    assert traded_targets(None, TARGETS) == TARGETS
    assert traded_targets("", TARGETS) == TARGETS


def test_shorter_list_than_the_cap_is_returned_whole():
    assert traded_targets("ROM1", [110.0, 120.0]) == [110.0, 120.0]


def test_the_carve_out_stays_small():
    """A guard against this quietly growing into a fleet-wide special case:
    every entry here is a real persist/publish gap verified in the emitter."""
    assert set(PUBLISHED_TARGET_COUNT) == {"ROM1", "AIM2"}


# ── the leg count IS the position model ──────────────────────────────────────


def test_trim_changes_the_leg_count_not_just_the_list():
    """Two of four targets hit, close back at entry.

    Untrimmed: (10 + 20 + 0 + 0) / 4 = 7.5
    Trimmed  : (10 + 20 + 0)     / 3 = 10.0
    """
    untrimmed = weighted_move_pct("LONG", ENTRY, ENTRY, TARGETS, 2)
    trimmed = weighted_move_pct("LONG", ENTRY, ENTRY, TARGETS, 2, "ROM1")
    assert untrimmed == 7.5
    assert trimmed == 10.0
    assert trimmed > untrimmed, "the whole point: dilution over phantom legs understated the bot"


def test_targets_hit_cannot_exceed_the_traded_legs():
    """A monitor scoring TP4 on a ROM1 trade credits a TP Cornix never posted."""
    trimmed = weighted_move_pct("LONG", ENTRY, ENTRY, TARGETS, 4, "ROM1")
    # k is capped at 3 traded legs: (10 + 20 + 30) / 3
    assert trimmed == 20.0


def test_omitting_the_model_reproduces_the_old_numbers():
    for k in range(0, 5):
        assert weighted_move_pct("LONG", ENTRY, 105.0, TARGETS, k) == weighted_move_pct(
            "LONG", ENTRY, 105.0, TARGETS, k, None
        )


def test_short_side_is_trimmed_the_same_way():
    shorts = [90.0, 80.0, 70.0, 60.0]
    untrimmed = weighted_move_pct("SHORT", ENTRY, ENTRY, shorts, 2)
    trimmed = weighted_move_pct("SHORT", ENTRY, ENTRY, shorts, 2, "ROM1")
    assert untrimmed == 7.5
    assert trimmed == 10.0


# ── realized_pnl_pct forwards it ─────────────────────────────────────────────


def test_realized_pnl_applies_leverage_after_the_trim():
    assert realized_pnl_pct("LONG", ENTRY, ENTRY, TARGETS, 2, 10, "ROM1") == 100.0
    assert realized_pnl_pct("LONG", ENTRY, ENTRY, TARGETS, 2, 10) == 75.0


def test_liquidation_floor_still_clamps_after_the_trim():
    losers = [90.0, 80.0, 70.0, 60.0]
    out = realized_pnl_pct("LONG", ENTRY, 50.0, losers, 0, 20, "ROM1")
    assert out == -100.0


def test_an_empty_target_list_is_still_none_not_a_zero():
    assert weighted_move_pct("LONG", ENTRY, ENTRY, [], 0, "ROM1") is None
    assert realized_pnl_pct("LONG", ENTRY, ENTRY, [], 0, 10, "ROM1") is None

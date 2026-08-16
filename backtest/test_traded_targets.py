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
    every entry here is a real persist/publish gap verified in the emitter.

    After T-099 (ROM1), T-100 (AIM2) and T-147 (EPD2: the legacy leg persisted
    its raw pool of up to 20 while Cornix published 3 — closed at the source by
    the thin+cap) all gaps are closed, so these entries are historical-only
    decoders for the archive. A FURTHER name appearing here would mean an
    emitter regressed into persisting more than it publishes — which `backtest/test_published_targets.py` exists to prevent.
    """
    assert set(PUBLISHED_TARGET_COUNT) == {"ROM1", "AIM2", "EPD2"}
    assert all(n == 3 for n in PUBLISHED_TARGET_COUNT.values())


def test_rom1_stays_correct_after_the_bot_side_fix():
    """T-2026-KYT-9050-099 closed the ROM1 gap at the source (28 now persists the
    published slice). The ROM1 entry must NOT be dropped for being "fixed":

      * rows written before that deploy still carry the 20-target pool, whose
        posted ladder is its first three — the trim is still required;
      * rows written after it carry exactly 3 — the trim is identity.

    So one lookup is right on both eras and no report needs a cutoff date.
    Dropping the entry would re-inflate every historical row back to a 20-leg
    position model (the 1.41x understatement, with the sign flipped).
    """
    legacy_row = TARGETS  # persisted 20 (here 4), posted the first three
    new_row = [110.0, 120.0, 130.0]  # persisted == posted
    assert traded_targets("ROM1", legacy_row) == [110.0, 120.0, 130.0]
    assert traded_targets("ROM1", new_row) == new_row
    # The measurement is the same for both, which is the whole point.
    assert weighted_move_pct("LONG", ENTRY, ENTRY, legacy_row, 2, model="ROM1") == weighted_move_pct(
        "LONG", ENTRY, ENTRY, new_row, 2, model="ROM1"
    )


def test_aim2_stays_correct_after_the_bot_side_fix():
    """T-2026-KYT-9050-100 closed the AIM2 gap the same way T-099 closed ROM1's:
    15_ai_master_bot now persists `targets[:n_show]`. Both entries are therefore
    historical-only — and both must stay, for the same reason.

    Measured before the fix: 89.4 % of AIM2 rows persisted more than the 3 published
    targets, 46 % persisted 10. Pruning the entry would score those rows as a
    10-leg position model against a stake that really rode on 3.
    """
    legacy_row = TARGETS  # persisted the full calculate_smart_targets list
    new_row = [110.0, 120.0, 130.0]  # persisted == posted
    assert traded_targets("AIM2", legacy_row) == [110.0, 120.0, 130.0]
    assert traded_targets("AIM2", new_row) == new_row
    assert weighted_move_pct("LONG", ENTRY, ENTRY, legacy_row, 2, model="AIM2") == weighted_move_pct(
        "LONG", ENTRY, ENTRY, new_row, 2, model="AIM2"
    )


def test_a_thin_only_the_message_variant_would_have_been_wrong():
    """Documents the trap this carve-out sits on: `traded_targets` takes the FIRST
    three PERSISTED targets. Had ROM1 thinned only what it posts while persisting
    the raw pool, the posted ladder [101.0, 103.4, 106.5] would have been scored as
    [101.0, 101.4, 101.8] — wrong prices, silently, on the highest-volume leg.
    """
    raw_pool = [101.0, 101.4, 101.8, 103.4, 103.9, 106.5]
    posted_if_thinned_late = [101.0, 103.4, 106.5]
    assert traded_targets("ROM1", raw_pool) != posted_if_thinned_late
    assert traded_targets("ROM1", raw_pool) == [101.0, 101.4, 101.8]


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

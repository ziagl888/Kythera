"""core/trailing_roster.py — which legs the trailing bot mirrors, and on what terms.

The register behind ``40_trailing_close_bot.py``. Cornix caps a channel at 500
simultaneously open trades, so the trailing channel is a scarce resource and the
roster is the answer to "who earns a seat" — decided in PR #198
(``tools/trailing_slot_budget.py`` → ``staging_models/replay/trailing_slot_budget_live.json``)
and picked by the operator (Michi, 2026-07-26) at the p95-safe operating point.

This is a REGISTER, not a computation: the numbers were produced once, reviewed, and
chosen. Re-deriving them at bot start would tie a live process to a research artifact
that a later sweep can overwrite with different parameters.

Provenance of every entry below: ``fills_by_act["2.0"]["p95"]["accepted"]``, ordered
by net result per occupied slot-day (``density_trail``) — the same order the greedy
fill used, which is why it is also the eviction order when the slot cap binds.

The register is NOT the last word on whether a leg is mirrored. ``shadow_gate`` is:
a leg that has since left LIVE is skipped even while it stands here (spec AK2). The
roster can only ever be more restrictive than the fleet, never less.

Invariants:
  * Keys are ``(pretty_name(model), direction)`` — the same identity
    ``tools/trailing_slot_budget.py`` scored, so a roster hit means exactly the leg
    that earned the seat.
  * ``density`` is strictly descending: the greedy fill accepted in this order, so
    slot-cap eviction in this order is the same decision run backwards.
  * The roster is the mirror set only; the live/shadow decision stays in
    ``core.shadow_gate``.
"""

from __future__ import annotations

from core.bot_naming import pretty_name

# ─────────────────────────────────────────────────────────────────────────────
# OPERATING POINT (operator decision, Michi 2026-07-26)
# ─────────────────────────────────────────────────────────────────────────────

#: Peak (unlevered %) a trade must reach before the trail arms. Without a floor a
#: scale-free trail is a micro-scalper — "give back 10 % of the peak" fires on a
#: +0.5 % peak and 15m noise closes almost everything inside one candle. That is
#: WHY trailing looked so strong in T-041/T-046: the variance collapses.
ACTIVATION_PCT = 2.0

#: Fraction of the running peak that, once given back, closes the trade.
RETRACE_FRAC = 0.10

#: Cornix' per-channel cap on simultaneously open trades (operator).
SLOT_CAP = 500

#: What the chosen selection is expected to draw, from the same run. Reported at
#: boot so a live occupancy that drifts far from this is visible, not silent.
EXPECTED_OCC_MEAN = 284.6
EXPECTED_OCC_P95 = 498.0

#: Report the roster was cut from — for the operator, and so a later regeneration
#: is traceable to the numbers that were actually decided on.
SOURCE_REPORT = "staging_models/replay/trailing_slot_budget_live.json"
SOURCE_SELECTION = 'fills_by_act["2.0"]["p95"]["accepted"]'
SOURCE_GENERATED_AT = "2026-07-26T14:21:06Z"

# ─────────────────────────────────────────────────────────────────────────────
# THE 33 LEGS (density-descending == greedy-accept order == eviction order)
# ─────────────────────────────────────────────────────────────────────────────

#: ``(tag, direction) -> net % per occupied slot-day`` from the chosen run.
ROSTER: dict[tuple[str, str], float] = {
    ("MIS2-72h", "SHORT"): 311.003,
    ("MIS2-168h", "SHORT"): 309.752,
    ("MIS2-24h", "SHORT"): 275.374,
    ("ABR1", "LONG"): 12.954,
    ("MIS1-8h", "SHORT"): 6.062,
    ("XSM1", "LONG"): 4.708,
    ("AIM2", "SHORT"): 4.294,
    ("ABR1", "SHORT"): 4.135,
    ("RUB1", "SHORT"): 3.943,
    ("MIS1-24h", "LONG"): 2.875,
    ("SKW1", "SHORT"): 2.380,
    ("RUB1", "LONG"): 2.346,
    ("EPD1", "SHORT"): 2.238,
    ("SRA2", "SHORT"): 1.852,
    ("UFI1", "SHORT"): 1.770,
    ("SKW1", "LONG"): 1.591,
    ("AIM2", "LONG"): 1.409,
    ("MIS1-72h", "LONG"): 0.959,
    ("TD_4H", "LONG"): 0.885,
    ("TD_4H", "SHORT"): 0.748,
    ("TD_1H", "LONG"): 0.745,
    ("QM_1H", "LONG"): 0.686,
    ("MAX1", "SHORT"): 0.656,
    ("TD_1H", "SHORT"): 0.607,
    ("SRA2", "LONG"): 0.575,
    ("BB_4H", "LONG"): 0.568,
    ("BR1H", "LONG"): 0.560,
    ("BR4H", "LONG"): 0.549,
    ("MIS1-168h", "LONG"): 0.541,
    ("QM_4H", "LONG"): 0.352,
    ("ATS2", "LONG"): 0.054,
    # FIF2 vol-gated ladder mirror (T-2026-KYT-9050-115, operator decision Michi
    # 2026-08-07). Like ODS1 below, an UNMEASURED leg: the PR #198 slot-budget run
    # predates it, so no `density_trail` exists for it and these two values are
    # placeholders below every measured leg, not estimates. Above ODS1 because the
    # operator rates the live book higher; the ordering between two placeholders is
    # a judgement, and it only decides which of the two unmeasured legs yields a
    # seat first. Re-derive both from live rows once FIF2 has its own book.
    #
    # FIF2 is a RE-FORWARDER, which is what got ROM1 struck from this register
    # (EXCLUDED_AS_DUPLICATE below), so the seat needs the distinction spelled out.
    # ROM1 re-posted the SAME trades the original legs already fed into this channel
    # and double-counted 10 334 % of a 49 204 % headline. FIF2 overlaps only where
    # the source leg is itself rostered, and there `admit()` resolves it: candidates
    # are sorted by density and the second one on a symbol is rejected SYMBOL_HELD,
    # so the measured leg wins and the FIF2 copy is dropped. What the seat actually
    # buys is the complement — a vol-gated admission path for legs that never earned
    # a seat (EPD3, TSM1, BB_1H, BR2H, FIF1), which reach the trailing channel only
    # through this leg.
    #
    # The seat is what made `REENTRY_LOCK_H` necessary in 40_trailing_close_bot:
    # a re-forwarded row carries a NEW ai_signals.id, so the bot's src_signal_id
    # re-entry lock does not recognise it as the trade it just trailed out of.
    ("FIF2", "LONG"): 0.012,
    ("FIF2", "SHORT"): 0.011,
    # ODS1 OI-divergence short (T-2026-KYT-9050-106, operator decision Michi
    # 2026-08-05). Seated at the BOTTOM on purpose: every other density above
    # was MEASURED by the PR #198 slot-budget run, and this column doubles as the
    # eviction order when the 500-slot cap binds. A leg with no measured density
    # must be the first to yield a seat, not the last — entering it high would let
    # an unmeasured leg evict measured ones the moment the channel fills, and
    # would quietly break the register's provenance (every other value traces to
    # `fills_by_act["2.0"]["p95"]["accepted"]`).
    #
    # The value is a placeholder BELOW the lowest measured leg, not an estimate.
    # Re-derive it from live rows once ODS1 has its own book, then re-sort.
    #
    # Why this leg belongs here at all rather than only in its own channel: T-096
    # measured the edge as a horizon return (+0.41 % @1h, +0.73 % @4h) with no
    # stop. A TP/SL bracket cannot express that; `TIME_STOP_H` can, and the
    # trailing bot is the only place in the fleet that has one.
    ("ODS1", "SHORT"): 0.010,
}

#: ROM1 removed from the roster (T-2026-KYT-9050-052, operator decision Michi
#: 2026-07-29): bot 28 is a RE-FORWARDER — its rows are the same trades the
#: original legs already post, carrying the ORIGINAL signal's open_time. In the
#: PR #198 selection it double-counted 10 334 % of the 49 204 % headline; in the
#: live bot the 30 s freshness window happened to filter it (hours-old
#: open_time), which stopped being a safe accident once that window widened to
#: cover the fleet's real insert latency. The seats it held go to fresh signals.
EXCLUDED_AS_DUPLICATE: dict[tuple[str, str], float] = {
    ("ROM1", "LONG"): 2.791,
    ("ROM1", "SHORT"): 1.837,
}

#: Legs that earned their density but would have burst the cap, with the joint p95
#: they would have produced. Kept so the exclusion is a documented decision rather
#: than an absence — and so a later cap change has the candidates at hand.
REJECTED_FOR_CAP: dict[tuple[str, str], float] = {
    ("BB_1H", "LONG"): 518.0,
    ("BR2H", "LONG"): 528.0,
    ("EPD3", "LONG"): 581.0,
    ("TSM1", "SHORT"): 525.0,
}


def leg_key(model: str, direction: str) -> tuple[str, str]:
    """Roster identity of a raw ``ai_signals.model`` + direction.

    Goes through ``pretty_name`` because that is what the slot-budget study scored:
    the raw model column carries historic spellings (``MSI1-24h``, ``MIS1-168H_pump``)
    that all collapse onto one leg. Matching raw strings would silently miss them.
    """
    return pretty_name(model), (direction or "").strip().upper()


def is_rostered(model: str, direction: str) -> bool:
    """True if this leg earned a seat in the trailing channel."""
    return leg_key(model, direction) in ROSTER


def density(model: str, direction: str) -> float:
    """Net % per occupied slot-day; 0.0 for a leg outside the roster.

    This is the priority under the slot cap: when the channel is full, the leg with
    the lowest density is the one that should not have taken the seat.
    """
    return ROSTER.get(leg_key(model, direction), 0.0)

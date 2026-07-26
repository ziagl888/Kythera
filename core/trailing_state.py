"""core/trailing_state.py — the trailing rule as incremental live state.

``core.wave_exit_sim.trailing_tp_trigger`` decides the same thing in batch: it walks
a finished mark-to-market series and returns the index where the trade gave back
``x`` of its running peak. A live bot cannot use that shape — it sees one price at a
time and would have to re-walk a growing array on every poll, for every open trade.

So this module is the streaming form of exactly that rule, and the parity pin
``backtest/test_trailing_state.py::test_live_state_matches_wave_exit_sim`` binds the
two: feeding one mark series through ``TrailingState`` step by step must yield the
same trigger index as handing the whole series to ``trailing_tp_trigger``. That pin
is what makes this a second SHAPE of one rule rather than a second rule (Regel 7) —
if someone re-tunes the trailing semantics on either side, the pin goes red.

Everything here is unlevered percent
------------------------------------
``mark``, ``peak`` and ``activation`` are all unlevered percentage moves from the
entry, because that is the unit the slot-budget study swept and the unit the
operator chose ``act = 2 %`` in. Feeding a leveraged mark would arm the trail 20×
too early.

Invariants:
  * The peak is monotone non-decreasing over the life of a position.
  * The trail can only fire once the peak has exceeded ``activation``; a trade that
    was never that far in profit is never trailed out.
  * Peak update happens BEFORE the retrace test on the same mark — mirroring
    ``trailing_tp_trigger`` exactly, which is what the parity pin asserts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: What ``peak_pct`` means before the first mark has been seen.
NO_PEAK = -math.inf


def mark_pct(entry: float, price: float, is_long: bool) -> float:
    """Unlevered percentage move of an open position, signed by direction."""
    if entry <= 0:
        raise ValueError(f"entry must be positive, got {entry!r}")
    return ((price - entry) / entry * 100.0) if is_long else ((entry - price) / entry * 100.0)


@dataclass
class TrailingState:
    """Running trailing-close decision for ONE open position.

    ``peak_pct`` is the only field that has to survive a process restart: rebuilding
    it from the current mark would re-arm the trail below a peak the trade already
    gave back, and a position whose profit has evaporated would then never close —
    the exact failure the bot exists to prevent.
    """

    entry: float
    is_long: bool
    retrace_frac: float
    activation: float
    peak_pct: float = NO_PEAK

    @property
    def armed(self) -> bool:
        """True once the peak cleared the activation floor — the trail is live."""
        return self.peak_pct > self.activation

    def stop_pct(self) -> float | None:
        """The mark at which this position closes, or None while disarmed."""
        return self.peak_pct * (1.0 - self.retrace_frac) if self.armed else None

    def update(self, price: float) -> tuple[bool, float, bool]:
        """Feed one observed price.

        Returns ``(should_close, mark, peak_advanced)``. ``peak_advanced`` tells the
        caller this is one of the rare polls worth persisting: the peak is monotone,
        so only new highs change durable state, which keeps the write rate at a
        handful per position instead of one per poll per position.
        """
        mark = mark_pct(self.entry, price, self.is_long)
        peak_advanced = mark > self.peak_pct
        if peak_advanced:
            self.peak_pct = mark
        if self.armed and mark <= self.peak_pct * (1.0 - self.retrace_frac):
            return True, mark, peak_advanced
        return False, mark, peak_advanced
